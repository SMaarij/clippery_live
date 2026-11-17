"""
Deletes live clips older than LIVE_RETENTION_HOURS (default 3): their files on R2, then their rows.
Finished watches are kept for LIVE_WATCH_RETENTION_DAYS (default 30) so the admin panel still shows the history,
then deleted once no clips are left.

Runs every 15 minutes from .github/workflows/cleanup.yml, and a running watcher also calls purge_clips() every
10 minutes, so clips go on time even if GitHub delays the scheduled run.

    python -m live.cleanup            # delete
    python -m live.cleanup --dry-run  # only report what would go
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from live import cloud  # noqa: E402


def retention_hours() -> float:
    try:
        return max(0.25, float(os.environ.get("LIVE_RETENTION_HOURS") or 3))
    except ValueError:
        return 3.0


def _cutoff(**delta) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(**delta)).isoformat()


def purge_clips(db, hours: float | None = None, dry_run: bool = False, log=print) -> int:
    hours = retention_hours() if hours is None else hours
    cutoff = _cutoff(hours=hours)
    removed = 0
    while True:
        rows = (db.table("live_clips").select("id,storage_path,raw_path,captions_path,thumbnail_path")
                .lt("created_at", cutoff).limit(500).execute().data)
        if not rows:
            break
        keys = [k for r in rows for k in (r["storage_path"], r.get("raw_path"), r.get("captions_path"), r.get("thumbnail_path")) if k]
        if dry_run:
            log(f"would delete {len(rows)} clips ({len(keys)} files) older than {hours:g} hours")
            return len(rows)
        cloud.remove(keys)
        db.table("live_clips").delete().in_("id", [r["id"] for r in rows]).execute()
        removed += len(rows)
    return removed


def purge_watches(db, days: float) -> int:
    old = (db.table("live_watches").select("id").in_("status", ["ended", "stopped", "failed"])
           .lt("created_at", _cutoff(days=days)).execute().data)
    empty = [w["id"] for w in old
             if not db.table("live_clips").select("id").eq("watch_id", w["id"]).limit(1).execute().data]
    if empty:
        db.table("live_watches").delete().in_("id", empty).execute()
    return len(empty)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--hours", type=float, default=retention_hours())
    ap.add_argument("--watch-days", type=float, default=float(os.environ.get("LIVE_WATCH_RETENTION_DAYS") or 30))
    a = ap.parse_args(argv)
    db = cloud.db()
    n = purge_clips(db, a.hours, a.dry_run)
    if not a.dry_run:
        w = purge_watches(db, a.watch_days)
        print(f"done: {n} clips older than {a.hours:g} h removed, {w} old watches removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
