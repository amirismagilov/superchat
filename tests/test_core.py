import hashlib
import hmac
import json
from datetime import timedelta
from uuid import uuid4

import jwt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import api, db as coredb
from app.config import settings
from app.db import Account, Base, Conversation, IntegrationSetting, Job, Message, now, session_dependency
from app.security import seal, unseal


@pytest.fixture
def app_client(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(settings, "encryption_key", Fernet.generate_key().decode())
    monkeypatch.setattr(settings, "jwt_secret", "test-only-secret-" * 4)
    monkeypatch.setattr(settings, "jwks_url", "")
    monkeypatch.setattr(settings, "demo", True)
    monkeypatch.setattr(settings, "local_test", False)
    monkeypatch.setattr(settings, "environment", "development")
    monkeypatch.setattr(coredb, "Session", factory)

    def sessions():
        with factory() as session:
            yield session
    api.app.dependency_overrides[session_dependency] = sessions
    with TestClient(api.app) as client:
        yield client, factory
    api.app.dependency_overrides.clear()
    engine.dispose()


def auth(sub="alice", tenant="company", role="agent", **extra):
    claims = {"sub": sub, "tenant_id": tenant, "role": role, "iss": settings.issuer, "aud": settings.audience,
              "iat": now(), "exp": now() + timedelta(minutes=5), **extra}
    return {"Authorization": "Bearer " + jwt.encode(claims, settings.jwt_secret, algorithm="HS256")}


def setup_conversation(client, factory, channel="demo", config=None):
    with factory.begin() as db:
        a = Account(tenant_id="company", owner_id="alice", channel=channel, label="Corporate", state="connected", encrypted_config=seal(config or {}))
        db.add(a)
        db.flush()
        c = Conversation(account_id=a.id, peer="15551234567", title="Client")
        db.add(c)
        db.flush()
        return a.id, c.id


def test_tenant_and_owner_isolation_for_every_resource(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    message = client.post(f"/api/conversations/{c}/messages", headers=auth(), json={"text": "private", "client_id": str(uuid4())}).json()
    job = client.post(f"/api/accounts/{a}/actions", headers=auth(), json={"action": "sync", "payload": {}}).json()
    for attacker in (auth("bob"), auth("alice", "other", "admin")):
        assert client.get("/api/accounts", headers=attacker).json() == []
        assert client.get("/api/conversations", headers=attacker).json() == []
        for path in (f"accounts/{a}", f"conversations/{c}/messages", f"messages/{message['id']}", f"jobs/{job['id']}"):
            assert client.get("/api/" + path, headers=attacker).status_code == 404
        assert client.post(f"/api/accounts/{a}/actions", headers=attacker, json={"action": "disconnect"}).status_code == 404


def test_idempotency_survives_duplicate_and_rejects_body_change(app_client):
    client, factory = app_client
    _, c = setup_conversation(client, factory)
    body = {"text": "Offer", "client_id": str(uuid4())}
    first = client.post(f"/api/conversations/{c}/messages", headers=auth(), json=body)
    second = client.post(f"/api/conversations/{c}/messages", headers=auth(), json=body)
    assert first.status_code == second.status_code == 202
    assert first.json()["id"] == second.json()["id"]
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json={**body, "text": "Different"}).status_code == 409
    with factory() as db:
        assert len(list(db.scalars(select(Message)))) == 1


def test_reassignment_keeps_history_and_removes_old_access(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json={"text": "contract", "client_id": str(uuid4())}).status_code == 202
    assert client.post(f"/api/accounts/{a}/assign", headers=auth(), json={"owner_id": "bob"}).status_code == 403
    assert client.post(f"/api/accounts/{a}/assign", headers=auth(role="admin"), json={"owner_id": "bob"}).status_code == 200
    assert client.get(f"/api/conversations/{c}/messages", headers=auth()).status_code == 404
    assert client.get(f"/api/conversations/{c}/messages", headers=auth("bob")).json()[0]["text"] == "contract"


def test_secrets_are_encrypted_and_never_returned(app_client):
    client, factory = app_client
    config = {"api_id": 123, "api_hash": "super-private-hash", "phone": "+15551234567"}
    response = client.post("/api/accounts", headers=auth(role="admin"), json={"channel": "telegram", "label": "TG", "config": config})
    assert response.status_code == 201
    assert "super-private-hash" not in response.text
    with factory() as db:
        saved = db.get(Account, response.json()["id"])
        assert "super-private-hash" not in saved.encrypted_config
        assert unseal(saved.encrypted_config) == config
    # Existing managers may still authenticate accounts provisioned by an admin.
    assert client.post(f"/api/accounts/{response.json()['id']}/actions", headers=auth(), json={"action": "login"}).status_code == 202
    assert client.post("/api/accounts", headers=auth(), json={"channel": "telegram", "label": "TG", "config": {**config, "session": "stolen"}}).status_code == 422
    assert client.post("/api/accounts", headers=auth(), json={"channel": "telegram", "label": "TG", "config": config}).status_code == 403


def test_telegram_app_setup_is_admin_only_tenant_scoped_and_never_returns_credentials(app_client):
    client, factory = app_client
    credentials = {"api_id": 234567, "api_hash": "a7" * 16}
    for method in (client.get, client.put):
        kwargs = {"json": credentials} if method == client.put else {}
        assert method("/api/integrations/telegram", headers=auth(), **kwargs).status_code == 403
    response = client.put("/api/integrations/telegram", headers=auth(role="admin"), json=credentials)
    assert response.json() == {"channel": "telegram", "configured": True}
    with factory() as db:
        saved = db.get(IntegrationSetting, ("company", "telegram"))
        assert unseal(saved.encrypted_config) == credentials
        assert credentials["api_hash"] not in saved.encrypted_config
    for tenant, configured in (("company", True), ("other", False)):
        public = client.get("/api/capabilities", headers=auth(tenant=tenant))
        tg = next(row for row in public.json() if row["channel"] == "telegram")
        assert tg["connection"]["configured"] is configured
        assert tg["connection"]["methods"] == ["phone", "qr"]
        assert [field["name"] for field in tg["config_fields"]] == ["phone"]
        assert credentials["api_hash"] not in public.text
        assert str(credentials["api_id"]) not in public.text
        response = client.get("/api/integrations/telegram", headers=auth(tenant=tenant, role="admin"))
        assert response.json() == {"channel": "telegram", "configured": configured}


def test_telegram_manager_creates_phone_or_qr_account_with_pinned_tenant_app_credentials(app_client):
    client, factory = app_client
    body = {"channel": "telegram", "label": "My Telegram", "config": {"phone": "+79990000001"}}
    assert client.post("/api/accounts", headers=auth(), json=body).json()["detail"] == "telegram_app_not_configured"
    credentials = {"api_id": 234567, "api_hash": "a7" * 16}
    assert client.put("/api/integrations/telegram", headers=auth(role="admin"), json=credentials).status_code == 200
    created = []
    for config in (body["config"], {}):
        response = client.post("/api/accounts", headers=auth(), json={**body, "config": config})
        assert response.status_code == 201
        assert credentials["api_hash"] not in response.text
        created.append(response.json()["id"])
        with factory() as db:
            assert unseal(db.get(Account, created[-1]).encrypted_config) == config | credentials
    # Separate tenants cannot use another company's Telegram application.
    assert client.post("/api/accounts", headers=auth(tenant="other"), json=body).status_code == 422
    # Rotation only affects new accounts, not already authenticated sessions.
    client.put("/api/integrations/telegram", headers=auth(role="admin"), json={"api_id": 999, "api_hash": "b" * 32})
    with factory() as db:
        assert unseal(db.get(Account, created[0]).encrypted_config)["api_hash"] == credentials["api_hash"]


@pytest.mark.parametrize("body", [
    {"api_id": True, "api_hash": "a" * 32}, {"api_id": 0, "api_hash": "a" * 32},
    {"api_id": 1, "api_hash": "secret-invalid"}, {"api_id": "123", "api_hash": "a" * 32},
    {"api_id": 1, "api_hash": "a" * 32, "session": "never-accept-session"},
])
def test_telegram_app_setup_rejects_invalid_input_without_echoing_credentials(app_client, body):
    client, _ = app_client
    response = client.put("/api/integrations/telegram", headers=auth(role="admin"), json=body)
    assert response.status_code == 422
    assert response.json() == {"detail": "telegram_configuration_invalid"}


def test_malformed_auth_body_does_not_echo_private_input(app_client):
    client, _ = app_client
    response = client.put("/api/integrations/telegram", headers=auth(role="admin"),
                          json=[{"api_hash": "sensitive-input"}])
    assert response.status_code == 422
    assert "sensitive-input" not in response.text
    assert all("input" not in detail for detail in response.json()["detail"])


def test_auth_rejects_expiry_audience_and_missing_tenant(app_client):
    client, _ = app_client
    assert client.get("/api/me").status_code == 401
    assert client.get("/api/me", headers=auth(exp=now() - timedelta(seconds=1))).status_code == 401
    assert client.get("/api/me", headers=auth(aud="wrong")).status_code == 401
    assert client.get("/api/me", headers=auth(tenant_id=None)).status_code == 401


def test_local_login_is_opt_in_and_preserves_identity_without_demo_data(app_client, monkeypatch):
    client, _ = app_client
    assert client.post("/api/local/token").status_code == 404
    assert client.get("/api/runtime").json() == {"local_test": False, "demo": True}
    monkeypatch.setattr(settings, "local_test", True)
    monkeypatch.setattr(settings, "demo", False)
    local_headers = {"Host": "localhost:8080", "Origin": "http://localhost:8080", "Sec-Fetch-Site": "same-origin"}
    assert client.get("/api/runtime", headers=local_headers).json() == {"local_test": True, "demo": False}
    assert client.post("/api/dev/token", headers=local_headers).status_code == 404
    first = client.post("/api/local/token", headers=local_headers)
    assert first.status_code == 200
    authorization = {**local_headers, "Authorization": "Bearer " + first.json()["access_token"]}
    assert client.get("/api/me", headers=authorization).json() == {"sub": "local-owner", "tenant_id": "local-test", "role": "admin"}
    assert client.get("/api/accounts", headers=authorization).json() == []
    assert client.get("/api/conversations", headers=authorization).json() == []
    account = client.post("/api/accounts", headers=authorization, json={"channel": "telegram", "label": "Local Telegram", "config": {"api_id": 123, "api_hash": "private", "phone": "+15551234567"}})
    assert account.status_code == 201
    second = client.post("/api/local/token", headers=local_headers).json()
    authorization["Authorization"] = "Bearer " + second["access_token"]
    saved = client.get("/api/accounts", headers=authorization).json()
    assert len(saved) == 1 and saved[0]["id"] == account.json()["id"]
    assert saved[0]["state"] == "disconnected"


@pytest.mark.parametrize("headers,status", [
    ({"Host": "attacker.example"}, 400),
    ({"Host": "localhost:8080", "Origin": "https://attacker.example"}, 403),
    ({"Host": "localhost:8080", "Origin": "null"}, 403),
    ({"Host": "localhost:8080", "Origin": "http://localhost:8081"}, 403),
    ({"Host": "localhost:8080", "Sec-Fetch-Site": "cross-site"}, 403),
    ({"Host": "127.0.0.1:8080"}, 200),
])
def test_local_login_rejects_rebinding_and_cross_origin(app_client, monkeypatch, headers, status):
    client, _ = app_client
    monkeypatch.setattr(settings, "local_test", True)
    monkeypatch.setattr(settings, "demo", False)
    response = client.post("/api/local/token", headers=headers)
    assert response.status_code == status
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("overrides", [
    {"environment": "production"}, {"demo": True}, {"jwt_secret": "short"},
    {"jwks_url": "https://example.com/jwks"},
])
def test_local_login_config_cannot_enable_production_bootstrap(overrides):
    from app.config import Settings
    config = {"environment": "development", "local_test": True, "demo": False,
              "database_url": "postgresql://unused", "encryption_key": "unused", "jwt_secret": "x" * 48, "jwks_url": ""}
    with pytest.raises(RuntimeError):
        Settings(**(config | overrides)).validate()


def test_cloud_outside_window_cannot_send_free_text(app_client):
    client, factory = app_client
    _, c = setup_conversation(client, factory, "whatsapp_cloud")
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json={"text": "hello", "client_id": str(uuid4())}).status_code == 409
    with factory.begin() as db:
        db.get(Conversation, c).last_inbound_at = now() - timedelta(hours=1)
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json={"text": "reply", "client_id": str(uuid4())}).status_code == 202


