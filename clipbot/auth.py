"""Web UI password.

The password is stored as a salted scrypt hash in DATA_DIR/web_password, set with
`docker exec -it clip-bot clipbot set-password`. Until one is set the web UI stays locked,
so it's never open on the internet by accident.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

PASSWORD_FILE = "web_password"
SECRET_KEY_FILE = "web_secret_key"
MIN_LENGTH = 10

# Failed logins per client in a sliding window before that client has to wait.
MAX_FAILURES = 5
WINDOW_SECONDS = 15 * 60


def password_hash(data: Path) -> str:
    try:
        return (data / PASSWORD_FILE).read_text().strip()
    except OSError:
        return ""


def set_password(data: Path, password: str) -> None:
    if len(password) < MIN_LENGTH:
        raise ValueError(f"use at least {MIN_LENGTH} characters")
    path = data / PASSWORD_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(generate_password_hash(password))
    os.chmod(tmp, 0o600)
    if os.getuid() == 0:
        # `docker exec` runs as root, but the web UI runs as PUID:PGID and must be able to read it.
        os.chown(tmp, int(os.environ.get("PUID", "99")), int(os.environ.get("PGID", "100")))
    tmp.replace(path)


def fingerprint(pw_hash: str) -> str:
    """What the session cookie stores: changes whenever the password does, reveals nothing."""
    return hashlib.sha256(pw_hash.encode()).hexdigest()[:32]


def check(data: Path, password: str) -> bool:
    h = password_hash(data)
    return bool(h) and check_password_hash(h, password)


def secret_key(data: Path) -> str:
    """Persisted so logins survive a container restart."""
    path = data / SECRET_KEY_FILE
    try:
        key = path.read_text().strip()
        if len(key) >= 32:
            return key
    except OSError:
        pass
    key = secrets.token_hex(32)
    try:
        path.write_text(key)
        os.chmod(path, 0o600)
    except OSError:
        pass  # read-only data dir: fall back to a per-start key
    return key


class Limiter:
    def __init__(self, max_failures: int = MAX_FAILURES, window: float = WINDOW_SECONDS):
        self.max, self.window = max_failures, window
        self.fails: dict[str, list[float]] = {}

    def _recent(self, who: str) -> list[float]:
        now = time.time()
        recent = [t for t in self.fails.get(who, []) if now - t < self.window]
        if recent:
            self.fails[who] = recent
        else:
            self.fails.pop(who, None)
        return recent

    def blocked_for(self, who: str) -> int:
        """Seconds until this client may try again (0 = allowed)."""
        recent = self._recent(who)
        if len(recent) < self.max:
            return 0
        return int(recent[0] + self.window - time.time()) + 1

    def fail(self, who: str) -> None:
        self.fails.setdefault(who, []).append(time.time())

    def reset(self, who: str) -> None:
        self.fails.pop(who, None)
