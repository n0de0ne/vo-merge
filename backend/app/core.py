"""Config persistence + SQLite state + the per-movie pipeline state machine."""
import json, os, sqlite3, threading, time
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
    "exclude_french_origin": True,
    "sync_tolerance_s": 2.0,
    "max_sync_retries": 4,                 # try this many different releases before giving up
    "sync_review": True,                   # low-confidence/inconclusive sync -> 'review' (human) instead of auto-reject
    "auto_sync": True,                     # auto-detect & correct constant A/V offset
    "auto_sync_min_conf": 0.2,             # min AUDIO cross-correlation confidence
    "sync_video_min_conf": 0.4,            # min VIDEO (scene-cut) confidence; video is primary
    "sync_window_start": 300,              # seconds into the film to start the analysis window
    "sync_window_dur": 480,                # analysis window length (s)
    "sync_windows": 4,                     # number of windows; need >=2 to agree (consensus)
    "sync_ffmpeg_threads": 4,              # cap decode threads (politeness)
    "sync_hwaccel": "vaapi",               # vaapi | qsv | none — offload decode to the iGPU
    "sync_hwaccel_device": "/dev/dri/renderD128",
    "search_interval_min": 60,
    "finish_interval_min": 10,
    "stall_timeout_min": 5,                # an incomplete download not moving (no seeds/0 speed) for
                                           # this long is dropped + blocklisted -> grab another release
    "stall_check_interval_min": 3,         # how often the stall sweep runs (independent of merges)
    "promote_interval_min": 1,             # how often completed downloads are moved onto the
                                           # merge queue (cheap qB poll; keeps the UI honest)
    "dl_max_age_min": 720,                 # absolute cap: a download active this long (even if slowly
                                           # trickling) is dropped + blocklisted -> grab another release
    "max_search_per_run": 25,              # cap new searches/grabs per cycle (ramp, don't flood)
    "max_inflight_downloads": 5,           # flow control: never have more than this many downloads
                                           # in qB at once (a season pack counts as one). vo-merge
                                           # won't grab another until a merge finishes + donor is
                                           # freed, dropping the count below the cap.
    "enabled": False,                      # master switch; off until configured
}

_lock = threading.Lock()


def load_config():
    cfg = dict(DEFAULTS)
    if os.path.exists(CONFIG_FILE):
        try:
            cfg.update(json.load(open(CONFIG_FILE)))
        except Exception:
            pass
    return cfg


def save_config(updates: dict):
    with _lock:
        cfg = load_config()
        cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
        os.makedirs(CONFIG_DIR, exist_ok=True)
        json.dump(cfg, open(CONFIG_FILE, "w"), indent=2)
    return load_config()


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
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
                                   "ai_status": "TEXT", "ai_verdict": "TEXT", "ai_at": "REAL"})


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
                                     "ai_status": "TEXT", "ai_verdict": "TEXT", "ai_at": "REAL"})


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
    fields["status"] = status; fields["updated"] = time.time()
    if status == "merged":                 # stamp once; later `updated` churn won't touch it
        fields.setdefault("merged_at", time.time())
    keys = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE episodes SET {keys} WHERE id=?", tuple(fields.values()) + (ep_id,))


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


def set_status(tmdb_id, status, **fields):
    fields["status"] = status
    fields["updated"] = time.time()
    if status == "merged":                 # stamp once; later `updated` churn won't touch it
        fields.setdefault("merged_at", time.time())
    keys = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        c.execute(f"UPDATE movies SET {keys} WHERE tmdb_id=?",
                  tuple(fields.values()) + (tmdb_id,))


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
