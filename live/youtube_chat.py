"""
YouTube live chat without an API key: the same endpoints the chat box on youtube.com polls.

    1. GET  /live_chat?v=<id>  -> the page carries the first continuation token, the API key and client version
    2. POST /youtubei/v1/live_chat/get_live_chat  {context, continuation}  -> new messages + next token + wait time

Runs as a thread like TwitchChat and calls on_message(ChatMessage) for every text message. Network goes through
`proxy` (e.g. the WARP SOCKS proxy) when given, because YouTube refuses datacenter addresses.
"""
from __future__ import annotations

import json
import re
import threading
import time

from live.chat import ChatMessage

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/130.0.0.0 Safari/537.36")


def _json_after(html: str, marker: str):
    i = html.find(marker)
    if i < 0:
        return None
    i = html.find("{", i)
    depth, j, in_str, esc = 0, i, False, False
    while j < len(html):
        ch = html[j]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return json.loads(html[i:j + 1])
        j += 1
    return None


def _continuation(conts: list) -> tuple[str | None, int]:
    for c in conts or []:
        for k in ("invalidationContinuationData", "timedContinuationData", "reloadContinuationData"):
            if k in c:
                return c[k].get("continuation"), int(c[k].get("timeoutMs") or 2000)
    return None, 3000


def _text(runs: list) -> str:
    out = []
    for r in runs or []:
        if "text" in r:
            out.append(r["text"])
        elif "emoji" in r:                                    # keep emotes as their shortcut, e.g. :face-with-tears-of-joy:
            sc = r["emoji"].get("shortcuts") or []
            out.append(sc[0] if sc else "")
    return "".join(out).strip()


def parse_actions(actions: list) -> list[ChatMessage]:
    msgs = []
    for a in actions or []:
        item = (a.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        text = _text((r.get("message") or {}).get("runs"))
        user = (r.get("authorName") or {}).get("simpleText") or r.get("authorExternalChannelId") or "?"
        ts = int(r.get("timestampUsec") or time.time() * 1e6) / 1e6
        if text:
            msgs.append(ChatMessage(ts=ts, user=user.lstrip("@").lower(), text=text))
    return msgs


class YouTubeChat(threading.Thread):
    def __init__(self, video_id: str, on_message, proxy: str | None = None, log=print):
        super().__init__(daemon=True)
        self.video_id, self.on_message, self.log = video_id, on_message, log
        self.proxies = {"http": proxy, "https": proxy} if proxy else None
        self._stop = threading.Event()
        self.count = 0

    def stop(self):
        self._stop.set()

    def _start(self, s):
        r = s.get("https://www.youtube.com/live_chat", params={"v": self.video_id, "is_popout": "1"}, timeout=20)
        r.raise_for_status()
        html = r.text
        key = re.search(r'"INNERTUBE_API_KEY":"([^"]+)"', html)
        ver = re.search(r'"INNERTUBE_CLIENT_VERSION":"([^"]+)"', html)
        data = _json_after(html, 'window["ytInitialData"]') or _json_after(html, "ytInitialData")
        if not (key and ver and data):
            raise RuntimeError("live chat page had no chat data (chat off, stream not live, or blocked)")
        conts = (((data.get("contents") or {}).get("liveChatRenderer") or {}).get("continuations"))
        token, _ = _continuation(conts)
        if not token:
            raise RuntimeError("no chat continuation token")
        return key.group(1), ver.group(1), token

    def run(self):
        import requests
        backoff = 2
        while not self._stop.is_set():
            try:
                s = requests.Session()
                s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
                if self.proxies:
                    s.proxies.update(self.proxies)
                key, ver, token = self._start(s)
                self.log("[ytchat] connected")
                backoff = 2
                body = {"context": {"client": {"clientName": "WEB", "clientVersion": ver, "hl": "en"}}}
                while token and not self._stop.is_set():
                    r = s.post("https://www.youtube.com/youtubei/v1/live_chat/get_live_chat",
                               params={"key": key, "prettyPrint": "false"}, json={**body, "continuation": token}, timeout=20)
                    r.raise_for_status()
                    lc = (r.json().get("continuationContents") or {}).get("liveChatContinuation") or {}
                    for m in parse_actions(lc.get("actions")):
                        self.count += 1
                        self.on_message(m)
                    token, wait = _continuation(lc.get("continuations"))
                    self._stop.wait(min(max(wait, 1000), 3000) / 1000)   # the server says 10 s; 3 s keeps spikes timely
                if not token:
                    self.log("[ytchat] chat ended")
                    return
            except Exception as e:
                self.log(f"[ytchat] {str(e)[:200]}; retrying in {backoff}s")
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 60)
