from clipbot import youtube
from clipbot.config import Config
from clipbot.state import State
from clipbot.uploader import DAY, process_rendered


def setup(tmp_path, n=1, signed_in=True):
    state = State(tmp_path / "db.sqlite")
    ids = []
    for i in range(n):
        c = state.enqueue(f"fp{i}", f"mirage1v3clutchmp9_{i}.mp4", 1)
        state.set_status(c.id, "rendered", output_path=str(tmp_path / f"{c.id}.mp4"))
        ids.append(c.id)
    if signed_in:
        (tmp_path / youtube.TOKEN_FILE).write_text("{}")
    # First pass(es) only generate titles.
    for _ in ids:
        assert process_rendered(state, Config(upload_enabled=False), tmp_path, upload_fn=lambda *a: 1 / 0) == 1
    return state, ids


def test_uploads_with_title_description_and_privacy(tmp_path):
    state, (cid,) = setup(tmp_path)
    calls = []

    def fake_upload(data, video, title, description, tags, privacy, mbps):
        calls.append((video.name, title, description, tags, privacy))
        return "abc123"

    cfg = Config(title_template="{title} #shorts")
    assert process_rendered(state, cfg, tmp_path, upload_fn=fake_upload) == 1
    clip = state.get(cid)
    assert clip.status == "uploaded" and clip.video_id == "abc123"
    name, title, desc, tags, privacy = calls[0]
    assert title == "Mirage 1v3 Clutch MP9 0 #shorts"
    assert desc.startswith("Mirage 1v3 Clutch MP9 0\n\n#cs2")
    assert privacy == "private" and "cs2" in tags


def test_waits_when_not_signed_in(tmp_path):
    state, (cid,) = setup(tmp_path, signed_in=False)
    assert process_rendered(state, Config(), tmp_path, upload_fn=lambda *a: "x") == 0
    assert state.get(cid).status == "rendered"


def test_daily_limit(tmp_path):
    state, ids = setup(tmp_path, n=3)
    t = {"now": 1_000_000.0}
    cfg = Config(daily_upload_limit=2, post_interval_minutes=0)
    up = lambda *a: "vid"
    clock = lambda: t["now"]
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 0
    t["now"] += DAY + 1
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1


def test_transient_failures_back_off_then_give_up(tmp_path):
    from clipbot.uploader import BACKOFF

    state, (cid,) = setup(tmp_path)
    t = {"now": 1_000_000.0}
    clock = lambda: t["now"]

    def boom(*a):
        raise RuntimeError("connection reset")

    cfg = Config(upload_retries=2)
    assert process_rendered(state, cfg, tmp_path, upload_fn=boom, clock=clock) == 1
    clip = state.get(cid)
    assert clip.status == "rendered" and clip.attempts == 1 and clip.retry_at == t["now"] + BACKOFF[0]
    # Not retried before its time.
    assert process_rendered(state, cfg, tmp_path, upload_fn=boom, clock=clock) == 0
    t["now"] += BACKOFF[0]
    process_rendered(state, cfg, tmp_path, upload_fn=boom, clock=clock)
    assert state.get(cid).attempts == 2
    t["now"] += BACKOFF[1]
    process_rendered(state, cfg, tmp_path, upload_fn=boom, clock=clock)
    clip = state.get(cid)
    assert clip.status == "failed" and "connection reset" in clip.error
    assert clip.title == "Mirage 1v3 Clutch MP9 0"  # kept for a manual retry


def test_quota_errors_wait_without_using_a_retry(tmp_path):
    from clipbot.uploader import QUOTA_WAIT

    state, (cid,) = setup(tmp_path)

    def quota(*a):
        raise RuntimeError('<HttpError 403 "quotaExceeded">')

    process_rendered(state, Config(), tmp_path, upload_fn=quota, clock=lambda: 100.0)
    clip = state.get(cid)
    assert clip.status == "rendered" and clip.attempts == 0 and clip.retry_at == 100.0 + QUOTA_WAIT


def test_expired_sign_in_waits(tmp_path):
    state, (cid,) = setup(tmp_path)

    def expired(*a):
        raise youtube.NotSignedIn("revoked")

    assert process_rendered(state, Config(), tmp_path, upload_fn=expired, clock=lambda: 0.0) == 0
    clip = state.get(cid)
    assert clip.status == "rendered" and clip.attempts == 0 and "revoked" in clip.error


