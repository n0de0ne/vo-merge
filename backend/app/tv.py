"""Series (Sonarr) pipeline — episode-level. Mirrors the movie pipeline but the unit
is the episode: detect French-only episodes, grab the English release (season pack when
many episodes in a season are missing, else single episodes), then merge the English
audio onto each French episode (best video kept), in place.

Reuses the proven merge helpers from pipeline.py / offdet*.py. Gated behind scope_series.
"""
import os, re, subprocess, shutil
from collections import defaultdict
from . import core
from .clients import Sonarr, Prowlarr, QBittorrent
from .pipeline import (probe, _video_quality, _pick_link, _hash_from_magnet, qb_grab, _is_stalled,
                       mirror_to_en, _qb_to_local, _free_donor, grab_budget, MERGE_GATE, FR_DUB,
                       EN_OK, EN_AUDIO, RES, SRC, MERGE_WAKE, _merging_now, NOT_VISIBLE_MAX)

SXXEXX = re.compile(r'[Ss](\d{1,3})[Ee](\d{1,4})')
VIDEXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts")
_TV_NOT_VISIBLE = {}      # dl_hash -> consecutive sweeps its completed path wasn't visible


def _parse_se(relpath):
    """(season, episode) from a download file path. Handles SxxExx, and anime layouts where
    the season is in a FOLDER ('Season 2'/'Saison 2') and the file is just an episode number
    ('Dr Stone - 01.mkv'). Returns (None, None) if it can't tell."""
    base = os.path.basename(relpath)
    m = SXXEXX.search(base)
    if m:
        return int(m.group(1)), int(m.group(2))
    # season: prefer the word 'season'/'saison' in the path (the deepest match wins, so a
    # per-season subfolder beats a 'S01+02' top folder)
    sm = re.findall(r'(?:season|saison)\s*0*(\d{1,3})', relpath, re.I)
    season = int(sm[-1]) if sm else 1                      # default S01 when only ep numbers
    # episode: '- 01', 'ep 01', 'e01', or the trailing number before the extension
    em = (re.search(r'(?:\s-\s|\bep\.?\s*|[._]e)0*(\d{1,4})', base, re.I)
          or re.search(r'\b0*(\d{1,4})\b(?!.*\b\d)', os.path.splitext(base)[0]))
    if not em:
        return None, None
    return season, int(em.group(1))


def _no_eng(al):
    """True if the file is MISSING an English audio track (a gap to fill) — covers fre, fre/jpn
    (anime), jpn-only, etc. Unknown audio (no mediaInfo) -> False, to avoid flagging
    un-analysed files."""
    if not al:
        return False
    a = al.lower()
    return ("eng" not in a) and ("english" not in a)


def _media(path, cfg):          # Sonarr /data path -> our /media mount
    return path.replace("/data/", cfg["media_mount"].rstrip("/") + "/", 1)


def _toks(s):
    return set(re.findall(r'[a-z0-9]+', (s or '').lower()))


# ------------------------------------------------------------------ SCAN
def scan(cfg=None):
    cfg = cfg or core.load_config()
    son = Sonarr(cfg["sonarr_url"], cfg["sonarr_key"])
    tagid = next((t["id"] for t in son.tags() if t["label"] == cfg["sonarr_vo_gap_tag"]), None)
    if tagid is None:
        core.log("tv scan: vo-gap tag not found"); return 0
    pilot = set(cfg.get("series_pilot") or [])
    n = 0
    for s in son.series():
        if tagid not in s.get("tags", []):
            continue
        if (s.get("originalLanguage") or {}).get("name") == "French":
            continue
        if pilot and s["title"] not in pilot:
            continue
        try:
            files = son.episode_files(s["id"])
        except Exception:
            continue
        poster = next((i.get("remoteUrl") or i.get("url") for i in s.get("images", [])
                       if i.get("coverType") == "poster"), None)
        for f in files:
            mi = f.get("mediaInfo") or {}
            if not _no_eng(mi.get("audioLanguages")):
                continue
            path = f.get("path") or ""
            m = SXXEXX.search(os.path.basename(path))
            if not m:
                continue
            season, ep = int(m.group(1)), int(m.group(2))
            core.upsert_episode({
                "id": f"{s['id']}:{season}:{ep}", "series_id": s["id"],
                "series_title": s["title"], "tvdb_id": s.get("tvdbId"),
                "season": season, "episode": ep, "french_path": _media(path, cfg),
                "quality": mi.get("resolution") or (f.get("quality", {}).get("quality", {}) or {}).get("name"),
                "poster": poster, "series_type": s.get("seriesType", "standard"),
            })
            n += 1
    core.log(f"tv scan: {n} episodes missing English (pilot={sorted(pilot) or 'all'})")
    return n


