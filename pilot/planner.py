"""Block policy: what the next block should be.

The old "queue_manager" decided this inline with a mode 1 / mode 2 coin flip
whose second "if/else" overrode the first, so mode 2 was almost dead code and
story timing raced the clock. This is that decision, pulled out, pure, seeded,
and driven by the block's **predicted airtime** rather than "datetime.now()".

A block = an optional talk track (moderation or story) + one music track
(generated or from the user's library). :class:'BlockPlan' says which, whether
to announce the time, and whether a pending caller is consumed into this block.
"""

from __future__ import annotations

import random
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

from pilot.config import Config
from pilot.domain import HistoryEntry, TrackKind

_MUSIC_ONLY_CHANCE = 0.12
_STORY_GAP_MINUTES = 22
_HALF_HOUR_WINDOW_MINUTES = 3.0
_MIXED_USER_RATIO = 0.5
_MODES = ("generative", "user", "mixed")


@dataclass(frozen=True)
class BlockPlan:
    talk: TrackKind | None  # MODERATION | STORY | None (music only)
    music: TrackKind  # MUSIC_GEN | MUSIC_USER
    announce_time: bool
    caller: str | None  # the pending caller text, if consumed into this block

    @property
    def has_talk(self) -> bool:
        return self.talk is not None


class Planner:
    def __init__(
        self,
        mode: str = "generative",
        *,
        rng: random.Random | None = None,
        music_only_chance: float = _MUSIC_ONLY_CHANCE,
        story_gap_minutes: float = _STORY_GAP_MINUTES,
        half_hour_window_minutes: float = _HALF_HOUR_WINDOW_MINUTES,
        mixed_user_ratio: float = _MIXED_USER_RATIO,
    ) -> None:
        self._mode_lock = threading.Lock()
        self._mode = _check_mode(mode)
        self._rng = rng or random.Random()
        self._music_only_chance = music_only_chance
        self._story_gap = timedelta(minutes=story_gap_minutes)
        self._window = half_hour_window_minutes
        self._mixed_user_ratio = mixed_user_ratio

    @classmethod
    def from_config(cls, config: Config, *, rng: random.Random | None = None) -> Planner:
        return cls(config.mode, rng=rng)

    @property
    def mode(self) -> str:
        with self._mode_lock:
            return self._mode

    def set_mode(self, mode: str) -> None:
        """Change the music mode. Thread-safe (the UI calls this)."""
        checked = _check_mode(mode)
        with self._mode_lock:
            self._mode = checked

    def plan(
        self,
        *,
        airtime: datetime,
        history: list[HistoryEntry],
        pending_caller: str | None = None,
    ) -> BlockPlan:
        near_half = self._near_half_hour(airtime)
        story_recent = self._story_within(history, airtime)
        music_only_roll = self._rng.random() < self._music_only_chance

        if near_half and not story_recent:
            talk: TrackKind | None = TrackKind.STORY
        elif pending_caller or not music_only_roll:
            talk = TrackKind.MODERATION
        else:
            talk = None

        return BlockPlan(
            talk=talk,
            music=self._pick_music(),
            announce_time=near_half and talk is TrackKind.MODERATION,
            caller=pending_caller if (talk is not None and pending_caller) else None,
        )

    # -- internals --------------------------------------------

    def _near_half_hour(self, when: datetime) -> bool:
        minutes = when.minute + when.second / 60.0
        offset = minutes % 30.0
        return min(offset, 30.0 - offset) <= self._window

    def _story_within(self, history: list[HistoryEntry], ref: datetime) -> bool:
        return any(
            e.had_story and abs(ref - e.at) < self._story_gap for e in history
        )

    def _pick_music(self) -> TrackKind:
        mode = self.mode
        if mode == "user":
            return TrackKind.MUSIC_USER
        if mode == "mixed" and self._rng.random() < self._mixed_user_ratio:
            return TrackKind.MUSIC_USER
        return TrackKind.MUSIC_GEN


def _check_mode(mode: str) -> str:
    if mode not in _MODES:
        raise ValueError(f"unknown mode {mode!r} (expected one of {_MODES})")
    return mode
