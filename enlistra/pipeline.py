"""Orchestrator: source -> analyze -> select -> edit -> critic -> (regenerate) -> post.

Per film:
  1. heuristic candidates (wide windows) and contact sheets
  2. vision picks N clips (or picks file, or text ranker fallback)
  3. each clip is rendered and reviewed by the critic
  4. clips that fail go back to the vision step with the critic's feedback,
     up to `select.max_rounds` rounds; survivors are kept, failures flagged
  5. manifests carry the scorecard; post.publish() refuses flagged reels
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path

import yaml

from . import analyze, critic, edit, post, select, source, store, vision

log = logging.getLogger("pipeline")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_cfg(path: str = "config.yaml", account: str | None = None) -> dict:
    """Base config merged with accounts/<account>.yaml (the channel's overrides)."""
    cfg = yaml.safe_load(Path(path).read_text())
    account = account or store.current_account()
    over = Path("accounts") / f"{account}.yaml"
    if over.exists():
        cfg = _merge(cfg, yaml.safe_load(over.read_text()) or {})
    cfg["account"] = account
    return cfg


def paused(cfg: dict) -> bool:
    return store.get(cfg["paths"]["work"]).paused()


def activity(cfg: dict, kind: str, text: str, **extra):
    store.get(cfg["paths"]["work"]).activity(kind, text, **extra)


def _record(cfg: dict, entry: dict):
    store.get(cfg["paths"]["work"]).record_film(entry)


def _owned_chaplin(film, info: dict) -> bool:
    """Chaplin's films from 1918 on (A Dog's Life onward) belong to Roy Export SAS."""
    text = f"{film.title} {info.get('title') or ''} {getattr(film, 'creator', '') or ''}".lower()
    year = info.get("year") or film.year
    try:
        return "chaplin" in text and year is not None and int(year) >= 1918
    except (TypeError, ValueError):
        return False


def _style_seed(cfg: dict) -> int:
    try:
        return len(store.get(cfg["paths"]["work"]).done_ids())
    except Exception:  # noqa: BLE001
        return 0


def _overlaps(c, kept) -> bool:
    return any(not (c.end <= k.start or c.start >= k.end) for k in kept)


def process_film(film, cfg: dict, work: Path, post_now: bool) -> list[Path]:
    scfg, ecfg, ccfg = cfg["select"], cfg["edit"], cfg.get("critic", {})
    ccfg = {**ccfg, "narration_start": ecfg.get("narration_start", 0.4),
            "anthropic_model": scfg.get("anthropic_model", "claude-sonnet-5-5")}
    activity(cfg, "source", f"Picked up {film.title}" + (f" ({film.year})" if film.year else ""), film=film.identifier)
    video = source.download(film, work / "src")
    a = analyze.analyze(video, work / "analysis" / f"{film.identifier}.json")
    cands = select.heuristic_candidates(a, scfg)
    sheets = work / "sheets" / film.identifier
    want = scfg["reels_per_film"]
    film_info = {"title": film.title, "year": film.year}

    picks_file = work / "picks" / f"{film.identifier}.json"
    hand = vision.load_picks_file(picks_file, cands)

    kept: list[tuple] = []       # (clip, out_path, card)
    flagged: list[tuple] = []
    feedback = None
    rounds = scfg.get("max_rounds", 3) if hand is None else 1
    for rnd in range(rounds):
        need = want - len(kept)
        if need <= 0:
            break
        if hand is not None:
            clips = hand
        else:
            res = None
            if scfg.get("use_vision", True):
                try:
                    res = vision.rank(video, film.title, film.year, cands, need, scfg, sheets,
                                      feedback=feedback)
                except Exception as e:  # noqa: BLE001
                    log.warning("vision ranking failed (%s)", e)
            if res:
                clips = res["clips"]
                if res.get("year") and not film_info.get("year"):
                    film_info["year"] = res["year"]
                if res.get("film_title") and len(res["film_title"]) > 3:
                    film_info["title"] = res["film_title"]
            else:
                log.info("using text ranker")
                clips = select.llm_rank(film.title, film.year, cands, need, scfg)
        if rnd == 0 and cfg["source"].get("require_identified") and not film_info.get("year") and hand is None:
            log.info("skipping %s: no year and the model could not identify the film (likely a compilation)", film.identifier)
            _record(cfg, {"identifier": film.identifier, "title": film.title, "year": None, "clips": [], "skipped": "unidentified"})
            return []
        if rnd == 0 and hand is None and _owned_chaplin(film, film_info):
            log.info("skipping %s: Chaplin film from 1918 or later (Roy Export owns these)", film.identifier)
            _record(cfg, {"identifier": film.identifier, "title": film_info["title"], "year": film_info.get("year"),
                          "clips": [], "skipped": "rights: Chaplin 1918+"})
            return []
        clips = [c for c in clips if not _overlaps(c, [k[0] for k in kept])]
        fb = []
        styles = ecfg.get("styles") or [ecfg.get("style", "cold")]
        for clip in clips:
            idx = len(kept) + len(flagged) + 1
            # rotate formats across reels so the metrics can tell which one wins
            clip.style = styles[(_style_seed(cfg) + idx) % len(styles)]
            out = work / "out" / f"{film.identifier}_{idx:02d}.mp4"
            out.parent.mkdir(parents=True, exist_ok=True)
            critic.prune_sfx(clip, a)
            try:
                edit.render(video, clip, out, ecfg, film=film_info, analysis=a)
            except Exception as e:  # noqa: BLE001
                log.error("render failed: %s", e)
                fb.append(f"render failed for {clip.start:.0f}-{clip.end:.0f}s: {e}")
                continue
            card = critic.review(out, clip, film_info, a, getattr(clip, "narr_dur", 0.0),
                                 float(ecfg.get("speed", 1.0)), ccfg)
            sc = (card.get("judge") or {}).get("scores", {}).get("overall")
            activity(cfg, "critic", f"{out.name}: {card['verdict']}" + (f", {sc}/5" if sc else "") +
                     (f". {card['feedback']}" if card["feedback"] else ""), reel=out.name)
            if card["verdict"] == "pass":
                kept.append((clip, out, card))
            else:
                flagged.append((clip, out, card))
                fb.append(f"pick at {clip.start:.0f}-{clip.end:.0f}s ('{clip.hook}'): {card['feedback']}")
        if len(kept) >= want or hand is not None:
            break
        feedback = "\n".join(fb) if fb else None
        log.info("round %d: %d kept, %d flagged; regenerating", rnd + 1, len(kept), len(flagged))

    # after the rounds: promote flagged reels that still clear the accept score
    flagged.sort(key=lambda t: -(t[2].get("judge") or {}).get("scores", {}).get("overall", 0))
    accept = ccfg.get("accept_score", 3.0)
    while flagged and len(kept) < want:
        clip, out, card = flagged[0]
        sc = (card.get("judge") or {}).get("scores", {}).get("overall", 0)
        if card["gates"] or sc < accept:
            break
        kept.append(flagged.pop(0))
        log.info("accepting %s at %.1f/5 after %d rounds", out.name, sc, rounds)
    outputs = []
    for clip, out, card in kept + flagged:
        status = "pass" if (clip, out, card) in kept else "flagged"
        mpath = post.write_manifest(out, {**asdict(film), **{"title": film_info["title"], "year": film_info["year"]}},
                                    asdict(clip) | {"narration": clip.narration, "payoff": clip.payoff,
                                                    "sfx": clip.sfx, "segments": clip.segs(),
                                                    "title": clip.title, "style": clip.style}, cfg["post"])
        m = json.loads(mpath.read_text())
        m["critic"] = {"verdict": status, "scores": (card.get("judge") or {}).get("scores"),
                       "feedback": card.get("feedback")}
        st = store.get(cfg["paths"]["work"])
        if status == "pass" and cfg["post"].get("require_approval", True):
            m["approval"] = "pending"
            st.save_reel(out.stem, m, card)
            activity(cfg, "queue", f"{out.name} is ready for your approval: '{clip.hook}'", reel=out.stem)
        elif post_now and status == "pass":
            st.save_reel(out.stem, m, card)
            res = post.publish(mpath, cfg["post"])
            log.info("%s -> %s", out.name, res)
            activity(cfg, "post", f"{out.name} posted: {res}", reel=out.stem)
        else:
            st.save_reel(out.stem, m, card)
        outputs.append(out)
    _record(cfg, {"identifier": film.identifier, "title": film_info["title"], "year": film_info["year"],
                  "clips": [asdict(c) | {"verdict": "pass"} for c, _, _ in kept] +
                           [asdict(c) | {"verdict": "flagged"} for c, _, _ in flagged]})
    return outputs


def run(cfg: dict, films: int = 1, identifier: str | None = None, post_now: bool = True) -> list[Path]:
    work = Path(cfg["paths"]["work"])
    kind = cfg.get("pipeline", "film_clips")
    if kind != "film_clips":
        log.info("%s uses the %s pipeline, which runs from its own module", cfg.get("account"), kind)
        if kind == "character":
            from . import character
            return character.run(cfg, post_now=post_now)
        return []
    if paused(cfg):
        log.info("paused (kill switch); not running")
        activity(cfg, "system", "Run skipped: the kill switch is on")
        return []
    done = store.get(cfg["paths"]["work"]).done_ids()
    if identifier:
        f = source.resolve(identifier, cfg["source"])
        if not f:
            raise SystemExit(f"{identifier} is not free to use or has no usable file")
        films_list = [f]
    else:
        films_list = source.find_films(cfg["source"], done, limit=films)
    if not films_list:
        log.info("no new free-to-use films found")
        return []
    outputs: list[Path] = []
    for film in films_list:
        outputs += process_film(film, cfg, work, post_now)
    return outputs
