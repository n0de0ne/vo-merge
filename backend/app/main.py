"""FastAPI app: REST API + serves the built React SPA."""
import os, subprocess, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import core, scheduler, pipeline
from .clients import Prowlarr, Radarr, QBittorrent, Plex, Sonarr

app = FastAPI(title="VO Merger")
STATIC = os.environ.get("VO_STATIC", "/app/static")
PREVIEW_DIR = os.path.join(core.CONFIG_DIR, "preview")


@app.on_event("startup")
def _startup():
    core.init_db()
    core.init_tv()
    core.init_probe_cache()
    scheduler.start()


# ----- API -----
api = FastAPI()


@api.get("/status")
def status():
    cfg = core.load_config()
    return {"enabled": cfg["enabled"], "grab_mode": cfg["grab_mode"],
            "counts": core.status_counts(), "states": core.STATES}


@api.get("/movies")
def movies(status: str | None = None):
    return core.get_movies(status)


@api.get("/settings")
def get_settings():
    cfg = core.load_config()
    cfg["qb_pass"] = "********" if cfg["qb_pass"] else ""   # never echo secret
    cfg["prowlarr_key"] = bool(cfg["prowlarr_key"])
    cfg["radarr_key"] = bool(cfg["radarr_key"])
    cfg["sonarr_key"] = bool(cfg.get("sonarr_key"))
    cfg["plex_token"] = bool(cfg["plex_token"])
    return cfg


class SettingsIn(BaseModel):
    data: dict


@api.post("/settings")
def post_settings(body: SettingsIn):
    # drop masked/unchanged secret placeholders
    d = {k: v for k, v in body.data.items()
         if not (k in ("qb_pass",) and v == "********")
         and not (k in ("prowlarr_key", "radarr_key", "sonarr_key", "plex_token") and v in (True, False))}
    cfg = core.save_config(d)
    scheduler.reschedule()
    return {"ok": True}


@api.post("/test/{which}")
def test(which: str):
    cfg = core.load_config()
    try:
        if which == "prowlarr":
            Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"]).ping()
        elif which == "radarr":
            Radarr(cfg["radarr_url"], cfg["radarr_key"]).ping()
        elif which == "sonarr":
            Sonarr(cfg["sonarr_url"], cfg["sonarr_key"]).ping()
        elif which == "qb":
            QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]).ping()
        elif which == "plex":
            Plex(cfg["plex_url"], cfg["plex_token"]).ping()
        else:
            raise HTTPException(404, "unknown service")
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@api.post("/scan")
def do_scan():
    return {"found": pipeline.scan()}


@api.post("/rescan")
def do_rescan(forget: bool = False, scope: str = "all"):
    """Re-decide the gaps from the FILES, one library at a time, in the background — a full
    probe takes minutes on the first pass.

    `scope` picks the library: "films" (Radarr, including anime films), "anime" (Sonarr series
    Sonarr flags as anime, or that live in an anime_dirs folder / are Japanese-original),
    "series" (everything else in Sonarr), or "all".

    Deliberately ignores `scope_films`/`scope_series` and `series_pilot`. Those exist to control
    what the pipeline *acts* on; a rescan only reads files and records what's missing, and an
    operator asking to re-read a library means that whole library, not the slice currently
    enabled. Records for a disabled scope simply sit as inventory until it's turned on.

    `forget=true` drops the probe cache for that library first, so every file is re-read even
    when its size and mtime are unchanged (use after fixing track tags by hand)."""
    import threading
    from . import tv

    if scope not in ("all", "films", "anime", "series"):
        raise HTTPException(422, "scope must be all | films | anime | series")
    if not pipeline.SCAN_LOCK.acquire(blocking=False):
        return {"ok": True, "started": False, "note": "a scan is already running",
                "state": pipeline.SCAN_STATE}
    if forget:
        with core.db() as c:
            c.execute("DELETE FROM probes")
        core.log(f"rescan({scope}): probe cache cleared — every file will be re-read")

    def _run():
        st = pipeline.SCAN_STATE
        st.update(running=True, scope=scope, started=time.time(), finished=0, phase="starting",
                  films=None, episodes=None, error=None)
        try:
            # whatever the scopes say; pilot cleared so a pilot list can't shrink a rescan
            cfg = dict(core.load_config(), series_pilot=[])
            if scope in ("all", "films"):
                st["phase"] = "films"
                st["films"] = pipeline.scan(cfg)
            if scope in ("all", "anime", "series"):
                st["phase"] = "anime" if scope == "anime" else ("series" if scope == "series" else "series & anime")
                kinds = None if scope == "all" else (scope,)
                st["episodes"] = tv.scan(cfg, kinds=kinds)
            st["phase"] = "done"
            core.log(f"rescan({scope}): {st['films']} film gap(s), {st['episodes']} episode gap(s)")
        except Exception as e:
            st["error"] = str(e)
            st["phase"] = "error"
            core.log(f"rescan({scope}) error: {e}")
        finally:
            st.update(running=False, finished=time.time())
            pipeline.SCAN_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True, "scope": scope, "probes": core.probe_stats()}


