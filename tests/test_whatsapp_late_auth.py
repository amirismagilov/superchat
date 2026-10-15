"""Actual worker + WAHA adapter, SQLite and in-memory HTTP; no live sessions."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

from cryptography.fernet import Fernet
import httpx
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import worker
from app.adapters.whatsapp_web import WhatsAppWebAdapter
from app.db import Account, Base, Conversation, Job, Message
from app.security import seal, unseal


SESSION = "omni-" + "c" * 32
IDENTITY = {"id": "79990000000@c.us", "phone": "+79990000000"}


@pytest.fixture
def store(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    sessions = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(worker, "Session", sessions)
    monkeypatch.setattr(worker.settings, "encryption_key", Fernet.generate_key().decode())
    monkeypatch.setenv("ENABLE_EXPERIMENTAL", "true")
    config = {"_waha_session": SESSION, "_waha_wait_deadline": 0,
              "wa_self": IDENTITY, "_waha_cursors": {"kept": {"watermark": 5}},
              "private_setting": "preserve"}
    with sessions.begin() as db:
        account = Account(tenant_id="company", owner_id="manager", channel="whatsapp_web",
                          label="WA", state="action_required",
                          public={"error": "waha_connect_timeout", "provider_state": "STARTING"},
                          encrypted_config=seal(config))
        db.add(account)
        db.flush()
        conversation = Conversation(account_id=account.id, peer="79991111111@c.us", title="Customer")
        db.add(conversation)
        db.flush()
        db.add(Message(conversation_id=conversation.id, text="Keep history", direction="inbound",
                       status="received", provider_id="old-message"))
        account_id = account.id
    yield SimpleNamespace(Session=sessions, account_id=account_id, config=deepcopy(config))
    engine.dispose()


class Steps:
    """Advance only the worker cadence clock, without sleeping or changing asyncio."""
    def __init__(self, count, on_wait=None):
        self.count, self.tick, self.stopped, self.on_wait = count, 0, False, on_wait

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    async def wait(self):
        self.tick += 1
        if self.on_wait:
            self.on_wait(self.tick)
        if self.tick >= self.count:
            self.set()
            return True
        raise TimeoutError


async def run_loop(store, monkeypatch, provider, *, steps=7, on_wait=None, ownership=None):
    stop = Steps(steps, on_wait)
    monkeypatch.setattr(worker, "time", SimpleNamespace(monotonic=lambda: stop.tick * 3.0))
    requests, created = [], []

    def handler(request):
        requests.append((stop.tick, request.method, request.url.path))
        if request.url.path.endswith(("/chats", "/chats/all/messages")):
            return httpx.Response(200, json=[])
        return provider(request, stop.tick)

    def factory(account, config):
        created.append(deepcopy(config))
        adapter = WhatsAppWebAdapter(config)
        adapter.http = httpx.AsyncClient(base_url="http://waha.invalid", transport=httpx.MockTransport(handler))
        return adapter

    monkeypatch.setattr(worker, "adapter_for", factory)
    await asyncio.wait_for(worker.account_loop(store.account_id, ownership or Mock(), stop, asyncio.Semaphore(1)), 3)
    with store.Session() as db:
        account = db.get(Account, store.account_id)
        result = SimpleNamespace(state=account.state, public=account.public,
                                 config=unseal(account.encrypted_config),
                                 history=[m.text for m in db.scalars(select(Message))])
    return result, requests, created


@pytest.mark.asyncio
async def test_late_verified_login_is_observed_at_15_seconds_without_restarting_session(store, monkeypatch):
    def provider(request, tick):
        return httpx.Response(200, json={"status": "STARTING"} if tick < 5 else
                              {"status": "WORKING", "me": {"id": IDENTITY["id"]}})

    result, requests, _ = await run_loop(store, monkeypatch, provider)
    assert result.state == "connected"
    assert result.public["identity"]["phone"] == IDENTITY["phone"]
    assert [(tick, method) for tick, method, path in requests if path == f"/api/sessions/{SESSION}"] == [(0, "GET"), (5, "GET")]
    assert all(method == "GET" for _, method, _ in requests)
    assert "_waha_wait_deadline" not in result.config
    for key in ("_waha_session", "wa_self", "_waha_cursors", "private_setting"):
        assert result.config[key] == store.config[key]
    assert result.history == ["Keep history"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["FAILED", "STOPPED"])
async def test_provider_terminal_state_stops_late_monitor(store, monkeypatch, status):
    result, requests, _ = await run_loop(store, monkeypatch,
        lambda request, tick: httpx.Response(200, json={"status": status}))
    assert result.state == "action_required"
    assert result.public["error"] == "waha_session_" + status.lower()
    assert requests == [(0, "GET", f"/api/sessions/{SESSION}")]
    assert result.config["_waha_session"] == SESSION
    assert result.history == ["Keep history"]


@pytest.mark.asyncio
async def test_unverified_working_identity_never_connects_or_extends_deadline(store, monkeypatch):
    def provider(request, tick):
        return httpx.Response(404) if request.url.path.endswith("/me") else httpx.Response(200, json={"status": "WORKING"})
    result, requests, _ = await run_loop(store, monkeypatch, provider)
    assert result.state == "action_required"
    assert result.public["error"] == "waha_connect_timeout"
    assert "identity" not in result.public
    assert result.config == store.config
    assert [tick for tick, _, path in requests if path.endswith("/me")] == [0, 5]


@pytest.mark.asyncio
async def test_identity_mismatch_stops_monitor_and_preserves_history_binding(store, monkeypatch):
    result, requests, _ = await run_loop(store, monkeypatch,
        lambda request, tick: httpx.Response(200, json={"status": "WORKING", "me": {"id": "79998888888@c.us"}}))
    assert result.state == "reauth_required"
    assert result.public["error"] == "whatsapp_account_identity_changed"
    assert requests == [(0, "GET", f"/api/sessions/{SESSION}")]
    assert result.config["wa_self"] == IDENTITY
    assert result.history == ["Keep history"]


@pytest.mark.asyncio
async def test_transient_status_error_retains_monitor_and_rebuilds_committed_adapter(store, monkeypatch):
    def provider(request, tick):
        return httpx.Response(503) if tick == 0 else httpx.Response(200, json={"status": "WORKING", "me": {"id": IDENTITY["id"]}})
    result, requests, created = await run_loop(store, monkeypatch, provider)
    assert result.state == "connected"
    assert len(created) == 2 and created[0] == created[1] == store.config
    assert [tick for tick, _, path in requests if path == f"/api/sessions/{SESSION}"] == [0, 5]
    assert all(method == "GET" for _, method, _ in requests)


@pytest.mark.asyncio
async def test_explicit_disconnect_job_is_processed_before_next_status_check(store, monkeypatch):
    def enqueue(tick):
        if tick == 1:
            with store.Session.begin() as db:
                db.add(Job(account_id=store.account_id, action="disconnect", encrypted_payload=seal({})))

    def provider(request, tick):
        return httpx.Response(200, json={"status": "STARTING"} if request.method == "GET" else {})

    result, requests, _ = await run_loop(store, monkeypatch, provider, on_wait=enqueue)
    assert result.state == "disconnected"
    assert requests == [(0, "GET", f"/api/sessions/{SESSION}"),
                        (1, "POST", f"/api/sessions/{SESSION}/logout"),
                        (1, "DELETE", f"/api/sessions/{SESSION}")]
    with store.Session() as db:
        job = db.scalar(select(Job))
        assert job.status == "done" and job.encrypted_payload is None
    assert result.config["wa_self"] == IDENTITY
    assert result.history == ["Keep history"]


@pytest.mark.asyncio
@pytest.mark.parametrize("state,error,channel", [("disconnected", "waha_connect_timeout", "whatsapp_web"),
                                                ("action_required", "waha_session_failed", "whatsapp_web"),
                                                ("action_required", "waha_connect_timeout", "telegram")])
async def test_late_monitor_is_scoped_to_whatsapp_wait_timeout(store, monkeypatch, state, error, channel):
    with store.Session.begin() as db:
        account = db.get(Account, store.account_id)
        account.state, account.channel, account.public = state, channel, {"error": error}
    _, requests, _ = await run_loop(store, monkeypatch,
        lambda request, tick: pytest.fail("Inactive account must not contact provider"))
    assert requests == []


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["connecting", "qr_required", "awaiting_qr"])
async def test_normal_pending_states_keep_normal_poll_cadence(store, monkeypatch, state):
    with store.Session.begin() as db:
        account = db.get(Account, store.account_id)
        account.state, account.public = state, {}
        config = unseal(account.encrypted_config)
        config["_waha_wait_deadline"] = 9_999_999_999
        account.encrypted_config = seal(config)
    result, requests, _ = await run_loop(store, monkeypatch,
        lambda request, tick: httpx.Response(200, json={"status": "STARTING"}), steps=3)
    assert result.state == "connecting"
    assert [tick for tick, _, _ in requests] == [0, 1, 2]
    assert result.config["_waha_wait_deadline"] == 9_999_999_999


@pytest.mark.asyncio
async def test_lost_ownership_during_late_check_cannot_commit_success(store, monkeypatch):
    ownership = Mock()
    def provider(request, tick):
        ownership.check.side_effect = worker.OwnershipLost("worker_ownership_lost")
        return httpx.Response(200, json={"status": "WORKING", "me": {"id": IDENTITY["id"]}})
    result, requests, _ = await run_loop(store, monkeypatch, provider, ownership=ownership)
    assert len(requests) == 1
    assert result.state == "action_required"
    assert result.public["error"] == "waha_connect_timeout"
    assert result.config == store.config
