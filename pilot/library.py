"""The owned media library.

ComfyUI is a generator, not a store (REBUILD_PLAN.md section 4). Every audio
asset the radio plays lives here, named by track id:

    <root>/tracks/<id>.mp3      owned generated tracks (GC'd)
    <root>/tracks/.tmp/         staging area for atomic publish
    <root>/imported/<key>.mp3   normalised copies of user music, content-keyed
                                and shared across plays (never GC'd)
    <root>/fallback/*.mp3       curated fallback pool (never GC'd)

"publish_atomic" is the only way a file enters "tracks/": it stages a full
copy under ".tmp/" and "os.replace"s it into place, so a reader never sees a
half-written file. "scan_recovery" reconciles disk and DB on startup.
"""

from __future__ import annotations

import hashlib
import logging
import os
import random
import shutil
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pilot import ffmpeg
from pilot.config import RetentionConfig
from pilot.domain import Track, TrackKind, TrackState
from pilot.store import Store

log = logging.getLogger("pilot.library")

_TERMINAL = (TrackState.PLAYED, TrackState.FAILED)


@dataclass(frozen=True)
class RecoveryReport:
    failed_unfinished: int = 0
    missing_files: int = 0
    finished_playing: int = 0
    orphan_files: int = 0
    dropped_segments: int = 0
    queued_segments: int = 0

    def __str__(self) -> str:
        return (
            f"{self.failed_unfinished} unfinished -> failed, "
            f"{self.missing_files} ready w/o file -> failed, "
            f"{self.finished_playing} playing -> played, "
            f"{self.orphan_files} orphan file(s) removed, "
            f"{self.dropped_segments} broken segment(s) dropped, "
            f"{self.queued_segments} segment(s) recovered"
        )


