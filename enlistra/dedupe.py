"""Duplicate guard: no reel goes out twice, even from a different print of the same film.

Every reel gets a visual fingerprint: perceptual hashes (dHash) of frames sampled from the
middle band of the picture, where the footage sits and on-screen text does not. Two reels are
duplicates when enough of their frames match. This catches:
  - the same gag cut twice in one run (overlapping candidates),
  - the same gag cut again in a later run,
  - the same gag from another upload of the film (different identifier, resolution, offset).

    python -m enlistra.dedupe audit --account silent-comedy          # report only
    python -m enlistra.dedupe audit --account silent-comedy --apply  # reject queued duplicates
"""
import argparse
import json
import logging
import subprocess
import tempfile
from pathlib import Path

from PIL import Image

from . import store

log = logging.getLogger("dedupe")
FPS = 2
BAND = (0.40, 0.68)          # vertical band of the 9:16 frame that holds footage, not text
HAMMING = 10                 # max bit difference for two frames to count as the same shot
MIN_MATCHES = 3              # matching frames needed, and...
MIN_SHARE = 0.25             # ...at least this share of the shorter reel's frames
LIVE = ("posted", "approved", "posting", "pending")   # stages that count as "already have it"


def _dhash(img: Image.Image) -> int | None:
    g = img.convert("L").resize((9, 8), Image.LANCZOS)
    px = list(g.getdata())
    if max(px) - min(px) < 18:         # blank, black or faded frame: carries no identity
        return None
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (px[r * 9 + c] > px[r * 9 + c + 1])
    return bits


def _footage_box(frames: list[Image.Image]) -> tuple[int, int, int, int]:
    """Find the film footage inside a 9:16 reel, whatever the layout (blurred fill, card, plain).

    Footage is the longest run of rows with real detail; blurred fill and flat card
    backgrounds have little, and text blocks are short runs separated from it.
    """
    import numpy as np
    acc = None
    for im in frames:
        a = np.asarray(im.convert("L"), dtype=np.float32)
        g = np.abs(np.diff(a, axis=1))[:-1, :] + np.abs(np.diff(a, axis=0))[:, :-1]
        acc = g if acc is None else acc + g
    rows = np.convolve(acc.mean(axis=1), np.ones(5) / 5, mode="same")
    on = rows > max(rows.max() * 0.07, 1e-6)
    best, cur, start = (0, 0), 0, 0
    for i, v in enumerate(list(on) + [False]):
        if v:
            if cur == 0:
                start = i
            cur += 1
        else:
            if cur > best[1] - best[0]:
                best = (start, start + cur)
            cur = 0
    y0, y1 = best
    cols = acc[y0:y1].mean(axis=0) if y1 > y0 else acc.mean(axis=0)
    cols = np.convolve(cols, np.ones(5) / 5, mode="same")
    xs = [i for i, v in enumerate(cols) if v > cols.max() * 0.07] or [0, acc.shape[1] - 1]
    x0, x1 = xs[0], xs[-1]
    w, h = acc.shape[1], acc.shape[0]
    if y1 - y0 < h * 0.12:                    # nothing found: fall back to the middle band
        y0, y1, x0, x1 = int(h * BAND[0]), int(h * BAND[1]), int(w * 0.1), int(w * 0.9)
    dx, dy = int((x1 - x0) * 0.06), int((y1 - y0) * 0.08)   # stay clear of borders and watermarks
    return x0 + dx, y0 + dy, x1 - dx, y1 - dy


