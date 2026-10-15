#!/usr/bin/env python3
"""Self-contained, bounded read-only network packet; safe to run as python3 -.

No routing/firewall/container changes, customer messages, raw logs or secrets.
"""
import ipaddress
import json
import logging
import os
from pathlib import Path
import re
import selectors
import subprocess
import time

ROOT = Path('/opt/omnicrm-pilot')
AUTO = Path('/opt/omnicrm-pilot-autoupdate/state.json')
AUTO_STATES = {'not_checked', 'checking', 'up_to_date', 'packaging', 'deploying', 'deployed', 'previous_attempt_uncertain', 'retry_required', 'non_fast_forward_blocked', 'main_changed_deferred', 'ci_changed_deferred', 'ci_missing', 'ci_pending', 'ci_failed', 'ci_attempt_changed', 'ci_latest_attempt_not_successful', 'ci_identity_mismatch', 'ci_repository_mismatch', 'ci_invalid_response', 'ci_invalid_run_identity', 'github_api_unavailable', 'git_command_failed', 'git_head_invalid', 'git_fetched_sha_mismatch', 'ancestry_unavailable', 'release_packaging_failed', 'release_checksum_mismatch', 'pilot_update_failed', 'command_unavailable_or_timeout', 'unexpected_failure'}
PROVIDER_STATES = {'STOPPED', 'STARTING', 'SCAN_QR_CODE', 'WORKING', 'FAILED', 'PASSKEY_REQUIRED', 'PASSKEY_CONFIRMATION_REQUIRED', 'unknown'}
ERRORS = {'ENOTFOUND', 'EAI_AGAIN', 'ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED', 'EHOSTUNREACH', 'ENETUNREACH', 'CERT_HAS_EXPIRED', 'UNABLE_TO_VERIFY_LEAF_SIGNATURE', 'SELF_SIGNED_CERT_IN_CHAIN', 'DEPTH_ZERO_SELF_SIGNED_CERT', 'ERR_TLS_CERT_ALTNAME_INVALID', 'UNABLE_TO_GET_ISSUER_CERT_LOCALLY'}


def allowed(value, choices):
    return value if isinstance(value, str) and value in choices else 'unknown'


def integer(value, maximum=10**18):
    return value if type(value) is int and 0 <= value <= maximum else None


class Runner:
    def __init__(self):
        self.deadline = time.monotonic() + 90
        self.env = dict(os.environ, LC_ALL='C', LANG='C')
        for name in ('LOCAL_PORT', 'DATABASE_URL', 'ENCRYPTION_KEY', 'JWT_SECRET', 'WAHA_API_KEY', 'POSTGRES_PASSWORD', 'SMS_GATEWAY_URL', 'WAHA_IMAGE'):
            self.env.pop(name, None)

    def capture(self, args, timeout=4, maximum=16384, merge_stderr=False):
        process = None
        selector = selectors.DefaultSelector()
        data = bytearray()
        deadline = min(self.deadline, time.monotonic() + timeout)
        if deadline <= time.monotonic():return 'packet_timeout', b''
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT if merge_stderr else subprocess.DEVNULL, env=self.env)
            selector.register(process.stdout, selectors.EVENT_READ)
            while selector.get_map():
                wait = deadline - time.monotonic()
                if wait <= 0 or not selector.select(wait):return 'timeout', b''
                chunk = os.read(process.stdout.fileno(), min(65536, maximum + 1 - len(data)))
                if not chunk:
                    selector.unregister(process.stdout)
                    break
                data.extend(chunk)
                if len(data) > maximum:return 'output_limit', b''
            code = process.wait(timeout=max(.01, deadline - time.monotonic()))
            return ('ok' if code == 0 else 'command_failed'), bytes(data)
        except subprocess.TimeoutExpired:return 'timeout', b''
        except Exception:return 'unavailable', b''
        finally:
            selector.close()
            if process is not None:
                if process.poll() is None:process.kill()
                try:process.wait(timeout=1)
                except Exception:pass
                if process.stdout:process.stdout.close()

    def compose(self, *args, **kwargs):
        return self.capture(['docker', 'compose', '--project-name', 'omnicrm-pilot', '--env-file', str(ROOT / '.env.pilot'), '-f', str(ROOT / 'compose.pilot.yaml'), *args], **kwargs)


