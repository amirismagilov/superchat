#!/usr/bin/env python3
"""Read-only, bounded WhatsApp pairing timeline; standalone host/stdin script."""
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import selectors
import signal
import subprocess
import time
from datetime import datetime, timezone

ROOT = Path('/opt/omnicrm-pilot')
AUTO = Path('/opt/omnicrm-pilot-autoupdate/state.json')
MAX_LOG_BYTES = 2 * 1024 * 1024
MAX_LOG_LINES = 5000
MAX_EVENTS = 60
MAX_PRETTY_RECORD_CHARS = 65536
MAX_PRETTY_CONTINUATIONS = 64
AUTO_STATES = {'checking','up_to_date','packaging','deploying','deployed','previous_attempt_uncertain','retry_required','non_fast_forward_blocked','main_changed_deferred','ci_changed_deferred','ci_missing','ci_pending','ci_failed','ci_attempt_changed','ci_latest_attempt_not_successful','ci_identity_mismatch','ci_repository_mismatch','ci_invalid_response','github_api_unavailable','git_command_failed','git_head_invalid','git_fetched_sha_mismatch','ancestry_unavailable','release_packaging_failed','release_checksum_mismatch','pilot_update_failed','command_unavailable_or_timeout','unexpected_failure'}
STATES = {'disconnected','connecting','qr_required','awaiting_qr','connected','reauth_required','action_required','code_required','password_required'}
PROVIDER_STATES = {'STOPPED','STARTING','SCAN_QR_CODE','PASSKEY_REQUIRED','PASSKEY_CONFIRMATION_REQUIRED','WORKING','FAILED'}
GATEWAY_STATES = {'ok','invalid_response','http_error','response_too_large','invalid_json','request_failed','deployment_config_missing_or_invalid','client_initialization_failed','session_reference_unavailable','session_not_found','ambiguous_session','session_found','limit_reached'}
ERRORS = {'ENOTFOUND','EAI_AGAIN','ETIMEDOUT','ECONNRESET','ECONNREFUSED','EHOSTUNREACH','ENETUNREACH','EPIPE','EACCES','EPERM','EROFS','ENOSPC','CERT_HAS_EXPIRED','UNABLE_TO_VERIFY_LEAF_SIGNATURE','SELF_SIGNED_CERT_IN_CHAIN','DEPTH_ZERO_SELF_SIGNED_CERT','ERR_TLS_CERT_ALTNAME_INVALID','UNABLE_TO_GET_ISSUER_CERT_LOCALLY','ERR_TLS_HANDSHAKE_TIMEOUT','SQLITE_BUSY','SQLITE_CANTOPEN'}
PUBLIC_ERRORS = {'experimental_adapter_disabled','invalid_waha_session','unsupported_action','waha_connect_timeout','waha_deployment_api_key_required','waha_deployment_url_required','waha_invalid_chats_response','waha_invalid_history_response','waha_invalid_lid_response','waha_invalid_message','waha_invalid_message_key','waha_invalid_qr_response','waha_invalid_response','waha_missing_message_id','waha_rate_limited','waha_request_rejected','waha_server_error','waha_session_failed','waha_session_stopped','waha_transport_error','waha_session_expired','whatsapp_account_identity_changed','provider_auth_error','provider_auth_timeout','provider_result_unknown','provider_unauthorized','auth_job_expired','worker_interrupted_retry_login','worker_interrupted_during_send','worker_fencing_epoch_changed','worker_fencing_owner_required','worker_ownership_connection_lost','worker_ownership_lost','worker_shard_not_owned','account_disconnected','TimeoutError','ConnectTimeout','ReadTimeout','ConnectionError'}
MARKERS = {
    'client_start_failed': r'Failed to start the client',
    'socket_initialization': r'Connecting using wa\.version',
    'registration_attempt': r'not logged in, attempting registration',
    'connection_validation_failed': r'error in validating connection',
    'connection_error': r'connection errored',
    'connection_closed': r'connection closed',
    'connection_opened': r'opened connection to WA',
    'starting_watchdog': r'Session stuck in STARTING status',
    'qr_watchdog': r'QR code has not been scanned yet',
    'reconnecting': r'Connection closed due to .*reconnecting',
    'pairing_configured': r'pairing configured successfully',
    'pairing_success': r'pair success recv',
    'pairing_failed': r'failed to pair|pairing failed|error in pairing',
    'socket_connected': r'(?<!opened )connected to WA',
    'login_attempt': r'logging in\.\.\.',
    'stream_error': r'stream errored out',
    'qr_refs_exhausted': r'QR refs attempts ended',
    'prekeys_upload_failed': r'Failed to upload pre-keys to server|Failed to check/upload pre-keys during initialization',
    'account_signature_invalid': r'Invalid account signature|Failed to verify account signature',
    'pairing_identity_missing': r'Missing device-identity or device in pair success node',
    'credentials_save_before_stop': r'Saving creds before stopping',
    'credentials_saved_during_stop': r'Creds saved',
    'credentials_file_repair_failed': r'Failed to fix [\x22\x27]?creds\.json',
    'auth_store_close_failed': r'Failed to close NOWEB auth store',
}


