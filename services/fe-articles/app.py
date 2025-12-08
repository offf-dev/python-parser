# app.py — полностью рабочая версия с print() вместо логов (2025)

import os
import re
import json
import threading
import asyncio
from datetime import datetime, timedelta

from flask import Flask, request, render_template_string
from bs4 import BeautifulSoup
import requests
from urllib.parse import urljoin
import pandas as pd

from urllib3.exceptions import InsecureRequestWarning
requests.packages.urllib3.disable_warnings(InsecureRequestWarning)

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Bot
from telegram.constants import ParseMode
from telegram.ext import ApplicationBuilder

# ====================== FLASK ======================
app = Flask(__name__)

# ====================== НАСТРОЙКИ ======================
TELEGRAM_TOKEN = os.getenv("TG_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = int(os.getenv("TG_CHAT_ID"))

DATA_FILE = 'data/resources.json'
LAST_RESULTS_FILE = 'data/last_results.json'

# ====================== ИНИЦИАЛИЗАЦИЯ БОТА ======================
print("Инициализация Telegram бота...")
bot_app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
bot = bot_app.bot

# ====================== ФАЙЛЫ ======================
def load_resources():
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"[INFO] Загружено {len(data)} ресурсов из {DATA_FILE}")
                return data
        print("[INFO] Файл ресурсов не найден — будет пустой список")
    except Exception as e:
        print(f"[ERROR] Ошибка загрузки ресурсов: {e}")
    return []

