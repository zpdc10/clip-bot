"""Turn a landscape gameplay clip into a 1080x1920 vertical Short with ffmpeg."""

from __future__ import annotations

import json
import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .config import Config
from .state import State

log = logging.getLogger(__name__)


class RenderError(Exception):
    pass


@dataclass(frozen=True)
class Probe:
    duration: float
    width: int
    height: int
    has_audio: bool


def probe(path: Path) -> Probe:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RenderError(f"ffprobe couldn't read {path.name}: {out.stderr.strip()[-500:]}")
    info = json.loads(out.stdout)
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    if video is None:
        raise RenderError(f"{path.name} has no video stream")
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    if duration <= 0:
        raise RenderError(f"{path.name} has no usable duration")
    return Probe(
        duration=duration,
        width=int(video["width"]),
        height=int(video["height"]),
        has_audio=any(s.get("codec_type") == "audio" for s in info["streams"]),
    )


def trim_window(duration: float, max_duration: int, trim: str) -> tuple[float, float]:
    """Return (start, length) in seconds. Moments clips usually end on the highlight, so the
    default keeps the end of an over-long clip."""
    if max_duration <= 0 or duration <= max_duration:
        return 0.0, duration
    if trim == "start":
        return 0.0, float(max_duration)
    return duration - max_duration, float(max_duration)


def video_filter(cfg: Config) -> str:
    """Crop the largest 9:16 window that fits, slide it horizontally by crop_position
    (0 = left edge, 0.5 = centre, 1 = right edge), then scale to the output size."""
    w = cfg.output_width
    h = cfg.output_height
    pos = min(max(cfg.crop_position, 0.0), 1.0)
    return (
        f"crop=w='trunc(min(iw,ih*{w}/{h})/2)*2':h='trunc(min(ih,iw*{h}/{w})/2)*2'"
        f":x='(iw-ow)*{pos}':y='(ih-oh)/2',"
        f"scale={w}:{h}:flags=lanczos,setsar=1"
    )


def build_command(src: Path, dst: Path, cfg: Config, p: Probe) -> list[str]:
    start, length = trim_window(p.duration, cfg.max_duration, cfg.trim)
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start > 0:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(src), "-t", f"{length:.3f}", "-map", "0:v:0"]
    if p.has_audio:
        cmd += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "192k"]
    cmd += [
        "-vf", video_filter(cfg),
        "-c:v", "libx264", "-preset", cfg.x264_preset, "-crf", str(cfg.crf),
        "-pix_fmt", "yuv420p", "-movflags", "+faststart",
        str(dst),
    ]
    return cmd


def render(src: Path, dst: Path, cfg: Config) -> Path:
    p = probe(src)
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".partial.mp4")
    cmd = build_command(src, tmp, cfg, p)
    log.debug("running %s", " ".join(cmd))
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        tmp.unlink(missing_ok=True)
        raise RenderError(f"ffmpeg failed: {out.stderr.strip()[-800:]}")
    tmp.replace(dst)
    return dst


def process_queued(
    state: State,
    cfg: Config,
    rendered_dir: Path,
    limit: int = 1,
    stopping: Callable[[], bool] = lambda: False,
) -> int:
    """Render up to `limit` queued clips. Returns how many were handled. Done in small batches
    so the main loop keeps scanning between long renders."""
    handled = 0
    for clip in state.by_status("queued")[:limit]:
        if stopping():
            break
        handled += 1
        src = cfg.watch_path / clip.rel_path
        dst = rendered_dir / f"{clip.id:06d}.mp4"
        state.set_status(clip.id, "processing")
        try:
            if not src.exists():
                raise RenderError(f"source file is gone: {clip.rel_path}")
            render(src, dst, cfg)
        except Exception as exc:  # keep the loop alive whatever ffmpeg does
            if stopping():
                # The container is shutting down and ffmpeg got the signal too. That isn't the
                # clip's fault, so put it back in the queue for the next start.
                log.info("render of clip #%d interrupted by shutdown; will retry on restart", clip.id)
                state.set_status(clip.id, "queued")
                break
            log.error("clip #%d (%s) failed to render: %s", clip.id, clip.rel_path, exc)
            state.set_status(clip.id, "failed", error=str(exc))
            continue
        state.set_status(clip.id, "rendered", output_path=str(dst))
        log.info("rendered clip #%d -> %s", clip.id, dst.name)
    return handled
