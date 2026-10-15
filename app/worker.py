"""Persistent outbox + single-owner provider sessions.

PostgreSQL session locks own stable shards, not 300 pooled account connections.
Uncertain outbound operations are never blindly retried.
"""
import asyncio
import hashlib
import logging
import os
import signal
import time
from pathlib import Path
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import select, text, update

from .adapters.base import ProviderError
from .capabilities import adapter_for
from .config import settings
from .db import Account, Base, Conversation, Job, Message, Session, ShardLease, engine, now
from .security import seal, unseal
from .services import advance_status, aware, ingest, reconcile_receipts

log = logging.getLogger("omnicrm.worker")
SHARD_COUNT = int(os.getenv("WORKER_SHARD_COUNT", "16"))
SHARDS = [int(x) for x in os.getenv("WORKER_SHARDS", ",".join(map(str, range(SHARD_COUNT)))).split(",")]
INTERVAL = float(os.getenv("ACCOUNT_POLL_SECONDS", "3"))
AUTH_JOB_TIMEOUT = 90
WHATSAPP_LATE_AUTH_INTERVAL = 15


class OwnershipLost(RuntimeError):
    pass


def auth_expired(exc):
    return isinstance(exc, ProviderError) and exc.code in {"telegram_session_expired", "telegram_account_identity_changed", "whatsapp_account_identity_changed", "max_account_identity_changed", "provider_unauthorized", "telegram_auth_required", "max_session_expired", "waha_session_expired"}


def auth_error_public(account, code):
    # Keep only the fields needed to retry a phone/code/password step. Do not
    # accidentally publish config, stale QR login tokens or raw provider errors.
    public = account.public or {}
    retained = {key: public[key] for key in ("phone", "auth_method", "code_delivery") if key in public}
    if account.channel == "max_personal":
        if type(public.get("code_length")) is int and 1 <= public["code_length"] <= 12:
            retained["code_length"] = public["code_length"]
        if isinstance(public.get("hint"), str):
            retained["hint"] = public["hint"][:160]
    return {**retained, "error": code[:96]}


def shard_for(account_id):
    return int.from_bytes(hashlib.sha256(account_id.encode()).digest()[:8], "big") % SHARD_COUNT


class Ownership:
    def __init__(self):
        self.connection = None
        self.shards = set()
        self.epochs = {}
        self.lost = False
        self.file = None

    def start(self):
        if engine.dialect.name == "postgresql":
            self.connection = engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        else:
            import fcntl
            self.file = open(".worker-local.lock", "a")
            fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.shards = set(range(SHARD_COUNT))
        self.refresh()

    def refresh(self):
        if self.connection is not None:
            self.check()
            for shard in SHARDS:
                if shard not in self.shards and self.connection.scalar(text("SELECT pg_try_advisory_lock(734128, :shard)"), {"shard": shard}):
                    self._claim_epoch(shard)
                    self.shards.add(shard)

    def _claim_epoch(self, shard):
        """Called only after acquiring the PostgreSQL advisory lock for shard."""
        epoch = str(uuid4())
        with Session.begin() as db:
            lease = db.scalar(select(ShardLease).where(ShardLease.shard == shard).with_for_update())
            # The prior owner may still hold its transaction's lease row lock.
            # Confirm our advisory connection survived the wait before taking over.
            self.check()
            if lease is None:
                db.add(ShardLease(shard=shard, epoch=epoch))
            else:
                lease.epoch = epoch
        self.epochs[shard] = epoch

    def fence(self, db, account_id):
        """Lock the generation until the caller's mutation transaction commits.

        The advisory lock chooses an owner. This row lock prevents an old owner
        from committing after a replacement owner has claimed a new generation.
        SQLite uses a process file lock; explicit epochs enable deterministic tests.
        """
        self.check()
        if db.get_bind().dialect.name == "sqlite" and not self.epochs:
            return
        shard = shard_for(account_id)
        expected = self.epochs.get(shard)
        if shard not in self.shards or not expected:
            self.lost = True
            raise OwnershipLost("worker_shard_not_owned")
        with db.no_autoflush:
            lease = db.scalar(select(ShardLease).where(ShardLease.shard == shard)
                              .with_for_update().execution_options(populate_existing=True))
        if lease is None or lease.epoch != expected:
            self.lost = True
            raise OwnershipLost("worker_fencing_epoch_changed")

    def check(self):
        if self.lost:
            raise OwnershipLost("worker_ownership_lost")
        if self.connection is not None:
            # Never reconnect transparently after losing locks: terminate this process.
            try:
                if self.connection.invalidated:
                    raise OwnershipLost("worker_ownership_connection_lost")
                self.connection.execute(text("SELECT 1"))
            except Exception as exc:
                self.lost = True
                raise OwnershipLost("worker_ownership_connection_lost") from exc

    def close(self):
        self.lost = True
        if self.connection is not None:
            try:
                if not self.connection.invalidated:
                    self.connection.execute(text("SELECT pg_advisory_unlock_all()"))
            finally:
                self.connection.close()
        if self.file:
            self.file.close()


