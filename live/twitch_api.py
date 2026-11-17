"""
Optional second signal: clips that *viewers* make on Twitch. When people press "Clip", something just happened.

Needs a free Twitch application (dev.twitch.tv -> Your Console -> Register an application) and its
TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET. Without them this module stays off and only chat is used.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import time
import urllib.parse
import urllib.request


class ViewerClips:
    def __init__(self, channel: str, log=print):
        self.client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
        self.secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
        self.channel = channel.lower()
        self.log = log
        self.enabled = bool(self.client_id and self.secret)
        self._token, self._token_exp, self._broadcaster = None, 0.0, None
        self._seen: set[str] = set()

    def _get_token(self) -> str:
        if self._token and time.time() < self._token_exp - 300:
            return self._token
        data = urllib.parse.urlencode({"client_id": self.client_id, "client_secret": self.secret,
                                       "grant_type": "client_credentials"}).encode()
        with urllib.request.urlopen(urllib.request.Request("https://id.twitch.tv/oauth2/token", data), timeout=20) as r:
            body = json.loads(r.read())
        self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return self._token

    def _api(self, path: str, params: dict) -> dict:
        url = "https://api.twitch.tv/helix/" + path + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Client-Id": self.client_id, "Authorization": "Bearer " + self._get_token()})
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())

    def poll(self, since: float) -> list[tuple[float, str]]:
        """New viewer clips created after `since`: [(moment wall-clock, clip id)]. A viewer clip shows the
        ~`duration` seconds before it was made, so the moment is a little before its creation time."""
        if not self.enabled:
            return []
        try:
            if not self._broadcaster:
                users = self._api("users", {"login": self.channel}).get("data") or []
                if not users:
                    self.enabled = False
                    return []
                self._broadcaster = users[0]["id"]
            started = dt.datetime.fromtimestamp(since - 60, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            data = self._api("clips", {"broadcaster_id": self._broadcaster, "started_at": started, "first": 50}).get("data") or []
        except Exception as e:
            self.log(f"[viewer-clips] skipped ({str(e)[:120]})")
            return []
        out = []
        for c in data:
            if c["id"] in self._seen:
                continue
            self._seen.add(c["id"])
            made = dt.datetime.fromisoformat(c["created_at"].replace("Z", "+00:00")).timestamp()
            if made < since:
                continue
            out.append((made - min(float(c.get("duration") or 30), 30) / 3, c["id"]))
        return out
