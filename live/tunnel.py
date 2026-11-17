"""
A free Cloudflare WARP tunnel for YouTube (which refuses GitHub's datacenter addresses).

ensure() runs scripts/warp_until_ok.sh: it registers a WARP identity, exposes it as a local SOCKS proxy and checks
that YouTube answers through it; if YouTube refuses that address it takes a fresh identity (a new exit address) and
tries again, up to `tries` times. refresh() does the same when an address gets blocked mid-stream. The proxy always
listens on the same local port, so the address in WARP_SOCKS never changes for the rest of the watcher.
"""
from __future__ import annotations

import os
import subprocess
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "scripts", "warp_until_ok.sh")
CHECK_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"     # any public video: the bot check is per address
_last_refresh = 0.0


def available() -> bool:
    return os.name != "nt" and os.path.exists(SCRIPT)


def ensure(tries: int = 8, log=print) -> bool:
    """Bring the tunnel up (or swap to a fresh address). True when YouTube answered through it."""
    global _last_refresh
    if not available():
        log("[tunnel] WARP needs Linux (GitHub runner); going direct")
        return False
    _last_refresh = time.time()
    try:
        r = subprocess.run(["bash", SCRIPT, CHECK_URL, str(tries)], capture_output=True, text=True, timeout=tries * 120)
    except subprocess.TimeoutExpired:
        log("[tunnel] timed out getting a WARP address")
        return False
    for line in (r.stdout or "").splitlines():
        if line.startswith("try ") or line.startswith("no WARP"):
            log("[tunnel] " + line[:200])
        if line.startswith("WARP_SOCKS="):
            os.environ["WARP_SOCKS"] = line.split("=", 1)[1].strip()
    if r.returncode == 0 and os.environ.get("WARP_SOCKS"):
        return True
    os.environ.pop("WARP_SOCKS", None)
    return False


def refresh(log=print, min_gap: float = 120) -> bool:
    """New WARP address after YouTube started refusing the current one (at most once every `min_gap` s)."""
    if time.time() - _last_refresh < min_gap:
        return bool(os.environ.get("WARP_SOCKS"))
    log("[tunnel] getting a fresh WARP address")
    return ensure(log=log)


def looks_blocked(text: str) -> bool:
    low = (text or "").lower()
    return "confirm you" in low or "not a bot" in low or "sign in to confirm" in low or "429" in low
