"""Keeps the last few hundred log lines in memory so the web UI can show them."""

from __future__ import annotations

import logging
from collections import deque

LINES: deque[str] = deque(maxlen=500)


class BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            LINES.append(self.format(record))
        except Exception:
            self.handleError(record)