@api.get("/rescan")
def rescan_state():
    """Progress of a running (or the last) rescan — it takes minutes, so the UI can say so."""
    return {**pipeline.SCAN_STATE, "probes": core.probe_stats()}


@api.post("/finish")
def do_finish():
    """Run the finish stage (poll qB, merge completed downloads) in-process so it shares the
    merge lock with the scheduler — never spawn merges in a separate process."""
    import threading
    from . import tv

    if not pipeline.FINISH_LOCK.acquire(blocking=False):
        return {"ok": True, "started": False, "note": "a finish cycle is already running"}

    def _run():
        try:
            cfg = core.load_config()
            pipeline.stage_finish(cfg)
            tv.stage_finish(cfg)
        except Exception as e:
            core.log(f"manual finish error: {e}")
        finally:
            pipeline.FINISH_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True}


@api.post("/search_all")
def search_all():
    """Run the search stage NOW over every pending record, instead of waiting for the search
    timer. Honours the in-flight download cap, so it grabs at most the number of free slots —
    the rest of the backlog stays pending for the next run."""
    import threading
    from . import tv

    if not pipeline.SEARCH_LOCK.acquire(blocking=False):
        return {"ok": True, "started": False, "note": "a search run is already in progress"}

    pending = len(core.get_movies("pending")) + len(core.get_episodes("pending"))
    try:
        slots = pipeline.grab_budget(core.load_config())
    except Exception:
        slots = None

    def _run():
        try:
            cfg = core.load_config()
            if cfg.get("scope_films", True):
                pipeline.stage_search(cfg)
            if cfg.get("scope_series"):
                tv.stage_search(cfg)
        except Exception as e:
            core.log(f"manual search error: {e}")
        finally:
            pipeline.SEARCH_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    core.log(f"manual search: {pending} pending, {slots if slots is not None else '?'} free slot(s)")
    return {"ok": True, "started": True, "pending": pending, "slots": slots}


@api.post("/movie/{tmdb_id}/search")
def do_search(tmdb_id: int):
    pipeline.search_movie(tmdb_id); return core.get_movie(tmdb_id)


@api.post("/movie/{tmdb_id}/merge")
def do_merge(tmdb_id: int):
    pipeline.merge_movie(tmdb_id); return core.get_movie(tmdb_id)


@api.get("/movie/{tmdb_id}/candidates")
def movie_candidates(tmdb_id: int):
    return pipeline.candidates(tmdb_id, include_tried=True)


class GrabIn(BaseModel):
    link: str
    rid: str | None = None
    title: str | None = None


@api.post("/movie/{tmdb_id}/grab")
def movie_grab(tmdb_id: int, body: GrabIn):
    pipeline.grab_release(tmdb_id, body.link, body.rid, body.title)
    return core.get_movie(tmdb_id)


class SyncIn(BaseModel):
    offset_ms: int


@api.post("/movie/{tmdb_id}/sync")
def set_sync(tmdb_id: int, body: SyncIn):
    mv = core.get_movie(tmdb_id)
    if mv and mv.get("status") == "merged" and mv.get("merged_file"):
        # already merged -> shift the English track in place (offset 0 = auto-detect)
        pipeline.resync_movie(tmdb_id, offset_ms=body.offset_ms or None)
    else:
        # not merged (sync_fail/error) -> re-attempt the merge; manual offset if given,
        # otherwise the video scene-cut matcher tries to align it.
        core.set_status(tmdb_id, mv["status"] if mv else "pending",
                        sync_offset_ms=(body.offset_ms or 0), error=None)
        pipeline.merge_movie(tmdb_id)
    return core.get_movie(tmdb_id)


@api.post("/movie/{tmdb_id}/retry")
def retry(tmdb_id: int):
    pipeline.retry_movie(tmdb_id); return {"ok": True}


@api.post("/retry_errors")
def retry_all_errors():
    """Retry every failed record — movies and episodes, error and sync_fail. Each one blocklists
    the release that failed and drops its donor first, so a bulk retry re-searches instead of
    re-grabbing the same broken release."""
    from . import tv
    cfg = core.load_config()
    return {"ok": True, "movies": pipeline.retry_errors(cfg), "episodes": tv.retry_errors(cfg)}


@api.post("/movie/{tmdb_id}/ignore")
def ignore(tmdb_id: int):
    core.set_status(tmdb_id, "ignored"); return {"ok": True}


@api.post("/movie/{tmdb_id}/unignore")
def unignore(tmdb_id: int):
    # fresh start: clear the tried/attempts blocklist so it can grab anything again
    core.set_status(tmdb_id, "pending", error=None, tried="[]", attempts=0,
                    dl_hash=None, dl_id=None, en_file=None)
    return {"ok": True}


@api.post("/movie/{tmdb_id}/research")
def research(tmdb_id: int):
    # search again now (keeps the tried-blocklist so it won't re-pick known-bad releases)
    core.set_status(tmdb_id, "pending", error=None, dl_hash=None, dl_id=None, en_file=None)
    pipeline.search_movie(tmdb_id)
    return core.get_movie(tmdb_id)


