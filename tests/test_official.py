"""Provider contract tests. No real account login or external messages."""
import hashlib
import hmac
import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.adapters.base import ProviderError
from app.adapters.official import (
    MaxBotAdapter, SMSGatewayAdapter, WhatsAppCloudAdapter,
    normalize_max, normalize_sms, normalize_whatsapp,
    verify_max_secret, verify_sms_signature, verify_webhook, verify_whatsapp_signature,
)


class SignatureTests(unittest.TestCase):
    def test_whatsapp_raw_bytes_and_missing_secret(self):
        raw = b'{ "entry": [] }'
        signature = "sha256=" + hmac.new(b"key", raw, hashlib.sha256).hexdigest()
        self.assertTrue(verify_whatsapp_signature("key", raw, signature))
        self.assertFalse(verify_whatsapp_signature("key", b'{"entry":[]}', signature))
        self.assertFalse(verify_whatsapp_signature("", raw, signature))
        self.assertTrue(verify_webhook("whatsapp_cloud", raw, {"X-Hub-Signature-256": signature}, {"app_secret": "key"}))

    def test_sms_signs_body_then_timestamp_and_rejects_stale(self):
        raw, timestamp = b'{"event":"sms:received"}', "1800000000"
        digest = hmac.new(b"key", raw + timestamp.encode(), hashlib.sha256).hexdigest()
        self.assertTrue(verify_sms_signature("key", raw, timestamp, digest, now=1800000010))
        self.assertFalse(verify_sms_signature("key", raw, timestamp, digest, now=1800000400))
        self.assertFalse(verify_sms_signature("key", raw, "invalid", digest, now=1800000010))
        self.assertFalse(verify_sms_signature("key", raw + b" ", timestamp, digest, now=1800000010))

    def test_max_secret_and_unknown_channel(self):
        self.assertTrue(verify_max_secret("secret", "secret"))
        self.assertFalse(verify_max_secret("", ""))
        self.assertFalse(verify_max_secret("secret", "secreu"))
        self.assertFalse(verify_webhook("unknown", b"{}", {}, {}))


class NormalizationTests(unittest.TestCase):
    def test_whatsapp_multi_account_batch_echo_status_and_history(self):
        value = {"metadata": {"phone_number_id": "111"},
                 "messages": [{"id": "in", "from": "79990000000", "timestamp": "1800000000", "type": "text", "text": {"body": "Hi"}}],
                 "message_echoes": [{"id": "out", "to": "79990000000", "type": "text", "text": {"body": "Reply"}}],
                 "statuses": [{"id": "out", "recipient_id": "79990000000", "status": "read"}],
                 "history": [{"threads": [{"id": "79990000000", "messages": [
                     {"id": "old", "from": "78880000000", "type": "text", "text": {"body": "Old"}}
                 ]}]}]}
        other = {"metadata": {"phone_number_id": "222"}, "messages": [{"id": "foreign", "from": "78880000000", "type": "image"}]}
        events = normalize_whatsapp({"object": "whatsapp_business_account", "entry": [{"changes": [{"value": value}, {"value": other}]}]})
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0].direction, "inbound")
        self.assertEqual(events[0].timestamp, 1800000000)
        self.assertEqual(events[1].direction, "outbound")
        self.assertTrue(events[2].metadata["historical"])
        self.assertTrue(events[3].metadata["status_only"])
        self.assertEqual(events[4].metadata["phone_number_id"], "222")
        self.assertEqual(events[4].text, "[image]")

    def test_max_dialog_uses_sender_and_millisecond_timestamp(self):
        event = normalize_max({"update_type": "message_created", "message": {
            "sender": {"user_id": 42, "first_name": "Client"},
            "recipient": {"user_id": 99, "chat_id": 77, "chat_type": "dialog"},
            "body": {"mid": "max123", "text": "Здравствуйте"}, "timestamp": 1800000000123}})[0]
        self.assertEqual(event.peer, "user:42")
        self.assertEqual(event.metadata["bot_id"], "99")
        self.assertAlmostEqual(event.timestamp, 1800000000.123)
        self.assertEqual(normalize_max({"update_type": "bot_started"}), [])

    def test_sms_batch_current_and_legacy_fields(self):
        events = normalize_sms({"event": "sms:batch:received", "deviceId": "dev1", "id": "event",
                                "payload": {"messages": [
                                    {"messageId": "a", "sender": "+79990000000", "message": "A", "simNumber": 2,
                                     "receivedAt": "2026-02-18T02:05:00+03:00"},
                                    {"messageId": "b", "phoneNumber": "+78880000000", "message": "B"}]}})
        self.assertEqual(len(events), 2)
        self.assertEqual(events[0].metadata["device_id"], "dev1")
        self.assertEqual(events[0].metadata["sim_number"], 2)
        self.assertTrue(events[0].metadata["historical"])
        self.assertIsNotNone(events[0].timestamp)
        self.assertEqual(events[1].peer, "+78880000000")

    def test_sms_delivery_is_status_only_not_empty_message(self):
        event = normalize_sms({"event": "sms:delivered", "deviceId": "dev1", "payload": {
            "messageId": "id", "recipient": "+79990000000", "sender": None}})[0]
        self.assertTrue(event.metadata["status_only"])
        self.assertEqual(event.status, "delivered")
        self.assertEqual(event.direction, "outbound")


