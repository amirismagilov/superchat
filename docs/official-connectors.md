# Официальные HTTP-коннекторы

Дата проверки схем: 14 октября 2026 года. Код: `app/adapters/official.py`. Тесты: `tests/test_official.py`. В этой среде проверены HTTP-контракты через `httpx.MockTransport`, подписи, нормализация и ошибки; реальные ключи/номера не подключались. Эти коннекторы отправляют **текст**, WhatsApp также поддерживает одобренные шаблоны с текстовыми параметрами. Входящие медиа отражаются обозначением типа; загрузка вложений пока не реализована.

## Общая схема

Создать account через `POST /api/accounts` с `channel`, `label`, `config`, затем `POST /api/accounts/{id}/actions` с `{"action":"login","payload":{}}`. Worker проверяет credentials реальным **GET** к провайдеру. `connected` означает успешную проверку доступа, но не автоматическую настройку внешней webhook-подписки. Затем настроить callback у провайдера и проверить входящее с тестового телефона. Конфигурация хранится зашифрованно; секреты не возвращаются в account API.

Webhook endpoint: `https://crm.example.com/webhooks/{channel}/{account_id}`. `account_id` — идентификатор подключения CRM. URL не является средством авторизации: обработчик обязательно проверяет подпись/секрет и принадлежность события этому внешнему аккаунту. Подписка должна быть настроена владельцем аккаунта провайдера.

`disconnect` для этих трёх HTTP-коннекторов **останавливает подключение CRM**, но не отзывает общий токен у провайдера. Ответ содержит `remote_revocation_required:true`. Для полного отзыва открыть кабинет Meta/MAX/SMSGate и отозвать/сменить credentials; затем удалить внешние webhook-подписки. Автоматически отзывать общий токен или удалять SMS-устройство нельзя: это может отключить другие интеграции и удалить очередь устройства. Зашифрованные credentials сохраняются для явного переподключения.

## WhatsApp Cloud API

Переменная deployment: `WHATSAPP_GRAPH_VERSION=v25.0` — выбранная версия API, настраивается оператором deployment. Базовый домен фиксирован: `graph.facebook.com`; значение из account config никогда не используется как URL.

Пример `config` (все значения заменить):

```json
{
  "phone_number_id": "123456789012345",
  "access_token": "SYSTEM_USER_ACCESS_TOKEN",
  "app_secret": "META_APPLICATION_SECRET",
  "verify_token": "SEPARATE_RANDOM_WEBHOOK_VERIFY_TOKEN"
}
```

Номер в `phone_number_id` — идентификатор номера в Graph API, не сам телефон. Login проверяет `GET /{version}/{phone_number_id}?fields=id,display_phone_number,verified_name`. Нужны права Meta для чтения номера и отправки сообщений; временный тестовый token не годится для постоянной эксплуатации. [Meta Cloud API collection](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api).

В Meta настроить callback `/webhooks/whatsapp_cloud/{account_id}` и тот же `verify_token`. GET callback возвращает `hub.challenge` после проверки mode/token. Для POST используется `X-Hub-Signature-256`: `sha256=` + hex HMAC-SHA256 **исходных байтов тела** с `app_secret`. Подпись не заменяется verify token. Подписаться на messages; для Coexistence добавить доступные `history`, `smb_message_echoes`, `smb_app_state_sync`.

