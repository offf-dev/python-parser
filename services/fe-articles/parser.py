"""Парсер: Playwright fetch → BS4 extract (с фильтрами + URL-нормализацией) → orchestrator."""

import asyncio
import html as _html
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
import heartbeat
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


class BlockedByAntibot(Exception):
    """Антибот-защита (Cloudflare и т.п.) не пустила нас на страницу.

    Принципиально отличается от «селекторы не найдены»: чинится не правкой
    CSS-селекторов, а сменой IP / темпа запросов. Раньше оба случая
    сваливались в один алерт, и ежечасные ложные «❌ селекторы не найдены»
    от заблокированных ресурсов топили настоящие поломки вёрстки.
    """

    def __init__(self, reason: str, status: int = None):
        super().__init__(reason)
        self.reason = reason
        self.status = status


# Префикс error-строки, по которому run_auto_parse отличает блокировку от
# прочих ошибок в сводке цикла.
BLOCKED_PREFIX = "заблокирован антиботом"

# Статусы, на которых Cloudflare отдаёт challenge/deny вместо контента.
_BLOCK_STATUSES = {403, 429, 503}

_BLOCK_MARKERS = (
    "just a moment",                          # CF managed challenge
    "errorcode: 1015",                        # CF rate limiting
    "used cloudflare to restrict access",     # заглушка 1015/1020
    "attention required",                     # CF firewall rule (1020)
    "checking your browser before accessing",  # старый CF JS-челлендж
    "cf-browser-verification",
    "__cf_chl",                               # артефакт скрипта челленджа
)

# Потолок размера, ниже которого тело с маркером считаем заглушкой даже при
# HTTP 200. Реальные листинги в проде — от 31 КБ; заглушки CF — 6-9 КБ.
# Порог посередине, с запасом в обе стороны.
_STUB_MAX_BYTES = 20_000


def _block_marker(html_text: str) -> str:
    """Возвращает найденный маркер антибот-заглушки или None."""
    low = html_text.lower()
    for m in _BLOCK_MARKERS:
        if m in low:
            return m
    return None


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
        # try/finally критичен: при cancel от asyncio.wait_for (site_timeout
        # в parse_resource) или исключении в page.content() браузер обязан
        # закрыться, иначе chromium-сабпроцесс остаётся сиротой → зомби под
        # PID 1 → за сутки накапливалось 900+ процессов и ядро отказывало в
        # pthread_create на следующих запусках.
        try:
            context = await browser.new_context(
                extra_http_headers=headers,
                user_agent=headers["User-Agent"],
                viewport={"width": 1920, "height": 1080},
            )
            page = await context.new_page()
            for script in _STEALTH_INIT:
                await page.add_init_script(script)

            last_err = None
            status = None
            for attempt in range(retries):
                try:
                    await asyncio.sleep(random.uniform(1, 3))
                    resp = await page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)

                    # Ранняя проверка на антибот-заглушку. Делать её надо ДО
                    # wait_for_selector: на странице челленджа селектора нет и
                    # не будет, а ожидание стоит 25с + 15с networkidle + 3с —
                    # 43 секунды впустую на каждый заблокированный ресурс.
                    if resp is not None:
                        status = resp.status
                        mitigated = resp.headers.get("cf-mitigated")
                        if mitigated:
                            raise BlockedByAntibot(
                                f"Cloudflare challenge (cf-mitigated: {mitigated}, HTTP {status})",
                                status,
                            )
                        if status in _BLOCK_STATUSES:
                            marker = _block_marker(await page.content())
                            detail = f", маркер: {marker!r}" if marker else ""
                            raise BlockedByAntibot(f"HTTP {status} от антибота{detail}", status)

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
                except BlockedByAntibot:
                    # Ретраить бессмысленно и вредно: повторный стук в том же
                    # окне mitigation-таймаута только продлевает блокировку
                    # (проверено — 3 попытки с интервалом 20с не пробились,
                    # а 200с тишины дали HTTP 200 с первого раза).
                    raise
                except Exception as e:
                    last_err = e
                    logger.warning(f"goto fail attempt {attempt + 1}/{retries} for {url}: {e}")
            else:
                raise last_err

            content = await page.content()
            # Вторая линия: CF иногда отдаёт заглушку с HTTP 200, а бывает что
            # скрипт челленджа к этому моменту уже вычистил DOM (в проде это
            # выглядело как «HTML длина: 39» — пустой <html><body></body></html>).
            if len(content) < _STUB_MAX_BYTES:
                marker = _block_marker(content)
                if marker:
                    raise BlockedByAntibot(
                        f"антибот-заглушка в теле ответа (HTTP {status}, маркер: {marker!r})",
                        status,
                    )
            return content
        finally:
            try:
                await browser.close()
            except Exception as e:
                logger.warning(f"browser.close() raised (ignoring): {e}")


