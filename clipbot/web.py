"""LAN web UI: queue, settings, and YouTube sign-in.

There's no login. It's meant for your home network only, so don't port-forward it.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import asdict, fields
from pathlib import Path

from flask import Flask, abort, flash, redirect, render_template_string, request, send_file, url_for

from . import config, logbuffer, youtube
from .state import STATUSES, State

log = logging.getLogger(__name__)

# (field, label, help). Field types come from the Config dataclass.
SETTINGS_LAYOUT = [
    ("Clips folder", [
        ("watch_dir", "Watch folder", "Path inside the container where your clips are exported."),
        ("extensions", "File types", "Comma-separated, e.g. .mp4,.mkv"),
        ("settle_seconds", "Wait after copy (s)", "A file must be unchanged this long before it's picked up."),
        ("poll_interval", "Scan every (s)", ""),
    ]),
    ("Vertical video", [
        ("crop_position", "Crop position", "0 = left edge, 0.5 = centre, 1 = right edge."),
        ("max_duration", "Max length (s)", "Longer clips are trimmed. YouTube allows up to 180. 0 = never trim."),
        ("trim", "When trimming keep", ""),
        ("crf", "Quality (CRF)", "Lower is better quality and bigger files. 18-23 is sensible."),
        ("x264_preset", "Encode speed", "Slower presets make smaller files."),
        ("output_width", "Width", ""),
        ("output_height", "Height", ""),
    ]),
    ("YouTube", [
        ("upload_enabled", "Upload automatically", "Off = render only."),
        ("require_approval", "Wait for my approval", "Rendered clips wait for an Approve click before uploading."),
        ("privacy", "Privacy", ""),
        ("daily_upload_limit", "Max uploads per 24 h", "0 = no cap. YouTube's API allows 100 a day."),
        ("upload_retries", "Retries on upload errors", "Spaced 5 min, 30 min, 2 h, 6 h apart (max 4)."),
        ("keep_rendered", "Keep Shorts after upload", "Off deletes the rendered file once it's on YouTube, saving disk space."),
        ("title_template", "Title template", "{title} is the generated title, e.g. {title} #shorts"),
        ("description_template", "Description", "{title} and {filename} work here."),
        ("tags", "Tags", "Comma-separated."),
    ]),
    ("Titles", [
        ("anthropic_api_key", "Anthropic API key", "Optional. With a key, Claude writes the titles. Leave blank to keep the saved key."),
        ("title_model", "Title model", ""),
    ]),
    ("Other", [
        ("log_level", "Log level", ""),
    ]),
]
CHOICES = {
    "trim": ["end", "start"],
    "privacy": ["private", "unlisted", "public"],
    "x264_preset": ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"],
    "log_level": ["DEBUG", "INFO", "WARNING", "ERROR"],
}
SECRET_FIELDS = {"anthropic_api_key"}
MULTILINE_FIELDS = {"description_template"}
FIELD_TYPES = {f.name: f.type for f in fields(config.Config)}


def create_app(data: Path | None = None) -> Flask:
    data = data or config.data_dir()
    app = Flask(__name__)
    app.secret_key = secrets.token_hex(16)
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024  # only small JSON uploads

    def db() -> State:
        return State(data / "clipbot.db")

    @app.context_processor
    def common():
        return {"signed_in": youtube.is_signed_in(data), "nav": request.endpoint}

    @app.get("/")
    def index():
        status = request.args.get("status") or None
        if status and status not in STATUSES:
            abort(404)
        st = db()
        try:
            clips = st.recent(200, status)
            counts = st.counts()
            uploads_24h = st.uploads_since(time.time() - 86400)
        finally:
            st.close()
        cfg = config.load(data)
        return render_template_string(
            _page(INDEX), clips=clips, counts=counts, status=status, cfg=cfg,
            uploads_24h=uploads_24h, fmt_time=_fmt_time, Path=Path,
        )

    def _clip_action(clip_id: int, fn):
        st = db()
        try:
            clip = st.get(clip_id)
            if clip is None:
                abort(404)
            msg = fn(st, clip)
        finally:
            st.close()
        if msg:
            flash(msg)
        return redirect(request.form.get("next") or url_for("index"))

    @app.post("/clips/<int:clip_id>/title")
    def set_title(clip_id):
        title = request.form.get("title", "").strip()[:100]

        def fn(st, clip):
            if not title:
                return "Title can't be empty."
            st.set_title(clip.id, title)
            return f"Saved title for #{clip.id}."
        return _clip_action(clip_id, fn)

    @app.post("/clips/<int:clip_id>/approve")
    def approve(clip_id):
        def fn(st, clip):
            st.set_approved(clip.id, True)
            return f"#{clip.id} approved for upload."
        return _clip_action(clip_id, fn)

    @app.post("/clips/<int:clip_id>/retry")
    def retry(clip_id):
        def fn(st, clip):
            st.reset_attempts(clip.id)
            if clip.output_path and Path(clip.output_path).exists():
                st.set_status(clip.id, "rendered")
                return f"#{clip.id} will be uploaded again."
            st.set_status(clip.id, "queued")
            return f"#{clip.id} queued for rendering."
        return _clip_action(clip_id, fn)

    @app.post("/clips/<int:clip_id>/rerender")
    def rerender(clip_id):
        def fn(st, clip):
            if clip.status == "uploaded":
                return "That clip is already on YouTube."
            st.set_status(clip.id, "queued")
            return f"#{clip.id} will be rendered again with the current settings."
        return _clip_action(clip_id, fn)

    @app.post("/clips/<int:clip_id>/skip")
    def skip(clip_id):
        def fn(st, clip):
            if clip.status in ("uploaded", "processing"):
                return f"Can't skip a clip that's {clip.status}."
            st.set_status(clip.id, "skipped")
            return f"#{clip.id} skipped. It won't be uploaded."
        return _clip_action(clip_id, fn)

    @app.get("/clips/<int:clip_id>/video")
    def video(clip_id):
        st = db()
        try:
            clip = st.get(clip_id)
        finally:
            st.close()
        if not clip or not clip.output_path or not Path(clip.output_path).exists():
            abort(404)
        return send_file(clip.output_path, mimetype="video/mp4", conditional=True)

    @app.route("/settings", methods=["GET", "POST"])
    def settings():
        cfg = config.load(data)
        if request.method == "POST":
            values = {}
            for _, items in SETTINGS_LAYOUT:
                for name, _, _ in items:
                    typ = FIELD_TYPES[name]
                    if typ in ("bool", bool):
                        values[name] = request.form.get(name) == "on"
                    elif name in SECRET_FIELDS:
                        raw = request.form.get(name, "")
                        if request.form.get(name + "__clear"):
                            values[name] = ""
                        else:
                            values[name] = raw.strip() or getattr(cfg, name)
                    else:
                        values[name] = request.form.get(name, "").strip()
            try:
                new = _validated(values)
            except ValueError as exc:
                flash(f"Not saved: {exc}")
                return redirect(url_for("settings"))
            config.save(new, data)
            flash("Settings saved. The bot picks them up within one scan.")
            return redirect(url_for("settings"))
        return render_template_string(
            _page(SETTINGS), cfg=asdict(cfg), layout=SETTINGS_LAYOUT, choices=CHOICES,
            types=FIELD_TYPES, secret=SECRET_FIELDS, multiline=MULTILINE_FIELDS,
        )

    @app.get("/youtube")
    def youtube_page():
        return render_template_string(
            _page(YOUTUBE), has_secret=(data / youtube.CLIENT_SECRET_FILE).exists(),
            auth_url=request.args.get("auth_url"),
        )

    @app.post("/youtube/client-secret")
    def upload_secret():
        f = request.files.get("file")
        if not f:
            flash("Choose the JSON file first.")
            return redirect(url_for("youtube_page"))
        try:
            parsed = json.loads(f.read().decode("utf-8"))
            if "installed" not in parsed:
                raise ValueError("this isn't a Desktop app OAuth client. Create one of type 'Desktop app'.")
        except (ValueError, UnicodeDecodeError) as exc:
            flash(f"That file didn't work: {exc}")
            return redirect(url_for("youtube_page"))
        path = data / youtube.CLIENT_SECRET_FILE
        path.write_text(json.dumps(parsed))
        os.chmod(path, 0o600)
        flash("OAuth client saved. Now sign in.")
        return redirect(url_for("youtube_page"))

    @app.post("/youtube/start")
    def youtube_start():
        try:
            url = youtube.start_auth(data)
        except youtube.NotSignedIn as exc:
            flash(str(exc))
            return redirect(url_for("youtube_page"))
        return redirect(url_for("youtube_page", auth_url=url))

    @app.post("/youtube/finish")
    def youtube_finish():
        try:
            youtube.finish_auth(data, request.form.get("pasted", ""))
        except Exception as exc:
            log.warning("sign-in failed: %s", exc)
            flash(f"Sign-in didn't work: {exc}. Start again and paste the new URL.")
            return redirect(url_for("youtube_page"))
        flash("Signed in to YouTube. Uploads start on the next cycle.")
        return redirect(url_for("index"))

    @app.get("/logs")
    def logs():
        return render_template_string(_page(LOGS), lines=list(logbuffer.LINES)[::-1])

    @app.get("/healthz")
    def healthz():
        st = db()
        try:
            st.counts()
        finally:
            st.close()
        return {"ok": True}

    @app.post("/youtube/sign-out")
    def youtube_sign_out():
        (data / youtube.TOKEN_FILE).unlink(missing_ok=True)
        flash("Signed out of YouTube.")
        return redirect(url_for("youtube_page"))

    return app


def _validated(values: dict) -> config.Config:
    cfg = config._apply(config.Config(), values, "web form")
    if not 0 <= cfg.crop_position <= 1:
        raise ValueError("crop position must be between 0 and 1")
    if not 0 <= cfg.upload_retries <= 4:
        raise ValueError("upload retries must be between 0 and 4")
    for name in ("poll_interval", "output_width", "output_height"):
        if getattr(cfg, name) <= 0:
            raise ValueError(f"{name.replace('_', ' ')} must be more than 0")
    if cfg.output_width % 2 or cfg.output_height % 2:
        raise ValueError("width and height must be even numbers")
    try:
        cfg.title_template.format(title="x")
        cfg.description_template.format(title="x", filename="x")
    except (KeyError, IndexError, ValueError) as exc:
        raise ValueError(f"template has an unknown placeholder {exc}; use {{title}} or {{filename}}") from exc
    for name, opts in CHOICES.items():
        if getattr(cfg, name) not in opts:
            raise ValueError(f"{name} must be one of {', '.join(opts)}")
    return cfg


def _page(body: str) -> str:
    return PAGE.replace("__BODY__", body)


def _fmt_time(ts: float | None) -> str:
    if not ts:
        return ""
    return time.strftime("%b %d %H:%M", time.localtime(ts))


def serve(host: str, port: int, data: Path) -> None:
    from waitress import serve as waitress_serve

    log.info("web UI on http://%s:%d", host, port)
    waitress_serve(create_app(data), host=host, port=port, threads=4, _quiet=True)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>clip-bot</title>
<style>
:root{--bg:#f6f7f9;--card:#fff;--fg:#1d2330;--muted:#667085;--line:#e4e7ec;--accent:#3b5bdb;--ok:#2b8a3e;--warn:#e67700;--bad:#c92a2a}
@media (prefers-color-scheme:dark){:root{--bg:#111318;--card:#1a1d24;--fg:#e6e8ee;--muted:#98a2b3;--line:#2a2f3a;--accent:#748ffc;--ok:#69db7c;--warn:#ffa94d;--bad:#ff8787}}
*{box-sizing:border-box}body{margin:0;font:15px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;background:var(--bg);color:var(--fg)}
header{display:flex;gap:18px;align-items:center;padding:12px 20px;background:var(--card);border-bottom:1px solid var(--line);flex-wrap:wrap}
header b{font-size:17px;margin-right:8px}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--fg);font-weight:600}
.pill{margin-left:auto;font-size:13px;padding:3px 10px;border-radius:99px;border:1px solid var(--line)}
main{max-width:1100px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.flash{border-left:4px solid var(--accent)}
.counts{display:flex;gap:10px;flex-wrap:wrap}.counts a{flex:1;min-width:110px;text-decoration:none;color:inherit}
.counts .n{font-size:24px;font-weight:600}.counts .card{margin:0;padding:10px 14px}.counts a.on .card{border-color:var(--accent)}
table{width:100%;border-collapse:collapse}td,th{padding:9px 6px;border-top:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;border-top:0}
.s{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.s-uploaded{color:var(--ok)}.s-failed{color:var(--bad)}.s-rendered{color:var(--accent)}.s-processing,.s-queued{color:var(--warn)}.s-skipped{color:var(--muted)}
.muted{color:var(--muted);font-size:13px}.err{color:var(--bad);font-size:13px;word-break:break-word}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:70px}
button,.btn{font:inherit;font-size:13px;padding:6px 11px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
button.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:grid;grid-template-columns:220px 1fr;gap:6px 16px;align-items:start;padding:8px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.row label{font-weight:500;padding-top:6px}
.inline{display:flex;gap:6px}.inline input{flex:1}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
form{margin:0}video{max-height:420px;border-radius:8px;background:#000}
details summary{cursor:pointer;color:var(--accent);font-size:13px}
code{background:var(--bg);padding:1px 5px;border-radius:5px;border:1px solid var(--line);word-break:break-all}
@media (max-width:700px){.row{grid-template-columns:1fr}.hide-sm{display:none}}
</style></head><body>
<header><b>clip-bot</b>
<a href="{{ url_for('index') }}" class="{{ 'on' if nav=='index' }}">Clips</a>
<a href="{{ url_for('settings') }}" class="{{ 'on' if nav=='settings' }}">Settings</a>
<a href="{{ url_for('youtube_page') }}" class="{{ 'on' if nav=='youtube_page' }}">YouTube</a>
<a href="{{ url_for('logs') }}" class="{{ 'on' if nav=='logs' }}">Logs</a>
<span class="pill">{{ 'YouTube connected' if signed_in else 'YouTube not connected' }}</span></header>
<main>
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="card flash">{{ m }}</div>{% endfor %}{% endwith %}
__BODY__
</main></body></html>"""