def test_cloud_template_allowed_outside_window_and_idempotency_covers_template(app_client):
    client, factory = app_client
    _, c = setup_conversation(client, factory, "whatsapp_cloud")
    body = {"text": "Условия договора", "client_id": str(uuid4()), "template": {"name": "contract_offer", "language": "ru", "parameters": ["Анна"]}}
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json=body).status_code == 202
    body["template"]["parameters"] = ["Другой клиент"]
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json=body).status_code == 409
    body["client_id"] = str(uuid4())
    body["template"]["name"] = "bad name"
    assert client.post(f"/api/conversations/{c}/messages", headers=auth(), json=body).status_code == 422


def test_webhook_auth_dedupe_and_number_binding(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory, "whatsapp_cloud", {"app_secret": "secret", "phone_number_id": "123"})
    body = {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {"metadata": {"phone_number_id": "123"}, "messages": [{"id": "inbound-1", "from": "15551234567", "type": "text", "text": {"body": "hello"}}]}}, {"value": {"metadata": {"phone_number_id": "other"}, "messages": [{"id": "foreign", "from": "15551234567", "type": "text", "text": {"body": "another tenant"}}]}}]}]}
    raw = json.dumps(body).encode()
    headers = {"X-Hub-Signature-256": "sha256=" + hmac.new(b"secret", raw, hashlib.sha256).hexdigest()}
    assert client.post(f"/webhooks/whatsapp_cloud/{a}", content=raw).status_code == 401
    for _ in range(2):
        assert client.post(f"/webhooks/whatsapp_cloud/{a}", content=raw, headers=headers).status_code == 200
    messages = client.get(f"/api/conversations/{c}/messages", headers=auth()).json()
    assert len(messages) == 1 and messages[0]["provider_id"] == "inbound-1"


