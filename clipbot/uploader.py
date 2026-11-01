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
# Wait before retry 1, 2, 3 ... after an upload error that might be temporary.
BACKOFF = [5 * 60, 30 * 60, 2 * 3600, 6 * 3600]
QUOTA_WAIT = 6 * 3600


def _error_reason(exc: Exception) -> str:
    """The reason string from a googleapiclient HttpError, e.g. 'quotaExceeded', else ''."""
    reason = ""
    try:
        details = getattr(exc, "error_details", None) or []
        if details and isinstance(details, list) and isinstance(details[0], dict):
            reason = details[0].get("reason", "")
    except Exception:
        pass
    text = str(exc)
    for r in ("quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded", "dailyLimitExceeded"):
        if r in text:
            return r
    return reason


def describe(cfg: Config, title: str, rel_path: str) -> str:
    return cfg.description_template.format(title=title, filename=Path(rel_path).stem).replace("\\n", "\n")


def process_rendered(
    state: State,
    cfg: Config,
    data: Path,
    upload_fn: Callable[..., str] = youtube.upload,
    clock: Callable[[], float] = time.time,
) -> int:
    """Title rendered clips, then upload at most one. Returns 1 if anything was done, else 0."""
    rendered = state.by_status("rendered")
    # Title clips as soon as they're rendered, so they can be checked and edited in the web UI
    # before upload. One per cycle, since it may be an API call.
    untitled = [c for c in rendered if not c.title]
    if untitled:
        c = untitled[0]
        state.set_title(c.id, titles.make_title(c.rel_path, cfg.anthropic_api_key, cfg.title_model))
        return 1

    if not cfg.upload_enabled:
        return 0
    now = clock()
    pending = [
        c for c in rendered
        if (c.approved or not cfg.require_approval) and (not c.retry_at or c.retry_at <= now)
    ]
    if not pending:
        return 0
    if not youtube.is_signed_in(data):
        log.debug("%d clip(s) ready but YouTube isn't signed in", len(pending))
        return 0
    if cfg.daily_upload_limit > 0 and state.uploads_since(clock() - DAY) >= cfg.daily_upload_limit:
        log.debug("daily upload limit (%d) reached", cfg.daily_upload_limit)
        return 0

    clip = pending[0]
    title = clip.title
    full_title = cfg.title_template.format(title=title)[:100]
    try:
        video_id = upload_fn(
            data, Path(clip.output_path), full_title, describe(cfg, title, clip.rel_path),
            [t.strip() for t in cfg.tags.split(",") if t.strip()], cfg.privacy,
        )
    except youtube.NotSignedIn as exc:
        log.warning("can't upload: %s", exc)
        state.schedule_retry(clip.id, now + 15 * 60, f"upload: {exc}", count_attempt=False)
        return 0
    except Exception as exc:
        reason = _error_reason(exc)
        if reason in ("quotaExceeded", "uploadLimitExceeded", "dailyLimitExceeded"):
            log.warning("YouTube says %s; pausing uploads of clip #%d for %d h", reason, clip.id, QUOTA_WAIT // 3600)
            state.schedule_retry(clip.id, now + QUOTA_WAIT, f"upload: {reason}, will retry later", count_attempt=False)
            return 1
        if clip.attempts < len(BACKOFF) and cfg.upload_retries > clip.attempts:
            wait = BACKOFF[clip.attempts]
            log.warning("upload of clip #%d failed (try %d), retrying in %d min: %s",
                        clip.id, clip.attempts + 1, wait // 60, exc)
            state.schedule_retry(clip.id, now + wait, f"upload try {clip.attempts + 1} failed: {exc}")
            return 1
        log.error("upload of clip #%d failed, giving up: %s", clip.id, exc)
        state.set_status(clip.id, "failed", error=f"upload: {exc}")
        return 1
    state.mark_uploaded(clip.id, video_id, clock())
    log.info("uploaded clip #%d as https://youtube.com/shorts/%s (%s): %s", clip.id, video_id, cfg.privacy, full_title)
    if not cfg.keep_rendered and clip.output_path:
        Path(clip.output_path).unlink(missing_ok=True)
    return 1
