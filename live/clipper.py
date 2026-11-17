"""
Turns one detected moment into a finished vertical clip.

1. Wait until the buffer holds a few seconds past the moment, then cut [moment - pre - pad, moment + post + pad]
   out of it (stream copy: instant).
2. Transcribe that window (Groq when the key is set, local Whisper otherwise) and move the start and end to
   whole sentences, so a clip never starts or stops mid-word: `refine_bounds`.
3. Render 9:16 with burned-in, word-timed captions in one of two layouts:
     center : the middle of the frame (gameplay with no facecam, or a creator in the middle)
     stack  : the facecam on top and the gameplay underneath (the usual gaming-clip layout)
4. Write an .srt and a thumbnail next to it.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass

W, H = 1080, 1920

FACECAM_PRESETS = {       # x, y, w, h as fractions of the source frame
    "top-left": (0.0, 0.0, 0.27, 0.27),
    "top-right": (0.73, 0.0, 0.27, 0.27),
    "bottom-left": (0.0, 0.73, 0.27, 0.27),
    "bottom-right": (0.73, 0.73, 0.27, 0.27),
}


@dataclass
class ClipResult:
    video: str
    srt: str | None
    thumb: str | None
    raw: str
    start_wall: float            # wall-clock time of the clip's first frame
    duration: float
    transcript: str


# ------------------------------------------------------------------------------------------------ pure helpers
def refine_bounds(segments: list[dict], start: float, end: float, total: float,
                  min_len: float = 12.0, max_len: float = 60.0, snap: float = 6.0) -> tuple[float, float]:
    """Move [start, end] (seconds in the file) to sentence boundaries from transcript `segments`.

    - start: if it lands inside a sentence that began at most `snap` s earlier, start at that sentence; if it
      lands in a pause right before a sentence, start at that sentence.
    - end: if it lands inside a sentence that finishes at most `snap` s later, end with it.
    The result stays inside [0, total] and between min_len and max_len long.
    """
    segs = sorted((s for s in segments if s.get("end", 0) > s.get("start", 0)), key=lambda s: s["start"])
    s0, e0 = start, end
    for s in segs:
        if s["start"] <= s0 <= s["end"]:
            if s0 - s["start"] <= snap:
                s0 = s["start"] - 0.15
            break
        if s0 < s["start"] <= s0 + 2.0:
            s0 = s["start"] - 0.15
            break
    for s in segs:
        if s["start"] <= e0 <= s["end"]:
            if s["end"] - e0 <= snap:
                e0 = s["end"] + 0.35
            break
    s0 = max(0.0, s0)
    e0 = min(total, e0)
    if e0 - s0 > max_len:
        e0 = s0 + max_len
    if e0 - s0 < min_len:
        grow = min_len - (e0 - s0)
        s0 = max(0.0, s0 - grow * 0.6)
        e0 = min(total, s0 + min_len)
    return round(s0, 2), round(e0, 2)


def caption_events(segments: list[dict], start: float, end: float, max_words: int = 3) -> list[tuple[float, float, str]]:
    """Word-timed caption chunks (relative to `start`) for the part of the transcript inside [start, end]."""
    words = []
    for s in segments:
        ws = s.get("words") or []
        if ws:
            words += [w for w in ws if w["end"] > start and w["start"] < end]
        elif s["end"] > start and s["start"] < end:            # no word timings: spread the sentence evenly
            toks = s["text"].split()
            step = (s["end"] - s["start"]) / max(1, len(toks))
            words += [{"start": s["start"] + i * step, "end": s["start"] + (i + 1) * step, "word": t} for i, t in enumerate(toks)]
    events, chunk = [], []
    for w in words:
        chunk.append(w)
        word = w["word"].strip()
        if len(chunk) >= max_words or word.endswith((".", "!", "?", ",")):
            events.append(chunk)
            chunk = []
    if chunk:
        events.append(chunk)
    out = []
    for c in events:
        a = max(0.0, c[0]["start"] - start)
        b = max(a + 0.35, min(end, c[-1]["end"]) - start)
        text = " ".join(w["word"].strip() for w in c).strip()
        if text:
            out.append((round(a, 2), round(b, 2), text))
    for i in range(len(out) - 1):                                # no overlaps: each chunk ends when the next starts
        if out[i][1] > out[i + 1][0]:
            out[i] = (out[i][0], out[i + 1][0], out[i][2])
    return out


def _ass_time(t: float) -> str:
    cs = int(round(t * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def write_ass(events, path: str, align_top_y: int | None = None) -> None:
    """Bold white captions with a heavy outline. Bottom-middle by default; `align_top_y` puts them at that height
    instead (the seam between facecam and gameplay in the stack layout)."""
    if align_top_y is None:
        align, margin = 2, 520
    else:
        align, margin = 8, align_top_y
    head = ("[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
            "[V4+ Styles]\nFormat: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
            "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
            "MarginL, MarginR, MarginV, Encoding\n"
            f"Style: C,Liberation Sans,82,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,{align},80,80,{margin},1\n\n"
            "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")
    with open(path, "w", encoding="utf-8") as f:
        f.write(head)
        for a, b, text in events:
            safe = text.upper().replace("{", "(").replace("}", ")").replace("\n", " ")
            f.write(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},C,,0,0,0,,{safe}\n")


def write_srt(events, path: str) -> None:
    def ts(t):
        ms = int(round(t * 1000))
        return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"
    with open(path, "w", encoding="utf-8") as f:
        for i, (a, b, text) in enumerate(events, 1):
            f.write(f"{i}\n{ts(a)} --> {ts(b)}\n{text}\n\n")


def stack_geometry(src_w: int, src_h: int, cam: tuple[float, float, float, float]) -> dict:
    """Crop boxes for the stack layout: facecam scaled to the full width on top, gameplay filling the rest."""
    fx, fy, fw, fh = cam
    cw, ch = max(2, int(src_w * fw) // 2 * 2), max(2, int(src_h * fh) // 2 * 2)
    cx, cy = int(src_w * fx) // 2 * 2, int(src_h * fy) // 2 * 2
    cx, cy = min(cx, src_w - cw), min(cy, src_h - ch)
    top = int(round(W * ch / cw / 2)) * 2
    top = max(480, min(900, top))
    bottom = H - top
    gh = src_h
    gw = int(gh * W / bottom) // 2 * 2
    if gw > src_w:
        gw = src_w
        gh = int(gw * bottom / W) // 2 * 2
    gx, gy = (src_w - gw) // 2 // 2 * 2, (src_h - gh) // 2 // 2 * 2
    return {"cam": (cw, ch, cx, cy), "top": top, "game": (gw, gh, gx, gy), "bottom": bottom}


# ------------------------------------------------------------------------------------------------ media steps
def probe(path: str, ffprobe: str = "ffprobe") -> tuple[int, int, float]:
    r = subprocess.run([ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                        "-show_entries", "format=duration", "-of", "json", path], capture_output=True, text=True, timeout=60)
    d = json.loads(r.stdout or "{}")
    st = (d.get("streams") or [{}])[0]
    return int(st.get("width", 0)), int(st.get("height", 0)), float((d.get("format") or {}).get("duration", 0) or 0)


def render(raw: str, start: float, end: float, segments: list[dict], out_path: str, layout: str = "center",
           facecam: str = "top-right", ffmpeg: str = "ffmpeg", threads: int = 0) -> list:
    """Render [start, end] of `raw` as a 1080x1920 clip with captions. Returns the caption events."""
    events = caption_events(segments, start, end)
    folder = os.path.dirname(os.path.abspath(out_path))
    src_w, src_h, _ = probe(raw)
    ass = os.path.join(folder, os.path.basename(out_path) + ".ass")
    if layout == "stack":
        cam = FACECAM_PRESETS.get(facecam) if isinstance(facecam, str) else tuple(facecam)
        g = stack_geometry(src_w, src_h, cam or FACECAM_PRESETS["top-right"])
        write_ass(events, ass, align_top_y=g["top"] + 60)
        cw, ch, cx, cy = g["cam"]
        gw, gh, gx, gy = g["game"]
        vf = (f"[0:v]split=2[a][b];[a]crop={cw}:{ch}:{cx}:{cy},scale={W}:{g['top']},setsar=1[cam];"
              f"[b]crop={gw}:{gh}:{gx}:{gy},scale={W}:{g['bottom']},setsar=1[game];"
              f"[cam][game]vstack=inputs=2,subtitles={os.path.basename(ass)}[v]")
    else:
        write_ass(events, ass)
        cw = int(src_h * 9 / 16) // 2 * 2
        vf = f"[0:v]crop={min(cw, src_w)}:{src_h}:(iw-{min(cw, src_w)})/2:0,scale={W}:{H},setsar=1,subtitles={os.path.basename(ass)}[v]"
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{start:.3f}", "-t", f"{end - start:.3f}",
           "-i", os.path.abspath(raw), "-filter_complex", vf, "-map", "[v]", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]
    if threads:
        cmd += ["-threads", str(threads)]
    subprocess.run(cmd + [os.path.abspath(out_path)], check=True, timeout=900, cwd=folder)
    try:
        os.remove(ass)
    except OSError:
        pass
    return events


def thumbnail(video: str, out: str, at: float, ffmpeg: str = "ffmpeg") -> str | None:
    r = subprocess.run([ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-ss", f"{max(0.0, at):.2f}", "-i", video,
                        "-frames:v", "1", "-vf", "scale=360:-2", "-q:v", "4", out], timeout=60)
    return out if r.returncode == 0 and os.path.exists(out) else None


def transcribe(path: str) -> list[dict]:
    from live import transcribe as stt
    try:
        return stt.transcribe(path)
    except Exception as e:
        print(f"[clip] transcription failed ({str(e)[:160]}); clip will have no captions")
        return []


def make_clip(buffer, moment: float, workdir: str, name: str, settings: dict, wait_timeout: float = 120.0,
              stream_ended=lambda: False, log=print) -> ClipResult:
    """Cut, refine and render the moment at wall-clock `moment`. Raises if the recording does not cover it."""
    pre, post, pad = float(settings.get("pre", 22)), float(settings.get("post", 10)), 6.0
    a, b = moment - pre - pad, moment + post + pad
    deadline = time.time() + wait_timeout
    while buffer.newest < b and time.time() < deadline and not stream_ended():
        time.sleep(1.0)
    os.makedirs(workdir, exist_ok=True)
    raw = os.path.join(workdir, f"{name}_raw.mp4")
    first, total = buffer.cut(a, b, raw)
    segments = transcribe(raw)
    s, e = refine_bounds(segments, a + pad - first, b - pad - first, total,
                         min_len=float(settings.get("min_len", 12)), max_len=float(settings.get("max_len", 60)))
    out = os.path.join(workdir, f"{name}.mp4")
    events = render(raw, s, e, segments, out, layout=settings.get("layout", "center"),
                    facecam=settings.get("facecam", "top-right"))
    srt = os.path.join(workdir, f"{name}.srt")
    write_srt(events, srt)
    thumb = thumbnail(out, os.path.join(workdir, f"{name}.jpg"), (e - s) / 2)
    text = " ".join(t for _, _, t in events)
    log(f"[clip] {name}: {e - s:.1f}s, {len(events)} caption lines")
    return ClipResult(video=out, srt=srt if events else None, thumb=thumb, raw=raw, start_wall=first + s,
                      duration=round(e - s, 2), transcript=text[:2000])
