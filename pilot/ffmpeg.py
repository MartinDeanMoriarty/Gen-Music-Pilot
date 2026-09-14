"""Thin wrappers around the ffmpeg toolchain.

The rebuild leans on ffmpeg for everything audio: "probe" (duration + tags),
"loudnorm" (normalise imported user files), "to_pcm" (decode any file to a
raw PCM stream the timeline can pace and fan out), and "pcm_player" (play that
PCM stream locally via ffplay).

The canonical PCM format for the whole pipeline is signed 16-bit little-endian,
44.1 kHz, stereo -- :data:'PCM_RATE' / :data:'PCM_CHANNELS', byte rate from
:func:'pcm_byte_rate'.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

FFMPEG = "ffmpeg"
FFPROBE = "ffprobe"
FFPLAY = "ffplay"

PCM_RATE = 44_100
PCM_CHANNELS = 2
PCM_SAMPLE_BYTES = 2  # s16
PCM_FORMAT = "s16le"


def pcm_byte_rate() -> int:
    """Bytes of raw PCM per second of audio."""
    return PCM_RATE * PCM_CHANNELS * PCM_SAMPLE_BYTES


class FFmpegError(RuntimeError):
    """An ffmpeg/ffprobe invocation failed."""


class FFmpegNotFound(FFmpegError):
    """A required ffmpeg-family binary is not on PATH."""


@dataclass(frozen=True)
class ProbeResult:
    duration_s: float
    tags: dict[str, str] = field(default_factory=dict)  # lower-cased keys
    sample_rate: int | None = None
    channels: int | None = None
    codec: str | None = None

    def tag(self, *names: str) -> str | None:
        for n in names:
            v = self.tags.get(n.lower())
            if v:
                return v
        return None


# --------------------------------------------------------------------------
# availability
# --------------------------------------------------------------------------


def which(binary: str) -> str | None:
    return shutil.which(binary)


def require(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise FFmpegNotFound(f"{binary} not found on PATH")
    return path


def have_toolchain() -> bool:
    return which(FFMPEG) is not None and which(FFPROBE) is not None


# --------------------------------------------------------------------------
# probe
# --------------------------------------------------------------------------


def probe(path: str | Path) -> ProbeResult:
    """Return duration + metadata for an audio file."""
    path = Path(path)
    require(FFPROBE)
    if not path.exists():
        raise FFmpegError(f"file not found: {path}")

    args = [
        FFPROBE,
        "-v",
        "quiet",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        out = subprocess.run(
            args, capture_output=True, text=True, check=True
        ).stdout
    except FileNotFoundError as e:  # pragma: no cover - require() guards this
        raise FFmpegNotFound(str(e)) from e
    except subprocess.CalledProcessError as e:
        raise FFmpegError(f"ffprobe failed for {path}: {e.stderr.strip()}") from e

    try:
        data = json.loads(out)
    except json.JSONDecodeError as e:
        raise FFmpegError(f"ffprobe returned invalid JSON for {path}") from e

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    audio = next((s for s in streams if s.get("codec_type") == "audio"), {})

    duration = _first_float(fmt.get("duration"), audio.get("duration"))
    if duration is None:
        raise FFmpegError(f"no duration reported for {path}")

    tags: dict[str, str] = {}
    for source in (audio.get("tags"), fmt.get("tags")):  # format tags win
        if isinstance(source, dict):
            tags.update({str(k).lower(): str(v) for k, v in source.items()})

    return ProbeResult(
        duration_s=duration,
        tags=tags,
        sample_rate=_first_int(audio.get("sample_rate")),
        channels=_first_int(audio.get("channels")),
        codec=audio.get("codec_name"),
    )


def _first_float(*values: object) -> float | None:
    for v in values:
        try:
            if v is not None:
                return float(v)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
    return None


def _first_int(*values: object) -> int | None:
    f = _first_float(*values)
    return int(f) if f is not None else None


# --------------------------------------------------------------------------
# loudness normalisation
# --------------------------------------------------------------------------


def loudnorm(
    src: str | Path,
    dst: str | Path,
    *,
    target_i: float = -16.0,
    target_tp: float = -1.5,
    target_lra: float = 11.0,
    quality: int = 2,
) -> None:
    """Re-encode "src" to "dst" (mp3) with the EBU R128 loudnorm filter.

    Single pass -- good enough to stop user files clashing in loudness with
    generated tracks (REBUILD_PLAN.md section 4).
    """
    src, dst = Path(src), Path(dst)
    require(FFMPEG)
    if not src.exists():
        raise FFmpegError(f"file not found: {src}")
    dst.parent.mkdir(parents=True, exist_ok=True)

    args = [
        FFMPEG,
        "-hide_banner",
        "-nostdin",
        "-y",
        "-i",
        str(src),
        "-af",
        f"loudnorm=I={target_i}:TP={target_tp}:LRA={target_lra}",
        "-c:a",
        "libmp3lame",
        "-q:a",
        str(quality),
        str(dst),
    ]
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise FFmpegError(f"loudnorm failed for {src}: {proc.stderr.strip()[-500:]}")


# --------------------------------------------------------------------------
# PCM streaming
# --------------------------------------------------------------------------


def to_pcm_args(path: str | Path, *, start_s: float = 0.0) -> list[str]:
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-nostdin"]
    if start_s > 0:
        args += ["-ss", f"{start_s:.3f}"]
    args += [
        "-i",
        str(path),
        "-f",
        PCM_FORMAT,
        "-ar",
        str(PCM_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "-",
    ]
    return args


def to_pcm(path: str | Path, *, start_s: float = 0.0) -> subprocess.Popen[bytes]:
    """Start decoding "path" to canonical PCM on the process's stdout.

    Caller owns the process: read ".stdout" in chunks, then ".wait()"; call
    ".terminate()" to stop early.
    """
    require(FFMPEG)
    if not Path(path).exists():
        raise FFmpegError(f"file not found: {path}")
    return subprocess.Popen(
        to_pcm_args(path, start_s=start_s),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        stdin=subprocess.DEVNULL,
    )


def pcm_player_args() -> list[str]:
    return [
        FFPLAY,
        "-hide_banner",
        "-loglevel",
        "error",
        "-nodisp",
        "-autoexit",
        "-infbuf",
        "-f",
        PCM_FORMAT,
        "-ar",
        str(PCM_RATE),
        "-ac",
        str(PCM_CHANNELS),
        "-i",
        "-",
    ]


def pcm_player() -> subprocess.Popen[bytes]:
    """Start an ffplay that plays canonical PCM written to its stdin.

    "bufsize=0" so writes reach ffplay immediately (low playback latency).
    """
    require(FFPLAY)
    return subprocess.Popen(
        pcm_player_args(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        bufsize=0,
    )
