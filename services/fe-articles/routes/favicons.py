"""Favicon proxy + on-disk cache.

Сценарий: на /sites рендерится <img src="/favicon/<domain>">, первый запрос
для нового домена ходит на сайт, парсит <link rel="icon"> в <head>, скачивает
файл и кладёт в static/icons/. Дальше отдаётся из кэша.
"""

import os
import re
import time
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from flask import Blueprint, send_from_directory

from logging_setup import get_logger


logger = get_logger()
bp = Blueprint("favicons", __name__)

CACHE_DIR = "static/icons"
CACHE_TTL_SECONDS = 30 * 24 * 3600  # 30 дней
ALLOWED_EXTS = {"ico", "png", "svg", "jpg", "jpeg", "gif", "webp"}

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

    for cand in candidate_urls:
        try:
            ext = urlparse(cand).path.rsplit(".", 1)[-1].lower().split("?")[0]
            if ext not in ALLOWED_EXTS:
                ext = "ico"
            resp = requests.get(cand, headers=_HEADERS, timeout=5, allow_redirects=True)
            if resp.ok and resp.content and len(resp.content) > 50:
                path = os.path.join(CACHE_DIR, f"{safe}.{ext}")
                with open(path, "wb") as f:
                    f.write(resp.content)
                logger.info(f"favicon saved: {domain} → {path} ({len(resp.content)}B)")
                return path
        except Exception as e:
            logger.warning(f"favicon download failed {cand}: {e}")
    return None


@bp.route("/favicon/<path:domain>")
def favicon(domain: str):
    cached = _find_cached(domain)
    if not cached:
        cached = _fetch_favicon(domain)
    if cached:
        d = os.path.dirname(cached)
        f = os.path.basename(cached)
        return send_from_directory(d, f, max_age=86400)
    # Не нашли — отдаём 1×1 PNG-заглушку через 204
    return "", 204