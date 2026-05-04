"""Ручной запуск ОДНОГО цикла парсера. Удобно в dev/local-write режиме.

Без аргументов: парсит все active=1 сайты (как scheduler).
С аргументами: парсит ТОЛЬКО указанные site_id, игнорируя active-флаг.
Полезно для Cloudflare-protected сайтов, которые не парсятся автоматом
с прод-сервера, но успешно парсятся с локальной машины.

Usage:
    # все активные:
    docker exec python-parser-fe-articles-1 python run_once.py
    # только конкретный сайт по id (active игнорируется):
    docker exec python-parser-fe-articles-1 python run_once.py 150
    # несколько:
    docker exec python-parser-fe-articles-1 python run_once.py 150 165 168
"""

import asyncio
import sys
import time
import traceback

import bot
import config
import parser as parser_mod
import storage
from logging_setup import get_logger
from sqlalchemy import text


logger = get_logger()


async def parse_specific_ids(site_ids: list[int]):
    """Парсим только указанные ID, игнорируя active-флаг. Сохраняем как обычный run_auto_parse."""
    blocked = storage.load_blocked_keywords()
    global_known = storage.get_known_urls() if config.USE_DB_FOR_ARTICLES else set()

    # Достаём конфиг каждого из БД
    if not storage.SessionLocal:
        logger.error("БД не настроена — нечего читать")
        return
    with storage.SessionLocal() as s:
        from sqlalchemy import bindparam
        stmt = text(
            "SELECT id, site_key, site_url AS url, articles_selector, title_selector, url_selector "
            "FROM sites WHERE id IN :ids"
        ).bindparams(bindparam("ids", expanding=True))
        rows = s.execute(stmt, {"ids": site_ids}).mappings().fetchall()

    if not rows:
        logger.error(f"Не найдено sites с id ∈ {site_ids}")
        return

    total_new = 0
    for r in rows:
        res = {
            "name": r["site_key"], "url": r["url"],
            "item_selector": r["articles_selector"],
            "title_selector": r["title_selector"],
            "link_selector": r["url_selector"],
        }
        logger.info(f"=== {res['name']} (id={r['id']}) ===")
        data, err = await parser_mod.parse_resource(res, blocked_keywords=blocked)
        if err:
            logger.error(f"  err: {err}")
            continue
        new_items = [it for it in data if it["url"] not in global_known]
        logger.info(f"  спаршено {len(data)}, новых {len(new_items)}")
        if new_items:
            storage.save_new_articles(res["name"], new_items)
            total_new += len(new_items)
            for it in new_items[:3]:
                logger.info(f"    + {it['title'][:60]}")

    logger.info(f"=== Итого новых сохранено: {total_new} ===")


async def main():
    args = sys.argv[1:]
    site_ids = []
    for a in args:
        try:
            site_ids.append(int(a))
        except ValueError:
            logger.error(f"невалидный site_id: {a!r}")
            return

    logger.info("=== run_once: старт ===")
    logger.info(
        f"USE_DB_FOR_RESOURCES={config.USE_DB_FOR_RESOURCES} "
        f"USE_DB_FOR_ARTICLES={config.USE_DB_FOR_ARTICLES} "
        f"READONLY_DB={config.READONLY_DB}"
    )
    logger.info(
        f"ARTICLES_BOT_ENABLED={config.ARTICLES_BOT_ENABLED} "
        f"LOGS_BOT_ENABLED={config.LOGS_BOT_ENABLED}"
    )
    if site_ids:
        logger.info(f"Целевые ID: {site_ids} (active-флаг игнорируется)")
    else:
        logger.info("Парсим все active=1 сайты")

    await bot.init_bots()

    if site_ids:
        await parse_specific_ids(site_ids)
    else:
        await parser_mod.run_auto_parse()

    logger.info("=== run_once: завершено ===")


if __name__ == "__main__":
    asyncio.run(main())