async def get_page_html_for_debug(url: str) -> str:
    """Упрощённая версия для /debug — без антидетекта, без ретраев."""
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"]
            )
            try:
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
                return await page.content()
            finally:
                try:
                    await browser.close()
                except Exception as e:
                    logger.warning(f"debug browser.close() raised (ignoring): {e}")
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
    """Формирует уведомление админу с доменом и всеми селекторами (docs/parser.md §4.4).

    HTML-escape всех динамических значений: Playwright кидает ошибки с
    `<launching>...</launching>` и прочими тегоподобными вставками, которые
    ломают TG parse_mode=HTML и алерт просто не доходит до logs-чата.
    """
    esc = _html.escape
    return (
        f"❌ <b>{esc(resource.get('name', 'unknown'))}</b>\n"
        f"{esc(problem)}\n\n"
        f"URL: {esc(resource.get('url', '?'))}\n"
        f"item:  <code>{esc(resource.get('item_selector', ''))}</code>\n"
        f"title: <code>{esc(resource.get('title_selector', ''))}</code>\n"
        f"link:  <code>{esc(resource.get('link_selector', ''))}</code>"
    )


def _blocked_msg(resource: dict, reason: str) -> str:
    """Алерт про антибот-блокировку — без слова «селекторы».

    Главное отличие от _admin_msg: явно сказано, что править селекторы не надо,
    иначе на этот алерт реагируют не тем действием.
    """
    esc = _html.escape
    return (
        f"🛡 <b>{esc(resource.get('name', 'unknown'))}</b> — блокировка антиботом\n"
        f"{esc(reason)}\n\n"
        f"URL: {esc(resource.get('url', '?'))}\n\n"
        f"Это <b>не</b> поломка вёрстки — селекторы править не нужно. "
        f"Лечится сменой IP (резидентный прокси) или снижением частоты запросов.\n"
        f"Повторные сообщения по этому ресурсу подавлены до смены состояния."
    )


# name → (состояние, monotonic-время последнего алерта).
# Состояния: "ok" | "blocked" | "selectors" | "error".
# Сбрасывается при рестарте контейнера — как и _ALERTED_DOMAIN_IDS выше.
_RESOURCE_STATE: dict[str, tuple[str, float]] = {}

_STATE_LABEL = {
    "blocked": "блокировка антиботом",
    "selectors": "селекторы не найдены",
    "error": "сайт недоступен",
}

# Даже при неизменном состоянии раз в N часов напоминаем — чтобы «тихо сломано»
# не превратилось в «тихо забыто». 0 = не напоминать вообще.
_ALERT_REPEAT_SEC = int(os.getenv("ALERT_REPEAT_HOURS", "12")) * 3600


async def _notify_state(resource: dict, state: str, msg: str = None):
    """Шлёт алерт только при смене состояния ресурса (или раз в ALERT_REPEAT_HOURS).

    До этого каждый цикл слал по алерту на каждый упавший ресурс: два вечно
    заблокированных сайта давали ~48 сообщений в сутки, и настоящая поломка
    вёрстки в этом потоке была неотличима от фона.
    """
    name = resource.get("name", "unknown")
    prev = _RESOURCE_STATE.get(name)
    now = time.monotonic()

    if state == "ok":
        if prev and prev[0] != "ok":
            await bot.send_notice(
                f"✅ <b>{_html.escape(name)}</b> снова парсится "
                f"(было: {_STATE_LABEL.get(prev[0], prev[0])})"
            )
        _RESOURCE_STATE[name] = ("ok", now)
        return

    if prev and prev[0] == state:
        stale = _ALERT_REPEAT_SEC and (now - prev[1]) >= _ALERT_REPEAT_SEC
        if not stale:
            logger.info(f"{name}: состояние '{state}' не изменилось — алерт подавлен")
            return

    if state == "blocked":
        await bot.send_notice(msg)
    else:
        await bot.send_log(msg)
    _RESOURCE_STATE[name] = (state, now)


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
            await _notify_state(resource, "selectors", _admin_msg(resource, problem))
            return [], problem
        logger.info(f"Спаршено {len(data)} статей с {resource['name']}")
        heartbeat.touch()
        await _notify_state(resource, "ok")
        return data, None
    except BlockedByAntibot as e:
        problem = f"{BLOCKED_PREFIX}: {e.reason}"
        logger.warning(f"БЛОКИРОВКА {resource.get('name', 'unknown')}: {e.reason}")
        await _notify_state(resource, "blocked", _blocked_msg(resource, e.reason))
        return [], problem
    except Exception as e:
        problem = f"сайт недоступен: {e}"
        logger.error(f"ОШИБКА парсинга {resource.get('name', 'unknown')}: {problem}")
        await _notify_state(resource, "error", _admin_msg(resource, problem))
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
                errors.append(f"• {res['name']}: {err}")
                continue

            known = global_known if config.USE_DB_FOR_ARTICLES else storage.get_known_urls(res["name"])
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

        elapsed = time.perf_counter() - start
        blocked_n = sum(1 for e in errors if BLOCKED_PREFIX in e)
        logger.info(
            f"Автопарсинг занял {elapsed:.2f}с | "
            f"всего новых: {len(all_new)} | ошибок: {len(errors)}"
            f" (из них блокировок: {blocked_n})"
        )
        heartbeat.touch()

        # Аудит: домены с дефолтным 🌐 и >5 статей → алерт в logs-чат раз в сутки
        await _audit_default_emoji_domains()
    except Exception as e:
        msg = f"Ошибка в автопарсинге: {e}\n{traceback.format_exc()}"
        logger.error(msg)
        await bot.send_log(msg)