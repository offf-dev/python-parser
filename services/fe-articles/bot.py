"""Два Telegram-бота: articles_bot (публичный канал) + logs_bot (админ-чат).

Если соответствующий ENV не задан — бот no-op, только пишет в лог. Это позволяет
поднять парсер локально без токенов.
"""

from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder

import config
from logging_setup import get_logger


logger = get_logger()

_articles_app = None
_articles_bot = None
_logs_app = None
_logs_bot = None


async def init_bots():
    """Поднять оба бота если включены. Вызывать ОДИН раз из async-контекста."""
    global _articles_app, _articles_bot, _logs_app, _logs_bot

    if config.ARTICLES_BOT_ENABLED:
        try:
            logger.info("Инициализация articles-бота...")
            _articles_app = ApplicationBuilder().token(config.TELEGRAM_TOKEN_ARTICLES).build()
            _articles_bot = _articles_app.bot
            await _articles_app.initialize()
            await _articles_app.start()
        except Exception as e:
            logger.error(f"Ошибка инициализации articles-бота: {e}")

    if config.LOGS_BOT_ENABLED:
        try:
            logger.info("Инициализация logs-бота...")
            _logs_app = ApplicationBuilder().token(config.TELEGRAM_TOKEN_LOGS).build()
            _logs_bot = _logs_app.bot
            await _logs_app.initialize()
            await _logs_app.start()
        except Exception as e:
            logger.error(f"Ошибка инициализации logs-бота: {e}")


async def send_articles(text: str):
    """Шлёт в публичный канал. No-op если бот выключен."""
    if not _articles_bot:
        logger.info(f"[TG-articles OFF] would send: {text[:120]}...")
        return
    try:
        await _articles_bot.send_message(
            chat_id=config.TELEGRAM_CHANNEL_ID_ARTICLES,
            text=text, parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        logger.info(f"[TG-articles ✓] sent: {text[:80]}")
    except Exception as e:
        msg = f"НЕ УДАЛОСЬ отправить в канал: {e}"
        logger.error(msg)
        await send_log(msg)


async def send_log(error_msg: str):
    """Шлёт в админ-чат. No-op если бот выключен."""
    if not _logs_bot:
        logger.info(f"[TG-logs OFF] would log: {error_msg[:200]}")
        return
    try:
        await _logs_bot.send_message(
            chat_id=config.TELEGRAM_CHANNEL_ID_LOGS,
            text=f"<b>🚨 Ошибка в парсере!</b>\n\n{error_msg}\n\nПроверьте логи для деталей.",
            parse_mode=ParseMode.HTML, disable_web_page_preview=True,
        )
        logger.info(f"[TG-logs ✓] sent: {error_msg[:80]}")
    except Exception as e:
        logger.error(f"НЕ УДАЛОСЬ отправить в logs-чат: {e}")