"""Все ENV-driven настройки и константы парсера в одном месте."""

import os


def _int_or_none(name: str):
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return None


# ====================== Telegram ======================
TELEGRAM_TOKEN_ARTICLES = os.getenv("TG_BOT_TOKEN_FOR_ARTICLES") or None
TELEGRAM_CHANNEL_ID_ARTICLES = _int_or_none("TG_CHAT_ID_FOR_ARTICLES")

TELEGRAM_TOKEN_LOGS = os.getenv("TG_BOT_TOKEN_FOR_LOGS") or None
TELEGRAM_CHANNEL_ID_LOGS = _int_or_none("TG_CHAT_ID_FOR_LOGS")

ARTICLES_BOT_ENABLED = bool(TELEGRAM_TOKEN_ARTICLES and TELEGRAM_CHANNEL_ID_ARTICLES)
LOGS_BOT_ENABLED = bool(TELEGRAM_TOKEN_LOGS and TELEGRAM_CHANNEL_ID_LOGS)

# ====================== Расписание ======================
PARSER_INTERVAL_MINUTES = int(os.getenv("PARSER_INTERVAL_MINUTES", 10))
SENDER_INTERVAL_MINUTES = int(os.getenv("SENDER_INTERVAL_MINUTES", 10))
ENABLE_SCHEDULER = os.getenv("ENABLE_SCHEDULER", "true").lower() not in ("false", "0", "no")

# ====================== Файлы ======================
DATA_FILE = "data/resources.json"
LAST_RESULTS_FILE = "data/last_results.json"

# ====================== БД ======================
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_DATABASE = os.getenv("DB_DATABASE")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_USE_SSL = os.getenv("DB_USE_SSL", "false").lower() == "true"

USE_DB_FOR_RESOURCES = os.getenv("USE_DB_FOR_RESOURCES", "false").lower() == "true"
USE_DB_FOR_ARTICLES = os.getenv("USE_DB_FOR_ARTICLES", "false").lower() == "true"

# READONLY_DB: читать из БД можно, писать (INSERT/UPDATE/DELETE) — нельзя.
# Удобно для теста — в READONLY режиме новые статьи отправляются в TG-канал
# как "🧪 PREVIEW" вместо сохранения в links; toggle/delete в /sites и /links
# отвечают success без эффекта.
READONLY_DB = os.getenv("READONLY_DB", "false").lower() in ("true", "1", "yes")

# ====================== HTTP / парсинг ======================
PER_PAGE_OPTIONS = [20, 50, 100]
DEFAULT_PER_PAGE = 20
PARSE_LIMIT = 20
PAGE_GOTO_TIMEOUT_MS = 180_000
PAGE_GOTO_RETRIES = 5

# ====================== Web ======================
PORT = int(os.getenv("PORT", 5000))
APP_ENV = os.getenv("APP_ENV", "production").lower()


def is_dev_mode() -> bool:
    if APP_ENV != "development":
        return False
    return os.getenv("ENABLE_RELOADER", "true").lower() not in ("false", "0", "no")