Одна подпись приложения может подтверждать события многих номеров. Normalizer возвращает `metadata.phone_number_id`; API должен оставлять только события нужного номера. Поддерживаются обычные сообщения, delivered/read/failed/sent, Coexistence echoes и history. State-sync контактов пока не импортируется. Исторические сообщения отмечены `historical=true`; они не должны открывать окно отправки или запускать автоматизацию нового обращения. [Coexistence events](https://docs.360dialog.com/partner/onboarding/whatsapp-coexistence/coexistence-webhooks).

Peer для нового диалога: телефон клиента, например `+79991234567`. Внешний формат нормализуется к цифрам, предусмотрен alias с `+`. Отправка: `POST /{phone_number_id}/messages`, text body. `client_id` передаётся как `biz_opaque_callback_data` для корреляции; это **не обещание идемпотентности Meta**. Повтор после неоднозначного timeout запрещён автоматически.

Свободный текст разрешается core только в течение 24 часов после нового входящего сообщения. Если окна нет и шаблон не указан, API возвращает `whatsapp_template_required_outside_24h`. **Embedded Signup и onboarding Coexistence в текущем адаптере не реализованы**: сначала настраивается подходящий Business номер вне этой формы. Клиент может первым написать на номер для открытия окна. [Правила WhatsApp](https://business.whatsapp.com/policy).

### Одобренные WhatsApp-шаблоны

`WhatsAppCloudAdapter.send_template(peer, template, client_id)` отправляет `type:template` на тот же Graph endpoint. Сначала создать шаблон в WhatsApp Manager, выбрать категорию и язык, получить approval и убедиться, что он активен. Наличие шаблона не отменяет opt-in клиента и остальные правила WhatsApp. Сам адаптер не создаёт и не одобряет шаблоны; фактическое имя, язык и число параметров проверяет Meta. [Формат Meta](https://www.postman.com/meta/whatsapp-business-platform/documentation/wlk6lh4/whatsapp-cloud-api?entity=request-13382743-bf13124f-2d24-4be7-b739-f7dbae2f325e), [схема параметров Meta](https://whatsapp.github.io/WhatsApp-Nodejs-SDK/api-reference/messages/template/).

Пример тела запроса в `POST /api/conversations/{conversation_id}/messages`:

```json
{
  "text": "Шаблон sales_followup: Анна, заказ №42",
  "client_id": "b523e977-0aa1-4fa9-b266-97376ddd4c79",
  "template": {
    "name": "sales_followup",
    "language": "ru",
    "parameters": ["Анна", "Заказ №42"]
  }
}
```

`text` — описание для истории CRM; клиент получает текст одобренного шаблона Meta с подстановками. Поддержан только простой вариант: позиционные текстовые параметры `{{1}}`, `{{2}}` в body. Статический шаблон использует пустой/отсутствующий `parameters`. Именованные параметры, media header, динамические buttons, currency/date_time пока не поддержаны.

Консервативные ограничения формы: имя `[a-z0-9_]{1,512}`, язык вида `ru`, `en_US`, `fil`; до 20 строковых параметров, каждый 1–1024 символа без управляющих символов, суммарно до 4096. Дополнительные ключи и произвольные Graph components отклоняются до сетевого запроса. Это ограничения данной реализации; они не заменяют проверку конкретного шаблона Meta. По-прежнему нельзя автоматически повторять отправку после неоднозначного timeout.

## MAX Bot API

Пример `config`:

```json
{
  "access_token": "MAX_BOT_TOKEN",
  "webhook_secret": "RANDOM_SECRET_5_TO_256_ALLOWED_CHARACTERS"
}
```

Login делает `GET https://platform-api2.max.ru/me`, проверяет `is_bot` и сохраняет `bot_id`. Токен передаётся как `Authorization: <token>` без `Bearer`. HTTPS проверяется; если выбранный TLS trust store не содержит нужную цепочку, настроить проверенный CA bundle через стандартный `SSL_CERT_FILE`/`SSL_CERT_DIR`. Не устанавливать `verify=False`. [Актуальный API MAX](https://dev.max.ru/docs-api).

Зарегистрировать через `POST /subscriptions` callback URL и `secret`, равный `webhook_secret`, с `update_types:["message_created"]`. MAX требует публичный HTTPS endpoint на 443. При callback сравнивается `X-Max-Bot-Api-Secret` в constant time. Это общий секрет, не HMAC тела. При долгой недоступности endpoint MAX может удалить подписку — мониторить и сверять её отдельно. [Подписки MAX](https://dev.max.ru/docs-api/methods/POST/subscriptions).

Peer: `user:123456` для личного диалога с ботом либо `chat:-123456` для доступного группового чата. Обычное число трактуется как user ID. Входящий личный диалог адресуется по `sender.user_id`, а не ID бота-получателя. Timestamp переводится из миллисекунд. Отправка: `POST /messages?user_id=…` или `chat_id=…`, JSON `{"text":"…"}`; успешный `message.body.mid` сохраняется как provider ID. [Сообщения](https://dev.max.ru/docs-api/methods/POST/messages).

Действуют лимиты провайдера; HTTP 429 возвращает retry_after в worker. История существующего личного аккаунта менеджера, QR-вход менеджера и изменение/удаление ранее полученного сообщения в этом адаптере не реализованы: это **бот**, отдельный канал. Для personal MAX см. `channel-feasibility.md` и экспериментальный адаптер.

## SMSGate через собственный сервер

Deployment задаёт `SMS_GATEWAY_URL=https://sms.example.com/3rdparty/v1`; также принимается alias `SMS_GATEWAY_BASE_URL`. Нельзя задавать произвольный URL из account config. `http://localhost` разрешён только вне production; остальные адреса требуют HTTPS. Этот адаптер рассчитан на **Private/Cloud server API**, а не локальный API телефона без `/devices`.

```json
{
  "username": "SMSGATE_USERNAME",
  "password": "SMSGATE_PASSWORD",
  "device_id": "REGISTERED_ANDROID_DEVICE_ID",
  "sim_number": 1,
  "signing_key": "DEVICE_WEBHOOK_SIGNING_KEY"
}
```

Создать сервер, подключить корпоративное Android-устройство, взять credentials и device ID. Login выполняет `GET /devices` с Basic Auth и проверяет наличие заданного устройства. `device_id` обязателен: без явного выбора upstream может распределять отправку между устройствами аккаунта. SIM задаётся явно, по умолчанию 1. Для разных менеджеров предпочтительны раздельные учётные записи SMSGate и отдельные signing keys. [Multi-device API](https://docs.sms-gate.app/features/multi-device/).

Отправка: `POST /messages`, JSON с `id=client_id`, `deviceId`, `simNumber`, `phoneNumbers:["+7999…"]`, `textMessage:{"text":"…"}`, `withDeliveryReport:true`. Возвращённый `id` означает принятие в очередь шлюза, не доставку SMS. Delivery обновляется отдельным событием; доступность устройства и операторский маршрут проверять отдельно. [Sending API](https://docs.sms-gate.app/features/sending-messages/).

В Android Settings → Webhooks получить Signing Key. Для каждого события (`sms:received`, `sms:sent`, `sms:delivered`, `sms:failed`, при необходимости `sms:batch:received`) зарегистрировать отдельный webhook через `POST /webhooks` с `url`, `event`, `device_id`. Подпись: hex HMAC-SHA256 от **raw body + X-Timestamp** без разделителя; ключ — signing key, заголовок — `X-Signature`. Проверяется возраст timestamp: максимум 300 секунд в обе стороны, часы серверов должны быть синхронизированы. [Webhook specification](https://docs.sms-gate.app/features/webhooks/).

Входящие используют `payload.sender`, статусы — `payload.recipient`; `phoneNumber` поддерживается как legacy fallback. `deviceId` и `simNumber` возвращаются в Event metadata: API обязан сверять их с подключением. Вложения/MMS и запуск экспорта старой истории пока не реализованы; полученная SMS-история сохраняется core, batch SMS normalizer поддержан. При мультисегментных SMS проверить, что операторский delivered отражает все части: агрегирование отдельных segment receipts в текущем адаптере не реализовано.

## Проверка реализации

```bash
python -m unittest discover -s tests -p test_official.py -v
```

20 тестов проверяют подписи по исходным байтам, replay window SMS, маршрутизационные поля, echoes/history/status callbacks, авторизацию без утечки credentials, фиксированные домены, отказ от redirects, выбор устройства/SIM, неоднозначные timeout, 429 и отправку/валидацию шаблонов. Отсутствие реальной e2e-проверки аккаунтов остаётся ограничением. Before/after production gate: реальный входящий → запись в CRM → ответ из CRM → получение клиентом → доставка/прочтение → restart/reconnect без дубликатов.
