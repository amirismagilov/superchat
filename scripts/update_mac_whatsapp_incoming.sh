#!/usr/bin/env bash
# Focused adapter-only patch for an existing local Mac/Linux pilot; no DB migration.
set -euo pipefail
umask 077
revision="${1:-}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo 'Нужен полный SHA версии.' >&2; exit 2; }
project_root="${2:-${OMNICRM_LOCAL_DIR:-$HOME/Projects/omnicrm-local}}"
fail() { printf '%s\n' "$*" >&2; exit 1; }
[ -d "$project_root" ] && [ ! -L "$project_root" ] || fail 'Нужна существующая папка локальной CRM.'
project_root="$(cd -- "$project_root" && pwd -P)"
adapter="$project_root/app/adapters/whatsapp_web.py"
for path in "$project_root/.env.local-docker" "$project_root/compose.local.yaml" "$adapter"; do
  [ -f "$path" ] && [ ! -L "$path" ] || fail 'Конфигурация и исходный адаптер должны быть обычными файлами.'
done
[ ! -L "$project_root/app" ] && [ ! -L "$project_root/app/adapters" ] || fail 'Каталог адаптера не должен быть ссылкой.'
if ! command -v docker >/dev/null 2>&1 && [ -x /Applications/Docker.app/Contents/Resources/bin/docker ]; then
  export PATH="/Applications/Docker.app/Contents/Resources/bin:$PATH"
fi
for dependency in docker curl tar shasum; do
  command -v "$dependency" >/dev/null 2>&1 || fail "Не найдена команда $dependency."
done
hash() { shasum -a 256 "$1" | awk '{print $1}'; }
if [ -n "${DOCKER_CONTEXT:-}" ]; then
  endpoint="$(docker context inspect "$DOCKER_CONTEXT" --format '{{(index .Endpoints "docker").Host}}')"
elif [ -n "${DOCKER_HOST:-}" ]; then
  endpoint="$DOCKER_HOST"
else
  endpoint="$(docker context inspect --format '{{(index .Endpoints "docker").Host}}')"