class HTTPAdapterTests(unittest.IsolatedAsyncioTestCase):
    async def test_whatsapp_template_builds_text_body_parameters_and_locale(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"messages": [{"id": "wamid-template"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WhatsAppCloudAdapter({"phone_number_id": "123", "access_token": "token"}, client=client)
            result = await adapter.send_template("+79990000000", {
                "name": "sales_followup", "language": "ru", "parameters": ["Анна", "Заказ №42"]}, "stable-client-id")
        self.assertEqual(result, "wamid-template")
        payload = json.loads(requests[0].content)
        self.assertEqual(payload["type"], "template")
        self.assertEqual(payload["to"], "79990000000")
        self.assertEqual(payload["template"], {"name": "sales_followup", "language": {"code": "ru"},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": "Анна"},
                                                              {"type": "text", "text": "Заказ №42"}]}]})
        self.assertEqual(payload["biz_opaque_callback_data"], "stable-client-id")
        self.assertNotIn("text", payload)

    async def test_whatsapp_static_template_omits_components(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"messages": [{"id": "static-template"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WhatsAppCloudAdapter({"phone_number_id": "123", "access_token": "token"}, client=client)
            await adapter.send_template("79990000000", {"name": "hello_world", "language": "en_US"}, "uuid")
        self.assertNotIn("components", json.loads(requests[0].content)["template"])

    async def test_whatsapp_invalid_templates_never_reach_provider(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(500)
        valid = {"name": "followup", "language": "ru", "parameters": []}
        invalid = [None, [], {}, {**valid, "name": "Bad Name"}, {**valid, "language": "ru/../me"},
                   {**valid, "components": [{"type": "header"}]},
                   {**valid, "parameters": [{"type": "image", "link": "https://other.test"}]},
                   {**valid, "parameters": ["x"] * 21}, {**valid, "parameters": ["x" * 1025]},
                   {**valid, "parameters": ["line\nbreak"]}, {**valid, "parameters": ["x" * 1024] * 5}]
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WhatsAppCloudAdapter({"phone_number_id": "123", "access_token": "token"}, client=client)
            for template in invalid:
                with self.subTest(template=template), self.assertRaises(ProviderError):
                    await adapter.send_template("+79990000000", template, "uuid")
        self.assertEqual(requests, [])

    async def test_whatsapp_unapproved_template_error_has_no_send_retry(self):
        requests = []
        def handler(request):
            requests.append(request)
            return httpx.Response(400, json={"error": {"code": 132001, "message": "Template missing"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WhatsAppCloudAdapter({"phone_number_id": "123", "access_token": "token"}, client=client)
            with self.assertRaises(ProviderError) as caught:
                await adapter.send_template("+79990000000", {"name": "not_approved", "language": "ru"}, "uuid")
        self.assertEqual(caught.exception.code, "provider_http_400")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(len(requests), 1)

    async def test_whatsapp_login_checks_credentials_then_sends_documented_payload(self):
        requests = []
        def handler(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"id": "123", "display_phone_number": "+78880000000"})
            return httpx.Response(200, json={"messages": [{"id": "wamid123"}]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = WhatsAppCloudAdapter({"phone_number_id": "123", "access_token": "secret"}, client=client)
            result = await adapter.auth("login", {})
            self.assertEqual(result.state, "connected")
            self.assertNotIn("secret", str(result.public))
            self.assertEqual(await adapter.send("+79990000000", "Hello", "client-uuid"), "wamid123")
        self.assertEqual(requests[0].url.host, "graph.facebook.com")
        self.assertEqual(requests[0].headers["Authorization"], "Bearer secret")
        payload = json.loads(requests[1].content)
        self.assertEqual(payload["to"], "79990000000")
        self.assertEqual(payload["biz_opaque_callback_data"], "client-uuid")

    async def test_failed_login_does_not_overwrite_working_credentials(self):
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(401))) as client:
            adapter = WhatsAppCloudAdapter({"phone_number_id": "123", "access_token": "old"}, client=client)
            with self.assertRaises(ProviderError) as caught:
                await adapter.auth("login", {"access_token": "new"})
            self.assertEqual(caught.exception.code, "provider_unauthorized")
            self.assertEqual(adapter.config["access_token"], "old")

    async def test_whatsapp_config_cannot_inject_url(self):
        adapter = WhatsAppCloudAdapter({"phone_number_id": "../evil?access_token=x", "access_token": "secret"})
        with self.assertRaises(ProviderError):
            await adapter.auth("login", {})
        self.assertIsNone(adapter.client)

    async def test_max_uses_current_host_plain_header_and_user_id(self):
        seen = []
        def handler(request):
            seen.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"user_id": 99, "is_bot": True, "name": "Sales"})
            return httpx.Response(200, json={"message": {"body": {"mid": "max1"}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = MaxBotAdapter({"access_token": "token"}, client=client)
            await adapter.auth("login", {})
            self.assertEqual(await adapter.send("user:42", "Привет", "uuid"), "max1")
        self.assertEqual(seen[1].url.host, "platform-api2.max.ru")
        self.assertEqual(seen[1].headers["Authorization"], "token")
        self.assertEqual(seen[1].url.params["user_id"], "42")
        self.assertNotIn("access_token", seen[1].url.params)

    async def test_sms_explicit_device_and_sim_and_stable_outgoing_id(self):
        seen = []
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=[{"id": "dev1"}] if request.method == "GET" else {"id": "uuid"})
        with patch.dict(os.environ, {"SMS_GATEWAY_URL": "https://sms.company.test/3rdparty/v1"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                adapter = SMSGatewayAdapter({"username": "u", "password": "p", "device_id": "dev1", "sim_number": 2,
                                             "base_url": "https://attacker.test"}, client=client)
                await adapter.auth("login", {})
                self.assertEqual(await adapter.send("+79990000000", "Hi", "uuid"), "uuid")
        payload = json.loads(seen[1].content)
        self.assertEqual(seen[1].url.host, "sms.company.test")
        self.assertEqual(payload["deviceId"], "dev1")
        self.assertEqual(payload["simNumber"], 2)
        self.assertEqual(payload["id"], "uuid")
        self.assertEqual(payload["textMessage"], {"text": "Hi"})

    async def test_sms_requires_registered_device(self):
        with patch.dict(os.environ, {"SMS_GATEWAY_URL": "https://sms.company.test/3rdparty/v1"}):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=[]))) as client:
                adapter = SMSGatewayAdapter({"username": "u", "password": "p", "device_id": "missing"}, client=client)
                with self.assertRaises(ProviderError) as caught:
                    await adapter.auth("login", {})
                self.assertEqual(caught.exception.code, "sms_device_not_found")

    async def test_timeout_after_send_is_uncertain_and_not_retried(self):
        calls = []
        def handler(request):
            calls.append(request)
            raise httpx.ReadTimeout("upstream timeout", request=request)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = MaxBotAdapter({"access_token": "token"}, client=client)
            with self.assertRaises(ProviderError) as caught:
                await adapter.send("user:42", "Message", "uuid")
            self.assertTrue(caught.exception.uncertain)
        self.assertEqual(len(calls), 1)

    async def test_redirect_is_never_followed_and_rate_limit_exposes_wait(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(302, headers={"Location": "https://attacker.test"})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), follow_redirects=True) as client:
            adapter = MaxBotAdapter({"access_token": "secret"}, client=client)
            with self.assertRaises(ProviderError) as caught:
                await adapter.auth("login", {})
            self.assertEqual(caught.exception.code, "provider_http_302")
        self.assertEqual(len(calls), 1)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(429, headers={"Retry-After": "17"}))) as client:
            adapter = MaxBotAdapter({"access_token": "secret"}, client=client)
            with self.assertRaises(ProviderError) as caught:
                await adapter.send("42", "Hello", "uuid")
            self.assertEqual(caught.exception.retry_after, 17)
            self.assertFalse(caught.exception.uncertain)

    async def test_local_disconnect_does_not_claim_remote_revocation(self):
        result = await MaxBotAdapter({"access_token": "secret"}).auth("disconnect", {})
        self.assertEqual(result.state, "disconnected")
        self.assertTrue(result.public["remote_revocation_required"])


if __name__ == "__main__":
    unittest.main()
