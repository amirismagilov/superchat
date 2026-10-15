#!/usr/bin/env bash
# Private, single-administrator account test accessed with authenticated SSH -L.
set -euo pipefail
umask 077
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$project_root"
action="${1:-start}"
case "$action" in start|stop|status) ;; *) echo 'Usage: bash scripts/server_pilot.sh [start|stop|status]' >&2; exit 2 ;; esac
command -v docker >/dev/null 2>&1 || { echo 'Install Docker Engine and Compose v2 for this server first.' >&2; exit 1; }
docker info >/dev/null 2>&1 || { echo 'Docker daemon is not available.' >&2; exit 1; }
docker compose version >/dev/null 2>&1 || { echo 'Docker Compose v2 is required.' >&2; exit 1; }
if [ -n "${DOCKER_CONTEXT:-}" ]; then
  docker_endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{(index .Endpoints "docker").Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then
  docker_endpoint="$DOCKER_HOST"
else
  docker_endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
fi
case "$docker_endpoint" in unix://*) ;; *) echo 'Run this script on the target server using its local Docker socket.' >&2; exit 1 ;; esac
if [ "$action" = start ]; then
  docker_architecture="$(docker info --format '{{.Architecture}}')"
  docker run --rm --network none --read-only --user "$(id -u):$(id -g)" \
    --volume "$project_root:/workspace" --workdir /workspace \
    python:3.12-slim-bookworm python scripts/init_local_docker.py \
    --output /workspace/.env.pilot --port "${LOCAL_PORT:-18580}" --architecture "$docker_architecture"
elif [ ! -f .env.pilot ]; then
  echo 'Pilot has not been initialized.'
  exit 0
fi
# Use the port saved with the pilot's credentials, including on later restarts.
compose=(env -u LOCAL_PORT -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE docker compose --project-name omnicrm-pilot --env-file "$project_root/.env.pilot" -f "$project_root/compose.pilot.yaml")
"${compose[@]}" config --quiet
case "$action" in
  start)
    build_ca_file="${LOCAL_BUILD_CA_FILE:-${CODEX_PROXY_CERT:-}}"
    build_args=()
    if [ -n "$build_ca_file" ]; then
      test -r "$build_ca_file" || { echo 'Build CA file is not readable.' >&2; exit 1; }
      build_args+=(--secret "id=proxy_ca,src=$build_ca_file")
    fi
    docker build "${build_args[@]}" -t omnicrm:pilot "$project_root"
    "${compose[@]}" up -d --no-build --wait --wait-timeout 180
    ;;
  stop)
    "${compose[@]}" stop --timeout 60
    echo 'Pilot stopped. Keys, accounts and history retained.'
    exit 0
    ;;
esac
"${compose[@]}" ps --all
"${compose[@]}" exec -T api python -c "import json,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5); r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/runtime',timeout=5)); assert r == {'local_test': True, 'demo': False}"
published_address="$("${compose[@]}" port api 8080)"
case "$published_address" in 127.0.0.1:*) ;; *) echo 'Unexpected non-loopback binding.' >&2; exit 1 ;; esac
echo "Private pilot ready on server $published_address. Open it through an authenticated SSH local forward."
published_port="${published_address##*:}"
echo 'In another terminal on your computer, run:'
printf 'ssh -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 -L 127.0.0.1:%s:127.0.0.1:%s %s\n' "$published_port" "$published_port" "${OMNICRM_SSH_TARGET:-USER@SERVER}"
printf 'Then open http://127.0.0.1:%s in your browser.\n' "$published_port"
