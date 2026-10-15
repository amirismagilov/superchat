#!/usr/bin/env bash
# Fixed MAX source patch for an existing local pilot; no DB/session changes.
set -euo pipefail
umask 077
revision="${1:-}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo 'Нужен полный SHA версии.' >&2; exit 2; }
project_root="${2:-${OMNICRM_LOCAL_DIR:-$HOME/Projects/omnicrm-local}}"
fail() { printf '%s\n' "$*" >&2; exit 1; }
[ -d "$project_root" ] && [ ! -L "$project_root" ] || fail 'Нужна существующая папка локальной CRM.'
project_root="$(cd -- "$project_root" && pwd -P)"
files=(
  app/adapters/max_personal.py app/capabilities.py app/api.py app/worker.py
  web/inbox.js web/styles.css
)
# Accepted source from bbaf270, also unchanged in the original f4f533a local install.
base_hashes=(
  06c79075a57ef17a7f48797c19e9d91b3f9180a3d8b369a467fa48656ebb98b1
  be7ec4c32657e070fb2aedd8e4738b3eea9e7ba336c0fd82c33e045df15e8ac4
  6b67dfbfbdf7a58ef0a03a2b7da2f2672f78150b170634634606735925f20dd4
  57e1a62b51bc95d5673981c4a9cfe18db98c4724e1bcee57ce6d364147ba920d
  49bc42adc0be1df1756a4debc106ca80cb69b6603f7ce3e7288ed30b57a5e57a
  149aa12a71773e699242cd94ca514b1ebdd59f1e67069a7b91d23b3173fc2a69
)
for directory in app app/adapters web; do
  [ -d "$project_root/$directory" ] && [ ! -L "$project_root/$directory" ] || fail 'Каталоги исходников должны быть обычными папками.'
done
for relative in .env.local-docker compose.local.yaml "${files[@]}"; do
  [ -f "$project_root/$relative" ] && [ ! -L "$project_root/$relative" ] || fail 'Конфигурация и исходники должны быть обычными файлами.'
done
[ ! -L "$project_root/.omnicrm-max-patch" ] || fail 'Маркер обновления не должен быть ссылкой.'
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
# Share the established lock so MAX and WhatsApp patches cannot overlap.
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
      for relative in "${files[@]}"; do
        if cp -p "$backup/$relative" "$lock/restore"; then
          mv -f "$lock/restore" "$project_root/$relative" || recovered=0
        else recovered=0; fi
      done
    fi
    if [ "$cutover" = 1 ]; then
      docker image tag "$old_image" omnicrm:local || recovered=0
      "${compose[@]}" up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api worker || recovered=0
    fi
    if [ "$recovered" = 1 ]; then
      echo 'Обновление не выполнено; прежние исходники и приложение сохранены/восстановлены.' >&2
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
stage="$(mktemp -d "${TMPDIR:-/tmp}/omnicrm-max-update.XXXXXXXX")"
echo 'Скачиваем зафиксированное обновление MAX…'
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
  "https://codeload.github.com/amirismagilov/superchat/tar.gz/$revision" -o "$stage/source.tar.gz"
# Extract only explicitly named regular entries; no archive path is unpacked.
for index in "${!files[@]}"; do
  relative="${files[$index]}"
  entry="superchat-$revision/$relative"
  [ "$(tar -tzf "$stage/source.tar.gz" "$entry" | wc -l | tr -d ' ')" = 1 ] || fail 'В архиве отсутствует единственный ожидаемый файл.'
  [ "$(tar -tvzf "$stage/source.tar.gz" "$entry" | cut -c1)" = '-' ] || fail 'Исходник в архиве должен быть обычным файлом.'
  tar -xOf "$stage/source.tar.gz" "$entry" > "$stage/$index"
  [ -s "$stage/$index" ] || fail 'Получен пустой исходник.'
  current_hash="$(hash "$project_root/$relative")"
  target_hash="$(hash "$stage/$index")"
  [ "$current_hash" = "${base_hashes[$index]}" ] || [ "$current_hash" = "$target_hash" ] || fail "Файл $relative локально изменён или имеет другую версию. Обновление остановлено."
  printf '%s\n' "$current_hash" > "$stage/$index.original-hash"
done
[ ! -L "$project_root/backups" ] || fail 'Каталог backups не должен быть ссылкой.'
mkdir -p "$project_root/backups"
release="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup="$project_root/backups/max-$release"
mkdir -m 700 "$backup"
for relative in "${files[@]}"; do
  mkdir -p "$backup/$(dirname "$relative")"
  cp -p "$project_root/$relative" "$backup/$relative"
done
printf '%s\n' "$old_image" > "$backup/old-image-id"
docker image tag "$old_image" "omnicrm:local-max-rollback-$release"
candidate="omnicrm:local-max-candidate-$release"
build_ca_file="${LOCAL_BUILD_CA_FILE:-${CODEX_PROXY_CERT:-}}"
if [ -n "$build_ca_file" ]; then
  [ -r "$build_ca_file" ] || fail 'Файл сертификата сборки недоступен.'
fi
# Revalidate the complete set before the first replacement; then recovery always
# restores all originals, including on failure midway through replacement.
for index in "${!files[@]}"; do
  [ "$(hash "$project_root/${files[$index]}")" = "$(cat "$stage/$index.original-hash")" ] || fail 'Исходники изменились во время подготовки.'
done
source_changed=1
for index in "${!files[@]}"; do
  cp "$stage/$index" "$lock/candidate"
  chmod 644 "$lock/candidate"
  mv -f "$lock/candidate" "$project_root/${files[$index]}"
done
echo 'Собираем исправленный образ. Запущенная CRM пока продолжает работать…'
if [ -n "$build_ca_file" ]; then
  docker build --secret "id=proxy_ca,src=$build_ca_file" -t "$candidate" "$project_root"
else
  docker build -t "$candidate" "$project_root"
fi
docker run --rm --network none --read-only --tmpfs /tmp --entrypoint python "$candidate" -c "import ast,pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ('app/adapters/max_personal.py','app/capabilities.py','app/api.py','app/worker.py')]"
[ "$(hash "$project_root/.env.local-docker")" = "$env_hash" ] && [ "$(hash "$project_root/compose.local.yaml")" = "$compose_hash" ] || fail 'Конфигурация изменилась во время сборки; остановили обновление.'
cutover=1
docker image tag "$candidate" omnicrm:local
"${compose[@]}" up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api worker
"${compose[@]}" exec -T api python -c "import json,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5); r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/runtime',timeout=5)); assert r.get('local_test') is True and r.get('demo') is False" </dev/null
[ "$("${compose[@]}" ps -q waha)" = "$waha_id" ] && [ "$("${compose[@]}" ps -q postgres)" = "$postgres_id" ] || fail 'Контейнер WhatsApp или PostgreSQL изменился; нужна проверка.'
[ "$("${compose[@]}" port api 8080)" = "$address" ] || fail 'Изменился порт локальной CRM.'
[ "$(hash "$project_root/.env.local-docker")" = "$env_hash" ] || fail 'Изменился файл ключей; нужна проверка.'
printf '%s\n' "$revision" > "$lock/patch-marker"
mv -f "$lock/patch-marker" "$project_root/.omnicrm-max-patch"
cutover=0
printf 'MAX_UPDATE_OK — обновление MAX установлено. WhatsApp, база и ключи сохранены.\nОткройте http://%s и подключите MAX.\nРезервная копия: %s\n' "$address" "$backup"
