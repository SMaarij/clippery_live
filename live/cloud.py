"""
The two services the watcher writes to, configured from environment variables (GitHub repo secrets):

    SUPABASE_URL, SUPABASE_SECRET_KEY                       the live_watches / live_clips tables
    R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, R2_BUCKET_UPLOADS   where clip files go

The secret key bypasses row-level security, so it must only ever live in repo secrets, never in code or logs.
"""
from __future__ import annotations

import mimetypes
import os
import threading
import time

_lock = threading.Lock()
_db = None
_s3 = None


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def db():
    global _db
    if _db is None:
        with _lock:
            if _db is None:
                url, key = env("SUPABASE_URL"), env("SUPABASE_SECRET_KEY")
                if not (url and key):
                    raise RuntimeError("SUPABASE_URL / SUPABASE_SECRET_KEY are not set")
                from supabase import create_client
                _db = create_client(url, key)
    return _db


def rpc(name: str, params: dict | None = None):
    return db().rpc(name, params or {}).execute().data


def r2_available() -> bool:
    return all(env(k) for k in ("R2_ACCOUNT_ID", "R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_BUCKET_UPLOADS"))


def bucket() -> str:
    return env("R2_BUCKET_UPLOADS")


def s3():
    global _s3
    if _s3 is None:
        with _lock:
            if _s3 is None:
                import boto3
                from botocore.config import Config
                _s3 = boto3.client(
                    "s3", endpoint_url=f"https://{env('R2_ACCOUNT_ID')}.r2.cloudflarestorage.com",
                    aws_access_key_id=env("R2_ACCESS_KEY_ID"), aws_secret_access_key=env("R2_SECRET_ACCESS_KEY"),
                    # path-style is required: R2's certificate doesn't cover per-bucket hostnames
                    config=Config(signature_version="s3v4", region_name="auto", s3={"addressing_style": "path"}))
    return _s3


def _retry(fn, attempts: int = 3):
    for i in range(attempts):
        try:
            return fn()
        except Exception:
            if i == attempts - 1:
                raise
            time.sleep(2 ** (i + 1))


def upload_file(key: str, local: str, content_type: str | None = None) -> None:
    ctype = content_type or mimetypes.guess_type(local)[0] or "application/octet-stream"
    _retry(lambda: s3().upload_file(local, bucket(), key, ExtraArgs={"ContentType": ctype}))


def remove(keys: list[str]) -> None:
    keys = [k for k in keys if k]
    for i in range(0, len(keys), 1000):                       # delete_objects takes at most 1000 keys
        chunk = keys[i:i + 1000]
        _retry(lambda: s3().delete_objects(Bucket=bucket(), Delete={"Objects": [{"Key": k} for k in chunk], "Quiet": True}))
