"""FastAPI app: REST API + serves the built React SPA."""
import os, subprocess, time
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import core, scheduler, pipeline, media
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
            "paused": bool(cfg.get("paused")), "hold": pipeline.hold_reason(cfg),
            "merging_now": len(pipeline._merging_now()),
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

    Two modes, and the difference matters after an interruption:

    - **`forget=true` — full re-read.** Drops the probe cache for that scope first, so every file
      is read again even when its size and mtime are unchanged (use after fixing track tags by
      hand, or when you don't trust the cached answer).
    - **`forget=false` — progressive.** Keeps the cache, so an `mkvmerge` runs only for files
      with no valid probe: ones never read, ones changed on disk, and — the case this exists for —
      **whatever a previous pass never reached** because the container restarted mid-scan. It is
      resumable by construction: each file's result is committed as it is read, so re-running
      picks up exactly where the last one stopped, and a library that is already fully probed
      costs a stat per file and no decoding at all.

    Both walk the whole scope and both prune what has vanished; they differ only in whether the
    cache is thrown away first."""
    import threading
    from . import tv

    if scope not in ("all", "films", "anime", "series"):
        raise HTTPException(422, "scope must be all | films | anime | series")
    if not pipeline.SCAN_LOCK.acquire(blocking=False):
        return {"ok": True, "started": False, "note": "a scan is already running",
                "state": pipeline.SCAN_STATE}
    if forget:
        # Clear only the probes this scope is about to re-read. Clearing all of them made a
        # one-library re-read blank the OTHER libraries' coverage until they were rescanned too —
        # the probe table is the whole inventory now, not just a speed-up cache.
        cfg0 = core.load_config()
        mount = (cfg0.get("media_mount") or "/media").rstrip("/")
        anime = {x.lower() for x in (cfg0.get("anime_dirs") or ["Anime"])}
        series = {x.lower() for x in (cfg0.get("series_dirs") or ["Series"])}
        # by top-level folder, the same way /coverage and /library classify a file: "films" is
        # everything that is neither an anime nor a series folder (anime *films* live in Radarr)
        def _in_scope(path):
            top = (path or "")[len(mount) + 1:].split(os.sep, 1)[0].lower()
            if scope == "anime":  return top in anime
            if scope == "series": return top in series
            return top not in anime and top not in series
        with core.db() as c:
            if scope == "all":
                n = c.execute("DELETE FROM probes").rowcount
            else:
                gone = [r["path"] for r in c.execute("SELECT path FROM probes")
                        if _in_scope(r["path"])]
                n = 0
                for i in range(0, len(gone), 400):
                    chunk = gone[i:i + 400]
                    n += c.execute(f"DELETE FROM probes WHERE path IN ({','.join('?' * len(chunk))})",
                                   chunk).rowcount
        core.log(f"rescan({scope}): dropped {n} cached probe(s) — those files will be re-read")

    def _run():
        st = pipeline.SCAN_STATE
        st.update(running=True, scope=scope, started=time.time(), finished=0, phase="starting",
                  films=None, episodes=None, error=None, pruned=None, pruned_records=None,
                  full=bool(forget))
        media.reset_stats()      # so the pass can report what it actually READ vs reused
        try:
            # whatever the scopes say; pilot cleared so a pilot list can't shrink a rescan
            cfg = dict(core.load_config(), series_pilot=[])
            # A scan adds what is new; this is the other half — drop what is gone. Nothing else
            # ever removes a probe or a record, so a deleted title keeps being counted (and keeps
            # dragging coverage down) forever. Guarded on the mount actually being there: if
            # /media is unmounted every path is "missing" and a blind prune would wipe the DB.
            mount = (cfg.get("media_mount") or "/media").rstrip("/")
            if os.path.isdir(mount) and os.listdir(mount):
                st["phase"] = "pruning deleted files"
                st["pruned"] = core.prune_missing_probes()
                mv, ep = core.prune_missing_records()
                st["pruned_records"] = mv + ep
                if st["pruned"] or st["pruned_records"]:
                    core.log(f"rescan({scope}): dropped {st['pruned']} probe(s) and "
                             f"{mv} movie/{ep} episode record(s) whose file is gone")
            else:
                core.log(f"rescan({scope}): {mount} looks unmounted — skipping the prune")
            if scope in ("all", "films"):
                st["phase"] = "films"
                st["films"] = pipeline.scan(cfg)
            if scope in ("all", "anime", "series"):
                st["phase"] = "anime" if scope == "anime" else ("series" if scope == "series" else "series & anime")
                kinds = None if scope == "all" else (scope,)
                st["episodes"] = tv.scan(cfg, kinds=kinds)
            st["phase"] = "done"
            core.log(f"rescan({scope}, {'full' if forget else 'progressive'}): "
                     f"{st['films']} film gap(s), {st['episodes']} episode gap(s) · "
                     f"read {media.STATS['probed']} file(s), "
                     f"{media.STATS['cached']} already cached")
        except Exception as e:
            st["error"] = str(e)
            st["phase"] = "error"
            core.log(f"rescan({scope}) error: {e}")
        finally:
            st.update(running=False, finished=time.time())
            pipeline.SCAN_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True, "scope": scope, "probes": core.probe_stats()}


# The one probe error that describes the FILE rather than our tools: mkvmerge read the container
# and found no audio, and ffprobe independently agreed. Nothing else may authorise a deletion —
# "unsupported container" and "audio mkvmerge can't read" mean the file is probably fine and we
# simply can't mux it, and "unreadable" means we know nothing at all.
BROKEN_ERR = "no audio track"


def _inventory(cfg, cols="path, auds, subs, err, excluded"):
    """Every probed library file, classified against the profile its library targets.

    The `probes` table is the ONLY complete inventory: `scan()` deliberately inserts a
    movies/episodes record only when a file HAS a gap, so those tables are a list of problems,
    not a list of files. Everything that reports on "how much of the library is correct" —
    /coverage and /library both — reads this, so the summary and the browsable list can never
    disagree about what "complete" means.

    Yields (row, top, kind, want_a, want_s, have_a, have_s, miss_a, miss_s). `-EN` mirrors are
    skipped: they are symlinks to the same files and would double-count.

    A row flagged `excluded` is inventory the pipeline deliberately does not target
    (`exclude_french_origin`). It is counted and listable — it is a real file on disk — but it is
    not scored as a failure for a gap nobody intends to fill."""
    mount = (cfg.get("media_mount") or "/media").rstrip("/")
    anime = {x.lower() for x in (cfg.get("anime_dirs") or ["Anime"])}
    series = {x.lower() for x in (cfg.get("series_dirs") or ["Series"])}
    mirrors = {v.lower() for v in pipeline.EN_LIBS.values()}
    prof = {}
    with core.db() as c:
        rows = c.execute(f"SELECT {cols} FROM probes").fetchall()
    for r in rows:
        path = r["path"] or ""
        top = path[len(mount) + 1:].split(os.sep, 1)[0] if path.startswith(mount + "/") else "?"
        if top.lower() in mirrors:
            continue
        kind = "anime" if top.lower() in anime else ("series" if top.lower() in series else "movie")
        if kind not in prof:
            prof[kind] = media.profile(kind, cfg)
        want_a, want_s = prof[kind]
        if r["err"]:
            yield r, top, kind, want_a, want_s, None, None, None, None
            continue
        have_a = {x for x in (r["auds"] or "").split(",") if x}
        have_s = {x for x in (r["subs"] or "").split(",") if x}
        yield (r, top, kind, want_a, want_s, have_a, have_s,
               [k for k in want_a if k not in have_a], [k for k in want_s if k not in have_s])


@api.get("/coverage")
def coverage():
    """How much of the library actually meets its language targets, grouped by top-level library
    folder — because that is how the operator thinks about it — and each folder scored against
    the profile its kind targets."""
    cfg = core.load_config()
    libs = {}
    for r, top, kind, want_a, want_s, have_a, have_s, miss_a, miss_s in _inventory(cfg):
        L = libs.setdefault(top, {"name": top, "kind": kind, "total": 0, "unreadable": 0,
                                  "complete": 0, "missing_audio": 0, "missing_subs": 0,
                                  "missing_both": 0, "excluded": 0,
                                  "audio": {k: 0 for k in want_a},
                                  "subs": {k: 0 for k in want_s},
                                  "targets": {"audio": want_a, "subs": want_s}})
        L["total"] += 1
        if r["excluded"]:
            L["excluded"] += 1        # counted as a file, not scored against the target
            continue
        if have_a is None:
            L["unreadable"] += 1
            continue
        for k in want_a:
            if k in have_a: L["audio"][k] += 1
        for k in want_s:
            if k in have_s: L["subs"][k] += 1
        if miss_a and miss_s:   L["missing_both"] += 1
        elif miss_a:            L["missing_audio"] += 1
        elif miss_s:            L["missing_subs"] += 1
        else:                   L["complete"] += 1
    order = {"movie": 0, "anime": 1, "series": 2}
    out = sorted(libs.values(), key=lambda x: (order.get(x["kind"], 9), x["name"]))
    tot = sum(l["total"] for l in out)
    excl = sum(l["excluded"] for l in out)
    return {"libraries": out, "total": tot,
            "complete": sum(l["complete"] for l in out),
            "unreadable": sum(l["unreadable"] for l in out),
            "excluded": excl,
            # the percentage is over what is actually TARGETED, so files we deliberately skip
            # neither inflate nor deflate it
            "targeted": tot - excl,
            "probed": tot}


@api.get("/library")
def library(state: str = "incomplete", lib: str = "", q: str = "",
            limit: int = 200, offset: int = 0):
    """Browse the probed inventory file by file, split into what meets its target and what
    doesn't. /coverage answers "how much" as a number; this answers "which ones", which is the
    only form you can act on.

    `state`: complete | incomplete | unreadable | all. `lib` filters to one top-level library,
    `q` is a case-insensitive substring of the path. The counts returned are for the whole
    (lib+q) selection, not just the returned page, so the tab headers stay honest while paging."""
    if state not in ("complete", "incomplete", "unreadable", "excluded", "all"):
        raise HTTPException(422,
                            "state must be complete | incomplete | unreadable | excluded | all")
    cfg = core.load_config()
    mount = (cfg.get("media_mount") or "/media").rstrip("/")
    ql, libl = q.strip().lower(), lib.strip().lower()
    counts = {"complete": 0, "incomplete": 0, "unreadable": 0, "excluded": 0}
    errs = {}          # unreadable broken down by WHY — they are rarely all the same problem
    libs, hits = {}, []
    for (r, top, kind, want_a, want_s, have_a, have_s, miss_a,
         miss_s) in _inventory(cfg, "path, auds, subs, err, excluded, dur, probed"):
        libs[top] = libs.get(top, 0) + 1
        if libl and top.lower() != libl:
            continue
        path = r["path"] or ""
        if ql and ql not in path.lower():
            continue
        row_state = ("excluded" if r["excluded"] else
                     "unreadable" if have_a is None else
                     "incomplete" if (miss_a or miss_s) else "complete")
        counts[row_state] += 1
        if row_state == "unreadable":
            errs[r["err"] or "unreadable"] = errs.get(r["err"] or "unreadable", 0) + 1
        if state not in ("all", row_state):
            continue
        rel = path[len(mount) + 1:] if path.startswith(mount + "/") else path
        parts = rel.split(os.sep)
        hits.append({
            "path": path, "rel": rel, "lib": top, "kind": kind,
            # the folder under the library is the title for a film and the show for an episode;
            # the file name is what distinguishes episodes within it
            "title": parts[1] if len(parts) > 1 else (parts[0] if parts else rel),
            "file": parts[-1],
            "state": row_state,
            "audio": sorted(have_a or []), "subs": sorted(have_s or []),
            "missing_audio": miss_a or [], "missing_subs": miss_s or [],
            "targets": {"audio": want_a, "subs": want_s},
            "dur": r["dur"], "probed": r["probed"], "err": r["err"],
        })
    hits.sort(key=lambda x: (x["lib"].lower(), x["rel"].lower()))
    total = len(hits)
    off = max(0, offset)
    return {"items": hits[off:off + max(1, min(limit, 1000))], "total": total,
            "offset": off, "counts": counts,
            "error_kinds": dict(sorted(errs.items(), key=lambda kv: -kv[1])),
            "repairable": errs.get(BROKEN_ERR, 0),   # the only error a repair may act on
            "libraries": [{"name": k, "total": v} for k, v in sorted(libs.items())]}


def _owner_index(cfg, paths):
    """Map each /media path to the *arr record that owns it.

    Radarr answers in one call (`movieFile` is embedded in the movie). Sonarr has no
    library-wide file endpoint, so only the series whose folder actually contains one of `paths`
    is queried — a repair of 30 files costs a handful of calls, not one per series."""
    out, want = {}, set(paths)
    try:
        for m in Radarr(cfg["radarr_url"], cfg["radarr_key"]).movies() or []:
            mf = m.get("movieFile") or {}
            p = media.to_media(mf.get("path"), cfg) if mf.get("path") else None
            if p in want:
                out[p] = {"arr": "radarr", "kind": "movie", "id": m["id"],
                          "file_id": mf["id"], "title": m.get("title") or ""}
    except Exception as e:
        core.log(f"repair: Radarr lookup failed: {e}")
    rest = [p for p in want if p not in out]
    if not rest:
        return out
    try:
        s = Sonarr(cfg["sonarr_url"], cfg["sonarr_key"])
        folders = []
        for se in s.series() or []:
            sp = media.to_media(se.get("path"), cfg)
            if sp:
                folders.append((sp.rstrip("/") + "/", se))
        cache = {}
        for p in rest:
            se = next((x for pre, x in folders if p.startswith(pre)), None)
            if not se:
                continue
            sid = se["id"]
            if sid not in cache:
                efs = {}
                for ef in s.episode_files(sid) or []:
                    mp = media.to_media(ef.get("path"), cfg)
                    if mp:
                        efs[mp] = ef["id"]
                eps = {}
                for e in s.episodes(sid) or []:
                    if e.get("episodeFileId"):
                        eps.setdefault(e["episodeFileId"], []).append(e["id"])
                cache[sid] = (efs, eps)
            efs, eps = cache[sid]
            fid = efs.get(p)
            if fid:
                out[p] = {"arr": "sonarr", "kind": "episode", "id": sid, "file_id": fid,
                          "episode_ids": eps.get(fid, []), "title": se.get("title") or ""}
    except Exception as e:
        core.log(f"repair: Sonarr lookup failed: {e}")
    return out


class RepairIn(BaseModel):
    paths: list[str] | None = None   # None = every file currently probed as audio-less
    dry_run: bool = True
    limit: int = 500


@api.post("/library/repair")
def library_repair(body: RepairIn):
    """Delete library files that genuinely carry no audio, and have Radarr/Sonarr fetch a
    replacement. A file with no audio track can never be fixed by grafting — there is nothing to
    sync against and nothing to keep — so the only repair is a new copy.

    This deletes the operator's media, so it is deliberately narrow:

    - **Only `no audio track`.** That error now means mkvmerge read the container AND ffprobe
      independently found zero audio streams. The other probe errors describe our tools, not the
      file, and are never acted on.
    - **Every candidate is re-probed with the cache bypassed** before anything is touched, so a
      stale row can't authorise a deletion.
    - **A file the *arrs don't know about is skipped**, because deleting it would just lose it —
      nothing would search for a replacement.
    - **Deletion goes through the *arr**, which removes the file from disk *and* marks the
      movie/episode missing. Unlinking it ourselves would leave the *arr believing it still has
      the file, and it would never search.
    - **`dry_run` is the default** and changes nothing.

    A real run re-probes every candidate, so it takes minutes: it runs in a thread under
    SCAN_LOCK (one heavy file pass at a time) and `GET /api/library/repair` reports progress."""
    import threading
    cfg = core.load_config()
    if body.paths is None:
        with core.db() as c:
            paths = [r["path"] for r in
                     c.execute("SELECT path FROM probes WHERE err=? ORDER BY path", (BROKEN_ERR,))]
    else:
        paths = [p for p in body.paths if p]
    paths = paths[:max(1, min(body.limit, 2000))]

    if body.dry_run:
        # Cheap: report what a real run would attempt, from the probes already on record. The
        # real run re-verifies each one anyway, so this list is a preview, not a promise.
        owners = _owner_index(cfg, paths) if paths else {}
        return {"dry_run": True, "total": len(paths),
                "candidates": [{"path": p, "title": (owners.get(p) or {}).get("title") or "",
                                "kind": (owners.get(p) or {}).get("kind") or "",
                                "known": p in owners} for p in paths],
                "unknown": sum(1 for p in paths if p not in owners)}

    if not pipeline.SCAN_LOCK.acquire(blocking=False):
        return {"ok": True, "started": False, "note": "a scan or repair is already running"}

    def _run():
        st = pipeline.REPAIR_STATE
        st.update(running=True, started=time.time(), finished=0, phase="verifying",
                  checked=0, total=len(paths), deleted=0, searched=0,
                  skipped=[], done=[], error=None)
        try:
            radarr = Radarr(cfg["radarr_url"], cfg["radarr_key"])
            sonarr = Sonarr(cfg["sonarr_url"], cfg["sonarr_key"])
            confirmed = []
            for p in paths:
                st["checked"] += 1
                if not os.path.exists(p):
                    st["skipped"].append({"path": p, "reason": "already gone"})
                    continue
                _, _, err = media.audit(p, refresh=True)    # never trust the cache for a delete
                if err != BROKEN_ERR:
                    st["skipped"].append({"path": p,
                                          "reason": f"re-probe says: {err or 'the file is fine'}"})
                    continue
                confirmed.append(p)
            st["phase"] = "matching to Radarr/Sonarr"
            owners = _owner_index(cfg, confirmed) if confirmed else {}
            st["phase"] = "deleting"
            for p in confirmed:
                o = owners.get(p)
                if not o:
                    st["skipped"].append({"path": p, "reason":
                        "not in Radarr/Sonarr — deleting it would just lose the title"})
                    continue
                try:
                    if o["arr"] == "radarr":
                        radarr.delete_movie_file(o["file_id"])
                        radarr.search([o["id"]])
                    else:
                        sonarr.delete_episode_file(o["file_id"])
                        if o["episode_ids"]:
                            sonarr.search(o["episode_ids"])
                        else:                  # no episode row points at it — re-scan instead
                            sonarr.rescan(o["id"])
                    core.forget_probe(p)
                    st["deleted"] += 1
                    st["searched"] += 1
                    st["done"].append({"path": p, "title": o["title"], "kind": o["kind"]})
                    core.log(f"repair: deleted audio-less {o['kind']} "
                             f"{o['title'] or os.path.basename(p)} and asked "
                             f"{o['arr'].title()} to search again")
                except Exception as e:
                    st["skipped"].append({"path": p, "reason": f"delete failed: {e}"})
            core.prune_missing_records()
            st["phase"] = "done"
            core.log(f"repair: {st['deleted']} file(s) deleted and re-searched, "
                     f"{len(st['skipped'])} skipped")
        except Exception as e:
            st["error"] = str(e)
            st["phase"] = "error"
            core.log(f"repair error: {e}")
        finally:
            st.update(running=False, finished=time.time())
            pipeline.SCAN_LOCK.release()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "started": True, "total": len(paths)}


@api.get("/library/repair")
def library_repair_state():
    """Progress of a running (or the last) repair pass — it re-probes every candidate, so it
    takes minutes."""
    return pipeline.REPAIR_STATE


@api.get("/rescan")
def rescan_state():
    """Progress of a running (or the last) rescan — it takes minutes, so the UI can say so.

    `read`/`reused` are live counters, so a progressive pass can show it is working through new
    files rather than looking identical to a scan that found nothing to do."""
    return {**pipeline.SCAN_STATE, "probes": core.probe_stats(),
            "read": media.STATS["probed"], "reused": media.STATS["cached"],
            # why *arr-known files did not become inventory rows — so "the count is wrong" can be
            # explained (no file in Radarr / path not under /media / unreadable / not targeted)
            "skips": pipeline.SCAN_SKIPS}


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


# ---------------------------------------------------------------- *arr webhooks
# Radarr/Sonarr "Connect -> Webhook" on Import/Upgrade. Turns a new file into a scanned,
# queued record in seconds instead of waiting up to search_interval_min for the sweep.
# The payload shape varies by *arr version, so nothing here is required: we read what we
# need, fetch the authoritative record from the *arr by id, and judge the FILE as always.
IMPORT_EVENTS = {"download", "movieimported", "episodefileimport", "upgrade", "rename"}
DELETE_EVENTS = {"moviefiledelete", "episodefiledelete", "moviedelete", "seriesdelete"}


def _hook_auth(token):
    want = (core.load_config().get("webhook_token") or "").strip()
    if want and (token or "") != want:
        raise HTTPException(401, "bad webhook token")


def _hook_after(fn, what):
    """Run the ingest off the request thread and return at once: Radarr/Sonarr time their
    webhooks out, and a probe plus a Prowlarr search is far too slow to answer inline."""
    import threading

    def _run():
        try:
            fn()
        except Exception as e:
            core.log(f"hook {what}: {e}")
    threading.Thread(target=_run, daemon=True).start()


@api.post("/hook/radarr")
def hook_radarr(body: dict, token: str | None = None):
    """Radarr Connect -> Webhook. Point it at http://<vo-merge>/api/hook/radarr."""
    _hook_auth(token)
    ev = str(body.get("eventType") or "").lower()
    mid = (body.get("movie") or {}).get("id")
    if ev == "test":
        core.log("hook radarr: test OK")
        return {"ok": True, "test": True}
    if ev in DELETE_EVENTS:
        path = (body.get("movieFile") or {}).get("path")
        local = media.to_media(path, core.load_config()) if path else None
        if local:
            core.forget_probe(local)          # gone/replaced: never answer from a stale probe
        return {"ok": True, "event": ev, "forgot": bool(local)}
    if ev not in IMPORT_EVENTS or not mid:
        return {"ok": True, "ignored": ev or "no eventType"}

    def _run():
        cfg = core.load_config()
        m = Radarr(cfg["radarr_url"], cfg["radarr_key"]).movie(mid)
        r = pipeline.ingest_movie(m, cfg, refresh=True)
        core.log(f"hook radarr: {ev} {m.get('title')!r} -> {r}")
        if r == "gap":
            _kick_search(cfg, lambda c: pipeline.stage_search(c))
    _hook_after(_run, f"radarr {ev} {mid}")
    return {"ok": True, "event": ev, "movie": mid, "queued": True}


@api.post("/hook/sonarr")
def hook_sonarr(body: dict, token: str | None = None):
    """Sonarr Connect -> Webhook. Point it at http://<vo-merge>/api/hook/sonarr."""
    from . import tv
    _hook_auth(token)
    ev = str(body.get("eventType") or "").lower()
    sid = (body.get("series") or {}).get("id")
    if ev == "test":
        core.log("hook sonarr: test OK")
        return {"ok": True, "test": True}
    if ev in DELETE_EVENTS:
        path = (body.get("episodeFile") or {}).get("path")
        local = media.to_media(path, core.load_config()) if path else None
        if local:
            core.forget_probe(local)
        return {"ok": True, "event": ev, "forgot": bool(local)}
    if ev not in IMPORT_EVENTS or not sid:
        return {"ok": True, "ignored": ev or "no eventType"}

    def _run():
        cfg = core.load_config()
        # one targeted pass over just this series: the probe cache means only the file that
        # actually changed costs an mkvmerge call
        n = tv.scan(cfg, only_series=sid, refresh=True)
        core.log(f"hook sonarr: {ev} series {sid} -> {n} gap(s)")
        if n:
            _kick_search(cfg, lambda c: tv.stage_search(c))
    _hook_after(_run, f"sonarr {ev} {sid}")
    return {"ok": True, "event": ev, "series": sid, "queued": True}


def _kick_search(cfg, run):
    """Search for the record we just ingested instead of waiting for the timer. Honours the
    same brakes as everything else — paused, a running scan, and the in-flight download cap
    (stage_search enforces that itself)."""
    if not cfg.get("enabled"):
        return
    why = pipeline.hold_reason(cfg)
    if why:
        core.log(f"hook: not searching yet ({why})"); return
    if not pipeline.SEARCH_LOCK.acquire(blocking=False):
        return                                   # a search run is already covering the backlog
    try:
        run(cfg)
    finally:
        pipeline.SEARCH_LOCK.release()


class PauseIn(BaseModel):
    on: bool


@api.post("/pause")
def set_pause(body: PauseIn):
    """Temporary brake: stop starting NEW searches, grabs and merges. Work already in flight is
    left to finish — killing mkvmerge mid-write would leave a corrupt library file — so the load
    drops as the current merge ends rather than instantly. Scans keep running, which is the
    point: pause is how you let a library re-read finish before anything grabs off it."""
    core.save_config({"paused": bool(body.on)})
    core.log("PAUSED by operator" if body.on else "resumed by operator")
    return {"ok": True, "paused": bool(body.on), "in_flight": pipeline._merging_now()}


@api.post("/search_all")
def search_all():
    """Run the search stage NOW over every pending record, instead of waiting for the search
    timer. Honours the in-flight download cap, so it grabs at most the number of free slots —
    the rest of the backlog stays pending for the next run."""
    import threading
    from . import tv

    if not pipeline.SEARCH_LOCK.acquire(blocking=False):
        return {"ok": True, "started": False, "note": "a search run is already in progress"}
    why = pipeline.hold_reason()
    if why:
        pipeline.SEARCH_LOCK.release()
        return {"ok": True, "started": False,
                "note": ("paused — resume first" if why == "paused"
                         else "a library re-read is running; searching would grab off a "
                              "half-finished scan")}

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
         "read_this_first": f"GET /movie/{tmdb_id}/context — the record, a probe of BOTH files "
                            "(fps/duration/audio tracks) and the matching log lines. Comparing "
                            "the two probes is the diagnosis for most sync and 'nothing to add' "
                            "failures.",
         "actions": [
             "GET  /movie/{id}/context — probes of both files + the relevant log lines",
             "GET  /movie/{id}/candidates — list releases (incl. already-tried)",
             "POST /movie/{id}/sync {\"offset_ms\":0} — re-run auto sync-detect + merge",
             "POST /movie/{id}/sync_probe {\"max_lag_s\":300} — MEASURE the offset and report "
             "it WITHOUT merging, searching much further out than the merge path does. On any "
             "\"couldn't sync\" this is the call to make FIRST: the merge path only searches "
             "+/-sync_max_lag_s, so a consistent offset beyond that reads as \"different cut\" "
             "when it is really a sponsor card or a 'previously on'. Returns every window's own "
             "answer, so a real re-edit (windows disagree) looks different from a large constant "
             "offset (windows agree). Add \"apply\":true to merge with what it finds.",
             "POST /movie/{id}/set_sync {\"offset_ms\":N,\"drift\":1.0427083} — apply a KNOWN "
             "offset and/or rate stretch with no detection (drift = donor_fps/base_fps; "
             "1.0427083 is film->PAL). Use when /context shows the two files' fps differ.",
             "POST /movie/{id}/another — blocklist current release, grab next best",
             "POST /movie/{id}/research — search again (keeps blocklist)",
             "POST /search_releases {\"query\":\"...\"} — run an ARBITRARY Prowlarr query and "
             "grab from the results. The built-in search composes its own query from the library "
             "title, so a title it never matches can never be found however often it re-searches; "
             "try the original/romaji/alternate title with no year.",
             "POST /movie/{id}/unfixable {\"reason\":\"...\"} — terminal give-up WITH a recorded "
             "reason (preferred over /ignore, which reads as an unexamined skip)",
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
         "read_this_first": f"GET /episode/{ep_id}/context — the record, a probe of both files, "
                            "the matching log lines, EVERY donor file with the (season, episode) "
                            "parsed from it, and the series' episode list. Comparing those two "
                            "lists IS the diagnosis for a numbering mismatch.",
         "actions": [
             "GET  /episode/{id}/context — probes, log lines, donor files vs the episode list",
             "GET  /episode/{id}/candidates — list releases (incl. already-tried)",
             "POST /episode/{id}/retry — blocklist current release, drop donor, re-search",
             "POST /episode/{id}/assign {\"path\":\"/abs/file.mkv\"} — map one donor file to this "
             "episode and queue the merge (when automatic numbering translation can't apply)",
             "POST /episode/{id}/sync_probe {\"max_lag_s\":300} — MEASURE the offset without "
             "merging, searching further out than the merge path does. Make this call FIRST on "
             "any \"couldn't sync\": windows that AGREE on a large offset mean extra material at "
             "the head (fixable with --sync), windows that DISAGREE mean a genuinely different "
             "cut (not fixable). Add \"apply\":true to merge with what it finds.",
             "POST /episode/{id}/set_sync {\"offset_ms\":N,\"drift\":1.0427083} — apply a known "
             "offset / rate stretch with no detection",
             "POST /search_releases {\"query\":\"...\"} — arbitrary Prowlarr query (original/"
             "romaji/alternate title, no year) for the no_release backlog",
             "POST /episode/{id}/unfixable {\"reason\":\"...\"} — terminal give-up WITH a reason",
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


@api.get("/ai_log")
def ai_log(outcome: str = "resolved", limit: int = 50):
    """What the on-call AI has actually reported back, newest first.

    Needed as its own query because a callback deliberately leaves the pipeline `status`
    untouched: a record the AI fixed has usually MOVED ON (back to pending, or downloading, or
    merged), so nothing that selects by pipeline status can find it. The Review tab lists
    problems by status and would therefore never show a single thing the AI solved — the work
    would be invisible exactly when it succeeded.

    `outcome`: resolved | failed | needs_human | all. Only records that carry an `ai_at` are
    returned, so the 60-min no-callback flip (which stamps `needs_human` with the sweep's own
    timestamp) can still be told apart by its verdict text."""
    if outcome not in _AI_STATUSES + ("all",):
        raise HTTPException(422, f"outcome must be one of {_AI_STATUSES + ('all',)}")
    where = "ai_at IS NOT NULL" + ("" if outcome == "all" else " AND ai_status=?")
    args = () if outcome == "all" else (outcome,)
    lim = max(1, min(limit, 500))
    out = []
    with core.db() as c:
        for r in c.execute(f"SELECT * FROM movies WHERE {where} "
                           f"ORDER BY ai_at DESC LIMIT {lim}", args):
            out.append({"kind": "movie", "key": str(r["tmdb_id"]), "title": r["title"],
                        "sub": str(r["year"] or ""), "status": r["status"],
                        "ai_status": r["ai_status"], "ai_verdict": r["ai_verdict"],
                        "ai_at": r["ai_at"], "error": r["error"], "poster": r["poster"]})
        for r in c.execute(f"SELECT * FROM episodes WHERE {where} "
                           f"ORDER BY ai_at DESC LIMIT {lim}", args):
            out.append({"kind": "episode", "key": r["id"], "title": r["series_title"],
                        "sub": f"S{r['season']:02d}E{r['episode']:02d}", "status": r["status"],
                        "ai_status": r["ai_status"], "ai_verdict": r["ai_verdict"],
                        "ai_at": r["ai_at"], "error": r["error"], "poster": r["poster"]})
    out.sort(key=lambda x: x["ai_at"] or 0, reverse=True)
    counts = {}
    with core.db() as c:
        for t in ("movies", "episodes"):
            for r in c.execute(f"SELECT ai_status, COUNT(*) n FROM {t} "
                               "WHERE ai_at IS NOT NULL GROUP BY ai_status"):
                counts[r["ai_status"]] = counts.get(r["ai_status"], 0) + r["n"]
    return {"items": out[:lim], "counts": counts, "now": time.time()}


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


class SyncProbeIn(BaseModel):
    max_lag_s: int = 300       # how far out to look; the merge path uses sync_max_lag_s (120)
    windows: int | None = None
    apply: bool = False        # merge with the result if one is found confidently


def _sync_probe(rec, base_key, donor_key, body):
    """MEASURE the offset between a record's two files and report it, without merging.

    The gap this fills: the AI could re-run detection (`/sync`, which just fails the same way) or
    apply an offset it had no way to obtain (`/set_sync`). It had no way to ASK what the offset
    is. So on a "couldn't sync" it could only guess or give up — which is why those tickets come
    back as "different cut, manual pick or ignore" even when the pair is a plain constant offset
    further out than the merge path searches.

    Returns every window's own answer, not just the verdict, so a real disagreement (a genuinely
    different cut) is visibly different from a consistent offset the merge path refused."""
    from . import sync as _sync
    cfg = dict(core.load_config(), sync_max_lag_s=max(5, min(body.max_lag_s, 900)))
    if body.windows:
        cfg["sync_windows"] = max(2, min(body.windows, 12))
    base, donor = rec.get(base_key), rec.get(donor_key)
    if not (base and donor and os.path.exists(base) and os.path.exists(donor)):
        raise HTTPException(409, "both the library file and the donor must exist on disk "
                                 f"(library={base!r} donor={donor!r})")
    bi, di = media.probe(base), media.probe(donor)
    if not bi or not di:
        raise HTTPException(409, "could not probe one of the files")
    off, conf, method, drift = _sync.detect(
        base, donor, 0, 0, bi["dur"] or di["dur"] or 0, cfg, tag=" probe")
    return {"offset_ms": off, "confidence": conf, "method": method, "drift": drift,
            "searched_lag_s": cfg["sync_max_lag_s"],
            "library": {"path": base, "fps": bi["fps"], "dur": bi["dur"]},
            "donor": {"path": donor, "fps": di["fps"], "dur": di["dur"]},
            "duration_delta_s": round(abs((bi["dur"] or 0) - (di["dur"] or 0)), 1),
            "hint": ("a duration difference with a consistent offset is extra material at the "
                     "head or tail (sponsor card, 'previously on'), which --sync fixes; a "
                     "duration difference with NO consistent offset is material inserted in the "
                     "middle, i.e. a genuinely different cut that no single offset can align")}


@api.post("/movie/{tmdb_id}/sync_probe")
def movie_sync_probe(tmdb_id: int, body: SyncProbeIn):
    """Measure the offset between this movie's library file and its donor, searching further out
    than the merge path does. Set `apply` to merge with the result when one is found."""
    mv = core.get_movie(tmdb_id)
    if not mv:
        raise HTTPException(404, "unknown movie")
    out = _sync_probe(mv, "french_path", "en_file", body)
    core.log(f"sync_probe {tmdb_id}: {out['offset_ms']}ms conf={out['confidence']} "
             f"method={out['method']} (searched +/-{out['searched_lag_s']}s)")
    if body.apply and out["offset_ms"] is not None:
        _set_sync("movie", mv, tmdb_id, SetSyncIn(offset_ms=int(out["offset_ms"]),
                                                  drift=out["drift"]))
        pipeline.merge_movie(tmdb_id)
        out["applied"] = True
    return out


@api.post("/episode/{ep_id}/sync_probe")
def episode_sync_probe(ep_id: str, body: SyncProbeIn):
    """Episode mirror of movie_sync_probe."""
    from . import tv
    e = core.get_episode(ep_id)
    if not e:
        raise HTTPException(404, "unknown episode")
    out = _sync_probe(e, "french_path", "en_file", body)
    core.log(f"sync_probe {ep_id}: {out['offset_ms']}ms conf={out['confidence']} "
             f"method={out['method']} (searched +/-{out['searched_lag_s']}s)")
    if body.apply and out["offset_ms"] is not None:
        _set_sync("episode", e, ep_id, SetSyncIn(offset_ms=int(out["offset_ms"]),
                                                 drift=out["drift"]))
        tv.merge_ready_episode(ep_id)
        out["applied"] = True
    return out


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

        # "Needs attention" means NEEDS YOU — not "something failed". The on-call AI is given
        # every failure within 3 min, so a panel listing all of them is mostly a list of things
        # already being worked. Show only what has come back from the AI unresolved
        # (failed / needs_human, which includes the 60-min no-callback flip) plus `review`, which
        # by definition is a human decision. Anything still with the AI is counted, not listed.
        # With ai_tickets off nothing would ever reach those states, so fall back to everything.
        NEEDS_YOU = ("ai_status IN ('failed','needs_human') OR status='review'"
                     if cfg.get("ai_tickets", True) else "1=1")
        working = sum(c.execute(
            f"SELECT COUNT(*) n FROM {t} WHERE status IN ({qn}) AND ai_status='pending'",
            ATTN).fetchone()["n"] for t in ("movies", "episodes"))

        attention = [{"kind": "movie", "key": f"m{r['tmdb_id']}", "title": r["title"],
                      "status": r["status"], "error": r["error"], "count": 1,
                      "sync_delta": r["sync_delta"], "poster": r["poster"], "ts": r["updated"],
                      "ai_status": r["ai_status"], "ai_verdict": r["ai_verdict"]}
                     for r in c.execute(f"SELECT * FROM movies WHERE status IN ({qn}) "
                                        f"AND ({NEEDS_YOU}) ORDER BY updated DESC LIMIT 8", ATTN)]
        # Episodes fail in packs: one bad season pack puts 30 identical rows in a row, and with a
        # flat LIMIT 8 those 30 crowd every other problem off the panel. Group a series' episodes
        # that share a status+error into ONE row spanning their episode range, then take 8 groups.
        from . import tv as _tv
        groups = {}
        # Read them ALL before grouping. A flat LIMIT here is a window over the most recently
        # updated rows, so with ~400 failing episodes (which is normal after one bad season) the
        # window silently cut groups off the panel, and any write that touched a row changed
        # which ones were visible. Grouping a few thousand rows in Python is nothing; the cap is
        # only a runaway backstop.
        for r in c.execute(f"SELECT * FROM episodes WHERE status IN ({qn}) "
                           f"AND ({NEEDS_YOU}) ORDER BY updated DESC LIMIT 5000", ATTN):
            g = groups.setdefault((r["series_title"], r["status"], r["error"], r["ai_status"]),
                                  {"eps": [], "row": r})
            g["eps"].append((r["season"], r["episode"]))
        for (title, status, err, ai), g in groups.items():
            r = g["row"]
            span = _tv.fmt_se(g["eps"], max_parts=3)
            attention.append({
                "kind": "episode", "key": f"e{r['id']}", "count": len(g["eps"]),
                "title": f"{title} {span}" if len(g["eps"]) == 1 else f"{title} · {span}",
                "status": status, "error": err, "sync_delta": r["sync_delta"],
                "poster": r["poster"], "ts": r["updated"],
                "ai_status": ai, "ai_verdict": r["ai_verdict"]})
        attention = sorted(attention, key=lambda x: x["ts"] or 0, reverse=True)[:8]

        # 'merged' is the terminal state for THREE different outcomes, and only two are work we
        # did: grafted (we added tracks), replaced (the download became the library file), and
        # already (the file met its profile on its own — the scan just closed the record out).
        # Counting all three made a library re-read look like thousands of merges in a day, and
        # filled "recently merged" with titles vo-merge never touched. Legacy rows have no
        # merge_kind, so fall back to "did we record adding anything?".
        # Require EVIDENCE that the file changed, not just a label saying so:
        #   - `replaced`: the download became the library file. Real work, and it legitimately
        #     records no added languages, so it can only be recognised by its merge_kind.
        #   - anything that recorded an added audio or subtitle language: that IS the evidence,
        #     and it covers legacy rows written before merge_kind existed.
        # A 'grafted' row with nothing recorded as added is a contradiction — it adds no
        # information about what the app did, so it stays out. `already` (the scan closing out a
        # file that was correct on its own) is excluded by both clauses, which is the point.
        DID_WORK = ("(merge_kind = 'replaced' OR COALESCE(added_langs,'') != '' "
                    "OR COALESCE(added_subs,'') != '')")
        recent = [{"kind": "movie", "title": r["title"], "langs": r["added_langs"],
                   "subs": r["added_subs"], "how": r["merge_kind"] or "grafted",
                   "poster": r["poster"], "ts": r["merged_at"] or r["updated"]}
                  for r in c.execute(f"SELECT * FROM movies WHERE status='merged' AND {DID_WORK} "
                                     "ORDER BY COALESCE(merged_at, updated) DESC LIMIT 10")]
        recent += [{"kind": "episode",
                    "title": f"{r['series_title']} S{r['season']:02d}E{r['episode']:02d}",
                    "langs": r["added_langs"], "subs": r["added_subs"],
                    "how": r["merge_kind"] or "grafted", "poster": r["poster"],
                    "ts": r["merged_at"] or r["updated"]}
                   for r in c.execute(f"SELECT * FROM episodes WHERE status='merged' AND {DID_WORK} "
                                      "ORDER BY COALESCE(merged_at, updated) DESC LIMIT 10")]
        recent = sorted(recent, key=lambda x: x["ts"] or 0, reverse=True)[:10]

        def merged_since(secs):
            return sum(c.execute(f"SELECT COUNT(*) n FROM {t} WHERE status='merged' AND {DID_WORK} "
                                 "AND COALESCE(merged_at, updated) >= ?",
                                 (now - secs,)).fetchone()["n"] for t in ("movies", "episodes"))
        merged_24h, merged_7d = merged_since(86400), merged_since(7 * 86400)
        # how the whole 'merged' population breaks down, so the tile can say what it means
        mk = {"grafted": 0, "replaced": 0, "already": 0}
        for t in ("movies", "episodes"):
            for r in c.execute(f"SELECT merge_kind, COUNT(*) n FROM {t} WHERE status='merged' "
                               "GROUP BY merge_kind"):
                mk[r["merge_kind"] if r["merge_kind"] in mk else "grafted"] += r["n"]

        # Is the on-call AI actually doing anything? The dispatcher runs on the host, outside
        # this app, so the only evidence we have is whether it calls back. `resolved` is the
        # only outcome it produced itself; `needs_human` is mostly the 60-min no-callback flip,
        # so a wall of needs_human with last_callback=None means the dispatcher never ran at all
        # — which looks identical to "the AI examined everything and gave up" unless it is
        # reported separately.
        ai = {"pending": 0, "resolved": 0, "failed": 0, "needs_human": 0, "never_sent": 0}
        last_cb = None
        for t in ("movies", "episodes"):
            for r in c.execute(f"SELECT ai_status, COUNT(*) n, MAX(ai_at) last FROM {t} "
                               f"WHERE status IN ({qn}) GROUP BY ai_status", ATTN):
                k = r["ai_status"] or "never_sent"
                if k in ai:
                    ai[k] += r["n"]
                if r["ai_status"] in ("resolved", "failed") and r["last"]:
                    last_cb = max(last_cb or 0, r["last"])   # a real verdict, not the stale flip
        ai["last_callback"] = last_cb
        ai["enabled"] = bool(cfg.get("ai_tickets", True))
        ai["stale_min"] = cfg.get("ai_stale_min", 60)

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
            "ai_working": working, "ai": ai,
            "merged_24h": merged_24h, "merged_7d": merged_7d, "merged_kinds": mk,
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
