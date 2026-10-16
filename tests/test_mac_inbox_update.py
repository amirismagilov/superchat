"""Mac inbox update/migration ordering and recovery using fake Docker/HTTP."""
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


ROOT = Path(__file__).resolve().parents[1]
REVISION = "d" * 40
FILES = (
    "app/db.py", "app/services.py", "app/api.py", "app/worker.py", "app/capabilities.py",
    "app/adapters/base.py", "app/adapters/telegram.py", "app/adapters/whatsapp_web.py",
    "app/adapters/max_personal.py", "web/inbox.js", "web/styles.css", "scripts/migrate.py",
)
OLD = {path: "# original " + path + "\n" for path in FILES}
PATCHED = {path: "# known previous patch " + path + "\n" for path in FILES}
NEW = {path: "# target " + path + "\n" for path in FILES}
KEYS = "LOCAL_PORT=18581\nENCRYPTION_KEY=fixture-persistent-key\nPOSTGRES_PASSWORD=fixture-password\n"
DUMP = "PGDMP fixture private history; encrypted credentials=ciphertext\n"
OLD_IMAGE = "sha256:" + "a" * 64
NEW_IMAGE = "sha256:" + "b" * 64

FAKE = r'''
import json,os,pathlib,subprocess,sys
args=sys.argv[1:]; base=pathlib.Path(os.environ['TEST_FIXTURE']); name=pathlib.Path(sys.argv[0]).name
mode=os.environ.get('TEST_MODE','success'); statepath=base/'runtime.json'; project=base/'local-crm'
state=json.loads(statepath.read_text()) if statepath.exists() else {
 'tag':'sha256:'+'a'*64,'active':'sha256:'+'a'*64,'up_count':0,'stop_count':0,
 'stopped':False,'schema':2,'migration_count':0,'address':os.environ.get('TEST_PUBLISHED_ADDRESS','127.0.0.1:18581')}
entry={'tool':name,'args':args,'local_port':os.environ.get('LOCAL_PORT'),
 'inherited_credentials':[key for key in ('DATABASE_URL','ENCRYPTION_KEY','JWT_SECRET','WAHA_API_KEY','POSTGRES_PASSWORD','SMS_GATEWAY_URL','WAHA_IMAGE','TELEGRAM_PROXY_URL') if key in os.environ]}
if name=='docker' and ('exec' in args or args[0]=='run' or (args[0]=='compose' and 'run' in args)):
 entry['stdin']=sys.stdin.read()
if name=='docker' and args[0]=='compose' and 'run' in args:
 override=json.loads(pathlib.Path(args[[i for i,x in enumerate(args) if x=='-f'][-1]+1]).read_text())
 entry['override']=override
with (base/'calls.jsonl').open('a') as output: output.write(json.dumps(entry)+'\n')
result=0
if name=='curl':
 pathlib.Path(args[args.index('-o')+1]).write_bytes((base/'source.tar.gz').read_bytes())
elif name=='mv':
 if mode=='partial-failure' and args[-2].endswith('/candidate') and args[-1].endswith('/app/capabilities.py'):
  (base/'partial.json').write_text(json.dumps({p:(project/p).read_text() for p in ('app/db.py','app/services.py','app/api.py','app/worker.py','app/capabilities.py')}))
  sys.exit(45)
 sys.exit(subprocess.call(['/bin/mv']+args))
elif args[0]=='context': print('ssh://remote' if mode=='remote' else 'unix:///var/run/docker.sock')
elif args[0]=='info': pass
elif args[0]=='inspect': print(state['active'])
elif args[0]=='image':
 if args[1]=='inspect': print(state['tag'])
 elif args[1]=='tag' and args[-1]=='omnicrm:local':
  state['tag']='sha256:'+'b'*64 if args[2].startswith('omnicrm:local-inbox-candidate-') else args[2]
elif args[0]=='build':
 if mode=='build-failure': result=41
 elif mode=='source-edit-during-build': (project/'web/styles.css').write_text('USER_EDIT_DURING_BUILD\n')
 elif mode=='config-edit-during-build': (project/'.env.local-docker').write_text((project/'.env.local-docker').read_text()+'USER_EDIT=keep\n')
elif args[0]=='run':
 if mode=='candidate-failure': result=42
elif args[0]=='compose':
 if 'version' in args: print('v2.fake')
 elif 'ps' in args: print(args[-1]+'-container')
 elif 'port' in args: print(state['address'])
 elif 'stop' in args:
  state['stop_count']+=1; state['stopped']=True
  if mode=='stop-failure': result=48
 elif 'run' in args:
  state['migration_count']+=1
  state['migration_image']=entry['override']['services']['migrate']['image']
  if not state['stopped']: result=98
  elif mode=='migration-failure': result=46
  else:
   state['schema']=3
   if mode=='migration-committed-failure': result=46
   if mode=='config-edit-during-migration': (project/'.env.local-docker').write_text((project/'.env.local-docker').read_text()+'USER_EDIT=keep\n')
 elif 'up' in args:
  state['up_count']+=1; state['active']=state['tag']; state['stopped']=False
  state['address']='127.0.0.1:'+os.environ.get('LOCAL_PORT','18581')
  if mode=='health-failure' and state['up_count']==1: result=43
 elif 'exec' in args:
  if any('pg_dump' in part for part in args):
   if not state['stopped']: result=98
   elif mode=='dump-empty': pass
   elif mode=='dump-invalid': print('NOT_A_POSTGRES_DUMP')
   elif mode=='dump-failure': print('PGDMP partial'); result=47
   else: print('PGDMP fixture private history; encrypted credentials=ciphertext')
  elif 'pg_restore' in args:
   if '--list' not in args: result=97
   elif mode=='backup-validation-failure' or not entry['stdin'].startswith('PGDMP '): result=49
   else: print('Private backup schema metadata')
  elif mode=='runtime-failure': result=44
else: result=99
statepath.write_text(json.dumps(state))
sys.exit(result)
'''


class MacInboxUpdateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.project = self.base / "local-crm"
        for relative, content in OLD.items():
            path = self.project / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            path.chmod(0o640)
        (self.project / "app/config.py").write_text("UNRELATED_KEEP = True\n")
        self.envfile = self.project / ".env.local-docker"
        self.envfile.write_text(KEYS)
        self.envfile.chmod(0o600)
        (self.project / "compose.local.yaml").write_text("name: omnicrm-local\n")
        self.markers = {".omnicrm-local-source": "a" * 40 + "\n",
                        ".omnicrm-wa-incoming-patch": "old-wa-patch\n",
                        ".omnicrm-max-patch": "old-max-patch\n"}
        for name, content in self.markers.items():
            (self.project / name).write_text(content)
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("docker", "curl", "mv"):
            path = self.bin / name
            path.write_text("#!" + sys.executable + "\n" + FAKE)
            path.chmod(0o755)
        self.stage = self.base / "tmp"
        self.stage.mkdir()
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                                TEST_FIXTURE=str(self.base), TMPDIR=str(self.stage))
        for key in ("DOCKER_HOST", "DOCKER_CONTEXT", "LOCAL_BUILD_CA_FILE", "CODEX_PROXY_CERT", "LOCAL_PORT"):
            self.environment.pop(key, None)
        script = (ROOT / "scripts/update_mac_inbox.sh").read_text()
        hashes = ["  \"" + " ".join(hashlib.sha256(version[p].encode()).hexdigest()
                                  for version in (OLD, PATCHED)) + "\"" for p in FILES]
        script, count = re.subn(r'known_hashes=\(\n(?:  "[a-f0-9 ]+"\n){12}\)',
                               "known_hashes=(\n" + "\n".join(hashes) + "\n)", script)
        self.assertEqual(count, 1)
        self.updater = self.base / "updater.sh"
        self.updater.write_text(script)
        self.archive()

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, bad_type=None, duplicate=False):
        with tarfile.open(self.base / "source.tar.gz", "w:gz") as archive:
            for relative in FILES:
                entry = tarfile.TarInfo("superchat-" + REVISION + "/" + relative)
                entry.type = bad_type if relative == FILES[-1] and bad_type else tarfile.REGTYPE
                entry.size = len(NEW[relative].encode()) if entry.type == tarfile.REGTYPE else 0
                if entry.type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    entry.linkname = "/etc/passwd"
                archive.addfile(entry, io.BytesIO(NEW[relative].encode()) if entry.size else None)
                if duplicate and relative == FILES[-1]:
                    archive.addfile(entry, io.BytesIO(NEW[relative].encode()))
            entry = tarfile.TarInfo("superchat-" + REVISION + "/app/config.py")
            entry.size = 12
            archive.addfile(entry, io.BytesIO(b"DO_NOT_COPY\n"))

    def calls(self):
        path = self.base / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def state(self):
        return json.loads((self.base / "runtime.json").read_text())

    def run_update(self, mode="success", revision=REVISION, **env):
        return subprocess.run(["/bin/bash", str(self.updater), revision, str(self.project)],
                              env=dict(self.environment, TEST_MODE=mode, **env), text=True,
                              input="UNCONSUMED_INPUT\n", capture_output=True, timeout=30)

    def assert_source(self, expected):
        for relative, content in expected.items():
            self.assertEqual((self.project / relative).read_text(), content, relative)

    def backups(self):
        return list((self.project / "backups").glob("inbox-*"))

    def assert_preserved(self, keys=KEYS):
        self.assertEqual(self.envfile.read_text(), keys)
        self.assertEqual(self.envfile.stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.project / "compose.local.yaml").read_text(), "name: omnicrm-local\n")
        self.assertEqual((self.project / "app/config.py").read_text(), "UNRELATED_KEEP = True\n")
        for name, content in self.markers.items():
            self.assertEqual((self.project / name).read_text(), content)
        self.assertFalse((self.project / ".omnicrm-wa-incoming-update.lock").exists())
        self.assertEqual(list(self.stage.iterdir()), [])
        self.assertFalse(list((self.project / "backups").glob("**/.env*")))
        for call in self.calls():
            args = call["args"]
            if call["tool"] != "docker":
                continue
            for forbidden in ("down", "restart", "prune", "volume"):
                self.assertNotIn(forbidden, args)
            if args[0] != "compose":
                continue
            if "version" in args:
                continue  # Capability probe reads no project configuration.
            self.assertEqual(call["inherited_credentials"], [])
            if "up" in args:
                self.assertEqual(args[-2:], ["api", "worker"])
                self.assertIn("--no-deps", args)
                self.assertIn("--no-build", args)
                self.assertEqual(args[args.index("--pull") + 1], "never")
            if "stop" in args:
                self.assertEqual(args[-2:], ["api", "worker"])
            if "run" in args:
                self.assertIn("--rm", args)
                self.assertIn("--no-deps", args)
                self.assertEqual(args[args.index("--pull") + 1], "never")
                self.assertEqual(args[-2:], ["migrate", "scripts/migrate.py"])
                self.assertEqual(list(call["override"]["services"]), ["migrate"])
                self.assertEqual(list(call["override"]["services"]["migrate"]), ["image"])
            if "pg_restore" in args:
                self.assertIn("--list", args)  # Archive validation only, never restore.
                self.assertEqual(call["stdin"], DUMP)
            elif "exec" in args or "run" in args:
                self.assertEqual(call.get("stdin"), "")

    def test_success_builds_then_stops_backs_up_validates_migrates_and_starts(self):
        ca = self.base / "ca.pem"
        ca.write_text("PUBLIC_CA_FIXTURE")
        result = self.run_update(TEST_PUBLISHED_ADDRESS="127.0.0.1:19222", LOCAL_PORT="29999",
                                 DATABASE_URL="do-not-inherit", ENCRYPTION_KEY="do-not-inherit",
                                 LOCAL_BUILD_CA_FILE=str(ca))
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_source(NEW)
        self.assert_preserved()
        self.assertNotIn("ciphertext", result.stdout + result.stderr)
        self.assertNotIn("fixture-password", result.stdout + result.stderr)
        self.assertIn("INBOX_UPDATE_OK", result.stdout)
        self.assertEqual((self.project / ".omnicrm-inbox-patch").read_text(), REVISION + "\n")
        backup, = self.backups()
        self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
        self.assertEqual((backup / "database.dump").stat().st_mode & 0o777, 0o600)
        self.assertEqual((backup / "database.dump").read_text(), DUMP)
        self.assertTrue((backup / "backup-complete").exists())
        self.assertTrue((backup / "migration-complete").exists())
        for path in FILES:
            self.assertEqual((backup / path).read_text(), OLD[path])
        state = self.state()
        self.assertEqual(state["active"], NEW_IMAGE)
        self.assertEqual(state["schema"], 3)
        self.assertEqual(state["address"], "127.0.0.1:19222")
        self.assertTrue(state["migration_image"].startswith("omnicrm:local-inbox-candidate-"))
        calls = [c for c in self.calls() if c["tool"] == "docker"]
        build = next(i for i, c in enumerate(calls) if c["args"][0] == "build")
        stop = next(i for i, c in enumerate(calls) if "stop" in c["args"])
        dump = next(i for i, c in enumerate(calls) if any("pg_dump" in x for x in c["args"]))
        validate = next(i for i, c in enumerate(calls) if "pg_restore" in c["args"])
        migrate = next(i for i, c in enumerate(calls) if c["args"][0] == "compose" and "run" in c["args"])
        up = next(i for i, c in enumerate(calls) if "up" in c["args"])
        self.assertEqual(sorted([build, stop, dump, validate, migrate, up]), [build, stop, dump, validate, migrate, up])
        self.assertEqual(calls[build]["args"][calls[build]["args"].index("--secret") + 1], "id=proxy_ca,src=" + str(ca))
        self.assertEqual([c["local_port"] for c in calls if "up" in c["args"] or "run" in c["args"] and c["args"][0] == "compose"], ["19222", "19222"])

    def test_partial_replacement_restores_every_original_and_permission(self):
        result = self.run_update("partial-failure")
        self.assertNotEqual(result.returncode, 0)
        partial = json.loads((self.base / "partial.json").read_text())
        for path in FILES[:4]:
            self.assertEqual(partial[path], NEW[path])
        self.assertEqual(partial[FILES[4]], OLD[FILES[4]])
        self.assert_source(OLD)
        self.assert_preserved()
        for path in FILES:
            self.assertEqual((self.project / path).stat().st_mode & 0o777, 0o640)
        self.assertFalse(any(c["args"][0] == "build" or "stop" in c["args"] for c in self.calls()))

    def test_build_failure_never_stops_or_migrates(self):
        result = self.run_update("build-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["stop_count"], 0)
        self.assertEqual(self.state()["migration_count"], 0)
        self.assertEqual(self.state()["active"], OLD_IMAGE)

    def test_candidate_validation_failure_never_stops_or_migrates(self):
        result = self.run_update("candidate-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["stop_count"], 0)
        self.assertEqual(self.state()["migration_count"], 0)

    def test_partial_stop_failure_restarts_old_app_without_migration(self):
        result = self.run_update("stop-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["active"], OLD_IMAGE)
        self.assertFalse(self.state()["stopped"])
        self.assertEqual(self.state()["migration_count"], 0)

    def test_failed_dump_preserves_private_partial_and_does_not_migrate(self):
        result = self.run_update("dump-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        backup, = self.backups()
        self.assertFalse((backup / "backup-complete").exists())
        self.assertEqual((backup / "database.dump.partial").stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.state()["migration_count"], 0)
        self.assertEqual(self.state()["active"], OLD_IMAGE)

    def test_empty_dump_refuses_migration(self):
        result = self.run_update("dump-empty")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["migration_count"], 0)

    def test_unreadable_dump_refuses_migration(self):
        result = self.run_update("backup-validation-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["migration_count"], 0)
        self.assertFalse((self.backups()[0] / "database.dump").exists())

    def test_migration_failure_restarts_old_without_reinvoking_old_migration(self):
        result = self.run_update("migration-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        state = self.state()
        self.assertEqual(state["active"], OLD_IMAGE)
        self.assertEqual(state["migration_count"], 1)
        self.assertEqual(state["up_count"], 1)
        self.assertEqual(state["schema"], 2)
        self.assertEqual((self.backups()[0] / "database.dump").read_text(), DUMP)

    def test_committed_migration_then_failure_retains_schema_and_data(self):
        result = self.run_update("migration-committed-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["active"], OLD_IMAGE)
        self.assertEqual(self.state()["schema"], 3)
        self.assertEqual(self.state()["migration_count"], 1)
        self.assertIn("не откатывались", result.stderr)

    def test_failed_new_health_restores_sources_old_image_and_live_port(self):
        result = self.run_update("health-failure", TEST_PUBLISHED_ADDRESS="127.0.0.1:19222")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        state = self.state()
        self.assertEqual(state["active"], OLD_IMAGE)
        self.assertEqual(state["tag"], OLD_IMAGE)
        self.assertEqual(state["schema"], 3)
        self.assertEqual(state["up_count"], 2)
        self.assertEqual(state["address"], "127.0.0.1:19222")
        self.assertFalse((self.project / ".omnicrm-inbox-patch").exists())

    def test_failed_runtime_profile_also_recovers_without_schema_downgrade(self):
        result = self.run_update("runtime-failure")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved()
        self.assertEqual(self.state()["active"], OLD_IMAGE)
        self.assertEqual(self.state()["schema"], 3)
        self.assertEqual(self.state()["migration_count"], 1)

    def test_known_previous_patches_and_target_mix_supported(self):
        expected = dict(OLD)
        expected[FILES[2]] = PATCHED[FILES[2]]
        expected[FILES[7]] = PATCHED[FILES[7]]
        expected[FILES[9]] = NEW[FILES[9]]
        for path, text in expected.items():
            (self.project / path).write_text(text)
        result = self.run_update()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assert_source(NEW)
        self.assert_preserved()
        for path, text in expected.items():
            self.assertEqual((self.backups()[0] / path).read_text(), text)

    def test_last_unknown_local_file_refuses_whole_patch_before_backup(self):
        expected = dict(OLD)
        expected[FILES[-1]] = "CUSTOM_MIGRATION_KEEP\n"
        (self.project / FILES[-1]).write_text(expected[FILES[-1]])
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(expected)
        self.assert_preserved()
        self.assertFalse((self.project / "backups").exists())
        self.assertEqual(self.state()["stop_count"], 0)

    def test_concurrent_source_edit_is_not_overwritten_during_recovery(self):
        result = self.run_update("source-edit-during-build")
        self.assertNotEqual(result.returncode, 0)
        expected = dict(OLD, **{"web/styles.css": "USER_EDIT_DURING_BUILD\n"})
        self.assert_source(expected)
        self.assert_preserved()
        self.assertEqual(self.state()["stop_count"], 0)
        self.assertIn("Не перезаписываем", result.stderr)

    def test_concurrent_configuration_edit_blocks_cutover_preserving_edit(self):
        result = self.run_update("config-edit-during-build")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved(keys=KEYS + "USER_EDIT=keep\n")
        self.assertEqual(self.state()["stop_count"], 0)

    def test_configuration_edit_after_migration_does_not_restart_with_other_keys(self):
        result = self.run_update("config-edit-during-migration")
        self.assertNotEqual(result.returncode, 0)
        self.assert_source(OLD)
        self.assert_preserved(keys=KEYS + "USER_EDIT=keep\n")
        state = self.state()
        self.assertEqual(state["up_count"], 0)
        self.assertTrue(state["stopped"])
        self.assertEqual(state["schema"], 3)

    def test_archive_symlink_hardlink_and_duplicate_refused_before_replacement(self):
        for bad_type, duplicate in ((tarfile.SYMTYPE, False), (tarfile.LNKTYPE, False), (None, True)):
            with self.subTest(bad_type=bad_type, duplicate=duplicate):
                self.archive(bad_type, duplicate)
                result = self.run_update()
                self.assertNotEqual(result.returncode, 0)
                self.assert_source(OLD)
                self.assert_preserved()
                self.assertFalse((self.project / "backups").exists())

    def test_symlink_scripts_directory_and_environment_refused_before_docker(self):
        scripts = self.project / "scripts"
        scripts.rename(self.base / "outside-scripts")
        scripts.symlink_to(self.base / "outside-scripts")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        scripts.unlink()
        (self.base / "outside-scripts").rename(scripts)
        self.envfile.rename(self.base / "private-keys")
        self.envfile.symlink_to(self.base / "private-keys")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_invalid_sha_remote_context_and_existing_shared_lock_refuse(self):
        result = self.run_update(revision="main")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        result = self.run_update("remote")
        self.assertNotEqual(result.returncode, 0)
        lock = self.project / ".omnicrm-wa-incoming-update.lock"
        lock.mkdir()
        (lock / "owner").write_text("other-updater")
        result = self.run_update()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((lock / "owner").read_text(), "other-updater")
        self.assertFalse(any(c["tool"] == "curl" for c in self.calls()))
        self.assert_source(OLD)


if __name__ == "__main__":
    unittest.main()
