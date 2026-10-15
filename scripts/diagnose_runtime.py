#!/usr/bin/env python3
"""Bounded host-only runtime diagnosis. No restarts, account changes or auth traffic."""
from __future__ import annotations
import errno
import json
import logging
import os
from pathlib import Path
import re
import selectors
import subprocess
import time
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

ROOT = Path('/opt/omnicrm-pilot')
AUTO = Path('/opt/omnicrm-pilot-autoupdate/state.json')
SERVICES = {'api', 'worker', 'postgres', 'waha', 'migrate', 'waha-volume-init'}
STATES = {'created', 'running', 'paused', 'restarting', 'removing', 'exited', 'dead'}
HEALTH = {'healthy', 'unhealthy', 'starting', 'none'}
ERRORS = {'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED', 'EHOSTUNREACH', 'ENETUNREACH', 'EADDRNOTAVAIL', 'EAFNOSUPPORT', 'EPIPE', 'EACCES', 'EPERM', 'EROFS', 'ENOSPC', 'CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'SELF_SIGNED_CERT_IN_CHAIN', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'ERR_TLS_CERT_ALTNAME_INVALID', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY', 'ERR_TLS_HANDSHAKE_TIMEOUT', 'SQLITE_BUSY', 'SQLITE_CANTOPEN'}
TG_ERRORS = {'telegram_transport_error', 'telegram_configuration_invalid', 'telegram_app_not_configured', 'telegram_request_rejected', 'telegram_server_error', 'telegram_auth_key_unavailable', 'telegram_session_expired', 'telegram_flood_wait', 'telegram_code_invalid', 'telegram_code_expired', 'telegram_phone_invalid', 'telegram_phone_banned', 'telegram_password_invalid', 'telegram_account_identity_changed', 'telegram_qr_expired', 'telegram_qr_invalid', 'provider_auth_error', 'provider_auth_timeout', 'provider_unauthorized', 'auth_job_expired', 'worker_interrupted_retry_login', 'worker_ownership_connection_lost', 'worker_ownership_lost', 'worker_fencing_epoch_changed', 'TimeoutError', 'ConnectTimeout', 'ReadTimeout', 'ConnectionError', 'unknown_error'}
AUTO_STATES = {'not_checked', 'checking', 'up_to_date', 'packaging', 'deploying', 'deployed', 'previous_attempt_uncertain', 'retry_required', 'non_fast_forward_blocked', 'main_changed_deferred', 'ci_changed_deferred', 'ci_missing', 'ci_pending', 'ci_failed', 'ci_attempt_changed', 'ci_latest_attempt_not_successful', 'ci_identity_mismatch', 'ci_repository_mismatch', 'ci_invalid_response', 'ci_invalid_run_identity', 'github_api_unavailable', 'git_command_failed', 'git_head_invalid', 'git_fetched_sha_mismatch', 'ancestry_unavailable', 'release_packaging_failed', 'release_checksum_mismatch', 'pilot_update_failed', 'command_unavailable_or_timeout', 'unexpected_failure'}


def enum(value, choices, fallback='unknown'):
    return value if isinstance(value, str) and value in choices else fallback


def number(value, maximum=10**15):
    return value if type(value) is int and 0 <= value <= maximum else None


