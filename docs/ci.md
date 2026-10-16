# Проверки и выпуск архива исходников

Workflow `.github/workflows/ci.yml` запускает job `test` для push в `main` и `fix/mac-local-start`,
pull request в `main` и ручного `workflow_dispatch`. Доступ GitHub token —
`contents: read`; checkout не сохраняет credentials. Workflow не использует
секреты мессенджеров, SSH-доступ или `pull_request_target` и сам не развёртывает
приложение. PR, в том числе из fork, запускает только проверки.

## Что проверяется

1. Python 3.12 и зависимости из `requirements.lock`; весь `pytest`.
2. Настоящий PostgreSQL 16 в одноразовом CI service. Fencing-тест проверяет
   ожидание транзакции при смене воркера и отказ устаревшему владельцу.
   Также проверяются миграция 0002 → 0003, поиск названий на кириллице и
   отметка прочтения при поступлении новых сообщений.
   JUnit-отчёт дополнительно проверяется, чтобы этот тест не оказался skipped.
3. `tests/update_pilot_smoke.py`: обновление и откат с имитацией Docker,
   сохранение данных, отказ при повреждённом архиве и изменённых исходниках.
4. Node 22, Playwright 1.62.1 и Chromium: `tests/frontend_connection.cjs`,
   затем `node --test tests/frontend_history.mjs` и `tests/frontend_inbox.cjs`:
   общий список, фильтры, получатели, история, переименование и мобильный вид.
5. Сборка Docker-образа и проверка синтаксиса/импортов внутри контейнера
   с `--network none --read-only`. Провайдеры и реальные сессии не запускаются.
6. Формирование архива и проверка его SHA-256. Успешный запуск публикует artifact
   `source-<commit SHA>` с архивом и checksum-файлом на семь дней.

PostgreSQL использует отдельные тестовые credentials, действующие только внутри
одноразового runner. `FENCING_POSTGRES_URL_FILE` указывает на JSON-файл с полем
`url` и режимом `0600` в `RUNNER_TEMP`. Файл удаляется завершающим шагом даже при
ошибке тестов. Это не credentials работающего сервера, они не входят в artifact.

Ответы Telegram/WhatsApp в тестах имитируются. Зелёный CI не подтверждает
работоспособность реального аккаунта, доступность провайдера или нагрузку
300 менеджеров. Проверка боевого окружения и пилот на реальных корпоративных
аккаунтах остаются отдельными этапами.

## Локальные команды

Для Python используйте отдельное виртуальное окружение и установите
`python -m pip install -r requirements.lock`. Для полной проверки fencing
подготовьте одноразовую PostgreSQL и приватный JSON с её URL; задайте путь через
`FENCING_POSTGRES_URL_FILE`. Без этого переменного окружения PostgreSQL-тест
локально будет skipped; CI всегда предоставляет service и проверяет его запуск.

```bash
python -m pytest -q
python tests/update_pilot_smoke.py
npm ci --prefix tests/browser --ignore-scripts
tests/browser/node_modules/.bin/playwright install --with-deps chromium
export NODE_PATH="$PWD/tests/browser/node_modules"
export CHROMIUM_PATH="$(node -p 'require("./tests/browser/node_modules/playwright").chromium.executablePath()')"
node tests/frontend_connection.cjs
node --test tests/frontend_history.mjs
node tests/frontend_inbox.cjs
```

`tests/browser/package.json` и `package-lock.json` закрепляют версию Playwright
и integrity npm-пакетов. Папка `node_modules` не входит в поставку.

## Архив для обновления

```bash
python scripts/package_release.py --output-dir /tmp/omnicrm-release
```

Результаты:

- `omnicrm-server-pilot.tar.gz` — только исходники с корнем `omnicrm/`.
- `omnicrm-server-pilot.tar.gz.sha256` — совместимый с `sha256sum --check` checksum.

Packager использует явный список корневых файлов, разрешённые расширения
исходников в `app`, `web`, `scripts`, `tests` и список технических документов.
`docs/source-manifest.json` формируется заново внутри архива: SHA-256 каждого
включённого файла, кроме самого manifest. Существующий manifest в рабочем
каталоге не меняется. Сортировка, uid/gid, режимы файлов, времена tar/gzip
фиксированы; одинаковые исходники дают одинаковый архив.

Символические ссылки в исходниках отклоняются. `.github`, `.git`, `.env`,
виртуальные окружения, `node_modules`, сессии, базы, ключи, HAR, скриншоты,
резервные копии и неизвестные документы не включаются. Проверка
`tests/test_package_release.py` распаковывает настоящий архив через валидатор
работающего `update_server_pilot.sh`, а также проверяет сохранение локального
manifest, детерминированность и исключение runtime-файлов.

Перед упаковкой для автоматического обновления нужно получить чистые исходники
именно одобренного CI commit. Сам packager не проверяет историю Git и не делает
произвольный рабочий каталог доверенным. Добавляя новый тип исходника или
технический документ, явно расширьте allowlist packager и проверьте совместимость
с валидатором updater. Workflow-файлы в серверный архив не включайте.

Для автоматического обновления сервер проверяет успешный **push/main** запуск
`ci.yml` для точного SHA; зелёный PR или ручной workflow не являются разрешением
развернуть их код. Подробности: [обновление из Git](git-auto-update.md).
