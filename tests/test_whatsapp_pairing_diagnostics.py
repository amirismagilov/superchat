"""Pairing packet must retain transitions while suppressing all secret/raw payloads."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('pairing_diagnostic',ROOT/'scripts/diagnose_whatsapp_pairing.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)


class Python310Datetime:
    """Exercise CPython <=3.10's fractional parser even when CI uses 3.12."""
    @staticmethod
    def fromisoformat(value):
        fractional=re.search(r'T\d{2}:\d{2}:\d{2}\.(\d+)',value)
        if value.endswith('Z') or fractional and len(fractional[1]) not in {3,6}:
            raise ValueError('Invalid isoformat string')
        return datetime.fromisoformat(value)

    @staticmethod
    def fromtimestamp(value,tz):
        return datetime.fromtimestamp(value,tz)


class PairingTests(unittest.TestCase):
    def test_normal_delayed_restart_is_not_a_transport_timeout(self):
        report=diag.timeline(json.dumps({'time':1792058400000,'session':'session-a',
            'msg':'Job scheduled with timeout 2000 ms','job':'start-engine'}).encode())
        self.assertEqual(report['events'][0]['event'],'engine_restart_scheduled')
        self.assertEqual(report['events'][0]['reason_codes'],[])
        self.assertEqual(report['session_summaries'][0]['reason_counts'],{})

    def test_compact_packet_keeps_deployment_and_per_account_failure_evidence(self):
        lines=[];accounts=[]
        for index in range(8):
            session='session-'+str(index)
            lines.append('2026-10-15T10:00:01.123456789Z '+json.dumps({
                'session':session,'msg':'pairing configured successfully',
                'private':'SECRET_DEVICE'}))
            lines.append('2026-10-15T10:00:02.123456789Z '+json.dumps({
                'session':session,'msg':'Connection closed due to WebSocket Error: timed out, reconnecting...',
                'err':{'output':{'statusCode':408},'trace':'SECRET_TRACE'},
                'private':'SECRET_CREDENTIAL'}))
            accounts.append({'account_hash':format(index,'012x'),'session_hash':diag.session_hash(session),
                'state':'action_required','public_error':'waha_session_failed',
                'gateway':{'status':'session_found','provider_state':'FAILED'},'phone':'SECRET_PHONE'})
        report={'diagnostic':'whatsapp_pairing_readonly_v2','result':'ok',
                'auto_update':{'status':'up_to_date','deployed_sha':'a'*40},
                'timeline':dict(diag.timeline('\n'.join(lines).encode()),capture_status='ok'),
                'accounts':diag.safe_accounts(json.dumps({'result':'ok','accounts_total':8,'accounts':accounts}))}
        compact=diag.compact_report(report)
        encoded=json.dumps(compact,separators=(',',':'))
        self.assertLess(len(encoded),6000)
        self.assertNotIn('SECRET',encoded);self.assertNotIn('session-0',encoded)
        self.assertEqual(compact['auto_update']['deployed_sha'],'a'*40)
        self.assertEqual(len(compact['accounts']),8)
        self.assertEqual(compact['accounts'][0]['status_codes'],[408])
        self.assertEqual(compact['accounts'][0]['reason_counts'],{'timeout':1,'websocket_error':1})
        self.assertEqual(compact['accounts'][0]['events']['pairing_configured'],1)
        self.assertEqual(compact['accounts'][0]['last_event']['timestamp_utc'],'2026-10-15T10:00:02.123Z')

    def test_failure_reason_attribute_is_allowlisted_without_raw_node(self):
        report=diag.timeline(json.dumps({'time':1792058400000,'session':'session-a',
            'msg':'connection errored','node':{'tag':'failure','attrs':{'reason':'401','credential':'SECRET'}}}).encode())
        self.assertEqual(report['session_summaries'][0]['status_codes'],[401])
        self.assertNotIn('SECRET',json.dumps(report))

    def test_docker_timestamp_fractions_work_with_python310_parser(self):
        with patch.object(diag,'datetime',Python310Datetime):
            for digits in range(1,10):
                fraction='123456789'[:digits]
                with self.subTest(digits=digits):
                    self.assertEqual(diag.timestamp('2026-10-15T10:00:01.'+fraction+'Z'),
                                     '2026-10-15T10:00:01.'+fraction[:3].ljust(3,'0')+'Z')
            for value,expected in (
                ('2026-10-15T10:00:01Z','2026-10-15T10:00:01.000Z'),
                ('2026-10-15T13:00:01.123456789+03:00','2026-10-15T10:00:01.123Z'),
                ('2026-10-15T06:00:01.123456789-04:00','2026-10-15T10:00:01.123Z'),
                ('2026-10-15T10:00:01.999999999Z','2026-10-15T10:00:01.999Z'),
                (1792058401123,'2026-10-15T10:00:01.123Z'),
            ):
                with self.subTest(value=value):self.assertEqual(diag.timestamp(value),expected)
            for value in ('2026-10-15T10:00:01.1234567890Z','2026-02-30T10:00:01.123Z',
                          '2026-10-15T10:00:01.123Z SECRET','SECRET_DATE'):
                with self.subTest(invalid=value):self.assertIsNone(diag.timestamp(value))

    def test_python310_timestamp_parsing_preserves_pretty_and_json_correlation(self):
        rows=[
            '2026-10-15T10:00:01.123456789Z [10:00:01.123] ERROR (NOWEBEngine/1): session:session-a - error in validating connection',
            '2026-10-15T10:00:01.123456790Z     err: {',
            '2026-10-15T10:00:01.123456791Z       "code": "ECONNRESET",',
            '2026-10-15T10:00:01.123456792Z       "output": {"statusCode": 401, "secret": "SECRET_TRACE"}',
            '2026-10-15T10:00:01.123456793Z     }',
            '2026-10-15T10:00:02.987654321Z '+json.dumps({'session':'session-b','msg':'stream errored out','fullErrorNode':{'attrs':{'code':'515'}},'token':'SECRET_TOKEN'}),
        ]
        with patch.object(diag,'datetime',Python310Datetime):
            report=diag.timeline('\n'.join(rows).encode())
        failure=next(event for event in report['events'] if event['event']=='connection_validation_failed')
        self.assertEqual(failure['timestamp_utc'],'2026-10-15T10:00:01.123Z')
        self.assertEqual(failure['session_hash'],diag.session_hash('session-a'))
        self.assertEqual(failure['status_codes'],[401])
        self.assertEqual(failure['error_codes'],['ECONNRESET'])
        restart=next(event for event in report['events'] if event['event']=='stream_error')
        self.assertEqual(restart['timestamp_utc'],'2026-10-15T10:00:02.987Z')
        self.assertEqual(restart['session_hash'],diag.session_hash('session-b'))
        self.assertTrue(restart['restart_required'])
        self.assertEqual(report['events_without_timestamp'],0)
        self.assertNotIn('SECRET',json.dumps(report))

    def test_pretty_multiline_error_codes_stay_with_the_correct_session(self):
        # WAHA uses pino-pretty singleLine=true, but error-like objects remain
        # multiline. Docker prefixes every physical line, including the stack.
        rows=[
            '2026-10-15T10:00:01.000000001Z [10:00:01.000] ERROR (NOWEBEngine/1): session:session-a - error in validating connection {"session":"session-a"}',
            '2026-10-15T10:00:01.000000002Z     err: {',
            '2026-10-15T10:00:01.000000003Z       "message": "SECRET_CONNECTION",',
            '2026-10-15T10:00:01.000000004Z       "code": "ECONNRESET",',
            '2026-10-15T10:00:01.000000005Z       "output": { "statusCode": 401 }',
            '2026-10-15T10:00:01.000000006Z     }',
            '2026-10-15T10:00:02.000000001Z [10:00:02.000] INFO (NOWEBEngine/1): session:session-b - opened connection to WA {"me":"SECRET_PHONE"}',
        ]
        report=diag.timeline('\n'.join(rows).encode())
        self.assertEqual(len(report['events']),2)
        failure,success=report['events']
        self.assertEqual(failure['session_hash'],diag.session_hash('session-a'))
        self.assertEqual(failure['timestamp_utc'],'2026-10-15T10:00:01.000Z')
        self.assertEqual(failure['status_codes'],[401])
        self.assertEqual(failure['error_codes'],['ECONNRESET'])
        self.assertEqual(success['session_hash'],diag.session_hash('session-b'))
        self.assertEqual(success['status_codes'],[])
        self.assertFalse(report['continuations_truncated'])
        self.assertNotIn('SECRET',json.dumps(report))

    def test_unrelated_unindented_record_clears_pretty_continuation_context(self):
        rows=[
            '2026-10-15T10:00:01Z [10:00:01.000] ERROR (NOWEBEngine/1): session:session-a - error in validating connection',
            '2026-10-15T10:00:02Z an unrelated non-pretty record SECRET_VALUE',
            '2026-10-15T10:00:02Z     "code": "ECONNRESET"',
        ]
        report=diag.timeline('\n'.join(rows).encode())
        self.assertEqual(report['events'][0]['error_codes'],[])
        self.assertEqual(report['events'][1]['event'],'transport_error')
        self.assertIsNone(report['events'][1]['session_hash'])
        self.assertNotIn('SECRET',json.dumps(report))

    def test_pretty_continuation_limits_are_explicit_and_new_records_survive(self):
        header='2026-10-15T10:00:01Z [10:00:01.000] ERROR (NOWEBEngine/1): session:session-a - error in validating connection'
        rows=[header]+['2026-10-15T10:00:01Z     SECRET_STACK']*diag.MAX_PRETTY_CONTINUATIONS
        rows+=['2026-10-15T10:00:01Z     "statusCode": 401',
               '2026-10-15T10:00:02Z [10:00:02.000] INFO (NOWEBEngine/1): session:session-b - opened connection to WA']
        report=diag.timeline('\n'.join(rows).encode())
        self.assertTrue(report['continuations_truncated'])
        self.assertEqual(report['events'][0]['status_codes'],[])
        self.assertEqual(report['events'][1]['session_hash'],diag.session_hash('session-b'))
        with patch.object(diag,'MAX_PRETTY_RECORD_CHARS',256):
            report=diag.timeline((header+'\n2026-10-15T10:00:01Z     '+'SECRET'*100).encode())
        self.assertTrue(report['continuations_truncated'])
        self.assertNotIn('SECRET',json.dumps(report))

    def test_json_and_pretty_events_correlate_without_secret_disclosure(self):
        session='omni-'+('a'*32)
        rows=[
            '2026-10-15T10:00:01.000000001Z '+json.dumps({'time':123,'session':session,'msg':'pairing configured successfully, expect to restart the connection...','me':{'id':'SECRET_PHONE'},'qr':'SECRET_QR'}),
            '2026-10-15T10:00:02.000000001Z '+json.dumps({'session':session,'msg':'stream errored out','fullErrorNode':{'attrs':{'code':'515','secret':'SECRET_TOKEN'},'children':['SECRET_NODE']}}),
            '2026-10-15T10:00:03.000000001Z \x1b[32mINFO\x1b[39m session:'+session+' - opened connection to WA {"me":"SECRET_PHONE"}',
            '2026-10-15T10:00:04.000000001Z '+json.dumps({'session':session,'msg':'error in pairing','trace':'SECRET_TRACE Invalid account signature SECRET_SIGNATURE'}),
            '2026-10-15T10:00:05.000000001Z SECRET_QR SECRET_PASSWORD',
        ]
        report=diag.timeline('\n'.join(rows).encode())
        self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn(session,json.dumps(report))
        self.assertEqual({event['session_hash'] for event in report['events']},{diag.session_hash(session)})
        self.assertTrue(next(event for event in report['events'] if event['event']=='stream_error')['restart_required'])
        self.assertIn('account_signature_invalid',[event['event'] for event in report['events']])
        self.assertEqual(report['session_summaries'][0]['event_count'],5)

    def test_early_transitions_survive_timeline_truncation_in_counters(self):
        rows=[json.dumps({'time':1792058400000,'session':'session-a','msg':'pairing configured successfully'})]
        rows += [json.dumps({'time':1792058401000+n,'session':'session-a','msg':'connection errored','trace':'SECRET_TRACE ETIMEDOUT'}) for n in range(200)]
        report=diag.timeline('\n'.join(rows).encode())
        self.assertEqual(len(report['events']),60);self.assertTrue(report['events_truncated'])
        summary=report['session_summaries'][0]
        self.assertEqual(summary['event_count'],201);self.assertEqual(summary['event_counts']['pairing_configured'],1)
        self.assertLess(summary['first_event_utc'],summary['latest_event_utc'])
        self.assertNotIn('SECRET',json.dumps(report))

    def test_limits_mark_explicitly_and_timestamp_cannot_smuggle_raw_values(self):
        report=diag.timeline(b'ignored\n'*6000)
        self.assertEqual(report['lines_examined'],5000);self.assertTrue(report['capture_truncated'])
        report=diag.timeline(b'ignored\n'*(diag.MAX_LOG_BYTES//8+2))
        self.assertTrue(report['capture_truncated'])
        raw=json.dumps({'time':'SECRET_TOKEN','session':'SECRET/SESSION','msg':'connection errored','statusCode':401,'trace':'SECRET_TRACE'})
        report=diag.timeline(raw.encode())
        self.assertNotIn('SECRET',json.dumps(report));self.assertIsNone(report['events'][0]['session_hash'])
        self.assertIsNone(report['events'][0]['timestamp_utc'])

    def test_embedded_account_hash_matches_log_hash_and_only_reads_data(self):
        namespace={'__name__':'embedded_test'}
        exec(compile(diag.ACCOUNT_PROBE,'<accounts>','exec'),namespace)
        account=SimpleNamespace(id='SECRET_ACCOUNT',state='connecting',updated_at=datetime.now(timezone.utc),public={'qr_text':'SECRET_QR','error':'SECRET_ERROR'},encrypted_config='SECRET_ENCRYPTED')
        result,internal=namespace['account_summary'](account,[],1,lambda _: {'_waha_session':'session-a','phone':'SECRET_PHONE'},datetime.now(timezone.utc))
        self.assertEqual(result['session_hash'],diag.session_hash('session-a'))
        self.assertEqual(internal,'session-a');self.assertNotIn('SECRET',json.dumps(result))
        self.assertIn('SET TRANSACTION READ ONLY',diag.ACCOUNT_PROBE)
        self.assertIn('signal.alarm(28)',diag.ACCOUNT_PROBE)
        self.assertNotIn('db.commit(',diag.ACCOUNT_PROBE)
        self.assertNotIn('client.post(',diag.ACCOUNT_PROBE)
        self.assertNotIn('client.delete(',diag.ACCOUNT_PROBE)

    def test_auth_timeout_and_late_status_check_errors_are_allowlisted_twice(self):
        namespace={'__name__':'embedded_test'};exec(diag.ACCOUNT_PROBE,namespace)
        account=SimpleNamespace(id='account',state='action_required',updated_at=datetime.now(timezone.utc),public={'error':'provider_auth_timeout','status_check_error':'waha_transport_error'},encrypted_config='SECRET_ENCRYPTED')
        summary,_=namespace['account_summary'](account,[],1,lambda _: {},datetime.now(timezone.utc))
        report=diag.safe_accounts(json.dumps({'result':'ok','accounts':[summary]}))
        self.assertEqual(report['accounts'][0]['public_error'],'provider_auth_timeout')
        self.assertEqual(report['accounts'][0]['status_check_error'],'waha_transport_error')
        summary['status_check_error']='SECRET_RAW_ERROR'
        report=diag.safe_accounts(json.dumps({'result':'ok','accounts':[summary]}))
        self.assertEqual(report['accounts'][0]['status_check_error'],'unknown_error')
        self.assertNotIn('SECRET',json.dumps(report))

    def test_embedded_provider_uses_get_only_and_caps_qr_checks(self):
        namespace={'__name__':'embedded_test'};exec(diag.ACCOUNT_PROBE,namespace)
        calls=[]
        class Response:
            status_code=200
            def __init__(self,path):self.path=path
            def __enter__(self):return self
            def __exit__(self,*args):pass
            def iter_bytes(self):
                payload=[{'name':'session-'+str(n),'status':'SCAN_QR_CODE','me':'SECRET_PHONE'} for n in range(5)] if self.path=='/api/sessions' else {'value':'SECRET_QR'}
                yield json.dumps(payload).encode()
        class Client:
            def stream(self,method,path,params=None):calls.append((method,path,params));return Response(path)
        pairs=[({},'session-'+str(n)) for n in range(5)]
        result=namespace['gateway_summary'](Client(),pairs)
        self.assertEqual(result['qr_checks'],3);self.assertEqual(len(calls),4)
        self.assertTrue(all(call[0]=='GET' for call in calls));self.assertNotIn('SECRET',json.dumps([result,pairs]))

    def test_host_sanitizer_rejects_raw_account_and_gateway_fields(self):
        raw={'result':'ok','accounts_total':1,'gateway':{'status':'ok','api_key':'SECRET_KEY'},'accounts':[{'account_hash':'a'*12,'session_hash':'b'*12,'state':'action_required','provider_state':'WORKING','public_error':'provider_unauthorized','phone':'SECRET_PHONE','gateway':{'status':'session_found','provider_state':'WORKING','qr_present':False,'qr':'SECRET_QR'},'jobs':[{'action':'login','status':'failed','error':'SECRET_RAW_EXCEPTION','age_seconds':3}]}]}
        report=diag.safe_accounts(json.dumps(raw));self.assertNotIn('SECRET',json.dumps(report))
        self.assertEqual(report['accounts'][0]['gateway']['provider_state'],'WORKING')
        self.assertEqual(report['accounts'][0]['jobs'][0]['error'],'unknown_error')

    def test_capture_keeps_tail_bounded_without_stderr_and_times_out(self):
        runner=diag.Runner()
        status,raw,truncated=runner.capture([sys.executable,'-c','import sys;sys.stderr.write("SECRET_ERROR");sys.stdout.write("x"*1000+"END")'],maximum=64,tail=True)
        self.assertEqual(status,'ok');self.assertTrue(truncated);self.assertEqual(len(raw),64);self.assertTrue(raw.endswith(b'END'));self.assertNotIn(b'SECRET',raw)
        status,raw,truncated=runner.capture([sys.executable,'-c','import time;time.sleep(5)'],timeout=.05)
        self.assertEqual(status,'timeout');self.assertTrue(truncated)

    def test_complete_packet_uses_readonly_commands_and_suppresses_raw_errors(self):
        calls=[]
        class Runner:
            def capture(self,args,**kwargs):
                calls.append((args,kwargs))
                if args[:3]==['docker','context','inspect']:return 'ok',b'unix:///var/run/docker.sock',False
                return 'ok',json.dumps({'service':'waha','state':'running','health':'healthy','restarts':2,'oom':False,'ipv4_options_exact':True,'legacy_options_exact':False,'Env':['SECRET_KEY']}).encode(),False
            def compose(self,*args,**kwargs):
                calls.append((list(args),kwargs))
                if args[0]=='ps':return 'ok',b'a'*64,False
                if args[0]=='logs':return 'ok',b'2026-10-15T10:00:00Z session:session-a - opened connection to WA SECRET_PHONE',False
                return 'command_failed',b'SECRET_ERROR',False
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.env.pilot').touch();(root/'compose.pilot.yaml').touch()
            with patch.object(diag,'Runner',Runner),patch.object(diag,'ROOT',root),patch.object(diag,'AUTO',root/'missing'),patch.dict(os.environ,{'DOCKER_HOST':''}):report=diag.diagnose()
        self.assertNotIn('SECRET',json.dumps(report));self.assertEqual(len(calls),5)
        self.assertTrue(report['containers']['services'][0]['ipv4_options_exact'])
        self.assertTrue(all(args[0] in {'docker','ps','logs','exec'} for args,_ in calls))
        for args,kwargs in calls:
            self.assertLessEqual(kwargs.get('timeout',5),30)
            for command in ('up','down','restart','stop','start'):self.assertNotIn(command,args)
        self.assertIn('captured_at_utc',report)

    def test_main_masks_exceptions(self):
        output=io.StringIO()
        with patch.object(diag,'diagnose',side_effect=RuntimeError('SECRET_EXCEPTION')),patch.object(diag.logging,'disable'),patch.object(diag.signal,'signal'),patch.object(diag.signal,'alarm'),contextlib.redirect_stdout(output):self.assertEqual(diag.main(),1)
        self.assertNotIn('SECRET',output.getvalue())


if __name__=='__main__':unittest.main()