class Runner:
    def __init__(self):
        self.deadline = time.monotonic() + 90
        self.env = dict(os.environ)
        for key in ('LOCAL_PORT', 'DATABASE_URL', 'ENCRYPTION_KEY', 'JWT_SECRET', 'WAHA_API_KEY', 'POSTGRES_PASSWORD', 'SMS_GATEWAY_URL', 'WAHA_IMAGE'):
            self.env.pop(key, None)

    def capture(self, args, timeout=8, maximum=65536, merge_stderr=False):
        remaining = min(timeout, self.deadline - time.monotonic())
        if remaining <= 0:
            return 'packet_timeout', b''
        process = None
        selector = selectors.DefaultSelector()
        data = bytearray()
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL, env=self.env)
            selector.register(process.stdout, selectors.EVENT_READ)
            deadline = time.monotonic() + remaining
            while selector.get_map():
                wait = deadline - time.monotonic()
                if wait <= 0:
                    return 'timeout', bytes(data)
                events = selector.select(wait)
                if not events:
                    return 'timeout', bytes(data)
                chunk = os.read(process.stdout.fileno(), min(65536, maximum + 1 - len(data)))
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                data.extend(chunk)
                if len(data) > maximum:
                    return 'output_limit', bytes(data[:maximum])
            code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
            return ('ok' if code == 0 else 'command_failed'), bytes(data)
        except subprocess.TimeoutExpired:
            return 'timeout', bytes(data)
        except Exception:
            return 'unavailable', b''
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=2)
                except Exception:
                    pass
                if process.stdout:
                    process.stdout.close()

    def compose(self, *args, **kwargs):
        return self.capture(['docker', 'compose', '--project-name', 'omnicrm-pilot', '--env-file', str(ROOT / '.env.pilot'), '-f', str(ROOT / 'compose.pilot.yaml'), *args], **kwargs)


def containers(runner):
    status, output = runner.compose('ps', '--all', '--quiet', maximum=4096)
    ids = output.decode('ascii', errors='ignore').split()
    if status != 'ok' or any(not re.fullmatch('[a-f0-9]{12,64}', value) for value in ids):
        return {'status': status if status != 'ok' else 'invalid_response', 'services': []}
    if not ids:
        return {'status': 'no_containers', 'services': []}
    template = '{"service":{{json (index .Config.Labels "com.docker.compose.service")}},"state":{{json .State.Status}},"health":{{if .State.Health}}{{json .State.Health.Status}}{{else}}"none"{{end}},"restart_count":{{.RestartCount}},"oom_killed":{{.State.OOMKilled}},"memory_limit_bytes":{{.HostConfig.Memory}}}'
    status, output = runner.capture(['docker', 'inspect', '--format', template, *ids[:20]], maximum=16384)
    result = []
    if status == 'ok':
        for line in output.splitlines():
            try:
                item = json.loads(line)
                if item.get('service') not in SERVICES:
                    continue
                result.append({'service': item['service'], 'state': enum(item.get('state'), STATES), 'health': enum(item.get('health'), HEALTH), 'restart_count': number(item.get('restart_count')), 'oom_killed': item.get('oom_killed') if type(item.get('oom_killed')) is bool else None, 'memory_limit_bytes': number(item.get('memory_limit_bytes'))})
            except Exception:
                status = 'invalid_response'
    return {'status': status, 'services': result, 'truncated': len(ids) > 20}


LOG_PATTERNS = {
    'dns_error': r'\b(?:ENOTFOUND|EAI_AGAIN|EAI_FAIL|getaddrinfo)\b',
    'tcp_error': r'\b(?:ECONNRESET|ECONNREFUSED|EHOSTUNREACH|ENETUNREACH|EPIPE)\b|socket hang up',
    'tls_error': r'\b(?:CERT_HAS_EXPIRED|UNABLE_TO_VERIFY_LEAF_SIGNATURE|SELF_SIGNED_CERT_IN_CHAIN|DEPTH_ZERO_SELF_SIGNED_CERT|ERR_TLS_CERT_ALTNAME_INVALID)\b|tls handshake|certificate verify failed',
    'timeout': r'\b(?:ETIMEDOUT|timeout|timed out)\b',
    'http_401': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+401\b',
    'http_403': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+403\b',
    'http_405': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+405\b',
    'http_408': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+408\b',
    'http_429': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+429\b',
    'http_440': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+440\b',
    'http_515': r'\b(?:statusCode|status|HTTP|code)[\s\x22\x27:=]+515\b',
    'out_of_memory': r'\b(?:OOMKilled|ENOMEM|oom-kill)\b|out of memory',
    'permission_error': r'\b(?:EACCES|EPERM|EROFS)\b|permission denied',
    'storage_error': r'\b(?:ENOSPC|SQLITE_BUSY|SQLITE_CANTOPEN)\b',
    'noweb_start_failed': r'Failed to start the client',
    'socket_initialization_reached': r'Connecting using wa\.version',
    'registration_attempted': r'not logged in, attempting registration',
    'wa_validation_failed': r'error in validating connection',
    'wa_connection_errored': r'connection errored',
    'wa_connection_closed': r'connection closed',
    'wa_connection_opened': r'opened connection to WA',
    'starting_watchdog': r'Session stuck in STARTING status',
    'qr_watchdog': r'QR code has not been scanned yet',
}


