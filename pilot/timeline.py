"""The timeline -- the radio's single clock.

It holds a queue of :class:'~pilot.domain.Segment's, and a worker thread that
pops them, decodes each track to one canonical PCM stream, and writes that stream
chunk by chunk to every sink **at real time**: after writing N bytes it sleeps
"N / pcm_byte_rate" seconds. So playback speed is set here, not by any sink --
local audio and the broadcaster both just receive the same paced bytes and
therefore stay in lock-step.

Underrun handling (insert a fallback when the queue is empty) lands in task 3.3;
for now an empty queue just idles.
"""

from __future__ import annotations

import logging
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass

from pilot import ffmpeg
from pilot.domain import Segment, Track, TrackKind, TrackState
from pilot.events import Bus, SegmentStarted, TrackFinished, TrackStarted, Underrun
from pilot.library import Library
from pilot.sinks import Sink
from pilot.store import Store

log = logging.getLogger("pilot.timeline")

_IDLE_POLL_S = 0.25


@dataclass(frozen=True)
class TimelineSnapshot:
    now_playing: str | None
    kind: str | None
    queue_segments: int
    queue_seconds: float
    running: bool


class Timeline:
    def __init__(
        self,
        bus: Bus,
        store: Store,
        library: Library,
        *,
        sinks: tuple[Sink, ...] = (),
        chunk_bytes: int | None = None,
    ) -> None:
        self._bus = bus
        self._store = store
        self._library = library
        self._sinks: list[Sink] = list(sinks)

        self._queue: deque[Segment] = deque(store.load_queue())
        self._pending: deque[Track] = deque()  # tracks left in the current segment
        self._current: Track | None = None
        self._current_started: float | None = None
        self._current_segment: Segment | None = None

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._in_underrun = False
        self._byte_rate = ffmpeg.pcm_byte_rate()
        self._chunk = chunk_bytes or self._byte_rate // 10  # ~100 ms

    # -- sinks --------------------------------------------------

    def add_sink(self, sink: Sink) -> None:
        self._sinks.append(sink)

    # -- queue --------------------------------------------------

    def enqueue(self, segment: Segment) -> None:
        for track in segment.tracks:
            self._store.upsert_track(track)
        with self._lock:
            self._queue.append(segment)
            self._persist_locked()
        # fallback segments come thick and fast during an underrun -> keep quiet
        level = (
            logging.DEBUG
            if segment.music.kind is TrackKind.FALLBACK and segment.talk is None
            else logging.INFO
        )
        log.log(
            level,
            "enqueued segment %s (%s%.0fs)",
            segment.id[:8],
            "talk + music, " if segment.talk else "",
            segment.duration_s,
        )

    def queue_seconds(self) -> float:
        with self._lock:
            total = sum(s.duration_s for s in self._queue)
            total += sum(t.duration_s or 0.0 for t in self._pending)
            total += self._remaining_current_locked()
            return total

    def snapshot(self) -> TimelineSnapshot:
        with self._lock:
            cur = self._current
            pending_seg = 1 if (self._current_segment and self._pending) else 0
            total = sum(s.duration_s for s in self._queue)
            total += sum(t.duration_s or 0.0 for t in self._pending)
            total += self._remaining_current_locked()
            return TimelineSnapshot(
                now_playing=cur.title if cur else None,
                kind=cur.kind.value if cur else None,
                queue_segments=len(self._queue) + pending_seg,
                queue_seconds=total,
                running=self._thread is not None and self._thread.is_alive(),
            )

    # -- lifecycle -------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        if self._queue:
            log.info("resuming %d segment(s) from the previous run", len(self._queue))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="timeline", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=10)
            self._thread = None
        for sink in self._sinks:
            try:
                sink.close()
            except Exception:
                log.exception("sink close failed")

    # -- worker --------------------------------------------

    def _run(self) -> None:
        log.info("timeline running (%d sink(s))", len(self._sinks))
        while not self._stop.is_set():
            track, new_segment = self._next_track()
            if track is None:
                self._handle_empty_queue()
                continue
            self._in_underrun = False
            if new_segment is not None:
                self._bus.publish(
                    SegmentStarted(
                        segment_id=new_segment.id,
                        has_talk=new_segment.talk is not None,
                        music_title=new_segment.music.title,
                    )
                )
            self._play_track(track)
        log.info("timeline stopped")

    def _handle_empty_queue(self) -> None:
        """Never dead-air: slot in a fallback, or (if there are none) report the
        silence once and keep polling."""
        fallback = self._library.random_fallback()
        if fallback is not None:
            log.warning("queue empty - inserting fallback: %s", fallback.title)
            self._bus.publish(
                Underrun(queue_seconds=0.0, detail=f"fallback: {fallback.title}")
            )
            self.enqueue(Segment(music=fallback))
            self._in_underrun = False
            return
        if not self._in_underrun:
            self._in_underrun = True
            log.error("queue empty and no fallback audio available - silence")
            self._bus.publish(
                Underrun(queue_seconds=0.0, detail="no fallback audio available")
            )
        self._stop.wait(_IDLE_POLL_S)

    def _next_track(self) -> tuple[Track | None, Segment | None]:
        with self._lock:
            if not self._pending:
                if not self._queue:
                    return None, None
                seg = self._queue.popleft()
                self._current_segment = seg
                self._pending.extend(seg.tracks)
                self._persist_locked()
                return self._pending.popleft(), seg
            return self._pending.popleft(), None

    def _play_track(self, track: Track) -> None:
        try:
            track.to_state(TrackState.PLAYING)
        except Exception:
            log.exception("cannot play track %s in state %s", track.id[:8], track.state)
            return
        self._store.upsert_track(track)
        with self._lock:
            self._current = track
            self._current_started = time.monotonic()
        self._bus.publish(
            TrackStarted(
                track_id=track.id,
                kind=track.kind.value,
                title=track.title,
                duration_s=track.duration_s or 0.0,
            )
        )

        completed = self._stream(track)

        with self._lock:
            self._current = None
            self._current_started = None
        track.to_state(TrackState.PLAYED)
        self._store.upsert_track(track)
        self._bus.publish(
            TrackFinished(
                track_id=track.id,
                kind=track.kind.value,
                title=track.title,
                completed=completed,
            )
        )

    def _stream(self, track: Track) -> bool:
        """Decode "track" to PCM and write it to every sink at real time.
        Returns True if it played to the end, False if cut short or unplayable."""
        if not track.path or not track.path.exists():
            log.warning("track %s: file missing, skipping", track.id[:8])
            return False
        try:
            proc = ffmpeg.to_pcm(track.path)
        except ffmpeg.FFmpegError as e:
            log.warning("track %s: decode failed: %s", track.id[:8], e)
            return False

        completed = True
        deadline = time.monotonic()
        try:
            assert proc.stdout is not None
            while not self._stop.is_set():
                data = proc.stdout.read(self._chunk)
                if not data:
                    break
                for sink in self._sinks:
                    sink.write(data)
                deadline += len(data) / self._byte_rate
                wait = deadline - time.monotonic()
                if wait > 0 and self._stop.wait(wait):
                    completed = False
                    break
            else:
                completed = False
        finally:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill()
            proc.wait()  # always reap, even if it already exited
            if proc.stdout is not None:
                proc.stdout.close()
        return completed

    # -- helpers (call under _lock) -----------------------

    def _remaining_current_locked(self) -> float:
        cur, started = self._current, self._current_started
        if cur is None or not cur.duration_s or started is None:
            return 0.0
        return max(0.0, cur.duration_s - (time.monotonic() - started))

    def _persist_locked(self) -> None:
        try:
            self._store.save_queue(list(self._queue))
        except Exception:
            log.exception("failed to persist queue snapshot")
