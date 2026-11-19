"""What the settings page shows: every option with its label, help text and limits.

The page builds its form from ``schema()`` and sends back the raw TOML-shaped
dict; ``config.from_dict`` stays the only validator, so the page can never
save a config the watcher would refuse.
"""
from __future__ import annotations

from typing import Any, Dict, List

from .config import (CAPTION_STYLES, DESCRIPTION_STYLES, PLATFORMS, POST_PLATFORMS, VIA_API,
                     VIA_OTHER, WHISPER_MODELS, Settings)

QUALITIES = ["720p60,720p,best", "1080p60,1080p,best", "720p,480p,best", "480p,360p,best"]


def _f(key, kind, label, help="", **extra) -> Dict[str, Any]:
    return {"key": key, "type": kind, "label": label, "help": help, **extra}


def schema() -> Dict[str, Any]:
    d = Settings()
    sections = [
        {"id": "general", "title": "Recording", "fields": [
            _f("max_live_channels", "int", "Channels recorded at once", "Each 720p60 stream needs about "
               "3.5 Mbit/s. A stream the connection cannot keep up with steps itself down in quality.",
               min=1, max=10, default=d.max_live_channels),
            _f("quality", "choice", "Recording quality", "First choice; the watcher steps down by itself "
               "when it falls behind.", choices=QUALITIES, default=d.quality),
            _f("buffer_seconds", "int", "Rolling buffer (seconds)", "How much of each live stream is kept "
               "on disk. About 260 MB per 10 minutes at 720p60.", min=120, max=3600, default=d.buffer_seconds),
            _f("check_interval", "int", "Live check every (seconds)", "How often each channel is asked "
               "whether it is live.", min=30, max=3600, default=d.check_interval),
            _f("data_dir", "text", "Data folder", "Clips, log and buffer. Relative to the config file.",
               default="livewatch_data"),
            _f("keep_raw_clips", "bool", "Keep the raw cut", "Keep raw.ts next to each Short (for "
               "re-editing).", default=d.keep_raw_clips),
        ]},
        {"id": "clip", "title": "Clip length", "fields": [
            _f("pre_seconds", "float", "Seconds before the moment", "The setup. Out-of-context clips need "
               "some.", min=3, max=120, default=d.pre_seconds),
            _f("post_seconds", "float", "Seconds after the moment", "The reaction.", min=0, max=60,
               default=d.post_seconds),
            _f("min_seconds", "float", "Shortest clip (seconds)", min=5, max=180, default=d.min_seconds),
            _f("max_seconds", "float", "Longest clip (seconds)", "The before and after windows together must fit within this limit.",
               min=10, max=180, default=d.max_seconds),
            _f("cooldown_seconds", "float", "Pause between live clips from one channel (seconds)", min=10, max=3600,
               default=d.cooldown_seconds),
            _f("max_clips_per_hour", "int", "Max live clips per hour per channel", min=1, max=60,
               default=d.max_clips_per_hour),
        ]},
        {"id": "detect", "title": "Peak detection", "fields": [
            _f("threshold", "float", "How strong must a moment be?", "Lower = more candidates. Higher = fewer. "
               "Applies to live detection and audio/chat selection for video imports.", min=1, max=20,
               step=0.1, default=d.threshold),
            _f("warmup_seconds", "float", "Warm-up (seconds)", "Time spent learning each stream's normal "
               "chat speed before clipping.", min=0, max=1800, default=d.warmup_seconds),
            _f("min_chat_rate", "float", "Slowest chat that counts (msg/s)", "Chats slower than this "
               "rely on loudness spikes.", min=0, max=100, step=0.1, default=d.min_chat_rate),
            _f("audio_weight", "float", "Loudness weight", "0 ignores loudness. Kick relies on it "
               "(no chat reader).", min=0, max=5, step=0.1, default=d.audio_weight),
        ]},
        {"id": "style", "title": "Look and captions", "fields": [
            _f("caption_style", "choice", "Hook and title style", "Sets the tone for the hook, title and generated copy. "
               "This does not change the look of the subtitles.",
               choices=list(CAPTION_STYLES) + ["rotate"], default=d.caption_style),
            _f("description_style", "choice", "Social caption style", "Applies to Instagram, TikTok, "
               "Facebook and Threads, not YouTube.", choices=list(DESCRIPTION_STYLES) + ["rotate"],
               default=d.description_style),
            _f("captions", "bool", "Word-by-word captions", "Show spoken words on the video. Requires speech recognition.", default=d.captions),
            _f("hook", "bool", "Hook text on top", "Show a short headline at the start of the clip.", default=d.hook),
            _f("whisper_model", "choice", "Speech recognition", "Used for subtitles and the AI transcript. "
               "Choose a larger model for better recognition, at the cost of processing time. Use the "
               "Word-by-word captions switch if you only want to hide subtitles.", choices=list(WHISPER_MODELS),
               default=d.whisper_model),
            _f("fps", "int", "Frames per second", min=24, max=60, default=d.fps),
            _f("encoder", "choice", "Video encoder", "Automatic tries NVIDIA, then Intel Quick Sync, then CPU.",
               choices=["auto", "qsv", "x264"], default=d.encoder),
        ]},
        {"id": "music", "title": "Background music", "fields": [
            _f("mode", "choice", "Add music", "Uses tracks you add in the Music tab. Only add music you have permission to use.",
               choices=["off", "always", "quiet"], default=d.music_mode),
            _f("volume_db", "float", "Music volume (dB)", "-20 is a soft bed under speech; it dips "
               "further whenever the streamer is loud.", min=-40, max=0, step=1,
               default=d.music_volume_db),
            _f("folder", "text", "Music folder", "Empty = livewatch_data/music.", default=""),
        ]},
        {"id": "ai", "title": "AI review and copy", "fields": [
            _f("provider", "choice", "AI for scoring and text", "Local AI needs Ollama running with the selected model. "
               "Gemini needs an API key in Accounts & keys. Without AI, ClipperHub uses templates instead.",
               choices=["local", "gemini", "off"], default=d.ai_provider),
            _f("director_enabled", "bool", "AI Director", "Use local Ollama to plan cuts and layouts. Falls back to classic clipping if no usable edit is found.", default=d.director_enabled),
            _f("director_mode", "choice", "Director intervention", choices=["adaptive", "cuts"], default=d.director_mode),
            _f("director_context", "float", "Extra context before and after (seconds)", min=0, max=60, default=d.director_context),
            _f("director_target", "float", "Target edit length (seconds)", min=5, max=180, default=d.director_target),
            _f("local_url", "text", "Local OpenAI-compatible base URL", default=d.local_url),
            _f("local_model", "text", "Local model name", "Use the exact model name installed in Ollama or your local server.",
               default=d.local_model),
            _f("local_vision_model", "text", "Local vision model", "Optional: use sampled video frames to write action-specific copy.",
               default=d.local_vision_model),
            _f("model", "text", "Gemini model", "Needs GEMINI_API_KEY (Keys section). Without a key, "
               "templates and chat phrases are used.", default=d.ai_model),
        ]},
        {"id": "post", "title": "Posting", "fields": [
            _f("delivery_mode", "choice", "How should clips be delivered?",
               "Review in ClipperHub, approve from Telegram, or get a daily TikTok shortlist. "
               "Manual posting and scheduling are available in every mode.",
               choices=["manual", "telegram_review", "top_daily", "legacy"], default=d.delivery_mode),
            _f("top_n", "int", "Most clips to send per day", min=1, max=20, default=d.top_n),
            _f("top_min_quality", "float", "Minimum quality for daily Telegram", min=1, max=10, step=0.1,
               default=d.top_min_quality),
            _f("top_time", "text", "Daily cutoff (computer local time)", "HH:MM; review the last 24-hour window at cutoff.",
               default=d.top_time),
            _f("enabled", "bool", "Enable legacy automatic posting", "Only applies in legacy mode.", default=d.post_enabled),
            _f("platforms", "multi", "Default posting platforms", "You can change the selection for individual clips. "
               "Telegram approval schedules YouTube only.", choices=list(POST_PLATFORMS), default=d.post_platforms),
            _f("youtube_via", "choice", "YouTube posting method", "Set up direct posting under Connect your accounts.",
               choices=list(VIA_API), default=d.youtube_via),
            _f("youtube_altered_content", "choice", "YouTube: Altered content",
               "Applies to all future direct YouTube uploads. Choose Yes if they contain material "
               "that YouTube requires you to disclose.",
               choices=["no", "yes"], default=d.youtube_altered_content),
            _f("instagram_via", "choice", "Instagram posting method", "Set up direct posting under Connect your accounts.",
               choices=list(VIA_API), default=d.instagram_via),
            _f("others_via", "choice", "TikTok and other networks", "With Telegram, you receive the clip and its caption, then post it yourself.",
               choices=list(VIA_OTHER), default=d.others_via),
            _f("upload_post_user", "text", "Upload-Post profile name",
               "Only used by networks that use Upload-Post.",
               default=""),
            _f("min_gap_minutes", "int", "Minutes between posts", "YouTube scheduling also counts the last successful upload "
               "through ClipperHub. Post now skips the wait.", min=0, max=1440, default=d.min_gap_minutes),
            _f("daily_cap", "int", "Daily limit for legacy auto-posting", min=1, max=200, default=d.daily_cap),
            _f("skip_spoilers", "bool", "Hold clips marked as spoilers", "Applies to the daily Telegram shortlist "
               "and legacy auto-posting. It does not block manual posting.", default=d.skip_spoilers),
            _f("base_hashtags", "list", "Hashtags on every post", "Comma separated, without #.",
               default=d.base_hashtags),
        ]},
    ]
    channel = [
        _f("platform", "choice", "Platform", choices=list(PLATFORMS), default="twitch"),
        _f("name", "text", "Channel", "Twitch/Kick login, or @handle for YouTube.", default=""),
        _f("live", "bool", "Live", "Monitor livestreams.", default=True),
        _f("video", "bool", "Video", "Monitor new YouTube uploads.", default=False),
        _f("label", "text", "Display name", "Optional; used in titles.", default=""),
        _f("game_filter", "list", "Only while playing", "Comma separated; empty = always. Ignored on "
           "YouTube.", default=[]),
        _f("tags", "list", "Extra hashtags", "Comma separated, without #.", default=[]),
        _f("caption_style", "choice", "Style for this channel", "Empty = the global style.",
           choices=[""] + list(CAPTION_STYLES), default=""),
        _f("language", "text", "Spoken language", "auto, or a code like en, es, fr.", default="auto"),
    ]
    secrets = [
        _f("GEMINI_API_KEY", "secret", "Gemini API key", "Free at Google AI Studio. Better hooks and "
           "titles, spoiler and quality checks."),
        _f("UPLOADPOST_API_KEY", "secret", "Upload-Post API key (optional)", "Only needed if you choose Upload-Post."),
        _f("TWITCH_CLIENT_ID", "secret", "Twitch Client ID (optional)", "From a free app at "
           "dev.twitch.tv/console (any name, redirect http://localhost). Lets Twitch replays use "
           "the moments viewers clipped, the best signal for VODs."),
        _f("TWITCH_CLIENT_SECRET", "secret", "Twitch Client Secret (optional)", "From the same "
           "Twitch app (New Secret)."),
    ]
    return {"sections": sections, "channel": channel, "secrets": secrets}


SECRET_KEYS: List[str] = ["GEMINI_API_KEY", "UPLOADPOST_API_KEY", "TWITCH_CLIENT_ID",
                          "TWITCH_CLIENT_SECRET", "YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET",
                          "YOUTUBE_REFRESH_TOKEN", "INSTAGRAM_ACCESS_TOKEN", "TELEGRAM_BOT_TOKEN",
                          "TELEGRAM_CHAT_ID"]
# set up from the Posting accounts panel, not typed in the API keys list
ACCOUNT_KEYS = ["YOUTUBE_CLIENT_ID", "YOUTUBE_CLIENT_SECRET", "INSTAGRAM_ACCESS_TOKEN",
                "TELEGRAM_BOT_TOKEN"]
