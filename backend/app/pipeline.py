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
def _video_quality(path, dur):
    """(height, video_bitrate) — to pick the better-looking source. mkv often omits
    per-stream bitrate, so fall back to filesize/duration."""
    h = _ffprobe(path, ["-select_streams", "v:0", "-show_entries", "stream=height",
                        "-of", "default=nk=1:nw=1"])
    br = _ffprobe(path, ["-select_streams", "v:0", "-show_entries", "stream=bit_rate",
                         "-of", "default=nk=1:nw=1"])
    try: h = int(h)
    except Exception: h = 0
    try: br = int(br)
    except Exception: br = 0
    if not br and dur:
        try: br = int(os.path.getsize(path) * 8 / dur)
        except Exception: br = 0
    return (h, br)


def merge_movie(tmdb_id, cfg=None):
    """Combine the English release and the existing French file into one multi-language
    file. The VIDEO is kept from whichever source has the better picture (higher
    resolution, then bitrate); the other source contributes its audio. The output
    replaces the library file IN PLACE (keeps its name, so Plex/Radarr paths stay valid)."""
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    if not mv or not mv.get("en_file") or not mv.get("french_path"):
        core.set_status(tmdb_id, "error", error="merge: missing en_file or french_path"); return
    en, fr = mv["en_file"], mv["french_path"]
    if not (os.path.exists(en) and os.path.exists(fr)):
        core.set_status(tmdb_id, "error", error="merge: file(s) not found on disk"); return
    ei, fi = probe(en), probe(fr)
    if not ei or not fi:
        core.set_status(tmdb_id, "error", error="merge: probe failed"); return
    # sync gate (duration + framerate)
    delta = abs((ei["dur"] or 0) - (fi["dur"] or 0))
    offset = mv.get("sync_offset_ms") or 0
    sync_ok = (ei["dur"] and fi["dur"] and (delta <= cfg["sync_tolerance_s"] or offset)
               and (ei["fps"] == fi["fps"] or offset))
    if not sync_ok:
        core.set_status(tmdb_id, "sync_fail", sync_delta=delta)
        core.log(f"merge {tmdb_id}: SYNC FAIL Δ={delta:.2f}s fps {ei['fps']} vs {fi['fps']}")
        return
    # keep the better video; the other source donates its audio
    eq, fq = _video_quality(en, ei["dur"]), _video_quality(fr, fi["dur"])
    if fq >= eq:
        base, bi, donor, di, who = fr, fi, en, ei, "FR"
    else:
        base, bi, donor, di, who = en, ei, fr, fi, "EN"
    core.set_status(tmdb_id, "merging", sync_delta=delta)
    have = {a["lang"] for a in bi["auds"]}
    ids, langs, daidx = [], {}, {}
    for ix, a in enumerate(di["auds"]):
        if a["lang"] == "und" or a["lang"] in have:
            continue
        ids.append(a["id"]); langs[a["id"]] = a["lang"]; daidx[a["id"]] = ix; have.add(a["lang"])
    if "eng" not in have:
        core.set_status(tmdb_id, "error", error="merge: no English audio in either file"); return
    if not ids:
        core.set_status(tmdb_id, "error", error="merge: no new audio tracks to add"); return
    # auto-detect the constant A/V offset between base video's audio and the donor track
    # (shared music/SFX cross-correlation) unless a manual offset was set. Applied to the
    # added tracks so they line up with the base video.
    if not offset and cfg.get("auto_sync", True):
        ws, wd = cfg.get("sync_window_start", 300), cfg.get("sync_window_dur", 600)
        m, conf, method = None, 0.0, None
        # PRIMARY: video scene-cut alignment (language-independent, most reliable)
        try:
            from .offdet_video import detect_offset_video_ms
            vm, vc = detect_offset_video_ms(base, donor, start=ws, dur=wd)
            if vm is not None and vc >= cfg.get("sync_video_min_conf", 0.4):
                m, conf, method = vm, vc, "video"
        except Exception as e:
            core.log(f"merge {tmdb_id}: video sync error: {e}")
        # FALLBACK: audio music/SFX cross-correlation
        if m is None:
            try:
                from .offdet import detect_offset_ms
                am, ac = detect_offset_ms(base, 0, donor, daidx[ids[0]], start=ws, dur=wd)
                if am is not None and ac >= cfg.get("auto_sync_min_conf", 0.2):
                    m, conf, method = am, ac, "audio"
            except Exception as e:
                core.log(f"merge {tmdb_id}: audio sync error: {e}")
        if m is not None and abs(m) >= 40:
            offset = int(round(m))
            core.log(f"merge {tmdb_id}: auto-sync {offset:+d}ms ({method} conf {conf:.2f})")
        elif m is not None:
            core.log(f"merge {tmdb_id}: in sync ({m:+.0f}ms {method} conf {conf:.2f})")
        else:
            core.log(f"merge {tmdb_id}: auto-sync inconclusive — merging unshifted")
    # output replaces the LIBRARY (french) file in place — keep its name; force .mkv
    libfile = mv["french_path"]
    outdir = os.path.dirname(libfile) + "/_merged"
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.splitext(os.path.basename(libfile))[0] + ".mkv")
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
    core.log(f"merge {tmdb_id}: OK video={who} ({bi['dur'] and int(_video_quality(base,bi['dur'])[1]/1000)}kbps "
             f"{_video_quality(base,bi['dur'])[0]}p) added {[langs[i] for i in ids]} -> {out}")
    finish_movie(tmdb_id, cfg)


