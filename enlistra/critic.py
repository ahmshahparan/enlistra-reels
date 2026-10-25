"""Critic: decides whether a rendered reel is good enough to post.

Layer 1, gates (free, deterministic). Each failure is a sentence the vision step can
act on. A reel that fails a gate is regenerated, not posted.
Layer 2, judge (model). Frames of the finished reel plus a timeline of what is heard
go to Claude with a rubric. Scores 1 to 5 with reasons; below `pass_score` the reel
is regenerated once, then flagged for a human.

Scorecards are written next to the reel as <name>.critic.json.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
from pathlib import Path

import numpy as np

from .select import Clip

log = logging.getLogger("critic")


# ------------------------------------------------------------------ layer 1
def prune_sfx(clip: Clip, analysis, floor: float = 0.12) -> list[dict]:
    """Drop sound cues that land on near-still frames (run before rendering)."""
    if analysis is None or not clip.sfx:
        return clip.sfx
    m = np.array(analysis.motion, dtype=float)
    if m.max() > 0:
        m = m / m.max()
    keep = []
    for s in clip.sfx:
        i = int(float(s["t"]))
        lo, hi = max(0, i - 1), min(len(m), i + 2)
        if hi > lo and m[lo:hi].max() >= floor:
            keep.append(s)
        else:
            log.info("dropping %s@%.1f: no motion there", s["kind"], float(s["t"]))
    clip.sfx = keep
    return keep


def gates(clip: Clip, analysis, narr_dur: float, speed: float, cfg: dict) -> list[str]:
    issues = []
    src_len = clip.src_duration
    out_len = src_len / speed
    lo, hi = cfg.get("min_len", 8), cfg.get("max_len", 26)
    if not (lo <= out_len <= hi):
        issues.append(f"reel is {out_len:.1f}s; it must be between {lo} and {hi} seconds")
    # the opening must move: mean motion of the first second vs the clip
    if analysis is not None:
        import numpy as _np
        m = _np.array(analysis.motion, dtype=float)
        if m.max() > 0:
            m = m / m.max()
            s0 = int(clip.segs()[0][0])
            first = m[s0:s0 + 2].mean() if s0 + 2 <= len(m) else 0
            whole = m[int(clip.start):int(clip.end) + 1].mean() if clip.end > clip.start else 0
            if whole > 0 and first < 0.35 * whole:
                issues.append("the first second is nearly still; start the first segment on movement")
    p_rel = clip.rel(clip.payoff) if clip.payoff is not None else None
    if clip.payoff is None:
        issues.append("no payoff timestamp was given")
    elif p_rel is None:
        issues.append(f"payoff {clip.payoff:.1f}s is not inside any segment")
    else:
        p = p_rel / speed
        if p < 2.5:
            issues.append(f"payoff at {p:.1f}s is too early; include a little setup")
        if out_len - p < 0.8:
            issues.append("less than 1 second after the payoff; extend the end")
        if out_len - p > 6:
            issues.append(f"the reel runs {out_len - p:.1f}s past the payoff; end 1 to 2 seconds after it")
        if narr_dur > 0:
            narr_end = cfg.get("narration_start", 0.3) + narr_dur
            if p < narr_end + cfg.get("voice_gap", 0.5):
                issues.append(f"narration ends at {narr_end:.1f}s but the payoff is at {p:.1f}s; "
                              f"shorten the narration so it ends before the payoff")
    words = len((getattr(clip, "narration", "") or "").split())
    if narr_dur > 0 and words > 22:
        issues.append(f"narration is {words} words; keep it under 18")
    if not (getattr(clip, "title", "") or "").strip():
        issues.append("no platform title was given")
    return issues


def mix_levels(reel: Path, narr_end: float) -> dict:
    """Peak levels (dBFS) during narration and after, from the rendered file."""
    def peak(ss, t):
        p = subprocess.run(["ffmpeg", "-v", "info", "-ss", f"{ss:.2f}", "-t", f"{t:.2f}", "-i",
                            str(reel), "-af", "volumedetect", "-f", "null", "-"],
                           capture_output=True, text=True).stderr
        for line in p.splitlines():
            if "mean_volume" in line:
                return float(line.split("mean_volume:")[1].split("dB")[0])
        return None
    return {"voice_mean_db": peak(0.5, max(narr_end - 0.5, 1)),
            "after_mean_db": peak(narr_end + 0.5, 3.0)}


# ------------------------------------------------------------------ layer 2
RUBRIC = """You are a tough short-form video editor reviewing a finished 9:16 Short made
from a public domain silent comedy. Channels posting plain Chaplin fight and chase clips
get tens of millions of views; this reel competes with them. You see frames from the
finished reel in order (timestamps burned in) and a timeline of what is heard. The layout
(footage full width over a blurred fill, bold text above) is fixed: do not score it.
Calibration: 3 = fine to post, 4 = good, 5 = would share it. Score 1 to 5, one blunt
sentence of reason each:

