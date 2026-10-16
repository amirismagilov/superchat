"""Provider-contract tests with real Telethon TL types, no live credentials."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from telethon import errors, functions, types

from app.adapters.base import ProviderError
from app.adapters.telegram import TelegramAdapter


NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
CLIENT_ID = "76f2d7e9-b78e-475f-bc36-f2e588ab53de"


def state(pts=10):
    return types.updates.State(pts=pts, qts=0, date=NOW, seq=3, unread_count=0)


def user(user_id=42, **kwargs):
    return types.User(id=user_id, access_hash=9876543, first_name="Клиент", **kwargs)


def message(message_id, *, peer=42, outgoing=False, text="Тест", **kwargs):
    return types.Message(id=message_id, peer_id=types.PeerUser(peer), date=NOW,
                         message=text, out=outgoing, **kwargs)


class FakeClient:
    def __init__(self):
        self.session = SimpleNamespace(save=Mock(return_value="private-session"))
        self.is_connected = Mock(return_value=True)
        self.connect = AsyncMock()
        self.disconnect = AsyncMock()
        self.is_user_authorized = AsyncMock(return_value=True)
        self.send_code_request = AsyncMock(return_value=SimpleNamespace(phone_code_hash="private-hash"))
        self.sign_in = AsyncMock()
        self.get_me = AsyncMock(return_value=user(1, is_self=True))
        self.get_dialogs = AsyncMock(return_value=[])
        self.get_messages = AsyncMock(return_value=[])
        self.get_entity = AsyncMock(return_value=user())
        self.log_out = AsyncMock(return_value=True)
        self.call = AsyncMock(return_value=state())

    async def __call__(self, request):
        return await self.call(request)


def adapter(config=None):
    config = config if config is not None else {
        "api_id": 12345, "api_hash": "a" * 32, "phone": "+79990000001",
        "session": "private-session", "tg_self_id": 1,
        "tg_cursor": TelegramAdapter._state(state()),
    }
    result = TelegramAdapter(config)
    result.client = FakeClient()
    return result


@pytest.mark.asyncio
async def test_login_code_password_can_resume_after_worker_restart_without_storing_secrets():
    a = adapter()
    a.client.is_user_authorized.return_value = False
    result = await a.auth("login", {})
    assert result.state == "awaiting_code"
    assert a.config["tg_code_hash"] == "private-hash"
    assert a.config["session"] == "private-session"
    assert "private-hash" not in str(result.public)
    restarted = adapter(deepcopy(a.config))
    restarted.client.sign_in.side_effect = errors.SessionPasswordNeededError(request=None)
    result = await restarted.auth("code", {"code": "12345"})
    assert result.state == "awaiting_password"
    restarted.client.sign_in.assert_awaited_with(
        phone="+79990000001", code="12345", phone_code_hash="private-hash")
    restarted.client.sign_in.side_effect = None
    result = await restarted.auth("password", {"password": "never-persist-this"})
    assert result.state == "connected"
    assert "never-persist-this" not in str(restarted.config)
    assert "tg_code_hash" not in restarted.config
    assert "code" not in restarted.config and "password" not in restarted.config


@pytest.mark.asyncio
async def test_qr_login_survives_restart_and_only_reports_confirmed_provider_identity():
    a = adapter()
    a.client.is_user_authorized.return_value = False
    expires = datetime.now(timezone.utc) + timedelta(seconds=30)
    a.client.call.return_value = types.auth.LoginToken(expires, b"temporary-provider-token")
    pending = await a.auth("qr", {})
    assert pending.state == "qr_required"
    assert pending.public["expires_at"] == expires.isoformat()
    assert pending.public["qr_data_url"].startswith("data:image/png;base64,iVBOR")
    assert "identity" not in pending.public
    assert "private-session" not in str(pending.public)
    assert "temporary-provider-token" not in str(a.config)
    assert isinstance(a.client.call.call_args.args[0], functions.auth.ExportLoginTokenRequest)
    restarted = adapter(deepcopy(a.config))
    restarted.client.is_user_authorized.return_value = False
    confirmed = user(1, phone="79990000001", username="manager", is_self=True)
    restarted.client.call.return_value = types.auth.LoginTokenSuccess(types.auth.Authorization(confirmed))
    restarted.client._on_login = AsyncMock()
    restarted.client.get_me.return_value = confirmed
    result = await restarted.auth("sync", {})
    assert result.state == "connected"
    assert result.public["identity"] == {"phone": "+79990000001", "display_name": "Клиент", "username": "manager"}
    restarted.client._on_login.assert_awaited_once_with(confirmed)
    assert "tg_qr_pending" not in restarted.config
    assert "tg_auth_method" not in restarted.config
    restarted.client.get_dialogs.assert_not_awaited()


@pytest.mark.asyncio
async def test_qr_login_dc_migration_imports_token_before_connected():
    a = adapter()
    a.client.is_user_authorized.return_value = False
    a.client._switch_dc = AsyncMock()
    a.client._on_login = AsyncMock()
    confirmed = user(1, is_self=True)
    a.client.call.side_effect = [types.auth.LoginTokenMigrateTo(4, b"dc-token"),
                               types.auth.LoginTokenSuccess(types.auth.Authorization(confirmed))]
    result = await a.auth("qr", {})
    assert result.state == "connected"
    a.client._switch_dc.assert_awaited_once_with(4)
    assert isinstance(a.client.call.call_args_list[1].args[0], functions.auth.ImportLoginTokenRequest)
    assert a.client.call.call_args_list[1].args[0].token == b"dc-token"


@pytest.mark.asyncio
async def test_qr_login_two_factor_is_resumable_and_password_not_persisted():
    a = adapter()
    a.client.is_user_authorized.return_value = False
    a.client.call.side_effect = errors.SessionPasswordNeededError(request=None)
    result = await a.auth("qr", {})
    assert result.state == "awaiting_password"
    assert result.public == {"auth_method": "qr"}
    restarted = adapter(deepcopy(a.config))
    result = await restarted.auth("password", {"password": "sensitive-2fa"})
    assert result.state == "connected"
    assert "sensitive-2fa" not in str(restarted.config)
    assert "sensitive-2fa" not in str(result.public)


@pytest.mark.asyncio
async def test_qr_never_reports_success_for_unknown_or_bot_provider_response():
    for response in (state(), types.auth.LoginTokenSuccess(types.auth.Authorization(user(1, bot=True)))):
        a = adapter()
        a.client.is_user_authorized.return_value = False
        a.client.call.return_value = response
        with pytest.raises(ProviderError):
            await a.auth("qr", {})


@pytest.mark.asyncio
async def test_phone_login_clears_pending_qr_and_identity_is_only_provider_confirmed():
    a = adapter()
    a.config["tg_qr_pending"] = True
    a.client.is_user_authorized.return_value = False
    result = await a.auth("login", {"phone": "+79990000002"})
    assert "tg_qr_pending" not in a.config
    assert result.public["phone"] == "+79990000002"
    assert "identity" not in result.public
    a.client.get_me.return_value = None
    with pytest.raises(ProviderError, match="telegram_user_account_required"):
        await a.auth("code", {"code": "12345"})


@pytest.mark.asyncio
async def test_pending_qr_login_is_bounded_and_can_be_explicitly_restarted():
    a = adapter()
    a.config.update(tg_qr_pending=True, tg_qr_deadline=1)
    a.client.is_user_authorized.return_value = False
    result = await a.auth("sync", {})
    assert result.state == "reauth_required"
    assert result.public["error"] == "telegram_qr_expired"
    a.client.call.assert_not_awaited()
    a.client.call.return_value = types.auth.LoginToken(datetime.now(timezone.utc) + timedelta(seconds=30), b"new-token")
    result = await a.auth("qr", {})
    assert result.state == "qr_required"
    assert a.config["tg_qr_deadline"] > 1


@pytest.mark.asyncio
async def test_reconnect_to_different_telegram_identity_is_rejected_after_disconnect():
    a = adapter()
    await a.auth("disconnect", {})
    assert a.config["telegram_bound_user_id"] == 1
    restarted = adapter(deepcopy(a.config))
    restarted.client.get_me.return_value = user(2, is_self=True)
    with pytest.raises(ProviderError, match="telegram_account_identity_changed"):
        await restarted.auth("login", {})
    assert restarted.config["telegram_bound_user_id"] == 1
    assert "tg_self_id" not in restarted.config


@pytest.mark.asyncio
async def test_start_is_allowed_unauthorized_but_send_requires_login():
    a = adapter()
    a.client.is_user_authorized.return_value = False
    await a.start()
    with pytest.raises(ProviderError, match="telegram_session_expired"):
        await a.send("@customer", "hello", CLIENT_ID)
    a.client.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_initial_login_sets_live_baseline_and_announces_progressive_history():
    a = adapter()
    a.config.pop("tg_cursor")
    a.config.pop("tg_self_id")
    result = await a.auth("login", {})
    assert result.public["history_import"] == "progressive"
    assert a.config["tg_cursor"]["pts"] == 10
    a.client.get_messages.assert_not_awaited()
    assert isinstance(a.client.call.call_args.args[0], functions.updates.GetStateRequest)


@pytest.mark.asyncio
async def test_replayed_difference_and_native_outbound_survive_restart():
    a = adapter()
    persisted = deepcopy(a.config)
    difference = types.updates.Difference(
        new_messages=[message(101), message(102, outgoing=True),
                      types.Message(id=103, peer_id=types.PeerChat(55), message="Group", date=NOW),
                      message(104, peer=999)],
        new_encrypted_messages=[], other_updates=[], chats=[],
        users=[user(), user(999, bot=True)], state=state(20),
    )
    a.client.call.return_value = difference
    first = await a.poll()
    assert [(e.provider_id, e.direction) for e in first] == [("101", "inbound"), ("102", "outbound"), ("103", "inbound"), ("104", "inbound")]
    assert a.config["tg_cursor"]["pts"] == 20
    assert a.config["tg_entities"]["42"]["hash"] == 9876543
    # A failed DB commit / crash reconstructs from persisted config, so replay is exact.
    restarted = adapter(persisted)
    restarted.client.call.return_value = difference
    assert await restarted.poll() == first
    assert restarted.client.call.call_args.args[0].pts == 10
    restarted_after_commit = adapter(deepcopy(a.config))
    restarted_after_commit.client.call.return_value = types.updates.DifferenceEmpty(NOW, 5)
    assert await restarted_after_commit.poll() == []
    assert restarted_after_commit.client.call.call_args.args[0].pts == 20


@pytest.mark.asyncio
async def test_difference_slice_uses_intermediate_state():
    a = adapter()
    a.client.call.return_value = types.updates.DifferenceSlice(
        new_messages=[message(1)], new_encrypted_messages=[], other_updates=[],
        chats=[], users=[user()], intermediate_state=state(15))
    assert len(await a.poll()) == 1
    assert a.config["tg_cursor"]["pts"] == 15


@pytest.mark.asyncio
async def test_send_nonce_is_stable_and_numeric_peer_works_after_reconnect():
    a = adapter()
    a.client.call.return_value = types.UpdateShortSentMessage(
        id=99, pts=11, pts_count=1, date=NOW, out=True)
    assert await a.send("@customer", "hello", CLIENT_ID) == "99"
    first = a.client.call.call_args.args[0]
    assert isinstance(first, functions.messages.SendMessageRequest)
    restarted = adapter(deepcopy(a.config))
    restarted.client.call.return_value = a.client.call.return_value
    assert await restarted.send("42", "hello", CLIENT_ID) == "99"
    retry = restarted.client.call.call_args.args[0]
    assert retry.random_id == first.random_id
    assert retry.peer == types.InputPeerUser(42, 9876543)
    restarted.client.get_entity.assert_not_awaited()
    restarted.client.get_dialogs.assert_not_awaited()
    event = restarted._event(message(99, outgoing=True), restarted.config)
    assert event.metadata["canonical_peer"] == "42"
    assert event.metadata["aliases"] == ["@customer"]


@pytest.mark.asyncio
async def test_send_extracts_id_from_update_message_id():
    a = adapter()
    nonce = TelegramAdapter._random_id(CLIENT_ID)
    a.client.call.return_value = types.Updates(
        updates=[types.UpdateMessageID(id=35, random_id=nonce)], users=[], chats=[], date=NOW, seq=1)
    assert await a.send("@customer", "hello", CLIENT_ID) == "35"


@pytest.mark.asyncio
async def test_reassigned_username_cannot_silently_change_conversation_recipient():
    a = adapter()
    a.config["tg_aliases"] = {"41": ["@customer"]}
    with pytest.raises(ProviderError, match="telegram_peer_identity_changed"):
        await a.send("@customer", "hello", CLIENT_ID)
    a.client.call.assert_not_awaited()


@pytest.mark.asyncio
async def test_flood_wait_is_structured_and_not_treated_as_delivered():
    a = adapter()
    a.client.call.side_effect = errors.FloodWaitError(request=None, capture=37)
    with pytest.raises(ProviderError) as raised:
        await a.send("@customer", "hello", CLIENT_ID)
    assert raised.value.code == "telegram_flood_wait"
    assert raised.value.retry_after == 37
    assert not raised.value.uncertain


@pytest.mark.asyncio
async def test_timeout_is_uncertain_only_after_send_begins():
    a = adapter()
    a.client.get_entity.side_effect = TimeoutError("sensitive-provider-data")
    with pytest.raises(ProviderError) as before:
        await a.send("@customer", "hello", CLIENT_ID)
    assert not before.value.uncertain
    a.client.get_entity.side_effect = None
    a.client.call.side_effect = TimeoutError("sensitive-provider-data")
    with pytest.raises(ProviderError) as after:
        await a.send("@customer", "hello", CLIENT_ID)
    assert after.value.uncertain
    assert "sensitive-provider-data" not in str(after.value)


@pytest.mark.asyncio
async def test_duplicate_nonce_does_not_claim_the_message_failed_to_send():
    a = adapter()
    a.client.call.side_effect = errors.RandomIdDuplicateError(request=None)
    with pytest.raises(ProviderError) as caught:
        await a.send("@customer", "hello", CLIENT_ID)
    assert caught.value.code == "telegram_nonce_already_used"
    assert caught.value.uncertain is True


@pytest.mark.asyncio
async def test_disconnect_revokes_before_forgetting_session():
    a = adapter()
    client = a.client
    result = await a.auth("disconnect", {})
    assert result.state == "disconnected"
    client.log_out.assert_awaited_once()
    assert "session" not in a.config
    assert not any(k.startswith("tg_") for k in a.config)
    assert a.config["api_id"] == 12345
    assert a.client is None


@pytest.mark.asyncio
async def test_failed_logout_retains_session_for_retry():
    a = adapter()
    a.client.log_out.return_value = False
    with pytest.raises(ProviderError, match="telegram_logout_failed"):
        await a.auth("disconnect", {})
    assert a.config["session"] == "private-session"


@pytest.mark.asyncio
async def test_sync_is_explicit_bounded_personal_and_atomic_with_difference():
    a = adapter()
    a.client.get_dialogs.return_value = [
        SimpleNamespace(entity=user()), SimpleNamespace(entity=user(999, bot=True)),
        SimpleNamespace(entity=user(1, is_self=True)), SimpleNamespace(entity=types.PeerChat(9)),
    ]
    result = await a.auth("sync", {"dialogs": 10, "messages": 20})
    assert result.public["personal_dialogs"] == 1
    assert a.config["tg_sync"] == ["42"]
    a.client.get_dialogs.assert_awaited_once_with(limit=10)
    a.client.get_messages.return_value = [message(1), message(2, outgoing=True)]
    a.client.call.return_value = types.updates.DifferenceEmpty(NOW, 5)
    events = await a.poll()
    assert [e.provider_id for e in events if not e.metadata.get("conversation_only")] == ["1", "2"]
    assert {e.peer for e in events if e.metadata.get("conversation_only")} == {"1", "42", "999"}
    a.client.get_messages.assert_any_await(types.InputPeerUser(42, 9876543), limit=20)
    assert a.config["tg_sync"] == []


@pytest.mark.asyncio
async def test_history_failure_still_commits_live_difference_and_retains_history_job():
    a = adapter()
    a.config["tg_entities"] = {"42": {"hash": 9876543}}
    a.config["tg_sync"] = ["42"]
    a.client.call.return_value = types.updates.Difference(
        new_messages=[message(101)], new_encrypted_messages=[], other_updates=[],
        chats=[], users=[user()], state=state(20))
    a.client.get_messages.side_effect = TimeoutError()
    events = await a.poll()
    assert [event.provider_id for event in events] == ["101"]
    assert a.config["tg_cursor"]["pts"] == 20
    assert a.config["tg_sync"] == ["42"]
    assert a.config["history_status"]["state"] == "partial"


@pytest.mark.asyncio
async def test_large_gap_never_silently_advances_cursor_and_can_explicitly_recover():
    a = adapter()
    a.client.call.return_value = types.updates.DifferenceTooLong(pts=10000)
    with pytest.raises(ProviderError, match="telegram_history_gap_sync_required"):
        await a.poll()
    assert a.config["tg_cursor"]["pts"] == 10
    a.client.call.return_value = state(10000)
    result = await a.auth("sync", {"recover_gap": True})
    assert result.public["bounded_recovery"] is True
    assert a.config["tg_cursor"]["pts"] == 10000


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [errors.AuthKeyUnregisteredError, errors.AuthKeyInvalidError,
                                       errors.SessionExpiredError, errors.SessionRevokedError])
async def test_expired_session_does_not_expose_rpc_details(error_type):
    a = adapter()
    a.client.call.side_effect = error_type(request=None)
    with pytest.raises(ProviderError, match="telegram_session_expired"):
        await a.poll()


def test_service_and_media_content_are_not_misrepresented():
    a = adapter()
    assert a._event(message(1, peer=777000), a.config) is None
    media = message(2, text="", media=types.MessageMediaPhoto())
    event = a._event(media, a.config)
    assert event.text == "[Вложение: откройте Telegram]"
    assert event.metadata["media_not_downloaded"] is True


@pytest.mark.asyncio
async def test_production_client_disables_internal_update_queue_and_auto_rpc_retry(monkeypatch):
    fake = FakeClient()
    fake.is_connected.return_value = False
    factory = Mock(return_value=fake)
    monkeypatch.setattr("app.adapters.telegram.TelegramClient", factory)
    a = TelegramAdapter({"api_id": 12345, "api_hash": "a" * 32})
    await a.start()
    fake.connect.assert_awaited_once()
    assert factory.call_args.kwargs["receive_updates"] is False
    assert factory.call_args.kwargs["request_retries"] == 0
    assert factory.call_args.kwargs["flood_sleep_threshold"] == 0


@pytest.mark.asyncio
async def test_revoked_key_at_connect_allows_new_unauthenticated_login(monkeypatch):
    a = adapter()
    revoked = a.client
    revoked.is_connected.return_value = False
    revoked.connect.side_effect = errors.SessionRevokedError(request=None)
    fresh = FakeClient()
    fresh.is_connected.return_value = False
    fresh.is_user_authorized.return_value = False
    monkeypatch.setattr("app.adapters.telegram.TelegramClient", Mock(return_value=fresh))
    await a.start()
    revoked.disconnect.assert_awaited_once()
    fresh.connect.assert_awaited_once()
    assert "session" not in a.config
    assert a.config["tg_cursor"]["pts"] == 10  # Preserve history recovery position.
    result = await a.auth("login", {})
    assert result.state == "awaiting_code"
    fresh.send_code_request.assert_awaited_once_with("+79990000001")


@pytest.mark.asyncio
async def test_temporarily_missing_auth_key_is_preserved_not_treated_as_revocation():
    a = adapter()
    a.client.is_connected.return_value = False
    a.client.connect.side_effect = errors.AuthKeyNotFound()
    with pytest.raises(ProviderError, match="telegram_auth_key_unavailable"):
        await a.start()
    assert a.config["session"] == "private-session"
    a.client.disconnect.assert_not_awaited()


@pytest.mark.asyncio
async def test_first_contact_phone_uses_official_resolve_without_importing_contacts():
    a = adapter()
    # Response order is not the identity contract: select resolved.peer.user_id.
    resolved = types.contacts.ResolvedPeer(peer=types.PeerUser(42), chats=[], users=[user(99), user(42)])
    sent = types.UpdateShortSentMessage(id=501, pts=11, pts_count=1, date=NOW, out=True)
    a.client.call.side_effect = [resolved, sent]
    assert await a.send("+79990000000", "Hello after our call", CLIENT_ID) == "501"
    requests = [call.args[0] for call in a.client.call.await_args_list]
    assert isinstance(requests[0], functions.contacts.ResolvePhoneRequest)
    assert requests[0].phone == "+79990000000"
    assert isinstance(requests[1], functions.messages.SendMessageRequest)
    assert requests[1].peer == types.InputPeerUser(42, 9876543)
    a.client.get_entity.assert_not_awaited()
    a.client.get_dialogs.assert_not_awaited()
    assert a.config["tg_aliases"]["42"] == ["+79990000000"]
    assert a.config["tg_entities"]["42"]["hash"] == 9876543
    event = a._event(message(502), a.config)
    assert event.metadata["canonical_peer"] == "42"
    assert "+79990000000" in event.metadata["aliases"]


@pytest.mark.asyncio
@pytest.mark.parametrize("error_type", [errors.PhoneNotOccupiedError, errors.UserPrivacyRestrictedError])
async def test_unavailable_phone_is_specific_error_before_send(error_type):
    a = adapter()
    a.client.call.side_effect = error_type(request=None)
    with pytest.raises(ProviderError) as caught:
        await a.send("+79990000000", "Hello", CLIENT_ID)
    assert caught.value.code == "telegram_phone_not_found_or_private"
    assert not caught.value.uncertain
    assert a.client.call.await_count == 1
    assert isinstance(a.client.call.call_args.args[0], functions.contacts.ResolvePhoneRequest)
    assert "tg_aliases" not in a.config


@pytest.mark.asyncio
async def test_phone_response_without_matching_full_user_does_not_send():
    a = adapter()
    a.client.call.return_value = types.contacts.ResolvedPeer(peer=types.PeerUser(42), chats=[], users=[user(99)])
    with pytest.raises(ProviderError, match="telegram_phone_not_found_or_private"):
        await a.send("+79990000000", "Hello", CLIENT_ID)
    assert a.client.call.await_count == 1
    assert "tg_entities" not in a.config


@pytest.mark.asyncio
async def test_phone_lookup_is_limited_to_one_per_three_seconds_after_restart(monkeypatch):
    monkeypatch.setattr("app.adapters.telegram.time.time", lambda: 100.0)
    a = adapter()
    a.client.call.return_value = types.contacts.ResolvedPeer(peer=types.PeerUser(42), chats=[], users=[user()])
    await a._resolve_peer("+79990000000")
    restarted = adapter(deepcopy(a.config))
    with pytest.raises(ProviderError) as caught:
        await restarted._resolve_peer("+79990000001")
    assert caught.value.code == "telegram_phone_lookup_rate_limited"
    assert caught.value.retry_after == 3
    restarted.client.call.assert_not_awaited()
    monkeypatch.setattr("app.adapters.telegram.time.time", lambda: 103.0)
    restarted.client.call.return_value = a.client.call.return_value
    assert await restarted._resolve_peer("+79990000001") == types.InputPeerUser(42, 9876543)
    assert restarted.client.call.await_count == 1


@pytest.mark.asyncio
async def test_reassigned_phone_cannot_silently_change_recipient():
    a = adapter()
    a.config["tg_aliases"] = {"41": ["+79990000000"]}
    a.client.call.return_value = types.contacts.ResolvedPeer(peer=types.PeerUser(42), chats=[], users=[user()])
    with pytest.raises(ProviderError, match="telegram_peer_identity_changed"):
        await a.send("+79990000000", "Hello", CLIENT_ID)
    assert a.client.call.await_count == 1
    assert isinstance(a.client.call.call_args.args[0], functions.contacts.ResolvePhoneRequest)
    assert a.config["tg_aliases"] == {"41": ["+79990000000"]}


@pytest.mark.asyncio
async def test_progressive_discovery_paginates_cloud_and_archived_empty_dialogs_after_restart():
    a = adapter()
    first_page = [SimpleNamespace(entity=user(i), message=message(1000 - i, peer=i)) for i in range(10, 60)]
    archived = types.Chat(id=77, title="Archived team", photo=types.ChatPhotoEmpty(), participants_count=2, date=NOW, version=1)
    calls = []

    async def dialogs(**kwargs):
        calls.append(kwargs)
        if kwargs["folder"] == 1:
            return [SimpleNamespace(entity=archived, message=None), SimpleNamespace(entity=user(1, is_self=True), message=None)]
        return [SimpleNamespace(entity=user(60), message=None)] if "offset_peer" in kwargs else first_page

    all_events = []
    for _ in range(3):
        a.client.call.return_value = types.updates.DifferenceEmpty(NOW, 5)
        a.client.get_dialogs.side_effect = dialogs
        all_events.extend(await a.poll())
        a = adapter(deepcopy(a.config))  # Traversal must survive an actual adapter reconstruction.
    discovered = {event.peer: event for event in all_events if event.metadata.get("conversation_only")}
    assert len(discovered) == 53
    assert discovered["-77"].peer_name == "Archived team"
    assert discovered["1"].peer_name == "Избранное"
    assert all(not event.provider_id and not event.text for event in discovered.values())
    assert calls[1]["offset_peer"] == types.InputPeerUser(59, 9876543)
    assert calls[1]["offset_id"] == 941 and calls[1]["ignore_pinned"] is True
    assert calls[2]["folder"] == 1
    assert a.config["tg_history"]["discovery_done"] is True
    assert a.config["history_status"]["chats_discovered"] == 53


@pytest.mark.asyncio
async def test_history_pages_replay_and_keep_live_difference_independent(monkeypatch):
    a = adapter()
    monkeypatch.setattr("app.adapters.telegram.time.time", lambda: NOW.timestamp() + 10)
    await a.request_history({"history_days": 3650})
    persisted = deepcopy(a.config)
    a.client.get_dialogs.return_value = [SimpleNamespace(entity=user(), message=message(300))]
    live = types.Message(id=999, peer_id=types.PeerUser(42), date=NOW + timedelta(seconds=20), message="Live")
    difference = types.updates.Difference(new_messages=[live], new_encrypted_messages=[], other_updates=[], chats=[], users=[user()], state=state(20))
    a.client.call.return_value = difference
    a.client.get_messages.return_value = [message(i) for i in range(300, 200, -1)]
    first = await a.poll()
    replay = adapter(persisted)
    replay.client.call.return_value = difference
    replay.client.get_dialogs.return_value = a.client.get_dialogs.return_value
    replay.client.get_messages.return_value = a.client.get_messages.return_value
    assert await replay.poll() == first
    assert a.config["tg_history"]["chats"]["42"]["before"] == 201
    assert a.config["tg_cursor"]["pts"] == 20
    assert next(event for event in first if event.provider_id == "999").metadata["historical"] is False
    assert all(event.metadata["historical"] for event in first if event.provider_id and event.provider_id != "999")
    restarted = adapter(deepcopy(a.config))
    restarted.client.call.return_value = types.updates.DifferenceEmpty(NOW, 5)
    restarted.client.get_messages.return_value = [message(200)]
    await restarted.poll()
    assert restarted.client.get_messages.call_args.kwargs["offset_id"] == 201
    assert restarted.config["tg_history"]["chats"]["42"]["done"] is True


@pytest.mark.asyncio
async def test_manual_history_recovers_explicit_difference_gap_before_snapshot():
    a = adapter()
    old_cursor = deepcopy(a.config["tg_cursor"])
    await a.request_history({"history_days": 3650})
    assert a.config["tg_cursor"] == old_cursor  # Ordinary manual import never resets live delivery.
    a.client.call.side_effect = [types.updates.DifferenceTooLong(9999), state(10000)]
    a.client.get_dialogs.return_value = [SimpleNamespace(entity=user(), message=message(20))]
    a.client.get_messages.return_value = [message(20)]
    events = await a.poll()
    assert [event.provider_id for event in events if event.provider_id] == ["20"]
    assert a.config["tg_cursor"]["pts"] == 10000
    assert a.config["tg_history"]["recovered_gap"] is True
    assert "tg_history_recover_gap" not in a.config
    assert isinstance(a.client.call.call_args_list[1].args[0], functions.updates.GetStateRequest)
    a.client.call.side_effect = None
    a.client.call.return_value = types.updates.DifferenceEmpty(NOW, 6)
    await a.poll()
    assert a.client.call.call_args.args[0].pts == 10000


@pytest.mark.asyncio
async def test_channel_live_cursor_is_distinct_from_backfill_and_ids_are_per_peer():
    a = adapter()
    channel = types.Channel(id=88, title="Team", photo=types.ChatPhotoEmpty(), date=NOW, access_hash=777, megagroup=True)
    a.client.get_dialogs.return_value = [SimpleNamespace(entity=channel, message=SimpleNamespace(id=100))]
    a.client.call.return_value = types.updates.Difference(new_messages=[message(101)], new_encrypted_messages=[], other_updates=[], chats=[], users=[user()], state=state(20))
    old = types.Message(id=90, peer_id=types.PeerChannel(88), date=NOW, message="Old group")
    new = types.Message(id=101, peer_id=types.PeerChannel(88), date=NOW, message="New group")

    async def messages(entity, **kwargs):
        assert entity == types.InputPeerChannel(88, 777)
        if kwargs.get("reverse"):
            assert kwargs["min_id"] == 100
            return [new]
        return [old]

    a.client.get_messages.side_effect = messages
    events = await a.poll()
    assert {(event.peer, event.provider_id) for event in events if event.provider_id} == {
        ("42", "101"), ("-1000000000088", "90"), ("-1000000000088", "101")}
    assert a.config["tg_live_channels"]["-1000000000088"] == 101
    assert await a._resolve_peer("-1000000000088") == types.InputPeerChannel(88, 777)


@pytest.mark.asyncio
@pytest.mark.parametrize("days", [0, 3651, True, "30", 1.5])
async def test_history_depth_is_validated_before_mutation(days):
    a = adapter()
    before = deepcopy(a.config)
    with pytest.raises(ProviderError, match="history_days_invalid"):
        await a.request_history({"history_days": days})
    assert a.config == before


@pytest.mark.asyncio
async def test_channel_top_message_from_same_second_is_not_lost_between_live_and_history(monkeypatch):
    monkeypatch.setattr("app.adapters.telegram.time.time", lambda: NOW.timestamp() + 0.25)
    a = adapter()
    channel = types.Channel(id=88, title="Team", photo=types.ChatPhotoEmpty(), date=NOW, access_hash=777)
    top = types.Message(id=100, peer_id=types.PeerChannel(88), date=NOW, message="At discovery second")
    a.client.get_dialogs.return_value = [SimpleNamespace(entity=channel, message=top)]
    a.client.call.return_value = types.updates.DifferenceEmpty(NOW, 5)
    async def messages(_entity, **kwargs):
        if kwargs.get("reverse"):
            return []  # min_id is exclusive and has already reached top_id.
        # Actual Telegram GetHistory offset_date is exclusive integer seconds.
        assert int(kwargs["offset_date"].timestamp()) == int(NOW.timestamp()) + 1
        return [top] if top.date.timestamp() < int(kwargs["offset_date"].timestamp()) else []
    a.client.get_messages.side_effect = messages
    events = await a.poll()
    assert [event.provider_id for event in events if event.provider_id] == ["100"]
    assert a.config["tg_history"]["chats"]["-1000000000088"]["done"] is True


@pytest.mark.asyncio
async def test_provider_dialog_title_refresh_emits_metadata_event_without_fake_message(monkeypatch):
    clock = [NOW.timestamp()]
    monkeypatch.setattr("app.adapters.telegram.time.time", lambda: clock[0])
    a = adapter()
    a.client.call.return_value = types.updates.DifferenceEmpty(NOW, 5)
    a.client.get_dialogs.return_value = [SimpleNamespace(entity=user(), message=None)]
    await a.poll()
    await a.poll()
    clock[0] += 61
    a.client.get_dialogs.return_value[0].entity.first_name = "Changed provider name"
    events = await a.poll()
    assert len(events) == 1
    assert events[0].metadata["conversation_only"] and events[0].peer_name == "Changed provider name"
    assert events[0].text == "" and events[0].provider_id == ""
