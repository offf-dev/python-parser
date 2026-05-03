"""APScheduler: парс-job + send-job + trickle-отправка одной старой статьи."""

import asyncio
import traceback
from datetime import datetime, timedelta

from apscheduler.events import EVENT_JOB_ERROR
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import text
from telegram.constants import ParseMode

import bot
import config
import parser
import storage
from logging_setup import get_logger


logger = get_logger()
scheduler = AsyncIOScheduler()
_send_lock = asyncio.Lock()


def _job_error_listener(event):
    if event.exception:
        msg = f"Ошибка в job {event.job_id}: {event.exception}\n{event.traceback}"
        logger.error(msg)
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(bot.send_log(msg))
        except RuntimeError:
            pass


scheduler.add_listener(_job_error_listener, EVENT_JOB_ERROR)


async def send_oldest_unsent_article():
    """Берёт самую старую is_send=0 статью и публикует её."""
    async with _send_lock:
        try:
            if config.USE_DB_FOR_ARTICLES and storage.SessionLocal:
                with storage.SessionLocal() as session:
                    article = session.execute(text("""
                        SELECT l.id, l.title, l.url, l.created_at, s.emoji
                        FROM links l
                        LEFT JOIN sites s ON l.site_id = s.id
                        WHERE l.is_send = 0
                        ORDER BY l.created_at ASC, l.id ASC
                        LIMIT 1
                    """)).mappings().first()
                    if not article:
                        return
                    msg = bot.format_article(article["title"], article["url"], article.get("emoji"))
                    await bot.send_articles(msg)
                    if config.READONLY_DB:
                        logger.info(f"[READONLY] would mark is_send=1 для id={article['id']}")
                    else:
                        session.execute(
                            text("UPDATE links SET is_send = 1, updated_at = NOW() WHERE id = :id"),
                            {"id": article["id"]},
                        )
                        session.commit()
                    logger.info(f"Отправлена статья: {article['title'][:80]}")
                return

            # JSON-режим
            last = storage.load_last_results()
            target_site = None
            target_idx = None
            target_art = None
            for site_name, articles in last.items():
                for idx, art in enumerate(articles):
                    if not art.get("is_send"):
                        if target_art is None or art.get("parsed_at", "") < target_art.get("parsed_at", ""):
                            target_site, target_idx, target_art = site_name, idx, art
            if target_art is None:
                return
            # Найти emoji для site_name среди ресурсов JSON
            emoji = None
            for r in storage.load_resources():
                if r.get("name") == target_site:
                    emoji = r.get("emoji")
                    break
            await bot.send_articles(bot.format_article(target_art["title"], target_art["url"], emoji))
            last[target_site][target_idx]["is_send"] = True
            storage.save_last_results(last)
            logger.info(f"[JSON] Отправлена: {target_art['title'][:80]}")
        except Exception as e:
            logger.error(f"Ошибка отправки старой статьи: {e}\n{traceback.format_exc()}")
            await bot.send_log(f"Ошибка отправки старой статьи: {e}")


async def _daily_prune():
    n = storage.prune_old_links()
    if n > 0:
        logger.info(f"Daily prune: deleted {n} old links (>{config.LINKS_RETENTION_DAYS}d, is_send=1)")
        if n >= 100:
            await bot.send_log(
                f"🧹 Daily prune: удалено <b>{n}</b> старых записей из links "
                f"(старше {config.LINKS_RETENTION_DAYS} дней, is_send=1)"
            )


def configure_jobs():
    """Регистрирует jobs — вызвать ОДИН раз перед scheduler.start()."""
    scheduler.add_job(
        parser.run_auto_parse, trigger="interval",
        minutes=config.PARSER_INTERVAL_MINUTES,
        next_run_time=datetime.now() + timedelta(seconds=30),
        id="auto_parse_job", max_instances=1, coalesce=True,
    )
    scheduler.add_job(
        send_oldest_unsent_article, trigger="interval",
        minutes=config.SENDER_INTERVAL_MINUTES,
        next_run_time=datetime.now() + timedelta(seconds=45),
        id="send_oldest_article_job", max_instances=1, coalesce=True,
    )
    if config.LINKS_RETENTION_DAYS > 0:
        scheduler.add_job(
            _daily_prune, trigger="cron",
            hour=4, minute=0,
            id="prune_old_links_job", max_instances=1, coalesce=True,
        )


async def send_startup_message():
    await bot.send_articles(
        "<b>Парсер запущен</b>\n\n"
        f"• Парсинг — каждые {config.PARSER_INTERVAL_MINUTES} мин\n"
        f"• Trickle-отправка — каждые {config.SENDER_INTERVAL_MINUTES} мин\n\n"
        "Первый парсинг через 30 секунд.",
        preview=False,
    )