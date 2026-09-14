"""Application object, lifecycle, and process entrypoint.

:class:'Application' owns the long-lived objects (config, event bus, store, and
later the producer / timeline / broadcaster) and a clean start/stop lifecycle.
"Application.run()" installs SIGINT/SIGTERM handlers, starts everything, blocks
until a shutdown is requested, then stops everything in reverse order.

Wired so far: config, bus, store, library (+ recovery), ComfyUI client (+
workflow validation), the timeline with local audio and the stream broadcaster,
and the full Producer (planner, DJ brain, weather, generated + user song
sources). The UI arrives in build step 8 (docs/REBUILD_PLAN.md).
"""

from __future__ import annotations

import logging
import os
import signal
import threading
from pathlib import Path
from types import FrameType

from pilot import __version__
from pilot.broadcast import Broadcaster, NullBroadcaster, build_broadcaster
from pilot.comfy import ComfyClient, ComfyUnreachable, validate_workflows
from pilot.comfy.workflows import WorkflowRenderer
from pilot.config import Config, ConfigError, load_config
from pilot.dj_brain import DJBrain
from pilot.domain import MUSIC_KINDS
from pilot.events import Bus, Event, TrackStarted
from pilot.ffmpeg import FFmpegError, FFmpegNotFound
from pilot.library import Library
from pilot.planner import Planner
from pilot.producer import Producer
from pilot.sinks import LocalSink, Sink
from pilot.sources import GeneratedSource, SourceError, UserMusicSource
from pilot.store import Store
from pilot.timeline import Timeline
from pilot.weather import Weather

log = logging.getLogger("pilot")

_SIGNALS = (signal.SIGINT, signal.SIGTERM)


class StartupError(RuntimeError):
    """A precondition for running the radio is not met (e.g. a workflow needs a
    ComfyUI node that is not installed)."""


def setup_logging() -> None:
    """Configure root logging. Level from "PILOT_LOG_LEVEL" (default INFO)."""
    level_name = os.environ.get("PILOT_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level_name, logging.INFO),
        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
        datefmt="%H:%M:%S",
        force=True,
    )


