"""MAX auth runs the pinned SDK bootstrap with mocked transport/provider calls.

No accounts, provider connections, SQLite sessions, or real credentials.
"""
import asyncio
import copy
import importlib.metadata
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import pytest

from app.adapters import max_personal as maxmod
from app.adapters.base import ProviderError


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sdk(monkeypatch, tmp_path):
    assert importlib.metadata.version("maxapi-python") == "2.4.1"
    from pymax import Client, WebClient
    from pymax.api.auth.service import AuthService
    from pymax.app import App
    from pymax.types.domain.auth import RequestQrResponse, CheckQrResponse, CheckCodeResponse, StartAuthResponse
    from pymax.types.domain.login import LoginResponse
    from pymax.types.domain.handshake import HandshakeResponse

    monkeypatch.setenv("ENABLE_EXPERIMENTAL", "true")
    monkeypatch.chdir(tmp_path)
    clock = [100.0]
    monkeypatch.setattr(maxmod.time, "time", lambda: clock[0])

    class Transport:
        is_open = False

        def __init__(self):
            self.opens = self.closes = 0

        async def open(self):
            self.opens += 1
            self.is_open = True

        async def close(self):
            self.closes += 1
            self.is_open = False

    transports = []

    def connection(_client):
        transport = Transport()
        transports.append(transport)
        return transport

    async def ping(_app):
        await asyncio.Future()

    profile = {"contact": {"id": 11, "phone": 79990000000, "names": [{"name": "Manager"}]}}
    login = AsyncMock(return_value=LoginResponse.model_validate({"profile": profile}))
    request_qr = AsyncMock(return_value=RequestQrResponse.model_validate({
        "trackId": "private-track", "qrLink": "https://max.ru/qr/private-link",
        "expiresAt": 200000, "pollingInterval": 5000, "ttl": 100000,
    }))
    check_qr = AsyncMock(return_value=CheckQrResponse.model_validate({"status": {
        "expiresAt": 200000, "loginAvailable": False,
    }}))
    confirm_qr = AsyncMock(return_value=CheckCodeResponse.model_validate({
        "tokenAttrs": {"LOGIN": {"token": "private-session"}},
    }))
    request_code = AsyncMock(return_value=StartAuthResponse.model_validate({
        "token": "private-code-token", "codeLength": 6, "requestMaxDuration": 60000,
        "requestCountLeft": 3, "altActionDuration": 60000,
    }))
    send_code = AsyncMock(return_value=confirm_qr.return_value)
    check_password = AsyncMock(return_value=confirm_qr.return_value)
    logout = AsyncMock(return_value=True)
    for client in (Client, WebClient):
        monkeypatch.setattr(client, "_build_connection", connection)
        monkeypatch.setattr(client, "logout", logout)
    monkeypatch.setattr(App, "handshake", AsyncMock(return_value=HandshakeResponse()))
    monkeypatch.setattr(App, "_ping_loop", ping)
    for name, method in {"login": login, "request_qr": request_qr, "check_qr": check_qr,
                         "confirm_qr": confirm_qr, "request_code": request_code,
                         "send_code": send_code, "check_password": check_password}.items():
        monkeypatch.setattr(AuthService, name, method)
    return NS(clock=clock, transports=transports, login=login, request_qr=request_qr,
              check_qr=check_qr, confirm_qr=confirm_qr, request_code=request_code,
              send_code=send_code, check_password=check_password, logout=logout, path=tmp_path)