# ------------------------------------------------------------------ SEARCH + GRAB (hybrid)
def _grab(link, savepath, cfg):
    """Fetch + add a torrent to qB, returning the confirmed infohash — or None on any failure
    (network/login/fetch). Never raises: a single grab failure must not abort a whole search
    cycle, and callers guard the None so a null hash is never assigned to an episode."""
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
        qb.login()
        qb.create_category(cfg["qb_tv_category"], cfg["qb_tv_download_dir"])
        return qb_grab(qb, link, cfg["qb_tv_category"], savepath)
    except Exception as ex:
        core.log(f"tv _grab failed ({savepath}): {ex}")
        return None


def _search(query, cfg, want_pack=False, season=None, ep=None, year=None):
    """Return best (score, seeders, title, link) for an English release, or None."""
    pro = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"])
    try:
        results = pro.search(query, cfg["en_indexer_ids"])
    except Exception as e:
        core.log(f"tv search '{query}': {e}"); return None
    qt = _toks(query.rsplit(" S", 1)[0] if " S" in query else query)
    best = None
    for r in results:
        t = r.get("title", ""); tl = t.lower()
        if FR_DUB.search(t) and not EN_OK.search(t) and not EN_AUDIO.search(t):
            continue
        if qt and len(qt & _toks(t)) / max(len(qt), 1) < 0.6:
            continue
        if want_pack:
            # season pack: has Sxx but NOT a single SxxExx, or marked complete/season
            if SXXEXX.search(t):
                continue
            if not re.search(rf'(s0?{season}\b|season\s*0?{season}\b|complete)', tl):
                continue
        else:
            m = SXXEXX.search(t)
            if not (m and int(m.group(1)) == season and int(m.group(2)) == ep):
                continue
        sc = min(int(r.get("seeders") or 0), 100)
        if RES.search(t): sc += 20
        if re.search(r'\bMULTI\b', t, re.I): sc += 20
        if EN_AUDIO.search(t): sc += 60          # explicit English / Dual-Audio (anime)
        link = _pick_link(r)
        if best is None or sc > best[0]:
            best = (sc, r.get("seeders") or 0, t, link)
    return best


def season_candidates(series_id, season, cfg=None):
    """Scored release candidates for a whole season (packs preferred) — for the UI's
    interactive season-pack search."""
    cfg = cfg or core.load_config()
    eps = [e for e in core.get_episodes() if e["series_id"] == series_id and e["season"] == season]
    if not eps:
        return []
    title = eps[0]["series_title"]
    import json as _json
    tried = set()
    for e in eps:
        tried |= set(_json.loads(e.get("tried") or "[]"))
    pro = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"])
    try:
        results = pro.search(f"{title} S{season:02d}", cfg["en_indexer_ids"] + cfg.get("multi_indexer_ids", []))
    except Exception as e:
        core.log(f"season_candidates: {e}"); return []
    qt = _toks(title); out = []
    for r in results:
        t = r.get("title", ""); tl = t.lower()
        if FR_DUB.search(t) and not EN_OK.search(t) and not EN_AUDIO.search(t):
            continue
        if qt and len(qt & _toks(t)) / max(len(qt), 1) < 0.6:
            continue
        m = SXXEXX.search(t)
        is_pack = (not m) and bool(re.search(rf"(s0?{season}\b|season\s*0?{season}\b|complete|int[eé]grale)", tl))
        is_ep = bool(m and int(m.group(1)) == season)
        if not (is_pack or is_ep):
            continue
        sc = min(int(r.get("seeders") or 0), 100)
        if is_pack: sc += 50
        if RES.search(t): sc += 20
        if re.search(r"\bMULTI\b", t, re.I): sc += 200
        if EN_AUDIO.search(t): sc += 120         # explicit English / Dual-Audio (anime)
        link = _pick_link(r); rid = _hash_from_magnet(link) or r.get("guid") or r.get("title")
        out.append({"score": sc, "seeders": r.get("seeders") or 0, "size": r.get("size") or 0,
                    "title": t, "indexer": r.get("indexer"), "pack": is_pack,
                    "multi": bool(re.search(r"\bMULTI\b", t, re.I)),
                    "link": link, "rid": rid, "tried": rid in tried, "info_url": r.get("infoUrl")})
    out.sort(key=lambda x: -x["score"])
    return out


