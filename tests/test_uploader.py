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

    def fake_upload(data, video, title, description, tags, privacy):
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
    cfg = Config(daily_upload_limit=2)
    up = lambda *a: "vid"
    clock = lambda: t["now"]
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 0
    t["now"] += DAY + 1
    assert process_rendered(state, cfg, tmp_path, upload_fn=up, clock=clock) == 1


def test_failed_upload_is_recorded(tmp_path):
    state, (cid,) = setup(tmp_path)

    def boom(*a):
        raise RuntimeError("quotaExceeded")

    process_rendered(state, Config(), tmp_path, upload_fn=boom)
    clip = state.get(cid)
    assert clip.status == "failed" and "quotaExceeded" in clip.error
    assert clip.title == "Mirage 1v3 Clutch MP9 0"  # kept for a retry


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