def auto_state():
    try:
        if AUTO.is_symlink() or AUTO.stat().st_size > 32768:raise ValueError
        data = json.loads(AUTO.read_bytes())
        result = {'status': allowed(data.get('status'), AUTO_STATES)}
        for key in ('deployed_sha', 'candidate_sha'):
            if isinstance(data.get(key), str) and re.fullmatch('[a-f0-9]{40}', data[key]):result[key] = data[key]
        return result
    except Exception:return {'status': 'state_unavailable'}


def bind_scope(value):
    try:
        address = ipaddress.ip_address(value)
        if address.is_loopback:return 'loopback'
        if address.is_unspecified:return 'public'
        return 'private' if address.is_private else 'public'
    except Exception:return 'unknown'


def xray(runner):
    status, raw = runner.capture(['docker', 'ps', '-a', '--filter', 'name=^/xray-tg$', '--format', '{{.ID}}'], maximum=128)
    if status != 'ok':return {'status': status}
    if not raw.strip():return {'status': 'absent', 'exists': False}
    identifier = raw.strip().decode('ascii', errors='ignore')
    if not re.fullmatch('[a-f0-9]{12,64}', identifier):return {'status': 'invalid_response'}
    template = '{"running":{{.State.Running}},"ports":{{json .NetworkSettings.Ports}},"network_mode":{{json .HostConfig.NetworkMode}}}'
    status, raw = runner.capture(['docker', 'inspect', '--format', template, identifier])
    if status != 'ok':return {'status': status, 'exists': True}
    try:
        data = json.loads(raw)
        mode = data.get('network_mode')
        result = {'status': 'ok', 'exists': True, 'running': data.get('running') if type(data.get('running')) is bool else None, 'network_mode': mode if mode in {'host', 'bridge', 'none', 'default'} else 'container' if isinstance(mode, str) and mode.startswith('container:') else 'custom', 'published_ports': []}
        for key, bindings in list((data.get('ports') or {}).items())[:50]:
            match = re.fullmatch(r'([0-9]{1,5})/(tcp|udp|sctp)', key)
            if not match or not 1 <= int(match[1]) <= 65535:continue
            for binding in (bindings or [])[:4]:
                port = binding.get('HostPort')
                if isinstance(port, str) and port.isdigit() and 1 <= int(port) <= 65535:
                    result['published_ports'].append({'container_port': int(match[1]), 'host_port': int(port), 'protocol': match[2], 'bind_scope': bind_scope(binding.get('HostIp'))})
        return result
    except Exception:return {'status': 'invalid_response', 'exists': True}


def interface_class(name):
    if not isinstance(name, str):return 'unknown'
    if re.fullmatch(r'(?:tun|tap|wg|tailscale)[0-9]+', name):return 'tunnel'
    if re.fullmatch(r'(?:docker[0-9]+|veth[a-f0-9]+|br-[a-f0-9]+)', name):return 'container'
    if re.fullmatch(r'(?:eth[0-9]+|en[a-z0-9]+)', name):return 'ethernet'
    return 'loopback' if name == 'lo' else 'other'


def routes(runner):
    result = {'scope': 'host_ipv4_current_route_not_proof_of_upstream_reachability'}
    for label, args in [('default', ['show', 'default']), ('telegram_dc2', ['get', '149.154.167.51'])]:
        status, raw = runner.capture(['ip', '-j', '-4', 'route', *args], merge_stderr=True)
        entry = {'status': status}
        if status == 'ok':
            try:
                records = json.loads(raw)
                if not isinstance(records, list):raise ValueError
                entry.update(present=bool(records), routes=[{'type': allowed(row.get('type', 'unicast'), {'unicast', 'unreachable', 'prohibit', 'blackhole', 'throw', 'local', 'broadcast'}), 'interface_class': interface_class(row.get('dev'))} for row in records[:8] if isinstance(row, dict)])
            except Exception:entry['status'] = 'invalid_response'
        elif status == 'command_failed':
            lower = raw.lower()
            entry['route_error'] = 'unreachable' if b'network is unreachable' in lower else 'prohibit' if b'administratively prohibited' in lower else 'unknown'
        result[label] = entry
    return result