INDEX = """
{% if not signed_in %}<div class="card flash">YouTube isn't connected yet, so clips will render but not upload.
<a href="{{ url_for('youtube_page') }}">Connect YouTube</a></div>{% endif %}
<div class="counts">
<a href="{{ url_for('index') }}" class="{{ 'on' if not status }}"><div class="card"><div class="n">{{ counts.values()|sum }}</div><div class="muted">all</div></div></a>
{% for s in ['queued','rendered','uploaded','failed','skipped'] %}
<a href="{{ url_for('index', status=s) }}" class="{{ 'on' if status==s }}"><div class="card"><div class="n">{{ counts[s] + (counts['processing'] if s=='queued' else 0) }}</div><div class="muted">{{ s }}</div></div></a>
{% endfor %}</div>
<p class="muted">{{ uploads_24h }} of {{ cfg.daily_upload_limit or '∞' }} uploads used in the last 24 h · new uploads are <b>{{ cfg.privacy }}</b>{% if cfg.require_approval %} · approval required{% endif %}{% if not cfg.upload_enabled %} · uploading is off{% endif %}
· <a href="{{ request.full_path }}">refresh</a></p>
<div class="card" style="padding:4px 12px">
<table><tr><th>#</th><th>Clip</th><th>Status</th><th>Title</th></tr>
{% for c in clips %}<tr>
<td class="muted">{{ c.id }}</td>
<td><div>{{ Path(c.rel_path).name }}</div><div class="muted">{{ fmt_time(c.created_at) }}</div></td>
<td><span class="s s-{{ c.status }}">{{ c.status }}{% if c.status=='rendered' and cfg.require_approval and not c.approved %} · needs approval{% endif %}</span>
{% if c.status=='rendered' and c.retry_at %}<div class="muted">retry after {{ fmt_time(c.retry_at) }}</div>{% endif %}
{% if c.video_id %}<div><a href="https://youtube.com/shorts/{{ c.video_id }}" target="_blank" rel="noopener">view</a></div>{% endif %}</td>
<td>
{% if c.status in ['uploaded'] %}{{ c.title }}
{% else %}<form method="post" action="{{ url_for('set_title', clip_id=c.id) }}" class="inline">
<input type="hidden" name="next" value="{{ request.full_path }}">
<input type="text" name="title" maxlength="100" value="{{ c.title or '' }}" placeholder="{{ 'generated after rendering' if not c.title }}"><button>Save</button></form>{% endif %}
{% if c.error %}<div class="err">{{ c.error[:300] }}</div>{% endif %}
<div class="actions">
{% if c.status=='rendered' and cfg.require_approval and not c.approved %}<form method="post" action="{{ url_for('approve', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button class="primary">Approve</button></form>{% endif %}
{% if c.status in ['failed','skipped'] or (c.status=='rendered' and c.retry_at) %}<form method="post" action="{{ url_for('retry', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button>Retry</button></form>{% endif %}
{% if c.status in ['rendered','failed','skipped'] %}<form method="post" action="{{ url_for('rerender', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button>Re-render</button></form>{% endif %}
{% if c.status in ['queued','rendered','failed'] %}<form method="post" action="{{ url_for('skip', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button>Skip</button></form>{% endif %}
</div>
{% if c.output_path and c.status in ['rendered','failed','skipped'] %}<details><summary>Preview</summary><video src="{{ url_for('video', clip_id=c.id) }}" controls preload="none"></video></details>{% endif %}
</td></tr>
{% else %}<tr><td colspan="4" class="muted" style="padding:20px 6px">No clips yet. Export a clip to the watch folder ({{ cfg.watch_dir }}) and it shows up here within a minute.</td></tr>{% endfor %}
</table></div>
"""

