#!/usr/bin/env python3
"""Compare fixed messenger transports directly and through temporary Mac SOCKS.

Run on the server while OpenSSH reverse dynamic forwarding exposes only
127.0.0.1:19187. No account authentication, messages, session changes or writes.
Standard library only; Python 3.10+. DNS/proxy/HTTP details are never printed.
"""
import base64
import errno
import hashlib
import hmac
import json
import os
import queue
import signal
import socket
import ssl
import struct
import threading
import time
from datetime import datetime, timezone

WA_HOST = 'web.whatsapp.com'
TG_HOST = '149.154.167.51'
SOCKS_ADDRESS = ('127.0.0.1', 19187)
ROUTES = ('server_direct_ipv4', 'via_mac_socks5')
ROUNDS = 3
PROBE_SECONDS = 5
PACKET_SECONDS = 55
HEADER_LIMIT = 4096
GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'


class ProbeError(Exception):
    def __init__(self, status, http_status=None):
        self.status = status
        self.http_status = http_status


def remaining(deadline):
    value = deadline - time.monotonic()
    if value <= 0:raise TimeoutError
    return value


def read_exact(sock, size, deadline):
    if not 0 <= size <= 4096:raise ProbeError('protocol_error')
    data = bytearray()
    while len(data) < size:
        sock.settimeout(remaining(deadline))
        part = sock.recv(size - len(data))
        if not part:raise ProbeError('peer_closed')
        data.extend(part)
    return bytes(data)


def send(sock, data, deadline):
    sock.settimeout(remaining(deadline))
    sock.sendall(data)


def resolve_whatsapp_ipv4(deadline):
    # A daemon resolver cannot extend process lifetime if libc DNS stalls.
    result = queue.Queue(maxsize=1)
    def lookup():
        try:result.put((True, socket.getaddrinfo(WA_HOST, 443, socket.AF_INET, socket.SOCK_STREAM)))
        except Exception:result.put((False, None))
    threading.Thread(target=lookup, daemon=True).start()
    try:ok, addresses = result.get(timeout=remaining(deadline))
    except queue.Empty:raise TimeoutError from None
    if not ok:raise ProbeError('dns_error')
    for family, kind, _protocol, _name, address in addresses:
        if family == socket.AF_INET and kind == socket.SOCK_STREAM and address[1] == 443:
            return address
    raise ProbeError('dns_error')


def socks_handshake(sock, host, deadline):
    if host not in {WA_HOST, TG_HOST}:raise ProbeError('endpoint_not_allowed')
    send(sock, b'\x05\x01\x00', deadline)
    greeting = read_exact(sock, 2, deadline)
    if greeting != b'\x05\x00':raise ProbeError('socks_authentication_unavailable' if greeting[:1] == b'\x05' else 'socks_protocol_error')
    # WA hostname is resolved by the Mac; Telegram is one fixed IPv4 endpoint.
    destination = b'\x03' + bytes([len(host)]) + host.encode('ascii') if host == WA_HOST else b'\x01' + socket.inet_aton(TG_HOST)
    send(sock, b'\x05\x01\x00' + destination + struct.pack('!H', 443), deadline)
    response = read_exact(sock, 4, deadline)
    if response[0] != 5 or response[2] != 0:raise ProbeError('socks_protocol_error')
    if response[1] != 0:
        errors = {1:'socks_general_failure',2:'socks_connection_not_allowed',3:'socks_network_unreachable',4:'socks_host_unreachable',5:'socks_connection_refused',6:'socks_ttl_expired',7:'socks_command_not_supported',8:'socks_address_type_not_supported'}
        raise ProbeError(errors.get(response[1], 'socks_protocol_error'))
    if response[3] == 1:size = 4
    elif response[3] == 4:size = 16
    elif response[3] == 3:
        size = read_exact(sock, 1, deadline)[0]
        if not size:raise ProbeError('socks_protocol_error')
    else:raise ProbeError('socks_protocol_error')
    read_exact(sock, size + 2, deadline)  # Do not expose the proxy's bound address.


