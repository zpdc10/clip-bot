"""Container entrypoint: scan the clip folder on a loop."""

from __future__ import annotations

import logging
import signal
import threading

from . import config
from .render import process_queued
from .state import State
from .watcher import Watcher

log = logging.getLogger("clipbot")


def main() -> None:
    data = config.data_dir()
    cfg = config.load(data)
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    state = State(data / "clipbot.db")
    restarted = state.requeue_interrupted()
    if restarted:
        log.info("re-queued %d clip(s) interrupted by the last shutdown", restarted)

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
            # Render one clip per cycle so new arrivals keep being picked up between renders.
            if process_queued(state, cfg, data / "rendered", stopping=stop.is_set):
                continue  # more may be waiting; skip the sleep
            # Step 3 hooks in here: upload rendered clips.
        except Exception:
            log.exception("scan failed; will retry")
        stop.wait(cfg.poll_interval)

    state.close()
    log.info("stopped")


if __name__ == "__main__":
    main()
