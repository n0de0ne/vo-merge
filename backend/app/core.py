"""Config persistence + SQLite state + the per-movie pipeline state machine."""
import json, os, re, sqlite3, threading, time
from contextlib import contextmanager

CONFIG_DIR = os.environ.get("VO_CONFIG", "/config")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")
DB_FILE = os.path.join(CONFIG_DIR, "vo-merge.db")
LOG_FILE = os.path.join(CONFIG_DIR, "vo-merge.log")

# Pipeline states a movie moves through.
STATES = ["pending", "searching", "no_release", "grabbed", "downloading",
          "ready", "merging", "merged", "review", "sync_fail", "error", "ignored"]

DEFAULTS = {
    "prowlarr_url": "http://10.0.1.5:9696",
    "prowlarr_key": "",
    "radarr_url": "http://10.0.1.5:7878",
    "radarr_key": "",
    "qb_url": "http://10.0.1.5:1290",
    "qb_user": "admin",
    "qb_pass": "",
    "plex_url": "http://10.0.1.5:32400",
    "plex_token": "",
    "plex2_url": "",                       # optional replica PMS (e.g. http://10.0.1.2:32400)
    "plex2_token": "",                     # replica's own token (usually a different account)
    "plex_media_prefix": "/data",          # how Plex sees what this app mounts at /media
    "en_indexer_ids": [105, 107],          # The Pirate Bay, Nyaa.si
    "multi_indexer_ids": [],               # extra indexers to also search for MULTI (e.g. FR trackers)
    "vo_gap_tag": "vo-gap",
    "qb_category": "audio-merge",
    # qB's view of the save path. Lives inside the Plex share's hidden .Téléchargements (qB's
    # /data == /mnt/nvme/Plex), so donors share the library's pool instead of a separate share.
    "qb_download_dir": "/data/.Téléchargements/completed/audio-merge",
    "downloads_mount": "/media/.Téléchargements/completed/audio-merge",   # how THIS container sees them
    "media_mount": "/media",                       # this container's view of /mnt/user/Plex
    # After a successful merge, free the donor download. English/public-tracker donors are
    # deleted with their files; torrents whose tracker matches `french_trackers` are LEFT
    # seeding (the operator's seed-manager script handles those).
    "delete_donor": True,
    "french_trackers": [],                         # substrings of tracker URLs to KEEP seeding
    "no_seed_public": True,                        # public donors: stop at 100%, never seed
    "ai_tickets": True,                            # page the host AI dispatcher on wedges/errors
    "ai_stale_min": 60,                            # if the AI doesn't report back within this many
                                                   # minutes, flag the item for manual review
    "score_threshold": 60,
    "min_seeders": 5,
    "grab_mode": "auto",                   # auto | approval
    "scope_films": True,
    "scope_series": False,
    "series_pilot": ["The Neighborhood", "Friends", "My Wife and Kids"],  # only these series run (empty = all tagged)
    "sonarr_url": "http://10.0.1.3:8989",
    "sonarr_key": "",
    "sonarr_vo_gap_tag": "vo-gap",
    "qb_tv_category": "audio-merge-tv",
    "qb_tv_download_dir": "/data/.Téléchargements/completed/audio-merge-tv",
    "tv_pack_threshold": 6,                # >= this many gap eps in a season -> grab a season pack
    # ---- how the gap is decided ------------------------------------------------------------
    # "files": probe every library file with mkvmerge and believe the container (accurate, and
    #          the only thing that can't go stale). "tag": the old behaviour — trust Radarr's
    #          vo-gap tag / Sonarr's mediaInfo, both of which are import-time snapshots.
    "scan_mode": "files",
    "scan_all_movies": True,               # files mode: consider EVERY Radarr movie, not just
                                           # the tagged ones (the tag is what we're replacing)
    # The END STATE each kind of title should reach. A file is a gap when it is missing any of
    # these; the missing codes are recorded per record (need_audio / need_subs) and are what the
    # merge grafts off the donor. Anime keeps its Japanese VO on top of FR+EN.
    "lang_profiles": {
        "movie":  {"audio": ["fre", "eng"],        "subs": ["fre", "eng"]},
        "series": {"audio": ["fre", "eng"],        "subs": ["fre", "eng"]},
        "anime":  {"audio": ["fre", "eng", "jpn"], "subs": ["fre", "eng"]},
    },
    "anime_dirs": ["Anime"],               # top-level library folders that mean "anime"; a
                                           # Japanese-original title also gets the anime profile
    "series_dirs": ["Series"],             # ...and the ones that mean "TV series". Only used to
                                           # pick a profile for the coverage report, where there
                                           # is no Sonarr record to ask.
    "want_subs": True,                     # graft the donor's subtitles, not just its audio
    "max_sub_tracks": 2,                   # per language, keep at most this many (packs ship 6+)
    "subs_only_gap": True,                 # chase a file that has every target AUDIO language but
                                           # is missing a target SUBTITLE. Bazarr is the cheaper
                                           # tool for this (a 50 KB .srt from a subtitle
                                           # provider), but it only searches subtitle providers —
                                           # when the sub exists solely inside a RELEASE, an
                                           # indexer is the only place to get it. score_release
                                           # then prefers the SMALLEST release rather than a
                                           # matching resolution, since only the text is kept.
    "sync_tolerance_s": 2.0,
    "max_sync_retries": 4,                 # try this many different releases before giving up
    "sync_review": True,                   # low-confidence/inconclusive sync -> 'review' (human) instead of auto-reject
    "auto_sync": True,                     # auto-detect & correct constant A/V offset
    "auto_sync_min_conf": 0.2,             # min AUDIO cross-correlation confidence
    "sync_video_min_conf": 0.4,            # min VIDEO (scene-cut) confidence; video is primary
    "sync_window_start": 300,              # seconds into the film to start the analysis window
    "sync_window_dur": 480,                # analysis window length (s)
    "sync_windows": 4,                     # number of windows; need >=2 to agree (consensus)
    "sync_max_lag_s": 120,                 # largest constant offset that can be FOUND. This is a
                                           # ceiling, not a tuning knob: it was 20s, and a
                                           # BD-vs-WEB anime pair routinely differs by 30-60s (a
                                           # sponsor card the WEB carries, a "previously on" the
                                           # BD drops). Past the limit the true correlation peak
                                           # was sliced off before the argmax, the windows
                                           # disagreed, and it was reported as "different cut".
                                           # Widening is free (the FFT is already computed) and
                                           # safe (unrelated files don't correlate at any lag).
    "sync_ratio_test": True,               # test known transfer rate ratios (PAL 25fps vs 23.976
                                           # etc). Fixes the "framerates differ but no reliable
                                           # drift could be measured" dead-end.
    "sync_ratio_span": 2400,               # seconds of runtime scanned for the ratio test
    "sync_ratio_min_conf": 0.35,           # min correlation for a ratio to be accepted
    "sync_ratio_margin": 1.3,              # ...and it must beat the no-stretch hypothesis by this
    "sync_ffmpeg_threads": 4,              # cap decode threads (politeness)
    "sync_hwaccel": "vaapi",               # vaapi | qsv | none — offload decode to the iGPU
    "sync_hwaccel_device": "/dev/dri/renderD128",
    "no_release_retry_h": 24,              # a record that found nothing is re-searched after this
                                           # many hours. Indexers gain releases constantly, so
                                           # "nothing existed when we looked" must not be
                                           # permanent — but re-querying every hourly sweep for
                                           # titles that genuinely don't exist is just abuse.
                                           # 0 = retry on every scan, negative = never.
    "search_interval_min": 60,
    "finish_interval_min": 10,
    "stall_timeout_min": 5,                # an incomplete download not moving (no seeds/0 speed) for
                                           # this long is dropped + blocklisted -> grab another release
    "stall_check_interval_min": 3,         # how often the stall sweep runs (independent of merges)
    "promote_interval_min": 1,             # how often completed downloads are moved onto the
                                           # merge queue (cheap qB poll; keeps the UI honest)
    "dl_max_age_min": 720,                 # absolute cap: a download active this long (even if slowly
                                           # trickling) is dropped + blocklisted -> grab another release
    "meta_timeout_min": 2,                 # a magnet still fetching metadata after this long is dead
                                           # (no peer ever answered) -> drop it without the full wait
    "max_search_per_run": 25,              # cap new searches/grabs per cycle (ramp, don't flood)
    "max_parallel_merges": 1,              # how many merges may run at once. Merging is CPU/iGPU
                                           # heavy (sync detection + remux) — 1 is safest; raise it
                                           # only if the box has headroom. Applied live.
    "max_inflight_downloads": 5,           # flow control: never have more than this many downloads
                                           # in qB at once (a season pack counts as one). vo-merge
                                           # won't grab another until a merge finishes + donor is
                                           # freed, dropping the count below the cap.
    "enabled": False,                      # master switch; off until configured
    "webhook_token": "",                   # optional shared secret for /api/hook/*. Empty =
                                           # no check (matches the rest of this LAN-only API).
                                           # Set it and Radarr/Sonarr must send ?token=… .
    "paused": False,                       # temporary brake: no NEW searches, grabs or merges.
                                           # Work already in flight finishes (killing mkvmerge
                                           # mid-write would leave a corrupt file), so the load
                                           # drops as the current merge ends. Scans still run —
                                           # pausing is how you let a rescan finish undisturbed.
}