def fingerprint(video: Path) -> list[int]:
    tmp = Path(tempfile.mkdtemp(prefix="fp_"))
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={FPS},scale=270:-2",
                    str(tmp / "f%04d.png")], check=True)
    files = sorted(tmp.glob("f*.png"))
    frames = [Image.open(f) for f in files]
    box = _footage_box(frames[:: max(1, len(frames) // 8)]) if frames else None
    out = []
    for im, f in zip(frames, files):
        h = _dhash(im.crop(box))
        if h is not None:
            out.append(h)
        f.unlink()
    tmp.rmdir()
    return out


def similarity(a: list[int], b: list[int]) -> tuple[int, float]:
    """Matching frames and their share of the shorter reel."""
    if not a or not b:
        return 0, 0.0
    short, long_ = (a, b) if len(a) <= len(b) else (b, a)
    hits = sum(1 for x in short if min(bin(x ^ y).count("1") for y in long_) <= HAMMING)
    return hits, hits / len(short)


def is_dup(a: list[int], b: list[int]) -> bool:
    hits, share = similarity(a, b)
    return hits >= MIN_MATCHES and share >= MIN_SHARE


def _ranges(r: dict) -> list[tuple[float, float]]:
    src = r.get("source") or {}
    segs = src.get("segments") or [[src.get("start"), src.get("end")]]
    return [(float(a), float(b)) for a, b in segs if a is not None and b is not None]


def same_cut(a: dict, b: dict) -> bool:
    """Same upload of the same film and the time ranges overlap by half of the shorter reel."""
    if not a.get("film_identifier") or a.get("film_identifier") != b.get("film_identifier"):
        return False
    ra, rb = _ranges(a), _ranges(b)
    if not ra or not rb:
        return False
    ov = sum(max(0.0, min(x1, y1) - max(x0, y0)) for x0, x1 in ra for y0, y1 in rb)
    shorter = min(sum(x1 - x0 for x0, x1 in ra), sum(y1 - y0 for y0, y1 in rb))
    return shorter > 0 and ov >= 0.5 * shorter


def duplicates(a: dict, b: dict, led: dict) -> bool:
    return same_cut(a, b) or is_dup(led.get(a["name"]) or [], led.get(b["name"]) or [])


# ------------------------------------------------------------------ ledger
def _ledger(st) -> dict:
    return st.setting("fingerprints") or {}


def remember(st, name: str, video: Path) -> list[int]:
    fp = fingerprint(video)
    led = _ledger(st)
    led[name] = fp
    st.set_setting("fingerprints", led)
    return fp


def find_duplicate(st, name: str, stages=("posted", "approved", "posting")) -> str | None:
    """Name of an existing reel in `stages` that this one duplicates, or None."""
    led = _ledger(st)
    reels = {r["name"]: r for r in st.reels()}
    me = reels.get(name)
    if not me:
        return None
    if name not in led:
        try:
            led[name] = fingerprint(st.local_files(name)[0])
            st.set_setting("fingerprints", led)
        except Exception:  # noqa: BLE001
            pass
    for other, r in reels.items():
        if other != name and r.get("stage") in stages and duplicates(me, r, led):
            return other
    return None


def _rank(r: dict) -> tuple:
    order = {"posted": 0, "approved": 1, "posting": 1, "pending": 2, "flagged": 3, "rejected": 4}
    score = float(((r.get("critic") or {}).get("scores") or {}).get("overall") or 0)
    return (order.get(r.get("stage"), 5), -score, r.get("created_at") or "")


def audit(account: str, apply: bool = False) -> list[dict]:
    store.use(account)
    st = store.get(account=account)
    reels = [r for r in st.reels() if r.get("stage") != "rejected"]
    led = _ledger(st)
    for r in reels:
        if r["name"] not in led:
            try:
                mp4, _ = st.local_files(r["name"])
                led[r["name"]] = fingerprint(mp4)
            except Exception as e:  # noqa: BLE001
                log.warning("%s: no video (%s)", r["name"], e)
    st.set_setting("fingerprints", led)
    # keep the best reel of every duplicate cluster; everything else is a duplicate of it
    kept: list[dict] = []
    dropped: list[dict] = []
    root: dict = {}
    findings = []
    for r in sorted(reels, key=_rank):
        fp = led.get(r["name"]) or []
        twin = next((k for k in kept if duplicates(r, k, led)), None)
        if not twin:   # a duplicate of a duplicate is a duplicate of the one we kept
            via = next((d for d in dropped if duplicates(r, d, led)), None)
            twin = root.get(via["name"]) if via else None
        if not twin:
            kept.append(r)
            continue
        dropped.append(r)
        root[r["name"]] = twin
        hits, share = similarity(fp, led.get(twin["name"]) or [])
        findings.append({"name": r["name"], "stage": r.get("stage"), "hook": r.get("hook"),
                         "duplicate_of": twin["name"], "of_stage": twin.get("stage"), "of_hook": twin.get("hook"),
                         "match": f"{hits} frames, {share:.0%}", "status": r.get("status")})
        if apply and r.get("stage") in ("pending", "flagged"):
            st.update_reel(r["name"], stage="rejected",
                           owner_note=f"Duplicate of {twin['name']} ({twin.get('hook')}); removed by the duplicate audit")
            st.activity("system", f"{r['name']} removed as a duplicate of {twin['name']}", reel=r["name"])
    return findings


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["audit"])
    ap.add_argument("--account", default="silent-comedy")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    print(json.dumps(audit(a.account, a.apply), indent=1, ensure_ascii=False))


if __name__ == "__main__":
    main()
