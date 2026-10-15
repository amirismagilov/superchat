"""Exercise the Mac installer/local launcher without Docker or external network."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'a' * 40

FAKE = r'''
import json,os,pathlib,subprocess,sys
name=pathlib.Path(sys.argv[0]).name;args=sys.argv[1:]
base=pathlib.Path(os.environ['TEST_FIXTURE'])
entry={'tool':name,'args':args,'telegram_proxy_present':'TELEGRAM_PROXY_URL' in os.environ}
if name=='docker' and 'exec' in args:entry['stdin']=sys.stdin.read()
with (base/'calls.jsonl').open('a') as handle:handle.write(json.dumps(entry)+'\n')
if name=='curl':
 destination=pathlib.Path(args[args.index('-o')+1])
 if os.environ.get('TEST_CURL_FAIL')=='1':destination.write_bytes(b'partial');sys.exit(7)
 destination.write_bytes((base/'source.tar.gz').read_bytes())
elif name=='tar':
 sys.exit(subprocess.call([os.environ['TEST_REAL_TAR'],*args]))
elif name=='docker':
 if args[:2]==['context','inspect']:print(os.environ.get('TEST_DOCKER_ENDPOINT','unix:///var/run/docker.sock'))
 elif args[0]=='info':
  if '--format' in args:print('aarch64')
 elif args[:2]==['volume','ls']:
  if os.environ.get('TEST_EXISTING_VOLUMES')=='1':print('omnicrm-local_postgres-data')
 elif args[0]=='run':
  root=pathlib.Path(args[args.index('--volume')+1].rsplit(':/workspace',1)[0])
  port=args[args.index('--env')+1].split('=',1)[1]
  sys.exit(subprocess.call([sys.executable,str(root/'scripts/init_local_docker.py'),'--architecture','aarch64','--port',port],stdin=subprocess.DEVNULL))
 elif args[0]=='compose':
  if 'version' in args:print('v2.fake')
  elif '--help' in args:print('--wait-timeout')
  elif 'port' in args:print('127.0.0.1:18581')
 else:sys.exit(8)
else:sys.exit(9)
'''

START_STUB = '''#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname -- "${BASH_SOURCE[0]}")/.."
if [ ! -f .env.local-docker ]; then
  printf 'LOCAL_PORT=%s\nENCRYPTION_KEY=fixture-persistent-key\n' "${LOCAL_PORT:-missing}" > .env.local-docker
fi
printf '%s\n' "${LOCAL_PORT:-unset}" >> "$TEST_FIXTURE/launches.txt"
'''


class MacLocalInstallTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.base = Path(self.temporary.name)
        self.bin = self.base/'bin';self.bin.mkdir()
        self.destination = self.base/'Projects'/'omnicrm-local'
        self.env = dict(os.environ,TEST_FIXTURE=str(self.base),TEST_REAL_TAR=shutil.which('tar'),OMNICRM_LOCAL_DIR=str(self.destination),PATH=str(self.bin)+os.pathsep+os.environ['PATH'])
        for key in ('DOCKER_HOST','DOCKER_CONTEXT','LOCAL_PORT','CODEX_PROXY_CERT','LOCAL_BUILD_CA_FILE','DATABASE_URL','ENCRYPTION_KEY','JWT_SECRET','WAHA_API_KEY','POSTGRES_PASSWORD','WAHA_IMAGE','TELEGRAM_PROXY_URL'):
            self.env.pop(key,None)
        for name in ('docker','curl','tar'):
            file=self.bin/name;file.write_text('#!'+sys.executable+'\n'+FAKE);file.chmod(0o755)
        self.make_archive()

    def tearDown(self):self.temporary.cleanup()

    def make_archive(self, revision=REVISION):
        with tarfile.open(self.base/'source.tar.gz','w:gz') as archive:
            for name,body in [('compose.local.yaml','name: omnicrm-local\n'),('scripts/local_start.sh',START_STUB)]:
                content=body.encode();entry=tarfile.TarInfo('superchat-'+revision+'/'+name);entry.size=len(content);entry.mode=0o755 if name.endswith('.sh') else 0o644
                archive.addfile(entry,io.BytesIO(content))

    def calls(self,tool=None):
        path=self.base/'calls.jsonl'
        calls=[json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        return [call for call in calls if tool is None or call['tool']==tool]

    def install(self,revision=REVISION,**environment):
        return subprocess.run(['bash',str(ROOT/'scripts/install_mac_local.sh'),revision],env=dict(self.env,**environment),text=True,capture_output=True,timeout=10)

    def local_tree(self):
        self.destination.mkdir(parents=True);(self.destination/'scripts').mkdir()
        for name in ('local_start.sh','init_local_docker.py'):
            shutil.copyfile(ROOT/'scripts'/name,self.destination/'scripts'/name)
        (self.destination/'compose.local.yaml').write_text('name: omnicrm-local\n')
        return self.destination/'scripts/local_start.sh'

    def test_requires_full_lowercase_commit_before_any_external_command(self):
        for revision in ('','main','a'*39,'A'*40,'../escape'):
            with self.subTest(revision=revision):self.assertEqual(self.install(revision).returncode,2)
        self.assertEqual(self.calls(),[]);self.assertFalse(self.destination.exists())

    def test_fresh_install_uses_pinned_archive_and_local_port_18581(self):
        result=self.install()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual((self.destination/'.omnicrm-local-source').read_text(),REVISION+'\n')
        self.assertIn('LOCAL_PORT=18581\n',(self.destination/'.env.local-docker').read_text())
        self.assertEqual((self.base/'launches.txt').read_text(),'18581\n')
        curl=self.calls('curl');self.assertEqual(len(curl),1)
        self.assertIn('https://codeload.github.com/amirismagilov/superchat/tar.gz/'+REVISION,curl[0]['args'])
        self.assertIn('=https',curl[0]['args']);self.assertIn('--max-time',curl[0]['args'])
        self.assertEqual(len(self.calls('tar')),1)
        self.assertEqual(list(self.destination.parent.glob('.omnicrm-download.*')),[])

    def test_rerun_preserves_credentials_and_source_without_redownload(self):
        self.assertEqual(self.install().returncode,0)
        secret=self.destination/'.env.local-docker';secret.write_text('LOCAL_PORT=19222\nENCRYPTION_KEY=do-not-replace\n')
        before=secret.read_bytes();result=self.install('b'*40)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(secret.read_bytes(),before)
        self.assertEqual((self.destination/'.omnicrm-local-source').read_text(),REVISION+'\n')
        self.assertEqual(len(self.calls('curl')),1);self.assertEqual(len(self.calls('tar')),1)
        self.assertEqual((self.base/'launches.txt').read_text(),'18581\nunset\n')

    def test_failed_download_cleans_stage_without_partial_install(self):
        result=self.install(TEST_CURL_FAIL='1')
        self.assertNotEqual(result.returncode,0);self.assertFalse(self.destination.exists())
        self.assertEqual(self.calls('tar'),[])
        self.assertEqual(list(self.destination.parent.glob('.omnicrm-download.*')),[])

    def test_archive_for_other_revision_is_not_installed(self):
        self.make_archive('b'*40);result=self.install()
        self.assertNotEqual(result.returncode,0);self.assertFalse(self.destination.exists())
        self.assertEqual(list(self.destination.parent.glob('.omnicrm-download.*')),[])

    def test_existing_project_volumes_block_before_download_or_key_creation(self):
        result=self.install(TEST_EXISTING_VOLUMES='1')
        self.assertNotEqual(result.returncode,0);self.assertEqual(self.calls('curl'),[])
        self.assertFalse(self.destination.exists());self.assertFalse((self.base/'launches.txt').exists())
        self.assertTrue(any(call['args'][:2]==['volume','ls'] for call in self.calls('docker')))

    def test_existing_unrelated_directory_is_not_modified(self):
        self.destination.mkdir(parents=True);file=self.destination/'important.txt';file.write_text('retain unrelated data')
        result=self.install()
        self.assertNotEqual(result.returncode,0);self.assertEqual(file.read_text(),'retain unrelated data')
        self.assertEqual(list(self.destination.iterdir()),[file]);self.assertEqual(self.calls('curl'),[])

    def test_remote_docker_context_is_refused_without_download(self):
        result=self.install(TEST_DOCKER_ENDPOINT='ssh://remote-server')
        self.assertNotEqual(result.returncode,0);self.assertFalse(self.destination.exists());self.assertEqual(self.calls('curl'),[])
        self.assertFalse(any(call['args'][0] in {'info','run','volume'} for call in self.calls('docker')))

    def test_local_start_refuses_orphaned_volumes_before_initializer_or_compose_up(self):
        script=self.local_tree()
        result=subprocess.run(['bash',str(script),'--no-build','--no-open'],env=dict(self.env,TEST_EXISTING_VOLUMES='1'),text=True,capture_output=True,timeout=10)
        self.assertNotEqual(result.returncode,0);self.assertFalse((self.destination/'.env.local-docker').exists())
        self.assertFalse(any(call['args'][0]=='run' for call in self.calls('docker')))
        self.assertFalse(any(call['args'][0]=='compose' and 'up' in call['args'] and '--help' not in call['args'] for call in self.calls('docker')))

    def test_local_health_check_cannot_consume_remaining_launcher_stdin(self):
        script=self.local_tree()
        environment=dict(self.env,TEST_LOCAL_SCRIPT=str(script),LOCAL_PORT='18581',TELEGRAM_PROXY_URL='http://old-private-proxy.invalid')
        command='bash "$TEST_LOCAL_SCRIPT" --no-build --no-open; status=$?; IFS= read -r marker; printf "TAIL:%s\\n" "$marker"; exit "$status"'
        result=subprocess.run(['bash','-c',command],env=environment,input='still-available-after-launch\n',text=True,capture_output=True,timeout=10)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertIn('TAIL:still-available-after-launch',result.stdout)
        health=[call for call in self.calls('docker') if 'exec' in call['args']]
        self.assertEqual(len(health),1);self.assertEqual(health[0]['stdin'],'')
        actual_compose=[call for call in self.calls('docker') if call['args'][0]=='compose' and '--project-name' in call['args']]
        self.assertTrue(actual_compose);self.assertTrue(all(not call['telegram_proxy_present'] for call in actual_compose))
        config=(self.destination/'.env.local-docker').read_text()
        self.assertIn('LOCAL_PORT=18581\n',config)
        self.assertIn('devlikeapro/waha:noweb-arm-2026.9.2@sha256:',config)
        self.assertEqual((self.destination/'.env.local-docker').stat().st_mode&0o777,0o600)


if __name__=='__main__':unittest.main()