def _pack_seasons(title, default_season):
    """Seasons a release title advertises. None = ALL seasons (complete/intégrale). Only expands
    beyond {default_season} on a clear multi-season signal (range like S01-S03, or list like
    S01+02) — otherwise a single season, to avoid over-claiming."""
    t = title or ""
    if re.search(r'(?<![a-z])(complete|int[eé]grale|integrale)(?![a-z])', t, re.I):
        return None
    seasons = set()
    for m in re.finditer(r'(?:s|season|saison)\s*0*(\d{1,2})\s*(?:[-–~]|to|[aà])\s*(?:s|season|saison)?\s*0*(\d{1,2})', t, re.I):
        a, b = int(m.group(1)), int(m.group(2))
        if a < b <= a + 30:
            seasons.update(range(a, b + 1))
    lm = re.search(r'(?:s|season|saison)\s*0*(\d{1,2})((?:\s*[+&]\s*(?:s|season|saison)?\s*0*\d{1,2})+)', t, re.I)
    if lm:
        seasons.add(int(lm.group(1)))
        seasons.update(int(x) for x in re.findall(r'\d{1,2}', lm.group(2)))
    return seasons or {default_season}


def _assign_pack(series_id, seasons, h, rid, title):
    """Tag a grabbed pack onto every gap episode of the series in `seasons` (None = all seasons)
    so a multi-season download isn't separately re-grabbed season-by-season. Returns count."""
    eps = [e for e in core.get_episodes()
           if e["series_id"] == series_id and e["status"] not in ("merged", "ignored", "downloading")
           and (seasons is None or e["season"] in seasons)]
    for e in eps:
        core.set_ep_status(e["id"], "downloading", dl_hash=h, dl_id=rid,
                           candidate_title=title, error=None)
    return len(eps)


def grab_season(series_id, season, link, rid=None, title=None, cfg=None):
    """Grab a user-chosen season pack and claim the gap episodes of every season the pack
    advertises (e.g. an 'S01+02' pack also claims S02, so it isn't re-grabbed separately)."""
    cfg = cfg or core.load_config()
    h = _grab(link, f"{cfg['qb_tv_download_dir']}/{series_id}_S{season:02d}", cfg)
    if not h:
        raise RuntimeError("torrent never appeared in qB (fetch/add failed)")
    seasons = _pack_seasons(title, season)
    n = _assign_pack(series_id, seasons, h, rid, title)
    core.log(f"tv grab SEASON {series_id} (seasons {sorted(seasons) if seasons else 'ALL'}): {n} eps <- {title}")
    return n


def episode_candidates(ep_id, cfg=None):
    """Scored release candidates for ONE episode (single-ep releases preferred, season packs
    that contain it also offered) — powers the per-episode interactive search. English/Dual-Audio
    releases are boosted (useful for anime that ship French+Japanese and need English added)."""
    cfg = cfg or core.load_config()
    e = core.get_episode(ep_id)
    if not e:
        return []
    title, season, ep = e["series_title"], e["season"], e["episode"]
    import json as _json
    tried = set(_json.loads(e.get("tried") or "[]"))
    pro = Prowlarr(cfg["prowlarr_url"], cfg["prowlarr_key"])
    try:
        results = pro.search(f"{title} S{season:02d}E{ep:02d}",
                             cfg["en_indexer_ids"] + cfg.get("multi_indexer_ids", []))
    except Exception as ex:
        core.log(f"episode_candidates: {ex}"); return []
    qt = _toks(title); out = []
    for r in results:
        t = r.get("title", ""); tl = t.lower()
        if FR_DUB.search(t) and not EN_OK.search(t) and not EN_AUDIO.search(t):
            continue
        if qt and len(qt & _toks(t)) / max(len(qt), 1) < 0.6:
            continue
        m = SXXEXX.search(t)
        is_ep = bool(m and int(m.group(1)) == season and int(m.group(2)) == ep)
        is_pack = (not m) and bool(re.search(rf"(s0?{season}\b|season\s*0?{season}\b|complete|int[eé]grale)", tl))
        if not (is_ep or is_pack):
            continue
        sc = min(int(r.get("seeders") or 0), 100)
        if is_pack: sc += 30
        if RES.search(t): sc += 20
        if re.search(r"\bMULTI\b", t, re.I): sc += 200
        if EN_AUDIO.search(t): sc += 120          # explicit English / Dual-Audio
        link = _pick_link(r); rid = _hash_from_magnet(link) or r.get("guid") or r.get("title")
        out.append({"score": sc, "seeders": r.get("seeders") or 0, "size": r.get("size") or 0,
                    "title": t, "indexer": r.get("indexer"), "pack": is_pack,
                    "multi": bool(re.search(r"\bMULTI\b", t, re.I)),
                    "link": link, "rid": rid, "tried": rid in tried, "info_url": r.get("infoUrl")})
    out.sort(key=lambda x: -x["score"])
    return out


