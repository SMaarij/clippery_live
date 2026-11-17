"""
Tries every YouTube route a runner has (direct, WARP, cookies; several player clients), then records 25 s with the
first that works and reads chat for 40 s. Prints a short report. Used by .github/workflows/youtube-test.yml.

    python tests/yt_probe.py <youtube live url>
"""
import base64
import os
import subprocess
import sys
import tempfile
import time

URL = sys.argv[1]
WARP = os.environ.get("WARP_SOCKS", "").strip()
COOKIES = None
if os.environ.get("YTDLP_COOKIES_B64", "").strip():
    COOKIES = os.path.join(tempfile.gettempdir(), "yt_cookies.txt")
    with open(COOKIES, "wb") as f:
        f.write(base64.b64decode(os.environ["YTDLP_COOKIES_B64"]))

report = []


def say(line):
    print(line, flush=True)
    report.append(line)


def opts(proxy, cookies, clients):
    o = {"quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 20, "skip_download": True,
         "js_runtimes": {"node": {}}, "remote_components": ["ejs:github"]}
    if proxy:
        o["proxy"] = proxy
    if cookies:
        o["cookiefile"] = cookies
    if clients:
        o["extractor_args"] = {"youtube": {"player_client": clients}}
    return o


def attempts():
    clients = [None, ["tv"], ["web_safari"], ["mweb"]]
    routes = [("direct", None, None)]
    if WARP:
        routes.append(("warp", WARP, None))
    if COOKIES:
        routes.append(("cookies", None, COOKIES))
        if WARP:
            routes.append(("warp+cookies", WARP, COOKIES))
    for name, proxy, cookies in routes:
        for c in clients:
            yield f"{name}/{','.join(c) if c else 'default'}", proxy, cookies, c


def main():
    import yt_dlp
    say(f"routes available: warp={'yes' if WARP else 'no'} cookies={'yes' if COOKIES else 'no'} "
        f"po-token={os.environ.get('POT_MINT', 'n/a')} yt-dlp={yt_dlp.version.__version__}")
    win = None
    for label, proxy, cookies, clients in attempts():
        t = time.time()
        try:
            with yt_dlp.YoutubeDL(opts(proxy, cookies, clients)) as y:
                info = y.extract_info(URL, download=False)
            fmts = [f for f in info.get("formats") or [] if f.get("vcodec") not in (None, "none")]
            ok = bool(fmts)
            say(f"{'OK  ' if ok else 'NONE'} {label}: live={info.get('is_live')} video formats={len(fmts)} ({time.time()-t:.0f}s)")
            if ok and not win:
                win = (label, proxy, cookies, clients, info)
                break
        except Exception as e:
            msg = str(e).replace("\n", " ")
            say(f"FAIL {label}: {msg[msg.find(':', 10)+1:][:140].strip()} ({time.time()-t:.0f}s)")
    if not win:
        say("RESULT: no route could open the stream")
    else:
        label, proxy, cookies, clients, info = win
        say(f"RESULT: '{label}' opens the stream: {info.get('title', '')[:60]}")
        # record 25 s through the same route
        o = opts(proxy, cookies, clients)
        o.update({"skip_download": False, "format": "best[height<=720]/best", "outtmpl": "rec.%(ext)s",
                  "downloader": {"default": "ffmpeg"}, "external_downloader_args": {"ffmpeg_i": ["-t", "25"]}})
        t = time.time()
        try:
            with yt_dlp.YoutubeDL(o) as y:
                y.download([URL])
        except Exception as e:
            say(f"record error: {str(e)[:200]}")
        files = [f for f in os.listdir(".") if f.startswith("rec.")]
        if files:
            p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                                "-of", "compact", files[0]], capture_output=True, text=True)
            say(f"recorded {files[0]}: {os.path.getsize(files[0])//1024} KB in {time.time()-t:.0f}s | {p.stdout.strip()[:160]}")
        else:
            say("recorded: nothing")
    chat(win[1] if win else WARP or None)
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as f:
        f.write("### YouTube from a GitHub runner\n```\n" + "\n".join(report) + "\n```\n")


def chat(proxy):
    """Unofficial live chat (no API key) through the same route."""
    env = dict(os.environ)
    if proxy:
        env["HTTPS_PROXY"] = env["HTTP_PROXY"] = env["ALL_PROXY"] = proxy
    code = r"""
import os, sys, time
from chat_downloader import ChatDownloader
n, t0, first = 0, time.time(), []
try:
    for m in ChatDownloader().get_chat(sys.argv[1], timeout=40):
        n += 1
        if len(first) < 3: first.append((m.get("message") or "")[:40])
        if time.time() - t0 > 40: break
except Exception as e:
    print("error", type(e).__name__, str(e)[:160])
print(f"{n} messages in {time.time()-t0:.0f}s, e.g. {first}")
"""
    try:
        p = subprocess.run([sys.executable, "-c", code, URL], capture_output=True, text=True, timeout=75, env=env)
        say(f"chat ({'proxy' if proxy else 'direct'}): {(p.stdout or p.stderr).strip().splitlines()[-1][:220] if (p.stdout or p.stderr).strip() else 'no output'}")
    except subprocess.TimeoutExpired:
        say("chat: timed out")


if __name__ == "__main__":
    main()
