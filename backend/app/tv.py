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
from .pipeline import (probe, _video_quality, _pick_link, _hash_from_magnet,
                       FR_DUB, EN_OK, RES, SRC)

SXXEXX = re.compile(r'[Ss](\d{1,3})[Ee](\d{1,4})')
VIDEXT = (".mkv", ".mp4", ".m4v", ".avi", ".ts")


def _fr_only(al):
    if not al:
        return False
    parts = [x.strip().lower() for x in al.replace("/", ",").split(",")]
    return bool(parts) and all(p.startswith("fr") for p in parts)


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
        for f in files:
            mi = f.get("mediaInfo") or {}
            if not _fr_only(mi.get("audioLanguages")):
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
            })
            n += 1
    core.log(f"tv scan: {n} French-only episodes (pilot={sorted(pilot) or 'all'})")
    return n


# ------------------------------------------------------------------ SEARCH + GRAB (hybrid)
def _grab(link, savepath, cfg):
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    qb.login()
    qb.create_category(cfg["qb_tv_category"], cfg["qb_tv_download_dir"])
    resp = qb.add(link, cfg["qb_tv_category"], savepath)
    ids = resp.get("added_torrent_ids") if isinstance(resp, dict) else None
    return (ids[0] if ids else None) or _hash_from_magnet(link)


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
        if FR_DUB.search(t) and not EN_OK.search(t):
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
        link = _pick_link(r)
        if best is None or sc > best[0]:
            best = (sc, r.get("seeders") or 0, t, link)
    return best


def stage_search(cfg=None):
    cfg = cfg or core.load_config()
    if not (cfg["enabled"] and cfg["scope_series"]):
        return
    pend = core.get_episodes("pending")
    # group by (series, season)
    by_season = defaultdict(list)
    for e in pend:
        by_season[(e["series_id"], e["series_title"], e["season"])].append(e)
    for (sid, title, season), eps in by_season.items():
        if len(eps) >= cfg["tv_pack_threshold"]:
            q = f"{title} S{season:02d}"
            best = _search(q, cfg, want_pack=True, season=season)
            if best:
                sc, seed, rtitle, link = best
                h = _grab(link, f"{cfg['qb_tv_download_dir']}/{sid}_S{season:02d}", cfg)
                for e in eps:
                    core.set_ep_status(e["id"], "downloading", dl_hash=h,
                                       candidate_title=rtitle, candidate_score=sc, candidate_seeders=seed)
                core.log(f"tv grab PACK '{q}': [{sc}] {seed}s {rtitle}")
                continue
            # no pack -> fall through to per-episode
        for e in eps:
            q = f"{title} S{e['season']:02d}E{e['episode']:02d}"
            best = _search(q, cfg, season=e["season"], ep=e["episode"])
            if not best or best[0] < cfg["min_seeders"]:
                core.set_ep_status(e["id"], "no_release",
                                   candidate_title=(best[2] if best else None))
                continue
            sc, seed, rtitle, link = best
            h = _grab(link, f"{cfg['qb_tv_download_dir']}/{e['id'].replace(':','_')}", cfg)
            core.set_ep_status(e["id"], "downloading", dl_hash=h,
                               candidate_title=rtitle, candidate_score=sc, candidate_seeders=seed)
            core.log(f"tv grab EP '{q}': [{sc}] {seed}s {rtitle}")