def test_qr_keeps_one_real_sdk_bootstrap_until_verified_login(sdk):
    from pymax.types.domain.auth import CheckQrResponse

    async def exercise():
        adapter = maxmod.MaxPersonalAdapter({})
        first = await adapter.auth("qr", {})
        assert first.state == "qr_required"
        assert first.public["polling_interval"] == 5
        assert first.public["expires_at"] == 200
        assert "qr_text" not in first.public
        client = adapter.client
        assert client.is_connected and client.me is None  # Socket alone is not success.
        assert sdk.transports[0].opens == 1 and sdk.transports[0].closes == 0
        pending = await adapter.auth("sync", {})
        assert pending.state == "qr_required" and adapter.client is client
        await adapter.auth("sync", {})  # Respect provider's 5-second polling interval.
        assert sdk.check_qr.await_count == 1
        sdk.clock[0] += 5
        sdk.check_qr.return_value = CheckQrResponse.model_validate({"status": {
            "expiresAt": 200000, "loginAvailable": True}})
        done = await adapter.auth("sync", {})
        assert done.state == "connected"
        assert done.public["identity"] == {"id": "11", "phone": "+79990000000", "display_name": "Manager"}
        assert adapter.client is client and client._app.started
        assert adapter.config["max_self"] == {"id": "11"}
        assert adapter.config["_max_session"]["token"] == "private-session"
        assert "_max_pending" not in adapter.config
        assert "private-" not in json.dumps(done.public)
        assert sdk.request_qr.await_count == sdk.confirm_qr.await_count == sdk.login.await_count == 1
        await adapter.close()
        assert not sdk.transports[0].is_open

    run(exercise())
    assert list(sdk.path.iterdir()) == []


def test_sms_wrong_code_and_2fa_retry_keep_transport_and_challenge(sdk, caplog):
    from pymax.exceptions import ApiError
    from pymax.types.domain.auth import CheckCodeResponse, CheckPasswordResponse
    sdk.send_code.side_effect = [
        ApiError(opcode=18, error="INVALID_CODE", message="private-provider-payload"),
        CheckCodeResponse.model_validate({"passwordChallenge": {"trackId": "private-2fa", "hint": "My hint"}}),
    ]
    sdk.check_password.side_effect = [
        CheckPasswordResponse.model_validate({"error": "private-password-error"}),
        CheckPasswordResponse.model_validate({"tokenAttrs": {"LOGIN": {"token": "private-session"}}}),
    ]

    async def exercise():
        adapter = maxmod.MaxPersonalAdapter({})
        code = await adapter.auth("login", {"login_method": "sms", "phone": "+79990000000"})
        assert code.state == "code_required" and code.public["phone"] == "+79990000000"
        client = adapter.client
        with pytest.raises(ProviderError, match="max_code_invalid"):
            await adapter.auth("code", {"code": "111111"})
        assert adapter.client is client and sdk.transports[0].closes == 0
        password = await adapter.auth("code", {"code": "222222"})
        assert password.state == "password_required" and password.public["hint"] == "My hint"
        with pytest.raises(ProviderError, match="max_password_invalid"):
            await adapter.auth("password", {"password": "private-wrong-password"})
        assert adapter.client is client
        assert (await adapter.auth("sync", {})).public["hint"] == "My hint"
        done = await adapter.auth("password", {"password": "private-password"})
        assert done.state == "connected"
        assert "private-password" not in json.dumps(adapter.config)
        assert "private-code-token" not in json.dumps(done.public)
        assert sdk.request_code.await_count == 1
        await adapter.close()

    run(exercise())
    assert "private-provider-payload" not in caplog.text
    assert "private-password" not in caplog.text


def test_pending_qr_resumes_after_worker_restart_and_expires(sdk):
    async def exercise():
        first = maxmod.MaxPersonalAdapter({})
        await first.auth("qr", {})
        config = copy.deepcopy(first.config)
        await first.close()
        restored = maxmod.MaxPersonalAdapter(config)
        assert (await restored.auth("sync", {})).state == "qr_required"
        assert sdk.request_qr.await_count == 1  # Existing track is resumed, not reissued.
        assert restored.config["_max_device_id"] == config["_max_device_id"]
        sdk.clock[0] = 201
        expired = await restored.auth("sync", {})
        assert expired.state == "login_required" and expired.public["reason"] == "qr_expired"
        assert "_max_pending" not in restored.config and restored.client is None
        assert sdk.confirm_qr.await_count == 0

    run(exercise())


