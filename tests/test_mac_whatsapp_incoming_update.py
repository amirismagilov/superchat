"""Exercise adapter-only update and recovery with fake Docker/HTTP, no network."""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
REVISION='b'*40
OLD='OLD = 1\n'
NEW='NEW = 2\n'
FAKE=r'''
import json,os,pathlib,sys
args=sys.argv[1:];base=pathlib.Path(os.environ['TEST_FIXTURE']);name=pathlib.Path(sys.argv[0]).name
statepath=base/'runtime.json'
state=json.loads(statepath.read_text()) if statepath.exists() else {'tag':'sha256:'+'a'*64,'active':'sha256:'+'a'*64,'up_count':0,'address':os.environ.get('TEST_PUBLISHED_ADDRESS','127.0.0.1:18581')}
entry={'tool':name,'args':args,'local_port':os.environ.get('LOCAL_PORT')}
if name=='docker' and 'exec' in args:entry['stdin']=sys.stdin.read()
with (base/'calls.jsonl').open('a') as handle:handle.write(json.dumps(entry)+'\n')
mode=os.environ.get('TEST_MODE','success');result=0
if name=='curl':pathlib.Path(args[args.index('-o')+1]).write_bytes((base/'source.tar.gz').read_bytes())
elif args[0]=='context':print('ssh://remote' if mode=='remote' else 'unix:///var/run/docker.sock')
elif args[0]=='info':pass
elif args[0]=='inspect':print(state['active'])
elif args[0]=='image':
 if args[1]=='inspect':print(state['tag'])
 elif args[1]=='tag' and args[-1]=='omnicrm:local':state['tag']='sha256:'+'b'*64 if args[2].startswith('omnicrm:local-wa-candidate-') else args[2]
elif args[0]=='build':
 if mode=='build-failure':result=41
elif args[0]=='run':
 if mode=='candidate-failure':result=42
elif args[0]=='compose':
 if 'version' in args:print('v2.fake')
 elif 'ps' in args:print(args[-1]+'-container')
 elif 'port' in args:print(state['address'])
 elif 'up' in args:
  state['up_count']+=1;state['active']=state['tag']
  state['address']='127.0.0.1:'+os.environ.get('LOCAL_PORT','18581')
  if mode=='health-failure' and state['up_count']==1:result=43
 elif 'exec' in args and mode=='runtime-failure':result=44
else:result=99
statepath.write_text(json.dumps(state))
sys.exit(result)
'''


class MacIncomingUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.project=self.base/'local-crm';(self.project/'app/adapters').mkdir(parents=True)
        self.adapter=self.project/'app/adapters/whatsapp_web.py';self.adapter.write_text(OLD)
        self.envfile=self.project/'.env.local-docker';self.envfile.write_text('LOCAL_PORT=18581\nENCRYPTION_KEY=fixture-persistent-key\nPOSTGRES_PASSWORD=fixture-password\n');self.envfile.chmod(0o600)
        (self.project/'compose.local.yaml').write_text('name: omnicrm-local\n')
        (self.project/'.omnicrm-local-source').write_text('a'*40+'\n')
        self.bin=self.base/'bin';self.bin.mkdir()
        for name in ('docker','curl'):
            path=self.bin/name;path.write_text('#!'+sys.executable+'\n'+FAKE);path.chmod(0o755)
        self.stage=self.base/'tmp';self.stage.mkdir()
        self.environment=dict(os.environ,PATH=str(self.bin)+os.pathsep+os.environ['PATH'],TEST_FIXTURE=str(self.base),TMPDIR=str(self.stage))
        for key in ('DOCKER_HOST','DOCKER_CONTEXT','LOCAL_BUILD_CA_FILE','CODEX_PROXY_CERT','LOCAL_PORT'):
            self.environment.pop(key,None)
        source=(ROOT/'scripts/update_mac_whatsapp_incoming.sh').read_text()
        # Synthetic source replaces only the known baseline digest, retaining the
        # production control flow and all immutable-target/hash comparisons.
        source,count=re.subn(r"base_hash='[a-f0-9]{64}'","base_hash='"+hashlib.sha256(OLD.encode()).hexdigest()+"'",source)
        self.assertEqual(count,1)
        self.updater=self.base/'updater.sh';self.updater.write_text(source)
        self.archive()

    def tearDown(self):self.temp.cleanup()

    def archive(self,entry_type=tarfile.REGTYPE):
        with tarfile.open(self.base/'source.tar.gz','w:gz') as archive:
            entry=tarfile.TarInfo('superchat-'+REVISION+'/app/adapters/whatsapp_web.py');entry.type=entry_type
            entry.size=len(NEW.encode()) if entry_type==tarfile.REGTYPE else 0
            if entry_type==tarfile.SYMTYPE:entry.linkname='/etc/passwd'
            archive.addfile(entry,io.BytesIO(NEW.encode()) if entry.size else None)

    def calls(self):
        path=self.base/'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_update(self,mode='success',**env):
        return subprocess.run(['bash',str(self.updater),REVISION,str(self.project)],env=dict(self.environment,TEST_MODE=mode,**env),text=True,input='UNCONSUMED_INPUT\n',capture_output=True,timeout=10)

    def assert_preserved(self):
        self.assertEqual(self.envfile.read_text(),'LOCAL_PORT=18581\nENCRYPTION_KEY=fixture-persistent-key\nPOSTGRES_PASSWORD=fixture-password\n')
        self.assertEqual(self.envfile.stat().st_mode&0o777,0o600)
        self.assertEqual((self.project/'.omnicrm-local-source').read_text(),'a'*40+'\n')
        self.assertEqual((self.project/'compose.local.yaml').read_text(),'name: omnicrm-local\n')
        self.assertFalse((self.project/'.omnicrm-wa-incoming-update.lock').exists())
        self.assertEqual(list(self.stage.iterdir()),[])
        self.assertFalse(list((self.project/'backups').glob('**/.env*')))
        for call in self.calls():
            args=call['args']
            if call['tool']=='docker' and args[0]=='compose' and 'up' in args:
                self.assertEqual(args[-2:],['api','worker'])
                self.assertIn('--no-deps',args);self.assertIn('--no-build',args)
                self.assertEqual(args[args.index('--pull')+1],'never')
            self.assertNotIn('down',args);self.assertNotIn('restart',args);self.assertNotIn('migrate',args)

    def test_success_preserves_keys_gateway_database_and_only_updates_api_worker(self):
        result=self.run_update()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.adapter.read_text(),NEW);self.assert_preserved()
        self.assertEqual(json.loads((self.base/'runtime.json').read_text())['active'],'sha256:'+'b'*64)
        patch=(self.project/'.omnicrm-wa-incoming-patch').read_text()
        self.assertEqual(patch,REVISION+' '+hashlib.sha256(NEW.encode()).hexdigest()+'\n')
        calls=self.calls();build=next(i for i,c in enumerate(calls) if c['args'][0]=='build');up=next(i for i,c in enumerate(calls) if 'up' in c['args'])
        self.assertLess(build,up)
        health=[c for c in calls if 'exec' in c['args']];self.assertEqual(health[0]['stdin'],'')
        backup=list((self.project/'backups').glob('wa-incoming-*'));self.assertEqual(len(backup),1)
        self.assertEqual((backup[0]/'whatsapp_web.py').read_text(),OLD)
        self.assertEqual(backup[0].stat().st_mode&0o777,0o700)
        self.assertTrue(any(c['args'][:2]==['image','tag'] and c['args'][-1].startswith('omnicrm:local-wa-rollback-') for c in calls))

    def test_build_and_candidate_failure_restore_source_without_container_cutover(self):
        for mode in ('build-failure','candidate-failure'):
            with self.subTest(mode=mode):
                result=self.run_update(mode)
                self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),OLD);self.assert_preserved()
                self.assertFalse(any('up' in c['args'] for c in self.calls()))
                self.assertEqual(json.loads((self.base/'runtime.json').read_text())['active'],'sha256:'+'a'*64)
                self.assertFalse((self.project/'.omnicrm-wa-incoming-patch').exists())

    def test_failed_health_restores_previous_image_source_and_runtime(self):
        result=self.run_update('health-failure')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),OLD);self.assert_preserved()
        state=json.loads((self.base/'runtime.json').read_text())
        self.assertEqual(state['tag'],'sha256:'+'a'*64);self.assertEqual(state['active'],'sha256:'+'a'*64);self.assertEqual(state['up_count'],2)
        self.assertFalse((self.project/'.omnicrm-wa-incoming-patch').exists())

    def test_runtime_profile_failure_also_restores_previous_runtime(self):
        result=self.run_update('runtime-failure')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),OLD);self.assert_preserved()
        self.assertEqual(json.loads((self.base/'runtime.json').read_text())['active'],'sha256:'+'a'*64)

    def test_actual_published_port_is_preserved_when_environment_and_shell_differ(self):
        result=self.run_update(TEST_PUBLISHED_ADDRESS='127.0.0.1:19222',LOCAL_PORT='29999')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assert_preserved()
        self.assertEqual(json.loads((self.base/'runtime.json').read_text())['address'],'127.0.0.1:19222')
        up=[c for c in self.calls() if 'up' in c['args']]
        self.assertEqual([c['local_port'] for c in up],['19222'])

    def test_health_failure_rollback_keeps_actual_published_port(self):
        result=self.run_update('health-failure',TEST_PUBLISHED_ADDRESS='127.0.0.1:19222',LOCAL_PORT='29999')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),OLD);self.assert_preserved()
        state=json.loads((self.base/'runtime.json').read_text())
        self.assertEqual(state['address'],'127.0.0.1:19222');self.assertEqual(state['active'],'sha256:'+'a'*64)
        up=[c for c in self.calls() if 'up' in c['args']]
        self.assertEqual([c['local_port'] for c in up],['19222','19222'])

    def test_invalid_or_multiple_published_bindings_refused_before_download(self):
        for address in ('127.0.0.1:0','127.0.0.1:65536','0.0.0.0:18581','127.0.0.1:18581\n0.0.0.0:18581'):
            with self.subTest(address=address):
                statefile=self.base/'runtime.json'
                if statefile.exists():statefile.unlink()
                result=self.run_update(TEST_PUBLISHED_ADDRESS=address)
                self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),OLD);self.assert_preserved()
                self.assertFalse(any(c['tool']=='curl' or c['args'][0]=='build' or 'up' in c['args'] for c in self.calls()))

    def test_custom_source_refused_without_build_or_backup(self):
        self.adapter.write_text('CUSTOM = "retain"\n');result=self.run_update()
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),'CUSTOM = "retain"\n');self.assert_preserved()
        self.assertFalse(any(c['args'][0]=='build' or 'up' in c['args'] for c in self.calls()))
        self.assertFalse((self.project/'backups').exists())

    def test_target_source_can_be_reapplied_to_finish_previously_interrupted_build(self):
        self.adapter.write_text(NEW);result=self.run_update()
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(self.adapter.read_text(),NEW);self.assert_preserved()
        self.assertEqual(json.loads((self.base/'runtime.json').read_text())['active'],'sha256:'+'b'*64)

    def test_remote_context_and_symlink_environment_refused_before_download(self):
        result=self.run_update('remote');self.assertNotEqual(result.returncode,0)
        self.assertFalse(any(c['tool']=='curl' for c in self.calls()));self.assertEqual(self.adapter.read_text(),OLD)
        self.envfile.rename(self.project/'private.env');self.envfile.symlink_to(self.project/'private.env')
        before=len(self.calls());result=self.run_update()
        self.assertNotEqual(result.returncode,0);self.assertEqual(len(self.calls()),before)

    def test_archive_symlink_rejected_without_replacing_source(self):
        self.archive(tarfile.SYMTYPE);result=self.run_update()
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.adapter.read_text(),OLD);self.assert_preserved()
        self.assertFalse(any(c['args'][0]=='build' for c in self.calls()))

    def test_optional_build_ca_is_passed_as_secret_not_copied(self):
        ca=self.base/'private-ca.pem';ca.write_text('TEST_CA_ONLY')
        result=self.run_update(LOCAL_BUILD_CA_FILE=str(ca))
        self.assertEqual(result.returncode,0,result.stderr);self.assert_preserved()
        build=next(c['args'] for c in self.calls() if c['args'][0]=='build')
        self.assertEqual(build[build.index('--secret')+1],'id=proxy_ca,src='+str(ca))
        self.assertFalse(list(self.project.rglob('*.pem')))


if __name__=='__main__':unittest.main()
