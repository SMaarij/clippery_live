"""
Finds moments worth clipping from chat activity.

Raw message counts are a weak signal: sub trains, raids, giveaways and emote spam all spike the count without
anything happening on screen, and a huge chat is already saturated so its volume barely moves. So the score for
each window is the number of *different people* who wrote, boosted by how many of those messages were reactions
(laughter, hype, "clip it", question-mark spam):

    score = unique chatters in the last `window` seconds * (1 + hype_weight * share of those people reacting)

A spike is a score of at least `ratio` x the median score of the last `baseline` seconds (and at least
`min_unique` different people), outside a cooldown after the previous spike. Commands ("!uptime") and known bots
are ignored; sub/raid notices never reach this class.

Chat reacts a few seconds *after* the thing happens, so the reported moment is set back by `reaction_delay`
and by half the window (the burst is spread across it).
"""
from __future__ import annotations

import re
import statistics
from collections import deque
from dataclasses import dataclass, field

BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "soundalerts", "wizebot", "botrixoficial",
        "sery_bot", "kofistreambot", "streamstickers", "commanderroot", "pokemoncommunitygame", "own3d", "blerp",
        "creatisbot", "tangiabot", "frostytoolsdotcom", "lumiastream", "songlistbot", "deepbot"}

# lower-cased whole tokens (emotes are case-sensitive on Twitch, but spellings vary, so compare lower-case)
_HYPE_TOKENS = {
    "kekw", "lul", "lulw", "omegalul", "lmao", "lmfao", "lol", "xd", "icant", "aware", "pog", "pogu", "pogchamp",
    "poggers", "pogslide", "w", "ww", "www", "wwww", "monkas", "monkaw", "omg", "wtf", "holy", "clip", "clipit",
    "clipped", "bruh", "wow", "insane", "dead", "nooo", "noooo", "noway", "pepelaugh", "kekl", "elegiggle",
    "residentsleeper", "lolw", "sadge", "hahaha", "hahahaha", "jebaited",
}
_HYPE_PHRASES = ("clip it", "clip that", "no way", "holy shit", "oh my god", "what the", "let's go", "lets go")
_HYPE_EMOJI = ("\U0001F602", "\U0001F923", "\U0001F480", "\U0001F525", "\U0001F631", "\U0001F633")
_TOKEN = re.compile(r"[a-z0-9']+")

PRESETS = {                      # sensitivity -> (ratio, min_unique)
    "low": (2.8, 12),
    "normal": (2.2, 8),
    "high": (1.8, 5),
}


def is_reaction(text: str) -> bool:
    t = text.lower()
    if any(e in text for e in _HYPE_EMOJI) or any(p in t for p in _HYPE_PHRASES):
        return True
    if t.count("?") >= 3 or t.count("!") >= 4:
        return True
    toks = _TOKEN.findall(t)
    if not toks:
        return False
    if any(tok in _HYPE_TOKENS for tok in toks):
        return True
    # "HAHAHAHA", "LMAOOOO", "WWWWW"
    return any(re.fullmatch(r"(ha){3,}h?|l+m+a+o+|w{3,}|o+m+g+|no{4,}", tok) for tok in toks)


def should_ignore(user: str, text: str) -> bool:
    return (not text) or text.startswith("!") or (user or "").lower() in BOTS


@dataclass
class Spike:
    at: float            # when the spike was detected (wall clock, seconds)
    moment: float        # best guess of when the thing happened on stream
    score: float
    baseline: float
    ratio: float
    unique: int
    reactions: int
    trigger: str = "chat"


@dataclass
class SpikeDetector:
    window: float = 10.0
    baseline: float = 180.0
    tick: float = 2.0
    ratio: float = 2.2
    min_unique: int = 8
    hype_weight: float = 3.0
    cooldown: float = 45.0
    warmup: float = 90.0
    reaction_delay: float = 3.0
    _msgs: deque = field(default_factory=deque)      # (ts, user, is_reaction)
    _scores: deque = field(default_factory=deque)    # (ts, score)
    _first: float | None = None
    _last_spike: float = -1e18

    @classmethod
    def from_settings(cls, settings: dict) -> "SpikeDetector":
        ratio, min_unique = PRESETS.get(settings.get("sensitivity", "normal"), PRESETS["normal"])
        extra = {k: float(settings[k]) for k in ("warmup", "baseline", "cooldown", "window", "reaction_delay") if k in settings}
        return cls(ratio=float(settings.get("ratio", ratio)), min_unique=int(settings.get("min_unique", min_unique)), **extra)

    def add(self, ts: float, user: str, text: str) -> None:
        if should_ignore(user, text):
            return
        if self._first is None:
            self._first = ts
        self._msgs.append((ts, (user or "").lower(), is_reaction(text)))

    def _window_stats(self, now: float) -> tuple[float, int, int]:
        while self._msgs and self._msgs[0][0] < now - self.baseline - self.window:
            self._msgs.popleft()
        users, reacting, n, nreact = set(), set(), 0, 0
        for ts, user, react in self._msgs:
            if now - self.window < ts <= now:
                n += 1
                users.add(user)
                if react:
                    nreact += 1
                    reacting.add(user)
        # share of *people* reacting, not of messages: one person spamming KEKW must not look like a room laughing
        share = (len(reacting) / len(users)) if users else 0.0
        return len(users) * (1.0 + self.hype_weight * share), len(users), len(reacting)

    def step(self, now: float) -> Spike | None:
        """Call every `tick` seconds. Returns a Spike when one starts, else None."""
        score, unique, reactions = self._window_stats(now)
        past = [s for t, s in self._scores if now - self.baseline <= t <= now - self.window]
        self._scores.append((now, score))
        while self._scores and self._scores[0][0] < now - self.baseline - self.window:
            self._scores.popleft()
        if self._first is None or now - self._first < self.warmup or len(past) < 5:
            return None
        base = statistics.median(past)
        floor = max(base, self.min_unique / self.ratio, 1.0)
        if unique < self.min_unique or score < self.ratio * floor or now - self._last_spike < self.cooldown:
            return None
        self._last_spike = now
        moment = now - self.window / 2 - self.reaction_delay
        return Spike(at=now, moment=moment, score=round(score, 1), baseline=round(base, 1),
                     ratio=round(score / floor, 2), unique=unique, reactions=reactions)
