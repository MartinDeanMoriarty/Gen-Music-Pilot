"""Typed configuration: load, resolve paths, validate.

"load_config()" reads "config/config.json" (shape documented in
"config/config.example.json" and REBUILD_PLAN.md section 10), resolves every
relative path against the repo root, and returns a frozen :class:'Config'.

Validation collects *every* problem before failing: a bad config raises a single
:class:'ConfigError' whose message lists all of them, so one run surfaces the
whole list instead of one error at a time.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger("pilot.config")

MODES = ("generative", "user", "mixed")
STREAM_BACKENDS = ("pyfanout", "icecast")
CODECS = ("mp3", "aac")

_MISSING = object()

_TOP_LEVEL_KEYS = {
    "comfy_url",
    "comfy",
    "assets_dir",
    "music",
    "library_dir",
    "buffer_target_seconds",
    "mode",
    "local_audio",
    "retention",
    "station",
    "ollama",
    "weather",
    "stream",
    "web",
    "workflows",
    "user_music_dirs",
}


class ConfigError(Exception):
    """Config file missing, unparseable, or invalid. Message lists every problem."""


# --------------------------------------------------------------------------
# dataclasses
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class RetentionConfig:
    max_age_minutes: int
    max_tracks: int


@dataclass(frozen=True)
class StationConfig:
    name: str
    persona_file: Path  # may not exist yet; created in build step 4


@dataclass(frozen=True)
class ComfyConfig:
    request_timeout: float = 10.0
    job_timeout: float = 600.0
    poll_interval: float = 1.5


@dataclass(frozen=True)
class MusicConfig:
    min_length_seconds: int = 120
    max_length_seconds: int = 300


@dataclass(frozen=True)
class OllamaConfig:
    url: str
    model: str


@dataclass(frozen=True)
class UserMusicDir:
    name: str
    path: Path


@dataclass(frozen=True)
class WeatherConfig:
    apikey: str
    location: str
    ttl_minutes: int = 60

    @property
    def enabled(self) -> bool:
        return bool(self.apikey)


@dataclass(frozen=True)
class IcecastConfig:
    host: str
    port: int
    mount: str
    source_password: str


@dataclass(frozen=True)
class StreamConfig:
    enabled: bool
    backend: str
    host: str
    port: int
    codec: str
    bitrate_kbps: int
    icecast: IcecastConfig | None


@dataclass(frozen=True)
class WebConfig:
    host: str
    port: int
    share: bool
    auto_start: bool


@dataclass(frozen=True)
class WorkflowsConfig:
    moderation: Path
    story: Path
    music: Path


@dataclass(frozen=True)
class Config:
    comfy_url: str
    comfy: ComfyConfig
    assets_dir: Path
    music: MusicConfig
    library_dir: Path
    buffer_target_seconds: int
    mode: str
    local_audio: bool
    retention: RetentionConfig
    station: StationConfig
    ollama: OllamaConfig
    weather: WeatherConfig
    stream: StreamConfig
    web: WebConfig
    workflows: WorkflowsConfig
    user_music_dirs: tuple[UserMusicDir, ...]
    repo_root: Path
    source_path: Path

    def summary(self) -> str:
        dirs = ", ".join(d.name for d in self.user_music_dirs) or "(none)"
        weather = "on" if self.weather.enabled else "off"
        if self.weather.enabled:
            weather += f" ({self.weather.location})"
        stream = "off"
        if self.stream.enabled:
            stream = (
                f"on [{self.stream.backend} {self.stream.host}:{self.stream.port} "
                f"{self.stream.codec}/{self.stream.bitrate_kbps}k]"
            )
        return "\n".join(
            [
                f"comfy_url        {self.comfy_url}",
                f"library_dir      {self.library_dir}",
                f"buffer_target    {self.buffer_target_seconds}s",
                f"mode             {self.mode}",
                f"local_audio      {self.local_audio}",
                f"stream           {stream}",
                f"web              {self.web.host}:{self.web.port}"
                + (" (auto-start)" if self.web.auto_start else ""),
                f"ollama           {self.ollama.model} @ {self.ollama.url}",
                f"weather          {weather}",
                f"user_music_dirs  {dirs}",
                f"retention        {self.retention.max_tracks} tracks / "
                f"{self.retention.max_age_minutes} min",
            ]
        )


# --------------------------------------------------------------------------
# loading
# --------------------------------------------------------------------------


def _typecheck(val: Any, typ: type) -> bool:
    if typ is bool:
        return isinstance(val, bool)
    if typ is int:
        return isinstance(val, int) and not isinstance(val, bool)
    if typ is float:
        return isinstance(val, (int, float)) and not isinstance(val, bool)
    return isinstance(val, typ)


class _Loader:
    def __init__(self, raw: dict, repo_root: Path, source: Path) -> None:
        self.raw = raw
        self.root = repo_root
        self.source = source
        self.errors: list[str] = []
        self.warnings: list[str] = []

    # -- primitives --------------------------------------------------------

    def _field(
        self,
        d: dict,
        key: str,
        typ: type,
        where: str,
        *,
        default: Any = _MISSING,
        choices: tuple | None = None,
    ) -> Any:
        if key not in d:
            if default is _MISSING:
                self.errors.append(f"{where}: missing required key '{key}'")
                return None
            return default
        val = d[key]
        if typ is float and _typecheck(val, int):
            val = float(val)
        if not _typecheck(val, typ):
            self.errors.append(
                f"{where}.{key}: expected {typ.__name__}, got {type(val).__name__}"
            )
            return None
        if choices is not None and val not in choices:
            self.errors.append(f"{where}.{key}: {val!r} is not one of {choices}")
            return None
        return val

    def _positive_int(
        self, d: dict, key: str, where: str, *, default: Any = _MISSING
    ) -> Any:
        val = self._field(d, key, int, where, default=default)
        if isinstance(val, int) and val <= 0:
            self.errors.append(f"{where}.{key}: must be > 0, got {val}")
            return None
        return val

    def _section(self, key: str, *, required: bool = True) -> dict:
        if key not in self.raw:
            if required:
                self.errors.append(f"missing required section '{key}'")
            return {}
        val = self.raw[key]
        if not isinstance(val, dict):
            self.errors.append(f"{key}: expected an object, got {type(val).__name__}")
            return {}
        return val

    def _resolve(self, raw_path: str) -> Path:
        p = Path(raw_path).expanduser()
        return p if p.is_absolute() else (self.root / p)

    # -- sections --------------------------------------------------------

    def _retention(self) -> RetentionConfig:
        d = self._section("retention", required=False)
        return RetentionConfig(
            max_age_minutes=self._positive_int(d, "max_age_minutes", "retention", default=60)
            or 60,
            max_tracks=self._positive_int(d, "max_tracks", "retention", default=40) or 40,
        )

    def _station(self) -> StationConfig:
        d = self._section("station", required=False)
        name = self._field(d, "name", str, "station", default="Gen-Music-Pilot")
        persona = self._field(
            d, "persona_file", str, "station", default="config/persona.md"
        )
        return StationConfig(
            name=name or "Gen-Music-Pilot",
            persona_file=self._resolve(persona or "config/persona.md"),
        )

    def _comfy(self) -> ComfyConfig:
        d = self._section("comfy", required=False)
        defaults = ComfyConfig()
        return ComfyConfig(
            request_timeout=self._field(
                d, "request_timeout", float, "comfy", default=defaults.request_timeout
            )
            or defaults.request_timeout,
            job_timeout=self._field(
                d, "job_timeout", float, "comfy", default=defaults.job_timeout
            )
            or defaults.job_timeout,
            poll_interval=self._field(
                d, "poll_interval", float, "comfy", default=defaults.poll_interval
            )
            or defaults.poll_interval,
        )

    def _ollama(self) -> OllamaConfig:
        d = self._section("ollama", required=False)
        return OllamaConfig(
            url=self._field(d, "url", str, "ollama", default="http://127.0.0.1:11434")
            or "http://127.0.0.1:11434",
            model=self._field(d, "model", str, "ollama") or "",
        )

    def _music(self) -> MusicConfig:
        d = self._section("music", required=False)
        lo = self._positive_int(d, "min_length_seconds", "music", default=120) or 120
        hi = self._positive_int(d, "max_length_seconds", "music", default=300) or 300
        if lo > hi:
            self.errors.append(
                f"music.min_length_seconds ({lo}) > max_length_seconds ({hi})"
            )
        return MusicConfig(min_length_seconds=lo, max_length_seconds=hi)

    def _weather(self) -> WeatherConfig:
        d = self._section("weather", required=False)
        apikey = self._field(d, "apikey", str, "weather", default="") or ""
        location = self._field(d, "location", str, "weather", default="") or ""
        if apikey and not location:
            self.errors.append("weather.location: required when weather.apikey is set")
        ttl = self._positive_int(d, "ttl_minutes", "weather", default=60) or 60
        return WeatherConfig(apikey=apikey, location=location, ttl_minutes=ttl)

    def _web(self) -> WebConfig:
        d = self._section("web", required=False)
        host = self._field(d, "host", str, "web", default="127.0.0.1") or "127.0.0.1"
        port = self._positive_int(d, "port", "web", default=7860) or 7860
        share = self._field(d, "share", bool, "web", default=False)
        auto_start = self._field(d, "auto_start", bool, "web", default=False)
        return WebConfig(
            host=host, port=port, share=bool(share), auto_start=bool(auto_start)
        )

    def _stream(self) -> StreamConfig:
        d = self._section("stream", required=False)
        enabled = self._field(d, "enabled", bool, "stream", default=False)
        backend = self._field(
            d, "backend", str, "stream", default="pyfanout", choices=STREAM_BACKENDS
        )
        bind = self._field(d, "bind", str, "stream", default="0.0.0.0:8080")
        host, port = "0.0.0.0", 8080
        if isinstance(bind, str):
            if ":" not in bind:
                self.errors.append(f"stream.bind: expected 'host:port', got {bind!r}")
            else:
                host, _, raw_port = bind.rpartition(":")
                try:
                    port = int(raw_port)
                    if not 0 <= port <= 65535:  # 0 -> pick a free port
                        raise ValueError
                except ValueError:
                    self.errors.append(f"stream.bind: invalid port in {bind!r}")
        codec = self._field(d, "codec", str, "stream", default="mp3", choices=CODECS)
        bitrate = self._positive_int(d, "bitrate_kbps", "stream", default=128) or 128

        icecast = None
        ice_raw = d.get("icecast")
        if ice_raw is not None and not isinstance(ice_raw, dict):
            self.errors.append("stream.icecast: expected an object")
        elif isinstance(ice_raw, dict):
            icecast = IcecastConfig(
                host=self._field(ice_raw, "host", str, "stream.icecast", default="127.0.0.1")
                or "127.0.0.1",
                port=self._positive_int(ice_raw, "port", "stream.icecast", default=8000)
                or 8000,
                mount=self._field(ice_raw, "mount", str, "stream.icecast", default="/live")
                or "/live",
                source_password=self._field(
                    ice_raw, "source_password", str, "stream.icecast", default=""
                )
                or "",
            )
        if backend == "icecast" and icecast is None:
            self.errors.append("stream.icecast: required when stream.backend is 'icecast'")

        return StreamConfig(
            enabled=bool(enabled),
            backend=backend or "pyfanout",
            host=host or "0.0.0.0",
            port=port,
            codec=codec or "mp3",
            bitrate_kbps=bitrate,
            icecast=icecast,
        )

    def _workflows(self) -> WorkflowsConfig:
        d = self._section("workflows", required=False)
        defaults = {
            "moderation": "workflows/Radio_TTS.json",
            "story": "workflows/Radio_Story.json",
            "music": "workflows/Radio_Music+Lyrics.json",
        }
        resolved = {}
        for key, dflt in defaults.items():
            raw_path = self._field(d, key, str, "workflows", default=dflt) or dflt
            path = self._resolve(raw_path)
            resolved[key] = path
            if not path.exists():
                self.warnings.append(f"workflows.{key}: file not found: {path}")
        return WorkflowsConfig(**resolved)

    def _user_music_dirs(self) -> tuple[UserMusicDir, ...]:
        raw = self.raw.get("user_music_dirs", [])
        if not isinstance(raw, list):
            self.errors.append("user_music_dirs: expected a list")
            return ()
        out: list[UserMusicDir] = []
        seen: set[str] = set()
        for i, entry in enumerate(raw):
            where = f"user_music_dirs[{i}]"
            if not isinstance(entry, dict):
                self.errors.append(f"{where}: expected an object")
                continue
            name = self._field(entry, "name", str, where)
            path = self._field(entry, "path", str, where)
            if not name or not path:
                continue
            if name in seen:
                self.warnings.append(f"{where}: duplicate name {name!r}")
            seen.add(name)
            resolved = self._resolve(path)
            if not resolved.is_dir():
                self.warnings.append(f"{where}: directory not found: {resolved}")
            out.append(UserMusicDir(name=name, path=resolved))
        return tuple(out)

    # -- top level ------------------------------------------------------

    def build(self) -> Config:
        for key in self.raw:
            if key not in _TOP_LEVEL_KEYS:
                self.warnings.append(f"unknown top-level key {key!r} (ignored)")

        comfy_url = self._field(self.raw, "comfy_url", str, "config") or ""
        library_dir = self._resolve(
            self._field(self.raw, "library_dir", str, "config", default="media") or "media"
        )
        assets_dir = self._resolve(
            self._field(self.raw, "assets_dir", str, "config", default="assets")
            or "assets"
        )
        buffer_target = (
            self._positive_int(self.raw, "buffer_target_seconds", "config", default=420)
            or 420
        )
        mode = self._field(
            self.raw, "mode", str, "config", default="generative", choices=MODES
        )
        local_audio = self._field(self.raw, "local_audio", bool, "config", default=True)

        comfy = self._comfy()
        music = self._music()
        retention = self._retention()
        station = self._station()
        ollama = self._ollama()
        weather = self._weather()
        stream = self._stream()
        web = self._web()
        workflows = self._workflows()
        user_music_dirs = self._user_music_dirs()

        if not local_audio and not stream.enabled:
            self.errors.append(
                "config: no audio output - set local_audio or stream.enabled"
            )
        if mode in ("user", "mixed") and not user_music_dirs:
            self.errors.append(
                f"config.mode is {mode!r} but user_music_dirs is empty"
            )

        return Config(
            comfy_url=comfy_url,
            comfy=comfy,
            assets_dir=assets_dir,
            music=music,
            library_dir=library_dir,
            buffer_target_seconds=buffer_target,
            mode=mode or "generative",
            local_audio=bool(local_audio),
            retention=retention,
            station=station,
            ollama=ollama,
            weather=weather,
            stream=stream,
            web=web,
            workflows=workflows,
            user_music_dirs=user_music_dirs,
            repo_root=self.root,
            source_path=self.source,
        )


def load_config(
    path: str | Path | None = None, *, repo_root: Path | None = None
) -> Config:
    """Load and validate the config. Raise :class:'ConfigError' listing all problems."""
    root = (repo_root or Path(__file__).resolve().parents[1]).resolve()
    if path is not None:
        cfg_path = Path(path).expanduser()
        if not cfg_path.is_absolute():
            cfg_path = (root / cfg_path).resolve()
    else:
        cfg_path = root / "config" / "config.json"

    if not cfg_path.exists():
        raise ConfigError(
            f"config file not found: {cfg_path}\n"
            "  copy config/config.example.json to config/config.json and edit it"
        )

    try:
        raw = json.loads(cfg_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ConfigError(
            f"{cfg_path}: invalid JSON at line {e.lineno} column {e.colno}: {e.msg}"
        ) from e
    if not isinstance(raw, dict):
        raise ConfigError(f"{cfg_path}: top level must be a JSON object")

    loader = _Loader(raw, root, cfg_path)
    cfg = loader.build()

    for warning in loader.warnings:
        log.warning("%s", warning)

    if loader.errors:
        raise ConfigError(
            f"{cfg_path}: {len(loader.errors)} problem(s):\n"
            + "\n".join(f"  - {e}" for e in loader.errors)
        )
    return cfg
