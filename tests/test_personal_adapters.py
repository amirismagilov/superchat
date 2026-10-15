"""Provider-boundary regression tests. Never contacts a messaging service."""
import asyncio
import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock

import httpx
import pytest

from app.adapters.base import ProviderError
from app.adapters.whatsapp_web import WhatsAppWebAdapter

SESSION = "omni-" + "1" * 32
CLIENT_ID = "3ade80ad-4c14-47c5-a64e-252aa94d0471"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def experimental(monkeypatch):
    monkeypatch.setenv("ENABLE_EXPERIMENTAL", "true")
    monkeypatch.setenv("WAHA_URL", "http://waha.internal:3000")
    monkeypatch.setenv("WAHA_API_KEY", "deployment-secret")


def test_personal_disabled_by_default(monkeypatch):
    monkeypatch.delenv("ENABLE_EXPERIMENTAL", raising=False)
    with pytest.raises(ProviderError, match="experimental_adapter_disabled"):
        run(WhatsAppWebAdapter({}).start())


def test_waha_uses_only_deployment_destination_and_key(experimental, monkeypatch):
    from app.adapters import whatsapp_web
    seen = []
    original_client = httpx.AsyncClient

    def fake_client(**kwargs):
        seen.append(kwargs)
        return original_client(**kwargs, transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"status": "WORKING", "me": {"id": "79990000000@c.us", "pushName": "Manager"}})))

    monkeypatch.setattr(whatsapp_web.httpx, "AsyncClient", fake_client)

    async def exercise():
        adapter = WhatsAppWebAdapter({"endpoint": "http://169.254.169.254", "api_key": "account-key"})
        result = await adapter.auth("login", {})
        assert result.state == "connected"
        assert adapter.config["_waha_session"].startswith("omni-")
        assert "deployment-secret" not in str(result.public)
        await adapter.close()

    run(exercise())
    assert seen[0]["base_url"] == "http://waha.internal:3000"
    assert seen[0]["headers"] == {"X-Api-Key": "deployment-secret"}
    assert seen[0]["follow_redirects"] is False


def test_waha_qr_uses_documented_raw_response(experimental):
    def handler(request):
        if request.url.path.endswith("/auth/qr"):
            assert request.url.params["format"] == "raw"
            return httpx.Response(200, json={"value": "a,b,c,provider-qr"})
        return httpx.Response(200, json={"status": "SCAN_QR_CODE"})

    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        result = await adapter.auth("sync", {})
        assert result.state == "qr_required"
        assert result.public["qr_text"] == "a,b,c,provider-qr"
        assert result.public["qr_data_url"].startswith("data:image/png;base64,iVBOR")
        await adapter.close()

    run(exercise())


def test_waha_stable_nonce_and_ambiguous_transport(experimental):
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        raise httpx.ReadTimeout("response lost", request=request)

    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        with pytest.raises(ProviderError) as caught:
            await adapter.send("+79990000000", "test", CLIENT_ID)
        assert caught.value.uncertain is True
        assert len(requests) == 1
        assert requests[0]["id"] == CLIENT_ID.replace("-", "").upper()
        assert requests[0]["chatId"] == "79990000000@c.us"
        await adapter.close()

    run(exercise())


def test_waha_history_replays_after_crash_and_keeps_timestamp_ties(experimental):
    config = {"_waha_session": SESSION}
    pages = []

    def handler(request):
        if request.url.path.endswith("/chats"):
            return httpx.Response(200, json=[{"id": "7@s.whatsapp.net", "name": "Customer"}])
        offset = int(request.url.params["offset"])
        pages.append(offset)
        records = [{"id": str(i), "body": "message", "timestamp": 100, "fromMe": False} for i in range(offset, min(offset + 100, 101))]
        return httpx.Response(200, json=records)

    async def poll(state):
        adapter = WhatsAppWebAdapter(state)
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        events = await adapter.poll()
        await adapter.close()
        return events

    before = copy.deepcopy(config)
    first = run(poll(config))
    replay = run(poll(before))  # Simulated crash before committing config.
    assert [event.provider_id for event in first] == [event.provider_id for event in replay]
    second = run(poll(config))
    assert len(first) == 100 and [event.provider_id for event in second] == ["100"]
    assert first[0].peer == "7@c.us"
    assert pages == [0, 0, 100]
    assert "watermark" in config["_waha_cursors"]["7@c.us"]


