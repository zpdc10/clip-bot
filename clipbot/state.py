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
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS clips_status ON clips(status);
"""

# Lifecycle: queued -> processing -> rendered -> uploaded, or failed at any point.
STATUSES = ("queued", "processing", "rendered", "uploaded", "failed")


@dataclass(frozen=True)
class Clip:
    id: int
    fingerprint: str
    rel_path: str
    size: int
    status: str
    error: str | None


class State:
    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA)
        self._db.commit()

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

    def set_status(self, clip_id: int, status: str, error: str | None = None) -> None:
        if status not in STATUSES:
            raise ValueError(f"unknown status {status!r}")
        self._db.execute(
            "UPDATE clips SET status = ?, error = ?, updated_at = ? WHERE id = ?",
            (status, error, time.time(), clip_id),
        )
        self._db.commit()

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
    )
