"""/debug — получить HTML страницы для анализа селекторов."""

import asyncio
from html import escape

from bs4 import BeautifulSoup
from flask import Blueprint, render_template, request

import parser
from logging_setup import get_logger


logger = get_logger()
bp = Blueprint("debug", __name__)


@bp.route("/debug", methods=["GET", "POST"])
def debug():
    logger.info("Запрос к /debug")
    url = ""
    error = None
    html = None
    html_length = 0
    clean_assets = False
    is_ajax = request.form.get("ajax") == "true"

    if request.method == "POST":
        url = request.form["url"].strip()
        clean_assets = "clean_assets" in request.form
        try:
            raw = asyncio.run(parser.get_page_html_for_debug(url))
            if "Ошибка" in raw[:30]:
                error = raw
            else:
                soup = BeautifulSoup(raw, "lxml")
                if clean_assets:
                    for tag in soup(["script", "style", "link", "meta", "noscript", "iframe", "svg"]):
                        tag.decompose()
                html = soup.prettify()
                html_length = len(html)
        except Exception as e:
            error = f"Ошибка получения HTML: {e}"

        if is_ajax:
            if error:
                return f'<div class="alert alert-error">{error}</div>'
            if html:
                return f'''
<div class="toolbar">
    <button class="copy-btn" onclick="copyHTML()">📋 Скопировать</button>
    <span>Длина: {html_length} символов</span>
</div>
<pre id="htmlCode">{escape(html)}</pre>
'''
            return ""

    return render_template(
        "debug.html",
        url=url, error=error, html=html,
        html_length=html_length, clean_assets=clean_assets,
        current_page="debug",
    )