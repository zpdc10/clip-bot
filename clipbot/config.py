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

    # --- vertical render (step 2) ---
    # Where the 9:16 crop window sits: 0.0 = left edge, 0.5 = centre, 1.0 = right edge.
    crop_position: float = 0.5
    output_width: int = 1080
    output_height: int = 1920
    # Longest Short to produce, in seconds (YouTube allows up to 180). 0 = never trim.
    max_duration: int = 60
    # Which part to keep when a clip is too long: "end" (where the highlight usually is) or "start".
    trim: str = "end"
    # x264 quality, lower is better (18-23 is sensible) and preset (slower = smaller file).
    crf: int = 20
    x264_preset: str = "medium"

    # --- YouTube upload (step 3) ---
    upload_enabled: bool = True
    # When on, rendered clips wait for an "Approve" click in the web UI before uploading.
    require_approval: bool = False
    # private / unlisted / public. Starts private so you can check the first few.
    privacy: str = "private"
    # Uploads per rolling 24 h. YouTube's API allows 100/day; 0 = no cap.
    daily_upload_limit: int = 10
    # {title} is the generated title.
    title_template: str = "{title}"
    # {title} and {filename} are available. Use \n for new lines.
    description_template: str = "{title}\n\n#cs2 #counterstrike #shorts"
    tags: str = "cs2,counter-strike,counterstrike 2,shorts,gaming"
    # Optional: with a key, Claude writes the titles; without one, built-in rules do.
    anthropic_api_key: str = ""
    title_model: str = "claude-opus-5-5"

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
    "crop_position": "CROP_POSITION",
    "output_width": "OUTPUT_WIDTH",
    "output_height": "OUTPUT_HEIGHT",
    "max_duration": "MAX_DURATION",
    "trim": "TRIM",
    "crf": "CRF",
    "x264_preset": "X264_PRESET",
    "upload_enabled": "UPLOAD_ENABLED",
    "require_approval": "REQUIRE_APPROVAL",
    "privacy": "PRIVACY",
    "daily_upload_limit": "DAILY_UPLOAD_LIMIT",
    "title_template": "TITLE_TEMPLATE",
    "description_template": "DESCRIPTION_TEMPLATE",
    "tags": "TAGS",
    "anthropic_api_key": "ANTHROPIC_API_KEY",
    "title_model": "TITLE_MODEL",
}


def data_dir() -> Path:
    return Path(os.environ.get("DATA_DIR", "/data"))


def _coerce(name: str, value, typ):
    if typ is bool or typ == "bool":
        if isinstance(value, bool):
            return value
        v = str(value).strip().lower()
        if v in ("1", "true", "yes", "on"):
            return True
        if v in ("0", "false", "no", "off"):
            return False
        raise ValueError(f"{name} must be true or false, got {value!r}")
    if typ is float or typ == "float":
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"{name} must be a number, got {value!r}") from exc
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
    cfg = replace(cfg, log_level=cfg.log_level.upper(), trim=cfg.trim.lower())
    if cfg.trim not in ("start", "end"):
        log.warning("trim must be 'start' or 'end', got %r; using 'end'", cfg.trim)
        cfg = replace(cfg, trim="end")
    cfg = replace(cfg, privacy=cfg.privacy.lower())
    if cfg.privacy not in ("private", "unlisted", "public"):
        log.warning("privacy must be private, unlisted or public, got %r; using private", cfg.privacy)
        cfg = replace(cfg, privacy="private")
    return cfg


def save(cfg: Config, data: Path | None = None) -> None:
    """Write settings atomically, so a crash mid-write can't leave a broken file."""
    data = data or data_dir()
    data.mkdir(parents=True, exist_ok=True)
    path = data / SETTINGS_FILE
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(asdict(cfg), indent=2) + "\n")
    tmp.replace(path)
