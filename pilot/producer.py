"""Keeps the timeline fed.

:class:'Producer' keeps "buffer_target_seconds" of audio buffered by
building one block at a time --

    plan -> pick song (generated, or a real file from the user's library when
    the plan says MUSIC_USER) -> weather -> DJ prompts -> render talk ->
    render/import music -> Segment -> enqueue -> record into the show memory

ComfyUI jobs run strictly one after another (the machine cannot do two), always
*ahead* of playback. Each block's DJ prompt carries its **predicted airtime**
(now + how much audio is already buffered), so the DJ announces the time the
clip will actually air.

Failure path: a talk render error just drops the announcement; a music render
error falls back to a library track for that block; "ComfyUnreachable" puts
the producer into "comfy down" mode where it keeps the buffer topped with
fallback segments and probes for recovery.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path

from pilot.comfy.client import ComfyCancelled, ComfyUnreachable
from pilot.comfy.workflows import WorkflowRenderer
from pilot.dj_brain import DJBrain, DJContext
from pilot.domain import Segment, SongData, Track, TrackKind
from pilot.events import Bus, ProducerState
from pilot.library import Library
from pilot.planner import BlockPlan, Planner
from pilot.sources import SongSource, UserMusicSource
from pilot.store import Store
from pilot.timeline import Timeline
from pilot.weather import Weather

log = logging.getLogger("pilot.producer")


class Producer:
    def __init__(
        self,
        *,
        timeline: Timeline,
        store: Store,
        bus: Bus,
        library: Library,
        planner: Planner,
        dj: DJBrain,
        renderer: WorkflowRenderer,
        song_source: SongSource,
        weather: Weather,
        user_source: UserMusicSource | None = None,
        target_seconds: float,
        check_interval: float = 4.0,
        recovery_interval: float = 15.0,
        history_window: int = 10,
        clock: Callable[[], datetime] | None = None,
        comfy_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._timeline = timeline
        self._store = store
        self._bus = bus
        self._library = library
        self._planner = planner
        self._dj = dj
        self._renderer = renderer
        self._source = song_source
        self._user_source = user_source
        self._weather = weather
        self._target = target_seconds
        self._interval = check_interval
        self._recovery_interval = recovery_interval
        self._history_window = history_window
        self._clock = clock or datetime.now
        self._comfy_probe = comfy_probe or renderer.client.is_reachable

        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._caller_lock = threading.Lock()
        self._pending_caller: str | None = None
        self._comfy_down = False

        # a stop() during a render aborts the poll instead of waiting it out
        self._renderer.cancel = self._stop

    # -- lifecycle -------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="producer", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=15)
            self._thread = None

    # -- caller injection (from the UI) ------------------------

    def set_mode(self, mode: str) -> None:
        """Switch the music mode live (delegates to the planner)."""
        self._planner.set_mode(mode)

    @property
    def mode(self) -> str:
        """The music mode currently in effect."""
        return self._planner.mode

    def inject_caller(self, text: str) -> None:
        cleaned = (text or "").strip()
        with self._caller_lock:
            self._pending_caller = cleaned or None
        log.info("caller injection queued: %s", cleaned or "(cleared)")

    def _take_caller(self) -> str | None:
        with self._caller_lock:
            caller, self._pending_caller = self._pending_caller, None
            return caller

    def _return_caller(self, text: str) -> None:
        with self._caller_lock:
            if self._pending_caller is None:
                self._pending_caller = text

    # -- worker ---------------------------------------------

    def _run(self) -> None:
        log.info("producer running (target %.0fs buffered)", self._target)
        self._emit("starting")
        self._comfy_down = False
        while not self._stop.is_set():
            buffered = self._timeline.queue_seconds()

            if self._comfy_down:
                if self._comfy_reachable():
                    self._comfy_down = False
                    log.info("ComfyUI reachable again - resuming generation")
                    self._emit("comfy back")
                    continue
                if buffered < self._target:
                    self._enqueue_fallback("comfy unreachable")
                else:
                    self._emit("resting (comfy down)", buffered)
                self._stop.wait(self._recovery_interval)
                continue

            if buffered >= self._target:
                self._emit("resting", buffered)
                self._stop.wait(self._interval)
                continue

            try:
                self._produce_block()
            except ComfyCancelled:
                break  # stop() was called during a render
            except ComfyUnreachable as e:
                self._comfy_down = True
                log.warning("ComfyUI unreachable (%s) - riding on fallback", e)
                self._emit("comfy unreachable", detail=str(e))
                self._enqueue_fallback("comfy unreachable")
            except Exception:  # noqa: BLE001
                log.exception("block production failed - inserting a fallback")
                self._emit("block failed", detail="fallback inserted")
                self._enqueue_fallback("render error")
                self._stop.wait(self._interval)
        log.info("producer stopped")

    def _comfy_reachable(self) -> bool:
        try:
            return bool(self._comfy_probe())
        except Exception:  # noqa: BLE001
            return False

    @property
    def comfy_reachable(self) -> bool:
        """Best-known ComfyUI reachability, from the producer's own probing --
        cheap to read (no I/O), for the UI to show a live status hint."""
        return not self._comfy_down

    def _enqueue_fallback(self, reason: str) -> bool:
        track = self._library.random_fallback()
        if track is None:
            log.debug("no fallback audio available (%s)", reason)
            return False
        self._timeline.enqueue(Segment(music=track))
        self._emit("fallback enqueued", detail=reason)
        return True

    def _produce_block(self) -> None:
        airtime = self._clock() + timedelta(seconds=self._timeline.queue_seconds())
        history = self._store.recent_history(self._history_window)
        caller = self._take_caller()
        plan = self._planner.plan(
            airtime=airtime, history=history, pending_caller=caller
        )
        if caller and plan.caller is None:
            self._return_caller(caller)  # a music-only block didn't use it

        song, user_file = self._pick_song(plan)
        weather = self._weather.current()
        ctx = DJContext(
            airtime=airtime,
            weather=weather,
            caller=plan.caller,
            announce_time=plan.announce_time,
        )
        meta = {
            "song_name": song.song_name,
            "artist": song.artist,
            "genre_desc": song.genre_desc,
        }

        # talk: a failure just drops the announcement. ComfyUnreachable is fatal
        # for a generated block (-> _run goes comfy-down) but a user track can
        # still air on its own.
        try:
            talk_track = self._render_talk(plan, song, ctx, meta, airtime)
        except ComfyCancelled:
            raise
        except ComfyUnreachable:
            if user_file is None:
                raise
            log.warning("ComfyUI unreachable - the user track airs without a talk")
            self._emit("comfy unreachable", detail="user track airs solo")
            talk_track = None
            if plan.caller:
                self._return_caller(plan.caller)
        except Exception:  # noqa: BLE001
            log.exception("talk render failed - block airs without an announcement")
            self._emit("talk failed", detail="block continues")
            talk_track = None
            if plan.caller:  # the caller never got their moment - try again next block
                self._return_caller(plan.caller)

        # music: a failure falls back to a library track for this block
        try:
            music_track = self._render_music(song, user_file, airtime)
        except (ComfyUnreachable, ComfyCancelled):
            raise
        except Exception:  # noqa: BLE001
            log.exception("music failed - using a fallback track for this block")
            self._emit("music failed", detail="fallback track")
            music_track = self._library.random_fallback()
            if music_track is None:
                return  # nothing to enqueue; the timeline handles the underrun

        had_story = plan.talk is TrackKind.STORY and talk_track is not None
        segment = Segment(
            music=music_track, talk=talk_track, planned_airtime=airtime
        )
        self._timeline.enqueue(segment)

        self._dj.record(
            kind=_block_kind(music_track, had_story=had_story),
            song=song,
            dj_text=talk_track.meta.get("dj_text", "") if talk_track else "",
            airtime=airtime,
            caller=plan.caller if talk_track else None,
            had_story=had_story,
            track_id=talk_track.id if talk_track else None,
        )
        self._emit("enqueued", detail=f"block airs ~{airtime:%H:%M}")

    def _pick_song(self, plan: BlockPlan) -> tuple[SongData, Path | None]:
        if plan.music is TrackKind.MUSIC_USER:
            if self._user_source is not None:
                return self._user_source.next_file()
            log.warning("plan wants user music but no user source - generating")
        return self._source.next_song(), None

    def _render_talk(
        self,
        plan: BlockPlan,
        song: SongData,
        ctx: DJContext,
        meta: dict,
        airtime: datetime,
    ) -> Track | None:
        if plan.talk is TrackKind.STORY:
            self._emit("generating story", detail=f"~{airtime:%H:%M}")
            prompt = self._dj.story_for(song, ctx)
            return self._renderer.story(
                system=prompt.system,
                prompt=prompt.user,
                meta=meta,
                on_poll=self._progress("story"),
            )
        if plan.talk is TrackKind.MODERATION:
            self._emit("generating moderation", detail=f"~{airtime:%H:%M}")
            prompt = self._dj.moderation_for(song, ctx)
            return self._renderer.moderation(
                system=prompt.system,
                prompt=prompt.user,
                meta=meta,
                on_poll=self._progress("moderation"),
            )
        return None

    def _render_music(
        self, song: SongData, user_file: Path | None, airtime: datetime
    ) -> Track:
        if user_file is not None:
            self._emit("importing user track", detail=user_file.name)
            return self._library.import_external(
                user_file,
                kind=TrackKind.MUSIC_USER,
                meta={"song_name": song.song_name, "artist": song.artist},
            )
        self._emit("generating music", detail=f"~{airtime:%H:%M}")
        return self._renderer.music(song, on_poll=self._progress("music"))

    # -- events --------------------------------------------

    def _emit(self, state: str, buffered: float | None = None, *, detail: str = "") -> None:
        self._bus.publish(
            ProducerState(
                state=state,
                buffered_seconds=(
                    buffered if buffered is not None else self._timeline.queue_seconds()
                ),
                detail=detail,
            )
        )

    def _progress(self, phase: str):
        def _cb(elapsed: float) -> None:
            self._emit(f"generating {phase}", detail=f"{elapsed:.0f}s")

        return _cb


def _block_kind(music_track: Track, *, had_story: bool) -> str:
    if had_story:
        return "story"
    return {
        TrackKind.MUSIC_USER: "user_music",
        TrackKind.FALLBACK: "fallback",
    }.get(music_track.kind, "gen_music")
