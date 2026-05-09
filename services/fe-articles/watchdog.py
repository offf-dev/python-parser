"""Watchdog поток: следит за свежестью logs/parser.log.

Запускается в отдельном thread (не asyncio), поэтому даже если event-loop
повис (Playwright leak / зомби-браузер), watchdog продолжит тикать.

Логика:
  Каждые WATCHDOG_INTERVAL_SEC проверяем mtime файла logs/parser.log.
  Если он не обновлялся дольше WATCHDOG_STALE_SEC — это hung-state:
    1. Шлём в logs-чат алерт (HTTP-запрос напрямую, без asyncio)
    2. os._exit(1) → docker compose с restart: unless-stopped поднимет
"""

import logging
import os
import threading
import time

import requests

import config


_logger = logging.getLogger("parser.watchdog")

# Дефолты можно переопределять через env
WATCHDOG_INTERVAL_SEC = int(os.getenv("WATCHDOG_INTERVAL_SEC", "60"))
WATCHDOG_STALE_SEC = int(os.getenv("WATCHDOG_STALE_SEC", "1800"))  # 30 мин
WATCHDOG_LOG_FILE = "logs/parser.log"


def _send_tg_alert_sync(text: str):
    """Прямой HTTP в Telegram, без python-telegram-bot и asyncio."""
    if not (config.TELEGRAM_TOKEN_LOGS and config.TELEGRAM_CHANNEL_ID_LOGS):
        _logger.warning("watchdog: logs-bot не настроен, алерт не уйдёт")
        return
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{config.TELEGRAM_TOKEN_LOGS}/sendMessage",
            json={
                "chat_id": config.TELEGRAM_CHANNEL_ID_LOGS,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        _logger.info(f"watchdog: TG alert sent (HTTP {r.status_code})")
    except Exception as e:
        _logger.error(f"watchdog: TG alert failed: {e}")


def _loop():
    # Дай контейнеру время стартануть, прежде чем начать паниковать
    time.sleep(WATCHDOG_INTERVAL_SEC * 3)
    while True:
        try:
            if os.path.exists(WATCHDOG_LOG_FILE):
                age = time.time() - os.path.getmtime(WATCHDOG_LOG_FILE)
                if age > WATCHDOG_STALE_SEC:
                    msg = (
                        f"❌ <b>Парсер завис</b>\n\n"
                        f"logs/parser.log не обновлялся "
                        f"<b>{int(age // 60)} мин</b> (порог {WATCHDOG_STALE_SEC // 60}).\n"
                        f"Скорее всего повисла Playwright-сессия. "
                        f"Перезапускаюсь — должен подняться через ~30 сек."
                    )
                    _logger.error(f"WATCHDOG: hung detected, age={int(age)}s")
                    _send_tg_alert_sync(msg)
                    # На всякий — небольшой sleep чтобы HTTP-запрос успел уйти
                    time.sleep(2)
                    os._exit(1)
        except Exception as e:
            _logger.error(f"watchdog loop error: {e}")
        time.sleep(WATCHDOG_INTERVAL_SEC)


def start():
    """Запустить watchdog-поток (демон, не блокирует выход)."""
    if os.getenv("ENABLE_WATCHDOG", "true").lower() in ("false", "0", "no"):
        _logger.info("watchdog disabled by ENABLE_WATCHDOG")
        return
    t = threading.Thread(target=_loop, daemon=True, name="watchdog")
    t.start()
    _logger.info(
        f"watchdog запущен: проверка каждые {WATCHDOG_INTERVAL_SEC}с, "
        f"порог {WATCHDOG_STALE_SEC}с"
    )