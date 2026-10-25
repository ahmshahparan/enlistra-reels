"""Selection agent: score candidate windows, then let the LLM pick and write hooks.

Step 1 (heuristic): slide a window across the film, score each by motion energy,
cut density and dialogue density, snap edges to scene cuts, and keep the top N
non-overlapping windows.
Step 2 (LLM, optional): hand the candidates plus film context to Claude, which
returns the ones most likely to work as reels with a hook, caption and on-screen
title. If no API key is set, the heuristic ranking is used with templated text.
"""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, asdict, field

import numpy as np

from .analyze import Analysis

log = logging.getLogger("select")


@dataclass
class Clip:
    start: float
    end: float
    score: float
    hook: str = ""          # on-screen title, 3 to 7 words
    caption: str = ""       # post caption
    why: str = ""           # LLM's rationale (kept for the feedback loop)
    transcript: str = ""
    narration: str = ""     # spoken intro line (vision step)
    payoff: float | None = None   # absolute time of the key moment
    sfx: list = field(default_factory=list)   # [{"t": abs seconds, "kind": str}]
    segments: list = field(default_factory=list)  # [[abs_start, abs_end], ...]; empty = whole clip
    title: str = ""         # platform title, searchable ("Charlie Chaplin ...")
    style: str = "cold"     # cold = action from frame one, music + sfx; cold_narrated = same plus voice

    def segs(self) -> list:
        return [list(map(float, s)) for s in self.segments] if self.segments else [[self.start, self.end]]

    @property
    def src_duration(self) -> float:
        return sum(e - s for s, e in self.segs())

    def rel(self, t: float):
        """Absolute film time -> seconds into the spliced (pre-speed) timeline, or None."""
        acc = 0.0
        for s, e in self.segs():
            if s <= t <= e:
                return acc + (t - s)
            acc += e - s
        return None

    @property
    def duration(self) -> float:
        return self.src_duration


def _snap(t: float, cuts: list[float], tol: float = 2.5) -> float:
    if not cuts:
        return t
    near = min(cuts, key=lambda c: abs(c - t))
    return near if abs(near - t) <= tol else t


