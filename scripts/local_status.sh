#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -f "$project_root/.env.local-docker" ]; then
  echo "Not initialized. Run Start.command or ./scripts/local_start.sh."
  exit 0
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker with Compose v2 is required." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running or its socket is unavailable." >&2
  exit 1
fi
compose=(env -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE docker compose --project-name omnicrm-local --env-file "$project_root/.env.local-docker" -f "$project_root/compose.local.yaml")
"${compose[@]}" ps --all
if "${compose[@]}" exec -T api python -c "import json,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=3); r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/runtime',timeout=3)); assert r.get('local_test') is True and r.get('demo') is False"; then
  published_address="$("${compose[@]}" port api 8080)"
  echo "Local account-test API ready: http://$published_address"
  echo "Container health does not prove that individual messenger accounts are connected."
else
  echo "Local API is stopped, unhealthy, or has an unexpected runtime mode." >&2
  exit 1
fi