@api.post("/movie/{tmdb_id}/another")
def another(tmdb_id: int):
    """Pick another version: blocklist the current release, drop its download, grab the
    next-best candidate."""
    import json as _json
    cfg = core.load_config()
    mv = core.get_movie(tmdb_id) or {}
    tried = _json.loads(mv.get("tried") or "[]")
    if mv.get("dl_id") and mv["dl_id"] not in tried:
        tried.append(mv["dl_id"])
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        if mv.get("dl_hash"):
            qb.delete([mv["dl_hash"]], delete_files=True)
    except Exception:
        pass
    core.set_status(tmdb_id, "pending", error=None, tried=_json.dumps(tried),
                    dl_hash=None, dl_id=None, en_file=None)
    pipeline.search_movie(tmdb_id)
    return core.get_movie(tmdb_id)


@api.post("/movie/{tmdb_id}/ai")
def movie_to_ai(tmdb_id: int):
    """Operator pressed 'Send to AI' on a Review item: file a ticket for the host's
    AI dispatcher with the full record + how to act on it, so the on-call agent can
    manage the item end-to-end (resync, pick another release, or report back)."""
    mv = core.get_movie(tmdb_id)
    if not mv:
        raise HTTPException(404, "unknown movie")
    record = {k: mv.get(k) for k in
              ("tmdb_id", "title", "original_title", "year", "original_lang", "status",
               "error", "sync_delta", "sync_offset_ms", "candidate_title",
               "candidate_score", "candidate_seeders", "attempts", "tried",
               "french_path", "en_file", "merged_file", "quality", "dl_hash")}
    summary = f"operator escalated from Review: {mv['title']} ({mv['year']}) — {mv['status']}"
    if mv.get("error"):
        summary += f": {mv['error']}"
    queued = core.ticket(
        f"review-m{tmdb_id}", summary,
        {"record": record,
         "api": "http://10.0.1.5:8090/api (host) / http://localhost:8080/api (in-container)",
         "actions": [
             "GET  /movie/{id}/candidates — list releases (incl. already-tried)",
             "POST /movie/{id}/sync {\"offset_ms\":0} — re-run auto sync-detect + merge",
             "POST /movie/{id}/another — blocklist current release, grab next best",
             "POST /movie/{id}/research — search again (keeps blocklist)",
             "POST /movie/{id}/ignore — give up on this title",
         ],
         "report_back": (
             f"When done, POST /movie/{tmdb_id}/ai_result with "
             "{\"status\":\"resolved|failed|needs_human\",\"verdict\":\"one line\","
             "\"action_taken\":\"what you did\"} so this leaves the operator's manual-review queue "
             "(needs_human = a person must decide)."),
         "docs": "/mnt/nvme/AIWorkspace/vo-merge/dev/vo-merge/CLAUDE.md"},
        force=True)
    if queued:
        core.set_status(tmdb_id, mv["status"], ai_status="pending", ai_at=time.time())
    return {"ok": True, "queued": bool(queued)}


@api.post("/episode/{ep_id}/ai")
def episode_to_ai(ep_id: str):
    """TV mirror of movie_to_ai: escalate one episode to the host AI dispatcher."""
    e = core.get_episode(ep_id)
    if not e:
        raise HTTPException(404, "unknown episode")
    record = {k: e.get(k) for k in
              ("id", "series_title", "season", "episode", "status", "error", "sync_delta",
               "sync_offset_ms", "candidate_title", "candidate_score", "candidate_seeders",
               "attempts", "tried", "french_path", "en_file", "quality", "dl_hash")}
    summary = f"operator escalated from Review: {e['series_title']} S{e['season']:02d}E{e['episode']:02d} — {e['status']}"
    if e.get("error"):
        summary += f": {e['error']}"
    queued = core.ticket(
        f"review-e{ep_id}", summary,
        {"record": record,
         "api": "http://10.0.1.5:8090/api (host) / http://localhost:8080/api (in-container)",
         "diagnose_first": (
             f"GET /episode/{ep_id}/context — the record, a probe of both files, the log lines, "
             "every donor file with the (season,episode) the parser read, the series' episode "
             "list, and a `numbering` block (library S/E vs the release's S/E and absolute "
             "number). vo-merge translates aired<->absolute itself from Sonarr, so "
             "`translated: true` means search and donor mapping already use the aired numbering."),
         "actions": [
             "GET  /episode/{id}/candidates — list releases (incl. already-tried)",
             "POST /episode/{id}/retry — blocklist current release, drop donor, re-search",
             "POST /episode/{id}/assign {\"path\":\"/abs/file.mkv\"} — map one donor file to this "
             "episode and queue the merge (when automatic numbering translation can't apply)",
             "POST /episode/{id}/set_sync {\"offset_ms\":N,\"drift\":1.0427083} — apply a known "
             "offset / rate stretch with no detection",
             "POST /episode/{id}/ignore — give up on this episode",
         ],
         "report_back": (
             f"When done, POST /episode/{ep_id}/ai_result with "
             "{\"status\":\"resolved|failed|needs_human\",\"verdict\":\"one line\","
             "\"action_taken\":\"what you did\"} so this leaves the operator's manual-review queue."),
         "docs": "/mnt/nvme/AIWorkspace/vo-merge/dev/vo-merge/CLAUDE.md"},
        force=True)
    if queued:
        core.set_ep_status(ep_id, e["status"], ai_status="pending", ai_at=time.time())
    return {"ok": True, "queued": bool(queued)}


