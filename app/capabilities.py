import os
from .config import settings


def field(name, label, kind="text", required=True):
    return {"name": name, "label": label, "type": kind, "required": required}


def capabilities(telegram_configured=False):
    return [
        {"channel": "telegram", "label": "Telegram — аккаунт менеджера", "mode": "official_protocol", "enabled": True,
         "description": "Обычный чат от корпоративного аккаунта. Код входа и 2FA; личная облачная история. Secret Chats не поддерживаются.",
         "config_fields": [field("phone", "Рабочий номер +…", required=False)],
         "connection": {"flow": "phone_code", "configured": telegram_configured, "methods": ["phone", "qr"],
                        "unavailable_reason": None if telegram_configured else "telegram_app_not_configured"},
         "actions": ["login", "qr", "code", "password", "sync", "disconnect"]},
        {"channel": "whatsapp_cloud", "label": "WhatsApp Business Cloud API", "mode": "official", "enabled": True,
         "description": "Официальный бизнес-номер. Свободный текст в течение 24 часов после сообщения клиента. Consumer WhatsApp не поддерживается.",
         "config_fields": [field("access_token", "Access token", "password"), field("phone_number_id", "Phone number ID"), field("app_secret", "Meta app secret", "password"), field("verify_token", "Webhook verify token", "password")],
         "actions": ["login", "disconnect"]},
        {"channel": "whatsapp_web", "label": "WhatsApp — подключённое устройство", "mode": "experimental", "enabled": settings.experimental and bool(os.getenv("WAHA_URL")),
         "description": "WAHA на ваших серверах; вход по QR. Неофициальный протокол: возможны разрывы сессий и блокировки номера.",
         "config_fields": [field("history_days", "Дней истории (по умолчанию 30)", "number", required=False)],
         "connection": {"flow": "qr", "configured": settings.experimental and bool(os.getenv("WAHA_URL")), "methods": ["qr"]},
         "actions": ["login", "sync", "disconnect"]},
        {"channel": "max_personal", "label": "MAX — аккаунт менеджера", "mode": "experimental", "enabled": settings.experimental,
         "description": "Рабочий аккаунт MAX: вход по QR-коду или номеру телефона, переписка и доступная история. Неофициальная интеграция.",
         "config_fields": [field("phone", "Рабочий номер +…", required=False), field("login_method", "Метод входа: qr или sms", required=False), field("history_days", "Дней истории (по умолчанию 30)", "number", required=False)],
         "connection": {"flow": "qr", "configured": settings.experimental, "methods": ["qr", "phone"]},
         "actions": ["login", "qr", "code", "password", "sync", "disconnect"]},
        {"channel": "max_bot", "label": "MAX — бот", "mode": "official", "enabled": True,
         "description": "Клиент видит бота компании. Это не вход в личный аккаунт менеджера.",
         "config_fields": [field("access_token", "Bot token", "password"), field("webhook_secret", "Webhook secret", "password")], "actions": ["login", "disconnect"]},
        {"channel": "sms_gateway", "label": "SMS — рабочая SIM", "mode": "gateway", "enabled": bool(os.getenv("SMS_GATEWAY_URL")),
         "description": "Self-hosted SMSGate и корпоративная SIM. Двусторонняя переписка с рабочего мобильного номера.",
         "config_fields": [field("username", "Gateway username"), field("password", "Gateway password", "password"), field("device_id", "Device ID"), field("signing_key", "Webhook signing key", "password")], "actions": ["login", "disconnect"]},
        {"channel": "demo", "label": "Демонстрационный канал", "mode": "demo", "enabled": settings.demo,
         "description": "Локальная проверка интерфейса и очереди; сообщения не уходят во внешние системы.", "config_fields": [], "actions": ["login", "disconnect"]},
    ]


def adapter_for(account, config):
    if account.channel == "demo" and settings.demo:
        from .adapters.demo import DemoAdapter
        return DemoAdapter(config)
    if account.channel == "telegram":
        from .adapters.telegram import TelegramAdapter
        return TelegramAdapter(config)
    if account.channel in {"whatsapp_cloud", "max_bot", "sms_gateway"}:
        from .adapters.official import WhatsAppCloudAdapter, MaxBotAdapter, SMSGatewayAdapter
        return {"whatsapp_cloud": WhatsAppCloudAdapter, "max_bot": MaxBotAdapter, "sms_gateway": SMSGatewayAdapter}[account.channel](config)
    if settings.experimental and account.channel == "whatsapp_web":
        from .adapters.whatsapp_web import WhatsAppWebAdapter
        return WhatsAppWebAdapter(config)
    if settings.experimental and account.channel == "max_personal":
        from .adapters.max_personal import MaxPersonalAdapter
        return MaxPersonalAdapter(config)
    from .adapters.base import ProviderError
    raise ProviderError("channel_disabled")