def retry_episode(ep_id, cfg=None):
    """Proper retry for an errored/failed episode: blocklist the release that failed, drop its
    donor from qB (only if no other live episode still needs that hash), clear the grab fields,
    and re-queue for a fresh search."""
    import json as _json
    cfg = cfg or core.load_config()
    e = core.get_episode(ep_id)
    if not e:
        return False
    h = e.get("dl_hash")
    tried = _json.loads(e.get("tried") or "[]")
    if e.get("dl_id") and e["dl_id"] not in tried:
        tried.append(e["dl_id"])
    if h:
        live = [x for x in core.get_episodes()
                if (x.get("dl_hash") == h and x["id"] != ep_id
                    and x["status"] not in ("error", "no_release", "ignored", "merged"))]
        if not live:
            try:
                qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
                qb.delete([h], delete_files=True)
                core.log(f"retry {ep_id}: dropped failed donor {str(h)[:12]}")
            except Exception as ex:
                core.log(f"retry {ep_id}: donor drop failed: {ex}")
    core.set_ep_status(ep_id, "pending", error=None, tried=_json.dumps(tried),
                       dl_hash=None, dl_id=None, en_file=None, progress="")
    return True


def retry_errors(cfg=None):
    """Retry every errored episode (bulk). Returns how many were re-queued."""
    cfg = cfg or core.load_config()
    n = 0
    for e in core.get_episodes("error"):
        if retry_episode(e["id"], cfg):
            n += 1
    core.log(f"tv retry-all: re-queued {n} errored episodes")
    return n


def grab_episode(ep_id, link, rid=None, title=None, cfg=None):
    """Grab a user-chosen release for a single episode (interactive)."""
    cfg = cfg or core.load_config()
    e = core.get_episode(ep_id)
    if not e:
        return 0
    h = _grab(link, f"{cfg['qb_tv_download_dir']}/{ep_id.replace(':', '_')}", cfg)
    if not h:
        raise RuntimeError("torrent never appeared in qB (fetch/add failed)")
    core.set_ep_status(ep_id, "downloading", dl_hash=h, dl_id=rid, candidate_title=title, error=None)
    # if the picked release is a multi-season pack, claim those seasons' gap episodes too
    seasons = _pack_seasons(title, e["season"])
    extra = _assign_pack(e["series_id"], seasons, h, rid, title) if (seasons is None or len(seasons) > 1) else 0
    core.log(f"tv grab EP(interactive) {ep_id}: {title} -> {1 + extra} ep(s)")
    return 1 + extra


def stage_search(cfg=None):
    cfg = cfg or core.load_config()
    if not (cfg["enabled"] and cfg["scope_series"]):
        return
    budget = grab_budget(cfg)                        # flow control: cap downloads in flight (shared with films)
    if budget <= 0:
        core.log(f"tv search: in-flight cap ({cfg.get('max_inflight_downloads', 5)}) reached -> not grabbing")
        return
    pend = core.get_episodes("pending")
    cap = min(budget, cfg.get("max_search_per_run", 25)); n = 0   # ramp gradually, don't flood indexers
    # group by (series, season)
    by_season = defaultdict(list)
    for e in pend:
        by_season[(e["series_id"], e["series_title"], e["season"])].append(e)
    # anime first: fill the Anime library ahead of standard TV
    def _order(item):
        (sid, title, season), eps = item
        is_anime = any((e.get("series_type") or "") == "anime" for e in eps)
        return (0 if is_anime else 1, title, season)
    for (sid, title, season), eps in sorted(by_season.items(), key=_order):
        if n >= cap:
            break
        if len(eps) >= cfg["tv_pack_threshold"]:
            q = f"{title} S{season:02d}"
            best = _search(q, cfg, want_pack=True, season=season)
            if best:
                sc, seed, rtitle, link = best
                h = _grab(link, f"{cfg['qb_tv_download_dir']}/{sid}_S{season:02d}", cfg)
                if not h:
                    # grab failed -> mark these eps error (NOT a null-hash download, which would
                    # loop grab -> reconcile-to-pending -> re-grab forever)
                    for e in eps:
                        core.set_ep_status(e["id"], "error", error="grab: torrent never appeared")
                    core.log(f"tv grab PACK '{q}': grab failed -> {len(eps)} ep(s) set to error")
                    n += 1
                    continue
                seasons = _pack_seasons(rtitle, season)   # claim every season the pack advertises
                claimed = _assign_pack(sid, seasons, h, None, rtitle)
                core.log(f"tv grab PACK '{q}': [{sc}] {seed}s {rtitle} -> {claimed} eps "
                         f"(seasons {sorted(seasons) if seasons else 'ALL'})")
                n += 1
                continue
            # no pack -> fall through to per-episode
        for e in eps:
            if n >= cap:
                break
            q = f"{title} S{e['season']:02d}E{e['episode']:02d}"
            best = _search(q, cfg, season=e["season"], ep=e["episode"])
            if not best or best[0] < cfg["min_seeders"]:
                core.set_ep_status(e["id"], "no_release",
                                   candidate_title=(best[2] if best else None))
                continue
            sc, seed, rtitle, link = best
            h = _grab(link, f"{cfg['qb_tv_download_dir']}/{e['id'].replace(':','_')}", cfg)
            if not h:
                core.set_ep_status(e["id"], "error", error="grab: torrent never appeared")
                core.log(f"tv grab EP '{q}': grab failed -> error")
                n += 1
                continue
            core.set_ep_status(e["id"], "downloading", dl_hash=h,
                               candidate_title=rtitle, candidate_score=sc, candidate_seeders=seed)
            core.log(f"tv grab EP '{q}': [{sc}] {seed}s {rtitle}")
            n += 1


