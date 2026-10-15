#!/usr/bin/env bash
# Run on macOS/Linux next to the trusted release archive and its SHA-256 file.
set -euo pipefail
ssh_target="${OMNICRM_SSH_TARGET:?Set OMNICRM_SSH_TARGET to user@your-server}"
umask 077
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$bundle_dir"
archive=omnicrm-server-pilot.tar.gz
checksum_file=omnicrm-server-pilot.tar.gz.sha256
for command in ssh tar awk; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required command is missing: $command" >&2; exit 1; }
done
[ -f "$archive" ] && [ -f "$checksum_file" ] || { echo 'Keep Update.command, the .tar.gz archive and the .sha256 file in one folder.' >&2; exit 1; }
expected="$(awk 'NR == 1 {print $1}' "$checksum_file")"
[[ "$expected" =~ ^[a-f0-9]{64}$ ]] || { echo 'Invalid archive checksum.' >&2; exit 1; }
if command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$archive" | awk '{print $1}')"
else
  actual="$(sha256sum "$archive" | awk '{print $1}')"
fi
[ "$actual" = "$expected" ] || { echo 'Archive checksum mismatch. Download the complete trusted bundle again.' >&2; exit 1; }
tar -tzf "$archive" >/dev/null
printf '%s\n' "Updating the existing private pilot on $ssh_target." 'Enter your SSH key passphrase or server password only in this terminal when SSH asks.'
remote_command="expected=$expected
$(cat <<'REMOTE'
set -eu
umask 077
incoming=$(mktemp /tmp/omnicrm-pilot-update.XXXXXXXX.tar.gz)
trap 'rm -f "$incoming"' 0
cat > "$incoming"
printf '%s  %s\n' "$expected" "$incoming" | sha256sum -c -
updater=$(tar -xOf "$incoming" omnicrm/scripts/update_server_pilot.sh)
bash -c "$updater" -- "$incoming" "$expected"
REMOTE
)"
set -- -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3
if [ "${OMNICRM_SSH_PASSWORD:-0}" = 1 ]; then
  set -- -o PubkeyAuthentication=no -o PreferredAuthentications=password,keyboard-interactive "$@"
fi
ssh "$@" \
  "$ssh_target" "$remote_command" < "$archive"
printf '\nUpdate finished. Keep your existing SSH tunnel, then reload the browser page.\n'
