"""
Deletes live clips older than LIVE_RETENTION_DAYS (default 30): their files on R2, then their rows.
Finished watches with no clips left are deleted too. Runs daily from .github/workflows/cleanup.yml.

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


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--days", type=float, default=float(os.environ.get("LIVE_RETENTION_DAYS") or 30))
    a = ap.parse_args(argv)
    cutoff = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=a.days)).isoformat()
    db = cloud.db()
    removed = 0
    while True:
        rows = (db.table("live_clips").select("id,storage_path,raw_path,captions_path,thumbnail_path")
                .lt("created_at", cutoff).limit(500).execute().data)
        if not rows:
            break
        keys = [k for r in rows for k in (r["storage_path"], r.get("raw_path"), r.get("captions_path"), r.get("thumbnail_path")) if k]
        print(f"{'would delete' if a.dry_run else 'deleting'} {len(rows)} clips ({len(keys)} files) older than {a.days:g} days")
        if a.dry_run:
            removed += len(rows)
            break
        cloud.remove(keys)
        db.table("live_clips").delete().in_("id", [r["id"] for r in rows]).execute()
        removed += len(rows)
    if not a.dry_run:
        old = (db.table("live_watches").select("id,clips_count").in_("status", ["ended", "stopped", "failed"])
               .lt("created_at", cutoff).execute().data)
        empty = []
        for w in old:
            left = db.table("live_clips").select("id").eq("watch_id", w["id"]).limit(1).execute().data
            if not left:
                empty.append(w["id"])
        if empty:
            db.table("live_watches").delete().in_("id", empty).execute()
        print(f"done: {removed} clips removed, {len(empty)} old watches removed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
