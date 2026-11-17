"""
Reads Twitch chat anonymously over IRC (no account, no token): irc.chat.twitch.tv:6667 as justinfanNNNNN.

Only PRIVMSG (what people type) is passed on. USERNOTICE (subs, raids, gift bombs) is ignored on purpose: those
bursts say nothing about what is happening on screen. The server's own timestamp (tmi-sent-ts) is used when
present, so a slow network on our side does not shift moments.
"""
from __future__ import annotations

import random
import socket
import threading
import time
from dataclasses import dataclass

HOST, PORT = "irc.chat.twitch.tv", 6667


@dataclass
class ChatMessage:
    ts: float
    user: str
    text: str


def _unescape_tag(v: str) -> str:
    return (v.replace("\\s", " ").replace("\\:", ";").replace("\\\\", "\\").replace("\\r", "\r").replace("\\n", "\n"))


def parse_line(line: str) -> dict:
    """Parse one IRC line into {tags, prefix, command, params, trailing}."""
    tags, prefix, trailing = {}, "", ""
    rest = line.rstrip("\r\n")
    if rest.startswith("@"):
        raw, _, rest = rest.partition(" ")
        for kv in raw[1:].split(";"):
            k, _, v = kv.partition("=")
            tags[k] = _unescape_tag(v)
    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
    if " :" in rest:
        rest, trailing = rest.split(" :", 1)
    parts = rest.split()
    return {"tags": tags, "prefix": prefix, "command": parts[0] if parts else "", "params": parts[1:], "trailing": trailing}


def to_message(p: dict, now: float | None = None) -> ChatMessage | None:
    if p["command"] != "PRIVMSG":
        return None
    tags = p["tags"]
    user = tags.get("display-name") or p["prefix"].split("!", 1)[0]
    try:
        ts = int(tags["tmi-sent-ts"]) / 1000.0
    except (KeyError, ValueError):
        ts = now if now is not None else time.time()
    text = p["trailing"]
    if text.startswith("\x01ACTION ") and text.endswith("\x01"):      # /me messages
        text = text[8:-1]
    return ChatMessage(ts=ts, user=user, text=text)


class TwitchChat:
    """Background reader. Calls on_message(ChatMessage) for every chat line; reconnects on its own."""

    def __init__(self, channel: str, on_message, log=print):
        self.channel = channel.lower().lstrip("#")
        self.on_message = on_message
        self.log = log
        self.stop_event = threading.Event()
        self.messages_seen = 0
        self.connected = False
        self._thread = threading.Thread(target=self._run, name="twitch-chat", daemon=True)

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self.stop_event.set()

    def _run(self):
        backoff = 2
        while not self.stop_event.is_set():
            try:
                self._session()
                backoff = 2
            except Exception as e:                                   # network blip, server restart
                self.connected = False
                self.log(f"[chat] disconnected ({e}); reconnecting in {backoff}s")
            if self.stop_event.wait(backoff):
                break
            backoff = min(backoff * 2, 60)

    def _session(self):
        with socket.create_connection((HOST, PORT), timeout=30) as s:
            s.settimeout(300)                                        # Twitch pings every ~5 min
            nick = f"justinfan{random.randint(10000, 99999)}"
            for cmd in ("CAP REQ :twitch.tv/tags twitch.tv/commands", "PASS SCHMOOPIIE", f"NICK {nick}", f"JOIN #{self.channel}"):
                s.sendall((cmd + "\r\n").encode())
            self.connected = True
            self.log(f"[chat] joined #{self.channel} as {nick}")
            buf = b""
            while not self.stop_event.is_set():
                data = s.recv(65536)
                if not data:
                    raise ConnectionError("server closed the connection")
                buf += data
                *lines, buf = buf.split(b"\r\n")
                for raw in lines:
                    line = raw.decode("utf-8", "replace")
                    if line.startswith("PING"):
                        s.sendall(line.replace("PING", "PONG", 1).encode() + b"\r\n")
                        continue
                    p = parse_line(line)
                    if p["command"] == "RECONNECT":
                        raise ConnectionError("server asked to reconnect")
                    msg = to_message(p)
                    if msg:
                        self.messages_seen += 1
                        self.on_message(msg)
