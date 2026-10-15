#!/usr/bin/env bash
set -euo pipefail
umask 077

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
build_enabled=true
open_enabled=true
for argument in "$@"; do
  case "$argument" in
    --no-build) build_enabled=false ;;
    --no-open) open_enabled=false ;;
    --help|-h)
      echo "Usage: ./scripts/local_start.sh [--no-build] [--no-open]"
      echo "Starts the isolated local stack; preserves keys, accounts and messages."
      exit 0 ;;
    *) echo "Unknown option: $argument" >&2; exit 2 ;;
  esac
done

if ! command -v docker >/dev/null 2>&1; then
  echo "Docker is not installed. Install and start Docker Desktop on macOS, or Docker Engine with Compose v2 on Linux." >&2
  echo "https://docs.docker.com/get-started/get-docker/" >&2
  exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose v2 is required. Update Docker Desktop or install the Docker Compose plugin." >&2
  exit 1
fi
if ! docker compose up --help | grep -q -- '--wait-timeout'; then
  echo "Update Docker Compose: this launcher needs 'up --wait --wait-timeout'." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not ready. Open Docker Desktop and wait until it is running, then launch Start.command again." >&2
  echo "On Linux, start Docker and ensure your user has access to its socket. This script does not use sudo." >&2
  exit 1
fi

# Refuse an explicitly remote Docker target: a local-login service belongs only
# on the computer where the user opens the browser.
if [ -n "${DOCKER_CONTEXT:-}" ]; then
  docker_endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{(index .Endpoints "docker").Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then
  docker_endpoint="$DOCKER_HOST"
else
  docker_endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
fi
case "$docker_endpoint" in
  unix://*) ;;
  *) echo "Select a local Docker Desktop/Engine context using a Unix socket. Remote Docker targets are not supported by this local-login launcher." >&2; exit 1 ;;
esac

# Use the same Python base image as the app: no host Python/Xcode install needed.
docker_architecture="$(docker info --format '{{.Architecture}}')"
docker run --rm --network none --read-only \
  --user "$(id -u):$(id -g)" \
  --volume "$project_root:/workspace" --workdir /workspace \
  --env "LOCAL_PORT=${LOCAL_PORT:-8080}" \
  python:3.12-slim-bookworm python scripts/init_local_docker.py --architecture "$docker_architecture"

# Do not let a developer's inherited production/demo environment override the
# dedicated local credential file. LOCAL_PORT is the sole convenience override.
compose=(env -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE docker compose --project-name omnicrm-local --env-file "$project_root/.env.local-docker" -f "$project_root/compose.local.yaml")
"${compose[@]}" config --quiet
build_option=--no-build
if [ "$build_enabled" = true ]; then
  # Optional managed/corporate build CA; neither the CA nor credentials enter an image layer.
  build_ca_file="${LOCAL_BUILD_CA_FILE:-${CODEX_PROXY_CERT:-}}"
  if [ -n "$build_ca_file" ]; then
    if [ ! -r "$build_ca_file" ]; then
      echo "LOCAL_BUILD_CA_FILE/CODEX_PROXY_CERT does not refer to a readable file." >&2
      exit 1
    fi
    docker build --secret "id=proxy_ca,src=$build_ca_file" -t omnicrm:local "$project_root"
  else
    build_option=--build
  fi
fi

echo "Starting local API, worker, PostgreSQL and WhatsApp gateway. The first build/download can take several minutes."
if ! "${compose[@]}" up -d "$build_option" --wait --wait-timeout 180; then
  echo "The stack did not become ready. Existing keys and volumes have been retained." >&2
  "${compose[@]}" ps --all
  echo "Run ./scripts/local_status.sh and see docs/local-accounts.md. Do not delete .env.local-docker or Docker volumes to retry." >&2
  exit 1
fi

# Also validate explicit runtime flags instead of treating any HTTP 200 as success.
"${compose[@]}" exec -T api python -c "import json,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5); r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/runtime',timeout=5)); assert r.get('local_test') is True and r.get('demo') is False, 'Unexpected runtime mode'"
published_address="$("${compose[@]}" port api 8080)"
case "$published_address" in
  127.0.0.1:*) ;;
  *) echo "Unexpected API binding: refusing to open a service outside loopback." >&2; exit 1 ;;
esac
local_url="http://$published_address"
echo "Ready: $local_url"
echo "Use the local login button, then connect your own messenger accounts. Messages are real; no demo channel is enabled."
echo "Stop safely with Stop.command or ./scripts/local_stop.sh."
if [ "$open_enabled" = true ]; then
  case "$(uname -s)" in
    Darwin) open "$local_url" >/dev/null 2>&1 || true ;;
    Linux) if command -v xdg-open >/dev/null 2>&1; then xdg-open "$local_url" >/dev/null 2>&1 || true; fi ;;
  esac
fi
