"""The pipeline: scan (Radarr gap) -> search/score (Prowlarr) -> grab (qB)
-> merge (mkvmerge, sync-gated) -> finish (library swap + Radarr rescan).

Search & merge logic is ported verbatim from the validated dry-run scripts.
"""
import json, os, re, subprocess, shutil
from . import core
from .clients import Prowlarr, Radarr, QBittorrent, Plex

FR_DUB = re.compile(r'\b(VFF|VFQ|VFI|VF2|TRUEFRENCH|FRENCH|VFNF)\b', re.I)
EN_OK  = re.compile(r'\b(MULTI|VOSTFR|VOST|ENGLISH|VO)\b', re.I)
RES    = re.compile(r'(2160p|1080p|720p|480p)', re.I)
SRC    = re.compile(r'(blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|dvdrip|remux)', re.I)
VIDEXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts")


def _toks(s):
    return set(re.findall(r'[a-z0-9]+', (s or '').lower()))


def _pick_link(r):
    """Prefer a magnet URI (qB needs no fetch — works behind the VPN killswitch);
    fall back to an http .torrent download URL."""
    for k in ("magnetUrl", "guid", "downloadUrl"):
        v = r.get(k)
        if v and str(v).startswith("magnet:"):
            return v
    return r.get("downloadUrl") or r.get("magnetUrl") or r.get("guid")


def _hash_from_magnet(link):
    m = re.search(r'urn:btih:([0-9a-fA-F]{40})', link or "")
    return m.group(1).lower() if m else None


def _clients(cfg):
    return (Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"]),
            Radarr(cfg["radarr_url"], cfg["radarr_key"]),
            QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]),
            Plex(cfg["plex_url"], cfg["plex_token"]))


# ---------------------------------------------------------------- SCAN
def scan(cfg=None):
    cfg = cfg or core.load_config()
    rad = Radarr(cfg["radarr_url"], cfg["radarr_key"])
    tagid = next((t["id"] for t in rad.tags() if t["label"] == cfg["vo_gap_tag"]), None)
    if tagid is None:
        core.log(f"scan: tag '{cfg['vo_gap_tag']}' not found in Radarr"); return 0
    n = 0
    for m in rad.movies():
        if tagid not in m.get("tags", []):
            continue
        lang = (m.get("originalLanguage") or {}).get("name", "?")
        if cfg["exclude_french_origin"] and lang == "French":
            continue
        mf = m.get("movieFile") or {}
        rel = mf.get("relativePath")
        # container path to the FR file under /media (Radarr path -> our media mount)
        fr_path = None
        if rel and m.get("path"):
            # m['path'] is Radarr's movie folder; map its leaf into our media mount
            fr_path = os.path.join(cfg["media_mount"], "Films",
                                   os.path.basename(m["path"].rstrip("/")), rel)
        core.upsert_movie({
            "tmdb_id": m["tmdbId"], "imdb_id": m.get("imdbId"), "radarr_id": m["id"],
            "title": m.get("title"), "original_title": m.get("originalTitle") or m.get("title"),
            "year": m.get("year"), "original_lang": lang, "french_path": fr_path,
            "quality": (((mf.get("quality") or {}).get("quality") or {}).get("name")),
        })
        n += 1
    core.log(f"scan: {n} candidate movies (non-French gap)")
    return n


# ---------------------------------------------------------------- SEARCH + SCORE
def score_release(r, otitle, year, imdb, tmdb, want_res, want_src):
    t = r.get("title", ""); tl = t.lower()
    idok = (imdb and r.get("imdbId") == imdb) or (tmdb and r.get("tmdbId") == tmdb)
    titleok = idok or (_toks(otitle) and
              len(_toks(otitle) & _toks(t)) / max(len(_toks(otitle)), 1) >= 0.6 and
              any(str(year + d) in t for d in (-1, 0, 1)))
    if not titleok:
        return None
    if FR_DUB.search(t) and not EN_OK.search(t):
        return None                      # French-dub-only -> skip
    sc = min(int(r.get("seeders") or 0), 100)
    if want_res and want_res.lower() in tl: sc += 60
    if want_src and re.search(want_src[:3], tl): sc += 30
    if idok: sc += 80
    if re.search(r'\bMULTI\b', t, re.I): sc += 20
    return sc


