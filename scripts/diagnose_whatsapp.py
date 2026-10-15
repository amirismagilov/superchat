#!/usr/bin/env python3
"""Read-only, allowlisted WhatsApp diagnosis; run via docker compose exec -T api python -.

Never print raw database/provider records, URLs, request exceptions, QR values,
phone numbers, labels, session IDs, credentials, or encrypted fields.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

MAX_ACCOUNTS = 10
MAX_JOBS = 3
MAX_QR_CHECKS = 3
MAX_RESPONSE_BYTES = 1024 * 1024
ACCOUNT_STATES = frozenset({
    "disconnected", "connecting", "qr_required", "awaiting_qr", "connected",
    "reauth_required", "action_required", "code_required", "password_required",
})
PROVIDER_STATES = frozenset({
    "STOPPED", "STARTING", "SCAN_QR_CODE", "PASSKEY_REQUIRED",
    "PASSKEY_CONFIRMATION_REQUIRED", "WORKING", "FAILED",
})
JOB_ACTIONS = frozenset({"login", "sync", "disconnect"})
JOB_STATES = frozenset({"queued", "running", "done", "failed"})
ERROR_CODES = frozenset({
    "experimental_adapter_disabled", "invalid_waha_session", "unsupported_action",
    "waha_connect_timeout", "waha_deployment_api_key_required", "waha_deployment_url_required",
    "waha_invalid_chats_response", "waha_invalid_history_response", "waha_invalid_lid_response",
    "waha_invalid_message", "waha_invalid_message_key", "waha_invalid_qr_response",
    "waha_invalid_response", "waha_missing_message_id", "waha_rate_limited",
    "waha_request_rejected", "waha_server_error", "waha_session_failed", "waha_session_stopped",
    "waha_transport_error", "waha_session_expired", "whatsapp_account_identity_changed",
    "provider_auth_error", "provider_result_unknown", "provider_unauthorized",
    "auth_job_expired", "worker_interrupted_retry_login", "worker_interrupted_during_send",
    "worker_fencing_epoch_changed", "worker_fencing_owner_required", "worker_ownership_connection_lost",
    "worker_ownership_lost", "worker_shard_not_owned", "account_disconnected",
    "TimeoutError", "ConnectTimeout", "ReadTimeout", "ConnectionError",
})


def allowed(value, choices, fallback="unknown"):
    return value if isinstance(value, str) and value in choices else fallback


def public_error(value):
    return None if value is None else allowed(value, ERROR_CODES, "unknown_error")


def age_seconds(value, current):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((current - value).total_seconds()))


def account_summary(account, jobs, ordinal, unseal, current):
    """Return public allowlist and an INTERNAL session ID, never serialize the latter."""
    public = account.public if isinstance(account.public, dict) else {}
    result = {
        "ordinal": ordinal,
        "account_hash": hashlib.sha256(str(account.id).encode()).hexdigest()[:12],
        "state": allowed(account.state, ACCOUNT_STATES),
        "updated_age_seconds": age_seconds(account.updated_at, current),
        "public_error": public_error(public.get("error")),
        "provider_state": allowed(public.get("provider_state"), PROVIDER_STATES, None),
        "public_qr_present": any(isinstance(public.get(key), str) and bool(public[key]) for key in ("qr_text", "qr_data_url")),
        "jobs": [{
            "action": allowed(job.action, JOB_ACTIONS),
            "status": allowed(job.status, JOB_STATES),
            "error": public_error(job.error),
            "age_seconds": age_seconds(job.created_at, current),
        } for job in jobs[:MAX_JOBS]],
    }
    session_id = None
    try:
        config = unseal(account.encrypted_config)
        value = config.get("_waha_session") if isinstance(config, dict) else None
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
            session_id = value
        result["session_reference"] = "present" if session_id else "missing_or_invalid"
    except Exception:
        result["session_reference"] = "decrypt_failed"
    return result, session_id


def get_json(client, path, params=None):
    """Bounded GET only. Response bodies and exception text never leave this helper."""
    http_status = None
    try:
        with client.stream("GET", path, params=params) as response:
            http_status = response.status_code
            if not isinstance(http_status, int) or not 100 <= http_status <= 599:
                return None, None, "invalid_response"
            if not 200 <= http_status < 300:
                return None, http_status, "http_error"
            parts = []
            size = 0
            for part in response.iter_bytes():
                size += len(part)
                if size > MAX_RESPONSE_BYTES:
                    return None, http_status, "response_too_large"
                parts.append(part)
            try:
                payload = json.loads(b"".join(parts))
            except (ValueError, UnicodeError):
                return None, http_status, "invalid_json"
            return payload, http_status, "ok"
    except Exception:
        return None, http_status, "request_failed"


def gateway_summary(client, account_pairs):
    """Inspect only existing sessions and at most three existing QR challenges."""
    records, http_status, status = get_json(client, "/api/sessions", {"all": "true"})
    result = {"status": status, "http_status": http_status}
    if status == "ok" and not isinstance(records, list):
        result["status"] = "invalid_response"
        records = None
    qr_checks = 0
    for summary, session_id in account_pairs:
        account_gateway = {"sessions_http_status": http_status, "status": result["status"], "provider_state": None, "qr_present": None}
        summary["gateway"] = account_gateway
        if result["status"] != "ok":
            continue
        if session_id is None:
            account_gateway["status"] = "session_reference_unavailable"
            continue
        matches = [row for row in records if isinstance(row, dict) and row.get("name") == session_id]
        if not matches:
            account_gateway["status"] = "session_not_found"
            continue
        if len(matches) != 1:
            account_gateway["status"] = "ambiguous_session"
            continue
        state = allowed(matches[0].get("status"), PROVIDER_STATES)
        account_gateway["provider_state"] = state
        account_gateway["status"] = "session_found"
        if state != "SCAN_QR_CODE":
            continue
        if qr_checks >= MAX_QR_CHECKS:
            account_gateway["qr_check"] = "limit_reached"
            continue
        qr_checks += 1
        qr, qr_http_status, qr_status = get_json(client, "/api/" + quote(session_id, safe="") + "/auth/qr", {"format": "raw"})
        account_gateway["qr_http_status"] = qr_http_status
        account_gateway["qr_check"] = qr_status
        if qr_status == "ok":
            account_gateway["qr_present"] = isinstance(qr, dict) and isinstance(qr.get("value"), str) and bool(qr["value"])
    result["qr_checks"] = qr_checks
    return result


def diagnose():
    # Standalone stdin execution does not put /app on sys.path automatically.
    sys.path.insert(0, "/app")
    import httpx
    from sqlalchemy import func, select, text
    from app.db import Account, Job, Session
    from app.security import unseal

    report = {"diagnostic": "whatsapp_readonly_v1", "result": "ok", "accounts": []}
    pairs = []
    current = datetime.now(timezone.utc)
    try:
        with Session() as db:
            if db.get_bind().dialect.name == "postgresql":
                db.execute(text("SET TRANSACTION READ ONLY"))
                db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
            total = db.scalar(select(func.count()).select_from(Account).where(Account.channel == "whatsapp_web"))
            accounts = db.scalars(select(Account).where(Account.channel == "whatsapp_web").order_by(Account.updated_at.desc(), Account.id).limit(MAX_ACCOUNTS)).all()
            report["accounts_total"] = total
            report["accounts_truncated"] = total > MAX_ACCOUNTS
            for ordinal, account in enumerate(accounts, 1):
                jobs = db.scalars(select(Job).where(Job.account_id == account.id).order_by(Job.created_at.desc(), Job.id).limit(MAX_JOBS)).all()
                pairs.append(account_summary(account, jobs, ordinal, unseal, current))
            # No flush, commit, migration, metadata create or provider auth call.
            db.rollback()
    except Exception:
        return {"diagnostic": "whatsapp_readonly_v1", "result": "database_read_failed", "accounts": []}
    report["accounts"] = [summary for summary, _session in pairs]
    endpoint = os.environ.get("WAHA_URL", "").rstrip("/")
    api_key = os.environ.get("WAHA_API_KEY", "")
    try:
        parsed = urlsplit(endpoint)
        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))
    except ValueError:
        valid = False
    if not valid or not api_key:
        report["gateway"] = {"status": "deployment_config_missing_or_invalid", "http_status": None}
        return report
    try:
        with httpx.Client(base_url=endpoint, headers={"X-Api-Key": api_key, "Accept": "application/json"}, timeout=httpx.Timeout(5, connect=3), follow_redirects=False) as client:
            report["gateway"] = gateway_summary(client, pairs)
    except Exception:
        report["gateway"] = {"status": "client_initialization_failed", "http_status": None}
    return report


def main():
    # Third-party debug logging must not emit request URLs/session identifiers.
    logging.disable(logging.CRITICAL)
    try:
        report = diagnose()
    except Exception:
        report = {"diagnostic": "whatsapp_readonly_v1", "result": "diagnostic_unavailable", "accounts": []}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get("result") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