class AiResultIn(BaseModel):
    status: str                       # resolved | failed | needs_human
    verdict: str | None = None
    action_taken: str | None = None


_AI_STATUSES = ("resolved", "failed", "needs_human")


@api.post("/movie/{tmdb_id}/ai_result")
def movie_ai_result(tmdb_id: int, body: AiResultIn):
    """The host AI dispatcher reports back on a record it was paged about. Stores the verdict
    (leaving the pipeline status untouched) so 'failed'/'needs_human' surface in the Review tab
    for a human, and 'resolved' shows the item was handled."""
    if body.status not in _AI_STATUSES:
        raise HTTPException(422, f"status must be one of {_AI_STATUSES}")
    mv = core.get_movie(tmdb_id)
    if not mv:
        raise HTTPException(404, "unknown movie")
    core.set_status(tmdb_id, mv["status"], ai_status=body.status,
                    ai_verdict=(body.verdict or body.action_taken), ai_at=time.time())
    core.log(f"ai_result movie {tmdb_id}: {body.status} — {(body.verdict or body.action_taken or '')[:80]}")
    return {"ok": True}


@api.post("/episode/{ep_id}/ai_result")
def episode_ai_result(ep_id: str, body: AiResultIn):
    """TV mirror of movie_ai_result."""
    if body.status not in _AI_STATUSES:
        raise HTTPException(422, f"status must be one of {_AI_STATUSES}")
    e = core.get_episode(ep_id)
    if not e:
        raise HTTPException(404, "unknown episode")
    core.set_ep_status(ep_id, e["status"], ai_status=body.status,
                       ai_verdict=(body.verdict or body.action_taken), ai_at=time.time())
    core.log(f"ai_result episode {ep_id}: {body.status} — {(body.verdict or body.action_taken or '')[:80]}")
    return {"ok": True}


# ---------------------------------------------------------------- AI ACTIONS
# The dispatcher could diagnose failures but barely act on them: it could retry,
# ignore, or re-search, and nothing else. These give it hands for the failure
# classes it actually meets — numbering mismatches, known rate stretches, and
# titles the built-in query never matches.

class AssignIn(BaseModel):
    path: str                       # donor video file to graft audio from


@api.post("/episode/{ep_id}/assign")
def episode_assign(ep_id: str, body: AssignIn):
    """Point an episode at a SPECIFIC downloaded file and queue it for merging.

    This is the fix for the commonest TV dead-end: a pack downloads fine but its
    files are numbered differently than the library expects (absolute vs aired
    season — e.g. library S01E44 vs release S03E08), so nothing auto-maps. The
    agent (or a human) works out the mapping and states it here, one call per
    episode."""
    e = core.get_episode(ep_id)
    if not e:
        raise HTTPException(404, "unknown episode")
    if not os.path.isfile(body.path):
        raise HTTPException(400, f"no such file: {body.path}")
    if not body.path.lower().endswith((".mkv", ".mp4", ".m4v", ".avi", ".ts")):
        raise HTTPException(400, "not a video file")
    core.set_ep_status(ep_id, "ready", en_file=body.path, error=None,
                       progress="queued for merge (assigned)")
    pipeline.MERGE_WAKE.set()
    core.log(f"assign {ep_id}: {os.path.basename(body.path)} -> merge queue")
    return {"ok": True, "queued": True}


class SetSyncIn(BaseModel):
    offset_ms: int = 0
    drift: float | None = None      # rate ratio, e.g. 1.0427083 for 23.976->25 PAL


def _set_sync(kind: str, rec, ident, body: SetSyncIn):
    if body.drift is not None and not (0.9 <= body.drift <= 1.11):
        raise HTTPException(422, "drift must be a rate ratio near 1.0 (0.9–1.11)")
    setter = core.set_status if kind == "movie" else core.set_ep_status
    setter(ident, rec["status"], sync_offset_ms=int(body.offset_ms),
           sync_drift=body.drift, error=None)
    return setter


@api.post("/movie/{tmdb_id}/set_sync")
def movie_set_sync(tmdb_id: int, body: SetSyncIn):
    """Apply a KNOWN offset and/or rate stretch, then re-merge — no detection.
    `drift` is the donor->base rate ratio the mux applies (25/23.976 = 1.0427083
    for a PAL library file vs a film-rate release). Use when detection fails but
    the correct ratio is known from the two framerates."""
    mv = core.get_movie(tmdb_id)
    if not mv:
        raise HTTPException(404, "unknown movie")
    _set_sync("movie", mv, tmdb_id, body)
    core.log(f"set_sync {tmdb_id}: offset={body.offset_ms}ms drift={body.drift}")
    pipeline.merge_movie(tmdb_id)
    return core.get_movie(tmdb_id)