def test_identity_pin_survives_disconnect_and_rejects_another_account(sdk):
    from pymax.types.domain.auth import CheckQrResponse
    from pymax.types.domain.login import LoginResponse
    sdk.check_qr.return_value = CheckQrResponse.model_validate({"status": {"expiresAt": 200000, "loginAvailable": True}})

    async def exercise():
        adapter = maxmod.MaxPersonalAdapter({})
        await adapter.auth("qr", {})
        await adapter.auth("sync", {})
        assert (await adapter.auth("disconnect", {})).state == "disconnected"
        assert adapter.config["max_self"] == {"id": "11"}
        sdk.login.return_value = LoginResponse.model_validate({"profile": {"contact": {"id": 22}}})
        await adapter.auth("qr", {})
        with pytest.raises(ProviderError, match="max_account_identity_changed"):
            await adapter.auth("sync", {})
        assert adapter.config["max_identity_mismatch"] is True
        with pytest.raises(ProviderError, match="max_account_identity_changed"):
            await adapter.poll()
        # Explicit cleanup can revoke a rejected session without changing history ownership.
        assert (await adapter.auth("disconnect", {})).state == "disconnected"
        assert adapter.config["max_self"] == {"id": "11"}
        assert "_max_session" not in adapter.config
        assert sdk.logout.await_count == 2

    run(exercise())


def test_no_profile_does_not_report_connected(sdk):
    from pymax.types.domain.login import LoginResponse
    sdk.login.return_value = LoginResponse()

    async def exercise():
        adapter = maxmod.MaxPersonalAdapter({})
        await adapter.auth("login", {"login_method": "sms", "phone": "+79990000000"})
        with pytest.raises(ProviderError, match="max_auth_failed"):
            await adapter.auth("code", {"code": "123456"})
        assert "max_self" not in adapter.config and adapter.client is None

    run(exercise())


@pytest.mark.parametrize("field", ["error", "message"])
def test_provider_revoked_session_allows_explicit_new_qr_preserving_history(sdk, field):
    from pymax.exceptions import ApiError
    from pymax.protocol import Opcode
    sdk.login.side_effect = ApiError(opcode=Opcode.LOGIN, **{field: "FAIL_LOGIN_TOKEN"})

    async def exercise():
        config = {"_max_session": {"token": "private-old-session", "device_id": "device", "phone": ""},
                  "max_self": {"id": "11"}, "_max_cursors": {"42": {"watermark": 90000}},
                  "_max_peer_aliases": {"42": ["user:22"]}}
        adapter = maxmod.MaxPersonalAdapter(config)
        result = await adapter.auth("qr", {})
        assert result.state == "qr_required" and sdk.request_qr.await_count == 1
        assert "_max_session" not in config
        assert config["max_self"] == {"id": "11"}
        assert config["_max_cursors"] == {"42": {"watermark": 90000}}
        assert config["_max_peer_aliases"] == {"42": ["user:22"]}
        await adapter.close()

    run(exercise())


def test_transport_restore_failure_retains_session_and_does_not_reissue_qr(sdk):
    sdk.login.side_effect = ConnectionError("private-network-error")

    async def exercise():
        config = {"_max_session": {"token": "private-session", "device_id": "device", "phone": ""},
                  "max_self": {"id": "11"}}
        adapter = maxmod.MaxPersonalAdapter(config)
        with pytest.raises(ProviderError, match="max_transport_error"):
            await adapter.auth("qr", {})
        assert config["_max_session"]["token"] == "private-session"
        assert sdk.request_qr.await_count == 0

    run(exercise())


def test_cancelled_and_timed_out_auth_close_pending_sdk_task(sdk):
    async def stalled():
        await asyncio.Future()
    sdk.request_qr.side_effect = stalled

    async def exercise():
        adapter = maxmod.MaxPersonalAdapter({})
        adapter.AUTH_WAIT_SECONDS = .01
        with pytest.raises(ProviderError, match="max_transport_error"):
            await adapter.auth("qr", {})
        assert adapter._connect_task is None and adapter.client is None
        assert not sdk.transports[-1].is_open
        adapter.AUTH_WAIT_SECONDS = 45
        task = asyncio.create_task(adapter.auth("qr", {}))
        while not sdk.transports[-1].is_open:
            await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert adapter._connect_task is None and adapter.client is None
        assert not sdk.transports[-1].is_open

    run(exercise())


