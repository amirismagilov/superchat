"""Opt-in WhatsApp linked-device adapter for a deployment-owned WAHA NOWEB.

Contract checked against WAHA 2026.9.2, commit 90ba445a (2026-10-14).
The WAHA process owns WhatsApp credentials; protect its persistent volume too.
"""
import os
import base64
import math
from io import BytesIO
import re
import time
import uuid
from urllib.parse import quote, urlsplit

import httpx

from .base import Adapter, AuthResult, Event, ProviderError, history_days


def _experimental_enabled() -> bool:
    return os.environ.get("ENABLE_EXPERIMENTAL", "").lower() == "true"


def _qr_data_url(value: str) -> str:
    # Render locally: the QR contains a temporary login credential.
    import qrcode
    output = BytesIO()
    qrcode.make(value).save(output, format="PNG")
    return "data:image/png;base64," + base64.b64encode(output.getvalue()).decode("ascii")


def _account_identity(me: object) -> dict | None:
    """Only expose the authenticated account fields required by the connect UI."""
    if not isinstance(me, dict):
        return None
    identifiers = [me.get("id"), me.get("jid")]
    valid = [value for value in identifiers if isinstance(value, str)
             and re.fullmatch(r"[0-9]+(?::[0-9]+)?@(?:c\.us|s\.whatsapp\.net|lid)", value)]
    if not valid:
        return None
    identity = {"id": valid[0]}
    for value in valid:
        if not value.endswith("@lid"):
            number = value.split("@", 1)[0].split(":", 1)[0]
            if 7 <= len(number) <= 15:
                identity["phone"] = "+" + number
                break
    name = me.get("pushName")
    if isinstance(name, str) and name.strip():
        identity["display_name"] = name.strip()[:160]
    return identity


def _to_cus_format(jid: str) -> str:
    """WAHA 2026.9.2 src/core/utils/jids.ts:149, including device suffixes."""
    if not jid or jid == "me" or jid.endswith(("@g.us", "@broadcast", "@newsletter")):
        return jid
    if jid.endswith("@lid"):
        return re.sub(r":\d+(?=@)", "", jid, count=1)
    number = jid.split("@", 1)[0].split(":", 1)[0]
    return number + "@c.us"


def _sent_message_identity(result: object) -> tuple[str, str, dict]:
    """Return the same ID that NOWEB history's toWAMessage/buildMessageId uses.

    NOWEB 2026.9.2 sendText returns raw Baileys WebMessageInfo. Other responses
    can already be normalized WAMessage; never treat raw key.id as its full ID.
    """
    if not isinstance(result, dict):
        raise ProviderError("waha_missing_message_id", uncertain=True)
    key = result.get("key") if isinstance(result.get("key"), dict) else {}
    recipient = result.get("to") or key.get("remoteJid") or ""
    recipient = _to_cus_format(recipient) if isinstance(recipient, str) else ""
    if isinstance(result.get("id"), str) and result["id"]:
        return result["id"], recipient, key
    if not isinstance(key.get("id"), str) or not key["id"] or not isinstance(key.get("remoteJid"), str) or not key["remoteJid"]:
        raise ProviderError("waha_missing_message_id", uncertain=True)
    if key.get("fromMe") is not None and not isinstance(key["fromMe"], bool):
        raise ProviderError("waha_invalid_message_key", uncertain=True)
    # Exact algorithm: src/core/engines/noweb/utils.ts:10 buildMessageId.
    parts = ["true" if key.get("fromMe") else "false", _to_cus_format(key["remoteJid"]), key["id"]]
    if key.get("participant"):
        if not isinstance(key["participant"], str):
            raise ProviderError("waha_invalid_message_key", uncertain=True)
        parts.append(_to_cus_format(key["participant"]))
    return "_".join(parts), recipient, key


