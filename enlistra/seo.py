"""YouTube SEO tools: channel description/keywords/banner, video metadata, playlists.

Needs the youtube.force-ssl scope on the stored token (reconnect from the dashboard).

    python -m enlistra.seo whoami  --account wild-facts
    python -m enlistra.seo apply   spec.json
    python -m enlistra.seo list    --account silent-comedy

spec.json: {"accounts": {"<id>": {"channel": {...}, "banner": "path|url", "videos": {"<vid>": {...}},
            "playlists": [{"title", "description", "videos": [ids]}]}}}
"""
import argparse
import json
import logging
import sys

import requests

from . import store
from .post import youtube_access_token

log = logging.getLogger("seo")
API = "https://www.googleapis.com/youtube/v3"


def _h(account):
    prev = getattr(store._LOCAL, "account", None)
    store.use(account)
    try:
        tok = youtube_access_token()
    finally:
        store.use(prev)
    if not tok:
        raise SystemExit(f"{account}: YouTube not connected")
    return {"Authorization": f"Bearer {tok}"}


def _ok(r):
    if r.status_code >= 300:
        raise RuntimeError(f"{r.request.method} {r.url.split('?')[0]} {r.status_code}: {r.text[:400]}")
    return r.json() if r.text else {}


def whoami(account):
    h = _h(account)
    d = _ok(requests.get(f"{API}/channels", params={"part": "snippet,brandingSettings,statistics", "mine": "true"},
                         headers=h, timeout=30))
    return d.get("items", [{}])[0]


def list_videos(account):
    h = _h(account)
    ch = whoami(account)
    uploads = _ok(requests.get(f"{API}/channels", params={"part": "contentDetails", "id": ch["id"]}, headers=h,
                               timeout=30))["items"][0]["contentDetails"]["relatedPlaylists"]["uploads"]
    out, tok = [], None
    while True:
        d = _ok(requests.get(f"{API}/playlistItems", params={"part": "contentDetails", "playlistId": uploads,
                                                               "maxResults": 50, "pageToken": tok or ""},
                             headers=h, timeout=30))
        out += [i["contentDetails"]["videoId"] for i in d.get("items", [])]
        tok = d.get("nextPageToken")
        if not tok:
            break
    vids = []
    for i in range(0, len(out), 50):
        d = _ok(requests.get(f"{API}/videos", params={"part": "snippet,status,statistics", "id": ",".join(out[i:i + 50])},
                             headers=h, timeout=30))
        vids += d.get("items", [])
    return vids


def update_channel(account, description=None, keywords=None, country=None, title=None):
    h = _h(account)
    ch = whoami(account)
    b = ch.get("brandingSettings", {})
    c = b.get("channel", {})
    if description is not None:
        c["description"] = description
    if keywords is not None:
        c["keywords"] = " ".join(f'"{k}"' if " " in k else k for k in keywords)
    if country:
        c["country"] = country
    if title:
        c["title"] = title
    c.pop("trackingAnalyticsAccountId", None)
    body = {"id": ch["id"], "brandingSettings": {"channel": c}}
    _ok(requests.put(f"{API}/channels", params={"part": "brandingSettings"}, headers=h, json=body, timeout=30))
    log.info("%s channel updated", account)


def upload_banner(account, src):
    h = _h(account)
    data = requests.get(src, timeout=60).content if src.startswith("http") else open(src, "rb").read()
    r = _ok(requests.post("https://www.googleapis.com/upload/youtube/v3/channelBanners/insert",
                          headers={**h, "Content-Type": "image/jpeg"}, data=data, timeout=120))
    ch = whoami(account)
    c = ch.get("brandingSettings", {})
    c.setdefault("image", {})["bannerExternalUrl"] = r["url"]
    body = {"id": ch["id"], "brandingSettings": {"channel": c.get("channel", {}), "image": c["image"]}}
    _ok(requests.put(f"{API}/channels", params={"part": "brandingSettings"}, headers=h, json=body, timeout=30))
    log.info("%s banner set", account)


