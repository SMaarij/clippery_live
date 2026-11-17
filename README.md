# clippery_live

The ClipperyAI live watcher

Follows a live Twitch channel and clips its best moments while the stream is still running, without ever
downloading the whole stream to your computer. Started from the ClipperyAI admin panel's **Live** page; it runs
on this repo's GitHub Actions.

## How it works

```
streamlink (Twitch HLS, ads skipped) --> ffmpeg -c copy --> 4 s segments in a rolling buffer (last ~10 min only)
Twitch chat (anonymous IRC) --> spike detector --> moment --> cut from buffer --> transcribe --> snap to sentences
                                                                   --> render 9:16 + captions --> R2 + live_clips
```

* **Recording costs almost nothing.** The stream is copied, never re-encoded, and old segments are deleted, so an
  8-hour stream never uses more than a few minutes of disk. Only the moments get rendered.
* **Spike detection** (`detector.py`). Score per 10 s window = *different people chatting* x (1 + 3 x share of them
  reacting: KEKW, LMAO, "clip it", ????, emoji...). A spike is >= `ratio` x the median of the last 3 minutes, from at
  least `min_unique` people, with a 45 s cooldown. Commands, bots and sub/raid notices are ignored, and one person
  spamming can't trigger it. Sensitivity presets: low (2.8x, 12 people), normal (2.2x, 8), high (1.8x, 5).
* **Viewer clips** (`twitch_api.py`, optional). When viewers press "Clip" on Twitch, that moment is clipped too. Needs
  `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` from a free Twitch app.
* **Clean edges** (`clipper.refine_bounds`). The window is 22 s before the moment and 10 s after, then moved to whole
  sentences using the transcript (`transcribe.py`: Groq, or a local Whisper model), so clips don't start or stop
  mid-word. 12-60 s long.
* **Layouts.** `center` (middle of the frame) or `stack` (facecam corner on top, gameplay below, captions at the seam).
* **Storage.** Clips go to the same Cloudflare R2 bucket as the ClipperyAI website (`live/` prefix) and are listed in
  the `live_clips` table. `live/cleanup.py` deletes them after `LIVE_RETENTION_DAYS` (default 30), daily.
* **Long streams.** A GitHub run is capped at 6 h, so after ~5.4 h the run starts the next run for the same watch and
  keeps recording until the new one has warmed up and taken over. `live_add_clip` refuses a second clip within 20 s
  of one already saved, so the overlap never produces duplicates.

## Running it

From the admin panel (normal use): **Live -> Watch a channel**.

Setup, once:
1. The database tables come from the ClipperyAI repo: `supabase/migrations/20261117000000_live_watcher.sql`.
2. This repo's secrets (Settings -> Secrets and variables -> Actions): `SUPABASE_URL`, `SUPABASE_SECRET_KEY`,
   `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_BUCKET_UPLOADS`, `GROQ_API_KEY`; optional
   `TWITCH_CLIENT_ID` / `TWITCH_CLIENT_SECRET` for viewer clips.
3. The admin panel needs a fine-grained GitHub token with **Actions: Read and write** on this repo (`GITHUB_TOKEN`).

By hand, without the database (clips land in a folder):

```
pip install -r requirements.txt
python -m live.watcher --channel somechannel --local clips_out --max-hours 1
python -m live.watcher --channel somechannel --local clips_out --settings "{\"layout\":\"stack\",\"facecam\":\"top-left\"}"
```

Create `clips_out/STOP` to stop a local run early.

## Tests

```
python tests/test_live.py          # unit tests + an offline end-to-end run with a generated stream
python tests/test_live.py --quick  # unit tests only
```

## Limits (phase 1)

* Twitch only. YouTube Live needs a different chat source and blocks recording from datacenter servers; Kick next.
* This repo is public, so its Actions minutes are free; its Actions logs are public too (secrets are masked, and so
  is the channel name).
* Only watch channels you own or are authorised to clip (for example paid clipping campaigns).

No license: the code is visible, but all rights are reserved.