def heuristic_candidates(a: Analysis, cfg: dict) -> list[Clip]:
    motion = np.array(a.motion, dtype=float)
    if motion.max() > 0:
        motion = motion / motion.max()
    n = len(motion)
    cuts_per_sec = np.zeros(n)
    for c in a.cuts:
        i = int(c)
        if i < n:
            cuts_per_sec[i] += 1
    dialog = np.zeros(n)
    for seg in a.transcript:
        s, e = int(seg["start"]), min(int(seg["end"]) + 1, n)
        dialog[s:e] = 1
    win = int((cfg["clip_seconds_min"] + cfg["clip_seconds_max"]) / 2)
    step = max(3, win // 4)
    # ignore the first and last 4 percent (titles, credits)
    lo, hi = int(n * 0.04), int(n * 0.96) - win
    scored = []
    for s in range(lo, max(lo + 1, hi), step):
        e = s + win
        m = motion[s:e].mean()
        c = min(cuts_per_sec[s:e].sum() / (win / 6), 1.0)   # ~1 cut per 6s = max
        d = dialog[s:e].mean() if a.has_dialogue else 0.0
        score = cfg["w_motion"] * m + cfg["w_cuts"] * c + cfg["w_dialogue"] * d
        scored.append((score, s, e))
    scored.sort(reverse=True)
    picked: list[Clip] = []
    for score, s, e in scored:
        if any(not (e <= p.start or s >= p.end) for p in picked):
            continue
        s2, e2 = _snap(s, a.cuts), _snap(e, a.cuts)
        if not (cfg["clip_seconds_min"] <= e2 - s2 <= cfg["clip_seconds_max"]):
            s2, e2 = float(s), float(e)
        text = " ".join(seg["text"] for seg in a.transcript
                        if seg["start"] >= s2 and seg["end"] <= e2)
        picked.append(Clip(start=round(s2, 2), end=round(e2, 2),
                           score=round(float(score), 3), transcript=text))
        if len(picked) >= cfg["candidates_per_film"]:
            break
    return picked


SYSTEM = """You are the editor for a short-form video account that posts clips from
public domain films. You receive candidate windows from one film with their
signal scores (motion, cuts, dialogue) and timestamps. Pick the windows most
likely to stop a scroll: a clear physical gag, a reveal, a reaction, a
complete beat with a beginning and payoff. Prefer windows that stand alone
without context. Return strict JSON only."""


def _call_llm(system: str, user: str, cfg: dict) -> str:
    """Backends: ollama (local, free, default) or anthropic (hosted)."""
    backend = cfg.get("llm_backend", "ollama")
    if backend == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        import anthropic
        msg = anthropic.Anthropic().messages.create(
            model=cfg.get("anthropic_model", "claude-sonnet-5-5"), max_tokens=4000,
            system=system, messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    import requests
    r = requests.post(f"{cfg.get('ollama_url', 'http://localhost:11434')}/api/chat", json={
        "model": cfg.get("ollama_model", "llama3.1:8b"), "stream": False, "format": "json",
        "options": {"temperature": 0.4},
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }, timeout=300)
    r.raise_for_status()
    return r.json()["message"]["content"]


def _fallback(film_title: str, year, candidates: list[Clip], want: int) -> list[Clip]:
    out = candidates[:want]
    for c in out:
        c.hook = f"{film_title} ({year})" if year else film_title
        c.caption = f"{film_title}{f' ({year})' if year else ''}. Public domain classic."
    return out


def llm_rank(film_title: str, year, candidates: list[Clip], want: int, cfg: dict) -> list[Clip]:
    if not cfg.get("use_llm_rank"):
        return _fallback(film_title, year, candidates, want)
    payload = {
        "film": film_title, "year": year, "pick": want,
        "candidates": [{"id": i, "start": c.start, "end": c.end, "score": c.score,
                        "transcript": c.transcript[:400]} for i, c in enumerate(candidates)],
    }
    user = (json.dumps(payload) + "\n\nReturn JSON: {\"picks\": [{\"id\": int, "
            "\"hook\": \"3-7 word on-screen title, no quotes\", \"caption\": \"1-2 sentence "
            "post caption, no hashtags\", \"why\": \"one line\"}]}")
    try:
        text = _call_llm(SYSTEM, user, cfg).strip()
        text = text[text.find("{"): text.rfind("}") + 1]
        picks = json.loads(text)["picks"][:want]
    except Exception as e:  # noqa: BLE001
        log.warning("LLM ranking failed (%s); using heuristic order", e)
        return _fallback(film_title, year, candidates, want)
    out = []
    for p in picks:
        c = candidates[int(p["id"])]
        c.hook, c.caption, c.why = p.get("hook", ""), p.get("caption", ""), p.get("why", "")
        out.append(c)
    return out


def select_clips(a: Analysis, film_title: str, year, cfg: dict, video=None,
                 picks_file=None, sheets_dir=None) -> list[Clip]:
    cands = heuristic_candidates(a, cfg)
    log.info("%d candidates from heuristic", len(cands))
    want = cfg["reels_per_film"]
    from . import vision
    if picks_file is not None:
        picked = vision.load_picks_file(picks_file, cands)
        if picked:
            return picked
    if cfg.get("use_vision", True) and video is not None:
        try:
            picked = vision.rank(video, film_title, year, cands, want, cfg,
                                 sheets_dir or video.parent.parent / "sheets")
            if picked:
                log.info("vision ranking picked %d clips", len(picked))
                return picked
            log.info("vision ranking unavailable (no ANTHROPIC_API_KEY); using text ranker")
        except Exception as e:  # noqa: BLE001
            log.warning("vision ranking failed (%s); using text ranker", e)
    return llm_rank(film_title, year, cands, want, cfg)
