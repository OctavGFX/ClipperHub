"""Streamlink command bridge: YouTube via yt-dlp, other platforms via Streamlink."""
from __future__ import annotations
import json
import os
import shutil
import subprocess
import sys
from urllib.parse import urlparse


def youtube_url(argv):
    for a in argv:
        if not (a.startswith('https://') or a.startswith('http://')):
            continue
        host = (urlparse(a).hostname or '').lower()
        if host == 'youtu.be' or host == 'youtube.com' or host.endswith('.youtube.com'):
            return a
    return ''


def extract(url):
    command = [
        sys.executable, "-m", "yt_dlp",
        "--no-live-from-start", "--no-playlist",
        "--dump-single-json", url,
    ]
    env = os.environ.copy()
    env["PYTHONIOENCODING"] = "utf-8"
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
    result = subprocess.run(
        command, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=100, env=env, creationflags=creationflags,
    )
    if result.returncode != 0:
        # Never print cookies or the JSON payload to user-facing logs.
        error = (result.stderr or "unknown yt-dlp error").strip()
        raise RuntimeError("yt-dlp CLI failed: " + error[-450:])
    try:
        data = json.loads(result.stdout)
    except (ValueError, TypeError) as exc:
        raise RuntimeError("yt-dlp CLI returned invalid JSON") from exc
    if not isinstance(data, dict) or not data.get("formats"):
        raise RuntimeError("yt-dlp CLI returned no video formats")
    return data

def audio_format(info):
    formats = [f for f in info.get('formats') or []
               if f.get('url') and 'm3u8' in (f.get('protocol') or '').lower()
               and f.get('vcodec') == 'none' and f.get('acodec') != 'none']
    return max(formats, key=lambda f: (f.get('abr') or f.get('tbr') or 0), default=None)


def candidates(info):
    formats = [f for f in info.get('formats') or []
               if f.get('url') and 'm3u8' in (f.get('protocol') or '').lower()
               and (f.get('vcodec') or 'none') != 'none']
    muxed = [f for f in formats if (f.get('acodec') or 'none') != 'none']
    return muxed or (formats if audio_format(info) else [])


def best_format(info, argv):
    fmts = candidates(info)
    if not fmts:
        return None
    quality = argv[-1] if argv else '720p60,720p,best'
    max_height = 720
    if '480p' in quality and '720p' not in quality:
        max_height = 480
    if '360p' in quality and '480p' not in quality and '720p' not in quality:
        max_height = 360
    acceptable = [f for f in fmts if 0 < (f.get('height') or 0) <= max_height]
    if not acceptable:
        acceptable = fmts
    return max(acceptable, key=lambda f: (
        1 if str(f.get('vcodec', '')).startswith('avc') else 0,
        f.get('height') or 0,
        f.get('fps') or 0,
        f.get('tbr') or 0,
    ))


def stream_command(info, fmt):
    ffmpeg = os.environ.get('FFMPEG_BIN') or shutil.which('ffmpeg') or 'ffmpeg'
    audio = audio_format(info) if (fmt.get('acodec') or 'none') == 'none' else None
    command = [ffmpeg, '-hide_banner', '-loglevel', 'warning', '-nostdin']
    for selected in ([fmt, audio] if audio else [fmt]):
        headers = selected.get('http_headers') or info.get('http_headers') or {}
        ua = headers.get('User-Agent') or headers.get('user-agent')
        if ua:
            command.extend(['-user_agent', str(ua)])
        extra_headers = ''.join(f'{k}: {v}\r\n' for k, v in headers.items()
                                if k.lower() not in ('user-agent', 'cookie'))
        if extra_headers:
            command.extend(['-headers', extra_headers])
        command.extend(['-i', selected['url']])
    command.extend(['-map', '0:v:0', '-map', '1:a:0' if audio else '0:a:0?',
                    '-c', 'copy', '-f', 'mpegts', 'pipe:1'])
    return command


def main():
    args = sys.argv[1:]
    url = youtube_url(args)
    if not url:
        # Preserve existing Twitch/Kick behavior completely.
        return subprocess.call([sys.executable, '-m', 'streamlink', *args])

    try:
        info = extract(url)
        fmt = best_format(info, args)
        live = info.get('live_status') == 'is_live' or info.get('is_live') is True
        if '--json' in args:
            data = {
                'streams': {'best': {'type': 'hls'}} if (live and fmt) else {},
                'metadata': {
                    'title': info.get('title') or '',
                    'author': info.get('uploader') or info.get('channel') or '',
                    'category': info.get('categories', [''])[0] if info.get('categories') else '',
                },
                'error': '' if (live and fmt) else ('live stream has no usable HLS video and audio formats' if live else 'not live'),
            }
            print(json.dumps(data, ensure_ascii=True))
            return 0
        if '--stdout' not in args:
            print('Usage: youtube_bridge --json URL | --stdout URL quality', file=sys.stderr)
            return 2
        if not live or fmt is None:
            print('YouTube: no usable live HLS audio+video format', file=sys.stderr)
            return 2
        # stdout stays binary: ClipperHub's ffmpeg consumes the MPEG-TS pipe.
        return subprocess.call(stream_command(info, fmt), stdout=sys.stdout.buffer, stderr=sys.stderr)
    except Exception as exc:
        if '--json' in args:
            print(json.dumps({'streams': {}, 'metadata': {}, 'error': str(exc)}))
            return 0
        print(f'YouTube bridge failed: {type(exc).__name__}: {exc}', file=sys.stderr)
        return 1

if __name__ == '__main__':
    raise SystemExit(main())
