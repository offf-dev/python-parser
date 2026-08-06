"""Два Telegram-бота: articles_bot (публичный канал) + logs_bot (админ-чат).

Если соответствующий ENV не задан — бот no-op, только пишет в лог. Это позволяет
поднять парсер локально без токенов.
"""

import html as _html
import re

from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder

import config
import heartbeat
from logging_setup import get_logger


logger = get_logger()


# ====================== Форматирование сообщений ======================
# Формат: {emoji} <b>{title}</b>\n{BRAILLE_BLANK}\n<a href="...">{ZWSP}</a>
# - BRAILLE_BLANK на отдельной строке = «пустая» строка, которую Telegram
#   не схлопывает (обычные \n между двумя инвизами он съедает).
# - ZWSP внутри <a> делает ссылку невидимой в тексте, но preview под сообщением
#   всё равно рендерится (фавикон + домен в карточке).
# - emoji приходит из колонки sites.emoji; если не задан — DEFAULT_EMOJI.

_BRAILLE_BLANK = "⠀"
_ZWSP = "​"
DEFAULT_EMOJI = "📰"

# Хосты, для которых Telegram надёжно НЕ отдаёт preview-карточку
# (страница за login-wall, OG-метатеги недоступны бот-агенту, etc).
# Для таких — делаем тайтл кликабельной ссылкой, чтобы пользователь
# мог хоть как-то перейти.
_NO_PREVIEW_DOMAINS = {
    "linkedin.com",  # посты/pulse — preview не появляется
}


def _has_preview(url: str) -> bool:
    from storage import extract_domain
    return extract_domain(url) not in _NO_PREVIEW_DOMAINS


def format_article(title: str, url: str, emoji: str = None) -> str:
    """Форматирует одну статью в TG-сообщение."""
    e = emoji or DEFAULT_EMOJI
    if _has_preview(url):
        # Скрытая ссылка — Telegram сам нарисует preview-карточку под сообщением
        return f'{e} <b>{title}</b><a href="{url}">{_ZWSP}</a>'
    # Превью гарантированно не будет → делаем тайтл кликабельным
    return f'{e} <b><a href="{url}">{title}</a></b>'

_articles_app = None
_articles_bot = None
_logs_app = None
_logs_bot = None
_summary_app = None
_summary_bot = None


async def init_bots():
    """Поднять боты если включены. Вызывать ОДИН раз из async-контекста."""
    global _articles_app, _articles_bot, _logs_app, _logs_bot, _summary_app, _summary_bot

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

    # Отдельный summary-бот: если задан TG_BOT_TOKEN_FOR_SUMMARY и он отличается
    # от articles-бота — поднимаем как отдельный Application.
    if config.TELEGRAM_TOKEN_SUMMARY and config.TELEGRAM_TOKEN_SUMMARY != config.TELEGRAM_TOKEN_ARTICLES:
        try:
            logger.info("Инициализация summary-бота...")
            _summary_app = ApplicationBuilder().token(config.TELEGRAM_TOKEN_SUMMARY).build()
            _summary_bot = _summary_app.bot
            await _summary_app.initialize()
            await _summary_app.start()
        except Exception as e:
            logger.error(f"Ошибка инициализации summary-бота: {e}")


# TG поддерживает только ограниченный набор HTML-тэгов. Когда в текст ошибки
# попадает что-то вроде Playwright-овского `<launching> chrome ...`, TG отвечает
# `Can't parse entities: unsupported start tag` и сообщение НЕ доходит — это в
# первую очередь убивало алерты watchdog'а.
_TG_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "a", "code", "pre", "br", "tg-spoiler", "blockquote", "span",
}
_TAG_RE = re.compile(r"</?([a-zA-Z][a-zA-Z0-9-]*)\b[^<>]*>")


def _sanitize_html_for_tg(text: str) -> str:
    """Escape-ит любые тэги вне TG-белого списка; легальные <b>/<code>/<a> и
    т.п. оставляет как есть. Голые `<` и `>` в строке (не часть тэга) трогать
    не нужно — TG их не пытается распарсить."""
    return _TAG_RE.sub(
        lambda m: m.group(0) if m.group(1).lower() in _TG_ALLOWED_TAGS else _html.escape(m.group(0)),
        text,
    )


