"""SQLite-backed record of every clip the bot has seen, so nothing is processed twice."""

from __future__ import annotations

import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    rel_path    TEXT NOT NULL,
    size        INTEGER NOT NULL,
    status      TEXT NOT NULL DEFAULT 'queued',
    error       TEXT,
    output_path TEXT,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""

# Lifecycle: queued -> processing -> rendered -> uploaded, or failed at any point.
# "skipped" is set from the web UI for clips that should never be uploaded.
STATUSES = ("queued", "processing", "rendered", "uploaded", "failed", "skipped")


@dataclass(frozen=True)
class Clip:
    id: int
    fingerprint: str
    rel_path: str
    size: int
    status: str
    error: str | None
    output_path: str | None = None
    title: str | None = None
    video_id: str | None = None
    uploaded_at: float | None = None
    approved: bool = False
    created_at: float = 0.0
    attempts: int = 0
    retry_at: float | None = None


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        # The worker loop and the web UI each open their own State; wait on locks instead of failing.
        self._db = sqlite3.connect(db_path, timeout=15, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._migrate()
        self._db.commit()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._db.execute("PRAGMA table_info(clips)")}
        for col, typ in (("output_path", "TEXT"), ("title", "TEXT"), ("video_id", "TEXT"), ("uploaded_at", "REAL"),
                         ("approved", "INTEGER NOT NULL DEFAULT 0"),
                         ("attempts", "INTEGER NOT NULL DEFAULT 0"), ("retry_at", "REAL")):
            if col not in cols:
                self._db.execute(f"ALTER TABLE clips ADD COLUMN {col} {typ}")

    def close(self) -> None:
        self._db.close()

    def has_fingerprint(self, fingerprint: str) -> bool:
        row = self._db.execute("SELECT 1 FROM clips WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row is not None

    def enqueue(self, fingerprint: str, rel_path: str, size: int) -> Clip | None:
        """Add a clip to the queue. Returns None if this exact clip was already recorded."""
        now = time.time()
        cur = self._db.execute(
            "INSERT OR IGNORE INTO clips (fingerprint, rel_path, size, status, created_at, updated_at)"
            " VALUES (?, ?, ?, 'queued', ?, ?)",
            (fingerprint, rel_path, size, now, now),
        )
        self._db.commit()
        if cur.rowcount == 0:
            return None
        return self.get(cur.lastrowid)

    def get(self, clip_id: int) -> Clip | None:
        row = self._db.execute("SELECT * FROM clips WHERE id = ?", (clip_id,)).fetchone()
        return _to_clip(row) if row else None

    def by_status(self, status: str) -> list[Clip]:
        rows = self._db.execute("SELECT * FROM clips WHERE status = ? ORDER BY id", (status,)).fetchall()
        return [_to_clip(r) for r in rows]

    def set_status(
        self, clip_id: int, status: str, error: str | None = None, output_path: str | None = None
    ) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self._db.execute(
            "UPDATE clips SET status = ?, error = ?, output_path = COALESCE(?, output_path),"
            " updated_at = ? WHERE id = ?",
            (status, error, output_path, time.time(), clip_id),
        )
        self._db.commit()

    def set_title(self, clip_id: int, title: str) -> None:
        self._db.execute("UPDATE clips SET title = ?, updated_at = ? WHERE id = ?", (title, time.time(), clip_id))
        self._db.commit()

    def mark_uploaded(self, clip_id: int, video_id: str, when: float) -> None:
        self._db.execute(
            "UPDATE clips SET status = 'uploaded', video_id = ?, uploaded_at = ?, error = NULL, retry_at = NULL,"
            " updated_at = ?"
            " WHERE id = ?",
            (video_id, when, time.time(), clip_id),
        )
        self._db.commit()

    def uploads_since(self, since: float) -> int:
        row = self._db.execute("SELECT COUNT(*) FROM clips WHERE uploaded_at >= ?", (since,)).fetchone()
        return row[0]

    def schedule_retry(self, clip_id: int, retry_at: float, error: str, count_attempt: bool = True) -> None:
        """Keep a clip 'rendered' but don't try it again before retry_at."""
        self._db.execute(
            "UPDATE clips SET status = 'rendered', error = ?, retry_at = ?, attempts = attempts + ?, updated_at = ?"
            " WHERE id = ?",
            (error, retry_at, int(count_attempt), time.time(), clip_id),
        )
        self._db.commit()

    def reset_attempts(self, clip_id: int) -> None:
        self._db.execute("UPDATE clips SET attempts = 0, retry_at = NULL, updated_at = ? WHERE id = ?",
                         (time.time(), clip_id))
        self._db.commit()

    def set_approved(self, clip_id: int, approved: bool = True) -> None:
        self._db.execute("UPDATE clips SET approved = ?, updated_at = ? WHERE id = ?",
                         (int(approved), time.time(), clip_id))
        self._db.commit()

    def recent(self, limit: int = 100, status: str | None = None) -> list[Clip]:
        if status:
            rows = self._db.execute("SELECT * FROM clips WHERE status = ? ORDER BY id DESC LIMIT ?",
                                    (status, limit)).fetchall()
        else:
            rows = self._db.execute("SELECT * FROM clips ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [_to_clip(r) for r in rows]

    def counts(self) -> dict[str, int]:
        out = {s: 0 for s in STATUSES}
        for status, n in self._db.execute("SELECT status, COUNT(*) FROM clips GROUP BY status"):
            out[status] = n
        return out

    def get_meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
        self._db.commit()

    def is_empty(self) -> bool:
        return self._db.execute("SELECT 1 FROM clips LIMIT 1").fetchone() is None

    def add_skipped(self, fingerprint: str, rel_path: str, size: int, reason: str) -> bool:
        clip = self.enqueue(fingerprint, rel_path, size)
        if clip is None:
            return False
        self.set_status(clip.id, "skipped", error=reason)
        return True

    def requeue_interrupted(self) -> int:
        """Clips left 'processing' by a crash or restart go back to the queue."""
        cur = self._db.execute(
            "UPDATE clips SET status = 'queued', updated_at = ? WHERE status = 'processing'", (time.time(),)
        )
        self._db.commit()
        return cur.rowcount


def _to_clip(row: sqlite3.Row) -> Clip:
    return Clip(
        id=row["id"],
        fingerprint=row["fingerprint"],
        rel_path=row["rel_path"],
        size=row["size"],
        status=row["status"],
        error=row["error"],
        output_path=row["output_path"],
        title=row["title"],
        video_id=row["video_id"],
        uploaded_at=row["uploaded_at"],
        approved=bool(row["approved"]),
        created_at=row["created_at"],
        attempts=row["attempts"],
        retry_at=row["retry_at"],
    )
