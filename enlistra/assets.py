"""Asset kit: public domain / CC0 music and sound effects from the Internet Archive.

Everything here is either a pre-1923 US sound recording (public domain since 2022),
a work explicitly dedicated to the public domain by its creator, or CC0. The kit is
downloaded once into assets/ and trimmed into short, normalised hits.

  python -c "from enlistra import assets; assets.ensure('assets')"
"""
from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import requests

log = logging.getLogger("assets")
DL = "https://archive.org/download/{id}/{name}"

# (category, archive identifier, file name, trim seconds or None, note)
MUSIC = [
    ("maple_leaf_rag", "MapleLeafRag", "MapleLeafRag.mp3", None,
     "Scott Joplin, Maple Leaf Rag. Rendition dedicated to the public domain by its creator."),
    ("castle_house_rag_1914", "Europes_Society_Orch-Castle_Rag",
     None, None, "James Reese Europe, Castle House Rag, Victor 1914. Pre-1923 recording, US public domain."),
    ("everybody_two_step_1913", "VictorMilitaryBand-EverybodyTwo-step",
     None, None, "Victor Military Band, Everybody Two-Step, 1913. Pre-1923 recording, US public domain."),
    ("ole_miss_rag_1917", "OleMissRag", "W.c.Handy-oleMissRag_1917.mp3", None,
     "W.C. Handy's Orchestra, Ole Miss Rag, Columbia 1917. Pre-1923 recording, US public domain."),
]

# Red Library / SSE Library: USC cinema sound collections on archive.org, CC0.
SFX = [
    ("boing",   "Red_Library_Foley_Props_3", "R25-15-Pluck Giant Rubber Band.mp3", 1.6),
    ("boing",   "Red_Library_Foley_Props_3", "R25-14-Pluck Big Rubber Band.mp3", 1.4),
    ("twang",   "Red_Library_Foley_Props_3", "R25-12-Large Plucked Bow on Wood.mp3", 1.5),
    ("whoosh",  "Red_Library_Foley_Props_3", "R24-18-Tent or Tarp Whoosh.mp3", 1.2),
    ("thud",    "Red_Library_Crashes",       "R27-17-Drop Object on Hard Surface.mp3", 1.2),
    ("thud",    "Red_Library_Foley_Props_3", "R22-36-Bang on Wooden Table.mp3", 1.0),
    ("crash",   "Red_Library_Crashes",       "R27-15-String Twang - Wood Crash.mp3", 2.5),
    ("crash",   "Red_Library_Crashes",       "R29-32-Large Quick Interior Crash.mp3", 2.5),
    ("crash",   "Red_Library_Crashes",       "R29-34-Big Object Falls and Crashes in Room.mp3", 2.5),
    ("glass",   "Red_Library_Crashes",       "R28-09-Big Glass Breaks.mp3", 2.0),
    ("glass",   "SSE_Library_GLASS",         "CRASH & DEBRIS/GLASCrsh_Small glass breaks_CS_USC.mp3", 1.5),
    ("bed",     "SSE_Library_FOLEY",         "PROP/FOLYProp-BED_Man jumps into firemen?s net or onto bed_CS_USC.mp3", 1.5),
    ("squeak",  "Red_Library_Metal",         "R16-29-Squeaky Wheel.mp3", 1.2),
    ("snare",   "Red_Library_Music",         "R17-07-Snare Drum.mp3", 1.0),
    ("drumroll","Red_Library_Music",         "R16-11-Snare Drum Roll.mp3", 2.5),
    ("gong",    "Red_Library_Music",         "R05-02-Gong Hit.mp3", 2.5),
    ("clank",   "Red_Library_Metal",         "R16-31-Metal Clank and Ring.mp3", 1.2),
]


# Musopen collection on archive.org: recordings released under the Public Domain Mark.
MUSOPEN = "MusopenCollectionAsFlac"
NATURE_MUSIC = [
    ("grieg_morning", "Greig_PeerGynt/EdvardGrieg-PeerGyntSuiteNo.1Op.46-01-Morning.mp3"),
    ("smetana_vltava", None),
    ("mendelssohn_hebrides", None),
    ("borodin_steppes", None),
    ("bach_goldberg_aria", None),
    ("dvorak_american_lento", None),
    ("haydn_lark_adagio", None),
    ("mendelssohn_italian_andante", None),
]
_NATURE_MATCH = {"smetana_vltava": "Vltava", "mendelssohn_hebrides": "HebridesOverture",
                 "borodin_steppes": "InTheSteppesOfCentralAsia", "bach_goldberg_aria": "-01-GoldbergVariationsBwv.988-Aria",
                 "dvorak_american_lento": "American-02-Lento", "haydn_lark_adagio": "Lark-02-AdagioCantabile",
                 "mendelssohn_italian_andante": "italian-02-AndanteConMoto"}


