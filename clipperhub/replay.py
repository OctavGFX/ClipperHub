"""Replay mode: run the whole pipeline on an ended YouTube stream or Twitch VOD.

For testing outputs and publishing without waiting for anyone to go live:

1. Read the video's details (title, channel, length).
2. Find the moments, from whatever the video offers:
   - **YouTube chat replay**, when the stream kept one. YouTube serves it
     sequentially at roughly 10-15x real time, so 30 minutes take 2-3 minutes;
     the download stops once the window is covered.
   - **Loudness**, when there is no chat (many channels turn chat replay off,
     and Twitch VODs never come with one): only the audio of the chosen
     window is downloaded (about 20 MB per 30 minutes) and its volume spikes
     are found, the same signal the live watcher uses for Kick.
   - **Twitch viewer clips**, with ``TWITCH_CLIENT_ID``/``TWITCH_CLIENT_SECRET``
     (a free Twitch developer app): the moments viewers clipped from that VOD,
     the strongest signal there is. Official Helix API, nothing scraped.
3. Download only the seconds around each of the best moments
   (``yt-dlp --download-sections``), never the whole multi-hour video.
4. Make each into a Short with the normal pipeline: transcript, hook, text,
   render and, if asked, posting.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from . import director, procs
from .chat import ChatMessage, parse_youtube_replay_line
from .config import Channel, Settings
from .detector import Peak, PeakDetector
from .facecam import SourceFacecam
from .recorder import parse_rms_lines

BASE = 1_000_000_000.0          # simulated wall clock: BASE + seconds into the video
MARGIN = 6.0                    # extra seconds fetched each side of a clip (keyframe slack)
MIN_GAP = 45.0                  # two moments closer than this are the same moment
_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "youtu.be", "music.youtube.com"}
_TW_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TW_VOD = re.compile(r"^/(?:[^/]+/)?(?:videos?|v)/(\d{5,15})/?$")


class ReplayError(RuntimeError):
    pass


def video_id(url: str) -> Optional[str]:
    """The 11-character id of a YouTube watch/live/shorts/youtu.be link, else None."""
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if u.scheme not in ("http", "https") or host not in _YT_HOSTS:
        return None
    if host == "youtu.be":
        cand = u.path.lstrip("/").split("/")[0]
    elif u.path == "/watch":
        cand = (parse_qs(u.query).get("v") or [""])[0]
    else:
        m = re.match(r"^/(?:live|shorts|embed)/([^/?#]+)", u.path)
        cand = m.group(1) if m else ""
    return cand if _ID.match(cand or "") else None


def twitch_vod_id(url: str) -> Optional[str]:
    """The numeric id of a ``twitch.tv/videos/<id>`` link, else None."""
    try:
        u = urlparse(str(url or "").strip())
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or (u.hostname or "").lower() not in _TW_HOSTS:
        return None
    m = _TW_VOD.match(u.path or "")
    return m.group(1) if m else None


def parse_source(url: str) -> Tuple[str, str, str]:
    """(platform, id, canonical url) of a replayable link; ReplayError otherwise."""
    vid = video_id(url)
    if vid:
        return "youtube", vid, f"https://www.youtube.com/watch?v={vid}"
    tid = twitch_vod_id(url)
    if tid:
        return "twitch", tid, f"https://www.twitch.tv/videos/{tid}"
    if "twitch.tv" in str(url):
        raise ReplayError("use a Twitch VOD link (twitch.tv/videos/<number>), not a channel page")
    raise ReplayError("use a YouTube video link or a Twitch VOD link (twitch.tv/videos/<number>)")


# --------------------------------------------------------------------------- peaks
def find_peaks(messages: List[tuple], settings: Settings, start: float, end: float,
               max_clips: Optional[int], loudness: Optional[List[tuple]] = None,
               extra: Optional[List[Peak]] = None, *, gap: float = MIN_GAP) -> List[Peak]:
    """The best moments in [start, end] seconds, best first, at most ``max_clips``.

    ``messages``: (offset, ChatMessage); ``loudness``: (offset, rms_db) per second;
    ``extra``: ready-made peaks (viewer clips). ``Peak.ts`` is seconds into the video.
    """
    peaks: List[Peak] = list(extra or [])
    if loudness:
        peaks += loudness_peaks(loudness, start, end, None if max_clips is None else max_clips * 2,
                                floor=settings.threshold if max_clips is None else 1.0,
                                min_samples=5 if max_clips is None else 60)
    if messages:
        det = PeakDetector(threshold=settings.threshold, warmup=min(settings.warmup_seconds, 90.0),
                           cooldown=settings.cooldown_seconds, min_chat_rate=settings.min_chat_rate,
                           audio_weight=0.0, max_per_hour=10_000)
        for off, msg in messages:
            if start - PeakDetector.BASELINE <= off <= end + 30:
                det.add_message(ChatMessage(BASE + off, msg.user, msg.text))
        for now in range(int(BASE + start), int(BASE + end + 30)):
            p = det.evaluate(now)
            if p and start <= p.ts - BASE <= end:
                peaks.append(replace(p, ts=p.ts - BASE))
    peaks.sort(key=lambda p: p.score, reverse=True)
    chosen: List[Peak] = []
    bins: dict[int, float] = {}
    for p in peaks:
        slot = math.floor(p.ts / gap)
        if all(abs(p.ts - bins[i]) >= gap for i in (slot - 1, slot, slot + 1) if i in bins):
            chosen.append(p)
            bins[slot] = p.ts
        if max_clips is not None and len(chosen) >= max_clips:
            break
    return chosen


def loudness_peaks(series: List[tuple], start: float, end: float, n: Optional[int],
                   floor: float = 1.0, *, min_samples: int = 60) -> List[Peak]:
    """The ``n`` loudest moments of [start, end], ranked, not thresholded.

    Live, a peak must clear a fixed bar; a replay wants the best moments of the
    window whatever its level. Each second is scored by its 3 s loudness against
    the whole series (median/MAD) and against the 30 s before it (a scream after
    a calm stretch counts more than a loud stretch that stays loud); local
    maxima at least ``MIN_GAP`` apart are kept, best first.
    """
    if len(series) < min_samples:
        return []
    by_sec = {}
    for t, db in series:
        by_sec[int(t)] = max(-90.0, float(db))
    secs = sorted(by_sec)
    vals = [by_sec[s] for s in secs]
    smooth = [sum(vals[max(0, i - 1):i + 2]) / len(vals[max(0, i - 1):i + 2]) for i in range(len(vals))]
    from statistics import median
    m = median(smooth)
    sigma = max(1.4826 * median([abs(v - m) for v in smooth]), 1.5)
    scores = []
    for i, v in enumerate(smooth):
        prev = smooth[max(0, i - 30):max(1, i - 2)]
        local = (v - median(prev)) / sigma if prev else 0.0
        scores.append(0.5 * (v - m) / sigma + 0.5 * local)
    half = int(MIN_GAP // 2)
    cands = []
    for i, sc in enumerate(scores):
        t = secs[i]
        if not (start <= t <= end) or sc < floor:
            continue
        lo, hi = max(0, i - half), min(len(scores), i + half + 1)
        if sc >= max(scores[lo:hi]):
            cands.append(Peak(ts=float(t), score=round(sc, 2), chat_z=0.0, audio_z=round(sc, 2),
                              hype_share=0.0, reason=f"loudness spike x{sc:.1f}"))
    cands.sort(key=lambda p: p.score, reverse=True)
    return cands[:n]


def clips_to_peaks(clips: List[dict], start: float, end: float) -> List[Peak]:
    """Twitch viewer clips -> peaks. A clip is cut a little after what it shows,
    so the moment is placed two thirds into it; more views = higher score."""
    out = []
    for c in clips:
        moment = float(c["start"]) + float(c.get("duration") or 0) * 0.66
        if start <= moment <= end:
            views = int(c.get("views") or 0)
            # viewers choosing to clip it outranks any loudness spike
            out.append(Peak(ts=moment, score=round(10.0 + math.log10(views + 1) * 2, 2),
                            chat_z=0.0, audio_z=0.0, hype_share=0.0,
                            reason=f"viewers clipped this ({views} views)"))
    return out


# --------------------------------------------------------------------------- replay
class Replay:
    """One replay run. ``run()`` blocks; ``cancel()`` may be called from another thread."""

    def __init__(self, settings: Settings, url: str, *, minutes: float = 30, start_minute: float = 0,
                 max_clips: int = 5, post: Optional[bool] = None, log: Callable[[str], None] = print,
                 service=None, video_import: bool = False, interval: Optional[Tuple[float, float]] = None,
                 upload_only: bool = False):
        self.platform, self.vid, self.url = parse_source(url)
        if video_import and self.platform != "youtube":
            raise ReplayError("video import currently supports YouTube videos only")
        if interval is not None and (not video_import or not all(math.isfinite(x) for x in interval)
                                     or not 0 <= interval[0] < interval[1]
                                     or not settings.min_seconds <= interval[1] - interval[0] <= settings.max_seconds):
            raise ReplayError(f"manual range must be {settings.min_seconds:g}-{settings.max_seconds:g} seconds long")
        self.video_import, self.interval = video_import, interval
        self.upload_only = upload_only
        if not video_import and (not (1 <= minutes <= 600) or not (0 <= start_minute <= 6000)):
            raise ReplayError("the window must be 1-600 minutes, starting within the video")
        if not video_import and not (1 <= max_clips <= 20):
            raise ReplayError("max clips must be 1-20")
        self.settings = settings
        self.minutes = minutes
        self.start = 0 if video_import else start_minute * 60
        self.max_clips = None if video_import else max_clips
        self.post = False if video_import else post
        self.log = log
        self.svc = service
        self.work = settings.data_dir / "replay" / f"{self.platform}_{self.vid}"
        self.stop_event = threading.Event()
        self._proc: Optional[subprocess.Popen] = None
        self.step = ""
        self.progress = 0.0
        self.signal = ""
        self.results: List[dict] = []
        self.meta: dict = {}

    def cancel(self):
        self.stop_event.set()
        procs.terminate(self._proc)

    def _check(self):
        if self.stop_event.is_set():
            raise ReplayError("cancelled")

    def _run_tool(self, cmd: List[str], timeout: float) -> Tuple[int, bytes, bytes]:
        """Run yt-dlp/ffmpeg so that cancel() can stop it."""
        self._check()
        if self.video_import and cmd[0] == procs.ytdlp():
            cmd = [*procs.guarded_ytdlp(), "--ignore-config", *cmd[1:]]
        self._proc = procs.popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            out, err = self._proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            procs.terminate(self._proc)
            raise ReplayError("timed out")
        self._check()
        return self._proc.returncode, out or b"", err or b""

    # ------------------------------------------------------------------ steps
    def fetch_meta(self) -> dict:
        self.step = "reading the video's details"
        rc, out, err = self._run_tool([procs.ytdlp(), "--no-warnings", "--skip-download",
                                       "--no-playlist", "-J", self.url], timeout=300)
        if rc != 0:
            raise ReplayError("could not read the video: " + err.decode("utf-8", "replace")[-300:])
        data = json.loads(out.decode("utf-8", "replace") or "{}")
        status = str(data.get("live_status") or "")
        if data.get("is_live"):
            status = "is_live"
        self.meta = {
            "title": str(data.get("title") or ""),
            "channel": str(data.get("channel") or data.get("uploader") or ""),
            "handle": str(data.get("uploader_id") or data.get("channel_id") or ""),
            "duration": float(data.get("duration") or 0),
            "live_status": status,
            "has_chat": "live_chat" in (data.get("subtitles") or {}),
            "heatmap": data.get("heatmap") or [],
        }
        if status in ("is_live", "is_upcoming", "post_live"):
            raise ReplayError("this stream is still live or has not started: watch it live instead")
        if self.upload_only and status == "was_live":
            raise ReplayError("video watcher skipped a finished livestream")
        return self.meta

    def fetch_chat(self, end: float) -> List[tuple]:
        """YouTube chat replay messages up to ``end`` seconds, as (offset, ChatMessage)."""
        self.step = "downloading the chat replay"
        chat_dir = self.work / "chat"
        chat_dir.mkdir(parents=True, exist_ok=True)
        for old in chat_dir.glob("c*"):
            try:
                old.unlink()
            except OSError:
                pass  # still held by an earlier run's process; this run uses its own name
        stem = f"c{int(time.time() * 1000)}"
        self._proc = procs.popen(
            [procs.ytdlp(), "--no-warnings", "--quiet", "--skip-download", "--no-playlist",
             "--write-subs", "--sub-langs", "live_chat", "-o", str(chat_dir / f"{stem}.%(ext)s"),
             self.url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        msgs: List[tuple] = []
        pending = b""
        fh = None
        last_off = 0.0
        last_report = 0.0
        try:
            while True:
                self._check()
                if fh is None:
                    for p in (chat_dir / f"{stem}.live_chat.json.part",
                              chat_dir / f"{stem}.live_chat.json"):
                        if p.exists():
                            fh = open(p, "rb")
                            break
                chunk = fh.read() if fh else b""
                if chunk:
                    pending += chunk
                    *lines, pending = pending.split(b"\n")
                    for ln in lines:
                        for off, m in parse_youtube_replay_line(ln.decode("utf-8", "replace")):
                            last_off = max(last_off, off)
                            if off <= end + 30:
                                msgs.append((off, m))
                    self.progress = min(1.0, last_off / max(end, 1))
                    if time.time() - last_report > 15:
                        last_report = time.time()
                        self.log(f"replay: chat downloaded up to {_mmss(last_off)} of {_mmss(end)} "
                                 f"({len(msgs)} messages)")
                if last_off > end + 30:
                    break
                if self._proc.poll() is not None and not chunk:
                    break
                time.sleep(0.5)
        finally:
            if fh:
                fh.close()
            procs.terminate(self._proc)
        return msgs

    def fetch_loudness(self, a: float, b: float) -> List[tuple]:
        """Per-second loudness of [a, b] from the audio track only, as (offset, rms_db)."""
        self.step = "downloading the audio"
        self.progress = 0.0
        audio_dir = self.work / "audio"
        audio_dir.mkdir(parents=True, exist_ok=True)
        if self.platform == "youtube":
            # YouTube serves a byte range of a section very slowly (minutes for 15 min of
            # audio), the whole 48 kbps track fast (about 1 min for 3 h, ~60 MB): fetch it
            # once, keep it, and measure any window of the video from it.
            full = next((p for p in audio_dir.glob("full.*") if p.suffix in (".m4a", ".webm", ".mp4")),
                        None)
            if full is None:
                rc, _, err = self._run_tool(
                    [procs.ytdlp(), "--no-warnings", "--quiet", "--no-playlist",
                     "-f", "139/worstaudio[ext=m4a]/worstaudio",
                     "-o", str(audio_dir / "full.%(ext)s"), self.url], timeout=1800)
                full = next((p for p in audio_dir.glob("full.*")
                             if p.suffix in (".m4a", ".webm", ".mp4")), None)
                if rc != 0 or full is None:
                    raise ReplayError("could not download the audio: "
                                      + err.decode("utf-8", "replace")[-300:])
            src, seek, temp = full, ["-ss", f"{a:.0f}", "-to", f"{b:.0f}"], []
        else:
            stem = f"a{int(time.time() * 1000)}"
            rc, _, err = self._run_tool(
                [procs.ytdlp(), "--no-warnings", "--quiet", "--no-playlist",
                 "-f", "Audio_Only/audio_only/bestaudio[abr<=160]/bestaudio/worst",
                 "--download-sections", f"*{a:.0f}-{b:.0f}", "-o", str(audio_dir / f"{stem}.%(ext)s"),
                 self.url], timeout=1800)
            temp = [p for p in audio_dir.glob(f"{stem}.*") if not p.name.endswith(".part")]
            if rc != 0 or not temp:
                raise ReplayError("could not download the audio: " + err.decode("utf-8", "replace")[-300:])
            src, seek = temp[0], []
        self.step = "measuring loudness"
        cmd = [procs.ffmpeg(), "-v", "error", "-nostdin", *seek, "-i", str(src), "-vn", "-af",
               "aresample=16000,asetnsamples=n=16000:p=0,astats=metadata=1:reset=1,"
               "ametadata=mode=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"]
        rc, out, err = self._run_tool(cmd, timeout=900)
        for f in temp:
            f.unlink(missing_ok=True)
        series = [(a + t, db) for t, db in parse_rms_lines(out.decode("utf-8", "replace"))]
        if rc != 0:
            raise ReplayError("could not measure audio: " + err.decode("utf-8", "replace")[-300:])
        if self.video_import:
            return series
        if len(series) < 60:
            raise ReplayError("the audio was too short or silent to find moments in")
        return series

    def fetch_viewer_clips(self, start: float, end: float) -> List[Peak]:
        try:
            import twitch_signals  # in the OpenShorts repo next to this package
        except ImportError:
            return []
        if not twitch_signals._credentials()[0]:
            return []
        self.step = "reading the clips viewers made"
        clips = twitch_signals.fetch_viewer_clips(self.vid)
        return clips_to_peaks(clips, start, end)

    def fetch_section(self, a: float, b: float, out_dir: Path) -> Path:
        """Only [a, b] seconds of the video, 720p or less, as one file."""
        out_dir.mkdir(parents=True, exist_ok=True)
        for old in out_dir.glob("raw.*"):
            old.unlink(missing_ok=True)
        fmt = ("bv*[height<=720][vcodec^=avc1]+ba[ext=m4a]/bv*[height<=720]+ba/"
               "b[height<=720]/720p60/720p/best")
        accurate = ["--force-keyframes-at-cuts"] if self.interval is not None else []
        rc, _, err = self._run_tool(
            [procs.ytdlp(), "--no-warnings", "--quiet", "--no-playlist", "-f", fmt,
             "--download-sections", f"*{a:.2f}-{b:.2f}", *accurate, "--merge-output-format", "mp4",
             "-o", str(out_dir / "raw.%(ext)s"), self.url], timeout=900)
        files = [p for p in out_dir.glob("raw.*") if p.suffix in (".mp4", ".mkv", ".webm", ".ts")]
        if rc != 0 or not files:
            raise ReplayError("downloading the clip failed: " + err.decode("utf-8", "replace")[-300:])
        return files[0]

    def find_moments(self, start: float, end: float) -> List[Peak]:
        msgs: List[tuple] = []
        loud: Optional[List[tuple]] = None
        extra: List[Peak] = []
        used = []
        if self.video_import:
            extra = self.heatmap_peaks(start, end)
            if extra:
                used.append("most replayed")
        if self.platform == "youtube" and self.meta.get("has_chat"):
            msgs = self.fetch_chat(end)
            self._check()
            self.log(f"replay: {len(msgs)} chat messages in the window")
            if msgs:
                used.append("chat")
        if self.platform == "twitch":
            extra = self.fetch_viewer_clips(start, end)
            if extra:
                used.append(f"{len(extra)} viewer clips")
                self.log(f"replay: {len(extra)} moments viewers clipped in the window")
        if self.video_import or not msgs:
            why = ("Twitch VODs have no chat replay" if self.platform == "twitch"
                   else "this video has no chat replay")
            self.log(f"replay: {why}; finding the moments by loudness instead")
            # 5 minutes of run-up, so the first moments are judged against a real baseline
            try:
                loud = self.fetch_loudness(max(0.0, start - 300), end)
                used.append("loudness")
            except ReplayError as exc:
                if self.stop_event.is_set() or not (msgs or extra):
                    raise
                self.log(f"replay: audio unavailable, using other signals: {exc}")
        self.signal = " + ".join(used)
        self.step = "finding the moments"
        gap = max(MIN_GAP, self.settings.min_seconds,
                  min(self.settings.max_seconds, self.settings.pre_seconds + self.settings.post_seconds))
        peaks = find_peaks(msgs, self.settings, start, end, self.max_clips, loud, extra,
                           gap=gap if self.video_import else MIN_GAP)
        if peaks or not self.video_import or self.upload_only:
            return peaks
        self._check()
        center = (start + end) / 2
        candidates = loudness_peaks(loud or [], start, end, None, floor=0.01, min_samples=3)
        best = max(candidates, key=lambda p: (p.score, -abs(p.ts - center)), default=None)
        if best:
            self.signal = "fallback: strongest available audio"
            fallback = replace(best, reason="below-threshold audio fallback")
        else:
            self.signal = "fallback: middle of video (no reliable signal)"
            fallback = Peak(center, 0, 0, 0, 0, reason="no-signal fallback")
        self.log(f"replay: no strong moments; manual import using {self.signal}")
        return [fallback]

    def heatmap_peaks(self, start: float, end: float) -> List[Peak]:
        samples = []
        for item in self.meta.get("heatmap", []):
            try:
                moment = (float(item["start_time"]) + float(item["end_time"])) / 2
                score = float(item["value"])
            except (TypeError, KeyError, ValueError):
                continue
            if start <= moment < end and 0 <= score <= 1:
                samples.append((moment, score))
        samples.sort()
        from itertools import groupby
        runs = [list(group) for _, group in groupby(samples, key=lambda row: row[1])]
        peaks = []
        for i, run in enumerate(runs):
            moment, score = run[len(run) // 2]
            left = runs[i - 1][0][1] if i else -1
            right = runs[i + 1][0][1] if i + 1 < len(runs) else -1
            if score >= 0.4 and score > max(left, right) and (len(runs) > 1 or len(samples) == 1):
                peaks.append(Peak(moment, round(10 * score, 2), 0, 0, 0, reason="most replayed"))
        return peaks

    # -------------------------------------------------------------------- run
    def run(self) -> List[dict]:
        from .service import ClipJob, LiveInfo, Service
        s = self.settings
        if self.post is not None:
            mode = "legacy" if self.post else (
                "telegram_review" if self.video_import and s.delivery_mode == "telegram_review" else "manual")
            s = replace(s, post_enabled=bool(self.post), delivery_mode=mode)
            self.settings = s
        svc = self.svc or Service(s, log=self.log if hasattr(self.log, "since") else None)
        svc.settings = s
        meta = self.fetch_meta()
        if self.video_import and (not math.isfinite(meta["duration"]) or meta["duration"] <= 0):
            raise ReplayError("YouTube did not report a valid video duration")
        dur = meta["duration"] or (self.start + self.minutes * 60)
        if self.start >= dur:
            raise ReplayError(f"the video is only {_mmss(dur)} long")
        end = dur if self.video_import else min(dur, self.start + self.minutes * 60)
        self.log(f"replay: {meta['channel']} - {meta['title'][:80]} ({_mmss(dur)}, {self.platform}), "
                 f"analysing {_mmss(self.start)}-{_mmss(end)}")
        if self.interval is not None:
            a, b = self.interval
            if not meta["duration"] or b > dur:
                raise ReplayError("manual range exceeds the video duration")
            self.signal = "manual range"
            peaks = [Peak((a + b) / 2, 0, 0, 0, 0, reason="manual range")]
        else:
            peaks = self.find_moments(self.start, end)
        if not peaks:
            self.log(f"replay: no strong moments found ({self.signal}).")
            self.step = "done"
            return []
        self.log(f"replay: {len(peaks)} moment(s) from {self.signal} at "
                 + ", ".join(f"{_mmss(p.ts)} ({p.score})" for p in sorted(peaks, key=lambda p: p.ts)))
        handle = re.sub(r"[^A-Za-z0-9_.@-]", "", meta["handle"])[:60] or self.vid
        ch = Channel(platform=self.platform, name=handle, label=meta["channel"][:60] or handle)
        info = LiveInfo(True, title=meta["title"],
                        category=("YouTube video" if self.video_import else
                                  "YouTube replay" if self.platform == "youtube" else "Twitch VOD"))
        for i, peak in enumerate(sorted(peaks, key=lambda p: p.ts), 1):
            self._check()
            if self.interval is not None:
                clip_start, clip_end = self.interval
            elif self.signal.startswith("fallback:"):
                length = min(dur, s.max_seconds, s.pre_seconds + s.post_seconds)
                clip_start = max(0.0, min(peak.ts - s.pre_seconds, dur - length))
                clip_end = min(dur, clip_start + length)
            else:
                clip_start = max(0.0, peak.ts - s.pre_seconds)
                clip_end = min(dur, clip_start + min(s.max_seconds, s.pre_seconds + s.post_seconds))
                if clip_end - clip_start < s.min_seconds:
                    clip_start = max(0.0, clip_end - s.min_seconds)
            classic = None
            if self.interval is None and s.director_enabled and s.ai_provider == "local":
                classic = clip_start, clip_end
                clip_start, clip_end = director.window(clip_start, clip_end, s, dur)
            a, b = max(0.0, clip_start - MARGIN), min(dur, clip_end + MARGIN)
            self.step = f"clip {i}/{len(peaks)}: downloading {_mmss(clip_start)}-{_mmss(clip_end)}"
            self.log(f"replay: {self.step}")
            job_dir = s.data_dir / "clips" / f"replay_{self.platform}_{self.vid}" / f"t{int(peak.ts):06d}"
            if self.upload_only:
                job_dir = s.data_dir / "clips" / f"video_youtube_{self.vid}" / f"watch_{round(peak.ts * 1000)}"
                if (job_dir / "meta.json").is_file() and (job_dir / "short.mp4").is_file():
                    self.results.append({"at": peak.ts, "file": str(job_dir / "short.mp4"), "dir": str(job_dir)})
                    continue
            elif self.video_import:
                job_dir = s.data_dir / "clips" / f"video_youtube_{self.vid}" / f"t{time.time_ns()}"
            try:
                raw = self.fetch_section(a, b, job_dir)
            except ReplayError as e:
                if self.stop_event.is_set():
                    raise
                self.log(f"replay: {e}")
                self.results.append({"at": peak.ts, "error": str(e)})
                continue
            job = ClipJob(ch, replace(peak, ts=time.time()), clip_start, clip_end, info,
                          s.style_for(ch, i), job_dir, desc_style=s.description_for(i),
                          moment=peak.ts - clip_start,
                          source_type="video" if self.video_import else "replay",
                          source_url=self.url, source_offset=clip_start,
                          facecam=SourceFacecam(reject_moved=True) if self.video_import else None,
                          automatic=self.upload_only, classic_range=classic)
            job.raw, job.raw_offset, job.duration = raw, clip_start - a, clip_end - clip_start
            self.step = f"clip {i}/{len(peaks)}: making the Short"
            try:
                out = svc.process(job)
                self.results.append({"at": peak.ts, "file": str(out), "dir": str(job_dir)})
            except Exception as e:
                self.log(f"replay: clip {i} failed: {e}")
                self.results.append({"at": peak.ts, "error": str(e)})
        self.step = "done"
        ok = sum(1 for r in self.results if "file" in r)
        self.log(f"replay: done, {ok} Short(s) made")
        return self.results


def _mmss(sec: float) -> str:
    sec = int(max(0, sec))
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
