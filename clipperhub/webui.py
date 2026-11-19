"""The local web UI: ``python -m livewatch ui`` opens http://127.0.0.1:8765.

One process: this HTTP server (standard library only) and, when started from
the page, the watcher and replay runs in background threads. The page polls
JSON endpoints once a second.

Local-only by design:
- binds 127.0.0.1, and refuses requests whose Host is not localhost/127.0.0.1
  on its port (DNS-rebinding);
- every request that changes something must carry ``X-Livewatch: 1``. A web
  page on another site cannot add that header without a CORS preflight,
  which this server never approves, so it cannot drive the UI;
- clip and music file names from the URL are checked against a strict
  pattern and must resolve inside their folders.
"""
from __future__ import annotations

from collections import deque
import json
import mimetypes
from html import escape as html_escape
import os
import re
import secrets
import shutil
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Dict, Optional
from urllib.parse import parse_qs, unquote, urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

from . import __version__, config, delivery_queue, music, poster, procs, publish, releases, render, tomlwrite, transcribe, viral
from .replay import Replay, ReplayError, video_id
from .settings_schema import SECRET_KEYS, schema
from .service import Logger, Service, deliver, write_meta
from .video_watch import CHECK_INTERVAL, VideoWatch

WEB_DIR = Path(__file__).resolve().parent / "web"
CLIP_ID = re.compile(r"^[A-Za-z0-9_.@-]{1,120}/[A-Za-z0-9_-]{1,40}$")
MAX_JSON = 1_000_000
MAX_UPLOAD = music.MAX_BYTES + 1024


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def _mask(v: str) -> str:
    return ("•" * 6 + v[-4:]) if len(v) > 8 else "•" * len(v)


