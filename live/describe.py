"""
Upload text for a clip, in the same shape the ClipperyAI website gives its clips:

    title       : a complete, scroll-stopping title (max 100 chars) to paste into a Shorts/TikTok/Reels upload form
    hook        : a short, punchy hook line (max 60 chars) for the on-screen caption / first line of the post
    description : 1-2 sentences about this specific clip (max 200 chars) ending with 2-4 lowercase hashtags

It asks Groq (GROQ_API_KEY, model GROQ_MODEL, default openai/gpt-oss-120b) and grounds the answer in what was
said in the clip plus what chat was typing at that moment. Without a key, or if Groq fails, it builds plain text
from the transcript so a clip never ends up with nothing.
"""
from __future__ import annotations

import json
import os
import re
import time

GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
MAX_TITLE, MAX_HOOK, MAX_DESC = 100, 60, 200


def _clip(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", (text or "")).strip().strip('"')
    text = re.sub(r"\s*[–—]\s*", ", ", text)            # no em/en dashes, same as the website copy
    if len(text) <= limit:
        return text
    cut = text[:limit - 1].rsplit(" ", 1)[0].rstrip(",;:-")
    return cut + "…"


def _tags(channel: str, note: str, platform: str = "Twitch") -> str:
    tags = ["#shorts", "#" + platform.lower()]
    if re.search(r"gta\s*(6|vi)", note or "", re.I):
        tags.append("#gta6")
    elif note:
        word = re.sub(r"[^a-z0-9]", "", note.lower().split()[0]) if note.split() else ""
        if 2 < len(word) < 20:
            tags.append("#" + word)
    ch = re.sub(r"[^a-z0-9]", "", channel.lower())
    if ch:
        tags.append("#" + ch)
    return " ".join(tags[:4])


def fallback(transcript: str, channel: str, note: str = "", platform: str = "Twitch") -> dict:
    """No-LLM text: first sentence of what was said, or a plain line about the moment."""
    said = (transcript or "").strip()
    first = re.split(r"(?<=[.!?])\s+", said)[0] if said else ""
    hook = _clip(first, MAX_HOOK) if first else "Chat went wild here"
    title = _clip(f"{channel}: {first}" if first else f"{channel} {note or 'live'} moment chat couldn't believe", MAX_TITLE)
    tags = _tags(channel, note, platform)
    body = _clip(f"{channel} live{(' (' + note + ')') if note else ''}: {first or 'the moment chat exploded.'}", MAX_DESC - len(tags) - 1)
    return {"title": title, "hook": hook, "description": f"{body} {tags}", "source": "fallback"}


def _prompt(transcript: str, chat_lines: list[str], channel: str, note: str, platform: str = "Twitch") -> str:
    chat = "\n".join(f"- {c}" for c in chat_lines[:40]) or "(no chat captured)"
    return f"""You write upload text for a short vertical clip cut from a {platform} livestream.

Streamer: {channel}
Stream note: {note or "(none)"}

What was said in the clip (speech-to-text, may be empty or messy if it is mostly gameplay):
\"\"\"{transcript.strip() or "(no speech)"}\"\"\"

What chat was typing at that moment (this is why the clip was cut):
{chat}

Return ONE JSON object with:
- "hook": a short, punchy on-screen hook line (max {MAX_HOOK} characters), grounded ONLY in the clip and chat above.
- "title": a complete, scroll-stopping title for this short (max {MAX_TITLE} characters) suitable for pasting into a YouTube Shorts/TikTok/Instagram upload form -- a fuller, more descriptive cousin of hook, not just a repeat of it.
- "description": a 1-2 sentence description of this specific clip (max {MAX_DESC} characters) suitable for a short-form video upload's description field, grounded ONLY in the clip and chat above. End it with 2-4 relevant, lowercase hashtags (e.g. "#shorts #gta6").

Do not use em dashes or en dashes. Do not invent events that are not supported by the speech or chat. Do not quote slurs or insults from chat. Return ONLY the JSON object."""


def describe(transcript: str, chat_lines: list[str], channel: str, note: str = "", log=print, platform: str = "Twitch") -> dict:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        return fallback(transcript, channel, note, platform)
    try:
        import requests
        body = {"model": os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
                "messages": [{"role": "user", "content": _prompt(transcript, chat_lines, channel, note, platform)}],
                "response_format": {"type": "json_object"}, "temperature": 0.7, "max_completion_tokens": 1200}
        data = None
        for i in range(3):
            r = requests.post(GROQ_CHAT_URL, headers={"Authorization": "Bearer " + key}, json=body, timeout=60)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(20, 3 * (i + 1)))
                continue
            r.raise_for_status()
            data = json.loads(r.json()["choices"][0]["message"]["content"])
            break
        if not isinstance(data, dict):
            raise RuntimeError("no answer")
        base = fallback(transcript, channel, note, platform)
        out = {"title": _clip(data.get("title") or base["title"], MAX_TITLE),
               "hook": _clip(data.get("hook") or data.get("hook_text") or base["hook"], MAX_HOOK),
               "description": _clip(data.get("description") or base["description"], MAX_DESC + 40),
               "source": "groq"}
        if "#" not in out["description"]:
            out["description"] = _clip(out["description"], MAX_DESC - 30) + " " + _tags(channel, note, platform)
        return out
    except Exception as e:
        log(f"[describe] Groq failed ({str(e)[:160]}); using plain text")
        return fallback(transcript, channel, note, platform)
