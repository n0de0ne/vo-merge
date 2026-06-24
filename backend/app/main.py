"""FastAPI app: REST API + serves the built React SPA."""
import os
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from . import core, scheduler, pipeline
from .clients import Prowlarr, Radarr, QBittorrent, Plex

app = FastAPI(title="VO Merger")
STATIC = os.environ.get("VO_STATIC", "/app/static")


@app.on_event("startup")
def _startup():
    core.init_db()
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
    core.set_status(tmdb_id, "ready", sync_offset_ms=body.offset_ms)
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
