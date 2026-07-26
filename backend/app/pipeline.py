"""The pipeline: scan (Radarr gap) -> search/score (Prowlarr) -> grab (qB)
-> merge (mkvmerge, sync-gated) -> finish (library swap + Radarr rescan).

Search & merge logic is ported verbatim from the validated dry-run scripts.
"""
import json, os, re, subprocess, shutil, time, hashlib, threading
from contextlib import contextmanager
import requests
from . import core, sync, media
from .clients import Prowlarr, Radarr, QBittorrent, Plex

class _MergeGate:
    """Admission control for concurrent merges (sync-detect + mux), covering every trigger:
    the merge workers, the resume path and the API. The limit is `max_parallel_merges` and is
    re-read while waiting, so changing it in Settings takes effect without a restart.
    Merging is CPU/iGPU heavy — the default of 1 keeps the old serialized behaviour."""

    def __init__(self):
        self._cv = threading.Condition()
        self._active = 0

    @staticmethod
    def limit(cfg=None):
        try:
            return max(1, int((cfg or core.load_config()).get("max_parallel_merges", 1)))
        except Exception:
            return 1

    def active(self):
        with self._cv:
            return self._active

    @contextmanager
    def slot(self, cfg=None):
        with self._cv:
            while self._active >= self.limit(cfg):
                self._cv.wait(timeout=5)        # re-check a possibly-changed limit
            self._active += 1
        try:
            yield
        finally:
            with self._cv:
                self._active -= 1
                self._cv.notify()


MERGE_GATE = _MergeGate()
# Only ONE finish cycle runs at a time — concurrent triggers (scheduler + API) would each keep
# their own per-pack offset cache and duplicate work. Callers acquire non-blocking and skip.
FINISH_LOCK = threading.Lock()
# Same for a search sweep: hammering the indexers twice over concurrently gets you rate-limited.
SEARCH_LOCK = threading.Lock()
SCAN_LOCK = threading.Lock()   # a library probe takes minutes; never run two at once
# promote_completed runs on its own 1-min timer AND inside stage_finish (every 10 min), so the
# two overlap regularly. Two passes over the same completed torrent race each other's claims,
# and a loser used to read as "this pack contains none of these episodes".
PROMOTE_LOCK = threading.Lock()
def hold_reason(cfg=None):
    """Why no NEW work should start right now, or None. One answer shared by every stage so the
    UI and the pipeline can never disagree:
      "paused"   - the operator pulled the brake (config `paused`)
      "scanning" - a library re-read is in progress. Grabbing off a half-finished scan picks
                   releases for gaps that may not exist and wastes slots the scan is about to
                   re-price, so searches wait for it rather than racing it."""
    cfg = cfg or core.load_config()
    if cfg.get("paused"):
        return "paused"
    if SCAN_LOCK.locked():
        return "scanning"
    return None


# progress of the current/last full rescan, so the UI can show a multi-minute job is alive
SCAN_STATE = {"running": False, "scope": "", "phase": "", "started": 0, "finished": 0,
              "films": None, "episodes": None, "error": None}

# Language detection in release names now lives in media._DUB_MARKERS, which carries a marker
# per language so scoring works for any profile — the old FR_DUB / EN_OK / EN_AUDIO trio only
# knew "French dub" and "English/MULTI" and was blind to every other target.
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


def _qb_to_local(p, cfg):
    """Translate a path as qB reports it (save_path/content_path) into THIS container's view.
    qB's /data == /mnt/nvme/Plex; ours is /media == /mnt/user/Plex (FUSE superset), so a
    /data/.Téléchargements/... donor is readable at /media/.Téléchargements/.... Legacy
    /downloads/* paths are mounted identically in both containers, so they pass through."""
    if not p:
        return p
    if p.startswith("/data/"):
        return cfg.get("media_mount", "/media") + p[len("/data"):]
    return p


def _tracker_is_french(qb, h, cfg):
    """True if any of the torrent's announce URLs matches the `french_trackers` keep-list.
    Those are left seeding (the operator's seed manager owns them); everything else is a
    throwaway English/public donor we delete after the merge."""
    keep = [s.lower() for s in cfg.get("french_trackers", []) if s]
    if not keep or not h:
        return False
    return any(any(s in u.lower() for s in keep) for u in qb.trackers(h))


def _free_donor(qb, h, cfg, tag=""):
    """Delete a finished donor download (with its files) unless it's a French-tracker torrent
    we keep seeding. Best-effort; never raises into the merge flow."""
    if not (cfg.get("delete_donor", True) and h):
        return
    try:
        if _tracker_is_french(qb, h, cfg):
            core.log(f"donor {tag}: French tracker -> left seeding ({str(h)[:12]})")
            return
        qb.delete([h], delete_files=True)
        core.log(f"donor {tag}: deleted with files ({str(h)[:12]})")
    except Exception as e:
        core.log(f"donor {tag}: delete failed ({str(h)[:12]}): {e}")


# The in-flight cap limits DOWNLOADS, not merges. A torrent that reached 100% has stopped
# using bandwidth and a slot: it frees its slot immediately (status leaves 'downloading' for
# 'ready') so new releases keep flowing while the merge queue drains. Counting ready/merging
# here is what let two finished packs squat the cap with the merger idle.
ACTIVE_STATES = ("downloading",)
# ...but the donor FILES must survive until the merge consumes them, so the orphan sweep keeps
# its hands off ready/merging (and review/sync_fail, kept for manual resync).
KEEP_DONOR_STATES = ("downloading", "ready", "merging", "review", "sync_fail")


def _dl_hashes(states):
    hs = set()
    for st in states:
        for m in core.get_movies(st):
            if m.get("dl_hash"): hs.add(str(m["dl_hash"]).lower())
        for e in core.get_episodes(st):
            if e.get("dl_hash"): hs.add(str(e["dl_hash"]).lower())
    return hs


def inflight_downloads(cfg):
    """How many grab slots are occupied = qB torrents (both categories) tied to an ACTIVE
    pipeline record (a season pack counts once). Torrents added <10 min ago count
    unconditionally — the record write may still be in flight right after a grab."""
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
    tors = qb.torrents(cfg["qb_category"]) + qb.torrents(cfg["qb_tv_category"])
    now = time.time()
    hashes = {t["hash"].lower() for t in tors if t.get("hash")}
    recent = {t["hash"].lower() for t in tors if t.get("hash")
              and now - (t.get("added_on") or 0) < 600}
    return len((hashes & _dl_hashes(ACTIVE_STATES)) | recent)


