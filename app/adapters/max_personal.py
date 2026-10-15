"""Experimental corporate MAX account integration, pinned to maxapi-python 2.4.1.

Uses PyMax's StoreProtocol and custom AuthFlow extension points. Authentication
challenges and sessions live in the core-encrypted account config, never SQLite.
Advanced send API is necessary to supply a deterministic protocol cid.
"""
import asyncio
import os
import re
import base64
from io import BytesIO
import time
import uuid
from typing import Any

from .base import Adapter, AuthResult, Event, ProviderError


def _qr_data_url(value: str) -> str:
    # Render locally: the QR contains a temporary login credential.
    import qrcode
    output = BytesIO()
    qrcode.make(value).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


class _PendingAuth(Exception):
    def __init__(self, result: AuthResult):
        self.result = result


class _ConfigSessionStore:
    """Duck-typed PyMax StoreProtocol; the caller encrypts the config at commit."""
    def __init__(self, config):
        self.config = config

    async def save_session(self, session_info):
        self.config["_max_session"] = session_info.model_dump(mode="json")

    async def load_session(self):
        from pymax.session.models import SessionInfo
        raw = self.config.get("_max_session")
        return SessionInfo.model_validate(raw) if raw else None

    async def load_session_by_device_id(self, device_id):
        session = await self.load_session()
        return session if session and session.device_id == device_id else None

    async def load_session_by_phone(self, phone):
        session = await self.load_session()
        return session if session and session.phone == phone else None

    async def update_token(self, old_token, new_token):
        session = self.config.get("_max_session")
        if session and session["token"] == old_token:
            session["token"] = new_token

    async def delete_session(self, token):
        session = self.config.get("_max_session")
        if session and session["token"] == token:
            self.config.pop("_max_session", None)

    async def close(self):
        pass


class _SteppedAuthFlow:
    """One provider request stage per job; survives restart between UI actions."""
    def __init__(self, config, action, payload):
        self.config, self.action, self.payload = config, action, payload

    async def authenticate(self, app):
        from pymax.auth.models import AuthResult as MaxAuthResult
        api = app.api.auth
        pending = self.config.get("_max_pending", {})
        # Device fingerprint must be stable even before a session token exists.
        self.config["_max_user_agent"] = app.config.device.user_agent.model_dump(mode="json")
        self.config["_max_mt_instance_id"] = app.config.device.mt_instance_id
        if self.action == "login":
            if self.config.get("login_method", "qr") == "sms":
                phone = self.config.get("phone", "")
                if not phone.startswith("+") or not phone[1:].isdigit():
                    raise ProviderError("max_phone_required")
                response = await api.request_code(phone)
                self.config["_max_pending"] = {"kind": "code", "token": response.token}
                raise _PendingAuth(AuthResult("code_required", {"code_length": response.code_length, "experimental": True}))
            response = await api.request_qr()
            self.config["_max_pending"] = {
                "kind": "qr", "track_id": response.track_id,
                "qr_text": response.qr_link, "expires_at": response.expires_at,
            }
            raise _PendingAuth(AuthResult("qr_required", {
                "qr_text": response.qr_link, "qr_data_url": _qr_data_url(response.qr_link), "expires_at": response.expires_at / 1000, "experimental": True,
            }))
        if self.action == "code" and pending.get("kind") == "code":
            code = str(self.payload.get("code", ""))
            if not code.isdigit():
                raise ProviderError("invalid_max_code")
            response = await api.send_code(pending["token"], code)
        elif self.action == "password" and pending.get("kind") == "password":
            password = str(self.payload.get("password", ""))
            if not password:
                raise ProviderError("max_password_required")
            response = await api.check_password(pending["track_id"], password)
        elif self.action == "sync" and pending.get("kind") == "qr":
            if pending["expires_at"] <= int(time.time() * 1000):
                self.config.pop("_max_pending", None)
                raise _PendingAuth(AuthResult("login_required", {"reason": "qr_expired", "experimental": True}))
            status = await api.check_qr(pending["track_id"])
            if not status.status.login_available:
                raise _PendingAuth(AuthResult("qr_required", {
                    "qr_text": pending["qr_text"], "qr_data_url": _qr_data_url(pending["qr_text"]), "expires_at": pending["expires_at"] / 1000, "experimental": True,
                }))
            response = await api.confirm_qr(pending["track_id"])
        else:
            raise ProviderError("max_auth_action_out_of_sequence")
        if response.login_token:
            self.config.pop("_max_pending", None)
            return MaxAuthResult(token=response.login_token)
        challenge = getattr(response, "password_challenge", None)
        if challenge:
            self.config["_max_pending"] = {"kind": "password", "track_id": challenge.track_id}
            raise _PendingAuth(AuthResult("password_required", {"hint": challenge.hint, "experimental": True}))
        # Never silently register a new MAX account.
        raise ProviderError("max_credentials_rejected_or_registration_required")


