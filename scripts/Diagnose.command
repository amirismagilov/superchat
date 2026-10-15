#!/usr/bin/env bash
# Read-only diagnosis; stream Python into the running API without installing files.
set -euo pipefail
ssh_target="${OMNICRM_SSH_TARGET:?Set OMNICRM_SSH_TARGET to user@your-server}"
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$bundle_dir/diagnose_whatsapp.py" ] || { echo 'Keep Diagnose.command and diagnose_whatsapp.py in the same folder.' >&2; exit 1; }
command -v ssh >/dev/null 2>&1 || { echo 'SSH is required.' >&2; exit 1; }
set -- -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3
if [ "${OMNICRM_SSH_PASSWORD:-0}" = 1 ]; then
  set -- -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive "$@"
fi
remote_command="$(cat <<'REMOTE'
set -eu
cd /opt/omnicrm-pilot 2>/dev/null || { printf '%s\n' '{"diagnostic":"whatsapp_readonly_v1","result":"pilot_directory_missing"}'; exit 1; }
[ -f .env.pilot ] && [ -f compose.pilot.yaml ] || { printf '%s\n' '{"diagnostic":"whatsapp_readonly_v1","result":"pilot_config_missing"}'; exit 1; }
# Do not print resolved configuration, logs, environment or raw Docker errors.
if ! env -u LOCAL_PORT -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE \
  docker compose --project-name omnicrm-pilot --env-file .env.pilot -f compose.pilot.yaml \
  exec -T -e PYTHONDONTWRITEBYTECODE=1 api python - 2>/dev/null; then
  printf '%s\n' '{"diagnostic":"whatsapp_readonly_v1","result":"diagnostic_exec_failed"}'
  exit 1
fi
REMOTE
)"
printf '%s\n' 'Read-only WhatsApp diagnosis; no restarts, login attempts, account changes or messages.' 'Enter your SSH passphrase or server password only in this terminal if requested.'
ssh "$@" "$ssh_target" "$remote_command" < "$bundle_dir/diagnose_whatsapp.py"
