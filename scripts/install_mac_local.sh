#!/usr/bin/env bash
# Install a pinned, independent local copy. Never contacts the pilot server.
set -euo pipefail
umask 077
revision="${1:-}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo 'Нужен полный SHA версии.' >&2; exit 2; }
destination="${OMNICRM_LOCAL_DIR:-$HOME/Projects/omnicrm-local}"
[ ! -L "$destination" ] || { echo 'Папка установки не должна быть ссылкой.' >&2; exit 1; }

if ! command -v docker >/dev/null 2>&1 && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
for dependency in docker curl tar; do
  command -v "$dependency" >/dev/null 2>&1 || { echo "Не найдена команда $dependency. Запустите установленный Docker Desktop." >&2; exit 1; }
done
if [ -n "${DOCKER_CONTEXT:-}" ]; then
  endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{(index .Endpoints "docker").Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then
  endpoint="$DOCKER_HOST"
else
  endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
fi
case "$endpoint" in unix://*) ;; *) echo 'Выберите локальный Docker Desktop: удалённый Docker здесь не используется.' >&2; exit 1 ;; esac

if ! docker info >/dev/null 2>&1; then
  if [ "$(uname -s)" = Darwin ]; then
    open -a Docker
    echo 'Ждём запуска Docker Desktop…'
    for attempt in $(seq 1 60); do
      docker info >/dev/null 2>&1 && break
      sleep 2
    done
  fi
  docker info >/dev/null 2>&1 || { echo 'Docker ещё не запущен. Откройте Docker Desktop и повторите команду.' >&2; exit 1; }
fi

if [ -e "$destination" ]; then
  if [ ! -f "$destination/.omnicrm-local-source" ] || [ ! -f "$destination/scripts/local_start.sh" ]; then
    echo "Папка уже существует: $destination. Её содержимое не изменено." >&2
    exit 1
  fi
  echo "Запускаем имеющуюся локальную копию: $destination"
else
  volumes="$(docker volume ls --format '{{.Name}}' --filter 'label=com.docker.compose.project=omnicrm-local')"
  if [ -n "$volumes" ]; then
    echo 'В Docker уже есть локальная CRM. Нужна её исходная папка с .env.local-docker, чтобы сохранить ключи и историю.' >&2
    exit 1
  fi
  parent="$(dirname -- "$destination")"
  mkdir -p "$parent"
  stage="$(mktemp -d "$parent/.omnicrm-download.XXXXXXXX")"
  cleanup() { rm -rf -- "$stage"; }
  trap cleanup EXIT
  echo 'Скачиваем зафиксированную версию CRM…'
  curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
    "https://codeload.github.com/amirismagilov/superchat/tar.gz/$revision" \
    -o "$stage/source.tar.gz"
  tar -xzf "$stage/source.tar.gz" -C "$stage"
  source_root="$stage/superchat-$revision"
  [ -f "$source_root/compose.local.yaml" ] && [ -f "$source_root/scripts/local_start.sh" ] || { echo 'Архив не содержит локального запуска.' >&2; exit 1; }
  printf '%s\n' "$revision" > "$source_root/.omnicrm-local-source"
  [ ! -e "$destination" ] || { echo 'Папка появилась во время скачивания. Повторите запуск.' >&2; exit 1; }
  mv "$source_root" "$destination"
fi

echo "Файлы CRM: $destination"
echo 'Первый запуск скачает образы и соберёт приложение. Это может занять несколько минут.'
if [ ! -f "$destination/.env.local-docker" ]; then
  export LOCAL_PORT=18581
fi
bash "$destination/scripts/local_start.sh"