_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            cfg.update(json.load(open(CONFIG_FILE)))
        except Exception:
            pass
    _SECRETS.clear()
    _SECRETS.update(str(cfg[k]) for k in _SECRET_KEYS if cfg.get(k) and len(str(cfg[k])) >= 8)
    return cfg


def save_config(updates: dict):
    with _lock:
        cfg = load_config()
        cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
        os.makedirs(CONFIG_DIR, exist_ok=True)
        json.dump(cfg, open(CONFIG_FILE, "w"), indent=2)
    return load_config()


# Secrets travel inside the URLs we report. A Prowlarr download link carries `apikey=…` in its
# query string, so a failed grab used to store the live API key in the DB and render it in the
# Review tab (and in every AI ticket built from that record). Errors and log lines are written
# from dozens of places, so scrub in the two funnels they all pass through rather than at each
# call site.
_SECRET_QS = re.compile(r'((?:api_?key|apikey|token|passkey|rss_?key|auth|pass(?:word)?)=)[^&\s]+',
                        re.I)
_SECRET_KEYS = ("prowlarr_key", "radarr_key", "sonarr_key", "plex_token", "plex2_token",
                "qb_pass", "webhook_token")
_SECRETS = set()      # the configured values themselves, refreshed whenever config is read


def redact(text):
    """Strip credentials out of anything about to be persisted or shown."""
    if not text:
        return text
    out = _SECRET_QS.sub(r"\1***", str(text))
    for v in _SECRETS:                 # a key can also appear outside a query string
        out = out.replace(v, "***")
    return out


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact(msg)}"
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ticket(kind, summary, context=None, key=None, force=False):
    """File an issue ticket for the host's AI dispatcher (an Unraid user script cron
    that runs the Claude Code CLI on each ticket). Tickets land in /config/ai-tickets/
    which the host reads as appdata/vo-merge/ai-tickets/. A (kind,key) pair is filed
    only once (persisted in ai_tickets_filed.json) so a standing condition doesn't
    re-page after being handled. force=True (operator-initiated, e.g. the Review tab's
    Send-to-AI button) skips the once-only guard and overwrites a pending same-kind
    ticket. Returns True if a ticket was filed."""
    try:
        seen_path = os.path.join(CONFIG_DIR, "ai_tickets_filed.json")
        try:
            seen = set(json.load(open(seen_path)))
        except Exception:
            seen = set()
        k = f"{kind}:{key or ''}"
        if k in seen and not force:
            return False
        d = os.path.join(CONFIG_DIR, "ai-tickets")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"{kind}.json")
        if os.path.exists(path) and not force:
            return False                      # same kind already awaiting dispatch
        with open(path, "w") as f:
            json.dump({"app": "vo-merge", "kind": kind, "summary": summary,
                       "context": context or {},
                       "ts": time.strftime("%Y-%m-%dT%H:%M:%S")},
                      f, ensure_ascii=False, indent=1, default=str)
        seen.add(k)
        json.dump(sorted(seen)[-3000:], open(seen_path, "w"))
        log(f"AI-TICKET {kind}: {summary[:70]}")
        return True
    except Exception as e:
        log(f"ticket() failed: {e}")
        return False