def test_waha_revoke_failure_preserves_session(experimental):
    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(lambda request: httpx.Response(503)))
        with pytest.raises(ProviderError):
            await adapter.auth("disconnect", {})
        assert adapter.config["_waha_session"] == SESSION
        await adapter.close()

    run(exercise())


@pytest.fixture
def max_module():
    pytest.importorskip("pymax")
    from app.adapters import max_personal
    return max_personal


def test_max_uses_custom_session_store_without_disk(max_module, experimental, tmp_path, monkeypatch):
    from pymax.session.models import SessionInfo
    monkeypatch.chdir(tmp_path)
    config = {}
    adapter = max_module.MaxPersonalAdapter(config)
    client = adapter._make_client("login")
    assert isinstance(client.extra_config.store, max_module._ConfigSessionStore)
    assert client.extra_config.telemetry is False

    async def exercise():
        store = client.extra_config.store
        await store.save_session(SessionInfo(token="secret", device_id="device", phone="+79990000000"))
        reconstructed = max_module._ConfigSessionStore(copy.deepcopy(config))
        assert (await reconstructed.load_session()).token == "secret"
        await reconstructed.update_token("secret", "new-secret")
        assert (await reconstructed.load_session()).token == "new-secret"
        await reconstructed.delete_session("new-secret")
        assert await reconstructed.load_session() is None

    run(exercise())
    assert list(tmp_path.iterdir()) == []


def fake_app(api):
    return NS(api=NS(auth=api), config=NS(device=NS(user_agent=NS(model_dump=lambda **kwargs: {}), mt_instance_id="instance")))


def test_max_sms_password_steps_survive_restart(max_module):
    config = {"login_method": "sms", "phone": "+79990000000"}
    api = NS(
        request_code=AsyncMock(return_value=NS(token="challenge-secret", code_length=6)),
        send_code=AsyncMock(return_value=NS(login_token=None, password_challenge=NS(track_id="2fa-track", hint="hint"))),
        check_password=AsyncMock(return_value=NS(login_token="session-secret")),
    )
    app = fake_app(api)
    with pytest.raises(max_module._PendingAuth) as first:
        run(max_module._SteppedAuthFlow(config, "login", {}).authenticate(app))
    assert first.value.result.state == "code_required"
    assert "challenge-secret" not in str(first.value.result.public)
    restarted = copy.deepcopy(config)
    with pytest.raises(max_module._PendingAuth) as second:
        run(max_module._SteppedAuthFlow(restarted, "code", {"code": "123456"}).authenticate(app))
    assert second.value.result.state == "password_required"
    result = run(max_module._SteppedAuthFlow(restarted, "password", {"password": "private"}).authenticate(app))
    assert result.token == "session-secret"
    assert "_max_pending" not in restarted
    assert "private" not in json.dumps(restarted)
    api.send_code.assert_awaited_once_with("challenge-secret", "123456")


