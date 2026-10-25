"""Enlistra Reels operator dashboard + scheduler.

One process does everything the Mac launchers used to do:
  - the operator web app (queue, approve / reject / edit, activity feed, brand brain)
  - the schedule (SCHEDULE="09:00,17:00", TZ America/New_York) that runs the pipeline
  - the kill switch, which the schedule respects

State lives in the store (Supabase when SUPABASE_URL/SUPABASE_SECRET_KEY are set,
otherwise the work/ folder). Protect it with DASHBOARD_PASSWORD (HTTP basic auth).

    python dashboard.py            # http://127.0.0.1:8765
"""
from __future__ import annotations

import base64
import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, Response, jsonify, request, send_file

from enlistra import env, pipeline, post as poster, store

env.load()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("dashboard")

CFG = pipeline.load_cfg("config.yaml")
WORK = Path(CFG["paths"]["work"])
ST = store.get(WORK)
PLIST = Path.home() / "Library/LaunchAgents/com.enlistra.reels.plist"
PASSWORD = os.environ.get("DASHBOARD_PASSWORD", "")
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/New_York"))
_SCHED = os.environ.get("SCHEDULE", "09:00,17:00")
SCHEDULE = [] if _SCHED.lower() == "off" else [s.strip() for s in _SCHED.split(",") if s.strip()]
app = Flask(__name__, static_folder=None)
RUN_LOCK = threading.Lock()


# ------------------------------------------------------------------ auth
@app.before_request
def _auth():
    if not PASSWORD:
        return None
    a = request.headers.get("Authorization", "")
    if a.startswith("Basic "):
        try:
            user, _, pw = base64.b64decode(a[6:]).decode().partition(":")
            if pw == PASSWORD:
                return None
        except Exception:  # noqa: BLE001
            pass
    return Response("Enlistra Reels: sign in", 401, {"WWW-Authenticate": 'Basic realm="Enlistra Reels"'})


# ------------------------------------------------------------------ jobs
def _job(name: str, fn, *args):
    def run():
        ST.set_job(name, "running")
        try:
            res = fn(*args)
            ST.set_job(name, "done", str(res))
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", name)
            ST.set_job(name, "error", str(e))
            ST.activity("error", f"{name}: {e}")
    threading.Thread(target=run, daemon=True).start()


def do_publish(name: str, platforms: list | None = None):
    mp4, man = ST.local_files(name)
    pcfg = {**CFG["post"], "platforms": platforms} if platforms else CFG["post"]
    res = poster.publish(man, pcfg, force=True)
    ST.activity("post", f"{name}.mp4 posted: {json.dumps(res)}", reel=name)
    return res


def do_rerender(name: str, hook: str, narration: str):
    from enlistra import analyze, critic, edit, source
    from enlistra.select import Clip
    r = ST.get_reel(name)
    if not r:
        raise RuntimeError("unknown reel")
    src = r["source"]
    ident = src["identifier"]
    videos = list((WORK / "src").glob(f"{ident}.*"))
    if videos:
        video = videos[0]
    else:
        film = source.resolve(ident, CFG["source"])
        if not film:
            raise RuntimeError(f"cannot fetch {ident} again from the Internet Archive")
        video = source.download(film, WORK / "src")
    apath = WORK / "analysis" / f"{ident}.json"
    a = analyze.Analysis.load(apath) if apath.exists() else analyze.analyze(video, apath)
    segs = src.get("segments") or [[src["start"], src["end"]]]
    viewer_caption = (r.get("caption") or "").split("\n\nSource:")[0].strip() or hook
    clip = Clip(start=segs[0][0], end=segs[-1][1], score=0, hook=hook, caption=viewer_caption,
                why=r.get("gag") or "", narration=narration, payoff=src.get("payoff"),
                sfx=src.get("sfx") or [], segments=segs, title=r.get("title") or hook,
                style=r.get("style") or CFG["edit"].get("style", "cold"))
    film_info = {"title": src.get("title"), "year": src.get("year")}
    out = ST.out / f"{name}.mp4"
    ecfg, scfg = CFG["edit"], CFG["select"]
    ccfg = {**CFG.get("critic", {}), "narration_start": ecfg.get("narration_start", 0.4),
            "anthropic_model": scfg.get("anthropic_model", "claude-sonnet-5-5")}
    critic.prune_sfx(clip, a)
    edit.render(video, clip, out, ecfg, film=film_info, analysis=a)
    card = critic.review(out, clip, film_info, a, getattr(clip, "narr_dur", 0.0), float(ecfg.get("speed", 1.0)), ccfg)
    verdict = "pass" if card["verdict"] == "pass" else "flagged"
    man = poster.write_manifest(out, {"identifier": ident, "title": src.get("title"), "year": src.get("year"),
                                      "reason_free": src.get("license")},
                                {"hook": hook, "caption": viewer_caption, "why": r.get("gag") or "",
                                 "start": clip.start, "end": clip.end, "segments": segs, "payoff": clip.payoff,
                                 "sfx": clip.sfx, "narration": narration, "title": clip.title,
                                 "style": clip.style}, CFG["post"])
    m = json.loads(man.read_text())
    m["critic"] = {"verdict": verdict, "scores": (card.get("judge") or {}).get("scores"), "feedback": card.get("feedback")}
    m["approval"] = "pending"
    ST.save_reel(name, m, card)
    sc = (card.get("judge") or {}).get("scores", {}).get("overall")
    ST.activity("critic", f"{name}.mp4 re-rendered: {verdict}" + (f", {sc}/5" if sc else "") +
                (f". {card['feedback']}" if card.get("feedback") else ""), reel=name)
    return verdict