def firewall(runner):
    result = {'scope': 'ufw_and_ipv4_filter_output_direct_rules_only_not_all_firewall_paths'}
    status, raw = runner.capture(['ufw', 'status'])
    match = re.search(rb'^Status: (active|inactive)\s*$', raw, re.M) if status == 'ok' else None
    result['ufw'] = match[1].decode() if match else status if status != 'ok' else 'unknown'
    status, raw = runner.capture(['iptables', '-w', '2', '-L', 'OUTPUT', '-n', '-v', '-x'])
    result['output'] = {'status': status}
    if status == 'ok':
        policy = re.search(rb'^Chain OUTPUT \(policy (ACCEPT|DROP) ([0-9]+) packets, ([0-9]+) bytes\)', raw, re.M)
        result['output']['policy'] = policy[1].decode() if policy else 'unknown'
        counts = {target: {'rules': 0, 'packets': 0, 'bytes': 0} for target in ('DROP', 'REJECT')}
        for line in raw.splitlines():
            match = re.match(rb'^\s*([0-9]{1,20})\s+([0-9]{1,20})\s+(DROP|REJECT)\s', line)
            if match:
                target = counts[match[3].decode()]
                target['rules'] += 1; target['packets'] += int(match[1]); target['bytes'] += int(match[2])
        result['output']['direct_rule_counters'] = counts
    return result


NODE_PROBE = r'''
const dns=require('dns'),net=require('net');
const codes=new Set(['ENOTFOUND','EAI_AGAIN','ETIMEDOUT','ECONNRESET','ECONNREFUSED','EHOSTUNREACH','ENETUNREACH','CERT_HAS_EXPIRED','UNABLE_TO_VERIFY_LEAF_SIGNATURE','SELF_SIGNED_CERT_IN_CHAIN','DEPTH_ZERO_SELF_SIGNED_CERT','ERR_TLS_CERT_ALTNAME_INVALID','UNABLE_TO_GET_ISSUER_CERT_LOCALLY']);
const safeCode=e=>codes.has(e&&e.code)?e.code:'unknown';
const opts=(process.env.NODE_OPTIONS||'').split(/\s+/);
const result={status:'complete',heap_default:opts.includes('--max-old-space-size=16384'),ipv4_first:opts.includes('--dns-result-order=ipv4first'),autoselection_disabled:opts.includes('--no-network-family-autoselection'),dns_order:dns.getDefaultResultOrder(),auto_select_family:net.getDefaultAutoSelectFamily(),proxy_configured:['WHATSAPP_PROXY_SERVER','WHATSAPP_PROXY_SERVER_LIST','WAHA_PROXY','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','http_proxy','https_proxy','all_proxy'].some(k=>!!process.env[k])};
let emitted=false;const emit=x=>{if(emitted)return;emitted=true;process.stdout.write(JSON.stringify({...result,wss_default:x}),()=>process.exit(0))};
if(result.proxy_configured){emit({status:'skipped_proxy_configuration'});}else{
let WebSocket;try{WebSocket=require('ws')}catch(e){emit({status:'module_unavailable'})}
if(WebSocket){let socket;const timer=setTimeout(()=>{emit({status:'timeout'});if(socket)socket.terminate()},6500);
const finish=x=>{clearTimeout(timer);emit(x)};
try{socket=new WebSocket('wss://web.whatsapp.com/ws/chat',{origin:'https://web.whatsapp.com',handshakeTimeout:6000,rejectUnauthorized:true});socket.on('open',()=>{finish({status:'opened'});socket.terminate()});socket.on('unexpected-response',(_,r)=>{finish({status:'http_rejected',http_status:r.statusCode});r.destroy();socket.terminate()});socket.on('error',e=>finish({status:'failed',error_code:safeCode(e)}));socket.on('close',()=>finish({status:'closed_before_open'}))}catch(e){finish({status:'failed',error_code:safeCode(e)})}
}}
'''

WAHA_PROBE = r'''
import json,logging,os,re
from urllib.request import Request,ProxyHandler,HTTPRedirectHandler,build_opener
logging.disable(logging.CRITICAL)
class NoRedirect(HTTPRedirectHandler):
 def redirect_request(self,*a,**k):return None
states={'STOPPED','STARTING','SCAN_QR_CODE','WORKING','FAILED','PASSKEY_REQUIRED','PASSKEY_CONFIRMATION_REQUIRED'}
result={'status':'unavailable','session_state_counts':{},'qr_checks':0,'qr_present_count':0}
base=os.environ.get('WAHA_URL','').rstrip('/');key=os.environ.get('WAHA_API_KEY','')
def get(path):
 try:
  with build_opener(ProxyHandler({}),NoRedirect).open(Request(base+path,headers={'X-Api-Key':key}),timeout=3) as response:
   body=response.read(1048577)
   if len(body)>1048576:return None
   return json.loads(body)
 except Exception:return None
if base=='http://waha:3000' and key:
 records=get('/api/sessions?all=true')
 if isinstance(records,list):
  result['status']='ok';result['truncated']=len(records)>1000
  for record in records[:1000]:
   if not isinstance(record,dict):continue
   state=record.get('status');state=state if isinstance(state,str) and state in states else 'unknown'
   result['session_state_counts'][state]=result['session_state_counts'].get(state,0)+1
   name=record.get('name')
   if state=='SCAN_QR_CODE' and result['qr_checks']<3 and isinstance(name,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',name):
    result['qr_checks']+=1;qr=get('/api/'+name+'/auth/qr?format=raw')
    if isinstance(qr,dict) and isinstance(qr.get('value'),str) and qr['value']:result['qr_present_count']+=1
else:result['status']='gateway_configuration_unavailable'
print(json.dumps(result))
'''


