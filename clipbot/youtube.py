"""YouTube sign-in and uploads.

Sign-in uses a Google OAuth client of type "Desktop app". The container usually isn't reachable
as "localhost" from the browser you sign in with, so the flow is copy-paste:
  1. start_auth() returns a Google sign-in URL.
  2. After approving, Google redirects the browser to http://localhost:8080/?code=... which
     won't load. That's expected; copy the whole URL from the address bar.
  3. finish_auth(pasted_url) swaps the code for a refresh token saved in DATA_DIR.
`clipbot auth` runs this in a terminal; the web UI (step 4) will offer the same thing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
REDIRECT_URI = "http://localhost:8080/"
CLIENT_SECRET_FILE = "client_secret.json"
TOKEN_FILE = "youtube_token.json"
PENDING_FILE = "oauth_pending.json"

# The redirect target is plain-http localhost, which oauthlib refuses unless told it's fine.
os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")


class NotSignedIn(Exception):
    pass


def _flow(data: Path, state: str | None = None, code_verifier: str | None = None):
    from google_auth_oauthlib.flow import Flow

    secret = data / CLIENT_SECRET_FILE
    if not secret.exists():
        raise NotSignedIn(
            f"{secret} is missing. Download your OAuth client (type: Desktop app) JSON from "
            "Google Cloud Console > APIs & Services > Credentials and save it there."
        )
    flow = Flow.from_client_secrets_file(
        str(secret), scopes=SCOPES, redirect_uri=REDIRECT_URI, state=state,
        code_verifier=code_verifier, autogenerate_code_verifier=code_verifier is None,
    )
    return flow


def start_auth(data: Path) -> str:
    flow = _flow(data)
    url, state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    # Saved so finish_auth works from another process (e.g. the web UI after a restart).
    (data / PENDING_FILE).write_text(json.dumps({"state": state, "code_verifier": flow.code_verifier}))
    return url


def finish_auth(data: Path, pasted: str) -> None:
    pending_path = data / PENDING_FILE
    if not pending_path.exists():
        raise NotSignedIn("start sign-in first")
    pending = json.loads(pending_path.read_text())
    flow = _flow(data, state=pending["state"], code_verifier=pending["code_verifier"])
    pasted = pasted.strip()
    if pasted.startswith("http"):
        flow.fetch_token(authorization_response=pasted)
    else:  # just the code
        flow.fetch_token(code=pasted)
    _save(data, flow.credentials)
    pending_path.unlink(missing_ok=True)
    log.info("YouTube sign-in saved")


def _save(data: Path, creds) -> None:
    path = data / TOKEN_FILE
    tmp = path.with_suffix(".tmp")
    tmp.write_text(creds.to_json())
    os.chmod(tmp, 0o600)
    tmp.replace(path)


def is_signed_in(data: Path) -> bool:
    return (data / TOKEN_FILE).exists()


def credentials(data: Path):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    path = data / TOKEN_FILE
    if not path.exists():
        raise NotSignedIn("not signed in to YouTube yet; run `clipbot auth`")
    creds = Credentials.from_authorized_user_file(str(path), SCOPES)
    if not creds.valid:
        if creds.refresh_token:
            from google.auth.exceptions import RefreshError

            try:
                creds.refresh(Request())
            except RefreshError as exc:
                raise NotSignedIn(f"YouTube sign-in expired or was revoked; sign in again in the web UI ({exc})")
            _save(data, creds)
        else:
            raise NotSignedIn("YouTube sign-in expired; run `clipbot auth` again")
    return creds


def upload(data: Path, video: Path, title: str, description: str, tags: list[str],
           privacy: str, max_mbps: float = 0.0, category_id: str = "20") -> tuple[str, str]:
    """Upload a video and return (YouTube id, privacy YouTube actually gave it). Category 20 is Gaming.
    max_mbps > 0 limits the average upload speed (megabits per second)."""
    import time

    from googleapiclient.discovery import build
    from googleapiclient.http import MediaFileUpload

    youtube = build("youtube", "v3", credentials=credentials(data), cache_discovery=False)
    body = {
        "snippet": {
            "title": title[:100],
            "description": description[:5000],
            "tags": tags,
            "categoryId": category_id,
        },
        "status": {
            "privacyStatus": privacy,
            "selfDeclaredMadeForKids": False,
        },
    }
    # Smaller chunks when throttled so the pacing is smooth (must be a multiple of 256 KB).
    chunk = 1024 * 1024 if max_mbps > 0 else 8 * 1024 * 1024
    media = MediaFileUpload(str(video), mimetype="video/mp4", chunksize=chunk, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = None
    started = time.monotonic()
    while response is None:
        status, response = request.next_chunk(num_retries=3)
        if status:
            log.debug("upload %s: %d%%", video.name, int(status.progress() * 100))
            if max_mbps > 0:
                time.sleep(throttle_delay(status.resumable_progress, time.monotonic() - started, max_mbps))
    actual = (response.get("status") or {}).get("privacyStatus") or privacy
    if actual != privacy:
        log.warning("asked YouTube for %s but it made %s %s; check the video in YouTube Studio",
                    privacy, response["id"], actual)
    return response["id"], actual


def throttle_delay(bytes_sent: int, elapsed: float, max_mbps: float) -> float:
    """Seconds to wait so the average speed so far stays at or under max_mbps."""
    target = bytes_sent * 8 / (max_mbps * 1_000_000)
    return max(0.0, target - elapsed)
