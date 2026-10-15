"""Release safety, reproducibility and compatibility with the actual updater."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tarfile

import pytest

from scripts import package_release as package


REPOSITORY = Path(__file__).resolve().parents[1]


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    for name in package.REQUIRED:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('source: ' + name + '\n')
    return root


def write(root, name, content='must not ship'):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def read_archive(path):
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        assert all(member.isfile() for member in members)
        assert all(member.name.startswith('omnicrm/') for member in members)
        assert all(member.uid == member.gid == member.mtime == 0 for member in members)
        assert all(member.mode == (0o755 if member.name.endswith(('.sh', '.command')) else 0o644) for member in members)
        return {member.name.removeprefix('omnicrm/'): archive.extractfile(member).read() for member in members}


def test_release_excludes_runtime_data_hidden_paths_research_and_unknown_files(source, tmp_path):
    excluded = ['.env.pilot', '.git/config', '.github/workflows/ci.yml', '.venv/app.py',
                'app/runtime.db', 'app/session.session', 'app/secrets.pem', 'app/private.key',
                'app/__pycache__/api.pyc', 'web/node_modules/package/index.js',
                'scripts/.private/deploy.sh', 'tests/raw.har', 'tests/credentials.json',
                'tests/artifacts/output.py', 'docs/inbox-preview.png', 'docs/commercial-services.md',
                'docs/unknown.md', 'package.json', 'archive.zip']
    for name in excluded:
        write(source, name)
    old_manifest = write(source, package.MANIFEST, '{"stale": "ignored"}')
    write(source, 'tests/browser/package.json', '{}')
    write(source, 'tests/browser/package-lock.json', '{}')
    write(source, 'docs/ci.md', 'technical documentation')
    archive, digest = package.build(source, tmp_path / 'output')
    contents = read_archive(archive)
    assert not set(excluded) & contents.keys()
    assert set(package.REQUIRED) <= contents.keys()
    assert 'tests/browser/package.json' in contents and 'docs/ci.md' in contents
    entries = json.loads(contents.pop(package.MANIFEST))
    assert set(entries) == set(contents)
    assert all(entries[name] == hashlib.sha256(data).hexdigest() for name, data in contents.items())
    assert old_manifest.read_text() == '{"stale": "ignored"}', 'Packaging must not rewrite a running deployment manifest'
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()
    assert (archive.parent / (package.ARCHIVE_NAME + '.sha256')).read_text() == digest + '  ' + package.ARCHIVE_NAME + '\n'


def test_release_bytes_are_deterministic_across_output_directories_and_file_modes(source, tmp_path):
    first, _ = package.build(source, tmp_path / 'one')
    (source / 'app/api.py').chmod(0o777)
    (source / 'scripts/migrate.py').touch()
    second, _ = package.build(source, tmp_path / 'two')
    assert first.read_bytes() == second.read_bytes()


@pytest.mark.parametrize('name', ['app/api.py', 'scripts/helper.py', 'docs/ci.md'])
def test_source_symlinks_are_rejected_without_reading_the_target(source, tmp_path, name):
    path = source / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    path.symlink_to(tmp_path / 'unread-secret')
    with pytest.raises(ValueError, match='Symlink'):
        package.build(source, tmp_path / 'output')


def test_source_directory_symlink_is_rejected(source, tmp_path):
    (source / 'app/private').symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        package.build(source, tmp_path / 'output')


def test_missing_runtime_source_is_rejected(source, tmp_path):
    (source / 'app/api.py').unlink()
    with pytest.raises(ValueError, match='Required release source'):
        package.build(source, tmp_path / 'output')


def test_output_symlink_is_rejected(source, tmp_path):
    output = tmp_path / 'output'
    output.mkdir()
    (output / package.ARCHIVE_NAME).symlink_to(tmp_path / 'unrelated')
    with pytest.raises(ValueError, match='Output files'):
        package.build(source, output)


def test_real_release_passes_actual_updater_extract_validation(tmp_path):
    archive, _ = package.build(REPOSITORY, tmp_path / 'output')
    contents = read_archive(archive)
    assert not any(name.startswith('.github/') for name in contents)
    # Extract through the real updater, not a copy of its validation rules.
    source = (REPOSITORY / 'scripts/update_server_pilot.sh').read_text()
    helper = source.split("<<'PY'\n", 1)[1].split('\nPY\n', 1)[0]
    ast.parse(helper)
    script = tmp_path / 'control.py'
    script.write_text(helper)
    old, stage = tmp_path / 'old', tmp_path / 'stage'
    old.mkdir()
    stage.mkdir()
    for name, content in contents.items():
        path = old / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    result = subprocess.run([sys.executable, str(script), 'extract', str(old), str(archive), str(stage)],
                            text=True, capture_output=True)
    assert result.returncode == 0, result.stderr
    assert (stage / 'app/api.py').read_bytes() == contents['app/api.py']
