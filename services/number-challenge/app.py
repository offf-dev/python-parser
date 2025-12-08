import os
import json
import asyncio
import random
import atexit

from flask import Flask, request, render_template_string
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    MessageHandler,
    filters
)
from telegram.constants import ParseMode

# ←←←←←←←←←←←←←←←← ЭТИ ДВЕ СТРОКИ БЫЛИ ПРОПУЩЕНЫ! ←←←←←←←←←←←←←←←
from hypercorn.config import Config
from hypercorn.asyncio import serve

app = Flask(__name__)

# ====================== НАСТРОЙКИ ======================
TELEGRAM_TOKEN = os.getenv("TG_BOT_TOKEN")
CHAT_ID = int(os.getenv("TG_CHAT_ID"))
DATA_FILE = 'data/triggers.json'
MEMBERS_FILE = 'data/active_members.json'

if TELEGRAM_TOKEN is None or CHAT_ID is None:
    print("[FATAL] Не заданы TG_BOT_TOKEN или TG_CHAT_ID в переменных окружения!")
    exit(1)

# ====================== УЧАСТНИКИ ======================
active_members = {}

def load_members():
    global active_members
    try:
        if os.path.exists(MEMBERS_FILE):
            with open(MEMBERS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                active_members = {int(k): v for k, v in data.items()}
            print(f"[INFO] Загружено {len(active_members)} участников")
    except Exception as e:
        print(f"[WARN] Ошибка загрузки участников: {e}")

def save_members():
    try:
        os.makedirs('data', exist_ok=True)
        with open(MEMBERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(active_members, f, ensure_ascii=False, indent=2)
        print(f"[INFO] Сохранено {len(active_members)} участников")
    except Exception as e:
        print(f"[ERROR] Не удалось сохранить участников: {e}")

atexit.register(save_members)

def add_user(user):
    if not user or user.is_bot:
        return
    mention = f"@{user.username}" if user.username else user.first_name
    user_id = user.id

    # Если пользователь новый — сразу сохраняем на диск
    if user_id not in active_members:
        print(f"[ADD] Новый участник: {mention} ({user_id})")
        active_members[user_id] = {"mention": mention, "name": user.full_name or "Аноним"}
        save_members_now()  # ←←← ВОТ ЭТО ГЛАВНОЕ
    else:
        # Даже если уже есть — обновляем mention на случай смены юзернейма
        active_members[user_id]["mention"] = mention

def save_members_now():
    """Принудительное сохранение участников прямо сейчас (без ожидания 5 минут)"""
    try:
        os.makedirs('data', exist_ok=True)
        with open(MEMBERS_FILE, 'w', encoding='utf-8') as f:
            json.dump(active_members, f, ensure_ascii=False, indent=2)
        # print(f"[SAVE] Сохранено {len(active_members)} участников")  # можно включить для дебага
    except Exception as e:
        print(f"[ERROR] Не удалось сохранить участников мгновенно: {e}")

async def get_random_mention():
    if not active_members:
        return "какого-то бедолагу"
    return random.choice(list(active_members.values()))["mention"]

load_members()

# ====================== ТРИГГЕРЫ ======================
triggers = []

def load_triggers():
    global triggers
    try:
        if os.path.exists(DATA_FILE):
            with open(DATA_FILE, 'r', encoding='utf-8') as f:
                raw = json.load(f)
                triggers = []
                for item in raw:
                    # Поддержка старого формата (без count)
                    if isinstance(item, dict):
                        triggers.append({
                            "keyword": item.get("keyword", ""),
                            "response": item.get("response", ""),
                            "count": item.get("count", 0)
                        })
                    else:
                        # старый формат — только строка
                        triggers.append({"keyword": str(item), "response": "", "count": 0})
            print(f"[INFO] Загружено {len(triggers)} триггер(ов)")
    except Exception as e:
        print(f"[ERROR] Ошибка загрузки триггеров: {e}")
        triggers = []

def save_triggers():
    try:
        os.makedirs('data', exist_ok=True)
        # Сохраняем только нужные поля
        clean = [{"keyword": t["keyword"], "response": t["response"], "count": t["count"]} for t in triggers]
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(clean, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[ERROR] Не удалось сохранить триггеры: {e}")

load_triggers()

# ====================== ОБРАБОТЧИК ======================
async def message_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    if not msg or msg.chat_id != CHAT_ID:
        return

    # Добавляем участников
    if msg.from_user:
        add_user(msg.from_user)
    if msg.reply_to_message and msg.reply_to_message.from_user:
        add_user(msg.reply_to_message.from_user)
    if msg.entities:
        for entity in msg.entities:
            if entity.user:
                add_user(entity.user)

    if not msg.text:
        return

    text_lower = " " + msg.text.lower() + " "  # добавляем пробелы для точного поиска

    triggered = False
    for i, trigger in enumerate(triggers):
        keyword = trigger["keyword"]

        # Приводим ключевое слово к нижнему регистру один раз
        kw_lower = keyword.lower()

        if kw_lower.startswith('#'):
            # Точный поиск хэштега как отдельного слова
            if f" {kw_lower} " in text_lower or text_lower.strip() == kw_lower:
                triggered = True
        else:
            # Обычное вхождение подстроки
            if kw_lower in text_lower:
                triggered = True

        if not triggered:
            continue

        # === ТРИГГЕР СРАБОТАЛ ===
        triggers[i]["count"] = triggers[i].get("count", 0) + 1
        save_triggers()

        mention = await get_random_mention()
        count = triggers[i]["count"]

#         final = f"<b>{trigger['response'].rstrip()} -> {mention}. Это уже {count}-й раз, когда кто-то сказал «{keyword}»!</b>"
        final = f"<b>{trigger['response'].rstrip()} -> {mention}</b>"

        await msg.reply_text(final, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        print(f"[TRIGGER #{count}] {keyword} → {mention}")
        break  # только один триггер за сообщение

# ====================== АВТОСОХРАНЕНИЕ ======================
async def autosave_loop():
    while True:
        await asyncio.sleep(300)
        save_members()

# ====================== ВЕБ-ИНТЕРФЕЙС ======================
HTML = '''<!DOCTYPE html>
<html lang="ru">
<head>
    <meta charset="UTF-8">
    <title>Number Challenge — Триггер-бот</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        body {font-family: system-ui, sans-serif; background:#0d1117; color:#c9d1d9; margin:0; padding:20px;}
        h1 {text-align:center; color:#58a6ff;}
        .container {max-width:1100px; margin:auto; display:flex; gap:30px; flex-wrap:wrap;}
        .card {background:#161b22; padding:25px; border-radius:12px; flex:1; min-width:300px; box-shadow:0 4px 20px rgba(0,0,0,.5);}
        input, textarea, button {width:100%; padding:14px; margin:10px 0; border-radius:8px; border:none; font-size:16px;}
        input, textarea {background:#30363d; color:#f2;color:#f0f6fc;}
        button {background:#238636; color:white; font-weight:bold; cursor:pointer;}
        button:hover {background:#2ea043;}
        .btn-small {padding:8px 16px; width:auto; display:inline-block;}
        .btn-danger {background:#da3633;}
        .trigger {background:#21262d; padding:15px; margin:10px 0; border-radius:8px; border-left:4px solid #58a6ff;}
        .status {padding:15px; border-radius:8px; margin:15px 0;}
        .success {background:#238636; color:white;}
        .error {background:#da3633; color:white;}
    </style>
</head>
<body>
    <h1>Number Challenge — Триггер-бот</h1>
    <div class="container">
        <div class="card">
            <h2>Триггеры ({{ triggers|length }})</h2>
            <button onclick="location.href='/?new=1'">+ Новый триггер</button>
            {% for t in triggers %}
            <div class="trigger">
                <b>Слово:</b> <code>{{ t.keyword }}</code><br>
                <b>Ответ:</b> {{ t.response|replace('\n', '<br>')|safe|truncate(120) }} (юзалось {{ t.count }} раз)
                <div style="margin-top:10px;">
                    <button class="btn-small" onclick="location.href='/?edit={{ loop.index0 }}'">Изменить</button>
                    <button class="btn-small btn-danger" onclick="if(confirm('Удалить?')) location.href='/?delete={{ loop.index0 }}'">Удалить</button>
                </div>
            </div>
            {% endfor %}
        </div>

        <div class="card">
            <h2>{% if trigger %}Редактировать{% else %}Новый триггер{% endif %}</h2>
            <form method="post">
                {% if edit_index is not none %}
                    <input type="hidden" name="edit_index" value="{{ edit_index }}">
                {% endif %}
                <input name="keyword" placeholder="слово-триггер" value="{{ trigger.keyword if trigger else '' }}" required autocomplete="off" onfocus="this.value = this.value;">
                <textarea name="response" placeholder="ответ бота (можно HTML)" rows="8" required>{{ trigger.response if trigger else '' }}</textarea>
                <button type="submit">Сохранить</button>
                <button type="button" onclick="location.href='/'">Отмена</button>
            </form>
            {% if success %}<div class="status success">{{ success }}</div>{% endif %}
            {% if error %}<div class="status error">{{ error }}</div>{% endif %}
        </div>
    </div>
</body>
</html>'''

@app.route('/', methods=['GET', 'POST'])
def index():
    global triggers
    success = error = None
    trigger = None
    edit_index = None

    if request.method == 'GET':
        delete_idx = request.args.get('delete')
        if delete_idx is not None:
            try:
                idx = int(delete_idx)
                if 0 <= idx < len(triggers):
                    deleted = triggers.pop(idx)
                    save_triggers()
                    success = f"Удалён триггер «{deleted['keyword']}»"
            except:
                error = "Ошибка удаления"

        edit_idx = request.args.get('edit')
        if edit_idx is not None:
            try:
                idx = int(edit_idx)
                if 0 <= idx < len(triggers):
                    trigger = triggers[idx]
                    edit_index = idx
            except:
                error = "Ошибка редактирования"

    if request.method == 'POST':
        keyword = request.form.get('keyword', '').strip()
        response = request.form.get('response', '').strip()
        edit_idx = request.form.get('edit_index')  # может быть None, '' или число или даже 'None'

        if not keyword or not response:
            error = "Заполните все поля"
        else:
            try:
                if edit_idx and edit_idx.isdigit():
                    idx = int(edit_idx)
                    old_count = triggers[idx].get("count", 0)  # сохраняем старый счётчик
                    triggers[idx] = {
                        "keyword": keyword,
                        "response": response,
                        "count": old_count
                    }
                    success = "Триггер обновлён"
                else:
                    triggers.append({
                        "keyword": keyword,
                        "response": response,
                        "count": 0
                    })
                    success = "Триггер добавлен"

                save_triggers()
            except Exception as e:
                print(f"[FATAL] Ошибка при сохранении триггера: {e}")
                error = "Не удалось сохранить триггер"

    load_triggers()
    return render_template_string(HTML, triggers=triggers, trigger=trigger,
                                  edit_index=edit_index, success=success, error=error,
                                  members_count=len(active_members))

# ====================== ЗАПУСК ======================
async def start_bot():
    application = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, message_handler))

    await application.initialize()
    await application.start()
    print(f"[INFO] Бот запущен и следит за чатом {CHAT_ID}")

#     try:
#         await application.bot.send_message(CHAT_ID, f"Ну шо вы, бродяги?")
#     except Exception as e:
#         print(f"[WARN] Не удалось отправить приветствие: {e}")

    asyncio.create_task(autosave_loop())
    await application.updater.start_polling(drop_pending_updates=True)
    await asyncio.sleep(999999999)

async def main():
    config = Config()
    config.bind = ["0.0.0.0:5000"]

    print("=== ТРИГГЕР-БОТ С РАНДОМ-ЖЕРТВОЙ — ЗАПУСКАЕМСЯ ===")
    await asyncio.gather(
        start_bot(),
        serve(app, config)
    )

if __name__ == '__main__':
    asyncio.run(main())