def safe_probe(raw, kind):
    try:
        data = json.loads(raw)
        if kind == 'sessions':
            counts = data.get('session_state_counts', {})
            return {'status': allowed(data.get('status'), {'ok', 'unavailable', 'gateway_configuration_unavailable'}), 'session_state_counts': {key: integer(counts[key], 1000) for key in PROVIDER_STATES if key in counts}, 'qr_checks': integer(data.get('qr_checks'), 3), 'qr_present_count': integer(data.get('qr_present_count'), 3), 'truncated': data.get('truncated') is True}
        result = {'status': allowed(data.get('status'), {'complete'}), 'scope': 'direct_websocket_upgrade_only_no_account_authentication'}
        for key in ('heap_default', 'ipv4_first', 'autoselection_disabled', 'auto_select_family', 'proxy_configured'):
            result[key] = data.get(key) if type(data.get(key)) is bool else None
        result['dns_order'] = allowed(data.get('dns_order'), {'ipv4first', 'ipv6first', 'verbatim'})
        ws = data.get('wss_default', {})
        result['wss_default'] = {'status': allowed(ws.get('status'), {'opened', 'timeout', 'failed', 'http_rejected', 'closed_before_open', 'module_unavailable', 'skipped_proxy_configuration'})}
        if 'error_code' in ws:result['wss_default']['error_code'] = allowed(ws['error_code'], ERRORS)
        code = integer(ws.get('http_status'), 599)
        if code is not None and code >= 100:result['wss_default']['http_status'] = code
        return result
    except Exception:return {'status': 'invalid_response'}


