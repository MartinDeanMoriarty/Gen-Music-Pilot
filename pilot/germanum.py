"""Spell German numbers out in words.

The TTS engine speaks written-out numbers far more reliably than digits, so the
moderation and story prompts carry phrases like "Dreizehn Uhr und Zwoelf
Minuten" instead of "13:12". The pre-rebuild workflows did this with a custom
ComfyUI node; now it is here.

Public helpers: :func:'cardinal', :func:'ordinal', :func:'spell_year',
:func:'duration_words', :func:'clock_words', :func:'date_words'. All return
Capitalised words to match the workflow examples ("Zwei Minuten und Fuenfzehn").
"""

from __future__ import annotations

from datetime import datetime

_ONES = [
    "null", "eins", "zwei", "drei", "vier", "fünf", "sechs", "sieben", "acht",
    "neun", "zehn", "elf", "zwölf", "dreizehn", "vierzehn", "fünfzehn",
    "sechzehn", "siebzehn", "achtzehn", "neunzehn",
]
_TENS = {
    2: "zwanzig", 3: "dreißig", 4: "vierzig", 5: "fünfzig", 6: "sechzig",
    7: "siebzig", 8: "achtzig", 9: "neunzig",
}
_ORD_IRREGULAR = {1: "erste", 3: "dritte", 7: "siebte", 8: "achte"}
_MONTHS = [
    "", "Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August",
    "September", "Oktober", "November", "Dezember",
]


def _card_lower(n: int) -> str:
    if n < 0:
        return "minus " + _card_lower(-n)
    if n < 20:
        return _ONES[n]
    if n < 100:
        tens, ones = divmod(n, 10)
        if ones == 0:
            return _TENS[tens]
        return f"{'ein' if ones == 1 else _ONES[ones]}und{_TENS[tens]}"
    if n < 1000:
        hundreds, rest = divmod(n, 100)
        head = ("ein" if hundreds == 1 else _ONES[hundreds]) + "hundert"
        return head if rest == 0 else head + _card_lower(rest)
    if n < 1_000_000:
        thousands, rest = divmod(n, 1000)
        head = ("ein" if thousands == 1 else _card_lower(thousands)) + "tausend"
        return head if rest == 0 else head + _card_lower(rest)
    raise ValueError(f"number out of range: {n}")


def cardinal(n: int) -> str:
    """1 -> 'Eins', 26 -> 'Sechsundzwanzig'."""
    return _card_lower(n).capitalize()


def ordinal(n: int) -> str:
    """1 -> 'Erste', 11 -> 'Elfte', 21 -> 'Einundzwanzigste'."""
    if n in _ORD_IRREGULAR:
        return _ORD_IRREGULAR[n].capitalize()
    if n < 20:
        return (_ONES[n] + "te").capitalize()
    return (_card_lower(n) + "ste").capitalize()


def spell_year(year: int) -> str:
    """2026 -> 'Zweitausendsechsundzwanzig' (one word, as German writes it)."""
    return cardinal(year)


def duration_words(seconds: int) -> str:
    """135 -> 'Zwei Minuten und Fuenfzehn Sekunden'."""
    minutes, secs = divmod(max(0, int(seconds)), 60)
    if secs == 0:
        return f"{cardinal(minutes)} Minuten"
    return f"{cardinal(minutes)} Minuten und {cardinal(secs)} Sekunden"


def clock_words(when: datetime) -> str:
    """13:12 -> 'Dreizehn Uhr und Zwoelf Minuten'."""
    if when.minute == 0:
        return f"{cardinal(when.hour)} Uhr"
    return f"{cardinal(when.hour)} Uhr und {cardinal(when.minute)} Minuten"


def date_words(when: datetime) -> str:
    """2026-03-11 -> 'Elfter März Zweitausendsechsundzwanzig'.

    Day as a masculine ordinal, month by name, year as one word -- the TTS
    voice (qwen3.5 upstream) mangles "Elfte Dritte 2026" but speaks this fine.
    """
    return f"{ordinal(when.day)}r {_MONTHS[when.month]} {spell_year(when.year)}"
