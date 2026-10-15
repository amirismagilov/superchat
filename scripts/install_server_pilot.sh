#!/usr/bin/env bash
# First installation only. Invoked on the server with a verified source archive.
set -euo pipefail
umask 077
archive="${1:?Source archive path is required}"
target=/opt/omnicrm-pilot
project=omnicrm-pilot

fail() { printf '%s\n' "$*" >&2; exit 1; }
[ "$(id -u)" = 0 ] || fail 'Run the installer as root on the target server.'
[ -f "$archive" ] || fail 'Source archive is missing.'
if [ -e "$target" ] || [ -L "$target" ]; then
  fail 'Existing /opt/omnicrm-pilot found. Installation stopped without overwriting it. For an existing pilot, inspect it and run: cd /opt/omnicrm-pilot && bash scripts/server_pilot.sh status'
fi
for command in docker ss tar df awk; do
  command -v "$command" >/dev/null 2>&1 || fail "Required command is missing: $command"
done
docker info >/dev/null 2>&1 || fail 'Docker daemon is unavailable.'
docker compose version >/dev/null 2>&1 || fail 'Docker Compose is unavailable.'
if [ -n "${DOCKER_CONTEXT:-}" ]; then
  endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{(index .Endpoints "docker").Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then
  endpoint="$DOCKER_HOST"
else
  endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
fi
case "$endpoint" in unix://*) ;; *) fail 'Remote Docker contexts are not supported. Select the local Docker Engine.' ;; esac

printf '\nServer inventory (read only):\n'
tar -xOf "$archive" omnicrm/scripts/server_preflight.sh | bash
if [ -n "${LOCAL_PORT:-}" ]; then
  [[ "$LOCAL_PORT" =~ ^[1-9][0-9]{3,4}$ ]] || fail 'LOCAL_PORT must be an integer between 1024 and 65535.'
  [ "$LOCAL_PORT" -ge 1024 ] && [ "$LOCAL_PORT" -le 65535 ] || fail 'LOCAL_PORT must be an integer between 1024 and 65535.'
  candidates=("$LOCAL_PORT")
else
  candidates=({18580..18599})
fi
chosen_port=''
for candidate in "${candidates[@]}"; do
  listeners="$(ss -H -lnt "( sport = :$candidate )")"
  if [ -z "$listeners" ]; then
    chosen_port="$candidate"
    break
  fi
done
[ -n "$chosen_port" ] || fail 'No requested TCP port is free. No pilot has been installed.'
printf '\nSelected private pilot address: 127.0.0.1:%s\n' "$chosen_port"
for resource in container network volume; do
  existing="$(docker "$resource" ls --quiet --filter "label=com.docker.compose.project=$project")"
  [ -z "$existing" ] || fail "Existing Docker $resource resources for omnicrm-pilot found. Inspect them before installing."
done
if docker image inspect omnicrm:pilot >/dev/null 2>&1; then
  fail 'The Docker image omnicrm:pilot already exists. Inspect it before installing.'
fi
for volume in omnicrm-pilot_postgres-data omnicrm-pilot_waha-sessions omnicrm-pilot_waha-media; do
  if docker volume inspect "$volume" >/dev/null 2>&1; then
    fail "Existing Docker volume $volume found. It will not be overwritten."
  fi
done
for network in omnicrm-pilot_backend omnicrm-pilot_outbound; do
  if docker network inspect "$network" >/dev/null 2>&1; then
    fail "Existing Docker network $network found. It will not be changed."
  fi
done
docker_root="$(docker info --format '{{.DockerRootDir}}')"
for filesystem in /opt "$docker_root"; do
  available_kb="$(df -Pk "$filesystem" | awk 'NR == 2 { print $4 }')"
  [[ "$available_kb" =~ ^[0-9]+$ ]] || fail 'Unable to determine free disk space.'
  [ "$available_kb" -ge 12582912 ] || fail "Less than 12 GiB free at $filesystem. Free space before installing."
done

# mkdir without -p also refuses a directory created concurrently after the checks.
mkdir -m 700 "$target"
tar -xzf "$archive" -C "$target" --strip-components=1 --no-same-owner
cd "$target"
printf '\nInstalling private pilot in %s\n' "$target"
LOCAL_PORT="$chosen_port" bash scripts/server_pilot.sh start
printf '\nINSTALLATION_OK — open an SSH tunnel from your computer.\n'