class Controller:
    """Everything the page can ask for; no HTTP in here (unit-testable)."""

    def __init__(self, config_path: Path):
        self.config_path = Path(config_path).resolve()
        self.release_status = {"status": "pending", "message": "Checking GitHub for updates."}
        self.update_lock = threading.Lock()
        self.update_enabled = False
        self.updating = False
        self.mutations = 0
        self.pending_update = None
        self.base = self.config_path.parent
        self.env_path = self.base / ".env"
        self._lock = threading.Lock()
        self._delivery_lock = threading.RLock()
        self._catalog_lock = threading.Lock()
        self._catalog = {}
        self._preview_lock = threading.Lock()
        self.svc: Optional[Service] = None
        self.replay = None
        self.replay_thread: Optional[threading.Thread] = None
        self.replay_error = ""
        self._replay_id = ""
        self._replay_active = False
        self._video_imports: deque[Replay] = deque()
        self._imports_closing = False
        self._telegram_imports: Dict[str, tuple] = {}
        self.video_watch = VideoWatch()
        self._loggers: Dict[str, Logger] = {}
        self._posting: Dict[str, threading.Thread] = {}
        self._oauth: Dict[str, tuple] = {}
        config.load_dotenv(self.env_path)

    # ------------------------------------------------------------- config
    def raw_config(self) -> dict:
        if not self.config_path.is_file():
            return {}
        return tomllib.loads(self.config_path.read_text(encoding="utf-8"))

    def settings(self) -> config.Settings:
        return config.from_dict(self.raw_config(), base_dir=self.base)

    def config_version(self) -> str:
        """Fingerprint of the settings file as it is now (path + content)."""
        import hashlib
        body = self.config_path.read_bytes() if self.config_path.is_file() else b""
        return hashlib.sha1(str(self.config_path).encode() + b"\0" + body).hexdigest()[:16]

    BACKUPS_KEPT = 20

    @property
    def backup_dir(self) -> Path:
        return self.base / "livewatch_backups"

    def backup_config(self) -> Optional[Path]:
        """Copy the settings file aside before it is overwritten; keep the last 20."""
        if not self.config_path.is_file():
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        dest = self.backup_dir / f"{self.config_path.stem}-{stamp}.toml"
        shutil.copy2(self.config_path, dest)
        old = sorted(self.backup_dir.glob(f"{self.config_path.stem}-*.toml"))
        for p in old[:-self.BACKUPS_KEPT]:
            p.unlink(missing_ok=True)
        return dest

    def backups(self) -> list:
        if not self.backup_dir.is_dir():
            return []
        out = []
        for p in sorted(self.backup_dir.glob(f"{self.config_path.stem}-*.toml"), reverse=True):
            try:
                data = tomllib.loads(p.read_text(encoding="utf-8"))
                chans = [f"{c.get('platform')}:{c.get('name')}" for c in data.get("channel", [])]
            except (OSError, ValueError):
                chans = []
            out.append({"name": p.name, "saved": p.stat().st_mtime, "channels": chans})
        return out

    def restore_backup(self, name: str) -> dict:
        p = (self.backup_dir / str(name or "")).resolve()
        if p.parent != self.backup_dir.resolve() or not p.is_file() or p.suffix != ".toml":
            raise ApiError(404, "no such backup")
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
        return self.save_config(raw)  # validated, and the current file is backed up first

    def save_config(self, raw: dict, version: Optional[str] = None) -> dict:
        if not isinstance(raw, dict):
            raise ApiError(400, "settings must be an object")
        if version is not None and version != self.config_version():
            # a page loaded from another (or an older) settings file must not overwrite this one
            raise ApiError(409, "the settings file changed since this page loaded it; "
                                "press Discard changes to reload, then edit again")
        try:
            config.from_dict(raw, base_dir=self.base)  # the watcher's own validation
            text = tomlwrite.dumps(raw)
            tomllib.loads(text)
        except (config.ConfigError, TypeError, ValueError) as e:
            raise ApiError(400, str(e))
        self.backup_config()
        tmp = self.config_path.with_suffix(".toml.tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(self.config_path)
        running = bool(self.svc and not self.svc.finished.is_set()) or self.video_watch.status()["running"]
        return {"ok": True, "restart_needed": running, "version": self.config_version()}

    def logger(self, s: Optional[config.Settings] = None) -> Logger:
        s = s or self._safe_settings()
        key = str(s.data_dir)
        if key not in self._loggers:
            s.data_dir.mkdir(parents=True, exist_ok=True)
            self._loggers[key] = Logger(s.data_dir / "livewatch.log")
        return self._loggers[key]

    def _safe_settings(self) -> config.Settings:
        try:
            return self.settings()
        except config.ConfigError:
            return config.from_dict({}, base_dir=self.base)

    # ------------------------------------------------------------ secrets
    def secrets(self) -> dict:
        return {k: {"set": bool(os.environ.get(k)), "hint": _mask(os.environ.get(k, ""))}
                for k in SECRET_KEYS}

    def save_secrets(self, values: dict) -> dict:
        if not isinstance(values, dict):
            raise ApiError(400, "expected an object")
        for k, v in values.items():
            if k not in SECRET_KEYS or not isinstance(v, str):
                raise ApiError(400, f"unknown key {k}")
            if len(v) > 500 or any(c in v for c in "\r\n\0"):
                raise ApiError(400, f"{k} does not look like an API key")
        lines = self.env_path.read_text(encoding="utf-8").splitlines() if self.env_path.exists() else []
        for k, v in values.items():
            v = v.strip()
            lines = [ln for ln in lines if not re.match(rf"^\s*(export\s+)?{k}\s*=", ln)]
            if v:
                lines.append(f"{k}={v}")
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        tmp = self.env_path.with_suffix(".tmp")
        tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp.replace(self.env_path)
        return self.secrets()

    # -------------------------------------------------------------- watcher
    def start(self) -> dict:
        with self._lock:
            if self.svc and not self.svc.finished.is_set():
                raise ApiError(409, "already running")
            try:
                s = self.settings()
            except config.ConfigError as e:
                raise ApiError(400, f"fix the settings first: {e}")
            if not any(c.live for c in s.channels):
                raise ApiError(400, "add at least one channel in Settings first")
            self.svc = Service(s, log=self.logger(s))
            threading.Thread(target=self.svc.run, daemon=True, name="watcher").start()
        return {"ok": True}

    def stop(self) -> dict:
        if self.svc:
            self.svc.stop()
        return {"ok": True}

    def clear_live_queue(self) -> dict:
        with self._lock:
            svc = self.svc
            if not svc or not svc.stop_event.is_set() or svc.finished.is_set():
                raise ApiError(409, "Stop the streaming watcher before clearing its queue")
            return {"ok": True, "discarded": svc.clear_queue()}

    def check_now(self) -> dict:
        if self.svc:
            self.svc.check_now()
        return {"ok": True}

    def status(self) -> dict:
        cfg_error = ""
        try:
            s = self.settings()
        except config.ConfigError as e:
            cfg_error = str(e)
            s = config.from_dict({}, base_dir=self.base)
        if self.svc:
            st = self.svc.status()
        else:
            st = {"running": False, "stopping": False, "started_at": None, "queue": 0,
                  "current_step": "", "counts": {"peaks": 0, "clips": 0, "posted": 0, "failed": 0},
                  "threshold": s.threshold, "encoder": render.pick_encoder(s.encoder),
                  "channels": [{"key": c.key, "platform": c.platform, "name": c.name,
                                "display": c.display, "url": c.url, "state": "stopped",
                                "live": False, "recording": False, "category": "", "title": "",
                                "game_filter": c.game_filter} for c in s.channels if c.live]}
        with self._lock:
            replay = self.replay_status()
            video_imports = [{"url": job.url, "interval": job.interval} for job in self._video_imports]
        st.update({
            "version": __version__, "release": {**self.release_status,
                                                "installable": self.update_enabled and "patch" in self.release_status},
            "config_error": cfg_error,
            "config_exists": self.config_path.is_file(), "config_path": str(self.config_path),
            "ai": ("local: " + (s.local_vision_model or s.local_model)
                   if s.local_vision_model or s.local_model else "local model not set")
            if s.ai_provider == "local" else
            ("gemini" if s.ai_provider == "gemini" and s.gemini_api_key else "offline templates"),
            "ai_provider": s.ai_provider,
            "posting": (s.delivery_mode in ("top_daily", "telegram_review") and not self.posting_issues(s))
            or bool(s.delivery_mode == "legacy" and s.post_enabled and not self.posting_issues(s)),
            "posting_issues": self.posting_issues(s),
            "delivery_mode": s.delivery_mode, "post_enabled": s.post_enabled,
            "whisper": transcribe.available() or "missing",
            "music_mode": s.music_mode, "music_tracks": len(music.Library(s.music_dir).tracks())
            if s.music_dir else 0,
            "disk_free_gb": round(shutil.disk_usage(self.base).free / 1e9, 1),
            "replay": replay, "video_import_queue": video_imports,
            "video_watch": {**self.video_watch.status(), "configured": s.video_channels,
                            "interval": CHECK_INTERVAL},
        })
        return st

    def log_since(self, seq: int) -> dict:
        lg = self.logger()
        lines = lg.since(seq)
        return {"seq": lg.seq, "boot": lg.boot,
                "lines": [{"seq": n, "text": t} for n, t in lines[-500:]]}

    # --------------------------------------------------------------- replay
    def replay_status(self) -> Optional[dict]:
        r = self.replay
        if not r:
            return None
        return {"id": self._replay_id, "running": self._replay_active, "url": r.url, "step": r.step,
                "progress": round(r.progress, 3),
                "platform": r.platform, "signal": r.signal, "video_import": r.video_import,
                "title": r.meta.get("title", ""), "channel": r.meta.get("channel", ""),
                "results": r.results, "error": self.replay_error}

    def start_replay(self, body: dict, *, video_import: bool = False) -> dict:
        with self._lock:
            if self._imports_closing:
                raise ApiError(409, "the dashboard is shutting down")
            if video_import:
                if not isinstance(body, dict):
                    raise ApiError(400, "expected an import configuration object")
                urls = body.get("urls", [body.get("url")])
                if (not isinstance(urls, list) or not urls
                        or any(not isinstance(url, str) or not url.strip() for url in urls)):
                    raise ApiError(400, "enter at least one YouTube video link")
                jobs = []
                for position, url in enumerate(urls, 1):
                    try:
                        jobs.append(self._prepare_replay({**body, "url": url.strip()}, True))
                    except ApiError as e:
                        raise ApiError(e.status, f"Link {position}: {e.message}") from e
                self._video_imports.extend(jobs)
                if not self._replay_active:
                    self._launch_replay(self._video_imports.popleft())
                return {"ok": True, "accepted": len(jobs)}
            return self._start_replay(body, video_import)

    def _start_replay(self, body: dict, video_import: bool, *, watched: bool = False) -> dict:
        if self._imports_closing or self._replay_active:
            raise ApiError(409, "a replay is already running")
        self._launch_replay(self._prepare_replay(body, video_import, watched=watched))
        return {"ok": True}

    def _prepare_replay(self, body: dict, video_import: bool, *, watched: bool = False) -> Replay:
        try:
            if not isinstance(body, dict):
                raise ValueError("expected an import configuration object")
            s = self.settings()
            post = body.get("post")
            mode = body.get("mode", "auto")
            if mode not in ("auto", "manual"):
                raise ValueError("mode must be auto or manual")
            interval = (float(body.get("from")), float(body.get("to"))) if mode == "manual" else None
            replay_options = {} if video_import else {
                "minutes": float(body.get("minutes", 30)), "start_minute": float(body.get("start", 0)),
                "max_clips": int(body.get("max_clips", 5)),
            }
            return Replay(s, str(body.get("url", "")), **replay_options,
                          post=bool(post) if post is not None else None, log=self.logger(s),
                          video_import=video_import, interval=interval, upload_only=watched)
        except (ReplayError, config.ConfigError, ValueError, TypeError) as e:
            raise ApiError(400, str(e))

    def _launch_replay(self, rep: Replay) -> None:
        self.replay, self.replay_error = rep, ""
        self._replay_id = secrets.token_hex(8)
        self._replay_active = True

        def run():
            try:
                rep.run()
            except ReplayError as e:
                self.replay_error = str(e)
                rep.log(f"replay: {e}")
            except Exception as e:  # noqa: BLE001 - shown to the user
                self.replay_error = f"{type(e).__name__}: {e}"
                rep.log(f"replay failed: {self.replay_error}")
            finally:
                with self._lock:
                    rep.step = rep.step if rep.step == "done" else "stopped"
                    if self._video_imports and not self._imports_closing:
                        self._launch_replay(self._video_imports.popleft())
                    else:
                        self._replay_active = False

        self.replay_thread = threading.Thread(target=run, daemon=True, name="replay")
        self.replay_thread.start()

    def video_watch_action(self, action: str) -> dict:
        watch = self.video_watch
        with watch.lock:
            if action == "stop":
                watch.stop.set()
                watch.wake.set()
            elif action == "check":
                if not watch.status()["running"] or watch.stop.is_set():
                    raise ApiError(409, "start the video watcher first")
                watch.wake.set()
            else:
                if watch.status()["running"]:
                    raise ApiError(409, "video watcher is already running")
                s = self.settings()
                if not s.video_channels:
                    raise ApiError(400, "enable Video for a YouTube channel in Settings > Channels")
                watch.stop.clear()
                watch.wake.clear()
                watch.error, watch.current, watch.pending = "", "", 0
                watch.channels = dict.fromkeys(s.video_channels, "Waiting for first check")

                def start_import(vid):
                    with self._lock:
                        if watch.stop.is_set() or self._imports_closing or self._replay_active:
                            return None
                        self._start_replay({"url": f"https://www.youtube.com/watch?v={vid}"}, True, watched=True)
                        return self.replay, self.replay_thread

                watch.worker = threading.Thread(target=watch.run, name="video-watcher", daemon=True,
                    args=(s.data_dir / "video_watch.json", s.video_channels, CHECK_INTERVAL,
                          start_import, self.logger(s)))
                watch.worker.start()
        return {"ok": True}

    def cancel_replay(self, expected_id: Optional[str] = None, *, closing: bool = False) -> dict:
        with self._lock:
            if closing:
                self._imports_closing = True
            if expected_id is not None and expected_id != self._replay_id:
                raise ApiError(409, "this import has already ended")
            replay = self.replay
        if replay:
            replay.cancel()
        return {"ok": True}

    # ---------------------------------------------------------------- clips
    def clips_root(self) -> Path:
        return self._safe_settings().data_dir / "clips"

    def clip_dir(self, clip_id: str) -> Path:
        if not CLIP_ID.match(clip_id or ""):
            raise ApiError(404, "no such clip")
        root = self.clips_root().resolve()
        d = (root / clip_id).resolve()
        if root not in d.parents or not (d / "meta.json").is_file():
            raise ApiError(404, "no such clip")
        return d

    def clip_preview(self, clip_id: str) -> tuple[Path, str]:
        d = self.clip_dir(clip_id)
        video, preview = d / "short.mp4", d / "preview.jpg"
        with self._preview_lock:
            if not video.is_file() or video.resolve().parent != d:
                raise ApiError(404, "not rendered")
            stat = video.stat()
            version = str(stat.st_mtime_ns)
            if (not preview.is_symlink() and preview.is_file()
                    and preview.stat().st_mtime_ns == stat.st_mtime_ns):
                return preview, version
            frames = render.extract_frames(video, [1.0], width=256, timeout=5)
            if not frames:
                frames = render.extract_frames(video, [0.0], width=256, timeout=5)
            if not frames:
                raise ApiError(422, "could not extract a preview frame")
            with tempfile.NamedTemporaryFile(dir=d, prefix=".preview-", delete=False) as handle:
                tmp = Path(handle.name)
            try:
                tmp.write_bytes(frames[0])
                if video.stat().st_mtime_ns != stat.st_mtime_ns:
                    raise ApiError(409, "video changed while preparing the preview")
                os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                tmp.replace(preview)
            finally:
                tmp.unlink(missing_ok=True)
            return preview, version

    def clips(self) -> list:
        with self._catalog_lock:
            return self._clip_catalog()

    def _clip_catalog(self) -> list:
        root = self.clips_root()
        out, seen = [], set()
        for m in root.glob("*/*/meta.json"):
            try:
                stat = m.stat()
            except OSError:
                continue
            cid = f"{m.parent.parent.name}/{m.parent.name}"
            if not CLIP_ID.fullmatch(cid):
                continue
            seen.add(m)
            version = (stat.st_mtime_ns, stat.st_size)
            cached = self._catalog.get(m)
            if cached and cached[0] == version:
                out.append({**cached[1], "posting_now": self.posting_now(cid)})
                continue
            try:
                meta = json.loads(m.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError):
                continue
            if not isinstance(meta, dict):
                continue
            try:
                video_stat = (m.parent / "short.mp4").stat()
            except OSError:
                video_stat = None
            row = {
                "id": cid, "channel": meta.get("channel"), "streamer": meta.get("streamer"),
                "moment": meta.get("moment"), "duration": meta.get("duration"),
                "score": meta.get("score"), "reason": meta.get("reason"),
                "category": meta.get("category"),
                "text": {k: v for k, v in (meta.get("text") or {}).items() if k in
                         ("title", "hook", "spoiler", "style", "description_style", "source")},
                "quality_review": meta.get("quality_review") or {},
                "source_type": meta.get("source_type") or (
                    "replay" if cid.startswith("replay_") else "local" if m.parent.parent.name == "manual" else "live"),
                "schedule": meta.get("schedule"),
                "schedule_test": meta.get("schedule_test"),
                "telegram_review": meta.get("telegram_review") or {},
                "editorial_feedback": meta.get("editorial_feedback"),
                "posted": meta.get("posted"), "music": meta.get("music"),
                "has_video": video_stat is not None,
                "preview_version": str(video_stat.st_mtime_ns) if video_stat else None,
                "created": meta.get("created") or (video_stat or stat).st_mtime,
            }
            self._catalog[m] = (version, row)
            out.append({**row, "posting_now": self.posting_now(cid)})
        self._catalog = {p: v for p, v in self._catalog.items() if p in seen}
        return sorted(out, key=lambda c: (c["created"], c["id"]), reverse=True)

    def clip_page(self, q: dict) -> dict:
        def one(key, default=""):
            return q.get(key, [default])[0]

        try:
            page = max(1, int(one("page", "1")))
            limit = max(1, min(48, int(one("limit", "12"))))
            minimum = float(one("min_quality", "0"))
            if not 0 <= minimum <= 10:
                raise ValueError
        except ValueError as e:
            raise ApiError(400, "page and limit must be integers; minimum quality must be 0-10") from e
        rows = self.clips()
        channels = sorted({c["streamer"] for c in rows if c["streamer"]})
        search, state, channel, source = (one(k) for k in ("search", "state", "channel", "source"))

        def matches(c):
            posted = (c["posted"] or {}).get("status") or "held"
            actual = ("posting" if c["posting_now"] else "scheduled" if c["schedule"]
                      else "failed" if posted == "posting" else posted)
            return ((not state or actual == state or (state == "posted" and actual == "partial"))
                    and (not channel or c["streamer"] == channel)
                    and (not source or c["source_type"] == source)
                    and (not minimum or (c["quality_review"].get("status") == "ok"
                                         and c["quality_review"].get("postworthy", 0) >= minimum))
                    and (not search or search.casefold() in " ".join(str(x or "") for x in (
                        c["streamer"], c["category"], c["text"].get("title"), c["text"].get("hook"))).casefold()))

        rows = [c for c in rows if matches(c)]
        if one("sort") == "quality":
            rows.sort(key=lambda c: c["quality_review"].get("postworthy", -1)
                      if c["quality_review"].get("status") == "ok" else -1, reverse=True)
        total = len(rows)
        pages = max(1, (total + limit - 1) // limit)
        page = min(page, pages)
        return {"clips": rows[(page - 1) * limit:page * limit], "total": total,
                "page": page, "pages": pages, "channels": channels}

    def posting_now(self, clip_id: str) -> bool:
        worker = self._posting.get(clip_id)
        return bool(worker and worker.is_alive())

    def clip_detail(self, clip_id: str) -> dict:
        d = self.clip_dir(clip_id)
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
        meta["posts"] = viral.with_tiktok(meta)
        try:
            version = str((d / "short.mp4").stat().st_mtime_ns)
        except FileNotFoundError:
            version = None
        return {**meta, "id": clip_id, "posting_now": self.posting_now(clip_id),
                "has_video": version is not None, "preview_version": version}

    def schedule_list(self) -> dict:
        entries = []
        for row in self.clips():
            posted = row["posted"] or {}
            schedule = row["schedule"] or row["schedule_test"]
            if not schedule and posted.get("status") in ("queued", "scheduled") and posted.get("at"):
                schedule = {"at": posted["at"], "platforms": []}
            if not schedule:
                continue
            try:
                at = datetime.fromisoformat(schedule["at"])
                detail = self.clip_detail(row["id"])
            except (ApiError, OSError, ValueError, KeyError, TypeError):
                continue
            platforms = schedule["platforms"] or list(detail["posts"])
            entries.append((at.timestamp(), {
                "id": row["id"], "streamer": row["streamer"], "has_video": detail["has_video"],
                "preview_version": detail["preview_version"], "at": schedule["at"],
                "posts": {p: detail["posts"][p] for p in platforms if p in detail["posts"]},
                "manual": bool(row["schedule"] or row["schedule_test"]),
                "test": not row["schedule"] and bool(row["schedule_test"]),
            }))
        entries.sort(key=lambda item: item[0])
        return {"entries": [row for _, row in entries]}

    @staticmethod
    def _schedule_time(row: dict) -> Optional[datetime]:
        posted = row["posted"] or {}
        schedule = row["schedule"]
        at = schedule.get("at") if schedule else (
            posted.get("at") if posted.get("status") in ("queued", "scheduled") else None)
        try:
            return datetime.fromisoformat(at).astimezone(timezone.utc) if at else None
        except (ValueError, TypeError):
            return None

    def update_posts(self, clip_id: str, posts: dict) -> dict:
        with self._delivery_lock:
            return self._update_posts(clip_id, posts)

    def _update_posts(self, clip_id: str, posts: dict) -> dict:
        d = self.clip_dir(clip_id)
        if self.posting_now(clip_id):
            raise ApiError(409, "this clip is already being posted")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
        if (meta.get("posted") or {}).get("status") in ("queued", "posting"):
            raise ApiError(409, "legacy or interrupted upload; check delivery status first")
        if not isinstance(posts, dict):
            raise ApiError(400, "expected an object of networks")
        clean = {}
        for p, t in posts.items():
            if p not in config.POST_PLATFORMS or not isinstance(t, dict):
                raise ApiError(400, f"unknown network {p}")
            title, desc = t.get("title", ""), t.get("description", "")
            if not isinstance(title, str) or not isinstance(desc, str):
                raise ApiError(400, "title and description must be text")
            if len(title) > 300 or len(desc) > 5000:
                raise ApiError(400, f"{p}: text too long")
            clean[p] = {"title": title, "description": desc}
        meta["posts"] = clean
        write_meta(d, meta)
        return {"ok": True}

    def schedule_clip(self, clip_id: str, body: dict) -> dict:
        if not isinstance(body, dict):
            raise ApiError(400, "expected schedule settings")
        with self._delivery_lock:
            d, meta = self._review_meta(clip_id)
            if body.get("cancel") is True:
                # An explicit cancellation also opts this candidate out of today's auto plan.
                if meta.pop("schedule", None):
                    meta["editorial_feedback"] = "reject"
                write_meta(d, meta)
                return {"ok": True}
            s = self.settings()
            nets, routes, problems = self._delivery_routes(meta, s, body.get("platforms"), "manual")
            now = datetime.now(timezone.utc)
            existing = meta.get("schedule")
            when = self._schedule_time({"schedule": existing, "posted": None})
            if when is None:
                when = self._next_bucket_time(clip_id, s, now, routes)
                if when is None:
                    return {**self._dispatch(clip_id, s, nets, "manual"), "posting_now": True}
            elif routes.get("youtube") in ("api", "upload-post"):
                youtube_ready = self._youtube_ready_at(self.clips(), timedelta(minutes=s.min_gap_minutes))
                if youtube_ready:
                    when = max(when, youtube_ready)
            meta["schedule"] = {"at": when.isoformat(), "platforms": nets,
                                "bucket": not existing or bool(existing.get("bucket"))}
            meta["editorial_feedback"] = "approve"
            write_meta(d, meta)
            return {"ok": True, "at": when.isoformat(), "warnings": problems}

    @staticmethod
    def _youtube_delivery_time(posted: dict) -> Optional[datetime]:
        youtube = (posted.get("networks") or {}).get("youtube") or {}
        if youtube.get("status") not in ("posted", "private"):
            return None
        # Older clips only have a delivery timestamp at the top level.
        at = youtube.get("at") or posted.get("at")
        try:
            return datetime.fromisoformat(at).astimezone(timezone.utc) if at else None
        except (ValueError, TypeError):
            return None

    def _youtube_ready_at(self, rows: list, gap: timedelta) -> Optional[datetime]:
        last = max((at for row in rows
                    if (at := self._youtube_delivery_time(row["posted"] or {})) is not None), default=None)
        return last + gap if last else None

    def _next_bucket_time(self, clip_id, s, now, routes) -> Optional[datetime]:
        rows = self.clips()
        gap = timedelta(minutes=s.min_gap_minutes)
        youtube_ready = (self._youtube_ready_at(rows, gap)
                         if routes.get("youtube") in ("api", "upload-post") else None)
        tail = max((at for row in rows if row["id"] != clip_id
                    and (at := self._schedule_time(row)) is not None), default=None)
        busy = any(worker.is_alive() for worker in self._posting.values())
        if tail is not None or busy:
            return max(max(now, tail or now) + gap, youtube_ready or now)
        return youtube_ready if youtube_ready and youtube_ready > now else None

    def feedback(self, clip_id: str, body: dict) -> dict:
        value = body.get("value") if isinstance(body, dict) else None
        if value not in ("approve", "reject"):
            raise ApiError(400, "feedback must be approve or reject")
        with self._delivery_lock:
            d, meta = self._review_meta(clip_id)
            if value == "reject" and meta.get("schedule"):
                raise ApiError(409, "cancel the scheduled delivery first")
            meta["editorial_feedback"] = value
            write_meta(d, meta)
        return {"ok": True}

    def _review_meta(self, clip_id: str) -> tuple:
        d = self.clip_dir(clip_id)
        if self.posting_now(clip_id):
            raise ApiError(409, "this clip is already being posted")
        if not (d / "short.mp4").is_file():
            raise ApiError(409, "this clip has no rendered video")
        meta = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
        if (meta.get("posted") or {}).get("status") in ("queued", "posting"):
            raise ApiError(409, "legacy or interrupted upload; check delivery status first")
        meta["posts"] = viral.with_tiktok(meta)
        return d, meta

    @staticmethod
    def _chosen_networks(meta: dict, platforms: Optional[list], s: config.Settings) -> list:
        if platforms is not None and (not isinstance(platforms, list)
                                      or any(p not in config.POST_PLATFORMS for p in platforms)):
            raise ApiError(400, "invalid network selection")
        nets = [p for p in meta["posts"] if p in (platforms if platforms is not None else s.post_platforms)]
        if not nets:
            raise ApiError(400, "no networks selected")
        return nets

    def post_clip(self, clip_id: str, platforms: Optional[list] = None) -> dict:
        """Post a clip now (the dashboard's Post button), the free way."""
        with self._delivery_lock:
            return self._dispatch(clip_id, self.settings(), platforms, "manual")

    def _delivery_routes(self, meta, s, platforms, mode):
        previous = meta.get("posted") or {}
        nets = self._chosen_networks(meta, platforms, s)
        nets = [p for p in nets if (previous.get("networks") or {}).get(p, {}).get("status")
                not in ("posted", "sent to phone", "private")]
        routes = publish.route(nets, s.youtube_via, s.instagram_via, "phone" if mode == "top_daily" else s.others_via)
        if mode == "telegram_review" and routes.get("youtube") not in ("api", "upload-post"):
            raise ApiError(400, "configure YouTube API or Upload-Post for automatic publishing")
        if not routes:
            raise ApiError(400, "selected networks are disabled or already delivered")
        creds = publish.credentials()
        problems = publish.missing(routes, creds, s.upload_post_user)
        if all(publish.missing({p: via}, creds, s.upload_post_user) for p, via in routes.items()):
            raise ApiError(400, "set up first: " + "; ".join(problems))
        return nets, routes, problems

    def _dispatch(self, clip_id, s, platforms, mode):
        d, meta = self._review_meta(clip_id)
        previous = meta.get("posted") or {}
        nets, routes, problems = self._delivery_routes(meta, s, platforms, mode)
        if at := self._youtube_delivery_time(previous):
            previous["networks"]["youtube"].setdefault("at", at.isoformat())
        log = self.logger(s)
        meta.pop("schedule", None)
        if mode in ("manual", "telegram_review"):
            meta["editorial_feedback"] = "approve"
        meta["posted"] = {**previous, "status": "posting", "via": mode}
        write_meta(d, meta)

        def run():
            try:
                result = deliver(d, s, log, only=nets, phone_tiktok=mode == "top_daily")
            except (OSError, ValueError, publish.PublishError) as e:
                log(f"{clip_id}: posting failed ({type(e).__name__})")
                result = {"status": "failed", "error": f"Posting failed ({type(e).__name__})"}
            with self._delivery_lock:
                result["networks"] = {**(previous.get("networks") or {}), **result.get("networks", {})}
                meta["posted"] = {**result, "via": mode}
                write_meta(d, meta)
                self._posting.pop(clip_id, None)
                if s.delivery_mode == "legacy" and result["status"] in ("posted", "partial"):
                    poster.PostLedger(s.data_dir / "post_ledger.json").record(datetime.now(timezone.utc))
                if result["status"] in ("posted", "partial"):
                    self._space_bucket(datetime.now(timezone.utc), timedelta(minutes=s.min_gap_minutes))
                log(f"{clip_id}: delivery {result['status']} ({mode})")

        th = threading.Thread(target=run, daemon=True, name=f"post-{clip_id}")
        self._posting[clip_id] = th
        th.start()
        return {"ok": True, "routes": routes, "warnings": problems}

    def _space_bucket(self, after: Optional[datetime], gap: timedelta,
                      *, youtube_ready: Optional[datetime] = None) -> None:
        waiting = []
        for row in self.clips():
            schedule = row["schedule"]
            if schedule and (at := self._schedule_time(row)) is not None:
                waiting.append((at, row["id"], schedule))
        for at, cid, schedule in sorted(waiting):
            when = max(at, after + gap) if after is not None and schedule.get("bucket") else at
            if youtube_ready and "youtube" in schedule["platforms"]:
                when = max(when, youtube_ready)
            if when != at:
                d = self.clip_dir(cid)
                meta = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
                meta["schedule"]["at"] = when.isoformat()
                write_meta(d, meta)
            after = max(after, when) if after is not None else when

    def delivery_tick(self, now: datetime) -> None:
        with self._delivery_lock:
            if any(worker.is_alive() for worker in self._posting.values()):
                return
            s = self.settings()
            rows = self.clips()
            due = sorted((at, c["id"], c["schedule"]["platforms"], c["schedule"].get("source"))
                         for c in rows if c["schedule"]
                         and (at := self._schedule_time(c)) is not None and at <= now)
            if due:
                _, cid, platforms, source = due[0]
                if "youtube" in platforms and s.youtube_via in ("api", "upload-post"):
                    gap = timedelta(minutes=s.min_gap_minutes)
                    youtube_ready = self._youtube_ready_at(rows, gap)
                    if youtube_ready and youtube_ready > now:
                        self._space_bucket(None, gap, youtube_ready=youtube_ready)
                        return
                try:
                    self._dispatch(cid, s, platforms, "telegram_review" if source == "telegram" else "manual")
                except ApiError as exc:
                    if exc.status == 404:
                        return
                    d = self.clip_dir(cid)
                    meta = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
                    meta.pop("schedule", None)
                    meta["posted"] = {**(meta.get("posted") or {}), "status": "failed", "error": exc.message}
                    write_meta(d, meta)
                    self.logger(s)(f"{cid}: delivery failed ({exc.message})")
                return
            if s.delivery_mode != "top_daily" or s.ai_provider != "local" or not (s.local_model or s.local_vision_model):
                return
            creds = publish.credentials()
            if not (creds["tg_token"] and creds["tg_chat"]):
                return
            by_id = {c["id"]: c for c in rows}
            for cid in delivery_queue.daily_batch(s, now, lambda: rows):
                row = by_id.get(cid)
                if (not row or not row["has_video"] or row["schedule"] or row["editorial_feedback"] == "reject"
                        or (row["posted"] or {}).get("status") not in (None, "held")):
                    continue
                try:
                    self._dispatch(cid, s, ["tiktok"], "top_daily")
                except ApiError as exc:
                    self.logger(s)(f"{cid}: automatic delivery skipped ({exc.message})")
                    continue
                break

    # ------------------------------------------------------- Telegram review
    @staticmethod
    def _telegram_review_credentials(s):
        if s.delivery_mode == "telegram_review":
            creds = publish.credentials()
            if creds["tg_token"] and publish.telegram_private_chat(creds["tg_chat"]):
                return creds["tg_token"], creds["tg_chat"]
        return None

    @staticmethod
    def _telegram_owner(chat_id: str, room: dict, sender: dict) -> bool:
        return (room.get("type") == "private" and str(room.get("id")) == chat_id
                and str(sender.get("id")) == chat_id)

    def telegram_video_import(self, message: dict, account: tuple) -> Optional[str]:
        token, chat = account
        if not self._telegram_owner(chat, message.get("chat") or {}, message.get("from") or {}):
            return None
        if self._telegram_review_credentials(self.settings()) != account:
            return None
        text = message.get("text")
        if not isinstance(text, str) or not text.strip():
            return None
        now = time.monotonic()
        self._telegram_imports = {key: entry for key, entry in self._telegram_imports.items()
                                  if entry[0] > now and entry[1] == account}
        reply_id = (message.get("reply_to_message") or {}).get("message_id")
        for key, (_, _, urls, _, range_msg) in self._telegram_imports.items():
            if range_msg is not None and reply_id == range_msg:
                try:
                    start, end = [self._telegram_import_time(part) for part in re.split(r"\s*[-–]\s*", text.strip())]
                except ValueError:
                    return "Interval invalid. Răspunde din nou la cererea de interval cu 00:15 - 00:45."
                return self._queue_telegram_import(key, urls, mode="manual", **{"from": start, "to": end})
        urls = text.split()
        ids = [video_id(url) for url in urls]
        if not all(ids):
            return "Trimite unul sau mai multe linkuri către videoclipuri YouTube, fără alt text."
        urls = [f"https://www.youtube.com/watch?v={vid}" for vid in ids]
        key = secrets.token_hex(12)
        subject = urls[0] if len(urls) == 1 else f"{len(urls)} videoclipuri (aceeași opțiune pentru toate)"
        message_id = publish.telegram_import_prompt(token, chat, f"Cum vrei să import?\n{subject}", {
            "inline_keyboard": [[{"text": "Caută momente automat", "callback_data": f"import:auto:{key}"}],
                                [{"text": "Alege intervalul manual", "callback_data": f"import:manual:{key}"}]],
        })
        self._telegram_imports[key] = (now + 86400, account, urls, message_id, None)
        return None

    @staticmethod
    def _telegram_import_time(value: str) -> float:
        if not re.fullmatch(r"(?:[0-9]{1,6}:){0,2}[0-9]{1,6}(?:[.,][0-9]{1,3})?", value):
            raise ValueError("invalid timestamp")
        first, *rest = map(float, value.replace(",", ".").split(":"))
        seconds = first
        for part in rest:
            if part >= 60:
                raise ValueError("invalid timestamp component")
            seconds = seconds * 60 + part
        return seconds

    def telegram_import_choice(self, callback: dict, account: tuple) -> str:
        token, chat = account
        msg = callback.get("message") or {}
        if not self._telegram_owner(chat, msg.get("chat") or {}, callback.get("from") or {}):
            return "Buton disponibil doar pentru contul Telegram configurat."
        match = re.fullmatch(r"import:(auto|manual):([0-9a-f]{24})", callback["data"])
        if not match or self._telegram_review_credentials(self.settings()) != account:
            return "Alegerea de import nu mai este disponibilă."
        mode, key = match.groups()
        entry = self._telegram_imports.get(key)
        if not entry or entry[0] <= time.monotonic():
            return "Cererea a expirat sau a fost deja procesată. Dacă e nevoie, retrimite linkul."
        expires, owner, urls, message_id, range_msg = entry
        if owner != account or msg.get("message_id") != message_id:
            return "Alegerea de import nu mai este disponibilă."
        if mode == "auto":
            return self._queue_telegram_import(key, urls, mode=mode)
        if range_msg is None:
            range_msg = publish.telegram_import_prompt(token, chat,
                "Răspunde la acest mesaj cu start - final, de exemplu 00:15 - 00:45 (sau 15 - 45).",
                {"force_reply": True, "input_field_placeholder": "00:15 - 00:45"})
            self._telegram_imports[key] = (expires, owner, urls, message_id, range_msg)
        return "Răspunde la mesajul botului cu intervalul."

    def _queue_telegram_import(self, key: str, urls: list[str], **options) -> str:
        try:
            self.start_replay({"urls": urls, **options}, video_import=True)
        except ApiError as exc:
            return f"Nu am putut adăuga linkurile în coadă: {exc.message}"
        self._telegram_imports.pop(key)
        return "Video adăugat în coada de import." if len(urls) == 1 else (
            f"{len(urls)} videoclipuri adăugate în coada de import.")

    def telegram_review_test(self, clip_id: str, *, clear: bool = False) -> dict:
        with self._delivery_lock:
            d, meta = self._review_meta(clip_id)
            if clear:
                meta.pop("schedule_test", None)
            else:
                if not self._telegram_review_credentials(self.settings()):
                    raise ApiError(400, "enable Telegram review and connect a private chat first")
                if (meta.get("telegram_review") or {}).get("status") in ("pending", "sending", "sent"):
                    raise ApiError(409, "this clip already has an active Telegram review")
                if not meta["posts"].get("youtube") or not (meta.get("quality_review") or {}).get("postworthy"):
                    raise ApiError(400, "this clip needs YouTube copy and a quality rating")
                meta.pop("schedule_test", None)
                meta["telegram_review"] = {"status": "pending", "test": True}
            write_meta(d, meta)
        return {"ok": True}

    def telegram_review_send(self) -> None:
        s = self.settings()
        account = self._telegram_review_credentials(s)
        if not account:
            return
        token, chat = account
        with self._delivery_lock:
            row = min((c for c in self.clips() if c["telegram_review"].get("status") == "pending"
                       and c["has_video"] and not c["posting_now"]
                       and (c["telegram_review"].get("test")
                            or (not c["schedule"] and c["editorial_feedback"] != "reject"
                                and (c["posted"] or {}).get("status") == "held"))),
                      key=lambda c: (c["created"], c["id"]), default=None)
            if row is None:
                return
            cid = row["id"]
            d, meta = self._review_meta(cid)
            copy = meta["posts"].get("tiktok")
            key = secrets.token_hex(16)
            test = bool(meta["telegram_review"].get("test"))
            meta["telegram_review"].update(status="sending", key=key, chat=chat)
            write_meta(d, meta)
        logger = self.logger(s)
        try:
            if not copy or not copy.get("title") or not copy.get("description"):
                raise publish.PublishError("TikTok title or caption is missing")
            review = meta.get("quality_review") or {}
            message_id = publish.telegram_review_video(
                d / "short.mp4", token=token, chat_id=chat, key=key,
                title=copy["title"], description=copy["description"],
                quality=review.get("postworthy") if review.get("status") == "ok" else None, test=test)
            result = {"status": "sent", "message_id": message_id}
        except publish.PublishError as exc:
            logger(f"{cid}: Telegram review send failed ({exc})")
            result = {"status": "failed"}
        except (OSError, ValueError, KeyError) as exc:
            logger(f"{cid}: Telegram review send failed ({type(exc).__name__})")
            result = {"status": "failed"}
        with self._delivery_lock:
            # An upload can take a while; keep any concurrent dashboard edits.
            current = json.loads((d / "meta.json").read_text(encoding="utf-8-sig"))
            current["telegram_review"].update(result)
            write_meta(d, current)

    def telegram_review_approve(self, callback: dict, account: tuple) -> Optional[str]:
        data = callback.get("data") or ""
        if not data.startswith("yt:"):
            return None
        _, chat = account
        msg = callback.get("message") or {}
        room = msg.get("chat") or {}
        sender = callback.get("from") or {}
        if not self._telegram_owner(chat, room, sender):
            return "Buton disponibil doar pentru contul Telegram configurat."
        with self._delivery_lock:
            s = self.settings()
            if self._telegram_review_credentials(s) != account:
                return "Aprobarea prin Telegram nu mai este activă."
            for row in self.clips():
                review = row["telegram_review"]
                if review.get("key") != data[3:] or review.get("chat") != chat:
                    continue
                if review.get("status") == "sending":
                    return "Mesajul se înregistrează. Mai apasă o dată."
                if review.get("message_id") != msg.get("message_id"):
                    break
                if review.get("status") == "approved":
                    return "Ai aprobat deja acest clip."
                if review.get("status") != "sent":
                    break
                cid = row["id"]
                d, meta = self._review_meta(cid)
                if review.get("test"):
                    now = datetime.now(timezone.utc)
                    routes = publish.route(["youtube"], s.youtube_via, s.instagram_via, s.others_via)
                    when = self._next_bucket_time(cid, s, now, routes) or now
                    meta["schedule_test"] = {"at": when.isoformat(), "platforms": ["youtube"]}
                    meta["telegram_review"]["status"] = "approved"
                    write_meta(d, meta)
                    return "Test adăugat în Schedule. Nu se va publica pe YouTube."
                if meta.get("editorial_feedback") == "reject":
                    return "Clipul a fost respins sau scos din coadă în dashboard."
                if meta.get("schedule"):
                    return "Clipul are deja o programare. Verifică pagina Schedule."
                if ((meta.get("posted") or {}).get("networks") or {}).get("youtube", {}).get("status") in (
                        "posted", "private"):
                    return "Clipul a fost deja încărcat pe YouTube."
                _, routes, _ = self._delivery_routes(meta, s, ["youtube"], "telegram_review")
                now = datetime.now(timezone.utc)
                when = self._next_bucket_time(cid, s, now, routes) or now
                # Save approval and its queue slot together, even if due immediately.
                meta["schedule"] = {"at": when.isoformat(), "platforms": ["youtube"],
                                    "bucket": True, "source": "telegram"}
                meta["editorial_feedback"] = "approve"
                meta["telegram_review"]["status"] = "approved"
                write_meta(d, meta)
                if when <= now:
                    return "Aprobat. Trimiterea către YouTube începe imediat."
                return f"Programat pe YouTube: {when.astimezone():%d.%m.%Y %H:%M} (ora PC-ului)."
        return "Clipul nu mai este disponibil pentru aprobare."

    # ------------------------------------------------------- posting accounts
    def posting_issues(self, s: config.Settings) -> list:
        if s.delivery_mode == "telegram_review":
            creds = publish.credentials()
            issues = publish.missing({"tiktok": "phone"}, creds)
            if creds["tg_chat"] and not publish.telegram_private_chat(creds["tg_chat"]):
                issues.append("use a private Telegram chat for approvals")
            if s.youtube_via not in ("api", "upload-post"):
                issues.append("configure automatic YouTube publishing (API or Upload-Post)")
            else:
                issues.extend(publish.missing({"youtube": s.youtube_via}, creds, s.upload_post_user))
            return issues
        if s.delivery_mode == "top_daily":
            issues = publish.missing({"tiktok": "phone"}, publish.credentials())
            if s.ai_provider != "local" or not (s.local_model or s.local_vision_model):
                issues.append("choose a local AI model (Settings > AI review and copy)")
            return issues
        if s.delivery_mode != "legacy" or not s.post_enabled:
            return []
        routes = publish.route(s.post_platforms, s.youtube_via, s.instagram_via, s.others_via)
        return publish.missing(routes, publish.credentials(), s.upload_post_user)

    def accounts(self) -> dict:
        c = publish.credentials()
        s = self._safe_settings()
        return {"routes": publish.route(s.post_platforms, s.youtube_via, s.instagram_via, s.others_via),
                "youtube": {"client": bool(c["yt_id"] and c["yt_secret"]), "connected": bool(c["yt_refresh"])},
                "instagram": {"token": bool(c["ig_token"])},
                "telegram": {"token": bool(c["tg_token"]), "chat": bool(c["tg_chat"])},
                "cloudflared": bool(procs.cloudflared())}

    def youtube_connect(self, redirect_uri: str) -> dict:
        c = publish.credentials()
        if not (c["yt_id"] and c["yt_secret"]):
            raise ApiError(400, "save your YouTube Client ID and Client Secret first")
        url, state, verifier = publish.youtube_auth_url(c["yt_id"], redirect_uri)
        self._oauth[state] = (verifier, redirect_uri, time.time())
        return {"url": url}

    def youtube_callback(self, state: str, code: str, error: str = "") -> str:
        pending = self._oauth.pop(state or "", None)
        if error:
            raise ApiError(400, f"Google said: {error}")
        if not pending or time.time() - pending[2] > 900 or not code:
            raise ApiError(400, "this sign-in link expired: press Connect YouTube again")
        c = publish.credentials()
        try:
            refresh = publish.youtube_exchange_code(c["yt_id"], c["yt_secret"], code, pending[1], pending[0])
        except publish.PublishError as e:
            raise ApiError(400, str(e))
        self.save_secrets({"YOUTUBE_REFRESH_TOKEN": refresh})
        return "YouTube is connected. You can close this tab and go back to ClipperHub."

    def check_account(self, net: str) -> dict:
        c = publish.credentials()
        need = {"youtube": ("yt_refresh", "press Connect YouTube first"),
                "instagram": ("ig_token", "paste and save your Instagram access token first"),
                "telegram": ("tg_token", "paste and save your Telegram bot token first")}.get(net)
        if need and not c[need[0]]:
            raise ApiError(400, need[1])
        try:
            if net == "youtube":
                publish.youtube_access_token(c["yt_id"], c["yt_secret"], c["yt_refresh"])
                return {"ok": True, "message": "YouTube sign-in works"}
            if net == "instagram":
                me = publish.instagram_me(c["ig_token"])
                return {"ok": True, "message": f"Instagram: @{me['username']} (id {me['id']})"}
            if net == "telegram":
                if not c["tg_chat"]:
                    if self.settings().delivery_mode == "telegram_review":
                        raise ApiError(400, "connect and verify your private Telegram chat in manual mode first")
                    found = publish.telegram_find_chat(c["tg_token"])
                    self.save_secrets({"TELEGRAM_CHAT_ID": found["chat_id"]})
                    c["tg_chat"] = found["chat_id"]
                publish.telegram_send_text(c["tg_token"], c["tg_chat"],
                                           "ClipperHub is connected to this Telegram chat.")
                return {"ok": True, "message": "Test message sent to your Telegram"}
        except publish.PublishError as e:
            raise ApiError(400, str(e))
        except OSError as e:
            raise ApiError(502, f"network error: {e}")
        raise ApiError(404, "unknown account")

    def refresh_instagram_token(self) -> None:
        """Instagram tokens last 60 days; renew it daily so it never runs out."""
        tok = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "")
        if tok:
            try:
                new = publish.instagram_refresh(tok)
            except OSError:
                return
            if new and new != tok:
                self.save_secrets({"INSTAGRAM_ACCESS_TOKEN": new})

    def delete_clip(self, clip_id: str) -> dict:
        with self._delivery_lock:
            d = self.clip_dir(clip_id)
            posted = json.loads((d / "meta.json").read_text(encoding="utf-8-sig")).get("posted") or {}
            if self.posting_now(clip_id) or posted.get("status") in ("queued", "posting"):
                raise ApiError(409, "check delivery status before deleting this clip")
            shutil.rmtree(d)
        return {"ok": True}

    def open_folder(self, clip_id: Optional[str] = None) -> dict:
        d = self.clip_dir(clip_id) if clip_id else self.clips_root()
        d.mkdir(parents=True, exist_ok=True)
        if hasattr(os, "startfile"):
            os.startfile(str(d))  # type: ignore[attr-defined]
        return {"ok": True, "path": str(d)}

    # ---------------------------------------------------------- upload-post
    def _upload_key(self) -> str:
        key = os.environ.get("UPLOADPOST_API_KEY", "")
        if not key:
            raise ApiError(400, "add your Upload-Post API key first (Settings > API keys)")
        return key

    def upload_profiles(self) -> dict:
        try:
            profiles = poster.list_profiles(self._upload_key())
        except poster.UploadPostError as e:
            raise ApiError(502, str(e))
        except OSError as e:
            raise ApiError(502, f"could not reach Upload-Post: {e}")
        current = self._safe_settings().upload_post_user
        return {"profiles": profiles, "current": current,
                "current_ok": any(p["username"] == current for p in profiles)}

    def create_upload_profile(self, body: dict) -> dict:
        try:
            name = poster.create_profile(self._upload_key(), str((body or {}).get("username", "")))
        except poster.UploadPostError as e:
            raise ApiError(400, str(e))
        except OSError as e:
            raise ApiError(502, f"could not reach Upload-Post: {e}")
        return {"ok": True, "username": name}

    def upload_connect_url(self, body: dict, return_url: str) -> dict:
        try:
            url = poster.connect_url(self._upload_key(), str((body or {}).get("username", "")),
                                     return_url)
        except poster.UploadPostError as e:
            raise ApiError(400, str(e))
        except OSError as e:
            raise ApiError(502, f"could not reach Upload-Post: {e}")
        return {"url": url}

    # ---------------------------------------------------------------- music
    def library(self) -> music.Library:
        s = self._safe_settings()
        return music.Library(s.music_dir or s.data_dir / "music")

    def music_list(self) -> dict:
        lib = self.library()
        return {"folder": str(lib.folder), "tracks": lib.describe()}

    def music_file(self, name: str) -> Path:
        lib = self.library()
        p = (lib.folder / unquote(name)).resolve()
        if p.parent != lib.folder.resolve() or not p.is_file() or p.suffix.lower() not in music.EXTENSIONS:
            raise ApiError(404, "no such track")
        return p


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "ClipperHub"
    ctl: Controller
    port: int

    def log_message(self, fmt, *args):  # keep the console for the watcher log
        pass

    # ------------------------------------------------------------- helpers
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").lower()
        return host in (f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _json(self, data, status: int = 200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, status: int, message: str):
        self._json({"error": message}, status)

    def _body(self, limit: int = MAX_JSON) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n < 0 or n > limit:
            raise ApiError(413, "request too large")
        return self.rfile.read(n) if n else b""

    def _json_body(self):
        try:
            return json.loads(self._body() or b"{}")
        except ValueError:
            raise ApiError(400, "invalid JSON")

    def _send_file(self, path: Path, ctype: str, *, cache_control: str = "no-store"):
        size = path.stat().st_size
        rng = self.headers.get("Range")
        start, end = 0, size - 1
        status = 200
        if rng:
            m = re.match(r"^bytes=(\d*)-(\d*)$", rng.strip())
            if not m or (not m.group(1) and not m.group(2)):
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Cache-Control", cache_control)
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        with open(path, "rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0:
                chunk = fh.read(min(256 * 1024, left))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    return
                left -= len(chunk)

    # ------------------------------------------------------------- routing
    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_PUT(self):
        self._dispatch("PUT")

    def do_DELETE(self):
        self._dispatch("DELETE")

    def _dispatch(self, method: str):
        if not self._host_ok():
            self._error(403, "ClipperHub only answers on localhost")
            return
        if method != "GET":
            if self.headers.get("X-Livewatch") != "1":
                self._error(403, "missing X-Livewatch header")
                return
            origin = self.headers.get("Origin")
            if origin and urlparse(origin).netloc.lower() not in (f"127.0.0.1:{self.port}",
                                                                    f"localhost:{self.port}"):
                self._error(403, "cross-origin request refused")
                return
        url = urlparse(self.path)
        path = url.path
        q = parse_qs(url.query)
        mutation = (method != "GET" and path != "/api/update") or path == "/oauth/youtube"
        try:
            if mutation:
                with self.ctl.update_lock:
                    if self.ctl.updating:
                        raise ApiError(409, "ClipperHub is restarting to install an update.")
                    self.ctl.mutations += 1
                try:
                    return self._route(method, path, q)
                finally:
                    with self.ctl.update_lock:
                        self.ctl.mutations -= 1
            self._route(method, path, q)
        except ApiError as e:
            self._error(e.status, e.message)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass
        except Exception as e:  # noqa: BLE001 - reported to the page
            self._error(500, f"{type(e).__name__}: {e}")

    def _route(self, method: str, path: str, q: dict):
        ctl = self.ctl
        if method == "POST" and path == "/api/update":
            if not ctl.update_enabled:
                raise ApiError(409, "Start the portable installation with ClipperHub.bat to install from here.")
            body = self._json_body()
            latest = ctl.release_status
            if not isinstance(body, dict) or body.get("version") != latest.get("version") or "patch" not in latest:
                raise ApiError(409, "The selected update is no longer available.")
            try:
                files = releases.download_update(latest)
            except releases.UpdateError as exc:
                raise ApiError(502, str(exc)) from exc
            with ctl.update_lock, ctl._delivery_lock, ctl._lock:
                if (ctl.updating or ctl.mutations or (ctl.svc and not ctl.svc.finished.is_set())
                        or (ctl.video_watch.worker and ctl.video_watch.worker.is_alive())
                        or ctl._replay_active or ctl._video_imports
                        or any(t.is_alive() for t in ctl._posting.values())):
                    raise ApiError(409, "Stop monitoring and wait for imports, clips and posting to finish, then try again.")
                s = ctl.settings()
                ctl.pending_update = (files, tuple(p.resolve() for p in
                                      (ctl.config_path, ctl.env_path, ctl.backup_dir, s.data_dir, s.music_dir)))
                ctl.updating = True
            try:
                self._json({"ok": True})
            finally:
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if method == "GET":
            if path in ("/", "/index.html"):
                return self._send_file(WEB_DIR / "index.html", "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(ctl.status())
            if path == "/api/log":
                return self._json(ctl.log_since(int((q.get("since") or ["0"])[0] or 0)))
            if path == "/api/config":
                cfg_error = ""
                raw = ctl.raw_config()
                try:
                    raw = config.migrate_upload_channels(raw)
                    s = ctl.settings()
                    raw.setdefault("ai", {}).setdefault("provider", s.ai_provider)
                    raw.setdefault("post", {}).setdefault("delivery_mode", s.delivery_mode)
                except config.ConfigError as e:
                    cfg_error = str(e)
                return self._json({"raw": raw, "schema": schema(),
                                   "error": cfg_error, "path": str(ctl.config_path),
                                   "version": ctl.config_version()})
            if path == "/api/secrets":
                return self._json(ctl.secrets())
            if path == "/api/clips":
                return self._json(ctl.clip_page(q))
            if path == "/api/schedule":
                return self._json(ctl.schedule_list())
            if path.startswith("/api/clips/"):
                return self._json(ctl.clip_detail(unquote(path[len("/api/clips/"):])))
            if path == "/api/music":
                return self._json(ctl.music_list())
            if path == "/api/uploadpost/profiles":
                return self._json(ctl.upload_profiles())
            if path == "/api/config/backups":
                return self._json({"backups": ctl.backups()})
            if path == "/api/accounts":
                return self._json(ctl.accounts())
            if path == "/oauth/youtube":
                one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
                try:
                    text = ctl.youtube_callback(one("state"), one("code"), one("error"))
                    ok = True
                except ApiError as e:
                    text, ok = e.message, False
                body = (f"<!doctype html><meta charset=utf-8><title>ClipperHub</title>"
                        f"<body style='font:16px system-ui;background:#0e1013;color:#e6e9ee;padding:40px'>"
                        f"<h2>{'✅' if ok else '❌'} {html_escape(text)}</h2>"
                        f"<a style='color:#a48bff' href='/'>Back to ClipperHub</a></body>").encode()
                self.send_response(200 if ok else 400)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            m = re.fullmatch(r"/media/(.+)/preview\.jpg", path)
            if m:
                f, version = ctl.clip_preview(unquote(m.group(1)))
                cache = "private, max-age=86400" if q.get("v") == [version] else "no-store"
                return self._send_file(f, "image/jpeg", cache_control=cache)
            m = re.match(r"^/media/(.+)/short\.mp4$", path)
            if m:
                d = ctl.clip_dir(unquote(m.group(1)))
                f = d / "short.mp4"
                if not f.is_file():
                    raise ApiError(404, "not rendered")
                return self._send_file(f, "video/mp4")
            m = re.match(r"^/music/([^/]+)$", path)
            if m:
                f = ctl.music_file(m.group(1))
                return self._send_file(f, mimetypes.guess_type(f.name)[0] or "audio/mpeg")
            raise ApiError(404, "not found")

        if method == "POST":
            watch_path = re.fullmatch(r"/api/video/watch/(start|stop|check)", path)
            if watch_path:
                return self._json(ctl.video_watch_action(watch_path.group(1)))
            if path == "/api/start":
                return self._json(ctl.start())
            if path == "/api/stop":
                return self._json(ctl.stop())
            if path == "/api/clear-queue":
                return self._json(ctl.clear_live_queue())
            if path == "/api/check":
                return self._json(ctl.check_now())
            if path == "/api/replay":
                return self._json(ctl.start_replay(self._json_body()))
            if path == "/api/video":
                return self._json(ctl.start_replay(self._json_body(), video_import=True))
            if path == "/api/replay/cancel":
                body = self._json_body()
                if not isinstance(body, dict):
                    raise ApiError(400, "expected cancellation settings")
                return self._json(ctl.cancel_replay(body.get("id")))
            if path == "/api/open":
                return self._json(ctl.open_folder())
            if path == "/api/uploadpost/profiles":
                return self._json(ctl.create_upload_profile(self._json_body()))
            if path == "/api/accounts/youtube/connect":
                return self._json(ctl.youtube_connect(f"http://127.0.0.1:{self.port}/oauth/youtube"))
            m = re.match(r"^/api/accounts/(youtube|instagram|telegram)/check$", path)
            if m:
                return self._json(ctl.check_account(m.group(1)))
            if path == "/api/config/restore":
                body = self._json_body()
                return self._json(ctl.restore_backup(body.get("name") if isinstance(body, dict) else ""))
            if path == "/api/uploadpost/connect":
                return self._json(ctl.upload_connect_url(
                    self._json_body(), f"http://127.0.0.1:{self.port}/"))
            if path == "/api/music":
                name = unquote(self.headers.get("X-Filename") or "")
                data = self._body(MAX_UPLOAD)
                try:
                    saved = ctl.library().add(name, data)
                except ValueError as e:
                    raise ApiError(400, str(e))
                return self._json({"ok": True, "name": saved})
            m = re.match(r"^/api/music/([^/]+)/(enable|disable)$", path)
            if m:
                try:
                    ctl.library().set_enabled(ctl.music_file(m.group(1)).name, m.group(2) == "enable")
                except FileNotFoundError:
                    raise ApiError(404, "no such track")
                return self._json({"ok": True})
            m = re.match(r"^/api/clips/(.+)/(post|open|schedule|feedback|telegram-test)$", path)
            if m:
                cid = unquote(m.group(1))
                if m.group(2) == "open":
                    return self._json(ctl.open_folder(cid))
                body = self._json_body()
                if m.group(2) == "telegram-test":
                    if not isinstance(body, dict):
                        raise ApiError(400, "expected Telegram test settings")
                    return self._json(ctl.telegram_review_test(cid, clear=body.get("clear") is True))
                if m.group(2) == "schedule":
                    return self._json(ctl.schedule_clip(cid, body))
                if m.group(2) == "feedback":
                    return self._json(ctl.feedback(cid, body))
                plats = body.get("platforms") if isinstance(body, dict) else None
                return self._json(ctl.post_clip(cid, plats))
            raise ApiError(404, "not found")

        if method == "PUT":
            if path == "/api/config":
                body = self._json_body()
                if not isinstance(body, dict) or "raw" not in body or not isinstance(body.get("version"), str):
                    raise ApiError(400, "reload the page: it is older than the server")
                return self._json(ctl.save_config(body["raw"], body["version"]))
            if path == "/api/secrets":
                return self._json(ctl.save_secrets(self._json_body()))
            m = re.match(r"^/api/clips/(.+)/posts$", path)
            if m:
                return self._json(ctl.update_posts(unquote(m.group(1)), self._json_body()))
            raise ApiError(404, "not found")

        if method == "DELETE":
            m = re.match(r"^/api/clips/(.+)$", path)
            if m:
                return self._json(ctl.delete_clip(unquote(m.group(1))))
            m = re.match(r"^/api/music/([^/]+)$", path)
            if m:
                ctl.library().remove(ctl.music_file(m.group(1)).name)
                return self._json({"ok": True})
            raise ApiError(404, "not found")


class _ExclusiveServer(ThreadingHTTPServer):
    """One livewatch per port. The stock server sets SO_REUSEADDR, which on
    Windows lets a second copy bind the same port and silently share it: two
    watchers would then record and clip the same streams."""
    allow_reuse_address = False

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def make_server(ctl: Controller, port: int = 8765) -> ThreadingHTTPServer:
    handler = type("BoundHandler", (Handler,), {"ctl": ctl, "port": port})
    srv = _ExclusiveServer(("127.0.0.1", port), handler)
    srv.daemon_threads = True
    # an ephemeral port (0) is known only after binding
    handler.port = srv.server_address[1]
    return srv


def serve(config_path: Path, port: int = 8765, open_browser: bool = True,
          autostart: bool = False, managed_updates: bool = False) -> None:
    ctl = Controller(config_path)
    root = WEB_DIR.parent.parent
    ctl.update_enabled = managed_updates and __package__ == "clipperhub" and not (root / ".git").exists()
    try:
        srv = make_server(ctl, port)
    except OSError:
        print(f"Port {port} is busy: ClipperHub is probably already running. "
              f"Open http://127.0.0.1:{port}/ instead.", flush=True)
        raise SystemExit(3)
    url = f"http://127.0.0.1:{srv.server_address[1]}/"
    print(f"ClipperHub UI on {url}  (Ctrl+C to quit)", flush=True)
    if autostart:
        try:
            ctl.start()
            print("watcher started", flush=True)
        except ApiError as e:
            print(f"watcher not started: {e.message}", flush=True)
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    scheduler_stop = threading.Event()

    def renew_tokens():
        while not scheduler_stop.is_set():
            try:
                ctl.refresh_instagram_token()
            except Exception:  # noqa: BLE001 - best effort, retried tomorrow
                pass
            scheduler_stop.wait(24 * 3600)
    token_worker = threading.Thread(target=renew_tokens, daemon=True, name="token-renew")
    token_worker.start()

    def check_releases():
        while not scheduler_stop.is_set():
            ctl.release_status = releases.check_latest()
            scheduler_stop.wait(releases.CHECK_INTERVAL)

    threading.Thread(target=check_releases, daemon=True, name="release-check").start()

    def dispatch_due():
        last_error = ""
        while not scheduler_stop.is_set():
            try:
                with ctl._delivery_lock:
                    if not ctl.updating:
                        ctl.delivery_tick(datetime.now(timezone.utc))
                last_error = ""
            except (OSError, ValueError, ApiError) as e:
                message = f"Delivery scheduler: {e}"
                if message != last_error:
                    ctl.logger()(message)
                last_error = message
            scheduler_stop.wait(10)

    delivery_worker = threading.Thread(target=dispatch_due, daemon=True, name="delivery")
    delivery_worker.start()

    def send_telegram_reviews():
        last_error = ""
        while not scheduler_stop.is_set():
            try:
                ctl.telegram_review_send()
                last_error = ""
            except (OSError, ValueError, KeyError, ApiError) as exc:
                message = f"Telegram review sender: {type(exc).__name__}"
                if message != last_error:
                    ctl.logger()(message)
                last_error = message
            scheduler_stop.wait(5)

    def poll_telegram_reviews():
        current_account = None
        offset = None
        last_error = ""
        while not scheduler_stop.is_set():
            try:
                account = ctl._telegram_review_credentials(ctl.settings())
                if account != current_account:
                    current_account, offset = account, None
                if account is None:
                    scheduler_stop.wait(5)
                    continue
                token, _ = account
                updates = publish.telegram_review_updates(token, offset)
                for upd in updates:
                    if scheduler_stop.is_set():
                        return
                    callback = upd.get("callback_query")
                    if callback:
                        try:
                            if (callback.get("data") or "").startswith("import:"):
                                answer = ctl.telegram_import_choice(callback, account)
                            else:
                                answer = ctl.telegram_review_approve(callback, account)
                        except ApiError as exc:
                            answer = f"Nu am putut programa clipul: {exc.message}"[:195]
                        except (OSError, ValueError, publish.PublishError) as exc:
                            ctl.logger()(f"Telegram import prompt failed ({type(exc).__name__})")
                            answer = "Nu am putut trimite cererea de interval. Încearcă din nou."
                        if answer:
                            try:
                                publish.telegram_answer(token, callback["id"], answer[:195])
                            except (OSError, ValueError, publish.PublishError) as exc:
                                ctl.logger()(f"Telegram approval reply failed ({type(exc).__name__})")
                    elif message := upd.get("message"):
                        try:
                            answer = ctl.telegram_video_import(message, account)
                            if answer:
                                publish.telegram_send_text(token, account[1], answer)
                        except (OSError, ValueError, publish.PublishError) as exc:
                            ctl.logger()(f"Telegram import reply failed ({type(exc).__name__}); retry the request")
                    offset = upd["update_id"] + 1
                last_error = ""
            except (OSError, ValueError, KeyError, publish.PublishError) as exc:
                reason = str(exc) if isinstance(exc, publish.PublishError) else type(exc).__name__
                message = f"Telegram approval polling failed ({reason})"
                if message != last_error:
                    ctl.logger()(message)
                last_error = message
                scheduler_stop.wait(10)

    telegram_workers = [threading.Thread(target=target, daemon=True) for target in
                        (send_telegram_reviews, poll_telegram_reviews)]
    for worker in telegram_workers:
        worker.start()
    try:
        srv.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("stopping...", flush=True)
    finally:
        scheduler_stop.set()
        ctl.video_watch.stop.set()
        ctl.video_watch.wake.set()
        ctl.cancel_replay(closing=True)
        if ctl.svc:
            ctl.svc.stop()
            deadline = time.time() + 60
            while not ctl.svc.finished.is_set() and time.time() < deadline:
                time.sleep(0.5)
        srv.server_close()
    if ctl.pending_update:
        for worker in (token_worker, delivery_worker, *telegram_workers):
            worker.join(timeout=35)
        if any(worker.is_alive() for worker in (token_worker, delivery_worker, *telegram_workers)):
            print("Update postponed: background work did not finish in time.", flush=True)
        else:
            try:
                releases.install_update(root, *ctl.pending_update)
            except (OSError, releases.UpdateError) as exc:
                print(f"Update could not be installed: {exc}", flush=True)
            else:
                print("Update installed. Restarting ClipperHub.", flush=True)
        raise SystemExit(42)