SETTINGS = """
<form method="post">
{% for section, items in layout %}<div class="card"><h3 style="margin:0 0 6px">{{ section }}</h3>
{% for name, label, help in items %}<div class="row"><label for="{{ name }}">{{ label }}</label><div>
{% if name in choices %}<select name="{{ name }}" id="{{ name }}">{% for o in choices[name] %}<option {{ 'selected' if cfg[name]==o }}>{{ o }}</option>{% endfor %}</select>
{% elif types[name] in ['bool'] %}<input type="checkbox" name="{{ name }}" id="{{ name }}" {{ 'checked' if cfg[name] }}>
{% elif name in secret %}<input type="password" name="{{ name }}" id="{{ name }}" autocomplete="off" placeholder="{{ 'saved (hidden)' if cfg[name] else 'not set' }}">
{% if cfg[name] %}<label class="muted"><input type="checkbox" name="{{ name }}__clear"> remove saved key</label>{% endif %}
{% elif name in multiline %}<textarea name="{{ name }}" id="{{ name }}">{{ cfg[name] }}</textarea>
{% else %}<input type="{{ 'number' if types[name] in ['int','float'] else 'text' }}" {{ 'step=any' if types[name]=='float' }} name="{{ name }}" id="{{ name }}" value="{{ cfg[name] }}">{% endif %}
{% if help %}<div class="muted">{{ help }}</div>{% endif %}</div></div>{% endfor %}
</div>{% endfor %}
<button class="primary">Save settings</button>
</form>
"""

