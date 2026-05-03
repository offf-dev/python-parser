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