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


# Языки, на которых langdetect срабатывает УВЕРЕННО при реальном не-английском
# тексте. Если top-1 равен любому из этого списка — это не английский, точка.
# (Список — пересечение основных мировых + европейских + соседей.)
_RELIABLY_NON_EN = frozenset({
    "fr", "es", "pt", "it", "ca", "de", "nl", "ro", "pl", "cs", "sk", "hu",
    "ru", "uk", "be", "bg", "mk", "sr", "hr", "sl",
    "el", "tr", "ar", "fa", "he", "hi", "ur", "bn", "ta", "te",
    "ja", "zh-cn", "zh-tw", "ko", "vi", "th", "ms",
    "fi", "et", "lv", "lt",
})

# А вот эти labels langdetect выдаёт ЛОЖНО для коротких/технических английских
# заголовков (типа 'CSS Grid is awesome' → 'af'). Для них применяем ASCII-фоллбэк
# вместо строгой блокировки. (Это явно не основные источники не-английских постов
# в frontend-блогах.)
# Не перечисляем — просто всё, что не 'en' и не в _RELIABLY_NON_EN, считаем
# «спорным» и отдаём в ASCII-проверку.


def is_english(title: str) -> bool:
    """Строгий режим: блокирует уверенно определённые не-английские языки.

    **Отступление** от docs/parser.md §4.2 (там было мягче — top-3 ИЛИ pure-ASCII).
    Логика:
      1) Эмодзи и спец-символы стрипаем перед детекцией (мешают langdetect).
      2) langdetect → top-1 == 'en' → pass.
      3) top-1 ∈ {fr, es, de, ru, ja, …} (известные надёжные не-en детекции) → reject.
      4) Иначе (langdetect выдал 'af'/'id'/'so' для технического английского,
         либо вообще упал на короткой строке) → pure-ASCII fallback.
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
            top_lang = scores[0].lang if scores else None
            if top_lang == "en":
                return True
            if top_lang in _RELIABLY_NON_EN:
                return False
            # Спорное определение — ASCII-фоллбэк
            return detect_input.isascii()
        except Exception:
            # Слишком короткое для детектора — ASCII-фоллбэк
            return detect_input.isascii()
    except ImportError:
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