"""The broadcaster interface.

A broadcaster is a timeline :class:'~pilot.sinks.Sink' ("write(pcm)" /
"close()") that also encodes that PCM once and fans it out to many listeners
at the same playhead. Concrete backends:

* :mod:'pilot.broadcast.pyfanout' -- zero-install, a stdlib HTTP server
* :mod:'pilot.broadcast.icecast'  -- push to an Icecast mount

:class:'NullBroadcaster' is used when "stream.enabled" is false: it just
swallows the PCM so the timeline still has a well-formed sink list.
"""

from __future__ import annotations

import abc
import logging

from pilot.config import Config

log = logging.getLogger("pilot.broadcast")


class Broadcaster(abc.ABC):
    @abc.abstractmethod
    def start(self) -> None:
        """Bring the encoder / server up. Safe to call once."""

    @abc.abstractmethod
    def write(self, pcm: bytes) -> None:
        """Feed one chunk of canonical PCM (s16le / 44.1k / stereo)."""

    @abc.abstractmethod
    def close(self) -> None:
        """Tear everything down."""

    def set_now_playing(self, title: str) -> None:
        """Update the stream's ICY title. Default: no-op."""

    def listener_count(self) -> int:
        return 0

    @property
    def listen_url(self) -> str | None:
        return None


class NullBroadcaster(Broadcaster):
    """Streaming disabled -- discards everything."""

    def start(self) -> None:
        pass

    def write(self, pcm: bytes) -> None:
        pass

    def close(self) -> None:
        pass


def build_broadcaster(config: Config) -> Broadcaster:
    stream = config.stream
    if not stream.enabled:
        return NullBroadcaster()

    if stream.backend == "pyfanout":
        from pilot.broadcast.pyfanout import PyFanoutBroadcaster

        return PyFanoutBroadcaster(
            host=stream.host,
            port=stream.port,
            codec=stream.codec,
            bitrate_kbps=stream.bitrate_kbps,
        )

    if stream.backend == "icecast":
        if stream.icecast is None:
            raise ValueError("stream.backend is 'icecast' but stream.icecast is unset")
        from pilot.broadcast.icecast import IcecastBroadcaster

        return IcecastBroadcaster(
            stream.icecast, codec=stream.codec, bitrate_kbps=stream.bitrate_kbps
        )

    raise ValueError(f"unknown stream backend: {stream.backend!r}")