def test_imported_history_is_chronological_and_cursor_is_scoped(app_client):
    client, factory = app_client
    a, c = setup_conversation(client, factory)
    with factory.begin() as db:
        db.add(Message(conversation_id=c, text="today", direction="inbound", status="received", created_at=now()))
        db.flush()
        db.add(Message(conversation_id=c, text="yesterday imported later", direction="inbound", status="received", created_at=now() - timedelta(days=1)))
    first = client.get(f"/api/conversations/{c}/messages?limit=1", headers=auth()).json()
    assert first[0]["text"] == "today"
    previous = client.get(f"/api/conversations/{c}/messages?before={first[0]['id']}", headers=auth()).json()
    assert previous[0]["text"] == "yesterday imported later"


def test_production_rejects_demo_and_sqlite(monkeypatch):
    from app.config import Settings
    s = Settings(environment="production", demo=True, database_url="sqlite://", encryption_key="x", jwt_secret="x" * 40)
    with pytest.raises(RuntimeError):
        s.validate()


def test_max_guided_connection_needs_no_app_credentials_and_encrypts_auth_jobs(app_client, monkeypatch):
    client, factory = app_client
    monkeypatch.setattr(settings, "experimental", True)
    capability = next(item for item in client.get("/api/capabilities", headers=auth()).json()
                      if item["channel"] == "max_personal")
    assert capability["connection"] == {"flow": "qr", "configured": True, "methods": ["qr", "phone"]}
    response = client.post("/api/accounts", headers=auth(), json={"channel": "max_personal", "label": "MAX", "config": {}})
    assert response.status_code == 201
    account_id = response.json()["id"]
    phone = "+79990000001"
    for action, payload in [("qr", {}), ("login", {"login_method": "sms", "phone": phone}),
                            ("code", {"code": "123456"}), ("password", {"password": "test-private-password"})]:
        result = client.post(f"/api/accounts/{account_id}/actions", headers=auth(), json={"action": action, "payload": payload})
        assert result.status_code == 202
        assert set(result.json()) == {"id", "status"}
        with factory() as db:
            job = db.get(Job, result.json()["id"])
            assert unseal(job.encrypted_payload) == payload
            assert all(value not in job.encrypted_payload for value in payload.values() if len(value) >= 6)
        public = client.get(f"/api/jobs/{result.json()['id']}", headers=auth()).json()
        assert set(public) == {"id", "status", "error"}
    assert phone not in client.get(f"/api/accounts/{account_id}", headers=auth()).text


