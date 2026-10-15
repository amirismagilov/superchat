# Запуск и эксплуатация

Поставляемая конфигурация включает API, транспортный worker, PostgreSQL 16 и необязательный WAHA. Это основа для контролируемого развёртывания и приёмки, **не подтверждение готовности обслуживать 300 реальных менеджеров**. До ввода нужны собственные номера/аккаунты, доступ к API провайдеров, CRM JWT и испытания ниже. Реальные подключения и гарантированная доставляемость в этой среде не проверялись.

## Быстрый локальный запуск

Для проверки своих аккаунтов на macOS/Linux подготовлен отдельный Docker-стенд: `bash scripts/local_start.sh` или `Start.command`. Он использует PostgreSQL, постоянные секреты `.env.local-docker` и явный `LOCAL_TEST_MODE=true`, без демо-аккаунтов. См. [пошаговую инструкцию](local-accounts.md). Ниже сохранён прежний Python/SQLite вариант для демонстрации интерфейса.

Нужны Python 3.12+ и Bash на Linux/macOS. Из каталога проекта:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.lock
python3 scripts/init_local.py
./scripts/run-local.sh
```

Открыть [локальный inbox](http://127.0.0.1:8080). Скрипт создаёт `.env.local` с уникальными ключами и правами `0600`, не выводит секреты и никогда не перезаписывает существующий файл. Локальный режим использует SQLite, ровно один worker и демонстрационный канал; сообщения этого канала не отправляются во внешние мессенджеры. API слушает только loopback. `Ctrl+C` завершает оба процесса.

Чтобы локально проверять реальные каналы, сохранить `APP_ENV=development`, выставить `DEMO_MODE=false` в `.env.local` и передавать подписанный тестовым CRM токен. `/api/dev/token` тогда отключится. Включение демонстрации нельзя использовать для публичного сервера: этот endpoint выдаёт локальную административную identity. Экспериментальные каналы дополнительно требуют `ENABLE_EXPERIMENTAL=true`.

Повторный запуск сохраняет историю в `omnicrm-demo.db`. Не удалять `.env.local`, если требуется доступ к зашифрованным сессиям в существующей БД: вновь сгенерированный Fernet key их не расшифрует. SQLite здесь — удобный локальный режим, не конфигурация на 300 сотрудников.

## Подготовка production-конфигурации

Используется Docker Compose v2. Создать закрытый файл `.env` рядом с `compose.yaml`. Его не нужно копировать в образ; `.gitignore` и `.dockerignore` исключают `.env*`. Для первого запуска можно сгенерировать индивидуальные секреты следующим фрагментом: он отказывается перезаписывать существующий файл и ничего секретного не печатает.

```bash
python3 - <<'PY'
import base64, os, secrets
password = secrets.token_urlsafe(36)
values = {
    'POSTGRES_DB': 'omnicrm',
    'POSTGRES_USER': 'omnicrm',
    'POSTGRES_PASSWORD': password,
    'DATABASE_URL': f'postgresql+psycopg://omnicrm:{password}@postgres:5432/omnicrm',
    'ENCRYPTION_KEY': base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    'JWT_SECRET': secrets.token_urlsafe(48),
    'JWT_JWKS_URL': '',
    'JWT_ISSUER': 'https://crm.example.test',
    'JWT_AUDIENCE': 'omnicrm',
    'ENABLE_EXPERIMENTAL': 'false',
    'WAHA_API_KEY': secrets.token_urlsafe(36),
    'WAHA_URL': '',
    'SMS_GATEWAY_URL': '',
}
fd = os.open('.env', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with os.fdopen(fd, 'w') as output:
    output.write('\n'.join(f'{key}={value}' for key, value in values.items()) + '\n')
print('Created private .env; configure the real CRM issuer before starting.')
PY
```

Затем настроить CRM:

| Переменная | Требование |
|---|---|
| `DATABASE_URL` | PostgreSQL через `postgresql+psycopg://`; имя хоста `postgres` для этой Compose-сети. Пароль должен совпадать с `POSTGRES_PASSWORD`. Для внешней БД использовать её защищённый адрес и TLS-настройки |
| `ENCRYPTION_KEY` | Fernet key, одинаковый у API и всех worker. Хранить отдельно от резервной копии БД |
| `JWT_JWKS_URL` | Предпочтительный вариант: HTTPS JWKS корпоративного issuer. В этой реализации проверяется `RS256` |
| `JWT_SECRET` | Альтернатива JWKS: случайный HS256 secret не короче 32 символов, общий только для CRM backend и сервиса; никогда не передавать в браузер |
| `JWT_ISSUER`, `JWT_AUDIENCE` | Точные значения `iss` и `aud`, которые выпускает CRM. `https://crm.example.test` из генератора заменить реальным issuer |
| `ENABLE_EXPERIMENTAL` | По умолчанию `false`. Включает допуск к неофициальным адаптерам; само по себе не подключает аккаунты |
| `WAHA_URL`, `WAHA_API_KEY` | Адрес развёрнутого администратором WAHA и ключ. Для профиля ниже: `http://waha:3000`. Клиенты API не выбирают произвольный адрес шлюза |
| `SMS_GATEWAY_URL` | Доверенный адрес вашего SMS Gateway. Входящие SMS требуют доступного callback/webhook endpoint; локального номера без шлюза недостаточно |

Если задан JWKS, он имеет приоритет над HS256. Токен должен содержать `sub`, `tenant_id`, `role` (`agent` либо `admin`), `iat`, `exp`, `iss`, `aud`. CRM должна формировать эти claims из своих прав, а не копировать значения из пользовательского запроса. Браузеру передаётся только короткоживущий Bearer JWT через свойство компонента; ключ подписи остаётся на сервере. В production нет локальной формы создания пользователей.

`APP_ENV=production` и `DEMO_MODE=false` заданы непосредственно в Compose. Отсутствие ключа шифрования или JWT-конфигурации приводит к отказу старта. PostgreSQL и WAHA не публикуют порты на хосте; API публикует только `127.0.0.1:8080`. Для работы сотрудников и provider webhooks нужен HTTPS reverse proxy на вашем домене. Установить лимиты размера/скорости запросов и не включать логирование Authorization, login codes, 2FA password или содержимого webhook. Сам reverse proxy и корпоративный issuer в эту поставку не входят.

## Сборка и первый запуск

```bash
docker compose config --quiet
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8080/health/ready
```

`config --quiet` проверяет конфигурацию, не печатая подставленные секреты. Не отправлять результат обычного `docker compose config` в чат/лог: он может содержать значения `.env`. Открытый UI в production требует CRM JWT; доступность страницы сама по себе не означает, что пользователь авторизован.

В managed cloud build с собственным CA сначала собрать образ явно, затем запустить Compose без повторной сборки:

```bash
docker build --secret id=proxy_ca,src=/etc/ssl/certs/ca-certificates.crt -t omnicrm:local .
docker compose up -d --no-build
```

BuildKit secret используется только на шаге `pip install`; session CA не сохраняется в образе. Для последующих внешних HTTPS-вызовов в среде с инспектирующим proxy также требуется доверие CA внутри запущенного контейнера. Сохранять предоставленные proxy-настройки и TLS verification. Эта особенность среды не нужна на обычном сервере с публичными доверенными сертификатами.

API/worker работают как UID `10001`, без Linux capabilities, с read-only filesystem и временным `/tmp`. Секреты не встроены в образ, но администратор Docker-хоста может прочитать окружение контейнера: доступ к Docker daemon равнозначен доступу к ним. Для штатной инфраструктуры передавать переменные из secret manager и ограничить доступ операторов.

Приложение устанавливается по `requirements.lock`; это полный lock установленной среды, включая экспериментальную библиотеку MAX, которая остаётся выключенной настройками. Теги `python:3.12-slim-bookworm` и `postgres:16-bookworm` фиксируют семейство, но не immutable build. После проверки выбранной сборки закрепить image digest в registry/production release manifest; не выдавать плавающий тег за воспроизводимый бинарный артефакт.

## Инициализация и изменение схемы

Сервис `migrate` запускает `python scripts/migrate.py` после готовности PostgreSQL. API и worker стартуют только после его успешного завершения. На пустой БД создаются таблицы SQLAlchemy и запись `0001_initial` с fingerprint модели. Повторный запуск проверяет эту запись, состав колонок и наличие ожидаемых индексов. Инициализаторы сериализуются PostgreSQL advisory lock.

**Это создание начальной схемы, не система обновления схемы.** `create_all()` не выполняет `ALTER TABLE`. Скрипт отказывается принимать непустую БД без собственной revision-записи и отказывается работать после изменения fingerprint модели. Для следующей версии следует добавить явные проверенные миграции, например Alembic, включая перенос данных и совместимость rolling deploy. Нельзя «починить» ошибку удалением revision-таблицы или руками заменить fingerprint. Проверка структуры скриптом также не заменяет полный аудит DB drift.

SQLite-демо создаёт таблицы отдельно и не является автоматически мигрируемой production-БД. Перенос тестовых данных в production — отдельная задача; файл demo DB нельзя просто подложить PostgreSQL.

## Необязательный WhatsApp linked-device шлюз

Для экспериментального пилота в `.env` установить `ENABLE_EXPERIMENTAL=true`, `WAHA_URL=http://waha:3000` и непустой случайный `WAHA_API_KEY`. Затем:

```bash
docker compose --profile waha up -d --build
docker compose --profile waha ps
```

Используется [WAHA 2026.9.2](https://github.com/devlikeapro/waha/releases/tag/2026.9.2), engine `NOWEB`. Docker Registry публикует engine-prefixed теги: голого `devlikeapro/waha:2026.9.2` нет. Compose по умолчанию фиксирует `devlikeapro/waha:noweb-2026.9.2@sha256:be61f8cd7774ab22bd1b755cbc539f98d71d81d5ca83555fdddfa4fddd7924e1` для `amd64`; для `arm64` задать `WAHA_IMAGE=devlikeapro/waha:noweb-arm-2026.9.2@sha256:c7fba52db50312c8d0d05536f11ef136ac851c6b2baa11a883ea2d3d6d877f82`. Оба manifest digest и архитектуры проверены в Docker Registry 2026-10-14. Его основной процесс работает как UID `1000`; отдельный одноразовый `waha-volume-init` использует root только для владения двумя named volumes и не имеет сети. Dashboard/Swagger и печать QR в лог выключены; QR для входа должен приходить через защищённый API CRM. WAHA без API key откажется стартовать.

На 2026-10-14 проверен **именно исходный тег `2026.9.2`**, commit [`90ba445a0e765701c8910f0fac08176250c3e0fd`](https://github.com/devlikeapro/waha/commit/90ba445a0e765701c8910f0fac08176250c3e0fd), а не только текущая ветка upstream. Контракт NOWEB в этом теге:

| Возможность адаптера | Подтверждение в исходниках зафиксированной версии |
|---|---|
| Пользовательский ID при `POST /api/sendText` | [`getMessageOptions`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/engines/noweb/session.noweb.core.ts#L3708) передаёт `request.id` как `messageId` в Baileys. Само наличие этого поля не доказывает exactly-once доставку |
| Ответ отправки и последующее чтение истории | [`sendText`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/engines/noweb/session.noweb.core.ts#L1122) возвращает raw Baileys message с `key`; история нормализуется через [`toWAMessage`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/engines/noweb/session.noweb.core.ts#L3449). ID для их сопоставления строится по [`buildMessageId`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/engines/noweb/utils.ts#L10), включая `fromMe`, нормализованный remote JID, ID и необязательный participant |
| Разрешение LID в телефон | [`GET /api/:session/lids/:lid`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/api/lids.controller.ts#L74) возвращает `pn` либо `null` |
| Ограниченные окна истории | [`SqlMessagesMethods`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/engines/noweb/store/sql/SqlMessagesMethods.ts#L20) применяет включительные `filter.timestamp.gte/lte` и pagination. Повторы на границе окна должны устраняться ID сообщения |
| Хранение и первоначальная синхронизация | [`NowebStoreConfig`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/structures/sessions.dto.ts#L82) содержит `enabled/fullSync`, а [`ensureStore`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/engines/noweb/session.noweb.core.ts#L480) создаёт отдельное persistent store для имени сессии |
| Несколько именованных NOWEB-сессий | [`SessionManagerCore.start`](https://github.com/devlikeapro/waha/blob/90ba445a0e765701c8910f0fac08176250c3e0fd/src/core/manager.core.ts#L329) создаёт engine по имени и хранит его в `sessions[name]`. [FAQ проекта](https://waha.devlike.pro/docs/overview/faq/) указывает, что с `2026.6.1` прежнее разделение Core/Plus снято и сессии не ограничиваются лицензией. Это не измерение ёмкости одного контейнера |

Проверка исходников и registry manifest не заменяет запуск образа с живым аккаунтом. Перед production сверить версию работающего WAHA с принятой сборкой и выполнить live contract tests. Особенно проверять совпадение ID ответа отправки и эха в истории, LID/phone mapping, страницы с одинаковыми timestamp и повторный вход.

Сессии и файлы находятся в `waha-sessions` и `waha-media`. Это чувствительные данные авторизации; Fernet шифрование основной БД на эти volumes не распространяется. Нужны шифрование дисков/backup, контроль доступа и собственный restore-тест. Лицензии и ограничения приведены в [оценке open source](opensource-assessment.md). Эта конфигурация не даёт SLA WhatsApp и не гарантирует перенос всей старой истории.

В этой поставке один адрес WAHA — одна группа аккаунтов. `docker compose --scale waha=N` не реализует корректную маршрутизацию сессий. Для нескольких WAHA-инстансов нужен registry `account → instance`, отдельное размещение их session storage и соответствующая доработка адаптера. Это остаётся задачей до масштабного запуска.

## Worker ownership и масштабирование

`WORKER_SHARD_COUNT=16` задаёт стабильное распределение аккаунтов по hash UUID. `WORKER_SHARDS` — список номеров, которые процесс может захватить. PostgreSQL session advisory locks допускают одного владельца каждого shard; при потере соединения владения worker должен остановиться. Все worker используют одинаковую БД, Fernet key и число shard. SQLite разрешает только один worker через локальную блокировку файла.

Дополнительно `ShardLease` хранит уникальную `epoch` каждого захвата shard. Перед изменением состояния worker блокирует строку lease до конца транзакции и сверяет свою epoch. Новый владелец меняет epoch после получения advisory lock: старый процесс не сможет записать результат после вступления нового владельца в права. Потеря ownership latch необратима для процесса; прозрачное переподключение с продолжением работы запрещено. Соединение владения должно идти напрямую в PostgreSQL либо через **session pooling**; PgBouncer transaction pooling для session advisory locks не подходит.

Это fencing записей в нашей БД. Он не отзывает уже ушедший внешний HTTP/MTProto запрос и не отменяет сообщение, которое провайдер успел принять. После такого разрыва состояние `sending` восстанавливается как `uncertain`; требуется сверка с историей, а не автоматическая повторная отправка.

По умолчанию процесс пробует все 16 shard. **Первый запущенный worker может захватить их все; `--scale worker=3` не является балансировкой нагрузки.** Для распределения назначить непересекающиеся списки, например `0,1,2,3,4,5,6,7` и `8,9,10,11,12,13,14,15`. Пример отдельного файла `compose.workers.yaml`:

```yaml
services:
  worker:
    environment:
      WORKER_SHARDS: "0,1,2,3,4,5,6,7"
  worker_b:
    extends:
      file: compose.yaml
      service: worker
    environment:
      WORKER_SHARDS: "8,9,10,11,12,13,14,15"
```

```bash
docker compose -f compose.yaml -f compose.workers.yaml up -d --build
```

Это пример распределения, не автоматического HA: при непересекающихся списках оставшийся worker не заберёт чужие shard. Для резервирования запускать дополнительный процесс с тем же списком либо отдельно строить scheduler. При перераспределении сначала корректно остановить старых владельцев. Не менять число shard при работающих старых worker.

Плановая нагрузка: 300 менеджеров × 3 мессенджера может означать 900 сессий, а не 300. Для 300 отправок в минуту средний поток — 5 сообщений/с; это **допущение**, которое нужно заменить реальной статистикой. Пиковые 50 сообщений/с, объём импорта истории и вложения — отдельные профили. PostgreSQL pool сейчас создаётся отдельно каждым процессом; учитывать суммарное число соединений при добавлении реплик. Один контейнер WAHA и один PostgreSQL из Compose не образуют отказоустойчивую систему на несколько узлов.

## Наблюдаемость и обработка сбоев

| Сигнал | Что подтверждает | Чего не подтверждает |
|---|---|---|
| `/health/live` | API-процесс отвечает | БД, schema и подключения мессенджеров |
| `/health/ready` | API может сделать `SELECT 1` в БД | Наличие всех таблиц, отсутствие backlog, доступность провайдеров |
| Worker heartbeat | Основной цикл недавно обновил файл | Доставляемость, состояние каждого аккаунта и скорость обработки |
| WAHA healthcheck | Авторизованный запрос списка сессий возвращает успешный HTTP | Что все пользовательские сессии подключены |

Docker healthcheck сам по себе не перезапускает unhealthy контейнер: `restart: unless-stopped` реагирует на выход процесса. Нужен внешний монитор/оркестратор и алерты. До production добавить метрики возраста самого старого queued сообщения, размера очереди, ошибок/таймаутов, `reauth_required`, `uncertain`, задержки входящих, заполнения диска и возраста резервной копии.

Восстановление после неоднозначной отправки отличается от обычного retry. Если внешний сервис мог принять сообщение, а подтверждение потерялось, состояние `uncertain` требует сверки истории/идентификатора и решения оператора. Нельзя массово менять такие сообщения обратно на `queued`: это может продублировать клиентские сообщения. При отзыве пользовательской сессии менеджер проходит повторную авторизацию, а не получает ложный статус «подключено».

## Резервные копии и восстановление

БД содержит историю сообщений и зашифрованные настройки аккаунтов. Тела сообщений в этой реализации не шифруются Fernet отдельно; защищать PostgreSQL storage, backup и каналы доступа средствами инфраструктуры. Fernet key хранить в отдельном secret manager с контролируемым backup. Потеря ключа делает зашифрованные сессии невосстановимыми.

Пример логической резервной копии PostgreSQL:

```bash
umask 077
mkdir -p backups
backup_file="backups/omnicrm-$(date -u +%Y%m%dT%H%M%SZ).dump"
docker compose exec -T postgres sh -c 'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_file"
```

Для производственной RPO меньше интервала дампа нужны WAL-архивирование/PITR или managed PostgreSQL backup. Named volume сам по себе не резервная копия. `docker compose down -v` удаляет данные; обычный stop/down без `-v` сохраняет volumes.

Restore-тест проводить в отдельном Compose project и новой БД, без запуска worker и без выхода к реальным провайдерам, пока не исключена повторная отправка. Подготовить отдельный `.env.restore` с ключом из backup и параметрами изолированной БД. Пример:

```bash
docker compose -p omnicrm-restore --env-file .env.restore up -d postgres
docker compose -p omnicrm-restore --env-file .env.restore exec -T postgres sh -c 'exec pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < backups/selected.dump
docker compose -p omnicrm-restore --env-file .env.restore run --rm migrate
```

После восстановления проверить количества диалогов/сообщений, чтение истории, расшифровку настроек и tenant isolation. Только затем решать, какие provider sessions можно включать. WAHA volumes копировать согласованно после остановки отправляющих worker и WAHA либо средствами snapshot с подтверждённой консистентностью. Фактическую пригодность восстановленной WhatsApp-сессии проверять отдельно: провайдер мог отозвать её после создания backup.

Ротация Fernet key не автоматизирована. Простая замена переменной ломает чтение старых записей. Нужен отдельный проверенный re-encryption процесс с резервной копией и переходным чтением старого/нового ключа. Планировать его до обязательной политики регулярной ротации.

## Приёмка перед запуском 300 сотрудников

Следующие результаты пока **не доказаны** этой поставкой и должны иметь ответственного и протокол:

1. Live login, send, receive и history на собственных тестовых номерах для каждого включаемого адаптера; проверка отзыва сессии, 2FA и повторного входа.
2. Длительный пилот 5–10, затем 30–50 пользователей с реальной сетью и ограничениями провайдера. Для experimental каналов — подтверждённый резервный способ продолжить разговор.
3. Изоляция разных tenants/менеджеров, смена владельца и отзыв доступа при увольнении; проверка trusted JWT issuer и публичного HTTPS ingress.
4. Нагрузочный тест на ожидаемую историю и пик отправки, при 300 клиентах UI и соответствующем числе мессенджер-сессий. Цели приёмки согласовать отдельно, например p95 чтения истории <500 мс и нормальный возраст локальной очереди <5 с без provider rate limit.
5. Fault injection: падение worker во время отправки, потеря БД, повтор webhook, рестарт сессии, заполнение диска; отсутствие бесконтрольных дублей и корректное `uncertain`.
6. Restore drill с измеренными RPO/RTO, сохранностью encryption key и доказательством, что резервный контур не отправляет сообщения клиентам.
7. Реальные алерты, эксплуатационный владелец, план обновлений API/SDK и подписанный release manifest с image digests, lockfile и лицензиями.

Перечень фиксирует границу готовности, а не заменяет выполненные проверки. Результаты локальных/контрактных тестов основной поставки следует читать отдельно в README; они не равнозначны испытаниям внешних мессенджеров или нагрузке 300 сотрудников.
