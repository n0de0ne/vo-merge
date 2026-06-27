"""The pipeline: scan (Radarr gap) -> search/score (Prowlarr) -> grab (qB)
-> merge (mkvmerge, sync-gated) -> finish (library swap + Radarr rescan).

Search & merge logic is ported verbatim from the validated dry-run scripts.
"""
import json, os, re, subprocess, shutil, time, hashlib, threading
import requests
from . import core, sync
from .clients import Prowlarr, Radarr, QBittorrent, Plex

# Only ONE merge (sync-detect + mux) runs at a time across the whole app, no matter how it's
# triggered (scheduler, resume, or the API), so concurrent merges can't peg the CPU/GPU.
MERGE_LOCK = threading.Lock()
# Only ONE finish cycle runs at a time — concurrent triggers (scheduler + API) would each keep
# their own per-pack offset cache and duplicate work. Callers acquire non-blocking and skip.
FINISH_LOCK = threading.Lock()

FR_DUB = re.compile(r'\b(VFF|VFQ|VFI|VF2|TRUEFRENCH|FRENCH|VFNF)\b', re.I)
EN_OK  = re.compile(r'\b(MULTI|VOSTFR|VOST|ENGLISH|VO)\b', re.I)
# strong signals the release actually carries an English track (esp. anime: "Dual Audio" =
# Japanese + English). Used to boost/prefer English-bearing releases.
EN_AUDIO = re.compile(r'(DUAL[\s._-]?AUDIO|\bDUAL\b|\bENG\b|\bENGLISH\b)', re.I)
RES    = re.compile(r'(2160p|1080p|720p|480p)', re.I)
SRC    = re.compile(r'(blu-?ray|bdrip|brrip|web-?dl|webrip|hdtv|dvdrip|remux)', re.I)
VIDEXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts")

# original-language name (Radarr/Sonarr) -> audio-track codes ffprobe may report (ISO-639-2 B/T)
_ORIG3 = {
    "english":{"eng"}, "french":{"fre","fra"}, "norwegian":{"nor"}, "japanese":{"jpn"},
    "korean":{"kor"}, "spanish":{"spa"}, "german":{"ger","deu"}, "italian":{"ita"},
    "portuguese":{"por"}, "russian":{"rus"}, "chinese":{"chi","zho"}, "mandarin":{"chi","zho"},
    "cantonese":{"chi","zho"}, "dutch":{"dut","nld"}, "swedish":{"swe"}, "danish":{"dan"},
    "finnish":{"fin"}, "polish":{"pol"}, "turkish":{"tur"}, "thai":{"tha"}, "arabic":{"ara"},
    "hindi":{"hin"}, "czech":{"cze","ces"}, "greek":{"gre","ell"}, "hungarian":{"hun"},
    "romanian":{"rum","ron"}, "ukrainian":{"ukr"}, "icelandic":{"ice","isl"}, "hebrew":{"heb"},
    "indonesian":{"ind"}, "vietnamese":{"vie"},
}
def _orig_codes(name):
    """Audio-track codes for a title's original language, EXCLUDING English/French (those are
    handled directly). Empty if the original is English/French/unknown."""
    s = _ORIG3.get((name or "").strip().lower(), set())
    return s - {"eng", "fre", "fra"}


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


def _infohash(data):
    """v1 infohash = SHA1 of the bencoded info dict (matches qB's lowercase hash)."""
    try:
        i = data.index(b"4:info") + 6
        def skip(p):
            c = data[p:p+1]
            if c.isdigit():
                colon = data.index(b":", p); return colon + 1 + int(data[p:colon])
            if c == b"i":
                return data.index(b"e", p) + 1
            if c in (b"l", b"d"):
                p += 1
                while data[p:p+1] != b"e":
                    p = skip(p)
                return p + 1
            raise ValueError("bad bencode")
        return hashlib.sha1(data[i:skip(i)]).hexdigest().lower()
    except Exception:
        return None


def _fetch_torrent(link):
    """Resolve a release link OURSELVES — vo-merge can reach LAN Prowlarr; qB behind the
    VPN killswitch cannot. Returns ('magnet', uri) or ('file', torrent_bytes)."""
    if not link:
        raise RuntimeError("empty link")
    if link.startswith("magnet:"):
        return "magnet", link
    url = link
    for _ in range(6):                       # follow redirects manually (requests can't follow magnet:)
        resp = requests.get(url, allow_redirects=False, timeout=60)
        loc = resp.headers.get("Location", "")
        if loc.startswith("magnet:"):
            return "magnet", loc
        if resp.status_code in (301, 302, 303, 307, 308) and loc:
            url = loc; continue
        resp.raise_for_status()
        body = resp.content
        if body[:7] == b"magnet:":
            return "magnet", body.decode("utf-8", "ignore").strip()
        return "file", body                  # .torrent (bencoded) bytes
    raise RuntimeError("too many redirects fetching torrent")