def tail_log(n=300):
    if not os.path.exists(LOG_FILE):
        return []
    with open(LOG_FILE) as f:
        return f.readlines()[-n:]


@contextmanager
def db():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_FILE, timeout=30)
    conn.row_factory = sqlite3.Row
    # the merge worker writes concurrently with the scheduler + API threads
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with db() as c:
        # WAL: a background merge worker writes while the scheduler/API read — without it
        # concurrent access raises "database is locked" and aborts a whole stage.
        try:
            c.execute("PRAGMA journal_mode=WAL")
        except Exception:
            pass
        c.execute("""CREATE TABLE IF NOT EXISTS movies (
            tmdb_id INTEGER PRIMARY KEY,
            imdb_id TEXT, radarr_id INTEGER,
            title TEXT, original_title TEXT, year INTEGER,
            original_lang TEXT,
            french_path TEXT,            -- existing FR library file (container /media path)
            quality TEXT,                -- FR file quality e.g. Bluray-1080p
            status TEXT DEFAULT 'pending',
            candidate_title TEXT,        -- chosen EN release title
            candidate_score INTEGER,
            candidate_seeders INTEGER,
            dl_hash TEXT,                -- qB torrent hash
            en_file TEXT,                -- downloaded EN file (container path) once complete
            merged_file TEXT,
            sync_delta REAL,             -- duration delta base<->donor at merge time
            sync_offset_ms INTEGER DEFAULT 0,  -- manual override
            error TEXT,
            updated REAL,
            dl_id TEXT,                  -- identity (infohash) of the current release
            tried TEXT,                  -- JSON list of release identities already rejected
            attempts INTEGER DEFAULT 0
        )""")
        _ensure_cols(c, "movies", {"dl_id": "TEXT", "tried": "TEXT", "attempts": "INTEGER DEFAULT 0",
                                   "added_langs": "TEXT", "poster": "TEXT", "progress": "TEXT",
                                   "merged_at": "REAL",
                                   # AI-review round-trip: status the host dispatcher reports back
                                   "ai_status": "TEXT", "ai_verdict": "TEXT", "ai_at": "REAL",
                                   # rate-stretch ratio applied at merge (PAL etc); 1.0 = none
                                   "sync_drift": "REAL",
                                   # what the FILE actually holds (from mkvmerge, not metadata)
                                   "audio_langs": "TEXT", "sub_langs": "TEXT",
                                   "needs": "TEXT",          # audio | subs | audio+subs
                                   # which target languages are still missing (comma lists)
                                   "need_audio": "TEXT", "need_subs": "TEXT",
                                   "added_subs": "TEXT",
                                   # WHY this record is 'merged': grafted (we added tracks) |
                                   # replaced (we used the download as the file) | already (it
                                   # met its profile on its own — we did nothing). NULL = legacy.
                                   "merge_kind": "TEXT"})


