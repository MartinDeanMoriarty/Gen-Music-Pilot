"""Push the stream to an Icecast mount.

One ffmpeg process encodes the timeline's canonical PCM to CBR MP3 (or AAC) and
sources it to "icecast://source:<pw>@host:port/mount" over Icecast's HTTP PUT
protocol. A monitor thread watches that process and reconnects with capped
exponential backoff whenever the mount is lost. The now-playing title is pushed
out of band to Icecast's "/admin/metadata" endpoint.

Icecast itself does the fan-out, so unlike :mod:'pilot.broadcast.pyfanout' there
is no per-listener bookkeeping here -- :meth:'listener_count' stays 0 and
:attr:'listen_url' points at the public mount.
"""

from __future__ import annotations

import base64
import logging
import socket
import subprocess
import threading
import urllib.error
import urllib.request
from collections import deque
from urllib.parse import quote, urlencode

from pilot import ffmpeg
from pilot.broadcast.base import Broadcaster
from pilot.config import IcecastConfig

log = logging.getLogger("pilot.broadcast.icecast")

_RECONNECT_MIN = 1.0
_RECONNECT_MAX = 30.0
_PREFLIGHT_SECONDS = 0.7  # window to catch a source ffmpeg bounces straight off
_METADATA_TIMEOUT = 5.0


class IcecastError(RuntimeError):
    """Icecast could not be reached or refused the source connection."""


def encoder_args(url: str, codec: str, bitrate_kbps: int, content_type: str) -> list[str]:
    is_aac = codec == "aac"
    return [
        ffmpeg.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        # input format is fully known -- skip probing so ffmpeg streams at once
        "-probesize", "32", "-analyzeduration", "0",
        "-f", ffmpeg.PCM_FORMAT,
        "-ar", str(ffmpeg.PCM_RATE),
        "-ac", str(ffmpeg.PCM_CHANNELS),
        "-i", "-",
        "-c:a", "aac" if is_aac else "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-content_type", content_type,
        "-ice_name", "Gen-Music-Pilot",
        "-f", "adts" if is_aac else "mp3",
        url,
    ]


