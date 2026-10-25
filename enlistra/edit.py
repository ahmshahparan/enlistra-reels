"""Edit agent v2: card frame, pacing, narrator, music bed, synced effects.

Picture
  background  Pillow-rendered card (dark warm field, thin cream frame, film title
              and year, hook text, watermark). No ffmpeg text filters needed.
  foreground  source clip, scaled into the frame, light grain and contrast,
              speed-up, punch-in zoom at the payoff moment.
Sound
  narration   Piper TTS line at the start (local, open source)
  music       public domain rag, random offset, ducked under narration, fade out
  sfx         CC0 hits on cue (from the vision step) or on motion peaks
Source audio is dropped: silent films carry whatever score the uploader added.
"""
from __future__ import annotations

import json
import logging
import os
import random
import shutil
import subprocess
import tempfile
from pathlib import Path

from .select import Clip

log = logging.getLogger("edit")
SFX_GAIN = {"boing": 0.9, "thud": 0.9, "crash": 0.8, "glass": 0.7, "whoosh": 0.8,
            "snare": 0.7, "squeak": 0.6, "bed": 0.9, "twang": 0.8, "clank": 0.7,
            "gong": 0.6, "drumroll": 0.6}


# ------------------------------------------------------------------ helpers
def _probe(video: Path) -> tuple[int, int, float]:
    out = subprocess.check_output(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=width,height:format=duration", "-of", "json", str(video)], text=True)
    j = json.loads(out)
    s = j["streams"][0]
    return int(s["width"]), int(s["height"]), float(j["format"]["duration"])


def _dur(path: Path) -> float:
    out = subprocess.check_output(["ffprobe", "-v", "error", "-show_entries",
                                   "format=duration", "-of", "csv=p=0", str(path)], text=True)
    return float(out.strip() or 0)


def _font(size: int, bold: bool = True):
    from PIL import ImageFont
    cands = ["/System/Library/Fonts/Supplemental/Georgia Bold.ttf" if bold else
             "/System/Library/Fonts/Supplemental/Georgia.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf" if bold else
             "/System/Library/Fonts/Supplemental/Arial.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf" if bold else
             "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
             "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"]
    for c in cands:
        try:
            return ImageFont.truetype(c, size)
        except OSError:
            continue
    return ImageFont.load_default()


def _wrap(draw, text, font, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) <= max_w or not cur:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or [" "]


def card(W: int, H: int, pic_w: int, pic_h: int, pic_y: int, hook: str, film: dict,
         watermark: str, out: Path) -> Path:
    """Static background card. The video is overlaid at (x=(W-pic_w)/2, y=pic_y)."""
    from PIL import Image, ImageDraw, ImageFilter
    bg, cream, dim = (20, 17, 14), (238, 230, 214), (150, 140, 124)
    img = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(img)
    # soft vignette
    vig = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vig)
    vd.ellipse((-W * 0.2, -H * 0.1, W * 1.2, H * 1.1), fill=70)
    vig = vig.filter(ImageFilter.GaussianBlur(220))
    img = Image.composite(Image.new("RGB", (W, H), (46, 38, 30)), img, vig)
    d = ImageDraw.Draw(img)
    # frame around the picture
    x0 = (W - pic_w) // 2
    pad = 14
    d.rectangle((x0 - pad, pic_y - pad, x0 + pic_w + pad, pic_y + pic_h + pad),
                outline=cream, width=3)
    d.rectangle((x0 - pad - 8, pic_y - pad - 8, x0 + pic_w + pad + 8, pic_y + pic_h + pad + 8),
                outline=dim, width=1)
    # eyebrow + hook above the picture
    f_eye = _font(int(W * 0.024), bold=False)
    eye = "ENLISTRA REELS  ·  SILENT COMEDY, NARRATED"
    f_hook = _font(int(W * 0.062))
    lines = _wrap(d, hook, f_hook, int(W * 0.86))
    lh = int(f_hook.size * 1.2)
    y = pic_y - pad - 40 - len(lines) * lh
    tw = d.textlength(eye, font=f_eye)
    d.text(((W - tw) / 2, y - 52), eye, font=f_eye, fill=dim)
    for ln in lines:
        tw = d.textlength(ln, font=f_hook)
        d.text(((W - tw) / 2 + 2, y + 2), ln, font=f_hook, fill=(0, 0, 0))
        d.text(((W - tw) / 2, y), ln, font=f_hook, fill=cream)
        y += lh
    # film title and year under the picture
    f_meta = _font(int(W * 0.03), bold=False)
    meta = film.get("title", "") or ""
    if film.get("year"):
        meta += f"  ·  {film['year']}"
    tw = d.textlength(meta, font=f_meta)
    d.text(((W - tw) / 2, pic_y + pic_h + pad + 26), meta, font=f_meta, fill=dim)
    # small rule + brand + watermark
    d.line((W * 0.38, H - 150, W * 0.62, H - 150), fill=dim, width=1)
    f_wm = _font(int(W * 0.03))
    tw = d.textlength(watermark, font=f_wm)
    d.text(((W - tw) / 2, H - 130), watermark, font=f_wm, fill=cream)
    f_sub = _font(int(W * 0.022), bold=False)
    sub = "public domain, restored for the feed"
    tw = d.textlength(sub, font=f_sub)
    d.text(((W - tw) / 2, H - 88), sub, font=f_sub, fill=dim)
    img.save(out)
    return out