def classify_logs(data):
    lines = data.decode('utf-8', errors='replace').splitlines()[-1000:]
    counts = {key: sum(bool(re.search(pattern, line, re.I)) for line in lines) for key, pattern in LOG_PATTERNS.items()}
    counts['websocket_error'] = sum(bool(re.search(r'websocket|web socket|\bwss\b', line, re.I) and re.search(r'error|fail|closed|timeout', line, re.I)) for line in lines)
    return {'lines_examined': len(lines), 'category_counts': counts, 'code_counts': {code: sum(bool(re.search(r'\b' + re.escape(code) + r'\b', line)) for line in lines) for code in sorted(ERRORS)}}


NODE_PROBE = r'''
const dns=require('dns'), https=require('https');
const allowed=new Set(['ENOTFOUND','EAI_AGAIN','ETIMEDOUT','ECONNRESET','ECONNREFUSED','EHOSTUNREACH','ENETUNREACH','EPIPE','CERT_HAS_EXPIRED','UNABLE_TO_VERIFY_LEAF_SIGNATURE','SELF_SIGNED_CERT_IN_CHAIN','DEPTH_ZERO_SELF_SIGNED_CERT','ERR_TLS_CERT_ALTNAME_INVALID','UNABLE_TO_GET_ISSUER_CERT_LOCALLY','ERR_TLS_HANDSHAKE_TIMEOUT']);
const code=e=>allowed.has(e&&e.code)?e.code:'other';
const proxy=['WHATSAPP_PROXY_SERVER','HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'].some(k=>!!process.env[k]);
let emitted=false;const emit=x=>{if(emitted)return;emitted=true;process.stdout.write(JSON.stringify(x),()=>process.exit(0))};
if(proxy){emit({status:'skipped_proxy_configuration',proxy_configured:true});}else{
const globalTimer=setTimeout(()=>emit({status:'probe_timeout',proxy_configured:false}),11000);
function lookup(){return new Promise(resolve=>{let done=false;const finish=x=>{if(done)return;done=true;clearTimeout(timer);resolve(x)};const timer=setTimeout(()=>finish({status:'timeout'}),6000);dns.lookup('web.whatsapp.com',{all:true},(e,a)=>finish(e?{status:'failed',error_code:code(e)}:{status:'resolved',ipv4_records:a.filter(x=>x.family===4).length,ipv6_records:a.filter(x=>x.family===6).length}));})}
function http(family){return new Promise(resolve=>{let done=false,req;const finish=x=>{if(done)return;done=true;clearTimeout(timer);resolve(x)};const timer=setTimeout(()=>{finish({status:'timeout'});if(req)req.destroy()},6500);try{req=https.get('https://web.whatsapp.com/',{family,agent:false,rejectUnauthorized:true,headers:{'User-Agent':'OmniCRM-readonly-connectivity-check'}},res=>{finish({status:'responded',http_status:res.statusCode,tls_authorized:res.socket.authorized===true});res.destroy()});req.on('error',e=>finish({status:'failed',error_code:code(e)}))}catch(e){finish({status:'failed',error_code:code(e)})}})}
function ws(family){return new Promise(resolve=>{let WebSocket;try{WebSocket=require('ws')}catch(e){resolve({status:'module_unavailable'});return}let done=false,socket;const finish=x=>{if(done)return;done=true;clearTimeout(timer);resolve(x)};const timer=setTimeout(()=>{finish({status:'timeout'});if(socket)socket.terminate()},6500);try{socket=new WebSocket('wss://web.whatsapp.com/ws/chat',{origin:'https://web.whatsapp.com',family,handshakeTimeout:6000,rejectUnauthorized:true});socket.on('open',()=>{finish({status:'opened'});socket.terminate()});socket.on('unexpected-response',(_,r)=>{finish({status:'http_rejected',http_status:r.statusCode});r.destroy();socket.terminate()});socket.on('error',e=>finish({status:'failed',error_code:code(e)}));socket.on('close',()=>finish({status:'closed_before_open'}))}catch(e){finish({status:'failed',error_code:code(e)})}})}
Promise.all([lookup(),http(0),http(4),ws(0),ws(4)]).then(([dns,https_default,https_ipv4,wss_default,wss_ipv4])=>{clearTimeout(globalTimer);emit({status:'complete',proxy_configured:false,dns,https_default,https_ipv4,wss_default,wss_ipv4})}).catch(()=>emit({status:'probe_failed',proxy_configured:false}));
}
'''

