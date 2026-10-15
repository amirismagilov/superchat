#!/usr/bin/env bash
# Read-only inventory before deploying a private pilot. No secrets or logs.
set -euo pipefail
uname -srm
if [ -r /etc/os-release ]; then
  sed -n '/^PRETTY_NAME=/p' /etc/os-release
fi
df -h / /opt
if command -v free >/dev/null 2>&1; then free -m; fi
if command -v nproc >/dev/null 2>&1; then nproc; fi
if command -v ss >/dev/null 2>&1; then
  ss -lnt '( sport = :18080 or sport = :18580 )'
fi
if [ -e /opt/omnicrm-pilot ]; then
  echo 'Existing /opt/omnicrm-pilot detected: inspect before uploading; do not overwrite.'
fi
if command -v docker >/dev/null 2>&1; then
  docker version --format 'Docker client={{.Client.Version}} server={{.Server.Version}}' || true
  docker compose version || true
  docker info --format 'Storage={{.Driver}} architecture={{.Architecture}} root={{.DockerRootDir}}' || true
  docker ps --format '{{.Names}} {{.Status}} {{.Ports}}' || true
else
  echo 'Docker is not installed; prepare installation for the detected OS.'
fi
