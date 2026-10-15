#!/usr/bin/env bash
set -u
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$project_root/scripts/local_stop.sh" "$@"
result=$?
if [ "$result" -ne 0 ] && [ -t 0 ]; then
  read -r -p "Press Enter to close this window... " _ || true
fi
exit "$result"