def _ensure_cols(c, table, cols):
    have = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
    for col, decl in cols.items():
        if col not in have:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_tv():
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS episodes (
            id TEXT PRIMARY KEY,             -- f"{series_id}:{season}:{ep}"
            series_id INTEGER, series_title TEXT, tvdb_id INTEGER,
            season INTEGER, episode INTEGER,
            french_path TEXT,                -- existing FR episode file (container /media path)
            quality TEXT,
            status TEXT DEFAULT 'pending',
            candidate_title TEXT, candidate_score INTEGER, candidate_seeders INTEGER,
            dl_hash TEXT,                    -- qB torrent (shared across a season pack)
            en_file TEXT, merged_file TEXT,
            sync_offset_ms INTEGER DEFAULT 0, sync_delta REAL,
            error TEXT, updated REAL,
            dl_id TEXT, tried TEXT, attempts INTEGER DEFAULT 0 )""")
        _ensure_cols(c, "episodes", {"dl_id": "TEXT", "tried": "TEXT", "attempts": "INTEGER DEFAULT 0",
                                     "poster": "TEXT", "progress": "TEXT", "added_langs": "TEXT",
                                     "series_type": "TEXT DEFAULT 'standard'", "merged_at": "REAL",
                                     # AI-review round-trip (see movies table)
                                     "ai_status": "TEXT", "ai_verdict": "TEXT", "ai_at": "REAL",
                                     "sync_drift": "REAL",
                                     # what the FILE actually holds (see movies table)
                                     "audio_langs": "TEXT", "sub_langs": "TEXT",
                                     "needs": "TEXT", "added_subs": "TEXT",
                                     "need_audio": "TEXT", "need_subs": "TEXT",
                                     # series' original language — the merge needs the same
                                     # inputs the scan used, or the two pick different profiles
                                     "orig_lang": "TEXT",
                                     "merge_kind": "TEXT"})   # see movies table


def init_probe_cache():
    """Cache of what each library file actually contains, keyed by path and invalidated by
    (size, mtime). A scan of a few thousand files would otherwise be a few thousand mkvmerge
    calls every cycle; with this, only files that changed on disk are re-read."""
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS probes (
            path TEXT PRIMARY KEY,
            size INTEGER, mtime REAL, probed REAL,
            dur REAL, fps REAL,
            auds TEXT,               -- comma-joined canonical audio language codes
            subs TEXT,               -- comma-joined canonical subtitle language codes
            ntracks INTEGER,         -- audio track count (0 with auds='' means unreadable)
            err TEXT )""")
        # Fingerprint of the EXTERNAL subtitle files beside the media file. The .mkv's own size
        # and mtime don't change when Bazarr drops an .srt next to it, so without this the cache
        # would answer from a probe taken before the subtitle existed and never re-read it.
        _ensure_cols(c, "probes", {"sidecars": "TEXT"})


