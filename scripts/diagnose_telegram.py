#!/usr/bin/env python3
"""Read-only Telegram account/job diagnosis, without provider connections or secrets.

Stream into a running api or worker container with `python -`. The worker
container can additionally see its private heartbeat; absence in api is normal.
Only allowlisted codes, booleans, relative ages and hashed account IDs are output.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import sys

MAX_ACCOUNTS = 10
MAX_JOBS = 3
ACCOUNT_STATES = frozenset({
    'disconnected', 'connecting', 'connected', 'reauth_required', 'action_required',
    'awaiting_code', 'awaiting_password', 'awaiting_qr', 'code_required',
    'password_required', 'qr_required',
})
JOB_ACTIONS = frozenset({'login', 'code', 'password', 'qr', 'sync', 'disconnect'})
JOB_STATES = frozenset({'queued', 'running', 'done', 'failed'})
ERROR_CODES = frozenset({
    'telegram_flood_wait', 'telegram_session_expired', 'telegram_auth_key_unavailable',
    'telegram_code_invalid', 'telegram_code_expired', 'telegram_password_invalid',
    'telegram_phone_invalid', 'telegram_phone_banned', 'telegram_configuration_invalid',
    'telegram_app_not_configured', 'telegram_qr_expired', 'telegram_qr_invalid',
    'telegram_account_restricted', 'telegram_nonce_already_used',
    'telegram_transport_error', 'telegram_server_error', 'telegram_request_rejected',
    'telegram_user_account_required', 'telegram_account_identity_changed',
    'telegram_logout_failed', 'telegram_login_required', 'telegram_password_required',
    'telegram_sync_limit_invalid', 'telegram_peer_unknown_use_username_or_sync',
    'telegram_personal_peer_required', 'telegram_peer_invalid',
    'telegram_phone_lookup_rate_limited', 'telegram_phone_not_found_or_private',
    'telegram_peer_not_found', 'telegram_peer_identity_changed', 'telegram_text_limit',
    'telegram_send_result_unknown', 'telegram_history_gap_sync_required',
    'telegram_difference_unknown', 'telegram_auth_required',
    'provider_auth_error', 'provider_auth_timeout', 'provider_result_unknown', 'provider_unauthorized',
    'auth_job_expired', 'worker_interrupted_retry_login', 'worker_interrupted_during_send',
    'worker_fencing_epoch_changed', 'worker_fencing_owner_required',
    'worker_ownership_connection_lost', 'worker_ownership_lost', 'worker_shard_not_owned',
    'account_disconnected', 'unsupported_action', 'experimental_adapter_disabled',
    'TimeoutError', 'ConnectTimeout', 'ReadTimeout', 'ConnectionError',
})


def allowed(value, choices, fallback='unknown'):
    return value if isinstance(value, str) and value in choices else fallback


def safe_error(value):
    return None if value is None else allowed(value, ERROR_CODES, 'unknown_error')


def age_seconds(value, current):
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return max(0, int((current - value).total_seconds()))


def account_summary(account, jobs, ordinal, shared_configuration_exists, current):
    public = account.public if isinstance(account.public, dict) else {}
    return {
        'ordinal': ordinal,
        'account_hash': hashlib.sha256(str(account.id).encode()).hexdigest()[:12],
        'state': allowed(account.state, ACCOUNT_STATES),
        'public_error': safe_error(public.get('error')),
        'updated_age_seconds': age_seconds(account.updated_at, current),
        'shared_configuration_exists': bool(shared_configuration_exists),
        'jobs': [{
            'action': allowed(job.action, JOB_ACTIONS),
            'status': allowed(job.status, JOB_STATES),
            'error': safe_error(job.error),
            'age_seconds': age_seconds(job.created_at, current),
        } for job in jobs[:MAX_JOBS]],
    }


def environment_summary(environment):
    # These are presence checks only. Application API ID/hash live in encrypted DB rows.
    return {
        'database_url_present': bool(environment.get('DATABASE_URL')),
        'encryption_key_present': bool(environment.get('ENCRYPTION_KEY')),
        'worker_heartbeat_path_present': bool(environment.get('WORKER_HEARTBEAT_PATH')),
    }


def heartbeat_summary(current, environment):
    result = {'status': 'not_visible_in_this_process', 'age_seconds': None, 'fresh': None}
    try:
        path = Path(environment.get('WORKER_HEARTBEAT_PATH') or '/tmp/omnicrm-worker.heartbeat')
        info = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return {**result, 'status': 'not_regular_file'}
        age = current.timestamp() - info.st_mtime
        if age < 0:
            return {**result, 'status': 'future_timestamp', 'fresh': False}
        return {'status': 'visible', 'age_seconds': int(age), 'fresh': age < 120}
    except FileNotFoundError:
        return result
    except Exception:
        return {**result, 'status': 'unavailable'}


def read_accounts(db, current):
    """Select only diagnostic fields; never load/decrypt account or job credentials."""
    from sqlalchemy import func, select, text
    from app.db import Account, IntegrationSetting, Job

    if db.get_bind().dialect.name == 'postgresql':
        db.execute(text('SET TRANSACTION READ ONLY'))
        db.execute(text("SET LOCAL statement_timeout = '5000ms'"))
    total = db.scalar(select(func.count()).select_from(Account).where(Account.channel == 'telegram'))
    shared_exists = bool(db.scalar(select(IntegrationSetting.tenant_id).where(IntegrationSetting.channel == 'telegram').limit(1)))
    accounts = db.execute(select(Account.id, Account.tenant_id, Account.state, Account.public, Account.updated_at)
                          .where(Account.channel == 'telegram')
                          .order_by(Account.updated_at.desc(), Account.id).limit(MAX_ACCOUNTS)).all()
    summaries = []
    for ordinal, account in enumerate(accounts, 1):
        jobs = db.execute(select(Job.action, Job.status, Job.error, Job.created_at)
                          .where(Job.account_id == account.id)
                          .order_by(Job.created_at.desc(), Job.id).limit(MAX_JOBS)).all()
        account_shared_exists = bool(db.scalar(select(IntegrationSetting.tenant_id)
                                              .where(IntegrationSetting.channel == 'telegram', IntegrationSetting.tenant_id == account.tenant_id).limit(1)))
        summaries.append(account_summary(account, jobs, ordinal, account_shared_exists, current))
    db.rollback()
    return {'accounts_total': total, 'accounts_truncated': total > MAX_ACCOUNTS,
            'shared_configuration_exists': shared_exists, 'accounts': summaries}


def diagnose():
    current = datetime.now(timezone.utc)
    report = {'diagnostic': 'telegram_readonly_v1', 'result': 'ok',
              'environment': environment_summary(os.environ),
              'worker_heartbeat': heartbeat_summary(current, os.environ), 'accounts': []}
    # Never let app.db fall back to creating a default SQLite file on a misconfigured host.
    database_url = os.environ.get('DATABASE_URL', '')
    if not database_url:
        return {**report, 'result': 'database_not_configured'}
    if database_url.split(':', 1)[0] not in {'postgresql', 'postgresql+psycopg', 'postgresql+psycopg2'}:
        return {**report, 'result': 'database_backend_not_supported'}
    sys.path.insert(0, '/app')
    from app.db import Session

    try:
        with Session() as db:
            report.update(read_accounts(db, current))
    except Exception:
        report['result'] = 'database_read_failed'
    return report


def main():
    logging.disable(logging.CRITICAL)
    try:
        report = diagnose()
    except Exception:
        report = {'diagnostic': 'telegram_readonly_v1', 'result': 'diagnostic_unavailable', 'accounts': []}
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report.get('result') == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