# ------------------------------------------------------------------ audio
def narrate(text: str, cfg: dict, out: Path) -> Path | None:
    """Piper TTS. Returns None if no voice model or piper is missing."""
    if not text.strip():
        return None
    voice = Path(cfg.get("voice", "assets/voices/en_GB-alan-medium.onnx"))
    piper = shutil.which("piper")
    if not voice.exists() or not piper:
        log.warning("narration skipped: piper or voice model missing")
        return None
    raw = out.with_suffix(".raw.wav")
    subprocess.run([piper, "-m", str(voice), "-f", str(raw), "--length-scale",
                    str(cfg.get("voice_speed", 1.0)), "--sentence-silence", "0.25"],
                   input=text, text=True, check=True, capture_output=True)
    # warm it up a little, normalise
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(raw), "-af",
                    "highpass=f=90,lowpass=f=11000,acompressor=threshold=-18dB:ratio=3,"
                    "loudnorm=I=-16:TP=-1.5:LRA=7", "-ar", "44100", "-ac", "2", str(out)],
                   check=True)
    raw.unlink(missing_ok=True)
    return out


def pick_music(music_dir: Path, rng: random.Random) -> Path | None:
    if not music_dir.exists():
        return None
    tracks = sorted(p for p in music_dir.iterdir() if p.suffix.lower() in (".m4a", ".mp3", ".wav", ".ogg"))
    return rng.choice(tracks) if tracks else None


def pick_sfx(sfx_dir: Path, kind: str, rng: random.Random) -> Path | None:
    if not sfx_dir.exists():
        return None
    files = sorted(sfx_dir.glob(f"{kind}_*.wav"))
    return rng.choice(files) if files else None


def motion_cues(analysis, start: float, end: float, max_cues: int = 4) -> list[dict]:
    """Fallback SFX cues when the vision step gave none: top motion spikes."""
    import numpy as np
    m = np.array(analysis.motion[int(start):int(end) + 1], dtype=float)
    if len(m) < 4:
        return []
    z = (m - m.mean()) / (m.std() + 1e-6)
    idx = [int(i) for i in np.argsort(-z) if z[i] > 1.2][:max_cues]
    kinds = ["thud", "whoosh", "boing", "crash"]
    return [{"t": start + i + 0.3, "kind": kinds[k % len(kinds)]} for k, i in enumerate(sorted(idx))]