def search_movie(tmdb_id, cfg=None, do_grab=None):
    cfg = cfg or core.load_config()
    if do_grab is None:
        do_grab = (cfg["grab_mode"] == "auto")
    mv = core.get_movie(tmdb_id)
    if not mv:
        return
    pro = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"])
    core.set_status(tmdb_id, "searching")
    otitle = mv["original_title"] or mv["title"]; year = mv["year"]
    want_res = (RES.search(mv["quality"] or "") or [None])[0]
    want_src = (SRC.search(mv["quality"] or "") or [None])[0]
    try:
        results = pro.search(otitle, cfg["en_indexer_ids"])
    except Exception as e:
        core.set_status(tmdb_id, "error", error=f"search: {e}"); return
    cand = []
    for r in results:
        sc = score_release(r, otitle, year, mv["imdb_id"], mv["tmdb_id"], want_res, want_src)
        if sc is None:
            continue
        cand.append((sc, r.get("seeders") or 0, r.get("title"), _pick_link(r)))
    cand.sort(reverse=True)
    if not cand or cand[0][0] < cfg["score_threshold"] or cand[0][1] < cfg["min_seeders"]:
        core.set_status(tmdb_id, "no_release",
                        candidate_title=(cand[0][2] if cand else None),
                        candidate_score=(cand[0][0] if cand else 0))
        core.log(f"search '{otitle}': no usable release (best={cand[0][0] if cand else 'none'})")
        return
    sc, seeders, title, link = cand[0]
    core.set_status(tmdb_id, "grabbed", candidate_title=title,
                    candidate_score=sc, candidate_seeders=seeders)
    core.log(f"search '{otitle}': picked [{sc}] {seeders}s {title}")
    if do_grab:
        grab(tmdb_id, link, cfg)


# ---------------------------------------------------------------- GRAB
def grab(tmdb_id, link, cfg=None):
    cfg = cfg or core.load_config()
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    mv = core.get_movie(tmdb_id)
    savepath = f"{cfg['qb_download_dir']}/{mv['tmdb_id']}"
    try:
        qb.login()
        qb.create_category(cfg["qb_category"], cfg["qb_download_dir"])
        resp = qb.add(link, cfg["qb_category"], savepath)
        ids = resp.get("added_torrent_ids") if isinstance(resp, dict) else None
        h = (ids[0] if ids else None) or _hash_from_magnet(link)
        core.set_status(tmdb_id, "downloading", dl_hash=h)
        core.log(f"grab tmdb={tmdb_id}: sent to qB ({savepath}) hash={h}")
    except Exception as e:
        core.set_status(tmdb_id, "error", error=f"grab: {e}")
        core.log(f"grab tmdb={tmdb_id} FAILED: {e}")


# ---------------------------------------------------------------- PROBE (local bins)
def _ffprobe(path, args):
    try:
        return subprocess.run(["ffprobe", "-v", "error"] + args + [path],
                              capture_output=True, text=True, timeout=120).stdout.strip()
    except Exception:
        return ""


def _norm(lang):
    if not lang: return "und"
    l = lang.lower()
    return {"en": "eng", "eng": "eng", "english": "eng",
            "fr": "fre", "fra": "fre", "fre": "fre", "french": "fre"}.get(l, l[:3])


def probe(path):
    dur = _ffprobe(path, ["-show_entries", "format=duration", "-of", "default=nk=1:nw=1"])
    fps = _ffprobe(path, ["-select_streams", "v:0", "-show_entries",
                          "stream=avg_frame_rate", "-of", "default=nk=1:nw=1"])
    try: dur = float(dur)
    except Exception: dur = None
    try:
        n, d = fps.split("/"); fps = round(float(n) / float(d), 3) if float(d) else None
    except Exception: fps = None
    try:
        j = subprocess.run(["mkvmerge", "-J", path], capture_output=True,
                           text=True, timeout=120).stdout
        auds = []
        for t in json.loads(j).get("tracks", []):
            if t.get("type") == "audio":
                p = t.get("properties", {})
                auds.append({"id": t["id"], "lang": _norm(p.get("language")),
                             "codec": t.get("codec"), "ch": p.get("audio_channels"),
                             "name": p.get("track_name") or ""})
    except Exception:
        return None
    return {"dur": dur, "fps": fps, "auds": auds}


def _find_video(folder):
    best = None
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith(VIDEXT):
                p = os.path.join(root, f)
                if best is None or os.path.getsize(p) > os.path.getsize(best):
                    best = p
    return best