class MaxPersonalAdapter(Adapter):
    PAGE_SIZE = 100
    CHATS_PER_POLL = 10

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client = None
        self._known_chats = set()

    def _check_enabled(self):
        if os.environ.get("ENABLE_EXPERIMENTAL", "").lower() != "true":
            raise ProviderError("experimental_adapter_disabled")
        try:
            import pymax  # noqa: F401
        except ImportError as exc:
            raise ProviderError("install_maxapi_python_2_4_1") from exc

    def _make_client(self, action="restore", payload=None):
        self._check_enabled()
        from pymax import Client, ExtraConfig, WebClient
        from pymax.api.session.payloads import MobileUserAgentPayload
        self.config.setdefault("_max_device_id", str(uuid.uuid4()))
        extra_args = {
            "store": _ConfigSessionStore(self.config), "persist_session": True,
            "device_id": self.config["_max_device_id"], "reconnect": False,
            "relogin": False, "telemetry": False, "request_timeout": 25.0,
            "log_level": "ERROR",
        }
        if self.config.get("_max_mt_instance_id"):
            extra_args["mt_instance_id"] = self.config["_max_mt_instance_id"]
        if self.config.get("_max_user_agent"):
            extra_args["user_agent"] = MobileUserAgentPayload.model_validate(self.config["_max_user_agent"])
        kwargs = {"extra_config": ExtraConfig(**extra_args), "auth_flow": _SteppedAuthFlow(self.config, action, payload or {})}
        if self.config.get("login_method", "qr") == "sms":
            client = Client(phone=self.config.get("phone", ""), **kwargs)
        else:
            client = WebClient(**kwargs)

        @client.on_message()
        async def remember_chat(message, _client):
            # Messages themselves are replayed from remote history. An in-memory
            # push callback is never the sole source of durable message content.
            if message.chat_id is not None:
                self._known_chats.add(str(message.chat_id))
        return client

    async def start(self) -> None:
        self._check_enabled()
        if self.client and self.client.is_connected:
            return
        if not self.config.get("_max_session"):
            return
        if self.client:
            await self.close()
        self.client = self._make_client()
        try:
            await asyncio.wait_for(self.client.connect(), 45)
        except Exception as exc:
            await self.close()
            raise ProviderError("max_restore_failed") from exc

    async def auth(self, action: str, payload: dict) -> AuthResult:
        self._check_enabled()
        if action == "disconnect":
            if self.config.get("_max_session"):
                await self.start()
                try:
                    revoked = await asyncio.wait_for(self.client.logout(), 30)
                except Exception as exc:
                    raise ProviderError("max_revoke_unconfirmed") from exc
                if not revoked:
                    raise ProviderError("max_revoke_unconfirmed")
            await self.close()
            for key in list(self.config):
                if key.startswith("_max_"):
                    self.config.pop(key)
            return AuthResult("disconnected")
        if action not in {"login", "code", "password", "sync"}:
            raise ProviderError("unsupported_action")
        if self.config.get("_max_session"):
            await self.start()
            return AuthResult("connected", {"experimental": True})
        pending = self.config.get("_max_pending", {})
        if action == "sync" and pending.get("kind") in {"code", "password"}:
            return AuthResult(pending["kind"] + "_required", {"experimental": True})
        await self.close()
        self.client = self._make_client(action, payload)
        try:
            await asyncio.wait_for(self.client.connect(), 45)
        except _PendingAuth as pending:
            await self.close()
            return pending.result
        except ProviderError:
            await self.close()
            raise
        except Exception as exc:
            await self.close()
            raise ProviderError("max_auth_failed") from exc
        return AuthResult("connected", {"experimental": True})

    async def _resolve_peer(self, peer: str) -> int:
        phone = peer.removeprefix("phone:")
        if phone.startswith("+") or peer.startswith("phone:"):
            if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                raise ProviderError("invalid_max_phone_use_e164")
            # Look up each explicit phone send afresh; a persisted alias is for
            # history reconciliation, never proof that a number still belongs
            # to the same account after a number transfer.
            user = None
            try:
                user = await asyncio.wait_for(self.client.search_by_phone(phone), 20)
            except Exception:
                # MAX can decline discovery by phone. Existing login/sync
                # contacts are a supported alternate source of the same ID.
                pass
            if user is None:
                matches = [contact for contact in (getattr(self.client, "contacts", None) or [])
                           if contact is not None and str(getattr(contact, "phone", "") or "").lstrip("+") == phone[1:]]
                if len(matches) > 1:
                    raise ProviderError("max_phone_ambiguous")
                user = matches[0] if matches else None
            if user is None:
                raise ProviderError("max_phone_not_found_or_private")
            user_id = user.id
            aliases = {peer, phone, "phone:" + phone}
        elif peer.startswith("user:"):
            try:
                user_id = int(peer.removeprefix("user:"))
            except ValueError as exc:
                raise ProviderError("invalid_max_user_id") from exc
            if user_id <= 0:
                raise ProviderError("invalid_max_user_id")
            aliases = {peer}
        else:
            try:
                return int(peer)
            except ValueError as exc:
                raise ProviderError("invalid_max_peer") from exc
        if not self.client.me:
            raise ProviderError("max_own_profile_missing")
        chat_id = self.client.get_chat_id(self.client.me.contact.id, user_id)
        known = self.config.setdefault("_max_peer_aliases", {})
        for known_chat, known_aliases in known.items():
            if known_chat != str(chat_id) and aliases.intersection(known_aliases):
                # A recycled phone must not silently redirect a CRM contact.
                raise ProviderError("max_peer_identity_changed")
        stored = known.setdefault(str(chat_id), [])
        known[str(chat_id)] = sorted(set(stored) | aliases | {"user:" + str(user_id)})
        return chat_id

    async def send(self, peer: str, text: str, client_id: str) -> str:
        await self.start()
        if not self.client:
            raise ProviderError("max_login_required")
        try:
            nonce = uuid.UUID(client_id).int & ((1 << 53) - 1)
        except ValueError as exc:
            raise ProviderError("invalid_max_peer_or_client_id") from exc
        chat_id = await self._resolve_peer(peer)
        # Persist the destination before the send, so an ambiguous send can be
        # recovered through history even after a worker restart.
        self.config.setdefault("_max_peer_aliases", {}).setdefault(str(chat_id), [])
        self._known_chats.add(str(chat_id))
        from pymax.api.messages.payloads import SendMessagePayload, SendMessagePayloadMessage
        from pymax.api.response import require_payload_model
        from pymax.protocol import Opcode
        from pymax.types import Message
        # High-level send_message generates a new cid on every call. Using the
        # verified advanced API preserves the outbox UUID across transport loss.
        frame = SendMessagePayload(chat_id=chat_id, notify=True, message=SendMessagePayloadMessage(
            text=text, cid=nonce or 1, elements=[], attaches=[],
        ))
        try:
            result = await asyncio.wait_for(self.client._app.invoke(Opcode.MSG_SEND, frame.to_payload()), 30)
            message = require_payload_model(result, Message)
        except Exception as exc:
            # Protocol errors can happen after delivery too. Conservatively stop
            # for operator reconciliation rather than retrying automatically.
            raise ProviderError("max_send_unconfirmed", uncertain=True) from exc
        self._known_chats.add(str(chat_id))
        return str(message.id)

    async def poll(self) -> list[Event]:
        await self.start()
        if not self.client:
            return []
        chats = {str(chat.id): chat for chat in (self.client.chats or [])}
        self._known_chats.update(chats)
        self._known_chats.update(self.config.get("_max_cursors", {}))
        self._known_chats.update(self.config.get("_max_peer_aliases", {}))
        peers = sorted(self._known_chats)
        if not peers:
            return []
        offset = int(self.config.get("_max_chat_offset", 0)) % len(peers)
        selected = peers[offset:offset + self.CHATS_PER_POLL]
        cursors = self.config.setdefault("_max_cursors", {})
        own_id = self.client.me.contact.id if self.client.me else None
        events = []
        for peer in selected:
            cursor = cursors.setdefault(peer, {})
            if "before" not in cursor:
                now = int(time.time() * 1000)
                days = min(max(int(self.config.get("history_days", 30)), 1), 3650)
                cursor.update(floor=cursor.get("watermark", now - days * 86400000), ceiling=now, before=now)
            try:
                messages = await asyncio.wait_for(self.client.fetch_history(
                    chat_id=int(peer), backward=self.PAGE_SIZE, forward=0, from_time=cursor["before"],
                ), 30)
            except Exception as exc:
                raise ProviderError("max_history_failed") from exc
            for message in messages:
                if not cursor["floor"] <= message.time <= cursor["ceiling"]:
                    continue
                outbound = own_id is not None and message.sender == own_id
                events.append(Event(
                    provider_id=str(message.id), peer=peer,
                    text=message.text or ("[Вложение]" if message.attaches else ""),
                    direction="outbound" if outbound else "inbound", timestamp=message.time / 1000,
                    peer_name=getattr(chats.get(peer), "title", None),
                    status="sent" if outbound else "received",
                    metadata={"has_media": bool(message.attaches), "canonical_peer": peer,
                              "aliases": self.config.get("_max_peer_aliases", {}).get(peer, [])},
                ))
            oldest = min((message.time for message in messages), default=0)
            if len(messages) < self.PAGE_SIZE or oldest <= cursor["floor"]:
                # Overlap by a minute so slightly late provider events replay.
                cursors[peer] = {"watermark": cursor["ceiling"] - 60000}
            elif oldest >= cursor["before"]:
                # More than one provider page at the same millisecond cannot be
                # disambiguated by this API. Surface a gap, never skip silently.
                raise ProviderError("max_history_timestamp_boundary_overflow")
            else:
                cursor["before"] = oldest
        self.config["_max_chat_offset"] = (offset + len(selected)) % len(peers)
        return events

    async def close(self) -> None:
        if self.client:
            client, self.client = self.client, None
            await client.close()
