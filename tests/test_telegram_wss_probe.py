"""No external sockets: exercise WS framing, transport authentication and bounds."""
import asyncio
import contextlib
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import ssl
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('telegram_wss_probe', ROOT / 'scripts/probe_telegram_wss.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def socket_double():
    return SimpleNamespace(subprotocol='binary', send=AsyncMock(), recv=AsyncMock(), close=AsyncMock())


class TelegramWssProbeTests(unittest.TestCase):
    def test_real_obfuscation_handles_split_packets_and_multiple_packets_in_one_frame(self):
        from telethon.crypto import AESModeCTR
        websocket = socket_double()
        transport = diag.ObfuscatedWebSocket(websocket)
        reverse = bytes(transport.header[55:7:-1])
        server_encryptor = AESModeCTR(reverse[:32], reverse[32:])
        first = b'A' * 512
        second = b'B' * 12
        stream = server_encryptor.encrypt(transport.codec.encode_packet(first) + transport.codec.encode_packet(second))
        # Abridged four-byte length itself crosses WS boundaries; the last frame
        # combines the end of packet one with all of packet two.
        websocket.recv.side_effect = [stream[:1], stream[1:3], stream[3:200], stream[200:]]
        async def receive():
            self.assertEqual(await transport.recv(), first)
            self.assertEqual(await transport.recv(), second)
        asyncio.run(receive())
        self.assertEqual(transport.buffer, b'')
        self.assertEqual(websocket.recv.await_count, 4)

    def test_outgoing_header_and_packets_keep_continuous_ctr_counter(self):
        from telethon.crypto import AESModeCTR
        websocket = socket_double()
        transport = diag.ObfuscatedWebSocket(websocket)
        server_decryptor = AESModeCTR(bytes(transport.header[8:40]), bytes(transport.header[40:56]))
        async def send():
            await transport.initialize()
            await transport.send(b'abcd')
            await transport.send(b'efghijkl')
        asyncio.run(send())
        frames = [call.args[0] for call in websocket.send.await_args_list]
        self.assertEqual(len(frames[0]), 64)
        server_decryptor.encrypt(frames[0])  # Header consumes the first 64 CTR bytes.
        self.assertEqual(server_decryptor.encrypt(frames[1]), b'\x01abcd')
        self.assertEqual(server_decryptor.encrypt(frames[2]), b'\x02efghijkl')

    def test_length_and_frame_limits_reject_unbounded_or_text_data(self):
        websocket = socket_double()
        transport = diag.ObfuscatedWebSocket(websocket)
        async def check():
            with self.assertRaises(ValueError):
                await transport.readexactly(diag.MAX_BYTES + 1)
            websocket.recv.assert_not_called()
            websocket.recv.return_value = 'SECRET_TEXT_FRAME'
            with self.assertRaises(ValueError):
                await transport.readexactly(1)
            websocket.recv.return_value = b'x' * 17
            with patch.object(diag, 'MAX_BYTES', 16), self.assertRaises(ValueError):
                await transport.readexactly(1)
        asyncio.run(check())

    def test_connection_requires_tls_binary_and_rejects_redirects_without_origin_spoofing(self):
        from websockets.asyncio import client
        connections = []
        websocket = socket_double()
        class FakeConnect:
            def __init__(self, uri, **kwargs):
                connections.append((self, uri, kwargs))
            def __await__(self):
                async def opened():return websocket
                return opened().__await__()
        with patch.object(client, 'connect', FakeConnect):
            result = asyncio.run(diag.open_websocket('kws2.web.telegram.org'))
        self.assertIs(result, websocket)
        connection, uri, options = connections[0]
        self.assertEqual(uri, 'wss://kws2.web.telegram.org/apiws')
        self.assertEqual(options['subprotocols'], ['binary'])
        self.assertIsNone(options['origin'])
        self.assertIsNone(options['proxy'])
        self.assertTrue(options['ssl'].check_hostname)
        self.assertEqual(options['ssl'].verify_mode, ssl.CERT_REQUIRED)
        self.assertEqual(options['max_size'], 4 * 1024 * 1024)
        redirect = RuntimeError('SECRET_REDIRECT')
        self.assertIs(connection.process_redirect(redirect), redirect)

    def test_success_uses_only_transport_authenticator_and_discards_auth_key(self):
        from telethon import TelegramClient
        from telethon.network import authenticator
        websocket = socket_double()
        exchange = AsyncMock(return_value=(SimpleNamespace(key=b'SECRET_TRANSPORT_KEY'), 0))
        with patch.object(diag, 'open_websocket', AsyncMock(return_value=websocket)), patch.object(authenticator, 'do_authentication', exchange), patch.object(TelegramClient, '__init__', side_effect=AssertionError('No user account client')):
            result = asyncio.run(diag.probe_endpoint(*diag.ENDPOINTS[0]))
        self.assertEqual(result['status'], 'mtproto_verified')
        self.assertIs(result['wss_open'], True)
        self.assertIs(result['mtproto_verified'], True)
        self.assertIsInstance(exchange.await_args.args[0]._connection, diag.ObfuscatedWebSocket)
        websocket.close.assert_awaited_once_with()
        self.assertNotIn('SECRET', json.dumps(result))

    def test_handshake_timeout_distinguished_from_opening_timeout_and_closes(self):
        from telethon.network import authenticator
        websocket = socket_double()
        async def stalled(*args):await asyncio.sleep(100)
        started = time.monotonic()
        with patch.object(diag, 'ENDPOINT_TIMEOUT', 0.01), patch.object(diag, 'open_websocket', AsyncMock(return_value=websocket)), patch.object(authenticator, 'do_authentication', stalled):
            result = asyncio.run(diag.probe_endpoint(*diag.ENDPOINTS[0]))
        self.assertLess(time.monotonic() - started, 1)
        self.assertEqual(result['status'], 'timeout')
        self.assertTrue(result['wss_open'])
        self.assertFalse(result['mtproto_verified'])
        websocket.close.assert_awaited_once_with()
        with patch.object(diag, 'open_websocket', AsyncMock(side_effect=TimeoutError('SECRET_ERROR'))):
            result = asyncio.run(diag.probe_endpoint(*diag.ENDPOINTS[0]))
        self.assertEqual(result['status'], 'timeout')
        self.assertFalse(result['wss_open'])
        self.assertNotIn('SECRET', json.dumps(result))

    def test_tls_error_and_http_rejection_return_metadata_only(self):
        from websockets.exceptions import InvalidStatus
        for error, status in (
            (ssl.SSLCertVerificationError('SECRET_CERT_DETAILS'), 'tls_error'),
            (InvalidStatus(SimpleNamespace(status_code=403, reason_phrase='SECRET', headers={'cookie': 'SECRET'})), 'http_rejected'),
        ):
            with self.subTest(status=status), patch.object(diag, 'open_websocket', AsyncMock(side_effect=error)):
                result = asyncio.run(diag.probe_endpoint(*diag.ENDPOINTS[0]))
            self.assertEqual(result['status'], status)
            self.assertFalse(result['wss_open'])
            self.assertNotIn('SECRET', json.dumps(result))
            if status == 'http_rejected':self.assertEqual(result['http_status'], 403)

    def test_missing_binary_subprotocol_cannot_be_verified(self):
        from telethon.network import authenticator
        websocket = socket_double()
        websocket.subprotocol = None
        with patch.object(diag, 'open_websocket', AsyncMock(return_value=websocket)), patch.object(authenticator, 'do_authentication', AsyncMock()) as exchange:
            result = asyncio.run(diag.probe_endpoint(*diag.ENDPOINTS[0]))
        self.assertEqual(result['status'], 'protocol_error')
        self.assertFalse(result['mtproto_verified'])
        exchange.assert_not_called()
        websocket.close.assert_awaited_once_with()

    def test_configured_proxy_skips_all_direct_calls_and_output_never_contains_proxy(self):
        with patch.dict(os.environ, {'wss_proxy': 'https://SECRET_CREDENTIAL@proxy'}, clear=True), patch.object(diag, 'probe_endpoint', AsyncMock()) as probe:
            result = asyncio.run(diag.diagnose())
        self.assertEqual(result['status'], 'skipped_proxy_configuration')
        probe.assert_not_called()
        self.assertNotIn('SECRET', json.dumps(result))

    def test_exactly_two_fixed_endpoints_and_independent_process_deadline(self):
        probe = AsyncMock(return_value={'status': 'mtproto_verified'})
        output = io.StringIO()
        with patch.dict(os.environ, {}, clear=True), patch.object(diag, 'probe_endpoint', probe), patch.object(diag.signal, 'alarm') as alarm, patch.object(logging, 'disable'), contextlib.redirect_stdout(output):
            diag.main()
        self.assertEqual(probe.await_count, 2)
        self.assertEqual([call.args for call in probe.await_args_list], [
            ('dc2_kws', 'kws2.web.telegram.org'), ('dc2_venus', 'venus.web.telegram.org'),
        ])
        self.assertEqual([call.args[0] for call in alarm.call_args_list], [26, 0])
        self.assertEqual(json.loads(output.getvalue())['status'], 'complete')


if __name__ == '__main__':
    unittest.main()