def sweep_orphan_donors(cfg=None):
    """Delete donor torrents (with files) that no record in KEEP_DONOR_STATES owns — the
    owner merged already, errored, or vanished, so nothing will ever consume the donor
    and it only eats disk. Review/sync_fail-owned donors are kept for manual resync.
    30-min age grace covers a grab whose record write is still in flight."""
    cfg = cfg or core.load_config()
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        tors = qb.torrents(cfg["qb_category"]) + qb.torrents(cfg["qb_tv_category"])
    except Exception as e:
        core.log(f"orphan sweep: qB error {e}"); return
    if not tors:
        return
    keep = _dl_hashes(KEEP_DONOR_STATES)
    now = time.time()
    for t in tors:
        h = (t.get("hash") or "").lower()
        if not h or h in keep:
            continue
        if now - (t.get("added_on") or now) < 1800:
            continue
        try:
            qb.delete([h], delete_files=True)
            core.log(f"orphan donor deleted (no active/review owner): {t.get('name','')[:50]}")
        except Exception as e:
            core.log(f"orphan sweep: delete {h[:12]} failed: {e}")


def grab_budget(cfg):
    """Remaining download slots before hitting max_inflight_downloads. 0 = don't grab this cycle.
    Fails safe: if qB can't be reached we return 0 (never flood when we can't see the queue)."""
    cap = cfg.get("max_inflight_downloads", 5)
    try:
        return max(0, cap - inflight_downloads(cfg))
    except Exception as e:
        core.log(f"grab_budget: qB unreachable ({e}) -> holding"); return 0


def _clients(cfg):
    return (Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"]),
            Radarr(cfg["radarr_url"], cfg["radarr_key"]),
            QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]),
            Plex(cfg["plex_url"], cfg["plex_token"]))


# ---------------------------------------------------------------- SCAN
def scan(cfg=None):
    """Find movies whose library file is missing English. Two modes (`scan_mode`):

    "files" (default) — ask Radarr only for METADATA (title/year/tmdb/original language, all of
      which we need to search) and take the gap decision from the FILE, by probing the container
      with mkvmerge. Radarr's `vo-gap` tag and Sonarr's mediaInfo are import-time snapshots that
      go stale and are empty for anything never analysed; the container cannot be stale.
    "tag" — the old behaviour, kept for comparison and as a fallback."""
    cfg = cfg or core.load_config()
    return _scan_files(cfg) if cfg.get("scan_mode", "files") == "files" else _scan_tagged(cfg)


def _radarr_file_path(m, cfg):
    """Container path of a Radarr movie's file. Prefers the absolute path Radarr reports; falls
    back to folder+relativePath. `media.to_media` verifies the result exists, so a path we
    can't map is reported instead of silently becoming a wrong french_path."""
    mf = m.get("movieFile") or {}
    for p in (mf.get("path"),
              (os.path.join(m["path"], mf["relativePath"])
               if m.get("path") and mf.get("relativePath") else None)):
        got = media.to_media(p, cfg)
        if got:
            return got
    return None


def _scan_files(cfg):
    rad = Radarr(cfg["radarr_url"], cfg["radarr_key"])
    tagid = None
    if not cfg.get("scan_all_movies", True):
        tagid = next((t["id"] for t in rad.tags() if t["label"] == cfg["vo_gap_tag"]), None)
        if tagid is None:
            core.log(f"scan: tag '{cfg['vo_gap_tag']}' not found in Radarr"); return 0
    gaps = filled = unmapped = unreadable = 0
    seen = 0
    for m in rad.movies():
        if tagid is not None and tagid not in m.get("tags", []):
            continue
        r = ingest_movie(m, cfg)
        seen += r != "no file"
        gaps += r == "gap"
        filled += r == "filled"
        unmapped += r == "unmapped"
        unreadable += r == "unreadable"
    mount = cfg["media_mount"]
    core.log(f"scan(files): {gaps} gap(s) of {seen} movie(s) probed"
             f"{f', {filled} already filled' if filled else ''}"
             f"{f', {unmapped} path not found under {mount}' if unmapped else ''}"
             f"{f', {unreadable} unreadable' if unreadable else ''}")
    return gaps


def ingest_movie(m, cfg, refresh=False):
    """Decide one Radarr movie's gap from its FILE and record it. Shared by the library scan and
    the Radarr webhook, so an import is judged by exactly the same rules as a sweep.

    Returns a short outcome: "gap" | "filled" | "ok" | "no file" | "unmapped" | "unreadable".
    `refresh=True` bypasses the probe cache — an import or upgrade just rewrote the file, and on
    a fast disk the new one can land within the cache's 1-second mtime tolerance."""
    mf = m.get("movieFile") or {}
    if not (mf.get("id") or mf.get("path")):
        return "no file"                                 # not downloaded yet — nothing to fix
    lang = (m.get("originalLanguage") or {}).get("name", "?")
    if cfg["exclude_french_origin"] and lang == "French":
        return "ok"
    fr_path = _radarr_file_path(m, cfg)
    if not fr_path:
        return "unmapped"
    if refresh:
        core.forget_probe(fr_path)
    auds, subs, err = media.audit(fr_path)
    if err:
        # unreadable != "has no English" — probing failed, so we know nothing. Skipping is
        # the only honest option; the count is logged so a systemic problem is visible.
        return "unreadable"
    kind = media.kind_of(fr_path, lang, cfg)
    miss_a, miss_s = media.gap_langs(auds, subs, kind, cfg)
    need = "+".join([x for x in (("audio" if miss_a else ""), ("subs" if miss_s else "")) if x])
    alangs, slangs = ",".join(sorted(auds)), ",".join(sorted(subs))
    existing = core.get_movie(m["tmdbId"])
    if not need:
        # No gap. Never insert these (a whole library of them would flood the pipeline);
        # if we already track it, the gap is filled — record that instead of re-searching.
        if existing and existing["status"] in ("pending", "no_release", "searching"):
            core.set_status(m["tmdbId"], "merged", added_langs="", progress="", error=None,
                            audio_langs=alangs, sub_langs=slangs, needs="",
                            need_audio="", need_subs="")
            return "filled"
        if existing:
            core.set_status(m["tmdbId"], existing["status"], audio_langs=alangs,
                            sub_langs=slangs, needs="", need_audio="", need_subs="")
        return "ok"
    poster = next((i.get("remoteUrl") or i.get("url") for i in m.get("images", [])
                   if i.get("coverType") == "poster"), None)
    core.upsert_movie({
        "tmdb_id": m["tmdbId"], "imdb_id": m.get("imdbId"), "radarr_id": m["id"],
        "title": m.get("title"), "original_title": m.get("originalTitle") or m.get("title"),
        "year": m.get("year"), "original_lang": lang, "french_path": fr_path,
        "quality": (((mf.get("quality") or {}).get("quality") or {}).get("name")),
        "poster": poster,
    })
    core.set_status(m["tmdbId"], (existing or {}).get("status") or "pending",
                    audio_langs=alangs, sub_langs=slangs, needs=need,
                    need_audio=",".join(miss_a), need_subs=",".join(miss_s))
    return "gap"


