"""Runtime packet must not disclose secrets even when Docker/provider output does."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('runtime_diagnostic', ROOT / 'scripts/diagnose_runtime.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class RuntimeDiagnosticTests(unittest.TestCase):
    def test_log_classification_never_returns_raw_lines(self):
        raw = b'SECRET_PHONE SECRET_SESSION SECRET_QR EAI_AGAIN\nconnection errored SECRET_TOKEN\n{"statusCode":403,"message":"SECRET_ERROR"}\nFailed to start the client SECRET_KEY\nSession stuck in STARTING status, force stopping the session. SECRET_SESSION\n'
        result = diag.classify_logs(raw)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['category_counts']['dns_error'], 1)
        self.assertEqual(result['category_counts']['http_403'], 1)
        self.assertEqual(result['category_counts']['noweb_start_failed'], 1)
        self.assertEqual(result['category_counts']['starting_watchdog'], 1)
        self.assertEqual(diag.classify_logs(b'line\n' * 1500)['lines_examined'], 1000)
        upstream = diag.classify_logs(b'Connecting using wa.version = 2.3000.1045649367 SECRET_TOKEN\nstatusCode:405\nstatusCode:408\nstatusCode:440\nstatusCode:515\n')
        self.assertEqual(upstream['category_counts']['socket_initialization_reached'], 1)
        for code in (405, 408, 440, 515):
            self.assertEqual(upstream['category_counts']['http_' + str(code)], 1)
        self.assertNotIn('SECRET', json.dumps(upstream))

    def test_container_inspect_is_explicit_and_output_filtered(self):
        calls = []
        class Runner:
            def compose(self, *args, **kwargs):return 'ok', b'a' * 64
            def capture(self, args, **kwargs):
                calls.append(args)
                return 'ok', json.dumps({'service':'api','state':'running','health':'healthy','restart_count':2,'oom_killed':False,'memory_limit_bytes':0,'Env':['SECRET_KEY'],'name':'SECRET_CONTAINER'}).encode()
        result = diag.containers(Runner())
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['services'][0]['restart_count'], 2)
        self.assertIn('--format', calls[0])
        self.assertNotIn('.Config.Env', calls[0][3])

    def test_probe_json_cannot_smuggle_errors_headers_addresses_or_credentials(self):
        output = json.dumps({'status':'complete','proxy_configured':False,'dns':{'status':'resolved','ipv4_records':2,'ipv6_records':1,'addresses':['SECRET_ADDRESS']},'wss_default':{'status':'http_rejected','http_status':403,'error_code':'SECRET_ERROR','headers':{'cookie':'SECRET_COOKIE'}},'https_ipv4':{'status':'responded','http_status':200,'tls_authorized':True,'body':'SECRET_HTML'},'extra':'SECRET_TOKEN'})
        result = diag.safe_probe(output, 'waha')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['wss_default']['http_status'], 403)
        self.assertEqual(result['https_ipv4']['tls_authorized'], True)
        output = json.dumps({'status':'complete','checks':[{'endpoint':'default_dc2_ipv4','status':'connected','elapsed_ms':20,'ip':'SECRET_IP'},{'endpoint':'SECRET_ENDPOINT','status':'connected'}]})
        result = diag.safe_probe(output, 'telegram')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(result['checks']), 1)

    def test_node_network_policy_only_exposes_known_settings(self):
        result = diag.safe_probe(json.dumps({'status': 'complete', 'proxy_configured': False,
            'dns_order': 'ipv4first', 'auto_select_family': False,
            'NODE_OPTIONS': 'SECRET_OPTION'}), 'waha')
        self.assertEqual(result['dns_order'], 'ipv4first')
        self.assertIs(result['auto_select_family'], False)
        self.assertNotIn('SECRET', json.dumps(result))
        result = diag.safe_probe(json.dumps({'status': 'complete',
            'dns_order': 'SECRET_VALUE', 'auto_select_family': 'SECRET_VALUE'}), 'waha')
        self.assertEqual(result['dns_order'], 'unknown')
        self.assertNotIn('auto_select_family', result)
        self.assertNotIn('SECRET', json.dumps(result))

    def test_nested_telegram_report_is_filtered_again(self):
        output = json.dumps({'result':'ok','accounts_total':1,'environment':{'DATABASE_URL':'SECRET_DATABASE'},'worker_heartbeat':{'status':'visible','age_seconds':4,'fresh':True,'path':'SECRET_PATH'},'accounts':[{'account_hash':'123456789abc','state':'connecting','public_error':'telegram_transport_error','updated_age_seconds':10,'phone':'SECRET_PHONE','qr_text':'SECRET_QR','jobs':[{'action':'login','status':'failed','error':'SECRET_RAW_EXCEPTION','age_seconds':2,'payload':'SECRET_PASSWORD'}]}]})
        result = diag.safe_telegram(output)
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['accounts'][0]['public_error'], 'telegram_transport_error')
        self.assertEqual(result['accounts'][0]['jobs'][0]['error'], 'unknown_error')

    def test_autoupdate_ignores_unexpected_state_and_systemd_values(self):
        class Runner:
            def capture(self, *args, **kwargs):return 'ok', b'ActiveState=active\nSubState=SECRET_ENV\nEnvironment=SECRET_KEY\n'
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder) / 'state.json'
            state.write_text(json.dumps({'status':'deployed','deployed_sha':'a'*40,'deployed_at':'2026-10-15T11:23:13+00:00','error':'SECRET_DATABASE_PASSWORD'}))
            with patch.object(diag, 'AUTO', state):result = diag.auto_state(Runner())
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['deployed_sha'], 'a'*40)
        self.assertEqual(result['service']['substate'], 'unknown')

    def test_subprocess_output_and_time_are_bounded_without_stderr_leak(self):
        runner = diag.Runner()
        status, output = runner.capture([sys.executable, '-c', 'import sys;sys.stderr.write("SECRET_ERROR");sys.stdout.write("x"*10000)'], maximum=64)
        self.assertEqual(status, 'output_limit')
        self.assertLessEqual(len(output), 64)
        self.assertNotIn(b'SECRET', output)
        status, output = runner.capture([sys.executable, '-c', 'import time;time.sleep(5)'], timeout=0.05)
        self.assertEqual(status, 'timeout')

    def test_network_payloads_use_no_auth_and_keep_tls_validation(self):
        self.assertIn('rejectUnauthorized:true', diag.NODE_PROBE)
        self.assertIn("origin:'https://web.whatsapp.com'", diag.NODE_PROBE)
        self.assertIn("wss://web.whatsapp.com/ws/chat", diag.NODE_PROBE)
        self.assertNotIn('.send(', diag.NODE_PROBE)
        self.assertNotIn('Authorization', diag.NODE_PROBE)
        self.assertNotIn('.send(', diag.TCP_PROBE)
        self.assertNotIn('sendall', diag.TCP_PROBE)
        compile(diag.TCP_PROBE, '<tcp-probe>', 'exec')

    def test_proxy_skip_outputs_complete_json_without_starting_network_checks(self):
        guard = "const d=require('dns'),h=require('https'); d.lookup=()=>{throw Error('network forbidden')};h.get=()=>{throw Error('network forbidden')};"
        process = subprocess.run(['node', '-e', guard + diag.NODE_PROBE], env=dict(os.environ, HTTPS_PROXY='http://SECRET_PROXY.invalid'), capture_output=True, text=True, timeout=2)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(json.loads(process.stdout), {'status':'skipped_proxy_configuration','proxy_configured':True})
        self.assertNotIn('SECRET', process.stdout)

    def test_main_hides_raw_exception(self):
        output = io.StringIO()
        with patch.object(diag, 'diagnose', side_effect=RuntimeError('SECRET_EXCEPTION')), patch.object(diag.logging, 'disable'), contextlib.redirect_stdout(output):
            status = diag.main()
        self.assertEqual(status, 1)
        self.assertNotIn('SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
