# clip-bot

Watches a folder of exported gaming clips, converts them to vertical (9:16) Shorts, and uploads them to YouTube. Runs as a Docker container on Unraid.

## Roadmap

1. **Folder watcher** (done): picks up new clips once they've finished copying and never queues the same clip twice.
2. **Vertical render** (done): ffmpeg center crop to 1080x1920, long clips trimmed to their last 60 s.
3. **YouTube upload**: Data API upload, private first, titles from a template.
4. **Web UI**: LAN-only page for settings, queue status, retry and approve.
5. **Unraid packaging**: container template, logs, retries, daily upload cap.

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
| `extensions` | `CLIP_EXTENSIONS` | `.mp4,.mkv,.mov` | Which files count as clips |
| `log_level` | `LOG_LEVEL` | `INFO` | `DEBUG` shows skipped files |
| `crop_position` | `CROP_POSITION` | `0.5` | Where the 9:16 window sits: `0` left, `0.5` centre, `1` right |
| `max_duration` | `MAX_DURATION` | `60` | Longest Short in seconds (YouTube allows 180); `0` never trims |
| `trim` | `TRIM` | `end` | Which part of a long clip to keep: `end` or `start` |
| `crf` | `CRF` | `20` | x264 quality, lower is better |
| `x264_preset` | `X264_PRESET` | `medium` | x264 speed/size trade-off |
| `output_width` / `output_height` | `OUTPUT_WIDTH` / `OUTPUT_HEIGHT` | `1080` / `1920` | Output size |

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

Replace `/mnt/user/clips` with your export share. The share can be mounted read-only, because the bot never modifies your originals. Rendered Shorts are written to `/data/rendered/NNNNNN.mp4`. Uploading comes in step 3.

## Development

```sh
pip install -e '.[dev]'
pytest
```