TCP_PROBE = r'''
import errno,json,os,socket,time
proxy=any(os.environ.get(k) for k in ('HTTPS_PROXY','HTTP_PROXY','ALL_PROXY','https_proxy','http_proxy','all_proxy'))
if proxy:
 print(json.dumps({'status':'skipped_proxy_configuration','proxy_configured':True}));raise SystemExit(0)
result={'status':'complete','proxy_configured':False,'checks':[]}
allowed={'ETIMEDOUT','ECONNRESET','ECONNREFUSED','EHOSTUNREACH','ENETUNREACH','EADDRNOTAVAIL','EAFNOSUPPORT','EACCES','EPERM'}
for label,family,address in [('default_dc2_ipv4',socket.AF_INET,('149.154.167.51',443)),('default_dc2_ipv6',socket.AF_INET6,('2001:67c:4e8:f002::a',443,0,0))]:
 start=time.monotonic();entry={'endpoint':label};connection=None
 try:
  connection=socket.socket(family,socket.SOCK_STREAM);connection.settimeout(4);connection.connect(address);entry['status']='connected'
 except TimeoutError:entry.update(status='timeout',error_code='ETIMEDOUT')
 except OSError as exc:entry.update(status='failed',error_code=errno.errorcode.get(exc.errno) if errno.errorcode.get(exc.errno) in allowed else 'other')
 except Exception:entry.update(status='failed',error_code='other')
 finally:
  if connection is not None:connection.close()
 entry['elapsed_ms']=int((time.monotonic()-start)*1000);result['checks'].append(entry)
print(json.dumps(result))
'''


def safe_probe(output, kind):
    try:
        data = json.loads(output)
        result = {'status': enum(data.get('status'), {'complete', 'skipped_proxy_configuration', 'probe_timeout', 'probe_failed'}), 'proxy_configured': data.get('proxy_configured') is True, 'scope': 'endpoint_connectivity_only_not_account_authentication'}
        def item(value):
            if not isinstance(value, dict):
                return {'status': 'invalid_response'}
            clean = {'status': enum(value.get('status'), {'resolved', 'responded', 'opened', 'http_rejected', 'closed_before_open', 'connected', 'failed', 'timeout', 'module_unavailable'})}
            if 'error_code' in value:clean['error_code'] = enum(value['error_code'], ERRORS, 'other')
            for key in ('ipv4_records', 'ipv6_records', 'elapsed_ms'):
                if key in value:clean[key] = number(value[key], 100000)
            if type(value.get('http_status')) is int and 100 <= value['http_status'] <= 599:clean['http_status'] = value['http_status']
            if type(value.get('tls_authorized')) is bool:clean['tls_authorized'] = value['tls_authorized']
            return clean
        if kind == 'waha':
            for key in ('dns', 'https_default', 'https_ipv4', 'wss_default', 'wss_ipv4'):
                if key in data:result[key] = item(data[key])
        elif isinstance(data.get('checks'), list):
            result['checks'] = [dict(item(value), endpoint=value['endpoint']) for value in data['checks'][:2] if isinstance(value, dict) and value.get('endpoint') in {'default_dc2_ipv4', 'default_dc2_ipv6'}]
        return result
    except Exception:
        return {'status': 'invalid_response'}