def do_run(films: int, ident: str | None, trigger: str = "owner"):
    if not RUN_LOCK.acquire(blocking=False):
        raise RuntimeError("a run is already in progress")
    try:
        cmd = [sys.executable, "cli.py", "run", "--films", str(films)] + (["--id", ident] if ident else [])
        ST.activity("system", f"Run started by {trigger}" + (f" for {ident}" if ident else f" ({films} film)"))
        with open(WORK / "run.log", "a") as fh:
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True)
        ST.activity("system", f"Run finished (exit {r.returncode})")
        return r.returncode
    finally:
        RUN_LOCK.release()


# ------------------------------------------------------------------ scheduler
def _scheduler():
    fired = set()
    log.info("schedule %s (%s)", SCHEDULE, TZ)
    while True:
        now = datetime.now(TZ)
        key = (now.date(), now.strftime("%H:%M"))
        if now.strftime("%H:%M") in SCHEDULE and key not in fired:
            fired.add(key)
            if ST.paused():
                ST.activity("system", "Scheduled run skipped: the kill switch is on")
            elif not RUN_LOCK.locked():
                _job("run", do_run, 1, None, "schedule")
        time.sleep(20)


# ------------------------------------------------------------------ routes
@app.get("/")
def index():
    return Response((Path(__file__).parent / "dashboard.html").read_text(), mimetype="text/html")


# ------------------------------------------------------------------ YouTube connect
@app.get("/oauth/youtube/start")
def yt_start():
    import secrets as _s
    from urllib.parse import urlencode
    cid = os.environ.get("YT_CLIENT_ID")
    if not cid:
        return "YT_CLIENT_ID is not set on the server yet.", 400
    state = _s.token_urlsafe(16)
    ST.set_setting("youtube_oauth_state", state)
    q = urlencode({"client_id": cid, "redirect_uri": _yt_redirect(), "response_type": "code",
                   "scope": poster.YT_SCOPES, "access_type": "offline", "prompt": "consent",
                   "include_granted_scopes": "true", "state": state})
    return Response(status=302, headers={"Location": "https://accounts.google.com/o/oauth2/v2/auth?" + q})


def _yt_redirect() -> str:
    base = os.environ.get("PUBLIC_URL") or request.host_url.rstrip("/").replace("http://", "https://")
    return base + "/oauth/youtube/callback"


@app.get("/oauth/youtube/callback")
def yt_callback():
    import requests as rq
    if request.args.get("state") != ST.setting("youtube_oauth_state"):
        return "State mismatch; start again from the dashboard.", 400
    code = request.args.get("code")
    if not code:
        return f"YouTube connection cancelled: {request.args.get('error')}", 400
    r = rq.post(poster.GOOGLE_TOKEN, data={"code": code, "client_id": os.environ["YT_CLIENT_ID"],
                                           "client_secret": os.environ["YT_CLIENT_SECRET"],
                                           "redirect_uri": _yt_redirect(), "grant_type": "authorization_code"},
                timeout=30).json()
    if "refresh_token" not in r:
        return f"Google did not return a refresh token: {r}", 400
    r["obtained_at"] = int(time.time())
    ch = rq.get("https://www.googleapis.com/youtube/v3/channels", params={"part": "snippet", "mine": "true"},
                headers={"Authorization": f"Bearer {r['access_token']}"}, timeout=30).json()
    items = ch.get("items") or []
    r["channel"] = {"id": items[0]["id"], "title": items[0]["snippet"]["title"]} if items else None
    ST.set_setting("youtube_token", r)
    name = r["channel"]["title"] if r["channel"] else "(no channel on this Google account yet)"
    ST.activity("owner", f"YouTube connected: {name}")
    return Response(status=302, headers={"Location": "/"})


