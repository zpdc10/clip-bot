import io
import json

import pytest

from clipbot import auth, config, youtube
from clipbot.state import State
from clipbot.web import create_app


@pytest.fixture
def env(tmp_path, monkeypatch):
    for v in config.ENV_NAMES.values():
        monkeypatch.delenv(v, raising=False)
    auth.set_password(tmp_path, "correct horse battery")
    app = create_app(tmp_path)
    app.testing = True
    state = State(tmp_path / "clipbot.db")
    client = app.test_client()
    assert client.post("/login", data={"password": "correct horse battery"}).status_code == 302
    return client, state, tmp_path


@pytest.fixture
def anon(tmp_path, monkeypatch):
    for v in config.ENV_NAMES.values():
        monkeypatch.delenv(v, raising=False)
    monkeypatch.setattr("clipbot.web.time.sleep", lambda s: None)
    app = create_app(tmp_path)
    app.testing = True
    return app.test_client(), tmp_path


def test_locked_until_password_set(anon):
    client, data = anon
    r = client.get("/settings")
    assert r.status_code == 302 and "/login" in r.headers["Location"]
    assert b"clipbot set-password" in client.get("/login").data
    # No password means nothing logs in.
    client.post("/login", data={"password": ""})
    assert client.get("/").status_code == 302
    assert client.post("/settings", data={}).status_code == 401
    assert client.get("/healthz").status_code == 200


def test_login_flow(anon):
    client, data = anon
    auth.set_password(data, "correct horse battery")
    assert client.get("/clips/1/video").status_code == 401
    assert client.get("/review/queue").status_code == 401
    r = client.post("/login", data={"password": "nope"})
    assert r.status_code == 401 and b"Wrong password" in r.data
    r = client.post("/login", data={"password": "correct horse battery", "next": "/settings"})
    assert r.status_code == 302 and r.headers["Location"].endswith("/settings")
    assert client.get("/settings").status_code == 200
    assert "HttpOnly" in r.headers["Set-Cookie"] and "SameSite=Lax" in r.headers["Set-Cookie"]
    # A new password logs existing sessions out.
    auth.set_password(data, "another long password")
    assert client.get("/settings").status_code == 302
    client.post("/login", data={"password": "another long password"})
    client.post("/logout")
    assert client.get("/").status_code == 302


@pytest.mark.parametrize("nxt", ["https://evil.example/", "//evil.example/", "/\\evil.example"])
def test_login_never_redirects_offsite(anon, nxt):
    client, data = anon
    auth.set_password(data, "correct horse battery")
    r = client.post("/login", data={"password": "correct horse battery", "next": nxt})
    assert r.headers["Location"] == "/"


def test_login_rate_limited(anon):
    client, data = anon
    auth.set_password(data, "correct horse battery")
    for _ in range(auth.MAX_FAILURES):
        client.post("/login", data={"password": "wrong"}, headers={"CF-Connecting-IP": "1.2.3.4"})
    r = client.post("/login", data={"password": "correct horse battery"}, headers={"CF-Connecting-IP": "1.2.3.4"})
    assert b"Too many" in r.data
    # Someone else isn't locked out.
    r = client.post("/login", data={"password": "correct horse battery"}, headers={"CF-Connecting-IP": "5.6.7.8"})
    assert r.status_code == 302


def test_short_password_rejected(tmp_path):
    with pytest.raises(ValueError):
        auth.set_password(tmp_path, "short")
    assert auth.password_hash(tmp_path) == ""


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
    assert cfg.upload_enabled is True and not hasattr(cfg, 'require_approval')
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
    ("post_window", "evenings"), ("upload_limit_mbps", "-1"),
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


def test_logs_and_health(env):
    import logging

    from clipbot import logbuffer

    client, _, _ = env
    logbuffer.LINES.append("2026-11-01 INFO clipbot: hello from the log")
    assert b"hello from the log" in client.get("/logs").data
    assert client.get("/healthz").json == {"ok": True}


def test_swipe_review_flow(env, tmp_path):
    client, state, data = env
    out = tmp_path / "1.mp4"
    out.write_bytes(b"video")
    a = state.enqueue("a", "mirage1v3clutchmp9.mp4", 1)
    b = state.enqueue("b", "anubisaflash.mp4", 1)
    for c in (a, b):
        state.set_status(c.id, "rendered", output_path=str(out))
    state.set_title(a.id, "Mirage 1v3 clutch")

    page = client.get("/review")
    assert page.status_code == 200 and b"Approval is off" not in page.data
    assert b"Swipe to review 2 clips" in client.get("/").data

    q = client.get("/review/queue").json
    assert [c["id"] for c in q["clips"]] == [a.id, b.id]
    assert q["clips"][0]["title"] == "Mirage 1v3 clutch"
    assert q["clips"][1]["title"]  # untitled clips get a filename-based suggestion

    def act(clip_id, **body):
        return client.post(f"/clips/{clip_id}/review", json=body)

    assert act(a.id, action="approve", title="  ").status_code == 400
    assert act(a.id, action="approve", title="Edited title").json == {"ok": True}
    assert state.get(a.id).approved and state.get(a.id).title == "Edited title"
    assert act(b.id, action="skip").status_code == 200
    assert state.get(b.id).status == "skipped"
    assert client.get("/review/queue").json["clips"] == []

    act(a.id, action="undo")
    act(b.id, action="undo")
    assert not state.get(a.id).approved and state.get(b.id).status == "rendered"
    assert len(client.get("/review/queue").json["clips"]) == 2

    state.mark_uploaded(a.id, "vid", 1.0)
    assert act(a.id, action="undo").status_code == 409
    assert act(a.id, action="skip").status_code == 409
    assert act(b.id, action="dance").status_code == 400
    assert act(999, action="skip").status_code == 404


def test_warns_when_uploads_are_not_public(env):
    client, state, tmp_path = env
    (tmp_path / youtube.TOKEN_FILE).write_text("{}")
    c = state.enqueue("a", "mirage1v3clutchmp9.mp4", 1)
    state.mark_uploaded(c.id, "vid1", 1.0, "private")
    page = client.get("/").data
    assert b"get no views" in page and b"vid1" in page
    (tmp_path / config.SETTINGS_FILE).write_text(json.dumps({"privacy": "public"}))
    assert b"get no views" not in client.get("/").data


def test_title_edits_refused_once_upload_starts(env, tmp_path):
    client, state, data = env
    out = tmp_path / "1.mp4"
    out.write_bytes(b"video")
    a = state.enqueue("a", "mirage1v3clutchmp9.mp4", 1)
    state.set_status(a.id, "rendered", output_path=str(out))
    state.set_title(a.id, "Generated")
    state.set_approved(a.id)
    assert state.start_upload(a.id)

    r = client.post(f"/clips/{a.id}/review", json={"action": "approve", "title": "Edited"})
    assert r.status_code == 409 and "uploading" in r.json["error"]
    client.post(f"/clips/{a.id}/title", data={"title": "Edited"})
    assert state.get(a.id).title == "Generated"


def test_approve_button_saves_the_typed_title(env, tmp_path):
    client, state, data = env
    a = state.enqueue("a", "mirage1v3clutchmp9.mp4", 1)
    state.set_status(a.id, "rendered", output_path=str(tmp_path / "1.mp4"))
    state.set_title(a.id, "Generated")
    page = client.get("/").data.decode()
    assert f'form="tf-{a.id}"' in page and f'id="tf-{a.id}"' in page
    client.post(f"/clips/{a.id}/approve", data={"title": "Typed but not saved"})
    clip = state.get(a.id)
    assert clip.approved and clip.title == "Typed but not saved"
