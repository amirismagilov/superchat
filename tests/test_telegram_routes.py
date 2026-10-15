"""Transport-only route diagnostics never open a user session or expose secrets."""
import asyncio
import contextlib
import errno
import importlib.util
import io
import json
import logging
import os
from pathlib import Path
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('telegram_routes', ROOT / 'scripts/probe_telegram_routes.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def worker_namespace():
    namespace = {}
    source = diag.TCP_PROBE + diag.HANDSHAKE_PROBE.rsplit('\nworker_main()', 1)[0]
    with patch.object(logging, 'disable'):
        exec(compile(source, '<worker-probe-test>', 'exec'), namespace)
    return namespace


class TelegramRouteTests(unittest.TestCase):
    def test_official_fixed_addresses_only_and_bounded_socket_failure(self):
        self.assertEqual({item[1] for item in diag._tcp['ENDPOINTS']}, {'149.154.167.51', '95.161.76.100'})
        self.assertEqual({item[2] for item in diag._tcp['ENDPOINTS']}, {80, 443, 5222})
        self.assertEqual(len(diag._tcp['ENDPOINTS']), 6)
        sock = Mock()
        sock.connect.side_effect = OSError(errno.ENETUNREACH, 'SECRET_CONNECTION_DETAILS')
        with patch.object(diag._tcp['socket'], 'socket', return_value=sock):
            result = diag._tcp['tcp_one'](diag._tcp['ENDPOINTS'][0])
        sock.settimeout.assert_called_once_with(4)
        sock.connect.assert_called_once_with(('149.154.167.51', 443))
        sock.close.assert_called_once_with()
        self.assertEqual(result['error'], 'ENETUNREACH')
        self.assertNotIn('SECRET', json.dumps(result))

    def test_proxy_configuration_prevents_any_direct_probe(self):
        with patch.dict(os.environ, {'ALL_PROXY': 'socks5://SECRET_PROXY'}, clear=True), patch.object(diag.os, 'geteuid', return_value=0), patch.object(diag, 'command') as command, patch.object(diag._tcp['socket'], 'socket') as socket:
            report = diag.diagnose()
            tcp = diag._tcp['tcp_probe']()
        command.assert_not_called()
        socket.assert_not_called()
        self.assertEqual(report['status'], 'skipped_proxy_configuration')
        self.assertEqual(tcp['status'], 'skipped_proxy_configuration')
        self.assertNotIn('SECRET', json.dumps(report))

    def test_container_json_cannot_leak_keys_phone_sessions_or_error_text(self):
        entry = {'endpoint': 'dc2_primary_443', 'status': 'connected', 'elapsed_ms': 8, 'error': 'SECRET_ERROR', 'key': 'SECRET_KEY'}
        result = diag.sanitized_report({
            'status': 'complete', 'proxy_configured': 'SECRET_PROXY', 'phone': 'SECRET_PHONE',
            'checks': [entry] * 20,
            'handshakes': [{'endpoint': 'dc2_alternate_443', 'status': 'verified', 'transport': 'SECRET', 'session': 'SECRET_SESSION'}] * 9,
        })
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(result['checks']), 6)
        self.assertEqual(len(result['handshakes']), 2)
        self.assertEqual(result['handshakes'][0]['transport'], 'tcp_full')
        self.assertEqual(diag.sanitized_report({'status': {}})['status'], 'invalid_response')
        self.assertEqual(diag.sanitized_report({'status': 'complete', 'checks': [{'endpoint': {}, 'status': 'connected'}]})['checks'], [])

    def test_subprocess_has_empty_stdin_and_discards_stderr(self):
        status, output = diag.command([sys.executable, '-c', 'import sys; print(repr(sys.stdin.read())); print("SECRET_STDERR", file=sys.stderr)'], timeout=2)
        self.assertEqual(status, 'ok')
        self.assertEqual(output, b"''\n")

    def test_subprocess_output_and_runtime_are_bounded(self):
        with patch.object(diag, 'MAX_OUTPUT', 1024):
            state, output = diag.command([sys.executable, '-c', 'print("SECRET" * 5000)'], timeout=2)
        self.assertEqual((state, output), ('command_output_too_large', b''))
        start = time.monotonic()
        state, output = diag.command([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=0.05)
        self.assertEqual((state, output), ('command_timeout', b''))
        self.assertLess(time.monotonic() - start, 1.5)

    def test_host_executes_existing_worker_only_with_safe_output(self):
        calls = []
        worker = {'status': 'complete', 'checks': [], 'key': 'SECRET_TRANSPORT_KEY'}
        def command(args, **kwargs):
            calls.append((args, kwargs))
            if args[1:3] == ['context', 'inspect']:
                return 'ok', b'unix:///var/run/docker.sock\n'
            return 'ok', json.dumps(worker).encode()
        with tempfile.TemporaryDirectory() as folder:
            pilot = Path(folder)
            (pilot / '.env.pilot').write_text('SECRET_ENV_NEVER_READ')
            (pilot / 'compose.pilot.yaml').write_text('services: {}')
            with patch.object(diag, 'PILOT', pilot), patch.dict(os.environ, {'DATABASE_URL': 'SECRET_DATABASE', 'WAHA_API_KEY': 'SECRET_API'}, clear=True), patch.object(diag.os, 'geteuid', return_value=0), patch.object(diag, 'command', side_effect=command), patch.dict(diag._tcp, {'tcp_probe': lambda: {'status': 'complete', 'checks': []}}), patch.object(Path, 'read_text', side_effect=AssertionError('No session or env reads')):
                report = diag.diagnose()
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(len(calls), 2)
        argv, options = calls[1]
        self.assertEqual(argv[-4:], ['worker', 'python', '-c', diag.WORKER_PROBE])
        self.assertIn('exec', argv)
        self.assertIn('-T', argv)
        self.assertNotIn('DATABASE_URL', options['environment'])
        self.assertNotIn('WAHA_API_KEY', options['environment'])
        self.assertLessEqual(options['timeout'], 45)
        self.assertNotIn('SECRET', json.dumps(report))
        compile(argv[-1], '<streamed-worker-probe>', 'exec')

    def test_remote_docker_rejected_before_host_probe(self):
        with tempfile.TemporaryDirectory() as folder:
            pilot = Path(folder)
            (pilot / '.env.pilot').touch()
            (pilot / 'compose.pilot.yaml').touch()
            host_probe = Mock()
            with patch.object(diag, 'PILOT', pilot), patch.dict(os.environ, {'DOCKER_HOST': 'tcp://SECRET_HOST'}, clear=True), patch.object(diag.os, 'geteuid', return_value=0), patch.object(diag, 'command') as command, patch.dict(diag._tcp, {'tcp_probe': host_probe}):
                result = diag.diagnose()
        self.assertEqual(result['status'], 'local_docker_required')
        command.assert_not_called()
        host_probe.assert_not_called()
        self.assertNotIn('SECRET', json.dumps(result))

    def test_worker_negotiates_only_ephemeral_transport_key_and_closes_connection(self):
        from telethon.network import authenticator, connection
        from telethon import TelegramClient
        namespace = worker_namespace()
        transport = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
        exchange = AsyncMock(return_value=(SimpleNamespace(key=b'SECRET_KEY'), 0))
        with patch.object(connection, 'ConnectionTcpFull', return_value=transport) as factory, patch.object(authenticator, 'do_authentication', exchange), patch.object(TelegramClient, '__init__', side_effect=AssertionError('Never create an account client')):
            result = asyncio.run(namespace['handshake_one'](namespace['ENDPOINTS'][0]))
        self.assertEqual(result['status'], 'verified')
        self.assertEqual(factory.call_args.args, ('149.154.167.51', 443, 2))
        transport.connect.assert_awaited_once_with(timeout=4)
        exchange.assert_awaited_once()
        transport.disconnect.assert_awaited_once_with()
        self.assertNotIn('SECRET', json.dumps(result))

    def test_worker_handshake_timeout_closes_without_retrying_or_exposing_error(self):
        from telethon.network import authenticator, connection
        namespace = worker_namespace()
        transport = SimpleNamespace(connect=AsyncMock(), disconnect=AsyncMock())
        exchange = AsyncMock(side_effect=TimeoutError('SECRET_REMOTE_MESSAGE'))
        with patch.object(connection, 'ConnectionTcpFull', return_value=transport), patch.object(authenticator, 'do_authentication', exchange):
            result = asyncio.run(namespace['handshake_one'](namespace['ENDPOINTS'][0]))
        self.assertEqual(result['status'], 'timeout')
        exchange.assert_awaited_once()
        transport.disconnect.assert_awaited_once_with()
        self.assertNotIn('SECRET', json.dumps(result))

    def test_worker_negotiates_at_most_two_tcp_reachable_routes(self):
        namespace = worker_namespace()
        handshake = AsyncMock(return_value={'status': 'verified'})
        namespace['handshake_one'] = handshake
        checks = [{'endpoint': endpoint[0], 'status': 'connected'} for endpoint in namespace['ENDPOINTS']]
        result = asyncio.run(namespace['handshake_routes']({'checks': checks}))
        self.assertEqual(handshake.await_count, 2)
        self.assertEqual([call.args[0][0] for call in handshake.await_args_list], ['dc2_primary_443', 'dc2_alternate_443'])
        self.assertEqual(len(result['handshakes']), 2)

    def test_worker_proxy_skips_handshake_and_has_independent_process_deadline(self):
        namespace = worker_namespace()
        namespace['handshake_routes'] = AsyncMock()
        output = io.StringIO()
        with patch.dict(os.environ, {'TELEGRAM_PROXY': 'SECRET_PROXY'}, clear=True), patch.object(namespace['signal'], 'alarm') as alarm, contextlib.redirect_stdout(output):
            namespace['worker_main']()
        namespace['handshake_routes'].assert_not_called()
        self.assertEqual([call.args[0] for call in alarm.call_args_list], [42, 0])
        self.assertEqual(json.loads(output.getvalue())['status'], 'skipped_proxy_configuration')
        self.assertNotIn('SECRET', output.getvalue())

    def test_top_level_failure_never_exposes_exception_text(self):
        output = io.StringIO()
        with patch.object(diag, 'diagnose', side_effect=RuntimeError('SECRET_DETAILS')), contextlib.redirect_stdout(output):
            diag.main()
        self.assertNotIn('SECRET', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['status'], 'diagnostic_unavailable')


if __name__ == '__main__':
    unittest.main()
