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


def _auth_error(exc: Exception, action: str) -> ProviderError:
    """Never expose SDK exceptions: they can contain a provider payload."""
    from pymax.exceptions import ApiError
    from pymax.protocol import Opcode
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, (ConnectionError, OSError, EOFError, TimeoutError)):
        return ProviderError("max_transport_error")
    if isinstance(exc, ApiError):
        if exc.opcode == Opcode.LOGIN and any(
                value in {"FAIL_LOGIN_TOKEN", "FAIL_LOGOUT_ALL"} for value in (exc.error, exc.message)):
            return ProviderError("max_session_expired")
        if action in {"code", "password"}:
            return ProviderError("max_code_invalid" if action == "code" else "max_password_invalid")
    return ProviderError("max_auth_failed")


def _pending_public(config: dict) -> AuthResult:
    pending = config.get("_max_pending", {})
    method = config.get("login_method", "qr")
    public = {"experimental": True, "auth_method": method}
    if method == "sms":
        public["phone"] = config.get("phone", "")
    if pending.get("kind") == "qr":
        public.update(qr_data_url=_qr_data_url(pending["qr_text"]),
                      expires_at=pending["expires_at"] / 1000,
                      polling_interval=pending.get("polling_interval", 5000) / 1000)
    elif pending.get("kind") == "code":
        public["code_length"] = pending.get("code_length", 6)
    elif pending.get("kind") == "password" and pending.get("hint"):
        public["hint"] = pending["hint"]
    return AuthResult(pending.get("kind", "login") + "_required", public)


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
                if not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone):
                    raise ProviderError("max_phone_required")
                response = await api.request_code(phone)
                self.config["_max_pending"] = {"kind": "code", "token": response.token,
                                               "code_length": response.code_length}
                raise _PendingAuth(_pending_public(self.config))
            response = await api.request_qr()
            self.config["_max_pending"] = {
                "kind": "qr", "track_id": response.track_id,
                "qr_text": response.qr_link, "expires_at": response.expires_at,
                "polling_interval": max(1000, getattr(response, "polling_interval", 5000)),
                "next_poll_at": 0,
            }
            raise _PendingAuth(_pending_public(self.config))
        if self.action == "code" and pending.get("kind") == "code":
            code = str(self.payload.get("code", ""))
            if not re.fullmatch(r"[0-9]{4,12}", code):
                raise ProviderError("invalid_max_code")
            response = await api.send_code(pending["token"], code)
        elif self.action == "password" and pending.get("kind") == "password":
            password = str(self.payload.get("password", ""))
            if not password:
                raise ProviderError("max_password_required")
            response = await api.check_password(pending["track_id"], password)
            if getattr(response, "error", None):
                raise ProviderError("max_password_invalid")
        elif self.action == "sync" and pending.get("kind") == "qr":
            if pending["expires_at"] <= int(time.time() * 1000):
                self.config.pop("_max_pending", None)
                raise _PendingAuth(AuthResult("login_required", {"reason": "qr_expired", "experimental": True}))
            if time.time() * 1000 < pending.get("next_poll_at", 0):
                raise _PendingAuth(_pending_public(self.config))
            status = await api.check_qr(pending["track_id"])
            pending["next_poll_at"] = int(time.time() * 1000) + pending.get("polling_interval", 5000)
            if not status.status.login_available:
                raise _PendingAuth(_pending_public(self.config))
            response = await api.confirm_qr(pending["track_id"])
        else:
            raise ProviderError("max_auth_action_out_of_sequence")
        if response.login_token:
            self.config.pop("_max_pending", None)
            return MaxAuthResult(token=response.login_token)
        challenge = getattr(response, "password_challenge", None)
        if challenge:
            self.config["_max_pending"] = {"kind": "password", "track_id": challenge.track_id,
                                           "hint": challenge.hint}
            raise _PendingAuth(_pending_public(self.config))
        # Never silently register a new MAX account.
        raise ProviderError("max_credentials_rejected_or_registration_required")


