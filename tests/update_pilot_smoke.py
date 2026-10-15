"""Standalone updater checks with synthetic source and a fake Docker executable."""
import copy, hashlib, io, json, os, pathlib, subprocess, tarfile, tempfile
SOURCE = (pathlib.Path(__file__).resolve().parents[1] / 'scripts/update_server_pilot.sh').read_text()
HELPER = SOURCE.split('<<\'PY\'\n', 1)[1].split('\nPY\n', 1)[0]
compile(HELPER, '<control.py>', 'exec')

def manifest(root):
    entries = {str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in root.rglob('*') if p.is_file() and str(p.relative_to(root)) != 'docs/source-manifest.json'}
    (root/'docs').mkdir(exist_ok=True)
    (root/'docs/source-manifest.json').write_text(json.dumps(entries))

def write(root, name, value):
    p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(value)

def archive(root, dest, extra=None):
    with tarfile.open(dest,'w:gz') as tf:
        for p in root.rglob('*'):
            if p.is_file():tf.add(p,arcname='omnicrm/'+str(p.relative_to(root)),recursive=False)
        if extra:
            info, content = extra
            tf.addfile(info,io.BytesIO(content) if content is not None else None)

def command(script,*args,ok=True,env=None):
    r=subprocess.run(['python3',str(script),*map(str,args)],text=True,capture_output=True,env=env)
    assert (r.returncode==0)==ok,(args,r.returncode,r.stdout,r.stderr)
    return r

checks=[]
with tempfile.TemporaryDirectory() as tmp:
    base=pathlib.Path(tmp);helper=base/'control.py';helper.write_text(HELPER)
    old=base/'old';new=base/'new';old.mkdir();new.mkdir()
    write(old,'app/value.py','OLD = 1\n');write(old,'app/removed.py','old\n');manifest(old)
    write(old,'.env.pilot','LOCAL_PORT=18580\nENCRYPTION_KEY=secret\n');write(old,'untracked-local.txt','retain me')
    write(new,'app/value.py','NEW = 2\n');write(new,'app/added.py','new\n');manifest(new)
    pack=base/'release.tar.gz';archive(new,pack);stage=base/'stage';stage.mkdir()
    command(helper,'extract',old,pack,stage);backup=base/'backup';backup.mkdir()
    command(helper,'backup',old,stage,backup);command(helper,'install',old,stage,backup)
    assert (old/'app/value.py').read_text()=='NEW = 2\n' and not(old/'app/removed.py').exists()
    assert (old/'.env.pilot').read_text().endswith('secret\n') and (old/'untracked-local.txt').read_text()=='retain me'
    assert not(backup/'source/.env.pilot').exists()
    command(helper,'restore',old,backup);assert(old/'app/removed.py').is_file() and not(old/'app/added.py').exists()
    checks.append('valid install/rollback, deleted/added source, persistent secrets and untracked files preserved, secrets excluded from source backup')
    write(old,'app/value.py','locally changed\n');command(helper,'extract',old,pack,base/'refused-stage',ok=False)
    command(helper,'restore-check',old,backup,ok=False);write(old,'app/value.py','OLD = 1\n')
    checks.append('local source modification rejected for update and rollback')
    write(old,'app/added.py','untracked collision');command(helper,'extract',old,pack,base/'refused-stage',ok=False);(old/'app/added.py').unlink()
    checks.append('untracked file collision rejected')
    (old/'app/value.py').unlink();(old/'app/value.py').symlink_to('/etc/passwd');command(helper,'extract',old,pack,base/'refused-stage',ok=False);(old/'app/value.py').unlink();write(old,'app/value.py','OLD = 1\n')
    checks.append('installed symlink rejected')
    for name,typ in [('omnicrm/../escape',tarfile.REGTYPE),('omnicrm/scripts/link',tarfile.SYMTYPE),('omnicrm/app/extra.py',tarfile.REGTYPE),('omnicrm/.env.pilot',tarfile.REGTYPE),('omnicrm/docs/credentials.har',tarfile.REGTYPE)]:
        info=tarfile.TarInfo(name);info.type=typ;info.size=1 if typ==tarfile.REGTYPE else 0;info.linkname='/etc/passwd'
        archive(new,pack,(info,b'x' if typ==tarfile.REGTYPE else None));command(helper,'extract',old,pack,base/'refused-stage',ok=False)
    checks.append('archive traversal, symlink, unlisted file, .env and HAR rejected')
    write(new,'app/value.py','tampered after manifest');archive(new,pack);command(helper,'extract',old,pack,base/'refused-stage',ok=False)
    checks.append('file digest mismatch rejected')

