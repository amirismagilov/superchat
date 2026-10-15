#!/usr/bin/env python3
"""Read-only WhatsApp inbound/store/cursor report; run in worker as python -.

No message text, names, phones, QR, session IDs, credentials or raw exceptions.
Only bounded GET calls to the configured local gateway and read-only DB queries.
"""
import hashlib
import json
import logging
import math
import os
import re
import signal
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

MAX_ACCOUNTS = 3
MAX_HISTORY = 3
MAX_CALLS = 10
STATES = {'connected','connecting','action_required','reauth_required','qr_required','disconnected'}
PROVIDER_STATES = {'WORKING','STARTING','STOPPED','FAILED','SCAN_QR_CODE','PASSKEY_REQUIRED','PASSKEY_CONFIRMATION_REQUIRED'}
ERRORS = {'waha_invalid_chats_response','waha_invalid_history_response','waha_invalid_lid_response','waha_invalid_message','waha_transport_error','waha_server_error','waha_request_rejected','waha_rate_limited','waha_session_failed','waha_session_stopped','whatsapp_account_identity_changed','worker_ownership_lost','worker_fencing_epoch_changed'}
PEER = re.compile(r'[0-9-]+@(?:c\.us|g\.us|lid)')


class DeadlineExceeded(BaseException):
    pass


def allowed(value,choices,fallback='unknown'):
    return value if isinstance(value,str) and value in choices else fallback


def hashed(value):
    return hashlib.sha256(value.encode()).hexdigest()[:12] if isinstance(value,str) and value else None


def integer(value,maximum=10**9):
    return value if type(value) is int and 0<=value<=maximum else None