def qb_grab(qb, link, category, savepath):
    """Add a release to qB robustly and return its real infohash (or None on failure).
    Fetches the torrent ourselves, uploads it to qB, then confirms by diffing the
    category's hash set before/after (also proves it actually landed)."""
    kind, payload = _fetch_torrent(link)
    before = qb.hashes(category)
    if kind == "magnet":
        qb.add(urls=payload, category=category, savepath=savepath)
        guess = _hash_from_magnet(payload)
    else:
        qb.add(torrent_file=payload, category=category, savepath=savepath)
        guess = _infohash(payload)
    for _ in range(12):                       # ~18s for magnet metadata / torrent registration
        new = qb.hashes(category) - before
        if new:
            return guess if (guess and guess in new) else new.pop()
        if guess and guess in before:         # 409 duplicate: already present
            return guess
        time.sleep(1.5)
    return guess


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
        poster = next((i.get("remoteUrl") or i.get("url") for i in m.get("images", [])
                       if i.get("coverType") == "poster"), None)
        core.upsert_movie({
            "tmdb_id": m["tmdbId"], "imdb_id": m.get("imdbId"), "radarr_id": m["id"],
            "title": m.get("title"), "original_title": m.get("originalTitle") or m.get("title"),
            "year": m.get("year"), "original_lang": lang, "french_path": fr_path,
            "quality": (((mf.get("quality") or {}).get("quality") or {}).get("name")),
            "poster": poster,
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
    if re.search(r'\bMULTI\b', t, re.I): sc += 200   # MULTI = both langs, native sync -> strongly prefer
    return sc


def candidates(tmdb_id, cfg=None, include_tried=False):
    """Scored English/MULTI release candidates for a movie (no grab) — powers the UI's
    interactive search and the auto-picker."""
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    if not mv:
        return []
    pro = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"])
    otitle = mv["original_title"] or mv["title"]; year = mv["year"]
    want_res = (RES.search(mv["quality"] or "") or [None])[0]
    want_src = (SRC.search(mv["quality"] or "") or [None])[0]
    try:
        results = pro.search(otitle, cfg["en_indexer_ids"] + cfg.get("multi_indexer_ids", []))
    except Exception as e:
        core.log(f"candidates {tmdb_id}: {e}"); return []
    import json as _json
    tried = set(_json.loads(mv.get("tried") or "[]"))
    out = []
    for r in results:
        sc = score_release(r, otitle, year, mv["imdb_id"], mv["tmdb_id"], want_res, want_src)
        if sc is None:
            continue
        link = _pick_link(r); rid = _hash_from_magnet(link) or r.get("guid") or r.get("title")
        if rid in tried and not include_tried:
            continue
        out.append({"score": sc, "seeders": r.get("seeders") or 0, "size": r.get("size") or 0,
                    "title": r.get("title"), "indexer": r.get("indexer"),
                    "multi": bool(re.search(r"\bMULTI\b", r.get("title", ""), re.I)),
                    "link": link, "rid": rid, "tried": rid in tried, "info_url": r.get("infoUrl")})
    out.sort(key=lambda x: -x["score"])
    return out


def search_movie(tmdb_id, cfg=None, do_grab=None):
    cfg = cfg or core.load_config()
    if do_grab is None:
        do_grab = (cfg["grab_mode"] == "auto")
    if not core.get_movie(tmdb_id):
        return
    core.set_status(tmdb_id, "searching")
    cand = candidates(tmdb_id, cfg)
    if not cand or cand[0]["score"] < cfg["score_threshold"] or cand[0]["seeders"] < cfg["min_seeders"]:
        core.set_status(tmdb_id, "no_release",
                        candidate_title=(cand[0]["title"] if cand else None),
                        candidate_score=(cand[0]["score"] if cand else 0))
        core.log(f"search {tmdb_id}: no usable release (best={cand[0]['score'] if cand else 'none'})")
        return
    top = cand[0]
    core.set_status(tmdb_id, "grabbed", candidate_title=top["title"],
                    candidate_score=top["score"], candidate_seeders=top["seeders"], dl_id=top["rid"])
    core.log(f"search {tmdb_id}: picked [{top['score']}] {top['seeders']}s {top['title']}")
    if do_grab:
        grab(tmdb_id, top["link"], cfg)


def grab_release(tmdb_id, link, rid=None, title=None, cfg=None):
    """Grab a specific user-chosen release (interactive search)."""
    cfg = cfg or core.load_config()
    fields = {"dl_id": rid, "error": None}
    if title:
        fields["candidate_title"] = title
    core.set_status(tmdb_id, "grabbed", **fields)
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
        h = qb_grab(qb, link, cfg["qb_category"], savepath)
        if not h:
            raise RuntimeError("torrent never appeared in qB (fetch/add failed)")
        core.set_status(tmdb_id, "downloading", dl_hash=h)
        core.log(f"grab tmdb={tmdb_id}: added to qB ({savepath}) hash={h}")
    except Exception as e:
        core.set_status(tmdb_id, "error", error=f"grab: {e}")
        core.log(f"grab tmdb={tmdb_id} FAILED: {e}")


def _is_stalled(t, cfg):
    """A qB torrent is 'stalled' = incomplete, active a while, NOT currently downloading, and
    has no seed source (or qB flags it stalled/errored). Progress level doesn't matter: a
    0-seed download that isn't moving will never finish, whether it's at 5% or 95%."""
    if (t.get("progress", 0) or 0) >= 1.0:
        return False
    if (t.get("time_active", 0) or 0) < cfg.get("stall_timeout_min", 30) * 60:
        return False                              # give it time to find peers first
    if (t.get("dlspeed", 0) or 0) > 0:
        return False                              # still pulling bytes -> not stalled
    seeds = t.get("num_complete", t.get("num_seeds", 0)) or 0   # full-swarm seed count
    return (seeds == 0) or t.get("state") in ("stalledDL", "error", "missingFiles", "metaDL")


def drop_stalled(mv, t, cfg):
    """Delete a stalled download, blocklist that release, and grab another (better-seeded)
    candidate — or give up after max_sync_retries."""
    import json as _json
    tmdb_id = mv["tmdb_id"]
    tried = _json.loads(mv.get("tried") or "[]")
    if mv.get("dl_id") and mv["dl_id"] not in tried:
        tried.append(mv["dl_id"])
    attempts = (mv.get("attempts") or 0) + 1
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        if mv.get("dl_hash"):
            qb.delete([mv["dl_hash"]], delete_files=True)
    except Exception:
        pass
    seeds = t.get("num_complete", t.get("num_seeds", 0)) or 0
    core.log(f"stall {tmdb_id}: '{t.get('name','')[:50]}' stalled "
             f"({int((t.get('time_active',0) or 0)/60)}min, {seeds} seeds) -> blocklisted, re-searching")
    if attempts >= cfg.get("max_sync_retries", 4):
        core.set_status(tmdb_id, "no_release", tried=_json.dumps(tried), attempts=attempts,
                        dl_hash=None, dl_id=None, en_file=None, progress="",
                        error=f"all candidate releases stalled after {attempts} tries")
        return
    core.set_status(tmdb_id, "pending", tried=_json.dumps(tried), attempts=attempts,
                    dl_hash=None, dl_id=None, en_file=None, error=None, progress="")
    search_movie(tmdb_id, cfg)


def reject_and_retry(tmdb_id, reason, cfg=None, delta=None):
    """A grabbed release didn't sync. Blocklist it, delete its download, and re-search for
    another release — or give up (sync_fail) after max_sync_retries."""
    import json as _json
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    tried = _json.loads(mv.get("tried") or "[]")
    if mv.get("dl_id") and mv["dl_id"] not in tried:
        tried.append(mv["dl_id"])
    attempts = (mv.get("attempts") or 0) + 1
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        if mv.get("dl_hash"):
            qb.delete([mv["dl_hash"]], delete_files=True)
    except Exception:
        pass
    if attempts >= cfg.get("max_sync_retries", 4):
        core.set_status(tmdb_id, "sync_fail", tried=_json.dumps(tried), attempts=attempts,
                        sync_delta=delta, error=f"{reason}; no compatible release after {attempts} tries")
        core.log(f"merge {tmdb_id}: giving up after {attempts} tries ({reason})")
    else:
        core.set_status(tmdb_id, "pending", tried=_json.dumps(tried), attempts=attempts,
                        dl_hash=None, dl_id=None, en_file=None, error=None)
        core.log(f"merge {tmdb_id}: {reason} -> trying another release (attempt {attempts}/{cfg.get('max_sync_retries',4)})")


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


def _place_multi(en, mv, cfg, tmdb_id):
    """A MULTI download already carries both languages in native sync — remux to a clean
    .mkv with the library name and place it directly. No merge, no offset, no drift."""
    libfile = mv["french_path"]
    outdir = os.path.dirname(libfile) + "/_merged"
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.splitext(os.path.basename(libfile))[0] + ".mkv")
    r = subprocess.run(["mkvmerge", "-o", out, en], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        core.set_status(tmdb_id, "error", error=f"multi remux rc={r.returncode}: {r.stderr[-200:]}"); return
    core.set_status(tmdb_id, "merged", merged_file=out, added_langs="", error=None)
    core.log(f"merge {tmdb_id}: MULTI release used directly (both langs, native sync) -> {out}")
    finish_movie(tmdb_id, cfg)


def merge_movie(tmdb_id, cfg=None):
    """Serialize merges (one at a time) so concurrent triggers can't peg CPU/GPU."""
    with MERGE_LOCK:
        return _merge_movie_impl(tmdb_id, cfg)


def _merge_movie_impl(tmdb_id, cfg=None):
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
    # The "wanted" foreign track is English; if this title's original language isn't English
    # and no English exists, the original-language VO is the fallback (e.g. Norwegian Kraken).
    rel_langs = {a["lang"] for a in ei["auds"]}
    orig_codes = _orig_codes(mv.get("original_lang"))
    # A release is "complete" when it has French + a wanted track (English, or the VO when
    # the release carries no English). Use it DIRECTLY only if its video isn't worse than the
    # library file; otherwise keep the library video and graft the wanted audio (fall through).
    has_wanted = ("eng" in rel_langs) or (bool(orig_codes & rel_langs) and "eng" not in rel_langs)
    if "fre" in rel_langs and has_wanted:
        if _video_quality(en, ei["dur"]) >= _video_quality(fr, fi["dur"]):
            core.set_status(tmdb_id, "merging")
            _place_multi(en, mv, cfg, tmdb_id); return
        core.log(f"merge {tmdb_id}: release is lower-res than the library file -> keeping the "
                 f"library video, grafting its audio instead")
    delta = abs((ei["dur"] or 0) - (fi["dur"] or 0))
    offset = mv.get("sync_offset_ms") or 0
    # Different framerates (e.g. 25 vs 23.976 PAL speedup) need a linear-drift STRETCH, not a
    # constant offset. We no longer reject these outright: the drift detector below corrects
    # them when the sync is confident (high R²), otherwise routes to review.
    fps_diff = not sync.fps_close(ei["fps"], fi["fps"])
    if fps_diff and not offset and not cfg.get("auto_sync", True):
        reject_and_retry(tmdb_id, f"framerate differs ({ei['fps']} vs {fi['fps']}), auto-sync off", cfg, delta)
        return
    # keep the better video; the other source donates its audio
    eq, fq = _video_quality(en, ei["dur"]), _video_quality(fr, fi["dur"])
    if fq >= eq:
        base, bi, donor, di, who = fr, fi, en, ei, "FR"
    else:
        base, bi, donor, di, who = en, ei, fr, fi, "EN"
    have = {a["lang"] for a in bi["auds"]}
    ids, langs, daidx = [], {}, {}
    for ix, a in enumerate(di["auds"]):
        if a["lang"] == "und" or a["lang"] in have:
            continue
        ids.append(a["id"]); langs[a["id"]] = a["lang"]; daidx[a["id"]] = ix; have.add(a["lang"])
    if not ("eng" in have or (orig_codes & have)):
        core.set_status(tmdb_id, "error",
                        error="merge: no English or original-language (VO) audio to add"); return
    if not ids:
        # base already has the wanted audio (English/VO) -> gap already filled (stale vo-gap tag
        # or a prior merge). Mark done instead of erroring on "nothing to add".
        core.set_status(tmdb_id, "merged", merged_file=fr, progress="", error=None, added_langs="")
        core.log(f"merge {tmdb_id}: library already has the wanted audio -> done")
        mirror_to_en(fr, cfg)
        return
    # Multi-point detection: constant offset, linear drift (framerate), or inconsistent (reject).
    drift = None
    if not offset and cfg.get("auto_sync", True):
        core.set_status(tmdb_id, "merging", progress="sync: starting", error=None)
        m, conf, method, drift = sync.detect(
            base, donor, 0, daidx[ids[0]], min(ei["dur"] or 0, fi["dur"] or 0), cfg, tag=f" {tmdb_id}",
            on_progress=lambda msg: core.set_status(tmdb_id, "merging", progress=msg))
        if m is None or (fps_diff and not drift):
            # m is None  -> inconsistent/low-confidence sync.
            # fps_diff & no drift -> framerates differ but only a constant offset was found
            #   (e.g. audio fallback); a constant can't correct frame drift, so don't risk it.
            why = ("framerates differ but no reliable drift could be measured"
                   if (m is not None and fps_diff and not drift)
                   else "low-confidence sync")
            if cfg.get("sync_review", True):
                core.set_status(tmdb_id, "review", sync_delta=delta, progress="",
                                error=f"{why} — review or pick another release")
                core.log(f"merge {tmdb_id}: {why} -> review")
            else:
                reject_and_retry(tmdb_id, f"couldn't sync ({why})", cfg, delta)
            return
        if abs(m) >= 40 or drift:
            offset = int(round(m))
        core.log(f"merge {tmdb_id}: sync {offset:+d}ms"
                 f"{' drift ' + format(drift, '.6f') if drift else ''} ({method} conf {conf:.2f})")
    core.set_status(tmdb_id, "merging", sync_delta=delta, progress="muxing audio…")
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
        if offset or drift:
            arg = f"{i}:{offset}"
            if drift:                          # linear drift correction (lossless timestamp stretch)
                arg += f",{round(drift * 1000000)}/1000000"
            cmd += ["--sync", arg]
    cmd += [donor]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):     # mkvmerge rc=1 = warnings (ok)
        core.set_status(tmdb_id, "error", error=f"mkvmerge rc={r.returncode}: {r.stderr[-300:]}")
        return
    core.set_status(tmdb_id, "merged", merged_file=out, progress="",
                    added_langs=",".join(sorted({langs[i] for i in ids})))
    core.log(f"merge {tmdb_id}: OK video={who} ({bi['dur'] and int(_video_quality(base,bi['dur'])[1]/1000)}kbps "
             f"{_video_quality(base,bi['dur'])[0]}p) added {[langs[i] for i in ids]} -> {out}")
    finish_movie(tmdb_id, cfg)


def resync_movie(tmdb_id, offset_ms=None, cfg=None, shift_lang=None):
    """Re-time the GRAFTED track inside an already-merged file to align with the base
    track (the one in sync with the video). Shifts the `added_langs` track (the donor),
    NOT always English. offset_ms=None auto-detects via multi-window audio consensus."""
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    f = mv.get("merged_file") or mv.get("french_path")
    if not f or not os.path.exists(f):
        core.set_status(tmdb_id, "error", error="resync: file not found"); return
    from .offdet import audio_langs
    langs = audio_langs(f)
    # which track was grafted (and therefore may be misaligned)?
    shift = shift_lang or (mv.get("added_langs") or "eng").split(",")[0]
    shift_pfx = "en" if shift.startswith("en") else "fr" if shift.startswith("fr") else shift[:2]
    try:
        shift_ai = next(i for i, l in enumerate(langs) if l.startswith(shift_pfx))
        ref_ai = next(i for i, l in enumerate(langs) if not l.startswith(shift_pfx))
    except StopIteration:
        core.set_status(tmdb_id, "error", error="resync: need two distinct audio tracks"); return
    ei = probe(f)
    if not offset_ms:
        m, conf = sync.audio_consensus(f, ref_ai, shift_ai, (ei or {}).get("dur") or 0, cfg, tag=f" {tmdb_id}")
        if m is None:
            core.set_status(tmdb_id, "sync_fail",
                            error=f"resync: no confident alignment ({conf:.2f}) — set offset manually")
            return
        offset_ms = int(round(m))
    # shift EVERY audio track of that language (a release can carry 2+, e.g. 5.1 + 2.0)
    j = json.loads(subprocess.run(["mkvmerge", "-J", f], capture_output=True, text=True).stdout)
    shift_ids = [t["id"] for t in j["tracks"]
                 if t["type"] == "audio" and (t["properties"].get("language") or "").lower().startswith(shift_pfx)]
    if not shift_ids:
        core.set_status(tmdb_id, "error", error=f"resync: no {shift_pfx} track"); return
    out = f + ".resync.mkv"
    cmd = ["mkvmerge", "-o", out]
    for sid in shift_ids:
        cmd += ["--sync", f"{sid}:{offset_ms:+d}"]
    cmd += [f]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):
        core.set_status(tmdb_id, "error", error=f"resync mkvmerge rc={r.returncode}"); return
    shutil.move(out, f)
    core.set_status(tmdb_id, "merged", sync_offset_ms=offset_ms, error=None)
    core.log(f"resync {tmdb_id}: shifted {len(shift_ids)} {shift_pfx} track(s) {offset_ms:+d}ms")
    try:
        plex_dir = os.path.dirname(f).replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
        Plex(cfg["plex_url"], cfg["plex_token"]).scan_path(plex_dir)
    except Exception:
        pass


