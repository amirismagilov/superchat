"""Documented HTTP providers. Credentials stay in encrypted account config.

No account-configurable destination URLs or automatic redirect following.
Webhook functions normalize only; the API must verify raw bytes and bind every
event to its account before ingesting it. See docs/official-connectors.md.
"""
import hashlib
import hmac
import os
import re
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .base import Adapter, AuthResult, Event, ProviderError


def verify_whatsapp_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not isinstance(signature, str):
        return False
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def verify_max_secret(secret: str, supplied: str) -> bool:
    return bool(secret and supplied and hmac.compare_digest(secret.encode(), supplied.encode()))


def verify_sms_signature(secret: str, body: bytes, timestamp: str, signature: str,
                         *, now: float | None = None, max_age: int = 300) -> bool:
    """SMSGate signs raw body followed by Unix-seconds timestamp, no separator."""
    if not secret or not re.fullmatch(r"[0-9]{1,12}", timestamp or ""):
        return False
    if abs((time.time() if now is None else now) - int(timestamp)) > max_age:
        return False
    expected = hmac.new(secret.encode(), body + timestamp.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), (signature or "").encode())


def verify_webhook(channel: str, body: bytes, headers: dict, config: dict) -> bool:
    """Use raw request bytes; never JSON reserialization. Headers are case-insensitive."""
    normalized = {str(key).lower(): value for key, value in headers.items()}
    if channel == "whatsapp_cloud":
        return verify_whatsapp_signature(config.get("app_secret", ""), body,
                                         normalized.get("x-hub-signature-256", ""))
    if channel == "max_bot":
        return verify_max_secret(config.get("webhook_secret", ""), normalized.get("x-max-bot-api-secret", ""))
    if channel == "sms_gateway":
        return verify_sms_signature(config.get("signing_key", ""), body,
                                    normalized.get("x-timestamp", ""), normalized.get("x-signature", ""))
    return False


def _required(config: dict, key: str) -> str:
    value = config.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ProviderError("missing_" + key)
    return value


def _number(peer: str, *, require_plus: bool = False) -> str:
    pattern = r"\+[1-9][0-9]{6,14}" if require_plus else r"\+?[1-9][0-9]{6,14}"
    if not isinstance(peer, str) or not re.fullmatch(pattern, peer):
        raise ProviderError("invalid_phone_number")
    return peer


def validate_whatsapp_template(template: dict) -> dict:
    """Build the supported positional text-body subset of Meta templates.

    These are conservative product limits. Approval, enabled status, locale and
    placeholder count remain provider checks; this does not register templates.
    Reject unknown keys instead of accepting arbitrary Graph components.
    """
    if not isinstance(template, dict) or set(template) - {"name", "language", "parameters"}:
        raise ProviderError("invalid_whatsapp_template")
    name, language = template.get("name"), template.get("language")
    parameters = template.get("parameters", [])
    if (not isinstance(name, str) or not re.fullmatch(r"[a-z0-9_]{1,512}", name)
            or not isinstance(language, str) or not re.fullmatch(r"[a-z]{2,3}(?:_[A-Z]{2})?", language)
            or not isinstance(parameters, list) or len(parameters) > 20):
        raise ProviderError("invalid_whatsapp_template")
    if any(not isinstance(value, str) or not 1 <= len(value) <= 1024
           or re.search(r"[\x00-\x1f\x7f]", value) for value in parameters):
        raise ProviderError("invalid_whatsapp_template_parameters")
    if sum(map(len, parameters)) > 4096:
        raise ProviderError("invalid_whatsapp_template_parameters")
    result = {"name": name, "language": {"code": language}}
    if parameters:
        result["components"] = [{"type": "body", "parameters": [
            {"type": "text", "text": value} for value in parameters]}]
    return result


def _unix(value: Any, *, milliseconds: bool = False) -> float | None:
    if value is None:
        return None
    try:
        result = float(value)
        if milliseconds:
            result /= 1000
        if not 0 <= result <= 32503680000:
            return None
        return result
    except (ValueError, TypeError):
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            return parsed.timestamp() if parsed.tzinfo else None
        except (ValueError, TypeError, OverflowError):
            return None