def test_rendered_file_deleted_after_upload_unless_kept(tmp_path):
    state, (a, b) = setup(tmp_path, n=2)
    for cid in (a, b):
        open(state.get(cid).output_path, "wb").write(b"x")
    process_rendered(state, Config(post_interval_minutes=0), tmp_path, upload_fn=lambda *x: "v")
    process_rendered(state, Config(keep_rendered=True, post_interval_minutes=0), tmp_path, upload_fn=lambda *x: "v")
    from pathlib import Path
    assert not Path(state.get(a).output_path).exists()
    assert Path(state.get(b).output_path).exists()


def test_disabled(tmp_path):
    state, _ = setup(tmp_path)
    assert process_rendered(state, Config(upload_enabled=False), tmp_path, upload_fn=lambda *a: "x") == 0


def test_auth_url_requires_client_secret(tmp_path):
    import pytest

    with pytest.raises(youtube.NotSignedIn):
        youtube.start_auth(tmp_path)


def test_titles_generated_before_upload_even_when_disabled(tmp_path):
    state, (cid,) = setup(tmp_path)
    assert state.get(cid).title == "Mirage 1v3 Clutch MP9 0"
    assert state.get(cid).status == "rendered"


def test_require_approval(tmp_path):
    state, (cid,) = setup(tmp_path)
    cfg = Config(require_approval=True)
    assert process_rendered(state, cfg, tmp_path, upload_fn=lambda *a: "v") == 0
    state.set_approved(cid)
    assert process_rendered(state, cfg, tmp_path, upload_fn=lambda *a: "v") == 1
    assert state.get(cid).status == "uploaded"


def test_edited_title_is_used(tmp_path):
    state, (cid,) = setup(tmp_path)
    state.set_title(cid, "My own title")
    seen = []
    process_rendered(state, Config(), tmp_path, upload_fn=lambda d, v, t, *a: seen.append(t) or "v")
    assert seen == ["My own title"]


def test_post_interval_spaces_uploads(tmp_path):
    state, ids = setup(tmp_path, n=2)
    t = {"now": 1_000_000.0}
    clock = lambda: t["now"]
    cfg = Config(post_interval_minutes=90)
    up = lambda *a: "v"
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1
    t["now"] += 89 * 60
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 0
    t["now"] += 60
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1


def test_post_window(tmp_path, monkeypatch):
    import time as _time

    monkeypatch.setenv("TZ", "UTC")
    _time.tzset()
    state, (cid,) = setup(tmp_path)
    up = lambda *a: "v"
    noon = 1_700_000_000 - (1_700_000_000 % 86400) + 12 * 3600  # 12:00 UTC
    assert process_rendered(state, Config(post_window="16:00-23:00"), tmp_path, upload_fn=up, clock=lambda: noon) == 0
    assert process_rendered(state, Config(post_window="22:00-13:00"), tmp_path, upload_fn=up, clock=lambda: noon) == 1


def test_speed_limit_is_passed_and_paced():
    from clipbot.youtube import throttle_delay

    assert throttle_delay(10_000_000, 4.0, 10) == 4.0  # 80 Mbit at 10 Mbps needs 8 s
    assert throttle_delay(10_000_000, 9.0, 10) == 0.0


def test_parse_window():
    import pytest
    from clipbot.config import in_window, parse_window

    assert parse_window("") is None
    assert parse_window("16:00-23:30") == (960, 1410)
    w = parse_window("22-2")
    assert in_window(w, 23 * 60) and in_window(w, 60) and not in_window(w, 12 * 60)
    for bad in ["nope", "25:00-1:00", "10:00-10:00"]:
        with pytest.raises(ValueError):
            parse_window(bad)


def test_records_privacy_youtube_reports(tmp_path):
    state, (cid,) = setup(tmp_path)
    cfg = Config(privacy="public")
    assert process_rendered(state, cfg, tmp_path, upload_fn=lambda *a: ("vid1", "private")) == 1
    clip = state.get(cid)
    assert clip.video_id == "vid1" and clip.privacy == "private"


def test_plain_id_falls_back_to_configured_privacy(tmp_path):
    state, (cid,) = setup(tmp_path)
    process_rendered(state, Config(privacy="unlisted"), tmp_path, upload_fn=lambda *a: "vid2")
    assert state.get(cid).privacy == "unlisted"
