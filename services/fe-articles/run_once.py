"""Ручной запуск ОДНОГО цикла парсера. Удобно в dev (где scheduler выключен).

Что делает:
1. Поднимает Telegram-боты (если токены в env)
2. Прогоняет parser.run_auto_parse() — обходит активные ресурсы, фильтрует, нормализует
3. В READONLY_DB режиме: каждая новая статья → preview в articles-канал; bulk-summary → logs-чат

Usage:
    docker exec python-parser-fe-articles-1 python run_once.py
"""

import asyncio

import bot
import config
import parser
from logging_setup import get_logger


logger = get_logger()


async def main():
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

    await bot.init_bots()
    await parser.run_auto_parse()

    logger.info("=== run_once: завершено ===")


if __name__ == "__main__":
    asyncio.run(main())