def fence_transaction(db, ownership, account_id):
    if ownership is None:
        if db.get_bind().dialect.name != "sqlite":
            raise OwnershipLost("worker_fencing_owner_required")
        return
    ownership.fence(db, account_id)


def recover_account(account_id, ownership=None):
    with Session.begin() as db:
        fence_transaction(db, ownership, account_id)
        ids = select(Conversation.id).where(Conversation.account_id == account_id)
        db.execute(update(Message).where(Message.conversation_id.in_(ids), Message.status == "sending")
                   .values(status="uncertain", error="worker_interrupted_during_send"))
        db.execute(update(Job).where(Job.account_id == account_id, Job.status == "running")
                   .values(status="failed", error="worker_interrupted_retry_login", encrypted_payload=None))


async def process_job(account_id, adapter, ownership):
    with Session.begin() as db:
        fence_transaction(db, ownership, account_id)
        job = db.scalar(select(Job).where(Job.account_id == account_id, Job.status == "queued").order_by(Job.created_at).limit(1).with_for_update(skip_locked=True))
        if not job:
            return False
        if now() - aware(job.created_at) > timedelta(minutes=10):
            job.status, job.error, job.encrypted_payload = "failed", "auth_job_expired", None
            return True
        job.status = "running"
        job_id, action, payload = job.id, job.action, unseal(job.encrypted_payload)
        max_auth = db.get(Account, account_id).channel == "max_personal"
        # Auth codes/passwords never survive a completed or interrupted job.
        job.encrypted_payload = None
    ownership.check()
    try:
        # Claim the job and erase its durable one-time credentials BEFORE any
        # network connection. Otherwise a failed start leaves it queued forever,
        # bypassing even the expiry check above on every worker retry.
        async with asyncio.timeout(AUTH_JOB_TIMEOUT):
            # MAX auth owns its bootstrap: it must handle an expired token on
            # explicit login and revoke an accidentally linked other identity.
            if not max_auth:
                await adapter.start()
            ownership.check()
            result = await adapter.auth(action, payload)
        ownership.check()
        with Session.begin() as db:
            fence_transaction(db, ownership, account_id)
            a = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
            a.state, a.public = result.state, result.public
            a.encrypted_config, a.updated_at = seal(adapter.config), now()
            db.get(Job, job_id).status = "done"
            if action == "disconnect":
                ids = select(Conversation.id).where(Conversation.account_id == account_id)
                db.execute(update(Message).where(Message.conversation_id.in_(ids), Message.status == "queued")
                           .values(status="failed", error="account_disconnected"))
    except OwnershipLost:
        raise
    except Exception as exc:
        ownership.check()
        code = (exc.code if isinstance(exc, ProviderError) else
                "provider_auth_timeout" if isinstance(exc, TimeoutError) else "provider_auth_error")
        with Session.begin() as db:
            fence_transaction(db, ownership, account_id)
            job = db.get(Job, job_id)
            job.status, job.error = "failed", code[:96]
            a = db.get(Account, account_id)
            a.encrypted_config, a.updated_at = seal(adapter.config), now()
            a.public = auth_error_public(a, code)
            if auth_expired(exc):
                a.state = "reauth_required"
        log.warning("auth_failed account=%s code=%s", account_id, code)
    finally:
        payload.clear()
    return True