def _scan_tagged(cfg):
    """Legacy mode: believe Radarr's vo-gap tag (maintained by the host mirror script)."""
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
        fr_path = _radarr_file_path(m, cfg)
        mf = m.get("movieFile") or {}
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
    core.log(f"scan(tag): {n} candidate movies (non-French gap)")
    return n


# ---------------------------------------------------------------- SEARCH + SCORE
def score_release(r, otitle, year, imdb, tmdb, want_res, want_src, need=()):
    """Score a Prowlarr result for this title, or None to reject it.

    `need` = the audio languages this file is still missing (its `need_audio`), and it drives
    both halves of the language judgement — for ANY language, not just French and English:

    - **reject** a release that advertises dubs and none of them is missing here. The library
      file already carries its own dub, so such a release adds nothing and burns a slot.
    - **boost** a release that names a language this file lacks (+60 each), on top of the MULTI
      bonus. So a MULTI still outranks a single-language dub, and a dub of a language we need
      outranks an unmarked release.

    With no `need` recorded nothing is rejected on language grounds — the conservative default."""
    t = r.get("title", ""); tl = t.lower()
    idok = (imdb and r.get("imdbId") == imdb) or (tmdb and r.get("tmdbId") == tmdb)
    titleok = idok or (_toks(otitle) and
              len(_toks(otitle) & _toks(t)) / max(len(_toks(otitle)), 1) >= 0.6 and
              any(str(year + d) in t for d in (-1, 0, 1)))
    if not titleok:
        return None
    if media.useless_release(t, need, otitle):
        return None                      # advertises only dubs we already have -> adds nothing
    sc = min(int(r.get("seeders") or 0), 100)
    if want_res and want_res.lower() in tl: sc += 60
    if want_src and re.search(want_src[:3], tl): sc += 30
    if idok: sc += 80
    if re.search(r'\bMULTI\b', t, re.I): sc += 200   # several langs, natively synced -> prefer
    sc += 60 * media.lang_hits(t, need, otitle)      # names a language this file is missing
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
    if not _toks(otitle):                 # non-Latin original title (JP/KR/etc.) tokenizes to
        otitle = mv["title"]              # nothing -> query+match on Radarr's English title instead
    want_res = (RES.search(mv["quality"] or "") or [None])[0]
    want_src = (SRC.search(mv["quality"] or "") or [None])[0]
    need = {x for x in (mv.get("need_audio") or "").split(",") if x}
    try:
        results = pro.search(otitle, cfg["en_indexer_ids"] + cfg.get("multi_indexer_ids", []))
    except Exception as e:
        core.log(f"candidates {tmdb_id}: {e}"); return []
    import json as _json
    tried = set(_json.loads(mv.get("tried") or "[]"))
    out = []
    for r in results:
        sc = score_release(r, otitle, year, mv["imdb_id"], mv["tmdb_id"], want_res, want_src,
                           need=need)
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


# qB states that mean "this will not finish on its own". pausedDL/stoppedDL are here
# because nothing in vo-merge can resume a torrent — a paused donor would otherwise hold
# its slot forever. forcedMetaDL is the force-started spelling of metaDL.
DEAD_DL_STATES = ("stalledDL", "error", "missingFiles", "metaDL", "forcedMetaDL",
                  "pausedDL", "stoppedDL")
NO_META_STATES = ("metaDL", "forcedMetaDL")


def _swarm_seeds(t):
    """Seed count, tolerating qB's -1 = 'not scraped yet'. Reading num_complete naively
    makes an unknown swarm (-1) look like a *seeded* one, so a genuinely dead torrent
    never trips the 0-seed test."""
    for k in ("num_complete", "num_seeds"):
        v = t.get(k)
        if isinstance(v, int) and v >= 0:
            return v
    return 0


def _is_stalled(t, cfg):
    """A qB torrent is 'stalled' = incomplete, active a while, NOT currently downloading, and
    has no seed source (or qB flags it dead). Progress level doesn't matter: a
    0-seed download that isn't moving will never finish, whether it's at 5% or 95%.
    An incomplete download that has been active past `dl_max_age_min` is ALSO dropped, even
    if it's still trickling bytes — a release that can't finish in that long isn't worth the slot."""
    if (t.get("progress", 0) or 0) >= 1.0:
        return False
    age = t.get("time_active", 0) or 0
    if age >= cfg.get("dl_max_age_min", 720) * 60:
        return True                               # absolute cap: too old regardless of speed/seeds
    state = t.get("state") or ""
    # A magnet with no metadata yet has nothing to wait for: no peer has ever answered, so
    # there is no download to be slow. Waiting the full stall timeout just parks a dead
    # magnet in a grab slot (the "0% · fetching metadata · 0 seeds" pileup).
    if state in NO_META_STATES and age >= cfg.get("meta_timeout_min", 2) * 60:
        return True
    if age < cfg.get("stall_timeout_min", 5) * 60:
        return False                              # give it time to find peers first
    if (t.get("dlspeed", 0) or 0) > 0:
        return False                              # still pulling bytes -> not stalled
    return (_swarm_seeds(t) == 0) or state in DEAD_DL_STATES


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


_WEDGE_SINCE = {"ts": None}


