"""Leaf & Ridge: the owner's own scenery footage, polished and turned into calm Shorts and long videos.

    upload (dashboard)  ->  polish()  ->  library  ->  make_short() / make_long()  ->  approval queue

polish()      HDR to SDR, trim the shaky ends, stabilize, light denoise, sharpen, a colour grade chosen
              from what is in the shot (autumn, forest, mountain, road, water, snow), smooth slow motion
              when the clip was filmed at 50/60 fps, normalised natural sound.
make_short()  a 15 to 30 second vertical cut that follows the most interesting part of the frame,
              calm music under the natural sound, one quiet line of text.
make_long()   30 to 90 minutes: the library's landscape clips flowing into each other with slow fades,
              full-length public domain piano and strings, chapters, title, description, thumbnail.

Footage lives on the persistent volume (FOOTAGE_DIR, default /data/footage), not in the database store.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import store

log = logging.getLogger("footage")

LOOKS = {   # gentle grades; the footage should still look like the place it is
    "autumn":   "eq=contrast=1.05:saturation=1.14:gamma=0.98,colorbalance=rs=0.04:gs=0.01:bs=-0.05:rm=0.03:bm=-0.03",
    "forest":   "eq=contrast=1.05:saturation=1.08:gamma=0.98,colorbalance=gs=0.02:bs=-0.015:gm=0.01",
    "mountain": "eq=contrast=1.07:saturation=1.05,colorbalance=bs=0.03:bh=0.02:rm=0.01",
    "road":     "eq=contrast=1.05:saturation=1.06,colorbalance=rm=0.02:bm=-0.02",
    "water":    "eq=contrast=1.04:saturation=1.06,colorbalance=bs=0.03:gs=0.01",
    "snow":     "eq=contrast=1.03:saturation=0.98:brightness=0.03:gamma=1.04,colorbalance=bs=0.02:bh=0.01",
    "neutral":  "eq=contrast=1.03:saturation=1.04",
}
HDR_TO_SDR = ("zscale=t=linear:npl=100,format=gbrpf32le,zscale=p=bt709,tonemap=tonemap=hable:desat=0,"
              "zscale=t=bt709:m=bt709:r=tv,format=yuv420p")


# ------------------------------------------------------------------ paths and library
def root() -> Path:
    base = os.environ.get("FOOTAGE_DIR") or ("/data/footage" if Path("/data").is_dir() else "work/footage")
    p = Path(base) / store.current_account()
    for sub in ("raw", "polished", "out"):
        (p / sub).mkdir(parents=True, exist_ok=True)
    return p


def library(st=None) -> list[dict]:
    return (st or store.get()).setting("footage_library") or []


def _save_library(lib: list[dict], st=None):
    (st or store.get()).set_setting("footage_library", lib)


def _probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, check=True)
    d = json.loads(r.stdout)
    v = next((s for s in d["streams"] if s["codec_type"] == "video"), {})
    a = next((s for s in d["streams"] if s["codec_type"] == "audio"), None)
    w, h = int(v.get("width", 0)), int(v.get("height", 0))
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(float(sd["rotation"]))
    rot = rot or int((v.get("tags") or {}).get("rotate", 0) or 0)
    if abs(rot) in (90, 270):
        w, h = h, w
    num, _, den = (v.get("avg_frame_rate") or "30/1").partition("/")
    fps = float(num) / float(den or 1) if float(den or 1) else 30.0
    tags = (d.get("format") or {}).get("tags") or {}
    return {"w": w, "h": h, "fps": round(fps, 2), "dur": float(d["format"].get("duration", 0)),
            "hdr": v.get("color_transfer") in ("arib-std-b67", "smpte2084"), "audio": a is not None,
            "created": tags.get("creation_time") or tags.get("com.apple.quicktime.creationdate"),
            "device": tags.get("com.apple.quicktime.model")}


def _frames(video: Path, n: int, width: int = 640) -> list[bytes]:
    dur = _probe(video)["dur"]
    out = []
    for i in range(n):
        t = dur * (i + 0.5) / n
        r = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video), "-frames:v", "1",
                            "-vf", f"scale={width}:-2", "-f", "image2", "-c:v", "mjpeg", "-q:v", "4", "-"],
                           capture_output=True)
        if r.stdout:
            out.append(r.stdout)
    return out


# ------------------------------------------------------------------ AI look at the footage
SCENE_PROMPT = """You are the editor of Leaf & Ridge, a calm nature channel built from the owner's own footage
(trees, leaves, roads, mountains, water, sky). Look at these frames from one clip, in time order.
Return strict JSON only:
{"scene": "what is in the shot, plainly, under 20 words",
 "look": "autumn|forest|mountain|road|water|snow|neutral",
 "season": "spring|summer|autumn|winter|unknown",
 "mood": "two or three words",
 "motion": "static|walking|driving|panning|drone",
 "slow_motion_suits": true or false (leaves falling, water, light through trees, wind in grass),
 "usable": true or false (false only if it is mostly blurred, pointed at the ground, or not scenery),
 "focus_x": 0.0 to 1.0, the horizontal centre of the most interesting part, for a vertical crop,
 "short_line": "one quiet line for a Short, 3 to 7 words, no hashtags, no emoji, e.g. 'A slow road through October'",
 "tags": ["5 to 8 lowercase search words"]}
