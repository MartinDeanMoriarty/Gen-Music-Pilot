"""Live UI state, fed off the event bus.

:class:'UiState' subscribes to the timeline / producer events and keeps one
lock-guarded view of "what is on air right now" for the Gradio layer to poll on
a timer (build step 8.3). It holds only volatile, event-derived state --
durable history comes from the store and lifecycle flags from the controller
(build step 8.2).
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from pilot.events import (
    Event,
    ProducerState,
    SegmentStarted,
    TrackFinished,
    TrackStarted,
    Underrun,
)

if TYPE_CHECKING:
    from pilot.events import Bus

_RECENT_MAX = 8


@dataclass(frozen=True, slots=True)
class RecentTrack:
    title: str
    kind: str
    at: datetime
    completed: bool


@dataclass(frozen=True, slots=True)
class UnderrunInfo:
    queue_seconds: float
    detail: str
    at: datetime

    def age(self, *, now: datetime | None = None) -> timedelta:
        return (now or datetime.now()) - self.at


@dataclass(frozen=True, slots=True)
class UiView:
    """An immutable read of the live state. Every field is safe to render."""

    now_playing: str | None = None
    now_kind: str | None = None
    now_since: datetime | None = None
    now_active: bool = False
    producer_phase: str = "idle"
    detail: str = ""
    buffered_seconds: float = 0.0
    segments_aired: int = 0
    recent: tuple[RecentTrack, ...] = ()
    underrun: UnderrunInfo | None = None
    updated_at: datetime | None = None


class UiState:
    """Aggregates bus events into a single :class:'UiView'. Thread-safe."""

    def __init__(self, bus: "Bus") -> None:
        self._lock = threading.Lock()
        self._now_playing: str | None = None
        self._now_kind: str | None = None
        self._now_id: str | None = None
        self._now_since: datetime | None = None
        self._now_active = False
        self._phase = "idle"
        self._detail = ""
        self._buffered = 0.0
        self._segments = 0
        self._recent: deque[RecentTrack] = deque(maxlen=_RECENT_MAX)
        self._underrun: UnderrunInfo | None = None
        self._updated: datetime | None = None

        self._unsubs: list = []
        self._unsubs.append(bus.subscribe(SegmentStarted, self._on_segment))
        self._unsubs.append(bus.subscribe(TrackStarted, self._on_track_started))
        self._unsubs.append(bus.subscribe(TrackFinished, self._on_track_finished))
        self._unsubs.append(bus.subscribe(Underrun, self._on_underrun))
        self._unsubs.append(bus.subscribe(ProducerState, self._on_producer))

    def close(self) -> None:
        """Unsubscribe from the bus. Idempotent."""
        with self._lock:
            unsubs, self._unsubs = self._unsubs, []
        for unsub in unsubs:
            unsub()

    def snapshot(self) -> UiView:
        with self._lock:
            return UiView(
                now_playing=self._now_playing,
                now_kind=self._now_kind,
                now_since=self._now_since,
                now_active=self._now_active,
                producer_phase=self._phase,
                detail=self._detail,
                buffered_seconds=self._buffered,
                segments_aired=self._segments,
                recent=tuple(self._recent),
                underrun=self._underrun,
                updated_at=self._updated,
            )

    # -- handlers (each holds the lock) ---------------------------

    def _touch(self, event: Event) -> None:
        self._updated = event.at

    def _on_segment(self, event: SegmentStarted) -> None:
        with self._lock:
            self._segments += 1
            self._touch(event)

    def _on_track_started(self, event: TrackStarted) -> None:
        with self._lock:
            self._now_playing = event.title
            self._now_kind = event.kind
            self._now_id = event.track_id
            self._now_since = event.at
            self._now_active = True
            self._touch(event)

    def _on_track_finished(self, event: TrackFinished) -> None:
        with self._lock:
            self._recent.appendleft(
                RecentTrack(event.title, event.kind, event.at, event.completed)
            )
            if event.track_id == self._now_id:
                self._now_active = False
            self._touch(event)

    def _on_underrun(self, event: Underrun) -> None:
        with self._lock:
            self._underrun = UnderrunInfo(event.queue_seconds, event.detail, event.at)
            self._touch(event)

    def _on_producer(self, event: ProducerState) -> None:
        with self._lock:
            self._phase = event.state
            self._detail = event.detail
            self._buffered = event.buffered_seconds
            self._touch(event)
