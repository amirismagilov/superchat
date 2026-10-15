#!/usr/bin/env python3
"""Build a deterministic, source-only release accepted by the pilot updater.

The generated manifest lives inside the archive. Working-tree manifests and
runtime files are never rewritten or bundled. No third-party modules required.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tarfile


ARCHIVE_NAME = "omnicrm-server-pilot.tar.gz"
MANIFEST = "docs/source-manifest.json"
TOP = {
    ".dockerignore", ".gitignore", "Dockerfile", "LICENSE", "README.md",
    "Start.command", "Stop.command", "requirements.in", "requirements.lock",
    "compose.yaml", "compose.local.yaml", "compose.pilot.yaml",
}
DOCS = {
    "architecture.md", "ci.md", "dependency-inventory.json",
    "frontend-integration.md", "implementation-contract.md", "load-test.md",
    "local-accounts.md", "official-connectors.md", "operations.md", "opensource-assessment.md",
    "personal-adapters.md", "server-pilot.md", "server-update.md",
    "telegram-adapter.md", "verification.md", "git-auto-update.md",
}
SUFFIXES = {
    "app": {".py"}, "web": {".js", ".css", ".html", ".svg"},
    "scripts": {".py", ".sh", ".command", ".service", ".timer"},
    "tests": {".py", ".cjs", ".mjs"},
}
EXACT = {"tests/browser/package.json", "tests/browser/package-lock.json"}
EXCLUDED_DIRS = {"node_modules", "__pycache__", "venv", "backups", "artifacts", "test-results", "sessions", "media"}
REQUIRED = {
    "Dockerfile", "requirements.lock", "compose.pilot.yaml", "app/api.py",
    "app/worker.py", "web/index.html", "scripts/migrate.py",
    "scripts/server_pilot.sh", "scripts/update_server_pilot.sh",
}
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024


def allowed(name: str) -> bool:
    """Independent allowlist, intentionally narrower than the updater's safe()."""
    if name in TOP:
        return True
    if not re.fullmatch(r"[A-Za-z0-9_./-]+", name):
        return False
    path = PurePosixPath(name)
    if path.is_absolute() or str(path) != name or len(path.parts) < 2:
        return False
    if any(part in {"", ".", ".."} or part.startswith(".") or part in EXCLUDED_DIRS for part in path.parts):
        return False
    if name in EXACT:
        return True
    if path.parts[0] == "docs":
        return len(path.parts) == 2 and path.name in DOCS
    return path.suffix in SUFFIXES.get(path.parts[0], set())


def read_regular(root: Path, name: str) -> bytes:
    path = root
    for part in PurePosixPath(name).parts:
        path /= part
        if path.is_symlink():
            raise ValueError("Symlink is not allowed in source path: " + name)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Source must be a regular file: " + name)
    if info.st_size > MAX_FILE:
        raise ValueError("Source file exceeds package limit: " + name)
    # Do not follow a link introduced between lstat and open.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("Source must be a regular file: " + name)
        content = stream.read(MAX_FILE + 1)
    if len(content) > MAX_FILE:
        raise ValueError("Source file exceeds package limit: " + name)
    return content


def collect(root: Path) -> dict[str, bytes]:
    root = root.resolve(strict=True)
    names = {name for name in TOP if (root / name).exists() or (root / name).is_symlink()}
    for prefix in (*SUFFIXES, "docs"):
        base = root / prefix
        if base.is_symlink():
            raise ValueError("Source directory must not be a symlink: " + prefix)
        if not base.exists():
            continue
        for current, directories, files in os.walk(base, followlinks=False):
            for directory in directories:
                candidate = Path(current) / directory
                if candidate.is_symlink() and not directory.startswith(".") and directory not in EXCLUDED_DIRS:
                    raise ValueError("Source directory must not be a symlink: " + str(candidate.relative_to(root)))
            directories[:] = [name for name in directories if not name.startswith(".") and name not in EXCLUDED_DIRS]
            for filename in files:
                name = str((Path(current) / filename).relative_to(root))
                if allowed(name):
                    names.add(name)
    if REQUIRED - names:
        raise ValueError("Required release source is missing: " + ", ".join(sorted(REQUIRED - names)))
    if len(names) > 2000:
        raise ValueError("Too many source files")
    contents = {name: read_regular(root, name) for name in sorted(names)}
    if sum(map(len, contents.values())) > MAX_TOTAL - 512 * 1024:
        raise ValueError("Source archive exceeds package limit")
    return contents


def build(root: Path, output_dir: Path) -> tuple[Path, str]:
    contents = collect(root)
    entries = {name: hashlib.sha256(data).hexdigest() for name, data in contents.items()}
    contents[MANIFEST] = (json.dumps(entries, indent=2, sort_keys=True) + "\n").encode()
    output_dir.mkdir(parents=True, exist_ok=True)
    archive = output_dir / ARCHIVE_NAME
    checksum = output_dir / (ARCHIVE_NAME + ".sha256")
    if archive.is_symlink() or checksum.is_symlink():
        raise ValueError("Output files must not be symlinks")
    # Stable ordering, ownership, modes, tar times, gzip header and compression.
    with archive.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0, compresslevel=9) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as tar:
                for name, data in sorted(contents.items()):
                    info = tarfile.TarInfo("omnicrm/" + name)
                    info.size = len(data)
                    info.mode = 0o755 if PurePosixPath(name).suffix in {".sh", ".command"} else 0o644
                    info.uid = info.gid = info.mtime = 0
                    info.uname = info.gname = ""
                    tar.addfile(info, io.BytesIO(data))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    checksum.write_text(digest + "  " + ARCHIVE_NAME + "\n", encoding="ascii")
    return archive, digest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    archive, digest = build(Path(__file__).resolve().parents[1], args.output_dir)
    print(f"Source-only release: {archive}")
    print(f"SHA-256: {digest}")


if __name__ == "__main__":
    main()