def enum(value, choices, fallback='unknown'):
    return value if isinstance(value, str) and value in choices else fallback


def number(value, maximum=10**12):
    return value if type(value) is int and 0 <= value <= maximum else None


def session_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if isinstance(value, str) and re.fullmatch('[A-Za-z0-9_-]{1,128}', value) else None


def timestamp(value):
    try:
        if type(value) in (int, float) and 1577836800000 <= value <= 4102444800000:
            date = datetime.fromtimestamp(value / 1000, timezone.utc)
        elif isinstance(value, str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?(?:Z|[+-]\d{2}:\d{2})', value):
            date = datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone(timezone.utc)
        else:return None
        return date.isoformat(timespec='milliseconds').replace('+00:00','Z')
    except Exception:return None


class Runner:
    def __init__(self):
        self.deadline = time.monotonic() + 80
        self.env = dict(os.environ)
        for key in ('LOCAL_PORT','DATABASE_URL','ENCRYPTION_KEY','JWT_SECRET','WAHA_API_KEY','POSTGRES_PASSWORD','SMS_GATEWAY_URL','WAHA_IMAGE'):
            self.env.pop(key, None)

    def capture(self, args, timeout=5, maximum=16384, tail=False):
        deadline = min(self.deadline, time.monotonic()+timeout)
        if deadline <= time.monotonic():return 'packet_timeout', b'', False
        process = None; data = bytearray(); truncated = False
        selector = selectors.DefaultSelector()
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=self.env)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                wait = deadline-time.monotonic()
                if wait <= 0 or not selector.select(wait):return 'timeout', bytes(data), True
                chunk = os.read(process.stdout.fileno(),65536)
                if not chunk:
                    selector.unregister(process.stdout);break
                data.extend(chunk)
                if len(data)>maximum:
                    truncated=True
                    if not tail:return 'output_limit', b'', True
                    del data[:-maximum]
            code=process.wait(timeout=max(.01,deadline-time.monotonic()))
            return ('ok' if code==0 else 'command_failed'), bytes(data), truncated
        except subprocess.TimeoutExpired:return 'timeout', bytes(data), True
        except Exception:return 'unavailable', b'', truncated
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:process.kill()
                try:process.wait(timeout=1)
                except Exception:pass
                if process.stdout:process.stdout.close()

    def compose(self,*args,**kwargs):
        return self.capture(['docker','compose','--project-name','omnicrm-pilot','--env-file',str(ROOT/'.env.pilot'),'-f',str(ROOT/'compose.pilot.yaml'),*args],**kwargs)


def auto_state():
    try:
        if AUTO.is_symlink() or AUTO.stat().st_size>32768:raise ValueError
        data=json.loads(AUTO.read_bytes());result={'status':enum(data.get('status'),AUTO_STATES)}
        for key in ('deployed_sha','candidate_sha'):
            if isinstance(data.get(key),str) and re.fullmatch('[a-f0-9]{40}',data[key]):result[key]=data[key]
        return result
    except Exception:return {'status':'state_unavailable'}


