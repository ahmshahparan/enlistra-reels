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

_BASE = pipeline.load_cfg("config.yaml", "silent-comedy")
WORK = Path(_BASE["paths"]["work"])


def S():
    """Store for the account this thread (request or job) is working on."""
    return store.get(WORK)


def C() -> dict:
    """Config for the current account (base config.yaml + accounts/<id>.yaml)."""
    return pipeline.load_cfg("config.yaml", store.current_account())


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
    store.use(request.args.get("a") or None)
    if not PASSWORD or request.path in ("/oauth/youtube/callback", "/health"):
        return None                      # the callback is protected by its one-time state value
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
    acct = store.current_account()

    def run():
        store.use(acct)
        S().set_job(name, "running")
        try:
            res = fn(*args)
            S().set_job(name, "done", str(res))
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", name)
            S().set_job(name, "error", str(e))
            S().activity("error", f"{name}: {e}")
    threading.Thread(target=run, daemon=True).start()


_PUBLISHING: set = set()
_PUB_LOCK = threading.Lock()


def do_publish(name: str, platforms: list | None = None):
    with _PUB_LOCK:
        if name in _PUBLISHING:
            return {"skipped": "already publishing"}
        _PUBLISHING.add(name)
    try:
        mp4, man = S().local_files(name)
        pcfg = {**C()["post"], "platforms": platforms} if platforms else C()["post"]
        res = poster.publish(man, pcfg, force=True)
    finally:
        _PUBLISHING.discard(name)
    S().activity("post", f"{name}.mp4 posted: {json.dumps(res)}", reel=name)
    return res


def do_rerender(name: str, hook: str, narration: str):
    from enlistra import analyze, critic, edit, source
    from enlistra.select import Clip
    r = S().get_reel(name)
    if not r:
        raise RuntimeError("unknown reel")
    src = r["source"]
    ident = src["identifier"]
    videos = list((WORK / "src").glob(f"{ident}.*"))
    if videos:
        video = videos[0]
    else:
        film = source.resolve(ident, C()["source"])
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
                style=r.get("style") or C()["edit"].get("style", "cold"))
    film_info = {"title": src.get("title"), "year": src.get("year")}
    out = S().out / f"{name}.mp4"
    ecfg, scfg = C()["edit"], C()["select"]
    ccfg = {**C().get("critic", {}), "narration_start": ecfg.get("narration_start", 0.4),
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
                                 "style": clip.style}, C()["post"])
    m = json.loads(man.read_text())
    m["critic"] = {"verdict": verdict, "scores": (card.get("judge") or {}).get("scores"), "feedback": card.get("feedback")}
    m["approval"] = "pending"
    S().save_reel(name, m, card)
    sc = (card.get("judge") or {}).get("scores", {}).get("overall")
    S().activity("critic", f"{name}.mp4 re-rendered: {verdict}" + (f", {sc}/5" if sc else "") +
                (f". {card['feedback']}" if card.get("feedback") else ""), reel=name)
    return verdict


def do_run(films: int, ident: str | None, trigger: str = "owner"):
    """Runs are serialized across all channels (one render at a time on this box);
    a run that arrives while another is going waits its turn."""
    acct = store.current_account()
    S().activity("system", f"Run queued by {trigger}" + (f" for {ident}" if ident else f" ({films} film)"))
    with RUN_LOCK:
        _housekeep()
        cmd = [sys.executable, "cli.py", "run", "--films", str(films), "--account", acct] + (["--id", ident] if ident else [])
        S().activity("system", "Run started")
        env_ = {**os.environ, "ENLISTRA_ACCOUNT": acct}
        with open(WORK / "run.log", "a") as fh:
            fh.write(f"\n===== {acct} {datetime.now(TZ):%Y-%m-%d %H:%M} ({trigger})\n")
            fh.flush()
            r = subprocess.run(cmd, stdout=fh, stderr=subprocess.STDOUT, text=True, env=env_)
        S().activity("system", f"Run finished (exit {r.returncode})")
        return r.returncode


def _housekeep():
    from enlistra import housekeeping
    try:
        freed = housekeeping.clean()
        if sum(freed.values()) > 50:
            S().activity("system", f"Disk cleanup freed {sum(freed.values()):.0f} MB ({', '.join(f'{k} {v:.0f}' for k, v in freed.items())})")
    except Exception as e:  # noqa: BLE001
        log.warning("housekeeping failed: %s", e)