# ------------------------------------------------------------------ FINISH (map + merge)
def _merge_episode(ep, en_file, cfg, hint=None):
    """Share the movie merge gate (max_parallel_merges) so merges can't peg CPU/GPU.
    `hint` = (offset, drift) from a pack-mate, to skip full sync detection when it matches.
    Returns (offset, drift) on a detected merge, else None."""
    with MERGE_GATE.slot(cfg):
        return _merge_episode_impl(ep, en_file, cfg, hint)


def _plex_ep_refresh(ep, cfg):
    """Refresh + analyze this episode on every PMS (master + replica) so the grafted English
    shows up on both — a plain scan won't re-read streams after an in-place remux."""
    from .pipeline import plex_refresh
    try:
        folder = os.path.dirname(ep["french_path"]).replace(cfg["media_mount"], cfg["plex_media_prefix"], 1)
        plex_refresh(cfg, folder, ep["series_title"], season=ep["season"], episode=ep["episode"])
    except Exception as e:
        core.log(f"tv plex refresh {ep['id']}: {e}")


def _merge_episode_impl(ep, en_file, cfg, hint=None):
    """Merge: keep better video, graft the other language's audio, replace FR file in place."""
    from . import sync
    from .clients import Sonarr as _S
    fr = ep["french_path"]
    if not (os.path.exists(en_file) and os.path.exists(fr)):
        core.set_ep_status(ep["id"], "error", error="merge: file missing"); return
    ei, fi = probe(en_file), probe(fr)
    if not ei or not fi:
        core.set_ep_status(ep["id"], "error", error="merge: probe failed"); return
    # MULTI episode: download has both langs -> remux directly, but ONLY if its video isn't
    # worse than the library file; otherwise keep the library video and graft English (below).
    if {"eng", "fre"} <= {a["lang"] for a in ei["auds"]} \
       and _video_quality(en_file, ei["dur"]) >= _video_quality(fr, fi["dur"]):
        out = os.path.dirname(fr) + "/_merged/" + os.path.splitext(os.path.basename(fr))[0] + ".mkv"
        os.makedirs(os.path.dirname(out), exist_ok=True)
        if subprocess.run(["mkvmerge", "-o", out, en_file], capture_output=True).returncode in (0, 1):
            shutil.move(out, fr)
            try: os.rmdir(os.path.dirname(out))
            except OSError: pass
            core.set_ep_status(ep["id"], "merged", merged_file=fr, added_langs="", error=None)
            core.log(f"tv merge {ep['id']}: MULTI used directly")
            try: _S(cfg["sonarr_url"], cfg["sonarr_key"]).rescan(ep["series_id"])
            except Exception: pass
            mirror_to_en(fr, cfg)
            _plex_ep_refresh(ep, cfg)
        else:
            core.set_ep_status(ep["id"], "error", error="multi remux failed")
        return
    delta = abs((ei["dur"] or 0) - (fi["dur"] or 0))
    offset = ep.get("sync_offset_ms") or 0
    # framerate mismatch is handled by the drift detector below (not rejected upfront);
    # only bail here if auto-sync is off, since a constant offset can't fix frame drift.
    fps_diff = not sync.fps_close(ei["fps"], fi["fps"])
    if fps_diff and not offset and not cfg.get("auto_sync", True):
        core.set_ep_status(ep["id"], "sync_fail", sync_delta=delta,
                           error=f"framerate differs ({ei['fps']} vs {fi['fps']}), auto-sync off"); return
    eq, fq = _video_quality(en_file, ei["dur"]), _video_quality(fr, fi["dur"])
    base, bi, donor, di = (fr, fi, en_file, ei) if fq >= eq else (en_file, ei, fr, fi)
    have = {a["lang"] for a in bi["auds"]}
    ids, langs, daidx = [], {}, {}
    for ix, a in enumerate(di["auds"]):
        if a["lang"] == "und" or a["lang"] in have:
            continue
        ids.append(a["id"]); langs[a["id"]] = a["lang"]; daidx[a["id"]] = ix; have.add(a["lang"])
    if "eng" not in have:
        core.set_ep_status(ep["id"], "error", error="merge: no English audio to add"); return
    if not ids:
        # episode file already has English -> already filled (stale tag / prior merge), mark done
        core.set_ep_status(ep["id"], "merged", merged_file=fr, progress="", error=None, added_langs="")
        core.log(f"tv merge {ep['id']}: already has English -> done")
        mirror_to_en(fr, cfg)
        _plex_ep_refresh(ep, cfg)
        return
    drift = None
    if not offset and cfg.get("auto_sync", True):
        core.set_ep_status(ep["id"], "merging", progress="sync: starting", error=None)
        m, conf, method, drift = sync.detect(
            base, donor, 0, daidx[ids[0]], min(ei["dur"] or 0, fi["dur"] or 0), cfg, tag=f" {ep['id']}",
            on_progress=lambda msg: core.set_ep_status(ep["id"], "merging", progress=msg), hint=hint,
            base_fps=bi.get("fps"), donor_fps=di.get("fps"),
            base_dur=bi.get("dur"), donor_dur=di.get("dur"))
        if m is None or (fps_diff and not drift):
            why = ("framerates differ but no reliable drift could be measured"
                   if (m is not None and fps_diff and not drift)
                   else "couldn't sync (incompatible release/cut)")
            core.set_ep_status(ep["id"], "sync_fail", sync_delta=delta, progress="", error=why); return
        if abs(m) >= 40 or drift:
            offset = int(round(m))
        core.log(f"tv sync {ep['id']}: {offset:+d}ms{' drift' if drift else ''} ({method} conf {conf:.2f})")
    core.set_ep_status(ep["id"], "merging", progress="muxing audio…")
    outdir = os.path.dirname(fr) + "/_merged"
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, os.path.splitext(os.path.basename(fr))[0] + ".mkv")
    cmd = ["mkvmerge", "-o", out, base, "--no-video", "--no-subtitles", "--no-chapters",
           "--no-buttons", "--no-track-tags", "--audio-tracks", ",".join(str(i) for i in ids)]
    for i in ids:
        cmd += ["--language", f"{i}:{langs[i]}", "--default-track", f"{i}:0"]
        if offset or drift:
            arg = f"{i}:{offset}" + (f",{round(drift * 1000000)}/1000000" if drift else "")
            cmd += ["--sync", arg]
    cmd += [donor]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode not in (0, 1):
        core.set_ep_status(ep["id"], "error", error=f"mkvmerge rc={r.returncode}"); return
    shutil.move(out, fr)            # replace FR file in place (same name)
    try:
        os.rmdir(outdir)
    except OSError:
        pass
    core.set_ep_status(ep["id"], "merged", merged_file=fr, sync_offset_ms=offset, sync_delta=delta,
                       sync_drift=drift, error=None, progress="",
                       added_langs=",".join(sorted({langs[i] for i in ids})))
    core.log(f"tv merge {ep['id']}: OK +{offset}ms -> {os.path.basename(fr)}")
    try:
        _S(cfg["sonarr_url"], cfg["sonarr_key"]).rescan(ep["series_id"])
    except Exception:
        pass
    mirror_to_en(fr, cfg)          # add to Series-EN/Anime-EN + refresh that section now
    _plex_ep_refresh(ep, cfg)      # + analyze the episode on both PMS so the new audio shows
    return (offset, drift)         # cache as the pack hint for the next episode