def ai_health_check(cfg=None):
    """Page the on-call AI (core.ticket -> host dispatcher runs Claude Code) when the
    pipeline is stuck: (a) the in-flight cap is saturated for >2h with NOTHING in
    'downloading' state — dead/orphaned donors are holding every slot (the 2026-07-12
    two-day deadlock); (b) records newly landed in error or review."""
    cfg = cfg or core.load_config()
    if not cfg.get("ai_tickets", True):
        return
    # (a) cap saturated but pipeline idle
    try:
        cap = int(cfg.get("max_inflight_downloads", 5))
        stuck = cap > 0 and inflight_downloads(cfg) >= cap and \
            not core.get_movies("downloading") and not core.get_episodes("downloading")
    except Exception:
        stuck = False
    if stuck:
        if _WEDGE_SINCE["ts"] is None:
            _WEDGE_SINCE["ts"] = time.time()
        elif time.time() - _WEDGE_SINCE["ts"] > 7200:
            tors = []
            try:
                qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
                tors = [{"hash": t["hash"], "state": t["state"],
                         "progress": round(t.get("progress", 0), 2), "name": t["name"][:60]}
                        for t in qb.torrents(cfg["qb_category"]) + qb.torrents(cfg["qb_tv_category"])]
            except Exception:
                pass
            core.ticket("cap-wedged",
                        "in-flight cap saturated >2h with nothing downloading (dead donors hold the slots)",
                        {"torrents": tors}, key=time.strftime("%Y-%m-%d"))
            _WEDGE_SINCE["ts"] = time.time()
    else:
        _WEDGE_SINCE["ts"] = None
    # (b) new error / review / sync_fail records — per-record seen-set so only NEW ones page,
    # and a standing backlog never re-pages when one more record errors. Each newly-paged record
    # is stamped ai_status='pending' so the UI shows "AI working" until the agent reports back.
    now = time.time()
    seen_path = os.path.join(core.CONFIG_DIR, "ai_seen_records.json")
    try:
        seen = set(json.load(open(seen_path)))
    except Exception:
        seen = set()
    news = []
    for st in ("error", "review", "sync_fail"):
        for m in core.get_movies(st):
            rk = f"movie:{m['tmdb_id']}:{st}"
            if rk in seen: continue
            seen.add(rk)
            core.set_status(m["tmdb_id"], st, ai_status="pending", ai_at=now)
            news.append({"type": "movie", "status": st, "id": m["tmdb_id"],
                         "title": m.get("title", ""), "error": (m.get("error") or "")[:200]})
        for e in core.get_episodes(st):
            rk = f"episode:{e['id']}:{st}"
            if rk in seen: continue
            seen.add(rk)
            core.set_ep_status(e["id"], st, ai_status="pending", ai_at=now)
            news.append({"type": "episode", "status": st, "id": e["id"],
                         "title": f"{e.get('series_title','')} S{e.get('season')}E{e.get('episode')}",
                         "error": (e.get("error") or "")[:200]})
    if news:
        json.dump(sorted(seen), open(seen_path, "w"))
        key = hashlib.sha1(",".join(sorted(str(n["id"]) for n in news)).encode()).hexdigest()[:16]
        core.ticket("errors-review",
                    f"{len(news)} NEW record(s) in error/review/sync_fail",
                    {"records": news[:60], "total_new": len(news),
                     "api": "http://10.0.1.5:8090/api (host) / http://localhost:8080/api (in-container)",
                     "report_back": (
                         "After handling each record, POST its outcome so it leaves the operator's "
                         "manual-review queue: movies -> /movie/{id}/ai_result, episodes -> "
                         "/episode/{id}/ai_result, body {\"status\":\"resolved|failed|needs_human\","
                         "\"verdict\":\"one line\",\"action_taken\":\"what you did\"}. "
                         "Use needs_human when a person must decide."),
                     "diagnose_first": (
                         "GET /movie/{id}/context or /episode/{id}/context — the record, a probe "
                         "of both files (fps/duration/audio tracks), the log lines for it, and for "
                         "episodes every donor file with the (season,episode) the parser read plus "
                         "the series' episode list. Read this before acting; it usually IS the "
                         "diagnosis and saves shelling into the container. The episode context also "
                         "carries a `numbering` block (library S/E vs the release's S/E and absolute "
                         "number): vo-merge now translates aired<->absolute itself from Sonarr, so "
                         "`translated: true` means the search and the donor mapping already use the "
                         "aired numbering and a plain /retry is the right move."),
                     "actions": [
                         "GET  /movie|episode/{id}/candidates — scored releases (incl. already-tried)",
                         "POST /movie/{id}/sync {\"offset_ms\":0} — re-run auto sync-detect + merge",
                         "POST /movie|episode/{id}/set_sync {\"offset_ms\":N,\"drift\":1.0427083} — "
                         "apply a KNOWN offset and/or rate stretch with no detection. drift is the "
                         "donor->base ratio = donor_fps/base_fps (25/23.976=1.0427083 film->PAL, "
                         "23.976/25=0.9590410 PAL->film). Use when fps are known but detection failed.",
                         "POST /episode/{id}/assign {\"path\":\"/abs/file.mkv\"} — map ONE donor file "
                         "to this episode and queue the merge. Aired-vs-absolute numbering is now "
                         "handled automatically (Sonarr's absoluteEpisodeNumber), so reach for this "
                         "only when the automatic mapping can't apply — Sonarr has no absolute "
                         "numbers for the series, or the pack numbers its files some third way. "
                         "Read /context, work out the mapping, call this per episode.",
                         "POST /search_releases {\"query\":\"...\"} — arbitrary Prowlarr query, returns "
                         "links. For titles the built-in query never matches, try the original / "
                         "romaji / English / alternate-transliteration name, or drop the year. Then "
                         "act on a result with POST /movie|episode/{id}/grab {\"link\":...}.",
                         "POST /movie|episode/{id}/another | /research | /retry | /ignore",
                         "POST /movie|episode/{id}/unfixable {\"reason\":\"...\"} — give up, recording why"]},
                    key=key)

    # staleness: a record we sent to the AI that never got a callback within ai_stale_min and is
    # STILL in a problem state -> the dispatcher likely crashed/failed silently. Flag it for a human.
    stale_cut = now - cfg.get("ai_stale_min", 60) * 60
    verdict = f"AI did not respond within {cfg.get('ai_stale_min', 60)}m — needs manual review"
    try:
        with core.db() as c:
            stale_m = [dict(r) for r in c.execute(
                "SELECT tmdb_id, status FROM movies WHERE ai_status='pending' AND ai_at < ? "
                "AND status IN ('error','review','sync_fail')", (stale_cut,))]
            stale_e = [dict(r) for r in c.execute(
                "SELECT id, status FROM episodes WHERE ai_status='pending' AND ai_at < ? "
                "AND status IN ('error','sync_fail')", (stale_cut,))]
        for m in stale_m:
            core.set_status(m["tmdb_id"], m["status"], ai_status="needs_human", ai_verdict=verdict)
        for e in stale_e:
            core.set_ep_status(e["id"], e["status"], ai_status="needs_human", ai_verdict=verdict)
        if stale_m or stale_e:
            core.log(f"ai staleness: {len(stale_m) + len(stale_e)} record(s) had no AI callback -> needs_human")
    except Exception as ex:
        core.log(f"ai staleness check failed: {ex}")


