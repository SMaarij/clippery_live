"""
Records a live stream into the rolling buffer.

    streamlink --stdout twitch.tv/<channel> best  |  ffmpeg -c copy -f segment ...  ->  seg_0000001.ts, segments.csv

streamlink fetches the HLS stream and skips Twitch's mid-roll ads (--twitch-disable-ads, so ad breaks never end up
in a clip). ffmpeg only copies packets into 4-second segments; nothing is re-encoded until a clip is cut.

For tests and demos, `source_file` replays a local video in real time instead of contacting Twitch.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time

from .buffer import SegmentBuffer


def streamlink_cmd() -> list[str]:
    exe = shutil.which("streamlink")
    return [exe] if exe else [sys.executable, "-m", "streamlink"]


def is_live(channel: str, timeout: float = 45) -> bool:
    """True when the channel is broadcasting right now (asks streamlink for its stream list)."""
    try:
        r = subprocess.run(streamlink_cmd() + ["--json", f"https://www.twitch.tv/{channel}"],
                           capture_output=True, text=True, timeout=timeout)
    except Exception:
        return False
    return r.returncode == 0 and '"streams"' in r.stdout and '"best"' in r.stdout


class Recorder:
    def __init__(self, buffer: SegmentBuffer, channel: str | None = None, quality: str = "best",
                 source_file: str | None = None, segment_sec: float = 4.0, ffmpeg: str = "ffmpeg", log=print):
        self.buffer = buffer
        self.channel = channel
        self.quality = quality
        self.source_file = source_file
        self.segment_sec = segment_sec
        self.ffmpeg = ffmpeg
        self.log = log
        self.ended = threading.Event()
        self.started = threading.Event()        # first segment arrived
        self._procs: list[subprocess.Popen] = []
        self._stop = threading.Event()
        self.exit_detail = ""

    def _source(self) -> subprocess.Popen:
        if self.source_file:
            cmd = [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-re", "-i", self.source_file,
                   "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "mpegts", "pipe:1"]
        else:
            cmd = streamlink_cmd() + ["--stdout", "--loglevel", "warning", "--twitch-disable-ads", "--twitch-low-latency",
                                      "--hls-live-edge", "2", "--stream-segment-attempts", "5", "--retry-open", "3",
                                      f"https://www.twitch.tv/{self.channel}", self.quality]
        return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def start(self):
        src = self._source()
        seg = subprocess.Popen(
            [self.ffmpeg, "-hide_banner", "-loglevel", "error", "-fflags", "+genpts", "-i", "pipe:0",
             "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-f", "segment", "-segment_time", str(self.segment_sec),
             "-segment_format", "mpegts", "-segment_list", self.buffer.list_path, "-segment_list_type", "csv",
             "-segment_list_flags", "+live", "-reset_timestamps", "1",
             os.path.join(self.buffer.folder, "seg_%07d.ts")],
            stdin=src.stdout, stderr=subprocess.PIPE)
        src.stdout.close()                       # so the source sees a broken pipe if ffmpeg dies
        self._procs = [src, seg]
        threading.Thread(target=self._watch, name="recorder", daemon=True).start()
        return self

    def _watch(self):
        src, seg = self._procs
        last_prune = time.time()
        while not self._stop.is_set():
            if self.buffer.poll_list():
                self.started.set()
            if time.time() - last_prune > 20:
                self.buffer.prune()
                last_prune = time.time()
            if seg.poll() is not None or src.poll() is not None:
                time.sleep(1.0)
                self.buffer.poll_list()
                err = b""
                for p in (src, seg):
                    try:
                        err += (p.stderr.read() or b"")[-600:]
                    except Exception:
                        pass
                self.exit_detail = err.decode("utf-8", "replace").strip()[-400:]
                self.log(f"[recorder] stream stopped (source exit {src.poll()}, segmenter exit {seg.poll()}) {self.exit_detail}")
                break
            time.sleep(0.5)
        self.ended.set()

    def stop(self):
        self._stop.set()
        for p in self._procs:
            if p.poll() is None:
                p.terminate()
        for p in self._procs:
            try:
                p.wait(timeout=10)
            except Exception:
                p.kill()
        self.ended.set()