def update_video(account, vid, title=None, description=None, tags=None, category=None):
    h = _h(account)
    cur = _ok(requests.get(f"{API}/videos", params={"part": "snippet", "id": vid}, headers=h, timeout=30))["items"]
    if not cur:
        raise RuntimeError(f"video {vid} not found on {account}")
    s = cur[0]["snippet"]
    snip = {"title": (title or s["title"])[:100], "description": (description if description is not None
                                                                   else s.get("description", ""))[:4900],
            "categoryId": str(category or s.get("categoryId", "23")),
            "tags": tags if tags is not None else s.get("tags", [])}
    if s.get("defaultLanguage"):
        snip["defaultLanguage"] = s["defaultLanguage"]
    _ok(requests.put(f"{API}/videos", params={"part": "snippet"}, headers=h, json={"id": vid, "snippet": snip},
                     timeout=30))
    log.info("%s video %s -> %s", account, vid, snip["title"])


def ensure_playlist(account, title, description, video_ids):
    h = _h(account)
    pls = _ok(requests.get(f"{API}/playlists", params={"part": "snippet", "mine": "true", "maxResults": 50},
                           headers=h, timeout=30)).get("items", [])
    pl = next((p for p in pls if p["snippet"]["title"] == title), None)
    if not pl:
        pl = _ok(requests.post(f"{API}/playlists", params={"part": "snippet,status"}, headers=h, timeout=30,
                               json={"snippet": {"title": title, "description": description},
                                     "status": {"privacyStatus": "public"}}))
    have = set()
    tok = None
    while True:
        d = _ok(requests.get(f"{API}/playlistItems", params={"part": "contentDetails", "playlistId": pl["id"],
                                                               "maxResults": 50, "pageToken": tok or ""},
                             headers=h, timeout=30))
        have |= {i["contentDetails"]["videoId"] for i in d.get("items", [])}
        tok = d.get("nextPageToken")
        if not tok:
            break
    for v in video_ids:
        if v not in have:
            _ok(requests.post(f"{API}/playlistItems", params={"part": "snippet"}, headers=h, timeout=30,
                              json={"snippet": {"playlistId": pl["id"],
                                                "resourceId": {"kind": "youtube#video", "videoId": v}}}))
    log.info("%s playlist '%s' (%d videos)", account, title, len(video_ids))
    return pl["id"]


def add_to_playlist(account, title, vid, description=""):
    """Used by the publisher: file a new upload into a named playlist (best effort)."""
    try:
        ensure_playlist(account, title, description, [vid])
    except Exception as e:  # noqa: BLE001
        log.warning("playlist add failed: %s", e)


def apply(spec):
    for acc, s in spec["accounts"].items():
        if "channel" in s:
            update_channel(acc, **s["channel"])
        if s.get("banner"):
            upload_banner(acc, s["banner"])
        for vid, v in s.get("videos", {}).items():
            update_video(acc, vid, **v)
        for p in s.get("playlists", []):
            ensure_playlist(acc, p["title"], p.get("description", ""), p.get("videos", []))


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["whoami", "list", "apply"])
    ap.add_argument("spec", nargs="?")
    ap.add_argument("--account", default="silent-comedy")
    a = ap.parse_args(argv)
    if a.cmd == "whoami":
        ch = whoami(a.account)
        print(json.dumps({"id": ch.get("id"), "title": ch["snippet"]["title"],
                          "handle": ch["snippet"].get("customUrl"), "stats": ch.get("statistics")}, indent=2))
    elif a.cmd == "list":
        for v in list_videos(a.account):
            print(v["id"], v["status"]["privacyStatus"], v["statistics"].get("viewCount"), "|", v["snippet"]["title"])
    else:
        apply(json.load(open(a.spec)))


if __name__ == "__main__":
    sys.exit(main())
