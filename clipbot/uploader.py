"""Pick rendered clips, title them, and upload them, within a daily cap."""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Callable

from . import titles, youtube
from .config import Config
from .state import State

log = logging.getLogger(__name__)

DAY = 24 * 3600


def describe(cfg: Config, title: str, rel_path: str) -> str:
    return cfg.description_template.format(title=title, filename=Path(rel_path).stem).replace("\\n", "\n")


def process_rendered(
    state: State,
    cfg: Config,
    data: Path,
    upload_fn: Callable[..., str] = youtube.upload,
    clock: Callable[[], float] = time.time,
) -> int:
    """Upload at most one rendered clip. Returns 1 if one was handled, else 0."""
    if not cfg.upload_enabled:
        return 0
    pending = state.by_status("rendered")
    if not pending:
        return 0
    if not youtube.is_signed_in(data):
        log.debug("%d clip(s) ready but YouTube isn't signed in", len(pending))
        return 0
    if cfg.daily_upload_limit > 0 and state.uploads_since(clock() - DAY) >= cfg.daily_upload_limit:
        log.debug("daily upload limit (%d) reached", cfg.daily_upload_limit)
        return 0

    clip = pending[0]
    title = clip.title or titles.make_title(clip.rel_path, cfg.anthropic_api_key, cfg.title_model)
    state.set_title(clip.id, title)
    full_title = cfg.title_template.format(title=title)[:100]
    try:
        video_id = upload_fn(
            data, Path(clip.output_path), full_title, describe(cfg, title, clip.rel_path),
            [t.strip() for t in cfg.tags.split(",") if t.strip()], cfg.privacy,
        )
    except youtube.NotSignedIn as exc:
        log.warning("can't upload: %s", exc)
        return 0
    except Exception as exc:
        log.error("upload of clip #%d failed: %s", clip.id, exc)
        state.set_status(clip.id, "failed", error=f"upload: {exc}")
        return 1
    state.mark_uploaded(clip.id, video_id, clock())
    log.info("uploaded clip #%d as https://youtube.com/shorts/%s (%s): %s", clip.id, video_id, cfg.privacy, full_title)
    return 1
