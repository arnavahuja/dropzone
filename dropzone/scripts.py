"""Writing-system detection for `read_sign`.

The player is shown what a sign looks like, not what it says: which script it is
written in, how long the name is, whether it carries diacritics, and the first
two characters. That is enough to narrow a continent without naming a place.
"""

from __future__ import annotations

import unicodedata

# Unicode block prefix (as `unicodedata.name()` reports it) -> script label.
SCRIPT_BY_PREFIX = {
    "LATIN": "Latin",
    "CYRILLIC": "Cyrillic",
    "GREEK": "Greek",
    "ARABIC": "Arabic",
    "HEBREW": "Hebrew",
    "DEVANAGARI": "Devanagari",
    "BENGALI": "Bengali",
    "GURMUKHI": "Gurmukhi",
    "GUJARATI": "Gujarati",
    "ORIYA": "Odia",
    "TAMIL": "Tamil",
    "TELUGU": "Telugu",
    "KANNADA": "Kannada",
    "MALAYALAM": "Malayalam",
    "SINHALA": "Sinhala",
    "THAI": "Thai",
    "LAO": "Lao",
    "TIBETAN": "Tibetan",
    "MYANMAR": "Myanmar",
    "GEORGIAN": "Georgian",
    "ARMENIAN": "Armenian",
    "ETHIOPIC": "Ethiopic",
    "CHEROKEE": "Cherokee",
    "KHMER": "Khmer",
    "MONGOLIAN": "Mongolian",
    "HANGUL": "Hangul",
    "HIRAGANA": "Hiragana",
    "KATAKANA": "Katakana",
    "CJK": "Han characters",
    "IDEOGRAPHIC": "Han characters",
    "SYRIAC": "Syriac",
    "THAANA": "Thaana",
    "NKO": "N'Ko",
    "TIFINAGH": "Tifinagh",
    "VAI": "Vai",
    "JAVANESE": "Javanese",
    "BALINESE": "Balinese",
}


def detect_script(text: str) -> str:
    """The dominant writing system of a string, as a human-readable label."""
    counts: dict[str, int] = {}
    for char in text:
        if not char.isalpha():
            continue
        try:
            name = unicodedata.name(char)
        except ValueError:
            continue
        prefix = name.split(" ")[0]
        label = SCRIPT_BY_PREFIX.get(prefix)
        if label is None and prefix in ("HALFWIDTH", "FULLWIDTH"):
            label = "Han characters"
        if label:
            counts[label] = counts.get(label, 0) + 1
    if not counts:
        return "unreadable"
    # Kana and Han mix constantly in the same name; report the mixture rather
    # than letting a coin flip pick one. Script names only: naming the language
    # would hand the player the answer.
    kana = [label for label in ("Hiragana", "Katakana") if label in counts]
    if kana and "Han characters" in counts:
        return f"{' and '.join(kana)} mixed with Han characters"
    if len(kana) == 2:
        return "Hiragana and Katakana"
    return max(counts, key=lambda key: counts[key])


def has_diacritics(text: str) -> bool:
    """True when any letter decomposes into a base plus a combining mark."""
    for char in text:
        if not char.isalpha():
            continue
        decomposed = unicodedata.normalize("NFD", char)
        if len(decomposed) > 1 and any(unicodedata.combining(part) for part in decomposed[1:]):
            return True
    return False


def describe_case(text: str) -> str:
    """Whether the script distinguishes letter case, and how the name uses it."""
    letters = [c for c in text if c.isalpha()]
    if not letters:
        return "no letters"
    if not any(c.lower() != c.upper() for c in letters):
        return "script has no upper and lower case"
    uppers = sum(1 for c in letters if c.isupper())
    if uppers == len(letters):
        return "all capitals"
    if uppers == 0:
        return "all lower case"
    return "mixed case"
