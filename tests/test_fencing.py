"""A replaced worker must not commit provider state or outbox results."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
import json
import os
from pathlib import Path
import threading
import time

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import worker
from app.adapters.base import AuthResult, Event, ProviderError
from app.db import Account, Base, Conversation, Job, Message, ShardLease
from app.security import seal, unseal


@pytest.fixture
def fenced_store(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "Session", sessions)
    monkeypatch.setattr(worker.settings, "encryption_key", Fernet.generate_key().decode())
    with sessions.begin() as db:
        account = Account(tenant_id="tenant", owner_id="agent", channel="telegram",
                          label="Work", state="connected", encrypted_config=seal({"cursor": 1}))
        db.add(account)
        db.flush()
        conversation = Conversation(account_id=account.id, peer="123", title="Customer")
        db.add(conversation)
        db.flush()
        message = Message(conversation_id=conversation.id, client_id=str(uuid4()),
                          text="Hello", direction="outbound", status="queued")
        db.add(message)
        db.flush()
        account_id, conversation_id, message_id = account.id, conversation.id, message.id
    owner = worker.Ownership()
    shard = worker.shard_for(account_id)
    owner._claim_epoch(shard)
    owner.shards.add(shard)
    yield SimpleNamespace(Session=sessions, account_id=account_id,
                          conversation_id=conversation_id, message_id=message_id,
                          owner=owner, shard=shard)
    engine.dispose()


def replace_owner(store):
    replacement = worker.Ownership()
    replacement._claim_epoch(store.shard)
    replacement.shards.add(store.shard)
    return replacement


def test_new_generation_rejects_stale_worker_before_any_mutation(fenced_store):
    store = fenced_store
    replacement = replace_owner(store)
    assert replacement.epochs[store.shard] != store.owner.epochs[store.shard]
    with pytest.raises(worker.OwnershipLost, match="fencing_epoch_changed"):
        with store.Session.begin() as db:
            store.owner.fence(db, store.account_id)
            db.get(Account, store.account_id).state = "disconnected"
    with store.Session() as db:
        assert db.get(Account, store.account_id).state == "connected"
    assert store.owner.lost is True


def test_lost_connection_denies_recovery_even_before_takeover(fenced_store):
    store = fenced_store
    with store.Session.begin() as db:
        db.get(Message, store.message_id).status = "sending"
    store.owner.connection = Mock(invalidated=True)
    with pytest.raises(worker.OwnershipLost):
        worker.recover_account(store.account_id, store.owner)
    with store.Session() as db:
        assert db.get(Message, store.message_id).status == "sending"


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_takeover_during_send_cannot_commit_result_or_error(fenced_store, provider_fails):
    store = fenced_store
    adapter = SimpleNamespace(config={"cursor": 1})
    replacement = None

    async def send(*args):
        nonlocal replacement
        replacement = replace_owner(store)
        adapter.config["cursor"] = 99
        if provider_fails:
            raise ProviderError("provider_server_error", uncertain=True)
        return "provider-1"

    adapter.send = AsyncMock(side_effect=send)
    with pytest.raises(worker.OwnershipLost):
        await worker.process_outbox(store.account_id, adapter, store.owner)
    with store.Session() as db:
        message = db.get(Message, store.message_id)
        assert message.status == "sending"
        assert message.provider_id is None
        assert unseal(db.get(Account, store.account_id).encrypted_config) == {"cursor": 1}
    worker.recover_account(store.account_id, replacement)
    with store.Session() as db:
        assert db.get(Message, store.message_id).status == "uncertain"


@pytest.mark.asyncio
async def test_takeover_during_poll_keeps_committed_cursor_and_messages(fenced_store):
    store = fenced_store
    adapter = SimpleNamespace(config={"cursor": 1})

    async def poll():
        replace_owner(store)
        adapter.config["cursor"] = 2
        return [Event("incoming-1", "123", "Replay me after takeover")]

    adapter.poll = AsyncMock(side_effect=poll)
    with pytest.raises(worker.OwnershipLost):
        await worker.poll_account(store.account_id, adapter, store.owner)
    with store.Session() as db:
        assert db.scalar(select(Message).where(Message.provider_id == "incoming-1")) is None
        assert unseal(db.get(Account, store.account_id).encrypted_config) == {"cursor": 1}


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_fails", [False, True])
async def test_takeover_during_auth_cannot_replace_new_owners_session(fenced_store, provider_fails):
    store = fenced_store
    with store.Session.begin() as db:
        job = Job(account_id=store.account_id, action="login", encrypted_payload=seal({}))
        db.add(job)
        db.flush()
        job_id = job.id
    adapter = SimpleNamespace(config={"cursor": 1})

    async def auth(*args):
        replace_owner(store)
        adapter.config["session"] = "old-worker-session"
        if provider_fails:
            raise ProviderError("provider_unauthorized")
        return AuthResult("connected")

    adapter.auth = AsyncMock(side_effect=auth)
    with pytest.raises(worker.OwnershipLost):
        await worker.process_job(store.account_id, adapter, store.owner)
    with store.Session() as db:
        account = db.get(Account, store.account_id)
        assert account.state == "connected"
        assert unseal(account.encrypted_config) == {"cursor": 1}
        assert db.get(Job, job_id).status == "running"
        assert db.get(Job, job_id).encrypted_payload is None


def test_failed_takeover_does_not_publish_epoch(fenced_store):
    store = fenced_store
    replacement = worker.Ownership()
    replacement.check = Mock(side_effect=worker.OwnershipLost("connection_lost"))
    with pytest.raises(worker.OwnershipLost):
        replacement._claim_epoch(store.shard)
    assert replacement.epochs == {}
    with store.Session() as db:
        assert db.get(ShardLease, store.shard).epoch == store.owner.epochs[store.shard]


def test_postgres_takeover_waits_for_mutation_commit_and_rejects_old_epoch(monkeypatch):
    """Optional integration test using a disposable PostgreSQL credential file.

    FENCING_POSTGRES_URL_FILE points to JSON containing {"url": "..."}.
    Uses its own random schema and advisory shard, never the deployment's shards.
    """
    config_path = os.getenv("FENCING_POSTGRES_URL_FILE")
    if not config_path:
        pytest.skip("FENCING_POSTGRES_URL_FILE not provided")
    url = json.loads(Path(config_path).read_text())["url"]
    schema = "fencing_test_" + uuid4().hex
    application = "fencing_test_" + uuid4().hex[:16]
    pg_engine = create_engine(url, pool_size=6, max_overflow=2,
                              connect_args={"application_name": application})
    scoped_engine = pg_engine.execution_options(schema_translate_map={None: schema})
    with pg_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    Base.metadata.create_all(scoped_engine)
    sessions = sessionmaker(scoped_engine, expire_on_commit=False)
    shard = 2_000_000_000 + int(uuid4().hex[:6], 16)
    monkeypatch.setattr(worker, "Session", sessions)
    monkeypatch.setattr(worker, "SHARDS", [shard])
    monkeypatch.setattr(worker, "shard_for", lambda account_id: shard)
    original = worker.Ownership()
    replacement = worker.Ownership()
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        with sessions.begin() as db:
            account = Account(tenant_id="tenant", owner_id="owner", channel="telegram",
                              label="Fencing", state="connected", encrypted_config="unused")
            db.add(account)
            db.flush()
            account_id = account.id
        original.connection = pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
        original.refresh()
        old_epoch = original.epochs[shard]
        backend_pid = original.connection.scalar(text("SELECT pg_backend_pid()"))
        takeover_started = threading.Event()

        def takeover():
            replacement.connection = pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT")
            takeover_started.set()
            replacement.refresh()

        with sessions.begin() as db:
            original.fence(db, account_id)
            db.get(Account, account_id).public = {"committed_before_takeover": True}
            with pg_engine.connect().execution_options(isolation_level="AUTOCOMMIT") as control:
                assert control.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": backend_pid})
            future = pool.submit(takeover)
            assert takeover_started.wait(timeout=5)
            # Observe an actual PostgreSQL lock wait, not just thread scheduling.
            deadline = time.monotonic() + 5
            waiting = False
            while time.monotonic() < deadline:
                with pg_engine.connect() as control:
                    waiting = bool(control.scalar(text("""
                        SELECT EXISTS (
                            SELECT 1 FROM pg_stat_activity
                            WHERE application_name = :application
                              AND wait_event_type = 'Lock'
                              AND query LIKE '%shard_leases%'
                        )
                    """), {"application": application}))
                if waiting:
                    break
                time.sleep(0.02)
            assert waiting, "Replacement never reached the fencing row lock"
            assert not future.done()
            assert replacement.epochs == {}
        future.result(timeout=5)
        assert replacement.epochs[shard] != old_epoch
        # Even if a connection liveness check passed before the race, epoch
        # validation itself must reject writes from the previous generation.
        monkeypatch.setattr(original, "check", lambda: None)
        with pytest.raises(worker.OwnershipLost, match="fencing_epoch_changed"):
            with sessions.begin() as db:
                original.fence(db, account_id)
                db.get(Account, account_id).public = {"stale_write": True}
        with sessions() as db:
            assert db.get(Account, account_id).public == {"committed_before_takeover": True}
    finally:
        pool.shutdown(wait=True)
        for owner in (original, replacement):
            with suppress(Exception):
                owner.close()
        with pg_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        pg_engine.dispose()