def _drop_stalled_eps(eps, t, cfg):
    """A stalled season-pack/episode torrent: delete it, blocklist that release for all its
    episodes, set them back to pending so stage_search grabs another (better-seeded) release."""
    import json as _json
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        if eps and eps[0].get("dl_hash"):
            qb.delete([eps[0]["dl_hash"]], delete_files=True)
    except Exception:
        pass
    seeds = t.get("num_complete", t.get("num_seeds", 0)) or 0
    for e in eps:
        tried = _json.loads(e.get("tried") or "[]")
        if e.get("dl_id") and e["dl_id"] not in tried:
            tried.append(e["dl_id"])
        attempts = (e.get("attempts") or 0) + 1
        st = "no_release" if attempts >= cfg.get("max_sync_retries", 4) else "pending"
        core.set_ep_status(e["id"], st, tried=_json.dumps(tried), attempts=attempts,
                           dl_hash=None, dl_id=None, en_file=None, progress="",
                           error=("all releases stalled" if st == "no_release" else None))
    core.log(f"tv stall: '{t.get('name','')[:50]}' stalled ({seeds} seeds) -> blocklisted "
             f"{len(eps)} ep(s), re-searching")


def sweep_stalled(cfg=None):
    """Drop seederless / non-progressing episode + pack downloads and re-search. Own fast timer,
    independent of the merge loop, so the merge backlog never delays stall handling."""
    cfg = cfg or core.load_config()
    if not (cfg["enabled"] and cfg["scope_series"]):
        return
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    try:
        qb.login()
        torrents = {t["hash"]: t for t in qb.torrents(cfg["qb_tv_category"])}
    except Exception as e:
        core.log(f"tv sweep: qB error {e}"); return
    by_hash = defaultdict(list)
    for e in core.get_episodes("downloading"):
        by_hash[e.get("dl_hash")].append(e)
    for h, eps in by_hash.items():
        t = torrents.get(h)
        if t and (t.get("progress", 0) or 0) < 1.0 and _is_stalled(t, cfg):
            _drop_stalled_eps(eps, t, cfg)


