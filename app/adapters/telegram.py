"""Corporate Telegram user accounts over MTProto (Telethon, MIT).

There is no in-memory event queue: Telegram's durable difference cursor is kept
in encrypted account config and committed by the worker with the events. Only
ordinary one-to-one cloud chats are supported. Never log this config.
"""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import datetime, timezone
from io import BytesIO
import hashlib
import ipaddress
import logging
import os
import re
import math
import time
from typing import Any
from uuid import UUID
from urllib.parse import unquote, urlsplit

from python_socks import ProxyError, ProxyConnectionError, ProxyTimeoutError
from telethon import TelegramClient, errors, functions, types
from telethon.sessions import StringSession

from .base import Adapter, AuthResult, Event, ProviderError


REVOKED_KEYS = (errors.AuthKeyUnregisteredError, errors.AuthKeyInvalidError,
                errors.AuthKeyPermEmptyError, errors.SessionExpiredError,
                errors.SessionRevokedError, errors.AuthKeyDuplicatedError)


class _PrivateTransportLogger(logging.Logger):
    """Telethon interpolates raw proxy/server errors; core logs safe error codes."""

    def getChild(self, suffix):
        return self


_TRANSPORT_LOGGER = _PrivateTransportLogger("omnicrm.telegram.transport", logging.CRITICAL + 1)
_TRANSPORT_LOGGER.propagate = False
_TRANSPORT_LOGGER.addHandler(logging.NullHandler())


def _deployment_proxy() -> dict[str, Any] | None:
    """Only the deployment can select a route; never persist proxy credentials."""
    raw = os.environ.get("TELEGRAM_PROXY_URL", "")
    if raw == "":
        return None
    try:
        if len(raw) > 2048 or any(ord(c) <= 32 or 127 <= ord(c) <= 159 for c in raw):
            raise ValueError
        if re.search(r"%(?![0-9a-fA-F]{2})", raw):
            raise ValueError
        parsed = urlsplit(raw)
        if parsed.scheme not in {"socks5", "http"} or parsed.path or parsed.query or parsed.fragment:
            raise ValueError
        # Even empty query/fragment delimiters are outside this configuration format.
        if "?" in raw or "#" in raw or parsed.netloc.count("@") > 1:
            raise ValueError
        host, port = parsed.hostname, parsed.port
        if not host or not port or not re.fullmatch(r"[0-9]{1,5}", parsed.netloc.rsplit(":", 1)[-1]):
            raise ValueError
        port_text = parsed.netloc.rsplit(":", 1)[-1]
        host_text = f"[{host}]" if ":" in host else host
        if parsed.netloc.rsplit("@", 1)[-1].lower() != f"{host_text}:{port_text}".lower():
            raise ValueError
        if ":" in host:
            ipaddress.IPv6Address(host)
            if "%" in host:
                raise ValueError
        elif len(host) > 253 or not all(re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
                                      for label in host.removesuffix(".").split(".")):
            raise ValueError
        username, password = parsed.username, parsed.password
        if (username is None) != (password is None):
            raise ValueError
        if username is not None:
            username, password = (unquote(value, encoding="utf-8", errors="strict")
                                  for value in (username, password))
            if any(not value or len(value.encode("utf-8")) > 255
                   or any(ord(c) < 32 or 127 <= ord(c) <= 159 for c in value)
                   for value in (username, password)):
                raise ValueError
        return {"proxy_type": parsed.scheme, "addr": host, "port": port,
                "rdns": True, "username": username, "password": password}
    except (ValueError, TypeError, UnicodeError):
        raise ProviderError("telegram_proxy_configuration_invalid") from None


