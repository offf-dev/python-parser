"""Точка входа парсера.

Dev: Flask debug-сервер с werkzeug-reloader (без scheduler/bots).
Prod: Hypercorn + APScheduler + Telegram-боты.
"""

import asyncio
import atexit
import traceback

from asgiref.wsgi import WsgiToAsgi
from flask import Flask
from hypercorn.asyncio import serve
from hypercorn.config import Config

import bot
import config
import scheduler as sched
import storage
from logging_setup import setup_logging
from routes import register_blueprints


logger = setup_logging()

# DB-миграции при старте процесса (idempotent, no-op без БД или в READONLY)
storage.ensure_schema()


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    register_blueprints(app)
    return app


app = create_app()


# ====================== Async runner (prod) ======================
async def _run_scheduler_and_bots():
    try:
        await bot.init_bots()
        if config.ENABLE_SCHEDULER:
            logger.info("Запуск планировщика APScheduler...")
            sched.configure_jobs()
            sched.scheduler.start()
            await sched.send_startup_message()
            logger.info(
                f"Планировщик активен: парсер каждые {config.PARSER_INTERVAL_MINUTES} мин, "
                f"trickle каждые {config.SENDER_INTERVAL_MINUTES} мин"
            )
        else:
            logger.warning("Scheduler ОТКЛЮЧЁН (ENABLE_SCHEDULER=false)")

        while True:
            await asyncio.sleep(3600)
    except Exception as e:
        msg = f"Критическая ошибка в _run_scheduler_and_bots: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        await bot.send_log(msg)


async def _main_prod():
    try:
        cfg = Config()
        cfg.bind = [f"0.0.0.0:{config.PORT}"]
        cfg.use_reloader = False
        cfg.worker_class = "asyncio"
        # Flask — это WSGI; встроенный Hypercorn-WSGI-мост падает с
        # UnexpectedMessageError на завершении ответа. asgiref.WsgiToAsgi
        # делает то же самое, но без бага.
        asgi_app = WsgiToAsgi(app)
        logger.info("Запуск Hypercorn + scheduler...")
        await asyncio.gather(
            _run_scheduler_and_bots(),
            serve(asgi_app, cfg),
        )
    except Exception as e:
        msg = f"Критическая ошибка в _main_prod: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        await bot.send_log(msg)


@atexit.register
def _on_exit():
    # Слать в Telegram отсюда нельзя — bot привязан к закрывающемуся event loop.
    logger.info("Парсер завершается")


if __name__ == "__main__":
    if config.is_dev_mode():
        logger.info(f"=== DEV: Flask debug на :{config.PORT} (scheduler/bots отключены) ===")
        app.run(host="0.0.0.0", port=config.PORT, debug=True, use_reloader=True)
    else:
        logger.info("=== PROD: Hypercorn + Async Scheduler ===")
        asyncio.run(_main_prod())