@api.post("/episode/{ep_id}/set_sync")
def episode_set_sync(ep_id: str, body: SetSyncIn):
    """TV mirror of movie_set_sync."""
    from . import tv
    e = core.get_episode(ep_id)
    if not e:
        raise HTTPException(404, "unknown episode")
    _set_sync("episode", e, ep_id, body)
    core.log(f"set_sync {ep_id}: offset={body.offset_ms}ms drift={body.drift}")
    tv.merge_ready_episode(ep_id)
    return core.get_episode(ep_id)


class FindIn(BaseModel):
    query: str
    indexers: list[int] | None = None


@api.post("/search_releases")
def search_releases(body: FindIn):
    """Run an ARBITRARY Prowlarr query and return the raw results.

    The built-in search composes its own query from the library title, so a title
    that never matches (alternate romanisation, different release name, wrong year)
    can never be found no matter how often it re-searches — the single biggest
    bucket in the backlog. This lets the caller supply the query itself and then
    act on a result with the existing /grab endpoints."""
    cfg = core.load_config()
    ids = body.indexers if body.indexers is not None else \
        cfg["en_indexer_ids"] + cfg.get("multi_indexer_ids", [])
    try:
        results = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"]).search(body.query, ids)
    except Exception as e:
        raise HTTPException(502, f"prowlarr: {e}")
    out = []
    for r in results:
        link = pipeline._pick_link(r)
        out.append({"title": r.get("title", ""), "seeders": r.get("seeders") or 0,
                    "size": r.get("size") or 0, "indexer": r.get("indexer"),
                    "link": link, "info_url": r.get("infoUrl"),
                    "rid": pipeline._hash_from_magnet(link) or r.get("guid") or r.get("title")})
    out.sort(key=lambda x: -x["seeders"])
    core.log(f"search_releases '{body.query[:60]}': {len(out)} result(s)")
    return {"query": body.query, "count": len(out), "results": out[:60]}


def _probe_brief(path):
    if not path or not os.path.exists(path):
        return None
    p = pipeline.probe(path)
    if not p:
        return {"path": path, "error": "probe failed"}
    return {"path": path, "dur": p.get("dur"), "fps": p.get("fps"),
            "audio": [{"id": a["id"], "lang": a["lang"], "codec": a.get("codec"),
                       "ch": a.get("ch"), "name": a.get("name")} for a in p.get("auds", [])],
            "subs": [{"id": s["id"], "lang": s["lang"], "codec": s.get("codec"),
                      "name": s.get("name"), "forced": s.get("forced"), "sdh": s.get("sdh")}
                     for s in p.get("subs", [])]}


@api.get("/movie/{tmdb_id}/context")
def movie_context(tmdb_id: int):
    """Everything needed to diagnose one movie in a single call — the record, a
    probe of both files (framerate/duration/audio tracks), and the log lines that
    mention it. Saves the agent from shelling into the container."""
    mv = core.get_movie(tmdb_id)
    if not mv:
        raise HTTPException(404, "unknown movie")
    key = str(tmdb_id)
    return {"record": mv,
            "library_file": _probe_brief(mv.get("french_path")),
            "donor_file": _probe_brief(mv.get("en_file")),
            "log": [l for l in core.tail_log(800) if key in l][-40:]}


@api.get("/episode/{ep_id}/context")
def episode_context(ep_id: str):
    """Episode mirror of movie_context, plus the two things a numbering mismatch
    needs: every video file in the donor download with the (season, episode) the
    parser read from it, and the series' episodes with their statuses. Comparing
    those two lists IS the diagnosis."""
    from . import tv
    e = core.get_episode(ep_id)
    if not e:
        raise HTTPException(404, "unknown episode")
    cfg = core.load_config()
    files = []
    if e.get("dl_hash"):
        try:
            qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
            t = next((x for x in qb.torrents(cfg["qb_tv_category"])
                      if x["hash"] == e["dl_hash"]), None)
            if t:
                local = pipeline._qb_to_local(t.get("content_path") or t.get("save_path") or "", cfg)
                root = local if os.path.isdir(local) else os.path.dirname(local)
                for r, _, fs in os.walk(root):
                    for f in fs:
                        if f.lower().endswith(tv.VIDEXT):
                            full = os.path.join(r, f)
                            s, ep = tv._parse_se(os.path.relpath(full, root))
                            files.append({"file": full, "parsed_season": s, "parsed_episode": ep,
                                          "maps_to": tv._alt_keys(e["series_id"], s, ep, cfg)
                                                     if s is not None else []})
        except Exception as ex:
            files = [{"error": str(ex)}]
    siblings = [{"id": x["id"], "season": x["season"], "episode": x["episode"],
                 "status": x["status"], "french_path": x.get("french_path")}
                for x in core.get_episodes() if x["series_id"] == e["series_id"]]
    rs, rn = tv._release_se(e, cfg)
    return {"record": e,
            "library_file": _probe_brief(e.get("french_path")),
            "donor_file": _probe_brief(e.get("en_file")),
            "donor_files": files,
            "series_episodes": sorted(siblings, key=lambda x: (x["season"], x["episode"])),
            # aired <-> absolute translation, so a numbering mismatch reads off the record:
            # the library files this episode as S{season}E{episode}, releases number it
            # S{release_season}E{release_episode} (absolute {absolute}).
            "numbering": {"library_season": e["season"], "library_episode": e["episode"],
                          "release_season": rs, "release_episode": rn,
                          "absolute": tv._abs_num(e, cfg),
                          "translated": (rs, rn) != (e["season"], e["episode"])},
            "log": [l for l in core.tail_log(800) if ep_id in l][-40:]}


