"""Network report discloses only enums/counts; all host/provider commands are read-only."""
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
spec = importlib.util.spec_from_file_location('network_route_diagnostic', ROOT / 'scripts/diagnose_network_route.py')
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


class NetworkRouteTests(unittest.TestCase):
    def test_xray_inspection_does_not_request_env_cmd_or_private_config(self):
        calls = []
        class Runner:
            def capture(self, args, **kwargs):
                calls.append(args)
                if args[1] == 'ps':return 'ok', b'a'*64
                return 'ok', json.dumps({'running':True, 'network_mode':'SECRET_NETWORK', 'ports':{'1080/tcp':[{'HostPort':'1081','HostIp':'127.0.0.1'},{'HostPort':'1082','HostIp':'10.5.6.7'},{'HostPort':'1083','HostIp':'0.0.0.0'}]},'Env':['SECRET_KEY'],'Cmd':['SECRET_CONFIG']}).encode()
        result = diag.xray(Runner())
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertNotIn('10.5.6.7', json.dumps(result))
        self.assertEqual(result['network_mode'], 'custom')
        self.assertEqual([p['bind_scope'] for p in result['published_ports']], ['loopback','private','public'])
        self.assertNotIn('Env', calls[1][3]);self.assertNotIn('Cmd', calls[1][3])
        self.assertEqual(calls[0][1], 'ps');self.assertEqual(calls[1][1], 'inspect')

    def test_absent_xray_does_not_inspect_any_other_container(self):
        class Runner:
            def capture(self, args, **kwargs):
                self.assertion = args
                return 'ok', b''
        result = diag.xray(Runner())
        self.assertEqual(result, {'status':'absent','exists':False})

    def test_routes_redact_gateway_source_device_and_errors(self):
        class Runner:
            def capture(self, args, **kwargs):
                if 'get' in args:return 'command_failed', b'RTNETLINK answers: Network is unreachable SECRET_IP SECRET_TOKEN'
                return 'ok', json.dumps([{'dev':'tun0','gateway':'SECRET_GATEWAY','prefsrc':'SECRET_SOURCE','dst':'default'},{'type':'blackhole','dev':'SECRET_INTERFACE'}]).encode()
        result = diag.routes(Runner())
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['default']['routes'][0]['interface_class'], 'tunnel')
        self.assertEqual(result['default']['routes'][1]['type'], 'blackhole')
        self.assertEqual(result['telegram_dc2']['route_error'], 'unreachable')

    def test_firewall_only_policy_and_direct_drop_reject_counts(self):
        calls = []
        class Runner:
            def capture(self, args, **kwargs):
                calls.append(args)
                if args[0]=='ufw':return 'ok', b'Status: active\nSECRET_IP ALLOW SECRET_CUSTOMER\n'
                return 'ok', b'Chain OUTPUT (policy ACCEPT 100 packets, 1000 bytes)\n pkts bytes target prot opt in out source destination\n 12 300 DROP all -- * * SECRET_SOURCE SECRET_DEST\n 3 100 REJECT tcp -- * * SECRET_IP SECRET_IP\n 9 200 SECRET_CHAIN all -- * * SECRET_IP SECRET_IP\n'
        result = diag.firewall(Runner())
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(result['ufw'], 'active')
        self.assertEqual(result['output']['policy'], 'ACCEPT')
        self.assertEqual(result['output']['direct_rule_counters']['DROP'], {'rules':1,'packets':12,'bytes':300})
        self.assertEqual(calls, [['ufw','status'],['iptables','-w','2','-L','OUTPUT','-n','-v','-x']])

    def test_provider_results_cannot_smuggle_session_credentials_or_qr(self):
        report = {'status':'ok','session_state_counts':{'STARTING':3,'SCAN_QR_CODE':1,'SECRET_PHONE':1},'qr_checks':1,'qr_present_count':1,'qr':'SECRET_QR','name':'SECRET_SESSION','me':{'id':'SECRET_PHONE'}}
        result = diag.safe_probe(json.dumps(report), 'sessions')
        self.assertEqual(result['qr_present_count'], 1)
        self.assertEqual(result['session_state_counts']['STARTING'], 3)
        self.assertNotIn('SECRET', json.dumps(result))
        report = {'status':'complete','heap_default':True,'ipv4_first':True,'autoselection_disabled':True,'dns_order':'ipv4first','auto_select_family':False,'NODE_OPTIONS':'SECRET_OPTIONS','wss_default':{'status':'http_rejected','http_status':403,'error_code':'SECRET_ERROR','headers':{'authorization':'SECRET_KEY'}}}
        result = diag.safe_probe(json.dumps(report), 'network')
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertTrue(result['ipv4_first']);self.assertEqual(result['wss_default']['http_status'], 403)

    def test_auto_update_retains_only_known_status_and_sha(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'state.json'
            path.write_text(json.dumps({'status':'deployed','deployed_sha':'a'*40,'candidate_sha':'SECRET_VALUE','error':'SECRET_ERROR'}))
            with patch.object(diag, 'AUTO', path):result = diag.auto_state()
        self.assertEqual(result, {'status':'deployed','deployed_sha':'a'*40})

    def test_worker_probe_only_gets_existing_sessions_and_challenges(self):
        namespace = {'__name__':'__main__'}
        calls = []
        class Response:
            def __init__(self, data):self.data = data
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def read(self, size):return json.dumps(self.data).encode()
        class Opener:
            def open(self, request, timeout):
                calls.append((request.get_method(), request.full_url, timeout))
                if '/api/sessions?' in request.full_url:
                    return Response([{'status':'SCAN_QR_CODE','name':'SECRET_SESSION_'+str(n),'me':{'id':'SECRET_PHONE'}} for n in range(5)] + [{'status':'STARTING','name':'SECRET_OTHER'}])
                return Response({'value':'SECRET_QR'})
        output = io.StringIO()
        with patch.dict(os.environ, {'WAHA_URL':'http://waha:3000','WAHA_API_KEY':'SECRET_KEY'}), patch('urllib.request.build_opener', return_value=Opener()), patch('logging.disable'), contextlib.redirect_stdout(output):
            exec(compile(diag.WAHA_PROBE, '<worker-probe>', 'exec'), namespace)
        self.assertNotIn('SECRET', output.getvalue())
        self.assertEqual(json.loads(output.getvalue())['qr_checks'], 3)
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(method=='GET' and url.startswith('http://waha:3000/api/') and timeout==3 for method,url,timeout in calls))
        self.assertNotIn('.send(', diag.NODE_PROBE)
        self.assertIn('rejectUnauthorized:true', diag.NODE_PROBE)
        self.assertIn("origin:'https://web.whatsapp.com'", diag.NODE_PROBE)

    def test_node_skips_network_when_existing_proxy_is_configured(self):
        guard = "const testDns=require('dns');testDns.lookup=()=>{throw Error('network forbidden')};"
        result = subprocess.run(['node','-e',guard+diag.NODE_PROBE], env=dict(os.environ,WHATSAPP_PROXY_SERVER_LIST='SECRET_PROXY_LIST'), capture_output=True, text=True, timeout=2)
        self.assertEqual(result.returncode, 0)
        report = json.loads(result.stdout)
        self.assertTrue(report['proxy_configured'])
        self.assertEqual(report['wss_default']['status'], 'skipped_proxy_configuration')
        self.assertNotIn('SECRET', result.stdout)

    def test_capture_bounds_time_output_and_stderr(self):
        runner = diag.Runner()
        status, raw = runner.capture([sys.executable,'-c','import sys;sys.stderr.write("SECRET_ERROR");sys.stdout.write("x"*1000)'], maximum=64)
        self.assertEqual((status, raw), ('output_limit', b''))
        status, raw = runner.capture([sys.executable,'-c','import time;time.sleep(5)'], timeout=.05)
        self.assertEqual((status, raw), ('timeout', b''))

    def test_embedded_telegram_payload_matches_reviewed_standalone_probe(self):
        self.assertEqual(diag.TELEGRAM_WSS_PROBE, (ROOT / 'scripts/probe_telegram_wss.py').read_text())
        compile(diag.TELEGRAM_WSS_PROBE, '<telegram-probe>', 'exec')

    def test_telegram_transport_results_reject_raw_protocol_and_auth_data(self):
        raw = {'status':'complete','proxy_configured':False,'session':'SECRET_SESSION','checks':[{'endpoint':'dc2_kws','status':'mtproto_verified','wss_open':True,'mtproto_verified':True,'elapsed_ms':130,'auth_key':'SECRET_KEY','nonce':'SECRET_NONCE','message':'SECRET_PHONE'},{'endpoint':'dc2_venus','status':'SECRET_ERROR','wss_open':False,'mtproto_verified':False,'elapsed_ms':11,'http_status':403,'headers':{'token':'SECRET_TOKEN'}}]}
        result = diag.safe_telegram_wss(json.dumps(raw))
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertTrue(result['checks'][0]['mtproto_verified'])
        self.assertEqual(result['checks'][1]['status'], 'unknown')
        self.assertEqual(result['checks'][1]['http_status'], 403)
        raw['checks'] = [{'endpoint':'SECRET_CUSTOM_ENDPOINT','status':'mtproto_verified'}]
        self.assertEqual(diag.safe_telegram_wss(json.dumps(raw))['checks'], [])

    def test_complete_packet_uses_only_bounded_readonly_commands(self):
        calls = []
        class Runner:
            def capture(self, args, **kwargs):
                calls.append((args, kwargs))
                if args[:3] == ['docker','context','inspect']:return 'ok', b'unix:///var/run/docker.sock'
                if args[:2] == ['docker','ps']:return 'ok', b''
                if args[0] == 'ip':return 'ok', b'[]'
                return 'unavailable', b''
            def compose(self, *args, **kwargs):
                calls.append((list(args), kwargs))
                return 'command_failed', b'SECRET_ERROR'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'.env.pilot').touch();(root/'compose.pilot.yaml').touch()
            with patch.object(diag, 'Runner', Runner), patch.object(diag, 'ROOT', root), patch.object(diag, 'AUTO', root/'missing'), patch.dict(os.environ, {'DOCKER_HOST':''}):
                result = diag.diagnose()
        self.assertNotIn('SECRET', json.dumps(result))
        self.assertEqual(len(calls), 9)
        self.assertTrue(all(args[0] in {'ip','ufw','iptables','docker','exec'} for args,kw in calls))
        for args, kwargs in calls:
            self.assertLessEqual(kwargs.get('timeout', 4), 30)
            self.assertNotIn('restart', args);self.assertNotIn('up', args);self.assertNotIn('down', args)
            if args[0]=='exec':self.assertIn('-T', args)

    def test_main_does_not_print_exception_details(self):
        output = io.StringIO()
        with patch.object(diag, 'diagnose', side_effect=RuntimeError('SECRET_ERROR')), patch.object(diag.logging, 'disable'), contextlib.redirect_stdout(output):
            self.assertEqual(diag.main(), 1)
        self.assertNotIn('SECRET', output.getvalue())


if __name__ == '__main__':
    unittest.main()
