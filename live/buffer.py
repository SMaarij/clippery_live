"""
Rolling recording buffer.

The stream is copied (no re-encoding, so it costs almost no CPU) into short MPEG-TS segments by ffmpeg's segment
muxer. Each finished segment is timestamped with the wall-clock time it arrived, which lines it up with chat
(viewers react to what they see after roughly the same delay we receive it). Segments older than `keep_sec` are
deleted, so an 8-hour stream never takes more than a few minutes of disk.

`window(a, b)` returns the segments covering wall-clock [a, b]; `cut(a, b, out)` joins them into one MP4.
"""
from __future__ import annotations

import csv
import os
import subprocess
import threading
import time
from dataclasses import dataclass


@dataclass
class Segment:
    path: str
    wall_start: float
    wall_end: float

    @property
    def duration(self) -> float:
        return self.wall_end - self.wall_start


class SegmentBuffer:
    def __init__(self, folder: str, keep_sec: float = 600.0, clock=time.time):
        self.folder = folder
        self.keep_sec = keep_sec
        self.clock = clock
        self.list_path = os.path.join(folder, "segments.csv")
        self.segments: list[Segment] = []
        self._lock = threading.Lock()
        self._read_lines = 0
        self.pinned: set[str] = set()          # paths a clip is still cutting from
        os.makedirs(folder, exist_ok=True)

    # -- fed by the recorder ----------------------------------------------------------------------------------
    def add(self, path: str, duration: float, arrived: float | None = None) -> Segment:
        end = self.clock() if arrived is None else arrived
        seg = Segment(path=path, wall_start=end - duration, wall_end=end)
        with self._lock:
            self.segments.append(seg)
        return seg

    def poll_list(self) -> int:
        """Read new rows from ffmpeg's segment list (filename,start,end). Returns how many were added."""
        if not os.path.exists(self.list_path):
            return 0
        with open(self.list_path, newline="", encoding="utf-8", errors="replace") as f:
            rows = list(csv.reader(f))
        new = rows[self._read_lines:]
        added = 0
        for row in new:
            if len(row) < 3:
                break                                   # half-written line; read it next time
            try:
                dur = max(0.05, float(row[2]) - float(row[1]))
            except ValueError:
                break
            self.add(os.path.join(self.folder, row[0]), dur)
            self._read_lines += 1
            added += 1
        return added

    def prune(self) -> int:
        cutoff = self.clock() - self.keep_sec
        removed = 0
        with self._lock:
            keep = []
            for s in self.segments:
                if s.wall_end < cutoff and s.path not in self.pinned:
                    try:
                        os.remove(s.path)
                    except OSError:
                        pass
                    removed += 1
                else:
                    keep.append(s)
            self.segments = keep
        return removed

    # -- used by the clipper ----------------------------------------------------------------------------------
    @property
    def newest(self) -> float:
        with self._lock:
            return self.segments[-1].wall_end if self.segments else 0.0

    @property
    def oldest(self) -> float:
        with self._lock:
            return self.segments[0].wall_start if self.segments else 0.0

    def window(self, a: float, b: float) -> list[Segment]:
        with self._lock:
            return [s for s in self.segments if s.wall_end > a and s.wall_start < b]

    def cut(self, a: float, b: float, out_path: str, ffmpeg: str = "ffmpeg") -> tuple[float, float]:
        """Join the segments covering [a, b] into out_path (stream copy). Returns (wall time of out_path's first
        frame, its length) so callers can convert wall-clock moments to positions in the file."""
        segs = self.window(a, b)
        if not segs:
            raise RuntimeError("no recording covers that moment (it is older than the buffer, or the stream dropped)")
        paths = [s.path for s in segs]
        self.pinned.update(paths)
        try:
            lst = out_path + ".txt"
            with open(lst, "w", encoding="utf-8") as f:
                for p in paths:
                    f.write("file '" + os.path.abspath(p).replace("\\", "/").replace("'", "'\\''") + "'\n")
            subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", lst,
                            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy", "-bsf:a", "aac_adtstoasc",
                            "-movflags", "+faststart", out_path], check=True, timeout=300)
            os.remove(lst)
        finally:
            self.pinned.difference_update(paths)
        return segs[0].wall_start, segs[-1].wall_end - segs[0].wall_start