EN_LIBS = {"Films": "Films-EN", "Series": "Series-EN", "Anime": "Anime-EN"}


def mirror_to_en(libfile, cfg=None):
    """A just-merged file now has English -> add its symlink to the matching -EN library and
    refresh that -EN Plex section immediately, so it shows up without waiting for the scheduled
    mirror script. Best-effort; never raises into the merge flow."""
    cfg = cfg or core.load_config()
    try:
        rel = os.path.relpath(libfile, cfg["media_mount"])     # e.g. Films/Movie (2003)/file.mkv
        parts = rel.split(os.sep, 1)
        en_top = EN_LIBS.get(parts[0]) if len(parts) == 2 else None
        if not en_top:
            return
        link = os.path.join(cfg["media_mount"], en_top, parts[1])
        if not os.path.lexists(link):
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(os.path.relpath(libfile, os.path.dirname(link)), link)
        en_dir = os.path.dirname(link).replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
        Plex(cfg["plex_url"], cfg["plex_token"]).scan_path(en_dir)
        core.log(f"mirror: EN symlink + Plex scan for {en_top}/{parts[1]}")
    except Exception as e:
        core.log(f"mirror EN failed for {libfile}: {e}")


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
        mirror_to_en(dest, cfg)        # add to the -EN library + refresh that section now
    except Exception as e:
        core.set_status(tmdb_id, "error", error=f"finish: {e}")


