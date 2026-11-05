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

from flask import Flask, abort, flash, jsonify, redirect, render_template_string, request, send_file, url_for

from . import config, logbuffer, titles, youtube
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
        ("render_ahead", "Render ahead", "How many clips to keep rendered and waiting for upload. 0 = render everything immediately. At least 10 are kept ready to swipe through."),
        ("crf", "Quality (CRF)", "Lower is better quality and bigger files. 18-23 is sensible."),
        ("x264_preset", "Encode speed", "Slower presets make smaller files."),
        ("output_width", "Width", ""),
        ("output_height", "Height", ""),
    ]),
    ("YouTube", [
        ("upload_enabled", "Upload approved clips", "Off = render only. Only clips you approve are ever uploaded."),
        ("privacy", "Privacy", ""),
        ("post_interval_minutes", "Post every (minutes)", "Minimum gap between uploads, e.g. 120 = at most one every 2 hours. 0 = no gap."),
        ("post_window", "Posting hours", "Only post in this local time window, e.g. 16:00-23:00. Leave empty for any time."),
        ("daily_upload_limit", "Max uploads per 24 h", "0 = no cap. YouTube's API allows 100 a day."),
        ("upload_limit_mbps", "Upload speed limit (Mbps)", "Caps upload bandwidth so it doesn't fill your connection. 0 = unlimited."),
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
            last = st.last_upload_at()
            to_review = sum(1 for c in st.by_status("rendered") if not c.approved)
        finally:
            st.close()
        cfg = config.load(data)
        next_post = ""
        if last and cfg.post_interval_minutes and time.time() - last < cfg.post_interval_minutes * 60:
            next_post = _fmt_time(last + cfg.post_interval_minutes * 60)
        return render_template_string(
            _page(INDEX), next_post=next_post, clips=clips, counts=counts, status=status, cfg=cfg,
            uploads_24h=uploads_24h, fmt_time=_fmt_time, Path=Path,
            to_review=to_review,
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
            if not st.set_title(clip.id, title):
                return f"#{clip.id} is already {st.get(clip.id).status}, so its title can't change any more."
            return f"Saved title for #{clip.id}."
        return _clip_action(clip_id, fn)

    @app.post("/clips/<int:clip_id>/approve")
    def approve(clip_id):
        # The Approve button submits the title box too, so a typed title isn't lost by not pressing Save.
        title = request.form.get("title", "").strip()[:100]

        def fn(st, clip):
            if title and title != clip.title and not st.set_title(clip.id, title):
                return f"#{clip.id} is already {st.get(clip.id).status}, so its title can't change any more."
            if not st.set_approved(clip.id, True):
                return f"#{clip.id} is {st.get(clip.id).status}, so it can't be approved."
            log.info("approved clip #%d on the Clips page", clip.id)
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
            if clip.status in ("uploaded", "uploading"):
                return "That clip is already on YouTube."
            st.set_status(clip.id, "queued")
            return f"#{clip.id} will be rendered again with the current settings."
        return _clip_action(clip_id, fn)

    @app.post("/clips/<int:clip_id>/skip")
    def skip(clip_id):
        def fn(st, clip):
            if clip.status in ("uploaded", "uploading", "processing"):
                return f"Can't skip a clip that's {clip.status}."
            st.set_status(clip.id, "skipped")
            return f"#{clip.id} skipped. It won't be uploaded."
        return _clip_action(clip_id, fn)

    @app.get("/review")
    def review():
        return render_template_string(REVIEW, cfg=config.load(data))

    @app.get("/review/queue")
    def review_queue():
        """Rendered clips still waiting for a swipe, oldest first (the order they'd upload in)."""
        st = db()
        try:
            clips = [c for c in st.by_status("rendered") if not c.approved]
            counts = st.counts()
            queued = counts["queued"] + counts["processing"]
        finally:
            st.close()
        items = [
            {"id": c.id, "name": Path(c.rel_path).name,
             "title": c.title or titles.rule_title(Path(c.rel_path).name),
             "video": url_for("video", clip_id=c.id)}
            for c in clips if c.output_path and Path(c.output_path).exists()
        ]
        return jsonify(clips=items, queued=queued)

    @app.post("/clips/<int:clip_id>/review")
    def review_action(clip_id):
        """JSON endpoint behind the swipe view: approve (with title), skip, or undo either."""
        body = request.get_json(silent=True) or {}
        action = body.get("action")
        st = db()
        try:
            clip = st.get(clip_id)
            if clip is None:
                abort(404)
            if action == "approve":
                title = str(body.get("title", "")).strip()[:100]
                if not title:
                    return jsonify(error="Title can't be empty."), 400
                if clip.status != "rendered" or not st.set_title(clip.id, title):
                    now = st.get(clip.id).status
                    return jsonify(error=f"Too late, that clip is {now} now, so the title can't change."), 409
                if not st.set_approved(clip.id, True):
                    return jsonify(error=f"Too late, that clip is {st.get(clip.id).status} now."), 409
                log.info("approved clip #%d in the swipe review", clip.id)
            elif action == "skip":
                if clip.status != "rendered":
                    return jsonify(error=f"That clip is {clip.status} now."), 409
                st.set_status(clip.id, "skipped")
                log.info("skipped clip #%d in the swipe review", clip.id)
            elif action == "undo":
                if clip.status == "skipped":
                    st.set_status(clip.id, "rendered")
                    log.info("undid skip of clip #%d", clip.id)
                elif clip.status == "rendered":
                    st.set_approved(clip.id, False)
                    log.info("undid approval of clip #%d", clip.id)
                else:
                    return jsonify(error=f"Too late to undo, it's {clip.status}."), 409
            else:
                return jsonify(error="Unknown action."), 400
        finally:
            st.close()
        return jsonify(ok=True)

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
    config.parse_window(cfg.post_window)
    if cfg.post_interval_minutes < 0 or cfg.upload_limit_mbps < 0:
        raise ValueError("post interval and speed limit can't be negative")
    if cfg.render_ahead < 0:
        raise ValueError("render ahead can't be negative")
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
header b{font-size:17px;margin-right:8px}header nav{display:contents}header a{color:var(--muted);text-decoration:none}header a.on{color:var(--fg);font-weight:600}
.pill{margin-left:auto;font-size:13px;padding:3px 10px;border-radius:99px;border:1px solid var(--line)}
main{max-width:1100px;margin:0 auto;padding:20px 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:16px}
.flash{border-left:4px solid var(--accent)}
.counts{display:flex;gap:10px;flex-wrap:wrap}.counts a{flex:1;min-width:110px;text-decoration:none;color:inherit}
.counts .n{font-size:24px;font-weight:600}.counts .card{margin:0;padding:10px 14px}.counts a.on .card{border-color:var(--accent)}
table{width:100%;border-collapse:collapse}td,th{padding:9px 6px;border-top:1px solid var(--line);vertical-align:top;text-align:left}
th{font-size:12px;color:var(--muted);font-weight:600;text-transform:uppercase;border-top:0}
.s{font-size:12px;padding:2px 8px;border-radius:99px;border:1px solid currentColor;white-space:nowrap}
.s-uploaded,.s-uploading{color:var(--ok)}.s-failed{color:var(--bad)}.s-rendered{color:var(--accent)}.s-processing,.s-queued{color:var(--warn)}.s-skipped{color:var(--muted)}
.muted{color:var(--muted);font-size:13px}.err{color:var(--bad);font-size:13px;word-break:break-word}
input[type=text],input[type=number],input[type=password],select,textarea{width:100%;padding:7px 9px;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);font:inherit}
textarea{min-height:70px}
button,.btn{font:inherit;font-size:13px;padding:6px 11px;border-radius:7px;border:1px solid var(--line);background:var(--card);color:var(--fg);cursor:pointer;text-decoration:none;display:inline-block}
.reviewcta{display:block;text-align:center;margin-bottom:12px;font-size:16px;padding:12px}
button.primary,.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.row{display:grid;grid-template-columns:220px 1fr;gap:6px 16px;align-items:start;padding:8px 0;border-top:1px solid var(--line)}
.row:first-of-type{border-top:0}.row label{font-weight:500;padding-top:6px}
.inline{display:flex;gap:6px}.inline input{flex:1}.actions{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
form{margin:0}video{max-height:420px;border-radius:8px;background:#000}.preview{margin-top:8px}
details summary{cursor:pointer;color:var(--accent);font-size:13px}
code{background:var(--bg);padding:1px 5px;border-radius:5px;border:1px solid var(--line);word-break:break-all}
.sm-only{display:none}.clipname{word-break:break-word}
.savebar{position:sticky;bottom:0;padding:12px 0;background:linear-gradient(transparent,var(--bg) 35%)}
@media (max-width:700px){
.row{grid-template-columns:1fr}.hide-sm{display:none}.sm-only{display:inline}
header{position:sticky;top:0;z-index:5;gap:4px 2px;padding:8px 12px}
header b{width:100%;display:flex;align-items:center;justify-content:space-between;margin:0 0 2px}
header nav{display:flex;width:100%;overflow-x:auto}header nav a{flex:1;text-align:center;padding:8px 6px;border-radius:7px}
header nav a.on{background:var(--bg)}
.pill{margin:0;font-size:12px;font-weight:400;padding:2px 9px}.pill.ok{color:var(--ok)}.pill.no{color:var(--warn)}
main{padding:12px 12px 24px}.card{padding:12px 14px;margin-bottom:12px}
.counts{display:grid;grid-template-columns:repeat(3,1fr);gap:8px}.counts a{min-width:0}
.counts .card{padding:8px 10px}.counts .n{font-size:20px}
input[type=text],input[type=number],input[type=password],select,textarea{font-size:16px;padding:9px 10px}
button,.btn{font-size:15px;padding:9px 14px;min-height:40px}
input[type=checkbox]{width:22px;height:22px}
.row.bool{grid-template-columns:1fr auto;align-items:center}.row.bool>div{display:contents}.row.bool .muted{grid-column:1/-1;order:3}
.row.bool label{padding-top:0}
.cliplist{background:none;border:0;padding:0!important}
.cliplist table,.cliplist tbody,.cliplist tr,.cliplist td{display:block}
.cliplist thead,.cliplist .c-id{display:none}
.savebar button{width:100%}
.cliplist tr{display:grid;grid-template-columns:1fr auto;gap:6px 10px;background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin-bottom:10px}
.cliplist td{border:0;padding:0}.cliplist .c-status{text-align:right}.cliplist .c-title,.cliplist .c-empty{grid-column:1/-1}
.actions button{flex:1}
video{width:100%;max-height:70vh}
.inline.stack-sm{flex-direction:column}
}
</style></head><body>
<header><b>clip-bot <span class="pill sm-only {{ 'ok' if signed_in else 'no' }}">{{ 'YouTube ✓' if signed_in else 'YouTube not connected' }}</span></b>
<nav><a href="{{ url_for('index') }}" class="{{ 'on' if nav=='index' }}">Clips</a>
<a href="{{ url_for('review') }}">Review</a>
<a href="{{ url_for('settings') }}" class="{{ 'on' if nav=='settings' }}">Settings</a>
<a href="{{ url_for('youtube_page') }}" class="{{ 'on' if nav=='youtube_page' }}">YouTube</a>
<a href="{{ url_for('logs') }}" class="{{ 'on' if nav=='logs' }}">Logs</a></nav>
<span class="pill hide-sm">{{ 'YouTube connected' if signed_in else 'YouTube not connected' }}</span></header>
<main>
{% with msgs = get_flashed_messages() %}{% for m in msgs %}<div class="card flash">{{ m }}</div>{% endfor %}{% endwith %}
__BODY__
</main></body></html>"""

INDEX = """
{% if not signed_in %}<div class="card flash">YouTube isn't connected yet, so clips will render but not upload.
<a href="{{ url_for('youtube_page') }}">Connect YouTube</a></div>{% endif %}
{% if signed_in and cfg.upload_enabled and cfg.privacy != 'public' %}<div class="card flash">New uploads are <b>{{ cfg.privacy }}</b>, so they won't show up in the Shorts feed and get no views.
{% if cfg.privacy == 'private' %}Only you can see them. {% endif %}To post them for everyone, set Privacy to public in <a href="{{ url_for('settings') }}">Settings</a>. Videos already uploaded have to be changed in YouTube Studio.</div>{% endif %}
{% if to_review %}<a class="btn primary reviewcta" href="{{ url_for('review') }}">Swipe to review {{ to_review }} clip{{ 's' if to_review != 1 }}</a>{% endif %}
<div class="counts">
<a href="{{ url_for('index') }}" class="{{ 'on' if not status }}"><div class="card"><div class="n">{{ counts.values()|sum }}</div><div class="muted">all</div></div></a>
{% for s in ['queued','rendered','uploaded','failed','skipped'] %}
<a href="{{ url_for('index', status=s) }}" class="{{ 'on' if status==s }}"><div class="card"><div class="n">{{ counts[s] + (counts['processing'] if s=='queued' else 0) + (counts['uploading'] if s=='uploaded' else 0) }}</div><div class="muted">{{ s }}</div></div></a>
{% endfor %}</div>
<p class="muted">{{ uploads_24h }} of {{ cfg.daily_upload_limit or '∞' }} uploads used in the last 24 h{% if cfg.post_interval_minutes %} · one every {{ cfg.post_interval_minutes }} min{% endif %}{% if cfg.post_window %} · posting {{ cfg.post_window }}{% endif %}{% if next_post %} · next post after {{ next_post }}{% endif %} · new uploads are <b>{{ cfg.privacy }}</b> · only approved clips upload{% if not cfg.upload_enabled %} · uploading is off{% endif %}
· <a href="{{ request.full_path }}">refresh</a></p>
<div class="card cliplist" style="padding:4px 12px">
<table><thead><tr><th>#</th><th>Clip</th><th>Status</th><th>Title</th></tr></thead><tbody>
{% for c in clips %}<tr>
<td class="muted c-id">{{ c.id }}</td>
<td class="c-clip"><div class="clipname">{{ Path(c.rel_path).name }}</div><div class="muted"><span class="sm-only">#{{ c.id }} · </span>{{ fmt_time(c.created_at) }}</div></td>
<td class="c-status"><span class="s s-{{ c.status }}">{{ c.status }}{% if c.status=='rendered' %}{{ ' · approved' if c.approved else ' · needs approval' }}{% endif %}</span>
{% if c.status=='rendered' and c.retry_at %}<div class="muted">retry after {{ fmt_time(c.retry_at) }}</div>{% endif %}
{% if c.video_id %}<div><a href="https://youtube.com/shorts/{{ c.video_id }}" target="_blank" rel="noopener">view</a>{% if c.privacy %} · <span class="{{ 'err' if c.privacy != 'public' }}">{{ c.privacy }}</span>{% endif %}</div>{% endif %}</td>
<td class="c-title">
{% if c.status in ['uploaded','uploading'] %}{{ c.title }}
{% else %}<form method="post" action="{{ url_for('set_title', clip_id=c.id) }}" class="inline" id="tf-{{ c.id }}">
<input type="hidden" name="next" value="{{ request.full_path }}">
<input type="text" name="title" maxlength="100" value="{{ c.title or '' }}" placeholder="{{ 'generated after rendering' if not c.title }}"><button>Save</button></form>{% endif %}
{% if c.error %}<div class="err">{{ c.error[:300] }}</div>{% endif %}
<div class="actions">
{% if c.status=='rendered' and not c.approved %}<button class="primary" form="tf-{{ c.id }}" formaction="{{ url_for('approve', clip_id=c.id) }}">Approve</button>{% endif %}
{% if c.status in ['failed','skipped'] or (c.status=='rendered' and c.retry_at) %}<form method="post" action="{{ url_for('retry', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button>Retry</button></form>{% endif %}
{% if c.status in ['rendered','failed','skipped'] %}<form method="post" action="{{ url_for('rerender', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button>Re-render</button></form>{% endif %}
{% if c.status in ['queued','rendered','failed'] %}<form method="post" action="{{ url_for('skip', clip_id=c.id) }}"><input type="hidden" name="next" value="{{ request.full_path }}"><button>Skip</button></form>{% endif %}
{% if c.output_path and c.status in ['rendered','failed','skipped'] %}<button type="button" onclick="togglePreview(this, {{ c.id }})">▶ Preview</button>{% endif %}
</div>
{% if c.output_path and c.status in ['rendered','failed','skipped'] %}<div class="preview" id="preview-{{ c.id }}" hidden><video data-src="{{ url_for('video', clip_id=c.id) }}" controls preload="none" playsinline></video></div>{% endif %}
</td></tr>
{% else %}<tr><td colspan="4" class="muted c-empty" style="padding:20px 6px">No clips yet. Export a clip to the watch folder ({{ cfg.watch_dir }}) and it shows up here within a minute.</td></tr>{% endfor %}
</tbody></table></div>
<script>
function togglePreview(btn, id){
  var box=document.getElementById('preview-'+id), v=box.querySelector('video');
  if(box.hidden){ if(!v.src) v.src=v.dataset.src; box.hidden=false; btn.textContent='■ Hide preview'; }
  else { v.pause(); box.hidden=true; btn.textContent='▶ Preview'; }
}
</script>
"""

SETTINGS = """
<form method="post">
{% for section, items in layout %}<div class="card"><h3 style="margin:0 0 6px">{{ section }}</h3>
{% for name, label, help in items %}<div class="row{{ ' bool' if types[name] in ['bool'] }}"><label for="{{ name }}">{{ label }}</label><div>
{% if name in choices %}<select name="{{ name }}" id="{{ name }}">{% for o in choices[name] %}<option {{ 'selected' if cfg[name]==o }}>{{ o }}</option>{% endfor %}</select>
{% elif types[name] in ['bool'] %}<input type="checkbox" name="{{ name }}" id="{{ name }}" {{ 'checked' if cfg[name] }}>
{% elif name in secret %}<input type="password" name="{{ name }}" id="{{ name }}" autocomplete="off" placeholder="{{ 'saved (hidden)' if cfg[name] else 'not set' }}">
{% if cfg[name] %}<label class="muted"><input type="checkbox" name="{{ name }}__clear"> remove saved key</label>{% endif %}
{% elif name in multiline %}<textarea name="{{ name }}" id="{{ name }}">{{ cfg[name] }}</textarea>
{% else %}<input type="{{ 'number' if types[name] in ['int','float'] else 'text' }}" {{ 'step=any' if types[name]=='float' }} name="{{ name }}" id="{{ name }}" value="{{ cfg[name] }}">{% endif %}
{% if help %}<div class="muted">{{ help }}</div>{% endif %}</div></div>{% endfor %}
</div>{% endfor %}
<div class="savebar"><button class="primary">Save settings</button></div>
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
<li>In <a href="https://console.cloud.google.com/projectcreate" target="_blank" rel="noopener">Google Cloud Console</a>, create a <b>new</b> project just for this bot, so changes can't affect other apps.</li>
<li>Enable the <a href="https://console.cloud.google.com/apis/library/youtube.googleapis.com" target="_blank" rel="noopener">YouTube Data API v3</a> in it.</li>
<li>Open <a href="https://console.cloud.google.com/auth/overview" target="_blank" rel="noopener">Google Auth Platform</a>. Under <b>Branding</b>, set an app name and your email. Under <b>Audience</b>, choose External, add the Google account that owns your channel as a test user, then click <b>Publish app</b>. Apps left in “Testing” lose their sign-in every 7 days. You don't need Google's review, because you're the only user.</li>
<li>Under <b>Clients</b>, choose <b>Create client</b>, set the type to <b>Desktop app</b>, and download the JSON.</li>
</ol></details>
<form method="post" action="{{ url_for('upload_secret') }}" enctype="multipart/form-data" class="actions">
<input type="file" name="file" accept=".json,application/json"><button>{{ 'Replace' if has_secret else 'Upload' }} client JSON</button></form></div>

<div class="card"><h3 style="margin-top:0">2. Sign in</h3>
{% if signed_in %}<p>✓ Connected. Clips upload to the channel you signed in with.</p>
<form method="post" action="{{ url_for('youtube_sign_out') }}"><button>Sign out</button></form>
{% elif auth_url %}
<p><b>a.</b> <a class="btn" href="{{ auth_url }}" target="_blank" rel="noopener">Open Google sign-in</a>. Pick the account that owns your channel and allow access. If Google warns that the app isn't verified, click <i>Advanced → Go to app</i>.</p>
<p><b>b.</b> Google then sends you to a <code>localhost:8080</code> page that won't load. That's expected. Copy the full address from the address bar and paste it here:</p>
<form method="post" action="{{ url_for('youtube_finish') }}" class="inline stack-sm"><input type="text" name="pasted" placeholder="http://localhost:8080/?state=...&code=..." required><button class="primary">Finish</button></form>
{% elif has_secret %}<form method="post" action="{{ url_for('youtube_start') }}"><button class="primary">Start sign-in</button></form>
{% else %}<p class="muted">Upload the OAuth client first.</p>{% endif %}
</div>
"""

# Full-screen swipe view, built for phones. Right = approve (after a title check), left = skip,
# up/down = next/previous clip. Talks to /review/queue and /clips/<id>/review.
REVIEW = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="theme-color" content="#000"><title>Review · clip-bot</title>
<style>
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0;height:100%;background:#000;color:#fff;font:15px/1.4 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;overflow:hidden;overscroll-behavior:none}
button{font:inherit;color:inherit;cursor:pointer;border:0;background:none}
.feed{position:fixed;inset:0;overflow-y:auto;overflow-anchor:none;scroll-snap-type:y mandatory;overscroll-behavior:contain;scrollbar-width:none}
.feed::-webkit-scrollbar{display:none}
.slide{position:relative;height:100dvh;height:100vh;scroll-snap-align:start;scroll-snap-stop:always;overflow:hidden}
@supports(height:100dvh){.slide{height:100dvh}}
.card{position:absolute;inset:0;touch-action:pan-y;user-select:none;-webkit-user-select:none;will-change:transform}
.card video{width:100%;height:100%;object-fit:contain;background:#000;display:block;pointer-events:none}
.shade{position:absolute;left:0;right:0;bottom:0;height:45%;background:linear-gradient(transparent,rgba(0,0,0,.75));pointer-events:none}
.info{position:absolute;left:16px;right:16px;bottom:calc(112px + env(safe-area-inset-bottom));pointer-events:none;text-shadow:0 1px 3px rgba(0,0,0,.8)}
.info .t{font-size:18px;font-weight:600;word-break:break-word}.info .n{font-size:13px;opacity:.75;word-break:break-all;margin-top:2px}
.stamp{position:absolute;top:22%;padding:6px 14px;border:4px solid;border-radius:10px;font-size:34px;font-weight:800;letter-spacing:2px;opacity:0;pointer-events:none}
.stamp.yes{left:24px;color:#51cf66;transform:rotate(-14deg)}.stamp.no{right:24px;color:#ff6b6b;transform:rotate(14deg)}
.paused{position:absolute;top:50%;left:50%;transform:translate(-50%,-50%);font-size:64px;opacity:0;transition:opacity .15s;pointer-events:none;text-shadow:0 2px 8px #000}
.card.is-paused .paused{opacity:.85}
.bar{position:fixed;top:0;left:0;right:0;z-index:5;display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top)) 14px 10px;background:linear-gradient(rgba(0,0,0,.6),transparent);pointer-events:none}
.bar>*{pointer-events:auto}.bar a{color:#fff;text-decoration:none;font-weight:600;padding:6px 4px}
.bar .count{flex:1;text-align:center;font-size:14px;opacity:.85}
.bar button{width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.15);font-size:18px;flex:none}
.bar .vol{width:96px;flex:none;accent-color:#fff;margin:0}
.notice{position:fixed;z-index:5;left:12px;right:12px;top:calc(60px + env(safe-area-inset-top));background:rgba(230,119,0,.95);color:#fff;border-radius:10px;padding:10px 12px;font-size:14px;display:flex;gap:10px;align-items:center}
.notice form{margin:0;margin-left:auto}.notice button{background:#fff;color:#000;border-radius:8px;padding:8px 12px;font-weight:600;white-space:nowrap}
.btns{position:fixed;z-index:4;left:0;right:0;bottom:calc(22px + env(safe-area-inset-bottom));display:flex;justify-content:center;gap:56px;pointer-events:none}
.btns button{pointer-events:auto;width:68px;height:68px;border-radius:50%;font-size:30px;background:rgba(20,20,20,.7);border:2px solid;backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);transition:transform .1s}
.btns button:active{transform:scale(.9)}.btns .no{color:#ff6b6b}.btns .yes{color:#51cf66}
.btns[hidden]{display:none}
.end{display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center;padding:24px;gap:12px}
.end .big{font-size:48px}.end p{margin:0;opacity:.8;max-width:320px}
.end a,.end button{color:#000;background:#fff;border-radius:10px;padding:10px 16px;font-weight:600;text-decoration:none}
.hint{position:fixed;z-index:6;left:50%;top:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,.8);border-radius:14px;padding:16px 20px;text-align:center;line-height:1.8;transition:opacity .4s;pointer-events:none}
.modal{position:fixed;inset:0;z-index:10;background:rgba(0,0,0,.6);display:flex;align-items:flex-end;justify-content:center}
.modal[hidden]{display:none}
.sheet{width:100%;max-width:520px;background:#1c1f26;border-radius:16px 16px 0 0;padding:18px 16px calc(16px + env(safe-area-inset-bottom));box-shadow:0 -6px 30px rgba(0,0,0,.5)}
@media (min-width:600px){.modal{align-items:center}.sheet{border-radius:16px}}
.sheet h2{margin:0 0 10px;font-size:17px}
.sheet textarea{width:100%;min-height:84px;resize:none;font:inherit;font-size:17px;padding:10px 12px;border-radius:10px;border:1px solid #3a3f4b;background:#0f1115;color:#fff}
.sheet .meta{display:flex;justify-content:space-between;font-size:12px;opacity:.65;margin:6px 2px 14px}
.sheet .err{color:#ff8787;font-size:14px;margin-bottom:10px}
.sheet .row{display:flex;gap:10px}.sheet .row button{flex:1;padding:13px;border-radius:10px;font-weight:600;font-size:16px;background:#2c313c}
.sheet .row .ok{background:#2f9e44}.sheet .row .ok:disabled{opacity:.5}
.toast{position:fixed;z-index:8;left:50%;top:calc(64px + env(safe-area-inset-top));transform:translateX(-50%);background:#fff;color:#000;border-radius:99px;padding:8px 8px 8px 16px;display:flex;gap:12px;align-items:center;font-size:14px;max-width:calc(100% - 24px);box-shadow:0 4px 20px rgba(0,0,0,.4)}
.toast[hidden]{display:none}.toast span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.toast button{background:#000;color:#fff;border-radius:99px;padding:6px 12px;font-weight:600;flex:none}
</style></head><body>
<div class="bar"><a href="{{ url_for('index') }}">‹ Clips</a><span class="count" id="count">Loading…</span>
<input type="range" class="vol" id="vol" min="0" max="100" step="1" value="100" aria-label="Volume" title="Volume (only for reviewing here, uploads are unaffected)">
<button id="mute" aria-label="Sound">🔇</button></div>
<div class="feed" id="feed"><div class="slide end" id="end"><div class="big">⏳</div><p>Loading clips…</p></div></div>
<div class="btns" id="btns" hidden><button class="no" id="bNo" aria-label="Skip">✕</button><button class="yes" id="bYes" aria-label="Approve">✓</button></div>
<div class="modal" id="modal" hidden><div class="sheet" role="dialog" aria-labelledby="mh">
<h2 id="mh">YouTube title</h2>
<textarea id="title" maxlength="100" rows="3" enterkeyhint="done"></textarea>
<div class="meta"><span id="mname"></span><span id="mlen"></span></div>
<div class="err" id="merr" hidden></div>
<div class="row"><button id="mCancel">Cancel</button><button class="ok" id="mOk">Okay</button></div>
</div></div>
<div class="toast" id="toast" hidden><span id="tmsg"></span><button id="tUndo">Undo</button></div>
<script>
(function(){
var feed=document.getElementById('feed'), end=document.getElementById('end'), countEl=document.getElementById('count');
var btns=document.getElementById('btns'), muteBtn=document.getElementById('mute'), volEl=document.getElementById('vol');
var modal=document.getElementById('modal'), titleEl=document.getElementById('title'), mErr=document.getElementById('merr');
var toast=document.getElementById('toast'), tmsg=document.getElementById('tmsg');
var QUEUE={{ url_for('review_queue')|tojson }}, ACT={{ url_for('review_action', clip_id=0)|tojson }};
var muted=true, volume=1, seen={}, left=0, queued=0, current=null, pending=null, lastAction=null, toastTimer=null, loading=false;

function slides(){return Array.prototype.slice.call(feed.querySelectorAll('.slide[data-id]'));}
function updateCount(){
  left=slides().length;
  countEl.textContent=left? left+' to review' : 'All caught up';
  btns.hidden=!current;
}
function endText(){
  var q=queued? queued+' more clip'+(queued==1?' is':'s are')+' waiting to render. They show up here when ready.' : 'New clips show up here once they finish rendering.';
  end.innerHTML='<div class="big">🎉</div><p><b>All caught up.</b></p><p>'+q+'</p><button type="button" id="reload">Check again</button><a href="'+{{ url_for('index')|tojson }}+'">Back to clips</a>';
  document.getElementById('reload').onclick=function(){load(true);};
}

function makeSlide(c){
  var s=document.createElement('section'); s.className='slide'; s.dataset.id=c.id;
  s.innerHTML='<div class="card"><video playsinline muted loop preload="none"></video><div class="shade"></div>'+
    '<div class="info"><div class="t"></div><div class="n"></div></div>'+
    '<div class="stamp yes">POST</div><div class="stamp no">SKIP</div><div class="paused">▶</div></div>';
  s.clip=c; s.querySelector('video').dataset.src=c.video; setInfo(s);
  bindSwipe(s); io.observe(s);
  return s;
}
function setInfo(s){s.querySelector('.t').textContent=s.clip.title; s.querySelector('.n').textContent=s.clip.name;}

function load(manual){
  if(loading) return; loading=true;
  fetch(QUEUE,{cache:'no-store'}).then(function(r){return r.json();}).then(function(d){
    queued=d.queued;
    var first=null;
    d.clips.forEach(function(c){ if(!seen[c.id]){ seen[c.id]=1; var s=makeSlide(c); feed.insertBefore(s, end); first=first||s; } });
    endText();
    // Snapping would otherwise stay on the "all caught up" slide; jump to the first new clip.
    if(!current && first){ feed.scrollTop=first.offsetTop; activate(first); }
    updateCount();
  }).catch(function(){ if(manual) alert("Couldn't reach clip-bot."); }).then(function(){loading=false;});
}

// Play whatever slide is on screen; keep only neighbours loaded so phones don't run out of memory.
var io=new IntersectionObserver(function(es){
  es.forEach(function(e){ if(e.isIntersecting && e.intersectionRatio>0.6) activate(e.target); });
},{root:feed,threshold:[0.6]});
io.observe(end);
function activate(s){
  if(!s) return;
  current=s.dataset.id? s : null;
  var all=Array.prototype.slice.call(feed.children), i=all.indexOf(s);
  all.forEach(function(x,j){
    var v=x.querySelector('video'); if(!v) return;
    if(Math.abs(j-i)<=1){ if(!v.getAttribute('src')){ v.src=v.dataset.src; v.preload='auto'; } }
    else if(v.getAttribute('src')){ v.pause(); v.removeAttribute('src'); v.load(); }
    if(j===i){ v.muted=muted; v.volume=volume; var p=v.play(); if(p&&p.catch)p.catch(function(){}); x.querySelector('.card').classList.remove('is-paused'); }
    else v.pause();
  });
  updateCount();
  if(!current && !loading) load(false);
}
function curVideo(){return current&&current.querySelector('video');}

function setMuted(m){ muted=m; muteBtn.textContent=muted?'🔇':'🔊'; var v=curVideo(); if(v){v.muted=muted; if(!muted&&v.paused)v.play().catch(function(){});} }
muteBtn.onclick=function(){ setMuted(!muted); };

// Playback volume for reviewing only: it changes the <video> element here, never the rendered file or the upload.
// Remembered per browser.
try{ var sv=localStorage.getItem('clipbot.reviewVolume'); if(sv!==null && !isNaN(+sv)) volume=Math.max(0,Math.min(1,+sv)); }catch(_){}
volEl.value=Math.round(volume*100);
volEl.addEventListener('input',function(){
  volume=volEl.value/100;
  try{ localStorage.setItem('clipbot.reviewVolume',String(volume)); }catch(_){}
  var v=curVideo(); if(v) v.volume=volume;
  if(volume>0 && muted) setMuted(false); else if(volume===0 && !muted) setMuted(true);
});
// Keep slider drags from scrolling or swiping the feed.
['pointerdown','touchstart'].forEach(function(t){ volEl.addEventListener(t,function(e){e.stopPropagation();},{passive:true}); });

function bindSwipe(s){
  var card=s.querySelector('.card'), yes=s.querySelector('.stamp.yes'), no=s.querySelector('.stamp.no');
  var st=null, dx=0, drag=false;
  function setX(x,anim){
    card.style.transition=anim?'transform .25s ease-out':'none';
    card.style.transform=x?'translateX('+x+'px) rotate('+(x/18)+'deg)':'';
    var w=feed.clientWidth*0.25;
    yes.style.opacity=Math.max(0,Math.min(1,x/w)); no.style.opacity=Math.max(0,Math.min(1,-x/w));
  }
  s.reset=function(){setX(0,true);};
  s.fly=function(dir,then){ setX(dir*feed.clientWidth*1.3,true); setTimeout(then,230); };
  card.addEventListener('pointerdown',function(e){ if(e.button>0) return; st={x:e.clientX,y:e.clientY,t:Date.now()}; dx=0; drag=false; });
  card.addEventListener('pointermove',function(e){
    if(!st) return;
    var mx=e.clientX-st.x, my=e.clientY-st.y;
    if(!drag){
      if(Math.abs(mx)>12 && Math.abs(mx)>Math.abs(my)*1.3){ drag=true; try{card.setPointerCapture(e.pointerId);}catch(_){} }
      else if(Math.abs(my)>12){ st=null; return; }
      else return;
    }
    dx=mx; setX(dx,false);
  });
  card.addEventListener('pointerup',function(e){
    if(!st) return;
    var dt=Date.now()-st.t, fast=Math.abs(dx)/Math.max(dt,1)>0.6 && Math.abs(dx)>50;
    if(drag){
      if(dx>feed.clientWidth*0.28 || (fast&&dx>0)) approve(s);
      else if(dx<-feed.clientWidth*0.28 || (fast&&dx<0)) skip(s);
      else setX(0,true);
    } else if(dt<350){ togglePlay(s); }
    st=null; drag=false;
  });
  card.addEventListener('pointercancel',function(){ st=null; if(drag) setX(0,true); drag=false; });
}
function togglePlay(s){
  var v=s.querySelector('video'), card=s.querySelector('.card');
  if(v.paused){ v.play().catch(function(){}); card.classList.remove('is-paused'); }
  else { v.pause(); card.classList.add('is-paused'); }
}

function post(id,body){
  return fetch(ACT.replace('/0/','/'+id+'/'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)})
    .then(function(r){return r.json().catch(function(){return {};}).then(function(d){ if(!r.ok) throw new Error(d.error||('Error '+r.status)); return d; });});
}

function approve(s){
  if(!s || pending) return;
  pending=s; s.fly(1,function(){});
  var v=s.querySelector('video'); v.pause();
  titleEl.value=s.clip.title; mErr.hidden=true; document.getElementById('mname').textContent=s.clip.name;
  lenUpdate(); modal.hidden=false;
  setTimeout(function(){ titleEl.focus(); titleEl.setSelectionRange(titleEl.value.length,titleEl.value.length); },50);
}
function lenUpdate(){ var n=titleEl.value.trim().length; document.getElementById('mlen').textContent=n+'/100'; document.getElementById('mOk').disabled=!n; }
titleEl.addEventListener('input',lenUpdate);
titleEl.addEventListener('keydown',function(e){ if(e.key==='Enter'){ e.preventDefault(); confirmApprove(); } });
function closeModal(){ modal.hidden=true; titleEl.blur(); }
document.getElementById('mCancel').onclick=function(){
  var s=pending; pending=null; closeModal();
  if(s){ s.reset(); s.querySelector('video').play().catch(function(){}); }
};
modal.addEventListener('click',function(e){ if(e.target===modal) document.getElementById('mCancel').onclick(); });
function confirmApprove(){
  var s=pending, t=titleEl.value.trim(); if(!s||!t) return;
  var ok=document.getElementById('mOk'); ok.disabled=true;
  post(s.dataset.id,{action:'approve',title:t}).then(function(){
    var old=s.clip.title; s.clip.title=t; setInfo(s); pending=null; closeModal();
    done(s,'Approved: '+t,{oldTitle:old});
  }).catch(function(err){ mErr.textContent=err.message; mErr.hidden=false; lenUpdate(); });
}
document.getElementById('mOk').onclick=confirmApprove;

function skip(s){
  if(!s || pending) return;
  pending=s;
  s.fly(-1,function(){
    post(s.dataset.id,{action:'skip'}).then(function(){ pending=null; done(s,'Skipped '+s.clip.name,{}); })
      .catch(function(err){ pending=null; s.reset(); showToast(err.message,false); });
  });
}

// Take the slide out of the feed; the next one slides into its place.
function done(s,msg,extra){
  var next=s.nextElementSibling; s.querySelector('video').pause();
  lastAction={slide:s,next:next,extra:extra};
  io.unobserve(s); s.remove(); current=null;
  requestAnimationFrame(function(){ activate(next); });
  showToast(msg,true);
}
function showToast(msg,undo){
  tmsg.textContent=msg; document.getElementById('tUndo').hidden=!undo; toast.hidden=false;
  clearTimeout(toastTimer); toastTimer=setTimeout(function(){toast.hidden=true; lastAction=null;},5000);
}
document.getElementById('tUndo').onclick=function(){
  var a=lastAction; if(!a) return; lastAction=null; toast.hidden=true;
  post(a.slide.dataset.id,{action:'undo'}).then(function(){
    var s=a.slide; s.querySelector('.card').style.cssText=''; s.querySelectorAll('.stamp').forEach(function(x){x.style.opacity=0;});
    feed.insertBefore(s,(a.next&&a.next.parentNode)?a.next:end); io.observe(s);
    s.scrollIntoView(); activate(s);
  }).catch(function(err){ showToast(err.message,false); });
};

document.getElementById('bYes').onclick=function(){ approve(current); };
document.getElementById('bNo').onclick=function(){ skip(current); };
document.addEventListener('keydown',function(e){
  if(!modal.hidden){ if(e.key==='Escape') document.getElementById('mCancel').onclick(); return; }
  if(e.target===volEl) return; // arrow keys adjust the volume slider, not approve/skip
  if(e.key==='ArrowRight') approve(current);
  else if(e.key==='ArrowLeft') skip(current);
  else if(e.key===' '&&current){ e.preventDefault(); togglePlay(current); }
});
setInterval(function(){ if(!current && modal.hidden) load(false); },20000);
load(false);
try{ if(!sessionStorage.getItem('clipbot-hint')){ sessionStorage.setItem('clipbot-hint','1');
  var h=document.createElement('div'); h.className='hint'; h.innerHTML='👉 Swipe right to post<br>👈 Swipe left to skip<br>👆 Swipe up for the next clip';
  document.body.appendChild(h); setTimeout(function(){h.style.opacity=0;},2600); setTimeout(function(){h.remove();},3200); } }catch(_){}
})();
</script></body></html>"""