_PACK_HINT = {}      # dl_hash -> (offset, drift) learned from the first episode of a pack


def _pack_done(h):
    """Has every episode served by this donor reached a terminal state?"""
    with core.db() as c:
        sts = [r["status"] for r in
               c.execute("SELECT status FROM episodes WHERE dl_hash=?", (h,))]
    return bool(sts) and all(s in ("merged", "ignored") for s in sts)


def free_donor_if_done(h, cfg, qb=None):
    """A season pack feeds many episodes: free the donor only once EVERY episode it serves has
    reached a terminal state. Called after each episode merge (the worker merges them one at a
    time, so this is what actually releases pack disk now)."""
    if not h or not _pack_done(h):
        return
    try:
        if qb is None:
            qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        _free_donor(qb, h, cfg, tag=f"tv pack {str(h)[:12]}")
    except Exception as e:
        core.log(f"tv donor free {str(h)[:12]} failed: {e}")


def merge_ready_episode(ep_id, cfg=None):
    """Merge one queued episode. Reuses the pack's learned A/V offset so the 2nd..Nth episode
    of a season pack skips full sync detection (the hint used to live in a local variable of
    the inline finish loop; it's now cached per donor hash so the worker keeps the speedup)."""
    cfg = cfg or core.load_config()
    ep = core.get_episode(ep_id)
    if not ep:
        return
    en = ep.get("en_file")
    if not en or not os.path.exists(en):
        core.set_ep_status(ep_id, "error", progress="", error="merge: donor file missing")
        return
    h = ep.get("dl_hash")
    res = _merge_episode(ep, en, cfg, hint=_PACK_HINT.get(h))
    if res and res[0] is not None and h:
        _PACK_HINT[h] = res
    free_donor_if_done(h, cfg)