def save_resources(resources):
    try:
        os.makedirs('data', exist_ok=True)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(resources, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Сохранено {len(resources)} ресурсов")
    except Exception as e:
        print(f"[ERROR] Ошибка сохранения ресурсов: {e}")

def load_last_results():
    try:
        if os.path.exists(LAST_RESULTS_FILE):
            with open(LAST_RESULTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                print(f"[INFO] Загружены последние результаты парсинга ({len(data)} источников)")
                return data
    except Exception as e:
        print(f"[ERROR] Ошибка загрузки last_results: {e}")
    return {}

def save_last_results(results):
    try:
        os.makedirs('data', exist_ok=True)
        with open(LAST_RESULTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(results, f, ensure_ascii=False, indent=2)
        print("[INFO] last_results сохранены")
    except Exception as e:
        print(f"[ERROR] Ошибка сохранения last_results: {e}")

resources = load_resources()
last_results = load_last_results()

# ====================== ПАРСИНГ ======================
def parse_resource(resource, limit=20):
    try:
        print(f"[INFO] Парсим: {resource['name']} → {resource['url']}")
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        response = requests.get(resource['url'], headers=headers, timeout=20, verify=False)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'lxml')
        items = soup.select(resource['item_selector'])
        print(f"[INFO] Найдено {len(items)} элементов по селектору, берём первые {limit}")

        data = []
        for item in items[:limit]:  # ← ВОТ ГЛАВНОЕ ИЗМЕНЕНИЕ!
            title_tag = item.select_one(resource['title_selector'])
            link_tag = item.select_one(resource['link_selector'])

            title = title_tag.get_text(strip=True) if title_tag else "—"
            link = link_tag['href'] if link_tag and link_tag.has_attr('href') else None
            if link:
                link = urljoin(resource['url'], link)

            if link:
                # Очищаем заголовок от SVG и прочего мусора уже здесь (на всякий случай)
                clean_title = BeautifulSoup(title, "lxml").get_text(strip=True)
                if not clean_title:
                    clean_title = "Без заголовка"

                data.append({
                    "Заголовок": clean_title,
                    "Ссылка": f"<a href='{link}'>{link}</a>"
                })

        print(f"[INFO] Успешно спаршено {len(data)} статей (лимит: {limit}) с {resource['name']}")
        return data
    except Exception as e:
        print(f"[ERROR] ОШИБКА парсинга {resource.get('name', 'unknown')}: {e}")
        return []

# ====================== ОТПРАВКА В ТГ ======================
async def send_telegram_message(text: str):
    try:
        await bot.send_message(
            chat_id=TELEGRAM_CHANNEL_ID,
            text=text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )
        print("[INFO] Сообщение успешно отправлено в Telegram канал")
    except Exception as e:
        print(f"[ERROR] НЕ УДАЛОСЬ отправить сообщение в Telegram: {e}")

# ====================== АВТОПАРСИНГ ======================
async def send_new_articles_async():
    global resources
    print("[INFO] Запуск автопарсинга — отправляем все свежие статьи")

    resources = load_resources()
    if not resources:
        await send_telegram_message("База ресурсов пуста")
        return

    all_articles = []      # для Telegram
    new_last_results = {}  # для сохранения в файл

    for resource in resources:
        name = resource['name']
        items = parse_resource(resource, limit=20)

        resource_articles = []
        print(f"\n=== {name.upper()} ===")
        for item in items:
            # Очистка заголовка
            clean_title = BeautifulSoup(item["Заголовок"], "lxml").get_text(strip=True)
            if not clean_title:
                clean_title = "Без заголовка"

            # Извлечение URL
            href_tag = item["Ссылка"]
            match = re.search(r'href=["\']([^"\']+)["\']', href_tag)
            if not match:
                continue
            url = match.group(1)

            # Печатаем в терминал
            print(f"• {clean_title}")
            print(f"  → {url}\n")

            resource_articles.append({"title": clean_title, "url": url})
            all_articles.append({"Источник": name, "title": clean_title, "url": url})

        new_last_results[name] = resource_articles
        print(f"[INFO] Спаршено и выведено {len(resource_articles)} статей с {name}")

    # Сохраняем в новый формат
    save_last_results(new_last_results)

    # Формируем сообщение в Telegram
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
    else:
        message = "Ничего не спарсили 😔"

    await send_telegram_message(message)
    print("[INFO] Цикл автопарсинга отработал")

async def run_auto_parse():
    print("[INFO] Задача планировщика стартовала")
    await send_new_articles_async()

# ====================== ПЛАНИРОВЩИК ======================
scheduler = AsyncIOScheduler()

scheduler.add_job(
    run_auto_parse,
    trigger='interval',
    minutes=10,
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
        "Далее — каждые 10 минут ✅"
    )
    print("[INFO] Стартовое сообщение отправлено")

# ====================== ASGI + Hypercorn ======================
from hypercorn.config import Config
from hypercorn.asyncio import serve

async def run_scheduler_and_bot():
    print("[INFO] Инициализация Telegram бота...")
    await bot_app.initialize()
    await bot_app.start()

    print("[INFO] Запуск планировщика APScheduler...")
    scheduler.start()

    await send_startup_message()
    print("[INFO] Планировщик активен: первое сообщение через 30 сек, потом каждые 10 мин")

    while True:
        await asyncio.sleep(3600)

async def main():
    config = Config()
    config.bind = ["0.0.0.0:5000"]
    config.use_reloader = False
    config.worker_class = "asyncio"

    print("[INFO] Запуск Hypercorn + планировщика...")
    await asyncio.gather(
        run_scheduler_and_bot(),
        serve(app, config)
    )

# ==================== HTML + РОУТ (остаётся без изменений) ====================
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
        table { width: 100%; border-collapse: collapse; margin-top: 20px; }
        th, td { padding: 12px; text-align: left; border-bottom: 1px solid #ddd; }
        who { background: #007bff; color: white; }
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
    </script>
</body>
</html>
'''

@app.route('/', methods=['GET', 'POST'])
def index():
    print("[INFO] Запрос к веб-интерфейсу")
    global resources
    resources = load_resources()

    edit_index = request.args.get('edit', type=int)
    load_index = request.args.get('load', type=int)
    delete_index = request.args.get('delete', type=int)

    # Эти переменные будем передавать в шаблон
    resource = {}
    error = success = table = count = None

    # Удаление
    if delete_index is not None and 0 <= delete_index < len(resources):
        deleted = resources.pop(delete_index)
        save_resources(resources)
        success = f"Удалён: {deleted['name']}"

    # Редактирование / загрузка из базы
    if edit_index is not None and 0 <= edit_index < len(resources):
        resource = resources[edit_index].copy()
    elif load_index is not None and 0 <= load_index < len(resources):
        resource = resources[load_index].copy()

    if request.method == 'POST':
        action = request.form.get('action')

        # Всегда берём свежие данные из формы
        current_form = {
            "name": request.form['name'].strip(),
            "url": request.form['url'].strip(),
            "item_selector": request.form['item_selector'].strip(),
            "title_selector": request.form['title_selector'].strip(),
            "link_selector": request.form['link_selector'].strip()
        }

        # Если это редактирование — сохраняем индекс
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
            # ВАЖНО: даже если только парсим — возвращаем данные обратно в форму!
            resource = current_form

            try:
                data = parse_resource(current_form, limit=100)
                if not data:
                    error = "Ничего не найдено по указанным селекторам"
                else:
                    df = pd.DataFrame(data)
                    table = df.to_html(escape=False, index=False)
                    count = len(data)
                    success = f"Успешно спаршено {len(data)} статей!"
            except Exception as e:
                error = f"Ошибка парсинга: {str(e)}"

    return render_template_string(HTML,
                                  resources=resources,
                                  resource=resource,      # ← вот сюда попадают данные из формы
                                  edit_index=edit_index if 'edit_index' in locals() else None,
                                  error=error,
                                  success=success,
                                  table=table,
                                  count=count)

if __name__ == '__main__':
    print("=== ЗАПУСК ПАРСЕРА (Flask + Async Scheduler) ===")
    asyncio.run(main())
