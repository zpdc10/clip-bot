import json
import os

from clipbot import config
from clipbot.state import State
from clipbot.watcher import Watcher, fingerprint


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def make(tmp_path, settle=30):
    clips = tmp_path / "clips"
    clips.mkdir()
    state = State(tmp_path / "data" / "db.sqlite")
    clock = Clock()
    w = Watcher(clips, state, frozenset({".mp4", ".mkv"}), settle, clock=clock)
    return clips, state, clock, w


def write(path, data, mtime):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    os.utime(path, (mtime, mtime))


def test_queues_after_two_stable_scans_and_settle(tmp_path):
    clips, state, clock, w = make(tmp_path)
    f = clips / "Game" / "clip1.mp4"
    write(f, b"x" * 5000, clock.t - 100)

    assert w.scan() == []  # first sighting
    ids = w.scan()  # unchanged and old enough
    assert len(ids) == 1
    clip = state.get(ids[0])
    assert clip.rel_path == os.path.join("Game", "clip1.mp4")
    assert clip.status == "queued"
    assert w.scan() == []  # never twice


def test_waits_while_file_is_growing(tmp_path):
    clips, state, clock, w = make(tmp_path)
    f = clips / "clip.mp4"
    write(f, b"x" * 100, clock.t - 100)
    w.scan()
    write(f, b"x" * 200, clock.t - 100)  # still copying
    assert w.scan() == []
    assert len(w.scan()) == 1


def test_waits_for_settle_time(tmp_path):
    clips, state, clock, w = make(tmp_path, settle=30)
    f = clips / "clip.mp4"
    write(f, b"x" * 100, clock.t - 5)
    w.scan()
    assert w.scan() == []
    clock.t += 30
    assert len(w.scan()) == 1


def test_ignores_other_files(tmp_path):
    clips, state, clock, w = make(tmp_path)
    for name in ["notes.txt", ".hidden.mp4", "clip.mp4.part", "empty.mp4"]:
        write(clips / name, b"" if name == "empty.mp4" else b"abc", clock.t - 100)
    w.scan()
    assert w.scan() == []


def test_duplicate_copy_is_skipped_and_survives_restart(tmp_path):
    clips, state, clock, w = make(tmp_path)
    write(clips / "a.mp4", b"same" * 1000, clock.t - 100)
    w.scan()
    assert len(w.scan()) == 1

    # A re-export of the same clip under another name, seen by a freshly restarted bot.
    write(clips / "copy of a.mp4", b"same" * 1000, clock.t - 100)
    w2 = Watcher(clips, state, frozenset({".mp4"}), 30, clock=clock)
    w2.scan()
    assert w2.scan() == []


def test_fingerprint_reads_head_and_tail(tmp_path):
    big = 3 * 1024 * 1024
    a = tmp_path / "a.mp4"
    b = tmp_path / "b.mp4"
    a.write_bytes(b"\0" * big)
    b.write_bytes(b"\0" * (big - 1) + b"\1")
    assert fingerprint(a) != fingerprint(b)


def test_missing_watch_dir_does_not_crash(tmp_path):
    state = State(tmp_path / "db.sqlite")
    w = Watcher(tmp_path / "nope", state, frozenset({".mp4"}), 30)
    assert w.scan() == []


def test_requeue_interrupted(tmp_path):
    state = State(tmp_path / "db.sqlite")
    c = state.enqueue("fp", "a.mp4", 1)
    state.set_status(c.id, "processing")
    assert state.requeue_interrupted() == 1
    assert state.get(c.id).status == "queued"


def test_settings_file_overrides_env(tmp_path, monkeypatch):
    monkeypatch.setenv("WATCH_DIR", "/from-env")
    monkeypatch.setenv("POLL_INTERVAL", "60")
    (tmp_path / "settings.json").write_text(json.dumps({"poll_interval": 5, "extensions": "mp4, MKV"}))
    cfg = config.load(tmp_path)
    assert cfg.watch_dir == "/from-env"
    assert cfg.poll_interval == 5
    assert cfg.extension_set == {".mp4", ".mkv"}


def test_save_roundtrip_and_bad_file(tmp_path, monkeypatch):
    for v in config.ENV_NAMES.values():
        monkeypatch.delenv(v, raising=False)
    cfg = config.Config(watch_dir="/mnt/clips", settle_seconds=10)
    config.save(cfg, tmp_path)
    assert config.load(tmp_path) == cfg

    (tmp_path / "settings.json").write_text("{not json")
    assert config.load(tmp_path) == config.Config()


def test_bool_and_privacy_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("UPLOAD_ENABLED", "no")
    monkeypatch.setenv("PRIVACY", "everyone")
    cfg = config.load(tmp_path)
    assert cfg.upload_enabled is False
    assert cfg.privacy == "private"
