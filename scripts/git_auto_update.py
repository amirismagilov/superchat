#!/usr/bin/env python3
"""Root-owned, fail-closed public GitHub main -> existing private pilot updater."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import tarfile
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ROOT = Path('/opt/omnicrm-pilot-autoupdate')
SHA = re.compile(r'[0-9a-f]{40}')
REPO = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
WORKFLOW = '.github/workflows/ci.yml'


class Blocked(Exception):
    """Only fixed, non-secret diagnostic codes belong in this exception."""


def timestamp():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.state-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def private_file(path):
    if path.is_symlink() or not path.is_file():
        raise Blocked('private_file_missing_or_symlink')
    info = path.stat()
    if info.st_uid != 0 or info.st_mode & 0o077:
        raise Blocked('private_file_permissions')
    return json.loads(path.read_text())


def validate_run(run, repo, sha):
    if not isinstance(run, dict):
        raise Blocked('ci_invalid_response')
    for field, expected in [('head_sha', sha), ('head_branch', 'main'), ('event', 'push'), ('path', WORKFLOW)]:
        if run.get(field) != expected:
            raise Blocked('ci_identity_mismatch')
    for field in ('head_repository', 'repository'):
        value = run.get(field)
        if not isinstance(value, dict) or not isinstance(value.get('full_name'), str) or value['full_name'].lower() != repo.lower():
            raise Blocked('ci_repository_mismatch')
    for field in ('id', 'run_attempt', 'workflow_id'):
        if type(run.get(field)) is not int or run[field] < 1:
            raise Blocked('ci_invalid_run_identity')
    return {'sha': sha, 'run_id': run['id'], 'attempt': run['run_attempt']}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Backend:
    def __init__(self, root, repo, lock_fd=None):
        self.root, self.repo = root, repo
        self.lock_fd = lock_fd
        self.url = 'https://github.com/' + repo + '.git'
        self.mirror = root / 'repository.git'
        self.environment = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null', GIT_TERMINAL_PROMPT='0')
        # No credential helper or shell command from a user's Git configuration.
        self.git_prefix = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'credential.helper=', '-c', 'protocol.file.allow=never', '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']

    def command(self, args, *, capture=False, timeout=120, cwd=None):
        try:
            result = subprocess.run(args, cwd=cwd, env=self.environment, stdout=subprocess.PIPE if capture else subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=timeout, check=False, pass_fds=() if self.lock_fd is None else (self.lock_fd,))
        except (OSError, subprocess.TimeoutExpired):
            raise Blocked('command_unavailable_or_timeout') from None
        return result

    def git(self, *args, capture=True, timeout=120):
        result = self.command([*self.git_prefix, *args], capture=capture, timeout=timeout)
        if result.returncode:
            raise Blocked('git_command_failed')
        return result.stdout.decode('utf-8', errors='strict').strip() if capture else ''

    def head(self):
        value = self.git('ls-remote', '--exit-code', self.url, 'refs/heads/main')
        parts = value.split()
        if len(parts) != 2 or not SHA.fullmatch(parts[0]) or parts[1] != 'refs/heads/main':
            raise Blocked('git_head_invalid')
        return parts[0]

    def api(self, suffix):
        request = Request('https://api.github.com/repos/' + self.repo + suffix, headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'omnicrm-private-pilot-autoupdate', 'X-GitHub-Api-Version': '2022-11-28'})
        try:
            with build_opener(NoRedirect).open(request, timeout=15) as response:
                if response.status != 200:
                    raise Blocked('github_api_unavailable')
                data = response.read(2 * 1024 * 1024 + 1)
                if len(data) > 2 * 1024 * 1024:
                    raise Blocked('github_api_response_too_large')
                return json.loads(data)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError):
            raise Blocked('github_api_unavailable') from None

    def approval(self, sha):
        query = urlencode({'branch': 'main', 'event': 'push', 'head_sha': sha, 'per_page': 20})
        result = self.api('/actions/workflows/ci.yml/runs?' + query)
        if not isinstance(result, dict) or not isinstance(result.get('workflow_runs'), list):
            raise Blocked('ci_invalid_response')
        runs = result['workflow_runs']
        if not runs:
            raise Blocked('ci_missing')
        # Inspect newest run, not an earlier successful run; rerun attempts share an id.
        for run in runs:
            validate_run(run, self.repo, sha)
        latest = max(runs, key=lambda run: (run['id'], run['run_attempt']))
        token = validate_run(latest, self.repo, sha)
        if latest.get('status') != 'completed':
            raise Blocked('ci_pending')
        if latest.get('conclusion') != 'success':
            raise Blocked('ci_failed')
        # Get current attempt directly; a newly started rerun must block an old success.
        current = self.api('/actions/runs/' + str(latest['id']))
        current_token = validate_run(current, self.repo, sha)
        if current_token != token or current.get('workflow_id') != latest['workflow_id']:
            raise Blocked('ci_attempt_changed')
        if current.get('status') != 'completed' or current.get('conclusion') != 'success':
            raise Blocked('ci_latest_attempt_not_successful')
        return token

    def fetch(self, sha):
        if self.mirror.is_symlink():
            raise Blocked('git_mirror_symlink')
        if not self.mirror.exists():
            self.git('init', '--bare', str(self.mirror))
        self.git('--git-dir', str(self.mirror), 'fetch', '--no-tags', '--force', self.url, sha + ':refs/heads/candidate', timeout=180)
        if self.git('--git-dir', str(self.mirror), 'rev-parse', 'refs/heads/candidate^{commit}') != sha:
            raise Blocked('git_fetched_sha_mismatch')

    def ancestor(self, deployed, candidate):
        result = self.command([*self.git_prefix, '--git-dir', str(self.mirror), 'merge-base', '--is-ancestor', deployed, candidate])
        if result.returncode == 1:
            return False
        if result.returncode:
            raise Blocked('ancestry_unavailable')
        return True

    def package(self, sha, directory):
        archive = directory / 'commit.tar'
        self.git('--git-dir', str(self.mirror), 'archive', '--format=tar', '--output=' + str(archive), sha, capture=False)
        source = directory / 'source'
        source.mkdir(mode=0o700)
        seen, size = set(), 0
        with tarfile.open(archive) as handle:
            for entry in handle:
                path = PurePosixPath(entry.name)
                if path.is_absolute() or '..' in path.parts or str(path) != entry.name.rstrip('/') or '.git' in path.parts or entry.name in seen:
                    raise Blocked('archive_path_invalid')
                seen.add(entry.name)
                if len(seen) > 5000 or not (entry.isfile() or entry.isdir()):
                    raise Blocked('archive_file_type_invalid')
                destination = source.joinpath(*path.parts)
                if entry.isdir():
                    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
                    continue
                size += entry.size
                if entry.size > 32 * 1024 * 1024 or size > 256 * 1024 * 1024:
                    raise Blocked('archive_too_large')
                destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with destination.open('xb') as output:
                    output.write(handle.extractfile(entry).read())
                destination.chmod(0o755 if entry.mode & 0o111 else 0o644)
        output = directory / 'release'
        output.mkdir(mode=0o700)
        result = self.command(['python3', 'scripts/package_release.py', '--output-dir', str(output)], cwd=source, timeout=120)
        if result.returncode:
            raise Blocked('release_packaging_failed')
        payload = output / 'omnicrm-server-pilot.tar.gz'
        check = output / 'omnicrm-server-pilot.tar.gz.sha256'
        if not payload.is_file() or payload.is_symlink() or not check.is_file() or check.is_symlink():
            raise Blocked('release_files_missing')
        digest = check.read_text().split()[0]
        if not re.fullmatch('[0-9a-f]{64}', digest) or hashlib.sha256(payload.read_bytes()).hexdigest() != digest:
            raise Blocked('release_checksum_mismatch')
        updater = source / 'scripts/update_server_pilot.sh'
        if not updater.is_file() or updater.is_symlink():
            raise Blocked('release_updater_missing')
        return updater, payload, digest

    def deploy(self, package):
        updater, archive, digest = package
        # Leave timeout/TERM management to systemd so updater can trap TERM and recover.
        result = self.command(['bash', str(updater), str(archive), digest], timeout=None)
        if result.returncode:
            raise Blocked('pilot_update_failed')


def cycle(root, backend, state, save, *, retry=False):
    def mark(status, **fields):
        state.update(status=status, checked_at=timestamp(), **fields)
        save(state)

    if state.get('in_progress'):
        state['failed_attempt'] = state.get('active_attempt')
        mark('previous_attempt_uncertain', in_progress=False)
    try:
        sha = backend.head()
        mark('checking', candidate_sha=sha)
        if state.get('deployed_sha') == sha:
            mark('up_to_date')
            return
        token = backend.approval(sha)
        if state.get('failed_attempt') == token and not retry:
            mark('retry_required')
            return
        backend.fetch(sha)
        previous = state.get('deployed_sha')
        if previous and not backend.ancestor(previous, sha):
            mark('non_fast_forward_blocked')
            return
        mark('packaging', in_progress=True, active_attempt=token)
        with tempfile.TemporaryDirectory(prefix='candidate-', dir=root) as temporary:
            package = backend.package(sha, Path(temporary))
            if backend.head() != sha:
                mark('main_changed_deferred', in_progress=False)
                return
            if backend.approval(sha) != token:
                mark('ci_changed_deferred', in_progress=False)
                return
            mark('deploying')
            backend.deploy(package)
        mark('deployed', deployed_sha=sha, deployed_at=timestamp(), deployed_attempt=token, in_progress=False, failed_attempt=None)
    except Blocked as exc:
        if state.get('in_progress'):
            state['failed_attempt'] = state.get('active_attempt')
        mark(str(exc), in_progress=False)
    except Exception:
        if state.get('in_progress'):
            state['failed_attempt'] = state.get('active_attempt')
        mark('unexpected_failure', in_progress=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--retry', action='store_true', help='Retry the current failed SHA only if its latest trusted CI attempt succeeds')
    args = parser.parse_args()
    try:
        if os.geteuid() != 0 or ROOT.is_symlink() or not ROOT.is_dir() or ROOT.stat().st_uid != 0 or ROOT.stat().st_mode & 0o077:
            raise Blocked('root_owned_private_directory_required')
        config = private_file(ROOT / 'config.json')
        if config.get('branch') != 'main' or config.get('workflow') != WORKFLOW or not REPO.fullmatch(config.get('repository', '')):
            raise Blocked('configuration_invalid')
        state_path = ROOT / 'state.json'
        with (ROOT / 'poll.lock').open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print(json.dumps({'status': 'another_poll_running'}))
                return 0
            state = private_file(state_path) if state_path.exists() else {'status': 'not_checked', 'repository': config['repository']}
            if not isinstance(state, dict) or state.get('repository') != config['repository']:
                raise Blocked('state_repository_mismatch')
            if state.get('deployed_sha') and not SHA.fullmatch(state['deployed_sha']):
                raise Blocked('state_commit_invalid')
            if not args.status:
                cycle(ROOT, Backend(ROOT, config['repository'], lock.fileno()), state, lambda value: atomic_json(state_path, value), retry=args.retry)
            # State contains only code/commit/run metadata; never child output or exception text.
            print(json.dumps(state, sort_keys=True, indent=2))
            return 0
    except Blocked as exc:
        print(json.dumps({'status': str(exc)}))
        return 1
    except Exception:
        print(json.dumps({'status': 'autoupdate_unavailable'}))
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
