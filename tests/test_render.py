import shutil
import subprocess

import pytest

from clipbot.config import Config
from clipbot.render import RenderError, build_command, probe, process_queued, render, trim_window
from clipbot.state import State

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def make_clip(path, seconds=3, size="1920x1080", audio=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size={size}:rate=30"]
    if audio:
        cmd += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest"]
    cmd += ["-pix_fmt", "yuv420p", str(path)]
    subprocess.run(cmd, check=True)
    return path


def test_trim_window():
    assert trim_window(30, 60, "end") == (0.0, 30)
    assert trim_window(90, 60, "end") == (30, 60.0)
    assert trim_window(90, 60, "start") == (0.0, 60.0)
    assert trim_window(90, 0, "end") == (0.0, 90)


@needs_ffmpeg
def test_renders_vertical_1080x1920(tmp_path):
    src = make_clip(tmp_path / "in.mp4")
    out = render(src, tmp_path / "out.mp4", Config())
    p = probe(out)
    assert (p.width, p.height) == (1080, 1920)
    assert p.has_audio
    assert abs(p.duration - 3) < 0.2
    assert not (tmp_path / "out.partial.mp4").exists()


@needs_ffmpeg
def test_long_clip_keeps_the_end(tmp_path):
    src = make_clip(tmp_path / "in.mp4", seconds=6)
    out = render(src, tmp_path / "out.mp4", Config(max_duration=2))
    assert abs(probe(out).duration - 2) < 0.2


@needs_ffmpeg
def test_handles_silent_and_odd_sized_clips(tmp_path):
    src = make_clip(tmp_path / "in.mp4", size="2560x1080", audio=False)  # ultrawide, no audio
    out = render(src, tmp_path / "out.mp4", Config(crop_position=0.0))
    p = probe(out)
    assert (p.width, p.height) == (1080, 1920)
    assert not p.has_audio


@needs_ffmpeg
def test_process_queued_marks_rendered_and_failed(tmp_path):
    clips = tmp_path / "clips"
    make_clip(clips / "good.mp4", seconds=1)
    (clips / "bad.mp4").write_bytes(b"not a video")
    state = State(tmp_path / "db.sqlite")
    good = state.enqueue("a", "good.mp4", 1)
    bad = state.enqueue("b", "bad.mp4", 1)
    gone = state.enqueue("c", "deleted.mp4", 1)
    cfg = Config(watch_dir=str(clips))

    assert process_queued(state, cfg, tmp_path / "rendered", limit=10) == 3
    g = state.get(good.id)
    assert g.status == "rendered" and g.output_path.endswith("000001.mp4")
    assert state.get(bad.id).status == "failed" and "ffprobe" in state.get(bad.id).error
    assert state.get(gone.id).status == "failed" and "gone" in state.get(gone.id).error


def test_command_skips_seek_for_short_clips(tmp_path):
    from clipbot.render import Probe

    cmd = build_command(tmp_path / "a.mp4", tmp_path / "b.mp4", Config(), Probe(10, 1920, 1080, False))
    assert "-ss" not in cmd and "0:a:0" not in cmd


def test_shutdown_mid_render_requeues_instead_of_failing(tmp_path, monkeypatch):
    import clipbot.render as r

    clips = tmp_path / "clips"
    clips.mkdir()
    (clips / "a.mp4").write_bytes(b"x")
    state = State(tmp_path / "db.sqlite")
    clip = state.enqueue("a", "a.mp4", 1)
    stopping = {"now": False}

    def killed(*a, **k):
        stopping["now"] = True  # SIGTERM arrives while ffmpeg runs
        raise RenderError("ffmpeg failed: ")

    monkeypatch.setattr(r, "render", killed)
    process_queued(state, Config(watch_dir=str(clips)), tmp_path / "out", stopping=lambda: stopping["now"])
    assert state.get(clip.id).status == "queued"


def test_render_ahead_limits_backlog(tmp_path, monkeypatch):
    import clipbot.render as r

    clips = tmp_path / "clips"
    clips.mkdir()
    state = State(tmp_path / "db.sqlite")
    for i in range(5):
        (clips / f"{i}.mp4").write_bytes(b"x")
        state.enqueue(str(i), f"{i}.mp4", 1)
    monkeypatch.setattr(r, "render", lambda src, dst, cfg: dst)
    monkeypatch.setattr(r, "REVIEW_AHEAD", 0)  # test render_ahead on its own
    cfg = Config(watch_dir=str(clips), render_ahead=2)
    out = tmp_path / "out"
    assert process_queued(state, cfg, out, limit=10) == 2
    assert process_queued(state, cfg, out, limit=10) == 0  # two already waiting
    state.set_status(state.by_status("rendered")[0].id, "uploaded")
    assert process_queued(state, cfg, out, limit=10) == 1
    assert len(state.by_status("queued")) == 2
    assert process_queued(state, Config(watch_dir=str(clips), render_ahead=0), out, limit=10) == 2


def test_render_ahead_keeps_a_review_stack(tmp_path, monkeypatch):
    import clipbot.render as r

    clips = tmp_path / "clips"
    clips.mkdir()
    state = State(tmp_path / "db.sqlite")
    for i in range(15):
        (clips / f"{i}.mp4").write_bytes(b"x")
        state.enqueue(str(i), f"{i}.mp4", 1)
    monkeypatch.setattr(r, "render", lambda src, dst, cfg: dst)
    cfg = Config(watch_dir=str(clips), render_ahead=2)
    out = tmp_path / "out"
    assert process_queued(state, cfg, out, limit=20) == r.REVIEW_AHEAD
    assert process_queued(state, cfg, out, limit=20) == 0
    for c in state.by_status("rendered")[:3]:
        state.set_approved(c.id)  # approved clips no longer count against the review stack
    assert process_queued(state, cfg, out, limit=20) == 3
