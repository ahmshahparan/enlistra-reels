"""Analyst agent: pulls per-video performance every night and ranks what works.

YouTube  videos.list(part=statistics) for every posted reel (views, likes, comments);
         when the connected token also carries yt-analytics.readonly, average view
         percentage and average view duration from the YouTube Analytics API.
Results go to the `metrics` table (one row per reel per platform per run) and a
compact summary per format (style) and per film goes to settings["analyst_summary"],
which the Showrunner and the dashboard read.
"""
from __future__ import annotations

import logging
import re
import time
from collections import defaultdict
from datetime import date, timedelta

import requests

from . import post, store

log = logging.getLogger("analyst")
YT_ID = re.compile(r"(?:shorts/|v=|youtu\.be/)([\w-]{11})")


def _yt_id(status: dict) -> str | None:
    m = YT_ID.search(str((status or {}).get("youtube_shorts") or ""))
    return m.group(1) if m else None


def _stats(tok: str, ids: list[str]) -> dict:
    out = {}
    for i in range(0, len(ids), 50):
        r = requests.get("https://www.googleapis.com/youtube/v3/videos",
                         params={"part": "statistics", "id": ",".join(ids[i:i + 50])},
                         headers={"Authorization": f"Bearer {tok}"}, timeout=30).json()
        for it in r.get("items", []):
            s = it.get("statistics", {})
            out[it["id"]] = {k: int(s.get(k, 0)) for k in ("viewCount", "likeCount", "commentCount")}
    return out


def _retention(tok: str, ids: list[str]) -> dict:
    """Average view percentage and duration per video (needs yt-analytics.readonly)."""
    if not ids:
        return {}
    r = requests.get("https://youtubeanalytics.googleapis.com/v2/reports", params={
        "ids": "channel==MINE", "startDate": "2020-01-01", "endDate": date.today().isoformat(),
        "metrics": "averageViewPercentage,averageViewDuration", "dimensions": "video",
        "filters": "video==" + ",".join(ids[:200])}, headers={"Authorization": f"Bearer {tok}"}, timeout=30)
    if r.status_code != 200:
        log.info("retention unavailable (%s); reconnect YouTube to grant analytics", r.status_code)
        return {}
    return {row[0]: {"avg_pct": row[1], "avg_s": row[2]} for row in r.json().get("rows", [])}


def run(account: str | None = None) -> dict:
    if account:
        store.use(account)
    st = store.get()
    tok = post.youtube_access_token()
    if not tok:
        return {"skipped": "YouTube not connected"}
    reels = [r for r in st.reels() if _yt_id(r.get("status"))]
    ids = {_yt_id(r["status"]): r for r in reels}
    stats = _stats(tok, list(ids))
    ret = _retention(tok, list(ids))
    rows = []
    for vid, r in ids.items():
        s = stats.get(vid)
        if not s:
            continue
        rows.append({"account_id": st.account, "reel": r["name"], "platform": "youtube",
                     "views": s["viewCount"], "likes": s["likeCount"], "comments": s["commentCount"],
                     "avg_watch_s": (ret.get(vid) or {}).get("avg_s"),
                     "completion": (ret.get(vid) or {}).get("avg_pct")})
    if rows and getattr(st, "kind", "") == "supabase":
        st._rest("POST", "metrics", body=rows)
    # summary by format and by film
    by = defaultdict(lambda: {"n": 0, "views": 0})
    for row in rows:
        r = ids[_yt_id(next(x for x in reels if x["name"] == row["reel"])["status"])]
        for key in (f"style:{r.get('style') or 'card'}", f"film:{(r.get('source') or {}).get('title')}"):
            by[key]["n"] += 1
            by[key]["views"] += row["views"]
    summary = {"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "videos": len(rows),
               "views": sum(r["views"] for r in rows),
               "per_video": {r["reel"]: {"views": r["views"], "likes": r["likes"], "completion": r["completion"]}
                             for r in rows},
               "groups": {k: {"n": v["n"], "avg_views": round(v["views"] / v["n"])} for k, v in by.items()}}
    st.set_setting("analyst_summary", summary)
    st.activity("analyst", f"Stats updated: {len(rows)} videos, {summary['views']:,} views in total")
    return {"videos": len(rows), "views": summary["views"]}
