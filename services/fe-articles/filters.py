"""Фильтры заголовков по правилам docs/parser.md §4.2.

Пропускаем title если ВСЕ условия true:
  is_english(title) AND not has_long_number(title) AND not has_blocked_keyword(title, blocked)

is_english:
  1) langdetect → 'en' в top-3 кандидатах, ИЛИ
  2) ASCII-фоллбэк после очистки эмодзи/пунктуации.
"""

import re


# Длинное число — отсекает шум вроде ID в URL/заголовке (5+ цифр подряд).
_LONG_NUM_RE = re.compile(r"\b\d{5,}\b")

# Любой символ из НЕ-латинских письменностей. Один такой в title — это уже
# не чисто английский, режем без участия langdetect (он часто ловится на
# доминирующих английских словах в смешанных заголовках, например
# "Async Bugs（IV）：Derived State 為什麼會漂移" → en:1.00).
_NON_LATIN_SCRIPT_RE = re.compile(
    "["
    "Ͱ-Ͽ"   # Greek
    "Ѐ-ӿ"   # Cyrillic
    "Ԁ-ԯ"   # Cyrillic Supplement
    "֐-׿"   # Hebrew
    "؀-ۿ"   # Arabic
    "܀-ݏ"   # Syriac
    "ऀ-ॿ"   # Devanagari
    "ঀ-৿"   # Bengali
    "਀-੿"   # Gurmukhi
    "઀-૿"   # Gujarati
    "଀-୿"   # Oriya
    "஀-௿"   # Tamil
    "ఀ-౿"   # Telugu
    "ಀ-೿"   # Kannada
    "ഀ-ൿ"   # Malayalam
    "฀-๿"   # Thai
    "຀-໿"   # Lao
    "ༀ-࿿"   # Tibetan
    "က-႟"   # Myanmar
    "Ⴀ-ჿ"   # Georgian
    "ᄀ-ᇿ"   # Hangul Jamo
    "぀-ゟ"   # Hiragana
    "゠-ヿ"   # Katakana
    "㐀-䶿"   # CJK Extension A
    "一-鿿"   # CJK Unified Ideographs
    "ꀀ-꓏"   # Yi Syllables
    "가-힯"   # Hangul Syllables
    "]"
)


def _has_non_latin_script(text: str) -> bool:
    return bool(text and _NON_LATIN_SCRIPT_RE.search(text))

# URL-паттерны заведомо «не статья». Совпадение → отбрасываем до сохранения.
_URL_BLACKLIST = [
    re.compile(r"(?i)://(?:www\.)?linkedin\.com/in/"),  # профили, не статьи
]


def is_blacklisted_url(url: str) -> bool:
    return any(p.search(url or "") for p in _URL_BLACKLIST)

# Эмодзи и спец-блоки Unicode, которые ломают ASCII-проверку
# (см. cleanStr в Laravel-парсере).
_EMOJI_RE = re.compile(
    "["
    "\U0001F100-\U0001F1FF"   # Enclosed Alphanumeric Supplement
    "\U0001F300-\U0001F5FF"   # Misc Symbols and Pictographs
    "\U0001F600-\U0001F64F"   # Emoticons
    "\U0001F680-\U0001F6FF"   # Transport
    "\U0001F900-\U0001F9FF"   # Supplemental Symbols and Pictographs
    "☀-⛿"           # Misc Symbols
    "✀-➿"           # Dingbats
    "]+",
    flags=re.UNICODE,
)


def _strip_for_ascii_check(title: str) -> str:
    """Убираем эмодзи/спец-знаки чтобы проверить, что остаток — чистый ASCII."""
    if not title:
        return ""
    cleaned = _EMOJI_RE.sub("", title)
    # Убираем также нелатинскую пунктуацию и неразрывные пробелы — они тоже не-ASCII.
    cleaned = re.sub(r"[  -⁯⸀-⹿]", " ", cleaned)
    return cleaned.strip()


