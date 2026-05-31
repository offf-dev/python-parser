"""Watchdog поток: следит за РЕАЛЬНОЙ работой парсера через heartbeat-файл.

Запускается в отдельном thread (не asyncio), поэтому даже если event-loop
повис (Playwright leak / зомби-браузер), watchdog продолжит тикать.

❗ История подхода:
  v1 — mtime parser.log: false-positive «всё работает», APScheduler
       продолжал писать "Job executed successfully" каждые 10 мин в hung-state.
  v2 — grep маркеров активности в хвосте parser.log: убил false-positive,
       но имел свои баги — окно хвоста (4MB) не всегда хватало при error-спаме,
       а TimedRotatingFileHandler в полночь создавал свежий пустой parser.log
       → 53 секунды спустя watchdog говорил «маркеров нет» → ложный рестарт.
  v3 (текущий) — heartbeat sidecar файл. Парсер пишет туда epoch на каждом
       успешном Спаршено / Автопарсинг занял / TG-articles ✓. Watchdog
       просто читает timestamp. Никаких регулярок, ротаций, TZ-предположений.

  Если heartbeat старее WATCHDOG_STALE_SEC — это hung-state:
    1. Шлём в logs-чат алерт (HTTP-запрос напрямую, без asyncio)
    2. os._exit(1) → docker compose с restart: unless-stopped поднимет
"""

import logging
import os
import threading
import time

import requests

import config
import heartbeat


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

# Время старта самого watchdog-а — используется как fallback-точка, если
# heartbeat-файла ещё не существует (свежий контейнер, парсер ни разу не
# успел `touch()` или volume пустой).
_WATCHDOG_START_TS = time.time()


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


def _last_activity_age_seconds() -> tuple[int, str] | None:
    """Возвращает (age_seconds, source).

    `source` — 'heartbeat' если возраст из sidecar-файла, 'fallback' если
    файла нет и watchdog работает уже дольше порога (на свежем контейнере
    парсер ещё ни разу не успел touch()-нуть).

    None если heartbeat нет и времени с старта тоже прошло мало —
    обычное состояние первых пары минут после старта.
    """
    age = heartbeat.age_seconds()
    if age is not None:
        return age, "heartbeat"

    # Файла нет / битый. Если watchdog работает дольше порога — это уже
    # подозрительно: парсер ни разу не успел отчитаться.
    elapsed = int(time.time() - _WATCHDOG_START_TS)
    if elapsed > WATCHDOG_STALE_SEC:
        return elapsed, "fallback"
    return None


def _loop():
    # Дай контейнеру время стартануть, прежде чем начать паниковать
    time.sleep(WATCHDOG_INTERVAL_SEC * 3)
    while True:
        try:
            res = _last_activity_age_seconds()
            if res is not None and res[0] > WATCHDOG_STALE_SEC:
                age, source = res
                if source == "heartbeat":
                    detail = f"Последняя успешная активность <b>{age // 60} мин назад</b>"
                else:
                    detail = (
                        f"Heartbeat-файл отсутствует; watchdog работает уже "
                        f"<b>{age // 60} мин</b>, парсер ни разу не отчитался"
                    )
                msg = (
                    f"❌ <b>Парсер завис</b>\n\n"
                    f"{detail} (порог {WATCHDOG_STALE_SEC // 60} мин).\n"
                    f"APScheduler продолжает крутить пустые тики, но Playwright "
                    f"скорее всего повис. Перезапускаюсь — должен подняться через ~30 сек."
                )
                _logger.error(
                    f"WATCHDOG: hung detected, age={age}s (source={source})"
                )
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