def containers(runner):
    status,raw,_=runner.compose('ps','--all','--quiet',maximum=4096)
    ids=raw.decode('ascii',errors='ignore').split()
    if status!='ok' or not ids or any(not re.fullmatch('[a-f0-9]{12,64}',x) for x in ids):return {'status':status if status!='ok' else 'no_valid_containers'}
    target='NODE_OPTIONS=--max-old-space-size=16384 --dns-result-order=ipv4first --no-network-family-autoselection'
    template='{{$fixed := false}}{{$legacy := false}}{{range .Config.Env}}{{if eq . "'+target+'"}}{{$fixed = true}}{{end}}{{if eq . "NODE_OPTIONS=--max-old-space-size=16384"}}{{$legacy = true}}{{end}}{{end}}{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}},"restarts":{{.RestartCount}},"oom":{{.State.OOMKilled}},"ipv4_options_exact":{{$fixed}},"legacy_options_exact":{{$legacy}}'
    status,raw,_=runner.capture(['docker','inspect','--format',template,*ids[:12]])
    result={'status':status,'services':[],'truncated':len(ids)>12}
    if status=='ok':
        for line in raw.splitlines():
            try:
                item=json.loads(line)
                if item.get('service') not in {'api','worker','waha','postgres'}:continue
                clean={'service':item['service'],'state':enum(item.get('state'),{'running','created','paused','restarting','removing','exited','dead'}),'health':enum(item.get('health'),{'healthy','unhealthy','starting','none'}),'restart_count':number(item.get('restarts')),'oom_killed':item.get('oom') if type(item.get('oom')) is bool else None}
                if item['service']=='waha':
                    for key in ('ipv4_options_exact','legacy_options_exact'):clean[key]=item.get(key) if type(item.get(key)) is bool else None
                result['services'].append(clean)
            except Exception:result['status']='invalid_response'
    return result


def log_records(lines):
    """Keep pino-pretty multiline error details with their own header only.

    Docker adds an RFC3339 prefix to every physical line. After removing that
    prefix, pino error continuations are indented even with singleLine=true.
    Every new unindented record ends the previous context, including unrelated
    messages, so an error cannot inherit another session's identity.
    """
    records=[];current=None;pretty=False;continuations=0;truncated=False
    for line in lines:
        line=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',line)
        prefix,_,body=line.partition(' ')
        if not timestamp(prefix):body=line
        if body[:1] in {' ', '\t'} and current is not None and pretty:
            continuations+=1
            if continuations<=MAX_PRETTY_CONTINUATIONS and len(current)+1+len(body)<=MAX_PRETTY_RECORD_CHARS:
                current+='\n'+body
            else:truncated=True
            continue
        if current is not None:records.append(current)
        current=line;continuations=0
        # JSON records already contain their own fields. Only a real pretty
        # header may acquire indented error detail lines.
        pretty=(not body[:1].isspace() and bool(re.match(
            r'^\[[^\]\r\n]{1,64}\]\s+(?:TRACE|DEBUG|INFO|WARN|ERROR|FATAL)\b',body)))
        if pretty and len(current)>MAX_PRETTY_RECORD_CHARS:
            current=current[:MAX_PRETTY_RECORD_CHARS];truncated=True
    if current is not None:records.append(current)
    return records,truncated