def safe_telegram(output):
    try:
        data = json.loads(output)
        result = {'status': enum(data.get('result'), {'ok', 'database_read_failed', 'diagnostic_unavailable', 'database_not_configured', 'database_backend_not_supported'}), 'accounts': []}
        for key in ('accounts_total',):
            if key in data:result[key] = number(data[key])
        for key in ('accounts_truncated', 'shared_configuration_exists'):
            if type(data.get(key)) is bool:result[key] = data[key]
        heartbeat = data.get('worker_heartbeat', {})
        result['worker_heartbeat'] = {'status': enum(heartbeat.get('status'), {'not_visible_in_this_process', 'not_regular_file', 'future_timestamp', 'visible', 'unavailable'}), 'age_seconds': number(heartbeat.get('age_seconds')), 'fresh': heartbeat.get('fresh') if type(heartbeat.get('fresh')) is bool else None}
        for account in data.get('accounts', [])[:10]:
            if not isinstance(account, dict) or not re.fullmatch('[a-f0-9]{12}', str(account.get('account_hash', ''))):continue
            clean = {'account_hash': account['account_hash'], 'state': enum(account.get('state'), {'disconnected', 'connecting', 'connected', 'reauth_required', 'action_required', 'awaiting_code', 'awaiting_password', 'awaiting_qr', 'code_required', 'password_required', 'qr_required'}), 'public_error': enum(account.get('public_error'), TG_ERRORS, None if account.get('public_error') is None else 'unknown_error'), 'updated_age_seconds': number(account.get('updated_age_seconds')), 'shared_configuration_exists': account.get('shared_configuration_exists') is True, 'jobs': []}
            for job in account.get('jobs', [])[:3]:
                if isinstance(job, dict):clean['jobs'].append({'action': enum(job.get('action'), {'login', 'code', 'password', 'qr', 'sync', 'disconnect'}), 'status': enum(job.get('status'), {'queued', 'running', 'done', 'failed'}), 'error': enum(job.get('error'), TG_ERRORS, None if job.get('error') is None else 'unknown_error'), 'age_seconds': number(job.get('age_seconds'))})
            result['accounts'].append(clean)
        return result
    except Exception:
        return {'status': 'invalid_response'}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):return None


def local_health(runner):
    status, output = runner.compose('port', 'api', '8080', maximum=128)
    match = re.fullmatch(r'127\.0\.0\.1:([0-9]{1,5})\s*', output.decode('ascii', errors='ignore'))
    if status != 'ok' or not match or not 1 <= int(match[1]) <= 65535:return {'status': 'loopback_port_unavailable'}
    result = {'status': 'checked', 'port': int(match[1]), 'checks': {}}
    for name, path in [('live', '/health/live'), ('ready', '/health/ready'), ('runtime', '/api/runtime')]:
        entry = {}
        try:
            request = Request('http://127.0.0.1:' + match[1] + path)
            try:response = build_opener(ProxyHandler({}), NoRedirect).open(request, timeout=3)
            except HTTPError as exc:response = exc
            with response:
                entry['http_status'] = response.code
                body = response.read(4097)
            data = json.loads(body) if len(body) <= 4096 else {}
            if isinstance(data, dict):
                if data.get('status') in {'ok', 'error', 'unavailable'}:entry['status'] = data['status']
                for key in ('local_test', 'demo'):
                    if type(data.get(key)) is bool:entry[key] = data[key]
        except Exception:
            entry['status'] = 'request_failed_or_body_not_json'
        result['checks'][name] = entry
    return result


