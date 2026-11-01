"""Find new clips in the watch folder and decide when each one has finished copying.

Polling is used instead of inotify on purpose: files arriving over SMB/NFS on Unraid
(especially via the FUSE /mnt/user path) don't reliably raise inotify events inside a container.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .state import State

log = logging.getLogger(__name__)

# Bytes read from each end of a file to fingerprint it. Hashing whole multi-GB clips over the
# network on every scan would be slow; size + head + tail is unique enough for clip dedupe.
_SAMPLE = 1024 * 1024

# Partial-download / temp suffixes some copy tools leave while writing.
_TEMP_SUFFIXES = {".part", ".partial", ".tmp", ".crdownload", ".download"}


def fingerprint(path: Path) -> str:
    size = path.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with path.open("rb") as f:
        h.update(f.read(_SAMPLE))
        if size > _SAMPLE:
            f.seek(max(size - _SAMPLE, _SAMPLE))
            h.update(f.read(_SAMPLE))
    return h.hexdigest()


@dataclass
class _Seen:
    size: int
    mtime: float


class Watcher:
    def __init__(
        self,
        watch_dir: Path,
        state: State,
        extensions: frozenset[str],
        settle_seconds: int,
        clock: Callable[[], float] = time.time,
    ):
        self.watch_dir = watch_dir
        self.state = state
        self.extensions = extensions
        self.settle_seconds = settle_seconds
        self._clock = clock
        # Size/mtime from the previous scan, per path. A file is only "done copying" once it
        # looks identical on two scans in a row and hasn't been written for settle_seconds.
        self._pending: dict[Path, _Seen] = {}
        # Paths already handled (queued or recognised as duplicates), keyed to their size/mtime,
        # so we don't re-hash them every scan. If the file changes, it's looked at again.
        self._done: dict[Path, _Seen] = {}

    def reconfigure(self, watch_dir: Path, extensions: frozenset[str], settle_seconds: int) -> None:
        if watch_dir != self.watch_dir:
            self._pending.clear()
            self._done.clear()
        self.watch_dir = watch_dir
        self.extensions = extensions
        self.settle_seconds = settle_seconds

    def _candidates(self) -> list[Path]:
        out = []
        for p in self.watch_dir.rglob("*"):
            name = p.name
            if name.startswith(".") or p.suffix.lower() in _TEMP_SUFFIXES:
                continue
            if p.suffix.lower() not in self.extensions:
                continue
            try:
                if p.is_file():
                    out.append(p)
            except OSError:
                continue
        return sorted(out)

    def baseline(self, reason: str) -> int:
        """Record every clip already in the folder as skipped, without rendering anything.
        Used on first start so only clips exported from now on get posted."""
        if not self.watch_dir.is_dir():
            return 0
        n = 0
        for path in self._candidates():
            try:
                st = path.stat()
                if st.st_size == 0:
                    continue
                fp = fingerprint(path)
            except OSError:
                continue
            if self.state.add_skipped(fp, str(path.relative_to(self.watch_dir)), st.st_size, reason):
                n += 1
            self._done[path] = _Seen(st.st_size, st.st_mtime)
        return n

    def scan(self) -> list[int]:
        """Run one pass over the folder. Returns ids of clips newly added to the queue."""
        if not self.watch_dir.is_dir():
            log.warning("watch folder %s is missing or not mounted", self.watch_dir)
            return []

        now = self._clock()
        queued: list[int] = []
        present = set()

        for path in self._candidates():
            present.add(path)
            try:
                st = path.stat()
            except OSError:
                continue
            seen = _Seen(st.st_size, st.st_mtime)

            if self._done.get(path) == seen:
                continue

            prev = self._pending.get(path)
            self._pending[path] = seen
            if prev != seen or seen.size == 0:
                continue  # first sighting, or still being written
            if now - seen.mtime < self.settle_seconds:
                continue

            try:
                fp = fingerprint(path)
            except OSError as exc:
                log.debug("can't read %s yet: %s", path, exc)
                continue

            rel = str(path.relative_to(self.watch_dir))
            clip = self.state.enqueue(fp, rel, seen.size)
            if clip:
                log.info("queued clip #%d: %s (%.1f MB)", clip.id, rel, seen.size / 1e6)
                queued.append(clip.id)
            else:
                log.debug("skipping %s, already processed", rel)
            self._pending.pop(path, None)
            self._done[path] = seen

        # Forget files that were deleted or moved away.
        for gone in set(self._pending) - present:
            del self._pending[gone]
        for gone in set(self._done) - present:
            del self._done[gone]

        return queued
