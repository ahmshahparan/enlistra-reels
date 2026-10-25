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
MIN_LEN, MAX_LEN = 14.0, 24.0


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
domain silent comedies with a deadpan narrator. You see contact sheets (12 frames each,
timestamps burned in) for candidate windows from one film. Pick windows that contain a
complete, visible physical gag with SETUP and PAYOFF, and describe ONLY what is visibly
happening. Never invent action you cannot see.

Cut rules (hard):
- A pick is ONE gag: setup, payoff, done. It is 1 to 3 SEGMENTS inside its candidate
  window, spliced in order; use segments only to cut dead air (each at least 3 seconds).
  Total spliced length 14 to 24 seconds. Cut tight; a reel that feels long is wrong.
- Include setup: the payoff must come no earlier than 9 seconds after the start and
  no later than 75 percent of the way through. Leave 2 to 3 seconds after the payoff
  and END THERE: never run on into unrelated footage after the payoff.
- No dead air: if the action pauses for more than 3 seconds, cut it out with a segment
  boundary or choose a different window. Timestamps for payoff and sfx are absolute
  film seconds and must fall inside one of the segments.
- The narrator speaks for about 6 seconds from the start, so nothing important may
  happen in the first 6 seconds; the setup should be readable, the payoff later.
- Picks must come from different candidates and must not overlap.

Narration style: dry and deadpan, two or three short sentences, 22 words max, with one
turn the viewer did not expect, like a punchline. Open with the year spoken as words
("Nineteen sixteen.") ONLY if the year is known; otherwise open with the setting. Never
say "watch as" or describe the gag itself; let the footage deliver it. No puns about
silent film, no exclamation marks. Hooks: 3 to 6 words, wry, true, and never spoil
the payoff.

Fields: "gag" is an internal note for the editor (what happens, any caveats). "caption"
is what viewers read under the post: one or two short wry sentences, under 150
characters, in the narrator's voice, that tease the scene without describing the payoff.
Never mention timestamps, candidates, contact sheets, frames or anything about this
editing process in hook, narration or caption.

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
                       "picks": [{"id": 0, "gag": "...", "hook": "...", "narration": "...", "caption": "...",
                                  "segments": [[0.0, 0.0], [0.0, 0.0]], "payoff": 0.0,
                                  "sfx": [{"t": 0.0, "kind": "thud"}]}]}}
    msg_text = f"Return exactly {want} picks, best first. " + json.dumps(spec)
    if feedback:
        msg_text += ("\n\nA previous attempt was rejected by the quality check. Fix these issues "
                     f"with different timing or a different candidate:\n{feedback}")
    content.append({"type": "text", "text": msg_text})
    client = anthropic.Anthropic()
    text, msg = "", None
    for attempt in range(2):
        msg = client.messages.create(
            model=cfg.get("anthropic_model", "claude-sonnet-5-5"), max_tokens=8000,
            system=SYSTEM + ("\n\nBRAND BRAIN (house rules, override the defaults above):\n" + brain() if brain() else ""),
            messages=[{"role": "user", "content": content}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        if "{" in text and "}" in text:
            break
        log.warning("vision reply had no JSON (stop_reason=%s); retrying", msg.stop_reason)
        content = content + [{"type": "text", "text": "Reply with the JSON object only."}]
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
        segs = [[round(float(a), 2), round(float(b), 2)] for a, b in segs if float(b) - float(a) >= 2.0]
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
        c.payoff = float(p["payoff"]) if p.get("payoff") is not None else None
        c.sfx = [s for s in p.get("sfx", []) if s.get("kind") in SFX_KINDS]
        out.append(c)
    return out


def load_picks_file(path: Path, candidates: list[Clip]) -> list[Clip] | None:
    if not path.exists():
        return None
    log.info("using hand-made picks from %s", path)
    return apply_picks(candidates, json.loads(path.read_text())["picks"])