class _HTTPAdapter(Adapter):
    def __init__(self, config: dict[str, Any], *, client: httpx.AsyncClient | None = None):
        super().__init__(config)
        self.client = client
        self._owns_client = client is None

    async def start(self) -> None:
        if self.client is None:
            # httpx honors SSL_CERT_FILE/SSL_CERT_DIR; never disable TLS checks.
            self.client = httpx.AsyncClient(timeout=20, follow_redirects=False)

    async def close(self) -> None:
        if self.client and self._owns_client:
            await self.client.aclose()
            self.client = None

    async def _request(self, method: str, url: str, **kwargs):
        await self.start()
        mutation = method != "GET"
        try:
            response = await self.client.request(method, url, follow_redirects=False, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ProviderError("provider_unreachable", retry_after=10) from exc
        except httpx.HTTPError as exc:
            raise ProviderError("provider_transport_error", uncertain=mutation) from exc
        if response.status_code == 429:
            retry = response.headers.get("Retry-After", "30")
            raise ProviderError("provider_rate_limited", retry_after=int(retry) if retry.isdigit() else 30)
        if response.status_code in (401, 403):
            raise ProviderError("provider_unauthorized")
        if response.status_code >= 500:
            raise ProviderError("provider_server_error", uncertain=mutation)
        if not response.is_success:
            # Provider payloads may echo tokens/content; do not include them in errors.
            raise ProviderError("provider_http_" + str(response.status_code))
        try:
            return response.json()
        except ValueError as exc:
            raise ProviderError("invalid_provider_response", uncertain=mutation) from exc

    async def auth(self, action, payload):
        if action == "disconnect":
            # Revoking a shared Meta system-user token, MAX bot token, or SMSGate
            # user cannot safely be inferred from disconnecting one CRM account.
            return AuthResult("disconnected", {"remote_revocation_required": True,
                              "notice": "Подключение CRM остановлено. Для отзыва токена откройте кабинет провайдера."})
        raise ProviderError("unsupported_action")


class WhatsAppCloudAdapter(_HTTPAdapter):
    def _url(self, config, suffix=""):
        version = os.getenv("WHATSAPP_GRAPH_VERSION", "v25.0")
        number_id = _required(config, "phone_number_id")
        if not re.fullmatch(r"v[0-9]+\.0", version) or not number_id.isdigit():
            raise ProviderError("invalid_whatsapp_configuration")
        return f"https://graph.facebook.com/{version}/{number_id}{suffix}"

    @staticmethod
    def _headers(config):
        return {"Authorization": "Bearer " + _required(config, "access_token")}

    async def auth(self, action, payload):
        if action not in {"login", "sync"}:
            return await super().auth(action, payload)
        candidate = {**self.config, **{k: v for k, v in payload.items()
                                    if k in {"access_token", "phone_number_id", "app_secret", "verify_token"}}}
        data = await self._request("GET", self._url(candidate), headers=self._headers(candidate),
                                   params={"fields": "id,display_phone_number,verified_name"})
        if not isinstance(data, dict) or str(data.get("id")) != candidate["phone_number_id"]:
            raise ProviderError("invalid_provider_response")
        self.config.update(candidate)
        return AuthResult("connected", {"display_phone": data.get("display_phone_number"),
                                        "name": data.get("verified_name"),
                                        "notice": "Cloud API: свободный текст только в 24-часовом окне; входящие через webhook"})

    async def send(self, peer, text, client_id):
        recipient = _number(peer).lstrip("+")
        data = await self._request("POST", self._url(self.config, "/messages"), headers=self._headers(self.config),
                                   json={"messaging_product": "whatsapp", "recipient_type": "individual",
                                         "to": recipient, "type": "text", "text": {"body": text},
                                         "biz_opaque_callback_data": client_id})
        return self._message_id(data)

    async def send_template(self, peer: str, template: dict, client_id: str) -> str:
        recipient = _number(peer).lstrip("+")
        provider_template = validate_whatsapp_template(template)
        data = await self._request("POST", self._url(self.config, "/messages"), headers=self._headers(self.config),
                                   json={"messaging_product": "whatsapp", "recipient_type": "individual",
                                         "to": recipient, "type": "template", "template": provider_template,
                                         "biz_opaque_callback_data": client_id})
        return self._message_id(data)

    @staticmethod
    def _message_id(data):
        try:
            result = data["messages"][0]["id"]
            if not isinstance(result, str) or not result:
                raise ValueError()
            return result
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("invalid_provider_response", uncertain=True) from exc


class MaxBotAdapter(_HTTPAdapter):
    BASE_URL = "https://platform-api2.max.ru"

    @staticmethod
    def _headers(config):
        return {"Authorization": _required(config, "access_token")}

    async def auth(self, action, payload):
        if action not in {"login", "sync"}:
            return await super().auth(action, payload)
        candidate = {**self.config, **{k: v for k, v in payload.items()
                                    if k in {"access_token", "webhook_secret"}}}
        data = await self._request("GET", self.BASE_URL + "/me", headers=self._headers(candidate))
        if not isinstance(data, dict) or data.get("is_bot") is not True or not data.get("user_id"):
            raise ProviderError("invalid_provider_response")
        candidate["bot_id"] = str(data["user_id"])
        self.config.update(candidate)
        return AuthResult("connected", {"name": data.get("name", data.get("first_name")),
                                        "username": data.get("username"),
                                        "notice": "Официальный бот MAX, отдельный собеседник; входящие через webhook"})

    async def send(self, peer, text, client_id):
        kind, _, identifier = peer.partition(":")
        if not identifier:
            kind, identifier = "user", peer
        if (kind not in {"user", "chat"} or not re.fullmatch(r"-?[0-9]+", identifier)
                or (kind == "user" and int(identifier) <= 0)):
            raise ProviderError("invalid_max_peer")
        data = await self._request("POST", self.BASE_URL + "/messages", headers=self._headers(self.config),
                                   params={"user_id" if kind == "user" else "chat_id": identifier},
                                   json={"text": text})
        try:
            result = data["message"]["body"]["mid"]
            if not isinstance(result, str) or not result:
                raise ValueError()
            return result
        except (KeyError, TypeError, ValueError) as exc:
            raise ProviderError("invalid_provider_response", uncertain=True) from exc


class SMSGatewayAdapter(_HTTPAdapter):
    @staticmethod
    def _base():
        value = (os.getenv("SMS_GATEWAY_URL") or os.getenv("SMS_GATEWAY_BASE_URL", "")).rstrip("/")
        parsed = urlsplit(value)
        local_dev = os.getenv("APP_ENV", "development") != "production" and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        if (not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme != "https" and not (local_dev and parsed.scheme == "http"))):
            raise ProviderError("invalid_sms_gateway_base_url")
        return value

    @staticmethod
    def _credentials(config):
        return httpx.BasicAuth(_required(config, "username"), _required(config, "password"))

    async def auth(self, action, payload):
        if action not in {"login", "sync"}:
            return await super().auth(action, payload)
        candidate = {**self.config, **{k: v for k, v in payload.items()
                                    if k in {"username", "password", "device_id", "sim_number", "signing_key"}}}
        device = _required(candidate, "device_id")
        sim_number = candidate.get("sim_number", 1)
        if type(sim_number) is not int or sim_number not in {1, 2, 3}:
            raise ProviderError("invalid_sim_number")
        data = await self._request("GET", self._base() + "/devices", auth=self._credentials(candidate))
        if not isinstance(data, list) or not any(isinstance(d, dict) and str(d.get("id")) == device for d in data):
            raise ProviderError("sms_device_not_found")
        candidate["sim_number"] = sim_number
        self.config.update(candidate)
        return AuthResult("connected", {"device_id": device, "sim_number": sim_number,
                                        "notice": "SMSGate: рабочая SIM; входящие и статусы через подписанные webhooks"})

    async def send(self, peer, text, client_id):
        recipient = _number(peer, require_plus=True)
        data = await self._request("POST", self._base() + "/messages", auth=self._credentials(self.config),
                                   json={"id": client_id, "deviceId": _required(self.config, "device_id"),
                                         "simNumber": self.config.get("sim_number", 1),
                                         "phoneNumbers": [recipient], "textMessage": {"text": text},
                                         "withDeliveryReport": True})
        if not isinstance(data, dict) or not isinstance(data.get("id"), str) or not data["id"]:
            raise ProviderError("invalid_provider_response", uncertain=True)
        return data["id"]