class UnfixableIn(BaseModel):
    reason: str


@api.post("/movie/{tmdb_id}/unfixable")
def movie_unfixable(tmdb_id: int, body: UnfixableIn):
    """Give up on a title permanently, with the reason recorded. Distinct from
    /ignore: this states WHY, so it doesn't look like an unexamined skip."""
    if not core.get_movie(tmdb_id):
        raise HTTPException(404, "unknown movie")
    core.set_status(tmdb_id, "ignored", error=f"unfixable: {body.reason}",
                    ai_status="needs_human", ai_verdict=body.reason, ai_at=time.time())
    core.log(f"unfixable {tmdb_id}: {body.reason[:80]}")
    return {"ok": True}


@api.post("/episode/{ep_id}/unfixable")
def episode_unfixable(ep_id: str, body: UnfixableIn):
    """Episode mirror of movie_unfixable."""
    if not core.get_episode(ep_id):
        raise HTTPException(404, "unknown episode")
    core.set_ep_status(ep_id, "ignored", error=f"unfixable: {body.reason}",
                       ai_status="needs_human", ai_verdict=body.reason, ai_at=time.time())
    core.log(f"unfixable {ep_id}: {body.reason[:80]}")
    return {"ok": True}


@api.get("/downloads")
def downloads():
    """Live qB progress for everything in the audio-merge categories, keyed by infohash.
    Polled by the UI to draw download progress bars without hammering the DB."""
    cfg = core.load_config()
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    out = {}
    try:
        qb.login()
        for cat in (cfg["qb_category"], cfg["qb_tv_category"]):
            for t in qb.torrents(cat):
                out[t["hash"].lower()] = {
                    "progress": t.get("progress", 0) or 0,
                    "dlspeed": t.get("dlspeed", 0) or 0,
                    "eta": t.get("eta", 0) or 0,
                    "state": t.get("state", ""),
                    "seeds": t.get("num_complete", t.get("num_seeds", 0)) or 0,
                    "size": t.get("size", 0) or 0,
                    "downloaded": t.get("completed", 0) or 0,
                }
    except Exception as e:
        return {"error": str(e), "items": {}}
    return {"items": out}


@api.get("/logs")
def logs():
    return {"lines": core.tail_log()}


