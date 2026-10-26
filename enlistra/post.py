"""Posting agent.

Every rendered reel gets a manifest (caption, hashtags, platform list, source
attribution) written next to it. Then, depending on `post.mode`:
  draft    -> manifest only + a Typefully draft (if TYPEFULLY_API_KEY is set)
  publish  -> the same, plus platform uploads for every platform with credentials

Platform notes (each needs its own developer app and tokens, set as env vars):
  Instagram Reels  IG_USER_ID, IG_ACCESS_TOKEN  (Graph API; needs a PUBLIC video URL,
                   so set PUBLIC_BASE_URL to wherever `work/out` is served from)
  YouTube Shorts   YT_CLIENT_ID, YT_CLIENT_SECRET; connect the channel from the dashboard
  TikTok           TIKTOK_ACCESS_TOKEN  (Content Posting API; app must be approved
                   for direct post, otherwise it lands in the user's inbox as a draft)
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

from . import store

log = logging.getLogger("post")


def write_manifest(reel: Path, film: dict, clip: dict, cfg: dict) -> Path:
    caption = clip["caption"].strip()
    attribution = f"Source: {film['title']}" + (f" ({film['year']})" if film.get("year") else "")
    attribution += ". Public domain, via Internet Archive."
    tags = [t for t in cfg["hashtags"] if t.lower() != "#shorts"][:3]   # YouTube shows the first 3
    footer = cfg.get("desc_footer", "").strip()
    full = "\n\n".join(x for x in (caption, attribution, footer, " ".join(tags)) if x)
    m = {
        "reel": str(reel), "cover": str(reel.with_suffix(".jpg")),
        "hook": clip["hook"], "title": clip.get("title") or clip["hook"], "style": clip.get("style"),
        "caption": full, "platforms": cfg["platforms"], "yt_tags": cfg.get("yt_tags"),
        "yt_category": cfg.get("yt_category", "23"), "playlist": cfg.get("playlist"),
        "source": {"identifier": film["identifier"], "title": film["title"],
                   "year": film.get("year"), "license": film.get("reason_free"),
                   "url": f"https://archive.org/details/{film['identifier']}",
                   "rights_checked": time.strftime("%Y-%m-%d"),
                   "start": clip["start"], "end": clip["end"], "segments": clip.get("segments"),
                   "payoff": clip.get("payoff"), "sfx": clip.get("sfx")},
        "narration": clip.get("narration", ""), "gag": clip.get("why", ""),
        "status": {p: "pending" for p in cfg["platforms"]},
        "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    p = reel.with_suffix(".json")
    p.write_text(json.dumps(m, indent=2))
    return p


# ---------------------------------------------------------------- Typefully
TF = "https://api.typefully.com/v1"


def typefully_draft(manifest: dict, schedule: bool) -> str | None:
    key = os.environ.get("TYPEFULLY_API_KEY")
    if not key:
        log.info("TYPEFULLY_API_KEY not set; skipping Typefully draft")
        return None
    h = {"X-API-KEY": f"Bearer {key}"}
    body = {"content": manifest["caption"], "schedule-date": "next-free-slot" if schedule else None}
    r = requests.post(f"{TF}/drafts/", headers=h, json={k: v for k, v in body.items() if v}, timeout=30)
    r.raise_for_status()
    d = r.json()
    log.info("Typefully draft %s created", d.get("id"))
    return str(d.get("id"))


# ---------------------------------------------------------------- Instagram
def instagram_reel(manifest: dict) -> str | None:
    uid, tok, base = (os.environ.get("IG_USER_ID"), os.environ.get("IG_ACCESS_TOKEN"),
                      os.environ.get("PUBLIC_BASE_URL"))
    if not (uid and tok and base):
        log.info("Instagram credentials or PUBLIC_BASE_URL missing; skipping")
        return None
    video_url = f"{base.rstrip('/')}/{Path(manifest['reel']).name}"
    g = f"https://graph.facebook.com/v21.0/{uid}"
    r = requests.post(f"{g}/media", params={"media_type": "REELS", "video_url": video_url,
                                            "caption": manifest["caption"], "access_token": tok},
                      timeout=60)
    r.raise_for_status()
    cid = r.json()["id"]
    for _ in range(30):   # wait for Instagram to fetch and process the video
        s = requests.get(f"https://graph.facebook.com/v21.0/{cid}",
                         params={"fields": "status_code", "access_token": tok}, timeout=30).json()
        if s.get("status_code") == "FINISHED":
            break
        time.sleep(10)
    r = requests.post(f"{g}/media_publish", params={"creation_id": cid, "access_token": tok}, timeout=60)
    r.raise_for_status()
    return r.json().get("id")


# ---------------------------------------------------------------- YouTube
# OAuth client: YT_CLIENT_ID / YT_CLIENT_SECRET (Google Cloud, Web application).
# The channel owner connects once via the dashboard ("Connect YouTube"); the
# refresh token is kept in the store under "youtube_token".
# Unaudited API projects can only upload private videos; set YT_PRIVACY=public
# after Google's YouTube API audit passes.
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
YT_SCOPES = ("https://www.googleapis.com/auth/youtube.upload https://www.googleapis.com/auth/youtube.readonly "
             "https://www.googleapis.com/auth/youtube.force-ssl "
             "https://www.googleapis.com/auth/yt-analytics.readonly")


def youtube_access_token() -> str | None:
    cid, sec = os.environ.get("YT_CLIENT_ID"), os.environ.get("YT_CLIENT_SECRET")
    st = store.get()
    d = st.setting("youtube_token")
    if not (cid and sec and d and d.get("refresh_token")):
        return None
    if time.time() < d.get("obtained_at", 0) + d.get("expires_in", 0) - 300:
        return d["access_token"]
    r = requests.post(GOOGLE_TOKEN, data={"client_id": cid, "client_secret": sec, "grant_type": "refresh_token",
                                          "refresh_token": d["refresh_token"]}, timeout=30).json()
    if "access_token" not in r:
        log.error("YouTube refresh failed: %s", r)
        return None
    d.update(access_token=r["access_token"], expires_in=r.get("expires_in", 3600), obtained_at=int(time.time()))
    st.set_setting("youtube_token", d)
    return d["access_token"]


def youtube_short(manifest: dict) -> str | None:
    tok = youtube_access_token()
    if not tok:
        log.info("YouTube not connected (dashboard > Connect YouTube); skipping")
        return None
    path = Path(manifest["reel"])
    size = path.stat().st_size
    base = (manifest.get("title") or manifest.get("hook") or "Charlie Chaplin")
    tag = " #charliechaplin" if "chaplin" in base.lower() else ""
    if len(base) > 80:                       # long fact titles: keep the words, drop the tag
        tag = ""
    title = (base[:100 - len(tag) - 8] + tag + " #shorts")[:100]
    if manifest.get("longform"):
        title = base[:100]
    body = {"snippet": {"title": title, "description": manifest["caption"][:4900], "categoryId": str(manifest.get("yt_category") or "23"),
                        "tags": manifest.get("yt_tags") or ["charlie chaplin", "chaplin", "charlie chaplin comedy",
                                                 "silent film", "classic comedy", "funny", "comedy shorts"]},
            "status": {"privacyStatus": os.environ.get("YT_PRIVACY", "private"),
                       "selfDeclaredMadeForKids": False, "embeddable": True,
                       "containsSyntheticMedia": bool(manifest.get("ai_generated"))}}
    init = requests.post("https://www.googleapis.com/upload/youtube/v3/videos",
                         params={"uploadType": "resumable", "part": "snippet,status"},
                         headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8",
                                  "X-Upload-Content-Type": "video/mp4", "X-Upload-Content-Length": str(size)},
                         data=json.dumps(body), timeout=60)
    if init.status_code != 200:
        raise RuntimeError(f"YouTube init {init.status_code}: {init.text[:300]}")
    with open(path, "rb") as fh:
        up = requests.put(init.headers["Location"], data=fh, timeout=600,
                          headers={"Authorization": f"Bearer {tok}", "Content-Type": "video/mp4"})
    if up.status_code not in (200, 201):
        raise RuntimeError(f"YouTube upload {up.status_code}: {up.text[:300]}")
    vid = up.json().get("id")
    log.info("YouTube video %s (%s)", vid, body["status"]["privacyStatus"])
    if manifest.get("playlist") and vid:
        from .seo import add_to_playlist
        add_to_playlist(store.current_account(), manifest["playlist"], vid)
    return f"https://youtube.com/shorts/{vid}"


# ---------------------------------------------------------------- TikTok
TIKTOK_TOKEN_FILE = Path("work/tiktok_token.json")


def _tiktok_token() -> str | None:
    """Return a valid access token: env var, or the token file (refreshed if needed)."""
    if os.environ.get("TIKTOK_ACCESS_TOKEN"):
        return os.environ["TIKTOK_ACCESS_TOKEN"]
    st = store.get()
    d = st.setting("tiktok_token")
    if not d and TIKTOK_TOKEN_FILE.exists():
        d = json.loads(TIKTOK_TOKEN_FILE.read_text())
        st.set_setting("tiktok_token", d)
    if not d:
        return None
    if time.time() < d.get("obtained_at", 0) + d.get("expires_in", 0) - 300:
        return d["access_token"]
    key, sec = os.environ.get("TIKTOK_CLIENT_KEY"), os.environ.get("TIKTOK_CLIENT_SECRET")
    if not (key and sec):
        log.warning("TikTok token expired and TIKTOK_CLIENT_KEY/SECRET not set to refresh it")
        return None
    r = requests.post("https://open.tiktokapis.com/v2/oauth/token/", data={
        "client_key": key, "client_secret": sec, "grant_type": "refresh_token",
        "refresh_token": d["refresh_token"]}, timeout=30)
    nd = r.json()
    if "access_token" not in nd:
        log.error("TikTok refresh failed: %s", nd)
        return None
    nd["obtained_at"] = int(time.time())
    st.set_setting("tiktok_token", nd)
    try:
        TIKTOK_TOKEN_FILE.write_text(json.dumps(nd, indent=2))
    except OSError:
        pass
    log.info("TikTok token refreshed")
    return nd["access_token"]


def tiktok_post(manifest: dict) -> str | None:
    tok = _tiktok_token()
    if not tok:
        log.info("No TikTok token (run tiktok_auth.py); skipping TikTok")
        return None
    h = {"Authorization": f"Bearer {tok}", "Content-Type": "application/json; charset=UTF-8"}
    size = Path(manifest["reel"]).stat().st_size
    # Unaudited TikTok apps may only post SELF_ONLY, and only to accounts set to
    # Private. After the app audit, set TIKTOK_PRIVACY=PUBLIC_TO_EVERYONE.
    body = {
        "post_info": {"title": (manifest.get("title") or manifest["hook"])[:150],
                      "is_aigc": bool(manifest.get("ai_generated")),
                      "privacy_level": os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY"),
                      "disable_duet": False, "disable_comment": False,
                      "disable_stitch": False, "video_cover_timestamp_ms": 1000},
        "source_info": {"source": "FILE_UPLOAD", "video_size": size,
                        "chunk_size": size, "total_chunk_count": 1}}
    init = requests.post("https://open.tiktokapis.com/v2/post/publish/video/init/",
                         headers=h, json=body, timeout=30)
    j = init.json() if init.content else {}
    if init.status_code != 200 or j.get("error", {}).get("code", "ok") != "ok":
        raise RuntimeError(f"TikTok init {init.status_code}: {j.get('error')}")
    d = j["data"]
    with open(manifest["reel"], "rb") as fh:
        up = requests.put(d["upload_url"], data=fh, headers={
            "Content-Type": "video/mp4", "Content-Range": f"bytes 0-{size-1}/{size}"}, timeout=300)
    up.raise_for_status()
    return d["publish_id"]


PUBLISHERS = {"instagram": instagram_reel, "youtube_shorts": youtube_short, "tiktok": tiktok_post}


def publish(manifest_path: Path, cfg: dict, force: bool = False) -> dict:
    m = json.loads(manifest_path.read_text())
    if m.get("critic", {}).get("verdict") == "flagged" and not force:
        log.warning("%s is flagged by the critic; not posting (use --force)", manifest_path.name)
        return {"skipped": "flagged by critic: " + str(m["critic"].get("feedback"))}
    schedule = cfg["mode"] == "publish"
    if cfg.get("typefully_enabled"):
        try:
            m["status"]["typefully"] = typefully_draft(m, schedule) or "skipped"
        except Exception as e:  # noqa: BLE001
            m["status"]["typefully"] = f"error: {e}"
    try:  # never post the same reel twice (double taps, retries, stale manifests)
        prior = (store.get().get_reel(manifest_path.stem) or {}).get("status") or {}
    except Exception:  # noqa: BLE001
        prior = {}
    try:  # never post a gag that is already out there (same cut, or the same moment from another print)
        from . import dedupe
        twin = dedupe.find_duplicate(store.get(), manifest_path.stem)
    except Exception as e:  # noqa: BLE001
        log.warning("duplicate check failed: %s", e)
        twin = None
    if twin and not any(str(v).startswith("http") for v in prior.values()):
        note = f"skipped: duplicate of {twin}, which is already posted"
        log.warning("%s: %s", manifest_path.stem, note)
        for p in cfg["platforms"]:
            m["status"][p] = note
        manifest_path.write_text(json.dumps(m, indent=2))
        try:
            store.get().update_reel(manifest_path.stem, status=m["status"], stage="rejected",
                                    owner_note=f"Duplicate of {twin}; not posted")
        except Exception:  # noqa: BLE001
            pass
        return m["status"]
    if schedule:
        for p in cfg["platforms"]:
            fn = PUBLISHERS.get(p)
            if not fn:
                continue
            done = str(prior.get(p) or m["status"].get(p) or "")
            if done.startswith("http"):
                log.info("%s already on %s (%s); not posting again", manifest_path.stem, p, done)
                m["status"][p] = done
                continue
            try:
                m["status"][p] = fn(m) or "skipped"
            except Exception as e:  # noqa: BLE001
                log.exception("publish %s failed", p)
                m["status"][p] = f"error: {e}"
    else:
        for p in cfg["platforms"]:
            m["status"][p] = "draft (post.mode=draft)"
    manifest_path.write_text(json.dumps(m, indent=2))
    posted = any(str(v).strip() and not str(v).startswith(("pending", "skipped", "error", "draft"))
                 for k, v in m["status"].items() if k != "typefully")
    try:
        store.get().update_reel(manifest_path.stem, status=m["status"], stage="posted" if posted else "pending")
    except Exception as e:  # noqa: BLE001
        log.warning("store update failed: %s", e)
    return m["status"]
