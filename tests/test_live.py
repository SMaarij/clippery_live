"""
Tests for the live watcher (live/).

    python tests/test_live.py          # unit tests + a full offline run (needs ffmpeg; ~1-2 minutes)
    python tests/test_live.py --quick  # unit tests only

The offline run replays a generated 16:9 test video "live" (ffmpeg -re) together with a scripted chat: a
quiet baseline and one burst of reactions. It checks that exactly one clip is cut around the burst, that it is
1080x1920 with captions, and that the rolling buffer stays small. Transcription is replaced by a fake so the test
needs no API key or speech model.
"""
import json
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
sys.path.insert(0, ROOT)

from live import clipper, detector  # noqa: E402
from live.buffer import SegmentBuffer  # noqa: E402
from live.chat import parse_line, to_message  # noqa: E402

results = []


def check(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS " if cond else "FAIL ") + name + (f"   -- {detail}" if detail and not cond else ""), flush=True)


def test_reactions():
    yes = ["KEKW", "LMAOOOO", "omg no way", "CLIP IT", "?????", "WWWW", "hahahahaha", "\U0001F602\U0001F602", "that was insane"]
    no = ["hello chat", "what game is this", "gg", "anyone know the song", "first time here"]
    check("reaction messages are recognised", all(detector.is_reaction(t) for t in yes), [t for t in yes if not detector.is_reaction(t)])
    check("ordinary messages are not reactions", not any(detector.is_reaction(t) for t in no), [t for t in no if detector.is_reaction(t)])
    check("commands and bots are ignored", detector.should_ignore("viewer", "!uptime") and detector.should_ignore("Nightbot", "hi")
          and not detector.should_ignore("viewer", "hi"))


def feed(det, t0, t1, per_sec, users, text_fn):
    t = t0
    i = 0
    while t < t1:
        for k in range(per_sec):
            det.add(t + k / per_sec, f"{users}{i % 60}", text_fn(i))
            i += 1
        t += 1


def run_detector(spike_at=None, burst_users=40, base_rate=3, until=400, settings=None):
    det = detector.SpikeDetector.from_settings(settings or {})
    spikes = []
    t, step = 1000.0, det.tick
    feed(det, t, t + until, base_rate, "u", lambda i: "chatting about the game")
    if spike_at is not None:
        for j in range(burst_users):
            det.add(t + spike_at + j * 0.15, f"fan{j}", "KEKW" if j % 2 else "LMAOOO no way")
    now = t
    while now < t + until:
        sp = det.step(now)
        if sp:
            spikes.append(sp)
        now += step
    return t, spikes


def test_detector():
    t0, spikes = run_detector(spike_at=None)
    check("steady chat produces no spike", not spikes, spikes)
    t0, spikes = run_detector(spike_at=200)
    check("a burst of reactions produces exactly one spike", len(spikes) == 1, spikes)
    if spikes:
        s = spikes[0]
        check("the spike is detected within ~10 s of the burst", 0 <= s.at - (t0 + 200) <= 10, s.at - t0)
        check("the reported moment is set back for reaction time", s.moment < s.at - 5, (s.at - s.moment))
    t0, spikes = run_detector(spike_at=30)
    check("no spike during warm-up (no baseline yet)", not spikes, spikes)
    det = detector.SpikeDetector()
    feed(det, 0, 300, 2, "u", lambda i: "hi")
    for j in range(60):
        det.add(150 + j * 0.1, "same_guy", "KEKW KEKW")       # one spammer
    found = [s for s in (det.step(x) for x in range(0, 300, 2)) if s]
    check("one person spamming does not count as a spike", not found, found)
    _, low = run_detector(spike_at=200, burst_users=10, settings={"sensitivity": "low"})
    _, high = run_detector(spike_at=200, burst_users=10, settings={"sensitivity": "high"})
    check("sensitivity changes what counts (small burst: high catches it, low does not)", high and not low, (len(high), len(low)))


def test_chat_parse():
    line = ("@badge-info=;badges=;color=#FF0000;display-name=Cool\\sGuy;emotes=;tmi-sent-ts=1700000000123;user-id=42 "
            ":coolguy!coolguy@coolguy.tmi.twitch.tv PRIVMSG #somechannel :KEKW that was wild")
    p = parse_line(line)
    m = to_message(p)
    check("PRIVMSG is parsed with display name, text and server timestamp",
          m and m.user == "Cool Guy" and m.text == "KEKW that was wild" and abs(m.ts - 1700000000.123) < 1e-6, m)
    sub = parse_line("@msg-id=sub;tmi-sent-ts=1 :tmi.twitch.tv USERNOTICE #somechannel :Great stream")
    check("sub/raid notices (USERNOTICE) are not treated as chat", to_message(sub) is None)
    me = to_message(parse_line(":a!a@a PRIVMSG #c :\x01ACTION dances\x01"), now=5.0)
    check("/me messages are unwrapped", me and me.text == "dances" and me.ts == 5.0, me)


def test_refine_and_captions():
    segs = [{"start": 0.5, "end": 4.0, "text": "so here we go"}, {"start": 5.0, "end": 11.0, "text": "watch this jump"},
            {"start": 12.0, "end": 20.0, "text": "oh my god it worked"}, {"start": 21.0, "end": 30.0, "text": "unbelievable"}]
    s, e = clipper.refine_bounds(segs, 7.0, 18.0, 40.0)
    check("start moves back to the start of the sentence", abs(s - 4.85) < 0.01, s)
    check("end moves forward to the end of the sentence", abs(e - 20.35) < 0.01, e)
    s, e = clipper.refine_bounds(segs, 4.3, 25.0, 40.0)
    check("a start in the pause before a sentence moves to that sentence", abs(s - 4.85) < 0.01, s)
    s, e = clipper.refine_bounds([], 5.0, 8.0, 40.0, min_len=12)
    check("too-short clips are widened to the minimum length", abs((e - s) - 12) < 0.01, (s, e))
    s, e = clipper.refine_bounds([], 0.0, 200.0, 40.0)
    check("clips stay inside the file and under the maximum", s >= 0 and e <= 40, (s, e))
    words = [{"start": 5.0 + i * 0.5, "end": 5.4 + i * 0.5, "word": w} for i, w in enumerate("watch this jump right now ok".split())]
    ev = clipper.caption_events([{"start": 5, "end": 8, "text": "", "words": words}], 5.0, 8.0)
    check("captions are short word-timed chunks relative to the clip start",
          ev and ev[0][0] == 0.0 and all(len(t.split()) <= 3 for _, _, t in ev) and all(a < b for a, b, _ in ev), ev)
    check("caption chunks never overlap", all(ev[i][1] <= ev[i + 1][0] for i in range(len(ev) - 1)), ev)


def test_stack_geometry():
    g = clipper.stack_geometry(1920, 1080, clipper.FACECAM_PRESETS["top-right"])
    cw, ch, cx, cy = g["cam"]
    gw, gh, gx, gy = g["game"]
    check("stack layout fills 1920 px exactly", g["top"] + g["bottom"] == 1920, g)
    check("facecam crop is inside the frame", cx + cw <= 1920 and cy + ch <= 1080 and cx >= 0, g["cam"])
    check("gameplay crop is inside the frame and centred", gx + gw <= 1920 and gh <= 1080 and abs((gx + gw / 2) - 960) <= 2, g["game"])
    check("all crop sizes are even (required by the H.264 encoder)", all(v % 2 == 0 for v in (cw, ch, gw, gh, g["top"])), g)


def test_buffer_prune():
    with tempfile.TemporaryDirectory() as d:
        clock = [1000.0]
        b = SegmentBuffer(d, keep_sec=30, clock=lambda: clock[0])
        for i in range(20):
            p = os.path.join(d, f"seg{i}.ts")
            open(p, "wb").write(b"x")
            clock[0] += 4
            b.add(p, 4.0)
        removed = b.prune()
        check("old segments are deleted, recent ones kept", removed > 0 and b.oldest >= clock[0] - 34
              and len(os.listdir(d)) == len(b.segments), (removed, len(b.segments)))
        segs = b.window(clock[0] - 10, clock[0])
        check("window() returns the segments covering a time range", 2 <= len(segs) <= 4, len(segs))


def offline_run():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
    except Exception:
        check("ffmpeg available for the offline run", False, "ffmpeg not found")
        return
    from live import watcher as W
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "stream.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "80", "-c:v", "libx264",
                        "-preset", "ultrafast", "-g", "60", "-c:a", "aac", "-shortest", src], check=True)
        chat = os.path.join(d, "chat.jsonl")
        with open(chat, "w", encoding="utf-8") as f:
            for t in range(0, 78):
                for k in range(2):
                    f.write(json.dumps({"t": t + k * 0.5, "user": f"u{(t * 2 + k) % 40}", "text": "chatting"}) + "\n")
            for j in range(30):
                f.write(json.dumps({"t": 50 + j * 0.2, "user": f"fan{j}", "text": "KEKW no way"}) + "\n")
        rows = sorted((json.loads(l) for l in open(chat, encoding="utf-8")), key=lambda r: r["t"])
        with open(chat, "w", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        fake_words = [{"start": i * 0.6, "end": i * 0.6 + 0.5, "word": w} for i, w in enumerate(("look at this " * 60).split())]
        orig = W.make_clip.__globals__["transcribe"]
        W.make_clip.__globals__["transcribe"] = lambda path: [{"start": 0.0, "end": 100.0, "text": "look at this", "words": fake_words}]
        out = os.path.join(d, "out")
        settings = {"warmup": 25, "baseline": 40, "pre": 15, "post": 6, "min_len": 10, "buffer_minutes": 1}
        try:
            W.main(["--channel", "teststream", "--local", out, "--source-file", src, "--chat-replay", chat,
                    "--settings", json.dumps(settings), "--max-hours", "1"])
        finally:
            W.make_clip.__globals__["transcribe"] = orig
        index = os.path.join(out, "clips.json")
        clips = json.load(open(index, encoding="utf-8")) if os.path.exists(index) else []
        check("offline run: exactly one clip for the one burst", len(clips) == 1, clips)
        if clips:
            c = clips[0]
            w, h, dur = clipper.probe(c["video"])
            check("offline run: clip is vertical 1080x1920", (w, h) == (1080, 1920), (w, h))
            check("offline run: clip length is within the limits", 10 <= dur <= 60, dur)
            check("offline run: captions (.srt) and thumbnail were written", c.get("srt") and os.path.getsize(c["srt"]) > 20
                  and c.get("thumb") and os.path.exists(c["thumb"]), c)
            check("offline run: the untouched 16:9 source window is kept too", c.get("raw") and clipper.probe(c["raw"])[:2] == (1280, 720))


def offline_handoff():
    """Two runners on one watch: the second takes over after warming up, the first exits, no duplicate clips."""
    import threading
    import time as _t
    from live import watcher as W
    from live.store import LocalStore
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "stream.mp4")
        subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
                        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100", "-t", "100", "-c:v", "libx264",
                        "-preset", "ultrafast", "-g", "60", "-c:a", "aac", "-shortest", src], check=True)
        chat = os.path.join(d, "chat.jsonl")
        rows = [{"t": t + k * 0.5, "user": f"u{(t * 2 + k) % 40}", "text": "chatting"} for t in range(98) for k in range(2)]
        rows += [{"t": 70 + j * 0.2, "user": f"fan{j}", "text": "KEKW no way"} for j in range(30)]
        with open(chat, "w", encoding="utf-8") as f:
            for r in sorted(rows, key=lambda r: r["t"]):
                f.write(json.dumps(r) + "\n")
        orig = W.make_clip.__globals__["transcribe"]
        W.make_clip.__globals__["transcribe"] = lambda path: [{"start": 0.0, "end": 200.0, "text": "look", "words": []}]
        settings = {"warmup": 20, "baseline": 30, "pre": 12, "post": 5, "min_len": 10, "buffer_minutes": 1}
        out = os.path.join(d, "out")
        store = LocalStore(out, "teststream", settings, 1)
        reasons = {}

        def run(name):
            w = W.Watcher(store, "teststream", settings, 1, os.path.join(d, name), source_file=src, chat_replay=chat)
            reasons[name] = w.run()

        try:
            a = threading.Thread(target=run, args=("A",))
            a.start()
            _t.sleep(15)
            b = threading.Thread(target=run, args=("B",))
            b.start()
            a.join(timeout=200)
            b.join(timeout=200)
        finally:
            W.make_clip.__globals__["transcribe"] = orig
        check("hand-over: the first runner exits once the second has taken over", reasons.get("A") == "handed_off", reasons)
        check("hand-over: the second runner owns the watch (generation 2)", store.watch.get("generation") == 2, store.watch.get("generation"))
        check("hand-over: the burst after the takeover is clipped exactly once", len(store.clips) == 1, [c["moment_at"] for c in store.clips])


def main():
    test_reactions()
    test_detector()
    test_chat_parse()
    test_refine_and_captions()
    test_stack_geometry()
    test_buffer_prune()
    if "--quick" not in sys.argv:
        offline_run()
        offline_handoff()
    print(f"\n{sum(results)}/{len(results)} passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