fi
case "$endpoint" in unix://*) ;; *) fail 'Нужен локальный Docker Desktop/Engine, не удалённый Docker.' ;; esac
docker info >/dev/null 2>&1 || fail 'Запустите Docker Desktop и повторите команду.'
docker compose version >/dev/null 2>&1 || fail 'Нужен Docker Compose.'
compose=(env -u LOCAL_PORT -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE -u TELEGRAM_PROXY_URL docker compose --project-name omnicrm-local --env-file "$project_root/.env.local-docker" -f "$project_root/compose.local.yaml")
"${compose[@]}" config --quiet
api_id="$("${compose[@]}" ps -q api)"
worker_id="$("${compose[@]}" ps -q worker)"
waha_id="$("${compose[@]}" ps -q waha)"
postgres_id="$("${compose[@]}" ps -q postgres)"
[ -n "$api_id" ] && [ -n "$worker_id" ] && [ -n "$waha_id" ] && [ -n "$postgres_id" ] || fail 'Сначала запустите существующую локальную CRM.'
old_image="$(docker inspect --format '{{.Image}}' "$api_id")"
[[ "$old_image" =~ ^sha256:[a-f0-9]{64}$ ]] || fail 'Не удалось проверить существующий образ.'
[ "$(docker inspect --format '{{.Image}}' "$worker_id")" = "$old_image" ] || fail 'API и worker используют разные образы.'
[ "$(docker image inspect --format '{{.Id}}' omnicrm:local)" = "$old_image" ] || fail 'Локальный тег образа отличается от запущенной CRM.'
address="$("${compose[@]}" port api 8080)"
[[ "$address" =~ ^127\.0\.0\.1:[0-9]{1,5}$ ]] || fail 'Нужен единственный локальный адрес CRM на 127.0.0.1.'
published_port="${address##*:}"
[ "$((10#$published_port))" -ge 1 ] && [ "$((10#$published_port))" -le 65535 ] || fail 'Некорректный локальный порт CRM.'
# An earlier launch may have overridden .env's port; keep the running binding
# through both cutover and recovery without editing the user's environment file.
compose=(env -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE -u TELEGRAM_PROXY_URL LOCAL_PORT="$published_port" docker compose --project-name omnicrm-local --env-file "$project_root/.env.local-docker" -f "$project_root/compose.local.yaml")
env_hash="$(hash "$project_root/.env.local-docker")"
compose_hash="$(hash "$project_root/compose.local.yaml")"
lock="$project_root/.omnicrm-wa-incoming-update.lock"
mkdir "$lock" 2>/dev/null || fail 'Обновление уже запущено или осталась его блокировка. Не запускайте параллельно.'
stage=''
backup=''
source_changed=0
cutover=0
cleanup() {
  result=$?
  trap - EXIT INT TERM
  recovered=1
  if [ "$result" -ne 0 ]; then
    if [ "$source_changed" = 1 ]; then
      if cp -p "$backup/whatsapp_web.py" "$lock/restore.py"; then
        mv -f "$lock/restore.py" "$adapter" || recovered=0
      else recovered=0; fi
    fi
    if [ "$cutover" = 1 ]; then
      docker image tag "$old_image" omnicrm:local || recovered=0
      "${compose[@]}" up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api worker || recovered=0
    fi
    if [ "$recovered" = 1 ]; then
      echo 'Обновление не выполнено; прежний адаптер и приложение сохранены/восстановлены.' >&2
    else
      printf 'Нужна проверка восстановления. Резервная копия: %s\n' "$backup" >&2
    fi
  fi
  [ -z "$stage" ] || rm -rf -- "$stage"
  rm -rf -- "$lock"
  exit "$result"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
stage="$(mktemp -d "${TMPDIR:-/tmp}/omnicrm-wa-update.XXXXXXXX")"
echo 'Скачиваем зафиксированное исправление входящих WhatsApp…'
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
  "https://codeload.github.com/amirismagilov/superchat/tar.gz/$revision" -o "$stage/source.tar.gz"
entry="superchat-$revision/app/adapters/whatsapp_web.py"
[ "$(tar -tzf "$stage/source.tar.gz" "$entry" | wc -l | tr -d ' ')" = 1 ] || fail 'В архиве отсутствует единственный ожидаемый адаптер.'
[ "$(tar -tvzf "$stage/source.tar.gz" "$entry" | cut -c1)" = '-' ] || fail 'Адаптер в архиве должен быть обычным файлом.'
tar -xOf "$stage/source.tar.gz" "$entry" > "$stage/whatsapp_web.py"
[ -s "$stage/whatsapp_web.py" ] || fail 'Получен пустой адаптер.'
base_hash='7f1a58f1ab6e87810db8c786e3c023fd82a353a0a03b3efc39e7aa9a966d0814'
current_hash="$(hash "$adapter")"
target_hash="$(hash "$stage/whatsapp_web.py")"
[ "$current_hash" = "$base_hash" ] || [ "$current_hash" = "$target_hash" ] || fail 'Адаптер локально изменён или имеет другую версию. Изменения не перезаписываем.'
[ ! -L "$project_root/backups" ] || fail 'Каталог backups не должен быть ссылкой.'
mkdir -p "$project_root/backups"
release="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup="$project_root/backups/wa-incoming-$release"
mkdir -m 700 "$backup"
cp -p "$adapter" "$backup/whatsapp_web.py"
printf '%s\n' "$old_image" > "$backup/old-image-id"
docker image tag "$old_image" "omnicrm:local-wa-rollback-$release"
candidate="omnicrm:local-wa-candidate-$release"
build_ca_file="${LOCAL_BUILD_CA_FILE:-${CODEX_PROXY_CERT:-}}"
if [ -n "$build_ca_file" ]; then
  [ -r "$build_ca_file" ] || fail 'Файл сертификата сборки недоступен.'
fi
# Source changes while old containers keep running; failed builds restore it.
[ "$(hash "$adapter")" = "$current_hash" ] || fail 'Адаптер изменился во время подготовки; остановили обновление.'
source_changed=1
cp "$stage/whatsapp_web.py" "$lock/candidate.py"
chmod 644 "$lock/candidate.py"
mv -f "$lock/candidate.py" "$adapter"
echo 'Собираем исправленный образ. Запущенная CRM пока продолжает работать…'
if [ -n "$build_ca_file" ]; then
  docker build --secret "id=proxy_ca,src=$build_ca_file" -t "$candidate" "$project_root"
else
  docker build -t "$candidate" "$project_root"
fi
docker run --rm --network none --read-only --tmpfs /tmp --entrypoint python "$candidate" -c "import ast,pathlib; ast.parse(pathlib.Path('app/adapters/whatsapp_web.py').read_text())"
[ "$(hash "$project_root/.env.local-docker")" = "$env_hash" ] && [ "$(hash "$project_root/compose.local.yaml")" = "$compose_hash" ] || fail 'Конфигурация изменилась во время сборки; остановили обновление.'
cutover=1
docker image tag "$candidate" omnicrm:local
"${compose[@]}" up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api worker
"${compose[@]}" exec -T api python -c "import json,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5); r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/runtime',timeout=5)); assert r.get('local_test') is True and r.get('demo') is False" </dev/null
[ "$("${compose[@]}" ps -q waha)" = "$waha_id" ] && [ "$("${compose[@]}" ps -q postgres)" = "$postgres_id" ] || fail 'Контейнер WhatsApp или PostgreSQL изменился; нужна проверка.'
[ "$("${compose[@]}" port api 8080)" = "$address" ] || fail 'Изменился порт локальной CRM.'
[ "$(hash "$project_root/.env.local-docker")" = "$env_hash" ] || fail 'Изменился файл ключей; нужна проверка.'
printf '%s %s\n' "$revision" "$target_hash" > "$project_root/.omnicrm-wa-incoming-patch"
cutover=0
printf 'WA_INCOMING_UPDATE_OK — исправление установлено. WhatsApp, база и ключи сохранены.\nОткройте http://%s и отправьте новое входящее сообщение.\nРезервная копия: %s\n' "$address" "$backup"
