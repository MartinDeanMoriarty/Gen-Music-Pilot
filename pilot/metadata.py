"""Read artist / title / genre from a user music file.

ID3 (or whatever container) tags first -- "pilot.ffmpeg.probe" already returns
them lower-cased. When a tag is missing, fall back to the filename: strip a
leading track number and parenthetical noise, then split on " - " as
"Artist - Title"; with no dash the stem is the title and the parent folder is
the best artist guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from pilot import ffmpeg

_UNKNOWN_ARTIST = "Unbekannter Interpret"

_TRACK_NO = re.compile(r"^\d{1,3}\s*[.):\-]?\s+(?=\S)")
_NOISE = re.compile(
    r"\s*[\(\[]\s*(?:official(?:\s*(?:music\s*)?video| audio)?|lyric[s]?(?:\s*video)?"
    r"|hd|hq|4k|audio|full\s*album|explicit|clean|remaster(?:ed)?(?:\s*\d{4})?"
    r"|visualizer|mv)\s*[^\)\]]*[\)\]]",
    re.IGNORECASE,
)
_AUDIO_EXTS = {".mp3", ".m4a", ".flac", ".ogg", ".opus", ".wav", ".aac", ".wma"}


@dataclass(frozen=True)
class SongMeta:
    title: str
    artist: str
    genre: str | None = None


def is_audio_file(path: str | Path) -> bool:
    return Path(path).suffix.lower() in _AUDIO_EXTS


def read_song_meta(path: str | Path) -> SongMeta:
    path = Path(path)
    tags: dict[str, str] = {}
    try:
        tags = ffmpeg.probe(path).tags
    except ffmpeg.FFmpegError:
        pass

    title = _clean(tags.get("title"))
    artist = _clean(tags.get("artist")) or _clean(tags.get("album_artist"))
    genre = _clean(tags.get("genre"))

    if not (title and artist):
        fn_artist, fn_title = _from_filename(path)
        title = title or fn_title
        artist = artist or fn_artist

    return SongMeta(title=title, artist=artist or _UNKNOWN_ARTIST, genre=genre)


def _from_filename(path: Path) -> tuple[str, str]:
    stem = _NOISE.sub("", path.stem)
    stem = _TRACK_NO.sub("", stem, count=1)
    stem = re.sub(r"\s+", " ", stem).strip(" -_")

    parts = [p.strip() for p in stem.split(" - ") if p.strip()]
    if len(parts) >= 2:
        return parts[0], parts[-1]

    folder = path.parent.name.strip()
    artist = folder.replace("_", " ").title() if folder else _UNKNOWN_ARTIST
    return artist, stem or path.stem


def _clean(value: str | None) -> str | None:
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip()
    return cleaned or None