with tempfile.TemporaryDirectory() as tmp:
    base = pathlib.Path(tmp); helper = base / 'control.py'; helper.write_text(HELPER)
    pinned = 'devlikeapro/waha:noweb-2026.9.2@sha256:be61f8cd7774ab22bd1b755cbc539f98d71d81d5ca83555fdddfa4fddd7924e1'
    options = '--max-old-space-size=16384 --dns-result-order=ipv4first --no-network-family-autoselection'
    appenv = {'DATABASE_URL':'db','ENCRYPTION_KEY':'key','JWT_SECRET':'jwt','WAHA_API_KEY':'wa','LOCAL_TEST_MODE':'true','DEMO_MODE':'false'}
    services = {name: {'image':'omnicrm:pilot','environment':dict(appenv)} for name in ('api','worker','migrate')}
    services['api']['ports'] = [{'host_ip':'127.0.0.1','published':'18580'}]
    services.update({name: {'image':name} for name in ('postgres','waha-volume-init')})
    services['waha'] = {'image':pinned,'environment':{'WAHA_API_KEY':'wa'},'volumes':[{'source':'waha-sessions','target':'/app/.sessions'},{'source':'waha-media','target':'/app/.media'}]}
    before = {'name':'omnicrm-pilot','services':services,'volumes':{'db':{}},'networks':{'private':{}}}
    after = copy.deepcopy(before); after['services']['waha']['environment']['NODE_OPTIONS'] = options
    oldfile, newfile, plan = base/'old.json', base/'new.json', base/'plan'
    oldfile.write_text(json.dumps(before)); newfile.write_text(json.dumps(after))
    command(helper, 'config', base, oldfile, newfile, plan)
    assert plan.read_text() == '1'
    oldfile.write_text(json.dumps(after)); command(helper, 'config', base, oldfile, newfile, plan)
    assert plan.read_text() == '0'
    for change in ('wrong-image','different-option','custom-old-option','empty-old-option','unrelated-env','different-volume','postgres-change'):
        left, right = copy.deepcopy(before), copy.deepcopy(after)
        if change == 'wrong-image':left['services']['waha']['image'] = right['services']['waha']['image'] = 'unapproved:latest'
        elif change == 'different-option':right['services']['waha']['environment']['NODE_OPTIONS'] += ' --trace-warnings'
        elif change == 'custom-old-option':left['services']['waha']['environment']['NODE_OPTIONS'] = '--max-old-space-size=4096'
        elif change == 'empty-old-option':left['services']['waha']['environment']['NODE_OPTIONS'] = ''
        elif change == 'unrelated-env':right['services']['waha']['environment']['WAHA_API_KEY'] = 'changed'
        elif change == 'different-volume':right['services']['waha']['volumes'][0]['source'] = 'other-sessions'
        elif change == 'postgres-change':right['services']['postgres']['image'] = 'postgres:17'
        oldfile.write_text(json.dumps(left)); newfile.write_text(json.dumps(right))
        command(helper, 'config', base, oldfile, newfile, plan, ok=False)
    checks.append('only exact pinned-WAHA absent-to-IPv4 transition accepted; stable options need no recreation; unrelated options/image/volume/database changes rejected')