def get_probe(path, sidecars=None):
    """Cached probe row for `path`, but only if the file on disk still matches it.

    `sidecars` is the caller's current external-subtitle fingerprint (media.sidecar_subs); when
    it differs from the stored one a subtitle file has been added, removed or replaced beside
    the media file, so the cached subtitle set is stale even though the .mkv is untouched."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    with db() as c:
        r = c.execute("SELECT * FROM probes WHERE path=?", (path,)).fetchone()
    if not r:
        return None
    if int(r["size"] or -1) != st.st_size or abs((r["mtime"] or 0) - st.st_mtime) > 1:
        return None
    if sidecars is not None and (r["sidecars"] or "") != sidecars:
        return None
    return dict(r)


def put_probe(path, dur=None, fps=None, auds="", subs="", ntracks=0, err=None, sidecars=""):
    try:
        st = os.stat(path)
        size, mtime = st.st_size, st.st_mtime
    except OSError:
        size, mtime = None, None
    with db() as c:
        c.execute("""INSERT INTO probes (path,size,mtime,probed,dur,fps,auds,subs,ntracks,err,
                                         sidecars)
                     VALUES (?,?,?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(path) DO UPDATE SET size=excluded.size,mtime=excluded.mtime,
                       probed=excluded.probed,dur=excluded.dur,fps=excluded.fps,
                       auds=excluded.auds,subs=excluded.subs,ntracks=excluded.ntracks,
                       err=excluded.err,sidecars=excluded.sidecars""",
                  (path, size, mtime, time.time(), dur, fps, auds, subs, ntracks, err, sidecars))


def forget_probe(path):
    """Drop a cached probe (after a merge rewrites the file in place)."""
    with db() as c:
        c.execute("DELETE FROM probes WHERE path=?", (path,))


def prune_missing_probes():
    """Drop probe rows whose file no longer exists. Nothing else ever removes them, so a library
    that has had titles deleted keeps counting them forever — which quietly skews every coverage
    percentage. Returns how many were dropped."""
    with db() as c:
        paths = [r["path"] for r in c.execute("SELECT path FROM probes")]
    gone = [p for p in paths if not os.path.exists(p)]
    for i in range(0, len(gone), 400):          # chunked: SQLite caps variables per statement
        chunk = gone[i:i + 400]
        with db() as c:
            c.execute(f"DELETE FROM probes WHERE path IN ({','.join('?' * len(chunk))})", chunk)
    return len(gone)


# A record in one of these is mid-flight: a merge writes a new file and swaps it in, so the
# library path can be absent for a moment. Never prune those — a prune during that window would
# delete the record whose merge is about to finish.
_PRUNE_SKIP = ("downloading", "ready", "merging", "grabbed", "searching")


def prune_missing_records():
    """Drop movie/episode records whose library file is gone — the title was deleted from the
    library, so the record can never be acted on and only clutters the counts. Returns
    (movies, episodes) removed."""
    out = []
    skip = ",".join("?" * len(_PRUNE_SKIP))
    for table, key in (("movies", "tmdb_id"), ("episodes", "id")):
        with db() as c:
            rows = [(r[key], r["french_path"]) for r in
                    c.execute(f"SELECT {key}, french_path FROM {table} "
                              f"WHERE status NOT IN ({skip})", _PRUNE_SKIP)]
        gone = [k for k, p in rows if p and not os.path.exists(p)]
        for i in range(0, len(gone), 400):
            chunk = gone[i:i + 400]
            with db() as c:
                c.execute(f"DELETE FROM {table} WHERE {key} IN ({','.join('?' * len(chunk))})", chunk)
        out.append(len(gone))
    return tuple(out)


def probe_stats():
    with db() as c:
        r = c.execute("SELECT COUNT(*) n, SUM(err IS NOT NULL) bad FROM probes").fetchone()
    return {"cached": r["n"] or 0, "unreadable": r["bad"] or 0}


def upsert_episode(e: dict):
    cols = ["id", "series_id", "series_title", "tvdb_id", "season", "episode",
            "french_path", "quality", "poster", "series_type"]
    with db() as c:
        ex = c.execute("SELECT status FROM episodes WHERE id=?", (e["id"],)).fetchone()
        if ex:
            c.execute("""UPDATE episodes SET series_title=?,tvdb_id=?,french_path=?,quality=?,poster=?,series_type=?,updated=?
                         WHERE id=?""",
                      (e["series_title"], e["tvdb_id"], e["french_path"], e["quality"],
                       e.get("poster"), e.get("series_type", "standard"), time.time(), e["id"]))
        else:
            c.execute(f"INSERT INTO episodes ({','.join(cols)},updated) "
                      f"VALUES ({','.join('?'*len(cols))},?)",
                      tuple(e.get(k) for k in cols) + (time.time(),))


def set_ep_status(ep_id, status, **fields):
    _set_row("episodes", "id", ep_id, status, fields)


def get_episodes(status=None):
    with db() as c:
        if status:
            rows = c.execute("SELECT * FROM episodes WHERE status=? ORDER BY updated DESC", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM episodes ORDER BY series_title,season,episode").fetchall()
    return [dict(r) for r in rows]


def get_episode(ep_id):
    with db() as c:
        r = c.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
    return dict(r) if r else None


def ep_status_counts():
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM episodes GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


def upsert_movie(m: dict):
    cols = ["tmdb_id", "imdb_id", "radarr_id", "title", "original_title", "year",
            "original_lang", "french_path", "quality", "poster"]
    with db() as c:
        existing = c.execute("SELECT tmdb_id FROM movies WHERE tmdb_id=?",
                             (m["tmdb_id"],)).fetchone()
        if existing:
            c.execute("""UPDATE movies SET imdb_id=?,radarr_id=?,title=?,original_title=?,
                         year=?,original_lang=?,french_path=?,quality=?,poster=?,updated=?
                         WHERE tmdb_id=?""",
                      (m["imdb_id"], m["radarr_id"], m["title"], m["original_title"],
                       m["year"], m["original_lang"], m["french_path"], m["quality"],
                       m.get("poster"), time.time(), m["tmdb_id"]))
        else:
            c.execute(f"""INSERT INTO movies ({','.join(cols)},updated)
                          VALUES ({','.join('?'*len(cols))},?)""",
                      tuple(m.get(k) for k in cols) + (time.time(),))


def _set_row(table, key_col, key, status, fields):
    """The one writer for both tables. Two timestamps that look incidental decide what the whole
    Overview shows, and both used to be re-stamped by writes that changed nothing:

    - **`updated` means "when the pipeline state last changed"**, not "when we last touched the
      row". It is the sort key for Needs attention and the FIFO order of the merge queue. But
      every scan re-writes each record with its CURRENT status just to refresh the language
      columns, and the AI sweep re-writes error records to stamp `ai_status` — so a record that
      had not changed in days jumped to the top of the panel whenever a scan or the 3-minute
      sweep ran. The CASE keeps the old value when the status is unchanged; SQLite evaluates
      every SET expression against the ORIGINAL row, so it sees the stored status even though
      the same statement is assigning a new one.
    - **`merged_at` is stamped on the TRANSITION into merged, and never again.** `setdefault`
      only checked whether the CALLER passed one, not whether the row already had one, so every
      later write with status='merged' — including a scan re-reading an already-merged file —
      reset it to now, floating old merges back into "Recently merged" and inflating the 24h/7d
      counters. Keying off the transition (rather than "is it NULL") also leaves pre-column rows
      alone, so an upgrade doesn't dump the whole back catalogue into "Recently merged" at once;
      those fall back to `updated`, which is now stable too.

    Pass `updated=<ts>` explicitly to force a bump."""
    if fields.get("error"):        # errors quote URLs, which carry apikey=... in the query string
        fields["error"] = redact(fields["error"])
    now = fields.pop("updated", None)
    forced = now is not None
    now = time.time() if now is None else now
    stamp = fields.pop("merged_at", None) or now
    fields["status"] = status
    sets = ",".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    if forced:
        sets += ",updated=?"; vals.append(now)
    else:
        sets += ",updated=CASE WHEN status=? THEN updated ELSE ? END"; vals += [status, now]
    if status == "merged":
        sets += ",merged_at=CASE WHEN status=? THEN merged_at ELSE ? END"
        vals += ["merged", stamp]
    with db() as c:
        c.execute(f"UPDATE {table} SET {sets} WHERE {key_col}=?", tuple(vals) + (key,))


def set_status(tmdb_id, status, **fields):
    _set_row("movies", "tmdb_id", tmdb_id, status, fields)


def claim_movie(tmdb_id, from_status, to_status, **fields):
    """Atomically move a movie from one status to another. Returns True only if THIS caller
    won the transition — the merge worker uses it to claim a queued item so a concurrent
    claimer (or a re-entrant sweep) can never merge the same file twice."""
    fields["status"] = to_status
    fields["updated"] = time.time()
    keys = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        cur = c.execute(f"UPDATE movies SET {keys} WHERE tmdb_id=? AND status=?",
                        tuple(fields.values()) + (tmdb_id, from_status))
        return cur.rowcount == 1


def claim_episode(ep_id, from_status, to_status, **fields):
    """Episode mirror of claim_movie."""
    fields["status"] = to_status
    fields["updated"] = time.time()
    keys = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        cur = c.execute(f"UPDATE episodes SET {keys} WHERE id=? AND status=?",
                        tuple(fields.values()) + (ep_id, from_status))
        return cur.rowcount == 1


def get_movies(status=None):
    with db() as c:
        if status:
            rows = c.execute("SELECT * FROM movies WHERE status=? ORDER BY updated DESC",
                            (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM movies ORDER BY updated DESC").fetchall()
    return [dict(r) for r in rows]


def get_movie(tmdb_id):
    with db() as c:
        r = c.execute("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()
    return dict(r) if r else None


def status_counts():
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM movies GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
