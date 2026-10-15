#!/usr/bin/env python3
"""Bounded official Telegram WSS/MTProto probe; run inside the existing worker.

Self-contained source can also be passed to ``python -c`` or streamed on stdin.
Only two fixed DC2 endpoints are contacted. No API id/hash, account session,
phone number, login code or database is read. Negotiated transport keys stay in
memory and are discarded; no user-account authorization is attempted.

Protocol and generic DC2 domain:
https://core.telegram.org/mtproto/transports#websocket
https://core.telegram.org/mtproto/mtproto-transports#transport-obfuscation
Official Web K endpoint (App.suffix='K', DC2, client connection):
https://github.com/morethanwords/tweb/blob/243eaee8b4fb69c0e6d8bac8a3a93eb77f94abe1/src/lib/mtproto/dcConfigurator.ts
No Origin is required by the transport documentation, so none is fabricated.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import os
import signal
import socket
import ssl
import time

ENDPOINTS = (
    ('dc2_kws', 'kws2.web.telegram.org'),
    ('dc2_venus', 'venus.web.telegram.org'),
)
MAX_BYTES = 4 * 1024 * 1024
ENDPOINT_TIMEOUT = 10
PROXY_KEYS = (
    'HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'WS_PROXY', 'WSS_PROXY',
    'http_proxy', 'https_proxy', 'all_proxy', 'ws_proxy', 'wss_proxy',
    'TELEGRAM_PROXY_URL', 'TELEGRAM_PROXY', 'TELEGRAM_SOCKS_PROXY', 'TELEGRAM_MTPROXY',
)


def proxy_configured():
    return any(os.environ.get(name) for name in PROXY_KEYS)


async def open_websocket(host):
    from websockets.asyncio.client import connect

    class FixedEndpointConnect(connect):
        def process_redirect(self, exc):
            # Never follow a provider redirect to any other host or URI.
            return exc

    return await FixedEndpointConnect(
        'wss://' + host + '/apiws',
        ssl=ssl.create_default_context(), subprotocols=['binary'],
        origin=None, proxy=None, compression=None,
        open_timeout=5, close_timeout=1, ping_interval=None,
        max_size=MAX_BYTES, max_queue=2, family=socket.AF_INET,
    )


class ObfuscatedWebSocket:
    """MTProto send/recv over one continuous binary WebSocket byte stream."""
    def __init__(self, websocket):
        from telethon.network.connection.tcpabridged import AbridgedPacketCodec
        from telethon.network.connection.tcpobfuscated import ObfuscatedIO

        self.websocket = websocket
        self.codec = AbridgedPacketCodec(self)
        self.header, self.encryptor, self.decryptor = ObfuscatedIO.init_header(AbridgedPacketCodec)
        self.buffer = bytearray()

    async def initialize(self):
        await self.websocket.send(bytes(self.header))

    async def send(self, data):
        if not isinstance(data, bytes) or not data or len(data) > MAX_BYTES or len(data) % 4:
            raise ValueError('invalid_transport_payload')
        await self.websocket.send(self.encryptor.encrypt(self.codec.encode_packet(data)))

    async def readexactly(self, size):
        if type(size) is not int or size < 0 or size > MAX_BYTES:
            raise ValueError('invalid_transport_length')
        while len(self.buffer) < size:
            frame = await self.websocket.recv()
            if not isinstance(frame, bytes) or len(self.buffer) + len(frame) > MAX_BYTES:
                raise ValueError('invalid_websocket_payload')
            self.buffer.extend(self.decryptor.encrypt(frame))
        result = bytes(self.buffer[:size])
        del self.buffer[:size]
        return result

    async def recv(self):
        return await self.codec.read_packet(self)


async def probe_endpoint(label, host):
    result = {'endpoint': label, 'status': 'protocol_error', 'wss_open': False, 'mtproto_verified': False}
    websocket = None
    started = time.monotonic()
    try:
        from telethon.errors import SecurityError
        from telethon.network.authenticator import do_authentication
        from telethon.network.mtprotoplainsender import MTProtoPlainSender
        from websockets.exceptions import InvalidStatus

        async with asyncio.timeout(ENDPOINT_TIMEOUT):
            websocket = await open_websocket(host)
            if websocket.subprotocol != 'binary':
                raise ValueError('missing_binary_subprotocol')
            result['wss_open'] = True
            transport = ObfuscatedWebSocket(websocket)
            await transport.initialize()
            logs = collections.defaultdict(lambda: logging.getLogger('telegram-wss-probe'))
            # The existing Telethon authenticator checks the official RSA keys,
            # nonces and DH values. This does not authorize any user account.
            key, _offset = await do_authentication(MTProtoPlainSender(transport, loggers=logs))
            if not key or not key.key:
                raise SecurityError('empty_key')
            result.update(status='mtproto_verified', mtproto_verified=True)
            del key
    except TimeoutError:
        result['status'] = 'timeout'
    except ImportError:
        result['status'] = 'module_unavailable'
    except ssl.SSLError:
        result['status'] = 'tls_error'
    except socket.gaierror:
        result['status'] = 'dns_error'
    except OSError:
        result['status'] = 'network_error'
    except Exception as exc:
        # Import failures were already handled; inspect known classes without
        # ever printing provider exception text or close reasons.
        name = type(exc).__name__
        if name == 'InvalidStatus':
            result['status'] = 'http_rejected'
            code = getattr(getattr(exc, 'response', None), 'status_code', None)
            if type(code) is int and 100 <= code <= 599:
                result['http_status'] = code
        elif name in {'SecurityError', 'AssertionError'}:
            result['status'] = 'security_error'
        else:
            result['status'] = 'protocol_error'
    finally:
        if websocket is not None:
            try:
                await asyncio.wait_for(websocket.close(), timeout=1)
            except Exception:
                pass
    result['elapsed_ms'] = min(26000, max(0, int((time.monotonic() - started) * 1000)))
    return result


async def diagnose():
    if proxy_configured():
        return {'status': 'skipped_proxy_configuration', 'proxy_configured': True, 'checks': []}
    checks = []
    for label, host in ENDPOINTS:
        checks.append(await probe_endpoint(label, host))
    return {'status': 'complete', 'proxy_configured': False, 'checks': checks}


def main():
    logging.disable(logging.CRITICAL)
    # Bound even CPU-heavy key exchange or a lost docker-exec client.
    signal.alarm(26)
    try:
        result = asyncio.run(diagnose())
    except Exception:
        result = {'status': 'probe_failed', 'checks': []}
    finally:
        signal.alarm(0)
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
