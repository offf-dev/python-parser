# app.py — версия с /debug формой для получения HTML (для анализа селекторов)

import os
import re
import json
import threading
import asyncio
from datetime import datetime, timedelta
import atexit  # Для обработки выхода/краша
import traceback  # Для стека ошибок

from flask import Flask, request, render_template_string, redirect, url_for, flash
from math import ceil
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
from urllib.parse import urljoin
import pandas as pd

from urllib3.exceptions import InsecureRequestWarning
import warnings

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_ERROR  # Для listener ошибок
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder

import time
import logging
from logging.handlers import TimedRotatingFileHandler

# Async Playwright импорт
from playwright.async_api import async_playwright

# Fake UA (если не установлен, удалите или pip install)
from fake_useragent import UserAgent

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.exc import SQLAlchemyError

from html import escape

# ====================== LOGGING ======================
def setup_logging():
    os.makedirs('logs', exist_ok=True)

    logger = logging.getLogger(__name__)
    logger.setLevel(logging.INFO)

    formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    handler = TimedRotatingFileHandler(
        filename='logs/parser.log',
        when='midnight',
        interval=1,
        backupCount=30,
        encoding='utf-8'
    )
    handler.setFormatter(formatter)
    logger.addHandler(handler)

    # Настройка для APScheduler: перенаправляем его логи в наш файл
    aps_logger = logging.getLogger('apscheduler')
    aps_logger.setLevel(logging.INFO)  # Или logging.WARNING, если хотите меньше вывода
    aps_logger.addHandler(handler)
    aps_logger.propagate = False  # Чтобы не дублировалось в root logger

    logging.captureWarnings(True)
    warnings.filterwarnings("always", category=MarkupResemblesLocatorWarning)
    warnings.filterwarnings(
        "ignore",
        message=".*strip_cdata.*",
        category=UserWarning,
        module="bs4.builder._lxml"
    )

    return logger

logger = setup_logging()

# ====================== FLASK ======================
app = Flask(__name__)

# ====================== НАСТРОЙКИ ======================
TELEGRAM_TOKEN_ARTICLES = os.getenv("TG_BOT_TOKEN_FOR_ARTICLES")
TELEGRAM_CHANNEL_ID_ARTICLES = int(os.getenv("TG_CHAT_ID_FOR_ARTICLES"))

TELEGRAM_TOKEN_LOGS = os.getenv("TG_BOT_TOKEN_FOR_LOGS")
TELEGRAM_CHANNEL_ID_LOGS = int(os.getenv("TG_CHAT_ID_FOR_LOGS"))

PARSER_INTERVAL_MINUTES = int(os.getenv("PARSER_INTERVAL_MINUTES", 10))

DATA_FILE = 'data/resources.json'
LAST_RESULTS_FILE = 'data/last_results.json'

# Пагинация
PER_PAGE_OPTIONS = [20, 50, 100]
DEFAULT_PER_PAGE = 20

# ГИБРИДНЫЙ РЕЖИМ
USE_DB_FOR_RESOURCES = os.getenv("USE_DB_FOR_RESOURCES", "false").lower() == "true"
USE_DB_FOR_ARTICLES = os.getenv("USE_DB_FOR_ARTICLES", "false").lower() == "true"

# БД
DB_HOST = os.getenv("DB_HOST", "127.0.0.1")
DB_PORT = os.getenv("DB_PORT", "3306")
DB_DATABASE = os.getenv("DB_DATABASE")
DB_USERNAME = os.getenv("DB_USERNAME")
DB_PASSWORD = os.getenv("DB_PASSWORD")
DB_USE_SSL = os.getenv("DB_USE_SSL", "false").lower() == "true"

# Lock для файлов
file_lock = threading.Lock()

# ====================== Создание движка БД ======================
def get_db_engine():
    if not all([DB_USERNAME, DB_PASSWORD, DB_DATABASE, DB_HOST]):
        logger.warning("Не все параметры БД указаны")
        return None

    # MySQL подключение
    connection_string = f"mysql+pymysql://{DB_USERNAME}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_DATABASE}"

    # Добавляем параметры для лучшей совместимости
    if DB_USE_SSL:
        connection_string += "?ssl_verify_cert=false&ssl_verify_server_cert=false"
    else:
        connection_string += "?ssl_verify_cert=false"

    return create_engine(
        connection_string,
        pool_pre_ping=True,
        pool_recycle=3600,
        pool_size=10,
        max_overflow=20,
        echo=False,
        connect_args={"charset": "utf8mb4"}
    )

engine = get_db_engine()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine) if engine else None

# ====================== ИНИЦИАЛИЗАЦИЯ БОТОВ ======================
articles_app = None
articles_bot = None
logs_app = None
logs_bot = None

async def init_articles_bot():
    global articles_app, articles_bot
    try:
        logger.info("Инициализация Telegram бота для статей...")
        articles_app = ApplicationBuilder().token(TELEGRAM_TOKEN_ARTICLES).build()
        articles_bot = articles_app.bot
        await articles_app.initialize()
        await articles_app.start()
    except Exception as e:
        logger.error(f"Ошибка инициализации бота для статей: {e}")
        raise

async def init_logs_bot():
    global logs_app, logs_bot
    try:
        logger.info("Инициализация Telegram бота для логов...")
        logs_app = ApplicationBuilder().token(TELEGRAM_TOKEN_LOGS).build()
        logs_bot = logs_app.bot
        await logs_app.initialize()
        await logs_app.start()
    except Exception as e:
        logger.error(f"Ошибка инициализации бота для логов: {e}")
        raise

# ====================== ОТПРАВКА В ТГ ======================
async def send_telegram_message(text: str):
    try:
        await articles_bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID_ARTICLES,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        logger.info("Сообщение успешно отправлено в Telegram канал для статей")
    except Exception as e:
        error_msg = f"НЕ УДАЛОСЬ отправить сообщение в Telegram (статьи): {e}"
        await send_error_to_telegram(error_msg)
        logger.error(error_msg)

async def send_error_to_telegram(error_msg: str):
    try:
        await logs_bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID_LOGS,
            text=f"<b>🚨 Ошибка в парсере!</b>\n\n{error_msg}\n\nПроверьте логи для деталей.",
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        logger.info("Сообщение об ошибке успешно отправлено в Telegram канал для логов")
    except Exception as e:
        error_msg = f"НЕ УДАЛОСЬ отправить сообщение об ошибке в Telegram (логи): {e}"
        logger.error(error_msg)

# ====================== ФАЙЛЫ ======================
def load_resources():
    if USE_DB_FOR_RESOURCES:
        return load_resources_from_db()
    else:
        return load_resources_from_json()

def load_resources_from_json():
    # ваш текущий код load_resources()
    with file_lock:
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for res in data:
                        if 'active' not in res:
                            res['active'] = not res.get('paused', False)  # миграция
                        if 'paused' in res:
                            del res['paused']
                    logger.info(f"Загружено {len(data)} ресурсов из JSON")
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки JSON ресурсов: {e}")
    return []

def load_resources_from_db():
    if not SessionLocal:
        logger.error("БД не настроена")
        return []
    try:
        with SessionLocal() as session:
            result = session.execute(text("""
                SELECT id, site_key, articles_selector, title_selector,
                       url_selector, active
                FROM sites
                WHERE active = 1
            """)).fetchall()

            resources = []
            for row in result:
                resources.append({
                    "name": row.site_key,
                    "url": f"https://{row.site_key}",                    # ← по умолчанию, как в Laravel
                    "item_selector": row.articles_selector,
                    "title_selector": row.title_selector,
                    "link_selector": row.url_selector,
                    "active": bool(row.active)
                })
            logger.info(f"Загружено {len(resources)} ресурсов из БД (sites)")
            return resources
    except Exception as e:
        logger.error(f"Ошибка загрузки ресурсов из БД: {e}")
        return []

def save_resources(resources):
    if USE_DB_FOR_RESOURCES:
        save_resources_to_db(resources)
    else:
        save_resources_to_json(resources)