def no_seed_public(cfg=None):
    """Public donors never seed: STOP every completed public (qB private=false) torrent
    in both vo-merge categories. A stopped donor's files stay on disk, so the merge
    still consumes it and _free_donor deletes it afterwards — this just closes the
    seeding window while a completed donor waits for its merge slot. Do NOT use
    setShareLimits here: qB's limit-reached action on this box is 'remove torrent AND
    delete content', which destroys the donor before the merge (verified live
    2026-07-11 — the capped donor was deleted and its episodes re-queued).
    French-tracker torrents are left seeding as usual."""
    cfg = cfg or core.load_config()
    if not cfg.get("no_seed_public", True):
        return
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    try:
        qb.login()
        tors = qb.torrents(cfg["qb_category"]) + qb.torrents(cfg["qb_tv_category"])
    except Exception as e:
        core.log(f"no_seed_public: qB error {e}"); return
    stop = [t["hash"] for t in tors
            if t.get("private") is False and t.get("hash")
            and (t.get("progress", 0) or 0) >= 1.0
            and t.get("state") not in ("stoppedUP", "pausedUP")
            and not _tracker_is_french(qb, t["hash"], cfg)]
    if stop:
        try:
            qb.stop(stop)
            core.log(f"no-seed: stopped {len(stop)} completed public donor(s)")
        except Exception as e:
            core.log(f"no_seed_public: stop failed: {e}")


def sweep_stalled(cfg=None):
    """Drop seederless / non-progressing movie downloads and grab another release. Runs on its
    OWN fast timer, NOT inside the finish/merge loop, so a long merge backlog never delays it."""
    cfg = cfg or core.load_config()
    if not cfg["enabled"]:
        return
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    try:
        qb.login()
        by_hash = {t["hash"]: t for t in qb.torrents(cfg["qb_category"])}
    except Exception as e:
        core.log(f"sweep: qB error {e}"); return
    for mv in core.get_movies("downloading"):
        t = by_hash.get(mv.get("dl_hash"))
        if t and (t.get("progress", 0) or 0) < 1.0 and _is_stalled(t, cfg):
            drop_stalled(mv, t, cfg)


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


def retry_movie(tmdb_id, cfg=None):
    """Proper operator-initiated retry, the movie mirror of tv.retry_episode: blocklist the
    release that failed, drop its donor from qB, clear the grab fields and re-queue for a fresh
    search. A bare flip to 'pending' would re-search and can pick the very same release again.
    `attempts` is reset because a human/AI asking for a retry means "try again" — a sync_fail
    record has already spent its budget and would otherwise fail straight back to sync_fail."""
    import json as _json
    cfg = cfg or core.load_config()
    mv = core.get_movie(tmdb_id)
    if not mv:
        return False
    tried = _json.loads(mv.get("tried") or "[]")
    if mv.get("dl_id") and mv["dl_id"] not in tried:
        tried.append(mv["dl_id"])
    if mv.get("dl_hash"):
        try:
            qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
            qb.delete([mv["dl_hash"]], delete_files=True)
            core.log(f"retry {tmdb_id}: dropped failed donor {str(mv['dl_hash'])[:12]}")
        except Exception as ex:
            core.log(f"retry {tmdb_id}: donor drop failed: {ex}")
    core.set_status(tmdb_id, "pending", error=None, tried=_json.dumps(tried), attempts=0,
                    dl_hash=None, dl_id=None, en_file=None, progress="")
    return True


def retry_errors(cfg=None, states=("error", "sync_fail")):
    """Bulk retry every failed movie. Returns how many were re-queued."""
    cfg = cfg or core.load_config()
    n = sum(1 for st in states for m in core.get_movies(st) if retry_movie(m["tmdb_id"], cfg))
    core.log(f"retry-all films: re-queued {n} failed title(s)")
    return n


# ---------------------------------------------------------------- PROBE (local bins)
def _ffprobe(path, args):
    try:
        return subprocess.run(["ffprobe", "-v", "error"] + args + [path],
                              capture_output=True, text=True, timeout=120).stdout.strip()
    except Exception:
        return ""


def probe(path):
    """{dur, fps, auds, subs} for a media file, or None if it can't be read.

    Delegates to `media.probe` so the whole app resolves track languages the same way — one
    canonical code per language, with `und` tracks falling back to their track/file name.
    Duration comes from the container when it states one, else from ffprobe (mkv records it;
    mp4/avi/ts often don't)."""
    info = media.probe(path)
    if info is None:
        return None
    if not info.get("dur"):
        try:
            info["dur"] = float(_ffprobe(path, ["-show_entries", "format=duration",
                                                "-of", "default=nk=1:nw=1"]))
        except Exception:
            info["dur"] = None
    return info


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
    """Cap concurrent merges at `max_parallel_merges` so they can't peg CPU/GPU."""
    with MERGE_GATE.slot(cfg):
        return _merge_movie_impl(tmdb_id, cfg)


def _pick_subs(donor_info, base_info, cfg, kind="movie"):
    """Donor subtitle tracks to graft alongside the audio, per the kind's target profile.
    English subs are missing from most French library files, and the donor we already downloaded
    for its audio usually carries them — so taking them costs one extra mkvmerge argument rather
    than another download."""
    if not cfg.get("want_subs", True):
        return []
    _, want = media.profile(kind, cfg)
    return media.wanted_subs(donor_info, base_info, want, cfg.get("max_sub_tracks", 2))