def websocket_upgrade(sock, deadline):
    key = base64.b64encode(os.urandom(16)).decode('ascii')
    request = ('GET /ws/chat HTTP/1.1\r\nHost: web.whatsapp.com\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: ' + key + '\r\nSec-WebSocket-Version: 13\r\nOrigin: https://web.whatsapp.com\r\n\r\n').encode('ascii')
    send(sock, request, deadline)
    data = bytearray()
    while b'\r\n\r\n' not in data:
        sock.settimeout(remaining(deadline))
        part = sock.recv(HEADER_LIMIT + 1 - len(data))
        if not part:raise ProbeError('peer_closed')
        data.extend(part)
        if len(data) > HEADER_LIMIT:raise ProbeError('http_headers_too_large')
    header = bytes(data).split(b'\r\n\r\n', 1)[0]
    lines = header.decode('iso-8859-1').split('\r\n')
    status = lines[0].split(' ', 2)
    if len(status) < 2 or status[0] != 'HTTP/1.1' or not status[1].isdigit():raise ProbeError('invalid_http_response')
    code = int(status[1])
    if not 100 <= code <= 599:raise ProbeError('invalid_http_response')
    if code != 101:raise ProbeError('http_rejected', code)
    headers = {}
    for line in lines[1:]:
        name, separator, value = line.partition(':')
        if not separator or not name or name[0].isspace():raise ProbeError('invalid_http_response')
        name = name.strip().lower()
        if name in {'upgrade','connection','sec-websocket-accept'}:
            if name in headers:raise ProbeError('invalid_http_response')
            headers[name] = value.strip()
    expected = base64.b64encode(hashlib.sha1((key + GUID).encode('ascii')).digest()).decode('ascii')
    if headers.get('upgrade','').lower() != 'websocket' or 'upgrade' not in {part.strip().lower() for part in headers.get('connection','').split(',')}:
        raise ProbeError('websocket_upgrade_invalid')
    if not hmac.compare_digest(headers.get('sec-websocket-accept',''), expected):raise ProbeError('websocket_accept_invalid')
    # No WebSocket frames, Noise/MTProto, pairing or account payload are sent.


def probe(route, endpoint, packet_deadline):
    started = time.monotonic();deadline = min(started + PROBE_SECONDS, packet_deadline)
    result = {'status':'unavailable','elapsed_ms':0}
    sock = None
    try:
        if route not in ROUTES or endpoint not in {'whatsapp_wss','telegram_dc2_tcp'}:raise ProbeError('endpoint_not_allowed')
        host = WA_HOST if endpoint == 'whatsapp_wss' else TG_HOST
        address = SOCKS_ADDRESS if route == 'via_mac_socks5' else resolve_whatsapp_ipv4(deadline) if host == WA_HOST else (TG_HOST,443)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(remaining(deadline));sock.connect(address)
        if route == 'via_mac_socks5':socks_handshake(sock, host, deadline)
        if endpoint == 'whatsapp_wss':
            context = ssl.create_default_context()
            sock.settimeout(remaining(deadline))
            sock = context.wrap_socket(sock, server_hostname=WA_HOST)
            result['tls_verified'] = True
            websocket_upgrade(sock, deadline)
            result['status'] = 'websocket_open'
        else:result['status'] = 'tcp_connected'
    except ProbeError as exc:
        result['status'] = exc.status
        if exc.http_status is not None:result['http_status'] = exc.http_status
    except (TimeoutError, socket.timeout):result['status'] = 'timeout'
    except ssl.SSLCertVerificationError:result['status'] = 'tls_certificate_error'
    except ssl.SSLError:result['status'] = 'tls_error'
    except OSError as exc:
        result['status'] = {errno.ECONNREFUSED:'connection_refused',errno.ECONNRESET:'connection_reset',errno.ENETUNREACH:'network_unreachable',errno.EHOSTUNREACH:'host_unreachable',errno.EACCES:'permission_denied',errno.EPERM:'permission_denied'}.get(exc.errno,'network_error')
    except Exception:result['status'] = 'probe_error'
    finally:
        if sock is not None:
            try:sock.close()
            except Exception:pass
    result['elapsed_ms'] = min(65000,max(0,int((time.monotonic()-started)*1000)))
    return result


def diagnose():
    deadline = time.monotonic() + PACKET_SECONDS
    result = {'diagnostic':'messenger_mac_route_readonly_v1','result':'complete','captured_at_utc':datetime.now(timezone.utc).isoformat(timespec='seconds'),'scope':'tcp_and_tls_websocket_transport_only_not_account_authentication','whatsapp':{route:{'successes':0,'checks':[]} for route in ROUTES},'telegram_dc2_tcp':{}}
    for round_number in range(1,ROUNDS+1):
        for route in ROUTES:
            check = probe(route,'whatsapp_wss',deadline)
            result['whatsapp'][route]['checks'].append(dict(check,round=round_number))
            result['whatsapp'][route]['successes'] += check['status'] == 'websocket_open'
    for route in ROUTES:result['telegram_dc2_tcp'][route] = probe(route,'telegram_dc2_tcp',deadline)
    if time.monotonic() >= deadline:result['result'] = 'packet_timeout'
    return result


def main():
    def deadline(*_):raise TimeoutError
    signal.signal(signal.SIGALRM,deadline);signal.alarm(65)
    try:result = diagnose()
    except Exception:result = {'diagnostic':'messenger_mac_route_readonly_v1','result':'probe_unavailable'}
    finally:signal.alarm(0)
    print(json.dumps(result,indent=2,ensure_ascii=True))
    return 0 if result.get('result') == 'complete' else 1


if __name__ == '__main__':raise SystemExit(main())