def test_max_qr_poll_and_expiry(max_module, monkeypatch):
    monkeypatch.setattr(max_module.time, "time", lambda: 100)
    config = {}
    api = NS(request_qr=AsyncMock(return_value=NS(track_id="track", qr_link="https://max.ru/qr/test", expires_at=200000)),
             check_qr=AsyncMock(return_value=NS(status=NS(login_available=False))))
    app = fake_app(api)
    with pytest.raises(max_module._PendingAuth) as first:
        run(max_module._SteppedAuthFlow(config, "login", {}).authenticate(app))
    assert first.value.result.state == "qr_required"
    with pytest.raises(max_module._PendingAuth) as pending:
        run(max_module._SteppedAuthFlow(config, "sync", {}).authenticate(app))
    assert pending.value.result.public["expires_at"] == 200
    monkeypatch.setattr(max_module.time, "time", lambda: 201)
    with pytest.raises(max_module._PendingAuth) as expired:
        run(max_module._SteppedAuthFlow(config, "sync", {}).authenticate(app))
    assert expired.value.result.state == "login_required"
    assert "_max_pending" not in config


def test_max_send_supplies_stable_cid_and_marks_timeout_uncertain(max_module, experimental):
    from pymax.protocol import InboundFrame
    invoke = AsyncMock(return_value=InboundFrame(opcode=64, payload={"id": 12, "time": 100, "type": "TEXT"}))
    client = NS(is_connected=True, _app=NS(invoke=invoke), close=AsyncMock())
    adapter = max_module.MaxPersonalAdapter({})
    adapter.client = client
    assert run(adapter.send("42", "Hello", CLIENT_ID)) == "12"
    first_payload = invoke.call_args.args[1]
    assert first_payload["chatId"] == 42
    invoke.side_effect = TimeoutError()
    with pytest.raises(ProviderError) as caught:
        run(adapter.send("42", "Hello", CLIENT_ID))
    assert caught.value.uncertain is True
    assert invoke.call_args.args[1]["message"]["cid"] == first_payload["message"]["cid"]
    assert invoke.await_count == 2  # Exactly one call per explicit send, no internal retries.


def test_max_poll_replays_history_and_keeps_outbound_direction(max_module, experimental, monkeypatch):
    monkeypatch.setattr(max_module.time, "time", lambda: 100)
    message = NS(id=5, time=99000, text="From mobile", sender=11, attaches=[])
    client = NS(is_connected=True, chats=[NS(id=42, title="Customer")], me=NS(contact=NS(id=11)),
                fetch_history=AsyncMock(return_value=[message]))

    def adapter(state):
        value = max_module.MaxPersonalAdapter(state)
        value.client = client
        return value

    config = {}
    first = run(adapter(config).poll())
    replay = run(adapter({}).poll())
    assert first == replay
    assert first[0].direction == "outbound" and first[0].timestamp == 99
    assert config["_max_cursors"]["42"]["watermark"] == 40000


def test_waha_phone_history_supplies_aliases_for_original_crm_peer(experimental):
    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        # No HTTP lookup is needed for a phone-number chat.
        metadata = await adapter._peer_metadata("79990000000@c.us")
        assert metadata["canonical_peer"] == "79990000000@c.us"
        assert {"+79990000000", "79990000000", "79990000000@s.whatsapp.net"} <= set(metadata["aliases"])

    run(exercise())


def test_waha_send_mapping_reconciles_lid_history_after_restart(experimental):
    def handler(request):
        if request.url.path == "/api/sendText":
            return httpx.Response(200, json={"id": "sent-1", "to": "12345@lid", "fromMe": True})
        if request.url.path.endswith("/chats"):
            return httpx.Response(200, json=[{"id": "12345@lid"}])
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=[{"id": "reply-1", "body": "Reply", "timestamp": 100, "fromMe": False}])
        pytest.fail("Cached authoritative mapping must not need another LID lookup")

    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        await adapter.send("+79990000000", "Hello", CLIENT_ID)
        persisted = copy.deepcopy(adapter.config)
        await adapter.close()
        restored = WhatsAppWebAdapter(persisted)
        restored.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        events = await restored.poll()
        assert events[0].peer == "12345@lid"
        assert events[0].metadata["canonical_peer"] == "79990000000@c.us"
        assert {"12345@lid", "+79990000000", "79990000000"} <= set(events[0].metadata["aliases"])
        await restored.close()

    run(exercise())