class WhatsAppWebAdapter(Adapter):
    PAGE_SIZE = 100
    CHATS_PER_POLL = 10
    HISTORY_CHATS_PER_POLL = 2
    HISTORY_OVERLAP_SECONDS = 300
    CONNECT_WAIT_SECONDS = 120
    PROVIDER_STATES = frozenset({
        "STOPPED", "STARTING", "SCAN_QR_CODE", "PASSKEY_REQUIRED",
        "PASSKEY_CONFIRMATION_REQUIRED", "WORKING", "FAILED",
    })

    async def start(self) -> None:
        if not _experimental_enabled():
            raise ProviderError("experimental_adapter_disabled")
        # Logout clears device state while the worker may retain this adapter.
        # Allocate the replacement session even when its HTTP client is alive.
        if not self.config.get("_waha_session"):
            self.config["_waha_session"] = "omni-" + uuid.uuid4().hex
        if not re.fullmatch(r"omni-[0-9a-f]{32}", self.config["_waha_session"]):
            raise ProviderError("invalid_waha_session")
        if getattr(self, "http", None) is not None:
            return
        # Account config must never choose an arbitrary network destination.
        endpoint = os.environ.get("WAHA_URL", "").rstrip("/")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ProviderError("waha_deployment_url_required")
        api_key = os.environ.get("WAHA_API_KEY", "")
        if not api_key:
            raise ProviderError("waha_deployment_api_key_required")
        self.http = httpx.AsyncClient(base_url=endpoint, headers={"X-Api-Key": api_key}, timeout=30, follow_redirects=False)

    @property
    def session(self) -> str:
        return self.config["_waha_session"]

    def _wait_deadline(self) -> float | None:
        value = self.config.get("_waha_wait_deadline")
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None

    def _wait_expired(self) -> bool:
        deadline = self._wait_deadline()
        return deadline is not None and time.time() >= deadline

    def _begin_wait(self) -> None:
        # Persist wall-clock time in encrypted config: sync and worker restarts
        # must not extend a provider initialization that never produces a QR.
        self.config["_waha_wait_deadline"] = time.time() + self.CONNECT_WAIT_SECONDS

    def _pending(self, provider_state: str, *, reset: bool = False) -> AuthResult:
        if reset or self._wait_deadline() is None:
            self._begin_wait()
        public = {"experimental": True, "provider_state": provider_state}
        if self._wait_expired():
            return AuthResult("action_required", {**public, "error": "waha_connect_timeout"})
        return AuthResult("connecting", public)

    def _clear_wait(self) -> None:
        self.config.pop("_waha_wait_deadline", None)

    def _remember_identity(self, identity: dict) -> None:
        current = {"id": _to_cus_format(identity["id"])}
        if identity.get("phone"):
            current["phone"] = identity["phone"]
        previous = self.config.get("wa_self")
        if previous:
            if previous.get("phone") and current.get("phone"):
                same = previous["phone"] == current["phone"]
            else:
                same = previous.get("id") == current["id"]
            if not same:
                self.config["wa_identity_mismatch"] = True
                raise ProviderError("whatsapp_account_identity_changed")
            current = {**previous, **current}
        # This binding survives device logout; history belongs to this account.
        self.config["wa_self"] = current
        self.config.pop("wa_identity_mismatch", None)

    async def _identity_for_state(self, state: dict) -> dict | None:
        identity = _account_identity(state.get("me"))
        if identity is None:
            me = await self._request("GET", f"/api/sessions/{self.session}/me", pending_statuses=(404, 422))
            identity = _account_identity(me)
        return identity

    async def _request(self, method, path, *, sending=False, allow_404=False, pending_statuses=(), **kwargs):
        try:
            response = await self.http.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            # Even a connection error may follow a partially delivered request.
            raise ProviderError("waha_transport_error", uncertain=sending) from exc
        if response.status_code == 404 and allow_404:
            return None
        if response.status_code in pending_statuses:
            return None
        if response.status_code == 429:
            delay = response.headers.get("retry-after", "30")
            raise ProviderError("waha_rate_limited", retry_after=int(delay) if delay.isdigit() else 30)
        if response.status_code >= 500:
            raise ProviderError("waha_server_error", uncertain=sending)
        if response.status_code >= 300:
            raise ProviderError("waha_request_rejected")
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("waha_invalid_response", uncertain=sending) from exc

    async def auth(self, action: str, payload: dict) -> AuthResult:
        await self.start()
        if action == "disconnect":
            # Older installed versions did not store a self identity. Capture it
            # before logout while the old device can still prove its account.
            if not self.config.get("wa_self"):
                state = await self._request("GET", f"/api/sessions/{self.session}", allow_404=True)
                if isinstance(state, dict) and state.get("status") == "WORKING":
                    identity = await self._identity_for_state(state)
                    if identity is not None:
                        self._remember_identity(identity)
            # Retain config on failure: an unconfirmed logout is not a revoke.
            await self._request("POST", f"/api/sessions/{self.session}/logout")
            await self._request("DELETE", f"/api/sessions/{self.session}", allow_404=True)
            for key in list(self.config):
                if key.startswith("_waha_"):
                    self.config.pop(key)
            self.config.pop("wa_identity_mismatch", None)
            self._qr_value = self._qr_image = None
            self._live_seen = {}
            return AuthResult("disconnected")
        if action not in {"login", "sync"}:
            raise ProviderError("unsupported_action")
        state = await self._request("GET", f"/api/sessions/{self.session}", allow_404=True)
        if state is None:
            if action != "login":
                return AuthResult("disconnected")
            self._begin_wait()
            state = await self._request("POST", "/api/sessions", json={
                "name": self.session, "start": True,
                "config": {"noweb": {"store": {"enabled": True, "fullSync": True}}},
            })
        if not isinstance(state, dict):
            raise ProviderError("waha_invalid_response")
        status = state.get("status")
        if not isinstance(status, str) or status not in self.PROVIDER_STATES:
            raise ProviderError("waha_invalid_response")
        if action == "login" and (status in {"STOPPED", "FAILED"}
                                  or (status == "STARTING" and self._wait_expired())):
            # WAHA considers a FAILED session object already running: /start is
            # a no-op there. /restart stops and starts the same device without
            # logout, deleting credentials, or allocating another session.
            operation = "start" if status == "STOPPED" else "restart"
            self._begin_wait()
            await self._request("POST", f"/api/sessions/{self.session}/{operation}")
            return self._pending("STARTING")
        if status == "WORKING":
            identity = await self._identity_for_state(state)
            if identity is None:
                return self._pending(status, reset=action == "login" and self._wait_expired())
            self._remember_identity(identity)
            self._clear_wait()
            self._qr_value = self._qr_image = None
            return AuthResult("connected", {"experimental": True, "identity": identity})
        if status == "SCAN_QR_CODE":
            qr = await self._request("GET", f"/api/{self.session}/auth/qr", params={"format": "raw"}, headers={"Accept": "application/json"}, pending_statuses=(404, 422))
            # Scanning can finish between the status and QR requests. Poll again
            # instead of displaying an error or claiming an authenticated account.
            if qr is None:
                return self._pending(status, reset=action == "login" and self._wait_expired())
            value = qr.get("value") if isinstance(qr, dict) else None
            # NOWEB clears its QR before the delayed post-pairing restart changes
            # SCAN_QR_CODE to STARTING. Its raw endpoint can return an empty value
            # during that interval; it is progress to check again, not a QR to
            # render or proof that the account has authenticated.
            if value == "":
                return self._pending(status, reset=action == "login" and self._wait_expired())
            if not isinstance(value, str) or len(value) > 4096:
                raise ProviderError("waha_invalid_qr_response")
            if value != getattr(self, "_qr_value", None):
                from qrcode.exceptions import DataOverflowError
                try:
                    self._qr_image = _qr_data_url(value)
                except (ValueError, OverflowError, DataOverflowError) as exc:
                    raise ProviderError("waha_invalid_qr_response") from exc
                self._qr_value = value
            self._clear_wait()
            return AuthResult("qr_required", {"qr_text": value, "qr_data_url": self._qr_image, "experimental": True})
        if status == "STARTING":
            return self._pending(status)
        self._clear_wait()
        if status in {"FAILED", "STOPPED"}:
            return AuthResult("action_required", {
                "provider_state": status, "experimental": True,
                "error": "waha_session_failed" if status == "FAILED" else "waha_session_stopped",
            })
        # Passkeys need the WhatsApp origin's WebAuthn flow; never claim success.
        return AuthResult("action_required", {
            "provider_state": status, "experimental": True,
        })

    async def send(self, peer: str, text: str, client_id: str) -> str:
        if self.config.get("wa_identity_mismatch"):
            raise ProviderError("whatsapp_account_identity_changed")
        await self.start()
        peer = peer.lstrip("+")
        if peer.isdigit():
            peer += "@c.us"
        if not re.fullmatch(r"[0-9-]+@(?:c\.us|g\.us|lid)", peer):
            raise ProviderError("invalid_whatsapp_peer")
        result = await self._request("POST", "/api/sendText", sending=True, json={
            "session": self.session, "chatId": peer, "text": text,
            "id": uuid.UUID(client_id).hex.upper(), "linkPreview": False,
        })
        message_id, recipient, key = _sent_message_identity(result)
        # Raw NOWEB responses identify the recipient in key.remoteJid;
        # normalized responses may have `to`. remoteJidAlt can carry the
        # corresponding PN/LID, as in WAHA's jidsFromKey helper.
        if peer.endswith("@c.us") and re.fullmatch(r"[0-9]+@lid", recipient):
            self.config.setdefault("_waha_lid_pn", {})[recipient] = peer
        self._remember_peer_alias(recipient, key)
        return message_id

    def _remember_peer_alias(self, recipient: str, key: dict) -> None:
        # Only a provider-supplied PN/LID pair can identify the same person.
        alternate = key.get("remoteJidAlt")
        alternate = _to_cus_format(alternate) if isinstance(alternate, str) else ""
        if re.fullmatch(r"[0-9]+@c\.us", recipient) and re.fullmatch(r"[0-9]+@lid", alternate):
            self.config.setdefault("_waha_lid_pn", {})[alternate] = recipient
        elif re.fullmatch(r"[0-9]+@lid", recipient) and re.fullmatch(r"[0-9]+@c\.us", alternate):
            self.config.setdefault("_waha_lid_pn", {})[recipient] = alternate

    async def _peer_metadata(self, peer: str, *, resolve_lid: bool = True) -> dict:
        mappings = self.config.setdefault("_waha_lid_pn", {})
        if resolve_lid and peer.endswith("@lid") and peer not in mappings:
            result = await self._request(
                "GET", f"/api/{self.session}/lids/{quote(peer, safe='')}", allow_404=True, timeout=2,
            )
            if result is not None:
                if not isinstance(result, dict):
                    raise ProviderError("waha_invalid_lid_response")
                pn = _to_cus_format(str(result.get("pn") or ""))
                if re.fullmatch(r"[0-9]+@c\.us", pn):
                    mappings[peer] = pn
        canonical = mappings.get(peer, peer)
        aliases = {peer, canonical}
        if canonical.endswith("@c.us"):
            phone = canonical.split("@", 1)[0]
            aliases.update({phone, "+" + phone, phone + "@s.whatsapp.net"})
            aliases.update(lid for lid, pn in mappings.items() if pn == canonical)
        return {"canonical_peer": canonical, "aliases": sorted(aliases)}

    @staticmethod
    def _message_peer(message: dict) -> tuple[str, dict]:
        raw = message.get("_data")
        key = raw.get("key") if isinstance(raw, dict) else None
        key = key if isinstance(key, dict) else {}
        # NOWEB getFromToParticipant uses the remote chat as `from` in BOTH
        # directions. In particular, an outgoing DM need not have `to`.
        value = key.get("remoteJid") or message.get("from")
        peer = _to_cus_format(value) if isinstance(value, str) else ""
        if not re.fullmatch(r"[0-9-]+@(?:c\.us|g\.us|lid)", peer):
            peer = ""
        return peer, key

    @staticmethod
    def _event(message: dict, peer: str, identity: dict, name=None) -> Event:
        if not isinstance(message, dict) or not message.get("id"):
            raise ProviderError("waha_invalid_message")
        outbound = bool(message.get("fromMe"))
        ack = message.get("ack") or 0
        status = ("read" if ack >= 3 else "delivered" if ack >= 2 else "sent") if outbound else "received"
        return Event(
            provider_id=str(message["id"]), peer=peer,
            text=message.get("body") or ("[Вложение]" if message.get("hasMedia") else ""),
            direction="outbound" if outbound else "inbound", timestamp=message.get("timestamp"),
            peer_name=name, status=status,
            metadata={"has_media": bool(message.get("hasMedia")), **identity},
        )

    def _history(self) -> dict:
        if "_waha_history" not in self.config:
            days = history_days(self.config, 30)
            self.config["_waha_history"] = {"days": days, "boundary": time.time(), "discovery_done": False,
                                            "completed": [], "offset": 0}
        return self.config["_waha_history"]

    async def request_history(self, payload: dict) -> AuthResult:
        days = history_days(payload)
        self.config["_waha_history"] = {"days": days, "boundary": time.time(), "discovery_done": False,
                                        "completed": [], "offset": 0}
        # The session, aliases, independent live tail and provider identity stay
        # intact. Only backfill/discovery traversal is restarted.
        self.config.pop("_waha_cursors", None)
        self.config["_waha_chat_offset"] = 0
        self._history_status()
        return AuthResult("connected")

    def _history_status(self) -> None:
        history = self._history()
        count = len(self.config.get("_waha_catalog", {}))
        completed = len(history["completed"])
        done = history["discovery_done"] and completed >= count
        self.config["history_status"] = {
            "state": "partial" if history.get("error") else "complete" if done else "importing",
            "chats_discovered": count, "chats_completed": completed, "history_days": history["days"],
            "reason": "history_fetch_failed" if history.get("error") else "waha_device_history_only",
        }

    async def poll(self) -> list[Event]:
        if self.config.get("wa_identity_mismatch"):
            raise ProviderError("whatsapp_account_identity_changed")
        await self.start()
        history = self._history()
        catalog = self.config.setdefault("_waha_catalog", {})
        history.pop("error", None)
        # Keep the proven all-JID live tail independent of history traversal.
        live = await self._request("GET", f"/api/{self.session}/chats/all/messages", params={
            "limit": self.PAGE_SIZE, "offset": 0,
            "sortBy": "timestamp", "sortOrder": "desc", "downloadMedia": "false",
        }, timeout=6)
        if not isinstance(live, list):
            raise ProviderError("waha_invalid_history_response")
        for message in live:
            if not isinstance(message, dict):
                raise ProviderError("waha_invalid_message")
            peer, key = self._message_peer(message)
            if peer:
                self._remember_peer_alias(peer, key)
        # Resolve active LIDs before discovery/backfill emits any events. A
        # cached discovery identity must not prevent a later provider PN lookup.
        candidates = set(catalog) | set(self.config.get("_waha_cursors", {}))
        candidates.update(self._message_peer(message)[0] for message in live)
        candidates = sorted(peer for peer in candidates if peer.endswith("@lid")
                            and peer not in self.config.get("_waha_lid_pn", {}))
        if candidates:
            lid_offset = int(self.config.get("_waha_lid_offset", 0)) % len(candidates)
            selected_lids = (candidates[lid_offset:] + candidates[:lid_offset])[:2]
            for peer in selected_lids:
                try:
                    await self._peer_metadata(peer)
                except ProviderError:
                    pass
            self.config["_waha_lid_offset"] = (lid_offset + len(selected_lids)) % len(candidates)
        offset = int(self.config.get("_waha_chat_offset", 0))
        try:
            chats = await self._request("GET", f"/api/{self.session}/chats", params={
                "limit": self.CHATS_PER_POLL, "offset": offset,
                "sortBy": "id", "sortOrder": "asc",
            }, timeout=6)
            if not isinstance(chats, list):
                raise ProviderError("waha_invalid_chats_response")
        except ProviderError:
            chats = None
            history["error"] = True
        events, identities = {}, {}
        for chat in chats or []:
            peer = _to_cus_format(str(chat.get("id", "")))
            if not re.fullmatch(r"[0-9-]+@(?:c\.us|g\.us|lid)", peer):
                continue
            name = chat.get("name") or chat.get("subject") or catalog.get(peer, {}).get("name")
            identity = await self._peer_metadata(peer, resolve_lid=False)
            identities[peer] = identity
            # Discovery is durable even for empty/provider-truncated chats.
            if (peer not in catalog or name != catalog[peer].get("name")
                    or catalog[peer].get("canonical_peer") != identity["canonical_peer"]):
                events[("conversation", peer)] = Event("", peer, "", peer_name=name,
                    metadata={**identity, "conversation_only": True})
            catalog[peer] = {"name": name, "canonical_peer": identity["canonical_peer"]}
        if chats is not None and len(chats) < self.CHATS_PER_POLL:
            history["discovery_done"] = True
            self.config["_waha_chat_offset"] = 0
        elif chats is not None:
            self.config["_waha_chat_offset"] = offset + len(chats)
        # Catalog enumeration and backfill have separate persisted cursors. A
        # long first chat cannot hide later/empty chats or starve recent events.
        peers = sorted(catalog)
        round_offset = history["offset"] % len(peers) if peers else 0
        selected = peers[round_offset:round_offset + self.HISTORY_CHATS_PER_POLL]
        cursors = self.config.setdefault("_waha_cursors", {})
        for peer in selected:
            identity = identities.get(peer) or await self._peer_metadata(peer, resolve_lid=False)
            identities[peer] = identity
            cursor = cursors.setdefault(peer, {})
            if "ceiling" not in cursor:
                now = int(time.time()) - 1
                floor = max(0, int(history["boundary"]) - history["days"] * 86400)
                if "watermark" in cursor:
                    floor = max(floor, cursor["watermark"] - self.HISTORY_OVERLAP_SECONDS)
                cursor.update(floor=floor, ceiling=now, offset=0)
            try:
                messages = await self._request("GET", f"/api/{self.session}/chats/{quote(peer, safe='')}/messages", params={
                    "limit": self.PAGE_SIZE, "offset": cursor["offset"],
                    "sortBy": "timestamp", "sortOrder": "asc", "downloadMedia": "false",
                    "filter.timestamp.gte": cursor["floor"], "filter.timestamp.lte": cursor["ceiling"],
                }, timeout=8)
                if not isinstance(messages, list):
                    raise ProviderError("waha_invalid_history_response")
                for message in messages:
                    event = self._event(message, peer, identity, catalog[peer].get("name"))
                    event.metadata["historical"] = (event.timestamp or 0) <= history["boundary"]
                    events[(identity["canonical_peer"], event.provider_id)] = event
                if len(messages) < self.PAGE_SIZE:
                    cursors[peer] = {"watermark": cursor["ceiling"]}
                    if peer not in history["completed"]:
                        history["completed"].append(peer)
                else:
                    cursor["offset"] += len(messages)
            except ProviderError:
                # Return and commit the independent live events, retaining this
                # history cursor for retry; one bad history must not block send.
                history["error"] = True
        history["offset"] = (round_offset + len(selected)) % len(peers) if peers else 0
        previous_live = getattr(self, "_live_seen", {})
        current_live = {}
        for message in reversed(live):
            peer, _key = self._message_peer(message)
            if not peer:
                continue
            if peer not in identities:
                identities[peer] = await self._peer_metadata(peer, resolve_lid=False)
            identity = identities[peer]
            key = (identity["canonical_peer"], str(message.get("id", "")))
            name = catalog.get(peer, {}).get("name") or catalog.get(identity["canonical_peer"], {}).get("name")
            raw = message.get("_data") or {}
            if not name and not peer.endswith("@g.us") and not message.get("fromMe") and isinstance(raw, dict):
                name = raw.get("pushName")
            if (peer not in catalog or (name and name != catalog[peer].get("name"))
                    or catalog[peer].get("canonical_peer") != identity["canonical_peer"]):
                catalog[peer] = {"name": name, "canonical_peer": identity["canonical_peer"]}
                events[("conversation", peer)] = Event("", peer, "", peer_name=name,
                    metadata={**identity, "conversation_only": True})
            event = self._event(message, peer, identity, name)
            event.metadata["historical"] = (event.timestamp or 0) <= history["boundary"]
            fingerprint = (event.peer, event.direction, event.timestamp, event.text,
                           event.status, event.metadata["has_media"], tuple(identity["aliases"]))
            current_live[key] = fingerprint
            if key in events or previous_live.get(key) != fingerprint:
                events[key] = event
        self._live_seen = current_live
        self._history_status()
        return list(events.values())

    async def close(self) -> None:
        self._live_seen = {}
        if getattr(self, "http", None) is not None:
            await self.http.aclose()
            self.http = None
