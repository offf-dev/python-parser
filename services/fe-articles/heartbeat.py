"""Heartbeat-sidecar для watchdog'а.

Парсер `touch`-ает файл на каждом маркере успешной активности (Спаршено,
Автопарсинг занял, TG-articles ✓). Watchdog читает timestamp — без регулярок,
без зависимости от ротации parser.log и TZ logger'а.

Старый подход через grep лога сломался когда TimedRotatingFileHandler в
полночь UTC создавал свежий пустой parser.log → watchdog моментально
говорил «маркеров нет» → fallback-перезапуск исправного парсера.
"""

import os
import time

HEARTBEAT_FILE = "logs/.heartbeat"


def touch() -> None:
    """Best-effort обновление heartbeat. Никогда не падает наверх — парсер
    не должен ломаться из-за проблем со sidecar-файлом."""
    try:
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, "w") as f:
            f.write(str(int(time.time())))
        os.replace(tmp, HEARTBEAT_FILE)
    except Exception:
        pass


def age_seconds() -> int | None:
    """Возраст последнего touch() в секундах, или None если файла нет /
    содержимое битое."""
    try:
        with open(HEARTBEAT_FILE, "r") as f:
            ts = int(f.read().strip())
        return int(time.time() - ts)
    except Exception:
        return None