def epoch(value):
    if isinstance(value,datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp(),'datetime'
    if type(value) not in (int,float) or not math.isfinite(value):return None,'invalid'
    if 1577836800<=value<=4102444800:return float(value),'seconds'
    if 1577836800000<=value<=4102444800000:return float(value)/1000,'milliseconds'
    return None,'invalid'


def age(value,now):
    stamp,_=epoch(value)
    return max(0,int(now-stamp)) if stamp is not None else None


def peer(value):
    if isinstance(value,dict):value=value.get('_serialized')
    if not isinstance(value,str):return None
    if value.isdigit():value+='@c.us'
    if value.startswith('+') and value[1:].isdigit():value=value[1:]+'@c.us'
    value=re.sub(r':\d+(?=@)','',value,count=1)
    if value.endswith('@s.whatsapp.net'):value=value.split('@')[0]+'@c.us'
    return value if PEER.fullmatch(value) else None


def peer_kind(value):
    return 'lid' if value.endswith('@lid') else 'group' if value.endswith('@g.us') else 'phone_number'


def cursor_summary(config,now):
    cursors=config.get('_waha_cursors',{})
    cursors=cursors if isinstance(cursors,dict) else {}
    rows=[value for value in list(cursors.values())[:10000] if isinstance(value,dict)]
    result={'chat_offset':integer(config.get('_waha_chat_offset',0)),'cursors_count':len(cursors),'cursors_truncated':len(cursors)>10000,'pending_history_count':sum('ceiling' in value for value in rows),'lid_aliases_count':len(config.get('_waha_lid_pn',{})) if isinstance(config.get('_waha_lid_pn'),dict) else 0}
    for key in ('watermark','ceiling','floor'):
        values=[age(value.get(key),now) for value in rows];values=[value for value in values if value is not None]
        result[key+'_age_min_seconds']=min(values) if values else None
        result[key+'_age_max_seconds']=max(values) if values else None
    offsets=[integer(value.get('offset')) for value in rows];offsets=[value for value in offsets if value is not None]
    result.update(history_offset_min=min(offsets) if offsets else None,history_offset_max=max(offsets) if offsets else None)
    return result


def chat_summary(records):
    if not isinstance(records,list):return {'status':'invalid_response'},[]
    shapes={'string':0,'object':0,'missing':0,'other':0};valid=[];adapter_valid=0;serialized=0
    for row in records[:20]:
        raw=row.get('id') if isinstance(row,dict) else None
        shape='string' if isinstance(raw,str) else 'object' if isinstance(raw,dict) else 'missing' if raw is None else 'other'
        shapes[shape]+=1
        serialized+=isinstance(raw,dict) and isinstance(raw.get('_serialized'),str)
        normal=peer(raw)
        if normal:valid.append(normal)
        # Current adapter coerces id to str before normalization, losing object IDs.
        adapter_valid+=peer(str(raw or '')) is not None
    return {'status':'ok','count':len(records),'rows_truncated':len(records)>20,'id_shapes':shapes,'objects_with_serialized_id':serialized,'normalized_valid_peers':len(valid),'adapter_string_valid_peers':adapter_valid,'lid_peers':sum(value.endswith('@lid') for value in valid)},valid


class Gateway:
    def __init__(self,client):self.client=client;self.calls=0;self.deadline=time.monotonic()+30
    def get(self,path,params=None):
        if self.calls>=MAX_CALLS or time.monotonic()>=self.deadline:return None,{'status':'budget_exhausted'}
        self.calls+=1;code=None
        try:
            with self.client.stream('GET',path,params=params,timeout=min(3,max(.1,self.deadline-time.monotonic()))) as response:
                code=response.status_code
                if type(code) is not int or not 100<=code<=599:return None,{'status':'invalid_response'}
                if not 200<=code<300:return None,{'status':'http_error','http_status':code}
                data=bytearray()
                for part in response.iter_bytes():
                    data.extend(part)
                    if len(data)>1048576:return None,{'status':'response_too_large','http_status':code}
                return json.loads(data),{'status':'ok','http_status':code}
        except Exception:return None,{'status':'request_failed','http_status':code if type(code) is int and 100<=code<=599 else None}


def read_only(db):
    from sqlalchemy import text
    if db.get_bind().dialect.name=='postgresql':
        db.execute(text('SET TRANSACTION READ ONLY'));db.execute(text("SET LOCAL statement_timeout = '2000ms'"))
    elif db.get_bind().dialect.name=='sqlite':db.execute(text('PRAGMA query_only = ON'))
    else:raise ValueError('unsupported_database')


def db_accounts(db,models,unseal,now):
    from sqlalchemy import case,func,select
    Account,Conversation,Message=models
    read_only(db)
    accounts=db.scalars(select(Account).where(Account.channel=='whatsapp_web').order_by(case((Account.state=='connected',0),else_=1),Account.updated_at.desc()).limit(MAX_ACCOUNTS)).all()
    current=datetime.fromtimestamp(now,timezone.utc);result=[]
    for account in accounts:
        config=unseal(account.encrypted_config)
        if not isinstance(config,dict):config={}
        session=config.get('_waha_session')
        session=session if isinstance(session,str) and re.fullmatch(r'omni-[a-f0-9]{32}',session) else None
        public=account.public if isinstance(account.public,dict) else {}
        summary={'account_hash':hashed(str(account.id)),'session_hash':hashed(session),'state':allowed(account.state,STATES),'public_error':allowed(public.get('error'),ERRORS,None if public.get('error') is None else 'unknown_error'),'updated_age_seconds':age(account.updated_at,now),'poll':cursor_summary(config,now),'db_messages':{},'gateway_history':[]}
        query=select(Message.direction,func.sum(case((Message.created_at>=current-timedelta(minutes=15),1),else_=0)),func.sum(case((Message.created_at>=current-timedelta(hours=24),1),else_=0)),func.max(Message.created_at)).join(Conversation,Conversation.id==Message.conversation_id).where(Conversation.account_id==account.id).group_by(Message.direction)
        summary['db_messages']={direction:{'last_15min':0,'last_24h':0,'latest_age_seconds':None} for direction in ('inbound','outbound')}
        for direction,last15,last24,latest in db.execute(query):
            if direction in summary['db_messages']:summary['db_messages'][direction]={'last_15min':int(last15 or 0),'last_24h':int(last24 or 0),'latest_age_seconds':age(latest,now)}
        query=select(Conversation.peer).join(Message,Message.conversation_id==Conversation.id).where(Conversation.account_id==account.id,Message.direction=='outbound').order_by(Message.created_at.desc(),Message.id.desc()).limit(10)
        peers=[]
        for value in db.scalars(query):
            normal=peer(value)
            if normal and normal not in peers:peers.append(normal)
        result.append({'id':account.id,'session':session,'config':config,'summary':summary,'candidates':peers})
    db.rollback()
    return result


def presence(db,models,account_id,records):
    from sqlalchemy import select
    _Account,Conversation,Message=models
    identifiers=[item.get('id') for item in records if isinstance(item,dict) and isinstance(item.get('id'),str) and 0<len(item['id'])<=256]
    if not identifiers:return set()
    read_only(db)
    values=set(db.scalars(select(Message.provider_id).join(Conversation,Conversation.id==Message.conversation_id).where(Conversation.account_id==account_id,Message.provider_id.in_(identifiers))).all())
    db.rollback();return values


def history_summary(records,existing,now):
    if not isinstance(records,list):return {'status':'invalid_response'}
    counts={'inbound':0,'outbound':0,'unknown':0};samples=[];latest={};kinds={}
    for item in records[:10]:
        if not isinstance(item,dict):continue
        direction='outbound' if item.get('fromMe') is True else 'inbound' if item.get('fromMe') is False else 'unknown'
        counts[direction]+=1;stamp,kind=epoch(item.get('timestamp'));kinds[kind]=kinds.get(kind,0)+1
        if stamp is not None:latest[direction]=max(latest.get(direction,0),stamp)
        identifier=item.get('id');valid=isinstance(identifier,str) and 0<len(identifier)<=256
        samples.append({'id_hash':hashed(identifier) if valid else None,'direction':direction,'age_seconds':age(item.get('timestamp'),now),'already_in_account_db':identifier in existing if valid else None})
    return {'status':'ok','returned':len(records),'rows_truncated':len(records)>10,'direction_counts':counts,'timestamp_shapes':kinds,'latest_age_seconds':{key:age(value,now) for key,value in latest.items()},'samples':samples}


def provider_accounts(gateway,items,db_factory,models,now):
    sessions,session_status=gateway.get('/api/sessions',{'all':'true'})
    history_budget=MAX_HISTORY
    for item in items:
        summary=item['summary'];session=item['session'];config=item['config']
        if session is None:summary['gateway']={'status':'session_reference_missing'};continue
        matches=[row for row in sessions if isinstance(row,dict) and row.get('name')==session] if isinstance(sessions,list) else []
        if len(matches)!=1:summary['gateway']={**session_status,'status':'session_not_found' if isinstance(sessions,list) else session_status['status']};continue
        row=matches[0];provider_config=row.get('config') if isinstance(row.get('config'),dict) else {};noweb=provider_config.get('noweb') if isinstance(provider_config.get('noweb'),dict) else {};store=noweb.get('store') if isinstance(noweb.get('store'),dict) else {}
        summary['gateway']={'status':'session_found','provider_state':allowed(row.get('status'),PROVIDER_STATES),'store_enabled':store.get('enabled') if type(store.get('enabled')) is bool else None,'store_full_sync':store.get('fullSync') if type(store.get('fullSync')) is bool else None}
        recent=[]
        for label,params in [('recent_chats',{'limit':10,'offset':0,'sortBy':'conversationTimestamp','sortOrder':'desc'}),('poll_chats',{'limit':10,'offset':integer(config.get('_waha_chat_offset',0)) or 0,'sortBy':'id','sortOrder':'asc'})]:
            rows,status=gateway.get('/api/'+session+'/chats',params);report,peers=chat_summary(rows)
            summary[label]={**report,'request':status}
            if label=='recent_chats':recent=peers
        mappings=config.get('_waha_lid_pn') if isinstance(config.get('_waha_lid_pn'),dict) else {}
        candidates=[]
        for value in item['candidates'][:1]+recent:
            if value not in candidates:candidates.append(value)
            for lid,pn in mappings.items():
                if peer(pn)==value and peer(lid) and peer(lid) not in candidates:candidates.append(peer(lid))
            if len(candidates)>=3:break
        for value in candidates[:history_budget]:
            history_budget-=1
            rows,status=gateway.get('/api/'+session+'/chats/'+quote(value,safe='')+'/messages',{'limit':10,'offset':0,'sortBy':'timestamp','sortOrder':'desc','downloadMedia':'false'})
            existing=set()
            if isinstance(rows,list):
                with db_factory() as db:existing=presence(db,models,item['id'],rows[:10])
            summary['gateway_history'].append({'peer_hash':hashed(value),'peer_kind':peer_kind(value),'alias_present':value in mappings or value in mappings.values(),'request':status,**history_summary(rows,existing,now)})
    return session_status


def diagnose():
    sys.path.insert(0,'/app')
    import httpx
    from app.db import Account,Conversation,Message,Session
    from app.security import unseal
    now=time.time();models=(Account,Conversation,Message)
    report={'diagnostic':'whatsapp_incoming_readonly_v1','result':'ok','captured_at_utc':datetime.fromtimestamp(now,timezone.utc).isoformat(timespec='seconds'),'accounts':[]}
    with Session() as db:items=db_accounts(db,models,unseal,now)
    report['accounts']=[item['summary'] for item in items]
    endpoint=os.environ.get('WAHA_URL','').rstrip('/');key=os.environ.get('WAHA_API_KEY','')
    if endpoint!='http://waha:3000' or not key:return dict(report,result='local_gateway_configuration_unavailable')
    with httpx.Client(base_url=endpoint,headers={'X-Api-Key':key,'Accept':'application/json'},timeout=3,trust_env=False,follow_redirects=False) as client:
        gateway=Gateway(client);report['gateway_sessions']=provider_accounts(gateway,items,Session,models,now);report['gateway_get_calls']=gateway.calls
    return report


def main():
    logging.disable(logging.CRITICAL)
    def expired(*_):raise DeadlineExceeded
    signal.signal(signal.SIGALRM,expired);signal.alarm(40)
    try:report=diagnose()
    except DeadlineExceeded:report={'diagnostic':'whatsapp_incoming_readonly_v1','result':'diagnostic_timeout'}
    except Exception:report={'diagnostic':'whatsapp_incoming_readonly_v1','result':'diagnostic_unavailable'}
    finally:signal.alarm(0)
    print(json.dumps(report,indent=2,ensure_ascii=True))
    return 0 if report.get('result')=='ok' else 1


if __name__=='__main__':raise SystemExit(main())
