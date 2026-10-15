"""Standalone updater checks with synthetic source and a fake Docker executable."""
import hashlib, io, json, os, pathlib, subprocess, tarfile, tempfile
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

FAKE_DOCKER='''#!/usr/bin/env python3
import json,os,pathlib,sys
args=sys.argv[1:];base=pathlib.Path(os.environ['TEST_BASE']);log=base/'docker.log'
with log.open('a') as f:f.write(json.dumps(args)+'\\n')
mode=os.environ.get('TEST_MODE','success')
if args[0]=='info':
 if '--format' in args:print(base)
elif args[0]=='context':print('unix:///var/run/docker.sock')
elif args[0]=='inspect':print('sha256:'+'a'*64)
elif args[0]=='image':pass
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
  print(json.dumps({'name':'omnicrm-pilot','services':services,'volumes':{'db':{}},'networks':{'private':{}}}))
 elif 'ps' in args:print(args[-1]+'-container')
 elif 'exec' in args:print('PRIVATE_DATABASE_DUMP')
 elif 'run' in args and mode=='migration-failure':sys.exit(42)
 elif 'up' in args and mode=='health-failure' and not(base/'up-failed').exists():
  (base/'up-failed').touch();sys.exit(43)
else:sys.exit('Unexpected Docker call: '+repr(args))
'''
for mode in ('success','build-failure','migration-failure','health-failure'):
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
        assert (r.returncode==0)==(mode=='success'),(mode,r.returncode,r.stdout,r.stderr)
        calls=[json.loads(line) for line in (base/'docker.log').read_text().splitlines()]
        stops=[i for i,c in enumerate(calls) if c[0]=='compose' and 'stop' in c]
        builds=[i for i,c in enumerate(calls) if c[0]=='build']
        assert all(c[-2:]==['api','worker'] for c in calls if c[0]=='compose' and ('stop' in c or 'up' in c))
        assert not any('down' in c or 'rm'==c[0] for c in calls)
        assert (target/'.env.pilot').read_text()=='LOCAL_PORT=18580\nENCRYPTION_KEY=keep-me\n'
        if mode=='build-failure':assert not stops and (target/'app/value.py').read_text()=='OLD = 1\n'
        else:
            assert builds[0]<stops[0]
            dbs=list((base/'backups').glob('*/database.dump'));assert len(dbs)==1 and dbs[0].stat().st_mode&0o777==0o600
            assert not list((base/'backups').rglob('.env.pilot'))
            if mode=='success':
                assert (target/'app/value.py').read_text()=='NEW = 2\n'
                rollback=next((base/'backups').glob('*/rollback.sh'))
                rr=subprocess.run(['bash',str(rollback)],env=env,text=True,capture_output=True)
                assert rr.returncode==0,(rr.stdout,rr.stderr)
                assert (target/'app/value.py').read_text()=='OLD = 1\n'
            else:assert (target/'app/value.py').read_text()=='OLD = 1\n'
        checks.append('mocked complete updater '+mode+(' + manual rollback' if mode=='success' else ''))
for check in checks:print('PASS:',check)
