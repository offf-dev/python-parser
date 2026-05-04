"""Favicon proxy + on-disk cache + letter-avatar fallback.

Сценарий: на /sites рендерится <img src="/favicon/<domain>">, первый запрос
для нового домена ходит на сайт, парсит <link rel="icon"> в <head>, скачивает
файл и кладёт в static/icons/. Дальше отдаётся из кэша.

Если фавикон не получилось вытащить вообще — отдаём сгенерированный SVG-аватар:
квадрат с цветом-по-хэшу домена и заглавной буквой по центру.
"""

import hashlib
import os
import re
import time
from html import escape as html_escape
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, Response, send_from_directory

from logging_setup import get_logger


logger = get_logger()
bp = Blueprint("favicons", __name__)

CACHE_DIR = "static/icons"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 дней
ALLOWED_EXTS = {"ico", "png", "svg", "jpg", "jpeg", "gif", "webp"}

# Точечные оверрайды — когда автопарсинг <link rel=icon> даёт плохую иконку
# (например, medium.com отдаёт мелкую плоскую "M" которая теряется),
# подсовываем нужный URL вручную. Будет скачан и закэширован как обычно.
_FAVICON_OVERRIDES = {
    "medium.com": "https://uxwing.com/wp-content/themes/uxwing/download/brands-and-social-media/medium-logo-icon.png",
}

# Заголовки чтобы прикинуться браузером (некоторые сайты режут python-requests)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
}


def _safe_domain(domain: str) -> str:
    """Только буквы/цифры/точки/дефисы — для безопасного имени файла."""
    return re.sub(r"[^a-zA-Z0-9.\-]", "", domain)[:80]


def _find_cached(domain: str):
    """Вернуть путь к закэшированной иконке если есть и не протухла."""
    safe = _safe_domain(domain)
    if not safe:
        return None
    if not os.path.isdir(CACHE_DIR):
        return None
    for ext in ALLOWED_EXTS:
        path = os.path.join(CACHE_DIR, f"{safe}.{ext}")
        if os.path.isfile(path):
            age = time.time() - os.path.getmtime(path)
            if age < CACHE_TTL_SECONDS:
                return path
    return None


def _fetch_favicon(domain: str) -> str | None:
    """Скачать favicon: парсим <link rel=icon> в head; fallback на /favicon.ico.

    Возвращает путь к сохранённому файлу или None.
    """
    safe = _safe_domain(domain)
    if not safe:
        return None
    os.makedirs(CACHE_DIR, exist_ok=True)
    site_url = f"https://{domain}/"

    candidate_urls = []
    # Override берём в первую очередь — если успешно скачается, до парсинга
    # сайта не дойдём.
    if domain in _FAVICON_OVERRIDES:
        candidate_urls.append(_FAVICON_OVERRIDES[domain])
    try:
        r = requests.get(site_url, headers=_HEADERS, timeout=5, allow_redirects=True)
        if r.ok:
            soup = BeautifulSoup(r.text, "lxml")
            for link in soup.find_all("link"):
                rel = link.get("rel") or []
                if isinstance(rel, list):
                    rel = " ".join(rel).lower()
                else:
                    rel = str(rel).lower()
                if "icon" in rel and link.get("href"):
                    candidate_urls.append(urljoin(r.url, link["href"]))
    except Exception as e:
        logger.warning(f"favicon fetch html failed for {domain}: {e}")
    # Fallback: стандартный /favicon.ico
    candidate_urls.append(urljoin(site_url, "/favicon.ico"))

    _CT_TO_EXT = {
        "image/png": "png", "image/jpeg": "jpg", "image/jpg": "jpg",
        "image/svg+xml": "svg", "image/x-icon": "ico",
        "image/vnd.microsoft.icon": "ico", "image/gif": "gif", "image/webp": "webp",
    }
    for cand in candidate_urls:
        try:
            resp = requests.get(cand, headers=_HEADERS, timeout=5, allow_redirects=True)
            if not (resp.ok and resp.content and len(resp.content) > 50):
                continue
            # Сначала смотрим Content-Type ответа, потом расширение из URL
            ct = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
            if ct in _CT_TO_EXT:
                ext = _CT_TO_EXT[ct]
            else:
                ext = urlparse(cand).path.rsplit(".", 1)[-1].lower().split("?")[0]
                if ext not in ALLOWED_EXTS:
                    ext = "ico"
            path = os.path.join(CACHE_DIR, f"{safe}.{ext}")
            with open(path, "wb") as f:
                f.write(resp.content)
            logger.info(f"favicon saved: {domain} → {path} ({len(resp.content)}B, ct={ct})")
            return path
        except Exception as e:
            logger.warning(f"favicon download failed {cand}: {e}")
    return None


def _letter_avatar_svg(domain: str) -> str:
    """SVG-аватар с заглавной буквой домена и цветом по хэшу.
    Возвращается всегда — гарантирует что в /sites колонка не будет с пустыми ячейками.
    """
    letter = (domain or "?")[0].upper()
    # safe для подстановки в SVG
    letter = html_escape(letter)
    # Hue из MD5(domain) — детерминированно, одинаковый цвет на одинаковом домене
    h = int(hashlib.md5((domain or "?").encode("utf-8", errors="ignore")).hexdigest()[:6], 16) % 360
    bg = f"hsl({h}, 60%, 45%)"
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">'
        f'<rect width="100" height="100" rx="20" fill="{bg}"/>'
        f'<text x="50" y="70" font-family="Inter,system-ui,sans-serif" '
        f'font-size="60" font-weight="600" fill="#fff" text-anchor="middle">{letter}</text>'
        '</svg>'
    )


@bp.route("/favicon/<path:domain>")
def favicon(domain: str):
    cached = _find_cached(domain)
    if not cached:
        cached = _fetch_favicon(domain)
    if cached:
        d = os.path.dirname(cached)
        f = os.path.basename(cached)
        return send_from_directory(d, f, max_age=86400)
    # Fallback: letter-avatar SVG (всегда что-то возвращаем)
    return Response(
        _letter_avatar_svg(domain),
        mimetype="image/svg+xml",
        headers={"Cache-Control": "public, max-age=86400"},
    )