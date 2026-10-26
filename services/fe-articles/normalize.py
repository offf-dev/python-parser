"""URL-нормализация по правилам docs/parser.md §4 шаги c, e, f.

- абсолютизация относительных через base_url
- срезаем `?...` (query) и `#...` (fragment)
- схлопываем `//+` в пути в одиночный `/`
"""

import re
from urllib.parse import urljoin, urlparse, urlunparse


_MULTI_SLASH = re.compile(r"/+")


def normalize_url(url: str, base_url: str = None) -> str:
    if base_url:
        url = urljoin(base_url, url)
    url = url.split("?", 1)[0]
    url = url.split("#", 1)[0]
    parts = urlparse(url)
    path = _MULTI_SLASH.sub("/", parts.path) if parts.path else parts.path
    return urlunparse(parts._replace(path=path))


_HAS_SCHEME = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")


def ensure_scheme(url: str, default: str = "https") -> str:
    """Дописывает схему, если её нет: `example.com/x` → `https://example.com/x`.

    Playwright на голом хосте падает не «сайт недоступен», а
    `Protocol error (Page.navigate): Cannot navigate to invalid URL` — и в
    логе это выглядит как поломка парсера, хотя человек просто не набрал
    `https://` в форме.
    """
    url = (url or "").strip()
    if not url or _HAS_SCHEME.match(url):
        return url
    return f"{default}://{url.lstrip('/')}"
