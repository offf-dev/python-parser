"""/debug — получить HTML страницы и пошагово подобрать селекторы.

Страница работает в два этапа:

1. `POST /debug` — Playwright забирает HTML. Сырой HTML кладётся в память под
   токеном: шаги с селекторами переиспользуют его и не дёргают браузер заново
   (один заход — это 10-30 секунд).
2. `POST /debug/check` — проверка селекторов по закэшированному HTML в порядке
   блок → заголовок → ссылка. Следующее поле на странице открывается только
   если предыдущее что-то нашло, а в конце показывается то, что реально вернёт
   `parser.extract_articles` — вместе с фильтрами, нормализацией URL и лимитом.
   Смысл именно в этом: увидеть не «селектор что-то нашёл», а «вот эти статьи
   уедут в канал».
"""

import asyncio
import time
import uuid
from collections import OrderedDict
from html import escape

from bs4 import BeautifulSoup
from flask import Blueprint, jsonify, render_template, request

import config
import parser
import storage
from logging_setup import get_logger


logger = get_logger()
bp = Blueprint("debug", __name__)


# ====================== Кэш HTML между шагами ======================
# Процесс один (Hypercorn, worker_class=asyncio), поэтому обычного dict хватает.
_CACHE_TTL_SEC = 30 * 60
_CACHE_MAX = 8
_html_cache: "OrderedDict[str, dict]" = OrderedDict()


def _cache_prune():
    now = time.time()
    for token in [t for t, v in _html_cache.items() if now - v["ts"] > _CACHE_TTL_SEC]:
        _html_cache.pop(token, None)
    while len(_html_cache) > _CACHE_MAX:
        _html_cache.popitem(last=False)


def _cache_put(url: str, html: str) -> str:
    _cache_prune()
    token = uuid.uuid4().hex
    _html_cache[token] = {"url": url, "html": html, "ts": time.time()}
    return token


def _cache_get(token: str):
    _cache_prune()
    return _html_cache.get(token)


# ====================== Анализ селекторов ======================
_SAMPLES = 5
_TEXT_CUT = 90
_HTML_CUT = 1200


def _cut(text: str, limit: int) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit] + "…"


def _signature(el) -> str:
    """Грубый структурный отпечаток блока: свой тег с классами + теги прямых
    детей. Нужен, чтобы отличить «30 одинаковых карточек статей» от «20 карточек
    и 10 случайно подошедших баннеров»."""
    classes = ".".join(sorted(el.get("class") or []))
    kids = ">".join(c.name for c in el.find_all(recursive=False))
    return f"{el.name}{'.' + classes if classes else ''} [{kids or '—'}]"


def _select(soup, selector: str):
    """soup.select с человеческой ошибкой вместо стектрейса."""
    try:
        return soup.select(selector), None
    except Exception as e:
        return [], f"селектор не разобран: {e}"


def _analyze_blocks(soup, selector: str) -> dict:
    items, err = _select(soup, selector)
    if err:
        return {"ok": False, "error": err}

    notes = []
    groups = []
    if items:
        counter = OrderedDict()
        for el in items:
            counter[_signature(el)] = counter.get(_signature(el), 0) + 1
        groups = [
            {"signature": sig, "count": n, "share": round(n * 100 / len(items))}
            for sig, n in sorted(counter.items(), key=lambda kv: -kv[1])
        ]

    uniform = bool(groups) and groups[0]["share"] >= 90
    if not items:
        notes.append("Ничего не найдено. Проверь, не рендерится ли список через JS "
                     "уже после загрузки, и не сменилась ли вёрстка.")
    elif len(items) == 1:
        notes.append("Найден ровно один блок — для списка статей это подозрительно: "
                     "скорее всего селектор указывает на контейнер, а не на карточку.")
    if items and not uniform:
        notes.append(f"Блоки разной структуры: самая частая — {groups[0]['share']}%. "
                     "Селектор, похоже, цепляет что-то лишнее.")

    return {
        "ok": bool(items),
        "count": len(items),
        "uniform": uniform,
        "groups": groups[:4],
        "sample": _cut(items[0].prettify(), _HTML_CUT) if items else "",
        "notes": notes,
    }


def _analyze_title(items, selector: str) -> dict:
    found, empty, with_url, samples, texts = 0, 0, 0, [], []
    for el in items:
        try:
            tag = el.select_one(selector)
        except Exception as e:
            return {"ok": False, "error": f"селектор не разобран: {e}"}
        if tag is None:
            continue
        found += 1
        text = tag.get_text(strip=True)
        texts.append(text)
        if not text:
            empty += 1
        if "http://" in text or "https://" in text:
            with_url += 1
        if len(samples) < _SAMPLES:
            samples.append(_cut(text, _TEXT_CUT) or "(пусто)")

    notes = []
    total = len(items)
    if found == 0:
        notes.append("Заголовок не найден ни в одном блоке — селектор ищется ВНУТРИ блока, "
                     "указывать путь от корня страницы не нужно.")
    elif found < total:
        notes.append(f"Заголовок есть только в {found} блоках из {total}: "
                     "либо в списке неоднородные карточки, либо селектор слишком узкий.")
    if empty:
        notes.append(f"{empty} заголовков пустые — текст, вероятно, лежит в дочернем элементе.")
    if with_url:
        notes.append(f"В {with_url} заголовках встречается URL: get_text() склеивает текст всех "
                     "вложенных элементов, значит селектор захватывает лишнее. Возьми тот "
                     "элемент, в котором лежит только название.")
    uniq = len(set(t for t in texts if t))
    if texts and uniq == 1 and len(texts) > 1:
        notes.append("Все заголовки одинаковые — селектор указывает на общий элемент, а не на свой у каждой карточки.")

    return {"ok": found > 0, "found": found, "total": total, "empty": empty,
            "with_url": with_url, "unique": uniq, "samples": samples, "notes": notes}