class TelegramAdapter(Adapter):
    def __init__(self, config: dict[str, Any]):
        super().__init__(config)
        self.client: TelegramClient | None = None

    @asynccontextmanager
    async def _errors(self, *, sending: bool = False):
        try:
            async with asyncio.timeout(30):
                yield
        except ProviderError:
            raise
        except errors.FloodWaitError as exc:
            raise ProviderError("telegram_flood_wait", retry_after=exc.seconds) from None
        except (*REVOKED_KEYS, errors.UserDeactivatedError,
                errors.UserDeactivatedBanError):
            raise ProviderError("telegram_session_expired") from None
        except errors.AuthKeyNotFound:
            # Telegram may temporarily forget a key. Preserve it for recovery;
            # unlike a confirmed revocation this is not proof it is invalid.
            raise ProviderError("telegram_auth_key_unavailable", uncertain=sending) from None
        except errors.PhoneCodeInvalidError:
            raise ProviderError("telegram_code_invalid") from None
        except errors.PhoneCodeExpiredError:
            raise ProviderError("telegram_code_expired") from None
        except errors.PasswordHashInvalidError:
            raise ProviderError("telegram_password_invalid") from None
        except errors.PhoneNumberInvalidError:
            raise ProviderError("telegram_phone_invalid") from None
        except errors.PhoneNumberBannedError:
            raise ProviderError("telegram_phone_banned") from None
        except errors.ApiIdInvalidError:
            raise ProviderError("telegram_configuration_invalid") from None
        except errors.AuthTokenExpiredError:
            raise ProviderError("telegram_qr_expired") from None
        except (errors.AuthTokenInvalidError, errors.AuthTokenAlreadyAcceptedError):
            raise ProviderError("telegram_qr_invalid") from None
        except errors.PeerFloodError:
            raise ProviderError("telegram_account_restricted") from None
        except errors.RandomIdDuplicateError:
            # The server remembers this nonce, but this response has no message id.
            raise ProviderError("telegram_nonce_already_used", uncertain=sending) from None
        except (asyncio.TimeoutError, ConnectionError, OSError,
                ProxyError, ProxyConnectionError, ProxyTimeoutError):
            raise ProviderError("telegram_transport_error", uncertain=sending) from None
        except errors.ServerError:
            raise ProviderError("telegram_server_error", uncertain=sending) from None
        except errors.RPCError:
            # Provider exception strings may contain phone numbers or other PII.
            raise ProviderError("telegram_request_rejected") from None

    async def start(self) -> None:
        """Open the transport; unauthenticated accounts must still allow login."""
        async with self._errors():
            if self.client is None:
                try:
                    api_id = int(self.config["api_id"])
                    api_hash = str(self.config["api_hash"])
                    if api_id <= 0 or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash):
                        raise ValueError
                    session = StringSession(self.config.get("session", ""))
                except (KeyError, ValueError, TypeError):
                    raise ProviderError("telegram_configuration_invalid") from None
                self.client = TelegramClient(
                    session, api_id, api_hash,
                    proxy=_deployment_proxy(), base_logger=_TRANSPORT_LOGGER,
                    receive_updates=False, flood_sleep_threshold=0,
                    request_retries=0, connection_retries=1, timeout=15,
                    raise_last_call_error=True,
                    device_model="Corporate CRM", app_version="1.0",
                )
            if not self.client.is_connected():
                try:
                    await self.client.connect()
                except REVOKED_KEYS:
                    if not self.config.get("session"):
                        raise
                    # A revoked credential can block connect before a queued
                    # login action runs. Negotiate a fresh unauthenticated key;
                    # a person must still supply the login code and optional 2FA.
                    await self.close()
                    self.config.pop("session", None)
                    self.config.pop("tg_code_hash", None)
                    await self.start()

    async def _ready(self) -> None:
        await self.start()
        if not await self.client.is_user_authorized():
            raise ProviderError("telegram_session_expired")

    def _save_session(self) -> None:
        # StringSession contains a login credential. Encryption belongs to core.
        self.config["session"] = self.client.session.save()

    @staticmethod
    def _state(state: Any) -> dict[str, int | float]:
        return {"pts": state.pts, "qts": state.qts,
                "date": state.date.timestamp(), "seq": state.seq}

    async def _connected(self) -> AuthResult:
        self._save_session()
        me = await self.client.get_me()
        if me is None or me.bot:
            raise ProviderError("telegram_user_account_required")
        # A CRM account owns its history/outbox. Never reconnect it as another
        # Telegram user, even after logout; create a separate CRM account instead.
        bound = self.config.get("telegram_bound_user_id", self.config.get("tg_self_id"))
        if bound is not None and bound != me.id:
            raise ProviderError("telegram_account_identity_changed")
        self.config["telegram_bound_user_id"] = me.id
        if self.config.get("tg_self_id") != me.id:
            for key in ("tg_cursor", "tg_entities", "tg_aliases", "tg_sync"):
                self.config.pop(key, None)
        self.config["tg_self_id"] = me.id
        self.config.pop("tg_code_hash", None)
        self.config.pop("tg_qr_pending", None)
        self.config.pop("tg_qr_deadline", None)
        self.config.pop("tg_auth_method", None)
        identity = {}
        phone = getattr(me, "phone", None)
        if isinstance(phone, str) and re.fullmatch(r"\+?[1-9][0-9]{7,14}", phone):
            identity["phone"] = "+" + phone.lstrip("+")
        name = " ".join(str(getattr(me, key, "") or "") for key in ("first_name", "last_name")).strip()
        if name:
            identity["display_name"] = name[:256]
        if getattr(me, "username", None):
            identity["username"] = str(me.username)[:64]
        self.config["tg_identity"] = identity
        if not self.config.get("tg_cursor"):
            self.config["tg_cursor"] = self._state(
                await self.client(functions.updates.GetStateRequest()))
        return AuthResult("connected", {"user_id": str(me.id), "identity": identity, "history_import": "explicit_sync"})

    async def _qr_login(self, *, reset=False) -> AuthResult:
        """Durable MTProto QR login, without an in-memory update waiter.

        ExportLoginToken is the protocol's completion check after a scan. Polling
        it also renews expired tokens; StringSession persists the auth key and DC
        across worker restarts. Like Telethon QRLogin.wait, handle DC migration
        then ImportLoginToken before accepting LoginTokenSuccess.
        """
        if await self.client.is_user_authorized():
            return await self._connected()
        if reset or not self.config.get("tg_qr_deadline"):
            self.config["tg_qr_deadline"] = time.time() + 300
        elif time.time() >= self.config["tg_qr_deadline"]:
            self.config.pop("tg_qr_pending", None)
            return AuthResult("reauth_required", {"error": "telegram_qr_expired", "auth_method": "qr"})
        self.config["tg_qr_pending"] = True
        self.config["tg_auth_method"] = "qr"
        self.config.pop("tg_code_hash", None)
        try:
            response = await self.client(functions.auth.ExportLoginTokenRequest(
                int(self.config["api_id"]), self.config["api_hash"], []))
            if isinstance(response, types.auth.LoginTokenMigrateTo):
                await self.client._switch_dc(response.dc_id)
                response = await self.client(functions.auth.ImportLoginTokenRequest(response.token))
        except errors.SessionPasswordNeededError:
            self._save_session()
            return AuthResult("awaiting_password", {"auth_method": "qr"})
        self._save_session()
        if isinstance(response, types.auth.LoginTokenSuccess):
            confirmed = response.authorization.user
            if not isinstance(confirmed, types.User) or confirmed.bot:
                raise ProviderError("telegram_user_account_required")
            await self.client._on_login(confirmed)
            return await self._connected()
        if not isinstance(response, types.auth.LoginToken):
            raise ProviderError("telegram_qr_invalid")
        import qrcode
        uri = "tg://login?token=" + base64.urlsafe_b64encode(response.token).decode().rstrip("=")
        output = BytesIO()
        qrcode.make(uri).save(output, format="PNG")
        return AuthResult("qr_required", {
            "auth_method": "qr",
            "qr_data_url": "data:image/png;base64," + base64.b64encode(output.getvalue()).decode(),
            "expires_at": response.expires.astimezone(timezone.utc).isoformat(),
        })

    async def auth(self, action: str, payload: dict[str, Any]) -> AuthResult:
        async with self._errors():
            await self.start()
            if action == "disconnect":
                # Revocation must succeed before discarding our only credential.
                if await self.client.is_user_authorized():
                    if not await self.client.log_out():
                        raise ProviderError("telegram_logout_failed")
                if self.config.get("tg_self_id"):
                    self.config.setdefault("telegram_bound_user_id", self.config["tg_self_id"])
                await self.close()
                for key in list(self.config):
                    if key == "session" or key.startswith("tg_"):
                        self.config.pop(key, None)
                return AuthResult("disconnected")
            if action == "login":
                if await self.client.is_user_authorized():
                    return await self._connected()
                phone = str(payload.get("phone", self.config.get("phone", ""))).strip()
                if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
                    raise ProviderError("telegram_phone_invalid")
                self.config["phone"] = phone
                self.config.pop("tg_qr_pending", None)
                self.config.pop("tg_qr_deadline", None)
                self.config["tg_auth_method"] = "phone"
                sent = await self.client.send_code_request(phone)
                self.config["tg_code_hash"] = sent.phone_code_hash
                self._save_session()
                return AuthResult("awaiting_code", {"code_delivery": "telegram_or_sms", "phone": phone, "auth_method": "phone"})
            if action == "qr" or (action == "sync" and self.config.get("tg_qr_pending")):
                return await self._qr_login(reset=action == "qr")
            if action == "code":
                code = str(payload.get("code", "")).strip()
                if not re.fullmatch(r"[0-9]{4,8}", code):
                    raise ProviderError("telegram_code_invalid")
                if not self.config.get("tg_code_hash"):
                    raise ProviderError("telegram_login_required")
                try:
                    await self.client.sign_in(
                        phone=self.config["phone"], code=code,
                        phone_code_hash=self.config["tg_code_hash"],
                    )
                except errors.SessionPasswordNeededError:
                    self._save_session()
                    return AuthResult("awaiting_password", {"auth_method": "phone", "phone": self.config["phone"]})
                return await self._connected()
            if action == "password":
                password = payload.get("password")
                if not isinstance(password, str) or not password:
                    raise ProviderError("telegram_password_required")
                await self.client.sign_in(password=password)
                return await self._connected()
            if action == "sync":
                await self._ready()
                if not self.config.get("tg_self_id"):
                    await self._connected()
                # Bounds cover examined dialogs, including excluded group chats.
                try:
                    dialog_limit = int(payload.get("dialogs", 25))
                    message_limit = int(payload.get("messages", 50))
                except (ValueError, TypeError):
                    raise ProviderError("telegram_sync_limit_invalid") from None
                if not 1 <= dialog_limit <= 100 or not 1 <= message_limit <= 100:
                    raise ProviderError("telegram_sync_limit_invalid")
                staged = deepcopy(self.config)
                if not staged.get("tg_cursor") or payload.get("recover_gap") is True:
                    # Explicit bounded recovery; never silently claim full recovery.
                    staged["tg_cursor"] = self._state(
                        await self.client(functions.updates.GetStateRequest()))
                dialogs = await self.client.get_dialogs(limit=dialog_limit)
                entities = [d.entity for d in dialogs if self._personal(d.entity)]
                self._cache_users(entities, staged)
                staged["tg_sync"] = [str(u.id) for u in entities]
                staged["tg_sync_limit"] = message_limit
                self.config.clear()
                self.config.update(staged)
                return AuthResult("connected", {
                    "identity": self.config.get("tg_identity", {}),
                    "history_import": "queued", "personal_dialogs": len(entities),
                    "max_messages_per_dialog": message_limit,
                    "bounded_recovery": payload.get("recover_gap") is True,
                })
            raise ProviderError("unsupported_action")

    def _personal(self, user: Any) -> bool:
        return (isinstance(user, types.User) and not user.bot and not user.is_self
                and user.id not in (self.config.get("tg_self_id"), 777000))

    def _cache_users(self, users: list, config: dict) -> None:
        cache = config.setdefault("tg_entities", {})
        for user in users:
            if not isinstance(user, types.User):
                continue
            key = str(user.id)
            previous = cache.get(key, {})
            # Minimal entities may carry access_hash=0 that cannot address a user.
            access_hash = previous.get("hash")
            if user.access_hash is not None and not user.min:
                access_hash = user.access_hash
            cache[key] = {
                "hash": access_hash, "bot": bool(user.bot),
                "name": " ".join(filter(None, [user.first_name, user.last_name])) or key,
                "username": user.username,
            }

    async def _resolve_peer(self, peer: str) -> types.InputPeerUser:
        peer = peer.strip()
        # Resolve username every time: usernames can change owners.
        if peer.isdecimal():
            user_id = int(peer)
            cached = self.config.get("tg_entities", {}).get(str(user_id))
            if not cached or cached.get("hash") is None:
                dialogs = await self.client.get_dialogs(limit=200)
                self._cache_users([d.entity for d in dialogs], self.config)
                cached = self.config.get("tg_entities", {}).get(str(user_id))
            if not cached or cached.get("hash") is None:
                raise ProviderError("telegram_peer_unknown_use_username_or_sync")
            if cached.get("bot") or user_id in (self.config.get("tg_self_id"), 777000):
                raise ProviderError("telegram_personal_peer_required")
            return types.InputPeerUser(user_id, cached["hash"])
        if not re.fullmatch(r"@?[A-Za-z][A-Za-z0-9_]{3,31}|\+[1-9][0-9]{7,14}", peer):
            raise ProviderError("telegram_peer_invalid")
        if peer.startswith("+"):
            # Official per-number lookup does not import the address book and
            # respects the target account's phone-discovery privacy settings.
            # Telegram requires at most one contacts.resolvePhone / 3 seconds.
            remaining = float(self.config.get("tg_phone_lookup_after", 0)) - time.time()
            if remaining > 0:
                raise ProviderError("telegram_phone_lookup_rate_limited", retry_after=max(1, math.ceil(remaining)))
            self.config["tg_phone_lookup_after"] = time.time() + 3
            try:
                resolved = await self.client(functions.contacts.ResolvePhoneRequest(phone=peer))
            except (errors.PhoneNotOccupiedError, errors.UserPrivacyRestrictedError):
                raise ProviderError("telegram_phone_not_found_or_private") from None
            resolved_peer = getattr(resolved, "peer", None)
            if not isinstance(resolved_peer, types.PeerUser):
                raise ProviderError("telegram_phone_not_found_or_private")
            user = next((candidate for candidate in getattr(resolved, "users", [])
                         if isinstance(candidate, types.User) and candidate.id == resolved_peer.user_id), None)
            if user is None or user.access_hash is None or user.min:
                raise ProviderError("telegram_phone_not_found_or_private")
        else:
            try:
                user = await self.client.get_entity(peer)
            except (ValueError, TypeError):
                raise ProviderError("telegram_peer_not_found") from None
        if not self._personal(user) or user.access_hash is None:
            raise ProviderError("telegram_personal_peer_required")
        # A conversation must not silently follow a username/phone to a new owner.
        if any(peer in aliases and old_id != str(user.id)
               for old_id, aliases in self.config.get("tg_aliases", {}).items()):
            raise ProviderError("telegram_peer_identity_changed")
        self._cache_users([user], self.config)
        aliases = self.config.setdefault("tg_aliases", {}).setdefault(str(user.id), [])
        if peer not in aliases:
            aliases.append(peer)
        return types.InputPeerUser(user.id, user.access_hash)

    @staticmethod
    def _random_id(client_id: str) -> int:
        try:
            digest = hashlib.sha256(UUID(client_id).bytes).digest()
        except (ValueError, TypeError, AttributeError):
            raise ProviderError("invalid_client_id") from None
        return int.from_bytes(digest[:8], "big", signed=True) or 1

    async def send(self, peer: str, text: str, client_id: str) -> str:
        if not isinstance(text, str) or not text or len(text) > 4096:
            raise ProviderError("telegram_text_limit")
        nonce = self._random_id(client_id)
        async with self._errors():
            await self._ready()
            entity = await self._resolve_peer(peer)
        # Resolving a peer cannot deliver a message. Only this call is ambiguous.
        async with self._errors(sending=True):
            result = await self.client(functions.messages.SendMessageRequest(
                peer=entity, message=text, random_id=nonce, no_webpage=True))
            if isinstance(result, types.UpdateShortSentMessage):
                return str(result.id)
            for update in getattr(result, "updates", []):
                if isinstance(update, types.UpdateMessageID) and update.random_id == nonce:
                    return str(update.id)
                if isinstance(update, types.UpdateNewMessage):
                    msg = update.message
                    if (getattr(msg, "out", False) and isinstance(msg.peer_id, types.PeerUser)
                            and msg.peer_id.user_id == entity.user_id):
                        return str(msg.id)
            raise ProviderError("telegram_send_result_unknown", uncertain=True)

    def _event(self, msg: Any, config: dict) -> Event | None:
        if not isinstance(msg, types.Message) or not isinstance(msg.peer_id, types.PeerUser):
            return None
        user_id = msg.peer_id.user_id
        if user_id == config.get("tg_self_id") and not msg.out:
            if not isinstance(msg.from_id, types.PeerUser):
                return None
            user_id = msg.from_id.user_id
        if user_id in (config.get("tg_self_id"), 777000):
            return None
        peer = str(user_id)
        cached = config.get("tg_entities", {}).get(peer, {})
        if cached.get("bot"):
            return None
        has_media = msg.media is not None and not isinstance(msg.media, types.MessageMediaEmpty)
        content = msg.message or ("[Вложение: откройте Telegram]" if has_media else "")
        if not content:
            return None
        return Event(
            provider_id=str(msg.id), peer=peer, text=content,
            direction="outbound" if msg.out else "inbound",
            timestamp=msg.date.timestamp() if msg.date else None,
            peer_name=cached.get("name", peer), status="sent" if msg.out else "received",
            metadata={"canonical_peer": peer, "aliases": config.get("tg_aliases", {}).get(peer, []),
                      "media_not_downloaded": has_media},
        )

    async def poll(self) -> list[Event]:
        async with self._errors():
            await self._ready()
            if not self.config.get("tg_self_id"):
                await self._connected()
            staged = deepcopy(self.config)
            if not staged.get("tg_cursor"):
                staged["tg_cursor"] = self._state(
                    await self.client(functions.updates.GetStateRequest()))
            cursor = staged["tg_cursor"]
            difference = await self.client(functions.updates.GetDifferenceRequest(
                pts=cursor["pts"], qts=cursor["qts"],
                date=datetime.fromtimestamp(cursor["date"], timezone.utc),
                pts_limit=100, qts_limit=100,
            ))
            events = []
            if isinstance(difference, types.updates.DifferenceTooLong):
                # Retain old cursor: an explicit bounded recovery is required.
                raise ProviderError("telegram_history_gap_sync_required")
            if isinstance(difference, types.updates.DifferenceEmpty):
                cursor["date"] = difference.date.timestamp()
                cursor["seq"] = difference.seq
            elif isinstance(difference, (types.updates.Difference, types.updates.DifferenceSlice)):
                self._cache_users(difference.users, staged)
                events.extend(filter(None, (self._event(m, staged) for m in difference.new_messages)))
                state = (difference.intermediate_state if isinstance(difference, types.updates.DifferenceSlice)
                         else difference.state)
                staged["tg_cursor"] = self._state(state)
            else:
                raise ProviderError("telegram_difference_unknown")
            if staged.get("tg_sync"):
                peer = staged["tg_sync"][0]
                cached = staged.get("tg_entities", {}).get(peer, {})
                if cached.get("hash") is None:
                    raise ProviderError("telegram_peer_unknown_use_username_or_sync")
                history = await self.client.get_messages(
                    types.InputPeerUser(int(peer), cached["hash"]),
                    limit=staged.get("tg_sync_limit", 50),
                )
                events.extend(filter(None, (self._event(m, staged) for m in history)))
                staged["tg_sync"].pop(0)
            # The core must commit these mutations and events in ONE transaction,
            # and reconstruct the adapter from durable config after any rollback.
            self.config.clear()
            self.config.update(staged)
            unique = {event.provider_id: event for event in events}
            return sorted(unique.values(), key=lambda event: (event.timestamp or 0, int(event.provider_id)))

    async def close(self) -> None:
        if self.client is not None:
            await self.client.disconnect()
            self.client = None
