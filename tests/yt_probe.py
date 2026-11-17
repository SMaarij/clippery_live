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
    say(f"WARP tries until accepted: {os.environ.get('WARP_TRIES', '-')}")
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
            if ok:
                say("  formats: " + ", ".join(f"{f.get('format_id')}:{f.get('height')}p/{f.get('protocol')}/{'a+v' if f.get('acodec') not in (None, 'none') else 'v'}" for f in fmts)[:300])
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
        # record ~30 s through the same route: yt-dlp CLI, killed after 40 s; MPEG-TS so a cut-off file still plays
        cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "-f", "b[height<=720]/b", "--hls-use-mpegts", "--no-part",
               "--js-runtimes", "node", "--remote-components", "ejs:github", "-o", "rec.ts", URL]
        if proxy:
            cmd[3:3] = ["--proxy", proxy]
        if cookies:
            cmd[3:3] = ["--cookies", cookies]
        if clients:
            cmd[3:3] = ["--extractor-args", "youtube:player_client=" + ",".join(clients)]
        t = time.time()
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            out, _ = p.communicate(timeout=40)
        except subprocess.TimeoutExpired:
            p.kill()
            out, _ = p.communicate()
        tail = " | ".join(l for l in (out or "").splitlines()[-3:])
        files = [f for f in os.listdir(".") if f.startswith("rec.")]
        if files:
            pr = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration:stream=width,height",
                                 "-of", "compact", files[0]], capture_output=True, text=True)
            say(f"recorded {files[0]}: {os.path.getsize(files[0])//1024} KB in {time.time()-t:.0f}s | {pr.stdout.strip()[:160]}")
        else:
            say(f"recorded: nothing | {tail[:200]}")
    chat(win[1] if win else WARP or None)
    with open(os.environ.get("GITHUB_STEP_SUMMARY", os.devnull), "a") as f:
        f.write("### YouTube from a GitHub runner\n```\n" + "\n".join(report) + "\n```\n")


def chat(proxy):
    """Our own innertube chat reader (live/youtube_chat.py) through the same route, for 40 s."""
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import yt_dlp
    from live.youtube_chat import YouTubeChat
    try:
        with yt_dlp.YoutubeDL({**opts(proxy, COOKIES, None), "extract_flat": False}) as y:
            vid = y.extract_info(URL, download=False, process=False).get("id")
    except Exception as e:
        vid = None
        say(f"chat: couldn't resolve the video id ({str(e)[:120]})")
    if not vid:
        return
    got = []
    c = YouTubeChat(vid, got.append, proxy=proxy, log=lambda m: say("  " + m))
    c.start()
    time.sleep(60)
    c.stop()
    users = len({m.user for m in got})
    say(f"chat ({'proxy' if proxy else 'direct'}): {len(got)} messages from {users} people in 60s, e.g. {[m.text[:30] for m in got[:3]]}")


if __name__ == "__main__":
    main()
