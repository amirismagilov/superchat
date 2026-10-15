"""Durability and ownership tests at the actual worker/database boundary."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from cryptography.fernet import Fernet
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import worker
from app.adapters.base import AuthResult, Event, ProviderError
from app.db import Account, Base, Conversation, Job, Message, now
from app.security import seal, unseal
from app.services import aware


@pytest.fixture
def worker_store(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "Session", session)
    monkeypatch.setattr(worker.settings, "encryption_key", Fernet.generate_key().decode())
    with session.begin() as db:
        account = Account(tenant_id="company", owner_id="manager", channel="telegram",
                          label="Corporate", state="connected", encrypted_config=seal({"cursor": 1}))
        db.add(account)
        db.flush()
        conversation = Conversation(account_id=account.id, peer="42", title="Customer")
        db.add(conversation)
        db.flush()
        account_id, conversation_id = account.id, conversation.id
    yield SimpleNamespace(Session=session, account_id=account_id, conversation_id=conversation_id)
    engine.dispose()


def fake_adapter(config=None):
    return SimpleNamespace(
        config=config if config is not None else {"cursor": 1},
        start=AsyncMock(), close=AsyncMock(),
        poll=AsyncMock(return_value=[]),
        send=AsyncMock(return_value="provider-101"),
        auth=AsyncMock(return_value=AuthResult("connected")),
    )


def enqueue(store, *, status="queued"):
    with store.Session.begin() as db:
        msg = Message(conversation_id=store.conversation_id, text="Hello", direction="outbound",
                      status=status, client_id="a3cb7ddf-f54d-45d1-a877-562d536638aa")
        db.add(msg)
        db.flush()
        return msg.id


@pytest.mark.asyncio
async def test_poll_failure_rolls_back_both_events_and_cursor(worker_store, monkeypatch):
    a = fake_adapter()

    async def poll():
        a.config["cursor"] = 2
        return [Event("101", "42", "first"), Event("102", "42", "second")]

    a.poll.side_effect = poll
    actual_ingest = worker.ingest

    def fail_second(db, account, event):
        actual_ingest(db, account, event)
        if event.provider_id == "102":
            raise RuntimeError("simulated transaction failure")

    monkeypatch.setattr(worker, "ingest", fail_second)
    with pytest.raises(RuntimeError):
        await worker.poll_account(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        assert list(db.scalars(select(Message))) == []
        assert unseal(db.get(Account, worker_store.account_id).encrypted_config)["cursor"] == 1


@pytest.mark.asyncio
async def test_account_loop_recreates_adapter_from_committed_cursor_after_failure(worker_store, monkeypatch):
    stop = asyncio.Event()
    created = []
    starting_configs = []

    def factory(account, config):
        a = fake_adapter(config)
        starting_configs.append(deepcopy(config))
        created.append(a)

        async def poll():
            a.config["cursor"] = 2
            if len(created) == 2:
                stop.set()
            return [Event("101", "42", "replayable")]

        a.poll.side_effect = poll
        return a

    actual_ingest = worker.ingest
    attempts = 0

    def fail_once(db, account, event):
        nonlocal attempts
        attempts += 1
        actual_ingest(db, account, event)
        if attempts == 1:
            raise RuntimeError("simulated commit failure")

    monkeypatch.setattr(worker, "adapter_for", factory)
    monkeypatch.setattr(worker, "ingest", fail_once)
    monkeypatch.setattr(worker, "INTERVAL", 0.001)
    await asyncio.wait_for(worker.account_loop(worker_store.account_id, Mock(), stop,
                                               asyncio.Semaphore(1)), timeout=2)
    assert starting_configs == [{"cursor": 1}, {"cursor": 1}]
    created[0].close.assert_awaited_once()
    with worker_store.Session() as db:
        assert len(list(db.scalars(select(Message)))) == 1
        assert unseal(db.get(Account, worker_store.account_id).encrypted_config)["cursor"] == 2


def test_recovery_never_requeues_inflight_send_and_erases_interrupted_auth_payload(worker_store):
    message_id = enqueue(worker_store, status="sending")
    with worker_store.Session.begin() as db:
        job = Job(account_id=worker_store.account_id, action="password", status="running",
                  encrypted_payload=seal({"password": "one-time-sensitive-data"}))
        db.add(job)
        db.flush()
        job_id = job.id
    worker.recover_account(worker_store.account_id)
    with worker_store.Session() as db:
        message = db.get(Message, message_id)
        assert message.status == "uncertain"
        assert message.error == "worker_interrupted_during_send"
        job = db.get(Job, job_id)
        assert job.status == "failed"
        assert job.encrypted_payload is None


@pytest.mark.asyncio
async def test_known_flood_wait_is_queued_with_backoff_without_early_retry(worker_store):
    message_id = enqueue(worker_store)
    a = fake_adapter()
    a.send.side_effect = ProviderError("telegram_flood_wait", retry_after=60)
    before = now()
    assert await worker.process_outbox(worker_store.account_id, a, Mock()) is True
    with worker_store.Session() as db:
        message = db.get(Message, message_id)
        assert message.status == "queued"
        assert message.attempts == 1
        assert aware(message.next_attempt_at) >= before + timedelta(seconds=60)
    assert await worker.process_outbox(worker_store.account_id, a, Mock()) is False
    a.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_unknown_send_failure_is_uncertain_and_never_retried(worker_store):
    message_id = enqueue(worker_store)
    a = fake_adapter()
    a.send.side_effect = TimeoutError("a credential must not appear in stored error")
    await worker.process_outbox(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        message = db.get(Message, message_id)
        assert message.status == "uncertain"
        assert message.error == "provider_result_unknown"
    assert await worker.process_outbox(worker_store.account_id, a, Mock()) is False
    a.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_outbound_commit_failure_preserves_uncertainty_instead_of_resending(worker_store, monkeypatch):
    message_id = enqueue(worker_store)
    a = fake_adapter()
    actual_seal = worker.seal
    seals = 0

    def fail_once(config):
        nonlocal seals
        seals += 1
        if seals == 1:
            raise RuntimeError("simulated failed outbound transaction")
        return actual_seal(config)

    monkeypatch.setattr(worker, "seal", fail_once)
    await worker.process_outbox(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        message = db.get(Message, message_id)
        assert message.status == "uncertain"
        assert message.provider_id is None
    assert await worker.process_outbox(worker_store.account_id, a, Mock()) is False
    a.send.assert_awaited_once()


@pytest.mark.asyncio
async def test_auth_payload_is_erased_before_network_and_failed_job_leaks_no_secret(worker_store):
    with worker_store.Session.begin() as db:
        job = Job(account_id=worker_store.account_id, action="password",
                  encrypted_payload=seal({"password": "not-for-logs"}))
        db.add(job)
        db.flush()
        job_id = job.id
    a = fake_adapter()

    async def auth(action, payload):
        assert action == "password" and payload["password"] == "not-for-logs"
        with worker_store.Session() as db:
            job = db.get(Job, job_id)
            assert job.status == "running"
            assert job.encrypted_payload is None
        raise RuntimeError("not-for-logs")

    a.auth.side_effect = auth
    await worker.process_job(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed"
        assert job.error == "provider_auth_error"
        assert job.encrypted_payload is None
        assert "not-for-logs" not in str(db.get(Account, worker_store.account_id).public)


def test_invalidated_ownership_connection_is_not_transparently_reconnected():
    ownership = worker.Ownership()
    ownership.connection = Mock(invalidated=True)
    with pytest.raises(RuntimeError, match="worker_ownership_connection_lost"):
        ownership.check()
    ownership.connection.execute.assert_not_called()


def test_ownership_refresh_does_not_take_reentrant_lock_on_already_owned_shard(monkeypatch):
    ownership = worker.Ownership()
    ownership.connection = Mock(invalidated=False)
    ownership.connection.scalar.return_value = True
    monkeypatch.setattr(ownership, "_claim_epoch", Mock(), raising=False)
    monkeypatch.setattr(worker, "SHARDS", [0, 1])
    ownership.refresh()
    ownership.refresh()
    assert ownership.shards == {0, 1}
    assert ownership.connection.scalar.call_count == 2


@pytest.mark.asyncio
async def test_poll_checks_ownership_again_after_network_before_committing(worker_store):
    a = fake_adapter()
    a.config["cursor"] = 2
    a.poll.return_value = [Event("101", "42", "must not commit from former owner")]
    ownership = Mock()
    ownership.check.side_effect = [None, RuntimeError("worker_ownership_connection_lost")]
    with pytest.raises(RuntimeError, match="worker_ownership_connection_lost"):
        await worker.poll_account(worker_store.account_id, a, ownership)
    with worker_store.Session() as db:
        assert list(db.scalars(select(Message))) == []
        assert unseal(db.get(Account, worker_store.account_id).encrypted_config)["cursor"] == 1


@pytest.mark.asyncio
async def test_auth_losing_ownership_does_not_save_mutated_session(worker_store):
    with worker_store.Session.begin() as db:
        job = Job(account_id=worker_store.account_id, action="login", encrypted_payload=seal({}))
        db.add(job)
        db.flush()
        job_id = job.id
    a = fake_adapter({"cursor": 2, "session": "new-uncommitted-session"})
    ownership = Mock()
    ownership.check.side_effect = [None, worker.OwnershipLost("worker_ownership_connection_lost")]
    with pytest.raises(worker.OwnershipLost):
        await worker.process_job(worker_store.account_id, a, ownership)
    with worker_store.Session() as db:
        assert unseal(db.get(Account, worker_store.account_id).encrypted_config) == {"cursor": 1}
        job = db.get(Job, job_id)
        assert job.status == "running"  # Recovered by the next legitimate owner.
        assert job.encrypted_payload is None


@pytest.mark.asyncio
async def test_send_losing_ownership_leaves_durable_inflight_marker_for_recovery(worker_store):
    message_id = enqueue(worker_store)
    a = fake_adapter({"cursor": 2})
    ownership = Mock()
    ownership.check.side_effect = [None, worker.OwnershipLost("worker_ownership_connection_lost")]
    with pytest.raises(worker.OwnershipLost):
        await worker.process_outbox(worker_store.account_id, a, ownership)
    with worker_store.Session() as db:
        assert unseal(db.get(Account, worker_store.account_id).encrypted_config) == {"cursor": 1}
        assert db.get(Message, message_id).status == "sending"
    worker.recover_account(worker_store.account_id)
    with worker_store.Session() as db:
        assert db.get(Message, message_id).status == "uncertain"


@pytest.mark.asyncio
async def test_read_receipt_during_send_wins_over_later_transport_timeout(worker_store):
    message_id = enqueue(worker_store)
    a = fake_adapter()

    async def send(*args):
        with worker_store.Session.begin() as db:
            message = db.get(Message, message_id)
            message.status = "read"
            message.provider_id = "confirmed-provider-id"
        raise ProviderError("transport_timeout", uncertain=True)

    a.send.side_effect = send
    await worker.process_outbox(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        message = db.get(Message, message_id)
        assert message.status == "read"
        assert message.provider_id == "confirmed-provider-id"
        assert message.error is None
    assert await worker.process_outbox(worker_store.account_id, a, Mock()) is False


@pytest.mark.asyncio
async def test_echo_merge_does_not_regress_read_to_delivered(worker_store):
    message_id = enqueue(worker_store)
    a = fake_adapter()

    async def send(*args):
        with worker_store.Session.begin() as db:
            db.get(Message, message_id).status = "read"
            db.add(Message(conversation_id=worker_store.conversation_id, text="echo", direction="outbound",
                           status="delivered", provider_id="provider-101"))
        return "provider-101"

    a.send.side_effect = send
    await worker.process_outbox(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        assert db.get(Message, message_id).status == "read"
        assert len(list(db.scalars(select(Message)))) == 1


@pytest.mark.asyncio
async def test_revoked_telegram_session_changes_account_state_and_stops_polling(worker_store, monkeypatch):
    stop = asyncio.Event()
    a = fake_adapter()
    a.poll.side_effect = ProviderError("telegram_session_expired")
    a.close.side_effect = stop.set
    monkeypatch.setattr(worker, "adapter_for", Mock(return_value=a))
    await asyncio.wait_for(worker.account_loop(worker_store.account_id, Mock(), stop,
                                               asyncio.Semaphore(1)), timeout=2)
    with worker_store.Session() as db:
        account = db.get(Account, worker_store.account_id)
        assert account.state == "reauth_required"
        assert account.public["error"] == "telegram_session_expired"


@pytest.mark.asyncio
@pytest.mark.parametrize("channel,state,error,extra", [
    ("telegram", "awaiting_code", "telegram_code_invalid", {}),
    ("max_personal", "code_required", "max_code_invalid", {"code_length": 6, "hint": "Work account"}),
])
async def test_wrong_code_preserves_retry_step_and_scrubs_completed_job_payload(worker_store, channel, state, error, extra):
    with worker_store.Session.begin() as db:
        account = db.get(Account, worker_store.account_id)
        account.channel, account.state = channel, state
        account.public = {"phone": "+79990000001", "auth_method": "phone", "code_delivery": "telegram_or_sms", **extra}
        job = Job(account_id=account.id, action="code", encrypted_payload=seal({"code": "12345"}))
        db.add(job)
        db.flush()
        job_id = job.id
    a = fake_adapter()
    a.auth.side_effect = ProviderError(error)
    assert await worker.process_job(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        account = db.get(Account, worker_store.account_id)
        assert account.state == state
        assert account.public == {"phone": "+79990000001", "auth_method": "phone",
                                  "code_delivery": "telegram_or_sms", "error": error, **extra}
        job = db.get(Job, job_id)
        assert job.status == "failed" and job.encrypted_payload is None


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["telegram_account_identity_changed", "whatsapp_account_identity_changed", "max_account_identity_changed"])
async def test_identity_change_blocks_outbox_even_if_previous_state_was_connected(worker_store, code):
    enqueue(worker_store)
    with worker_store.Session.begin() as db:
        db.add(Job(account_id=worker_store.account_id, action="login", encrypted_payload=seal({})))
    a = fake_adapter()
    a.auth.side_effect = ProviderError(code)
    await worker.process_job(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        assert db.get(Account, worker_store.account_id).state == "reauth_required"
    assert await worker.process_outbox(worker_store.account_id, a, Mock()) is False
    a.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_disconnect_uses_revoke_path_even_after_identity_mismatch(worker_store):
    message_id = enqueue(worker_store)
    with worker_store.Session.begin() as db:
        account = db.get(Account, worker_store.account_id)
        account.channel, account.state = "max_personal", "reauth_required"
        db.add(Job(account_id=account.id, action="disconnect", encrypted_payload=seal({})))
    adapter = fake_adapter()
    adapter.start.side_effect = ProviderError("max_account_identity_changed")
    adapter.auth.return_value = AuthResult("disconnected")
    assert await worker.process_job(worker_store.account_id, adapter, Mock())
    adapter.start.assert_not_awaited()
    adapter.auth.assert_awaited_once_with("disconnect", {})
    with worker_store.Session() as db:
        assert db.get(Account, worker_store.account_id).state == "disconnected"
        assert db.get(Message, message_id).status == "failed"


@pytest.mark.asyncio
async def test_max_explicit_qr_can_recover_expired_token_before_prestart(worker_store):
    with worker_store.Session.begin() as db:
        account = db.get(Account, worker_store.account_id)
        account.channel, account.state = "max_personal", "reauth_required"
        db.add(Job(account_id=account.id, action="qr", encrypted_payload=seal({})))
    adapter = fake_adapter()
    adapter.start.side_effect = ProviderError("max_session_expired")
    adapter.auth.return_value = AuthResult("qr_required", {"auth_method": "qr"})
    assert await worker.process_job(worker_store.account_id, adapter, Mock())
    adapter.start.assert_not_awaited()
    adapter.auth.assert_awaited_once_with("qr", {})
    with worker_store.Session() as db:
        assert db.get(Account, worker_store.account_id).state == "qr_required"


@pytest.mark.asyncio
async def test_startup_failure_finishes_queued_login_and_retry_reuses_account(worker_store, monkeypatch):
    """Regression: start used to fail before the worker could even claim a job."""
    stop = asyncio.Event()
    with worker_store.Session.begin() as db:
        account = db.get(Account, worker_store.account_id)
        account.state = "disconnected"
        original_config = account.encrypted_config
        job = Job(account_id=account.id, action="login", encrypted_payload=seal({"phone": "+79990000001"}))
        db.add(job)
        db.flush()
        first_id = job.id
    a = fake_adapter()

    async def failed_start():
        with worker_store.Session() as db:
            job = db.get(Job, first_id)
            assert job.status == "running", "Network connection must happen after durable job claim"
            assert job.encrypted_payload is None, "One-time payload must be scrubbed before connecting"
        stop.set()
        raise ProviderError("telegram_transport_error")

    a.start.side_effect = failed_start
    monkeypatch.setattr(worker, "adapter_for", Mock(return_value=a))
    await asyncio.wait_for(worker.account_loop(worker_store.account_id, Mock(), stop, asyncio.Semaphore(1)), timeout=2)
    a.auth.assert_not_awaited()
    with worker_store.Session() as db:
        job = db.get(Job, first_id)
        assert job.status == "failed" and job.error == "telegram_transport_error"
        assert job.encrypted_payload is None
        account = db.get(Account, worker_store.account_id)
        assert account.state == "disconnected"
        assert account.public == {"error": "telegram_transport_error"}
        assert unseal(account.encrypted_config) == unseal(original_config)
        assert db.get(Conversation, worker_store.conversation_id) is not None

    # An explicit retry keeps the same account/history and clears the old error
    # only after the provider confirms the next step.
    a.start.side_effect = None
    a.auth.return_value = AuthResult("awaiting_code", {"phone": "+79990000001", "auth_method": "phone"})
    with worker_store.Session.begin() as db:
        retry = Job(account_id=worker_store.account_id, action="login", encrypted_payload=seal({}))
        db.add(retry)
        db.flush()
        retry_id = retry.id
    await worker.process_job(worker_store.account_id, a, Mock())
    with worker_store.Session() as db:
        assert db.get(Job, retry_id).status == "done"
        account = db.get(Account, worker_store.account_id)
        assert account.state == "awaiting_code" and "error" not in account.public
        assert len(list(db.scalars(select(Account)))) == 1
        assert db.get(Conversation, worker_store.conversation_id) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["start", "auth"])
async def test_auth_start_timeout_is_terminal_and_scrubs_payload(worker_store, monkeypatch, phase):
    with worker_store.Session.begin() as db:
        db.get(Account, worker_store.account_id).state = "awaiting_password"
        job = Job(account_id=worker_store.account_id, action="password", encrypted_payload=seal({"password": "never-return-me"}))
        db.add(job)
        db.flush()
        job_id = job.id
    a = fake_adapter()
    cancelled = asyncio.Event()

    async def blocked_start(*args):
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    getattr(a, phase).side_effect = blocked_start
    monkeypatch.setattr(worker, "AUTH_JOB_TIMEOUT", 0.01)
    assert await worker.process_job(worker_store.account_id, a, Mock())
    assert cancelled.is_set()
    if phase == "start":
        a.auth.assert_not_awaited()
    else:
        a.auth.assert_awaited_once()
    with worker_store.Session() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed" and job.error == "provider_auth_timeout"
        assert job.encrypted_payload is None
        account = db.get(Account, worker_store.account_id)
        assert account.state == "awaiting_password"
        assert account.public["error"] == "provider_auth_timeout"
        assert "never-return-me" not in str(account.public)


@pytest.mark.asyncio
async def test_expired_queued_job_fails_without_attempting_network(worker_store):
    with worker_store.Session.begin() as db:
        job = Job(account_id=worker_store.account_id, action="code", encrypted_payload=seal({"code": "12345"}),
                  created_at=now() - timedelta(minutes=11))
        db.add(job)
        db.flush()
        job_id = job.id
    a = fake_adapter()
    a.start.side_effect = ProviderError("telegram_transport_error")
    assert await worker.process_job(worker_store.account_id, a, Mock())
    a.start.assert_not_awaited()
    a.auth.assert_not_awaited()
    with worker_store.Session() as db:
        job = db.get(Job, job_id)
        assert job.status == "failed" and job.error == "auth_job_expired" and job.encrypted_payload is None


@pytest.mark.asyncio
async def test_idle_disconnected_account_does_not_open_provider_connection(worker_store, monkeypatch):
    with worker_store.Session.begin() as db:
        db.get(Account, worker_store.account_id).state = "disconnected"
    stop = asyncio.Event()
    a = fake_adapter()

    async def no_jobs(*args):
        stop.set()
        return False

    monkeypatch.setattr(worker, "adapter_for", Mock(return_value=a))
    monkeypatch.setattr(worker, "process_job", no_jobs)
    await asyncio.wait_for(worker.account_loop(worker_store.account_id, Mock(), stop, asyncio.Semaphore(1)), timeout=2)
    a.start.assert_not_awaited()
    a.auth.assert_not_awaited()
    a.poll.assert_not_awaited()
