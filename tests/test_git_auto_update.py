"""No-network contract tests for the root-owned GitHub deployment gate."""
import copy
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('git_auto_update', Path(__file__).resolve().parents[1] / 'scripts/git_auto_update.py')
auto = importlib.util.module_from_spec(spec)
spec.loader.exec_module(auto)
SHA = 'a' * 40
OLD = 'b' * 40
REPO = 'amirismagilov/superchat'
TOKEN = {'sha': SHA, 'run_id': 42, 'attempt': 1}


def run(**changes):
    result = {'id': 42, 'run_attempt': 1, 'workflow_id': 7, 'head_sha': SHA, 'head_branch': 'main', 'event': 'push', 'path': '.github/workflows/ci.yml', 'head_repository': {'full_name': REPO}, 'repository': {'full_name': REPO}, 'status': 'completed', 'conclusion': 'success'}
    result.update(changes)
    return result


class FakeBackend:
    def __init__(self):
        self.heads = [SHA, SHA]
        self.token = dict(TOKEN)
        self.calls = []
        self.failure = None
        self.forward = True

    def head(self):
        self.calls.append('head')
        return self.heads.pop(0)

    def approval(self, sha):
        self.calls.append('approval')
        if self.failure in {'ci_pending', 'ci_failed', 'github_api_unavailable'}:
            raise auto.Blocked(self.failure)
        return self.token

    def fetch(self, sha):
        self.calls.append(('fetch', sha))

    def ancestor(self, old, candidate):
        self.calls.append(('ancestor', old, candidate))
        return self.forward

    def package(self, sha, directory):
        self.calls.append(('package', sha))
        if self.failure == 'release_packaging_failed':
            raise auto.Blocked(self.failure)
        return ('updater', 'archive', 'digest')

    def deploy(self, package):
        self.calls.append('deploy')
        if self.failure == 'pilot_update_failed':
            raise auto.Blocked(self.failure)
        if self.failure == 'unexpected':
            raise RuntimeError('SECRET_FROM_CHILD_PROCESS')


