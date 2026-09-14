"""Zero-install streaming: one ffmpeg encoder, a stdlib HTTP server, fan-out.

The timeline feeds canonical PCM through :meth:'PyFanoutBroadcaster.write'. One
ffmpeg process encodes it to CBR MP3 (or AAC) once; a reader thread pushes each
encoded chunk to every connected listener's queue from the live edge. A slow
listener has its oldest chunk dropped so it stays near live rather than lagging
without bound. New listeners get a short burst of recent audio for a fast start.

ICY metadata ("icy-metaint" + "StreamTitle=") is interleaved when the client
asks ("Icy-MetaData: 1"); :meth:'set_now_playing' sets the title.

"GET /favicon.ico" (or ".png") is answered directly from "pilot/static/"
instead of being treated as a listener -- a browser that opens the stream URL
gets its own tab icon rather than joining the audio as a silent client.
"""

from __future__ import annotations

import logging
import queue
import select
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from pilot import ffmpeg
from pilot.broadcast.base import Broadcaster

log = logging.getLogger("pilot.broadcast.pyfanout")

_ICY_METAINT = 16000
_CLIENT_QUEUE_MAX = 128  # ~a few seconds of chunks; drop-oldest past this
_READ_CHUNK = 4096

# The browser tab icon for a listener who opens the stream URL directly (the
# "receiver" side) -- distinct from the dashboard, which uses Gradio's own.
_FAVICON_ROUTES = frozenset({"/favicon.ico", "/favicon.png"})
_FAVICON_PATH = Path(__file__).resolve().parents[1] / "static" / "favicon_client.png"


def _load_favicon() -> bytes | None:
    try:
        return _FAVICON_PATH.read_bytes()
    except OSError:
        log.warning("stream favicon missing at %s", _FAVICON_PATH)
        return None


_FAVICON_BYTES = _load_favicon()


# --------------------------------------------------------------------------
# ICY metadata
# --------------------------------------------------------------------------


def _icy_escape(title: str) -> str:
    out = title.replace("'", "").replace(";", " ")
    out = "".join(c if c >= " " else " " for c in out)
    return out.strip()[:200]


