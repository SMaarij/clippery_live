"""
Follows one live channel and clips its moments.

    python -m live.watcher --watch <watch_id>                         # from the admin panel / GitHub Actions
    python -m live.watcher --channel somechannel --local clips_out    # no database, clips land in a folder
    python -m live.watcher --channel test --local out --source-file demo.mp4 --chat-replay chat.jsonl   # offline test

Loop (every 2 s): feed chat into the spike detector; on a spike, hand the moment to a clip worker (cut from the
rolling buffer, transcribe, snap to sentences, render 9:16 with captions, upload). Every 15 s: heartbeat, check
the stop flag and the hour cap. If the stream drops it waits a few minutes for it to come back before ending.

GitHub runners stop after 6 hours, so after `handoff_after` seconds the run starts a fresh run of the workflow
for the same watch and keeps recording until the new run has warmed up and taken over (both runners save into
the same table; a database function drops the duplicate clip if both catch the same moment).
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import json
import os
import queue
import shutil
import socket
import sys
import tempfile
import threading
import time
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from live.buffer import SegmentBuffer          # noqa: E402
from live.chat import ChatMessage, TwitchChat  # noqa: E402
from live.clipper import make_clip             # noqa: E402
from live.describe import describe             # noqa: E402
from live.detector import SpikeDetector        # noqa: E402
from live.recorder import Recorder, is_live    # noqa: E402
from live.store import DbStore, LocalStore     # noqa: E402
from live.twitch_api import ViewerClips        # noqa: E402

DEFAULTS = {"sensitivity": "normal", "layout": "center", "facecam": "top-right", "quality": "best",
            "pre": 22, "post": 10, "min_len": 12, "max_len": 60, "wait_minutes": 30, "max_clips": 300,
            "keep_raw": True, "buffer_minutes": 10}


def log(msg: str) -> None:
    print(f"{dt.datetime.utcnow():%H:%M:%S} {msg}", flush=True)


def dispatch_next_run(watch_id: str) -> bool:
    """Start a fresh run of this workflow for the same watch (GitHub Actions only)."""
    repo, token = os.environ.get("GITHUB_REPOSITORY"), os.environ.get("GITHUB_TOKEN")
    workflow = os.environ.get("LIVE_WORKFLOW_FILE", "live-watch.yml")
    if not (repo and token):
        log("[handoff] not on GitHub Actions (no GITHUB_REPOSITORY/GITHUB_TOKEN); cannot hand over")
        return False
    body = json.dumps({"ref": os.environ.get("GITHUB_REF_NAME", "main"), "inputs": {"watch_id": watch_id}}).encode()
    req = urllib.request.Request(f"https://api.github.com/repos/{repo}/actions/workflows/{workflow}/dispatches", body,
                                 {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                  "Content-Type": "application/json", "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            ok = r.status in (200, 201, 204)
    except Exception as e:
        log(f"[handoff] dispatch failed: {e}")
        return False
    log("[handoff] started the next runner" if ok else "[handoff] dispatch was not accepted")
    return ok


class Watcher:
    def __init__(self, store, channel: str, settings: dict, max_hours: float, workdir: str,
                 source_file: str | None = None, chat_replay: str | None = None, handoff_after: float = 5.4 * 3600,
                 clock=time.time):
        self.store, self.channel, self.max_hours = store, channel.lower(), float(max_hours)
        self.settings = {**DEFAULTS, **(settings or {})}
        self.workdir, self.source_file, self.chat_replay = workdir, source_file, chat_replay
        self.handoff_after = handoff_after
        self.clock = clock
        self.run_id = os.environ.get("GITHUB_RUN_ID") or f"{socket.gethostname()}-{os.getpid()}"
        self.detector = SpikeDetector.from_settings(self.settings)
        self.viewer = ViewerClips(self.channel, log=log)
        self.jobs: queue.Queue = queue.Queue()
        self.made, self.failed, self.n = 0, 0, 0
        self.chat_lock = threading.Lock()
        self.recent_chat: collections.deque = collections.deque(maxlen=4000)
        self.note = ""
        self.recorder: Recorder | None = None
        self.buffer: SegmentBuffer | None = None
        self.session = 0
        self.my_generation = None
        self.handed_off = False
        self.reason = "ended"

    # -- chat ----------------------------------------------------------------------------------------------------
    def on_chat(self, m: ChatMessage) -> None:
        with self.chat_lock:
            self.detector.add(m.ts, m.user, m.text)
            self.recent_chat.append((m.ts, m.text))

    def _replay_chat(self, path: str, t0: float) -> None:
        with open(path, encoding="utf-8") as f:
            rows = [json.loads(line) for line in f if line.strip()]
        for r in rows:
            delay = t0 + float(r["t"]) - self.clock()
            if delay > 0:
                time.sleep(delay)
            self.on_chat(ChatMessage(ts=t0 + float(r["t"]), user=r["user"], text=r["text"]))

    # -- recording -----------------------------------------------------------------------------------------------
    def _start_recorder(self) -> None:
        self.session += 1
        folder = os.path.join(self.workdir, f"buffer{self.session}")
        self.buffer = SegmentBuffer(folder, keep_sec=float(self.settings["buffer_minutes"]) * 60)
        self.recorder = Recorder(self.buffer, channel=self.channel, quality=self.settings["quality"],
                                 source_file=self.source_file, log=log).start()

    # -- clips ---------------------------------------------------------------------------------------------------
    def _clip_worker(self) -> None:
        while True:
            item = self.jobs.get()
            if item is None:
                return
            moment, trigger, score, buffer, recorder = item
            self.n += 1
            name = f"clip{self.n:03d}"
            try:
                res = make_clip(buffer, moment, os.path.join(self.workdir, "clips"), name, self.settings,
                                stream_ended=recorder.ended.is_set, log=log)
                when = dt.datetime.utcfromtimestamp(moment).strftime("%H:%M UTC")
                label = "viewer clip" if trigger == "viewer_clip" else f"chat spike {score}x"
                text = describe(res.transcript or "", self._chat_near(moment), self.channel, self.note, log=log)
                title = text.get("title") or f"{self.channel} {when} ({label})"
                cid = self.store.add_clip(res, moment, trigger, score, title, hook=text.get("hook"),
                                          description=text.get("description"))
                if cid:
                    self.made += 1
                    log(f"[clip] saved {name} -> {cid} ({res.duration}s, {label})")
                else:
                    log(f"[clip] {name} skipped: that moment was already saved")
                if isinstance(self.store, DbStore):
                    for p in (res.video, res.srt, res.thumb, res.raw):
                        if p and os.path.exists(p):
                            os.remove(p)
            except Exception as e:
                self.failed += 1
                log(f"[clip] {name} failed: {str(e)[:300]}")
            finally:
                self.jobs.task_done()

    def _chat_near(self, moment: float, before: float = 15, after: float = 10) -> list[str]:
        """Distinct chat lines typed around the moment (what made chat spike), most repeated first."""
        with self.chat_lock:
            lines = [t.strip() for ts, t in self.recent_chat if moment - before <= ts <= moment + after and t.strip()]
        counts = collections.Counter(lines)
        return [f"{t} (x{n})" if n > 1 else t for t, n in counts.most_common(40)]

    def _queue_clip(self, moment: float, trigger: str, score: float | None) -> None:
        if self.made + self.jobs.qsize() >= int(self.settings["max_clips"]):
            return
        if self.buffer and moment - float(self.settings["pre"]) < self.buffer.oldest:
            log("[clip] moment is older than the buffer; skipped")
            return
        log(f"[spike] {trigger} at {dt.datetime.utcfromtimestamp(moment):%H:%M:%S} UTC (score {score})")
        self.jobs.put((moment, trigger, score, self.buffer, self.recorder))

    # -- main ----------------------------------------------------------------------------------------------------
    def _heartbeat(self, status: str, last: list) -> None:
        now = self.clock()
        spent = int(now - last[0])
        last[0] = now
        try:
            w = self.store.load()
            self.store.update(status=status, last_heartbeat=dt.datetime.now(dt.timezone.utc).isoformat(),
                              runner_seconds=int(w.get("runner_seconds") or 0) + spent)
        except Exception as e:
            log(f"[heartbeat] {e}")

    def _wait_until_live(self, deadline: float) -> bool:
        if self.source_file:
            return True
        while self.clock() < deadline:
            if self.store.stop_requested():
                self.reason = "stopped"
                return False
            if is_live(self.channel):
                return True
            log(f"[wait] {self.channel} is not live yet")
            time.sleep(60)
        self.reason = "ended"
        return False

    def run(self) -> str:
        t_start = self.clock()
        watch = self.store.load()
        self.note = (watch.get("note") or "").strip()
        self.my_generation = int(watch.get("generation") or 0) + 1
        hb = [t_start]
        if int(watch.get("generation") or 0) == 0:                # a hand-over run must not overwrite the live run's status
            self.store.update(status="waiting", active_run=self.run_id, error=None)
        wait_deadline = t_start + float(self.settings["wait_minutes"]) * 60
        if not self._wait_until_live(wait_deadline):
            if int(watch.get("generation") or 0) == 0:
                self.store.update(status="stopped" if self.reason == "stopped" else "ended",
                                  ended_at=dt.datetime.now(dt.timezone.utc).isoformat(),
                                  error=None if self.reason == "stopped" else "The channel never went live.")
            return self.reason
        workers = [threading.Thread(target=self._clip_worker, daemon=True) for _ in range(int(self.settings.get("workers", 1)))]
        for w in workers:
            w.start()
        self._start_recorder()
        live_since = self.clock()
        if self.chat_replay:
            threading.Thread(target=self._replay_chat, args=(self.chat_replay, live_since), daemon=True).start()
            chat = None
        else:
            chat = TwitchChat(self.channel, self.on_chat, log=log).start()
        if not watch.get("started_at"):
            self.store.update(started_at=dt.datetime.now(dt.timezone.utc).isoformat())
        started_at = watch.get("started_at")
        watch_began = (dt.datetime.fromisoformat(started_at.replace("Z", "+00:00")).timestamp() if started_at else live_since)
        claimed = int(watch.get("generation") or 0) == 0
        if claimed:
            self.store.update(generation=self.my_generation, active_run=self.run_id, status="recording")
        self.recorder.started.wait(timeout=90)
        log(f"[watch] recording {self.channel} (run {self.run_id}, generation {self.my_generation})")
        last_check, last_viewer, last_purge = 0.0, self.clock(), 0.0
        drop_checks = 0
        try:
            while True:
                now = self.clock()
                with self.chat_lock:
                    spike = self.detector.step(now)
                if spike and claimed:
                    self._queue_clip(spike.moment, "chat", spike.ratio)
                if self.viewer.enabled and now - last_viewer >= 60:
                    for moment, _cid in self.viewer.poll(last_viewer):
                        if claimed:
                            self._queue_clip(moment, "viewer_clip", None)
                    last_viewer = now
                if now - last_check >= 15:
                    last_check = now
                    gen, owner = self.store.generation_owner()
                    if not claimed and now - live_since >= self.detector.warmup + 5:
                        self.store.update(generation=self.my_generation, active_run=self.run_id, status="recording")
                        claimed = True
                        log("[handoff] took over the watch")
                    elif claimed and gen > self.my_generation:
                        log("[handoff] the next runner has taken over; finishing up")
                        self.reason = "handed_off"
                        break
                    if self.store.stop_requested():
                        self.reason = "stopped"
                        break
                    if now - watch_began >= self.max_hours * 3600:
                        self.reason = "max_hours"
                        break
                    if claimed:
                        self._heartbeat("recording", hb)
                    if claimed and isinstance(self.store, DbStore) and now - last_purge >= 600:
                        last_purge = now                  # clips expire after LIVE_RETENTION_HOURS even mid-stream
                        try:
                            from live import cleanup
                            n = cleanup.purge_clips(self.store.db)
                            if n:
                                log(f"[cleanup] deleted {n} expired clips")
                        except Exception as e:
                            log(f"[cleanup] {str(e)[:200]}")
                    if (not self.handed_off and os.environ.get("GITHUB_ACTIONS") == "true"
                            and now - t_start >= self.handoff_after):
                        self.handed_off = dispatch_next_run(self.store.watch_id)
                if self.recorder.ended.is_set():
                    if self.source_file:
                        self.reason = "ended"
                        break
                    drop_checks += 1
                    if drop_checks > 3:
                        self.reason = "ended"
                        break
                    log(f"[watch] stream dropped; checking if it comes back ({drop_checks}/3)")
                    time.sleep(45)
                    if is_live(self.channel):
                        self._start_recorder()
                        drop_checks = 0
                    continue
                time.sleep(self.detector.tick)
        finally:
            if chat:
                chat.stop()
            if self.recorder:
                self.recorder.stop()
            done = self.clock() + 240
            while self.jobs.unfinished_tasks and self.clock() < done:
                time.sleep(1)
            for _ in workers:
                self.jobs.put(None)
            if self.reason != "handed_off":
                status = {"stopped": "stopped", "max_hours": "stopped"}.get(self.reason, "ended")
                self._heartbeat(status, hb)
                note = "Stopped at the hour limit." if self.reason == "max_hours" else None
                self.store.update(status=status, ended_at=dt.datetime.now(dt.timezone.utc).isoformat(), error=note)
            else:
                self._heartbeat("recording", hb)
            log(f"[watch] done: {self.reason}; {self.made} clips saved, {self.failed} failed")
        return self.reason


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Clip a live Twitch stream from chat spikes.")
    ap.add_argument("--watch", help="live_watches id (database mode)")
    ap.add_argument("--channel", help="Twitch channel (local mode)")
    ap.add_argument("--local", help="folder for clips (local mode, no database)")
    ap.add_argument("--max-hours", type=float, default=4.0)
    ap.add_argument("--settings", default="{}", help='JSON, e.g. {"layout":"stack","facecam":"top-left"}')
    ap.add_argument("--source-file", help="replay a local video instead of Twitch (testing)")
    ap.add_argument("--chat-replay", help="JSONL chat to replay: {\"t\": seconds, \"user\": ..., \"text\": ...}")
    ap.add_argument("--handoff-after", type=float, default=float(os.environ.get("LIVE_HANDOFF_AFTER", 5.4 * 3600)))
    a = ap.parse_args(argv)
    work = tempfile.mkdtemp(prefix="live_")
    try:
        if a.watch:
            store = DbStore(a.watch, keep_raw=True)
            w = store.load()
            settings = {**(w.get("settings") or {})}
            store.keep_raw = bool(settings.get("keep_raw", True))
            if w["status"] in ("ended", "stopped", "failed"):
                log(f"[watch] {a.watch} is already {w['status']}; nothing to do")
                return 0
            if os.environ.get("GITHUB_ACTIONS") == "true":
                print(f"::add-mask::{w['channel']}", flush=True)     # Actions logs of a public repo are public
            watcher = Watcher(store, w["channel"], settings, float(w["max_hours"]), work, handoff_after=a.handoff_after)
        else:
            if not (a.channel and a.local):
                ap.error("use --watch ID, or --channel NAME --local FOLDER")
            settings = json.loads(a.settings)
            store = LocalStore(a.local, a.channel, settings, a.max_hours)
            watcher = Watcher(store, a.channel, settings, a.max_hours, work, source_file=a.source_file,
                              chat_replay=a.chat_replay, handoff_after=a.handoff_after)
        try:
            watcher.run()
        except Exception as e:
            log(f"[watch] crashed: {e}")
            try:
                store.update(status="failed", error=str(e)[:500], ended_at=dt.datetime.now(dt.timezone.utc).isoformat())
            except Exception:
                pass
            raise
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