async def process_outbox(account_id, adapter, ownership):
    with Session.begin() as db:
        fence_transaction(db, ownership, account_id)
        a = db.get(Account, account_id)
        if a.state != "connected":
            return False
        m = db.scalar(select(Message).join(Conversation).where(Conversation.account_id == account_id, Message.status == "queued").order_by(Message.id).limit(1).with_for_update(skip_locked=True))
        if not m or aware(m.next_attempt_at) > now():
            return False
        c = db.get(Conversation, m.conversation_id)
        if a.channel == "whatsapp_cloud" and m.template is None and (not c.last_inbound_at or now() - aware(c.last_inbound_at) >= timedelta(hours=24)):
            m.status, m.error = "failed", "whatsapp_template_required_outside_24h"
            return True
        m.status, m.attempts = "sending", m.attempts + 1
        message_id, peer, body, client_id = m.id, c.peer, m.text, m.client_id
        template = m.template
    ownership.check()
    try:
        operation = adapter.send_template(peer, template, client_id) if template is not None else adapter.send(peer, body, client_id)
        provider_id = await asyncio.wait_for(operation, timeout=45)
        ownership.check()
        with Session.begin() as db:
            fence_transaction(db, ownership, account_id)
            a = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
            m = db.get(Message, message_id)
            # A webhook/native echo may have arrived while HTTPS was in flight.
            echo = db.scalar(select(Message).where(Message.conversation_id == m.conversation_id, Message.provider_id == str(provider_id), Message.id != m.id))
            status = m.status if m.status in {"delivered", "read", "failed"} else "sent"
            if echo:
                rank = {"sent": 1, "failed": 2, "delivered": 3, "read": 4}
                if rank.get(echo.status, 0) > rank.get(status, 0):
                    status = echo.status
                db.delete(echo)
                db.flush()
            m.provider_id, m.status, m.error = str(provider_id), status, None
            reconcile_receipts(db, a, m, peer)
            a.encrypted_config, a.updated_at = seal(adapter.config), now()
    except OwnershipLost:
        raise
    except Exception as exc:
        ownership.check()
        # Only an explicit provider response proving non-acceptance permits retry.
        ambiguous = not isinstance(exc, ProviderError) or exc.uncertain
        code = exc.code if isinstance(exc, ProviderError) else "provider_result_unknown"
        with Session.begin() as db:
            fence_transaction(db, ownership, account_id)
            a = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
            m = db.get(Message, message_id)
            if m.status in {"delivered", "read"}:
                m.error = None
                return True
            if isinstance(exc, ProviderError) and exc.retry_after is not None and not ambiguous and m.attempts < 5:
                m.status = "queued"
                m.next_attempt_at = now() + timedelta(seconds=max(1, min(exc.retry_after, 86400)))
            else:
                m.status = "uncertain" if ambiguous else "failed"
            m.error = code[:96]
            a.encrypted_config, a.updated_at = seal(adapter.config), now()
            if auth_expired(exc):
                a.state = "reauth_required"
        log.warning("send_failed account=%s message=%s code=%s", account_id, message_id, code)
    return True


async def poll_account(account_id, adapter, ownership):
    ownership.check()
    events = await asyncio.wait_for(adapter.poll(), timeout=45)
    ownership.check()
    with Session.begin() as db:
        fence_transaction(db, ownership, account_id)
        a = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
        for event in events:
            ingest(db, a, event)
        a.encrypted_config, a.updated_at = seal(adapter.config), now()