def save_resources_to_json(resources):
    with file_lock:
        try:
            os.makedirs('data', exist_ok=True)
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(resources, f, ensure_ascii=False, indent=2)
            logger.info(f"Сохранено {len(resources)} ресурсов в JSON")
        except Exception as e:
            logger.error(f"Ошибка сохранения ресурсов в JSON: {e}")

def save_resources_to_db(resources):
    if not SessionLocal:
        logger.error("БД не настроена, сохранение невозможно")
        return
    try:
        with SessionLocal() as session:
            for res in resources:
                site_key = res.get("name")
                active = res.get("active", False)

                # Обновляем существующий сайт
                result = session.execute(text("""
                    UPDATE sites
                    SET active = :active,
                        updated_at = NOW()
                    WHERE site_key = :site_key
                """), {"active": active, "site_key": site_key})

                # Если не обновилось — значит сайта нет, можно добавить (опционально)
                if result.rowcount == 0:
                    logger.warning(f"Сайт {site_key} не найден в БД при сохранении")

            session.commit()
            logger.info(f"Обновлён статус активности для {len(resources)} ресурсов в БД")
    except Exception as e:
        logger.error(f"Ошибка сохранения ресурсов в БД: {e}")

def load_last_results():
    with file_lock:
        try:
            if os.path.exists(LAST_RESULTS_FILE):
                with open(LAST_RESULTS_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    logger.info(f"Загружены последние результаты ({len(data)} источников)")
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки last_results: {e}")
    return {}

def save_last_results(results):
    with file_lock:
        try:
            os.makedirs('data', exist_ok=True)
            with open(LAST_RESULTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            logger.info("last_results сохранены")
        except Exception as e:
            logger.error(f"Ошибка сохранения last_results: {e}")

resources = load_resources()
last_results = load_last_results()

# ====================== РАБОТА С ИСТОРИЕЙ СТАТЕЙ ======================

def get_known_urls(resource_name: str):
    """Возвращает set URL-ов уже известных статей для ресурса"""
    if USE_DB_FOR_ARTICLES:
        return get_known_urls_from_db(resource_name)
    else:
        return get_known_urls_from_json(resource_name)

def get_known_urls_from_json(resource_name: str):
    last_results = load_last_results()
    articles = last_results.get(resource_name, [])
    return {art['url'] for art in articles}

def get_known_urls_from_db(resource_name: str):
    if not SessionLocal:
        logger.warning("БД недоступна, возвращаем пустой набор known_urls")
        return set()
    try:
        with SessionLocal() as session:
            site_id = session.execute(text(
                "SELECT id FROM sites WHERE site_key = :site_key"
            ), {"site_key": resource_name}).scalar()

            if not site_id:
                return set()

            result = session.execute(text(
                "SELECT url FROM links WHERE site_id = :site_id"
            ), {"site_id": site_id}).fetchall()

            return {row.url for row in result}
    except Exception as e:
        logger.error(f"Ошибка получения known_urls из БД для {resource_name}: {e}")
        return set()


def save_new_articles(resource_name: str, new_articles: list):
    """Сохраняет новые статьи в JSON или в БД (is_send = false)"""
    if not new_articles:
        return
    if USE_DB_FOR_ARTICLES:
        save_new_articles_to_db(resource_name, new_articles)
    else:
        save_new_articles_to_json(resource_name, new_articles)

def save_new_articles_to_json(resource_name: str, new_articles: list):
    last_results = load_last_results()
    if resource_name not in last_results:
        last_results[resource_name] = []
    new_articles = [{**art, 'parsed_at': datetime.now().isoformat()} for art in new_articles]
    last_results[resource_name].extend(new_articles)
    save_last_results(last_results)
    logger.info(f"Сохранено {len(new_articles)} новых статей в JSON для {resource_name}")

def save_new_articles_to_db(resource_name: str, new_articles: list):
    if not SessionLocal:
        return
    try:
        with SessionLocal() as session:
            site_id = session.execute(text(
                "SELECT id FROM sites WHERE site_key = :site_key"
            ), {"site_key": resource_name}).scalar()

            if not site_id:
                logger.warning(f"Сайт {resource_name} не найден в таблице sites")
                return

            for art in new_articles:
                session.execute(text("""
                    INSERT IGNORE INTO links
                    (site_id, title, description, url, is_send, created_at, updated_at)
                    VALUES (:site_id, :title, :description, :url, false, NOW(), NOW())
                """), {
                    "site_id": site_id,
                    "title": art["title"],
                    "description": f"<a href='{art['url']}'>{art['title']}</a>",
                    "url": art["url"]
                })
            session.commit()
            logger.info(f"Добавлено {len(new_articles)} новых статей в БД (links) для {resource_name}")
    except Exception as e:
        logger.error(f"Ошибка сохранения новых статей в БД: {e}")

# ====================== ПАРСИНГ ======================
async def parse_resource(resource, limit=20):
    try:
        logger.info(f"Парсим: {resource['name']} → {resource['url']}")

        # Фиксированные headers (как в п1)
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Accept-Encoding': 'gzip, deflate, br',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1',
            'Referer': 'https://www.google.com/',
            'Sec-CH-UA': '"Not A;Brand";v="99", "Chromium";v="121", "Google Chrome";v="121"',
            'Sec-CH-UA-Mobile': '?0',
            'Sec-CH-UA-Platform': '"Windows"'
        }
        logger.info(f"Используемые заголовки: {headers}")  # Для отладки

        async with async_playwright() as p:
            # Запуск с маскировкой (п2)
            browser = await p.chromium.launch(
                headless=True,
                args=[
                    '--no-sandbox',
                    '--disable-setuid-sandbox',
                    '--disable-blink-features=AutomationControlled',  # Скрываем автоматизацию
                    '--disable-infobars',  # Убираем панель "Chrome управляется"
                    '--window-size=1920,1080'  # Реалистичный размер окна
                ]
            )
            context = await browser.new_context(
                extra_http_headers=headers,
                user_agent=headers['User-Agent'],
                viewport={'width': 1920, 'height': 1080}  # Десктопный вид
            )
            page = await context.new_page()

            # Добавляем скрипты маскировки (п2)
            await page.add_init_script("""Object.defineProperty(navigator, 'webdriver', { get: () => undefined });""")
            await page.add_init_script("""Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });""")  # Фейковые плагины
            await page.add_init_script("""Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });""")  # Фейковые языки
            await page.add_init_script("""Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });""")  # Фейковая платформа
            await page.add_init_script("""Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });""")  # Фейковое железо

            # Goto с повтором и задержкой
            for attempt in range(5):  # Было 3
                try:
                    await asyncio.sleep(random.uniform(1, 3))  # Добавить import random
                    await page.goto(resource['url'], wait_until='domcontentloaded', timeout=180000)
                    await page.wait_for_timeout(3000)
                    break
                except Exception as goto_e:
                    logger.warning(f"Ошибка goto (попытка {attempt+1}/5) для {resource['url']}: {str(goto_e)}")
                    if attempt == 4:
                        raise

            html = await page.content()
            logger.info(f"Длина полученного HTML: {len(html)}")
            await browser.close()

        soup = BeautifulSoup(html, 'lxml')
        items = soup.select(resource['item_selector'])
        logger.info(f"Найдено {len(items)} элементов, берём первые {limit}")

        data = []
        for item in items[:limit]:
            title_tag = item.select_one(resource['title_selector'])
            link_tag = item.select_one(resource['link_selector'])

            title = title_tag.get_text(strip=True) if title_tag else "—"
            link = link_tag['href'] if link_tag and link_tag.has_attr('href') else None
            if link:
                link = urljoin(resource['url'], link)
                if not link.startswith('http'):
                    continue

                with warnings.catch_warnings(record=True) as w:
                    clean_title = BeautifulSoup(title, "lxml").get_text(strip=True)
                    for warning in w:
                        msg = str(warning.message)
                        if "strip_cdata" not in msg:
                            logger.warning(f"Предупреждение для заголовка '{title}': {msg} (файл: {warning.filename}, строка: {warning.lineno})")
                if not clean_title:
                    clean_title = "Без заголовка"

                data.append({
                    "title": clean_title,
                    "url": link
                })

        if not data:
            return [], "селекторы не найдены или нет статей на странице"

        logger.info(f"Успешно спаршено {len(data)} статей (лимит: {limit}) с {resource['name']}")
        return data, None
    except Exception as e:
        error_msg = f"сайт недоступен: {str(e)}"
        logger.error(f"ОШИБКА парсинга {resource.get('name', 'unknown')}: {error_msg}")
        await send_error_to_telegram(error_msg)
        return [], error_msg

# Новая async функция для получения только HTML (для /debug)
async def get_page_html(url):
    try:
        ua = UserAgent()
        headers = {
            'User-Agent': ua.random,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.5',
            'Referer': 'https://www.google.com/',
            'Connection': 'keep-alive',
            'Upgrade-Insecure-Requests': '1',
            'Sec-Fetch-Dest': 'document',
            'Sec-Fetch-Mode': 'navigate',
            'Sec-Fetch-Site': 'none',
            'Sec-Fetch-User': '?1'
        }

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-setuid-sandbox'])
            context = await browser.new_context(extra_http_headers=headers)
            page = await context.new_page()

            for attempt in range(2):
                try:
                    await page.goto(url, wait_until='domcontentloaded', timeout=120000)
                    break
                except Exception as goto_e:
                    if 'Timeout' in str(goto_e):
                        logger.warning(f"Timeout на goto (попытка {attempt+1}/2) для {url}")
                        if attempt == 1:
                            raise
                    else:
                        raise

            html = await page.content()
            await browser.close()

        return html
    except Exception as e:
        logger.error(f"ОШИБКА получения HTML для {url}: {e}")
        return f"Ошибка: {str(e)}"

# ====================== АВТОПАРСИНГ ======================
async def send_new_articles_async():
    try:
        start_time = time.perf_counter()  # Начало измерения общего времени

        global resources, last_results
        logger.info("Запуск автопарсинга — проверяем на новые статьи")

        resources = load_resources()
        if not resources:
            await send_telegram_message("База ресурсов пуста")
            return

        all_new_articles = []
        lines = []

        for resource in resources:
            if not resource.get('active', False):
                logger.info(f"Ресурс {resource['name']} на паузе — пропускаем")
                continue

            name = resource['name']
            res_start_time = time.perf_counter()  # Начало измерения для ресурса

            try:
                current_items, error_msg = await parse_resource(resource, limit=20)
            except Exception as parse_e:
                error_msg = f"Неожиданная ошибка парсинга {name}: {str(parse_e)}"
                logger.error(error_msg)
                await send_error_to_telegram(error_msg)  # Добавляем отправку в TG
                current_items = []

            res_elapsed = time.perf_counter() - res_start_time
            logger.info(f"Парсинг ресурса {name} занял {res_elapsed:.2f} секунд")

            if error_msg:
                logger.info(f"Ошибка для {name}: {error_msg}")
                continue  # Пропускаем добавление в lines

            if len(current_items) > 0:
                lines.append(f"📍 {name} ({len(current_items)} статей)")

            resource_articles = []
            new_items = []

            logger.info(f"\n=== {name.upper()} ===")

            known_urls = get_known_urls(name)   # ← ключевой вызов

            for item in current_items:
                clean_title = item['title']
                url = item['url']

                logger.info(f"• {clean_title}")
                logger.info(f"  → {url}\n")

                resource_articles.append({"title": clean_title, "url": url})

                if url not in known_urls:
                    new_items.append({"title": clean_title, "url": url})
                    all_new_articles.append({"Источник": name, "title": clean_title, "url": url})

            if new_items:
                save_new_articles(name, new_items)   # ← сохранение

            resource_articles_count = len(resource_articles)
            new_items_count = len(new_items)

            logger.info(f"Спаршено {resource_articles_count} статей с {name} (из них новых: {new_items_count})")

        if lines:
            message = f"🔥 Обновление парсинга\n\n" + "\n".join(lines)

            if all_new_articles:
                new_lines = []
                current_source = None
                for art in all_new_articles:
                    if art["Источник"] != current_source:
                        current_source = art["Источник"]
                        new_lines.append(f"\n<b>📍 {current_source}</b>\n")
                    new_lines.append(f"• <a href='{art['url']}'>{art['title']}</a>")

                message += f"\n\n<b>Новые статьи ({len(all_new_articles)} шт.):</b>\n"
                message += "\n".join(new_lines)
        else:
            message = "Ничего не спарсили 😔"

        await send_telegram_message(message)
        logger.info("Цикл автопарсинга отработал")

        total_elapsed = time.perf_counter() - start_time
        logger.info(f"Общий парсинг занял {total_elapsed:.2f} секунд")

    except Exception as e:
        error_msg = f"Ошибка в автопарсинге: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        await send_error_to_telegram(error_msg)

async def run_auto_parse():
    try:
        logger.info("Задача планировщика стартовала")
        await send_new_articles_async()
    except Exception as e:
        error_msg = f"Ошибка в задаче scheduler: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        await send_error_to_telegram(error_msg)

# ====================== ПЛАНИРОВЩИК ======================
scheduler = AsyncIOScheduler()

def job_error_listener(event):
    if event.exception:
        error_msg = f"Ошибка в job {event.job_id}: {str(event.exception)}\n{event.traceback}"
        logger.error(error_msg)
        loop = asyncio.get_event_loop()
        asyncio.run_coroutine_threadsafe(send_error_to_telegram(error_msg), loop)

scheduler.add_listener(job_error_listener, EVENT_JOB_ERROR)

scheduler.add_job(
    run_auto_parse,
    trigger='interval',
    minutes=PARSER_INTERVAL_MINUTES,
    next_run_time=datetime.now() + timedelta(seconds=30),
    id='auto_parse_job',
    max_instances=1,
    coalesce=True
)

# ====================== СТАРТОВОЕ СООБЩЕНИЕ ======================
async def send_startup_message():
    await send_telegram_message(
        "<b>Парсер запущен!</b>\n\n"
        "Первое сообщение — через 30 секунд\n"
        f"Далее — каждые {PARSER_INTERVAL_MINUTES} минут ✅"
    )
    logger.info("Стартовое сообщение отправлено")

# ====================== ASGI + Hypercorn ======================
from hypercorn.config import Config
from hypercorn.asyncio import serve

async def run_scheduler_and_bot():
    try:
        await init_articles_bot()
        await init_logs_bot()

        logger.info("Запуск планировщика APScheduler...")
        scheduler.start()

        await send_startup_message()
        logger.info(f"Планировщик активен: первое сообщение через 30 сек, потом каждые {PARSER_INTERVAL_MINUTES} мин")

        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        error_msg = f"Критическая ошибка в run_scheduler_and_bot: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        await send_error_to_telegram(error_msg)

def on_exit():
    error_msg = "Парсер завершается (возможно, краш или рестарт)"
    logger.info(error_msg)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(send_error_to_telegram(error_msg))

atexit.register(on_exit)

async def main():
    try:
        config = Config()
        config.bind = ["0.0.0.0:5000"]
        config.use_reloader = False
        config.worker_class = "asyncio"

        logger.info("Запуск Hypercorn + планировщика...")
        await asyncio.gather(
            run_scheduler_and_bot(),
            serve(app, config)
        )
    except Exception as e:
        error_msg = f"Критическая ошибка в main: {str(e)}\n{traceback.format_exc()}"
        logger.error(error_msg)
        await send_error_to_telegram(error_msg)

# ==================== ФОРМИРОВАНИЕ ДАННЫХ ====================

def get_all_sites():
    if USE_DB_FOR_RESOURCES:
        if not SessionLocal:
            return []
        try:
            with SessionLocal() as session:
                result = session.execute(text("""
                    SELECT id, site_key, articles_selector, title_selector,
                           url_selector, active
                    FROM sites
                    ORDER BY site_key DESC
                """)).fetchall()
                sites = []
                for row in result:
                    sites.append({
                        "id": row.id,
                        "identifier": row.id,                    # ← для чекбокса (int)
                        "site_key": row.site_key,
                        "url": f"https://{row.site_key}",        # ← по умолчанию, как в Laravel
                        "articles_selector": row.articles_selector,
                        "title_selector": row.title_selector,
                        "url_selector": row.url_selector,
                        "active": bool(row.active)
                    })
                return sites
        except Exception as e:
            logger.error(f"Ошибка получения сайтов из БД: {e}")
            return []
    else:
        # JSON-режим
        resources = load_resources_from_json()
        sites = []
        for i, r in enumerate(resources):
            sites.append({
                "id": i + 1,
                "identifier": r.get("name"),                 # ← строка (name)
                "site_key": r.get("name", ""),
                "url": r.get("url", ""),
                "articles_selector": r.get("item_selector", ""),
                "title_selector": r.get("title_selector", ""),
                "url_selector": r.get("link_selector", ""),
                "active": r.get("active", False)
            })
        return sites


def get_all_links():
    """Возвращает список статей для страницы /links"""
    if USE_DB_FOR_ARTICLES:
        if not SessionLocal:
            return []
        try:
            with SessionLocal() as session:
                result = session.execute(text("""
                    SELECT l.id, l.title, l.url, l.is_send, l.created_at,
                           s.site_key as site_name
                    FROM links l
                    LEFT JOIN sites s ON l.site_id = s.id
                    ORDER BY l.created_at DESC
                """)).fetchall()
                return [dict(row) for row in result]
        except Exception as e:
            logger.error(f"Ошибка получения статей из БД: {e}")
            return []
    else:
        # Режим JSON
        last_results = load_last_results()
        links = []
        link_id = 1
        for site_name, articles in last_results.items():
            for art in articles:
                parsed_at = art.get("parsed_at")
                created_at = datetime.fromisoformat(parsed_at) if parsed_at else None
                links.append({
                    "id": link_id,
                    "title": art.get("title", ""),
                    "url": art.get("url", ""),
                    "is_send": True,  # в JSON режиме считаем все отправленными
                    "created_at": created_at,
                    "site_name": site_name
                })
                link_id += 1
        return links

# ==================== ПАГИНАЦИЯ ====================

def get_pagination(total_items, page, per_page):
    total_pages = ceil(total_items / per_page)
    page = max(1, min(page, total_pages)) if total_pages > 0 else 1
    offset = (page - 1) * per_page
    return {
        'page': page,
        'per_page': per_page,
        'total_pages': total_pages,
        'total_items': total_items,
        'has_prev': page > 1,
        'has_next': page < total_pages,
        'offset': offset
    }

# ==================== HTML + РОУТ ====================
def get_navigation_bar(current_page=''):
    """Возвращает готовую HTML-навигацию с подставленными значениями"""
    db_type = 'MySQL' if DB_HOST and 'mysql' in DB_HOST.lower() else 'JSON'
    resources_mode = 'DB' if USE_DB_FOR_RESOURCES else 'JSON'
    articles_mode = 'DB' if USE_DB_FOR_ARTICLES else 'JSON'

    return f'''
<nav style="background: #343a40; padding: 12px 20px; margin-bottom: 20px; color: white;">
    <div style="max-width: 1400px; margin: 0 auto; display: flex; align-items: center; gap: 30px;">
        <div style="display: flex; gap: 25px;">
            <a href="/" style="color: white; text-decoration: none; font-weight: bold; border-bottom: 3px solid {'#0d6efd' if current_page == 'main' else 'transparent'};">Main</a>
            <a href="/sites" style="color: white; text-decoration: none; font-weight: bold; border-bottom: 3px solid {'#0d6efd' if current_page == 'sites' else 'transparent'};">Resources</a>
            <a href="/links" style="color: white; text-decoration: none; font-weight: bold; border-bottom: 3px solid {'#0d6efd' if current_page == 'links' else 'transparent'};">Articles</a>
            <a href="/debug" style="color: white; text-decoration: none; font-weight: bold; border-bottom: 3px solid {'#0d6efd' if current_page == 'debug' else 'transparent'};">Debug HTML</a>
        </div>

        <div style="margin-left: auto; color: #adb5bd; font-size: 13px;">
            DB: {db_type} | Resources: {resources_mode} | Articles: {articles_mode}
        </div>
    </div>
</nav>
'''

HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Парсер статей</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        .form-container { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 15px rgba(0,0,0,0.1); }
        input, button { width: 100%; padding: 12px; margin: 10px 0; font-size: 16px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        button { background: #007bff; color: white; cursor: pointer; font-weight: bold; }
        button[disabled] { opacity: 0.3; filter: grayscale(1); pointer-events: none; }
        button:hover { background: #0056b3; }
        .btn-group { display: flex; gap: 10px; }
        .btn-green { background: #28a745; }
        .btn-green:hover { background: #218838; }
        .links { margin: 20px 0; font-size: 16px; }
        .links a { margin-right: 20px; color: #007bff; text-decoration: none; }
        .error { color: red; background: #ffe6e6; padding: 15px; border-radius: 5px; }
        .success { color: green; background: #e6ffe6; padding: 15px; border-radius: 5px; }
    </style>
</head>
<body>
{{ NAVIGATION_BAR | safe }}

<h1>Создать / Редактировать сайт для парсинга</h1>

<div class="form-container">
    <div class="links">
        <a href="/sites">→ Все сайты ({{ sites_count }})</a>
        <a href="/links">→ Все статьи ({{ links_count }})</a>
    </div>

    <h2>{% if edit_index is defined %}Редактировать ресурс{% else %}Новый ресурс{% endif %}</h2>

    <form id="parseForm" method="post">
        {% if site_id %}
            <input type="hidden" name="site_id" value="{{ site_id }}">
        {% endif %}
        {% if edit_index is defined %}
            <input type="hidden" name="edit_index" value="{{ edit_index }}">
        {% endif %}
        <input type="text" name="name" placeholder="Название ресурса (например: smashingmagazine.com)" value="{{ resource.name if resource else '' }}" required>
        <input type="text" name="url" placeholder="URL страницы[](https://...)" value="{{ resource.url if resource else '' }}" required>
        <input type="text" name="item_selector" placeholder="Селектор блока статьи (например: .article)" value="{{ resource.item_selector if resource else '' }}" required>
        <input type="text" name="title_selector" placeholder="Селектор заголовка внутри блока" value="{{ resource.title_selector if resource else '' }}" required>
        <input type="text" name="link_selector" placeholder="Селектор ссылки внутри блока" value="{{ resource.link_selector if resource else '' }}" required>

        <div class="btn-group" style="margin-top: 20px;">
            <button type="submit" name="action" value="parse" class="btn-green" id="parseBtn">Parse resource</button>
            <button type="submit" name="action" value="save">Save resource</button>
            <button type="button" onclick="clearAll()">Clear all</button>
        </div>
    </form>

    <div id="loading" style="display:none; margin: 15px 0; color: #0066cc;">
        ⏳ Парсинг страницы... Пожалуйста, подождите
    </div>

    <div id="parseResult"></div>

    {% if error %}<div class="error">{{ error }}</div>{% endif %}
    {% if success %}<div class="success">{{ success }}</div>{% endif %}

    {% if table %}
        <div class="table">
            <h3>Результат парсинга ({{ count }} статей)</h3>
            {{ table|safe }}
        </div>
    {% endif %}
</div>

<script>
    function parseSaved(i) { location.href = '/?load=' + i; }
    function editResource(i) { location.href = '/?edit=' + i; }
    function deleteResource(i) { if(confirm('Удалить?')) location.href = '/?delete=' + i; }

    document.getElementById('parseBtn').addEventListener('click', async function(e) {
        e.preventDefault();

        const btn = this;
        const form = document.getElementById('parseForm');
        const loading = document.getElementById('loading');
        const resultDiv = document.getElementById('parseResult');

        // Отключаем кнопку
        btn.disabled = true;
        btn.textContent = 'Парсинг...';
        loading.style.display = 'block';
        resultDiv.innerHTML = '';

        const formData = new FormData(form);

        try {
            const response = await fetch('/parse_now', {
                method: 'POST',
                body: formData
            });

            const result = await response.json();

            if (result.success) {
                resultDiv.innerHTML = `
                    <h3>Результат парсинга (${result.count} статей)</h3>
                    ${result.table}
                `;
                resultDiv.scrollIntoView({ behavior: 'smooth', block: 'start' });
            } else {
                resultDiv.innerHTML = `<div class="error">${result.error}</div>`;
            }

        } catch (err) {
            resultDiv.innerHTML = `<div class="error">Ошибка соединения: ${err.message}</div>`;
        } finally {
            // Возвращаем кнопку
            btn.disabled = false;
            btn.textContent = 'Парсить сейчас';
            loading.style.display = 'none';
        }
    });

    function clearAll() {
        document.getElementById('parseForm').reset();
        const resultDiv = document.getElementById('parseResult');
        if (resultDiv) resultDiv.innerHTML = '';  // Очищаем результаты парсинга
        document.querySelectorAll('.error, .success, .table').forEach(el => el.remove());  // Очищаем ошибки/успех
    }
</script>
</body>
</html>
'''

SITES_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Сайты - Парсер статей</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 1400px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #007bff; color: white; position: sticky; top: 0; }
        tr:hover { background: #f8f9fa; }
        .pagination { margin: 20px 0; text-align: center; }
        .pagination a, .pagination span { padding: 8px 12px; margin: 0 4px; border: 1px solid #ddd; text-decoration: none; border-radius: 4px; }
        .pagination .active { background: #007bff; color: white; border-color: #007bff; }
        .per-page { margin: 10px 0; }
        .btn { padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .btn-edit { background: #17a2b8; color: white; }
        .btn-parse { background: #28a745; color: white; }
        .btn-delete { background: #dc3545; color: white; }
        .checkbox { transform: scale(1.3); }
        .active-yes { color: green; font-weight: bold; }
        .active-no { color: red; }
        .filter-form { margin-bottom: 20px; display: flex; gap: 10px; }
        .filter-form input { padding: 8px; flex-grow: 1; border: 1px solid #ddd; border-radius: 4px; }
        .filter-form button { padding: 8px 16px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .filter-form button:hover { background: #0056b3; }
        .reset-btn { background: #6c757d; }
        .reset-btn:hover { background: #5a6268; }
    </style>
</head>
<body>
{{ NAVIGATION_BAR | safe }}
<div class="container">
    <h1>Сайты для парсинга <a href="/" style="float:right; font-size:16px;">← Создать новый сайт</a></h1>
    <p>Всего сайтов: {{ sites_count }}</p>

    {% if USE_DB_FOR_RESOURCES %}
    <form class="filter-form" method="get">
        <input type="text" name="search" placeholder="Фильтр по домену..." value="{{ search }}">
        <input type="hidden" name="per_page" value="{{ pagination.per_page if pagination else '' }}">
        <input type="hidden" name="page" value="1">
        <button type="submit">Фильтровать</button>
        <button type="button" class="reset-btn" onclick="location.href='?per_page={{ pagination.per_page if pagination else '' }}&page=1'">Сброс</button>
    </form>
    {% else %}
    <div class="filter-form">
        <input type="text" class="filter-input" placeholder="Фильтр по домену... (на лету)" value="">
        <button type="button" class="reset-btn" onclick="resetFilter()">Сброс</button>
    </div>
    {% endif %}

    <div class="per-page">
        Показывать по:
        {% for pp in per_page_options %}
            <a href="?per_page={{ pp }}&page=1{% if search %}&search={{ search }}{% endif %}" class="{% if pp == pagination.per_page %}active{% endif %}">{{ pp }}</a>
        {% endfor %}
    </div>

    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Домен</th>
                <th>URL</th>
                <th>Articles selector</th>
                <th>Title selector</th>
                <th>URL selector</th>
                <th>Active</th>
                <th>Действия</th>
            </tr>
        </thead>
        <tbody>
            {% for site in sites %}
            <tr>
                <td>{{ site.id }}</td>
                <td><strong>{{ site.site_key }}</strong></td>
                <td><a href="{{ site.url }}" target="_blank">{{ site.url }}</a></td>
                <td><code>{{ site.articles_selector or '-' }}</code></td>
                <td><code>{{ site.title_selector or '-' }}</code></td>
                <td><code>{{ site.url_selector or '-' }}</code></td>
                <td>
                    <input type="checkbox" class="checkbox" {{ 'checked' if site.active else '' }}
                           onchange="toggleCheckbox('{{ site.identifier }}', this.checked, {{ USE_DB_FOR_RESOURCES|lower }})">
                </td>
                <td>
                    <a href="/?edit={{ site.id }}" class="btn btn-edit">Редактировать</a>
                    <a href="/?parse_now={{ site.id }}" class="btn btn-parse">Спарсить</a>
                    <button onclick="deleteSite({{ site.id }}, '{{ site.site_key }}')" class="btn btn-delete">Удалить</button>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    {% if pagination and pagination.total_pages > 1 %}
    <div class="pagination">
        {% if pagination.has_prev %}
            <a href="?page={{ pagination.page - 1 }}&per_page={{ pagination.per_page }}{% if search %}&search={{ search }}{% endif %}">← Назад</a>
        {% endif %}

        {% for p in range(1, pagination.total_pages + 1) %}
            {% if p == pagination.page %}
                <span class="active">{{ p }}</span>
            {% elif p == 1 or p == pagination.total_pages or (p >= pagination.page - 2 and p <= pagination.page + 2) %}
                <a href="?page={{ p }}&per_page={{ pagination.per_page }}{% if search %}&search={{ search }}{% endif %}">{{ p }}</a>
            {% elif p == pagination.page - 3 or p == pagination.page + 3 %}
                <span>...</span>
            {% endif %}
        {% endfor %}

        {% if pagination.has_next %}
            <a href="?page={{ pagination.page + 1 }}&per_page={{ pagination.per_page }}{% if search %}&search={{ search }}{% endif %}">Вперёд →</a>
        {% endif %}
    </div>
    {% elif pagination is none %}
    <p><em>В режиме JSON пагинация отключена (всего сайтов: {{ sites|length }})</em></p>
    {% endif %}
</div>

<script>
function toggleCheckbox(identifier, isActive, isDbMode) {
    const url = `/sites/toggle?identifier=${encodeURIComponent(identifier)}&active=${isActive}&db=${isDbMode}`;
    location.href = url;
}

function deleteSite(id, name) {
    if (confirm(`Удалить сайт "${name}" и все его ссылки?`)) {
        fetch('/sites/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({id: id})
        })
        .then(() => location.reload());
    }
}

{% if not USE_DB_FOR_RESOURCES %}
document.querySelector('.filter-input').addEventListener('input', function() {
    let filter = this.value.toLowerCase();
    document.querySelectorAll('tbody tr').forEach(row => {
        let domain = row.querySelector('td:nth-child(2) strong').textContent.toLowerCase();
        row.style.display = domain.includes(filter) ? '' : 'none';
    });
});

function resetFilter() {
    const input = document.querySelector('.filter-input');
    input.value = '';
    input.dispatchEvent(new Event('input'));
}
{% endif %}
</script>
</body>
</html>
'''

LINKS_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Статьи (links) - Парсер</title>
    <style>
        body { font-family: Arial, sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }
        .container { max-width: 1400px; margin: 0 auto; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        table { width: 100%; border-collapse: collapse; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #6c757d; color: white; position: sticky; top: 0; }
        tr:hover { background: #f8f9fa; }
        .pagination { margin: 20px 0; text-align: center; }
        .pagination a, .pagination span { padding: 8px 12px; margin: 0 4px; border: 1px solid #ddd; text-decoration: none; border-radius: 4px; }
        .pagination .active { background: #6c757d; color: white; }
        .per-page { margin: 10px 0; }
        .btn-delete { background: #dc3545; color: white; padding: 6px 12px; border: none; border-radius: 4px; cursor: pointer; }
        .send-yes { color: green; font-weight: bold; }
        .send-no { color: orange; }
        .title { max-width: 600px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .filter-form { margin-bottom: 20px; display: flex; gap: 10px; }
        .filter-form input { padding: 8px; flex-grow: 1; border: 1px solid #ddd; border-radius: 4px; }
        .filter-form button { padding: 8px 16px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .filter-form button:hover { background: #0056b3; }
        .reset-btn { background: #6c757d; }
        .reset-btn:hover { background: #5a6268; }
    </style>
</head>
<body>
{{ NAVIGATION_BAR | safe }}
<div class="container">
    <h1>Список статей <a href="/" style="float:right; font-size:16px;">← Главная</a></h1>
    <p>Всего статей: {{ pagination.total_items }}</p>

    {% if USE_DB_FOR_ARTICLES %}
    <form class="filter-form" method="get">
        <input type="text" name="search" placeholder="Фильтр по заголовку..." value="{{ search }}">
        <input type="hidden" name="per_page" value="{{ pagination.per_page }}">
        <input type="hidden" name="page" value="1">
        <button type="submit">Фильтровать</button>
        <button type="button" class="reset-btn" onclick="location.href='?per_page={{ pagination.per_page }}&page=1'">Сброс</button>
    </form>
    {% else %}
    <div class="filter-form">
        <input type="text" class="filter-input" placeholder="Фильтр по заголовку... (на лету)" value="">
        <button type="button" class="reset-btn" onclick="resetFilter()">Сброс</button>
    </div>
    {% endif %}

    <div class="per-page">
        Показывать по:
        {% for pp in per_page_options %}
            <a href="?per_page={{ pp }}&page=1{% if search %}&search={{ search }}{% endif %}" class="{% if pp == pagination.per_page %}active{% endif %}">{{ pp }}</a>
        {% endfor %}
    </div>

    <table>
        <thead>
            <tr>
                <th>ID</th>
                <th>Заголовок</th>
                <th>Сайт</th>
                <th>Отправлено</th>
                <th>Дата</th>
                <th>Действия</th>
            </tr>
        </thead>
        <tbody>
            {% for link in links %}
            <tr>
                <td>{{ link.id }}</td>
                <td class="title">
                    <a href="{{ link.url }}" target="_blank">{{ link.title }}</a>
                </td>
                <td>{{ link.site_name or '—' }}</td>
                <td>
                    <span class="{% if link.is_send %}send-yes{% else %}send-no{% endif %}">
                        {{ '✅ Да' if link.is_send else '❌ Нет' }}
                    </span>
                </td>
                <td>{{ link.created_at.strftime('%d %b %y') if link.created_at else '-' }}</td>
                <td>
                    <button onclick="deleteLink('{{ link.site_name|replace("'", "\\'") }}', '{{ link.url|replace("'", "\\'") }}')" class="btn-delete">Удалить</button>
                </td>
            </tr>
            {% endfor %}
        </tbody>
    </table>

    {% if pagination and pagination.total_pages > 1 %}
    <div class="pagination">
        {% if pagination.has_prev %}
            <a href="?page={{ pagination.page - 1 }}&per_page={{ pagination.per_page }}{% if search %}&search={{ search }}{% endif %}">← Назад</a>
        {% endif %}

        {% for p in range(1, pagination.total_pages + 1) %}
            {% if p == pagination.page %}
                <span class="active">{{ p }}</span>
            {% elif p == 1 or p == pagination.total_pages or (p >= pagination.page - 2 and p <= pagination.page + 2) %}
                <a href="?page={{ p }}&per_page={{ pagination.per_page }}{% if search %}&search={{ search }}{% endif %}">{{ p }}</a>
            {% elif p == pagination.page - 3 or p == pagination.page + 3 %}
                <span>...</span>
            {% endif %}
        {% endfor %}

        {% if pagination.has_next %}
            <a href="?page={{ pagination.page + 1 }}&per_page={{ pagination.per_page }}{% if search %}&search={{ search }}{% endif %}">Вперёд →</a>
        {% endif %}
    </div>
    {% elif pagination is none %}
    <p><em>В режиме JSON пагинация отключена (всего сайтов: {{ sites|length }})</em></p>
    {% endif %}
</div>

<script>
function deleteLink(site_name, url) {
    if (confirm(`Удалить статью с URL "${url}"?`)) {
        fetch('/links/delete', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({site_name: site_name, url: url})
        })
        .then(() => location.reload());
    }
}

{% if not USE_DB_FOR_ARTICLES %}
document.querySelector('.filter-input').addEventListener('input', function() {
    let filter = this.value.toLowerCase();
    document.querySelectorAll('tbody tr').forEach(row => {
        let title = row.querySelector('.title a').textContent.toLowerCase();
        row.style.display = title.includes(filter) ? '' : 'none';
    });
});

function resetFilter() {
    const input = document.querySelector('.filter-input');
    input.value = '';
    input.dispatchEvent(new Event('input'));
}
{% endif %}
</script>
</body>
</html>
'''

# Новый маршрут /debug
DEBUG_HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Debug: Получить HTML страницы</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h2 { color: #333; }
        input[type="text"] { width: calc(100% - 120px); padding: 12px; margin: 10px 0; font-size: 16px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        label { display: inline-flex; align-items: center; margin: 10px 0; font-size: 15px; }
        button { padding: 12px 20px; background: #28a745; color: white; cursor: pointer; border: none; border-radius: 4px; margin: 10px 0; }
        button:hover { background: #218838; }
        button[disabled] { opacity: 0.3; filter: grayscale(1); pointer-events: none; }
        .error { color: red; background: #ffe6e6; padding: 15px; border-radius: 5px; margin: 10px 0; }
        .toolbar { margin: 15px 0; display: flex; gap: 10px; align-items: center; }
        .copy-btn { background: #007bff; padding: 8px 16px; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .copy-btn:hover { background: #0056b3; }
        pre {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 6px;
            font-size: 12px; /* Меньший шрифт */
            line-height: 1.4; /* Переносы и читаемость */
            white-space: pre-wrap;
            word-wrap: break-word;
            max-height: 70vh;
            overflow: auto;
            border: 1px solid #ddd;
        }
    </style>
</head>
<body>
{{ NAVIGATION_BAR | safe }}
    <h2>Debug: Введите URL для получения HTML</h2>
    <form id="debugForm" method="post">
        <input type="text" name="url" placeholder="URL страницы" value="{{ url if url else '' }}" required>
        <label>
            <input type="checkbox" name="clean_assets" {{ 'checked' if clean_assets else '' }}> Удалить ассеты (скрипты, стили, meta и т.д.)
        </label>
        <button type="submit" id="getHtmlBtn">Получить HTML</button>
    </form>

    <div id="loading" style="display:none; margin: 15px 0; color: #0066cc;">
        ⏳ Загрузка HTML... Пожалуйста, подождите
    </div>

    <div id="debugResult"></div>

<script>
    function copyHTML() {
        const codeElement = document.getElementById('htmlCode');
        if (codeElement) {
            const rawCode = codeElement.innerHTML.replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&');
            navigator.clipboard.writeText(rawCode).then(() => {
                alert('Сырой HTML скопирован!');
            }).catch(err => {
                alert('Ошибка: ' + err);
            });
        }
    };
    document.addEventListener('DOMContentLoaded', () => {
        const form = document.getElementById('debugForm');
        const btn = document.getElementById('getHtmlBtn');
        const originalText = btn.textContent;

        form.addEventListener('submit', async (e) => {
            e.preventDefault();
            btn.disabled = true;
            btn.textContent = 'Загрузка...';
            document.getElementById('loading').style.display = 'block';
            document.getElementById('debugResult').innerHTML = '';

            const formData = new FormData(form);
            formData.append('ajax', 'true');  // Добавляем индикатор AJAX

            try {
                const response = await fetch('/debug', { method: 'POST', body: formData });
                const htmlFragment = await response.text();
                document.getElementById('debugResult').innerHTML = htmlFragment;
            } catch (err) {
                document.getElementById('debugResult').innerHTML = `<div class="error">Ошибка: ${err.message}</div>`;
            } finally {
                btn.disabled = false;
                btn.textContent = originalText;
                document.getElementById('loading').style.display = 'none';
            }
        });
    });
</script>
</body>
</html>
'''

@app.route('/', methods=['GET', 'POST'])
def index():
    logger.info("Запрос к веб-интерфейсу")
    global resources
    resources = load_resources()

    edit_index = request.args.get('edit', type=int)
    parse_now = request.args.get('parse_now', type=int)

    resource = {}
    error = success = table = count = None

    if USE_DB_FOR_RESOURCES:
        with SessionLocal() as session:
            sites_count = session.execute(text("SELECT COUNT(*) FROM sites")).scalar() or 0
    else:
        sites_count = len(load_resources_from_json())

    if USE_DB_FOR_ARTICLES:
        with SessionLocal() as session:
            links_count = session.execute(text("SELECT COUNT(*) FROM links")).scalar() or 0
    else:
        last_results = load_last_results()
        links_count = sum(len(articles) for articles in last_results.values())

    site_id = None
    if edit_index is not None or parse_now is not None:
        target_id = edit_index if edit_index is not None else parse_now
        all_sites = get_all_sites()
        target_site = next((s for s in all_sites if s['id'] == target_id), None)

        if target_site:
            site_id = target_site['id']
            resource = {
                "name": target_site['site_key'],
                "url": target_site.get('url', f"https://{target_site['site_key']}"),
                "item_selector": target_site.get('articles_selector', ''),
                "title_selector": target_site.get('title_selector', ''),
                "link_selector": target_site.get('url_selector', ''),
                "active": target_site.get('active', False)
            }

            # Если это parse_now — сразу парсим
            if parse_now is not None:
                try:
                    data, parse_error = asyncio.run(parse_resource(resource, limit=20))
                    if parse_error:
                        error = f"Ошибка парсинга: {parse_error}"
                    elif not data:
                        error = "Ничего не найдено по указанным селекторам"
                    else:
                        df = pd.DataFrame([{"Заголовок": art['title'], "Ссылка": f"<a href='{art['url']}'>{art['url']}</a>"} for art in data])
                        table = df.to_html(escape=False, index=False)
                        count = len(data)
                        success = f"Успешно спаршено {count} статей с {resource['name']}!"
                except Exception as e:
                    error = f"Ошибка парсинга: {str(e)}"

    if request.method == 'POST':
        action = request.form.get('action')
        site_id_from_form = request.form.get('site_id', type=int)

        current_form = {
            "name": request.form['name'].strip(),
            "url": request.form['url'].strip(),
            "item_selector": request.form['item_selector'].strip(),
            "title_selector": request.form['title_selector'].strip(),
            "link_selector": request.form['link_selector'].strip(),
            "active": resource.get('active', False)  # Сохраняем оригинальное значение active, для нового - False
        }

        if action == "save":
            if USE_DB_FOR_RESOURCES and site_id_from_form:
                # DB-режим: обновляем существующий сайт
                try:
                    with SessionLocal() as session:
                        session.execute(text("""
                            UPDATE sites
                            SET site_key = :site_key,
                                articles_selector = :articles_selector,
                                title_selector = :title_selector,
                                url_selector = :url_selector,
                                updated_at = NOW()
                            WHERE id = :id
                        """), {
                            "site_key": current_form["name"],
                            "articles_selector": current_form["item_selector"],
                            "title_selector": current_form["title_selector"],
                            "url_selector": current_form["link_selector"],
                            "id": site_id_from_form
                        })
                        session.commit()
                    success = f"Сайт обновлён: {current_form['name']}"
                except Exception as e:
                    error = f"Ошибка обновления в БД: {str(e)}"
            else:
                # JSON-режим (или новый сайт)
                if edit_index is not None:
                    # обновление в JSON
                    resources[edit_index - 1] = current_form
                    save_resources(resources)
                    success = f"Обновлён: {current_form['name']}"
                else:
                    resources.append(current_form)
                    save_resources(resources)
                    success = f"Добавлен новый ресурс: {current_form['name']}"
                    edit_index = len(resources)  # Устанавливаем edit_index для нового ресурса

            # После сохранения обновляем resource текущими значениями формы
            if success:
                resource = current_form

                # Пересчитываем sites_count
                if USE_DB_FOR_RESOURCES:
                    with SessionLocal() as session:
                        sites_count = session.execute(text("SELECT COUNT(*) FROM sites")).scalar() or 0
                else:
                    resources = load_resources()  # Перезагружаем
                    sites_count = len(resources)

    return render_template_string(HTML,
                                  resource=resource,
                                  edit_index=edit_index,
                                  site_id=site_id,                    # ← передаём в шаблон
                                  error=error,
                                  success=success,
                                  table=table,
                                  count=count,
                                  NAVIGATION_BAR=get_navigation_bar('main'),
                                  sites_count=sites_count,
                                  links_count=links_count)

@app.route('/parse_now', methods=['POST'])
def parse_now():
    try:
        site_id = request.form.get('site_id', type=int)
        resource = None

        # Если передан site_id — загружаем существующий ресурс
        if site_id:
            all_sites = get_all_sites()
            target = next((s for s in all_sites if s['id'] == site_id), None)
            if target:
                resource = {
                    "name": target['site_key'],
                    "url": f"https://{target['site_key']}",
                    "item_selector": target.get('articles_selector', ''),
                    "title_selector": target.get('title_selector', ''),
                    "link_selector": target.get('url_selector', ''),
                }

        # Если не нашли по ID — берём данные из формы
        if not resource:
            resource = {
                "name": request.form['name'].strip(),
                "url": request.form['url'].strip(),
                "item_selector": request.form['item_selector'].strip(),
                "title_selector": request.form['title_selector'].strip(),
                "link_selector": request.form['link_selector'].strip(),
            }

        data, parse_error = asyncio.run(parse_resource(resource, limit=20))

        if parse_error:
            return {"success": False, "error": parse_error}
        if not data:
            return {"success": False, "error": "Ничего не найдено по указанным селекторам"}

        df = pd.DataFrame([{"Заголовок": art['title'], "Ссылка": f"<a href='{art['url']}'>{art['url']}</a>"} for art in data])
        table_html = df.to_html(escape=False, index=False)

        return {
            "success": True,
            "count": len(data),
            "table": table_html,
            "resource_name": resource.get("name")
        }

    except Exception as e:
        return {"success": False, "error": str(e)}

@app.route('/sites')
def sites_list():
    search = request.args.get('search', '').strip().lower()

    if USE_DB_FOR_RESOURCES:
        # БД-режим с пагинацией и серверным поиском
        page = request.args.get('page', 1, type=int)
        per_page = request.args.get('per_page', DEFAULT_PER_PAGE, type=int)
        if per_page not in PER_PAGE_OPTIONS:
            per_page = DEFAULT_PER_PAGE

        with SessionLocal() as session:
            if search:
                total_query = text("SELECT COUNT(*) FROM sites WHERE LOWER(site_key) LIKE :search")
                total = session.execute(total_query, {"search": f"%{search}%"}).scalar() or 0
                result_query = text("""
                    SELECT id, site_key, articles_selector, title_selector, url_selector,
                           active, created_at
                    FROM sites
                    WHERE LOWER(site_key) LIKE :search
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """)
            else:
                total_query = text("SELECT COUNT(*) FROM sites")
                total = session.execute(total_query).scalar() or 0
                result_query = text("""
                    SELECT id, site_key, articles_selector, title_selector, url_selector,
                           active, created_at
                    FROM sites
                    ORDER BY created_at DESC
                    LIMIT :limit OFFSET :offset
                """)

            pagination = get_pagination(total, page, per_page)

            result = session.execute(result_query, {"limit": per_page, "offset": pagination['offset'], "search": f"%{search}%"} if search else {"limit": per_page, "offset": pagination['offset']}).fetchall()

            sites = [dict(row) for row in result]
            for site in sites:
                site['url'] = f"https://{site['site_key']}"
            sites_count = total
    else:
        # JSON-режим: загружаем все, сортируем по алфавиту, пагинация отключена, фильтр на клиенте
        all_sites = get_all_sites()
        all_sites = sorted(all_sites, key=lambda s: s['site_key'].lower())
        sites = all_sites
        pagination = None
        sites_count = len(sites)
        per_page_options = PER_PAGE_OPTIONS

    return render_template_string(SITES_HTML,
                                      sites=sites,
                                      pagination=pagination,
                                      per_page_options=per_page_options,
                                      sites_count=sites_count,
                                      NAVIGATION_BAR=get_navigation_bar('sites'),
                                      USE_DB_FOR_RESOURCES=USE_DB_FOR_RESOURCES,
                                      search=search)

@app.route('/sites/toggle')
def sites_toggle():
    identifier = request.args.get('identifier')
    active_str = request.args.get('active', 'true')
    active = active_str.lower() in ('true', '1', 'on')
    is_db_mode = request.args.get('db', 'false').lower() == 'true'

    if not identifier:
        return redirect('/sites')

    if is_db_mode:
        # БД-режим
        if not SessionLocal:
            return redirect('/sites')
        try:
            with SessionLocal() as session:
                session.execute(text("""
                    UPDATE sites
                    SET active = :active, updated_at = NOW()
                    WHERE id = :id
                """), {"active": 1 if active else 0, "id": int(identifier)})
                session.commit()
            logger.info(f"DB toggle: site_id={identifier} → active={active}")
        except Exception as e:
            logger.error(f"Ошибка toggle в БД: {e}")
    else:
        # JSON-режим
        resources = load_resources_from_json()
        updated = False
        for res in resources:
            if res.get("name") == identifier:
                res['active'] = active
                updated = True
                break
        if updated:
            save_resources_to_json(resources)
            logger.info(f"JSON toggle: '{identifier}' → active={active}")
        else:
            logger.warning(f"JSON: ресурс '{identifier}' не найден")

    return redirect('/sites')


@app.route('/sites/delete', methods=['POST'])
def delete_site():
    data = request.get_json()
    site_id = data.get('id')

    if not site_id:
        return {'error': 'no id'}, 400

    if USE_DB_FOR_RESOURCES:
        with SessionLocal() as session:
            session.execute(text("DELETE FROM sites WHERE id = :id"), {"id": site_id})
            session.commit()
    else:
        # JSON-режим
        resources = load_resources_from_json()
        if 1 <= site_id <= len(resources):
            del_resource = resources.pop(site_id - 1)
            save_resources_to_json(resources)

    return {'success': True}

@app.route('/links')
def links_list():
    search = request.args.get('search', '').strip().lower()
    page = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', DEFAULT_PER_PAGE, type=int)
    if per_page not in PER_PAGE_OPTIONS:
        per_page = DEFAULT_PER_PAGE

    if USE_DB_FOR_ARTICLES:
        # БД-режим с пагинацией и серверным поиском
        with SessionLocal() as session:
            if search:
                total_query = text("SELECT COUNT(*) FROM links WHERE LOWER(title) LIKE :search")
                total = session.execute(total_query, {"search": f"%{search}%"}).scalar() or 0
                result_query = text("""
                    SELECT l.id, l.title, l.url, l.is_send, l.created_at,
                           COALESCE(s.site_key, '—') as site_name
                    FROM links l
                    LEFT JOIN sites s ON l.site_id = s.id
                    WHERE LOWER(l.title) LIKE :search
                    ORDER BY l.created_at DESC
                    LIMIT :limit OFFSET :offset
                """)
            else:
                total_query = text("SELECT COUNT(*) FROM links")
                total = session.execute(total_query).scalar() or 0
                result_query = text("""
                    SELECT l.id, l.title, l.url, l.is_send, l.created_at,
                           COALESCE(s.site_key, '—') as site_name
                    FROM links l
                    LEFT JOIN sites s ON l.site_id = s.id
                    ORDER BY l.created_at DESC
                    LIMIT :limit OFFSET :offset
                """)

            pagination = get_pagination(total, page, per_page)
            links_count = total

            result = session.execute(result_query, {"limit": per_page, "offset": pagination['offset'], "search": f"%{search}%"} if search else {"limit": per_page, "offset": pagination['offset']}).fetchall()

            links = [dict(row) for row in result]
            per_page_options = PER_PAGE_OPTIONS
    else:
        # JSON-режим
        all_links = get_all_links()
        all_links.sort(key=lambda l: l['created_at'] or datetime.min, reverse=True)
        links_count = len(all_links)
        pagination = get_pagination(links_count, page, per_page)
        start = pagination['offset']
        links = all_links[start:start + per_page]          # ← исправлено
        per_page_options = PER_PAGE_OPTIONS

    return render_template_string(LINKS_HTML,
                                  links=links,
                                  pagination=pagination,
                                  per_page_options=per_page_options,
                                  links_count=links_count,
                                  NAVIGATION_BAR=get_navigation_bar('links'),
                                  USE_DB_FOR_ARTICLES=USE_DB_FOR_ARTICLES,
                                  search=search)

@app.route('/links/delete', methods=['POST'])
def delete_link():
    data = request.get_json()
    site_name = data.get('site_name')
    url = data.get('url')

    if not (site_name and url):
        return {'error': 'no params'}, 400

    if USE_DB_FOR_ARTICLES:
        with SessionLocal() as session:
            session.execute(text("DELETE FROM links WHERE url = :url"), {"url": url})
            session.commit()
    else:
        last_results = load_last_results()
        if site_name in last_results:
            last_results[site_name] = [art for art in last_results[site_name] if art['url'] != url]
            save_last_results(last_results)

    return {'success': True}


@app.route('/debug', methods=['GET', 'POST'])
def debug():
    logger.info("Запрос к /debug")
    url = ''
    error = None
    html = None
    html_length = 0
    clean_assets = False

    is_ajax = request.form.get('ajax') == 'true'

    if request.method == 'POST':
        url = request.form['url'].strip()
        clean_assets = 'clean_assets' in request.form
        try:
            raw_html = asyncio.run(get_page_html(url))
            if 'Ошибка' in raw_html:
                error = raw_html
            else:
                if clean_assets:
                    soup = BeautifulSoup(raw_html, 'lxml')
                    for tag in soup(['script', 'style', 'link', 'meta', 'noscript', 'iframe', 'svg']):
                        tag.decompose()
                    html = soup.prettify()
                else:
                    html = BeautifulSoup(raw_html, 'lxml').prettify()
                html_length = len(html)
        except Exception as e:
            error = f"Ошибка получения HTML: {str(e)}"

        if is_ajax:
            # Возвращаем только фрагмент для #debugResult
            if error:
                return '<div class="error">' + error + '</div>'
            if html:
                escaped_html = escape(html)
                return f'''
<div class="toolbar">
    <button class="copy-btn" onclick="copyHTML()">📋 Скопировать</button>
    <span>Длина: {html_length} символов</span>
</div>
<pre id="htmlCode">{escaped_html}</pre>
'''
            return ''

    # Для обычного GET/POST возвращаем полный шаблон
    return render_template_string(DEBUG_HTML,
                                  NAVIGATION_BAR=get_navigation_bar('debug'),
                                  url=url,
                                  error=error,
                                  html=html,
                                  html_length=html_length,
                                  clean_assets=clean_assets)


if __name__ == '__main__':
    logger.info("=== ЗАПУСК ПАРСЕРА (Flask + Async Scheduler) ===")
    asyncio.run(main())