def _analyze_link(items, selector: str) -> dict:
    found, relative, anchors, samples = 0, 0, 0, []
    for el in items:
        if selector == parser.THIS_ARTICLE:
            raw = el.get("href") if el.name == "a" else None
            if not raw:
                a = el.find("a", href=True)
                raw = a["href"] if a else None
        else:
            try:
                tag = el.select_one(selector)
            except Exception as e:
                return {"ok": False, "error": f"селектор не разобран: {e}"}
            raw = (tag.get("href") or tag.get("data-href")) if tag is not None else None
        if not raw:
            continue
        found += 1
        if raw.startswith("#"):
            anchors += 1
        elif not raw.startswith("http"):
            relative += 1
        if len(samples) < _SAMPLES:
            samples.append(_cut(raw, _TEXT_CUT))

    notes = []
    total = len(items)
    if found == 0:
        notes.append("Ссылка не найдена. Если сам блок — это <a>, впиши "
                     f"<code>{parser.THIS_ARTICLE}</code>.")
    elif found < total:
        notes.append(f"Ссылка есть только в {found} блоках из {total}.")
    if anchors:
        notes.append(f"{anchors} ссылок ведут на якорь (#...) — это не статьи.")
    if relative:
        notes.append(f"{relative} относительных ссылок — парсер достроит их до абсолютных "
                     "сам, это нормально.")

    return {"ok": found > 0, "found": found, "total": total, "relative": relative,
            "anchors": anchors, "samples": samples, "notes": notes}


def _final_preview(html: str, url: str, item_sel: str, title_sel: str, link_sel: str) -> dict:
    """То, что реально вернёт парсер: с фильтрами, нормализацией и лимитом."""
    try:
        blocked = storage.load_blocked_keywords()
    except Exception:
        blocked = []
    try:
        articles = parser.extract_articles(
            html, item_sel, title_sel, link_sel, base_url=url,
            limit=config.PARSE_LIMIT, blocked_keywords=blocked,
        )
    except Exception as e:
        return {"ok": False, "error": f"extract_articles упал: {e}"}
    return {
        "ok": bool(articles),
        "count": len(articles),
        "limit": config.PARSE_LIMIT,
        "items": [{"title": _cut(a["title"], _TEXT_CUT), "url": _cut(a["url"], 120)}
                  for a in articles[:20]],
    }


# ====================== Роуты ======================
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
        token = None
        try:
            raw = asyncio.run(parser.get_page_html_for_debug(url))
            if "Ошибка" in raw[:30]:
                error = raw
            else:
                # В кэш кладём именно сырой HTML: селекторы проверяются по тому
                # же дереву, с которым работает боевой парсер, а не по
                # почищенному от скриптов варианту для чтения глазами.
                token = _cache_put(url, raw)
                soup = BeautifulSoup(raw, "lxml")
                if clean_assets:
                    for tag in soup(["script", "style", "link", "meta", "noscript", "iframe", "svg"]):
                        tag.decompose()
                html = soup.prettify()
                html_length = len(html)
        except Exception as e:
            error = f"Ошибка получения HTML: {e}"

        if is_ajax:
            return jsonify({
                "ok": error is None,
                "error": error,
                "token": token,
                "url": url,
                "length": html_length,
                "html": escape(html) if html else "",
            })

    return render_template(
        "debug.html",
        url=url, error=error, html=html,
        html_length=html_length, clean_assets=clean_assets,
        this_article=parser.THIS_ARTICLE,
        current_page="debug",
    )


@bp.route("/debug/check", methods=["POST"])
def debug_check():
    """Проверка селекторов по закэшированному HTML. Этапы считаются по порядку
    и обрываются на первом же, который ничего не нашёл."""
    data = request.get_json(silent=True) or {}
    entry = _cache_get((data.get("token") or "").strip())
    if not entry:
        return jsonify({"expired": True,
                        "error": "HTML больше не в памяти — нажми «Получить HTML» заново."})

    item_sel = (data.get("item") or "").strip()
    title_sel = (data.get("title") or "").strip()
    link_sel = (data.get("link") or "").strip()

    soup = BeautifulSoup(entry["html"], "lxml")
    out = {"expired": False}

    if not item_sel:
        return jsonify(out)

    out["blocks"] = _analyze_blocks(soup, item_sel)
    if not out["blocks"].get("ok") or not title_sel:
        return jsonify(out)

    items = soup.select(item_sel)
    out["title"] = _analyze_title(items, title_sel)
    if not out["title"].get("ok") or not link_sel:
        return jsonify(out)

    out["link"] = _analyze_link(items, link_sel)
    if not out["link"].get("ok"):
        return jsonify(out)

    out["final"] = _final_preview(entry["html"], entry["url"], item_sel, title_sel, link_sel)
    out["config"] = {
        "site_url": entry["url"],
        "articles_selector": item_sel,
        "title_selector": title_sel,
        "url_selector": link_sel,
    }
    return jsonify(out)