hook       does the very first second already show motion or a situation you want resolved
legible    is it clear at a glance who we are watching (Charlie) and what is at stake
payoff     is there a visible, physical payoff (hit, fall, escape, reversal)
title      would the platform title make someone tap: plain, searchable, true, not spoiling
pace       no dead air anywhere; ends quickly after the payoff
overall    would a stranger scrolling watch to the end and replay (this is the one that counts)

Then "fix": the single most useful change, as an instruction to the editor
(e.g. "start 3 seconds later, on the first shove", "cut the last 4 seconds", "the title
should say he fights the big man"). Return strict JSON:
{"scores": {"hook": n, "legible": n, "payoff": n, "title": n, "pace": n, "overall": n},
 "reasons": {...same keys...}, "fix": "..."}"""


def _brain() -> str:
    from .vision import brain
    return brain("voice")


def _frames(reel: Path, out: Path, n: int = 10) -> Path:
    from .vision import contact_sheet
    dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                         "-of", "csv=p=0", str(reel)], text=True).strip())
    return contact_sheet(reel, 0, dur, out, n=n, cols=5, tile_w=216)


def judge(reel: Path, clip: Clip, film: dict, narr_dur: float, speed: float, cfg: dict) -> dict | None:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    import anthropic
    sheet = _frames(reel, reel.with_suffix(".critic.jpg"))
    out_len = (clip.end - clip.start) / speed
    timeline = {
        "duration_s": round(out_len, 1),
        "text_on_screen": clip.hook,
        "platform_title": getattr(clip, "title", ""),
        "narration": ({"text": getattr(clip, "narration", ""), "from_s": cfg.get("narration_start", 0.3),
                       "to_s": round(cfg.get("narration_start", 0.3) + narr_dur, 1)}
                      if narr_dur > 0 else "none (music and sound effects only)"),
        "payoff_s": round(clip.rel(clip.payoff) / speed, 1) if clip.payoff is not None and clip.rel(clip.payoff) is not None else None,
        "segments": clip.segs(),
        "sfx": [{"t": round(clip.rel(float(s["t"])) / speed, 1), "kind": s["kind"]} for s in clip.sfx if clip.rel(float(s["t"])) is not None],
        "music": "public domain ragtime" + (", ducked under the narration" if narr_dur > 0 else ""),
        "film": film,
    }
    data = base64.standard_b64encode(sheet.read_bytes()).decode()
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("anthropic_model", "claude-sonnet-5-5"), max_tokens=4000,
        system=RUBRIC + ("\n\nHouse voice rules the narration must follow:\n" + _brain() if _brain() else ""),
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": data}},
            {"type": "text", "text": "Timeline of what is heard:\n" + json.dumps(timeline, indent=1)}]}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    text = text[text.find("{"): text.rfind("}") + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        log.warning("judge returned unparsable text")
        return None


def review(reel: Path, clip: Clip, film: dict, analysis, narr_dur: float, speed: float,
           cfg: dict) -> dict:
    """Run both layers, write the scorecard, return it with a verdict."""
    issues = gates(clip, analysis, narr_dur, speed, cfg)
    card = {"gates": issues, "judge": None, "verdict": "pass", "feedback": ""}
    if issues:
        card["verdict"] = "regenerate"
        card["feedback"] = "; ".join(issues)
    elif cfg.get("use_judge", True):
        j = judge(reel, clip, film, narr_dur, speed, cfg)
        card["judge"] = j
        if j and j.get("scores"):
            overall = float(j["scores"].get("overall", 0))
            if overall < cfg.get("target_score", 3.5):
                card["verdict"] = "regenerate"
                card["feedback"] = f"judge scored overall {overall}/5: {j.get('fix', '')}"
    card["levels"] = mix_levels(reel, cfg.get("narration_start", 0.4) + narr_dur)
    reel.with_suffix(".critic.json").write_text(json.dumps(card, indent=2))
    log.info("critic: %s %s", reel.name, card["verdict"] + (" | " + card["feedback"] if card["feedback"] else ""))
    return card
