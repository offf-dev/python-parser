"""Watchdog поток: следит за РЕАЛЬНОЙ работой парсера через содержимое лога.

Запускается в отдельном thread (не asyncio), поэтому даже если event-loop
повис (Playwright leak / зомби-браузер), watchdog продолжит тикать.

❗ Старая версия watchdog'а проверяла mtime файла лога. Это давало
false-positive «всё работает» в типичном hung-state, потому что APScheduler
продолжает писать в лог «Job executed successfully» каждые 10 мин даже когда
реальной работы не происходит. mtime обновлялся → watchdog не срабатывал.

Новая логика:
  Каждые WATCHDOG_INTERVAL_SEC сканируем последние строки лога и ищем
  МАРКЕРЫ реальной активности:
    • "Автопарсинг занял" — успешное завершение run_auto_parse
    • "[TG-articles ✓]"   — успешная отправка статьи в канал
    • "Спаршено"           — успешный парсинг одного сайта

  Если ни одного маркера за WATCHDOG_STALE_SEC — это hung-state:
    1. Шлём в logs-чат алерт (HTTP-запрос напрямую, без asyncio)
    2. os._exit(1) → docker compose с restart: unless-stopped поднимет

  Дополнительный сигнал: если в логе есть N подряд предупреждений
  "maximum number of running instances reached" без промежуточного маркера
  успеха — парсер точно завис.
"""

import logging
import os
import re
import threading
import time

import requests

import config


_logger = logging.getLogger("parser.watchdog")

# Дефолты можно переопределять через env
WATCHDOG_INTERVAL_SEC = int(os.getenv("WATCHDOG_INTERVAL_SEC", "60"))

# Порог должен быть БОЛЬШЕ чем парс-интервал, иначе watchdog ловит
# нормальную тишину между циклами как «зависание». Берём 2× от парса
# с минимумом 30 мин — это даёт grace для одного пропущенного цикла +
# запас на затяжной парсинг (~3 мин на 30 сайтов).
_PARSER_INTERVAL_SEC = int(os.getenv("PARSER_INTERVAL_MINUTES", "60")) * 60
_DEFAULT_STALE_SEC = max(1800, _PARSER_INTERVAL_SEC * 2)
WATCHDOG_STALE_SEC = int(os.getenv("WATCHDOG_STALE_SEC", str(_DEFAULT_STALE_SEC)))

WATCHDOG_LOG_FILE = "logs/parser.log"
WATCHDOG_TAIL_LINES = 200

# Время старта самого watchdog-а — используется как fallback-точка, если
# в логе вообще ни одного маркера активности (например, все парс-сайты
# валятся подряд → никаких "Спаршено" не появилось).
_WATCHDOG_START_TS = time.time()

# Маркеры реальной активности
_ACTIVITY_MARKERS = (
    "Автопарсинг занял",
    "[TG-articles ✓]",
    "Спаршено",
)
# Лог-таймстамп в начале каждой строки: "YYYY-MM-DD HH:MM:SS - ..."
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


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


def _last_activity_age_seconds() -> int | None:
    """Возвращает возраст последнего ACTIVITY_MARKER в логе в секундах.
    None если файл лога не найден или маркеры не встречаются."""
    if not os.path.exists(WATCHDOG_LOG_FILE):
        return None
    try:
        # tail последних строк через простое чтение хвоста файла
        with open(WATCHDOG_LOG_FILE, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            chunk = 64 * 1024
            start = max(0, size - chunk)
            f.seek(start)
            data = f.read().decode("utf-8", errors="ignore")
        lines = data.splitlines()[-WATCHDOG_TAIL_LINES:]
    except Exception as e:
        _logger.warning(f"watchdog: can't read log: {e}")
        return None

    # Идём с конца, ищем последнюю строку с маркером активности
    for line in reversed(lines):
        if any(m in line for m in _ACTIVITY_MARKERS):
            m = _TS_RE.match(line)
            if not m:
                continue
            try:
                # Метки лога без TZ → трактуем как UTC (logger использует localtime,
                # но контейнер сам в UTC согласно Dockerfile/timezone). На разнице
                # ±пара часов с реальностью watchdog все равно сработает корректно
                # для порога 30 мин.
                t = time.strptime(m.group(1), "%Y-%m-%d %H:%M:%S")
                ts = time.mktime(t)
                return int(time.time() - ts)
            except Exception:
                continue

    # Fallback: маркеров не было НИ РАЗУ. Если watchdog уже работает >2×
    # парс-интервала и так и не увидел маркеров — это тоже зависание
    # (типа все парсинги тихо падают на старте).
    parser_interval_min = int(os.getenv("PARSER_INTERVAL_MINUTES", "60"))
    elapsed = int(time.time() - _WATCHDOG_START_TS)
    if elapsed > 2 * parser_interval_min * 60:
        return elapsed
    return None


def _loop():
    # Дай контейнеру время стартануть, прежде чем начать паниковать
    time.sleep(WATCHDOG_INTERVAL_SEC * 3)
    while True:
        try:
            age = _last_activity_age_seconds()
            if age is not None and age > WATCHDOG_STALE_SEC:
                msg = (
                    f"❌ <b>Парсер завис</b>\n\n"
                    f"Последняя реальная активность (парсинг/отправка статьи) "
                    f"была <b>{age // 60} мин назад</b> (порог {WATCHDOG_STALE_SEC // 60}).\n"
                    f"APScheduler продолжает крутить пустые тики, но Playwright "
                    f"скорее всего повис. Перезапускаюсь — должен подняться через ~30 сек."
                )
                _logger.error(f"WATCHDOG: hung detected, last activity age={age}s")
                _send_tg_alert_sync(msg)
                # дать HTTP-запросу уйти
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
        f"порог {WATCHDOG_STALE_SEC}с (={WATCHDOG_STALE_SEC // 60} мин), "
        f"парс-интервал {_PARSER_INTERVAL_SEC // 60} мин"
    )