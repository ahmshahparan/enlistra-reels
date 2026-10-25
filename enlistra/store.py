"""Persistence layer with two backends behind one interface.

  FileStore      everything under work/ (the original Mac setup; zero dependencies)
  SupabaseStore  Postgres tables + the private `reels` storage bucket, when
                 SUPABASE_URL and SUPABASE_SECRET_KEY are set. Rendering still
                 happens on local disk; finished reels are uploaded and the row
                 in `reels` is the source of truth. Every row carries account_id,
                 so one deployment can run many niche accounts.

Use `store.get()`; it picks the backend from the environment.
"""
from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

import requests

log = logging.getLogger("store")
ACCOUNT = os.environ.get("ENLISTRA_ACCOUNT", "silent-comedy")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ====================================================================== files
class FileStore:
    kind = "file"

    def __init__(self, work: Path, account: str = "silent-comedy"):
        self.account = account
        self.brain_dir = Path("brain") if account == "silent-comedy" else Path("brain") / account
        self.work = Path(work)
        self.out = self.work / "out"
        self.out.mkdir(parents=True, exist_ok=True)

    # films
    def done_ids(self) -> set[str]:
        p = self.work / "state.jsonl"
        if not p.exists():
            return set()
        return {json.loads(l)["identifier"] for l in p.read_text().splitlines() if l.strip()}

    def record_film(self, entry: dict):
        with (self.work / "state.jsonl").open("a") as fh:
            fh.write(json.dumps(entry) + "\n")

    # activity
    def activity(self, kind: str, text: str, **extra):
        with (self.work / "activity.jsonl").open("a") as fh:
            fh.write(json.dumps({"t": _now(), "kind": kind, "text": text, **extra}) + "\n")

    def activities(self, n: int = 80) -> list[dict]:
        p = self.work / "activity.jsonl"
        if not p.exists():
            return []
        rows = []
        for line in p.read_text().splitlines()[-n:]:
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
        return list(reversed(rows))

    # reels (manifest json next to the mp4 is the record)
    def save_reel(self, name: str, manifest: dict, card: dict | None = None):
        (self.out / f"{name}.json").write_text(json.dumps(manifest, indent=2))

    def get_reel(self, name: str) -> dict | None:
        p = self.out / f"{name}.json"
        if not p.exists():
            return None
        m = json.loads(p.read_text())
        cp = self.out / f"{name}.critic.json"
        card = json.loads(cp.read_text()) if cp.exists() else {}
        return _row_from_manifest(name, m, card)

    def reels(self) -> list[dict]:
        rows = []
        for p in sorted(self.out.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
            if p.name.endswith(".critic.json"):
                continue
            r = self.get_reel(p.stem)
            if r:
                rows.append(r)
        return rows

    def update_reel(self, name: str, **fields):
        p = self.out / f"{name}.json"
        m = json.loads(p.read_text())
        if "stage" in fields:
            m["approval"] = {"approved": "approved", "rejected": "rejected", "pending": "pending"}.get(fields["stage"], m.get("approval"))
        for k in ("hook", "narration", "status", "critic", "owner_note"):
            if k in fields:
                m[k] = fields[k]
        p.write_text(json.dumps(m, indent=2))

    def local_files(self, name: str) -> tuple[Path, Path]:
        """Make sure the reel's mp4 and manifest are on local disk; return their paths."""
        return self.out / f"{name}.mp4", self.out / f"{name}.json"

    def media(self, fname: str) -> Path | None:
        p = self.out / fname
        return p if p.exists() else None

    # settings / brain / jobs / pause
    def setting(self, key: str, default=None):
        p = self.work / f"{key}.json"
        return json.loads(p.read_text()) if p.exists() else default

    def set_setting(self, key: str, value):
        (self.work / f"{key}.json").write_text(json.dumps(value, indent=2))

    def brain(self, name: str = "voice") -> str:
        p = self.brain_dir / f"{name}.md"
        return p.read_text().strip() if p.exists() else ""

    def brains(self) -> dict:
        d = self.brain_dir
        return {p.stem: p.read_text() for p in sorted(d.glob("*.md"))} if d.exists() else {}

    def set_brain(self, name: str, text: str):
        self.brain_dir.mkdir(parents=True, exist_ok=True)
        (self.brain_dir / f"{name}.md").write_text(text)

    def paused(self) -> bool:
        return (self.work / "PAUSED").exists()

    def set_paused(self, on: bool):
        f = self.work / "PAUSED"
        if on:
            f.touch()
        elif f.exists():
            f.unlink()

    _jobs: dict = {}

    def job(self, name: str) -> dict | None:
        return self._jobs.get(name)

    def set_job(self, name: str, status: str, text: str = ""):
        self._jobs[name] = {"status": status, "text": text, "t": _now()}

    def jobs(self) -> dict:
        return dict(self._jobs)


def _row_from_manifest(name: str, m: dict, card: dict) -> dict:
    crit = m.get("critic") or {}
    verdict = crit.get("verdict", "unknown")
    approval = m.get("approval")
    st = m.get("status") or {}
    posted = any(str(v).strip() and not str(v).startswith(("pending", "skipped", "error", "draft"))
                 for k, v in st.items() if k != "typefully")
    if posted:
        stage = "posted"
    elif approval == "rejected":
        stage = "rejected"
    elif approval == "approved":
        stage = "posting"
    elif verdict == "flagged":
        stage = "flagged"
    elif verdict == "pass":
        stage = "pending"
    else:
        stage = "unknown"
    return {"name": name, "hook": m.get("hook"), "title": m.get("title"), "style": m.get("style"),
            "narration": m.get("narration"), "gag": m.get("gag"),
            "caption": m.get("caption"), "source": m.get("source"), "created_at": m.get("created"),
            "stage": stage, "critic": crit, "gates": card.get("gates") or [], "judge": card.get("judge"),
            "status": st, "owner_note": m.get("owner_note"),
            "video_path": f"{name}.mp4", "cover_path": f"{name}.jpg",
            "sheet_path": f"{name}.critic.jpg", "film_identifier": (m.get("source") or {}).get("identifier")}


# =================================================================== supabase
class SupabaseStore(FileStore):
    """Supabase backend. Local work/ is still the scratch area for rendering."""
    kind = "supabase"
    BUCKET = "reels"

    def __init__(self, work: Path, url: str, key: str, account: str = ACCOUNT):
        super().__init__(work, account)
        self.url = url.rstrip("/")
        self.key = key
        self.h = {"apikey": key, "Authorization": f"Bearer {key}"}
        self.cache = self.work / "cache"
        self.cache.mkdir(parents=True, exist_ok=True)

    # -- low level
    def _rest(self, method: str, table: str, params: dict | None = None, body=None, prefer: str | None = None):
        h = {**self.h, "Content-Type": "application/json"}
        if prefer:
            h["Prefer"] = prefer
        r = requests.request(method, f"{self.url}/rest/v1/{table}", headers=h, params=params,
                             data=json.dumps(body) if body is not None else None, timeout=30)
        if r.status_code >= 300:
            raise RuntimeError(f"supabase {method} {table}: {r.status_code} {r.text[:300]}")
        return r.json() if r.content and "json" in r.headers.get("content-type", "") else None

    def _upload(self, path: Path, obj: str, ctype: str):
        with open(path, "rb") as fh:
            r = requests.post(f"{self.url}/storage/v1/object/{self.BUCKET}/{obj}", headers={
                **self.h, "Content-Type": ctype, "x-upsert": "true"}, data=fh, timeout=300)
        if r.status_code >= 300:
            raise RuntimeError(f"storage upload {obj}: {r.status_code} {r.text[:200]}")

    def _download(self, obj: str, dest: Path) -> Path | None:
        r = requests.get(f"{self.url}/storage/v1/object/{self.BUCKET}/{obj}", headers=self.h, timeout=300, stream=True)
        if r.status_code != 200:
            return None
        dest.parent.mkdir(parents=True, exist_ok=True)
        with open(dest, "wb") as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
        return dest

    def _obj(self, fname: str) -> str:
        return f"{self.account}/{fname}"

    # -- films
    def done_ids(self) -> set[str]:
        rows = self._rest("GET", "films", {"account_id": f"eq.{self.account}", "select": "identifier"})
        return {r["identifier"] for r in rows}

    def record_film(self, entry: dict):
        super().record_film(entry)
        self._rest("POST", "films", body={"account_id": self.account, "identifier": entry["identifier"],
                                           "title": entry.get("title"), "year": entry.get("year"),
                                           "skipped": entry.get("skipped"), "clips": entry.get("clips")},
                   prefer="resolution=merge-duplicates")

    # -- activity
    def activity(self, kind: str, text: str, **extra):
        super().activity(kind, text, **extra)
        try:
            self._rest("POST", "activity", body={"account_id": self.account, "kind": kind, "text": text,
                                                  "reel": extra.get("reel"), "film": extra.get("film")})
        except Exception as e:  # noqa: BLE001
            log.warning("activity not stored: %s", e)

    def activities(self, n: int = 80) -> list[dict]:
        rows = self._rest("GET", "activity", {"account_id": f"eq.{self.account}", "order": "t.desc", "limit": n})
        return [{**r, "t": r["t"][:19]} for r in rows]

    # -- reels
    def save_reel(self, name: str, manifest: dict, card: dict | None = None):
        super().save_reel(name, manifest, card)
        row = _row_from_manifest(name, manifest, card or {})
        for fname, ctype in ((f"{name}.mp4", "video/mp4"), (f"{name}.jpg", "image/jpeg"), (f"{name}.critic.jpg", "image/jpeg")):
            p = self.out / fname
            if p.exists():
                self._upload(p, self._obj(fname), ctype)
        body = {"account_id": self.account, "name": name, "film_identifier": row["film_identifier"],
                "hook": row["hook"], "title": row["title"], "style": row["style"],
                "narration": row["narration"], "gag": row["gag"], "caption": row["caption"],
                "source": row["source"], "critic": row["critic"], "gates": row["gates"], "judge": row["judge"],
                "stage": row["stage"], "status": row["status"], "video_path": self._obj(f"{name}.mp4"),
                "cover_path": self._obj(f"{name}.jpg"), "sheet_path": self._obj(f"{name}.critic.jpg"),
                "updated_at": _now()}
        self._rest("POST", "reels", body=body, prefer="resolution=merge-duplicates")

    def get_reel(self, name: str) -> dict | None:
        rows = self._rest("GET", "reels", {"account_id": f"eq.{self.account}", "name": f"eq.{name}"})
        return rows[0] if rows else None

    def reels(self) -> list[dict]:
        return self._rest("GET", "reels", {"account_id": f"eq.{self.account}", "order": "created_at.desc", "limit": 300})

    def update_reel(self, name: str, **fields):
        fields["updated_at"] = _now()
        self._rest("PATCH", "reels", {"account_id": f"eq.{self.account}", "name": f"eq.{name}"}, body=fields)
        if (self.out / f"{name}.json").exists():
            try:
                super().update_reel(name, **fields)
            except Exception:  # noqa: BLE001
                pass

    def local_files(self, name: str) -> tuple[Path, Path]:
        """Bring the mp4 and a manifest back to local disk (after a redeploy)."""
        mp4, man = self.out / f"{name}.mp4", self.out / f"{name}.json"
        if not mp4.exists():
            if not self._download(self._obj(f"{name}.mp4"), mp4):
                raise RuntimeError(f"{name}.mp4 is not in storage")
        if not man.exists():
            r = self.get_reel(name)
            m = {"reel": str(mp4), "cover": str(self.out / f"{name}.jpg"), "hook": r["hook"], "caption": r["caption"],
                 "title": r.get("title"), "style": r.get("style"),
                 "narration": r["narration"], "gag": r["gag"], "source": r["source"], "status": r.get("status") or {},
                 "critic": r.get("critic") or {}, "platforms": list((r.get("status") or {}).keys()),
                 "ai_generated": (r.get("style") == "character"),
                 "created": r.get("created_at")}
            man.write_text(json.dumps(m, indent=2))
        return mp4, man

    def media(self, fname: str) -> Path | None:
        p = self.out / fname
        if p.exists():
            return p
        c = self.cache / fname
        if c.exists():
            return c
        return self._download(self._obj(fname), c)

    # -- settings / brain / pause / jobs
    def setting(self, key: str, default=None):
        rows = self._rest("GET", "settings", {"account_id": f"eq.{self.account}", "key": f"eq.{key}", "select": "value"})
        return rows[0]["value"] if rows else default

    def set_setting(self, key: str, value):
        self._rest("POST", "settings", body={"account_id": self.account, "key": key, "value": value, "updated_at": _now()},
                   prefer="resolution=merge-duplicates")

    def brain(self, name: str = "voice") -> str:
        rows = self._rest("GET", "brain", {"account_id": f"eq.{self.account}", "name": f"eq.{name}", "select": "content"})
        if rows:
            return rows[0]["content"].strip()
        txt = super().brain(name)          # seed from the repo's brain/*.md on first use
        if txt:
            self.set_brain(name, txt)
        return txt

    def brains(self) -> dict:
        rows = self._rest("GET", "brain", {"account_id": f"eq.{self.account}", "select": "name,content", "order": "name"})
        d = {r["name"]: r["content"] for r in rows}
        for k, v in super().brains().items():
            if k not in d:
                self.set_brain(k, v)
                d[k] = v
        return d

    def set_brain(self, name: str, text: str):
        self._rest("POST", "brain", body={"account_id": self.account, "name": name, "content": text, "updated_at": _now()},
                   prefer="resolution=merge-duplicates")

    def paused(self) -> bool:
        return bool(self.setting("paused", False))

    def set_paused(self, on: bool):
        self.set_setting("paused", bool(on))

    def job(self, name: str) -> dict | None:
        rows = self._rest("GET", "jobs", {"account_id": f"eq.{self.account}", "name": f"eq.{name}"})
        return rows[0] if rows else None

    def set_job(self, name: str, status: str, text: str = ""):
        body = {"account_id": self.account, "name": name, "status": status, "text": text[:400]}
        if status == "running":
            body["started_at"], body["ended_at"] = _now(), None
        else:
            body["ended_at"] = _now()
        self._rest("POST", "jobs", body=body, prefer="resolution=merge-duplicates")

    def jobs(self) -> dict:
        rows = self._rest("GET", "jobs", {"account_id": f"eq.{self.account}"})
        return {r["name"]: r for r in rows}


# ===================================================================== factory
# One store per account (channel). The current account comes from, in order:
# an explicit argument, the calling thread (store.use), or ENLISTRA_ACCOUNT.
import threading as _threading

_STORES: dict = {}
_LOCAL = _threading.local()


def current_account() -> str:
    return getattr(_LOCAL, "account", None) or os.environ.get("ENLISTRA_ACCOUNT", "silent-comedy")


def use(account: str | None):
    """Bind this thread to an account (None = back to the default)."""
    _LOCAL.account = account


def get(work: str | Path = "work", account: str | None = None):
    account = account or current_account()
    if account not in _STORES:
        url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY")
        if url and key:
            _STORES[account] = SupabaseStore(Path(work), url, key, account)
            log.info("store: supabase (%s)", account)
        else:
            _STORES[account] = FileStore(Path(work) if account == "silent-comedy" else Path(work) / account, account)
            log.info("store: files under %s", _STORES[account].work)
    return _STORES[account]


def accounts() -> list[dict]:
    """All channels. Supabase: the accounts table; files: accounts/*.yaml."""
    url, key = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SECRET_KEY")
    if url and key:
        r = requests.get(f"{url.rstrip('/')}/rest/v1/accounts", params={"select": "*", "order": "created_at"},
                         headers={"apikey": key, "Authorization": f"Bearer {key}"}, timeout=30)
        if r.status_code < 300:
            return r.json()
    return [{"id": p.stem, "name": p.stem} for p in sorted(Path("accounts").glob("*.yaml"))] or \
           [{"id": "silent-comedy", "name": "Enlistra Reels"}]
