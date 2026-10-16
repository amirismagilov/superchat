#!/usr/bin/env bash
# Reviewed inbox/history update: additive migration, private backup, app rollback.
set -euo pipefail
umask 077
revision="${1:-}"
[[ "$revision" =~ ^[a-f0-9]{40}$ ]] || { echo 'Нужен полный SHA версии.' >&2; exit 2; }
project_root="${2:-${OMNICRM_LOCAL_DIR:-$HOME/Projects/omnicrm-local}}"
fail() { printf '%s\n' "$*" >&2; exit 1; }
[ -d "$project_root" ] && [ ! -L "$project_root" ] || fail 'Нужна существующая папка локальной CRM.'
project_root="$(cd -- "$project_root" && pwd -P)"
files=(
  app/db.py app/services.py app/api.py app/worker.py app/capabilities.py
  app/adapters/base.py app/adapters/telegram.py app/adapters/whatsapp_web.py
  app/adapters/max_personal.py web/inbox.js web/styles.css scripts/migrate.py
)
# Per-file SHA256 allowlist: f4f533a, bbaf270 and f063e5d. A mixed installation
# with either prior fixed patch is supported; unknown local edits are refused.
known_hashes=(
  "caa2c01aa892dedf645832ef5b2f2ee74032912593efa1543ae356cd75267d5f"
  "bc96b7ef4c159de49732cbbaa2a0f415d05460091f5ba53c4c92f3e5c11fd7bb"
  "6b67dfbfbdf7a58ef0a03a2b7da2f2672f78150b170634634606735925f20dd4 71155f8a940056db8125047a19e2b71c74f7df96206c09015cb2111d8e3bd319"
  "57e1a62b51bc95d5673981c4a9cfe18db98c4724e1bcee57ce6d364147ba920d 3d77f7460fdc84631481a32a0479d79edfc2db940f3f3bc4bb00fca6da455f53"
  "be7ec4c32657e070fb2aedd8e4738b3eea9e7ba336c0fd82c33e045df15e8ac4 fa977a71259938734abaa7d8b35f74b52590c0e94853b8217fae03e22dd4d712"
  "60a30c1e796361eb5496a655eeecb84f18fdd8628b219527828689eb6eed8f1d"
  "0b209463681ac6897efce3ed898882cd02d4587b72ff34af5e6165b560980fe2"
  "7f1a58f1ab6e87810db8c786e3c023fd82a353a0a03b3efc39e7aa9a966d0814 9df838dc1ae9b36f150b53b88e525ce895384037c0f73fc52356eb3462cc0214"
  "06c79075a57ef17a7f48797c19e9d91b3f9180a3d8b369a467fa48656ebb98b1 68a4638cf0c3dd5f0f3ac24652184c6d817df86c5a568a75cf6fbfb4fbe06883"
  "49bc42adc0be1df1756a4debc106ca80cb69b6603f7ce3e7288ed30b57a5e57a 2a2262e0ecb9882f288c1041d48e1a1b51c8631c3e47c08c108ea3c782170025"
  "149aa12a71773e699242cd94ca514b1ebdd59f1e67069a7b91d23b3173fc2a69 4cd63f067ea358608d610e03e8838258a8f4f1d5a0e58a3025bd8156119a4264"
  "6e7bab7c1e9815ce6def9be5432e9124f39ffd432366d21fb3ed5af5d576d7b9"
)
source_paths_valid() {
  local directory relative
  for directory in app app/adapters web scripts; do
    [ -d "$project_root/$directory" ] && [ ! -L "$project_root/$directory" ] || return 1
  done
  for relative in "${files[@]}"; do
    [ -f "$project_root/$relative" ] && [ ! -L "$project_root/$relative" ] || return 1
  done
}
source_paths_valid || fail 'Исходники должны быть обычными файлами в обычных каталогах.'
for relative in .env.local-docker compose.local.yaml; do
  [ -f "$project_root/$relative" ] && [ ! -L "$project_root/$relative" ] || fail 'Конфигурация должна быть обычным файлом.'