# Exact standalone probe copy; tests enforce synchronization for stdin delivery.
TELEGRAM_WSS_PROBE = '#!/usr/bin/env python3\n"""Bounded official Telegram WSS/MTProto probe; run inside the existing worker.\n\nSelf-contained source can also be passed to ``python -c`` or streamed on stdin.\nOnly two fixed DC2 endpoints are contacted. No API id/hash, account session,\nphone number, login code or database is read. Negotiated transport keys stay in\nmemory and are discarded; no user-account authorization is attempted.\n\nProtocol and generic DC2 domain:\nhttps://core.telegram.org/mtproto/transports#websocket\nhttps://core.telegram.org/mtproto/mtproto-transports#transport-obfuscation\nOfficial Web K endpoint (App.suffix=\'K\', DC2, client connection):\nhttps://github.com/morethanwords/tweb/blob/243eaee8b4fb69c0e6d8bac8a3a93eb77f94abe1/src/lib/mtproto/dcConfigurator.ts\nNo Origin is required by the transport documentation, so none is fabricated.\n"""\nfrom __future__ import annotations\n\nimport asyncio\nimport collections\nimport json\nimport logging\nimport os\nimport signal\nimport socket\nimport ssl\nimport time\n\nENDPOINTS = (\n    (\'dc2_kws\', \'kws2.web.telegram.org\'),\n    (\'dc2_venus\', \'venus.web.telegram.org\'),\n)\nMAX_BYTES = 4 * 1024 * 1024\nENDPOINT_TIMEOUT = 10\nPROXY_KEYS = (\n    \'HTTP_PROXY\', \'HTTPS_PROXY\', \'ALL_PROXY\', \'WS_PROXY\', \'WSS_PROXY\',\n    \'http_proxy\', \'https_proxy\', \'all_proxy\', \'ws_proxy\', \'wss_proxy\',\n    \'TELEGRAM_PROXY_URL\', \'TELEGRAM_PROXY\', \'TELEGRAM_SOCKS_PROXY\', \'TELEGRAM_MTPROXY\',\n)\n\n\ndef proxy_configured():\n    return any(os.environ.get(name) for name in PROXY_KEYS)\n\n\nasync def open_websocket(host):\n    from websockets.asyncio.client import connect\n\n    class FixedEndpointConnect(connect):\n        def process_redirect(self, exc):\n            # Never follow a provider redirect to any other host or URI.\n            return exc\n\n    return await FixedEndpointConnect(\n        \'wss://\' + host + \'/apiws\',\n        ssl=ssl.create_default_context(), subprotocols=[\'binary\'],\n        origin=None, proxy=None, compression=None,\n        open_timeout=5, close_timeout=1, ping_interval=None,\n        max_size=MAX_BYTES, max_queue=2, family=socket.AF_INET,\n    )\n\n\nclass ObfuscatedWebSocket:\n    """MTProto send/recv over one continuous binary WebSocket byte stream."""\n    def __init__(self, websocket):\n        from telethon.network.connection.tcpabridged import AbridgedPacketCodec\n        from telethon.network.connection.tcpobfuscated import ObfuscatedIO\n\n        self.websocket = websocket\n        self.codec = AbridgedPacketCodec(self)\n        self.header, self.encryptor, self.decryptor = ObfuscatedIO.init_header(AbridgedPacketCodec)\n        self.buffer = bytearray()\n\n    async def initialize(self):\n        await self.websocket.send(bytes(self.header))\n\n    async def send(self, data):\n        if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES or len(data) % 4:\n            raise ValueError(\'invalid_transport_payload\')\n        await self.websocket.send(self.encryptor.encrypt(self.codec.encode_packet(data)))\n\n    async def readexactly(self, size):\n        if type(size) is not int or size < 0 or size > MAX_BYTES:\n            raise ValueError(\'invalid_transport_length\')\n        while len(self.buffer) < size:\n            frame = await self.websocket.recv()\n            if not isinstance(frame, bytes) or len(self.buffer) + len(frame) > MAX_BYTES:\n                raise ValueError(\'invalid_websocket_payload\')\n            self.buffer.extend(self.decryptor.encrypt(frame))\n        result = bytes(self.buffer[:size])\n        del self.buffer[:size]\n        return result\n\n    async def recv(self):\n        return await self.codec.read_packet(self)\n\n\nasync def probe_endpoint(label, host):\n    result = {\'endpoint\': label, \'status\': \'protocol_error\', \'wss_open\': False, \'mtproto_verified\': False}\n    websocket = None\n    started = time.monotonic()\n    try:\n        from telethon.errors import SecurityError\n        from telethon.network.authenticator import do_authentication\n        from telethon.network.mtprotoplainsender import MTProtoPlainSender\n        from websockets.exceptions import InvalidStatus\n\n        async with asyncio.timeout(ENDPOINT_TIMEOUT):\n            websocket = await open_websocket(host)\n            if websocket.subprotocol != \'binary\':\n                raise ValueError(\'missing_binary_subprotocol\')\n            result[\'wss_open\'] = True\n            transport = ObfuscatedWebSocket(websocket)\n            await transport.initialize()\n            logs = collections.defaultdict(lambda: logging.getLogger(\'telegram-wss-probe\'))\n            # The existing Telethon authenticator checks the official RSA keys,\n            # nonces and DH values. This does not authorize any user account.\n            key, _offset = await do_authentication(MTProtoPlainSender(transport, loggers=logs))\n            if not key or not key.key:\n                raise SecurityError(\'empty_key\')\n            result.update(status=\'mtproto_verified\', mtproto_verified=True)\n            del key\n    except TimeoutError:\n        result[\'status\'] = \'timeout\'\n    except ImportError:\n        result[\'status\'] = \'module_unavailable\'\n    except ssl.SSLError:\n        result[\'status\'] = \'tls_error\'\n    except socket.gaierror:\n        result[\'status\'] = \'dns_error\'\n    except OSError:\n        result[\'status\'] = \'network_error\'\n    except Exception as exc:\n        # Import failures were already handled; inspect known classes without\n        # ever printing provider exception text or close reasons.\n        name = type(exc).__name__\n        if name == \'InvalidStatus\':\n            result[\'status\'] = \'http_rejected\'\n            code = getattr(getattr(exc, \'response\', None), \'status_code\', None)\n            if type(code) is int and 100 <= code <= 599:\n                result[\'http_status\'] = code\n        elif name in {\'SecurityError\', \'AssertionError\'}:\n            result[\'status\'] = \'security_error\'\n        else:\n            result[\'status\'] = \'protocol_error\'\n    finally:\n        if websocket is not None:\n            try:\n                await asyncio.wait_for(websocket.close(), timeout=1)\n            except Exception:\n                pass\n    result[\'elapsed_ms\'] = min(26000, max(0, int((time.monotonic() - started) * 1000)))\n    return result\n\n\nasync def diagnose():\n    if proxy_configured():\n        return {\'status\': \'skipped_proxy_configuration\', \'proxy_configured\': True, \'checks\': []}\n    checks = []\n    for label, host in ENDPOINTS:\n        checks.append(await probe_endpoint(label, host))\n    return {\'status\': \'complete\', \'proxy_configured\': False, \'checks\': checks}\n\n\ndef main():\n    logging.disable(logging.CRITICAL)\n    # Bound even CPU-heavy key exchange or a lost docker-exec client.\n    signal.alarm(26)\n    try:\n        result = asyncio.run(diagnose())\n    except Exception:\n        result = {\'status\': \'probe_failed\', \'checks\': []}\n    finally:\n        signal.alarm(0)\n    print(json.dumps(result, sort_keys=True))\n\n\nif __name__ == \'__main__\':\n    main()\n'