def timeline(raw, capture_truncated=False):
    if len(raw)>MAX_LOG_BYTES:raw=raw[-MAX_LOG_BYTES:];capture_truncated=True
    lines=raw.decode('utf-8',errors='replace').splitlines()
    if capture_truncated and lines:lines=lines[1:]
    truncated=capture_truncated or len(lines)>=MAX_LOG_LINES
    lines=lines[-MAX_LOG_LINES:];events=[];summaries={};unparsed=0
    records,continuations_truncated=log_records(lines)
    for line in records:
        line=re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]','',line)
        prefix,_,body=line.partition(' ');stamp=timestamp(prefix)
        if not stamp:body=line
        try:item=json.loads(body)
        except Exception:item={}
        if not isinstance(item,dict):item={}
        message=item.get('msg',item.get('message',body))
        if not isinstance(message,str):continue
        found=[key for key,pattern in MARKERS.items() if re.search(pattern,message if key not in {'qr_refs_exhausted','account_signature_invalid','pairing_identity_missing'} else line,re.I)]
        if 'Job scheduled with timeout 2000 ms' in message and (item.get('job')=='start-engine' or re.search(r'\bjob[\s\x22\x27:=]+start-engine\b',line)):
            found.append('engine_restart_scheduled')
        errors=sorted(code for code in ERRORS if re.search(r'\b'+re.escape(code)+r'\b',line))
        if not found:
            if not errors:continue
            found=['transport_error']
        stamp=stamp or timestamp(item.get('time')) or timestamp(item.get('timestamp'))
        if not stamp:unparsed+=1
        session=item.get('session')
        if isinstance(session,dict):session=session.get('name')
        if session is None:
            match=re.search(r'(?<![A-Za-z0-9_])session:([A-Za-z0-9_-]{1,128})(?=\s|$)',message)
            session=match[1] if match else None
        hashed=session_hash(session)
        codes=sorted({int(value) for value in re.findall(r'\b(?:statusCode|status|code)[\s\x22\x27:=]+([1-5][0-9]{2})\b',line)})[:5]
        if isinstance(item.get('fullErrorNode'),dict):
            attrs=item['fullErrorNode'].get('attrs',{})
            code=attrs.get('code') if isinstance(attrs,dict) else None
            if str(code) in {'401','403','408','411','428','440','500','503','515'}:codes=sorted(set(codes+[int(code)]))
        for marker in found:
            event={'timestamp_utc':stamp,'event':marker,'session_hash':hashed,'error_codes':errors[:5],'status_codes':codes}
            if 515 in codes:event['restart_required']=True
            events.append(event)
            key=hashed or 'unattributed'
            summary=summaries.setdefault(key,{'session_hash':hashed,'event_count':0,'first_event_utc':stamp,'latest_event_utc':stamp,'event_counts':{}})
            summary['event_count']+=1;summary['event_counts'][marker]=summary['event_counts'].get(marker,0)+1
            if stamp:
                summary['first_event_utc']=min(summary['first_event_utc'] or stamp,stamp)
                summary['latest_event_utc']=max(summary['latest_event_utc'] or stamp,stamp)
    events.sort(key=lambda e:e['timestamp_utc'] or '')
    return {'window_minutes':60,'lines_examined':len(lines),'capture_truncated':truncated,'continuations_truncated':continuations_truncated,'event_count':len(events),'events_truncated':len(events)>MAX_EVENTS,'events_without_timestamp':unparsed,'session_summaries':list(summaries.values())[:100],'session_summaries_truncated':len(summaries)>100,'events':events[-MAX_EVENTS:]}