def _schedule_for(acct: str) -> list[str]:
    cfg = pipeline.load_cfg("config.yaml", acct)
    sch = cfg.get("schedule")
    if sch is None and acct == "silent-comedy":
        sch = SCHEDULE
    return [] if not sch or sch == "off" else list(sch)


def do_analyst():
    from enlistra import analyst
    return analyst.run()


# ------------------------------------------------------------------ scheduler
ANALYST_AT = os.environ.get("ANALYST_AT", "03:30")


def _scheduler():
    fired = set()
    log.info("scheduler started (%s)", TZ)
    while True:
        now = datetime.now(TZ)
        hm = now.strftime("%H:%M")
        try:
            accts = [a["id"] for a in store.accounts() if a.get("active", True)]
        except Exception as e:  # noqa: BLE001
            log.warning("accounts lookup failed: %s", e)
            accts = ["silent-comedy"]
        if hm == ANALYST_AT:
            for acct in accts:
                key = (acct, now.date(), "analyst")
                if key not in fired:
                    fired.add(key)
                    store.use(acct)
                    _job("analyst", do_analyst)
                    store.use(None)
        for acct in accts:
            key = (acct, now.date(), hm)
            if key in fired or hm not in _schedule_for(acct):
                continue
            fired.add(key)
            store.use(acct)
            if S().paused():
                S().activity("system", "Scheduled run skipped: the kill switch is on")
            else:
                _job(f"run", do_run, 1, None, "schedule")
            store.use(None)
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
    state = store.current_account() + "." + _s.token_urlsafe(16)
    S().set_setting("youtube_oauth_state", state)
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
    store.use((request.args.get("state") or "").split(".")[0] or None)
    if request.args.get("state") != S().setting("youtube_oauth_state"):
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
    S().set_setting("youtube_token", r)
    name = r["channel"]["title"] if r["channel"] else "(no channel on this Google account yet)"
    S().activity("owner", f"YouTube connected: {name}")
    return Response(status=302, headers={"Location": f"/?a={store.current_account()}"})


@app.get("/health")
def health():
    return jsonify(ok=True, store=S().kind)


@app.get("/media/<path:fname>")
def media(fname):
    if "/" in fname or fname.startswith("."):
        return "", 404
    p = S().media(fname)
    if not p:
        return "", 404
    if request.args.get("dl"):
        return send_file(p.resolve(), as_attachment=True, download_name=fname)
    return send_file(p.resolve(), conditional=True)


@app.post("/api/manual/<name>")
def manual_posted(name):
    plat = (request.json or {}).get("platform", "tiktok")
    r = S().get_reel(name) or {}
    st = dict(r.get("status") or {})
    st[plat] = "posted manually " + time.strftime("%Y-%m-%d")
    S().update_reel(name, status=st)
    S().activity("owner", f"{name}.mp4 posted on {plat} from the phone", reel=name)
    return jsonify(ok=True)


@app.get("/api/state")
def state():
    jobs = S().jobs()
    perf = (S().setting("analyst_summary") or {})
    items = []
    for r in S().reels():
        name = r["name"]
        items.append({**r, "video": f"/media/{name}.mp4", "cover": f"/media/{name}.jpg",
                      "scores": (r.get("critic") or {}).get("scores") or (r.get("judge") or {}).get("scores"),
                      "fix": (r.get("judge") or {}).get("fix"), "created": r.get("created_at"),
                      "job": jobs.get(name), "perf": (perf.get("per_video") or {}).get(name)})
    counts = {}
    for i in items:
        counts[i["stage"]] = counts.get(i["stage"], 0) + 1
    runlog = WORK / "run.log"
    tail = runlog.read_text().splitlines()[-25:] if runlog.exists() else []
    return jsonify({
        "reels": items, "counts": counts, "activity": S().activities(),
        "paused": S().paused(), "schedule": _schedule_for(store.current_account()),
        "account": store.current_account(), "accounts": store.accounts(),
        "analyst": {k: perf.get(k) for k in ("at", "videos", "views", "groups")},
        "pipeline": C().get("pipeline", "film_clips"),
        "footage": _footage_summary() if C().get("pipeline") == "footage" else None,
        "bible": S().setting("bible_refs") if C().get("pipeline") == "character" else None,
        "gen_spend": (S().setting("gen_spend") or {}).get(time.strftime("%Y-%m")),
        "gen_budget": C().get("character", {}).get("monthly_budget_usd", 250), "tz": str(TZ), "store": S().kind,
        "require_approval": bool(C()["post"].get("require_approval", True)),
        "privacy": os.environ.get("TIKTOK_PRIVACY", "SELF_ONLY"),
        "youtube": _yt_status(),
        "jobs": jobs, "running": (jobs.get("run") or {}).get("status") == "running",
        "log": tail, "platform": platform.system(), "brain": S().brains(),
        "config": {"reels_per_film": C()["select"]["reels_per_film"], "target": C().get("critic", {}).get("target_score"),
                   "accept": C().get("critic", {}).get("accept_score"), "platforms": C()["post"]["platforms"]},
    })


