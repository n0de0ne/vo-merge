"""FastAPI app: REST API + serves the built React SPA."""
import os, subprocess
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import core, scheduler, pipeline
from .clients import Prowlarr, Radarr, QBittorrent, Plex

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
    cfg["plex_token"] = bool(cfg["plex_token"])
    return cfg


class SettingsIn(BaseModel):
    data: dict


@api.post("/settings")
def post_settings(body: SettingsIn):
    # drop masked/unchanged secret placeholders
    d = {k: v for k, v in body.data.items()
         if not (k in ("qb_pass",) and v == "********")
         and not (k in ("prowlarr_key", "radarr_key", "plex_token") and v in (True, False))}
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
    """Generate a ~30s preview: muted downscaled video + the chosen audio track, as
    separate web-playable files so the browser can shift audio vs picture live."""
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
    vid, aud = os.path.join(out, "video.mp4"), os.path.join(out, f"audio_{lang}.m4a")
    subprocess.run(["nice", "-n", "19", "ffmpeg", "-y", "-ss", str(t), "-t", "30", "-i", f,
                    "-an", "-vf", "scale=640:-2", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "26", "-movflags", "+faststart", vid], capture_output=True)
    subprocess.run(["nice", "-n", "19", "ffmpeg", "-y", "-ss", str(t), "-t", "30", "-i", f,
                    "-map", f"0:a:{ai}", "-vn", "-c:a", "aac", "-b:a", "160k", aud], capture_output=True)
    return {"video": f"/api/preview/{tmdb_id}/video.mp4?v={t}",
            "audio": f"/api/preview/{tmdb_id}/audio_{lang}.m4a?v={t}",
            "start": t, "fps": fps, "duration": 30}


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
