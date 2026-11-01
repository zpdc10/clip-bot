import io
import json

import pytest

from clipbot import config, youtube
from clipbot.state import State
from clipbot.web import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    for v in config.ENV_NAMES.values():
        monkeypatch.delenv(v, raising=False)
    app = create_app(tmp_path)
    app.testing = True
    state = State(tmp_path / "clipbot.db")
    return app.test_client(), state, tmp_path


def test_pages_render(env):
    client, state, _ = env
    state.enqueue("a", "mirage1v3clutchmp9.mp4", 1)
    for url in ["/", "/?status=queued", "/settings", "/youtube"]:
        r = client.get(url)
        assert r.status_code == 200, url
    assert b"mirage1v3clutchmp9.mp4" in client.get("/").data
    assert b"YouTube not connected" in client.get("/").data
    assert client.get("/?status=bogus").status_code == 404


def test_save_settings_roundtrip(env):
    client, _, data = env
    form = {k: str(v) for k, v in vars(config.Config()).items() if not isinstance(v, bool)}
    form.update(crop_position="0.3", privacy="unlisted", title_template="{title} #shorts",
                upload_enabled="on", anthropic_api_key="sk-secret")
    r = client.post("/settings", data=form, follow_redirects=True)
    assert b"Settings saved" in r.data
    cfg = config.load(data)
    assert cfg.crop_position == 0.3 and cfg.privacy == "unlisted"
    assert cfg.upload_enabled is True and cfg.require_approval is False
    assert cfg.anthropic_api_key == "sk-secret"
    assert b"sk-secret" not in client.get("/settings").data  # never echoed back

    # Blank key keeps the saved one; the clear box removes it.
    form["anthropic_api_key"] = ""
    client.post("/settings", data=form)
    assert config.load(data).anthropic_api_key == "sk-secret"
    client.post("/settings", data={**form, "anthropic_api_key__clear": "on"})
    assert config.load(data).anthropic_api_key == ""


@pytest.mark.parametrize("field,value", [
    ("crop_position", "2"), ("crf", "abc"), ("title_template", "{nope}"), ("output_width", "1081"),
])
def test_bad_settings_are_rejected(env, field, value):
    client, _, data = env
    form = {k: str(v) for k, v in vars(config.Config()).items() if not isinstance(v, bool)}
    form[field] = value
    r = client.post("/settings", data=form, follow_redirects=True)
    assert b"Not saved" in r.data
    assert not (data / config.SETTINGS_FILE).exists()


def test_clip_actions(env, tmp_path):
    client, state, data = env
    out = tmp_path / "1.mp4"
    out.write_bytes(b"video")
    c = state.enqueue("a", "a.mp4", 1)
    state.set_status(c.id, "rendered", output_path=str(out))

    client.post(f"/clips/{c.id}/title", data={"title": "New title"})
    assert state.get(c.id).title == "New title"
    client.post(f"/clips/{c.id}/approve")
    assert state.get(c.id).approved
    assert client.get(f"/clips/{c.id}/video").data == b"video"

    client.post(f"/clips/{c.id}/skip")
    assert state.get(c.id).status == "skipped"
    client.post(f"/clips/{c.id}/retry")
    assert state.get(c.id).status == "rendered"  # output exists, so straight back to upload
    client.post(f"/clips/{c.id}/rerender")
    assert state.get(c.id).status == "queued"
    assert client.post("/clips/999/skip").status_code == 404


def test_retry_without_output_rerenders(env):
    client, state, _ = env
    c = state.enqueue("a", "a.mp4", 1)
    state.set_status(c.id, "failed", error="boom")
    client.post(f"/clips/{c.id}/retry")
    assert state.get(c.id).status == "queued"


def test_client_secret_upload_and_sign_in_start(env):
    client, _, data = env
    bad = client.post("/youtube/client-secret", data={"file": (io.BytesIO(b'{"web":{}}'), "c.json")},
                      follow_redirects=True)
    assert b"Desktop app" in bad.data
    secret = {"installed": {"client_id": "x.apps.googleusercontent.com", "client_secret": "s",
                            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                            "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": ["http://localhost"]}}
    client.post("/youtube/client-secret", data={"file": (io.BytesIO(json.dumps(secret).encode()), "c.json")})
    assert (data / youtube.CLIENT_SECRET_FILE).exists()
    r = client.post("/youtube/start")
    assert "accounts.google.com" in r.headers["Location"]
    page = client.get(r.headers["Location"]).data
    assert b"Open Google sign-in" in page

    r = client.post("/youtube/finish", data={"pasted": "http://localhost:8080/?state=wrong&code=x"},
                    follow_redirects=True)
    assert b"didn" in r.data  # mismatched state is refused, not a crash
