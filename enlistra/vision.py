"""Vision selection: let a model that can SEE the frames decide what the gag is.

For each candidate window we build one contact sheet (frames with timestamps) and
ask Claude to pick the best windows and return, per pick:
  gag        what literally happens (used for QA and the caption)
  hook       on-screen title, wry, true to the footage, 3 to 6 words
  narration  two or three short sentences, <= 22 words, deadpan, one twist
  start/end  the window to cut (seconds, absolute in the film), 18 to 32 s long
  payoff     timestamp of the key moment (absolute)
  sfx        list of {"t": abs seconds, "kind": one of SFX_KINDS}
Plus, once per film: film_title and year if the model recognises the short.

Candidate windows are wide (up to 45 s) so the model can choose where the setup
begins. The critic (critic.py) can send the model back here with `feedback`.

If ANTHROPIC_API_KEY is missing, the caller falls back to the text-only ranker.
A picks file (work/picks/<film>.json) overrides the model entirely.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .select import Clip

log = logging.getLogger("vision")
SFX_KINDS = ["boing", "thud", "crash", "glass", "whoosh", "snare", "squeak", "bed",
             "twang", "clank", "gong", "drumroll"]
MIN_LEN, MAX_LEN = 8.0, 26.0


def contact_sheet(video: Path, start: float, end: float, out: Path, n: int = 12,
                  cols: int = 4, tile_w: int = 320) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    step = (end - start) / n
    tiles = []
    for i in range(n):
        t = start + step * (i + 0.5)
        jpg = out.with_name(f"{out.stem}_f{i}.jpg")
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                        "-frames:v", "1", "-vf", f"scale={tile_w}:-2", "-q:v", "4", str(jpg)],
                       check=True)
        tiles.append((t, Image.open(jpg).convert("RGB")))
        jpg.unlink()
    th = max(im.height for _, im in tiles)
    rows = (n + cols - 1) // cols
    sheet = Image.new("RGB", (cols * tile_w, rows * (th + 22)), "black")
    d = ImageDraw.Draw(sheet)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
    except OSError:
        font = ImageFont.load_default()
    for i, (t, im) in enumerate(tiles):
        x, y = (i % cols) * tile_w, (i // cols) * (th + 22)
        sheet.paste(im, (x, y + 22))
        d.text((x + 6, y + 3), f"t={t:.1f}s", font=font, fill="yellow")
    sheet.save(out, quality=80)
    return out


SYSTEM = """You are the editor for a short-form video account that posts clips from public
domain silent comedies (mostly Charlie Chaplin). You see contact sheets (12 frames each,
timestamps burned in) for candidate windows from one film. Describe ONLY what is visibly
happening. Never invent action you cannot see.

What wins on Shorts in this niche (from channels with 100M-view Chaplin clips):
- Physical conflict reads instantly with no context: Charlie vs a much bigger man,
  slaps and kicks traded, boxing, fights with doors, police chases and escapes,
  falls, things hitting people, Charlie outwitting a bully. Underdog wins > subtle gags.
- The clip starts IN the action. The first second must already show Charlie moving or
  something about to happen. No establishing shots, no slow walk-ins.
- Short: one beat, then out. 10 to 25 seconds is the sweet spot; 8 is fine if the
  gag is complete.

Cut rules (hard):
- A pick is ONE beat, 1 to 3 SEGMENTS inside its candidate window, spliced in order;
  use segments only to cut dead air (each at least 2 seconds). Total spliced length
  8 to 25 seconds.
- The first segment must start on visible motion, at most 1 second before the action.
- The payoff (the hit, the fall, the escape, the reversal) comes at least 3 seconds in.
  Leave 1 to 2 seconds after it and END: never run on into unrelated footage.
- No dead air: cut any pause longer than 2 seconds. Timestamps for payoff and sfx are
  absolute film seconds and must fall inside one of the segments.
- Picks must come from different candidates and must not overlap. Prefer candidates
  where Charlie (the tramp: bowler hat, cane, moustache) is clearly visible.

