"""History requests stay separate from account authentication and commit with data."""
from unittest.mock import AsyncMock, Mock

import pytest

from app import worker
from app.adapters.base import AuthResult, Event, ProviderError
from app.db import Account, Job
from app.security import seal, unseal
from test_worker import worker_store, fake_adapter


def queue(store, *, state="connected"):
    with store.Session.begin() as db:
        a = db.get(Account, store.account_id)
        a.state = state
        a.public = {"identity": {"id": "corporate-account"}}
        j = Job(account_id=a.id, action="history", encrypted_payload=seal({"history_days": 3650}))
        db.add(j)
        db.flush()
        return j.id


@pytest.mark.asyncio
async def test_history_request_does_not_login_or_change_verified_identity(worker_store):
    job_id = queue(worker_store)
    adapter = fake_adapter()

    async def request(payload):
        assert payload == {"history_days": 3650}
        adapter.config["history_status"] = {"state": "importing", "chats_discovered": 2,
                                            "history_days": 3650, "token": "private"}
        return AuthResult("connected", {"should_not_be_public": "private"})

    adapter.request_history = AsyncMock(side_effect=request)
    await worker.process_job(worker_store.account_id, adapter, Mock())
    adapter.auth.assert_not_awaited()
    adapter.start.assert_not_awaited()
    with worker_store.Session() as db:
        a = db.get(Account, worker_store.account_id)
        assert a.state == "connected"
        assert a.public == {"identity": {"id": "corporate-account"}, "history_sync": {
            "state": "importing", "chats_discovered": 2, "history_days": 3650}}
        assert unseal(a.encrypted_config)["history_status"]["token"] == "private"
        j = db.get(Job, job_id)
        assert j.status == "done" and j.encrypted_payload is None


@pytest.mark.asyncio
async def test_queued_history_rechecks_connected_state(worker_store):
    job_id = queue(worker_store, state="disconnected")
    adapter = fake_adapter()
    adapter.request_history = AsyncMock()
    await worker.process_job(worker_store.account_id, adapter, Mock())
    adapter.request_history.assert_not_awaited()
    with worker_store.Session() as db:
        j = db.get(Job, job_id)
        assert (j.status, j.error, j.encrypted_payload) == ("failed", "account_not_connected", None)


@pytest.mark.asyncio
async def test_failed_history_preserves_connection_and_identity(worker_store):
    job_id = queue(worker_store)
    adapter = fake_adapter()
    adapter.request_history = AsyncMock(side_effect=ProviderError("history_not_supported"))
    await worker.process_job(worker_store.account_id, adapter, Mock())
    with worker_store.Session() as db:
        a = db.get(Account, worker_store.account_id)
        assert a.state == "connected" and a.public["identity"]["id"] == "corporate-account"
        assert a.public["history_sync"] == {"state": "partial", "error": "history_not_supported"}
        assert db.get(Job, job_id).status == "failed"


@pytest.mark.asyncio
async def test_progress_rolls_back_with_failed_message_transaction(worker_store, monkeypatch):
    adapter = fake_adapter()
    adapter.config["history_status"] = {"state": "complete", "chats_completed": 50}
    adapter.poll.return_value = [Event("old-message", "42", "history", metadata={"historical": True})]
    monkeypatch.setattr(worker, "ingest", Mock(side_effect=RuntimeError("transaction failed")))
    with pytest.raises(RuntimeError):
        await worker.poll_account(worker_store.account_id, adapter, Mock())
    with worker_store.Session() as db:
        a = db.get(Account, worker_store.account_id)
        assert "history_sync" not in (a.public or {})
        assert "history_status" not in unseal(a.encrypted_config)


def test_progress_is_bounded_and_never_copies_arbitrary_provider_fields():
    assert worker.history_public({"history_status": {"state": "complete", "chats_discovered": True,
                                                   "chats_completed": -2, "history_days": "3650",
                                                   "session": "private"}}) == {"state": "complete"}
    assert worker.history_public({"history_status": {"state": "provider-token"}}) is None
