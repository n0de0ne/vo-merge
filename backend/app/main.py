"""FastAPI app: REST API + serves the built React SPA."""
import os, subprocess
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
    core.set_status(tmdb_id, "pending", error=None); return {"ok": True}


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


@api.get("/logs")
def logs():
    return {"lines": core.tail_log()}


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
    return core.get_episodes(status)


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
    core.set_ep_status(ep_id, "pending", error=None); return {"ok": True}


@api.post("/episode/{ep_id}/ignore")
def ep_ignore(ep_id: str):
    core.set_ep_status(ep_id, "ignored"); return {"ok": True}


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
