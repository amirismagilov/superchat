#!/usr/bin/env bash
# Run on macOS/Linux next to the source archive and its SHA-256 file.
set -euo pipefail
umask 077
bundle_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd "$bundle_dir"
archive=omnicrm-server-pilot.tar.gz
checksum_file=omnicrm-server-pilot.tar.gz.sha256
ssh_target="${OMNICRM_SSH_TARGET:?Set OMNICRM_SSH_TARGET to user@your-server}"
for command in ssh tar; do
  command -v "$command" >/dev/null 2>&1 || { echo "Required command is missing: $command" >&2; exit 1; }
done
[ -f "$archive" ] && [ -f "$checksum_file" ] || { echo 'Keep this script, the .tar.gz archive and the .sha256 file in the same folder.' >&2; exit 1; }
expected="$(awk 'NR == 1 {print $1}' "$checksum_file")"
[[ "$expected" =~ ^[[:xdigit:]]{64}$ ]] || { echo 'Invalid archive checksum.' >&2; exit 1; }
if command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$archive" | awk '{print $1}')"
else
  actual="$(sha256sum "$archive" | awk '{print $1}')"
fi
[ "$actual" = "$expected" ] || { echo 'Archive checksum mismatch. Download the complete bundle again.' >&2; exit 1; }
tar -tzf "$archive" >/dev/null
printf 'Connecting to %s with your usual SSH authentication.\n' "$ssh_target"
echo 'Enter a key passphrase or server password only in this terminal when SSH asks.'

# The source archive is streamed as stdin; no credentials are copied to the server.
# Only a validated hexadecimal digest is substituted into the remote command.
remote_command="expected=$expected
$(cat <<'REMOTE'
set -eu
umask 077
incoming=$(mktemp /tmp/omnicrm-pilot.XXXXXXXX.tar.gz)
trap 'rm -f "$incoming"' 0
cat > "$incoming"
printf '%s  %s\n' "$expected" "$incoming" | sha256sum -c -
installer=$(tar -xOf "$incoming" omnicrm/scripts/install_server_pilot.sh)
bash -c "$installer" -- "$incoming"
REMOTE
)"
ssh -o ConnectTimeout=15 -o ServerAliveInterval=30 -o ServerAliveCountMax=3 \
  "$ssh_target" "$remote_command" < "$archive"
echo
echo 'Installation finished. Use the SSH tunnel command and browser address printed above.'