async def account_loop(account_id, ownership, stop, semaphore):
    try:
        ownership.check()
        recover_account(account_id, ownership)
    except OwnershipLost:
        stop.set()
        return
    adapter = None
    failures = 0
    next_late_auth_check = 0.0
    while not stop.is_set():
        late_auth_check = False
        try:
            async with semaphore:
                ownership.check()
                if adapter is None:
                    with Session() as db:
                        a = db.get(Account, account_id)
                        adapter = adapter_for(a, unseal(a.encrypted_config))
                    # Auth startup belongs inside process_job's claimed,
                    # bounded operation. Connected adapters open lazily in poll
                    # or send; idle/disconnected accounts do not connect here.
                await process_job(account_id, adapter, ownership)
                with Session() as db:
                    account = db.get(Account, account_id)
                    state = account.state
                    late_auth_check = (account.channel == "whatsapp_web"
                                       and state == "action_required"
                                       and (account.public or {}).get("error") == "waha_connect_timeout"
                                       and time.monotonic() >= next_late_auth_check)
                # A UI wait deadline is not a provider logout. Continue reading
                # the existing WAHA session so a late verified login is observed.
                # Keep processing jobs every cycle, including explicit disconnect.
                if state in {"connecting", "qr_required", "awaiting_qr"} or late_auth_check:
                    next_late_auth_check = time.monotonic() + WHATSAPP_LATE_AUTH_INTERVAL
                    result = await asyncio.wait_for(adapter.auth("sync", {}), timeout=45)
                    ownership.check()
                    with Session.begin() as db:
                        fence_transaction(db, ownership, account_id)
                        a = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
                        a.state, a.public = result.state, result.public
                        a.encrypted_config, a.updated_at = seal(adapter.config), now()
                    state = result.state
                    late_auth_check = False
                if state == "connected":
                    # Commit pending inbound/cursors before handling another send.
                    await poll_account(account_id, adapter, ownership)
                    await process_outbox(account_id, adapter, ownership)
                failures = 0
        except OwnershipLost:
            stop.set()
            break
        except asyncio.CancelledError:
            break
        except Exception as exc:
            # Reset from durable encrypted state on any rollback/cursor uncertainty.
            failures += 1
            code = exc.code if isinstance(exc, ProviderError) else type(exc).__name__
            log.warning("account_cycle_failed account=%s code=%s", account_id, code)
            if adapter:
                try:
                    await asyncio.wait_for(adapter.close(), timeout=5)
                except Exception:
                    pass
            adapter = None
            try:
                with Session.begin() as db:
                    fence_transaction(db, ownership, account_id)
                    a = db.get(Account, account_id)
                    if late_auth_check and not auth_expired(exc):
                        # A temporary status-read failure must not abandon the
                        # late-login monitor or replace its original deadline.
                        a.public = {**(a.public or {}), "error": "waha_connect_timeout",
                                    "status_check_error": str(code)[:96], "retrying": True}
                    else:
                        a.public = {"error": str(code)[:96], "retrying": True}
                    if auth_expired(exc):
                        a.state = "reauth_required"
            except OwnershipLost:
                stop.set()
                break
            except Exception:
                pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=min(INTERVAL * (2 ** min(failures, 5)), 60))
        except TimeoutError:
            pass
    if adapter:
        try:
            await asyncio.wait_for(adapter.close(), timeout=5)
        except Exception:
            pass


async def run():
    settings.validate()
    if settings.environment != "production":
        Base.metadata.create_all(engine)
    ownership = Ownership()
    stop = asyncio.Event()
    tasks = {}
    semaphore = asyncio.Semaphore(int(os.getenv("WORKER_CONCURRENCY", "32")))
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    ownership.start()
    try:
        while not stop.is_set():
            ownership.refresh()
            with Session() as db:
                ids = list(db.scalars(select(Account.id)))
            for account_id in ids:
                if shard_for(account_id) in ownership.shards and account_id not in tasks:
                    tasks[account_id] = asyncio.create_task(account_loop(account_id, ownership, stop, semaphore))
            Path(os.getenv("WORKER_HEARTBEAT_PATH", "/tmp/omnicrm-worker.heartbeat")).touch()
            try:
                await asyncio.wait_for(stop.wait(), timeout=2)
            except TimeoutError:
                pass
    finally:
        stop.set()
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        ownership.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    asyncio.run(run())