# ---------------------------------------------------------------- STAGE DRIVERS
def stage_search(cfg=None):
    cfg = cfg or core.load_config()
    if not cfg["enabled"]:
        return
    cap = cfg.get("max_search_per_run", 25); n = 0
    for mv in core.get_movies("pending"):
        search_movie(mv["tmdb_id"], cfg); n += 1
        if n >= cap:
            break


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
    all_paths = "".join((t.get("save_path", "") + t.get("content_path", "")) for t in torrents)
    for mv in core.get_movies("downloading"):
        tmdb = str(mv["tmdb_id"])
        t = by_hash.get(mv.get("dl_hash"))
        if not t:   # fall back: match by save/content path containing /<tmdb>
            t = next((x for x in torrents
                      if f"/{tmdb}" in (x.get("save_path", "") + x.get("content_path", ""))), None)
        if not t:
            # torrent vanished from qB (removed/failed) -> re-queue so it searches again
            if f"/{tmdb}" not in all_paths:
                core.set_status(mv["tmdb_id"], "pending", dl_hash=None, dl_id=None, en_file=None, error=None)
                core.log(f"reconcile {mv['tmdb_id']}: download no longer in qB -> re-queued")
            continue
        if t.get("progress", 0) < 1.0:
            if _is_stalled(t, cfg):
                drop_stalled(mv, t, cfg)
            continue
        # qB save dir was <qb_download_dir>/<tmdb>; we see it under downloads_mount/<tmdb>
        local = os.path.join(cfg["downloads_mount"], str(mv["tmdb_id"]))
        vid = _find_video(local) if os.path.isdir(local) else (local if os.path.exists(local) else None)
        if not vid:
            core.log(f"stage_finish {mv['tmdb_id']}: download complete but no video found in {local}")
            continue
        core.set_status(mv["tmdb_id"], "ready", en_file=vid)
        merge_movie(mv["tmdb_id"], cfg)
    # resume merges interrupted by a restart/crash: stuck at 'merging' but not updated recently
    # (an active merge bumps `updated` every window via the progress field).
    for mv in core.get_movies("merging"):
        if time.time() - (mv.get("updated") or 0) < 900:     # <15min -> probably still running
            continue
        en, fr = mv.get("en_file"), mv.get("french_path")
        if en and fr and os.path.exists(en) and os.path.exists(fr):
            core.log(f"resume {mv['tmdb_id']}: stale merge -> re-merging")
            merge_movie(mv["tmdb_id"], cfg)
        else:
            core.set_status(mv["tmdb_id"], "pending", progress="",
                            error="merge interrupted and source file missing")
