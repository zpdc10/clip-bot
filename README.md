# clip-bot

Watches a folder of exported gaming clips, converts them to vertical (9:16) Shorts, and uploads them to YouTube. Runs as a Docker container on Unraid.

## Roadmap

1. **Folder watcher** (done): picks up new clips once they've finished copying and never queues the same clip twice.
2. **Vertical render** (done): ffmpeg center crop to 1080x1920, long clips trimmed to their last 60 s.
3. **YouTube upload** (done): Data API upload, private first, titles generated from the filename.
4. **Web UI** (done): LAN-only page for settings, the clip queue (edit titles, approve, retry, skip, preview), and YouTube sign-in.
5. **Unraid packaging** (done): prebuilt image, Unraid template, upload retries, disk cleanup, logs page.

## How the watcher works

The watcher scans `WATCH_DIR` (including subfolders) every `POLL_INTERVAL` seconds. A clip is queued only after its size and modified time look the same on two scans in a row and it hasn't changed for `SETTLE_SECONDS`. That way, clips that are still copying over the network are left alone. Each clip is fingerprinted by its size plus the first and last MB, so a re-export or a renamed copy of the same clip is skipped.

Polling is used instead of filesystem events because SMB copies into Unraid shares don't reliably trigger inotify inside a container.

Queue state lives in `DATA_DIR/clipbot.db` (SQLite).

## How rendering works

Each queued clip is cropped to the largest 9:16 window, which is centred by default, and then scaled to 1080x1920. The source frame rate is kept, the video is encoded as H.264, and the audio as AAC. If a clip runs longer than `max_duration`, the end is kept, since that's usually where the highlight is. One clip renders per loop, so new clips keep being picked up between renders. If the container stops mid-render, the clip goes back in the queue rather than being marked failed.

## Settings

| Setting (`settings.json` key) | Env var | Default | Meaning |
|---|---|---|---|
| `watch_dir` | `WATCH_DIR` | `/clips` | Folder your clips are exported to |
| `poll_interval` | `POLL_INTERVAL` | `15` | Seconds between scans |
| `settle_seconds` | `SETTLE_SECONDS` | `30` | How long a file must be unchanged before it counts as fully copied |
| `backfill_existing` | `BACKFILL_EXISTING` | `false` | Only read on the very first start. Off: clips already in the folder are listed as **skipped** and only new exports get posted. Click Retry on one to send it to review |
| `extensions` | `CLIP_EXTENSIONS` | `.mp4,.mkv,.mov` | Which files count as clips |
| `log_level` | `LOG_LEVEL` | `INFO` | `DEBUG` shows skipped files |
| `crop_position` | `CROP_POSITION` | `0.5` | Where the 9:16 window sits: `0` left, `0.5` centre, `1` right |
| `max_duration` | `MAX_DURATION` | `60` | Longest Short in seconds (YouTube allows 180); `0` never trims |
| `trim` | `TRIM` | `end` | Which part of a long clip to keep: `end` or `start` |
| `render_ahead` | `RENDER_AHEAD` | `3` | Keep at most this many rendered Shorts waiting for upload, so a big backlog doesn't hog CPU and disk. `0` = render everything immediately |
| `crf` | `CRF` | `20` | x264 quality, lower is better |
| `x264_preset` | `X264_PRESET` | `medium` | x264 speed/size trade-off |
| `output_width` / `output_height` | `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` | `1080` / `1920` | Output size |
| `upload_enabled` | `UPLOAD_ENABLED` | `true` | Turn uploading of approved clips off without stopping renders |
| `privacy` | `PRIVACY` | `private` | `private`, `unlisted` or `public` |
| `post_interval_minutes` | `POST_INTERVAL_MINUTES` | `120` | Minimum minutes between uploads, so posts are spread out. `0` = no gap |
| `post_window` | `POST_WINDOW` | empty | Only post during this local time window, e.g. `16:00-23:00` (can wrap past midnight). Uses the container's `TZ` |
| `upload_limit_mbps` | `UPLOAD_LIMIT_MBPS` | `0` | Upload speed limit in Mbps. `0` = unlimited |
| `daily_upload_limit` | `DAILY_UPLOAD_LIMIT` | `10` | Uploads per rolling 24 h (the API allows 100); `0` = no cap |
| `title_template` | `TITLE_TEMPLATE` | `{title}` | Wraps the generated title, e.g. `{title} #shorts` |
| `description_template` | `DESCRIPTION_TEMPLATE` | `{title}\n\n#cs2 #counterstrike #shorts` | `{title}` and `{filename}` available |
| `upload_retries` | `UPLOAD_RETRIES` | `3` | Retries for temporary upload errors, spaced 5 min / 30 min / 2 h / 6 h. Quota errors wait 6 h without using a retry |
| `keep_rendered` | `KEEP_RENDERED` | `false` | Keep the rendered Short after upload (off saves disk) |
| `tags` | `TAGS` | `cs2,counter-strike,...` | Comma-separated |
| `anthropic_api_key` | `ANTHROPIC_API_KEY` | empty | Optional. With a key, Claude writes the titles |
| `title_model` | `TITLE_MODEL` | `claude-opus-5-5` | Model used for titles |

