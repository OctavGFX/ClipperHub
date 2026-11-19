"""Poll selected YouTube upload tabs and persist the pending import queue."""
from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit

from . import procs

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
CHECK_INTERVAL = 300
CHANNEL_PATH = re.compile(r"/(?:@[\w.-]+|channel/UC[A-Za-z0-9_-]{22}|(?:c|user)/[\w.-]+)(?:/videos)?/?", re.ASCII)


def channel_url(value: str) -> str:
    value = value.strip()
    if value.startswith("@"):
        value = "https://www.youtube.com/" + value
    u = urlsplit(value)
    if (u.scheme != "https" or u.netloc.lower() not in ("youtube.com", "www.youtube.com")
            or not CHANNEL_PATH.fullmatch(u.path) or u.query or u.fragment):
        raise ValueError(f"Expected a YouTube channel URL or @handle: {value}")
    path = u.path.rstrip("/")
    return "https://www.youtube.com" + (path if path.endswith("/videos") else path + "/videos")


def list_uploads(url: str) -> list[dict]:
    result = procs.run([*procs.guarded_ytdlp(), "--ignore-config", "--flat-playlist", "--playlist-end", "25",
                        "--socket-timeout", "12", "--no-warnings", "--dump-single-json",
                        channel_url(url)], timeout=50)
    if result.returncode:
        raise ValueError("YouTube upload listing failed: " + result.stderr.decode("utf-8", "replace").strip()[-320:])
    entries = json.loads(result.stdout)["entries"]
    if not isinstance(entries, list):
        raise ValueError("YouTube returned no upload listing")
    return [{"id": e["id"], "title": str(e.get("title") or e["id"])[:160], "channel": url}
            for e in entries if isinstance(e, dict) and VIDEO_ID.fullmatch(str(e.get("id") or ""))
            and e.get("live_status") not in ("is_live", "is_upcoming", "post_live", "was_live")]


def ingest(state: dict, url: str, uploads: list[dict]) -> None:
    seen = state["seen"]
    prior = seen.get(url)
    if prior is not None:
        known = set(prior)
        pending = {item["id"] for item in state["pending"]}
        for item in reversed(uploads):
            if item["id"] not in known and item["id"] not in pending:
                state["pending"].append(item)
                pending.add(item["id"])
    seen[url] = list(dict.fromkeys([*(item["id"] for item in uploads), *(prior or [])]))[:250]


class VideoWatch:
    def __init__(self):
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.wake = threading.Event()
        self.worker: threading.Thread | None = None
        self.channels: dict = {}
        self.pending = 0
        self.current = ""
        self.error = ""

    def status(self) -> dict:
        with self.lock:
            return {"running": bool(self.worker and self.worker.is_alive()), "stopping": self.stop.is_set()
                    and bool(self.worker and self.worker.is_alive()), "channels": dict(self.channels),
                    "pending": self.pending, "current": self.current, "error": self.error}

    def run(self, path: Path, channels: list[str], interval: int, start_import, log) -> None:
        active = None
        try:
            state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"seen": {}, "pending": []}

            def save():
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
                tmp.replace(path)

            def finish(item, replay):
                if replay.step != "done" and replay.stop_event.is_set():
                    return
                state["pending"].remove(item)
                save()
                rendered = sum("file" in row for row in replay.results)
                errors = sum("error" in row for row in replay.results)
                with self.lock:
                    self.channels[item["channel"]] = (
                        f"{item['title']}: {rendered} clips" + (f", {errors} failed" if errors else "")
                        if replay.step == "done" else f"Import failed: {item['title']} (see Progress or Log)")

            due = 0.0
            while not self.stop.is_set():
                if self.wake.is_set():
                    self.wake.clear()
                    due = 0.0
                if time.monotonic() >= due:
                    for url in channels:
                        if self.stop.is_set():
                            break
                        try:
                            uploads = list_uploads(url)
                        except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as e:
                            with self.lock:
                                self.channels[url] = str(e)
                            log(f"video watcher: {e}")
                            continue
                        ingest(state, url, uploads)
                        save()
                        with self.lock:
                            self.channels[url] = f"Watching · {len(uploads)} recent uploads checked"
                    due = time.monotonic() + interval
                if active and not active[2].is_alive():
                    item, replay, _ = active
                    if replay.step != "done" and replay.stop_event.is_set():
                        self.stop.set()
                    finish(item, replay)
                    active = None
                with self.lock:
                    self.pending = sum(item["channel"] in channels for item in state["pending"])
                    self.current = active[0]["title"] if active else ""
                if not active and not self.stop.is_set() and self.pending:
                    item = next(item for item in state["pending"] if item["channel"] in channels)
                    job = start_import(item["id"])
                    if job:
                        active = (item, *job)
                    elif self.stop.is_set():
                        break
                self.wake.wait(2)
            if active:
                item, replay, worker = active
                if worker.is_alive():
                    replay.cancel()
                    worker.join()
                finish(item, replay)
        except (OSError, ValueError, KeyError) as e:
            with self.lock:
                self.error = str(e)
            log(f"video watcher stopped: {e}")
        finally:
            if active and active[2].is_alive():
                active[1].cancel()
            with self.lock:
                self.current = ""
