"""Lifecycle controller between the Gradio layer and :class:'~pilot.app.Application'.

The UI never touches an "Application" directly. :class:'RadioController' owns
one (built fresh per run), attaches a :class:'~pilot.web.state.UiState' to its
bus, and exposes:

* "start()" / "stop()" -- "ConfigError" / "StartupError" are caught and
  returned as a status line, never raised into a Gradio callback;
* "inject_caller()" / "set_mode()" -- return a short result message;
* "view()" -- one immutable :class:'ControllerView' merging the live bus state,
  the timeline snapshot, the broadcaster, and the store's recent history.

Every method is safe to call from Gradio's worker threads.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from pilot.app import Application, StartupError
from pilot.config import ConfigError, load_config
from pilot.domain import HistoryEntry
from pilot.web.state import RecentTrack, UiState, UnderrunInfo

log = logging.getLogger("pilot.web")

MODE_CHOICES = ("generative", "user", "mixed")
_HISTORY_LINES = 8


@dataclass(frozen=True, slots=True)
class HistoryLine:
    kind: str
    at: datetime
    title: str | None
    dj_summary: str | None
    caller: str | None
    had_story: bool


@dataclass(frozen=True, slots=True)
class ControllerView:
    on_air: bool = False
    status: str = ""
    phase: str = "idle"
    detail: str = ""
    comfy_reachable: bool | None = None  # None = unknown (not on air yet)
    now_playing: str | None = None
    now_kind: str | None = None
    now_since: datetime | None = None
    now_active: bool = False
    buffered_seconds: float = 0.0
    buffer_target_seconds: float = 0.0
    queue_segments: int = 0
    queue_seconds: float = 0.0
    segments_aired: int = 0
    listeners: int = 0
    listen_url: str | None = None
    streaming: bool = False
    mode: str = "generative"
    mode_choices: tuple[str, ...] = MODE_CHOICES
    user_music_ready: bool = False
    recent: tuple[RecentTrack, ...] = ()
    history: tuple[HistoryLine, ...] = ()
    underrun: UnderrunInfo | None = None


class RadioController:
    def __init__(
        self,
        *,
        config_path: str | Path | None = None,
        repo_root: Path | None = None,
        app_factory: Callable[[], Application] | None = None,
    ) -> None:
        self._config_path = config_path
        self._repo_root = repo_root
        self._factory = app_factory or self._default_factory
        self._lock = threading.RLock()
        self._app: Application | None = None
        self._ui: UiState | None = None
        self._status = "idle -- press Start to go on air"
        # the mode to start in / switch to as soon as we can -- chosen before
        # Start (seeded from the config's own default), or remembered across
        # a stop() so a restart doesn't forget it. Only peeked with the real
        # factory -- a caller supplying their own app_factory (tests) is not
        # necessarily backed by an on-disk config at config_path/repo_root.
        self._pending_mode: str | None = (
            self._peek_config_mode() if app_factory is None else None
        )

    def _default_factory(self) -> Application:
        return Application(self._config_path, repo_root=self._repo_root)

    def _peek_config_mode(self) -> str | None:
        """Best-effort read of config.mode without building an Application --
        just so the mode radio shows the right thing before the first Start.
        Any failure here is silently ignored; start() reports it properly."""
        try:
            return load_config(self._config_path, repo_root=self._repo_root).mode
        except Exception:  # noqa: BLE001
            return None

    # -- lifecycle ------------------------------------------------

    @property
    def on_air(self) -> bool:
        with self._lock:
            return self._app is not None

    def start(self) -> str:
        with self._lock:
            if self._app is not None:
                return "already on air"
            try:
                app = self._factory()
            except ConfigError as e:
                self._status = f"config error -- {e}"
                return self._status
            ui = UiState(app.bus)
            try:
                app.start()
            except StartupError as e:
                ui.close()
                self._safe_stop(app)
                self._status = f"could not go on air -- {e}"
                return self._status
            except Exception as e:  # noqa: BLE001 -- a dashboard must not crash
                ui.close()
                self._safe_stop(app)
                log.exception("unexpected error starting the radio")
                self._status = f"could not go on air -- {e}"
                return self._status
            self._app, self._ui = app, ui
            self._status = "on air"
            if self._pending_mode and self._pending_mode != app.mode:
                try:
                    app.set_mode(self._pending_mode)
                except ValueError as e:
                    self._status = f"on air (Modus {self._pending_mode!r} nicht möglich: {e})"
            self._pending_mode = None  # app.mode is authoritative from here on
            return self._status

    def stop(self) -> str:
        with self._lock:
            app, ui = self._app, self._ui
            self._app = self._ui = None
            if app is None:
                self._status = "idle"
                return self._status
            self._pending_mode = app.mode  # so a restart picks up where it left off
        if ui is not None:
            ui.close()
        self._safe_stop(app)
        self._status = "stopped"
        return self._status

    close = stop

    @staticmethod
    def _safe_stop(app: Application) -> None:
        try:
            app.stop()
        except Exception:  # noqa: BLE001
            log.exception("error while stopping the radio")

    # -- controls -----------------------------------------------

    def inject_caller(self, text: str) -> str:
        with self._lock:
            if self._app is None:
                return "not on air"
            self._app.inject_caller(text)
        cleaned = (text or "").strip()
        return f"queued: {cleaned}" if cleaned else "caller note cleared"

    def set_mode(self, mode: str) -> str:
        with self._lock:
            if mode not in MODE_CHOICES:
                return f"unknown Mode: {mode!r}"
            if self._app is None:
                # not on air yet -- remember the choice for the next Start
                # rather than reject it (there is no user_source to validate
                # against yet; a bad choice surfaces clearly once started)
                self._pending_mode = mode
                return f"Scheduled for launch: {mode}"
            try:
                self._app.set_mode(mode)
            except ValueError as e:
                return str(e)
            return f"mode -> {self._app.mode}"

    # -- read --------------------------------------------------

    def view(self) -> ControllerView:
        with self._lock:
            app, ui, pending = self._app, self._ui, self._pending_mode
        if app is None or ui is None:
            return ControllerView(status=self._status, mode=pending or "generative")

        live = ui.snapshot()
        snap = app.timeline.snapshot() if app.timeline is not None else None
        broadcaster = app.broadcaster
        history: tuple[HistoryLine, ...] = ()
        if app.store is not None:
            history = tuple(
                _history_line(h) for h in app.store.recent_history(_HISTORY_LINES)
            )

        return ControllerView(
            on_air=True,
            status=self._status,
            phase=live.producer_phase,
            detail=live.detail,
            comfy_reachable=app.producer.comfy_reachable if app.producer else None,
            now_playing=live.now_playing or (snap.now_playing if snap else None),
            now_kind=live.now_kind,
            now_since=live.now_since,
            now_active=live.now_active,
            buffered_seconds=live.buffered_seconds,
            buffer_target_seconds=float(app.config.buffer_target_seconds),
            queue_segments=snap.queue_segments if snap else 0,
            queue_seconds=snap.queue_seconds if snap else 0.0,
            segments_aired=live.segments_aired,
            listeners=broadcaster.listener_count() if broadcaster else 0,
            listen_url=broadcaster.listen_url if broadcaster else None,
            streaming=bool(broadcaster and broadcaster.listen_url),
            mode=app.mode,
            user_music_ready=app.user_source is not None,
            recent=live.recent,
            history=history,
            underrun=live.underrun,
        )


def _history_line(h: HistoryEntry) -> HistoryLine:
    if h.song_name and h.artist:
        title: str | None = f"{h.song_name} - {h.artist}"
    else:
        title = h.song_name or None
    return HistoryLine(
        kind=h.kind,
        at=h.at,
        title=title,
        dj_summary=h.dj_summary,
        caller=h.caller_interaction,
        had_story=h.had_story,
    )
