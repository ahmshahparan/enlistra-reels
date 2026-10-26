"""Long-form: a full public domain short, restored and re-scored, with narration.

Why: from Feb 2027 YouTube admits new channels on 8,000 watch hours a year, and pays
long-form far better than Shorts. A 20-minute restored Chaplin short watched to the
halfway mark earns about 300 times the watch time of one Short.

What is added (so it is a new work, not a re-upload; YouTube's reused-content rule):
  - restoration pass: denoise, deflicker, gentle sharpen, stabilised levels, 1080p 16:9
  - a new score: public domain rags, crossfaded under the whole film
  - a narrated intro (who, when, what to watch for) written by Claude from the frames
  - chapter cards with a one-line narrated note at the main scene changes
  - an end card with the public domain credit
"""
from __future__ import annotations

import json
import logging
import random
import subprocess
import tempfile
from pathlib import Path

from . import analyze, edit, store, vision

log = logging.getLogger("longform")
W, H = 1920, 1080


def _chapters(a, n: int = 5) -> list[float]:
    """Pick n chapter points at the strongest scene cuts, spread through the film."""
    if not a.cuts:
        return [a.duration * (i + 1) / (n + 1) for i in range(n)]
    out = []
    gap = a.duration / (n * 1.6)
    for i in range(n):
        target = a.duration * (i + 1) / (n + 1)
        c = min(a.cuts, key=lambda c: abs(c - target))
        if abs(c - target) > gap / 2 or any(abs(c - o) < gap for o in out):
            c = target                         # no usable cut nearby: chapter on the target time
        if all(abs(c - o) >= gap * 0.8 for o in out):
            out.append(c)
    return sorted(round(c, 1) for c in out)


SYSTEM = """You write the narration for a restored public domain silent comedy, presented on
YouTube as a long-form video. Deadpan, warm, informed; never describe a gag before it
happens. Return strict JSON:
{"title": "YouTube title, 50 to 80 characters, names the star and the film, says restored",
 "year": "release year as an integer if certain, else null",
 "description": "3 short paragraphs: what the film is, why it matters, what was restored",
 "intro": "narration, 45 to 70 words: year, studio, who is in it, one fact, what to watch for",
 "chapters": [{"t": seconds, "name": "2 to 5 words", "note": "one sentence, under 20 words"}],
 "outro": "narration, under 30 words"}
Chapters: use exactly the timestamps given. Facts must be certain; if unsure, say less."""


def plan(video: Path, film: dict, a, cfg: dict) -> dict:
    import anthropic
    sheets = Path(tempfile.mkdtemp(prefix="lf_"))
    content = []
    pts = _chapters(a)
    for i, t in enumerate(pts):
        s = vision.contact_sheet(video, max(0, t - 5), min(a.duration, t + 25), sheets / f"c{i}.jpg", n=8)
        content += [{"type": "text", "text": f"Chapter at {t:.0f}s"}, vision._img_block(s)]
    content.append({"type": "text", "text": json.dumps({"film": film, "duration_s": round(a.duration),
                                                         "chapter_times": pts})})
    msg = anthropic.Anthropic().messages.create(model=cfg.get("anthropic_model", "claude-sonnet-5-5"),
                                                max_tokens=8000, system=SYSTEM,
                                                messages=[{"role": "user", "content": content}])
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    return json.loads(text[text.find("{"): text.rfind("}") + 1])


def _card(text: str, sub: str, out: Path) -> Path:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), (14, 12, 10))
    d = ImageDraw.Draw(img)
    f1, f2 = edit._font(84), edit._font(40, bold=False)
    for y, (t, f, c) in ((H * 0.40, (text, f1, (238, 230, 214))), (H * 0.55, (sub, f2, (150, 140, 124)))):
        for i, ln in enumerate(edit._wrap(d, t, f, int(W * 0.8))[:3]):
            tw = d.textlength(ln, font=f)
            d.text(((W - tw) / 2, y + i * f.size * 1.2), ln, font=f, fill=c)
    img.save(out)
    return out