def _wa_text(message):
    kind = message.get("type", "unknown")
    if kind == "text":
        return str((message.get("text") or {}).get("body", ""))
    caption = (message.get(kind) or {}).get("caption", "") if isinstance(message.get(kind), dict) else ""
    return f"[{kind}]" + (" " + str(caption) if caption else "")


def normalize_whatsapp(body: dict) -> list[Event]:
    """Native Meta payloads, including Coexistence echoes and history.

    Caller MUST filter metadata.phone_number_id against account.phone_number_id;
    a Meta application signature can authenticate events for many accounts.
    """
    if not isinstance(body, dict):
        raise ValueError("invalid_webhook_payload")
    events = []
    if body.get("object") != "whatsapp_business_account":
        return events
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value") or {}
            account = str((value.get("metadata") or {}).get("phone_number_id", ""))
            names = {str(c.get("wa_id")): (c.get("profile") or {}).get("name") for c in value.get("contacts", [])}

            def message_event(message, direction="inbound", history_peer=None, historical=False):
                peer = str(history_peer or message.get("to" if direction == "outbound" else "from", "")).lstrip("+")
                message_id = message.get("id")
                if not message_id or not peer or not account:
                    return
                events.append(Event(str(message_id), peer, _wa_text(message), direction=direction,
                                    timestamp=_unix(message.get("timestamp")), peer_name=names.get(peer),
                                    status="sent" if direction == "outbound" else "received",
                                    metadata={"phone_number_id": account, "type": message.get("type"),
                                              "aliases": ["+" + peer], "historical": historical}))

            for message in value.get("messages", []):
                message_event(message)
            for message in value.get("message_echoes", []):
                message_event(message, "outbound")
            for chunk in value.get("history", []):
                for thread in chunk.get("threads", []):
                    for message in thread.get("messages", []):
                        thread_peer = str(thread.get("id", "")).lstrip("+")
                        sender = str(message.get("from", "")).lstrip("+")
                        direction = "inbound" if sender == thread_peer else "outbound"
                        message_event(message, direction, thread_peer, True)
            for status in value.get("statuses", []):
                peer = str(status.get("recipient_id", "")).lstrip("+")
                if status.get("id") and peer and account and status.get("status") in {"sent", "delivered", "read", "failed"}:
                    events.append(Event(str(status["id"]), peer, "", direction="outbound", status=status["status"],
                                        timestamp=_unix(status.get("timestamp")), metadata={"status_only": True,
                                        "phone_number_id": account, "aliases": ["+" + peer],
                                        "client_id": status.get("biz_opaque_callback_data")}))
    return events