async def _send_with_html_fallback(bot_obj, chat_id, text: str, **kwargs):
    """send_message с HTML; при unsupported-tag ошибке — sanitize + retry,
    иначе хотя бы plain-text fallback. Возвращает True при успехе."""
    try:
        await bot_obj.send_message(chat_id=chat_id, text=text, parse_mode=ParseMode.HTML, **kwargs)
        return True
    except Exception as e:
        if "parse entities" not in str(e).lower() and "unsupported" not in str(e).lower():
            raise
        logger.warning(f"TG HTML parse failed ({e}); retry with sanitized HTML")
        try:
            await bot_obj.send_message(
                chat_id=chat_id, text=_sanitize_html_for_tg(text),
                parse_mode=ParseMode.HTML, **kwargs,
            )
            return True
        except Exception as e2:
            logger.warning(f"sanitized retry failed ({e2}); fallback to plain text")
            await bot_obj.send_message(chat_id=chat_id, text=text, **kwargs)
            return True


async def send_articles(text: str, preview: bool = True):
    """Шлёт в публичный канал. No-op если бот выключен.
    По умолчанию preview включён — статьи рендерятся как карточки с фавиконом.
    """
    if not _articles_bot:
        logger.info(f"[TG-articles OFF] would send: {text[:120]}...")
        return
    try:
        await _send_with_html_fallback(
            _articles_bot, config.TELEGRAM_CHANNEL_ID_ARTICLES, text,
            disable_web_page_preview=not preview,
        )
        logger.info(f"[TG-articles ✓] sent: {text[:80]}")
        heartbeat.touch()
    except Exception as e:
        msg = f"НЕ УДАЛОСЬ отправить в канал: {e}"
        logger.error(msg)
        await send_log(msg)


async def send_summary(text: str):
    """Шлёт сводку цикла парсинга / стартовое сообщение в summary-канал.
    Использует отдельный summary-бот если задан, иначе articles-бот.
    No-op если ENABLE_PARSE_SUMMARY=false.
    """
    if not config.ENABLE_PARSE_SUMMARY:
        logger.info(f"[TG-summary OFF by env] would send: {text[:120]}")
        return
    bot_to_use = _summary_bot or _articles_bot
    if not bot_to_use or not config.TELEGRAM_CHANNEL_ID_SUMMARY:
        logger.info(f"[TG-summary OFF] would send: {text[:120]}")
        return
    try:
        await _send_with_html_fallback(
            bot_to_use, config.TELEGRAM_CHANNEL_ID_SUMMARY, text,
            disable_web_page_preview=True,
        )
        logger.info(f"[TG-summary ✓] sent: {text[:80]}")
    except Exception as e:
        logger.error(f"НЕ УДАЛОСЬ отправить summary: {e}")


async def send_notice(text: str):
    """Операционное уведомление в logs-чат — без обёртки «🚨 Ошибка в парсере».

    Для сообщений вида «ресурс заблокирован антиботом» / «снова парсится»:
    это состояние внешнего мира, а не сбой нашего кода, и выглядеть как
    авария оно не должно — иначе настоящие ошибки теряются в потоке.
    """
    if not _logs_bot:
        logger.info(f"[TG-logs OFF] would notice: {text[:200]}")
        return
    try:
        await _send_with_html_fallback(
            _logs_bot, config.TELEGRAM_CHANNEL_ID_LOGS, text,
            disable_web_page_preview=True,
        )
        logger.info(f"[TG-logs ✓] notice: {text[:80]}")
    except Exception as e:
        logger.error(f"НЕ УДАЛОСЬ отправить notice в logs-чат: {e}")


async def send_log(error_msg: str):
    """Шлёт в админ-чат. No-op если бот выключен."""
    if not _logs_bot:
        logger.info(f"[TG-logs OFF] would log: {error_msg[:200]}")
        return
    try:
        await _send_with_html_fallback(
            _logs_bot, config.TELEGRAM_CHANNEL_ID_LOGS,
            f"<b>🚨 Ошибка в парсере!</b>\n\n{error_msg}\n\nПроверьте логи для деталей.",
            disable_web_page_preview=True,
        )
        logger.info(f"[TG-logs ✓] sent: {error_msg[:80]}")
    except Exception as e:
        logger.error(f"НЕ УДАЛОСЬ отправить в logs-чат: {e}")