def render(identifier: str, cfg: dict, limit_s: float | None = None) -> Path:
    from . import source
    st = store.get()
    film = source.resolve(identifier, cfg["source"])
    if not film:
        raise RuntimeError(f"{identifier} is not free to use")
    video = source.download(film, Path(cfg["paths"]["work"]) / "src")
    a = analyze.analyze(video, Path(cfg["paths"]["work"]) / "analysis" / f"{identifier}.json", want_transcript=False)
    info = {"title": film.title, "year": film.year}
    if limit_s:
        a.duration = min(a.duration, limit_s)
        a.cuts = [c for c in a.cuts if c < limit_s - 10]
    p = plan(video, info, a, cfg["select"])
    if not info.get("year") and isinstance(p.get("year"), int):
        info["year"] = p["year"]
    tmp = Path(tempfile.mkdtemp(prefix="lfr_"))
    ecfg = cfg["edit"]

    # 1. restored film, 16:9 pillarboxed over a soft blur, no source audio
    body = tmp / "body.mp4"
    vf = (f"split=2[a][b];[b]scale=480:270:force_original_aspect_ratio=increase,crop=480:270,boxblur=10:2,"
          f"eq=brightness=-0.25,scale={W}:{H}[bg];[a]hqdn3d=2.5:2:5:4,deflicker=size=5,"
          f"eq=contrast=1.06,scale=-2:{H}:flags=lanczos,unsharp=5:5:0.4[fg];[bg][fg]overlay=(W-w)/2:0,fps=24,format=yuv420p")
    subprocess.run(["ffmpeg", "-y", "-v", "error", *(["-t", str(limit_s)] if limit_s else []), "-i", str(video),
                    "-filter_complex", vf, "-an",
                    "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-tune", "film", str(body)], check=True)
    dur = edit._dur(body)

    # 2. intro + end cards with narration
    def narrated_card(text_big, text_small, narration, name):
        png = _card(text_big, text_small, tmp / f"{name}.png")
        wav = edit.narrate(narration, ecfg, tmp / f"{name}.wav")
        d = (edit._dur(wav) if wav else 3) + 1.2
        out = tmp / f"{name}.mp4"
        cmd = ["ffmpeg", "-y", "-v", "error", "-loop", "1", "-t", f"{d:.2f}", "-i", str(png)]
        cmd += (["-i", str(wav)] if wav else ["-f", "lavfi", "-t", f"{d:.2f}", "-i", "anullsrc=r=44100:cl=stereo"])
        cmd += ["-filter_complex", f"[0:v]fps=24,format=yuv420p,fade=t=in:d=0.4,fade=t=out:st={d - 0.4:.2f}:d=0.4[v];"
                f"[1:a]adelay=500|500,apad=whole_dur={d:.2f}[a]", "-map", "[v]", "-map", "[a]", "-t", f"{d:.2f}",
                "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-ar", "44100", "-ac", "2", str(out)]
        subprocess.run(cmd, check=True)
        return out

    sub = (f"{info['year']}  ·  " if info.get("year") else "") + "restored for Enlistra Reels"
    intro = narrated_card(info["title"] or "", sub, p["intro"], "intro")
    outro = narrated_card("Thanks for watching", "Public domain film, restored and re-scored. @enlistra_reels", p["outro"], "outro")

    # 3. score for the body: rags back to back, crossfaded, with chapter notes ducked in
    rng = random.Random(identifier)
    tracks = sorted(Path(ecfg.get("music_dir", "assets/music")).glob("*.m4a"))
    rng.shuffle(tracks)
    seq, total = [], 0.0
    while total < dur + 5 and tracks:
        t = tracks[len(seq) % len(tracks)]
        seq.append(t)
        total += edit._dur(t) - 3
    ins = sum((["-i", str(t)] for t in seq), [])
    chain = "[0:a]"
    graph = []
    for i in range(1, len(seq)):
        graph.append(f"{chain}[{i}:a]acrossfade=d=3[x{i}]")
        chain = f"[x{i}]"
    music = tmp / "score.m4a"
    subprocess.run(["ffmpeg", "-y", "-v", "error", *ins, "-filter_complex",
                    (";".join(graph) + ";" if graph else "") + f"{chain}atrim=0:{dur:.2f},volume=0.7[m]",
                    "-map", "[m]", "-c:a", "aac", str(music)], check=True)
    notes = []
    for i, ch in enumerate(p.get("chapters", [])):
        wav = edit.narrate(ch["note"], ecfg, tmp / f"ch{i}.wav")
        if wav:
            notes.append((float(ch["t"]), wav))
    nin = sum((["-i", str(w)] for _, w in notes), [])
    mix = "".join(f"[{i + 1}:a]adelay={int(t * 1000)}|{int(t * 1000)},apad=whole_dur={dur:.2f}[n{i}];" for i, (t, _) in enumerate(notes))
    if notes:
        mix += "".join(f"[n{i}]" for i in range(len(notes))) + f"amix=inputs={len(notes)}:normalize=0[nar];[nar]asplit[n1][n2];"
        mix += "[0:a][n2]sidechaincompress=threshold=0.03:ratio=8:release=600[mduck];[mduck][n1]amix=inputs=2:normalize=0[a]"
    else:
        mix = "[0:a]anull[a]"
    body_audio = tmp / "body_audio.m4a"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(music), *nin, "-filter_complex", mix, "-map", "[a]",
                    "-c:a", "aac", "-ar", "44100", "-ac", "2", str(body_audio)], check=True)
    # chapter names as lower-third text
    from PIL import Image, ImageDraw
    overlays = []
    for i, ch in enumerate(p.get("chapters", [])):
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        f = edit._font(46)
        d.text((80, H - 140), ch["name"], font=f, fill=(255, 255, 255, 235), stroke_width=3, stroke_fill=(0, 0, 0, 220))
        pth = tmp / f"chn{i}.png"
        img.save(pth)
        overlays.append((float(ch["t"]), pth))
    oin = sum((["-i", str(o)] for _, o in overlays), [])
    vchain, vg = "[0:v]", []
    for i, (t, _) in enumerate(overlays):
        vg.append(f"{vchain}[{i + 2}:v]overlay=0:0:enable='between(t,{t:.1f},{t + 4:.1f})'[o{i}]")
        vchain = f"[o{i}]"
    body2 = tmp / "body2.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(body), "-i", str(body_audio), *oin, "-filter_complex",
                    (";".join(vg) + ";" if vg else "") + f"{vchain}null[v]", "-map", "[v]", "-map", "1:a",
                    "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-ar", "44100", "-ac", "2", "-shortest", str(body2)],
                   check=True)

    # 4. join
    lst = tmp / "list.txt"
    lst.write_text("".join(f"file '{x}'\n" for x in (intro, body2, outro)))
    name = f"{identifier}_feature"
    out = st.out / f"{name}.mp4"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
                    "-c:v", "libx264", "-crf", "20", "-c:a", "aac", "-movflags", "+faststart", str(out)], check=True)
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-ss", f"{edit._dur(intro) + 60:.1f}", "-i", str(out),
                    "-frames:v", "1", "-q:v", "3", str(out.with_suffix(".jpg"))], check=True)
    from . import post
    m = json.loads(post.write_manifest(out, {"identifier": identifier, "title": info["title"], "year": info["year"],
                                             "reason_free": film.reason_free},
                                       {"hook": p["title"], "title": p["title"], "caption": p["description"],
                                        "why": "long-form feature", "start": 0, "end": dur, "style": "feature"},
                                       cfg["post"]).read_text())
    chap = "\n".join(f"{int((edit._dur(intro) + float(c['t'])) // 60)}:{int((edit._dur(intro) + float(c['t'])) % 60):02d} "
                     f"{c['name']}" for c in p.get("chapters", []))
    credit = f"Source: {info['title']}" + (f" ({info['year']})" if info.get("year") else "") + \
        ". Public domain film, restored and re-scored by Enlistra Reels. Music: public domain recordings."
    tags = " ".join(cfg["post"].get("hashtags", []))
    m["caption"] = f"{p['description']}\n\n0:00 Introduction\n{chap}\n\n{credit}\n\n{tags}"
    m["critic"] = {"verdict": "pass", "feedback": ""}
    m["approval"] = "pending"
    m["longform"] = True
    st.save_reel(name, m, {"gates": [], "judge": None})
    st.activity("queue", f"Long-form feature ready for approval: '{p['title']}' ({edit._dur(out) / 60:.0f} min)", reel=name)
    return out
