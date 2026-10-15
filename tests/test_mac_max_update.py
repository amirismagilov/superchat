"""MAX multi-file patch: fake Docker/HTTP and injected file failure, no network."""
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

from test_mac_whatsapp_incoming_update import FAKE

ROOT = Path(__file__).resolve().parents[1]
REVISION = 'c' * 40
FILES = ('app/adapters/max_personal.py', 'app/capabilities.py', 'app/api.py',
         'app/worker.py', 'web/inbox.js', 'web/styles.css')
OLD = {path: '# original ' + path + '\n' for path in FILES}
NEW = {path: '# target ' + path + '\n' for path in FILES}
KEYS = 'LOCAL_PORT=18581\nENCRYPTION_KEY=fixture-persistent-key\nPOSTGRES_PASSWORD=fixture-password\n'
WA = 'CUSTOM_WHATSAPP_WITH_INCOMING_FIX = True\n'
# Fail after three files have already been replaced, but let restoration run.
FAILING_MV = r'''
import json,os,pathlib,subprocess,sys
base=pathlib.Path(os.environ['TEST_FIXTURE']); args=sys.argv[1:]
if os.environ.get('TEST_MODE')=='partial-failure' and args[-2].endswith('/candidate') and args[-1].endswith('/app/worker.py'):
 project=base/'local-crm'
 (base/'partial.json').write_text(json.dumps({p:str((project/p).read_text()) for p in ('app/adapters/max_personal.py','app/capabilities.py','app/api.py','app/worker.py')}))
 sys.exit(45)
sys.exit(subprocess.call(['/bin/mv']+args))
'''


class MacMaxUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base/'local-crm'
        for relative, contents in OLD.items():
            path = self.project/relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents)
            path.chmod(0o640)
        (self.project/'app/adapters/whatsapp_web.py').write_text(WA)
        self.envfile = self.project/'.env.local-docker'
        self.envfile.write_text(KEYS); self.envfile.chmod(0o600)
        (self.project/'compose.local.yaml').write_text('name: omnicrm-local\n')
        (self.project/'.omnicrm-local-source').write_text('a'*40+'\n')
        (self.project/'.omnicrm-wa-incoming-patch').write_text('existing-wa-patch\n')
        self.bin = self.base/'bin'; self.bin.mkdir()
        for name in ('docker', 'curl', 'mv'):
            source = FAILING_MV if name == 'mv' else FAKE.replace('omnicrm:local-wa-candidate-', 'omnicrm:local-max-candidate-')
            path = self.bin/name
            path.write_text('#!'+sys.executable+'\n'+source); path.chmod(0o755)
        self.stage = self.base/'tmp'; self.stage.mkdir()
        self.environment = dict(os.environ, PATH=str(self.bin)+os.pathsep+os.environ['PATH'],
                                TEST_FIXTURE=str(self.base), TMPDIR=str(self.stage))
        for key in ('DOCKER_HOST', 'DOCKER_CONTEXT', 'LOCAL_BUILD_CA_FILE', 'CODEX_PROXY_CERT', 'LOCAL_PORT'):
            self.environment.pop(key, None)
        # Only replace known baseline digests; exercise all real update logic.
        script = (ROOT/'scripts/update_mac_max.sh').read_text()
        digests = '\n'.join('  '+hashlib.sha256(OLD[p].encode()).hexdigest() for p in FILES)
        script, count = re.subn(r'base_hashes=\(\n(?:  [a-f0-9]{64}\n){6}\)',
                               'base_hashes=(\n'+digests+'\n)', script)
        self.assertEqual(count, 1)
        self.updater = self.base/'updater.sh'; self.updater.write_text(script)
        self.archive()

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, bad_type=None, duplicate=False):
        with tarfile.open(self.base/'source.tar.gz', 'w:gz') as archive:
            for relative in FILES:
                entry = tarfile.TarInfo('superchat-'+REVISION+'/'+relative)
                entry.type = bad_type if relative == FILES[-1] and bad_type else tarfile.REGTYPE
                entry.size = len(NEW[relative].encode()) if entry.type == tarfile.REGTYPE else 0
                if entry.type == tarfile.SYMTYPE:
                    entry.linkname = '/etc/passwd'
                archive.addfile(entry, io.BytesIO(NEW[relative].encode()) if entry.size else None)
                if duplicate and relative == FILES[-1]:
                    archive.addfile(entry, io.BytesIO(NEW[relative].encode()))
            # Even an archive containing unrelated files may replace only FILES.
            entry = tarfile.TarInfo('superchat-'+REVISION+'/app/adapters/whatsapp_web.py')
            entry.size = len(b'DO_NOT_COPY\n'); archive.addfile(entry, io.BytesIO(b'DO_NOT_COPY\n'))

    def calls(self):
        path = self.base/'calls.jsonl'
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def run_update(self, mode='success', revision=REVISION, **env):
        return subprocess.run(['bash', str(self.updater), revision, str(self.project)],
                              env=dict(self.environment, TEST_MODE=mode, **env), text=True,
                              input='UNCONSUMED_INPUT\n', capture_output=True, timeout=15)

    def assert_source(self, expected):
        for relative, text in expected.items():
            self.assertEqual((self.project/relative).read_text(), text, relative)

    def assert_preserved(self):
        self.assertEqual(self.envfile.read_text(), KEYS)
        self.assertEqual(self.envfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.project/'app/adapters/whatsapp_web.py').read_text(), WA)
        self.assertEqual((self.project/'.omnicrm-local-source').read_text(), 'a'*40+'\n')
        self.assertEqual((self.project/'.omnicrm-wa-incoming-patch').read_text(), 'existing-wa-patch\n')
        self.assertEqual((self.project/'compose.local.yaml').read_text(), 'name: omnicrm-local\n')
        self.assertFalse((self.project/'.omnicrm-wa-incoming-update.lock').exists())
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertFalse(list((self.project/'backups').glob('**/.env*')))
        for call in self.calls():
            args = call['args']
            if args[0] == 'compose' and 'up' in args:
                self.assertEqual(args[-2:], ['api', 'worker'])
                self.assertIn('--no-deps', args); self.assertIn('--no-build', args)
                self.assertEqual(args[args.index('--pull')+1], 'never')
            for forbidden in ('down', 'restart', 'migrate'):
                self.assertNotIn(forbidden, args)

    def test_success_preserves_wa_keys_runtime_port_and_backs_up_complete_set(self):
        ca = self.base/'private-ca.pem'; ca.write_text('TEST_CA')
        result = self.run_update(TEST_PUBLISHED_ADDRESS='127.0.0.1:19222', LOCAL_PORT='29999', LOCAL_BUILD_CA_FILE=str(ca))
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assert_source(NEW); self.assert_preserved()
        self.assertEqual((self.project/'.omnicrm-max-patch').read_text(), REVISION+'\n')
        backups = list((self.project/'backups').glob('max-*')); self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o700)
        for relative in FILES:
            self.assertEqual((backups[0]/relative).read_text(), OLD[relative])
        state = json.loads((self.base/'runtime.json').read_text())
        self.assertEqual(state['active'], 'sha256:'+'b'*64); self.assertEqual(state['address'], '127.0.0.1:19222')
        calls = self.calls(); build = next(c['args'] for c in calls if c['args'][0] == 'build')
        self.assertEqual(build[build.index('--secret')+1], 'id=proxy_ca,src='+str(ca))
        self.assertEqual([c['stdin'] for c in calls if 'exec' in c['args']], [''])
        self.assertEqual([c['local_port'] for c in calls if 'up' in c['args']], ['19222'])
        self.assertFalse(list(self.project.rglob('*.pem')))

    def test_partial_replacement_restores_every_original_before_build(self):
        result = self.run_update('partial-failure')
        self.assertNotEqual(result.returncode, 0)
        partial = json.loads((self.base/'partial.json').read_text())
        for relative in FILES[:3]:
            self.assertEqual(partial[relative], NEW[relative])
        self.assertEqual(partial[FILES[3]], OLD[FILES[3]])
        self.assert_source(OLD); self.assert_preserved()
        for relative in FILES:
            self.assertEqual((self.project/relative).stat().st_mode & 0o777, 0o640)
        self.assertFalse(any(c['args'][0] == 'build' or 'up' in c['args'] for c in self.calls()))

    def test_build_and_candidate_failure_restore_all_without_container_cutover(self):
        for mode in ('build-failure', 'candidate-failure'):
            with self.subTest(mode=mode):
                result = self.run_update(mode)
                self.assertNotEqual(result.returncode, 0)
                self.assert_source(OLD); self.assert_preserved()
                self.assertFalse(any('up' in c['args'] for c in self.calls()))
                self.assertFalse((self.project/'.omnicrm-max-patch').exists())

    def test_health_and_runtime_failure_restore_all_sources_image_and_port(self):
        for mode in ('health-failure', 'runtime-failure'):
            with self.subTest(mode=mode):
                statefile = self.base/'runtime.json'
                if statefile.exists(): statefile.unlink()
                result = self.run_update(mode, TEST_PUBLISHED_ADDRESS='127.0.0.1:19222')
                self.assertNotEqual(result.returncode, 0)
                self.assert_source(OLD); self.assert_preserved()
                state = json.loads(statefile.read_text())
                self.assertEqual(state['active'], 'sha256:'+'a'*64)
                self.assertEqual(state['tag'], state['active']); self.assertEqual(state['up_count'], 2)
                self.assertEqual(state['address'], '127.0.0.1:19222')
                self.assertFalse((self.project/'.omnicrm-max-patch').exists())

    def test_last_custom_file_refuses_whole_patch_before_first_replacement(self):
        expected = dict(OLD); expected[FILES[-1]] = 'CUSTOM_CSS = keep\n'
        (self.project/FILES[-1]).write_text(expected[FILES[-1]])
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(expected); self.assert_preserved()
        self.assertFalse((self.project/'backups').exists())
        self.assertFalse(any(c['args'][0] == 'build' or 'up' in c['args'] for c in self.calls()))

    def test_mixed_base_target_set_can_be_retried_and_backup_keeps_original_mix(self):
        (self.project/FILES[0]).write_text(NEW[FILES[0]])
        result = self.run_update('health-failure')
        self.assertNotEqual(result.returncode, 0)
        expected = dict(OLD); expected[FILES[0]] = NEW[FILES[0]]
        self.assert_source(expected); self.assert_preserved()
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stderr); self.assert_source(NEW); self.assert_preserved()

    def test_late_archive_symlink_or_duplicate_refused_before_replacement(self):
        for bad_type, duplicate in ((tarfile.SYMTYPE, False), (None, True)):
            with self.subTest(bad_type=bad_type, duplicate=duplicate):
                self.archive(bad_type, duplicate)
                result = self.run_update()
                self.assertNotEqual(result.returncode, 0)
                self.assert_source(OLD); self.assert_preserved()
                self.assertFalse((self.project/'backups').exists())

    def test_symlink_parent_or_environment_refused_before_download(self):
        web = self.project/'web'; web.rename(self.base/'outside-web'); web.symlink_to(self.base/'outside-web')
        result = self.run_update(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        web.unlink(); (self.base/'outside-web').rename(web)
        self.envfile.rename(self.base/'keys'); self.envfile.symlink_to(self.base/'keys')
        result = self.run_update(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), []); self.assert_source(OLD)

    def test_remote_context_invalid_revision_and_shared_lock_refuse(self):
        result = self.run_update(revision='main'); self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        result = self.run_update('remote'); self.assertNotEqual(result.returncode, 0)
        lock = self.project/'.omnicrm-wa-incoming-update.lock'; lock.mkdir(); (lock/'owner').write_text('other-update')
        result = self.run_update(); self.assertNotEqual(result.returncode, 0)
        self.assertEqual((lock/'owner').read_text(), 'other-update')
        self.assertFalse(any(c['tool'] == 'curl' for c in self.calls())); self.assert_source(OLD)


if __name__ == '__main__':
    unittest.main()