done
[ ! -L "$project_root/.omnicrm-inbox-patch" ] || fail 'Маркер обновления не должен быть ссылкой.'
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
# Existing WhatsApp/MAX updaters use this same lock.
lock="$project_root/.omnicrm-wa-incoming-update.lock"
mkdir "$lock" 2>/dev/null || fail 'Обновление уже запущено или осталась его блокировка. Не запускайте параллельно.'
stage=''
backup=''
source_changed=0
services_stopped=0
migration_started=0
config_unchanged() {
  [ -f "$project_root/.env.local-docker" ] && [ ! -L "$project_root/.env.local-docker" ] &&
    [ -f "$project_root/compose.local.yaml" ] && [ ! -L "$project_root/compose.local.yaml" ] &&
    [ "$(hash "$project_root/.env.local-docker")" = "$env_hash" ] &&
    [ "$(hash "$project_root/compose.local.yaml")" = "$compose_hash" ]
}
cleanup() {
  result=$?
  trap - EXIT INT TERM
  recovered=1
  if [ "$result" -ne 0 ]; then
    if [ "$source_changed" = 1 ]; then
      if source_paths_valid; then
        for index in "${!files[@]}"; do
          relative="${files[$index]}"
          current_hash="$(hash "$project_root/$relative")"
          # Preserve an edit made by the user while the build was running.
          if [ "$current_hash" != "$(cat "$stage/$index.original-hash")" ] && [ "$current_hash" != "$(hash "$stage/$index")" ]; then
            printf 'Не перезаписываем изменённый во время обновления файл: %s\n' "$relative" >&2
            recovered=0
            continue
          fi
          if cp -p "$backup/$relative" "$lock/restore"; then
            mv -f "$lock/restore" "$project_root/$relative" || recovered=0
          else recovered=0; fi
        done
      else recovered=0; fi
    fi
    if [ "$services_stopped" = 1 ]; then
      if config_unchanged; then
        docker image tag "$old_image" omnicrm:local || recovered=0
        # Never run the old migration here: old apps can use additive tables,
        # while their old migration validator deliberately rejects revision 0003.
        "${compose[@]}" up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api worker || recovered=0
      else
        echo 'Конфигурация изменилась: автоматический запуск с другими ключами остановлен.' >&2
        recovered=0
      fi
    fi
    if [ "$recovered" = 1 ]; then
      echo 'Обновление не выполнено; прежние исходники и приложение сохранены/восстановлены.' >&2
    else
      echo 'Автоматическое восстановление требует проверки; не удаляйте данные и ключи.' >&2
    fi
    if [ "$migration_started" = 1 ]; then
      echo 'Схема и данные БД не откатывались. Дополнительные таблицы совместимы с прежним приложением.' >&2
    fi
    [ -z "$backup" ] || printf 'Закрытая резервная копия: %s\n' "$backup" >&2
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
compose=(env -u DATABASE_URL -u ENCRYPTION_KEY -u JWT_SECRET -u WAHA_API_KEY -u POSTGRES_PASSWORD -u SMS_GATEWAY_URL -u WAHA_IMAGE -u TELEGRAM_PROXY_URL LOCAL_PORT="$published_port" docker compose --project-name omnicrm-local --env-file "$project_root/.env.local-docker" -f "$project_root/compose.local.yaml")
env_hash="$(hash "$project_root/.env.local-docker")"
compose_hash="$(hash "$project_root/compose.local.yaml")"
stage="$(mktemp -d "${TMPDIR:-/tmp}/omnicrm-inbox-update.XXXXXXXX")"
echo 'Скачиваем зафиксированное обновление диалогов и истории…'
curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 10 --max-time 120 \
  "https://codeload.github.com/amirismagilov/superchat/tar.gz/$revision" -o "$stage/source.tar.gz"
# Read exact regular entries into numbered files. Never unpack archive paths.
for index in "${!files[@]}"; do
  relative="${files[$index]}"
  entry="superchat-$revision/$relative"
  [ "$(tar -tzf "$stage/source.tar.gz" "$entry" | wc -l | tr -d ' ')" = 1 ] || fail 'В архиве отсутствует единственный ожидаемый файл.'
  [ "$(tar -tvzf "$stage/source.tar.gz" "$entry" | cut -c1)" = '-' ] || fail 'Исходник в архиве должен быть обычным файлом.'
  tar -xOf "$stage/source.tar.gz" "$entry" > "$stage/$index"
  [ -s "$stage/$index" ] || fail 'Получен пустой исходник.'
  current_hash="$(hash "$project_root/$relative")"
  target_hash="$(hash "$stage/$index")"
  case " ${known_hashes[$index]} " in
    *" $current_hash "*) ;;
    *) [ "$current_hash" = "$target_hash" ] || fail "Файл $relative локально изменён или имеет другую версию. Обновление остановлено." ;;
  esac
  printf '%s\n' "$current_hash" > "$stage/$index.original-hash"
done
[ ! -L "$project_root/backups" ] || fail 'Каталог backups не должен быть ссылкой.'
mkdir -p "$project_root/backups"
release="$(date -u +%Y%m%dT%H%M%SZ)-$$"
backup="$project_root/backups/inbox-$release"
mkdir -m 700 "$backup"
for relative in "${files[@]}"; do
  mkdir -p "$backup/$(dirname "$relative")"
  cp -p "$project_root/$relative" "$backup/$relative"