def _footage_summary() -> dict:
    lib = S().setting("footage_library") or []
    busy = [k for k, v in (S().jobs() or {}).items() if k.startswith("polish-") and (v or {}).get("status") == "running"]
    return {"clips": len(lib), "minutes": round(sum(e.get("dur", 0) for e in lib) / 60, 1),
            "landscape_minutes": round(sum(e.get("dur", 0) for e in lib if not e.get("vertical")) / 60, 1),
            "unused": sum(1 for e in lib if not e.get("shorts")), "polishing": len(busy),
            "need_minutes": C().get("footage", {}).get("min_library_minutes", 12)}


def _yt_status() -> dict:
    t = S().setting("youtube_token") or {}
    return {"configured": bool(os.environ.get("YT_CLIENT_ID")), "connected": bool(t.get("refresh_token")),
            "channel": (t.get("channel") or {}).get("title"), "privacy": os.environ.get("YT_PRIVACY", "private")}


@app.post("/api/approve/<name>")
def approve(name):
    plats = (request.get_json(silent=True) or {}).get("platforms")
    S().update_reel(name, stage="approved")
    S().activity("owner", f"Approved {name}.mp4; posting" + (f" to {', '.join(plats)}" if plats else ""), reel=name)
    _job(name, do_publish, name, plats)
    return jsonify(ok=True)


@app.post("/api/reject/<name>")
def reject(name):
    note = (request.json or {}).get("note", "")
    S().update_reel(name, stage="rejected", owner_note=note)
    S().activity("owner", f"Rejected {name}.mp4" + (f": {note}" if note else ""), reel=name)
    return jsonify(ok=True)


@app.post("/api/restore/<name>")
def restore(name):
    S().update_reel(name, stage="pending")
    return jsonify(ok=True)


@app.post("/api/rerender/<name>")
def rerender(name):
    d = request.json or {}
    S().activity("owner", f"Edited {name}.mp4; re-rendering", reel=name)
    _job(name, do_rerender, name, d.get("hook", ""), d.get("narration", ""))
    return jsonify(ok=True)


@app.post("/api/run")
def run_now():
    if (S().job("run") or {}).get("status") == "running":
        return jsonify(ok=False, error="a run is already queued or running for this channel"), 409
    d = request.json or {}
    _job("run", do_run, int(d.get("films", 1)), d.get("id") or None)
    return jsonify(ok=True)


@app.post("/api/bible/draft")
def bible_draft():
    from enlistra import character
    _job("bible", character.build_bible, C().get("character", {}))
    return jsonify(ok=True)


@app.post("/api/bible/approve")
def bible_approve():
    b = S().setting("bible_refs") or {}
    if not b.get("views"):
        return jsonify(ok=False, error="no character sheet drafted yet"), 400
    b["approved"] = bool((request.json or {}).get("ok", True))
    S().set_setting("bible_refs", b)
    S().activity("owner", "Character sheet " + ("approved" if b["approved"] else "sent back for a redraft"))
    return jsonify(ok=True)


def do_feature(ident: str):
    acct = store.current_account()
    with RUN_LOCK:
        _housekeep()
        S().activity("system", f"Long-form feature started for {ident}")
        with open(WORK / "run.log", "a") as fh:
            r = subprocess.run([sys.executable, "cli.py", "feature", ident, "--account", acct], stdout=fh,
                               stderr=subprocess.STDOUT, text=True, env={**os.environ, "ENLISTRA_ACCOUNT": acct})
        S().activity("system", f"Long-form feature finished (exit {r.returncode})")
        return r.returncode


