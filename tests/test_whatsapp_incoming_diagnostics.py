"""Read-only incoming-message diagnosis: real DB scope and adversarial redaction."""
import contextlib
from datetime import datetime,timedelta,timezone
import importlib.util
import io
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from sqlalchemy import Column,DateTime,Integer,JSON,String,Text,create_engine,event
from sqlalchemy.orm import declarative_base,sessionmaker

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('incoming_diagnostic',ROOT/'scripts/diagnose_whatsapp_incoming.py')
diag=importlib.util.module_from_spec(spec);spec.loader.exec_module(diag)
Base=declarative_base()

class Account(Base):
    __tablename__='accounts'
    id=Column(String,primary_key=True);channel=Column(String);state=Column(String);public=Column(JSON);encrypted_config=Column(Text);updated_at=Column(DateTime)
class Conversation(Base):
    __tablename__='conversations'
    id=Column(String,primary_key=True);account_id=Column(String);peer=Column(String);title=Column(Text)
class Message(Base):
    __tablename__='messages'
    id=Column(Integer,primary_key=True);conversation_id=Column(String);provider_id=Column(String);direction=Column(String);created_at=Column(DateTime);text=Column(Text)
MODELS=(Account,Conversation,Message)
NOW=1792058400

class Response:
    def __init__(self,data,status=200):self.data=data;self.status_code=status
    def __enter__(self):return self
    def __exit__(self,*_):pass
    def iter_bytes(self):yield self.data if isinstance(self.data,bytes) else json.dumps(self.data).encode()

class Client:
    def __init__(self,responses):self.responses=list(responses);self.calls=[]
    def stream(self,method,path,params=None,timeout=None):
        self.calls.append((method,path,params,timeout));response=self.responses.pop(0)
        if isinstance(response,BaseException):raise response
        return response


