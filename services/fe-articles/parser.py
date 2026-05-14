"""Парсер: Playwright fetch → BS4 extract (с фильтрами + URL-нормализацией) → orchestrator."""

import asyncio
import os
import random
import time
import traceback
import warnings

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

import bot
import config
import filters
import normalize
import storage
from logging_setup import get_logger


logger = get_logger()

parse_lock = asyncio.Lock()

# Аудит доменов без своего эмодзи — отслеживаем те, про которые уже алертили,
# чтобы не спамить тем же списком каждый цикл, но при этом сразу замечать новые
# домены, пересёкшие порог. Set сбрасывается при рестарте контейнера.
_ALERTED_DOMAIN_IDS: set[int] = set()


async def _audit_default_emoji_domains():
    needs = storage.get_domains_needing_emoji(min_articles=5)
    fresh = [d for d in needs if d["id"] not in _ALERTED_DOMAIN_IDS]
    if not fresh:
        return
    lines = [f"• <b>{d['name']}</b> — {d['articles_count']} ст." for d in fresh[:30]]
    msg = (
        f"🟢 Новых доменов без своего эмодзи (с 🌐) и ≥5 статей: {len(fresh)}\n\n"
        + "\n".join(lines)
    )
    if len(fresh) > 30:
        msg += f"\n\n…ещё {len(fresh) - 30}"
    msg += "\n\nПоставь свой эмодзи на /domains"
    await bot.send_log(msg)
    for d in fresh:
        _ALERTED_DOMAIN_IDS.add(d["id"])


_DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Referer": "https://www.google.com/",
    "Sec-CH-UA": '"Not A;Brand";v="99", "Chromium";v="121", "Google Chrome";v="121"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"Windows"',
}

_STEALTH_INIT = [
    "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });",
    "Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });",
    "Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });",
    "Object.defineProperty(navigator, 'platform', { get: () => 'Win32' });",
    "Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });",
]


async def fetch_html(url: str, *, headers=None, retries=None, timeout_ms=None,
                     wait_for_selector: str = None) -> str:
    """Headless Playwright Chromium с антидетект-настройками.

    Если задан wait_for_selector — ждём пока он появится в DOM (до 25с).
    Это надёжнее чем networkidle для JS-рендеренных листингов: гарантия что
    нужный нам контент уже сгенерирован.
    """
    headers = headers or _DEFAULT_HEADERS
    retries = retries or config.PAGE_GOTO_RETRIES
    timeout_ms = timeout_ms or config.PAGE_GOTO_TIMEOUT_MS

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", "--disable-setuid-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars", "--window-size=1920,1080",
            ],
        )
        context = await browser.new_context(
            extra_http_headers=headers,
            user_agent=headers["User-Agent"],
            viewport={"width": 1920, "height": 1080},
        )
        page = await context.new_page()
        for script in _STEALTH_INIT:
            await page.add_init_script(script)

        last_err = None
        for attempt in range(retries):
            try:
                await asyncio.sleep(random.uniform(1, 3))
                await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
                if wait_for_selector:
                    # Ждём конкретный селектор. Если не появится за 25с — попробуем
                    # networkidle и фиксированный wait как fallback.
                    try:
                        await page.wait_for_selector(wait_for_selector, timeout=25000)
                    except Exception:
                        logger.warning(
                            f"selector '{wait_for_selector}' never appeared on {url} — "
                            f"fall back to networkidle"
                        )
                        try:
                            await page.wait_for_load_state("networkidle", timeout=15000)
                        except Exception:
                            pass
                        await page.wait_for_timeout(3000)
                else:
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        await page.wait_for_timeout(5000)
                break
            except Exception as e:
                last_err = e
                logger.warning(f"goto fail attempt {attempt + 1}/{retries} for {url}: {e}")
        else:
            await browser.close()
            raise last_err

        html = await page.content()
        await browser.close()
        return html


