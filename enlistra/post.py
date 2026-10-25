"""Posting agent.

Every rendered reel gets a manifest (caption, hashtags, platform list, source
attribution) written next to it. Then, depending on `post.mode`:
  draft    -> manifest only + a Typefully draft (if TYPEFULLY_API_KEY is set)
  publish  -> the same, plus platform uploads for every platform with credentials

Platform notes (each needs its own developer app and tokens, set as env vars):
  Instagram Reels  IG_USER_ID, IG_ACCESS_TOKEN  (Graph API; needs a PUBLIC video URL,
                   so set PUBLIC_BASE_URL to wherever `work/out` is served from)
  YouTube Shorts   YT_CLIENT_SECRETS (OAuth JSON) and a stored refresh token
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
    full = f"{caption}\n\n{attribution}\n\n{' '.join(cfg['hashtags'])}"
    m = {
        "reel": str(reel), "cover": str(reel.with_suffix(".jpg")),
        "hook": clip["hook"], "caption": full, "platforms": cfg["platforms"],
        "source": {"identifier": film["identifier"], "title": film["title"],
                   "year": film.get("year"), "license": film.get("reason_free"),
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
def youtube_short(manifest: dict) -> str | None:
    """Requires google-api-python-client + an OAuth refresh token in YT_TOKEN_JSON."""
    tok = os.environ.get("YT_TOKEN_JSON")
    if not tok:
        log.info("YT_TOKEN_JSON not set; skipping YouTube")
        return None
    try:
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build
        from googleapiclient.http import MediaFileUpload
    except ImportError:
        log.warning("google-api-python-client not installed; skipping YouTube")
        return None
    creds = Credentials.from_authorized_user_info(json.loads(Path(tok).read_text()))
    yt = build("youtube", "v3", credentials=creds)
    body = {"snippet": {"title": manifest["hook"][:95] + " #Shorts",
                        "description": manifest["caption"], "categoryId": "23"},
            "status": {"privacyStatus": "public", "selfDeclaredMadeForKids": False}}
    media = MediaFileUpload(manifest["reel"], mimetype="video/mp4", resumable=True)
    req = yt.videos().insert(part="snippet,status", body=body, media_body=media)
    resp = None
    while resp is None:
        _, resp = req.next_chunk()
    return resp.get("id")


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
        "post_info": {"title": manifest["hook"][:150],
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
    if schedule:
        for p in cfg["platforms"]:
            fn = PUBLISHERS.get(p)
            if not fn:
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
