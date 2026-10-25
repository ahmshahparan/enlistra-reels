# Enlistra Reels

Agentic pipeline that turns free-to-use films into short-form reels and queues them for posting.

```
source  ->  analyze  ->  select  ->  edit  ->  post
Internet    motion,      heuristic   ffmpeg    manifest + Typefully draft,
Archive     scene cuts,  + Claude    9:16,     Instagram / TikTok / YouTube
(PD / CC)   transcript   ranking     captions  uploads when credentials exist
```

Only public domain and Creative Commons items are used. `source.is_free_to_use()`
accepts a film when it was published before 1929 (US public domain) or carries a
CC / public domain license. Everything else is skipped. Keep it that way: swapping
in copyrighted sources is the fastest route to a banned account.

## Quick start

```bash
pip install -r requirements.txt        # ffmpeg must be on PATH
python cli.py check silent-the-count   # verify an Internet Archive item is free to use
python cli.py run --id silent-the-count   # one film -> reels in work/out/
python cli.py run --films 3            # discover 3 new films from config queries
```

Each reel produces three files in `work/out/`: `name.mp4`, `name.jpg` (cover), and
`name.json` (manifest with caption, hashtags, source attribution and post status).
`work/state.jsonl` records every processed film so re-runs skip it.

## Environment variables

| Variable | Needed for |
|---|---|
| `ANTHROPIC_API_KEY` | Optional. Only if `llm_backend: anthropic`. Default is Ollama (local, free): `ollama pull llama3.1:8b`. Claude picks the best candidates and writes hooks and captions. Without it the top heuristic windows are used with templated text. |
| `TYPEFULLY_API_KEY` | Creates a draft (or schedules, in publish mode) for X, LinkedIn, Threads, Bluesky, Mastodon. |
| `IG_USER_ID`, `IG_ACCESS_TOKEN`, `PUBLIC_BASE_URL` | Instagram Reels via Graph API. Instagram fetches the video from a public URL, so serve `work/out/` somewhere (S3, Netlify, a small web server) and set `PUBLIC_BASE_URL`. |
| `YT_TOKEN_JSON` | Path to an OAuth token file for YouTube Shorts (`pip install google-api-python-client google-auth-oauthlib`). |
| `TIKTOK_ACCESS_TOKEN` | TikTok Content Posting API. Unapproved apps land posts in the creator's inbox as drafts. |

`post.mode: draft` (default) never publishes; it writes manifests and Typefully drafts
for review. Switch to `publish` once you have watched a week of output.

## Tuning

- `select.*` weights: for silent comedy, motion and cut density carry the signal.
  For sources with dialogue, install `faster-whisper` and raise `w_dialogue`.
- `edit.crop_mode`: `blur_bg` keeps the whole 4:3 frame over a blurred fill;
  `center` hard-crops to 9:16 (loses the edges, fills the screen).
- `edit.music_dir`: drop CC0 or licensed tracks in `assets/music/` and one is mixed
  in at `music_volume`. Silent films without a score get a silent track otherwise.
- `source.queries`: Internet Archive advanced-search syntax. Add Keaton, Lloyd,
  Prelinger, or any collection you like.

## Running on a schedule

Any box with Python 3.11+ and ffmpeg works. Example cron for two films a day:

```
0 9,17 * * * cd /opt/enlistra-reels && ANTHROPIC_API_KEY=... TYPEFULLY_API_KEY=... python cli.py run >> work/run.log 2>&1
```

## Roadmap (not built yet)

- Feedback loop: pull view / retention stats per platform, store next to the manifest,
  feed the top performers' timestamps and hooks back into the LLM ranking prompt.
- Intertitle OCR for silent films so the selector can read the gag setup text.
- Multi-clip montage mode (three gags, 10 s each) for higher completion rates.
