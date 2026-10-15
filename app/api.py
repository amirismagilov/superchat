import hashlib
import hmac
import json
import re
from contextlib import asynccontextmanager
from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import and_, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .capabilities import capabilities
from .config import settings
from .db import Account, Base, Conversation, IntegrationSetting, Job, Message, WebhookReceipt, engine, now, session_dependency
from .security import Identity, identity, seal, unseal
from .services import account_for, account_json, audit, aware, conversation_for, ingest, message_json


@asynccontextmanager
async def lifespan(app):
    settings.validate()
    if settings.environment != "production":
        Base.metadata.create_all(engine)
    yield


app = FastAPI(title="OmniCRM messaging", version="0.1.0", lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    # Pydantic's default error response echoes `input`, including API hashes,
    # passwords and one-time codes when the surrounding request is malformed.
    return JSONResponse({"detail": [{key: item[key] for key in ("loc", "msg", "type")}
                                    for item in exc.errors()]}, status_code=422)


@app.middleware("http")
async def headers(request, call_next):
    # Docker forwards a loopback-published port from its bridge address, so validate
    # Host rather than the TCP client address. Reject DNS rebinding in local mode.
    if settings.local_test and request.url.hostname not in {"localhost", "127.0.0.1", "::1"}:
        response = JSONResponse({"detail": "local_host_required"}, status_code=400)
    else:
        response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'"
    return response


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AccountCreate(Body):
    channel: str
    label: str = Field(min_length=1, max_length=160)
    config: dict = Field(default_factory=dict)
    owner_id: str | None = Field(default=None, min_length=1, max_length=128)


class Action(Body):
    action: str
    payload: dict = Field(default_factory=dict)


class Assign(Body):
    owner_id: str = Field(min_length=1, max_length=128)


class ConversationCreate(Body):
    account_id: str
    peer: str = Field(min_length=1, max_length=256)
    title: str = Field(default="", max_length=256)


class Send(Body):
    text: str = Field(min_length=1, max_length=4000)
    client_id: UUID
    template: dict | None = None


class DemoInbound(Body):
    conversation_id: str
    text: str = Field(min_length=1, max_length=4000)


@app.get("/health/live")
def live():
    return {"status": "ok"}


@app.get("/health/ready")
def ready(db: Session = Depends(session_dependency)):
    db.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/api/dev/token")
def dev_token(db: Session = Depends(session_dependency)):
    if not settings.demo or settings.environment == "production":
        raise HTTPException(404)
    demo_user = Identity("demo-agent", "demo", "admin")
    account = db.scalar(select(Account).where(Account.tenant_id == "demo", Account.channel == "demo"))
    if not account:
        account = Account(tenant_id="demo", owner_id="demo-agent", channel="demo", label="Демо · рабочий номер", state="connected", encrypted_config=seal({}))
        db.add(account)
        db.flush()
        c = Conversation(account_id=account.id, peer="demo-client", title="Анна · пример диалога")
        db.add(c)
        db.flush()
        from .adapters.base import Event
        ingest(db, account, Event("seed", "demo-client", "Здравствуйте! Пришлите, пожалуйста, условия договора.", peer_name="Анна · пример диалога"))
        audit(db, demo_user, "demo.initialize", account.id)
        db.commit()
    token = jwt.encode({"sub": "demo-agent", "tenant_id": "demo", "role": "admin", "iss": settings.issuer,
                        "aud": settings.audience, "iat": now(), "exp": now() + timedelta(hours=1)}, settings.jwt_secret, algorithm="HS256")
    return {"access_token": token}


@app.get("/api/runtime")
def runtime():
    return {"local_test": settings.local_test and settings.environment != "production",
            "demo": settings.demo and settings.environment != "production"}


@app.post("/api/local/token")
def local_token(request: Request):
    if not settings.local_test or settings.environment == "production":
        raise HTTPException(404)
    origin = request.headers.get("origin")
    if (origin is not None and origin != f"{request.url.scheme}://{request.url.netloc}") or request.headers.get("sec-fetch-site") == "cross-site":
        raise HTTPException(403, "same_origin_required")
    # A stable local identity preserves access after restarting the browser or
    # containers. No accounts, conversations or messages are created at login.
    token = jwt.encode({"sub": "local-owner", "tenant_id": "local-test", "role": "admin",
                        "iss": settings.issuer, "aud": settings.audience,
                        "iat": now(), "exp": now() + timedelta(hours=8)}, settings.jwt_secret, algorithm="HS256")
    return {"access_token": token}


@app.get("/api/me")
def me(user=Depends(identity)):
    return user


@app.get("/api/capabilities")
def get_capabilities(user=Depends(identity), db: Session = Depends(session_dependency)):
    return capabilities(db.get(IntegrationSetting, (user.tenant_id, "telegram")) is not None)


@app.get("/api/integrations/telegram")
def get_telegram_integration(user=Depends(identity), db: Session = Depends(session_dependency)):
    if user.role != "admin":
        raise HTTPException(403, "admin_required")
    return {"channel": "telegram", "configured": db.get(IntegrationSetting, (user.tenant_id, "telegram")) is not None}


@app.put("/api/integrations/telegram")
def configure_telegram(body: dict, user=Depends(identity), db: Session = Depends(session_dependency)):
    if user.role != "admin":
        raise HTTPException(403, "admin_required")
    # Validation errors deliberately contain no submitted values or credentials.
    api_id, api_hash = body.get("api_id"), body.get("api_hash")
    if (set(body) != {"api_id", "api_hash"} or isinstance(api_id, bool)
            or not isinstance(api_id, int) or not 1 <= api_id <= 2147483647
            or not isinstance(api_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", api_hash)):
        raise HTTPException(422, "telegram_configuration_invalid")
    row = db.get(IntegrationSetting, (user.tenant_id, "telegram"), with_for_update=True)
    if row is None:
        row = IntegrationSetting(tenant_id=user.tenant_id, channel="telegram")
        db.add(row)
    row.encrypted_config = seal({"api_id": api_id, "api_hash": api_hash})
    row.updated_at = now()
    audit(db, user, "integration.configure", "telegram")
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "integration_update_conflict_retry") from None
    return {"channel": "telegram", "configured": True}


@app.get("/api/accounts")
def accounts(user=Depends(identity), db: Session = Depends(session_dependency)):
    query = select(Account).where(Account.tenant_id == user.tenant_id)
    if user.role != "admin":
        query = query.where(Account.owner_id == user.sub)
    return [account_json(a) for a in db.scalars(query.order_by(Account.created_at))]


@app.get("/api/accounts/{account_id}")
def get_account(account_id: str, user=Depends(identity), db: Session = Depends(session_dependency)):
    return account_json(account_for(db, account_id, user))


@app.post("/api/accounts", status_code=201)
def create_account(body: AccountCreate, user=Depends(identity), db: Session = Depends(session_dependency)):
    capability = next((c for c in capabilities() if c["channel"] == body.channel), None)
    if not capability or not capability["enabled"]:
        raise HTTPException(422, "channel_disabled")
    if body.owner_id and body.owner_id != user.sub and user.role != "admin":
        raise HTTPException(403, "admin_required")
    allowed = {f["name"] for f in capability["config_fields"]}
    if body.channel == "telegram":
        # Keep the original account API working for existing integrations.
        allowed.update({"api_id", "api_hash"})
    if set(body.config) - allowed or len(json.dumps(body.config)) > 20000:
        raise HTTPException(422, "unsupported_config_fields")
    missing = [f["name"] for f in capability["config_fields"] if f["required"] and not body.config.get(f["name"])]
    if missing:
        raise HTTPException(422, {"missing_fields": missing})
    config = dict(body.config)
    if body.channel == "telegram":
        if "api_id" in config or "api_hash" in config:
            if user.role != "admin":
                raise HTTPException(403, "admin_required")
            if not config.get("api_id") or not config.get("api_hash"):
                raise HTTPException(422, "telegram_configuration_invalid")
        else:
            integration = db.get(IntegrationSetting, (user.tenant_id, "telegram"))
            if integration is None:
                raise HTTPException(422, "telegram_app_not_configured")
            # Pin app credentials to the session: rotating the admin setting does
            # not silently replace the app identity of existing MTProto sessions.
            config.update(unseal(integration.encrypted_config))
        if "phone" in config and (not isinstance(config["phone"], str) or not re.fullmatch(r"\+[1-9][0-9]{7,14}", config["phone"])):
            raise HTTPException(422, "telegram_phone_invalid")
    if body.channel == "max_personal":
        if config.get("login_method", "qr") not in ("qr", "sms"):
            raise HTTPException(422, "max_login_method_invalid")
        if "phone" in config and (not isinstance(config["phone"], str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", config["phone"])):
            raise HTTPException(422, "max_phone_required")
        if "history_days" in config and (type(config["history_days"]) is not int or not 1 <= config["history_days"] <= 3650):
            raise HTTPException(422, "max_history_days_invalid")
    a = Account(tenant_id=user.tenant_id, owner_id=body.owner_id or user.sub, channel=body.channel,
                label=body.label, encrypted_config=seal(config))
    db.add(a)
    db.flush()
    audit(db, user, "account.create", a.id)
    db.commit()
    return account_json(a)


@app.post("/api/accounts/{account_id}/assign")
def assign(account_id: str, body: Assign, user=Depends(identity), db: Session = Depends(session_dependency)):
    if user.role != "admin":
        raise HTTPException(403, "admin_required")
    a = account_for(db, account_id, user)
    a.owner_id = body.owner_id
    audit(db, user, "account.assign", a.id)
    db.commit()
    return account_json(a)


@app.post("/api/accounts/{account_id}/actions", status_code=202)
def action(account_id: str, body: Action, user=Depends(identity), db: Session = Depends(session_dependency)):
    a = account_for(db, account_id, user)
    if body.action not in {"login", "code", "password", "sync", "disconnect", "qr"}:
        raise HTTPException(422, "unsupported_action")
    if len(json.dumps(body.payload)) > 12000:
        raise HTTPException(422, "payload_too_large")
    if a.channel == "max_personal":
        allowed = {"login": {"phone", "login_method"}, "qr": set(), "code": {"code"},
                   "password": {"password"}, "sync": set(), "disconnect": set()}[body.action]
        if set(body.payload) - allowed:
            raise HTTPException(422, "unsupported_auth_fields")
        if "login_method" in body.payload and body.payload["login_method"] not in ("qr", "sms"):
            raise HTTPException(422, "max_login_method_invalid")
        if "phone" in body.payload and (not isinstance(body.payload["phone"], str) or not re.fullmatch(r"\+[1-9][0-9]{6,14}", body.payload["phone"])):
            raise HTTPException(422, "max_phone_required")
    job = Job(account_id=a.id, action=body.action, encrypted_payload=seal(body.payload))
    db.add(job)
    db.flush()
    audit(db, user, "account." + body.action, a.id)
    db.commit()
    return {"id": job.id, "status": job.status}


@app.post("/api/accounts/{account_id}/disconnect", status_code=202)
def disconnect(account_id: str, user=Depends(identity), db: Session = Depends(session_dependency)):
    return action(account_id, Action(action="disconnect"), user, db)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, user=Depends(identity), db: Session = Depends(session_dependency)):
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(404)
    account_for(db, job.account_id, user)
    return {"id": job.id, "status": job.status, "error": job.error}


@app.get("/api/conversations")
def conversations(account_id: str | None = None, user=Depends(identity), db: Session = Depends(session_dependency)):
    query = select(Conversation).join(Account).where(Account.tenant_id == user.tenant_id)
    if user.role != "admin":
        query = query.where(Account.owner_id == user.sub)
    if account_id:
        account_for(db, account_id, user)
        query = query.where(Conversation.account_id == account_id)
    return list(db.scalars(query.order_by(Conversation.last_message_at.desc().nullslast()).limit(500)))


@app.post("/api/conversations", status_code=201)
def create_conversation(body: ConversationCreate, user=Depends(identity), db: Session = Depends(session_dependency)):
    account_for(db, body.account_id, user)
    peer = body.peer.strip()
    if not peer:
        raise HTTPException(422, "peer_required")
    existing = db.scalar(select(Conversation).where(Conversation.account_id == body.account_id, Conversation.peer == peer))
    if existing:
        return existing
    c = Conversation(account_id=body.account_id, peer=peer, title=body.title.strip() or peer)
    db.add(c)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        c = db.scalar(select(Conversation).where(Conversation.account_id == body.account_id, Conversation.peer == peer))
    return c


@app.get("/api/conversations/{conversation_id}/messages")
def messages(conversation_id: str, before: int | None = None, limit: int = Query(100, ge=1, le=200), user=Depends(identity), db: Session = Depends(session_dependency)):
    conversation_for(db, conversation_id, user)
    query = select(Message).where(Message.conversation_id == conversation_id)
    if before is not None:
        cursor = db.get(Message, before)
        if not cursor or cursor.conversation_id != conversation_id:
            raise HTTPException(422, "invalid_message_cursor")
        query = query.where(or_(Message.created_at < cursor.created_at, and_(Message.created_at == cursor.created_at, Message.id < cursor.id)))
    rows = list(db.scalars(query.order_by(Message.created_at.desc(), Message.id.desc()).limit(limit)))
    return [message_json(m) for m in reversed(rows)]


@app.post("/api/conversations/{conversation_id}/messages", status_code=202)
def send(conversation_id: str, body: Send, user=Depends(identity), db: Session = Depends(session_dependency)):
    c = conversation_for(db, conversation_id, user)
    a = account_for(db, c.account_id, user)
    if body.template is not None:
        if a.channel != "whatsapp_cloud":
            raise HTTPException(422, "templates_only_for_whatsapp_cloud")
        from .adapters.base import ProviderError
        from .adapters.official import validate_whatsapp_template
        try:
            validate_whatsapp_template(body.template)
        except ProviderError as exc:
            raise HTTPException(422, exc.code) from None
    old = db.scalar(select(Message).where(Message.conversation_id == c.id, Message.client_id == str(body.client_id)))
    if old:
        if old.text != body.text or old.template != body.template:
            raise HTTPException(409, "idempotency_key_reused_with_different_body")
        return message_json(old)
    if a.state != "connected":
        raise HTTPException(409, "account_not_connected")
    if not body.text.strip():
        raise HTTPException(422, "empty_text")
    if a.channel == "whatsapp_cloud" and body.template is None and (not c.last_inbound_at or now() - aware(c.last_inbound_at) >= timedelta(hours=24)):
        raise HTTPException(409, "whatsapp_template_required_outside_24h")
    m = Message(conversation_id=c.id, client_id=str(body.client_id), text=body.text, template=body.template, direction="outbound", status="queued")
    db.add(m)
    c.last_message_at = now()
    try:
        db.flush()
        audit(db, user, "message.enqueue", m.id)
        db.commit()
    except IntegrityError:
        db.rollback()
        old = db.scalar(select(Message).where(Message.conversation_id == c.id, Message.client_id == str(body.client_id)))
        if not old or old.text != body.text or old.template != body.template:
            raise HTTPException(409, "idempotency_conflict")
        return message_json(old)
    return message_json(m)


@app.get("/api/messages/{message_id}")
def get_message(message_id: int, user=Depends(identity), db: Session = Depends(session_dependency)):
    m = db.get(Message, message_id)
    if not m:
        raise HTTPException(404)
    conversation_for(db, m.conversation_id, user)
    return message_json(m)


@app.post("/api/demo/inbound")
def demo_inbound(body: DemoInbound, user=Depends(identity), db: Session = Depends(session_dependency)):
    c = conversation_for(db, body.conversation_id, user)
    a = account_for(db, c.account_id, user)
    if not settings.demo or a.channel != "demo":
        raise HTTPException(404)
    from .adapters.base import Event
    ingest(db, a, Event(str(uuid4()), c.peer, body.text))
    db.commit()
    return {"ok": True}


@app.get("/webhooks/whatsapp_cloud/{account_id}")
def verify_whatsapp(account_id: str, request: Request, db: Session = Depends(session_dependency)):
    a = db.get(Account, account_id)
    q = request.query_params
    if not a or a.channel != "whatsapp_cloud":
        raise HTTPException(404)
    expected = unseal(a.encrypted_config).get("verify_token", "")
    if q.get("hub.mode") != "subscribe" or not expected or not hmac.compare_digest(q.get("hub.verify_token", ""), expected):
        raise HTTPException(403)
    return PlainTextResponse(q.get("hub.challenge", ""))


@app.post("/webhooks/{channel}/{account_id}")
async def webhook(channel: str, account_id: str, request: Request):
    if channel not in {"whatsapp_cloud", "max_bot", "sms_gateway"}:
        raise HTTPException(404)
    # Bounded streaming read; never trust Content-Length alone.
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > 1024 * 1024:
            raise HTTPException(413)
    return await run_in_threadpool(store_webhook, channel, account_id, bytes(raw), dict(request.headers))


def store_webhook(channel, account_id, raw, headers):
    from .db import Session as DbSession
    with DbSession() as db:
        return _store_webhook(db, channel, account_id, raw, headers)


def _store_webhook(db, channel, account_id, raw, headers):
    a = db.scalar(select(Account).where(Account.id == account_id).with_for_update())
    if not a or a.channel != channel:
        raise HTTPException(404)
    config = unseal(a.encrypted_config)
    from .adapters.official import verify_webhook, normalize_whatsapp, normalize_max, normalize_sms
    if not verify_webhook(channel, bytes(raw), headers, config):
        raise HTTPException(401, "invalid_webhook_signature")
    if a.state != "connected":
        return {"ok": True, "ignored": "account_disconnected"}
    digest = hashlib.sha256(raw).hexdigest()
    if db.scalar(select(WebhookReceipt).where(WebhookReceipt.account_id == a.id, WebhookReceipt.digest == digest)):
        return {"ok": True}
    try:
        body = json.loads(raw)
        events = {"whatsapp_cloud": normalize_whatsapp, "max_bot": normalize_max, "sms_gateway": normalize_sms}[channel](body)
    except (ValueError, TypeError, KeyError, AttributeError):
        raise HTTPException(400, "invalid_webhook_payload") from None
    for event in events:
        # Provider apps/gateways can authenticate a batch for several numbers.
        # Never infer device/number ownership from an untrusted webhook URL.
        if channel == "whatsapp_cloud" and event.metadata.get("phone_number_id") != str(config.get("phone_number_id")):
            continue
        if channel == "sms_gateway":
            if event.metadata.get("device_id") != str(config.get("device_id")):
                continue
            if event.metadata.get("sim_number") is not None and event.metadata["sim_number"] != config.get("sim_number", 1):
                continue
        if channel == "max_bot" and event.metadata.get("bot_id") and event.metadata["bot_id"] != str(config.get("bot_id")):
            continue
        ingest(db, a, event)
    db.add(WebhookReceipt(account_id=a.id, digest=digest))
    db.commit()
    return {"ok": True}


web = Path(__file__).resolve().parent.parent / "web"
app.mount("/static", StaticFiles(directory=web), name="static")


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(web / "index.html")
