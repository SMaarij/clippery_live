"""
Where the watcher keeps its state and its clips.

    DbStore    : the live_watches / live_clips tables (Supabase, service role) + Cloudflare R2 for the files (live/cloud.py)
    LocalStore : no database: clips are copied into a folder and listed in clips.json (tests, trying it out)
"""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import uuid


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).isoformat()


class LocalStore:
    def __init__(self, folder: str, channel: str, settings: dict | None = None, max_hours: float = 4.0):
        self.folder = folder
        os.makedirs(folder, exist_ok=True)
        self.watch = {"id": "local-" + uuid.uuid4().hex[:8], "channel": channel, "settings": settings or {},
                      "max_hours": max_hours, "generation": 0, "stop_requested": False, "status": "queued"}
        self.watch_id = self.watch["id"]
        self.clips: list[dict] = []
        self.index = os.path.join(folder, "clips.json")

    def load(self) -> dict:
        return self.watch

    def update(self, **fields) -> None:
        self.watch.update(fields)

    def stop_requested(self) -> bool:
        return bool(self.watch.get("stop_requested")) or os.path.exists(os.path.join(self.folder, "STOP"))

    def generation_owner(self) -> tuple[int, str | None]:
        return self.watch.get("generation", 0), self.watch.get("active_run")

    def add_clip(self, clip, moment: float, trigger: str, score: float | None, title: str,
                 hook: str | None = None, description: str | None = None) -> str | None:
        if any(abs(c["moment"] - moment) < 20 for c in self.clips):
            return None
        cid = uuid.uuid4().hex[:10]
        dst = {}
        for key, path in (("video", clip.video), ("srt", clip.srt), ("thumb", clip.thumb), ("raw", clip.raw)):
            if path and os.path.exists(path):
                target = os.path.join(self.folder, f"{cid}{os.path.splitext(path)[1] if key != 'raw' else '_raw.mp4'}")
                shutil.copyfile(path, target)
                dst[key] = target
        row = {"id": cid, "moment": moment, "moment_at": iso(moment), "duration": clip.duration, "trigger": trigger,
               "score": score, "title": title, "hook": hook, "description": description,
               "transcript": clip.transcript, **dst}
        self.clips.append(row)
        with open(self.index, "w", encoding="utf-8") as f:
            json.dump(self.clips, f, indent=2)
        return cid


class DbStore:
    def __init__(self, watch_id: str, keep_raw: bool = True):
        from live import cloud
        self.cloud = cloud
        self.db = cloud.db()
        self.watch_id = watch_id
        self.keep_raw = keep_raw
        if not cloud.r2_available():
            raise RuntimeError("R2 is not configured (R2_* variables); live clips are stored on R2")

    def load(self) -> dict:
        rows = self.db.table("live_watches").select("*").eq("id", self.watch_id).limit(1).execute().data
        if not rows:
            raise RuntimeError(f"watch {self.watch_id} not found")
        return rows[0]

    def update(self, **fields) -> None:
        self.db.table("live_watches").update(fields).eq("id", self.watch_id).execute()

    def stop_requested(self) -> bool:
        rows = self.db.table("live_watches").select("stop_requested").eq("id", self.watch_id).limit(1).execute().data
        return bool(rows and rows[0]["stop_requested"])

    def generation_owner(self) -> tuple[int, str | None]:
        rows = self.db.table("live_watches").select("generation,active_run").eq("id", self.watch_id).limit(1).execute().data
        return (rows[0]["generation"], rows[0]["active_run"]) if rows else (0, None)

    def add_clip(self, clip, moment: float, trigger: str, score: float | None, title: str,
                 hook: str | None = None, description: str | None = None) -> str | None:
        cid = uuid.uuid4().hex
        base = f"live/{self.watch_id}/{dt.datetime.utcfromtimestamp(moment):%Y%m%d-%H%M%S}-{cid[:8]}"
        uploads = [(base + ".mp4", clip.video, "video/mp4")]
        if clip.srt:
            uploads.append((base + ".srt", clip.srt, "application/x-subrip"))
        if clip.thumb:
            uploads.append((base + ".jpg", clip.thumb, "image/jpeg"))
        if self.keep_raw:
            uploads.append((base + "_raw.mp4", clip.raw, "video/mp4"))
        for key, local, ctype in uploads:
            self.cloud.upload_file(key, local, ctype)
        new_id = self.cloud.rpc("live_add_clip", {
            "p_watch": self.watch_id, "p_moment": iso(moment), "p_duration": clip.duration, "p_trigger": trigger,
            "p_score": score, "p_title": title[:200], "p_transcript": clip.transcript,
            "p_storage": base + ".mp4", "p_raw": base + "_raw.mp4" if self.keep_raw else None,
            "p_captions": base + ".srt" if clip.srt else None, "p_thumb": base + ".jpg" if clip.thumb else None,
        })
        if not new_id:                                    # another runner already saved this moment
            self.cloud.remove([k for k, _, _ in uploads])
        elif hook or description:
            try:                                          # columns from the 20261117010000 migration
                self.db.table("live_clips").update({"hook": hook, "description": description}).eq("id", new_id).execute()
            except Exception as e:
                print(f"[store] couldn't save hook/description (run the live_clip_text migration?): {str(e)[:160]}")
        return new_id
