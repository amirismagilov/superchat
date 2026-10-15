#!/usr/bin/env bash
# One-time, explicitly authorized server installation; existing private pilot only.
set -euo pipefail
umask 077
repository="${1:-amirismagilov/superchat}"
[[ "$repository" =~ ^[A-Za-z0-9][A-Za-z0-9_-]{0,99}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$ ]] || { echo 'Use OWNER/REPO for a public GitHub repository.' >&2; exit 1; }
[ "$(id -u)" = 0 ] || { echo 'Run as root on the target server.' >&2; exit 1; }
for command in git python3 systemctl docker flock install; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required command is missing: $command" >&2; exit 1; }
done
source_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
[ -f "$source_dir/git_auto_update.py" ] && [ ! -L "$source_dir/git_auto_update.py" ] || { echo 'Keep this installer and git_auto_update.py from the same pinned release together.' >&2; exit 1; }
[ -d /opt/omnicrm-pilot ] && [ ! -L /opt/omnicrm-pilot ] && [ -f /opt/omnicrm-pilot/.env.pilot ] && [ ! -L /opt/omnicrm-pilot/.env.pilot ] || { echo 'An existing private pilot and its original .env.pilot are required.' >&2; exit 1; }
# Existing script validates its local Docker context, runtime and loopback address.
if ! bash /opt/omnicrm-pilot/scripts/server_pilot.sh status >/dev/null 2>&1; then
  echo 'Existing pilot health check failed; inspect it before enabling automatic updates.' >&2
  exit 1
fi
root=/opt/omnicrm-pilot-autoupdate
[ ! -L "$root" ] || { echo 'Autoupdate directory cannot be a symlink.' >&2; exit 1; }
if [ -e "$root" ]; then
  [ "$(stat -c %u "$root")" = 0 ] && [ "$(stat -c %a "$root")" = 700 ] || { echo 'Existing autoupdate directory must be root-owned, mode 0700.' >&2; exit 1; }
fi
install -d -m 700 -o root -g root "$root"
exec 8>"$root/poll.lock"
flock -n 8 || { echo 'An automatic update is running. Retry installation when it finishes.' >&2; exit 1; }
python3 - "$root" "$repository" <<'PY'
import json, os, pathlib, sys
root, repo = pathlib.Path(sys.argv[1]), sys.argv[2]
config = root / 'config.json'
value = {'repository': repo, 'branch': 'main', 'workflow': '.github/workflows/ci.yml'}
if config.exists() or config.is_symlink():
    if config.is_symlink() or config.stat().st_uid != 0 or config.stat().st_mode & 0o077 or json.loads(config.read_text()) != value:
        raise SystemExit('Existing autoupdate config differs; inspect it manually before reinstalling.')
else:
    with config.open('x') as handle:
        json.dump(value, handle)
    config.chmod(0o600)
for name in ('git_auto_update.py', 'state.json', 'poll.lock'):
    path = root / name
    if path.is_symlink():
        raise SystemExit('Unexpected symlink in autoupdate directory.')
PY
install -m 700 -o root -g root "$source_dir/git_auto_update.py" "$root/git_auto_update.py.new"
mv -f "$root/git_auto_update.py.new" "$root/git_auto_update.py"
# Distinct unit names: existing project services, ports, database and volumes are untouched.
for unit in omnicrm-pilot-autoupdate.service omnicrm-pilot-autoupdate.timer; do
  [ ! -L "/etc/systemd/system/$unit" ] || { echo "Unit $unit is a symlink; inspect it before installation." >&2; exit 1; }
  if [ -e "/etc/systemd/system/$unit" ] && ! head -n 1 "/etc/systemd/system/$unit" | grep -Fxq '# Managed by OmniCRM private pilot autoupdate'; then
    echo "Existing unrelated unit $unit found; refusing to replace it." >&2
    exit 1
  fi
done
cat > /etc/systemd/system/omnicrm-pilot-autoupdate.service <<'UNIT'
# Managed by OmniCRM private pilot autoupdate
[Unit]
Description=Update private OmniCRM pilot from successful trusted main CI
Wants=network-online.target
After=network-online.target docker.service
ConditionPathExists=/opt/omnicrm-pilot/.env.pilot

[Service]
Type=oneshot
User=root
Group=root
UMask=0077
WorkingDirectory=/opt/omnicrm-pilot-autoupdate
ExecStart=/usr/bin/python3 /opt/omnicrm-pilot-autoupdate/git_auto_update.py
TimeoutStartSec=30min
TimeoutStopSec=5min
KillMode=control-group
PrivateTmp=true
NoNewPrivileges=true
StandardOutput=journal
StandardError=journal
UNIT
cat > /etc/systemd/system/omnicrm-pilot-autoupdate.timer <<'UNIT'
# Managed by OmniCRM private pilot autoupdate
[Unit]
Description=Check public GitHub main for a successful OmniCRM release every two minutes

[Timer]
OnBootSec=2min
OnUnitInactiveSec=2min
RandomizedDelaySec=10
Unit=omnicrm-pilot-autoupdate.service

[Install]
WantedBy=timers.target
UNIT
systemctl daemon-reload
systemctl enable --now omnicrm-pilot-autoupdate.timer
printf '%s\n' 'AUTOUPDATE_INSTALLED — timer checks main every two minutes; no application deployment has been claimed.' 'Status: python3 /opt/omnicrm-pilot-autoupdate/git_auto_update.py --status' 'Disable future checks: systemctl disable --now omnicrm-pilot-autoupdate.timer' 'One-time retry after inspection: python3 /opt/omnicrm-pilot-autoupdate/git_auto_update.py --retry'