def auto_state(runner):
    result = {}
    try:
        if AUTO.is_symlink() or AUTO.stat().st_size > 32768:raise ValueError
        data = json.loads(AUTO.read_bytes())
        result['status'] = enum(data.get('status'), AUTO_STATES)
        for key in ('deployed_sha', 'candidate_sha'):
            if isinstance(data.get(key), str) and re.fullmatch('[a-f0-9]{40}', data[key]):result[key] = data[key]
        if isinstance(data.get('deployed_at'), str) and re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+00:00|Z)', data['deployed_at']):result['deployed_at'] = data['deployed_at']
    except Exception:
        result['status'] = 'state_unavailable'
    for unit in ('service', 'timer'):
        status, output = runner.capture(['systemctl', 'show', 'omnicrm-pilot-autoupdate.' + unit, '--property=ActiveState,SubState'], timeout=3, maximum=512)
        safe = {'status': status}
        if status == 'ok':
            for line in output.decode('ascii', errors='ignore').splitlines():
                key, _, value = line.partition('=')
                if key == 'ActiveState':safe['active'] = enum(value, {'active', 'inactive', 'activating', 'deactivating', 'failed', 'reloading'})
                if key == 'SubState':safe['substate'] = enum(value, {'running', 'dead', 'exited', 'waiting', 'elapsed', 'failed', 'start', 'stop', 'start-pre', 'start-post', 'stop-sigterm', 'stop-sigkill', 'auto-restart'})
        result[unit] = safe
    return result


def diagnose():
    runner = Runner()
    result = {'diagnostic': 'runtime_readonly_v1', 'result': 'ok'}
    if not (ROOT / '.env.pilot').is_file() or not (ROOT / 'compose.pilot.yaml').is_file():return {**result, 'result': 'pilot_config_missing'}
    if os.environ.get('DOCKER_CONTEXT'):
        status, output = runner.capture(['docker', 'context', 'inspect', os.environ['DOCKER_CONTEXT'], '--format', '{{(index .Endpoints "docker").Host}}'], maximum=1024)
    elif os.environ.get('DOCKER_HOST'):
        status, output = 'ok', os.environ['DOCKER_HOST'].encode()
    else:
        status, output = runner.capture(['docker', 'context', 'inspect', '--format', '{{(index .Endpoints "docker").Host}}'], maximum=1024)
    if status != 'ok' or not output.strip().startswith(b'unix://'):return {**result, 'result': 'local_docker_unavailable'}
    result['containers'] = containers(runner)
    result['local_api'] = local_health(runner)
    status, output = runner.compose('logs', '--no-color', '--no-log-prefix', '--since', '30m', '--tail', '1000', 'waha', maximum=1024 * 1024, merge_stderr=True)
    result['waha_logs'] = dict(classify_logs(output), capture_status=status, window_minutes=30)
    status, output = runner.compose('exec', '-T', 'waha', 'node', '-e', NODE_PROBE, timeout=14, maximum=8192)
    result['waha_network'] = safe_probe(output, 'waha') if status == 'ok' else {'status': status}
    status, output = runner.compose('exec', '-T', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'worker', 'python', '-c', TCP_PROBE, timeout=12, maximum=4096)
    result['telegram_tcp'] = safe_probe(output, 'telegram') if status == 'ok' else {'status': status}
    status, output = runner.compose('exec', '-T', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'worker', 'python', 'scripts/diagnose_telegram.py', timeout=12, maximum=32768)
    result['telegram_accounts'] = safe_telegram(output) if output and status in {'ok', 'command_failed'} else {'status': 'diagnostic_unavailable'}
    result['auto_update'] = auto_state(runner)
    return result


def main():
    logging.disable(logging.CRITICAL)
    try:result = diagnose()
    except Exception:result = {'diagnostic': 'runtime_readonly_v1', 'result': 'diagnostic_unavailable'}
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result.get('result') == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
