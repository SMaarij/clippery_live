"""
Speech-to-text for a clip window, with word timings for the captions.

Groq's hosted Whisper (GROQ_API_KEY) answers in seconds. Without the key, or if Groq is busy, it falls back to a
local faster-whisper model (WHISPER_MODEL, default "base"), which is slower but needs nothing.

Returns [{"start", "end", "text", "words": [{"start", "end", "word"}]}] in seconds from the start of the file.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
import time

GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
_model = None


def _groq(path: str) -> list[dict]:
    import requests
    key = os.environ["GROQ_API_KEY"]
    model = os.environ.get("GROQ_WHISPER_MODEL", "whisper-large-v3-turbo")
    with tempfile.TemporaryDirectory() as tmp:
        mp3 = os.path.join(tmp, "a.mp3")
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k", mp3],
                       check=True, timeout=300)
        for i in range(4):
            with open(mp3, "rb") as fh:
                r = requests.post(GROQ_URL, headers={"Authorization": "Bearer " + key},
                                  files={"file": ("a.mp3", fh, "audio/mpeg")},
                                  data=[("model", model), ("response_format", "verbose_json"),
                                        ("timestamp_granularities[]", "word"), ("timestamp_granularities[]", "segment")],
                                  timeout=180)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(min(30, 2 ** (i + 1)))
                continue
            if r.status_code != 200:
                raise RuntimeError(f"Groq HTTP {r.status_code}: {r.text[:200]}")
            data = r.json()
            break
        else:
            raise RuntimeError("Groq is busy")
    words = [{"start": w["start"], "end": w["end"], "word": w["word"]} for w in data.get("words", [])]
    out = []
    for s in data.get("segments", []):
        text = (s.get("text") or "").strip()
        if not text or s.get("no_speech_prob", 0) > 0.8:          # Whisper invents words over silence/music
            continue
        a, b = s["start"], s["end"]
        out.append({"start": a, "end": b, "text": text,
                    "words": [w for w in words if w["start"] >= a - 0.05 and w["end"] <= b + 0.05]})
    return out


def _local(path: str) -> list[dict]:
    global _model
    from faster_whisper import WhisperModel
    if _model is None:
        _model = WhisperModel(os.environ.get("WHISPER_MODEL", "base"), device="cpu", compute_type="int8")
    segs, _info = _model.transcribe(path, word_timestamps=True, vad_filter=True)
    out = []
    for s in segs:
        words = [{"start": w.start, "end": w.end, "word": w.word} for w in (s.words or [])]
        out.append({"start": s.start, "end": s.end, "text": s.text.strip(), "words": words})
    return out


def transcribe(path: str) -> list[dict]:
    if os.environ.get("GROQ_API_KEY"):
        try:
            return _groq(path)
        except Exception as e:
            print(f"[transcribe] Groq failed ({str(e)[:160]}); using the local model")
    return _local(path)
