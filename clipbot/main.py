"""Container entrypoint: scan the clip folder on a loop."""

from __future__ import annotations

import logging
import os
import signal
import threading

from . import config
from .render import process_queued
from .state import State
from .uploader import process_rendered
from . import youtube
from .watcher import Watcher

log = logging.getLogger("clipbot")


def auth() -> None:
    """`clipbot auth`: sign in to YouTube from a terminal (docker exec -it clip-bot clipbot auth)."""
    data = config.data_dir()
    print("1. Open this URL, choose your channel's Google account and approve:\n")
    print(youtube.start_auth(data))
    print("\n2. Your browser will then fail to load a localhost page. That's expected.")
    pasted = input("   Copy the full URL from the address bar and paste it here: ")
    youtube.finish_auth(data, pasted)
    print("Signed in. Uploads will start on the next cycle.")


def main() -> None:
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "auth":
        auth()
        return
    data = config.data_dir()
    cfg = config.load(data)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    state = State(data / "clipbot.db")
    restarted = state.requeue_interrupted()
    if restarted:
        log.info("re-queued %d clip(s) interrupted by the last shutdown", restarted)

    if os.environ.get("WEB_ENABLED", "true").lower() not in ("0", "false", "no", "off"):
        from .web import serve

        host = os.environ.get("WEB_HOST", "0.0.0.0")
        port = int(os.environ.get("WEB_PORT", "8765"))
        threading.Thread(target=serve, args=(host, port, data), daemon=True, name="web").start()

    watcher = Watcher(cfg.watch_path, state, cfg.extension_set, cfg.settle_seconds)

    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())

    log.info(
        "watching %s every %ds (files must be unchanged for %ds); data in %s",
        cfg.watch_dir, cfg.poll_interval, cfg.settle_seconds, data,
    )
    while not stop.is_set():
        try:
            new = config.load(data)
            if new != cfg:
                log.info("settings changed, applying")
                logging.getLogger().setLevel(new.log_level)
                watcher.reconfigure(new.watch_path, new.extension_set, new.settle_seconds)
                cfg = new
            watcher.scan()
            # One render and one upload per cycle so new arrivals keep being picked up.
            busy = process_queued(state, cfg, data / "rendered", stopping=stop.is_set)
            busy += process_rendered(state, cfg, data)
            if busy:
                continue  # more may be waiting; skip the sleep
        except Exception:
            log.exception("scan failed; will retry")
        stop.wait(cfg.poll_interval)

    state.close()
    log.info("stopped")


if __name__ == "__main__":
    main()