def safe_telegram_wss(raw):
    try:
        data = json.loads(raw)
        result = {'status': allowed(data.get('status'), {'complete', 'skipped_proxy_configuration', 'probe_failed'}), 'scope': 'ephemeral_mtproto_transport_only_no_account_authentication', 'proxy_configured': data.get('proxy_configured') is True, 'checks': []}
        for item in data.get('checks', [])[:2]:
            if not isinstance(item, dict) or item.get('endpoint') not in {'dc2_kws', 'dc2_venus'}:continue
            clean = {'endpoint': item['endpoint'], 'status': allowed(item.get('status'), {'mtproto_verified', 'timeout', 'http_rejected', 'tls_error', 'dns_error', 'network_error', 'protocol_error', 'security_error', 'module_unavailable'}), 'wss_open': item.get('wss_open') is True, 'mtproto_verified': item.get('mtproto_verified') is True, 'elapsed_ms': integer(item.get('elapsed_ms'), 30000)}
            code = integer(item.get('http_status'), 599)
            if code is not None and code >= 100:clean['http_status'] = code
            result['checks'].append(clean)
        return result
    except Exception:return {'status': 'invalid_response'}


def diagnose():
    runner = Runner()
    result = {'diagnostic': 'network_route_readonly_v1', 'result': 'ok', 'auto_update': auto_state(), 'host_routes': routes(runner), 'host_firewall': firewall(runner)}
    # Force local Engine selection; never inspect a remote Docker context.
    context = [os.environ['DOCKER_CONTEXT']] if os.environ.get('DOCKER_CONTEXT') else []
    status, raw = runner.capture(['docker', 'context', 'inspect', *context, '--format', '{{(index .Endpoints "docker").Host}}'], maximum=1024)
    if os.environ.get('DOCKER_HOST') or status != 'ok' or not raw.strip().startswith(b'unix://'):
        return dict(result, docker='local_docker_unavailable')
    result['xray_tg'] = xray(runner)
    if not (ROOT / '.env.pilot').is_file() or not (ROOT / 'compose.pilot.yaml').is_file():return dict(result, pilot='configuration_unavailable')
    status, raw = runner.compose('exec', '-T', 'waha', 'node', '-e', NODE_PROBE, timeout=9, maximum=4096)
    result['waha_network'] = safe_probe(raw, 'network') if status == 'ok' else {'status': status}
    status, raw = runner.compose('exec', '-T', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'worker', 'python', '-c', WAHA_PROBE, timeout=15, maximum=4096)
    result['waha_sessions'] = safe_probe(raw, 'sessions') if status == 'ok' else {'status': status}
    status, raw = runner.compose('exec', '-T', '-e', 'PYTHONDONTWRITEBYTECODE=1', 'worker', 'python', '-c', TELEGRAM_WSS_PROBE, timeout=30, maximum=4096)
    result['telegram_wss'] = safe_telegram_wss(raw) if status == 'ok' else {'status': status}
    return result


def main():
    logging.disable(logging.CRITICAL)
    try:result = diagnose()
    except Exception:result = {'diagnostic': 'network_route_readonly_v1', 'result': 'diagnostic_unavailable'}
    print(json.dumps(result, indent=2, ensure_ascii=True))
    return 0 if result.get('result') == 'ok' else 1


if __name__ == '__main__':
    raise SystemExit(main())
