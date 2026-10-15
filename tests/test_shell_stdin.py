"""Regression: container health checks must not consume an SSH script's stdin."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]


def executable(path, source):
    path.write_text(source)
    path.chmod(0o755)


@pytest.fixture
def pilot(tmp_path):
    root = tmp_path / 'pilot'
    scripts = root / 'scripts'
    scripts.mkdir(parents=True)
    (root / '.env.pilot').write_text('LOCAL_PORT=18580\n')
    executable(scripts / 'server_pilot.sh', (ROOT / 'scripts/server_pilot.sh').read_text())
    commands = tmp_path / 'bin'
    commands.mkdir()
    executable(commands / 'docker', '''#!/usr/bin/env python3
import os, pathlib, sys
args = sys.argv[1:]
if args[0] == 'context':
    print('unix:///var/run/docker.sock')
elif args[0] == 'compose':
    if 'exec' in args:
        # Compose exec normally forwards stdin, even with -T.
        pathlib.Path(os.environ['CAPTURED_STDIN']).write_text(sys.stdin.read())
    elif 'port' in args:
        print('127.0.0.1:18580')
''')
    executable(commands / 'id', '#!/bin/sh\nprintf "0\\n"\n')
    executable(commands / 'systemctl', '#!/bin/sh\nexit 0\n')
    env = {'PATH': str(commands) + os.pathsep + os.environ['PATH'],
           'CAPTURED_STDIN': str(tmp_path / 'captured-stdin')}
    return root, env


def streamed_command(script, env):
    result = subprocess.run(['bash', '-se'], input=f'bash "{script}" status\nprintf "AFTER_HEALTH_CHECK\\n"\n',
                            text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'AFTER_HEALTH_CHECK' in result.stdout
    assert Path(env['CAPTURED_STDIN']).read_text() == ''


def test_server_status_does_not_consume_following_ssh_commands(pilot):
    root, env = pilot
    streamed_command(root / 'scripts/server_pilot.sh', env)


def test_installer_protects_stdin_even_with_an_older_pilot(pilot):
    root, env = pilot
    # An already installed older health check can read stdin. The new installer
    # must isolate it without requiring that older installation to be patched.
    executable(root / 'scripts/server_pilot.sh', '#!/bin/sh\ncat > "$CAPTURED_STDIN"\n')
    installer = (ROOT / 'scripts/install_git_auto_update.sh').read_text()
    # Execute the real preflight only; never install units or alter /opt in tests.
    preflight = installer.split('\nroot=/opt/omnicrm-pilot-autoupdate\n', 1)[0]
    assert preflight != installer
    preflight = preflight.replace('/opt/omnicrm-pilot', str(root))
    path = root / 'scripts/installer-preflight.sh'
    executable(path, preflight)
    (root / 'scripts/git_auto_update.py').write_text('# test sibling\n')
    result = subprocess.run(['bash', '-se'],
                            input=f'bash "{path}" owner/repo\nprintf "AFTER_HEALTH_CHECK\\n"\n',
                            text=True, capture_output=True, env=env, timeout=10)
    assert result.returncode == 0, result.stderr
    assert 'AFTER_HEALTH_CHECK' in result.stdout
    assert Path(env['CAPTURED_STDIN']).read_text() == ''
