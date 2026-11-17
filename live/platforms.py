"""
Where a watch's stream lives.

    twitch   channel = "summit1g"                 -> https://www.twitch.tv/summit1g
    youtube  channel = "@handle"                  -> https://www.youtube.com/@handle/live
             channel = "UCxxxxxxxxxxxxxxxxxxxxxx" -> https://www.youtube.com/channel/UC.../live
             channel = "v=VIDEOID11ch"            -> https://www.youtube.com/watch?v=VIDEOID11ch

YouTube refuses GitHub's datacenter addresses, so YouTube traffic (stream, live check, chat) goes through the WARP
SOCKS proxy that live/tunnel.py brings up (WARP_SOCKS).
"""
from __future__ import annotations

import os

PLATFORMS = ("twitch", "youtube")


def stream_url(platform: str, channel: str) -> str:
    if platform == "youtube":
        if channel.startswith("v="):
            return f"https://www.youtube.com/watch?v={channel[2:]}"
        if channel.startswith("UC") and len(channel) == 24:
            return f"https://www.youtube.com/channel/{channel}/live"
        return f"https://www.youtube.com/{channel if channel.startswith('@') else '@' + channel}/live"
    return f"https://www.twitch.tv/{channel}"


def label(platform: str) -> str:
    return "YouTube" if platform == "youtube" else "Twitch"


def proxy(platform: str) -> str | None:
    return (os.environ.get("WARP_SOCKS") or None) if platform == "youtube" else None


def youtube_video_id(url: str, proxy_url: str | None = None) -> str | None:
    """The id of the live video a channel's /live page points at right now (None if it can't be resolved)."""
    return youtube_live_info(url, proxy_url).get("id")


def youtube_live_info(url: str, proxy_url: str | None = None) -> dict:
    """{"id": live video id, "name": channel display name}, or {} if it can't be resolved."""
    try:
        import yt_dlp
        opts = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "skip_download": True,
                "js_runtimes": {"node": {}}, "remote_components": ["ejs:github"]}
        if proxy_url:
            opts["proxy"] = proxy_url
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False, process=False)
        if not info or len(str(info.get("id") or "")) != 11:
            return {}
        return {"id": info["id"], "name": info.get("channel") or info.get("uploader") or ""}
    except Exception:
        return {}
