"""Where a block's :class:'~pilot.domain.SongData' comes from.

"GeneratedSource" mixes a random name / artist / style from "assets/*.json"
and a random length -- the old "random_mix" plus "get_random(120, 300)".
"UserMusicSource" (build step 6) will read the same shape from MP3 tags.
"""

from __future__ import annotations

import json
import logging
import random
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pilot import ffmpeg
from pilot.config import Config, UserMusicDir
from pilot.domain import SongData
from pilot.metadata import is_audio_file, read_song_meta

log = logging.getLogger("pilot.sources")

_USER_GENRE_FALLBACK = "aus der Plattensammlung"


class SourceError(RuntimeError):
    """An asset file is missing or the wrong shape."""


class SongSource(Protocol):
    def next_song(self) -> SongData: ...


@dataclass(frozen=True)
class Assets:
    names: tuple[str, ...]
    artists: tuple[str, ...]
    genres: tuple[str, ...]

    @classmethod
    def load(cls, assets_dir: str | Path) -> Assets:
        base = Path(assets_dir)

        def _strings(filename: str) -> tuple[str, ...]:
            path = base / filename
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except FileNotFoundError as e:
                raise SourceError(f"asset not found: {path}") from e
            except json.JSONDecodeError as e:
                raise SourceError(f"{path}: invalid JSON: {e}") from e
            if not isinstance(data, list) or not all(isinstance(x, str) for x in data):
                raise SourceError(f"{path}: expected a JSON list of strings")
            items = tuple(s.strip() for s in data if s.strip())
            if not items:
                raise SourceError(f"{path}: no usable entries")
            return items

        return cls(
            names=_strings("names.json"),
            artists=_strings("artists.json"),
            genres=_strings("genres.json"),
        )


class GeneratedSource:
    def __init__(
        self,
        assets: Assets,
        *,
        length_range: tuple[int, int] = (120, 300),
        rng: random.Random | None = None,
        avoid_last_names: int = 10,
    ) -> None:
        self._assets = assets
        lo, hi = length_range
        if lo > hi:
            raise ValueError(f"length_range {length_range}: min > max")
        self._lo, self._hi = int(lo), int(hi)
        self._rng = rng or random.Random()
        self._recent: deque[str] = deque(maxlen=max(0, avoid_last_names))

    @classmethod
    def from_config(cls, config: Config, *, rng: random.Random | None = None) -> GeneratedSource:
        return cls(
            Assets.load(config.assets_dir),
            length_range=(
                config.music.min_length_seconds,
                config.music.max_length_seconds,
            ),
            rng=rng,
        )

    def next_song(self) -> SongData:
        return SongData(
            song_name=self._pick_name(),
            artist=self._rng.choice(self._assets.artists),
            genre_desc=self._rng.choice(self._assets.genres),
            length_s=self._rng.randint(self._lo, self._hi),
        )

    def _pick_name(self) -> str:
        for _ in range(20):
            name = self._rng.choice(self._assets.names)
            if name not in self._recent:
                self._recent.append(name)
                return name
        name = self._rng.choice(self._assets.names)
        self._recent.append(name)
        return name


class UserMusicSource:
    """Serves real MP3s from the configured "user_music" dirs.

    A shuffle bag means every track plays once before any repeats. "next_file"
    returns the :class:'SongData' (from tags / filename + real duration) *and*
    the path, so the producer can import that exact file.
    """

    def __init__(
        self,
        dirs: Iterable[UserMusicDir | str | Path],
        *,
        rng: random.Random | None = None,
        default_genre: str = _USER_GENRE_FALLBACK,
    ) -> None:
        self._files = _index_music_dirs(dirs)
        if not self._files:
            raise SourceError("no playable audio in the user music dir(s)")
        self._rng = rng or random.Random()
        self._default_genre = default_genre
        self._bag: list[Path] = []

    @classmethod
    def from_config(
        cls, config: Config, *, rng: random.Random | None = None
    ) -> UserMusicSource:
        return cls(config.user_music_dirs, rng=rng)

    def __len__(self) -> int:
        return len(self._files)

    def next_file(self) -> tuple[SongData, Path]:
        path = self._draw()
        meta = read_song_meta(path)
        probe = ffmpeg.probe(path)
        song = SongData(
            song_name=meta.title,
            artist=meta.artist,
            genre_desc=meta.genre or self._default_genre,
            length_s=int(round(probe.duration_s)),
        )
        return song, path

    def _draw(self) -> Path:
        if not self._bag:
            self._bag = list(self._files)
            self._rng.shuffle(self._bag)
        return self._bag.pop()


def _index_music_dirs(dirs: Iterable[UserMusicDir | str | Path]) -> list[Path]:
    files: list[Path] = []
    seen: set[Path] = set()
    for entry in dirs:
        path = Path(entry.path if isinstance(entry, UserMusicDir) else entry)
        if not path.is_dir():
            log.warning("user music dir not found: %s", path)
            continue
        found = sorted(
            p for p in path.rglob("*") if p.is_file() and is_audio_file(p)
        )
        if not found:
            log.warning("no audio files in user music dir: %s", path)
        for p in found:
            resolved = p.resolve()
            if resolved not in seen:
                seen.add(resolved)
                files.append(p)
    return files