done
printf '%s\n' "$old_image" > "$backup/old-image-id"
printf '%s\n' "$revision" > "$backup/target-revision"
docker image tag "$old_image" "omnicrm:local-inbox-rollback-$release"
candidate="omnicrm:local-inbox-candidate-$release"
build_ca_file="${LOCAL_BUILD_CA_FILE:-${CODEX_PROXY_CERT:-}}"
if [ -n "$build_ca_file" ]; then
  [ -r "$build_ca_file" ] || fail 'Файл сертификата сборки недоступен.'
fi
source_paths_valid || fail 'Пути исходников изменились во время подготовки.'
for index in "${!files[@]}"; do
  [ "$(hash "$project_root/${files[$index]}")" = "$(cat "$stage/$index.original-hash")" ] || fail 'Исходники изменились во время подготовки.'
done
source_changed=1
for index in "${!files[@]}"; do
  cp "$stage/$index" "$lock/candidate"
  chmod 644 "$lock/candidate"
  mv -f "$lock/candidate" "$project_root/${files[$index]}"
done
echo 'Собираем новый образ. Запущенная CRM пока продолжает работать…'
if [ -n "$build_ca_file" ]; then
  docker build --secret "id=proxy_ca,src=$build_ca_file" -t "$candidate" "$project_root"
else
  docker build -t "$candidate" "$project_root"
fi
docker run --rm --network none --read-only --tmpfs /tmp --entrypoint python "$candidate" -c \
  "import ast,pathlib; [ast.parse(pathlib.Path(p).read_text()) for p in ('app/db.py','app/services.py','app/api.py','app/worker.py','app/capabilities.py','app/adapters/base.py','app/adapters/telegram.py','app/adapters/whatsapp_web.py','app/adapters/max_personal.py','scripts/migrate.py')]" </dev/null
config_unchanged || fail 'Конфигурация изменилась во время сборки; остановили обновление.'
source_paths_valid || fail 'Пути исходников изменились во время сборки.'
for index in "${!files[@]}"; do
  [ "$(hash "$project_root/${files[$index]}")" = "$(hash "$stage/$index")" ] || fail 'Исходники изменились во время сборки.'
done
echo 'Кратко останавливаем только API и worker для резервной копии и миграции…'
services_stopped=1
"${compose[@]}" stop --timeout 60 api worker
config_unchanged || fail 'Конфигурация изменилась перед резервным копированием.'
# Never copy .env: the dump contains private history and encrypted account
# credentials. Access is 0600 under a 0700 directory; the Fernet key stays put.
"${compose[@]}" exec -T postgres sh -ec \
  'exec pg_dump --username="$POSTGRES_USER" --dbname="$POSTGRES_DB" --format=custom' \
  </dev/null > "$backup/database.dump.partial"
chmod 600 "$backup/database.dump.partial"
[ -s "$backup/database.dump.partial" ] || fail 'Резервная копия БД пуста; миграция не запускалась.'
"${compose[@]}" exec -T postgres pg_restore --list \
  < "$backup/database.dump.partial" > /dev/null
mv -f "$backup/database.dump.partial" "$backup/database.dump"
touch "$backup/backup-complete"
config_unchanged || fail 'Конфигурация изменилась перед миграцией.'
printf '{"services":{"migrate":{"image":"%s"}}}\n' "$candidate" > "$stage/candidate.override.json"
migration_started=1
touch "$backup/migration-requested"
# Candidate-only one-shot migration; no dependency start, image pull or ports.
"${compose[@]}" -f "$stage/candidate.override.json" run --rm --no-deps --pull never -T \
  --entrypoint python migrate scripts/migrate.py </dev/null
touch "$backup/migration-complete"
config_unchanged || fail 'Конфигурация изменилась во время миграции.'
docker image tag "$candidate" omnicrm:local
"${compose[@]}" up -d --no-deps --no-build --pull never --wait --wait-timeout 180 api worker
"${compose[@]}" exec -T api python -c "import json,urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health/ready',timeout=5); r=json.load(urllib.request.urlopen('http://127.0.0.1:8080/api/runtime',timeout=5)); assert r.get('local_test') is True and r.get('demo') is False" </dev/null
[ "$("${compose[@]}" ps -q waha)" = "$waha_id" ] && [ "$("${compose[@]}" ps -q postgres)" = "$postgres_id" ] || fail 'Контейнер WhatsApp или PostgreSQL изменился; нужна проверка.'
[ "$("${compose[@]}" port api 8080)" = "$address" ] || fail 'Изменился порт локальной CRM.'
config_unchanged || fail 'Конфигурация изменилась после запуска; нужна проверка.'
printf '%s\n' "$revision" > "$lock/patch-marker"
mv -f "$lock/patch-marker" "$project_root/.omnicrm-inbox-patch"
services_stopped=0
printf 'INBOX_UPDATE_OK — диалоги и история обновлены. Аккаунты, ключи, WhatsApp и данные сохранены.\nОткройте http://%s и обновите страницу.\nЗакрытая резервная копия: %s\n' "$address" "$backup"