@pytest.mark.parametrize("pn, canonical", [("79990000000@c.us", "79990000000@c.us"), (None, "12345@lid")])
def test_waha_lid_lookup_uses_provider_mapping_only(experimental, pn, canonical):
    requests = []

    def handler(request):
        requests.append(request.url.path)
        return httpx.Response(200, json={"lid": "12345@lid", "pn": pn})

    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        identity = await adapter._peer_metadata("12345@lid")
        assert identity["canonical_peer"] == canonical
        if pn is None:
            assert identity["aliases"] == ["12345@lid"]
        await adapter.close()

    run(exercise())
    assert requests == [f"/api/{SESSION}/lids/12345@lid"]


def max_send_client(*, contacts=None, lookup=None):
    from pymax.protocol import InboundFrame
    from pymax.api.users.service import UserService
    from unittest.mock import Mock
    return NS(
        is_connected=True, contacts=contacts or [], chats=[], me=NS(contact=NS(id=11)),
        search_by_phone=lookup or AsyncMock(),
        get_chat_id=Mock(side_effect=lambda first, second: UserService.get_chat_id(None, first, second)),
        _app=NS(invoke=AsyncMock(return_value=InboundFrame(opcode=64, payload={"id": 12, "time": 100, "type": "TEXT"}))),
    )


def test_max_first_contact_by_phone_and_alias_recovery(max_module, experimental, monkeypatch):
    from pymax.types import User
    monkeypatch.setattr(max_module.time, "time", lambda: 100)
    client = max_send_client(lookup=AsyncMock(return_value=User(id=99, phone=79990000000)))
    config = {}
    adapter = max_module.MaxPersonalAdapter(config)
    adapter.client = client
    assert run(adapter.send("+79990000000", "Hello after our call", CLIENT_ID)) == "12"
    client.search_by_phone.assert_awaited_once_with("+79990000000")
    client.get_chat_id.assert_called_once_with(11, 99)
    assert client._app.invoke.call_args.args[1]["chatId"] == 104
    assert config["_max_peer_aliases"]["104"] == ["+79990000000", "phone:+79990000000", "user:99"]
    # Remote chat list may not have updated yet. Persisted resolution still
    # discovers its history after the worker has restarted.
    restored = max_module.MaxPersonalAdapter(copy.deepcopy(config))
    restored.client = client
    client.fetch_history = AsyncMock(return_value=[NS(id=13, time=99000, text="Reply", sender=99, attaches=[])])
    events = run(restored.poll())
    assert events[0].peer == events[0].metadata["canonical_peer"] == "104"
    assert "+79990000000" in events[0].metadata["aliases"]
    assert events[0].direction == "inbound"


def test_max_phone_discovery_falls_back_to_existing_contacts(max_module, experimental):
    from pymax.types import User
    client = max_send_client(
        lookup=AsyncMock(side_effect=RuntimeError("phone discovery unavailable")),
        contacts=[None, User(id=99, phone=79990000000)],
    )
    adapter = max_module.MaxPersonalAdapter({})
    adapter.client = client
    assert run(adapter.send("phone:+79990000000", "Hello", CLIENT_ID)) == "12"
    assert client._app.invoke.call_args.args[1]["chatId"] == 104


def test_max_unresolved_phone_fails_before_any_send(max_module, experimental):
    client = max_send_client(lookup=AsyncMock(side_effect=RuntimeError("privacy restriction")))
    adapter = max_module.MaxPersonalAdapter({})
    adapter.client = client
    with pytest.raises(ProviderError) as caught:
        run(adapter.send("+79990000000", "Hello", CLIENT_ID))
    assert caught.value.code == "max_phone_not_found_or_private"
    assert caught.value.uncertain is False
    client._app.invoke.assert_not_awaited()
    assert "_max_peer_aliases" not in adapter.config