class IncomingDiagnosticTests(unittest.TestCase):
    def test_database_counts_readonly_scope_and_no_message_body_query(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine);Session=sessionmaker(engine)
        current=datetime.fromtimestamp(NOW,timezone.utc)
        with Session() as db:
            db.add_all([Account(id='account-a',channel='whatsapp_web',state='connected',public={'error':'SECRET_RAW_ERROR'},encrypted_config=json.dumps({'_waha_session':'omni-'+'a'*32}),updated_at=current),Account(id='account-b',channel='whatsapp_web',state='disconnected',public={},encrypted_config='{}',updated_at=current)])
            db.add_all([Conversation(id='ca',account_id='account-a',peer='79991234567@c.us',title='SECRET_NAME'),Conversation(id='cb',account_id='account-b',peer='78881234567@c.us',title='SECRET_NAME')])
            for identifier,conversation,provider,direction,seconds in [(1,'ca','SECRET_IN','inbound',60),(2,'ca','SECRET_OUT','outbound',120),(3,'ca','SECRET_OLD','inbound',90000),(4,'cb','SECRET_FOREIGN','inbound',30)]:
                db.add(Message(id=identifier,conversation_id=conversation,provider_id=provider,direction=direction,created_at=current-timedelta(seconds=seconds),text='SECRET_BODY'))
            db.commit()
        statements=[]
        event.listen(engine,'before_cursor_execute',lambda conn,cursor,statement,parameters,context,executemany:statements.append(statement))
        with Session() as db:items=diag.db_accounts(db,MODELS,json.loads,NOW)
        summary=items[0]['summary']
        self.assertEqual(summary['db_messages']['inbound'],{'last_15min':1,'last_24h':1,'latest_age_seconds':60})
        self.assertEqual(summary['db_messages']['outbound']['latest_age_seconds'],120)
        self.assertNotIn('SECRET',json.dumps(summary));self.assertNotIn('79991234567',json.dumps(summary))
        with Session() as db:found=diag.presence(db,MODELS,'account-a',[{'id':'SECRET_IN'},{'id':'SECRET_FOREIGN'}])
        self.assertEqual(found,{'SECRET_IN'})
        self.assertTrue(all(statement.lstrip().upper().startswith(('SELECT','PRAGMA')) for statement in statements))
        self.assertTrue(any('query_only' in statement for statement in statements))
        self.assertFalse(any('messages.text' in statement.lower() or 'conversations.title' in statement.lower() for statement in statements))

    def test_cursor_backlog_and_identifier_shapes_are_safe_and_meaningful(self):
        config={'_waha_chat_offset':40,'_waha_cursors':{'SECRET_PEER':{'floor':NOW-86400,'ceiling':NOW-600,'offset':300},'SECRET_OTHER':{'watermark':NOW-5}},'_waha_lid_pn':{'SECRET_LID':'SECRET_PHONE'}}
        report=diag.cursor_summary(config,NOW)
        self.assertEqual(report['pending_history_count'],1);self.assertEqual(report['history_offset_max'],300)
        self.assertEqual(report['ceiling_age_max_seconds'],600);self.assertEqual(report['watermark_age_min_seconds'],5)
        self.assertNotIn('SECRET',json.dumps(report))
        rows=[{'id':'79991234567@s.whatsapp.net','name':'SECRET_NAME'},{'id':{'_serialized':'12345@lid','user':'SECRET_PHONE'}},{'id':{'unexpected':'SECRET_ID'}},{}]
        report,peers=diag.chat_summary(rows)
        self.assertEqual(report['normalized_valid_peers'],2);self.assertEqual(report['adapter_string_valid_peers'],1)
        self.assertEqual(report['id_shapes'],{'string':1,'object':2,'missing':1,'other':0})
        self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn('79991234567',json.dumps(report))

    def test_history_reports_missing_inbound_without_text_or_raw_ids(self):
        rows=[{'id':'SECRET_IN','fromMe':False,'timestamp':(NOW-4)*1000,'body':'SECRET_TEXT','from':'SECRET_PHONE'},{'id':'SECRET_OUT','fromMe':True,'timestamp':NOW-7,'body':'SECRET_TEXT'},{'id':{'secret':'SECRET_ID'},'timestamp':'SECRET_TS'}]
        report=diag.history_summary(rows,{'SECRET_OUT'},NOW)
        self.assertEqual(report['direction_counts'],{'inbound':1,'outbound':1,'unknown':1})
        self.assertFalse(report['samples'][0]['already_in_account_db']);self.assertTrue(report['samples'][1]['already_in_account_db'])
        self.assertEqual(report['latest_age_seconds']['inbound'],4)
        self.assertEqual(report['timestamp_shapes'],{'milliseconds':1,'seconds':1,'invalid':1})
        self.assertNotIn('SECRET',json.dumps(report))

    def test_provider_history_get_is_unfiltered_recent_and_readonly(self):
        session='omni-'+'a'*32;pn='79991234567@c.us';lid='123456@lid'
        item={'id':'account-a','session':session,'config':{'_waha_chat_offset':20,'_waha_lid_pn':{lid:pn}},'candidates':[pn],'summary':{'gateway_history':[]}}
        sessions=[{'name':session,'status':'WORKING','me':{'id':'SECRET_PHONE'},'config':{'noweb':{'store':{'enabled':True,'fullSync':True}},'secret':'SECRET_KEY'}}]
        client=Client([Response(sessions),Response([{'id':pn,'name':'SECRET_NAME'},{'id':lid}]),Response([]),Response([{'id':'SECRET_NEW','fromMe':False,'timestamp':NOW-1,'body':'SECRET_BODY'}]),Response([])])
        class DB:
            def __enter__(self):return self
            def __exit__(self,*_):pass
        with patch.object(diag,'presence',return_value=set()):diag.provider_accounts(diag.Gateway(client),[item],DB,MODELS,NOW)
        report=item['summary'];self.assertEqual(report['gateway']['provider_state'],'WORKING');self.assertTrue(report['gateway']['store_enabled'])
        self.assertEqual(len(report['gateway_history']),2);self.assertTrue(report['gateway_history'][0]['alias_present'])
        self.assertNotIn('SECRET',json.dumps(report));self.assertNotIn(pn,json.dumps(report));self.assertNotIn(lid,json.dumps(report))
        self.assertTrue(all(method=='GET' and timeout<=3 for method,path,params,timeout in client.calls))
        self.assertEqual(client.calls[1][2]['sortBy'],'conversationTimestamp')
        self.assertEqual(client.calls[2][2]['offset'],20)
        for _,path,params,_ in client.calls:
            self.assertNotIn('/auth/',path)
            if path.endswith('/messages'):
                self.assertEqual(params,{'limit':10,'offset':0,'sortBy':'timestamp','sortOrder':'desc','downloadMedia':'false'})

    def test_http_budget_size_and_exceptions_are_bounded_and_redacted(self):
        client=Client([Response([]) for _ in range(10)]);gateway=diag.Gateway(client)
        for _ in range(12):gateway.get('/api/sessions')
        self.assertEqual(len(client.calls),10)
        self.assertEqual(gateway.get('/api/sessions')[1]['status'],'budget_exhausted')
        for response in (Response(b'x'*1048577),RuntimeError('SECRET_EXCEPTION'),Response({'secret':'SECRET'},403)):
            value,status=diag.Gateway(Client([response])).get('/api/sessions')
            self.assertIsNone(value);self.assertNotIn('SECRET',json.dumps(status))

    def test_hard_deadline_is_not_swallowed_by_http_error_handling(self):
        with self.assertRaises(diag.DeadlineExceeded):diag.Gateway(Client([diag.DeadlineExceeded()])).get('/api/sessions')
        output=io.StringIO()
        with patch.object(diag,'diagnose',side_effect=diag.DeadlineExceeded()),patch.object(diag.logging,'disable'),patch.object(diag.signal,'signal'),patch.object(diag.signal,'alarm') as alarm,contextlib.redirect_stdout(output):
            self.assertEqual(diag.main(),1)
        self.assertEqual(json.loads(output.getvalue())['result'],'diagnostic_timeout')
        self.assertEqual([call.args[0] for call in alarm.call_args_list],[40,0])

    def test_main_never_prints_unknown_exception(self):
        output=io.StringIO()
        with patch.object(diag,'diagnose',side_effect=RuntimeError('SECRET_EXCEPTION')),patch.object(diag.logging,'disable'),patch.object(diag.signal,'signal'),patch.object(diag.signal,'alarm'),contextlib.redirect_stdout(output):self.assertEqual(diag.main(),1)
        self.assertNotIn('SECRET',output.getvalue())


if __name__=='__main__':unittest.main()
