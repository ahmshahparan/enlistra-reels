"""Character pipeline (Bolt): bible -> episode -> shots -> assembled reel -> critic.

1. Bible       brain/<account>/bible.md (approved by the owner) + reference images
               (front, side, back, expressions) stored under settings["bible_refs"].
2. Episode     Claude writes a beat sheet: 6 to 10 shots of 4 to 8 seconds, each with
               a shot prompt, a sound cue and the goal/attempt/reversal it serves. The
               variety ledger (previous episodes) is in the prompt so stories differ.
3. Shots       Google Veo via the Gemini API, one call per shot, with the bible's
               reference images. Spend is metered per shot and capped per month
               (cfg character.monthly_budget_usd, default 250).
4. Assemble    concat shots, cold-open text, music bed + CC0 sound cues, fades.
5. Critic      the AI judge with the character rubric; the owner approves in the
               dashboard as with every other channel.

Needs GEMINI_API_KEY. Model names are configurable because Google renames previews.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from . import store

log = logging.getLogger("character")
GL = "https://generativelanguage.googleapis.com/v1beta"


# ------------------------------------------------------------------ budget
def _month() -> str:
    return time.strftime("%Y-%m")


def spent(st) -> float:
    d = st.setting("gen_spend") or {}
    return float(d.get(_month(), 0.0))


def _charge(st, usd: float):
    d = st.setting("gen_spend") or {}
    d[_month()] = round(float(d.get(_month(), 0.0)) + usd, 4)
    st.set_setting("gen_spend", d)


def _check_budget(st, cfg: dict, cost: float):
    cap = float(cfg.get("monthly_budget_usd", 250))
    if spent(st) + cost > cap:
        raise RuntimeError(f"generation budget reached: ${spent(st):.2f} of ${cap:.0f} this month")


# ------------------------------------------------------------------ gemini calls
def _key() -> str:
    k = os.environ.get("GEMINI_API_KEY")
    if not k:
        raise RuntimeError("GEMINI_API_KEY is not set")
    return k


def make_image(prompt: str, refs: list[bytes], cfg: dict) -> bytes:
    """One still (character sheet view or expression) from the image model."""
    parts = [{"inline_data": {"mime_type": "image/png", "data": base64.b64encode(r).decode()}} for r in refs]
    parts.append({"text": prompt})
    model = cfg.get("image_model", "gemini-2.5-flash-image")
    r = requests.post(f"{GL}/models/{model}:generateContent", headers={"x-goog-api-key": _key()},
                      json={"contents": [{"parts": parts}]}, timeout=180)
    r.raise_for_status()
    for c in r.json().get("candidates", []):
        for p in c.get("content", {}).get("parts", []):
            data = p.get("inline_data") or p.get("inlineData")
            if data:
                return base64.b64decode(data["data"])
    raise RuntimeError(f"no image returned: {r.text[:300]}")


def make_shot(prompt: str, refs: list[bytes], seconds: int, cfg: dict, out: Path) -> Path:
    """One video shot from Veo, with up to 3 reference images of the character."""
    st = store.get()
    price = float(cfg.get("video_usd_per_second", 0.15))
    cost = price * seconds
    _check_budget(st, cfg, cost)
    model = cfg.get("video_model", "veo-3.1-fast-generate-preview")
    inst = {"prompt": prompt}
    if refs:
        inst["referenceImages"] = [{"image": {"bytesBase64Encoded": base64.b64encode(r).decode(), "mimeType": "image/png"},
                                    "referenceType": "asset"} for r in refs[:3]]
    body = {"instances": [inst], "parameters": {"aspectRatio": "9:16", "durationSeconds": seconds,
                                                "personGeneration": "dont_allow"}}
    r = requests.post(f"{GL}/models/{model}:predictLongRunning", headers={"x-goog-api-key": _key()},
                      json=body, timeout=120)
    if r.status_code >= 300:
        raise RuntimeError(f"Veo request failed {r.status_code}: {r.text[:400]}")
    op = r.json()["name"]
    for _ in range(90):                                   # up to ~15 minutes
        time.sleep(10)
        j = requests.get(f"{GL}/{op}", headers={"x-goog-api-key": _key()}, timeout=60).json()
        if j.get("done"):
            break
    else:
        raise RuntimeError("Veo timed out")
    if "error" in j:
        raise RuntimeError(f"Veo error: {j['error']}")
    _charge(st, cost)
    samples = (j.get("response", {}).get("generateVideoResponse", {}) or {}).get("generatedSamples") or []
    if not samples:
        raise RuntimeError(f"Veo returned no video (possibly filtered): {json.dumps(j)[:400]}")
    uri = samples[0]["video"]["uri"]
    v = requests.get(uri, headers={"x-goog-api-key": _key()}, timeout=300, allow_redirects=True)
    v.raise_for_status()
    out.write_bytes(v.content)
    return out


# ------------------------------------------------------------------ bible
SHEET_VIEWS = {
    "front": "Character sheet, front view, full body, centred on a plain light grey background.",
    "side": "Character sheet, side view facing right, full body, plain light grey background.",
    "action": "The character mid-action, reaching up towards a kitchen counter edge, seen from floor height.",
}


def build_bible(cfg: dict) -> dict:
    """Generate reference stills from the written bible; stored for owner approval."""
    st = store.get()
    bible = st.brain("bible")
    if not bible:
        raise RuntimeError("no bible.md in the brand brain")
    refs: dict = {}
    first = None
    for view, framing in SHEET_VIEWS.items():
        prompt = f"{framing}\n\nCharacter description (follow exactly):\n{bible[:3500]}"
        img = make_image(prompt, [first] if first else [], cfg)
        first = first or img
        path = st.out / f"bible_{view}.png"
        path.write_bytes(img)
        if getattr(st, "kind", "") == "supabase":
            st._upload(path, st._obj(path.name), "image/png")
        refs[view] = path.name
        _charge(st, float(cfg.get("image_usd", 0.04)))
    st.set_setting("bible_refs", {"views": refs, "approved": False, "at": time.strftime("%Y-%m-%dT%H:%M:%S")})
    st.activity("generator", f"Character sheet drafted ({len(refs)} views); waiting for your approval")
    return refs


def _ref_bytes(st) -> list[bytes]:
    info = st.setting("bible_refs") or {}
    if not info.get("approved"):
        raise RuntimeError("the character sheet is not approved yet")
    out = []
    for name in (info.get("views") or {}).values():
        p = st.media(name)
        if p:
            out.append(Path(p).read_bytes())
    return out


# ------------------------------------------------------------------ episode
EPISODE_SYSTEM = """You write wordless slapstick episodes for a recurring animated
character. Read the bible and follow it exactly. Return strict JSON:
{"title": "platform title, 40 to 70 characters, names the character and the situation",
 "hook": "on-screen text, 2 to 5 words",
 "caption": "one wry sentence under 140 characters",
 "logline": "one sentence: goal, obstacle, twist",
 "shots": [{"seconds": 4|6|8, "beat": "goal|attempt|reversal|win|button",
            "prompt": "a complete, self-contained video prompt for this shot: setting, camera,
                       action, the character's look restated briefly, lighting, style; no words
                       or text on screen; no human faces",
            "sound": "one of: whirr, clink, chirp_yes, boop_no, ratchet, thud, crash, boing, whoosh,
                      squeak, none"}]}
Rules: 6 to 10 shots, total 45 to 75 seconds; the goal is visible in shot 1; three attempts
that fail in different ways; a reversal; end on a sound gag. Each shot must stand alone as a
prompt because each is generated separately. Avoid any premise listed under PREVIOUS."""


def write_episode(cfg: dict) -> dict:
    import anthropic
    st = store.get()
    prev = [r.get("title") or r.get("hook") for r in st.reels()[:40]]
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("anthropic_model", "claude-sonnet-5-5"), max_tokens=8000, system=EPISODE_SYSTEM,
        messages=[{"role": "user", "content": f"BIBLE:\n{st.brain('bible')}\n\nVOICE:\n{st.brain('voice')}\n\n"
                                              f"PREVIOUS:\n" + "\n".join(f"- {p}" for p in prev if p)}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


# ------------------------------------------------------------------ assemble
def assemble(shots: list[Path], ep: dict, out: Path, cfg: dict) -> Path:
    from .edit import overlay_text, pick_music, pick_sfx, _dur
    import random
    W, H = 1080, 1920
    tmp = Path(tempfile.mkdtemp(prefix="bolt_"))
    lst = tmp / "list.txt"
    norm = []
    for i, s in enumerate(shots):
        n = tmp / f"s{i}.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(s), "-vf",
                        f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps=30,setsar=1",
                        "-an", "-c:v", "libx264", "-crf", "19", "-pix_fmt", "yuv420p", str(n)], check=True)
        norm.append(n)
    lst.write_text("".join(f"file '{p}'\n" for p in norm))
    cat = tmp / "cat.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(cat)],
                   check=True)
    dur = _dur(cat)
    text = overlay_text(W, H, int(H * 0.16), int(H * 0.7), ep.get("hook", ""), cfg.get("watermark", ""), tmp / "t.png")
    rng = random.Random(ep.get("title", ""))
    inputs = ["-i", str(cat), "-loop", "1", "-t", f"{dur:.2f}", "-i", str(text)]
    agraph, mix, idx = [], [], 2
    music = pick_music(Path(cfg.get("music_dir", "assets/music")), rng)
    if music:
        inputs += ["-t", f"{dur + 0.5:.2f}", "-i", str(music)]
        agraph.append(f"[{idx}:a]volume={cfg.get('music_volume', 0.5)}[mus]"); mix.append("[mus]"); idx += 1
    t = 0.0
    for k, (shot, s) in enumerate(zip(ep["shots"], shots)):
        kind = {"whirr": "whoosh", "clink": "clank", "chirp_yes": "twang", "boop_no": "boing",
                "ratchet": "squeak"}.get(shot.get("sound", "none"), shot.get("sound", "none"))
        f = pick_sfx(Path(cfg.get("sfx_dir", "assets/sfx")), kind, rng) if kind != "none" else None
        if f:
            ms = int((t + min(1.5, _dur(s) / 2)) * 1000)
            inputs += ["-i", str(f)]
            agraph.append(f"[{idx}:a]volume=0.8,adelay={ms}|{ms},apad=whole_dur={dur:.2f}[s{k}]"); mix.append(f"[s{k}]"); idx += 1
        t += _dur(s)
    if mix:
        agraph.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=longest,atrim=0:{dur:.2f},"
                      f"afade=t=out:st={dur - 0.8:.2f}:d=0.8,alimiter=limit=0.95[aout]")
    else:
        agraph.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{dur:.2f}[aout]")
    fc = f"[0:v][1:v]overlay=0:0:shortest=1,fade=t=out:st={dur - 0.4:.2f}:d=0.4,format=yuv420p[vout];" + ";".join(agraph)
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc, "-map", "[vout]", "-map", "[aout]",
                    "-t", f"{dur:.2f}", "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
                    "-movflags", "+faststart", str(out)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(2.0, dur / 3):.2f}", "-i", str(out), "-frames:v", "1",
                    "-q:v", "3", str(out.with_suffix(".jpg"))], check=True)
    return out


# ------------------------------------------------------------------ run
def run(cfg: dict, post_now: bool = False) -> list[Path]:
    st = store.get()
    ccfg = cfg.get("character", {})
    if not (st.setting("bible_refs") or {}).get("approved"):
        st.activity("generator", "Bolt is waiting for the character sheet to be approved")
        return []
    refs = _ref_bytes(st)
    ep = write_episode({**ccfg, "anthropic_model": cfg["select"].get("anthropic_model")})
    est = sum(int(s.get("seconds", 6)) for s in ep["shots"]) * float(ccfg.get("video_usd_per_second", 0.15))
    _check_budget(st, ccfg, est)
    st.activity("generator", f"Episode '{ep['title']}': {len(ep['shots'])} shots, est. ${est:.2f}")
    name = f"bolt_{time.strftime('%Y%m%d_%H%M')}"
    tmp = Path(tempfile.mkdtemp(prefix="boltshots_"))
    shots = []
    for i, s in enumerate(ep["shots"]):
        shots.append(make_shot(s["prompt"], refs, int(s.get("seconds", 6)), ccfg, tmp / f"{i:02d}.mp4"))
    out = st.out / f"{name}.mp4"
    assemble(shots, ep, out, {**cfg["edit"], **ccfg})
    from . import critic, post
    from .select import Clip
    clip = Clip(start=0, end=0, score=0, hook=ep.get("hook", ""), caption=ep.get("caption", ""), why=ep.get("logline", ""))
    clip.title = ep.get("title", "")
    card = {"gates": critic.variety_issues(clip), "judge": None, "verdict": "pass", "feedback": ""}
    m = json.loads(post.write_manifest(out, {"identifier": name, "title": "Bolt", "year": None,
                                             "reason_free": "original, generated with Google Veo (AI disclosed)"},
                                       {"hook": clip.hook, "caption": clip.caption, "why": clip.why, "start": 0, "end": 0,
                                        "title": clip.title, "style": "character"}, cfg["post"]).read_text())
    m["critic"] = {"verdict": "flagged" if card["gates"] else "pass", "feedback": "; ".join(card["gates"])}
    m["approval"] = "pending"
    m["ai_generated"] = True
    st.save_reel(name, m, card)
    st.activity("queue", f"{name}.mp4 is ready for your approval: '{clip.title}' (spent ${spent(st):.2f} this month)")
    return [out]
