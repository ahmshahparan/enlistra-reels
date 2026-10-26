"""Keep the 5 GB server volume from filling up.

Safe to delete (everything here can be fetched or rebuilt again):
  work/src        film downloads (a re-render downloads the film again)
  work/cache      local copies of media that also live in Supabase storage
  work/out        local copies of reels that are posted or rejected (posted ones live on YouTube)
  media vault     big files (long videos) once posted or rejected
  footage raw     half-finished uploads older than a day
Kept: anything pending or approved, the footage library (polished masters), music, logs.
"""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

from . import store

log = logging.getLogger("housekeeping")
WORK = Path("work")
DONE = ("posted", "rejected")


def _size(p: Path) -> int:
    if p.is_file() or p.is_symlink():
        try:
            return p.lstat().st_size if p.is_symlink() else p.stat().st_size
        except OSError:
            return 0
    return sum(_size(c) for c in p.rglob("*") if c.is_file())


def report() -> dict:
    out = {}
    for d in sorted(WORK.iterdir()) if WORK.exists() else []:
        out[d.name] = round(_size(d) / 1e6, 1)
    du = shutil.disk_usage(WORK if WORK.exists() else ".")
    return {"dirs_mb": out, "used_gb": round(du.used / 1e9, 2), "free_gb": round(du.free / 1e9, 2),
            "total_gb": round(du.total / 1e9, 2)}


def _rm(p: Path) -> int:
    n = _size(p)
    try:
        if p.is_dir() and not p.is_symlink():
            shutil.rmtree(p)
        else:
            p.unlink()
    except OSError:
        return 0
    return n


def clean(keep_src: set[str] | None = None) -> dict:
    now, freed = time.time(), {}

    def add(k, n):
        freed[k] = freed.get(k, 0) + n

    # film downloads: keep only what a running job is using
    for f in (WORK / "src").glob("*"):
        if keep_src and any(f.name.startswith(k) for k in keep_src):
            continue
        if now - f.stat().st_mtime > 3600:
            add("src", _rm(f))
    # cached copies of stored media
    for f in (WORK / "cache").glob("*"):
        if now - f.lstat().st_mtime > 6 * 3600:
            add("cache", _rm(f))
    # reels that are finished with: posted (on YouTube) or rejected
    stages = {}
    for acct in [a["id"] for a in store.accounts()]:
        try:
            for r in store.get(account=acct).reels():
                stages[r["name"]] = r.get("stage")
        except Exception as e:  # noqa: BLE001
            log.warning("reels for %s: %s", acct, e)
    for f in list((WORK / "out").glob("*")):
        stem = f.name.split(".")[0]
        if stages.get(stem) in DONE and now - f.stat().st_mtime > 1800:
            add("out", _rm(f))
    vault = WORK / "media"
    for f in vault.glob("*") if vault.exists() else []:
        stem = f.name.split("__")[-1].split(".")[0]
        if stages.get(stem) in DONE:
            add("vault", _rm(f))
    # abandoned uploads
    for raw in (WORK / "footage").glob("*/raw/*") if (WORK / "footage").exists() else []:
        if now - raw.stat().st_mtime > 86400:
            add("raw", _rm(raw))
    res = {k: round(v / 1e6, 1) for k, v in freed.items()}
    log.info("housekeeping freed %s MB", res)
    return res