class AutoUpdateTests(unittest.TestCase):
    def cycle(self, backend=None, state=None, retry=False):
        backend = backend or FakeBackend()
        state = state or {'repository': REPO, 'status': 'not_checked'}
        snapshots = []
        with tempfile.TemporaryDirectory() as folder:
            auto.cycle(Path(folder), backend, state, lambda value: snapshots.append(copy.deepcopy(value)), retry=retry)
        return state, snapshots, backend

    def approval(self, runs, current=None):
        backend = auto.Backend(Path('/unused'), REPO)
        replies = iter([{'workflow_runs': runs}, current if current is not None else (runs[0] if runs else None)])
        backend.api = lambda suffix: next(replies)
        return backend.approval(SHA)

    def test_exact_success_deploy_and_atomic_success_order(self):
        state, snapshots, backend = self.cycle()
        self.assertEqual(state['status'], 'deployed')
        self.assertEqual(state['deployed_sha'], SHA)
        self.assertEqual(backend.calls.count('deploy'), 1)
        self.assertEqual(backend.calls.count('approval'), 2)
        self.assertFalse(any('deployed_sha' in snapshot for snapshot in snapshots[:-1]))
        self.assertTrue(next(s for s in snapshots if s['status'] == 'deploying')['in_progress'])

    def test_pending_failed_and_http_errors_never_package_or_deploy(self):
        for error in ('ci_pending', 'ci_failed', 'github_api_unavailable'):
            with self.subTest(error=error):
                backend = FakeBackend(); backend.failure = error
                state, _, backend = self.cycle(backend)
                self.assertEqual(state['status'], error)
                self.assertNotIn('deploy', backend.calls)
                self.assertFalse(any(isinstance(c, tuple) and c[0] == 'package' for c in backend.calls))

    def test_approval_accepts_only_latest_successful_same_repo_push(self):
        self.assertEqual(self.approval([run()]), TOKEN)
        for invalid in [run(event='pull_request'), run(head_sha=OLD), run(head_branch='feature'), run(path='.github/workflows/other.yml'), run(head_repository={'full_name': 'attacker/fork'}), run(repository={'full_name': 'attacker/fork'}), run(status='in_progress', conclusion=None), run(conclusion='failure')]:
            with self.subTest(invalid=invalid):
                with self.assertRaises(auto.Blocked):
                    self.approval([invalid])
        with self.assertRaises(auto.Blocked):
            self.approval([])

    def test_newest_run_and_rerun_must_succeed(self):
        with self.assertRaisesRegex(auto.Blocked, 'ci_pending'):
            self.approval([run(), run(id=43, status='queued', conclusion=None)])
        with self.assertRaisesRegex(auto.Blocked, 'ci_attempt_changed'):
            self.approval([run()], run(run_attempt=2, status='in_progress', conclusion=None))
        with self.assertRaisesRegex(auto.Blocked, 'ci_latest_attempt_not_successful'):
            self.approval([run()], run(conclusion='failure'))

    def test_failed_attempt_latch_and_explicit_retry(self):
        backend = FakeBackend(); backend.failure = 'pilot_update_failed'
        state, _, _ = self.cycle(backend)
        self.assertEqual(state['failed_attempt'], TOKEN)
        self.assertNotIn('deployed_sha', state)
        state, _, backend = self.cycle(state=state)
        self.assertEqual(state['status'], 'retry_required')
        self.assertNotIn('deploy', backend.calls)
        state, _, backend = self.cycle(state=state, retry=True)
        self.assertEqual(state['status'], 'deployed')
        self.assertEqual(backend.calls.count('deploy'), 1)

    def test_new_successful_ci_attempt_allows_retry(self):
        state = {'repository': REPO, 'failed_attempt': TOKEN}
        backend = FakeBackend(); backend.token = dict(TOKEN, attempt=2)
        state, _, _ = self.cycle(backend, state)
        self.assertEqual(state['status'], 'deployed')
        self.assertEqual(state['deployed_attempt']['attempt'], 2)

    def test_interrupted_attempt_is_uncertain_and_not_rebuilt(self):
        state = {'repository': REPO, 'in_progress': True, 'active_attempt': TOKEN}
        state, snapshots, backend = self.cycle(state=state)
        self.assertEqual(snapshots[0]['status'], 'previous_attempt_uncertain')
        self.assertEqual(state['status'], 'retry_required')
        self.assertNotIn('deploy', backend.calls)

    def test_no_automatic_rollback_and_branch_change_defers(self):
        backend = FakeBackend(); backend.forward = False
        state, _, backend = self.cycle(backend, {'repository': REPO, 'deployed_sha': OLD})
        self.assertEqual(state['status'], 'non_fast_forward_blocked')
        self.assertNotIn('deploy', backend.calls)
        backend = FakeBackend(); backend.heads = [SHA, OLD]
        state, _, backend = self.cycle(backend)
        self.assertEqual(state['status'], 'main_changed_deferred')
        self.assertNotIn('deploy', backend.calls)
        self.assertFalse(state['in_progress'])

    def test_up_to_date_avoids_github_api_calls(self):
        state, _, backend = self.cycle(state={'repository': REPO, 'deployed_sha': SHA})
        self.assertEqual(state['status'], 'up_to_date')
        self.assertEqual(backend.calls, ['head'])

    def test_errors_never_store_raw_child_output(self):
        backend = FakeBackend(); backend.failure = 'unexpected'
        state, snapshots, _ = self.cycle(backend)
        self.assertEqual(state['status'], 'unexpected_failure')
        self.assertNotIn('SECRET', json.dumps(snapshots))
        self.assertEqual(state['failed_attempt'], TOKEN)

    def test_fetch_pins_exact_sha_in_dedicated_mirror(self):
        with tempfile.TemporaryDirectory() as folder:
            backend = auto.Backend(Path(folder), REPO)
            calls = []
            def command(args, **kwargs):
                calls.append(args)
                output = (SHA + '\n').encode() if 'rev-parse' in args else b''
                return subprocess.CompletedProcess(args, 0, stdout=output)
            backend.command = command
            backend.fetch(SHA)
            fetch = next(args for args in calls if 'fetch' in args)
            self.assertEqual(fetch[-1], SHA + ':refs/heads/candidate')
            self.assertIn(str(Path(folder) / 'repository.git'), fetch)
            self.assertFalse(any('/opt/omnicrm-pilot/' in arg for args in calls for arg in args))

    def test_git_archive_rejects_links_and_path_traversal(self):
        for name, kind in [('../escape', tarfile.REGTYPE), ('scripts/link', tarfile.SYMTYPE)]:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                backend = auto.Backend(root, REPO)
                def fake_git(*args, **kwargs):
                    destination = Path(next(arg.split('=', 1)[1] for arg in args if arg.startswith('--output=')))
                    with tarfile.open(destination, 'w') as handle:
                        entry = tarfile.TarInfo(name); entry.type = kind; entry.linkname = '/etc/passwd'
                        handle.addfile(entry, io.BytesIO(b'') if kind == tarfile.REGTYPE else None)
                backend.git = fake_git
                with self.assertRaises(auto.Blocked):
                    backend.package(SHA, root)
                self.assertFalse((root.parent / 'escape').exists())

    @unittest.skipUnless(shutil.which('git'), 'Git is required for local archive integration')
    def test_real_git_archive_packages_pinned_source_without_runtime_or_workflow_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            repository = root / 'source-repository'
            repository.mkdir()
            required = ['Dockerfile', 'requirements.lock', 'compose.pilot.yaml', 'app/api.py', 'app/worker.py', 'web/index.html', 'scripts/migrate.py', 'scripts/server_pilot.sh', 'scripts/update_server_pilot.sh']
            for name in required:
                path = repository / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('# pinned source\n')
            (repository / '.github/workflows').mkdir(parents=True)
            (repository / '.github/workflows/ci.yml').write_text('name: ci\n')
            (repository / '.env.pilot').write_text('DO_NOT_INCLUDE_FAKE_SECRET=yes\n')
            shutil.copy2(Path(__file__).resolve().parents[1] / 'scripts/package_release.py', repository / 'scripts/package_release.py')
            environment = dict(auto.os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null')
            for command in [['git', 'init', '-q'], ['git', 'add', '.'], ['git', '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'pinned source']]:
                subprocess.run(command, cwd=repository, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, check=True)
            sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=repository, env=environment, text=True).strip()
            backend = auto.Backend(root, REPO)
            backend.mirror = repository / '.git'
            stage = root / 'candidate'
            stage.mkdir()
            updater, payload, digest = backend.package(sha, stage)
            self.assertTrue(updater.is_file())
            self.assertEqual(hashlib.sha256(payload.read_bytes()).hexdigest(), digest)
            with tarfile.open(payload) as archive:
                names = archive.getnames()
                self.assertIn('omnicrm/app/api.py', names)
                self.assertIn('omnicrm/docs/source-manifest.json', names)
                self.assertFalse(any('.env.pilot' in name or '.github' in name for name in names))

    def test_state_write_is_atomic_private_json(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'state.json'
            auto.atomic_json(path, {'status': 'checking'})
            auto.atomic_json(path, {'status': 'deployed', 'deployed_sha': SHA})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text())['deployed_sha'], SHA)
            self.assertEqual([p.name for p in Path(folder).iterdir()], ['state.json'])


if __name__ == '__main__':
    unittest.main()
