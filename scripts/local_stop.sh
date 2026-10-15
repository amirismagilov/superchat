#!/usr/bin/env bash
set -euo pipefail
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
if [ ! -f "$project_root/.env.local-docker" ]; then
  echo "No local Docker configuration exists yet. Nothing to stop."
  exit 0
fi
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker with Compose v2 is required to stop the local stack." >&2
  exit 1
fi
if ! docker info >/dev/null 2>&1; then
  echo "Docker is not running or its socket is unavailable. No files or volumes have been changed." >&2
  exit 1
fi
env -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE \
  docker compose --project-name omnicrm-local --env-file "$project_root/.env.local-docker" \
  -f "$project_root/compose.local.yaml" stop --timeout 60
echo "Local containers stopped. Accounts, history, WhatsApp sessions and encryption keys retained."
