#!/usr/bin/env bash
set -u
project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$project_root/scripts/local_start.sh" "$@"
result=$?
if [ "$result" -ne 0 ] && [ -t 0 ]; then
  echo "Launch failed. Read the diagnostic above; your data was not deleted."
  read -r -p "Press Enter to close this window... " _ || true
fi
exit "$result"