@app.get("/health")
def health():
    return jsonify(ok=True, store=ST.kind)


@app.get("/media/<path:fname>")
def media(fname):
    if "/" in fname or fname.startswith("."):
        return "", 404
    p = ST.media(fname)
    if not p:
        return "", 404
    return send_file(p.resolve(), conditional=True)


@app.get("/api/state")
def state():
    jobs = ST.jobs()
    items = []
    for r in ST.reels():
        name = r["name"]
        items.append({**r, "video": f"/media/{name}.mp4", "cover": f"/media/{name}.jpg",
                      "scores": (r.get("critic") or {}).get("scores") or (r.get("judge") or {}).get("scores"),
                      "fix": (r.get("judge") or {}).get("fix"), "created": r.get("created_at"),
                      "job": jobs.get(name)})
    counts = {}
    for i in items:
        counts[i["stage"]] = counts.get(i["stage"], 0) + 1
    runlog = WORK / "run.log"
    tail = runlog.read_text().splitlines()[-25:] if runlog.exists() else []
    return jsonify({
        "reels": items, "counts": counts, "activity": ST.activities(),
        "paused": ST.paused(), "schedule": SCHEDULE, "tz": str(TZ), "store": ST.kind,
        "require_approval": bool(CFG["post"].get("require_approval", True)),
        "privacy": os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY"),
        "youtube": _yt_status(),
        "jobs": jobs, "running": RUN_LOCK.locked() or (jobs.get("run") or {}).get("status") == "running",
        "log": tail, "platform": platform.system(), "brain": ST.brains(),
        "config": {"reels_per_film": CFG["select"]["reels_per_film"], "target": CFG.get("critic", {}).get("target_score"),
                   "accept": CFG.get("critic", {}).get("accept_score"), "platforms": CFG["post"]["platforms"]},
    })


def _yt_status() -> dict:
    t = ST.setting("youtube_token") or {}
    return {"configured": bool(os.environ.get("YT_CLIENT_ID")), "connected": bool(t.get("refresh_token")),
            "channel": (t.get("channel") or {}).get("title"), "privacy": os.environ.get("YT_PRIVACY", "private")}


@app.post("/api/approve/<name>")
def approve(name):
    plats = (request.get_json(silent=True) or {}).get("platforms")
    ST.update_reel(name, stage="approved")
    ST.activity("owner", f"Approved {name}.mp4; posting" + (f" to {', '.join(plats)}" if plats else ""), reel=name)
    _job(name, do_publish, name, plats)
    return jsonify(ok=True)


@app.post("/api/reject/<name>")
def reject(name):
    note = (request.json or {}).get("note", "")
    ST.update_reel(name, stage="rejected", owner_note=note)
    ST.activity("owner", f"Rejected {name}.mp4" + (f": {note}" if note else ""), reel=name)
    return jsonify(ok=True)


@app.post("/api/restore/<name>")
def restore(name):
    ST.update_reel(name, stage="pending")
    return jsonify(ok=True)


@app.post("/api/rerender/<name>")
def rerender(name):
    d = request.json or {}
    ST.activity("owner", f"Edited {name}.mp4; re-rendering", reel=name)
    _job(name, do_rerender, name, d.get("hook", ""), d.get("narration", ""))
    return jsonify(ok=True)


@app.post("/api/run")
def run_now():
    if RUN_LOCK.locked():
        return jsonify(ok=False, error="a run is already in progress"), 409
    d = request.json or {}
    _job("run", do_run, int(d.get("films", 1)), d.get("id") or None)
    return jsonify(ok=True)


@app.post("/api/pause")
def pause():
    on = bool((request.json or {}).get("on", True))
    ST.set_paused(on)
    ST.activity("owner", "Kill switch ON: scheduled runs will not start" if on else "Kill switch OFF: scheduled runs resume")
    return jsonify(ok=True, paused=on)


@app.post("/api/brain/<name>")
def brain(name):
    if not name.replace("-", "").replace("_", "").isalnum():
        return jsonify(ok=False), 400
    ST.set_brain(name, (request.json or {}).get("text", ""))
    ST.activity("owner", f"Brand brain updated: {name}.md")
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    if SCHEDULE:
        threading.Thread(target=_scheduler, daemon=True).start()
    print(f"Enlistra Reels dashboard: http://{host}:{port}  (store: {ST.kind})")
    app.run(host=host, port=port, debug=False, threaded=True)