@pytest.mark.parametrize("config", [
    {"login_method": "external"}, {"login_method": []}, {"phone": "not-a-phone"},
    {"phone": 79990000001}, {"history_days": True}, {"history_days": 0},
    {"_max_session": {"token": "not-client-supplied"}},
])
def test_max_rejects_invalid_or_private_connection_config(app_client, monkeypatch, config):
    client, factory = app_client
    monkeypatch.setattr(settings, "experimental", True)
    response = client.post("/api/accounts", headers=auth(), json={"channel": "max_personal", "label": "MAX", "config": config})
    assert response.status_code == 422
    with factory() as db:
        assert list(db.scalars(select(Account))) == []


@pytest.mark.parametrize("action,payload", [
    ("login", {"login_method": "external"}), ("login", {"phone": "wrong"}),
    ("qr", {"track_id": "foreign-challenge"}), ("code", {"code": "123456", "token": "foreign-token"}),
])
def test_max_rejects_client_supplied_challenges_before_enqueue(app_client, monkeypatch, action, payload):
    client, factory = app_client
    monkeypatch.setattr(settings, "experimental", True)
    account_id, _ = setup_conversation(client, factory, channel="max_personal")
    response = client.post(f"/api/accounts/{account_id}/actions", headers=auth(), json={"action": action, "payload": payload})
    assert response.status_code == 422
    with factory() as db:
        assert list(db.scalars(select(Job))) == []