ACCOUNT_PROBE = '#!/usr/bin/env python3\n"""Read-only, allowlisted WhatsApp diagnosis; run via docker compose exec -T api python -.\n\nNever print raw database/provider records, URLs, request exceptions, QR values,\nphone numbers, labels, session IDs, credentials, or encrypted fields.\n"""\nfrom __future__ import annotations\n\nimport hashlib\nimport signal\nimport json\nimport logging\nimport os\nimport re\nimport sys\nfrom datetime import datetime, timezone\nfrom urllib.parse import quote, urlsplit\n\nMAX_ACCOUNTS = 10\nMAX_JOBS = 3\nMAX_QR_CHECKS = 3\nMAX_RESPONSE_BYTES = 1024 * 1024\nACCOUNT_STATES = frozenset({\n    "disconnected", "connecting", "qr_required", "awaiting_qr", "connected",\n    "reauth_required", "action_required", "code_required", "password_required",\n})\nPROVIDER_STATES = frozenset({\n    "STOPPED", "STARTING", "SCAN_QR_CODE", "PASSKEY_REQUIRED",\n    "PASSKEY_CONFIRMATION_REQUIRED", "WORKING", "FAILED",\n})\nJOB_ACTIONS = frozenset({"login", "sync", "disconnect"})\nJOB_STATES = frozenset({"queued", "running", "done", "failed"})\nERROR_CODES = frozenset({\n    "experimental_adapter_disabled", "invalid_waha_session", "unsupported_action",\n    "waha_connect_timeout", "waha_deployment_api_key_required", "waha_deployment_url_required",\n    "waha_invalid_chats_response", "waha_invalid_history_response", "waha_invalid_lid_response",\n    "waha_invalid_message", "waha_invalid_message_key", "waha_invalid_qr_response",\n    "waha_invalid_response", "waha_missing_message_id", "waha_rate_limited",\n    "waha_request_rejected", "waha_server_error", "waha_session_failed", "waha_session_stopped",\n    "waha_transport_error", "waha_session_expired", "whatsapp_account_identity_changed",\n    "provider_auth_error", "provider_auth_timeout", "provider_result_unknown", "provider_unauthorized",\n    "auth_job_expired", "worker_interrupted_retry_login", "worker_interrupted_during_send",\n    "worker_fencing_epoch_changed", "worker_fencing_owner_required", "worker_ownership_connection_lost",\n    "worker_ownership_lost", "worker_shard_not_owned", "account_disconnected",\n    "TimeoutError", "ConnectTimeout", "ReadTimeout", "ConnectionError",\n})\n\n\ndef allowed(value, choices, fallback="unknown"):\n    return value if isinstance(value, str) and value in choices else fallback\n\n\ndef public_error(value):\n    return None if value is None else allowed(value, ERROR_CODES, "unknown_error")\n\n\ndef age_seconds(value, current):\n    if not isinstance(value, datetime):\n        return None\n    if value.tzinfo is None:\n        value = value.replace(tzinfo=timezone.utc)\n    return max(0, int((current - value).total_seconds()))\n\n\ndef account_summary(account, jobs, ordinal, unseal, current):\n    """Return public allowlist and an INTERNAL session ID, never serialize the latter."""\n    public = account.public if isinstance(account.public, dict) else {}\n    result = {\n        "ordinal": ordinal,\n        "account_hash": hashlib.sha256(str(account.id).encode()).hexdigest()[:12],\n        "state": allowed(account.state, ACCOUNT_STATES),\n        "updated_age_seconds": age_seconds(account.updated_at, current),\n        "public_error": public_error(public.get("error")),\n        "status_check_error": public_error(public.get("status_check_error")),\n        "provider_state": allowed(public.get("provider_state"), PROVIDER_STATES, None),\n        "public_qr_present": any(isinstance(public.get(key), str) and bool(public[key]) for key in ("qr_text", "qr_data_url")),\n        "jobs": [{\n            "action": allowed(job.action, JOB_ACTIONS),\n            "status": allowed(job.status, JOB_STATES),\n            "error": public_error(job.error),\n            "age_seconds": age_seconds(job.created_at, current),\n        } for job in jobs[:MAX_JOBS]],\n    }\n    session_id = None\n    try:\n        config = unseal(account.encrypted_config)\n        value = config.get("_waha_session") if isinstance(config, dict) else None\n        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):\n            session_id = value\n        result["session_reference"] = "present" if session_id else "missing_or_invalid"\n    except Exception:\n        result["session_reference"] = "decrypt_failed"\n    result["session_hash"] = hashlib.sha256(session_id.encode()).hexdigest()[:12] if session_id else None\n    return result, session_id\n\n\ndef get_json(client, path, params=None):\n    """Bounded GET only. Response bodies and exception text never leave this helper."""\n    http_status = None\n    try:\n        with client.stream("GET", path, params=params) as response:\n            http_status = response.status_code\n            if not isinstance(http_status, int) or not 100 <= http_status <= 599:\n                return None, None, "invalid_response"\n            if not 200 <= http_status < 300:\n                return None, http_status, "http_error"\n            parts = []\n            size = 0\n            for part in response.iter_bytes():\n                size += len(part)\n                if size > MAX_RESPONSE_BYTES:\n                    return None, http_status, "response_too_large"\n                parts.append(part)\n            try:\n                payload = json.loads(b"".join(parts))\n            except (ValueError, UnicodeError):\n                return None, http_status, "invalid_json"\n            return payload, http_status, "ok"\n    except Exception:\n        return None, http_status, "request_failed"\n\n\ndef gateway_summary(client, account_pairs):\n    """Inspect only existing sessions and at most three existing QR challenges."""\n    records, http_status, status = get_json(client, "/api/sessions", {"all": "true"})\n    result = {"status": status, "http_status": http_status}\n    if status == "ok" and not isinstance(records, list):\n        result["status"] = "invalid_response"\n        records = None\n    qr_checks = 0\n    for summary, session_id in account_pairs:\n        account_gateway = {"sessions_http_status": http_status, "status": result["status"], "provider_state": None, "qr_present": None}\n        summary["gateway"] = account_gateway\n        if result["status"] != "ok":\n            continue\n        if session_id is None:\n            account_gateway["status"] = "session_reference_unavailable"\n            continue\n        matches = [row for row in records if isinstance(row, dict) and row.get("name") == session_id]\n        if not matches:\n            account_gateway["status"] = "session_not_found"\n            continue\n        if len(matches) != 1:\n            account_gateway["status"] = "ambiguous_session"\n            continue\n        state = allowed(matches[0].get("status"), PROVIDER_STATES)\n        account_gateway["provider_state"] = state\n        account_gateway["status"] = "session_found"\n        if state != "SCAN_QR_CODE":\n            continue\n        if qr_checks >= MAX_QR_CHECKS:\n            account_gateway["qr_check"] = "limit_reached"\n            continue\n        qr_checks += 1\n        qr, qr_http_status, qr_status = get_json(client, "/api/" + quote(session_id, safe="") + "/auth/qr", {"format": "raw"})\n        account_gateway["qr_http_status"] = qr_http_status\n        account_gateway["qr_check"] = qr_status\n        if qr_status == "ok":\n            account_gateway["qr_present"] = isinstance(qr, dict) and isinstance(qr.get("value"), str) and bool(qr["value"])\n    result["qr_checks"] = qr_checks\n    return result\n\n\ndef diagnose():\n    # Standalone stdin execution does not put /app on sys.path automatically.\n    sys.path.insert(0, "/app")\n    import httpx\n    from sqlalchemy import func, select, text\n    from app.db import Account, Job, Session\n    from app.security import unseal\n\n    report = {"diagnostic": "whatsapp_readonly_v1", "result": "ok", "accounts": []}\n    pairs = []\n    current = datetime.now(timezone.utc)\n    try:\n        with Session() as db:\n            if db.get_bind().dialect.name == "postgresql":\n                db.execute(text("SET TRANSACTION READ ONLY"))\n                db.execute(text("SET LOCAL statement_timeout = \'5000ms\'"))\n            total = db.scalar(select(func.count()).select_from(Account).where(Account.channel == "whatsapp_web"))\n            accounts = db.scalars(select(Account).where(Account.channel == "whatsapp_web").order_by(Account.updated_at.desc(), Account.id).limit(MAX_ACCOUNTS)).all()\n            report["accounts_total"] = total\n            report["accounts_truncated"] = total > MAX_ACCOUNTS\n            for ordinal, account in enumerate(accounts, 1):\n                jobs = db.scalars(select(Job).where(Job.account_id == account.id).order_by(Job.created_at.desc(), Job.id).limit(MAX_JOBS)).all()\n                pairs.append(account_summary(account, jobs, ordinal, unseal, current))\n            # No flush, commit, migration, metadata create or provider auth call.\n            db.rollback()\n    except Exception:\n        return {"diagnostic": "whatsapp_readonly_v1", "result": "database_read_failed", "accounts": []}\n    report["accounts"] = [summary for summary, _session in pairs]\n    endpoint = os.environ.get("WAHA_URL", "").rstrip("/")\n    api_key = os.environ.get("WAHA_API_KEY", "")\n    try:\n        parsed = urlsplit(endpoint)\n        valid = parsed.scheme in {"http", "https"} and parsed.hostname and not any((parsed.username, parsed.password, parsed.query, parsed.fragment))\n    except ValueError:\n        valid = False\n    if not valid or not api_key:\n        report["gateway"] = {"status": "deployment_config_missing_or_invalid", "http_status": None}\n        return report\n    try:\n        with httpx.Client(base_url=endpoint, headers={"X-Api-Key": api_key, "Accept": "application/json"}, timeout=httpx.Timeout(5, connect=3), follow_redirects=False) as client:\n            report["gateway"] = gateway_summary(client, pairs)\n    except Exception:\n        report["gateway"] = {"status": "client_initialization_failed", "http_status": None}\n    return report\n\n\ndef main():\n    # Third-party debug logging must not emit request URLs/session identifiers.\n    logging.disable(logging.CRITICAL)\n    try:\n        report = diagnose()\n    except Exception:\n        report = {"diagnostic": "whatsapp_readonly_v1", "result": "diagnostic_unavailable", "accounts": []}\n    print(json.dumps(report, ensure_ascii=True, indent=2))\n    return 0 if report.get("result") == "ok" else 1\n\n\nif __name__ == "__main__":\n    signal.alarm(28)\n    raise SystemExit(main())\n'


