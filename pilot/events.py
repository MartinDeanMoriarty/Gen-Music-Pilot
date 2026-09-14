"""In-process event bus and the event types the components exchange.

Design:

* "Bus.publish(event)" invokes every matching handler **synchronously, on the
  caller's thread**. Handlers must be cheap and non-blocking; anything slow
  should hand off to its own queue/thread.
* Matching is by "isinstance": subscribing to :class:'Event' receives every
  event, subscribing to :class:'TrackStarted' receives only those.
* A handler that raises is logged and skipped -- it never stops the other
  handlers from being notified.
* The bus is thread-safe. The subscriber list is snapshotted under a lock and
  handlers are then called outside it, so a handler may itself subscribe,
  unsubscribe or publish without deadlocking.

Events carry plain values (ids, titles, seconds), never domain objects, so a
consumer such as the UI never needs to import the domain model.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

log = logging.getLogger("pilot.events")


@dataclass(frozen=True, slots=True, kw_only=True)
class Event:
    """Base class for everything published on the :class:'Bus'."""

    at: datetime = field(default_factory=datetime.now)


@dataclass(frozen=True, slots=True, kw_only=True)
class SegmentStarted(Event):
    segment_id: str
    has_talk: bool
    music_title: str


@dataclass(frozen=True, slots=True, kw_only=True)
class TrackStarted(Event):
    track_id: str
    kind: str
    title: str
    duration_s: float


@dataclass(frozen=True, slots=True, kw_only=True)
class TrackFinished(Event):
    track_id: str
    kind: str
    title: str
    completed: bool = True  # False when playback was cut short (stop / skip)


@dataclass(frozen=True, slots=True, kw_only=True)
class Underrun(Event):
    """The timeline ran out of ready audio."""

    queue_seconds: float
    detail: str = ""


@dataclass(frozen=True, slots=True, kw_only=True)
class ProducerState(Event):
    """A snapshot of what the producer is doing (successor of the old
    global "progress_state" string)."""

    state: str
    buffered_seconds: float = 0.0
    detail: str = ""


Handler = Callable[[Event], None]


class Bus:
    """A synchronous, thread-safe, isinstance-matched publish/subscribe bus."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subs: list[tuple[type[Event], Handler]] = []

    def subscribe(self, event_type: type[Event], handler: Handler) -> Callable[[], None]:
        """Register "handler" for "event_type" and its subclasses.

        Returns a zero-arg callable that unsubscribes it again.
        """
        if not (isinstance(event_type, type) and issubclass(event_type, Event)):
            raise TypeError(f"event_type must be an Event subclass, got {event_type!r}")
        entry = (event_type, handler)
        with self._lock:
            self._subs.append(entry)

        def _unsubscribe() -> None:
            with self._lock:
                try:
                    self._subs.remove(entry)
                except ValueError:
                    pass

        return _unsubscribe

    def unsubscribe(self, event_type: type[Event], handler: Handler) -> None:
        with self._lock:
            self._subs = [e for e in self._subs if e != (event_type, handler)]

    def publish(self, event: Event) -> int:
        """Notify every matching handler. Returns how many were called."""
        if not isinstance(event, Event):
            raise TypeError(f"can only publish Event instances, got {event!r}")
        with self._lock:
            targets = [h for (etype, h) in self._subs if isinstance(event, etype)]

        for handler in targets:
            try:
                handler(event)
            except Exception:
                log.exception(
                    "event handler %r failed for %s",
                    getattr(handler, "__qualname__", handler),
                    type(event).__name__,
                )
        return len(targets)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subs)