def test_max_known_user_id_is_supported_without_phone_lookup(max_module, experimental):
    client = max_send_client()
    adapter = max_module.MaxPersonalAdapter({})
    adapter.client = client
    assert run(adapter.send("user:99", "Hello", CLIENT_ID)) == "12"
    client.search_by_phone.assert_not_awaited()
    client.get_chat_id.assert_called_once_with(11, 99)
    assert client._app.invoke.call_args.args[1]["chatId"] == 104


def test_max_changed_phone_identity_stops_before_send(max_module, experimental):
    from pymax.types import User
    client = max_send_client(lookup=AsyncMock(return_value=User(id=100, phone=79990000000)))
    config = {"_max_peer_aliases": {"104": ["+79990000000", "phone:+79990000000", "user:99"]}}
    before = copy.deepcopy(config)
    adapter = max_module.MaxPersonalAdapter(config)
    adapter.client = client
    with pytest.raises(ProviderError, match="max_peer_identity_changed"):
        run(adapter.send("+79990000000", "Hello", CLIENT_ID))
    client._app.invoke.assert_not_awaited()
    assert config == before


@pytest.mark.parametrize("key, chat, expected", [
    ({"remoteJid": "79990000000@s.whatsapp.net", "fromMe": True, "id": "BAILEYS1"},
     "79990000000@s.whatsapp.net", "true_79990000000@c.us_BAILEYS1"),
    ({"remoteJid": "79990000000:17@s.whatsapp.net", "fromMe": True, "id": "BAILEYS2"},
     "79990000000@s.whatsapp.net", "true_79990000000@c.us_BAILEYS2"),
    ({"remoteJid": "12345:2@lid", "remoteJidAlt": "79990000000@s.whatsapp.net", "fromMe": True, "id": "BAILEYS3"},
     "12345@lid", "true_12345@lid_BAILEYS3"),
    ({"remoteJid": "123-456@g.us", "fromMe": True, "id": "BAILEYS4", "participant": "79990000000:2@s.whatsapp.net"},
     "123-456@g.us", "true_123-456@g.us_BAILEYS4_79990000000@c.us"),
])
def test_waha_noweb_raw_send_id_matches_normalized_poll_history(experimental, key, chat, expected):
    # Fixtures use tag 2026.9.2 utils.ts buildMessageId and jids.ts toCusFormat.
    # Raw sendText and normalized GET chat messages must dedupe to the same ID.
    def handler(request):
        if request.url.path == "/api/sendText":
            return httpx.Response(200, json={"key": key, "messageTimestamp": 100, "message": {"conversation": "Hello"}})
        if request.url.path.endswith("/chats"):
            return httpx.Response(200, json=[{"id": chat}])
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json=[{"id": expected, "body": "Hello", "timestamp": 100, "fromMe": True}])
        pytest.fail("No unexpected provider calls")

    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(handler))
        peer = "123-456@g.us" if chat.endswith("@g.us") else "+79990000000"
        sent_id = await adapter.send(peer, "Hello", CLIENT_ID)
        assert sent_id == expected
        if key["remoteJid"].endswith("@lid"):
            assert adapter.config["_waha_lid_pn"]["12345@lid"] == "79990000000@c.us"
        events = await adapter.poll()
        assert len(events) == 1 and events[0].provider_id == sent_id
        assert events[0].direction == "outbound"
        await adapter.close()

    run(exercise())


def test_waha_raw_send_missing_key_fields_remains_uncertain(experimental):
    async def exercise():
        adapter = WhatsAppWebAdapter({"_waha_session": SESSION})
        adapter.http = httpx.AsyncClient(base_url="http://waha", transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json={"key": {"id": "RAW_ID_ONLY", "fromMe": True}})))
        with pytest.raises(ProviderError) as caught:
            await adapter.send("+79990000000", "Hello", CLIENT_ID)
        assert caught.value.uncertain is True
        assert caught.value.code == "waha_missing_message_id"
        await adapter.close()

    run(exercise())