def normalize_max(body: dict) -> list[Event]:
    if not isinstance(body, dict):
        raise ValueError("invalid_webhook_payload")
    if body.get("update_type") != "message_created":
        return []
    message = body.get("message") or {}
    content = message.get("body") or {}
    sender = message.get("sender") or {}
    recipient = message.get("recipient") or {}
    if not content.get("mid") or not sender.get("user_id"):
        return []
    dialog = recipient.get("chat_type") == "dialog"
    identifier = str(sender["user_id"] if dialog else recipient.get("chat_id", ""))
    if not identifier:
        return []
    peer = ("user:" if dialog else "chat:") + identifier
    text = content.get("text") or ("[вложение]" if content.get("attachments") else "[пересланное сообщение]")
    return [Event(str(content["mid"]), peer, str(text), timestamp=_unix(message.get("timestamp", body.get("timestamp")), milliseconds=True),
                  peer_name=sender.get("name", sender.get("first_name")), metadata={
                      "bot_id": str(recipient.get("user_id", "")) if dialog else None,
                      "aliases": [identifier] if dialog else [], "attachments_present": bool(content.get("attachments"))})]


def normalize_sms(body: dict) -> list[Event]:
    if not isinstance(body, dict):
        raise ValueError("invalid_webhook_payload")
    kind = body.get("event")
    payload = body.get("payload") or {}
    incoming = kind in {"sms:received", "sms:batch:received"}
    statuses = {"sms:sent": ("sent", "sentAt"), "sms:delivered": ("delivered", "deliveredAt"),
                "sms:failed": ("failed", "failedAt"), "sms:cancelled": ("failed", "cancelledAt")}
    if not incoming and kind not in statuses:
        return []
    events = []
    messages = payload.get("messages", []) if kind == "sms:batch:received" else [payload]
    for message in messages:
        peer = message.get("sender" if incoming else "recipient") or message.get("phoneNumber")
        if not peer or not message.get("messageId") or not body.get("deviceId"):
            continue
        status, time_key = ("received", "receivedAt") if incoming else statuses[kind]
        events.append(Event(str(message["messageId"]), str(peer), str(message.get("message", "")),
                            direction="inbound" if incoming else "outbound", status=status,
                            timestamp=_unix(message.get(time_key)), metadata={
                                "device_id": str(body["deviceId"]), "sim_number": message.get("simNumber"),
                                "status_only": not incoming, "event_id": body.get("id"),
                                "historical": kind == "sms:batch:received"}))
    return events