@api.get("/dashboard")
def dashboard():
    """Everything the Overview tab needs in one call. Blocks degrade independently
    (qB unreachable -> inflight=None, disk stat failure -> disk=None) — never a 500."""
    import shutil, time
    cfg = core.load_config()
    now = time.time()
    ACTIVE = ("grabbed", "downloading", "ready", "merging")
    ATTN = ("review", "sync_fail", "error")
    order = {s: i for i, s in enumerate(("merging", "ready", "downloading", "grabbed"))}
    qa, qn = ",".join("?" * len(ACTIVE)), ",".join("?" * len(ATTN))
    with core.db() as c:
        mcounts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM movies GROUP BY status")}
        ecounts = {r["status"]: r["n"] for r in c.execute(
            "SELECT status, COUNT(*) n FROM episodes GROUP BY status")}

        # merge-queue position (1 = next up) so a finished download can say where it stands
        qpos = {}
        for i, (kind, rid, _ts) in enumerate(pipeline.merge_queue(cfg), start=1):
            qpos[f"{'m' if kind == 'movie' else 'e'}{rid}"] = i

        active = [{"kind": "movie", "key": f"m{r['tmdb_id']}", "title": r["title"],
                   "sub": r["candidate_title"], "status": r["status"], "progress": r["progress"],
                   "dl_hash": r["dl_hash"], "poster": r["poster"], "count": 1,
                   "queue_pos": qpos.get(f"m{r['tmdb_id']}")}
                  for r in c.execute(f"SELECT * FROM movies WHERE status IN ({qa})", ACTIVE)]
        # episodes: fold season-pack siblings (same torrent + status) into one row
        packs = {}
        for r in c.execute(f"SELECT * FROM episodes WHERE status IN ({qa}) "
                           "ORDER BY series_title, season, episode", ACTIVE):
            e = dict(r)
            k = (e["series_title"], e["season"], e["status"], e["dl_hash"] or e["id"])
            g = packs.setdefault(k, {"e": e, "eps": [], "ids": [], "progress": None})
            g["eps"].append(e["episode"])
            g["ids"].append(e["id"])
            g["progress"] = g["progress"] or e.get("progress")
        for g in packs.values():
            e, eps = g["e"], sorted(g["eps"])
            rng = f"E{eps[0]:02d}" + (f"–E{eps[-1]:02d}" if len(eps) > 1 else "")
            # a folded pack row reports the best (soonest) position among its episodes
            pos = [p for p in (qpos.get(f"e{i}") for i in g["ids"]) if p]
            active.append({"kind": "episode", "key": f"e{e['id']}",
                           "title": f"{e['series_title']} S{e['season']:02d} {rng}",
                           "sub": e["candidate_title"], "status": e["status"],
                           "progress": g["progress"], "dl_hash": e["dl_hash"],
                           "poster": e["poster"], "count": len(eps),
                           "queue_pos": min(pos) if pos else None})
        active.sort(key=lambda x: (order.get(x["status"], 9), x["title"]))

        attention = [{"kind": "movie", "key": f"m{r['tmdb_id']}", "title": r["title"],
                      "status": r["status"], "error": r["error"],
                      "sync_delta": r["sync_delta"], "poster": r["poster"], "ts": r["updated"],
                      "ai_status": r["ai_status"], "ai_verdict": r["ai_verdict"]}
                     for r in c.execute(f"SELECT * FROM movies WHERE status IN ({qn}) "
                                        "ORDER BY updated DESC LIMIT 8", ATTN)]
        attention += [{"kind": "episode", "key": f"e{r['id']}",
                       "title": f"{r['series_title']} S{r['season']:02d}E{r['episode']:02d}",
                       "status": r["status"], "error": r["error"],
                       "sync_delta": r["sync_delta"], "poster": r["poster"], "ts": r["updated"],
                       "ai_status": r["ai_status"], "ai_verdict": r["ai_verdict"]}
                      for r in c.execute(f"SELECT * FROM episodes WHERE status IN ({qn}) "
                                         "ORDER BY updated DESC LIMIT 8", ATTN)]
        attention = sorted(attention, key=lambda x: x["ts"] or 0, reverse=True)[:8]

        recent = [{"kind": "movie", "title": r["title"], "langs": r["added_langs"],
                   "poster": r["poster"], "ts": r["merged_at"] or r["updated"]}
                  for r in c.execute("SELECT * FROM movies WHERE status='merged' "
                                     "ORDER BY COALESCE(merged_at, updated) DESC LIMIT 10")]
        recent += [{"kind": "episode",
                    "title": f"{r['series_title']} S{r['season']:02d}E{r['episode']:02d}",
                    "langs": r["added_langs"], "poster": r["poster"],
                    "ts": r["merged_at"] or r["updated"]}
                   for r in c.execute("SELECT * FROM episodes WHERE status='merged' "
                                      "ORDER BY COALESCE(merged_at, updated) DESC LIMIT 10")]
        recent = sorted(recent, key=lambda x: x["ts"] or 0, reverse=True)[:10]

        def merged_since(secs):
            return sum(c.execute(f"SELECT COUNT(*) n FROM {t} WHERE status='merged' "
                                 "AND COALESCE(merged_at, updated) >= ?",
                                 (now - secs,)).fetchone()["n"] for t in ("movies", "episodes"))
        merged_24h, merged_7d = merged_since(86400), merged_since(7 * 86400)

    try:
        inflight = pipeline.inflight_downloads(cfg)
    except Exception:
        inflight = None                       # qB unreachable — tile shows "?"

    disk = None
    try:
        p = cfg.get("downloads_mount") or cfg.get("media_mount") or "/media"
        while p and p != "/" and not os.path.isdir(p):
            p = os.path.dirname(p)            # dir may not exist until the first grab
        u = shutil.disk_usage(p)
        disk = {"path": p, "total": u.total, "free": u.free}
    except Exception:
        pass

    next_runs = {}
    try:
        for job in scheduler._sched.get_jobs():
            if job.next_run_time:
                next_runs[job.id] = job.next_run_time.timestamp()
    except Exception:
        pass

    return {"enabled": cfg["enabled"], "grab_mode": cfg["grab_mode"],
            "scope_series": bool(cfg.get("scope_series")),
            "movies": mcounts, "episodes": ecounts,
            "active": active, "attention": attention, "recent": recent,
            "merged_24h": merged_24h, "merged_7d": merged_7d,
            "inflight": inflight, "inflight_cap": int(cfg.get("max_inflight_downloads", 5)),
            "merge_cap": pipeline.MERGE_GATE.limit(cfg),
            "disk": disk, "next_runs": next_runs, "now": now}


# ----- live sync editor -----
def _audio_index(path, lang):
    from .offdet import audio_langs
    pfx = "en" if lang.startswith("en") else "fr" if lang.startswith("fr") else lang[:2]
    for i, l in enumerate(audio_langs(path)):
        if l.startswith(pfx):
            return i
    return 0


