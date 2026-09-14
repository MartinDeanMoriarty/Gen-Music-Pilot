"""The DJ's memory and prompt assembly (feature 2).

Only moderation and story go through here. The lyrics workflow keeps its raw
song-data inputs, uninfluenced by the DJ (REBUILD_PLAN.md decision B).

This module owns:

* :class:'StationBible' -- the persona text that becomes the system prompt
* :func:'history_digest' -- a compact recap of recent blocks for the user prompt
* (task 4.2) prompt assembly, (4.3) the summariser, (4.4) the "DJBrain" facade
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pilot.domain import HistoryEntry, SongData
from pilot.germanum import clock_words, date_words, duration_words

if TYPE_CHECKING:
    from pilot.config import Config
    from pilot.store import Store

log = logging.getLogger("pilot.dj_brain")

# The canonical persona. Kept in sync with the "System Prompt" node in
# workflows/Radio_TTS.json and Radio_Story.json (config/persona.md is the source
# of truth; Python injects it at runtime). Tuned for qwen3.5: it is verbose, so
# the length is pinned; the "spoken text" rules list the characters that break
# the TTS voice rather than a single "no special characters".
DEFAULT_PERSONA = (
    "Du bist ein unterhaltsamer Radio Moderator. Der Show-Master von "
    "Gen-Music-Pilot.\nDu sagst den nächsten Song an und hast immer einen "
    "flotten Spruch bereit.\n\n"
    "Deine Ansagen sind kurz und knapp, meist zwei bis vier Sätze. Du bist "
    "frei in deiner Interpretation und musst nicht jede Information steril "
    "vorlesen. Nimm Titel, Interpret und Genre als Steilvorlage.\n\n"
    "Eine Anrufer Interaktion ist eine wunderbare Abwechslung. Wenn eine "
    "vorliegt, also nicht None, dann geh darauf ein.\n\n"
    "Rund um die halbe und die volle Stunde sagst du die Uhrzeit an. Sonst "
    "nutzt du die Zeit nur für dich, um deine Atmosphäre zu schaffen und "
    "vielleicht einen Bezug zum Song, zum Genre oder zur Jahreszeit "
    "herzustellen. Ganz wie du willst, hab einfach Spaß.\n\n"
    "Wichtig bei Uhrzeit und Datum: Du bekommst sie im Prompt exakt "
    "vorgegeben, unter „Uhrzeit:\" und „Datum:\". Übernimm sie wortwörtlich, "
    "genau wie sie dort stehen. Erfinde niemals eine eigene Uhrzeit, ein "
    "eigenes Datum oder eine ungefähre Angabe wie „halb X\", und rechne "
    "nichts um. Bist du unsicher, wie du die Angabe in den Satz einbauen "
    "sollst, lass sie einfach weg, statt etwas Falsches zu sagen.\n\n"
    "Wichtig: Schreib ausschließlich auf Deutsch. Dein Text wird direkt in "
    "Sprache umgewandelt, also schreib normalen Fließtext in ganzen Sätzen, nur "
    "mit Buchstaben, Punkten und Kommas. Keine Anführungszeichen, keine "
    "Ausrufezeichen, keine Fragezeichen, keine Klammern, keine Sternchen, keine "
    "Emojis, keine Aufzählungen, keine Überschriften. Übernimm Zahlen und "
    "Datumsangaben als Wörter, so wie sie im Text stehen."
)

_NONE_VALUES = {"", "none", "keine", "nichts"}


@dataclass(frozen=True)
class StationBible:
    name: str
    persona: str

    @classmethod
    def load(cls, *, name: str, persona_file: str | Path) -> StationBible:
        path = Path(persona_file)
        if path.is_file():
            text = path.read_text(encoding="utf-8").strip()
            if text:
                return cls(name=name, persona=text)
            log.warning("persona file %s is empty - using the built-in default", path)
        else:
            log.warning("persona file %s not found - using the built-in default", path)
        return cls(name=name, persona=DEFAULT_PERSONA)

    def system(self) -> str:
        """The persona, ready as a system prompt (it already names the show)."""
        return self.persona


def is_caller_present(value: str | None) -> bool:
    return bool(value) and value.strip().lower() not in _NONE_VALUES


def history_digest(entries: Sequence[HistoryEntry], *, limit: int = 8) -> str:
    """A short chronological recap for the DJ prompt.

    "entries" arrive newest-first (as :meth:'Store.recent_history' returns
    them); the digest lists them oldest-first so it reads like a running log,
    then appends continuity hooks pulled from each entry's "meta".
    """
    recent = list(entries)[:limit][::-1]  # oldest -> newest
    if not recent:
        return "Die Sendung hat gerade erst begonnen. Es gab noch keine Ansagen."

    lines: list[str] = [
        "Was bisher lief, nur zu deiner Info, nicht vorlesen, damit du dich "
        "nicht wiederholst:"
    ]
    greeted: list[str] = []
    gags: list[str] = []

    for e in recent:
        parts = [f"- {e.at.strftime('%H:%M')} {_song_label(e)}"]
        summary = (e.dj_summary or "").strip()
        if summary:
            parts.append(f'– du sagtest sinngemäß: „{summary}"')
        if e.had_story:
            parts.append("(Story)")
        if is_caller_present(e.caller_interaction):
            parts.append(f"(Anrufer: {e.caller_interaction.strip()})")
        lines.append(" ".join(parts))

        for name in _as_list(e.meta.get("greeted")):
            if name and name not in greeted:
                greeted.append(name)
        gag = e.meta.get("running_gag")
        if isinstance(gag, str) and gag.strip() and gag not in gags:
            gags.append(gag.strip())

    if greeted:
        lines.append("Bereits gegrüßt: " + ", ".join(greeted) + ".")
    if gags:
        lines.append("Laufende Gags: " + "; ".join(gags) + ".")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# prompt assembly (task 4.2) -- pure and deterministic
#
# The layout follows the "Prompt" nodes the owner wrote in the workflows.
# Numbers are spelled out (pilot.germanum) because the TTS engine speaks
# written-out numbers far more reliably than digits.
# --------------------------------------------------------------------------

STORY_ADDENDUM = (
    "Jetzt ist Story Time. Du darfst etwas länger reden, gern vier bis sechs "
    "Sätze, ausführlich auf das Wetter und die Jahreszeit eingehen oder dir "
    "eine kleine Geschichte ausdenken. Danach leitest du zum Song über."
)


@dataclass(frozen=True)
class Prompt:
    system: str
    user: str


def moderation_prompt(
    bible: StationBible,
    song: SongData,
    *,
    airtime: datetime,
    digest: str,
    caller: str | None = None,
    announce_time: bool = False,
) -> Prompt:
    sections = [
        f"Caller Interaction: {_caller_value(caller)}",
        _song_and_time_block(song, airtime),
    ]
    if announce_time:
        sections.append("Sag zu Beginn kurz die Uhrzeit an.")
    if digest:
        sections.append(digest)
    return Prompt(system=bible.system(), user="\n\n".join(sections))


def story_prompt(
    bible: StationBible,
    song: SongData,
    *,
    airtime: datetime,
    digest: str,
    weather: str,
    caller: str | None = None,
) -> Prompt:
    weather_line = (
        weather.strip() if weather and weather.strip() else "nicht verfügbar"
    )
    sections = [
        STORY_ADDENDUM,
        f"Caller Interaction: {_caller_value(caller)}",
        _song_and_time_block(song, airtime),
        f"Wetter: {weather_line}",
    ]
    if digest:
        sections.append(digest)
    return Prompt(system=bible.system(), user="\n\n".join(sections))


def _caller_value(caller: str | None) -> str:
    return caller.strip() if is_caller_present(caller) else "None"


def _song_and_time_block(song: SongData, airtime: datetime) -> str:
    return "\n".join(
        [
            "Nächster Song:",
            f"{song.song_name} by {song.artist}",
            f"Genre: {spoken_genre(song.genre_desc)}",
            f"Length: {duration_words(song.length_s)}",
            "",
            f"Uhrzeit: {clock_words(airtime)}",
            f"Datum: {date_words(airtime)}",
        ]
    )


def spoken_genre(genre: str) -> str:
    """Strip bpm and bare numbers from a genre string before it reaches the TTS
    prompt -- the voice mangles "96 bpm". The music/lyrics workflow keeps the
    raw genre (ACE-Step wants the bpm)."""
    g = re.sub(r"\s*,?\s*\b\d+\s*bpm\b", "", genre, flags=re.IGNORECASE)
    g = re.sub(r"\b\d+\b", "", g)
    g = re.sub(r"\s*,(\s*,)+", ",", g)
    g = re.sub(r"\s+", " ", g)
    return g.strip(" ,.")


# --------------------------------------------------------------------------
# summariser (task 4.3) -- extractive, no LLM
# --------------------------------------------------------------------------

_SUMMARY_MAX = 180
_STAGE_DIRECTION = re.compile(r"\[[^\]]*\]|\*[^*]*\*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?…])\s+")
_TRIM_CHARS = " \t\"'“”„»«"


def summarise_dj_text(
    text: str, *, max_chars: int = _SUMMARY_MAX, sentences: int = 2
) -> str:
    """Condense what the DJ said into one line for the show memory.

    Extractive: strip stage directions, collapse whitespace, take the first one
    or two sentences, cap the length at a word boundary. Safe on empty or
    garbage input (returns "").
    """
    if not text or not text.strip():
        return ""
    cleaned = _STAGE_DIRECTION.sub(" ", text)
    cleaned = " ".join(cleaned.split()).strip(_TRIM_CHARS)
    if not re.search(r"\w", cleaned):
        return ""

    parts = [p for p in _SENTENCE_SPLIT.split(cleaned) if p]
    summary = " ".join(parts[:sentences]).strip()
    if len(summary) <= max_chars:
        return summary
    return summary[:max_chars].rsplit(" ", 1)[0].rstrip(" ,;:—–-") + "…"


def _song_label(e: HistoryEntry) -> str:
    if e.song_name and e.artist:
        return f'"{e.song_name}" von {e.artist}'
    if e.song_name:
        return f'"{e.song_name}"'
    return "ein Musikstück"


def _as_list(value: object) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, (list, tuple)):
        return [str(v) for v in value]
    return []


# --------------------------------------------------------------------------
# facade (task 4.4)
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class DJContext:
    """Per-block info the DJ needs beyond the song. Built by the producer."""

    airtime: datetime
    weather: str = ""
    caller: str | None = None
    announce_time: bool = False


class DJBrain:
    """Ties the persona, the show history and the prompt builders together."""

    def __init__(
        self, bible: StationBible, store: Store, *, history_limit: int = 8
    ) -> None:
        self._bible = bible
        self._store = store
        self._history_limit = history_limit

    @classmethod
    def from_config(cls, config: Config, store: Store) -> DJBrain:
        bible = StationBible.load(
            name=config.station.name, persona_file=config.station.persona_file
        )
        return cls(bible, store)

    @property
    def bible(self) -> StationBible:
        return self._bible

    def _digest(self) -> str:
        return history_digest(
            self._store.recent_history(self._history_limit),
            limit=self._history_limit,
        )

    def moderation_for(self, song: SongData, ctx: DJContext) -> Prompt:
        return moderation_prompt(
            self._bible,
            song,
            airtime=ctx.airtime,
            digest=self._digest(),
            caller=ctx.caller,
            announce_time=ctx.announce_time,
        )

    def story_for(self, song: SongData, ctx: DJContext) -> Prompt:
        return story_prompt(
            self._bible,
            song,
            airtime=ctx.airtime,
            digest=self._digest(),
            weather=ctx.weather,
            caller=ctx.caller,
        )

    def record(
        self,
        *,
        kind: str,
        song: SongData,
        dj_text: str,
        airtime: datetime | None = None,
        caller: str | None = None,
        had_story: bool = False,
        track_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> HistoryEntry:
        """Write what just aired into the show memory. Returns the entry."""
        entry = HistoryEntry(
            kind=kind,
            at=airtime or datetime.now(),
            song_name=song.song_name,
            artist=song.artist,
            genre=song.genre_desc,
            dj_summary=summarise_dj_text(dj_text),
            caller_interaction=caller if is_caller_present(caller) else None,
            had_story=had_story,
            track_id=track_id,
            meta=dict(meta or {}),
        )
        self._store.append_history(entry)
        return entry