def test_live_incoming_is_not_hidden_behind_frozen_history(sdk):
    async def exercise():
        latest = NS(id="new", time=99000, sender=22, text="new", attaches=[])
        old = [NS(id=str(n), time=n * 1000, sender=11, text="old", attaches=[]) for n in (19, 18)]

        async def history(**kwargs):
            return [latest] if kwargs["from_time"] == 100000 else old

        config = {"max_self": {"id": "11"}, "_max_cursors": {
            "42": {"floor": 1000, "ceiling": 50000, "before": 20000}}}
        adapter = maxmod.MaxPersonalAdapter(config)
        adapter.PAGE_SIZE = 2
        adapter.client = NS(is_connected=True, me=NS(contact=NS(id=11)),
                            chats=[NS(id=42, title="Customer")], fetch_history=AsyncMock(side_effect=history))
        events = await adapter.poll()
        assert {event.provider_id for event in events if not event.metadata.get("conversation_only")} == {"new", "19", "18"}
        assert next(event for event in events if event.provider_id == "new").direction == "inbound"
        assert next(event for event in events if event.provider_id == "19").direction == "outbound"
        assert config["_max_cursors"]["42"]["before"] == 18000

    run(exercise())


def test_late_stored_message_is_replayed_even_below_history_watermark(sdk):
    async def exercise():
        message = NS(id="late", time=98000, sender=22, text="late", attaches=[])
        adapter = maxmod.MaxPersonalAdapter({"max_self": {"id": "11"},
            "_max_cursors": {"42": {"watermark": 99000}}})
        adapter.client = NS(is_connected=True, me=NS(contact=NS(id=11)),
                            chats=[NS(id=42, title="Customer")],
                            fetch_history=AsyncMock(return_value=[message]))
        assert [event.provider_id for event in await adapter.poll() if not event.metadata.get("conversation_only")] == ["late"]
        assert adapter.client.fetch_history.await_count == 1

    run(exercise())


def test_old_page_failure_keeps_latest_message_and_durable_backfill_cursor(sdk):
    async def exercise():
        current = NS(id="new", time=101000, sender=22, text="new", attaches=[])
        async def history(**kwargs):
            if kwargs["from_time"] == 100000:
                return [current]
            raise TimeoutError()
        config = {"max_self": {"id": "11"}, "_max_history": {"days": 3650, "boundary": 90,
            "completed": [], "catalog_after": 0}, "_max_cursors": {
            "42": {"floor": 1000, "ceiling": 50000, "before": 20000}}}
        value = maxmod.MaxPersonalAdapter(config)
        value.client = NS(is_connected=True, me=NS(contact=NS(id=11)), chats=[NS(id=42, title="Customer")],
                          fetch_history=AsyncMock(side_effect=history))
        events = await value.poll()
        assert [event.provider_id for event in events if event.provider_id] == ["new"]
        assert next(event for event in events if event.provider_id).metadata["historical"] is False
        assert config["_max_cursors"]["42"]["before"] == 20000
        assert config["history_status"]["state"] == "partial" and config["history_status"]["chats_completed"] == 0
    run(exercise())


def test_timestamp_overflow_reports_partial_without_skipping_or_blocking_live(sdk):
    async def exercise():
        records = [NS(id=str(i), time=100000, sender=22, text="same timestamp", attaches=[]) for i in range(2)]
        value = maxmod.MaxPersonalAdapter({"max_self": {"id": "11"}})
        value.PAGE_SIZE = 2
        value.client = NS(is_connected=True, me=NS(contact=NS(id=11)), chats=[NS(id=42, title="Customer")],
                          fetch_history=AsyncMock(return_value=records))
        events = await value.poll()
        assert {event.provider_id for event in events if event.provider_id} == {"0", "1"}
        assert value.config["_max_cursors"]["42"]["before"] == 100000
        assert value.config["history_status"]["state"] == "partial"
        assert value.config["history_status"]["chats_completed"] == 0
    run(exercise())