# Языки с не-латинской письменностью — langdetect определяет их надёжно,
# и ASCII-фоллбэк на них всё равно не сработал бы. Жёстко режем, минуя fallback.
_NON_LATIN_LANGS = frozenset({
    "ru", "uk", "be", "bg", "mk", "sr", "ka",          # кириллица + грузинский
    "ja", "zh-cn", "zh-tw", "ko",                       # CJK
    "ar", "fa", "he", "ur",                             # арабский / иврит / фарси / урду
    "hi", "bn", "ta", "te", "gu", "kn", "ml", "pa",     # индийские
    "mr", "ne", "si",                                   # маратхи / непальский / синхала
    "th", "lo", "my", "km",                             # ЮВ Азия
    "el",                                               # греческий
    "am", "ti",                                         # эфиопские
})

# Латиничные не-английские языки, которые langdetect определяет надёжно
# при реальном тексте. Блокируем ТОЛЬКО при высокой уверенности (≥ 0.95) —
# на технических английских заголовках langdetect ставит эти языки с низкой
# уверенностью (0.4-0.85), и ASCII-фоллбэк должен их пропустить.
#
# НЕ включены: af, it, ro — langdetect стабильно ошибается на них для коротких
# технических английских заголовков.
_RELIABLY_NON_EN_LATIN = frozenset({
    "fr", "es", "pt", "de", "nl", "da", "sv", "no", "fi",
    "pl", "cs", "sk", "hu",
    "tr", "vi", "id", "tl", "ms",
    "ca",
    "hr", "sl",
})
_NON_EN_CONFIDENCE_THRESHOLD = 0.95


def is_english(title: str) -> bool:
    """По правилам docs/parser.md §4.2:
       'en' в top-3 langdetect ИЛИ pure-ASCII (после эмодзи).

    Дополнение: если top-1 — язык с не-латинской письменностью, режем сразу
    (это окно эффективности docs-правила всё равно ловило через ASCII, теперь явно).

    Trade-off: пропускает французский/испанский/немецкий (они на латинице).
    На наших frontend-фидах это редкость — основной не-английский шум приходит
    из CJK/кириллицы, что мы режем.
    """
    if not title:
        return False

    # Жёсткая проверка: если в строке есть хоть один НЕ-латинский символ
    # (кириллица, CJK, арабский, индийские, греческий, грузинский и т.д.) —
    # это уже не чисто английский. Защита от смешанных заголовков типа
    # 'Async Bugs（IV）：Derived State 為什麼會漂移', где langdetect видит
    # доминирующие английские слова и ставит en:1.00.
    if _has_non_latin_script(title):
        return False

    detect_input = _EMOJI_RE.sub("", title).strip()
    if not detect_input:
        return False

    word_count = len(detect_input.split())

    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        try:
            scores = detect_langs(detect_input)
            if scores:
                top = scores[0]
                # Не-латиница — режем сразу
                if top.lang in _NON_LATIN_LANGS:
                    return False
                # Уверенно определённая латиница не-английская — режем.
                # ИСКЛЮЧЕНИЕ: на 1-2-словных заголовках langdetect почти всегда
                # галлюцинирует ('JSX' → 'id(0.99)'); там детектору не доверяем.
                if (
                    word_count >= 3
                    and top.lang in _RELIABLY_NON_EN_LATIN
                    and top.prob >= _NON_EN_CONFIDENCE_THRESHOLD
                ):
                    return False
            top3 = [s.lang for s in scores[:3]]
            if "en" in top3:
                return True
        except Exception:
            pass  # короткая/смешанная — переходим к ASCII
    except ImportError:
        pass

    cleaned = _strip_for_ascii_check(title)
    return bool(cleaned) and cleaned.isascii()


def has_long_number(title: str) -> bool:
    return bool(_LONG_NUM_RE.search(title or ""))


def has_blocked_keyword(title: str, blocked_entries: list) -> bool:
    """blocked_entries: список строк. Каждая может содержать запятую — разделяет на слова.
    Сравнение регистронезависимое, подстрочное (см. docs/parser.md §4.2).
    """
    if not title or not blocked_entries:
        return False
    title_lower = title.lower()
    for entry in blocked_entries:
        if not entry:
            continue
        for word in entry.split(","):
            word = word.strip().lower()
            if word and word in title_lower:
                return True
    return False


def passes_filters(title: str, blocked_entries: list) -> tuple:
    """Прогон через все три фильтра. Возвращает (passed, reason_if_rejected)."""
    if not is_english(title):
        return False, "не английский"
    if has_long_number(title):
        return False, "содержит длинное число"
    if has_blocked_keyword(title, blocked_entries):
        return False, "стоп-слово"
    return True, None