def safe_accounts(raw):
    try:
        data=json.loads(raw);result={'result':enum(data.get('result'),{'ok','database_read_failed','diagnostic_unavailable'}),'accounts_total':number(data.get('accounts_total')),'accounts_truncated':data.get('accounts_truncated') is True,'accounts':[]}
        def gateway(value):
            value=value if isinstance(value,dict) else {}
            clean={'status':enum(value.get('status'),GATEWAY_STATES),'provider_state':enum(value.get('provider_state'),PROVIDER_STATES,None),'qr_present':value.get('qr_present') if type(value.get('qr_present')) is bool else None}
            for key in ('http_status','sessions_http_status','qr_http_status'):
                code=number(value.get(key),599)
                if code is not None and code>=100:clean[key]=code
            if 'qr_check' in value:clean['qr_check']=enum(value['qr_check'],GATEWAY_STATES)
            return clean
        result['gateway']=gateway(data.get('gateway'))
        for account in data.get('accounts',[])[:10]:
            if not isinstance(account,dict):continue
            clean={'state':enum(account.get('state'),STATES),'provider_state':enum(account.get('provider_state'),PROVIDER_STATES,None),'public_error':enum(account.get('public_error'),PUBLIC_ERRORS,None if account.get('public_error') is None else 'unknown_error'),'status_check_error':enum(account.get('status_check_error'),PUBLIC_ERRORS,None if account.get('status_check_error') is None else 'unknown_error'),'updated_age_seconds':number(account.get('updated_age_seconds')),'public_qr_present':account.get('public_qr_present') is True,'gateway':gateway(account.get('gateway')),'jobs':[]}
            for key in ('account_hash','session_hash'):
                value=account.get(key);clean[key]=value if isinstance(value,str) and re.fullmatch('[a-f0-9]{12}',value) else None
            for job in account.get('jobs',[])[:3]:
                if isinstance(job,dict):clean['jobs'].append({'action':enum(job.get('action'),{'login','sync','disconnect'}),'status':enum(job.get('status'),{'queued','running','done','failed'}),'error':enum(job.get('error'),PUBLIC_ERRORS,None if job.get('error') is None else 'unknown_error'),'age_seconds':number(job.get('age_seconds'))})
            result['accounts'].append(clean)
        return result
    except Exception:return {'result':'invalid_response'}


