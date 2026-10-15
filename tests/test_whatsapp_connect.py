"""Connect wizard provider boundaries; all HTTP is an in-memory mock."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace

import httpx
import pytest

from app.adapters.base import ProviderError
from app.adapters.whatsapp_web import WhatsAppWebAdapter


SESSION = "omni-" + "b" * 32


@pytest.fixture(autouse=True)
def experimental(monkeypatch):
    monkeypatch.setenv("ENABLE_EXPERIMENTAL", "true")


def exercise(handler, action="sync"):
    async def run():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        try:
            return await adapter.auth(action, {})
        finally:
            await adapter.close()
    return asyncio.run(run())


def test_connected_identity_is_verified_and_allowlisted():
    result = exercise(lambda request: httpx.Response(200, json={
        "status": "WORKING", "me": {
            "id": "999999999999@lid", "jid": "79990000000:7@s.whatsapp.net",
            "pushName": "Manager", "access_token": "must-not-appear",
        },
        "config": {"arbitrary_secret": "must-not-appear"},
    }))
    assert result.state == "connected"
    assert result.public["identity"] == {
        "id": "999999999999@lid", "phone": "+79990000000", "display_name": "Manager",
    }
    assert "must-not-appear" not in str(result.public)
    assert "qr_data_url" not in result.public


def test_working_without_verified_identity_stays_pending():
    paths = []
    def handler(request):
        paths.append(request.url.path)
        return httpx.Response(200, json=None if request.url.path.endswith("/me") else {"status": "WORKING"})
    result = exercise(handler)
    assert result.state == "connecting"
    assert paths == [f"/api/sessions/{SESSION}", f"/api/sessions/{SESSION}/me"]
    assert "identity" not in result.public


def test_working_identity_can_come_from_me_endpoint():
    def handler(request):
        return httpx.Response(200, json={"id": "79990000000@c.us"} if request.url.path.endswith("/me") else {"status": "WORKING"})
    result = exercise(handler)
    assert result.state == "connected"
    assert result.public["identity"]["phone"] == "+79990000000"


@pytest.mark.parametrize("status", [404, 422])
def test_scanning_finishes_between_status_and_qr(status):
    def handler(request):
        return httpx.Response(status) if request.url.path.endswith("/auth/qr") else httpx.Response(200, json={"status": "SCAN_QR_CODE"})
    result = exercise(handler)
    assert result.state == "connecting"
    assert "qr_data_url" not in result.public
    assert "identity" not in result.public


@pytest.mark.parametrize("value", [None, {"token": "untrusted"}, "x" * 5000])
def test_invalid_qr_is_a_safe_provider_error(value):
    def handler(request):
        return httpx.Response(200, json={"value": value} if request.url.path.endswith("/auth/qr") else {"status": "SCAN_QR_CODE"})
    with pytest.raises(ProviderError, match="waha_invalid_qr_response"):
        exercise(handler)


def test_empty_qr_during_pairing_is_pending_until_verified_identity(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: clock[0]))

    async def run():
        phase = "qr"
        calls = []

        def handler(request):
            calls.append(request.method)
            if request.url.path.endswith("/auth/qr"):
                return httpx.Response(200, json={"value": "temporary-qr" if phase == "qr" else ""})
            state = {"status": "SCAN_QR_CODE"} if phase != "working" else {
                "status": "WORKING", "me": {"id": "79990000000@c.us"},
            }
            return httpx.Response(200, json=state)

        config = {"_waha_session": SESSION}
        shown = await auth_step(config, handler)
        assert shown.state == "qr_required"
        phase = "scanned"
        pending = await auth_step(config, handler)
        assert pending.state == "connecting"
        assert "qr_data_url" not in pending.public and "identity" not in pending.public
        deadline = config["_waha_wait_deadline"]
        clock[0] += 3
        assert (await auth_step(config, handler)).state == "connecting"
        assert config["_waha_wait_deadline"] == deadline
        phase = "working"
        connected = await auth_step(config, handler)
        assert connected.state == "connected"
        assert connected.public["identity"]["phone"] == "+79990000000"
        assert config["_waha_session"] == SESSION
        assert set(calls) == {"GET"}

    asyncio.run(run())


def test_empty_qr_wait_keeps_deadline_across_worker_restarts(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: clock[0]))

    def handler(request):
        return httpx.Response(200, json={"value": ""} if request.url.path.endswith("/auth/qr")
                              else {"status": "SCAN_QR_CODE"})

    async def run():
        config = {"_waha_session": SESSION}
        assert (await auth_step(config, handler)).state == "connecting"
        restored = deepcopy(config)
        clock[0] += 120
        result = await auth_step(restored, handler)
        assert result.state == "action_required"
        assert result.public["error"] == "waha_connect_timeout"
        assert "qr_data_url" not in result.public and "identity" not in result.public
        assert restored["_waha_wait_deadline"] == config["_waha_wait_deadline"]

    asyncio.run(run())


def test_qr_authorization_error_does_not_get_mistaken_for_pending():
    def handler(request):
        return httpx.Response(401) if request.url.path.endswith("/auth/qr") else httpx.Response(200, json={"status": "SCAN_QR_CODE"})
    with pytest.raises(ProviderError, match="waha_request_rejected"):
        exercise(handler)


def test_changed_qr_replaces_previous_image_and_connected_clears_it():
    async def run():
        step = 0
        def handler(request):
            if request.url.path.endswith("/auth/qr"):
                return httpx.Response(200, json={"value": f"temporary-qr-{step}"})
            return httpx.Response(200, json={"status": "WORKING", "me": {"id": "79990000000@c.us"}} if step == 2 else {"status": "SCAN_QR_CODE"})
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        try:
            first = await adapter.auth("sync", {})
            step = 1
            second = await adapter.auth("sync", {})
            assert first.public["qr_data_url"] != second.public["qr_data_url"]
            step = 2
            connected = await adapter.auth("sync", {})
            assert connected.state == "connected"
            assert "qr_data_url" not in connected.public
        finally:
            await adapter.close()
    asyncio.run(run())


def test_different_identity_after_reconnect_cannot_send_or_import_history():
    async def run():
        config = {"_waha_session": SESSION, "wa_self": {"id": "79990000000@c.us", "phone": "+79990000000"}}
        requests = []
        def handler(request):
            requests.append(request.url.path)
            return httpx.Response(200, json={"status": "WORKING", "me": {"id": "79991111111@c.us"}})
        adapter = WhatsAppWebAdapter(config)
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ProviderError, match="whatsapp_account_identity_changed"):
                await adapter.auth("sync", {})
            assert config["wa_self"]["phone"] == "+79990000000"
            assert config["wa_identity_mismatch"] is True
            with pytest.raises(ProviderError, match="whatsapp_account_identity_changed"):
                await adapter.poll()
            with pytest.raises(ProviderError, match="whatsapp_account_identity_changed"):
                await adapter.send("+79992222222", "Do not send", "unused")
            assert len(requests) == 1
        finally:
            await adapter.close()
    asyncio.run(run())


def test_legacy_disconnect_pins_old_account_before_removing_device_session():
    async def run():
        paths = []
        def handler(request):
            paths.append((request.method, request.url.path))
            if request.method == "GET":
                return httpx.Response(200, json={"status": "WORKING", "me": {"id": "79990000000@c.us"}})
            return httpx.Response(200, json={})
        config = {"_waha_session": SESSION, "_waha_cursors": {"old": {}}}
        adapter = WhatsAppWebAdapter(config)
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        try:
            result = await adapter.auth("disconnect", {})
            assert result.state == "disconnected"
            assert config["wa_self"]["phone"] == "+79990000000"
            assert "_waha_session" not in config
            assert paths[0][0] == "GET" and paths[-1][0] == "DELETE"
        finally:
            await adapter.close()
    asyncio.run(run())


def test_reconnect_on_same_worker_adapter_allocates_new_device_session():
    async def run():
        created = []
        def handler(request):
            if request.method == "GET" and request.url.path == f"/api/sessions/{SESSION}":
                return httpx.Response(200, json={"status": "WORKING", "me": {"id": "79990000000@c.us"}})
            if request.url.path.endswith("/auth/qr"):
                return httpx.Response(200, json={"value": "new-device-qr"})
            if request.method == "GET":
                return httpx.Response(404)
            if request.method == "POST" and request.url.path == "/api/sessions":
                import json
                created.append(json.loads(request.content)["name"])
                return httpx.Response(201, json={"status": "SCAN_QR_CODE"})
            return httpx.Response(200, json={})
        config = {"_waha_session": SESSION}
        adapter = WhatsAppWebAdapter(config)
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        try:
            await adapter.auth("disconnect", {})
            result = await adapter.auth("login", {})
            assert result.state == "qr_required"
            assert len(created) == 1 and created[0] != SESSION
            assert config["wa_self"]["phone"] == "+79990000000"
        finally:
            await adapter.close()
    asyncio.run(run())


async def auth_step(config, handler, action="sync"):
    """Reconstruct a worker adapter from its last durable account config."""
    adapter = WhatsAppWebAdapter(config)
    adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
    try:
        return await adapter.auth(action, {})
    finally:
        await adapter.close()


@pytest.mark.parametrize("provider_state, missing_status", [
    ("STARTING", None), ("WORKING", 404), ("WORKING", 422),
    ("SCAN_QR_CODE", 404), ("SCAN_QR_CODE", 422),
])
def test_no_qr_or_identity_wait_is_bounded_across_worker_restarts(monkeypatch, provider_state, missing_status):
    clock = [1000.0]
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: clock[0]))
    requests = []

    def handler(request):
        requests.append(request.method)
        if request.url.path.endswith(("/auth/qr", "/me")):
            return httpx.Response(missing_status)
        return httpx.Response(200, json={"status": provider_state})

    async def run():
        config = {"_waha_session": SESSION}
        initial = await auth_step(config, handler)
        assert initial.state == "connecting"
        deadline = config["_waha_wait_deadline"]
        assert deadline == 1120.0
        clock[0] = 1119.0
        restored = deepcopy(config)
        assert (await auth_step(restored, handler)).state == "connecting"
        assert restored["_waha_wait_deadline"] == deadline
        clock[0] = 1120.0
        expired = await auth_step(deepcopy(restored), handler)
        assert expired.state == "action_required"
        assert expired.public == {"experimental": True, "provider_state": provider_state,
                                  "error": "waha_connect_timeout"}
        assert "qr_data_url" not in expired.public
        clock[0] = 2000.0
        assert (await auth_step(deepcopy(restored), handler)).state == "action_required"
        assert set(requests) == {"GET"}  # Polls never restart or revoke a device.

    asyncio.run(run())


@pytest.mark.parametrize("provider_state, operation", [("FAILED", "restart"), ("STOPPED", "start")])
def test_explicit_retry_uses_correct_waha_operation_and_preserves_device(monkeypatch, provider_state, operation):
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: 1000.0))
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": provider_state})

    async def run():
        config = {"_waha_session": SESSION, "_waha_wait_deadline": 900.0,
                  "wa_self": {"id": "79990000000@c.us"}, "_waha_cursors": {"chat": {"watermark": 10}}}
        saved_identity, saved_cursors = deepcopy(config["wa_self"]), deepcopy(config["_waha_cursors"])
        pending = await auth_step(config, handler, "login")
        assert pending.state == "connecting"
        assert config["_waha_wait_deadline"] == 1120.0
        assert config["_waha_session"] == SESSION
        assert config["wa_self"] == saved_identity and config["_waha_cursors"] == saved_cursors
        terminal = await auth_step(config, handler)
        assert terminal.state == "action_required"
        assert terminal.public["error"] == "waha_session_" + provider_state.lower()
        assert [entry for entry in requests if entry[0] != "GET"] == [
            ("POST", f"/api/sessions/{SESSION}/{operation}")]

    asyncio.run(run())


def test_timed_out_starting_only_restarts_on_explicit_login(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: clock[0]))
    requests = []

    def handler(request):
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json={"status": "STARTING"})

    async def run():
        config = {"_waha_session": SESSION}
        await auth_step(config, handler, "login")
        clock[0] = 1100.0
        await auth_step(config, handler, "login")
        assert config["_waha_wait_deadline"] == 1120.0  # Repeated clicks do not extend a healthy wait.
        clock[0] = 1120.0
        assert (await auth_step(config, handler)).public["error"] == "waha_connect_timeout"
        clock[0] = 1200.0
        assert (await auth_step(config, handler, "login")).state == "connecting"
        assert config["_waha_wait_deadline"] == 1320.0
        clock[0] = 1320.0
        assert (await auth_step(config, handler)).public["error"] == "waha_connect_timeout"
        assert [entry for entry in requests if entry[0] != "GET"] == [
            ("POST", f"/api/sessions/{SESSION}/restart")]
        assert config["_waha_session"] == SESSION

    asyncio.run(run())


def test_valid_qr_clears_wait_and_a_later_missing_qr_gets_a_new_window(monkeypatch):
    clock, phase = [1000.0], ["STARTING"]
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: clock[0]))

    def handler(request):
        assert request.method == "GET"
        if request.url.path.endswith("/auth/qr"):
            return httpx.Response(200, json={"value": "temporary-qr"})
        return httpx.Response(200, json={"status": phase[0], "me": {"id": "79990000000@c.us"}})

    async def run():
        config = {"_waha_session": SESSION}
        await auth_step(config, handler)
        clock[0], phase[0] = 1100.0, "SCAN_QR_CODE"
        assert (await auth_step(config, handler, "login")).state == "qr_required"
        assert "_waha_wait_deadline" not in config
        clock[0], phase[0] = 1200.0, "STARTING"
        assert (await auth_step(config, handler)).state == "connecting"
        assert config["_waha_wait_deadline"] == 1320.0
        clock[0], phase[0] = 1350.0, "WORKING"
        assert (await auth_step(config, handler, "login")).state == "connected"
        assert "_waha_wait_deadline" not in config
        assert config["wa_self"]["phone"] == "+79990000000"

    asyncio.run(run())


def test_explicit_check_of_working_without_identity_never_restarts(monkeypatch):
    monkeypatch.setattr("app.adapters.whatsapp_web.time", SimpleNamespace(time=lambda: 1000.0))

    def handler(request):
        assert request.method == "GET"
        return httpx.Response(404) if request.url.path.endswith("/me") else httpx.Response(200, json={"status": "WORKING"})

    async def run():
        identity = {"id": "79990000000@c.us"}
        config = {"_waha_session": SESSION, "wa_self": identity.copy(), "_waha_wait_deadline": 900.0}
        assert (await auth_step(config, handler, "login")).state == "connecting"
        assert config["wa_self"] == identity
        assert config["_waha_wait_deadline"] == 1120.0

    asyncio.run(run())


@pytest.mark.parametrize("status", [None, "untrusted-provider-value", {"token": "private"}])
def test_unknown_status_is_rejected_without_echoing_provider_data(status):
    with pytest.raises(ProviderError, match="^waha_invalid_response$"):
        exercise(lambda request: httpx.Response(200, json={"status": status}))