LOGS = """
<p class="muted">Newest first. The last 500 lines since the container started. <a href="{{ url_for('logs') }}">refresh</a></p>
<div class="card"><pre style="margin:0;white-space:pre-wrap;word-break:break-word;font-size:12.5px">{% for l in lines %}{{ l }}
{% else %}Nothing logged yet.{% endfor %}</pre></div>
"""

YOUTUBE = """
<div class="card"><h3 style="margin-top:0">1. Google OAuth client</h3>
{% if has_secret %}<p>✓ OAuth client saved.</p>{% endif %}
<details {{ 'open' if not has_secret }}><summary>How to get one (about 5 minutes, one time)</summary><ol>
<li>Open <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">Google Cloud Console</a> and create a project (any name).</li>
<li>Enable the <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener">YouTube Data API v3</a> for it.</li>
<li>Open <a href="https://console.cloud.google.com/apis/credentials/consent" target="_blank" rel="noopener">OAuth consent screen</a>, choose <b>External</b>, and fill in the app name and your email. Add the Google account that owns your channel as a test user.
Then click <b>Publish app</b>. Apps left in “Testing” lose their sign-in every 7 days. Google won't review it, because you're the only user.</li>
<li>Go to <a href="https://console.cloud.google.com/apis/credentials" target="_blank" rel="noopener">Credentials</a>, choose <b>Create credentials → OAuth client ID</b>, set the type to <b>Desktop app</b>, then click <b>Download JSON</b>.</li>
</ol></details>
<form method="post" action="{{ url_for('upload_secret') }}" enctype="multipart/form-data" class="actions">
<input type="file" name="file" accept=".json,application/json"><button>{{ 'Replace' if has_secret else 'Upload' }} client JSON</button></form></div>

<div class="card"><h3 style="margin-top:0">2. Sign in</h3>
{% if signed_in %}<p>✓ Connected. Clips upload to the channel you signed in with.</p>
<form method="post" action="{{ url_for('youtube_sign_out') }}"><button>Sign out</button></form>
{% elif auth_url %}
<p><b>a.</b> <a class="btn" href="{{ auth_url }}" target="_blank" rel="noopener">Open Google sign-in</a>. Pick the account that owns your channel and allow access. If Google warns that the app isn't verified, click <i>Advanced → Go to app</i>.</p>
<p><b>b.</b> Google then sends you to a <code>localhost:8080</code> page that won't load. That's expected. Copy the full address from the address bar and paste it here:</p>
<form method="post" action="{{ url_for('youtube_finish') }}" class="inline"><input type="text" name="pasted" placeholder="http://localhost:8080/?state=...&code=..." required><button class="primary">Finish</button></form>
{% elif has_secret %}<form method="post" action="{{ url_for('youtube_start') }}"><button class="primary">Start sign-in</button></form>
{% else %}<p class="muted">Upload the OAuth client first.</p>{% endif %}
</div>
"""