class _ResumableAuthFlow:
    """Keep SDK bootstrap/handshake alive while each CRM job supplies one step.

    Only private challenge metadata is persisted. A restarted worker recreates
    the transport and resumes that challenge; code/password stay in memory only.
    """
    def __init__(self, config, action, payload, commands, results):
        self.config, self.action, self.payload = config, action, payload
        self.commands, self.results = commands, results

    async def authenticate(self, app):
        action, payload = self.action, self.payload
        self.payload = None
        while True:
            try:
                return await _SteppedAuthFlow(self.config, action, payload).authenticate(app)
            except _PendingAuth as pending:
                self.results.put_nowait(pending.result)
            except Exception as exc:
                error = _auth_error(exc, action)
                if error.code in {"max_transport_error", "max_session_expired"}:
                    raise error from None
                self.results.put_nowait(error)
            finally:
                payload.clear()
            action, payload = await self.commands.get()


class MaxPersonalAdapter(Adapter):
    PAGE_SIZE = 100
    CHATS_PER_POLL = 10
    AUTH_WAIT_SECONDS = 45

    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client = None
        self._known_chats = set()
        self._dirty_chats = {}
        self._message_sequence = 0
        self._connect_task = None
        self._auth_commands = asyncio.Queue()
        self._auth_results = asyncio.Queue()

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
            # SDK error tracebacks may include provider payloads. The worker
            # logs our stable error codes instead of these raw exceptions.
            "log_level": "CRITICAL",
        }
        if self.config.get("_max_mt_instance_id"):
            extra_args["mt_instance_id"] = self.config["_max_mt_instance_id"]
        if self.config.get("_max_user_agent"):
            extra_args["user_agent"] = MobileUserAgentPayload.model_validate(self.config["_max_user_agent"])
        kwargs = {"extra_config": ExtraConfig(**extra_args), "auth_flow": _ResumableAuthFlow(
            self.config, action, dict(payload or {}), self._auth_commands, self._auth_results)}
        if self.config.get("login_method", "qr") == "sms":
            client = Client(phone=self.config.get("phone", ""), **kwargs)
        else:
            client = WebClient(**kwargs)

        @client.on_message()
        async def remember_chat(message, _client):
            # Messages themselves are replayed from remote history. An in-memory
            # push callback is never the sole source of durable message content.
            if message.chat_id is not None:
                peer = str(message.chat_id)
                self._known_chats.add(peer)
                self._message_sequence += 1
                self._dirty_chats[peer] = self._message_sequence
        return client

    def _verified_identity(self) -> dict:
        contact = getattr(getattr(self.client, "me", None), "contact", None)
        user_id = getattr(contact, "id", None)
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ProviderError("max_own_profile_missing")
        previous = self.config.get("max_self")
        if previous and str(previous["id"]) != str(user_id):
            self.config["max_identity_mismatch"] = True
            raise ProviderError("max_account_identity_changed")
        identity = {"id": str(user_id)}
        phone = str(getattr(contact, "phone", "") or "").lstrip("+")
        if re.fullmatch(r"[1-9][0-9]{6,14}", phone):
            identity["phone"] = "+" + phone
        for name in getattr(contact, "names", []) or []:
            display = getattr(name, "name", None) or " ".join(filter(None, (
                getattr(name, "first_name", None), getattr(name, "last_name", None))))
            if isinstance(display, str) and display.strip():
                identity["display_name"] = display.strip()[:160]
                break
        self.config["max_self"] = {"id": str(user_id)}
        self.config.pop("max_identity_mismatch", None)
        return identity

    def _connected(self) -> AuthResult:
        if not self.client or not self.client.is_connected:
            raise ProviderError("max_transport_error")
        return AuthResult("connected", {"experimental": True, "identity": self._verified_identity()})

    def _forget_revoked_session(self) -> None:
        # Only called for the SDK's explicit invalid LOGIN token response.
        # Keep provider identity, message cursors, aliases and device fingerprint.
        self.config.pop("_max_session", None)
        self.config.pop("_max_pending", None)

    async def start(self) -> None:
        self._check_enabled()
        if self.config.get("max_identity_mismatch"):
            raise ProviderError("max_account_identity_changed")
        if self._connect_task and not self._connect_task.done():
            return  # A pending auth handshake is not an authenticated session.
        if self.client and self.client.is_connected:
            if self.config.get("max_self") or self.config.get("_max_session"):
                self._verified_identity()
            return
        if not self.config.get("_max_session"):
            return
        if self.client:
            await self.close()
        self.client = self._make_client()
        try:
            await asyncio.wait_for(self.client.connect(), 45)
            self._connected()
        except Exception as exc:
            await self.close()
            error = _auth_error(exc, "restore")
            if error.code == "max_session_expired":
                self._forget_revoked_session()
            raise error from None

    async def auth(self, action: str, payload: dict) -> AuthResult:
        self._check_enabled()
        if action == "disconnect":
            if self.config.get("_max_session"):
                try:
                    # Logout is permitted even for a rejected identity. This
                    # transport is used only to revoke its saved session; it
                    # never goes through poll/send or changes the account pin.
                    if (not self.client or not self.client.is_connected
                            or (self._connect_task and not self._connect_task.done())):
                        await self.close()
                        self.client = self._make_client()
                        await asyncio.wait_for(self.client.connect(), 45)
                    if not self.config.get("max_self"):
                        self._verified_identity()
                    revoked = await asyncio.wait_for(self.client.logout(), 30)
                except Exception as exc:
                    if _auth_error(exc, "restore").code == "max_session_expired":
                        self._forget_revoked_session()
                        revoked = True
                    else:
                        raise ProviderError("max_revoke_unconfirmed") from None
                if not revoked:
                    raise ProviderError("max_revoke_unconfirmed")
            await self.close()
            for key in list(self.config):
                if key.startswith("_max_"):
                    self.config.pop(key)
            self.config.pop("max_identity_mismatch", None)
            return AuthResult("disconnected")
        if action == "qr":
            action, payload = "login", {"login_method": "qr"}
        if action not in {"login", "code", "password", "sync"}:
            raise ProviderError("unsupported_action")
        if self.config.get("max_identity_mismatch"):
            raise ProviderError("max_account_identity_changed")
        if self.config.get("_max_session"):
            try:
                await self.start()
            except ProviderError as exc:
                if exc.code != "max_session_expired" or action != "login":
                    raise
                # An explicit new login may proceed after provider-proven
                # revocation; a network error never discards credentials.
            else:
                return self._connected()
        if action == "login":
            method = payload.get("login_method", self.config.get("login_method", "qr"))
            if method not in {"qr", "sms"}:
                raise ProviderError("max_auth_action_out_of_sequence")
            phone = payload.get("phone", self.config.get("phone", ""))
            if method == "sms" and (not isinstance(phone, str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", phone)):
                raise ProviderError("max_phone_required")
            await self.close()
            self.config["login_method"] = method
            if method == "sms":
                self.config["phone"] = phone
            self.config.pop("_max_pending", None)
            # A QR WebClient fingerprint cannot be reused as a mobile client.
            self.config.pop("_max_user_agent", None)
        pending = self.config.get("_max_pending", {})
        if action == "sync" and pending.get("kind") in {"code", "password"}:
            return _pending_public(self.config)
        if action == "sync" and not pending:
            return AuthResult("login_required", {"experimental": True})
        if self._connect_task is None or self._connect_task.done():
            await self.close()
            self.client = self._make_client(action, payload)
            self._connect_task = asyncio.create_task(self.client.connect())
        else:
            self._auth_commands.put_nowait((action, dict(payload)))
        result_task = asyncio.create_task(self._auth_results.get())
        try:
            done, _ = await asyncio.wait({self._connect_task, result_task}, timeout=self.AUTH_WAIT_SECONDS,
                                         return_when=asyncio.FIRST_COMPLETED)
            if not done:
                raise ProviderError("max_transport_error")
            if self._connect_task in done:
                await self._connect_task
                return self._connected()
            result = result_task.result()
            if isinstance(result, ProviderError):
                raise result
            if result.state == "login_required":
                await self.close()
            return result
        except asyncio.CancelledError:
            await self.close()
            raise
        except Exception as exc:
            error = _auth_error(exc, action)
            if error.code == "max_session_expired":
                self._forget_revoked_session()
            if error.code not in {"invalid_max_code", "max_code_invalid", "max_password_required", "max_password_invalid"}:
                await self.close()
            raise error from None
        finally:
            result_task.cancel()
            await asyncio.gather(result_task, return_exceptions=True)

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
        if not self.client or (self._connect_task and not self._connect_task.done()):
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
        if not self.client or (self._connect_task and not self._connect_task.done()):
            return []
        chats = {str(chat.id): chat for chat in (self.client.chats or [])}
        self._known_chats.update(chats)
        self._known_chats.update(self.config.get("_max_cursors", {}))
        self._known_chats.update(self.config.get("_max_peer_aliases", {}))
        peers = sorted(self._known_chats)
        if not peers:
            return []
        offset = int(self.config.get("_max_chat_offset", 0)) % len(peers)
        scheduled = peers[offset:offset + self.CHATS_PER_POLL]
        # Pushes choose which histories to refresh first, but are never the
        # sole durable source. Keep the fair history round-robin progressing.
        dirty = dict(self._dirty_chats)
        urgent = sorted(dirty, key=dirty.get, reverse=True)[:self.CHATS_PER_POLL]
        selected = list(dict.fromkeys(urgent + scheduled))
        cursors = self.config.setdefault("_max_cursors", {})
        own_id = self.client.me.contact.id if self.client.me else None
        events = {}
        for peer in selected:
            cursor = cursors.setdefault(peer, {})
            now = int(time.time() * 1000)
            days = min(max(int(self.config.get("history_days", 30)), 1), 3650)
            history_floor = now - days * 86400000
            if "before" not in cursor:
                cursor.update(floor=cursor.get("watermark", history_floor), ceiling=now, before=now)
            try:
                # A frozen backfill window must not hide incoming messages
                # sent after its ceiling. Read the newest page independently.
                latest = []
                if cursor["before"] < now:
                    latest = await asyncio.wait_for(self.client.fetch_history(
                        chat_id=int(peer), backward=self.PAGE_SIZE, forward=0, from_time=now,
                    ), 30)
                messages = await asyncio.wait_for(self.client.fetch_history(
                    chat_id=int(peer), backward=self.PAGE_SIZE, forward=0, from_time=cursor["before"],
                ), 30)
                if cursor["before"] == now:
                    latest = messages  # Same request also serves the independent live tail.
            except Exception as exc:
                raise ProviderError("max_history_failed") from exc
            history = [message for message in messages if cursor["floor"] <= message.time <= cursor["ceiling"]]
            for message in history + latest:
                if message.time < history_floor:
                    continue
                outbound = own_id is not None and message.sender == own_id
                events[(peer, str(message.id))] = Event(
                    provider_id=str(message.id), peer=peer,
                    text=message.text or ("[Вложение]" if message.attaches else ""),
                    direction="outbound" if outbound else "inbound", timestamp=message.time / 1000,
                    peer_name=getattr(chats.get(peer), "title", None),
                    status="sent" if outbound else "received",
                    metadata={"has_media": bool(message.attaches), "canonical_peer": peer,
                              "aliases": self.config.get("_max_peer_aliases", {}).get(peer, [])},
                )
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
            if peer in dirty and self._dirty_chats.get(peer) == dirty[peer]:
                self._dirty_chats.pop(peer, None)
        self.config["_max_chat_offset"] = (offset + len(scheduled)) % len(peers)
        return list(events.values())

    async def close(self) -> None:
        task, self._connect_task = self._connect_task, None
        if task:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        if self.client:
            client, self.client = self.client, None
            await client.close()
        self._auth_commands = asyncio.Queue()
        self._auth_results = asyncio.Queue()