Text fields:
- "title": the platform title, plain and searchable, 40 to 70 characters, starts with
  "Charlie Chaplin" (or the star's name if it is someone else), says what happens in
  everyday words, may end with one fitting emoji. Examples of the register:
  "Charlie Chaplin vs the big man who would not stop pushing",
  "Charlie Chaplin escapes four policemen at once".
- "hook": on-screen text, 2 to 6 words, punchy, true, does not spoil the payoff.
- "narration": two short deadpan sentences, 18 words max, with one unexpected turn.
  It plays OVER the action from the first second, so it must work while things move and
  must end before the payoff. Open with the year in words only if the year is certain.
  Never describe the gag; no puns about silent film; no exclamation marks.
- "caption": what viewers read under the post: one or two wry sentences under 150
  characters that tease without describing the payoff.
- "gag": an internal note for the editor (what happens, any caveats).
Never mention timestamps, candidates, contact sheets, frames or anything about this
editing process in title, hook, narration or caption.

If you recognise the film (a Chaplin, Keaton, Lloyd, Arbuckle or Linder short), give
its real title and year in film_title and year; otherwise null. Return strict JSON."""


def brain(name: str = "voice") -> str:
    """Editable brand brain (brain/<name>.md); appended to the system prompt if present."""
    from . import store
    try:
        return store.get().brain(name)
    except Exception:  # noqa: BLE001
        p = Path("brain") / f"{name}.md"
        return p.read_text().strip() if p.exists() else ""


def _img_block(path: Path) -> dict:
    data = base64.standard_b64encode(path.read_bytes()).decode()
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}}


def rank(video: Path, film_title: str, year, candidates: list[Clip], want: int,
         cfg: dict, sheets_dir: Path, feedback: str | None = None) -> dict | None:
    """Returns {"clips": [...], "film_title": str|None, "year": int|None} or None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    content = []
    for i, c in enumerate(candidates):
        sheet = sheets_dir / f"cand_{i:02d}.jpg"
        if not sheet.exists():
            contact_sheet(video, c.start, c.end, sheet)
        content.append({"type": "text", "text": f"Candidate {i}: {c.start:.1f}s to {c.end:.1f}s"})
        content.append(_img_block(sheet))
    spec = {"film_title_hint": film_title, "year_hint": year, "pick": want, "sfx_kinds": SFX_KINDS,
            "format": {"film_title": "real title or null", "year": "int or null",
                       "picks": [{"id": 0, "gag": "...", "title": "...", "hook": "...", "narration": "...", "caption": "...",
                                  "segments": [[0.0, 0.0], [0.0, 0.0]], "payoff": 0.0,
                                  "sfx": [{"t": 0.0, "kind": "thud"}]}]}}
    msg_text = f"Return exactly {want} picks, best first. " + json.dumps(spec)
    if feedback:
        msg_text += ("\n\nA previous attempt was rejected by the quality check. Fix these issues "
                     f"with different timing or a different candidate:\n{feedback}")
    content.append({"type": "text", "text": msg_text})
    system = SYSTEM
    pf = cfg.get("prompt_file")
    if pf and Path(pf).exists():
        system = Path(pf).read_text()
    client = anthropic.Anthropic()
    text, msg = "", None
    for attempt in range(2):
        msg = client.messages.create(
            model=cfg.get("anthropic_model", "claude-sonnet-5-5"), max_tokens=16000,
            system=system + ("\n\nBRAND BRAIN (house rules, override the defaults above):\n" + brain() if brain() else ""),
            messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if "{" in text and "}" in text:
            break
        log.warning("vision reply had no JSON (stop_reason=%s); retrying", msg.stop_reason)
        content = content + [{"type": "text", "text": "Your last reply ran out of room before the JSON. Reply with the JSON object only: no analysis, no preamble, short field values."}]
    text = text[text.find("{"): text.rfind("}") + 1]
    data = json.loads(text)
    log.info("vision used %d in / %d out tokens", msg.usage.input_tokens, msg.usage.output_tokens)
    clips = apply_picks(candidates, data.get("picks", [])[:want])
    return {"clips": clips, "film_title": data.get("film_title"), "year": data.get("year")}


def apply_picks(candidates: list[Clip], picks: list[dict]) -> list[Clip]:
    out = []
    for p in picks:
        base = candidates[int(p["id"])] if p.get("id") is not None and int(p["id"]) < len(candidates) else Clip(0, 0, 0)
        segs = p.get("segments") or [[p.get("start", base.start), p.get("end", base.end)]]
        segs = [[round(float(a), 2), round(float(b), 2)] for a, b in segs if float(b) - float(a) >= 1.5]
        segs.sort()
        total = sum(b - a for a, b in segs)
        if total > MAX_LEN:                      # trim the tail segment
            over = total - MAX_LEN
            segs[-1][1] = round(max(segs[-1][0] + 2.0, segs[-1][1] - over), 2)
        c = Clip(start=segs[0][0], end=segs[-1][1], score=base.score,
                 hook=p.get("hook", ""), caption=p.get("caption") or p.get("hook", ""), why=p.get("gag", ""),
                 transcript=base.transcript)
        c.segments = segs
        c.narration = p.get("narration", "")
        c.title = (p.get("title") or "").strip()
        c.fact_confidence = p.get("fact_confidence")
        c.payoff = float(p["payoff"]) if p.get("payoff") is not None else None
        c.sfx = [s for s in p.get("sfx", []) if s.get("kind") in SFX_KINDS]
        out.append(c)
    return out


def load_picks_file(path: Path, candidates: list[Clip]) -> list[Clip] | None:
    if not path.exists():
        return None
    log.info("using hand-made picks from %s", path)
    return apply_picks(candidates, json.loads(path.read_text())["picks"])