Never invent a place name or location."""


def describe(video: Path, cfg: dict) -> dict:
    import anthropic
    imgs = _frames(video, 4)
    content = [{"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                            "data": base64.b64encode(b).decode()}} for b in imgs]
    content.append({"type": "text", "text": "Describe this clip."})
    msg = anthropic.Anthropic().messages.create(
        model=cfg.get("select", {}).get("anthropic_model", "claude-sonnet-5-5"), max_tokens=1200,
        system=SCENE_PROMPT, messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        d = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception:  # noqa: BLE001
        d = {}
    d.setdefault("look", "neutral")
    d.setdefault("usable", True)
    d.setdefault("focus_x", 0.5)
    d.setdefault("short_line", "")
    d.setdefault("scene", "")
    if d["look"] not in LOOKS:
        d["look"] = "neutral"
    return d


# ------------------------------------------------------------------ polish
def polish(raw: Path, cfg: dict, original_name: str | None = None) -> dict:
    """Clean up one uploaded clip and add it to the library."""
    st = store.get()
    info = _probe(raw)
    if info["dur"] < 3:
        raise RuntimeError(f"{raw.name} is only {info['dur']:.1f}s long")
    fid = f"lr{int(time.time())}{random.randint(100, 999)}"
    tmp = Path(tempfile.mkdtemp(prefix="polish_"))
    vertical = info["h"] > info["w"]
    W, H = (1080, 1920) if vertical else (1920, 1080)
    trim = 1.2 if info["dur"] > 8 else 0.3            # cut the shaky start and end
    span = max(1.0, info["dur"] - 2 * trim)

    # a quick look first (on a proxy) to choose grade and pace
    proxy = tmp / "proxy.mp4"
    pre = (HDR_TO_SDR + ",") if info["hdr"] else ""
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{trim}", "-t", f"{min(span, 40)}", "-i", str(raw),
                    "-vf", pre + "scale=640:-2", "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "28", str(proxy)],
                   check=True)
    notes = describe(proxy, cfg)
    if not notes.get("usable", True):
        st.activity("system", f"Footage {original_name or raw.name} skipped: {notes.get('scene') or 'not usable scenery'}")
        shutil.rmtree(tmp, ignore_errors=True)
        return {}

    slow = bool(notes.get("slow_motion_suits")) and info["fps"] >= 47
    trf = tmp / "t.trf"
    work = "scale='if(gt(iw,ih),min(2560,iw),-2)':'if(gt(iw,ih),-2,min(2560,ih))':flags=lanczos"
    # pass 1: measure the shake (skipped for still shots)
    if notes.get("motion") != "static":
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{trim}", "-t", f"{span}", "-i", str(raw),
                    "-vf", f"{pre}{work},vidstabdetect=shakiness=5:accuracy=9:stepsize=8:result={trf}",
                    "-f", "null", "-"], check=True)
    # pass 2: stabilize, clean, grade, pace; landscape master plus a vertical master cut from full resolution
    base = [pre.rstrip(",")] if pre else []
    # gentle: smooth the jitter, never chase big motion (that zooms in and throws the frame away)
    stab = [] if notes.get("motion") == "static" else [
        f"vidstabtransform=input={trf}:smoothing=12:optzoom=0:zoom=4:maxshift=48:maxangle=0.03:crop=keep:interpol=bicubic"]
    base += [work, *stab,
             "hqdn3d=1.5:1.5:4:4", LOOKS[notes["look"]]]
    if slow:
        base.append("setpts=2.0*PTS")
    base.append("fps=30")
    fx = max(0.0, min(1.0, float(notes.get("focus_x") or 0.5)))
    out = root() / "polished" / f"{fid}.mp4"
    vout = root() / "polished" / f"{fid}_v.mp4"
    enc = ["-c:v", "libx264", "-preset", "fast", "-crf", "20", "-tune", "film", "-movflags", "+faststart"]
    aud = ["-an"]
    if info["audio"] and not slow:
        aud = ["-map", "0:a:0", "-af", "highpass=f=60,afftdn=nf=-25,loudnorm=I=-24:TP=-3:LRA=11",
               "-c:a", "aac", "-b:a", "160k"]
    finish = "unsharp=5:5:0.55:5:5:0.0,vignette=angle=0.26,format=yuv420p"
    if vertical:
        fc = f"[0:v]{','.join(base)},scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,{finish}[V]"
        outs = ["-map", "[V]", *aud, *enc, str(vout)]
    else:
        fc = (f"[0:v]{','.join(base)},split=2[a][b];"
              f"[a]scale=1920:1080:force_original_aspect_ratio=increase:flags=lanczos,crop=1920:1080,{finish}[L];"
              f"[b]crop=trunc(ih*9/16/2)*2:ih:max(0\\,min(iw-ow\\,{fx:.3f}*iw-ow/2)):0,"
              f"scale=1080:1920:flags=lanczos,{finish}[V]")
        outs = ["-map", "[L]", *aud, *enc, str(out), "-map", "[V]", *aud, *enc, str(vout)]
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{trim}", "-t", f"{span}", "-i", str(raw),
                    "-filter_complex", fc, *outs], check=True)
    if vertical:
        out = vout
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "1", "-i", str(out), "-frames:v", "1", "-vf", "scale=480:-2",
                    "-q:v", "4", str(out.with_suffix(".jpg"))], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    entry = {"id": fid, "file": out.name, "vfile": vout.name, "original": original_name or raw.name, "w": W, "h": H,
             "vertical": vertical, "dur": round(_probe(out)["dur"], 2), "slow": slow, "audio": info["audio"] and not slow,
             "recorded": info["created"], "device": info["device"], "added": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "shorts": [], "longs": [], **{k: notes.get(k) for k in ("scene", "look", "season", "mood", "motion",
                                                                     "focus_x", "short_line", "tags")}}
    lib = library(st)
    lib.append(entry)
    _save_library(lib, st)
    st.activity("source", f"Footage polished: {entry['scene'] or entry['original']} ({entry['dur']:.0f}s, "
                          f"{entry['look']} grade{', slow motion' if slow else ''})")
    return entry


# ------------------------------------------------------------------ music
def _music(kind: str = "calm") -> list[Path]:
    from . import assets
    base = Path(os.environ.get("ASSETS_DIR") or ("/data/assets" if Path("/data").is_dir() else "work/assets"))
    have = sorted((base / f"music-{kind}").glob("*.m4a")) if (base / f"music-{kind}").exists() else []
    if len(have) < 6:
        try:
            have = assets.ensure_calm(base)
        except Exception as e:  # noqa: BLE001
            log.warning("calm music download failed: %s", e)
    if not have:
        have = sorted(Path("assets/music-nature").glob("*.m4a"))
    return have


def _music_bed(seconds: float, out: Path, seed: int) -> list[str]:
    """Join calm tracks with crossfades up to `seconds`; returns the track names used."""
    tracks = _music()
    rnd = random.Random(seed)
    rnd.shuffle(tracks)
    picked, total = [], 0.0
    i = 0
    while total < seconds + 10 and tracks:
        t = tracks[i % len(tracks)]
        picked.append(t)
        total += _probe(t)["dur"] - 6
        i += 1
    if len(picked) == 1:
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(picked[0]), "-t", f"{seconds:.2f}",
                        "-af", f"afade=t=in:d=3,afade=t=out:st={max(0, seconds - 5):.2f}:d=5", "-c:a", "aac",
                        "-b:a", "160k", str(out)], check=True)
    else:
        ins = sum((["-i", str(p)] for p in picked), [])
        f, prev = [], "[0:a]"
        for k in range(1, len(picked)):
            lab = f"[x{k}]"
            f.append(f"{prev}[{k}:a]acrossfade=d=6:c1=tri:c2=tri{lab}")
            prev = lab
        f.append(f"{prev}atrim=0:{seconds:.2f},afade=t=in:d=3,afade=t=out:st={max(0, seconds - 6):.2f}:d=6[out]")
        subprocess.run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex", ";".join(f), "-map", "[out]",
                        "-c:a", "aac", "-b:a", "160k", str(out)], check=True)
    return [p.stem for p in picked]


# ------------------------------------------------------------------ Shorts
def make_short(entry: dict, cfg: dict) -> str:
    """One calm vertical Short from a library clip; saved to the approval queue."""
    from . import edit, post
    st = store.get()
    src = root() / "polished" / (entry.get("vfile") or entry["file"])
    n = len(entry.get("shorts") or [])
    length = min(28.0, max(12.0, entry["dur"] - 1))
    start = 0.0 if n == 0 else max(0.0, min(entry["dur"] - length, entry["dur"] / 2 - length / 2 + 4 * n))
    name = f"{entry['id']}_s{n + 1}"
    out = st.out / f"{name}.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="lrshort_"))
    W, H = 1080, 1920
    vf = f"scale={W}:{H}"      # the vertical master was already cut around the interesting part
    text_png = edit.overlay_text(W, H, int(H * 0.60), int(H * 0.27), "", cfg["edit"].get("watermark", ""),
                                 tmp / "wm.png")
    line = (entry.get("short_line") or "").strip()
    line_png = _quiet_line(line, W, H, tmp / "line.png") if line else None
    bed = tmp / "bed.m4a"
    _music_bed(length, bed, seed=hash(name) & 0xFFFF)
    inputs = ["-ss", f"{start:.2f}", "-t", f"{length:.2f}", "-i", str(src), "-i", str(bed),
              "-loop", "1", "-t", f"{length:.2f}", "-i", str(text_png)]
    fg = f"[0:v]{vf},fade=t=in:d=0.8,fade=t=out:st={length - 1.2:.2f}:d=1.2[v0];[v0][2:v]overlay=0:0[v1]"
    last = "[v1]"
    if line_png:
        inputs += ["-loop", "1", "-t", f"{length:.2f}", "-i", str(line_png)]
        fg += f";[3:v]format=rgba,fade=t=in:st=0.6:d=1.2:alpha=1,fade=t=out:st={min(6.0, length - 2):.2f}:d=1.5:alpha=1[ln];{last}[ln]overlay=0:0[v2]"
        last = "[v2]"
    if entry.get("audio"):
        af = f"[0:a]volume=0.8[nat];[1:a]volume={cfg['edit'].get('music_volume', 0.5)}[mus];[nat][mus]amix=inputs=2:duration=first:normalize=0[a]"
    else:
        af = f"[1:a]volume={min(1.0, cfg['edit'].get('music_volume', 0.5) + 0.3)}[a]"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fg + ";" + af,
                    "-map", last, "-map", "[a]", "-t", f"{length:.2f}", "-c:v", "libx264", "-preset", "slow",
                    "-crf", "18", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart",
                    str(out)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", "2", "-i", str(out), "-frames:v", "1", "-q:v", "3",
                    str(out.with_suffix(".jpg"))], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    title = _short_title(entry)
    caption = (f"{line or entry.get('scene', '')}\n\nFilmed by us. Original footage, gently restored."
               f"\nMusic: public domain recordings (Musopen).")
    m = json.loads(post.write_manifest(out, {"identifier": entry["id"], "title": "Leaf & Ridge original footage",
                                             "year": None, "reason_free": "owner's original footage"},
                                       {"hook": line or title, "title": title, "caption": caption,
                                        "why": entry.get("scene", ""), "start": start, "end": start + length,
                                        "style": "scenery"}, cfg["post"]).read_text())
    m["critic"] = {"verdict": "pass", "feedback": ""}
    m["approval"] = "pending"
    st.save_reel(name, m, {"gates": [], "judge": None})
    lib = library(st)
    for e in lib:
        if e["id"] == entry["id"]:
            e.setdefault("shorts", []).append(name)
    _save_library(lib, st)
    st.activity("queue", f"Leaf & Ridge Short ready for your approval: '{title}'", reel=name)
    return name


def _quiet_line(text: str, W: int, H: int, out: Path) -> Path:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    from .edit import _sans
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf", 58)
        f.set_variation_by_name("Italic") if hasattr(f, "set_variation_by_name") else None
    except Exception:  # noqa: BLE001
        f = _sans(56)
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=f)
    x, y = (W - tw) / 2, H * 0.15
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).text((x, y + 3), text, font=f, fill=(0, 0, 0, 170))
    img = Image.alpha_composite(img, shadow.filter(ImageFilter.GaussianBlur(6)))
    ImageDraw.Draw(img).text((x, y), text, font=f, fill=(246, 240, 226, 255))
    img.save(out)
    return out


def _short_title(entry: dict) -> str:
    look = {"autumn": "Autumn", "forest": "Forest", "mountain": "Mountain", "road": "Quiet Road",
            "water": "Water", "snow": "Snow", "neutral": "Nature"}[entry.get("look") or "neutral"]
    line = (entry.get("short_line") or entry.get("scene") or "").rstrip(".")
    base = f"{line} 🍂" if entry.get("look") == "autumn" else line
    return (f"{base} | {look} Calm" if base else f"{look} Calm | Relaxing Nature")[:95]


# ------------------------------------------------------------------ long relaxing video
def make_long(cfg: dict, minutes: float | None = None) -> str | None:
    """Stitch the landscape library into one long calm video; saved to the approval queue."""
    from . import post
    st = store.get()
    lib = [e for e in library(st) if not e.get("vertical")]
    total = sum(e["dur"] for e in lib)
    lcfg = cfg.get("footage", {})
    need = float(lcfg.get("min_library_minutes", 12)) * 60
    if total < need:
        st.activity("system", f"Long video waiting for footage: {total / 60:.1f} of {need / 60:.0f} minutes of landscape clips polished")
        return None
    max_repeat = int(lcfg.get("max_repeat", 3))
    target = min(float(minutes or lcfg.get("long_minutes", 60)) * 60, total * max_repeat)
    # order: group by look so the video moves through moods slowly, then repeat with a new order
    rnd = random.Random(int(time.time()))
    order: list[dict] = []
    while sum(e["dur"] - 2 for e in order) < target:
        batch = sorted(lib, key=lambda e: (e.get("look") or "", rnd.random()))
        rnd.shuffle(batch) if len(order) else None
        order += batch
    tmp = Path(tempfile.mkdtemp(prefix="lrlong_"))
    segs, chapters, t = [], [], 0.0
    used = 0.0
    for i, e in enumerate(order):
        if used >= target:
            break
        d = min(e["dur"], target - used)
        if d < 4:
            break
        seg = tmp / f"s{i:04d}.mp4"
        src = root() / "polished" / e["file"]
        silence = [] if e.get("audio") else ["-f", "lavfi", "-t", f"{d:.2f}", "-i", "anullsrc=r=48000:cl=stereo"]
        a_map = "0:a" if e.get("audio") else "1:a"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-t", f"{d:.2f}", "-i", str(src), *silence,
                        "-vf", f"fade=t=in:d=1.5,fade=t=out:st={d - 1.5:.2f}:d=1.5", "-af",
                        f"afade=t=in:d=1.5,afade=t=out:st={d - 1.5:.2f}:d=1.5,aresample=48000,aformat=channel_layouts=stereo",
                        "-map", "0:v", "-map", a_map, "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                        "-r", "30", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ar", "48000", str(seg)],
                       check=True)
        segs.append(seg)
        if not chapters or chapters[-1][1] != e.get("look"):
            chapters.append((t, e.get("look"), e.get("scene")))
        t += d
        used += d
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{s}'\n" for s in segs))
    body = tmp / "body.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(body)],
                   check=True)
    dur = _probe(body)["dur"]
    bed = tmp / "bed.m4a"
    tracks = _music_bed(dur, bed, seed=int(time.time()))
    name = f"leafridge_long_{time.strftime('%Y%m%d%H%M')}"
    out = st.out / f"{name}.mp4"
    mv = cfg["edit"].get("music_volume", 0.5)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(body), "-i", str(bed), "-filter_complex",
                    f"[0:a]volume=0.7[n];[1:a]volume={mv}[m];[n][m]amix=inputs=2:duration=first:normalize=0[a]",
                    "-map", "0:v", "-map", "[a]", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                    "-movflags", "+faststart", str(out)], check=True)
    meta = _long_meta(lib, chapters, dur, tracks, cfg)
    _thumbnail(order[0], meta["thumb_text"], out.with_suffix(".jpg"))
    chap = "\n".join(f"{_ts(c[0])} {c[2] or c[1]}" for c in _spread(chapters, dur))
    caption = (f"{meta['description']}\n\n{chap}\n\nAll footage filmed by Leaf & Ridge. Music: public domain "
               f"recordings from Musopen ({', '.join(tr.split('_', 1)[-1] for tr in tracks[:4])}).")
    m = json.loads(post.write_manifest(out, {"identifier": name, "title": meta["title"], "year": None,
                                             "reason_free": "owner's original footage"},
                                       {"hook": meta["title"], "title": meta["title"], "caption": caption,
                                        "why": "long relaxing video", "start": 0, "end": dur, "style": "feature"},
                                       cfg["post"]).read_text())
    m["critic"] = {"verdict": "pass", "feedback": ""}
    m["approval"] = "pending"
    m["longform"] = True
    st.save_reel(name, m, {"gates": [], "judge": None})
    lib_all = library(st)
    used_ids = {e["id"] for e in order}
    for e in lib_all:
        if e["id"] in used_ids:
            e.setdefault("longs", []).append(name)
    _save_library(lib_all, st)
    st.set_setting("last_long_at", time.strftime("%Y-%m-%d"))
    st.activity("queue", f"Long relaxing video ready for approval: '{meta['title']}' ({dur / 60:.0f} min)", reel=name)
    shutil.rmtree(tmp, ignore_errors=True)
    return name


def _ts(s: float) -> str:
    s = int(s)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60}:{s % 60:02d}"


def _spread(chapters: list, dur: float) -> list:
    """YouTube chapters: first at 0:00, at least 10 seconds apart, at most ~12."""
    out = []
    for c in chapters:
        if not out or c[0] - out[-1][0] >= max(10, dur / 12):
            out.append(c)
    if out:
        out[0] = (0.0, out[0][1], out[0][2])
    return out


def _long_meta(lib: list[dict], chapters: list, dur: float, tracks: list[str], cfg: dict) -> dict:
    import anthropic
    looks = sorted({e.get("look") for e in lib})
    seasons = sorted({e.get("season") for e in lib if e.get("season") not in (None, "unknown")})
    scenes = "; ".join(sorted({e.get("scene") or "" for e in lib})[:20])
    hours = dur / 3600
    length = f"{round(hours)} Hour{'s' if round(hours) > 1 else ''}" if hours >= 0.9 else f"{round(dur / 60)} Minutes"
    prompt = (f"Write YouTube metadata for a {length} calm nature video from our own footage. Scenes: {scenes}. "
              f"Looks: {', '.join(looks)}. Seasons: {', '.join(seasons) or 'mixed'}. Music: gentle classical piano "
              f"and strings. Return strict JSON: {{\"title\": \"under 90 characters, mood plus what you see plus "
              f"'{length}' plus 'Relaxing Music' or 'Nature Sounds'; no place names; no emoji except at most one\", "
              f"\"description\": \"3 short paragraphs: what you will see, what it is good for (study, sleep, work, "
              f"reading), that the footage is ours and the music is public domain; no place names; end with 3 "
              f"hashtags\", \"thumb_text\": \"2 to 4 words for the thumbnail\"}}")
    try:
        msg = anthropic.Anthropic().messages.create(
            model=cfg.get("select", {}).get("anthropic_model", "claude-sonnet-5-5"), max_tokens=1500,
            messages=[{"role": "user", "content": prompt}])
        text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        d = json.loads(text[text.find("{"): text.rfind("}") + 1])
    except Exception as e:  # noqa: BLE001
        log.warning("long meta failed: %s", e)
        d = {}
    d.setdefault("title", f"Calm Nature Scenes | {length} of Relaxing Music and Nature Sounds")
    d.setdefault("description", "Slow, quiet nature footage with gentle music.\n\n#relaxingmusic #nature #calm")
    d.setdefault("thumb_text", f"{length} of calm")
    return d


def _thumbnail(entry: dict, text: str, out: Path):
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    src = root() / "polished" / entry["file"]
    frame = out.with_suffix(".frame.jpg")
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{entry['dur'] / 2:.1f}", "-i", str(src), "-frames:v", "1",
                    "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720", str(frame)], check=True)
    img = Image.open(frame).convert("RGB")
    grad = Image.new("L", (1280, 720), 0)
    gd = ImageDraw.Draw(grad)
    for y in range(720):
        gd.line([(0, y), (1280, y)], fill=int(150 * max(0, (y - 360) / 360)))
    img = Image.composite(Image.new("RGB", (1280, 720), (0, 0, 0)), img, grad)
    try:
        f = ImageFont.truetype("/usr/share/fonts/truetype/google-fonts/Lora-Variable.ttf", 96)
        f.set_variation_by_name("Bold")
    except Exception:  # noqa: BLE001
        f = ImageFont.load_default()
    d = ImageDraw.Draw(img)
    tw = d.textlength(text, font=f)
    sh = Image.new("RGBA", (1280, 720), (0, 0, 0, 0))
    ImageDraw.Draw(sh).text(((1280 - tw) / 2, 560), text, font=f, fill=(0, 0, 0, 200))
    img = Image.alpha_composite(img.convert("RGBA"), sh.filter(ImageFilter.GaussianBlur(8))).convert("RGB")
    ImageDraw.Draw(img).text(((1280 - tw) / 2, 556), text, font=f, fill=(246, 240, 226))
    img.save(out, quality=92)
    frame.unlink(missing_ok=True)


# ------------------------------------------------------------------ scheduled run
def run(cfg: dict, post_now: bool = False) -> list:
    """Each scheduled run: one new Short from footage that has the fewest Shorts; a long video weekly."""
    st = store.get()
    made = []
    lib = [e for e in library(st)]
    per_clip = int(cfg.get("footage", {}).get("shorts_per_clip", 2))
    fresh = sorted([e for e in lib if len(e.get("shorts") or []) < per_clip and e["dur"] >= 10],
                   key=lambda e: (len(e.get("shorts") or []), e.get("added", "")))
    if fresh:
        made.append(make_short(fresh[0], cfg))
    else:
        st.activity("system", "Leaf & Ridge: no unused footage for a new Short. Upload more clips from the dashboard.")
    last = st.setting("last_long_at")
    every = int(cfg.get("footage", {}).get("long_every_days", 7))
    if not last or (time.time() - time.mktime(time.strptime(last, "%Y-%m-%d"))) > every * 86400:
        name = make_long(cfg)
        if name:
            made.append(name)
    return made
