"""Settings.

Values come from three layers, later ones winning:
  1. built-in defaults
  2. environment variables (handy in the Unraid container template)
  3. DATA_DIR/settings.json, which the web UI (step 4) will edit

DATA_DIR itself is only read from the environment, since it's where settings.json lives.
The main loop calls load() every cycle, so edits to settings.json apply without a restart.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path

log = logging.getLogger(__name__)

SETTINGS_FILE = "settings.json"


@dataclass(frozen=True)
class Config:
    # Folder the clips are exported to (the NAS share, mounted into the container).
    watch_dir: str = "/clips"
    # Seconds between folder scans.
    poll_interval: int = 15
    # A file must sit unchanged this long before it is treated as fully copied.
    settle_seconds: int = 30
    # Comma-separated video extensions to pick up.
    extensions: str = ".mp4,.mkv,.mov"
    log_level: str = "INFO"

    @property
    def watch_path(self) -> Path:
        return Path(self.watch_dir)

    @property
    def extension_set(self) -> frozenset[str]:
        out = set()
        for e in self.extensions.split(","):
            e = e.strip().lower()
            if e:
                out.add(e if e.startswith(".") else "." + e)
        return frozenset(out)


# Environment variable for each setting.
ENV_NAMES = {
    "watch_dir": "WATCH_DIR",
    "poll_interval": "POLL_INTERVAL",
    "settle_seconds": "SETTLE_SECONDS",
    "extensions": "CLIP_EXTENSIONS",
    "log_level": "LOG_LEVEL",
}


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _coerce(name: str, value, typ):
    if typ is int or typ == "int":
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a whole number, got {value!r}") from exc
    return str(value)


def _apply(cfg: Config, values: dict, source: str) -> Config:
    types = {f.name: f.type for f in fields(Config)}
    changes = {}
    for key, value in values.items():
        if key not in types:
            log.warning("ignoring unknown setting %r from %s", key, source)
            continue
        if value is None or (isinstance(value, str) and value.strip() == ""):
            continue
        changes[key] = _coerce(key, value, types[key])
    return replace(cfg, **changes)


def load(data: Path | None = None) -> Config:
    data = data or data_dir()
    cfg = Config()
    env = {key: os.environ.get(var) for key, var in ENV_NAMES.items()}
    cfg = _apply(cfg, env, "environment")

    path = data / SETTINGS_FILE
    if path.exists():
        try:
            saved = json.loads(path.read_text())
            if not isinstance(saved, dict):
                raise ValueError("expected a JSON object")
            cfg = _apply(cfg, saved, str(path))
        except (OSError, ValueError) as exc:
            log.error("couldn't read %s, using defaults and environment: %s", path, exc)
    cfg = replace(cfg, log_level=cfg.log_level.upper())
    return cfg


def save(cfg: Config, data: Path | None = None) -> None:
    """Write settings atomically, so a crash mid-write can't leave a broken file."""
    data = data or data_dir()
    data.mkdir(parents=True, exist_ok=True)
    path = data / SETTINGS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    tmp.replace(path)