def diagnose():
    runner=Runner();result={'diagnostic':'whatsapp_pairing_readonly_v1','result':'ok','captured_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),'auto_update':auto_state()}
    context=[os.environ['DOCKER_CONTEXT']] if os.environ.get('DOCKER_CONTEXT') else []
    status,raw,_=runner.capture(['docker','context','inspect',*context,'--format','{{(index .Endpoints "docker").Host}}'],maximum=1024)
    if os.environ.get('DOCKER_HOST') or status!='ok' or not raw.strip().startswith(b'unix://'):return dict(result,result='local_docker_unavailable')
    if not(ROOT/'.env.pilot').is_file() or not(ROOT/'compose.pilot.yaml').is_file():return dict(result,result='pilot_config_missing')
    result['containers']=containers(runner)
    status,raw,truncated=runner.compose('logs','--no-color','--no-log-prefix','--timestamps','--since','60m','--tail',str(MAX_LOG_LINES),'waha',timeout=12,maximum=MAX_LOG_BYTES,tail=True)
    result['timeline']=dict(timeline(raw,truncated),capture_status=status)
    status,raw,_=runner.compose('exec','-T','-e','PYTHONDONTWRITEBYTECODE=1','worker','python','-c',ACCOUNT_PROBE,timeout=30,maximum=65536)
    result['accounts']=safe_accounts(raw) if raw and status in {'ok','command_failed'} else {'result':status}
    return result


def main():
    logging.disable(logging.CRITICAL)
    def deadline(*_):raise TimeoutError
    signal.signal(signal.SIGALRM,deadline);signal.alarm(85)
    try:result=diagnose()
    except Exception:result={'diagnostic':'whatsapp_pairing_readonly_v1','result':'diagnostic_unavailable'}
    finally:signal.alarm(0)
    print(json.dumps(result,indent=2,ensure_ascii=True))
    return 0 if result.get('result')=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