# ---------------------------------------------------------------- MERGE + FINISH
def merge_movie(tmdb_id, cfg=None):
    """Base = downloaded English release; donor = existing FR file. Add FR/VO audio."""
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    if not mv or not mv.get("en_file") or not mv.get("french_path"):
        core.set_status(tmdb_id, "error", error="merge: missing en_file or french_path"); return
    base, donor = mv["en_file"], mv["french_path"]
    if not (os.path.exists(base) and os.path.exists(donor)):
        core.set_status(tmdb_id, "error", error="merge: file(s) not found on disk"); return
    bi, di = probe(base), probe(donor)
    if not bi or not di:
        core.set_status(tmdb_id, "error", error="merge: probe failed"); return
    if not any(a["lang"] == "eng" for a in bi["auds"]):
        core.set_status(tmdb_id, "error", error="merge: no English audio in base release"); return
    delta = abs((di["dur"] or 0) - (bi["dur"] or 0))
    offset = mv.get("sync_offset_ms") or 0
    sync_ok = (di["dur"] and bi["dur"] and (delta <= cfg["sync_tolerance_s"] or offset)
               and (di["fps"] == bi["fps"] or offset))
    if not sync_ok:
        core.set_status(tmdb_id, "sync_fail", sync_delta=delta)
        core.log(f"merge {tmdb_id}: SYNC FAIL Δ={delta:.2f}s fps {di['fps']} vs {bi['fps']}")
        return
    core.set_status(tmdb_id, "merging", sync_delta=delta)
    have = {a["lang"] for a in bi["auds"]}
    ids, langs = [], {}
    for a in di["auds"]:
        if a["lang"] in ("eng", "und") or a["lang"] in have:
            continue
        ids.append(a["id"]); langs[a["id"]] = a["lang"]; have.add(a["lang"])
    if not ids:
        core.set_status(tmdb_id, "error", error="merge: no new non-English tracks in FR file"); return
    outdir = os.path.dirname(base) + "/_merged"
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.basename(base))
    cmd = ["mkvmerge", "-o", out, base,
           "--no-video", "--no-subtitles", "--no-chapters", "--no-buttons", "--no-track-tags",
           "--audio-tracks", ",".join(str(i) for i in ids)]
    for i in ids:
        cmd += ["--language", f"{i}:{langs[i]}", "--default-track", f"{i}:0"]
        if offset:
            cmd += ["--sync", f"{i}:{offset}"]
    cmd += [donor]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):     # mkvmerge rc=1 = warnings (ok)
        core.set_status(tmdb_id, "error", error=f"mkvmerge rc={r.returncode}: {r.stderr[-300:]}")
        return
    core.set_status(tmdb_id, "merged", merged_file=out)
    core.log(f"merge {tmdb_id}: OK added {[langs[i] for i in ids]} -> {out}")
    finish_movie(tmdb_id, cfg)


def finish_movie(tmdb_id, cfg=None):
    """Swap merged file into the library folder, remove FR-only, trigger Radarr rescan.
    Hardlink-aware: if the FR file has nlink>1 (seeded), we don't delete it, just rename aside."""
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    merged, donor = mv.get("merged_file"), mv["french_path"]
    if not merged or not os.path.exists(merged):
        return
    libdir = os.path.dirname(donor)
    dest = os.path.join(libdir, os.path.basename(merged))
    try:
        shutil.move(merged, dest)
        # remove the old FR-only library file (its seed copy, if any, is a separate path)
        if os.path.exists(donor) and os.path.abspath(donor) != os.path.abspath(dest):
            os.remove(donor)
        core.set_status(tmdb_id, "merged", merged_file=dest)
        if mv.get("radarr_id"):
            Radarr(cfg["radarr_url"], cfg["radarr_key"]).rescan(mv["radarr_id"])
        core.log(f"finish {tmdb_id}: placed {dest}, Radarr rescan queued")
    except Exception as e:
        core.set_status(tmdb_id, "error", error=f"finish: {e}")


# ---------------------------------------------------------------- STAGE DRIVERS
def stage_search(cfg=None):
    cfg = cfg or core.load_config()
    if not cfg["enabled"]:
        return
    for mv in core.get_movies("pending"):
        search_movie(mv["tmdb_id"], cfg)


def stage_finish(cfg=None):
    """Poll qB for completed audio-merge downloads, resolve the EN file, merge."""
    cfg = cfg or core.load_config()
    if not cfg["enabled"]:
        return
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    try:
        qb.login()
        torrents = qb.torrents(cfg["qb_category"])
    except Exception as e:
        core.log(f"stage_finish: qB error {e}"); return
    by_hash = {t["hash"]: t for t in torrents}
    for mv in core.get_movies("downloading"):
        tmdb = str(mv["tmdb_id"])
        t = by_hash.get(mv.get("dl_hash"))
        if not t:   # fall back: match by save/content path containing /<tmdb>
            t = next((x for x in torrents
                      if f"/{tmdb}" in (x.get("save_path", "") + x.get("content_path", ""))), None)
        if not t or t.get("progress", 0) < 1.0:
            continue
        # qB save dir was <qb_download_dir>/<tmdb>; we see it under downloads_mount/<tmdb>
        local = os.path.join(cfg["downloads_mount"], str(mv["tmdb_id"]))
        vid = _find_video(local) if os.path.isdir(local) else (local if os.path.exists(local) else None)
        if not vid:
            core.log(f"stage_finish {mv['tmdb_id']}: download complete but no video found in {local}")
            continue
        core.set_status(mv["tmdb_id"], "ready", en_file=vid)
        merge_movie(mv["tmdb_id"], cfg)
