"""PCM sinks.

The timeline decodes each track to one canonical PCM stream (s16le / 44.1 kHz /
stereo) and writes it, chunk by chunk, to every registered :class:'Sink'. Local
playback and (later) the broadcaster are both just sinks, so they cannot drift
from each other -- the timeline is the single clock.

A sink must never raise from :meth:'write': a dead local audio device must not
stall the radio. Sinks degrade quietly and report it via "healthy".
"""

from __future__ import annotations

import logging
import subprocess
from typing import Protocol

from pilot import ffmpeg


class Sink(Protocol):
    def write(self, pcm: bytes) -> None: ...
    def close(self) -> None: ...


class NullSink:
    """Discards everything. Use when only the stream is wanted."""

    healthy = True

    def write(self, pcm: bytes) -> None:  # noqa: D102
        pass

    def close(self) -> None:  # noqa: D102
        pass


class BufferSink:
    """Collects everything written. Test aid."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.closed = False

    @property
    def healthy(self) -> bool:
        return not self.closed

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)

    def write(self, pcm: bytes) -> None:
        self.chunks.append(pcm)

    def close(self) -> None:
        self.closed = True


class LocalSink:
    """Plays the PCM stream on the local audio device via one long-lived ffplay.

    Construction raises :class:'ffmpeg.FFmpegNotFound' if ffplay is missing.
    After that it never raises: if ffplay dies (no audio device, unplugged
    output) the sink degrades to a no-op and logs it once.
    """

    def __init__(self) -> None:
        self._log = logging.getLogger("pilot.sinks.local")
        self._proc: subprocess.Popen[bytes] = ffmpeg.pcm_player()
        self._broken = False
        self.bytes_written = 0

    @property
    def healthy(self) -> bool:
        return not self._broken and self._proc.poll() is None

    def write(self, pcm: bytes) -> None:
        if self._broken:
            return
        if self._proc.poll() is not None:
            self._degrade(f"ffplay exited (code {self._proc.returncode})")
            return
        try:
            assert self._proc.stdin is not None
            self._proc.stdin.write(pcm)
            self._proc.stdin.flush()
            self.bytes_written += len(pcm)
        except (BrokenPipeError, OSError, ValueError) as e:
            self._degrade(f"write failed: {e}")

    def close(self) -> None:
        self._broken = True
        self._cleanup()

    # -- internals --------------------------------------------

    def _degrade(self, why: str) -> None:
        if not self._broken:
            self._log.warning("local audio disabled: %s", why)
        self._broken = True
        self._cleanup()

    def _cleanup(self) -> None:
        stdin = self._proc.stdin
        try:
            if stdin is not None and not stdin.closed:
                stdin.close()
        except OSError:
            pass
        if self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self._proc.wait()
