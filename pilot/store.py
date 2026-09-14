"""SQLite persistence: tracks, show history, and the timeline queue snapshot.

One :class:'sqlite3.Connection' ("check_same_thread=False") guarded by a single
lock -- every public method takes it, so the store is safe to share across the
producer, timeline and UI threads. WAL mode keeps reads non-blocking and
survives an unclean shutdown.

Schema changes go through "PRAGMA user_version" migrations in "_MIGRATIONS":
on open, every migration past the file's current version runs in order. An empty
file starts at version 0 and is migrated up to :data:'SCHEMA_VERSION'.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pilot.domain import (
    HistoryEntry,
    Segment,
    Track,
    TrackKind,
    TrackState,
)

SCHEMA_VERSION = 1


def _migration_1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE tracks (
            id              TEXT PRIMARY KEY,
            kind            TEXT NOT NULL,
            state           TEXT NOT NULL,
            path            TEXT,
            duration_s      REAL,
            comfy_prompt_id TEXT,
            error           TEXT,
            created_at      TEXT NOT NULL,
            updated_at      TEXT NOT NULL,
            meta            TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_tracks_state ON tracks(state);

        CREATE TABLE history (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            at                 TEXT NOT NULL,
            kind               TEXT NOT NULL,
            song_name          TEXT,
            artist             TEXT,
            genre              TEXT,
            dj_summary         TEXT,
            caller_interaction TEXT,
            had_story          INTEGER NOT NULL DEFAULT 0,
            track_id           TEXT,
            meta               TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX idx_history_at ON history(at);

        CREATE TABLE queue_snapshot (
            position        INTEGER PRIMARY KEY,
            segment_id      TEXT NOT NULL,
            talk_track_id   TEXT,
            music_track_id  TEXT NOT NULL,
            planned_airtime TEXT
        );
        """
    )


_MIGRATIONS = [_migration_1]  # index i migrates version i -> i+1


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        if str(self.path) != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        try:
            self._migrate()
        except BaseException:
            self._conn.close()
            raise

    # -- lifecycle ---------------------------------------------------

    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path}: schema version {version} is newer than this "
                    f"build supports ({SCHEMA_VERSION})"
                )
            for v in range(version, SCHEMA_VERSION):
                _MIGRATIONS[v](self._conn)  # executescript() commits its own DDL
                self._conn.execute(f"PRAGMA user_version = {v + 1}")
                self._conn.commit()

    @property
    def schema_version(self) -> int:
        with self._lock:
            return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- tracks ----------------------------------------------------

    def upsert_track(self, track: Track) -> None:
        now = datetime.now().isoformat()
        row = {
            "id": track.id,
            "kind": str(track.kind),
            "state": str(track.state),
            "path": str(track.path) if track.path is not None else None,
            "duration_s": track.duration_s,
            "comfy_prompt_id": track.comfy_prompt_id,
            "error": track.error,
            "created_at": track.created_at.isoformat(),
            "updated_at": now,
            "meta": json.dumps(track.meta, default=str),
        }
        with self._lock, self._conn:
            self._conn.execute(
                """
                INSERT INTO tracks
                    (id, kind, state, path, duration_s, comfy_prompt_id, error,
                     created_at, updated_at, meta)
                VALUES
                    (:id, :kind, :state, :path, :duration_s, :comfy_prompt_id,
                     :error, :created_at, :updated_at, :meta)
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind, state=excluded.state, path=excluded.path,
                    duration_s=excluded.duration_s,
                    comfy_prompt_id=excluded.comfy_prompt_id,
                    error=excluded.error, updated_at=excluded.updated_at,
                    meta=excluded.meta
                """,
                row,
            )

    def get_track(self, track_id: str) -> Track | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM tracks WHERE id = ?", (track_id,)
            ).fetchone()
        return _track_from_row(row) if row is not None else None

    def tracks_by_state(self, *states: TrackState) -> list[Track]:
        with self._lock:
            if states:
                placeholders = ",".join("?" * len(states))
                rows = self._conn.execute(
                    f"SELECT * FROM tracks WHERE state IN ({placeholders}) "
                    "ORDER BY created_at",
                    [str(s) for s in states],
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT * FROM tracks ORDER BY created_at"
                ).fetchall()
        return [_track_from_row(r) for r in rows]

    def delete_track(self, track_id: str) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM tracks WHERE id = ?", (track_id,))

    # -- history -------------------------------------------------

    def append_history(self, entry: HistoryEntry) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(
                """
                INSERT INTO history
                    (at, kind, song_name, artist, genre, dj_summary,
                     caller_interaction, had_story, track_id, meta)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    entry.at.isoformat(),
                    entry.kind,
                    entry.song_name,
                    entry.artist,
                    entry.genre,
                    entry.dj_summary,
                    entry.caller_interaction,
                    int(entry.had_story),
                    entry.track_id,
                    json.dumps(entry.meta, default=str),
                ),
            )
            new_id = int(cur.lastrowid)
        entry.id = new_id
        return new_id

    def recent_history(self, n: int = 10) -> list[HistoryEntry]:
        """Most recent first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM history ORDER BY id DESC LIMIT ?", (n,)
            ).fetchall()
        return [_history_from_row(r) for r in rows]

    # -- queue snapshot ----------------------------------------

    def save_queue(self, segments: Sequence[Segment]) -> None:
        """Replace the stored queue. Referenced tracks must already be upserted."""
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM queue_snapshot")
            self._conn.executemany(
                """
                INSERT INTO queue_snapshot
                    (position, segment_id, talk_track_id, music_track_id,
                     planned_airtime)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (
                        i,
                        seg.id,
                        seg.talk.id if seg.talk is not None else None,
                        seg.music.id,
                        seg.planned_airtime.isoformat()
                        if seg.planned_airtime is not None
                        else None,
                    )
                    for i, seg in enumerate(segments)
                ],
            )

    def load_queue(self) -> list[Segment]:
        """Rebuild the queue. Segments whose tracks are gone are dropped."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM queue_snapshot ORDER BY position"
            ).fetchall()
            out: list[Segment] = []
            for row in rows:
                music = self.get_track(row["music_track_id"])
                if music is None:
                    continue
                talk = (
                    self.get_track(row["talk_track_id"])
                    if row["talk_track_id"]
                    else None
                )
                if row["talk_track_id"] and talk is None:
                    continue
                out.append(
                    Segment(
                        music=music,
                        talk=talk,
                        id=row["segment_id"],
                        planned_airtime=_dt(row["planned_airtime"]),
                    )
                )
        return out


# --------------------------------------------------------------------------
# row <-> dataclass
# --------------------------------------------------------------------------


def _track_from_row(row: sqlite3.Row) -> Track:
    return Track(
        kind=TrackKind(row["kind"]),
        id=row["id"],
        state=TrackState(row["state"]),
        path=Path(row["path"]) if row["path"] else None,
        duration_s=row["duration_s"],
        comfy_prompt_id=row["comfy_prompt_id"],
        error=row["error"],
        created_at=datetime.fromisoformat(row["created_at"]),
        meta=json.loads(row["meta"]),
    )


def _history_from_row(row: sqlite3.Row) -> HistoryEntry:
    meta: dict[str, Any] = json.loads(row["meta"])
    return HistoryEntry(
        kind=row["kind"],
        at=datetime.fromisoformat(row["at"]),
        song_name=row["song_name"],
        artist=row["artist"],
        genre=row["genre"],
        dj_summary=row["dj_summary"],
        caller_interaction=row["caller_interaction"],
        had_story=bool(row["had_story"]),
        track_id=row["track_id"],
        id=row["id"],
        meta=meta,
    )