class Application:
    def __init__(
        self,
        config_path: str | Path | None = None,
        *,
        repo_root: Path | None = None,
    ) -> None:
        self.config: Config = load_config(config_path, repo_root=repo_root)
        self.bus = Bus()
        self.store: Store | None = None
        self.library: Library | None = None
        self.comfy: ComfyClient | None = None
        self.weather: Weather | None = None
        self.user_source: UserMusicSource | None = None
        self.broadcaster: Broadcaster | None = None
        self.timeline: Timeline | None = None
        self.producer: Producer | None = None

        self._shutdown = threading.Event()
        self._started = False
        self._stopped = False
        self._orig_handlers: dict[int, object] = {}

    # -- lifecycle -------------------------------------------------

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        cfg = self.config

        log.info("Gen-Music-Pilot v%s", __version__)
        log.info("config: %s", cfg.source_path)
        for line in cfg.summary().splitlines():
            log.info("  %s", line)

        self.bus.subscribe(Event, self._trace_event)
        self.bus.subscribe(TrackStarted, self._on_track_started)

        db_path = cfg.library_dir / "state.db"
        self.store = Store(db_path)
        log.info("store: %s (schema v%d)", db_path, self.store.schema_version)

        self.library = Library(cfg.library_dir, self.store)
        report = self.library.scan_recovery()
        log.info("recovery: %s", report)

        self.comfy = ComfyClient(
            cfg.comfy_url,
            request_timeout=cfg.comfy.request_timeout,
            job_timeout=cfg.comfy.job_timeout,
            poll_interval=cfg.comfy.poll_interval,
        )
        self._check_workflows()

        self.broadcaster = self._build_broadcaster()
        self.timeline = Timeline(
            self.bus, self.store, self.library, sinks=tuple(self._build_sinks())
        )
        self.timeline.start()

        self.weather = Weather(cfg.weather)
        try:
            song_source = GeneratedSource.from_config(cfg)
        except SourceError as e:
            raise StartupError(f"cannot load song assets: {e}") from None

        self.user_source = self._build_user_source()
        if cfg.mode in ("user", "mixed") and self.user_source is None:
            raise StartupError(
                f"mode is {cfg.mode!r} but no playable audio was found under "
                f"{[str(d.path) for d in cfg.user_music_dirs] or 'user_music_dirs'}"
            )

        renderer = WorkflowRenderer(self.comfy, self.library, cfg.workflows)
        self.producer = Producer(
            timeline=self.timeline,
            store=self.store,
            bus=self.bus,
            library=self.library,
            planner=Planner.from_config(cfg),
            dj=DJBrain.from_config(cfg, self.store),
            renderer=renderer,
            song_source=song_source,
            user_source=self.user_source,
            weather=self.weather,
            target_seconds=cfg.buffer_target_seconds,
        )
        self.producer.start()

        log.info("on air (mode: %s)", cfg.mode)

    def _build_user_source(self) -> UserMusicSource | None:
        """Build the user-music source whenever dirs are configured, so the mode
        can be switched to it later even if the config starts generative."""
        if not self.config.user_music_dirs:
            return None
        try:
            src = UserMusicSource.from_config(self.config)
        except SourceError as e:
            log.warning("user music unavailable: %s", e)
            return None
        log.info("user music: %d track(s)", len(src))
        return src

    # -- runtime controls (for the UI) ------------------------

    def inject_caller(self, text: str) -> None:
        """Queue a caller interaction for the next talk block. Safe from any
        thread."""
        if self.producer is not None:
            self.producer.inject_caller(text)

    def set_mode(self, mode: str) -> None:
        """Switch the music mode live. Raises ValueError for an unknown mode or
        for user/mixed with no user music available."""
        if mode in ("user", "mixed") and self.user_source is None:
            raise ValueError(
                f"cannot switch to {mode!r}: no user music dirs with playable audio"
            )
        if self.producer is not None:
            self.producer.set_mode(mode)
            log.info("mode -> %s", mode)

    @property
    def mode(self) -> str:
        """The music mode in effect now (live if running, else the config default)."""
        return self.producer.mode if self.producer is not None else self.config.mode

    def _build_sinks(self) -> list[Sink]:
        sinks: list[Sink] = []
        if self.config.local_audio:
            try:
                sinks.append(LocalSink())
                log.info("local audio: on")
            except FFmpegNotFound as e:
                log.warning("local audio requested but unavailable: %s", e)
        if self.broadcaster is not None and not isinstance(
            self.broadcaster, NullBroadcaster
        ):
            sinks.append(self.broadcaster)
        if not sinks:
            log.info("no audio sink active - the timeline paces silently for now")
        return sinks

    def _build_broadcaster(self) -> Broadcaster:
        """Build and start the stream broadcaster. A streaming failure is loud
        but not fatal: the radio keeps playing (locally / silently) on a no-op
        broadcaster so the operator can fix the stream without downtime."""
        cfg = self.config
        broadcaster = build_broadcaster(cfg)
        if isinstance(broadcaster, NullBroadcaster):
            log.info("streaming: off")
            return broadcaster
        try:
            broadcaster.start()
        except (FFmpegError, OSError, RuntimeError) as e:
            log.error(
                "streaming disabled - the %s broadcaster did not start: %s",
                cfg.stream.backend, e,
            )
            return NullBroadcaster()
        log.info(
            "streaming (%s): listen at %s", cfg.stream.backend, broadcaster.listen_url
        )
        return broadcaster

    def _check_workflows(self) -> None:
        cfg = self.config
        assert self.comfy is not None
        if os.environ.get("PILOT_SKIP_COMFY_CHECK"):
            log.warning("PILOT_SKIP_COMFY_CHECK set - workflows not validated")
            return
        if not self.comfy.is_reachable():
            log.warning(
                "ComfyUI not reachable at %s - workflows not validated; the radio "
                "will run on fallback content until ComfyUI is up",
                cfg.comfy_url,
            )
            return
        try:
            report = validate_workflows(self.comfy, cfg.workflows)
        except ComfyUnreachable:
            log.warning("ComfyUI went away during validation - skipping")
            return
        if report:
            lines = [
                f"  {name}:\n" + "\n".join(f"    - {p}" for p in problems)
                for name, problems in report.items()
            ]
            raise StartupError(
                "ComfyUI cannot run these workflows:\n"
                + "\n".join(lines)
                + "\n\nInstall the missing custom nodes (ComfyUI-Manager) or "
                "re-export the workflow after the bring-up checklist "
                "(docs/REBUILD_PLAN.md section 9). Set PILOT_SKIP_COMFY_CHECK=1 "
                "to start anyway."
            )
        log.info("workflows validated against ComfyUI")

    def stop(self) -> None:
        if self._stopped or not self._started:
            self._stopped = True
            return
        self._stopped = True
        log.info("stopping")

        # reverse order of start()
        if self.producer is not None:
            self.producer.stop()
            self.producer = None
        if self.timeline is not None:
            self.timeline.stop()  # closes its sinks, the broadcaster among them
            self.timeline = None
        if self.broadcaster is not None:
            self.broadcaster.close()  # idempotent - safe after the sink close
            self.broadcaster = None
        self.user_source = None
        if self.weather is not None:
            self.weather.close()
            self.weather = None
        if self.comfy is not None:
            self.comfy.close()
            self.comfy = None
        self.library = None
        if self.store is not None:
            self.store.close()
            self.store = None
        log.info("stopped")

    def request_stop(self) -> None:
        """Ask :meth:'run' to unblock and shut down. Safe from any thread."""
        self._shutdown.set()

    def run(self) -> None:
        """Start, block until a signal or :meth:'request_stop', then stop."""
        self._install_signal_handlers()
        try:
            self.start()
            self._shutdown.wait()
        finally:
            self.stop()
            self._restore_signal_handlers()

    # -- context manager (mainly for tests) -----------------------

    def __enter__(self) -> Application:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    # -- internals ----------------------------------------------

    def _trace_event(self, event: Event) -> None:
        logging.getLogger("pilot.events.trace").debug("%s", event)

    def _on_track_started(self, event: TrackStarted) -> None:
        """Push the now-playing title to the stream when a music track begins;
        talk tracks leave the last song showing."""
        if self.broadcaster is not None and event.kind in MUSIC_KINDS:
            self.broadcaster.set_now_playing(event.title)

    def _handle_signal(self, signum: int, _frame: FrameType | None) -> None:
        log.info("received %s - shutting down", signal.Signals(signum).name)
        self._shutdown.set()

    def _install_signal_handlers(self) -> None:
        try:
            for sig in _SIGNALS:
                self._orig_handlers[sig] = signal.signal(sig, self._handle_signal)
        except ValueError:
            # not on the main thread (e.g. under a test runner) - rely on
            # request_stop() instead
            log.debug("signal handlers not installed (not main thread)")

    def _restore_signal_handlers(self) -> None:
        for sig, handler in self._orig_handlers.items():
            try:
                signal.signal(sig, handler)  # type: ignore[arg-type]
            except (ValueError, TypeError):
                pass
        self._orig_handlers.clear()


def main() -> None:
    setup_logging()
    try:
        app = Application()
    except ConfigError as e:
        log.error("%s", e)
        raise SystemExit(1) from None
    try:
        app.run()
    except StartupError as e:
        log.error("%s", e)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
