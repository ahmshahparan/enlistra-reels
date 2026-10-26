"""Narration throughout a long-form film: a light commentary track laid over a finished render.

The narrator speaks about once a minute: what is going on, who is on screen, the craft behind a gag,
a little history. Quiet during big gags and while intertitles are up. The score dips under each line.
Works on the finished mp4 (video is copied, only the audio is rebuilt), so it takes minutes, not an hour.
"""
from __future__ import annotations

import base64
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from . import edit

log = logging.getLogger("commentary")

SYSTEM = """You are the narrator of a restored silent comedy on a YouTube channel. You write a light commentary
track that plays over the whole film, like a friendly, knowledgeable guide sitting next to the viewer.

You get frames sampled through the finished video with their timestamps. The first and last cards are the
channel's own intro and outro (already narrated): do not speak over those.

Write lines that:
- come in about every 40 to 75 seconds across the whole film, so there is never a long silence and never a crowd;
- are 1 or 2 short sentences each, 6 to 14 seconds when read aloud;
- add something the picture does not: who a performer is, why a gag works, how a stunt was done, what the
  audience of the time would have known, how the scene sets up what comes next;
- never just describe what is plainly on screen, never spoil a gag before it lands, never speak while an
  intertitle card fills the frame (let viewers read);
- use only well-established facts about the film, its cast and its era; if you are not sure of a fact, talk about
  the craft you can see instead. Never invent names, dates or numbers;
- sound warm and plain. No em dashes or en dashes. Spell numbers as words if they will be read aloud.

Return strict JSON only: {"lines": [{"t": seconds_from_start_of_video, "text": "..."}]}"""


def _frames(video: Path, every: float, width: int = 512) -> list[tuple[float, bytes]]:
    dur = edit._dur(video)
    out, t = [], every / 2
    while t < dur:
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.1f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "5", "-"],
                           capture_output=True)
        if r.stdout:
            out.append((t, r.stdout))
        t += every
    return out


def write(video: Path, film: dict, cfg: dict, avoid: list[float] | None = None) -> list[dict]:
    import anthropic
    dur = edit._dur(video)
    every = max(12.0, dur / 90)                       # at most ~90 frames per request
    frames = _frames(video, every)
    content = []
    for t, b in frames:
        content.append({"type": "text", "text": f"t={t:.0f}s"})
        content.append({"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                                    "data": base64.b64encode(b).decode()}})
    content.append({"type": "text", "text": json.dumps({
        "film": film, "video_seconds": round(dur),
        "already_narrated_moments_s": [round(a) for a in (avoid or [])],
        "target_lines": max(4, int(dur / 55))})})
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("select", {}).get("anthropic_model", "claude-sonnet-5-5"), max_tokens=12000,
        system=SYSTEM, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    lines = json.loads(text[text.find("{"): text.rfind("}") + 1]).get("lines", [])
    return sorted([ln for ln in lines if ln.get("text") and 0 <= float(ln.get("t", -1)) < dur],
                  key=lambda ln: float(ln["t"]))


def apply(video: Path, lines: list[dict], cfg: dict, out: Path, gap: float = 3.0) -> list[dict]:
    """Voice the lines, keep them from overlapping, duck the score under them, copy the picture."""
    tmp = Path(tempfile.mkdtemp(prefix="comm_"))
    dur = edit._dur(video)
    placed, end_prev = [], 0.0
    for i, ln in enumerate(lines):
        wav = edit.narrate(ln["text"].replace("—", ", ").replace("–", "-"), cfg.get("edit", {}),
                           tmp / f"n{i:03d}.wav")
        if not wav:
            continue
        d = edit._dur(wav)
        t = max(float(ln["t"]), end_prev + gap)
        if t + d > dur - 2:
            continue
        placed.append({"t": round(t, 2), "d": round(d, 2), "text": ln["text"], "wav": str(wav)})
        end_prev = t + d
    if not placed:
        raise RuntimeError("no narration could be voiced (is the Piper voice installed?)")
    ins = ["-i", str(video)] + sum((["-i", p["wav"]] for p in placed), [])
    f = []
    for k, p in enumerate(placed, start=1):
        ms = int(p["t"] * 1000)
        f.append(f"[{k}:a]adelay={ms}|{ms}[n{k}]")
    f.append("".join(f"[n{k}]" for k in range(1, len(placed) + 1)) +
             f"amix=inputs={len(placed)}:normalize=0:duration=longest,apad=whole_dur={dur:.2f},asplit=2[nar][key]")
    f.append("[0:a]aresample=44100,aformat=channel_layouts=stereo[bed]")
    f.append("[bed][key]sidechaincompress=threshold=0.02:ratio=10:attack=30:release=600:makeup=1[ducked]")
    f.append("[ducked][nar]amix=inputs=2:normalize=0:duration=first,alimiter=limit=0.95[a]")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(f), "-map", "0:v", "-map", "[a]",
                    "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(out)], check=True)
    for p in placed:
        Path(p["wav"]).unlink(missing_ok=True)
    return [{k: p[k] for k in ("t", "d", "text")} for p in placed]


def narrate_feature(name: str, cfg: dict) -> dict:
    """Add the commentary to a finished long-form reel in place and send it back to the approval queue."""
    import shutil
    from . import store
    st = store.get()
    r = st.get_reel(name)
    if not r:
        raise RuntimeError(f"unknown reel {name}")
    mp4, man = st.local_files(name)
    src = Path(mp4).resolve()
    film = {"title": (r.get("source") or {}).get("title"), "year": (r.get("source") or {}).get("year")}
    lines = write(src, film, cfg)
    tmp_out = src.with_name(src.stem + ".narrated.mp4")
    placed = apply(src, lines, cfg, tmp_out)
    shutil.move(str(tmp_out), src)                    # replace the render in place (vault or out)
    m = json.loads(Path(man).read_text())
    m["commentary"] = placed
    Path(man).write_text(json.dumps(m, indent=2))
    st.update_reel(name, stage="pending", owner_note=None)
    st.activity("queue", f"{name}.mp4 now has narration throughout ({len(placed)} lines); ready for your approval",
                reel=name)
    return {"lines": len(placed)}