FAKE_DOCKER='''#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:];base=pathlib.Path(os.environ['TEST_BASE']);log=base/'docker.log'
with log.open('a') as f:f.write(json.dumps(args)+'\\n')
mode=os.environ.get('TEST_MODE','success')
waha_mode=mode.startswith('waha-')
def runtime():
 path=base/'waha-runtime.json'
 if path.exists():return json.loads(path.read_text())
 result={'image':'sha256:'+'b'*64,'mounts':[{'Type':'volume','Name':'omnicrm-pilot_waha-media','Destination':'/app/.media','RW':True},{'Type':'volume','Name':'omnicrm-pilot_waha-sessions','Destination':'/app/.sessions','RW':True}], 'node_default':True,'node_target':False,'node_custom':False,'proxy_configured':False,'generation':0}
 if mode=='waha-custom-options':result.update(node_default=False,node_custom=True)
 if mode=='waha-custom-proxy':result['proxy_configured']=True
 if mode=='waha-image-mismatch':result['image']='sha256:'+'c'*64
 if mode=='waha-volume-mismatch':result['mounts'][0]['Name']='unexpected-volume'
 return result
if args[0]=='info':
 if '--format' in args:print(base)
elif args[0]=='context':print('unix:///var/run/docker.sock')
elif args[0]=='inspect':
 if 'node_default' in args[2]:print(json.dumps(runtime()))
 else:print('sha256:'+'a'*64)
elif args[0]=='image':
 if args[1]=='inspect' and '--format' in args:print('sha256:'+'b'*64)
elif args[0]=='build':
 if mode=='build-failure':sys.exit(41)
elif args[0]=='run':pass
elif args[0]=='compose':
 if 'version' in args:print('v2.test')
 elif 'config' in args:
  env={'DATABASE_URL':'db','ENCRYPTION_KEY':'key','JWT_SECRET':'jwt','WAHA_API_KEY':'wa','LOCAL_TEST_MODE':'true','DEMO_MODE':'false'}
  services={s:{'image':'omnicrm:pilot','environment':env} for s in ('api','worker','migrate')}
  services['api']['ports']=[{'host_ip':'127.0.0.1','published':'18580'}]
  services.update({s:{'image':s} for s in ('postgres','waha','waha-volume-init')})
  if waha_mode:
   services['waha']={'image':'devlikeapro/waha:noweb-2026.9.2@sha256:be61f8cd7774ab22bd1b755cbc539f98d71d81d5ca83555fdddfa4fddd7924e1','environment':{'WAHA_API_KEY':'wa'}}
   if pathlib.Path(args[args.index('-f')+1])!=base/'pilot/compose.pilot.yaml':
    services['waha']['environment']['NODE_OPTIONS']='--max-old-space-size=16384 --dns-result-order=ipv4first --no-network-family-autoselection'
    if mode=='waha-unrelated-env':services['waha']['environment']['WAHA_API_KEY']='different'
  print(json.dumps({'name':'omnicrm-pilot','services':services,'volumes':{'db':{}},'networks':{'private':{}}}))
 elif 'ps' in args:
  if args[-1]=='waha' and waha_mode:print('waha-container-'+str(runtime()['generation']))
  else:print(args[-1]+'-container')
 elif 'exec' in args:print('PRIVATE_DATABASE_DUMP')
 elif 'run' in args and mode=='migration-failure':sys.exit(42)
 elif 'up' in args:
  if args[-1]=='waha':
   state=runtime();target=(base/'pilot/compose.pilot.yaml').read_text()=='test: new\\n'
   state.update(node_default=not target,node_target=target,generation=state['generation']+1)
   (base/'waha-runtime.json').write_text(json.dumps(state))
   if mode=='waha-health-failure' and target and not(base/'waha-up-failed').exists():
    (base/'waha-up-failed').touch();sys.exit(44)
  elif mode in ('health-failure','waha-app-health-failure') and not(base/'up-failed').exists():
   (base/'up-failed').touch();sys.exit(43)
else:sys.exit('Unexpected Docker call: '+repr(args))
'''
for mode in ('success','build-failure','migration-failure','health-failure','waha-success','waha-health-failure','waha-app-health-failure','waha-custom-options','waha-custom-proxy','waha-image-mismatch','waha-volume-mismatch','waha-unrelated-env'):
    with tempfile.TemporaryDirectory() as tmp:
        base=pathlib.Path(tmp);target=base/'pilot';target.mkdir();bin=base/'bin';bin.mkdir()
        wrapped=SOURCE.replace('target=/opt/omnicrm-pilot','target='+str(target)).replace('backup_root=/opt/omnicrm-pilot-updates','backup_root='+str(base/'backups')).replace('/opt/.omnicrm-pilot-update.lock',str(base/'update.lock')).replace('/opt/omnicrm-pilot-update.stage.',str(base/'stage.')).replace('for filesystem in /opt ', 'for filesystem in '+str(base)+' ')
        write(target,'app/value.py','OLD = 1\n');write(target,'scripts/server_pilot.sh','#!/usr/bin/env bash\necho PRIVATE_STATUS_OK\n');write(target,'compose.pilot.yaml','test: old\n');manifest(target);write(target,'.env.pilot','LOCAL_PORT=18580\nENCRYPTION_KEY=keep-me\n')
        release=base/'release';release.mkdir();write(release,'app/value.py','NEW = 2\n');write(release,'scripts/server_pilot.sh','#!/usr/bin/env bash\necho PRIVATE_STATUS_OK\n');write(release,'compose.pilot.yaml','test: new\n');write(release,'scripts/update_server_pilot.sh',wrapped);manifest(release)
        pack=base/'release.tar.gz';archive(release,pack)
        digest=hashlib.sha256(pack.read_bytes()).hexdigest();updater=base/'updater.sh';updater.write_text(wrapped)
        write(bin,'docker',FAKE_DOCKER);(bin/'docker').chmod(0o755)
        write(bin,'id','#!/bin/sh\necho 0\n');(bin/'id').chmod(0o755)
        write(bin,'stat','#!/bin/sh\nif [ \"$2\" = %u ]; then echo 0; else /usr/bin/stat \"$@\"; fi\n');(bin/'stat').chmod(0o755)
        write(bin,'df','#!/bin/sh\nprintf "Filesystem 1024-blocks Used Available Capacity Mounted on\\n/dev/test 99999999 1 99999998 1%% /\\n"\n');(bin/'df').chmod(0o755)
        env=dict(os.environ,PATH=str(bin)+':'+os.environ['PATH'],TEST_BASE=str(base),TEST_MODE=mode)
        r=subprocess.run(['bash',str(updater),str(pack),digest],text=True,capture_output=True,env=env)
        assert (r.returncode==0)==(mode in ('success','waha-success')),(mode,r.returncode,r.stdout,r.stderr)
        calls=[json.loads(line) for line in (base/'docker.log').read_text().splitlines()]
        stops=[i for i,c in enumerate(calls) if c[0]=='compose' and 'stop' in c]
        builds=[i for i,c in enumerate(calls) if c[0]=='build']
        assert all(c[-2:]==['api','worker'] or (mode.startswith('waha-') and 'up' in c and c[-1]=='waha' and '--no-deps' in c and '--no-build' in c and c[c.index('--pull')+1]=='never') for c in calls if c[0]=='compose' and ('stop' in c or 'up' in c))
        assert not any('down' in c or 'rm'==c[0] for c in calls)
        assert (target/'.env.pilot').read_text()=='LOCAL_PORT=18580\nENCRYPTION_KEY=keep-me\n'
        if mode in ('build-failure','waha-custom-options','waha-custom-proxy','waha-image-mismatch','waha-volume-mismatch','waha-unrelated-env'):
            assert not stops and (target/'app/value.py').read_text()=='OLD = 1\n'
            if mode!='build-failure':assert not builds
        else:
            assert builds[0]<stops[0]
            dbs=list((base/'backups').glob('*/database.dump'));assert len(dbs)==1 and dbs[0].stat().st_mode&0o777==0o600
            assert not list((base/'backups').rglob('.env.pilot'))
            if mode in ('success','waha-success'):
                assert (target/'app/value.py').read_text()=='NEW = 2\n'
                rollback=next((base/'backups').glob('*/rollback.sh'))
                rr=subprocess.run(['bash',str(rollback)],env=env,text=True,capture_output=True)
                assert rr.returncode==0,(rr.stdout,rr.stderr)
                assert (target/'app/value.py').read_text()=='OLD = 1\n'
            else:assert (target/'app/value.py').read_text()=='OLD = 1\n'
            if mode.startswith('waha-'):
                backup_dir=dbs[0].parent
                assert (backup_dir/'requires-waha-recreate').is_file()
                saved=json.loads((backup_dir/'waha-runtime.json').read_text())
                final=json.loads((base/'waha-runtime.json').read_text())
                assert final['node_default'] is True and final['node_target'] is False
                assert final['image']==saved['image']=='sha256:'+'b'*64
                assert {m['Name'] for m in final['mounts']}=={m['Name'] for m in saved['volumes']}
                # Success/manual rollback or failure recovery each recreates WAHA twice.
                assert final['generation']==2
        checks.append('mocked complete updater '+mode+(' + manual rollback' if mode in ('success','waha-success') else ''))
for check in checks:print('PASS:',check)