class IcecastBroadcaster(Broadcaster):
    def __init__(
        self,
        config: IcecastConfig,
        *,
        codec: str = "mp3",
        bitrate_kbps: int = 128,
        reconnect_min: float = _RECONNECT_MIN,
        reconnect_max: float = _RECONNECT_MAX,
    ) -> None:
        self._cfg = config
        self._codec = codec
        self._bitrate = bitrate_kbps
        self._reconnect_min = reconnect_min
        self._reconnect_max = reconnect_max
        self.content_type = "audio/aac" if codec == "aac" else "audio/mpeg"

        self._mount = config.mount if config.mount.startswith("/") else f"/{config.mount}"
        secret = quote(config.source_password, safe="")
        self._url = (
            f"icecast://source:{secret}@{config.host}:{config.port}{self._mount}"
        )

        self._lock = threading.Lock()
        self._title = ""
        self._stop = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._proc_lock = threading.Lock()
        self._monitor: threading.Thread | None = None
        self._last_err: deque[str] = deque(maxlen=20)

    # -- Broadcaster API ------------------------------------

    def start(self) -> None:
        if self._monitor is not None:
            return
        ffmpeg.require(ffmpeg.FFMPEG)
        self._preflight()
        self._spawn()  # raises IcecastError if ffmpeg bounces straight off
        self._monitor = threading.Thread(
            target=self._monitor_loop, name="icecast-monitor", daemon=True
        )
        self._monitor.start()
        log.info("icecast source live: %s", self.listen_url)

    def write(self, pcm: bytes) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return  # reconnecting -- drop this chunk
        try:
            proc.stdin.write(pcm)  # type: ignore[union-attr]
        except (BrokenPipeError, OSError, ValueError):
            pass

    def set_now_playing(self, title: str) -> None:
        title = title or ""
        with self._lock:
            if title == self._title:
                return
            self._title = title
        threading.Thread(
            target=self._push_metadata, args=(title,),
            name="icecast-metadata", daemon=True,
        ).start()

    @property
    def listen_url(self) -> str:
        return f"http://{self._cfg.host}:{self._cfg.port}{self._mount}"

    def close(self) -> None:
        self._stop.set()
        with self._proc_lock:
            proc, self._proc = self._proc, None
        _terminate(proc)
        if self._monitor is not None:
            self._monitor.join(timeout=3)
            self._monitor = None

    # -- internals ----------------------------------------

    def _preflight(self) -> None:
        try:
            with socket.create_connection(
                (self._cfg.host, self._cfg.port), timeout=5
            ):
                pass
        except OSError as e:
            raise IcecastError(
                f"Icecast unreachable at {self._cfg.host}:{self._cfg.port} ({e})"
            ) from e

    def _spawn(self) -> None:
        args = encoder_args(self._url, self._codec, self._bitrate, self.content_type)
        try:
            proc = subprocess.Popen(
                args,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as e:  # pragma: no cover -- require() already checked
            raise IcecastError(f"cannot start the stream encoder: {e}") from e

        drain = threading.Thread(
            target=self._drain_stderr, args=(proc,), name="icecast-stderr", daemon=True
        )
        drain.start()

        if proc.stdin is not None:  # a little silence so ffmpeg opens the mount
            try:                    # now (and fails now) rather than lazily
                proc.stdin.write(b"\x00" * (ffmpeg.pcm_byte_rate() // 5))
            except OSError:
                pass
        if self._stop.wait(_PREFLIGHT_SECONDS):
            _terminate(proc)
            return
        if proc.poll() is not None:
            drain.join(timeout=1)
            raise IcecastError(
                f"Icecast refused the source connection to {self._mount}: "
                f"{self._err_tail() or f'ffmpeg exited {proc.returncode}'}"
            )
        with self._proc_lock:
            if self._stop.is_set():  # closed while we were connecting
                _terminate(proc)
                return
            self._proc = proc

    def _drain_stderr(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stderr is not None
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._last_err.append(line)

    def _err_tail(self) -> str:
        return " / ".join(self._last_err)

    def _monitor_loop(self) -> None:
        while not self._stop.is_set():
            with self._proc_lock:
                proc = self._proc
            if proc is None:
                break
            proc.wait()
            if self._stop.is_set():
                break
            log.warning(
                "icecast mount lost (ffmpeg exited %s: %s) - reconnecting",
                proc.returncode, self._err_tail() or "no detail",
            )
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
            self._reconnect()

    def _reconnect(self) -> None:
        delay = self._reconnect_min
        while not self._stop.is_set():
            if self._stop.wait(delay):
                return
            try:
                self._preflight()
                self._spawn()
            except IcecastError as e:
                log.warning("icecast reconnect failed (retry in %.0fs): %s", delay, e)
                delay = min(delay * 2, self._reconnect_max)
                continue
            log.info("icecast source reconnected: %s", self.listen_url)
            with self._lock:
                title = self._title
            if title:
                threading.Thread(
                    target=self._push_metadata, args=(title,),
                    name="icecast-metadata", daemon=True,
                ).start()
            return

    def _push_metadata(self, title: str) -> None:
        query = urlencode({
            "mount": self._mount,
            "mode": "updinfo",
            "song": title,
            "charset": "UTF-8",
        })
        url = f"http://{self._cfg.host}:{self._cfg.port}/admin/metadata?{query}"
        token = base64.b64encode(
            f"source:{self._cfg.source_password}".encode()
        ).decode("ascii")
        req = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
        try:
            with urllib.request.urlopen(req, timeout=_METADATA_TIMEOUT) as resp:
                resp.read()
        except (urllib.error.URLError, OSError) as e:
            log.warning("icecast metadata update failed: %s", e)


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    if proc.stdin is not None:  # EOF -- ffmpeg then exits on its own at once
        try:
            proc.stdin.close()
        except OSError:
            pass
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