# ------------------------------------------------------------------ render
def render(video: Path, clip: Clip, out: Path, cfg: dict, film: dict | None = None,
           analysis=None) -> Path:
    film = film or {}
    W, H, fps = cfg["width"], cfg["height"], cfg["fps"]
    speed = float(cfg.get("speed", 1.08))
    rng = random.Random(f"{video.name}:{clip.start}")
    tmp = Path(tempfile.mkdtemp(prefix="enlistra_"))

    src_w, src_h, _ = _probe(video)
    pic_w = int(W * 0.93) // 2 * 2
    pic_h = int(pic_w * src_h / src_w) // 2 * 2
    pic_y = int(H * 0.27)
    segs = clip.segs()
    src_dur = clip.src_duration
    dur = src_dur / speed
    p_rel = clip.rel(clip.payoff) if clip.payoff is not None else None

    # ---- picture
    card_png = card(W, H, pic_w, pic_h, pic_y, clip.hook or film.get("title", ""), film,
                    cfg.get("watermark", ""), tmp / "card.png")
    zoom = "1"
    if p_rel is not None:
        p = p_rel                                # seconds into the spliced timeline (before speed)
        a, hold, rel = 0.22, 1.1, 0.35
        zoom = (f"1+0.16*if(between(it,{p:.2f},{p+a:.2f}),(it-{p:.2f})/{a},"
                f"if(between(it,{p+a:.2f},{p+a+hold:.2f}),1,"
                f"if(between(it,{p+a+hold:.2f},{p+a+hold+rel:.2f}),({p+a+hold+rel:.2f}-it)/{rel},0)))")
    n_seg = len(segs)
    cat = "".join(f"[{i}:v]" for i in range(n_seg)) + f"concat=n={n_seg}:v=1:a=0[cat];" if n_seg > 1 else "[0:v]null[cat];"
    fg = (f"{cat}[cat]fps={fps},scale={pic_w}:{pic_h},setsar=1,"
          f"zoompan=z='{zoom}':d=1:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={pic_w}x{pic_h}:fps={fps},"
          f"eq=contrast=1.06:brightness=0.01,noise=c0s=5:c0f=t,"
          f"setpts=PTS/{speed}[fg]")
    card_idx = n_seg
    vgraph = (f"{fg};[{card_idx}:v][fg]overlay={(W - pic_w) // 2}:{pic_y}:shortest=1,"
              f"fade=t=in:st=0:d=0.35,fade=t=out:st={dur - 0.45:.2f}:d=0.45,format=yuv420p[vout]")

    # ---- audio
    inputs = []
    for s0, e0 in segs:
        inputs += ["-ss", f"{s0:.2f}", "-t", f"{e0 - s0:.2f}", "-i", str(video)]
    inputs += ["-loop", "1", "-framerate", str(fps), "-t", f"{dur:.2f}", "-i", str(card_png)]
    a_in = n_seg + 1
    agraph, mix = [], []

    narr = narrate(getattr(clip, "narration", ""), cfg, tmp / "narr.wav")
    narr_dur = _dur(narr) if narr else 0.0
    clip.narr_dur = narr_dur
    music = pick_music(Path(cfg.get("music_dir", "assets/music")), rng)
    if music:
        mdur = _dur(music)
        off = rng.uniform(2.0, max(2.0, mdur - dur - 4))
        inputs += ["-ss", f"{off:.2f}", "-t", f"{dur + 0.5:.2f}", "-i", str(music)]
        mi = a_in; a_in += 1
        mvol = float(cfg.get("music_volume", 0.8))
        if narr:
            inputs += ["-i", str(narr)]
            ni = a_in; a_in += 1
            delay = int(cfg.get("narration_start", 0.4) * 1000)
            agraph.append(f"[{ni}:a]adelay={delay}|{delay},apad=whole_dur={dur:.2f}[nar]")
            agraph.append(f"[nar]asplit=2[nar_mix][nar_sc]")
            agraph.append(f"[{mi}:a]volume={mvol}[m0]")
            agraph.append(f"[m0][nar_sc]sidechaincompress=threshold=0.03:ratio=8:attack=40:release=500:makeup=1[mus]")
            mix += ["[mus]", "[nar_mix]"]
        else:
            agraph.append(f"[{mi}:a]volume={mvol}[mus]")
            mix.append("[mus]")
    elif narr:
        inputs += ["-i", str(narr)]
        ni = a_in; a_in += 1
        agraph.append(f"[{ni}:a]adelay=400|400,apad=whole_dur={dur:.2f}[nar_mix]")
        mix.append("[nar_mix]")

    cues = list(getattr(clip, "sfx", []) or [])
    if not cues and analysis is not None:
        cues = [c for c in motion_cues(analysis, clip.start, clip.end) if clip.rel(c["t"]) is not None]
    sfx_dir = Path(cfg.get("sfx_dir", "assets/sfx"))
    for k, cue in enumerate(cues):
        r = clip.rel(float(cue["t"]))
        if r is None:
            continue
        t_out = r / speed
        if not (0 <= t_out < dur - 0.2):
            continue
        f = pick_sfx(sfx_dir, cue["kind"], rng)
        if not f:
            continue
        inputs += ["-i", str(f)]
        si = a_in; a_in += 1
        ms = int(t_out * 1000)
        g = SFX_GAIN.get(cue["kind"], 0.8) * float(cfg.get("sfx_volume", 1.0))
        agraph.append(f"[{si}:a]volume={g:.2f},adelay={ms}|{ms},apad=whole_dur={dur:.2f}[s{k}]")
        mix.append(f"[s{k}]")

    if mix:
        agraph.append("".join(mix) + f"amix=inputs={len(mix)}:normalize=0:duration=longest,"
                      f"atrim=0:{dur:.2f},afade=t=in:st=0:d=0.2,afade=t=out:st={dur - 0.9:.2f}:d=0.9,"
                      f"alimiter=limit=0.95[aout]")
    else:
        agraph.append(f"anullsrc=r=44100:cl=stereo,atrim=0:{dur:.2f}[aout]")

    fc = ";".join([vgraph] + agraph)
    cmd = ["ffmpeg", "-y", "-v", "error", *inputs, "-filter_complex", fc,
           "-map", "[vout]", "-map", "[aout]", "-t", f"{dur:.2f}",
           "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "160k", "-ar", "44100", "-movflags", "+faststart", str(out)]
    log.info("rendering %s (%.1fs, speed %.2f, %d sfx, narration=%s)", out.name, dur, speed,
             len(mix) - (2 if narr and music else 1 if (narr or music) else 0), bool(narr))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        log.error("ffmpeg failed:\n%s", proc.stderr[-3000:])
        raise RuntimeError(f"ffmpeg failed rendering {out.name}")

    # cover: the payoff frame if known, else the busiest second
    cover_t = 1.0
    if p_rel is not None:
        cover_t = p_rel / speed + 0.35
    elif analysis is not None:
        import numpy as np
        m = np.array(analysis.motion[int(clip.start):int(clip.end)], dtype=float)
        if len(m):
            cover_t = (int(np.argmax(m)) + 0.5) / speed
    cover_t = min(max(cover_t, 0.3), dur - 0.3)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{cover_t:.2f}", "-i", str(out),
                    "-frames:v", "1", "-q:v", "3", str(out.with_suffix(".jpg"))], check=True)
    shutil.rmtree(tmp, ignore_errors=True)
    return out