def ensure_nature(root: str | Path = "assets", seconds: float = 150.0) -> list[Path]:
    """Calm public domain music for the Wild Facts channel (first N seconds of each)."""
    root = Path(root)
    files = [f["name"] for f in requests.get(f"https://archive.org/metadata/{MUSOPEN}", timeout=60).json()["files"]
             if f["name"].endswith(".mp3")]
    out = []
    for key, name in NATURE_MUSIC:
        name = name or next((f for f in files if _NATURE_MATCH[key] in f), None)
        if not name:
            log.warning("nature track %s not found", key)
            continue
        src = _fetch(MUSOPEN, name, root / "raw" / f"{key}.mp3")
        dst = root / "music-nature" / f"{key}.m4a"
        dst.parent.mkdir(parents=True, exist_ok=True)
        if not dst.exists():
            subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-t", f"{seconds:.0f}",
                            "-af", "loudnorm=I=-20:TP=-2:LRA=9,afade=t=out:st=%.0f:d=3" % (seconds - 3),
                            "-c:a", "aac", "-b:a", "128k", str(dst)], check=True)
        out.append(dst)
    (root / "music-nature" / "CREDITS.md").write_text(
        "# Wild Facts music\n\nMusopen recordings, Public Domain Mark 1.0, via "
        "https://archive.org/details/MusopenCollectionAsFlac\n" + "".join(f"- {p.stem}\n" for p in out))
    return out


def _first_mp3(identifier: str) -> str:
    d = requests.get(f"https://archive.org/metadata/{identifier}", timeout=30).json()
    for f in d["files"]:
        if f["name"].lower().endswith(".mp3") and "64kb" not in f["name"]:
            return f["name"]
    raise RuntimeError(f"no mp3 in {identifier}")


def _fetch(identifier: str, name: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return dest
    url = DL.format(id=identifier, name=requests.utils.quote(name))
    log.info("fetching %s", name)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 18):
                fh.write(chunk)
    return dest


def _slate_end(src: Path) -> float:
    """Many archive library effects open with a spoken slate ("cue thirty five").
    Return the time the speech ends, or 0. Uses faster-whisper if installed."""
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError:
        return 0.0
    raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(src), "-t", "6",
                                   "-f", "f32le", "-ac", "1", "-ar", "16000", "-"])
    audio = np.frombuffer(raw, dtype=np.float32)
    model = _slate_end.model = getattr(_slate_end, "model", None) or WhisperModel("tiny.en", compute_type="int8")
    segs, _ = model.transcribe(audio, vad_filter=False)
    end = 0.0
    junk = {"you", "thank you", "the", "bye", "oh"}   # common whisper hallucinations on noise
    for sg in segs:
        words = "".join(ch for ch in sg.text.lower() if ch.isalnum() or ch == " ").strip()
        if sg.start < 4.0 and words and words not in junk and (len(words) > 3 or any(c.isdigit() for c in words)):
            end = max(end, sg.end)
    return min(end, 8.0)


def _norm(src: Path, dst: Path, trim: float | None, music: bool):
    """Loudness-normalise, trim hits to their first N seconds, clean old 78s,
    and cut off any spoken slate at the head of an effect."""
    if dst.exists():
        return
    skip = 0.0 if music else _slate_end(src)
    if skip:
        log.info("slate detected in %s, skipping %.1fs", src.name, skip)
    af = []
    if music:
        af.append("highpass=f=80,lowpass=f=9000")
        if "1913" in dst.name or "1914" in dst.name or "1917" in dst.name:
            af.append("afftdn=nf=-28")   # tame shellac hiss
        af.append("loudnorm=I=-18:TP=-2:LRA=9")
    else:
        af.append("silenceremove=start_periods=1:start_threshold=-40dB")
        af.append("loudnorm=I=-14:TP=-1:LRA=7")
        if trim:
            af.append(f"afade=t=out:st={max(trim - 0.25, 0):.2f}:d=0.25")
    cmd = ["ffmpeg", "-y", "-v", "error"]
    if skip:
        cmd += ["-ss", f"{skip + 0.15:.2f}"]
    cmd += ["-i", str(src)]
    if trim and not music:
        cmd += ["-t", f"{trim:.2f}"]
    cmd += ["-af", ",".join(af), "-ar", "44100", "-ac", "2", str(dst)]
    subprocess.run(cmd, check=True)


def ensure(root: str | Path = "assets") -> dict:
    root = Path(root)
    raw = root / "raw"
    out = {"music": [], "sfx": {}}
    for key, ident, name, trim, _note in MUSIC:
        try:
            name = name or _first_mp3(ident)
            src = _fetch(ident, name, raw / f"{key}.mp3")
            dst = root / "music" / f"{key}.m4a"
            dst.parent.mkdir(parents=True, exist_ok=True)
            _norm(src, dst, None, music=True)
            out["music"].append(dst)
        except Exception as e:  # noqa: BLE001
            log.warning("music %s skipped: %s", key, e)
    for i, (cat, ident, name, trim) in enumerate(SFX):
        try:
            src = _fetch(ident, name, raw / f"{cat}_{i}.mp3")
            dst = root / "sfx" / f"{cat}_{i}.wav"
            dst.parent.mkdir(parents=True, exist_ok=True)
            _norm(src, dst, trim, music=False)
            out["sfx"].setdefault(cat, []).append(dst)
        except Exception as e:  # noqa: BLE001
            log.warning("sfx %s skipped: %s", name, e)
    (root / "CREDITS.md").write_text(
        "# Audio credits\n\nMusic (all public domain in the US):\n" +
        "".join(f"- {n}\n" for *_, n in MUSIC) +
        "\nSound effects: Red Library and SSE Library (USC cinema sound collections), "
        "archive.org, CC0.\n")
    return out


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    r = ensure()
    print(len(r["music"]), "music tracks;", sum(len(v) for v in r["sfx"].values()), "sfx")
