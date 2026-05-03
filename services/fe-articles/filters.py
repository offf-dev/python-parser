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
# (Belt-and-suspenders: ASCII-проверка их и так отсекла бы, но явное лучше неявного.)
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

    detect_input = _EMOJI_RE.sub("", title).strip()
    if not detect_input:
        return False

    try:
        from langdetect import DetectorFactory, detect_langs

        DetectorFactory.seed = 0
        try:
            scores = detect_langs(detect_input)
            if scores and scores[0].lang in _NON_LATIN_LANGS:
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