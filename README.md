# clip-bot

Watches a folder of exported gaming clips, converts them to vertical (9:16) Shorts, and uploads them to YouTube. Runs as a Docker container on Unraid.

## Roadmap

1. **Folder watcher** (done): picks up new clips once they've finished copying and never queues the same clip twice.
2. **Vertical render**: ffmpeg center crop to 1080x1920, trimmed to under 60 s.
3. **YouTube upload**: Data API upload, private first, titles from a template.
4. **Web UI**: LAN-only page for settings, queue status, retry and approve.
5. **Unraid packaging**: container template, logs, retries, daily upload cap.

## How the watcher works

The watcher scans `WATCH_DIR` (including subfolders) every `POLL_INTERVAL` seconds. A clip is queued only after its size and modified time look the same on two scans in a row and it hasn't changed for `SETTLE_SECONDS`. That way, clips that are still copying over the network are left alone. Each clip is fingerprinted by its size plus the first and last MB, so a re-export or a renamed copy of the same clip is skipped.

Polling is used instead of filesystem events because SMB copies into Unraid shares don't reliably trigger inotify inside a container.

Queue state lives in `DATA_DIR/clipbot.db` (SQLite).

## Settings

| Setting (`settings.json` key) | Env var | Default | Meaning |
|---|---|---|---|
| `watch_dir` | `WATCH_DIR` | `/clips` | Folder your clips are exported to |
| `poll_interval` | `POLL_INTERVAL` | `15` | Seconds between scans |
| `settle_seconds` | `SETTLE_SECONDS` | `30` | How long a file must be unchanged before it counts as fully copied |
| `extensions` | `CLIP_EXTENSIONS` | `.mp4,.mkv,.mov` | Which files count as clips |
| `log_level` | `LOG_LEVEL` | `INFO` | `DEBUG` shows skipped files |

Env vars set the defaults. `DATA_DIR/settings.json` overrides them and is re-read on every scan, so changes apply without a restart. The web UI in step 4 will edit this file. `DATA_DIR` itself (default `/data`) is only set by env var.

## Running it now

```sh
docker build -t clip-bot .
docker run -d --name clip-bot \
  -v /mnt/user/clips:/clips:ro \
  -v /mnt/user/appdata/clip-bot:/data \
  clip-bot
docker logs -f clip-bot
```

Replace `/mnt/user/clips` with your export share. The share can be mounted read-only, because the bot never modifies your originals. At this stage, the bot only logs `queued clip #N` lines.

## Development

```sh
pip install -e '.[dev]'
pytest
```