Env vars set the defaults. `DATA_DIR/settings.json` overrides them and is re-read on every scan, so changes apply without a restart. The web UI in step 4 will edit this file. `DATA_DIR` itself (default `/data`) is only set by env var.

## Web UI

The UI runs on port `8765` (set with `WEB_PORT`). Open `http://<unraid-ip>:8765`. It has no login, so keep it on your LAN and don't port-forward it.

- **Clips**: everything the bot has seen, with status filters. You can edit a title before it uploads, preview the rendered Short, and approve, retry, re-render, or skip a clip.
- **Review** (built for phones): a full-screen feed of rendered clips waiting for approval. Swipe right to post (you get a box to check or edit the title first), swipe left to skip, swipe up for the next clip. Tap pauses, and Undo appears for a few seconds after each swipe. At least 10 clips are kept rendered and ready to review.

**Nothing uploads without your approval.** There is no setting to turn this off (the old `require_approval` setting is ignored). A clip can only upload after you swipe it right (and press Okay on the title) or press Approve on the Clips page, and not until 2 minutes after that, so Undo always wins. Skipping or re-rendering a clip withdraws its approval, so a skipped clip you Retry goes back to review. Approvals, skips and undos are written to the log, and each upload's log line says when it was approved. To check past uploads, run `docker exec clip-bot clipbot audit`: it lists every uploaded clip and whether it was approved.
- **Settings**: every setting in the table below. Changes are saved to `settings.json` and apply on the next scan. Once you save from the UI, those values win over the container's env vars.
- **YouTube**: upload your OAuth client JSON and sign in, with step-by-step instructions.

Env-only settings, which need a container restart: `DATA_DIR`, `WEB_PORT` (default 8765), `WEB_HOST` (default 0.0.0.0), and `WEB_ENABLED` (default true).

## Titles

Titles come from the clip's filename. The built-in rules split shorthand like `mirage1v3clutchmp9` using a CS2 vocabulary of maps, callouts, and weapons, plus `3k`/`4k`, `1vX`, and English word frequencies, which gives `Mirage 1v3 Clutch MP9`. If `anthropic_api_key` is set, Claude writes a more natural title from the same filename. If the API call fails, the bot falls back to the rules.

## YouTube setup (one time)

1. In [Google Cloud Console](https://console.cloud.google.com/), create a new project just for this bot.
2. Enable **YouTube Data API v3** for it.
3. Open **Google Auth Platform**. Under **Branding**, set an app name and your email. Under **Audience**, choose External, add your channel's Google account as a test user, then click **Publish app**. Apps left in Testing lose their sign-in every 7 days. You don't need verification, because you're the only user, so you can click through the “unverified app” warning.
4. Under **Clients**, choose **Create client**, set the type to **Desktop app**, and download the JSON.
5. In the web UI's **YouTube** tab, upload the JSON, click **Start sign-in**, and follow the steps there. You can also do this from a terminal with `docker exec -it clip-bot clipbot auth`.

## Install on Unraid

The image is built by GitHub Actions on every push to `main` and published as `ghcr.io/zpdc10/clip-bot:latest`.

1. **Let Unraid pull the image.** The repo is private, so the image is too. Either make the package public under GitHub → your profile → Packages → clip-bot → Package settings → Change visibility (the image holds only this code, no secrets), or run `docker login ghcr.io` on the Unraid terminal with a GitHub token that has `read:packages`.
2. **Add the template.** On the Unraid terminal, run:
   ```sh
   wget -O /boot/config/plugins/dockerMan/templates-user/my-clip-bot.xml \
     https://raw.githubusercontent.com/zpdc10/clip-bot/main/unraid/clip-bot.xml
   ```
   This only works if the repo is public. Otherwise, copy `unraid/clip-bot.xml` there yourself.
3. In **Docker → Add Container**, pick **clip-bot** from the template list. Set **Clips folder** to the share Moments exports to, then click Apply.
4. Open `http://<unraid-ip>:8765` and connect YouTube in the **YouTube** tab.

Updates: when Unraid shows "update ready", click it. Settings, the queue, and the YouTube sign-in are stored in the app data folder and survive updates.

The container runs as `99:100` (nobody:users) by default, via `PUID`/`PGID`, so the files in appdata stay editable from the share. It has a health check on `/healthz`.

### Without the template

```sh
docker run -d --name clip-bot --restart unless-stopped \
  -v /mnt/user/clips:/clips:ro \
  -v /mnt/user/appdata/clip-bot:/data \
  -p 8765:8765 \
  ghcr.io/zpdc10/clip-bot:latest
```

## Development

```sh
pip install -e '.[dev]'
pytest
```