# ------------------------------------------------------------------ FINISH (map + merge)
def _merge_episode(ep, en_file, cfg):
    """Merge: keep better video, graft the other language's audio, replace FR file in place."""
    from . import sync
    from .clients import Sonarr as _S
    fr = ep["french_path"]
    if not (os.path.exists(en_file) and os.path.exists(fr)):
        core.set_ep_status(ep["id"], "error", error="merge: file missing"); return
    ei, fi = probe(en_file), probe(fr)
    if not ei or not fi:
        core.set_ep_status(ep["id"], "error", error="merge: probe failed"); return
    # MULTI episode: download already has both langs -> remux directly, no merge
    if {"eng", "fre"} <= {a["lang"] for a in ei["auds"]}:
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
        else:
            core.set_ep_status(ep["id"], "error", error="multi remux failed")
        return
    delta = abs((ei["dur"] or 0) - (fi["dur"] or 0))
    offset = ep.get("sync_offset_ms") or 0
    if not offset and not sync.fps_close(ei["fps"], fi["fps"]):
        core.set_ep_status(ep["id"], "sync_fail", sync_delta=delta,
                           error=f"framerate differs ({ei['fps']} vs {fi['fps']})"); return
    eq, fq = _video_quality(en_file, ei["dur"]), _video_quality(fr, fi["dur"])
    base, bi, donor, di = (fr, fi, en_file, ei) if fq >= eq else (en_file, ei, fr, fi)
    have = {a["lang"] for a in bi["auds"]}
    ids, langs, daidx = [], {}, {}
    for ix, a in enumerate(di["auds"]):
        if a["lang"] == "und" or a["lang"] in have:
            continue
        ids.append(a["id"]); langs[a["id"]] = a["lang"]; daidx[a["id"]] = ix; have.add(a["lang"])
    if "eng" not in have or not ids:
        core.set_ep_status(ep["id"], "error", error="merge: no English audio to add"); return
    drift = None
    if not offset and cfg.get("auto_sync", True):
        m, conf, method, drift = sync.detect(base, donor, 0, daidx[ids[0]],
                                              min(ei["dur"] or 0, fi["dur"] or 0), cfg, tag=f" {ep['id']}")
        if m is None:
            core.set_ep_status(ep["id"], "sync_fail", sync_delta=delta,
                               error="couldn't sync (incompatible release/cut)"); return
        if abs(m) >= 40 or drift:
            offset = int(round(m))
        core.log(f"tv sync {ep['id']}: {offset:+d}ms{' drift' if drift else ''} ({method} conf {conf:.2f})")
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
                       error=None, added_langs=",".join(sorted({langs[i] for i in ids})))
    core.log(f"tv merge {ep['id']}: OK +{offset}ms -> {os.path.basename(fr)}")
    try:
        _S(cfg["sonarr_url"], cfg["sonarr_key"]).rescan(ep["series_id"])
    except Exception:
        pass


def stage_finish(cfg=None):
    cfg = cfg or core.load_config()
    if not (cfg["enabled"] and cfg["scope_series"]):
        return
    qb = QBittorrent(cfg["qb_url"], cfg["qb_user"], cfg["qb_pass"])
    try:
        qb.login()
        torrents = {t["hash"]: t for t in qb.torrents(cfg["qb_tv_category"])}
    except Exception as e:
        core.log(f"tv finish: qB error {e}"); return
    downloading = core.get_episodes("downloading")
    # group episodes by their torrent hash; map each completed torrent's files to episodes
    by_hash = defaultdict(list)
    for e in downloading:
        by_hash[e.get("dl_hash")].append(e)
    for h, eps in by_hash.items():
        t = torrents.get(h)
        if not t or t.get("progress", 0) < 1.0:
            continue
        save = (t.get("content_path") or t.get("save_path") or "")
        local = save.replace(cfg["qb_tv_download_dir"], cfg["qb_tv_download_dir"], 1)  # same mount
        # index downloaded video files by (season,episode)
        files = {}
        root = local if os.path.isdir(local) else os.path.dirname(local)
        for r, _, fs in os.walk(root):
            for f in fs:
                if f.lower().endswith(VIDEXT):
                    m = SXXEXX.search(f)
                    if m:
                        files[(int(m.group(1)), int(m.group(2)))] = os.path.join(r, f)
        for e in eps:
            vid = files.get((e["season"], e["episode"]))
            if not vid:
                continue
            core.set_ep_status(e["id"], "ready", en_file=vid)
            _merge_episode(core.get_episode(e["id"]), vid, cfg)