def promote_completed(cfg=None):
    """Fast sweep: map completed pack/episode downloads onto episodes and queue them for
    merging. No ffmpeg here — just a qB poll and a directory walk."""
    cfg = cfg or core.load_config()
    if not (cfg["enabled"] and cfg["scope_series"]):
        return 0
    try:
        qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"]); qb.login()
        torrents = {t["hash"]: t for t in qb.torrents(cfg["qb_tv_category"])}
    except Exception as e:
        core.log(f"tv promote: qB error {e}"); return 0
    by_hash = defaultdict(list)
    for e in core.get_episodes("downloading"):
        by_hash[e.get("dl_hash")].append(e)
    queued = 0
    for h, eps in by_hash.items():
        t = torrents.get(h)
        if not t or (t.get("progress", 0) or 0) < 1.0:
            continue
        local = _qb_to_local(t.get("content_path") or t.get("save_path") or "", cfg)
        root = local if os.path.isdir(local) else os.path.dirname(local)
        if not os.path.isdir(root):
            miss = _TV_NOT_VISIBLE.get(h, 0) + 1
            _TV_NOT_VISIBLE[h] = miss
            if miss >= NOT_VISIBLE_MAX:
                _TV_NOT_VISIBLE.pop(h, None)
                for e in eps:
                    core.set_ep_status(e["id"], "error", progress="",
                                       error=f"download complete but {root} never became visible")
                core.log(f"tv promote: {str(h)[:12]} path never appeared -> error")
            continue
        _TV_NOT_VISIBLE.pop(h, None)
        # index EVERY downloaded video file by (season,episode) — parse handles anime layouts
        files = {}
        for r, _, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(VIDEXT):
                    full = os.path.join(r, f)
                    s, ep = _parse_se(os.path.relpath(full, root))
                    if s is not None:
                        files[(s, ep)] = full
        # A single download can span multiple seasons (e.g. an "S01+02" anime pack). Match its
        # files against ALL gap episodes of the series — not just the ones originally tagged with
        # this hash — claiming pending episodes (e.g. S02) the pack also satisfies.
        sid = eps[0]["series_id"]
        gap = {(x["season"], x["episode"]): x for x in core.get_episodes()
               if x["series_id"] == sid and x["status"] not in ("merged", "ignored")}
        claimed = 0
        for (s, ep), vid in sorted(files.items()):
            tgt = gap.get((s, ep))
            if not tgt or tgt["status"] in ("ready", "merging"):
                continue                       # already queued or being merged — don't disturb
            # conditional on the status we just read: if the worker claimed it in between,
            # this fails and we leave the live merge alone (never re-queue a merging episode)
            if core.claim_episode(tgt["id"], tgt["status"], "ready", en_file=vid,
                                  dl_hash=t["hash"], dl_id=eps[0].get("dl_id"),
                                  progress="queued for merge"):
                claimed += 1
        if claimed:
            queued += claimed
            core.log(f"tv queued: {t['name'][:50]} -> {claimed} episode(s) on the merge queue")
        elif not files:
            # complete, dir present, but ZERO parseable video -> dead release
            core.log(f"tv promote: {t['name'][:50]} complete but no video files -> re-searching")
            _drop_stalled_eps(eps, t, cfg)
        else:
            # Files parsed but none matched a gap episode — almost always a numbering mismatch
            # (absolute vs season, e.g. a "Complete Collection S01-S04" pack). This used to log
            # forever while the pack squatted a slot; surface it for review/AI instead.
            for e in eps:
                core.set_ep_status(e["id"], "error", progress="",
                                   error=f"download complete but no episode files matched "
                                         f"(parsed {sorted(files.keys())[:6]})")
            core.log(f"tv promote: {t['name'][:50]} complete but no files mapped to episodes "
                     f"(parsed {sorted(files.keys())[:6]}) -> error")
    if queued:
        MERGE_WAKE.set()
    return queued


def stage_finish(cfg=None):
    """Reconcile episode downloads against qB and queue completed ones. Never merges inline —
    the background worker drains the queue."""
    cfg = cfg or core.load_config()
    if not (cfg["enabled"] and cfg["scope_series"]):
        return
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    try:
        qb.login()
        torrents = {t["hash"]: t for t in qb.torrents(cfg["qb_tv_category"])}
    except Exception as e:
        core.log(f"tv finish: qB error {e}"); return
    by_hash = defaultdict(list)
    for e in core.get_episodes("downloading"):
        by_hash[e.get("dl_hash")].append(e)
    for h, eps in by_hash.items():
        t = torrents.get(h)
        if not t:
            # torrent vanished from qB (removed/failed/never-added) -> re-queue to re-search
            for e in eps:
                core.set_ep_status(e["id"], "pending", dl_hash=None, dl_id=None,
                                   en_file=None, error=None, progress="")
            core.log(f"tv reconcile: {len(eps)} ep(s) no longer in qB (hash {str(h)[:12]}) -> re-queued")
            continue
        if (t.get("progress", 0) or 0) < 1.0 and _is_stalled(t, cfg):
            _drop_stalled_eps(eps, t, cfg)
    promote_completed(cfg)
    # sweep: free any donor still in qB whose episodes have all finished (covers a pack whose
    # last episode merged after the previous cycle looked at it)
    for h in list(torrents):
        free_donor_if_done(h, cfg, qb)
    # re-queue episode merges interrupted by a restart/crash (skip live worker merges)
    import time as _t
    live = _merging_now()
    for e in core.get_episodes("merging"):
        if f"e{e['id']}" in live or _t.time() - (e.get("updated") or 0) < 900:
            continue
        en, fr = e.get("en_file"), e.get("french_path")
        if en and fr and os.path.exists(en) and os.path.exists(fr):
            core.set_ep_status(e["id"], "ready", progress="re-queued after interruption")
            core.log(f"resume {e['id']}: stale merge -> back on the merge queue")
            MERGE_WAKE.set()
        else:
            core.set_ep_status(e["id"], "pending", progress="",
                               error="merge interrupted and source file missing")
