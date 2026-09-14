"""Core domain model: tracks, segments, and the track state machine.

A :class:'Track' is one audio asset with a stable "id". It moves through a
guarded lifecycle::

    PLANNED --> GENERATING --> READY --> PLAYING --> PLAYED
       \\___________\\___________\\__________\\______> FAILED

:meth:'Track.to_state' is the only way to change "state"; an illegal move
raises :class:'IllegalTransition'. Entering "READY" additionally requires a
"path" and a "duration_s" -- the atomic-publish contract from
REBUILD_PLAN.md section 4, enforced here so a half-generated track can never be
handed to the timeline.

A :class:'Segment' is one playlist block: an optional talk track (moderation or
story) followed by exactly one music track.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class TrackKind(StrEnum):
    MODERATION = "moderation"
    STORY = "story"
    MUSIC_GEN = "music_gen"
    MUSIC_USER = "music_user"
    FALLBACK = "fallback"


class TrackState(StrEnum):
    PLANNED = "planned"
    GENERATING = "generating"
    READY = "ready"
    PLAYING = "playing"
    PLAYED = "played"
    FAILED = "failed"


TALK_KINDS = frozenset({TrackKind.MODERATION, TrackKind.STORY})
MUSIC_KINDS = frozenset({TrackKind.MUSIC_GEN, TrackKind.MUSIC_USER, TrackKind.FALLBACK})

_TERMINAL = frozenset({TrackState.PLAYED, TrackState.FAILED})

_ALLOWED: dict[TrackState, frozenset[TrackState]] = {
    TrackState.PLANNED: frozenset({TrackState.GENERATING, TrackState.FAILED}),
    TrackState.GENERATING: frozenset({TrackState.READY, TrackState.FAILED}),
    TrackState.READY: frozenset({TrackState.PLAYING, TrackState.FAILED}),
    TrackState.PLAYING: frozenset({TrackState.PLAYED, TrackState.FAILED}),
    TrackState.PLAYED: frozenset(),
    TrackState.FAILED: frozenset(),
}


class IllegalTransition(ValueError):
    """Raised when a Track is asked to make a state move the machine forbids."""


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class Track:
    kind: TrackKind
    id: str = field(default_factory=_new_id)
    state: TrackState = TrackState.PLANNED
    path: Path | None = None
    duration_s: float | None = None
    comfy_prompt_id: str | None = None
    error: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    meta: dict[str, Any] = field(default_factory=dict)

    # -- constructors --------------------------------------------------

    @classmethod
    def planned(cls, kind: TrackKind, **meta: Any) -> Track:
        return cls(kind=kind, state=TrackState.PLANNED, meta=dict(meta))

    @classmethod
    def ready(
        cls, kind: TrackKind, path: Path, duration_s: float, **meta: Any
    ) -> Track:
        """A track that already exists on disk (import / fallback)."""
        return cls(
            kind=kind,
            state=TrackState.READY,
            path=Path(path),
            duration_s=float(duration_s),
            meta=dict(meta),
        )

    @classmethod
    def fallback(cls, path: Path, duration_s: float, **meta: Any) -> Track:
        """A ready-to-play fallback track already on disk."""
        return cls.ready(TrackKind.FALLBACK, path, duration_s, **meta)

    # -- lifecycle ---------------------------------------------------

    def to_state(self, new_state: TrackState, *, reason: str | None = None) -> None:
        """Move to "new_state" or raise :class:'IllegalTransition'."""
        new_state = TrackState(new_state)
        if new_state not in _ALLOWED[self.state]:
            raise IllegalTransition(
                f"track {self.id[:8]} ({self.kind}): "
                f"{self.state} -> {new_state} is not allowed"
            )
        if new_state is TrackState.READY and (self.path is None or self.duration_s is None):
            raise IllegalTransition(
                f"track {self.id[:8]}: cannot become READY without path and duration_s"
            )
        if new_state is TrackState.FAILED and reason:
            self.error = reason
        self.state = new_state

    def mark_generating(self, comfy_prompt_id: str | None = None) -> None:
        if comfy_prompt_id is not None:
            self.comfy_prompt_id = comfy_prompt_id
        self.to_state(TrackState.GENERATING)

    def mark_ready(self, path: Path, duration_s: float) -> None:
        self.path = Path(path)
        self.duration_s = float(duration_s)
        self.to_state(TrackState.READY)

    def mark_failed(self, reason: str) -> None:
        self.to_state(TrackState.FAILED, reason=reason)

    # -- queries ---------------------------------------------------

    @property
    def is_terminal(self) -> bool:
        return self.state in _TERMINAL

    @property
    def is_talk(self) -> bool:
        return self.kind in TALK_KINDS

    @property
    def is_music(self) -> bool:
        return self.kind in MUSIC_KINDS

    @property
    def title(self) -> str:
        """Human label for logs / events / ICY metadata."""
        m = self.meta
        if m.get("title"):
            return str(m["title"])
        name, artist = m.get("song_name"), m.get("artist")
        if name and artist:
            return f"{name} - {artist}"
        if name:
            return str(name)
        return self.kind.value


@dataclass(slots=True)
class Segment:
    music: Track
    talk: Track | None = None
    id: str = field(default_factory=_new_id)
    planned_airtime: datetime | None = None

    def __post_init__(self) -> None:
        if self.music.kind not in MUSIC_KINDS:
            raise ValueError(
                f"segment music track must be a music kind, got {self.music.kind}"
            )
        if self.talk is not None and self.talk.kind not in TALK_KINDS:
            raise ValueError(
                f"segment talk track must be moderation/story, got {self.talk.kind}"
            )

    @property
    def tracks(self) -> tuple[Track, ...]:
        """Tracks in play order."""
        return (self.music,) if self.talk is None else (self.talk, self.music)

    @property
    def is_ready(self) -> bool:
        return all(
            t.state is TrackState.READY and t.duration_s is not None
            for t in self.tracks
        )

    @property
    def duration_s(self) -> float:
        """Sum of known track durations (unknown counted as 0)."""
        return sum(t.duration_s or 0.0 for t in self.tracks)


@dataclass(frozen=True, slots=True)
class SongData:
    """The seed a block is built from -- random for generated music, read from
    tags for user music."""

    song_name: str
    artist: str
    genre_desc: str
    length_s: int


@dataclass(slots=True)
class HistoryEntry:
    """One aired block, recorded for the DJ's rolling show memory (section 5).

    "kind" is the block kind ("gen_music", "user_music", "story", ...).
    "meta" holds free-form continuity hooks (running gags, greeted names).
    "id" is assigned by the store on insert.
    """

    kind: str
    at: datetime = field(default_factory=datetime.now)
    song_name: str | None = None
    artist: str | None = None
    genre: str | None = None
    dj_summary: str | None = None
    caller_interaction: str | None = None
    had_story: bool = False
    track_id: str | None = None
    id: int | None = None
    meta: dict[str, Any] = field(default_factory=dict)