@app.post("/api/feature")
def feature():
    ident = ((request.json or {}).get("id") or "").strip()
    if not ident:
        return jsonify(ok=False, error="give an Internet Archive id"), 400
    _job("feature", do_feature, ident)
    return jsonify(ok=True)


# ------------------------------------------------------------------ footage upload (Leaf & Ridge)
UPLOADS: dict = {}


def _safe(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9._-]+", "_", name)[-80:] or "clip.mov"


@app.post("/api/upload/start")
def upload_start():
    from enlistra import footage
    import secrets as _s
    j = request.json or {}
    uid = _s.token_hex(6)
    dest = footage.root() / "raw" / f"{uid}_{_safe(j.get('name') or 'clip.mov')}"
    dest.write_bytes(b"")
    UPLOADS[uid] = {"path": str(dest), "size": int(j.get("size") or 0), "name": j.get("name") or dest.name,
                    "account": store.current_account()}
    return jsonify(ok=True, id=uid)


@app.post("/api/upload/<uid>/chunk")
def upload_chunk(uid):
    u = UPLOADS.get(uid)
    if not u:
        return jsonify(ok=False, error="unknown upload; start again"), 404
    p = Path(u["path"])
    have = p.stat().st_size
    off = int(request.args.get("offset", have))
    if off != have:
        return jsonify(ok=False, have=have), 409
    with open(p, "ab") as fh:
        while True:
            buf = request.stream.read(1 << 20)
            if not buf:
                break
            fh.write(buf)
    return jsonify(ok=True, have=p.stat().st_size)


def do_polish(path: str, name: str):
    from enlistra import footage
    with RUN_LOCK:
        _housekeep()
        try:
            e = footage.polish(Path(path), C(), name)
        finally:
            Path(path).unlink(missing_ok=True)
    return (e or {}).get("id") or "skipped"


@app.post("/api/upload/<uid>/done")
def upload_done(uid):
    u = UPLOADS.pop(uid, None)
    if not u:
        return jsonify(ok=False, error="unknown upload"), 404
    size = Path(u["path"]).stat().st_size
    if u["size"] and size != u["size"]:
        Path(u["path"]).unlink(missing_ok=True)
        return jsonify(ok=False, error=f"upload incomplete ({size} of {u['size']} bytes)"), 400
    S().activity("owner", f"Uploaded footage: {u['name']} ({size / 1e6:.0f} MB); polishing next")
    _job(f"polish-{uid}", do_polish, u["path"], u["name"])
    return jsonify(ok=True)


@app.post("/api/footage/long")
def footage_long():
    from enlistra import footage

    def go():
        with RUN_LOCK:
            return footage.make_long(C())
    _job("long", go)
    return jsonify(ok=True)


@app.get("/api/admin/disk")
def admin_disk():
    from enlistra import housekeeping
    return jsonify(housekeeping.report())


@app.post("/api/admin/clean")
def admin_clean():
    from enlistra import housekeeping
    with RUN_LOCK:
        freed = housekeeping.clean()
    return jsonify(freed=freed, after=housekeeping.report())


@app.post("/api/analyst")
def analyst_now():
    _job("analyst", do_analyst)
    return jsonify(ok=True)


@app.post("/api/pause")
def pause():
    on = bool((request.json or {}).get("on", True))
    S().set_paused(on)
    S().activity("owner", "Kill switch ON: scheduled runs will not start" if on else "Kill switch OFF: scheduled runs resume")
    return jsonify(ok=True, paused=on)


@app.post("/api/brain/<name>")
def brain(name):
    if not name.replace("-", "").replace("_", "").isalnum():
        return jsonify(ok=False), 400
    S().set_brain(name, (request.json or {}).get("text", ""))
    S().activity("owner", f"Brand brain updated: {name}.md")
    return jsonify(ok=True)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8765"))
    host = os.environ.get("HOST", "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1")
    if os.environ.get("SCHEDULE", "").lower() != "off":
        threading.Thread(target=_scheduler, daemon=True).start()
    print(f"Enlistra Reels dashboard: http://{host}:{port}  (store: {S().kind})")
    app.run(host=host, port=port, debug=False, threaded=True)
