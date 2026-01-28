# app.py — версия с проверкой на новые статьи и логированием

import os
import re
import json
import threading
import asyncio
from datetime import datetime, timedelta
import atexit  # Для обработки выхода/краша
import traceback  # Для стека ошибок

from flask import Flask, request, render_template_string
from bs4 import BeautifulSoup, MarkupResemblesLocatorWarning
import requests
from urllib.parse import urljoin
import pandas as pd

from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.events import EVENT_JOB_ERROR  # Для listener ошибок
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder

import logging
from logging.handlers import TimedRotatingFileHandler
import warnings

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
TELEGRAM_TOKEN = os.getenv("TG_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = int(os.getenv("TG_CHAT_ID"))
PARSER_INTERVAL_MINUTES = int(os.getenv("PARSER_INTERVAL_MINUTES", 10))

DATA_FILE = 'data/resources.json'
LAST_RESULTS_FILE = 'data/last_results.json'

# Lock для файлов (чтобы избежать race conditions в async/Flask)
file_lock = threading.Lock()

# ====================== ИНИЦИАЛИЗАЦИЯ БОТА ======================
bot_app = None
bot = None

async def init_bot():
    global bot_app, bot
    try:
        logger.info("Инициализация Telegram бота...")
        bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
        bot = bot_app.bot
        await bot_app.initialize()
        await bot_app.start()
    except Exception as e:
        logger.error(f"Ошибка инициализации бота: {e}")
        raise  # Поднимем, чтобы main() поймал

# ====================== ОТПРАВКА В ТГ (с обработкой ошибок) ======================
async def send_telegram_message(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        logger.info("Сообщение успешно отправлено в Telegram канал")
    except Exception as e:
        logger.error(f"НЕ УДАЛОСЬ отправить сообщение в Telegram: {e}")

# Функция для отправки ошибки в Telegram (async)
async def send_error_to_telegram(error_msg: str):
    await send_telegram_message(f"<b>🚨 Ошибка в парсере!</b>\n\n{error_msg}\n\nПроверьте логи для деталей.")

# ====================== ФАЙЛЫ ======================
def load_resources():
    with file_lock:
        try:
            if os.path.exists(DATA_FILE):
                with open(DATA_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for res in data:
                        if 'paused' not in res:
                            res['paused'] = False
                    logger.info(f"Загружено {len(data)} ресурсов из {DATA_FILE}")
                    return data
        except Exception as e:
            logger.error(f"Ошибка загрузки ресурсов: {e}")
    return []

def save_resources(resources):
    with file_lock:
        try:
            os.makedirs('data', exist_ok=True)
            with open(DATA_FILE, 'w', encoding='utf-8') as f:
                json.dump(resources, f, ensure_ascii=False, indent=2)
            logger.info(f"Сохранено {len(resources)} ресурсов")
        except Exception as e:
            logger.error(f"Ошибка сохранения ресурсов: {e}")

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

# ====================== ПАРСИНГ ======================
def parse_resource(resource, limit=20):
    try:
        logger.info(f"Парсим: {resource['name']} → {resource['url']}")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(resource['url'], headers=headers, timeout=20, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')
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
                if not link.startswith('http'):  # Пропускаем невалидные (mailto:, js:)
                    continue

                # Очистка заголовка (только здесь, без повторов)
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

        logger.info(f"Успешно спаршено {len(data)} статей (лимит: {limit}) с {resource['name']}")
        return data
    except Exception as e:
        logger.error(f"ОШИБКА парсинга {resource.get('name', 'unknown')}: {e}")
        return []

# ====================== АВТОПАРСИНГ ======================
async def send_new_articles_async():
    try:
        global resources, last_results
        logger.info("Запуск автопарсинга — проверяем на новые статьи")

        resources = load_resources()
        last_results = load_last_results()
        if not resources:
            await send_telegram_message("База ресурсов пуста")
            return

        all_articles = []
        all_new_articles = []
        updated_last_results = last_results.copy()

        for resource in resources:
            if resource.get('paused', False):
                logger.info(f"Ресурс {resource['name']} на паузе — пропускаем")
                continue
            name = resource['name']
            current_items = parse_resource(resource, limit=50)

            known_articles = updated_last_results.get(name, [])
            known_urls = {art['url'] for art in known_articles}

            resource_articles = []
            new_items = []

            logger.info(f"\n=== {name.upper()} ===")
            for item in current_items:
                clean_title = item['title']
                url = item['url']

                logger.info(f"• {clean_title}")
                logger.info(f"  → {url}\n")

                resource_articles.append({"title": clean_title, "url": url})
                all_articles.append({"Источник": name, "title": clean_title, "url": url})

                if url not in known_urls:
                    new_items.append({"title": clean_title, "url": url})
                    all_new_articles.append({"Источник": name, "title": clean_title, "url": url})

            if new_items:
                known_articles.extend(new_items)
                updated_last_results[name] = known_articles
                logger.info(f"Добавлено {len(new_items)} новых статей в базу для {name}")

            logger.info(f"Спаршено {len(resource_articles)} статей с {name} (из них новых: {len(new_items)})")

        save_last_results(updated_last_results)

        if all_articles:
            lines = []
            current_source = None
            for art in all_articles:
                if art["Источник"] != current_source:
                    current_source = art["Источник"]
                    lines.append(f"\n<b>📍 {current_source}</b>\n")
                lines.append(f"• <a href='{art['url']}'>{art['title']}</a>")

            message = f"<b>🔥 Свежие статьи ({len(all_articles)} шт.)</b>\n"
            message += "\n".join(lines)

            if all_new_articles:
                new_lines = []
                current_source = None
                for art in all_new_articles:
                    if art["Источник"] != current_source:
                        current_source = art["Источник"]
                        new_lines.append(f"\n<b>📍 {current_source}</b>\n")
                    new_lines.append(f"• <a href='{art['url']}'>{art['title']}</a>")

                message += f"\n\n<b>Среди них новые ({len(all_new_articles)} шт.):</b>\n"
                message += "\n".join(new_lines)
        else:
            message = "Ничего не спарсили 😔"

        await send_telegram_message(message)
        logger.info("Цикл автопарсинга отработал")
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
        # Поскольку listener sync, используем coroutine_threadsafe для async отправки
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
        await init_bot()  # Инициализация с try

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

# Обработка выхода/краша (atexit — sync, так что threadsafe)
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

# ==================== HTML + РОУТ (без изменений, кроме limit в parse) ====================
HTML = '''
<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Парсер статей + Telegram уведомления</title>
    <style>
        body { font-family: Arial, sans-serif; max-width: 1200px; margin: 0 auto; padding: 20px; background: #f5f5f5; }
        h1 { color: #333; text-align: center; }
        .container { display: flex; gap: 20px; flex-wrap: wrap; }
        .left { flex: 1; min-width: 300px; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        .right { flex: 2; min-width: 300px; background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }
        input, button { width: 100%; padding: 12px; margin: 10px 0; font-size: 16px; border: 1px solid #ccc; border-radius: 4px; box-sizing: border-box; }
        button { background: #007bff; color: white; cursor: pointer; }
        button:hover { background: #0056b3; }
        .btn-small { padding: 8px 12px; font-size: 14px; width: auto; display: inline-block; margin: 0 5px; }
        .btn-danger { background: #dc3545; }
        .btn-danger:hover { background: #c82333; }
        .btn-pause { background: #ffc107; color: black; }
        .btn-pause:hover { background: #e0a800; }
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        th { background: #007bff; color: white; }
        tr:hover { background: #f1f1f1; }
        .error { color: red; background: #ffe6e6; padding: 15px; border-radius: 5px; margin: 10px 0; }
        .success { color: green; background: #e6ffe6; padding: 15px; border-radius: 5px; margin: 10px 0; }
        .resource-item { padding: 10px; margin: 10px 0; background: #f8f9fa; border-radius: 5px; border-left: 4px solid #007bff; }
    </style>
</head>
<body>
    <h1>Парсер статей + Автоуведомления в Telegram</h1>
    <div class="container">
        <div class="left">
            <h2>Сохранённые ресурсы</h2>
            <button onclick="location.href='/?new=1'">+ Новый ресурс</button>
            {% if resources %}
                {% for r in resources %}
                <div class="resource-item">
                    <strong>{{ r.name }}</strong><br>
                    <small>{{ r.url }}</small>
                    <div style="margin-top: 8px;">
                        <button class="btn-small" onclick="parseSaved({{ loop.index0 }})">Спарсить</button>
                        <button class="btn-small" onclick="editResource({{ loop.index0 }})">Редактировать</button>
                        <button class="btn-small btn-danger" onclick="deleteResource({{ loop.index0 }})">Удалить</button>
                        <button class="btn-small btn-pause" onclick="togglePause({{ loop.index0 }})">
                            {% if r.paused %}Плей{% else %}Пауза{% endif %}
                        </button>
                    </div>
                </div>
                {% endfor %}
            {% else %}
                <p>Пока нет сохранённых ресурсов</p>
            {% endif %}
        </div>

        <div class="right">
            <h2>{% if edit_index is defined %}Редактировать ресурс{% else %}Новый / Текущий ресурс{% endif %}</h2>

            <form id="parseForm" method="post">
                {% if edit_index is defined %}
                    <input type="hidden" name="edit_index" value="{{ edit_index }}">
                {% endif %}
                <input type="text" name="name" placeholder="Название ресурса" value="{{ resource.name if resource else '' }}" required>
                <input type="text" name="url" placeholder="URL страницы" value="{{ resource.url if resource else '' }}" required>
                <input type="text" name="item_selector" placeholder="Селектор айтема" value="{{ resource.item_selector if resource else '' }}" required>
                <input type="text" name="title_selector" placeholder="Селектор заголовка" value="{{ resource.title_selector if resource else '' }}" required>
                <input type="text" name="link_selector" placeholder="Селектор ссылки" value="{{ resource.link_selector if resource else '' }}" required>

                <div style="margin: 15px 0; display: flex; gap: 10px; flex-wrap: wrap;">
                    <button type="submit" name="action" value="parse" style="background: #28a745;">Парсить сейчас</button>
                    <button type="submit" name="action" value="save" style="background: #007bff;">Сохранить в базу</button>
                    <button type="button" onclick="document.getElementById('parseForm').reset(); this.form.elements['name'].focus();" style="background: #6c757d;">Очистить форму</button>
                </div>
            </form>

            {% if error %}<div class="error">{{ error }}</div>{% endif %}
            {% if success %}<div class="success">{{ success }}</div>{% endif %}

            {% if table %}
                <h3>Результат парсинга ({{ count }} статей)</h3>
                {{ table|safe }}
                <button onclick="document.getElementById('parseForm').elements['action'].value='save'; document.getElementById('parseForm').submit();">
                    Добавить этот ресурс в базу
                </button>
            {% endif %}
        </div>
    </div>

    <script>
        function parseSaved(i) { location.href = '/?load=' + i; }
        function editResource(i) { location.href = '/?edit=' + i; }
        function deleteResource(i) { if(confirm('Удалить навсегда?')) location.href = '/?delete=' + i; }
        function togglePause(i) { location.href = '/?pause=' + i; }
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
    load_index = request.args.get('load', type=int)
    delete_index = request.args.get('delete', type=int)
    pause_index = request.args.get('pause', type=int)

    resource = {}
    error = success = table = count = None

    if pause_index is not None and 0 <= pause_index < len(resources):
        resources[pause_index]['paused'] = not resources[pause_index].get('paused', False)
        save_resources(resources)
        success = f"Статус паузы для {resources[pause_index]['name']} изменён"

    if delete_index is not None and 0 <= delete_index < len(resources):
        deleted = resources.pop(delete_index)
        save_resources(resources)
        success = f"Удалён: {deleted['name']}"

    if edit_index is not None and 0 <= edit_index < len(resources):
        resource = resources[edit_index].copy()
    elif load_index is not None and 0 <= load_index < len(resources):
        resource = resources[load_index].copy()

    if request.method == 'POST':
        action = request.form.get('action')

        current_form = {
            "name": request.form['name'].strip(),
            "url": request.form['url'].strip(),
            "item_selector": request.form['item_selector'].strip(),
            "title_selector": request.form['title_selector'].strip(),
            "link_selector": request.form['link_selector'].strip(),
            "paused": False
        }

        edit_idx = request.form.get('edit_index')

        if action == "save":
            if edit_idx and edit_idx.isdigit() and int(edit_idx) < len(resources):
                old_name = resources[int(edit_idx)]['name']
                resources[int(edit_idx)] = current_form
                save_resources(resources)
                success = f"Обновлён: {old_name} → {current_form['name']}"
            else:
                resources.append(current_form)
                save_resources(resources)
                success = f"Добавлен: {current_form['name']}"

        elif action == "parse":
            resource = current_form  # Возвращаем в форму

            try:
                data = parse_resource(current_form, limit=50)  # Унифицировали limit
                if not data:
                    error = "Ничего не найдено по указанным селекторам"
                else:
                    df = pd.DataFrame([{"Заголовок": art['title'], "Ссылка": f"<a href='{art['url']}'>{art['url']}</a>"} for art in data])
                    table = df.to_html(escape=False, index=False)
                    count = len(data)
                    success = f"Успешно спаршено {len(data)} статей!"
            except Exception as e:
                error = f"Ошибка парсинга: {str(e)}"

    return render_template_string(HTML,
                                  resources=resources,
                                  resource=resource,
                                  edit_index=edit_index if 'edit_index' in locals() else None,
                                  error=error,
                                  success=success,
                                  table=table,
                                  count=count)

if __name__ == '__main__':
    logger.info("=== ЗАПУСК ПАРСЕРА (Flask + Async Scheduler) ===")
    asyncio.run(main())