def icy_block(title: str, last_title: str) -> bytes:
    """One ICY metadata block: empty ("\\x00") if the title has not changed,
    otherwise "<len/16><StreamTitle='...';padded to 16>"."""
    if title == last_title:
        return b"\x00"
    payload = f"StreamTitle='{_icy_escape(title)}';".encode("utf-8", "replace")
    payload += b"\x00" * ((-len(payload)) % 16)
    return bytes([len(payload) // 16]) + payload


class _IcyWriter:
    """Writes audio to a socket, inserting an ICY block every "metaint" bytes
    ("metaint" <= 0 disables it)."""

    def __init__(self, wfile, metaint: int, get_title) -> None:
        self._w = wfile
        self._metaint = metaint
        self._get_title = get_title
        self._since = 0
        self._last_title = ""

    def write(self, data: bytes) -> None:
        if self._metaint <= 0:
            self._w.write(data)
            return
        view = memoryview(data)
        while view:
            take = min(self._metaint - self._since, len(view))
            self._w.write(view[:take])
            self._since += take
            view = view[take:]
            if self._since >= self._metaint:
                title = self._get_title()
                self._w.write(icy_block(title, self._last_title))
                self._last_title = title
                self._since = 0


# --------------------------------------------------------------------------
# encoder
# --------------------------------------------------------------------------


def encoder_args(codec: str, bitrate_kbps: int) -> list[str]:
    is_aac = codec == "aac"
    return [
        ffmpeg.FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin",
        # The input format is fully known, so skip stream probing -- otherwise
        # ffmpeg buffers the first ~1 s of audio before it emits anything, which
        # stalls a low-bitrate live stream indefinitely.
        "-probesize", "32", "-analyzeduration", "0",
        "-f", ffmpeg.PCM_FORMAT,
        "-ar", str(ffmpeg.PCM_RATE),
        "-ac", str(ffmpeg.PCM_CHANNELS),
        "-i", "-",
        "-c:a", "aac" if is_aac else "libmp3lame",
        "-b:a", f"{bitrate_kbps}k",
        "-f", "adts" if is_aac else "mp3",
        "-flush_packets", "1",
        "-",
    ]


# --------------------------------------------------------------------------
# broadcaster
# --------------------------------------------------------------------------


class PyFanoutBroadcaster(Broadcaster):
    def __init__(
        self,
        *,
        host: str,
        port: int,
        codec: str = "mp3",
        bitrate_kbps: int = 128,
    ) -> None:
        self._host = host
        self._port = port
        self._codec = codec
        self._bitrate = bitrate_kbps
        self.content_type = "audio/aac" if codec == "aac" else "audio/mpeg"

        self._lock = threading.Lock()
        self._clients: set[queue.Queue[bytes]] = set()
        self._burst = bytearray()
        self._burst_max = max(16_384, bitrate_kbps * 1000 // 8 * 2)  # ~2 s
        self._title = ""

        self._stop = threading.Event()
        self._proc: subprocess.Popen[bytes] | None = None
        self._proc_lock = threading.Lock()
        self._server: _StreamServer | None = None
        self._encoder_thread: threading.Thread | None = None

    # -- Broadcaster API ------------------------------------

    def start(self) -> None:
        if self._encoder_thread is not None:
            return
        ffmpeg.require(ffmpeg.FFMPEG)
        self._server = _StreamServer((self._host, self._port), _Handler, self)
        self._port = self._server.server_address[1]
        threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.2},
            name="stream-http",
            daemon=True,
        ).start()
        self._encoder_thread = threading.Thread(
            target=self._encoder_loop, name="stream-encoder", daemon=True
        )
        self._encoder_thread.start()
        log.info("stream on air: %s", self.listen_url)

    def write(self, pcm: bytes) -> None:
        with self._proc_lock:
            proc = self._proc
        if proc is None or proc.poll() is not None:
            return  # encoder restarting -- drop this chunk
        try:
            proc.stdin.write(pcm)
        except (BrokenPipeError, OSError, ValueError):
            pass

    def set_now_playing(self, title: str) -> None:
        with self._lock:
            self._title = title or ""

    def listener_count(self) -> int:
        with self._lock:
            return len(self._clients)

    @property
    def listen_url(self) -> str:
        host = "localhost" if self._host in ("", "0.0.0.0", "::") else self._host
        return f"http://{host}:{self._port}/"

    def close(self) -> None:
        self._stop.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        with self._proc_lock:
            proc, self._proc = self._proc, None
        _terminate(proc)
        if self._encoder_thread is not None:
            self._encoder_thread.join(timeout=3)
            self._encoder_thread = None

    # -- internals ----------------------------------------

    def _current_title(self) -> str:
        with self._lock:
            return self._title

    def _attach_listener(self) -> tuple[queue.Queue[bytes], bytes]:
        q: queue.Queue[bytes] = queue.Queue(maxsize=_CLIENT_QUEUE_MAX)
        with self._lock:
            self._clients.add(q)
            burst = bytes(self._burst)
            n = len(self._clients)
        log.info("stream listener connected (%d)", n)
        return q, burst

    def _detach_listener(self, q: queue.Queue[bytes]) -> None:
        with self._lock:
            self._clients.discard(q)
            n = len(self._clients)
        log.info("stream listener left (%d)", n)

    def _encoder_loop(self) -> None:
        while not self._stop.is_set():
            try:
                proc = subprocess.Popen(
                    encoder_args(self._codec, self._bitrate),
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                    bufsize=0,
                )
            except OSError as e:
                log.error("cannot start the stream encoder: %s", e)
                return
            with self._proc_lock:
                self._proc = proc
            self._pump(proc)
            proc.wait()
            with self._proc_lock:
                if self._proc is proc:
                    self._proc = None
            if self._stop.is_set():
                break
            log.warning(
                "stream encoder exited (code %s) - restarting", proc.returncode
            )
            self._stop.wait(1.0)

    def _pump(self, proc: subprocess.Popen[bytes]) -> None:
        assert proc.stdout is not None
        while not self._stop.is_set():
            chunk = proc.stdout.read(_READ_CHUNK)
            if not chunk:
                return
            self._fan_out(chunk)

    def _fan_out(self, chunk: bytes) -> None:
        with self._lock:
            self._burst.extend(chunk)
            if len(self._burst) > self._burst_max:
                del self._burst[: len(self._burst) - self._burst_max]
            clients = list(self._clients)
        for q in clients:
            try:
                q.put_nowait(chunk)
            except queue.Full:
                try:  # drop-oldest: stay near live
                    q.get_nowait()
                    q.put_nowait(chunk)
                except (queue.Empty, queue.Full):
                    pass


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


class _StreamServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, handler, broadcaster: PyFanoutBroadcaster) -> None:
        super().__init__(addr, handler)
        self.broadcaster = broadcaster


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, *_a: object) -> None:
        pass

    def do_HEAD(self) -> None:  # noqa: N802
        self.send_response(200)
        self.send_header("Content-Type", self.server.broadcaster.content_type)  # type: ignore[attr-defined]
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        if self.path.split("?", 1)[0] in _FAVICON_ROUTES:
            self._serve_favicon()
            return

        b: PyFanoutBroadcaster = self.server.broadcaster  # type: ignore[attr-defined]
        want_icy = self.headers.get("Icy-MetaData", "0").strip() == "1"

        self.send_response(200)
        self.send_header("Content-Type", b.content_type)
        self.send_header("Cache-Control", "no-cache, no-store")
        self.send_header("Connection", "close")
        self.send_header("icy-name", "Gen-Music-Pilot")
        if want_icy:
            self.send_header("icy-metaint", str(_ICY_METAINT))
        self.end_headers()

        q, burst = b._attach_listener()
        writer = _IcyWriter(
            self.wfile, _ICY_METAINT if want_icy else 0, b._current_title
        )
        try:
            if burst:
                writer.write(burst)
            while not b._stop.is_set():
                if self._client_gone():
                    break
                try:
                    chunk = q.get(timeout=0.2)
                except queue.Empty:
                    continue
                writer.write(chunk)
        except (BrokenPipeError, ConnectionResetError, OSError, ValueError):
            pass
        finally:
            b._detach_listener(q)

    def _serve_favicon(self) -> None:
        if _FAVICON_BYTES is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(_FAVICON_BYTES)))
        self.send_header("Cache-Control", "public, max-age=86400")
        self.end_headers()
        self.wfile.write(_FAVICON_BYTES)

    def _client_gone(self) -> bool:
        """True once the listener has closed the connection -- so an idle
        listener is dropped even while no audio is flowing."""
        try:
            ready, _, _ = select.select([self.connection], [], [], 0)
            if not ready:
                return False
            return not self.connection.recv(1, socket.MSG_PEEK)
        except OSError:
            return True