async def get_page_html_for_debug(url: str) -> str:
    """Упрощённая версия для /debug — без антидетекта, без ретраев."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            context = await browser.new_context(extra_http_headers=_DEFAULT_HEADERS)
            page = await context.new_page()
            for attempt in range(2):
                try:
                    await page.goto(url, wait_until="domcontentloaded", timeout=120_000)
                    break
                except Exception as e:
                    if attempt == 1:
                        raise
                    logger.warning(f"debug goto retry: {e}")
            html = await page.content()
            await browser.close()
        return html
    except Exception as e:
        logger.error(f"ОШИБКА получения HTML для {url}: {e}")
        return f"Ошибка: {e}"


THIS_ARTICLE = "this_article"  # спец-значение для link_selector (docs/parser.md §3)


def extract_articles(html: str, item_selector: str, title_selector: str,
                     link_selector: str, base_url: str, limit: int = None,
                     blocked_keywords: list = None) -> list:
    """HTML + селекторы → [{title, url}].

    Применяет фильтры (docs/parser.md §4.2) и URL-нормализацию.
    Реверсирует результат (docs/parser.md §4.1) — самая свежая статья сохранится
    последней, чтобы trickle-отправка не перепрыгивала по времени.
    """
    limit = limit or config.PARSE_LIMIT
    blocked_keywords = blocked_keywords or []
    soup = BeautifulSoup(html, "lxml")
    items = soup.select(item_selector)

    out = []
    for item in items[:limit]:
        # Title
        title_tag = item.select_one(title_selector)
        title = title_tag.get_text(strip=True) if title_tag else ""

        # Link: спец-значение 'this_article' = сам элемент уже <a>
        if link_selector == THIS_ARTICLE:
            link_raw = item.get("href") if item.name == "a" else None
            if not link_raw:
                a = item.find("a", href=True)
                link_raw = a["href"] if a else None
        else:
            link_tag = item.select_one(link_selector)
            if link_tag is not None:
                # Fallback на data-href для сайтов вроде Medium, где URL живёт
                # на <div data-href="..."> вместо <a href="...">.
                link_raw = link_tag.get("href") or link_tag.get("data-href")
            else:
                link_raw = None

        if not link_raw:
            continue

        link = normalize.normalize_url(link_raw, base_url=base_url)
        if not link.startswith("http"):
            continue

        if filters.is_blacklisted_url(link):
            logger.info(f"  ✗ blacklisted URL: {link}")
            continue

        with warnings.catch_warnings(record=True):
            clean_title = BeautifulSoup(title, "lxml").get_text(strip=True)
        if not clean_title:
            continue  # без заголовка — пропускаем (Laravel так же делает)

        passed, reason = filters.passes_filters(clean_title, blocked_keywords)
        if not passed:
            logger.info(f"  ✗ filtered ({reason}): {clean_title[:80]}")
            continue

        out.append({"title": clean_title, "url": link})

    # Реверс: свежее в DOM первое, нам нужно чтобы свежее ушло в канал последним.
    out.reverse()
    return out


def _admin_msg(resource: dict, problem: str) -> str:
    """Формирует уведомление админу с доменом и всеми селекторами (docs/parser.md §4.4)."""
    return (
        f"❌ <b>{resource.get('name', 'unknown')}</b>\n"
        f"{problem}\n\n"
        f"URL: {resource.get('url', '?')}\n"
        f"item:  <code>{resource.get('item_selector', '')}</code>\n"
        f"title: <code>{resource.get('title_selector', '')}</code>\n"
        f"link:  <code>{resource.get('link_selector', '')}</code>"
    )


async def parse_resource(resource: dict, limit: int = None, blocked_keywords: list = None):
    """Полный цикл парсинга одного ресурса. Возвращает (data, error_msg)."""
    limit = limit or config.PARSE_LIMIT
    if blocked_keywords is None:
        blocked_keywords = storage.load_blocked_keywords()
    try:
        logger.info(f"Парсим: {resource['name']} → {resource['url']}")
        # Жёсткий потолок на один сайт — 5 мин. Если Playwright повис на этом
        # ресурсе, asyncio.wait_for прервёт его и парсинг пойдёт к следующему,
        # а не заблокирует event-loop на час.
        site_timeout = int(os.getenv("PARSE_SITE_TIMEOUT_SEC", "300"))
        html = await asyncio.wait_for(
            fetch_html(
                resource["url"],
                wait_for_selector=resource.get("item_selector"),
            ),
            timeout=site_timeout,
        )
        logger.info(f"HTML длина: {len(html)}")

        data = extract_articles(
            html,
            resource["item_selector"], resource["title_selector"],
            resource["link_selector"], resource["url"], limit=limit,
            blocked_keywords=blocked_keywords,
        )
        if not data:
            problem = "селекторы не найдены или все статьи отфильтрованы"
            await bot.send_log(_admin_msg(resource, problem))
            return [], problem
        logger.info(f"Спаршено {len(data)} статей с {resource['name']}")
        return data, None
    except Exception as e:
        problem = f"сайт недоступен: {e}"
        logger.error(f"ОШИБКА парсинга {resource.get('name', 'unknown')}: {problem}")
        await bot.send_log(_admin_msg(resource, problem))
        return [], problem


async def run_auto_parse():
    """Тик автопарсинга: обходит все активные ресурсы, сохраняет новые."""
    try:
        start = time.perf_counter()
        logger.info("Запуск автопарсинга")
        resources = storage.load_resources()
        if not resources:
            await bot.send_articles("База ресурсов пуста")
            return

        all_new = []
        lines = []
        errors = []

        # Один раз на цикл — экономим запросы к БД/файлу.
        blocked = storage.load_blocked_keywords()

        if config.USE_DB_FOR_ARTICLES:
            global_known = storage.get_known_urls()
        else:
            global_known = set()

        for res in resources:
            if not res.get("active", False):
                logger.info(f"{res['name']} на паузе — пропускаем")
                continue

            current = []
            err = None
            try:
                async with parse_lock:
                    current, err = await parse_resource(res, blocked_keywords=blocked)
            except Exception as e:
                err = f"Неожиданная ошибка: {e}"
                logger.error(err)
                await bot.send_log(err)

            if err:
                lines.append(f"📍 {res['name']} ❌ ошибка парсинга")
                errors.append(f"• {res['name']}: {err}")
                continue

            known = global_known if config.USE_DB_FOR_ARTICLES else storage.get_known_urls(res["name"])
            new_count = sum(1 for it in current if it["url"] not in known)

            if not current:
                lines.append(f"📍 {res['name']} (0 статей — селекторы не сработали)")
            elif new_count == 0:
                lines.append(f"📍 {res['name']} ({len(current)} статей, новых нет)")
            else:
                lines.append(f"📍 {res['name']} ({len(current)} статей, {new_count} новых)")

            new_items = [it for it in current if it["url"] not in known]
            if new_items:
                storage.save_new_articles(res["name"], new_items)
                for it in new_items:
                    all_new.append({"Источник": res["name"], **it})
                # READONLY: ничего не сохранится в БД, но пусть пользователь
                # увидит, что бы ушло в канал — preview-публикация.
                if config.READONLY_DB:
                    for it in new_items:
                        emoji = storage.get_emoji_for_url(it["url"])
                        await bot.send_articles(
                            bot.format_article(it["title"], it["url"], emoji)
                        )

        # Bulk-summary в articles-канал — это операционная сводка прогона
        # парсера для аудитории канала, не ошибка. logs-чат только для алертов.
        message = "🔥 Обновление парсинга\n\n" + "\n".join(lines) if lines else "Ничего не спарсили 😔"
        if errors:
            message += "\n\n⚠️ Проблемы при парсинге:\n" + "\n".join(errors)
        if all_new:
            if config.READONLY_DB:
                message += f"\n\n🧪 READONLY: новых {len(all_new)} (preview уже улетел в articles)"
            else:
                message += f"\n\nНовых статей: {len(all_new)} (уйдут в канал по trickle-расписанию)"
        await bot.send_summary(message)

        elapsed = time.perf_counter() - start
        logger.info(
            f"Автопарсинг занял {elapsed:.2f}с | "
            f"всего новых: {len(all_new)} | ошибок: {len(errors)}"
        )

        # Аудит: домены с дефолтным 🌐 и >5 статей → алерт в logs-чат раз в сутки
        await _audit_default_emoji_domains()
    except Exception as e:
        msg = f"Ошибка в автопарсинге: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        await bot.send_log(msg)