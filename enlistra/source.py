"""Sourcing agent: find and download free-to-use films from the Internet Archive.

Only items that pass `is_free_to_use()` are returned: US works published before the
configured cutoff year (public domain) or items carrying a Creative Commons /
public domain license URL. Everything else is skipped and logged.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, asdict
from pathlib import Path

import requests

log = logging.getLogger("source")
IA_SEARCH = "https://archive.org/advancedsearch.php"
IA_META = "https://archive.org/metadata/{id}"
IA_DL = "https://archive.org/download/{id}/{name}"
FREE_LICENSE_HINTS = ("creativecommons.org", "publicdomain", "public domain")


@dataclass
class Film:
    identifier: str
    title: str
    year: int | None
    license_url: str | None
    file_name: str | None = None
    size_mb: float | None = None
    duration_s: float | None = None
    local_path: str | None = None
    reason_free: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self))


def _year(meta: dict) -> int | None:
    for key in ("year", "date"):
        v = meta.get(key)
        if isinstance(v, list):
            v = v[0] if v else None
        if v:
            m = re.search(r"(1[89]\d\d|20\d\d)", str(v))
            if m:
                return int(m.group(1))
    return None


def is_free_to_use(meta: dict, pd_year_cutoff: int) -> tuple[bool, str]:
    year = _year(meta)
    lic = meta.get("licenseurl") or ""
    if isinstance(lic, list):
        lic = lic[0] if lic else ""
    rights = str(meta.get("rights") or "").lower()
    if year and year < pd_year_cutoff:
        return True, f"published {year}, before {pd_year_cutoff} (US public domain)"
    low = lic.lower()
    if "creativecommons.org" in low and ("-nc" in low or "-nd" in low or "/nc" in low or "/nd" in low):
        return False, f"license {lic} forbids commercial use or edits"
    if any(h in low for h in FREE_LICENSE_HINTS):
        return True, f"license {lic}"
    if "public domain" in rights:
        return True, f"rights field: {rights[:60]}"
    return False, f"year={year} license={lic or 'none'}"


def search(query: str, rows: int = 25) -> list[dict]:
    params = {
        "q": query,
        "fl[]": ["identifier", "title", "year", "date", "licenseurl"],
        "rows": rows,
        "output": "json",
        "sort[]": "downloads desc",
    }
    r = requests.get(IA_SEARCH, params=params, timeout=30)
    r.raise_for_status()
    return r.json()["response"]["docs"]


def _height(f: dict) -> int:
    try:
        return int(f.get("height") or 0)
    except (TypeError, ValueError):
        return 0


def pick_file(files: list[dict], prefer: list[str], max_mb: float) -> dict | None:
    """The sharpest video file under the size cap (resolution first, then preferred
    container, then the smaller file). Archive '512kb' derivatives are a last resort."""
    rank = {ext: i for i, ext in enumerate(prefer)}
    cands = [f for f in files
             if f["name"].lower().rsplit(".", 1)[-1] in rank and int(f.get("size", 0)) / 1e6 <= max_mb]
    if not cands:
        return None
    return max(cands, key=lambda f: (_height(f), "512kb" not in f["name"].lower(),
                                     -rank[f["name"].lower().rsplit(".", 1)[-1]], -int(f.get("size", 0))))


def resolve(identifier: str, cfg: dict) -> Film | None:
    r = requests.get(IA_META.format(id=identifier), timeout=30)
    r.raise_for_status()
    d = r.json()
    meta = d.get("metadata", {})
    ok, reason = is_free_to_use(meta, cfg["pd_year_cutoff"])
    title = meta.get("title", identifier)
    if isinstance(title, list):
        title = title[0]
    if not ok:
        log.info("skip %s (%s): %s", identifier, title, reason)
        return None
    f = pick_file(d.get("files", []), cfg["prefer_formats"], cfg["max_download_mb"])
    if f and _height(f) and _height(f) < int(cfg.get("min_height", 0)):
        log.info("skip %s: best copy is %sp, below %sp", identifier, _height(f), cfg.get("min_height"))
        return None
    if not f:
        log.info("skip %s: no usable video file under %s MB", identifier, cfg["max_download_mb"])
        return None
    return Film(
        identifier=identifier,
        title=str(title),
        year=_year(meta),
        license_url=meta.get("licenseurl"),
        file_name=f["name"],
        size_mb=round(int(f.get("size", 0)) / 1e6, 1),
        duration_s=float(f["length"]) if f.get("length") else None,
        reason_free=reason,
    )


def download(film: Film, dest_dir: Path) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{film.identifier}{Path(film.file_name).suffix}"
    if out.exists() and out.stat().st_size > 1_000_000:
        film.local_path = str(out)
        return out
    url = IA_DL.format(id=film.identifier, name=requests.utils.quote(film.file_name))
    log.info("downloading %s (%s MB)", film.title, film.size_mb)
    with requests.get(url, stream=True, timeout=120) as r:
        r.raise_for_status()
        with open(out, "wb") as fh:
            for chunk in r.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    film.local_path = str(out)
    return out


def find_films(cfg: dict, already: set[str], limit: int = 1) -> list[Film]:
    """Return up to `limit` new, free-to-use films across the configured queries."""
    found: list[Film] = []
    for ident in cfg.get("priority_ids", []):          # known sharp prints first
        if ident in already or any(f.identifier == ident for f in found):
            continue
        film = resolve(ident, cfg)
        if film:
            found.append(film)
            if len(found) >= limit:
                return found
    for q in cfg["queries"]:
        for doc in search(q):
            ident = doc["identifier"]
            if ident in already or any(f.identifier == ident for f in found):
                continue
            film = resolve(ident, cfg)
            if film:
                found.append(film)
                if len(found) >= limit:
                    return found
    return found