def resync_movie(tmdb_id, offset_ms=None, cfg=None):
    """Re-time the English track inside the already-merged library file. offset_ms=None
    auto-detects (English vs the base/French track); positive = English plays later.
    Operates on the current file (no need for the original sources)."""
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    f = mv.get("merged_file") or mv.get("french_path")
    if not f or not os.path.exists(f):
        core.set_status(tmdb_id, "error", error="resync: file not found"); return
    from .offdet import audio_langs, detect_offset_ms
    langs = audio_langs(f)
    try:
        ref = next(i for i, l in enumerate(langs) if l.startswith("fr"))
        eng = next(i for i, l in enumerate(langs) if l.startswith("en"))
    except StopIteration:
        core.set_status(tmdb_id, "error", error="resync: need both fr+en tracks"); return
    if not offset_ms:
        m, conf = detect_offset_ms(f, ref, f, eng)
        if m is None or conf < cfg.get("auto_sync_min_conf", 0.2):
            core.set_status(tmdb_id, "sync_fail",
                            error=f"resync: low confidence ({conf:.2f}) — set offset manually")
            return
        offset_ms = int(round(m))
    j = json.loads(subprocess.run(["mkvmerge", "-J", f], capture_output=True, text=True).stdout)
    eng_id = next(t["id"] for t in j["tracks"]
                  if t["type"] == "audio" and (t["properties"].get("language") or "").startswith("en"))
    out = f + ".resync.mkv"
    r = subprocess.run(["mkvmerge", "-o", out, "--sync", f"{eng_id}:{offset_ms:+d}", f],
                       capture_output=True, text=True)
    if r.returncode not in (0, 1):
        core.set_status(tmdb_id, "error", error=f"resync mkvmerge rc={r.returncode}"); return
    shutil.move(out, f)
    core.set_status(tmdb_id, "merged", sync_offset_ms=offset_ms, error=None)
    core.log(f"resync {tmdb_id}: applied {offset_ms:+d}ms to English track")
    try:
        plex_dir = os.path.dirname(f).replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
        Plex(cfg["plex_url"], cfg["plex_token"]).scan_path(plex_dir)
    except Exception:
        pass


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
    mergedir = os.path.dirname(merged)
    try:
        shutil.move(merged, dest)
        # remove the old FR-only library file (its seed copy, if any, is a separate path)
        if os.path.exists(donor) and os.path.abspath(donor) != os.path.abspath(dest):
            os.remove(donor)
        try:
            os.rmdir(mergedir)          # clean up now-empty _merged
        except OSError:
            pass
        core.set_status(tmdb_id, "merged", merged_file=dest)
        if mv.get("radarr_id"):
            Radarr(cfg["radarr_url"], cfg["radarr_key"]).rescan(mv["radarr_id"])
        # tell Plex to re-read the changed file so the new audio track shows up
        try:
            plex_dir = libdir.replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
            Plex(cfg["plex_url"], cfg["plex_token"]).scan_path(plex_dir)
            core.log(f"finish {tmdb_id}: placed {dest}; Radarr rescan + Plex scan ({plex_dir}) queued")
        except Exception as e:
            core.log(f"finish {tmdb_id}: placed {dest}; Radarr rescan queued; Plex scan skipped: {e}")
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