class Library:
    def __init__(self, root: str | Path, store: Store) -> None:
        self.root = Path(root)
        self.store = store
        self.tracks_dir = self.root / "tracks"
        self.tmp_dir = self.tracks_dir / ".tmp"
        self.imported_dir = self.root / "imported"
        self.fallback_dir = self.root / "fallback"
        for d in (self.tracks_dir, self.tmp_dir, self.imported_dir, self.fallback_dir):
            d.mkdir(parents=True, exist_ok=True)

    # -- paths -----------------------------------------------------

    def track_path(self, track_id: str) -> Path:
        return self.tracks_dir / f"{track_id}.mp3"

    def _owns(self, path: Path | None) -> bool:
        if path is None:
            return False
        try:
            return path.resolve().parent == self.tracks_dir.resolve()
        except OSError:
            return False

    # -- publish -------------------------------------------------

    def publish_atomic(self, track_id: str, src: str | Path) -> Path:
        """Copy "src" into the library as "<id>.mp3". Atomic: on failure
        nothing lands in "tracks/"."""
        src = Path(src)
        if not src.is_file():
            raise FileNotFoundError(f"source not found: {src}")
        dest = self.track_path(track_id)
        staging = self.tmp_dir / f"{track_id}.part"
        try:
            with open(src, "rb") as fsrc, open(staging, "wb") as fdst:
                shutil.copyfileobj(fsrc, fdst)
                fdst.flush()
                os.fsync(fdst.fileno())
            os.replace(staging, dest)
        except BaseException:
            staging.unlink(missing_ok=True)
            raise
        return dest

    def import_external(
        self,
        src: str | Path,
        *,
        kind: TrackKind,
        normalise: bool = True,
        track_id: str | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Track:
        """Bring an outside file into the library and record it as a READY track.

        The file lands in "imported/<key>.mp3" where "<key>" is derived from
        the source path + size + mtime, so the same file is loudnorm-re-encoded
        ("normalise") only once and reused on every later play. Tags from the
        file are merged under "meta".
        """
        src = Path(src)
        if not src.is_file():
            raise FileNotFoundError(f"source not found: {src}")

        dest = self.imported_dir / f"{_cache_key(src)}.mp3"
        if dest.exists():
            log.debug("import cache hit: %s", src.name)
        else:
            self._materialise(src, dest, normalise=normalise)

        probe = ffmpeg.probe(dest)
        tags = {
            k: probe.tags[k]
            for k in ("title", "artist", "genre", "album")
            if probe.tags.get(k)
        }
        track = Track.ready(
            kind, dest, probe.duration_s, **{**tags, "source_path": str(src), **(meta or {})}
        )
        if track_id:
            track.id = track_id
        self.store.upsert_track(track)
        return track

    def _materialise(self, src: Path, dest: Path, *, normalise: bool) -> None:
        staged = self.tmp_dir / f"{dest.stem}.import.mp3"
        try:
            if normalise:
                ffmpeg.loudnorm(src, staged)
            else:
                with open(src, "rb") as fsrc, open(staged, "wb") as fdst:
                    shutil.copyfileobj(fsrc, fdst)
                    fdst.flush()
                    os.fsync(fdst.fileno())
            os.replace(staged, dest)
        except BaseException:
            staged.unlink(missing_ok=True)
            raise

    # -- fallbacks ---------------------------------------------

    def fallbacks(self) -> list[Path]:
        return sorted(
            p
            for p in self.fallback_dir.glob("*")
            if p.suffix.lower() in (".mp3", ".m4a", ".ogg", ".flac", ".wav")
        )

    def random_fallback(self) -> Track | None:
        candidates = self.fallbacks()
        if not candidates:
            return None
        choice = random.choice(candidates)
        try:
            probe = ffmpeg.probe(choice)
        except ffmpeg.FFmpegError:
            log.warning("fallback %s could not be probed", choice)
            return None
        return Track.fallback(
            choice,
            probe.duration_s,
            title=probe.tag("title") or choice.stem,
            artist=probe.tag("artist"),
        )

    # -- garbage collection ----------------------------------

    def gc(self, retention: RetentionConfig) -> int:
        """Delete files + rows for finished tracks past the retention limits.
        Returns the number removed."""
        cutoff = datetime.now() - timedelta(minutes=retention.max_age_minutes)
        finished = [
            t
            for t in self.store.tracks_by_state(*_TERMINAL)
            if t.created_at < cutoff
        ]
        removed = 0
        for track in finished:
            self._remove(track)
            removed += 1

        owned = self.store.tracks_by_state()  # created_at order
        overflow = len(owned) - retention.max_tracks
        if overflow > 0:
            purgeable = [t for t in owned if t.state in _TERMINAL]
            for track in purgeable[:overflow]:
                self._remove(track)
                removed += 1
        return removed

    def _remove(self, track: Track) -> None:
        # only generated tracks own their file; imported/ and fallback/ are shared
        if self._owns(track.path):
            track.path.unlink(missing_ok=True)  # type: ignore[union-attr]
        self.store.delete_track(track.id)

    # -- startup recovery ----------------------------------

    def scan_recovery(self) -> RecoveryReport:
        failed = missing = played = orphans = 0

        for track in self.store.tracks_by_state():
            if track.state in (TrackState.PLANNED, TrackState.GENERATING):
                track.to_state(TrackState.FAILED, reason="interrupted by restart")
                self.store.upsert_track(track)
                failed += 1
            elif track.state is TrackState.PLAYING:
                track.to_state(TrackState.PLAYED)
                self.store.upsert_track(track)
                played += 1
            elif track.state is TrackState.READY and not (
                track.path and track.path.exists()
            ):
                track.to_state(TrackState.FAILED, reason="file missing after restart")
                self.store.upsert_track(track)
                missing += 1

        # staging files are always garbage after a restart
        for part in self.tmp_dir.iterdir():
            part.unlink(missing_ok=True)

        known = {t.id for t in self.store.tracks_by_state()}
        for f in self.tracks_dir.glob("*.mp3"):
            if f.stem not in known:
                f.unlink(missing_ok=True)
                orphans += 1

        before = self.store.load_queue()
        good = [
            seg
            for seg in before
            if all(t.state is TrackState.READY for t in seg.tracks)
        ]
        if len(good) != len(before):
            self.store.save_queue(good)

        report = RecoveryReport(
            failed_unfinished=failed,
            missing_files=missing,
            finished_playing=played,
            orphan_files=orphans,
            dropped_segments=len(before) - len(good),
            queued_segments=len(good),
        )
        return report


def _cache_key(src: Path) -> str:
    """Stable per-file key: path + size + mtime. Editing the file (new size or
    mtime) yields a new key, so it is re-imported."""
    st = src.stat()
    raw = f"{src.resolve()}|{st.st_size}|{st.st_mtime_ns}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
