#!/usr/bin/env python3
"""Bounded direct Telegram transport diagnosis for the existing private pilot.

Run on the pilot server as root, including via ``python3 - < this_file``. The
host needs only Python's standard library. The existing worker supplies Telethon
for at most two ephemeral MTProto transport-key negotiations. No API credentials,
phone codes, account authorization, database access or session files are used.

Fixed DC2 bootstrap addresses come from Telegram Desktop's official source:
https://github.com/telegramdesktop/tdesktop/blob/dev/Telegram/SourceFiles/mtproto/mtproto_dc_options.cpp
TCP ports 80/443/5222 are documented by Telegram:
https://core.telegram.org/mtproto/transports
This diagnostic does not override a client's dcOption or change its connection.
A timeout is evidence for this probe only, not proof of network blocking.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import selectors
import subprocess
import time

PILOT = Path('/opt/omnicrm-pilot')
MAX_OUTPUT = 65536
TOTAL_SECONDS = 58

# Kept as source constants so a host script streamed on stdin can run the same
# fixed probes inside the existing worker without mounting or copying a file.
TCP_PROBE = r'''
import concurrent.futures
import errno
import os
import socket
import time

ENDPOINTS = (
    ('dc2_primary_443', '149.154.167.51', 443),
    ('dc2_alternate_443', '95.161.76.100', 443),
    ('dc2_primary_80', '149.154.167.51', 80),
    ('dc2_alternate_80', '95.161.76.100', 80),
    ('dc2_primary_5222', '149.154.167.51', 5222),
    ('dc2_alternate_5222', '95.161.76.100', 5222),
)
PROXY_KEYS = (
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy',
    'TELEGRAM_PROXY_URL', 'TELEGRAM_PROXY',
    'TELEGRAM_SOCKS_PROXY', 'TELEGRAM_MTPROXY',
)
TCP_ERRORS = frozenset({
    'ETIMEDOUT', 'ECONNRESET', 'ECONNREFUSED', 'EHOSTUNREACH',
    'ENETUNREACH', 'EADDRNOTAVAIL', 'EAFNOSUPPORT', 'EACCES', 'EPERM',
})


def proxy_configured():
    return any(os.environ.get(name) for name in PROXY_KEYS)


def tcp_one(endpoint):
    label, address, port = endpoint
    result = {'endpoint': label}
    started = time.monotonic()
    connection = None
    try:
        connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.settimeout(4)
        connection.connect((address, port))
        result['status'] = 'connected'
    except TimeoutError:
        result.update(status='timeout', error='ETIMEDOUT')
    except OSError as exc:
        code = errno.errorcode.get(exc.errno, 'other')
        result.update(status='failed', error=code if code in TCP_ERRORS else 'other')
    except Exception:
        result.update(status='failed', error='other')
    finally:
        if connection is not None:
            connection.close()
    result['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return result


def tcp_probe():
    if proxy_configured():
        return {'status': 'skipped_proxy_configuration', 'proxy_configured': True, 'checks': []}
    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
        checks = list(pool.map(tcp_one, ENDPOINTS))
    return {'status': 'complete', 'proxy_configured': False, 'checks': checks}
'''

HANDSHAKE_PROBE = r'''
import asyncio
import collections
import json
import logging
import signal

logging.disable(logging.CRITICAL)


async def handshake_one(endpoint):
    label, address, port = endpoint
    result = {'endpoint': label, 'transport': 'tcp_full'}
    started = time.monotonic()
    connection = None
    try:
        from telethon.network.connection import ConnectionTcpFull
        from telethon.network.mtprotoplainsender import MTProtoPlainSender
        from telethon.network.authenticator import do_authentication
        from telethon.errors import SecurityError

        logs = collections.defaultdict(lambda: logging.getLogger('telegram-route-probe'))
        connection = ConnectionTcpFull(address, port, 2, loggers=logs)
        async with asyncio.timeout(10):
            await connection.connect(timeout=4)
            # Telethon verifies Telegram's RSA fingerprint, nonces and DH checks.
            # This creates only an in-memory transport key, never user login.
            key, _offset = await do_authentication(MTProtoPlainSender(connection, loggers=logs))
            if not key or not key.key:
                raise SecurityError('empty_key')
            result['status'] = 'verified'
            del key
    except TimeoutError:
        result['status'] = 'timeout'
    except ImportError:
        result['status'] = 'module_unavailable'
    except Exception as exc:
        name = type(exc).__name__
        result['status'] = (
            'security_error' if name in {'SecurityError', 'AssertionError'}
            else 'transport_error' if isinstance(exc, (OSError, ConnectionError))
            else 'protocol_error'
        )
    finally:
        if connection is not None:
            try:
                await asyncio.wait_for(connection.disconnect(), timeout=2)
            except Exception:
                pass
    result['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
    return result


async def handshake_routes(report):
    reachable = {item['endpoint'] for item in report.get('checks', []) if item.get('status') == 'connected'}
    selected = [endpoint for endpoint in ENDPOINTS if endpoint[0] in reachable][:2]
    report['handshakes'] = []
    for endpoint in selected:
        report['handshakes'].append(await handshake_one(endpoint))
    return report


def worker_main():
    # Also bounds the container process if the docker-exec client is interrupted.
    signal.alarm(42)
    try:
        report = tcp_probe()
        if report.get('status') == 'complete':
            report = asyncio.run(handshake_routes(report))
        print(json.dumps(report))
    except Exception:
        print(json.dumps({'status': 'probe_failed'}))
    finally:
        signal.alarm(0)


worker_main()
'''

WORKER_PROBE = TCP_PROBE + HANDSHAKE_PROBE
_tcp: dict = {}
exec(compile(TCP_PROBE, '<fixed-telegram-tcp-probe>', 'exec'), _tcp)
ENDPOINT_LABELS = frozenset(endpoint[0] for endpoint in _tcp['ENDPOINTS'])


def command(args, *, timeout, environment=None):
    """Never expose stderr; cap stdout before parsing and terminate on deadline."""
    process = None
    try:
        process = subprocess.Popen(
            args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=environment,
        )
        output = bytearray()
        deadline = time.monotonic() + timeout
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return 'command_timeout', b''
                if not selector.select(min(remaining, 0.2)):
                    continue
                chunk = os.read(process.stdout.fileno(), min(8192, MAX_OUTPUT + 1 - len(output)))
                if not chunk:
                    code = process.wait(timeout=max(0.01, deadline - time.monotonic()))
                    return ('ok' if code == 0 else 'command_failed'), bytes(output)
                output.extend(chunk)
                if len(output) > MAX_OUTPUT:
                    return 'command_output_too_large', b''
    except subprocess.TimeoutExpired:
        return 'command_timeout', b''
    except (OSError, ValueError):
        return 'command_unavailable', b''
    finally:
        if process is not None:
            if process.poll() is None:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                pass
            if process.stdout is not None:
                process.stdout.close()


def sanitized_report(value):
    """Allowlisted metadata only, even if container output is unexpected."""
    if not isinstance(value, dict):
        return {'status': 'invalid_response'}
    status = value.get('status')
    if not isinstance(status, str) or status not in {
        'complete', 'skipped_proxy_configuration', 'probe_failed',
    }:
        return {'status': 'invalid_response'}
    result = {'status': status, 'proxy_configured': value.get('proxy_configured') is True}
    for field, limit, statuses in (
        ('checks', 6, {'connected', 'failed', 'timeout'}),
        ('handshakes', 2, {'verified', 'timeout', 'module_unavailable',
                           'security_error', 'transport_error', 'protocol_error'}),
    ):
        source = value.get(field, [])
        result[field] = []
        if not isinstance(source, list):
            continue
        for item in source[:limit]:
            if not isinstance(item, dict):
                continue
            label, item_status = item.get('endpoint'), item.get('status')
            if not isinstance(label, str) or label not in ENDPOINT_LABELS:
                continue
            if not isinstance(item_status, str) or item_status not in statuses:
                continue
            clean = {'endpoint': label, 'status': item_status}
            elapsed = item.get('elapsed_ms')
            if type(elapsed) is int and 0 <= elapsed <= 120000:
                clean['elapsed_ms'] = elapsed
            error = item.get('error')
            if isinstance(error, str) and error in _tcp['TCP_ERRORS']:
                clean['error'] = error
            if field == 'handshakes':
                clean['transport'] = 'tcp_full'
            result[field].append(clean)
    return result


def diagnose():
    started = time.monotonic()
    report = {
        'diagnostic': 'telegram_routes_v1',
        'scope': 'transport_only_no_account_authentication',
        'host': {'status': 'not_checked'},
        'worker': {'status': 'not_checked'},
    }
    if os.geteuid() != 0:
        report['status'] = 'run_on_server_as_root'
        return report
    if _tcp['proxy_configured']():
        report['status'] = 'skipped_proxy_configuration'
        report['host'] = report['worker'] = {'status': 'skipped_proxy_configuration'}
        return report
    required = [PILOT / '.env.pilot', PILOT / 'compose.pilot.yaml']
    if PILOT.is_symlink() or not PILOT.is_dir() or any(
        path.is_symlink() or not path.is_file() for path in required
    ):
        report['status'] = 'existing_pilot_required'
        return report

    environment = dict(os.environ)
    for name in (
        'LOCAL_PORT', 'DATABASE_URL', 'ENCRYPTION_KEY', 'JWT_SECRET',
        'WAHA_API_KEY', 'POSTGRES_PASSWORD', 'SMS_GATEWAY_URL', 'WAHA_IMAGE',
    ):
        environment.pop(name, None)
    context = environment.get('DOCKER_CONTEXT')
    if context or not environment.get('DOCKER_HOST'):
        args = ['docker', 'context', 'inspect']
        if context:
            args.append(context)
        args.extend(['--format', '{{.Endpoints.docker.Host}}'])
        state, output = command(args, timeout=4, environment=environment)
        if state != 'ok':
            report['status'] = state
            return report
        docker_host = output.decode('utf-8', errors='replace').strip()
    else:
        docker_host = environment['DOCKER_HOST']
    if not docker_host.startswith('unix://'):
        report['status'] = 'local_docker_required'
        return report

    report['host'] = sanitized_report(_tcp['tcp_probe']())
    remaining = TOTAL_SECONDS - (time.monotonic() - started)
    if remaining <= 0:
        report['worker'] = {'status': 'command_timeout'}
    else:
        args = [
            'docker', 'compose', '--project-name', 'omnicrm-pilot',
            '--env-file', str(PILOT / '.env.pilot'),
            '-f', str(PILOT / 'compose.pilot.yaml'), 'exec', '-T',
            '-e', 'PYTHONDONTWRITEBYTECODE=1', 'worker', 'python', '-c', WORKER_PROBE,
        ]
        state, output = command(args, timeout=min(45, remaining), environment=environment)
        if state != 'ok':
            report['worker'] = {'status': state}
        else:
            try:
                report['worker'] = sanitized_report(json.loads(output))
            except (ValueError, UnicodeError):
                report['worker'] = {'status': 'invalid_response'}
    report['status'] = 'complete'
    return report


def main():
    try:
        report = diagnose()
    except Exception:
        report = {'diagnostic': 'telegram_routes_v1', 'status': 'diagnostic_unavailable'}
    print(json.dumps(report, ensure_ascii=True, indent=2, sort_keys=True))


if __name__ == '__main__':
    main()