@api.get("/movie/{tmdb_id}/preview")
def make_preview(tmdb_id: int, lang: str = "eng", t: int = -1):
    """A STABLE 20s window: muted video clip (frame-steppable, never re-rendered) + the
    raw chosen audio track. The browser shifts audio vs picture live (waveform + Web
    Audio), so tuning is reload-free. Offset is applied only on Apply."""
    mv = core.get_movie(tmdb_id)
    f = (mv or {}).get("merged_file") or (mv or {}).get("french_path")
    if not f or not os.path.exists(f):
        raise HTTPException(404, "file not found")
    ei = pipeline.probe(f) or {}
    dur, fps = ei.get("dur") or 0, ei.get("fps") or 23.976
    if t < 0:
        t = int(dur * 0.45) if dur else 600
    out = os.path.join(PREVIEW_DIR, str(tmdb_id))
    os.makedirs(out, exist_ok=True)
    ai = _audio_index(f, lang)
    vid, aud = os.path.join(out, "video.mp4"), os.path.join(out, "audio.m4a")
    subprocess.run(["nice", "-n", "19", "ffmpeg", "-y", "-ss", str(t), "-t", "20", "-i", f,
                    "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", "scale=640:-2",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "26",
                    "-movflags", "+faststart", vid], capture_output=True)
    subprocess.run(["nice", "-n", "19", "ffmpeg", "-y", "-ss", str(t), "-t", "20", "-i", f,
                    "-map", f"0:a:{ai}", "-vn", "-c:a", "aac", "-b:a", "160k", aud], capture_output=True)
    ver = int(os.path.getmtime(f))           # changes whenever Apply rewrites the file -> busts cache
    return {"video": f"/api/preview/{tmdb_id}/video.mp4?v={t}_{ver}",
            "audio": f"/api/preview/{tmdb_id}/audio.m4a?v={t}_{ver}",
            "start": t, "fps": fps, "duration": 20, "movie_dur": int(dur)}


@api.get("/preview/{tmdb_id}/{name}")
def serve_preview(tmdb_id: int, name: str):
    p = os.path.join(PREVIEW_DIR, str(tmdb_id), os.path.basename(name))
    if not os.path.isfile(p):
        raise HTTPException(404)
    return FileResponse(p)


class ApplyIn(BaseModel):
    offset_ms: int
    lang: str = "eng"


@api.post("/movie/{tmdb_id}/apply_offset")
def apply_offset(tmdb_id: int, body: ApplyIn):
    pipeline.resync_movie(tmdb_id, offset_ms=body.offset_ms, shift_lang=body.lang)
    return core.get_movie(tmdb_id)


# ----- series (Sonarr) -----
@api.get("/tv/status")
def tv_status():
    return {"counts": core.ep_status_counts()}


@api.get("/tv/episodes")
def tv_episodes(status: str | None = None):
    """Episode rows, each stamped with the numbering a RELEASE uses for it (`aired`) when that
    differs from the library's — an absolute-as-S01 anime record is filed E51 but released as
    S04E15, and without saying so the UI shows a season/episode no indexer has ever heard of."""
    from . import tv
    cfg = core.load_config()
    rows = core.get_episodes(status)
    for e in rows:
        try:
            rs, rn = tv._release_se(e, cfg)          # cached per series; identity for plain TV
        except Exception:
            continue
        if (rs, rn) != (e["season"], e["episode"]):
            e["aired"] = f"S{rs:02d}E{rn:02d}"
    return rows


@api.post("/tv/scan")
def tv_scan():
    from . import tv
    return {"found": tv.scan()}


@api.get("/tv/{series_id}/{season}/candidates")
def tv_season_candidates(series_id: int, season: int):
    from . import tv
    return tv.season_candidates(series_id, season)


@api.post("/tv/{series_id}/{season}/grab")
def tv_season_grab(series_id: int, season: int, body: GrabIn):
    from . import tv
    n = tv.grab_season(series_id, season, body.link, body.rid, body.title)
    return {"ok": True, "episodes": n}


@api.post("/episode/{ep_id}/retry")
def ep_retry(ep_id: str):
    from . import tv
    tv.retry_episode(ep_id); return {"ok": True}


@api.post("/tv/retry_errors")
def tv_retry_errors():
    from . import tv
    return {"ok": True, "retried": tv.retry_errors()}


@api.post("/episode/{ep_id}/ignore")
def ep_ignore(ep_id: str):
    core.set_ep_status(ep_id, "ignored"); return {"ok": True}


@api.get("/episode/{ep_id}/candidates")
def ep_candidates(ep_id: str):
    from . import tv
    return tv.episode_candidates(ep_id)


@api.post("/episode/{ep_id}/grab")
def ep_grab(ep_id: str, body: GrabIn):
    from . import tv
    n = tv.grab_episode(ep_id, body.link, body.rid, body.title)
    return {"ok": True, "episodes": n}


app.mount("/api", api)

# ----- serve SPA (built React) -----
if os.path.isdir(STATIC):
    app.mount("/assets", StaticFiles(directory=os.path.join(STATIC, "assets")), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str):
        f = os.path.join(STATIC, full_path)
        if full_path and os.path.isfile(f):
            return FileResponse(f)
        return FileResponse(os.path.join(STATIC, "index.html"))