def _donor_opts(aud_ids, langs, subs, offset, drift):
    """mkvmerge options selecting what the DONOR contributes. These precede the donor filename,
    so they apply to it and not to the base (which keeps its video, audio, subs and chapters).

    Both audio and subtitles get the same --sync: the donor's tracks are timed to the donor's
    video, so a subtitle grafted onto the base drifts exactly as its audio would."""
    sub_ids = [s["id"] for s in subs]
    opts = ["--no-video", "--no-chapters", "--no-buttons", "--no-track-tags",
            "--audio-tracks", ",".join(str(i) for i in aud_ids)]
    opts += ["--subtitle-tracks", ",".join(str(i) for i in sub_ids)] if sub_ids else ["--no-subtitles"]
    for i in aud_ids:
        opts += ["--language", f"{i}:{langs[i]}", "--default-track", f"{i}:0"]
    for s in subs:
        # never default-flag a grafted subtitle: it would start burned-in for every viewer
        opts += ["--language", f"{s['id']}:{s['lang']}", "--default-track", f"{s['id']}:0"]
        if s.get("forced"):
            opts += ["--forced-track", f"{s['id']}:1"]
    if offset or drift:
        arg = f"{offset}" + (f",{round(drift * 1000000)}/1000000" if drift else "")
        for i in aud_ids + sub_ids:
            opts += ["--sync", f"{i}:{arg}"]
    return opts


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
    orig_codes = _orig_codes(mv.get("original_lang"))
    # A release is "complete" when it ALONE satisfies this title's audio profile — asked of the
    # probed file, not of the release name. Use it DIRECTLY only if its video isn't worse than
    # the library file; otherwise keep the library video and graft what's missing (fall through).
    # This used to be a literal `"fre" in rel_langs and "eng" in rel_langs`, which is the right
    # question for exactly one library shape and blind to every other profile.
    kind0 = media.kind_of(fr, mv.get("original_lang"), cfg)
    if not media.gap_langs(*media.langs(ei), kind0, cfg)[0]:
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
    # Take only the profile's target languages the base lacks (plus the VO fallback), one track
    # per language — grabbing every new language would bloat the file with dubs nobody asked for.
    kind = media.kind_of(fr, mv.get("original_lang"), cfg)
    want_a, _ = media.profile(kind, cfg)
    picked = media.wanted_audio(di, bi, want_a, extra=orig_codes)
    ids = [a["id"] for a in picked]
    langs = {a["id"]: a["lang"] for a in picked}
    daidx = {a["id"]: ix for ix, a in enumerate(di["auds"]) if a["id"] in set(ids)}
    have |= {a["lang"] for a in picked}
    subs = _pick_subs(di, bi, cfg, kind)
    if not ids and not subs:
        # The donor contributes nothing. Two very different reasons, so decide from the FILE
        # rather than assuming: either the library file already meets its language profile —
        # a stale record or a prior merge, mark it done — or this release simply carried none
        # of the languages still missing, in which case blocklist it and try another. The old
        # check demanded English specifically, which rejected a donor that carried exactly the
        # language the record was short of (French, or a VO).
        still_a, still_s = media.gap_langs(*media.langs(bi), kind, cfg)
        if still_a or still_s:
            reject_and_retry(tmdb_id, "release carries none of the missing languages "
                                      f"(still needs {'+'.join(still_a + still_s)})", cfg, delta)
            return
        core.set_status(tmdb_id, "merged", merged_file=fr, progress="", error=None, added_langs="")
        core.log(f"merge {tmdb_id}: library already meets its language profile -> done")
        en_dir = mirror_to_en(fr, cfg)
        plex_refresh(cfg, [os.path.dirname(fr).replace(cfg["media_mount"], cfg["plex_media_prefix"], 1),
                           en_dir], mv.get("title"), year=mv.get("year"))
        return
    # Multi-point detection: constant offset, linear drift (framerate), or inconsistent (reject).
    # A manually-set offset skips detection. Honour a stored stretch ratio too, so a
    # known rate correction (e.g. a PAL 1.0427 ratio) can be applied by hand via
    # /set_sync when detection can't measure it.
    drift = (mv.get("sync_drift") or None) if offset else None
    if drift and abs(drift - 1.0) < 1e-6:
        drift = None
    if not offset and cfg.get("auto_sync", True):
        core.set_status(tmdb_id, "merging", progress="sync: starting", error=None)
        m, conf, method, drift = sync.detect(
            base, donor, 0, (daidx[ids[0]] if ids else 0),
            min(ei["dur"] or 0, fi["dur"] or 0), cfg, tag=f" {tmdb_id}",
            on_progress=lambda msg: core.set_status(tmdb_id, "merging", progress=msg),
            base_fps=bi.get("fps"), donor_fps=di.get("fps"),
            base_dur=bi.get("dur"), donor_dur=di.get("dur"))
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
    what = "audio" if ids else ""
    if subs:
        what = (what + "+subs") if what else "subs"
    core.set_status(tmdb_id, "merging", sync_delta=delta, progress=f"muxing {what}…")
    # output replaces the LIBRARY (french) file in place — keep its name; force .mkv
    libfile = mv["french_path"]
    outdir = os.path.dirname(libfile) + "/_merged"
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.splitext(os.path.basename(libfile))[0] + ".mkv")
    cmd = ["mkvmerge", "-o", out, base] + \
          _donor_opts(ids, langs, subs, offset, drift) + [donor]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):     # mkvmerge rc=1 = warnings (ok)
        core.set_status(tmdb_id, "error", error=f"mkvmerge rc={r.returncode}: {r.stderr[-300:]}")
        return
    core.set_status(tmdb_id, "merged", merged_file=out, progress="",
                    sync_offset_ms=offset, sync_drift=drift,
                    added_subs=",".join(sorted({s["lang"] for s in subs})),
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
        plex_refresh(cfg, plex_dir, mv.get("title"), year=mv.get("year"))
    except Exception:
        pass


def _plex_targets(cfg):
    """Every configured PMS: the master, plus the optional replica (plex2_*)."""
    out = [Plex(cfg["plex_url"], cfg["plex_token"])]
    if cfg.get("plex2_url"):
        out.append(Plex(cfg["plex2_url"], cfg.get("plex2_token", "")))
    return out


def plex_refresh(cfg, folders, title, year=None, season=None, episode=None):
    """Refresh + analyze an item on ALL configured PMS (master + replica), across ALL the
    libraries that hold it — the FR library AND its -EN mirror. Grafting audio or subtitles
    rewrites the file IN PLACE, and a plain scan does not re-read streams of a file whose path
    and name are unchanged; only `analyze` does. Best-effort per server; never raises into the
    merge flow."""
    res = []
    folders = [f for f in ([folders] if isinstance(folders, str) else folders) if f]
    for p in _plex_targets(cfg):
        host = p.url.split("//")[-1]
        try:
            keys = p.refresh_analyze(folders, title, year=year, season=season, episode=episode)
            res.append(f"{host}={len(keys)} analyzed" if keys else f"{host}=scan-only")
        except Exception as e:
            res.append(f"{host}=ERR:{str(e)[:40]}")
    return res


EN_LIBS = {"Films": "Films-EN", "Series": "Series-EN", "Anime": "Anime-EN"}


def mirror_to_en(libfile, cfg=None):
    """A just-merged file now has English -> add its symlink to the matching -EN library, and
    return that library's folder as Plex sees it so the caller can refresh + ANALYZE it in the
    same pass as the FR folder. Returns None when the file isn't in a mirrored library.

    This used to fire a bare `scan_path` here, which is not enough: the -EN entry is a symlink
    to the same file, so when a merge rewrites it in place (adding subtitles to a title that
    already had English audio, say) the -EN item's path and name never change and Plex won't
    re-read its streams. Best-effort; never raises into the merge flow."""
    cfg = cfg or core.load_config()
    try:
        rel = os.path.relpath(libfile, cfg["media_mount"])     # e.g. Films/Movie (2003)/file.mkv
        parts = rel.split(os.sep, 1)
        en_top = EN_LIBS.get(parts[0]) if len(parts) == 2 else None
        if not en_top:
            return None
        link = os.path.join(cfg["media_mount"], en_top, parts[1])
        if not os.path.lexists(link):
            os.makedirs(os.path.dirname(link), exist_ok=True)
            os.symlink(os.path.relpath(libfile, os.path.dirname(link)), link)
            core.log(f"mirror: EN symlink for {en_top}/{parts[1]}")
        return os.path.dirname(link).replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
    except Exception as e:
        core.log(f"mirror EN failed for {libfile}: {e}")
        return None


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
        # the library file changed on disk: drop both cached probes so the next scan re-reads
        # the new track list instead of reporting the pre-merge languages
        core.forget_probe(donor); core.forget_probe(dest)
        core.set_status(tmdb_id, "merged", merged_file=dest)
        if mv.get("radarr_id"):
            Radarr(cfg["radarr_url"], cfg["radarr_key"]).rescan(mv["radarr_id"])
        # Mirror FIRST so the -EN symlink exists, then refresh + ANALYZE both folders on every
        # PMS in one pass — a plain scan won't re-read streams after an in-place remux, and the
        # -EN copy is the same file under a different path, so it needs analysing too.
        plex_dir = libdir.replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
        en_dir = mirror_to_en(dest, cfg)
        res = plex_refresh(cfg, [plex_dir, en_dir], mv.get("title"), year=mv.get("year"))
        core.log(f"finish {tmdb_id}: placed {dest}; Radarr rescan + Plex refresh {res}")
        if mv.get("dl_hash"):          # donor served its purpose -> free the space
            try:
                qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
                _free_donor(qb, mv["dl_hash"], cfg, tag=f"movie {tmdb_id}")
            except Exception as e:
                core.log(f"finish {tmdb_id}: donor cleanup skipped: {e}")
    except Exception as e:
        core.set_status(tmdb_id, "error", error=f"finish: {e}")


# ---------------------------------------------------------------- STAGE DRIVERS
def stage_search(cfg=None):
    cfg = cfg or core.load_config()
    if not cfg["enabled"]:
        return
    why = hold_reason(cfg)
    if why:
        core.log(f"search films: holding off ({why})"); return
    budget = grab_budget(cfg)                      # flow control: cap downloads in flight
    if budget <= 0:
        core.log(f"search films: in-flight cap ({cfg.get('max_inflight_downloads', 5)}) reached -> not grabbing")
        return
    cap = min(budget, cfg.get("max_search_per_run", 25)); n = 0
    for mv in core.get_movies("pending"):
        search_movie(mv["tmdb_id"], cfg); n += 1
        if n >= cap:
            break


# ---------------------------------------------------------------- MERGE QUEUE + WORKER
# Merging used to run INLINE inside the qB-poll loop, so a completed download stayed in
# 'downloading' until every earlier item had finished merging (hours for a season pack) and
# it held a grab slot the whole time. Now the poll only promotes completed downloads to
# 'ready' (the queue) and a single background worker drains it.
MERGE_WAKE = threading.Event()          # set by a promotion -> worker starts immediately
_MERGING_NOW = set()                    # keys actively being merged BY THE WORKER right now
_MERGING_NOW_LOCK = threading.Lock()


def _merging_now():
    with _MERGING_NOW_LOCK:
        return set(_MERGING_NOW)


def merge_queue(cfg=None):
    """Everything waiting to merge, oldest first (FIFO — the old LIFO ordering meant the
    longest-waiting item was served last). Returns [(kind, id, updated), ...]."""
    items = [("movie", m["tmdb_id"], m.get("updated") or 0) for m in core.get_movies("ready")]
    items += [("episode", e["id"], e.get("updated") or 0) for e in core.get_episodes("ready")]
    items.sort(key=lambda x: x[2])
    return items


def merge_next(cfg=None):
    """Claim and merge ONE queued item. Returns True if something was merged."""
    from . import tv as _tv
    cfg = cfg or core.load_config()
    for kind, rid, _ts in merge_queue(cfg):
        key = f"{'m' if kind == 'movie' else 'e'}{rid}"
        claim = core.claim_movie if kind == "movie" else core.claim_episode
        # atomic ready -> merging: if we lose the race, another claimer has it
        if not claim(rid, "ready", "merging", progress="starting…"):
            continue
        with _MERGING_NOW_LOCK:
            _MERGING_NOW.add(key)
        try:
            if kind == "movie":
                merge_movie(rid, cfg)
            else:
                _tv.merge_ready_episode(rid, cfg)
        except Exception as e:
            # never let one bad item kill the worker (the old inline merge aborted the
            # whole finish cycle, stranding every record behind it)
            setter = core.set_status if kind == "movie" else core.set_ep_status
            setter(rid, "error", error=f"merge: {e}", progress="")
            core.log(f"merge {key} FAILED: {e}")
        finally:
            with _MERGING_NOW_LOCK:
                _MERGING_NOW.discard(key)
        return True
    return False


def merge_worker(index=0):
    """Background thread: drains the merge queue, forever. `index` is this worker's slot in the
    pool — if `max_parallel_merges` is lowered, workers above the new limit retire themselves."""
    core.log(f"merge worker #{index} started")
    while True:
        try:
            cfg = core.load_config()
            if index >= MERGE_GATE.limit(cfg):
                core.log(f"merge worker #{index} retired (max_parallel_merges lowered)")
                return
            if cfg.get("enabled") and not cfg.get("paused") and merge_next(cfg):
                continue                       # straight on to the next queued item
        except Exception as e:
            core.log(f"merge worker #{index} error: {e}")
        MERGE_WAKE.wait(timeout=10)
        MERGE_WAKE.clear()


def _resolve_donor_video(mv, t, cfg):
    """Locate the downloaded video for a completed movie donor. Returns (video_path, local_root)."""
    save = _qb_to_local(t.get("content_path") or t.get("save_path") or "", cfg)
    local = save if (save and os.path.exists(save)) \
        else os.path.join(cfg["downloads_mount"], str(mv["tmdb_id"]))
    if not os.path.exists(local):
        return None, local
    return (_find_video(local) if os.path.isdir(local) else local), local


_NOT_VISIBLE = {}          # tmdb_id -> consecutive sweeps its completed path wasn't visible
NOT_VISIBLE_MAX = 10       # ~10 min at the 1-min promote cadence, then stop waiting forever


def promote_completed(cfg=None):
    """Wrapper: only ever one promote pass at a time (see PROMOTE_LOCK)."""
    if not PROMOTE_LOCK.acquire(blocking=False):
        return 0
    try:
        return _promote_completed(cfg)
    finally:
        PROMOTE_LOCK.release()


def _promote_completed(cfg=None):
    """Fast sweep: every download that reached 100% leaves 'downloading' NOW and joins the
    merge queue. Cheap (one qB poll, no ffmpeg) so it runs every minute — the UI never shows
    a finished torrent as 'downloading', and the freed slot lets a new release start."""
    cfg = cfg or core.load_config()
    if not (cfg.get("enabled") and cfg.get("scope_films", True)):
        return 0
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        torrents = qb.torrents(cfg["qb_category"])
    except Exception as e:
        core.log(f"promote: qB error {e}"); return 0
    by_hash = {t["hash"]: t for t in torrents}
    queued = 0
    for mv in core.get_movies("downloading"):
        tmdb = str(mv["tmdb_id"])
        t = by_hash.get(mv.get("dl_hash"))
        if not t:
            t = next((x for x in torrents
                      if _owns(x.get("save_path", "") + x.get("content_path", ""), tmdb)), None)
        if not t or (t.get("progress", 0) or 0) < 1.0:
            continue
        vid, local = _resolve_donor_video(mv, t, cfg)
        if vid:
            _NOT_VISIBLE.pop(mv["tmdb_id"], None)
            if core.claim_movie(mv["tmdb_id"], "downloading", "ready",
                                en_file=vid, progress="queued for merge"):
                queued += 1
                core.log(f"queued {mv['tmdb_id']}: download complete -> merge queue")
        elif os.path.exists(local):
            # complete but no usable video (archive-only / wrong layout) -> try another release
            core.log(f"promote {mv['tmdb_id']}: complete but no video in {local} -> re-searching")
            reject_and_retry(mv["tmdb_id"], "download complete but no video file", cfg)
        else:
            # path not visible (mount race) — retry, but don't wait forever holding a slot
            miss = _NOT_VISIBLE.get(mv["tmdb_id"], 0) + 1
            _NOT_VISIBLE[mv["tmdb_id"]] = miss
            if miss >= NOT_VISIBLE_MAX:
                _NOT_VISIBLE.pop(mv["tmdb_id"], None)
                core.set_status(mv["tmdb_id"], "error", progress="",
                                error=f"download complete but {local} never became visible")
                core.log(f"promote {mv['tmdb_id']}: path never appeared -> error")
    if queued:
        MERGE_WAKE.set()                       # wake the worker immediately
    return queued


def _owns(paths, tmdb):
    """Does the concatenated qB path string belong to this movie? Savepath is exactly
    `{qb_download_dir}/{tmdb_id}`, so match a whole `/{tmdb}` segment — a bare substring test
    would let `/1234` false-match tmdb 123 and mis-reconcile a genuinely-gone download."""
    s = f"/{tmdb}"
    return f"{s}/" in paths or paths.rstrip("/").endswith(s)


def stage_finish(cfg=None):
    """Reconcile movie downloads against qB and queue completed ones for merging. This NEVER
    merges inline — the background worker does that — so the cycle stays fast and one big
    season pack can't stall every other completed download behind it."""
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
        if not t:   # fall back: match by save/content path owning the /<tmdb> segment
            t = next((x for x in torrents
                      if _owns(x.get("save_path", "") + x.get("content_path", ""), tmdb)), None)
        if not t:
            # torrent vanished from qB (removed/failed) -> re-queue so it searches again
            if not _owns(all_paths, tmdb):
                core.set_status(mv["tmdb_id"], "pending", dl_hash=None, dl_id=None, en_file=None, error=None)
                core.log(f"reconcile {mv['tmdb_id']}: download no longer in qB -> re-queued")
            continue
        if (t.get("progress", 0) or 0) < 1.0 and _is_stalled(t, cfg):
            drop_stalled(mv, t, cfg)
    # completed downloads -> merge queue (also runs on its own fast timer)
    promote_completed(cfg)
    # re-queue merges interrupted by a restart/crash: stuck at 'merging' but not updated
    # recently (an active merge bumps `updated` every window via the progress field). Skip
    # anything the worker is merging RIGHT NOW — a long remux writes no progress.
    live = _merging_now()
    for mv in core.get_movies("merging"):
        if f"m{mv['tmdb_id']}" in live:
            continue
        if time.time() - (mv.get("updated") or 0) < 900:     # <15min -> probably still running
            continue
        en, fr = mv.get("en_file"), mv.get("french_path")
        if en and fr and os.path.exists(en) and os.path.exists(fr):
            core.set_status(mv["tmdb_id"], "ready", progress="re-queued after interruption")
            core.log(f"resume {mv['tmdb_id']}: stale merge -> back on the merge queue")
            MERGE_WAKE.set()
        else:
            core.set_status(mv["tmdb_id"], "pending", progress="",
                            error="merge interrupted and source file missing")
