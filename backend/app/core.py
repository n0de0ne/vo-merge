"""Config persistence + SQLite state + the per-movie pipeline state machine."""
import json, os, re, shutil, sqlite3, subprocess, threading, time
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
    # Prowlarr's numeric indexer IDs are per-INSTANCE, so shipping one install's values means a
    # fresh deployment silently queries indexers that don't exist there — and an empty result is
    # then recorded as "no release exists". Empty = search every configured indexer.
    "en_indexer_ids": [],
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
    # How the host AI dispatcher reaches this API, e.g. "http://10.0.1.5:8090". Written into every
    # ticket; empty = tell it only the in-container address. This was hard-coded to one install's
    # LAN address, as was the CLAUDE.md path below.
    "api_url": "",
    "docs_path": "",                               # host path to CLAUDE.md, for the ticket brief
    # A title arriving via the *arr webhook is one that someone has just ASKED for — that is what
    # an import event means — so it jumps the queue instead of joining the back of a backlog it
    # would never reach. Set 0 to disable. If you bulk-import a whole library the hook fires for
    # everything and priority stops distinguishing anything, which degrades to the old ordering
    # rather than breaking; turn it off for the duration of an import if that matters.
    "priority_on_import": 1,
    "ai_tickets": True,                            # page the host AI dispatcher on wedges/errors
    "ai_stale_min": 60,                            # if the AI doesn't report back within this many
                                                   # minutes, flag the item for manual review
    "ai_max_tickets": 50,                          # cap on QUEUED (undispatched) per-record
                                                   # tickets. One bad season pack is 400 episodes;
                                                   # filing all 400 at once buries the queue for
                                                   # hours. Records beyond the cap stay unpaged
                                                   # (and unstamped — the staleness timer must not
                                                   # run on a page that was never sent) and are
                                                   # picked up as the queue drains.
    "ai_dispatcher_alarm_min": 120,                # alarm when the OLDEST queued ticket has waited
                                                   # this long and the dispatcher heartbeat is
                                                   # absent/stale. Generous by default so a legacy
                                                   # hourly host cron (which keeps no heartbeat)
                                                   # doesn't false-alarm.
    # ---- the out-of-band alarm channel (notify.py) -----------------------------------------
    # Empty = off. A Discord/Slack webhook URL gets their JSON envelope; anything else gets an
    # ntfy-style plain POST with a Title header. This is for the automation's OWN failures —
    # dead dispatcher, broken config, full disk, prolonged dependency outage, records needing a
    # human — not per-merge chatter.
    "notify_url": "",
    "notify_repeat_h": 24,                         # one alarm per kind per this many hours; a
                                                   # condition observed healthy again re-arms
                                                   # immediately
    "dep_down_alarm_min": 60,                      # a dependency (Prowlarr/qB/*arr) continuously
                                                   # unreachable this long raises an alarm. Every
                                                   # sweep already logs-and-returns per cycle;
                                                   # this is the part that remembers DURATION.
    "disk_floor_gb": 10,                           # alarm (and, see merge gate, hold merges) when
                                                   # free space under media_mount or /config
                                                   # drops below this
    "score_threshold": 60,
    "min_seeders": 5,
    "grab_mode": "auto",                   # auto | approval
    "scope_films": True,
    "scope_series": False,
    # Which series the pipeline acts on; empty = all of them. This used to ship three personal
    # show names, which quietly limited every other install to those three.
    "series_pilot": [],
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
    # merge grafts off the donor. Anime keeps its ORIGINAL audio on top of FR+EN — `orig` is a
    # token resolved per title from Radarr/Sonarr's originalLanguage, not a literal `jpn`, because
    # not every show filed as anime is Japanese (Arcane is French).
    "lang_profiles": {
        "movie":  {"audio": ["fre", "eng"],         "subs": ["fre", "eng"]},
        "series": {"audio": ["fre", "eng"],         "subs": ["fre", "eng"]},
        "anime":  {"audio": ["fre", "eng", "orig"], "subs": ["fre", "eng"]},
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
    "sync_wide_probe": True,               # before PARKING a sync failure (review/sync_fail),
                                           # re-run detection once at ±sync_probe_lag_s. This is
                                           # the first line of the AI runbook ("call /sync_probe
                                           # FIRST") executed by the pipeline itself: a large
                                           # constant offset (sponsor card, 'previously on')
                                           # reads as "different cut" inside ±sync_max_lag_s and
                                           # is trivially fixable further out.
    "sync_probe_lag_s": 300,               # how far the parking-rescue probe searches
    "transient_max": 5,                    # consecutive infrastructure failures (grab hiccup,
                                           # unreadable base, qB blip) a record retries by
                                           # itself before it becomes a real `error` and pages
                                           # the AI. Retrying is cheaper than an agent run for
                                           # everything a retry can fix.
    "sync_ratio_span": 2400,               # seconds of runtime scanned for the ratio test
    "sync_ratio_min_conf": 0.35,           # min correlation for a ratio to be accepted
    "sync_ratio_margin": 1.3,              # ...and it must beat the no-stretch hypothesis by this
    "postmerge_qc": True,                  # after a graft, cross-correlate the grafted audio
                                           # against the base's own track IN THE OUTPUT before
                                           # the library swap. A confident-but-wrong sync is the
                                           # one failure nothing downstream can ever detect: the
                                           # language reads as present, the record closes, the
                                           # donor is deleted. Costs seconds per merge.
    "qc_min_conf": 0.35,                   # below this the QC verdict is 'inconclusive' and the
                                           # merge is ACCEPTED — absence of evidence is not
                                           # evidence of misalignment
    "qc_max_offset_ms": 1500,              # a confident residual offset beyond this rejects the
                                           # merge (blocklist donor, try another release)
    # ---- lip-sync: the only ABSOLUTE sync signal we have (lipsync.py) ----------------------
    # Every other measurement in this app is RELATIVE — donor audio against base audio, donor cuts
    # against base cuts. All of them are satisfied by two files that agree with each other and are
    # both wrong, which is exactly what a donor with a leader and a base with the same leader
    # produces. Correlating mouth movement in the PICTURE against the speech envelope of a track
    # answers a different question: is this audio in sync with what is on screen. It is also the
    # only thing that can measure a library file's OWN sync, with no donor at all.
    "lipsync_enabled": True,               # allow the /lipsync endpoints and the rescue rung
    "lipsync_qc": False,                   # additionally verify every graft this way. Off by
                                           # default: it costs a second decode pass per merge, and
                                           # postmerge_qc already catches the common failure.
    "lipsync_rescue": True,                # when window + ratio detection have both failed, try
                                           # lip-sync before parking the record. This is the rung
                                           # that turns "different cut, manual pick or ignore" into
                                           # a number — see docs/AUTONOMY.md.
    "lipsync_windows": 6,                  # sampling windows across the runtime
    "lipsync_window_dur": 24,              # seconds per window (dialogue-dense enough to correlate)
    "lipsync_fps": 12,                     # frames/s sampled; syllables run 2-8 Hz, so 12 is
                                           # comfortably above Nyquist and keeps the decode cheap
    "lipsync_max_lag_s": 4.0,              # search bound. Lip-sync error beyond a few seconds is
                                           # not a lip-sync problem, it is a different cut.
    "lipsync_min_conf": 0.30,              # per-window correlation floor
    "lipsync_min_windows": 3,              # windows that must agree before a verdict is reported
    "recycle_keep_days": 7,                # originals DISCARDED by a replacement (_place_multi /
                                           # TV direct remux) go to <media>/.vo-merge-recycle for
                                           # this many days instead of being destroyed, so a bad
                                           # replacement is reversible by machine. 0 = old
                                           # destructive behaviour. Grafts don't recycle: their
                                           # output carries every track the original had.
    "mux_timeout_min": 240,                # kill an mkvmerge that runs longer than this. It is a
                                           # deadlock guard, not a tuning knob — a wedged mux (a
                                           # stalled /mnt/user read, a hung iGPU decode) held the
                                           # merge worker forever, and at max_parallel_merges=1
                                           # that silently stops ALL merging with no error.
    "sync_decode_timeout_min": 30,         # ...and the same for one sync-detection decode pass,
                                           # which reads a bounded window (sync_window_dur) and so
                                           # can never legitimately take this long.
    "sync_ffmpeg_threads": 4,              # cap decode threads (politeness)
    "sync_hwaccel": "vaapi",               # vaapi | qsv | none — offload decode to the iGPU
    "sync_hwaccel_device": "/dev/dri/renderD128",
    "tried_ttl_days": 30,                  # a blocklisted release becomes eligible again after
                                           # this many days. The blocklist only ever grew, so a
                                           # release that stalled ONCE (0 seeds on a bad day) was
                                           # burned forever — for some titles that is the only
                                           # release that exists. 0/negative = never expire.
    "no_release_escalate_rounds": 3,       # a no_release record whose built-in query has come
                                           # back empty this many separate rounds is paged to
                                           # the AI once with a compose-a-better-query brief —
                                           # /search_releases exists for exactly these, but
                                           # no_release was never escalated. 0 = off.
    "ignored_revisit_days": 90,            # a record `ignored` this long is re-examined with ONE
                                           # cheap search: "no release exists" decays as truth,
                                           # and without a revisit the verdict is permanent by
                                           # accident. Finds something usable -> re-opened;
                                           # still nothing -> sleeps another cycle. 0 = never.
    "ignored_revisit_per_day": 10,         # cap on revisits per housekeeping day, so a large
                                           # ignored backlog doesn't hammer the indexers
    "auto_repair": False,                  # run the audio-less repair pass (delete via the *arr
                                           # + re-search) on the daily housekeeping schedule.
                                           # Off by default: it deletes media. Its guards are the
                                           # strong ones either way — cache-bypassed re-probe,
                                           # BROKEN_ERR only, *arr-known files only.
    "donor_keep_days": 14,                 # donors kept for parked failure states (review/
                                           # sync_fail/error — kept so /assign and /set_sync can
                                           # still use them) are freed after this long. With a
                                           # dead or ignoring actor they otherwise pin gigabytes
                                           # forever. 0 = keep forever (old behaviour).
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
    "history_keep_days": 90,               # how long the state-transition log (core.events) is
                                           # kept. It is what every chart on the Overview is drawn
                                           # from; 0 disables the daily prune, never the writing.
    "db_backup_keep": 7,                   # nightly VACUUM INTO /config/backup/, keeping this
                                           # many daily snapshots (0 = no backup). The DB is the
                                           # probe inventory plus every record's state, and it
                                           # runs in WAL — a plain file copy of it is torn.
    "enabled": False,                      # master switch; off until configured
    "webhook_token": "",                   # optional shared secret for /api/hook/*. Empty =
                                           # no check (matches the rest of this LAN-only API).
                                           # Set it and Radarr/Sonarr must send ?token=… .
    "api_key": "",                         # optional shared secret for the WHOLE API. Empty = no
                                           # check, which is the historical behaviour. Set it and
                                           # every /api call must send `X-API-Key: …` (or
                                           # ?apikey=…); /api/hook/* is exempt because the *arrs
                                           # can't be taught an extra header and already have
                                           # webhook_token. Cross-origin POSTs are refused
                                           # regardless — see main._guard.
    "paused": False,                       # temporary brake: no NEW searches, grabs or merges.
                                           # Work already in flight finishes (killing mkvmerge
                                           # mid-write would leave a corrupt file), so the load
                                           # drops as the current merge ends. Scans still run —
                                           # pausing is how you let a rescan finish undisturbed.
}

_lock = threading.Lock()


# Whether the persisted config currently fails to parse, and which file state we already
# complained about. `load_config` runs on EVERY API request and every scheduler tick, so logging
# the failure each time would bury the real history — the log rotates at 8 MB keeping 3 files, so
# a broken config would churn through all of it in minutes. Complain once per distinct
# (mtime, size), i.e. once per edit.
_CONFIG_BROKEN = {"at": 0.0, "reported": None}

# The last config that parsed, kept so the broken-config alarm can still reach the notify URL
# that is trapped inside the file it cannot read. In-memory only: after a restart into a broken
# config the alarm degrades to ticket + log, which is still infinitely better than silence.
_LAST_GOOD_CFG = None


def load_config():
    global _LAST_GOOD_CFG
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_FILE) as f:
            raw = f.read()
    except OSError:
        # No file at all is the normal first-run state — and it is also how an operator ACTS ON
        # the message below, which says "fix or remove the file". Clearing the flag here is what
        # makes removing it work; keying only off a successful parse left the process refusing to
        # save for its whole lifetime against a file that no longer existed.
        _CONFIG_BROKEN.update(at=0.0, reported=None)
        raw = None
    if raw is not None:
        try:
            cfg.update(json.loads(raw))
            _CONFIG_BROKEN.update(at=0.0, reported=None)
            # Remember the last config that PARSED. When the file breaks, the broken copy holds
            # the notify URL we would use to say so — the one credential the failure itself
            # hides — so the alarm below reads it from here instead.
            _LAST_GOOD_CFG = dict(cfg)
        except Exception as e:
            # Silently falling back to DEFAULTS is how a truncated config.json erased an install:
            # every URL and key reads as empty, `enabled` flips to False, and the next save_config
            # — which starts from this very dict — writes the defaults back over the real file.
            # Say so loudly, and refuse to save over it (see save_config).
            try:
                st = os.stat(CONFIG_FILE)
                fingerprint = (st.st_mtime, st.st_size)
            except OSError:
                fingerprint = None
            if _CONFIG_BROKEN["reported"] != fingerprint:
                _CONFIG_BROKEN["reported"] = fingerprint
                log(f"config: {CONFIG_FILE} could not be parsed ({e}) — running on DEFAULTS and "
                    f"REFUSING to overwrite it. Fix or remove the file.")
                # A broken config doesn't just degrade — with `enabled` defaulting False it
                # STOPS the whole pipeline, and until now the only trace was the log line
                # above. Page the dispatcher (it has host access and can usually repair a
                # truncated JSON file itself) and raise the out-of-band alarm. Both are
                # per-fingerprint, i.e. once per distinct broken state; both must never be the
                # thing that breaks config loading.
                try:
                    ticket("config-broken",
                           f"config.json could not be parsed ({e}) — the pipeline is STOPPED "
                           f"(running on defaults, enabled=False)",
                           {"file": CONFIG_FILE, "error": str(e),
                            "hint": "fix the JSON in place (nightly copies are in "
                                    "/config/backup/config-*.json) or remove the file; "
                                    "vo-merge refuses to save over it while it is broken"},
                           key=str(fingerprint))
                except Exception:
                    pass
                try:
                    from . import notify as _notify
                    _notify.send("config", "config.json unparseable — pipeline stopped",
                                 f"{CONFIG_FILE}: {e}. Running on defaults with enabled=False "
                                 f"until the file is fixed or removed.",
                                 cfg=_LAST_GOOD_CFG or cfg)
                except Exception:
                    pass
            _CONFIG_BROKEN["at"] = _CONFIG_BROKEN["at"] or time.time()
    # Build the new set first and REBIND, rather than clear()+update() in place. Every thread and
    # every job calls this constantly, and a redact() running inside the clear-to-update window
    # saw an empty set — writing the secret it was meant to scrub verbatim into the log. Rebinding
    # is atomic as far as other threads are concerned: they see either the old set or the new one.
    global _SECRETS
    _SECRETS = {str(cfg[k]) for k in _SECRET_KEYS if cfg.get(k) and len(str(cfg[k])) >= 8}
    return cfg


def migrate_config():
    """One-shot upgrades of persisted config values whose MEANING changed.

    `lang_profiles.anime.audio` shipped with a literal `jpn` — the slot always meant "keep the
    ORIGINAL audio", and anime being Japanese made the two indistinguishable. They are not:
    Arcane is filed as anime and made in French, so a literal jpn target is a gap no release can
    ever fill. Every episode searches forever and ends up ignored while its FR+EN audio and subs
    are already complete. Rewriting it to the `orig` token leaves Japanese anime behaving exactly
    as before and fixes every other origin."""
    if not os.path.exists(CONFIG_FILE):
        return                                  # nothing persisted; DEFAULTS already say `orig`
    cfg = load_config()
    prof = (cfg.get("lang_profiles") or {}).get("anime") or {}
    aud = list(prof.get("audio") or [])
    if "jpn" not in aud or "orig" in aud:
        return
    prof = dict(prof)
    prof["audio"] = ["orig" if a == "jpn" else a for a in aud]
    profiles = dict(cfg["lang_profiles"]); profiles["anime"] = prof
    try:
        save_config({"lang_profiles": profiles})
    except ConfigUnreadable as e:
        log(f"config: skipping the jpn -> orig migration ({e})")
        return       # a startup migration must never be the thing that stops the app coming up
    log("config: anime audio target jpn -> orig (the original language, resolved per title)")


class ConfigUnreadable(Exception):
    """The persisted config exists but can't be parsed, so saving would destroy it."""


def save_config(updates: dict):
    with _lock:
        cfg = load_config()
        if _CONFIG_BROKEN["at"]:
            # load_config fell back to DEFAULTS, so `cfg` is defaults+this change. Writing that
            # would replace every setting the operator ever entered with a default.
            raise ConfigUnreadable(
                f"{CONFIG_FILE} is present but unparseable; refusing to overwrite it")
        cfg.update({k: v for k, v in updates.items() if k in DEFAULTS})
        os.makedirs(CONFIG_DIR, exist_ok=True)
        # Write-then-rename: json.dump straight onto CONFIG_FILE truncates first, so a crash or a
        # power cut mid-dump leaves a half-written file that parses as nothing. os.replace is
        # atomic on POSIX, so a reader sees either the old file or the new one.
        tmp = CONFIG_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_FILE)
    return load_config()


# Secrets travel inside the URLs we report. A Prowlarr download link carries `apikey=…` in its
# query string, so a failed grab used to store the live API key in the DB and render it in the
# Review tab (and in every AI ticket built from that record). Errors and log lines are written
# from dozens of places, so scrub in the two funnels they all pass through rather than at each
# call site.
_SECRET_QS = re.compile(r'((?:api_?key|apikey|token|passkey|rss_?key|auth|pass(?:word)?)=)[^&\s]+',
                        re.I)
_SECRET_KEYS = ("prowlarr_key", "radarr_key", "sonarr_key", "plex_token", "plex2_token",
                "qb_pass", "webhook_token", "api_key")
_SECRETS = set()      # the configured values themselves, refreshed whenever config is read


def redact(text):
    """Strip credentials out of anything about to be persisted or shown."""
    if not text:
        return text
    out = _SECRET_QS.sub(r"\1***", str(text))
    for v in _SECRETS:                 # a key can also appear outside a query string
        out = out.replace(v, "***")
    return out


LOG_MAX_BYTES = 8 * 1024 * 1024        # rotate past this
LOG_KEEP = 3                           # vo-merge.log.1 .. .3


def _rotate_log():
    """Roll vo-merge.log once it passes LOG_MAX_BYTES, keeping LOG_KEEP old files.

    Nothing truncated this file before, so a busy install grew it without limit — and `tail_log`
    read the WHOLE thing into memory on every call, on paths as hot as the UI's log poll and the
    per-record AI context built every 3 minutes."""
    try:
        if os.path.getsize(LOG_FILE) < LOG_MAX_BYTES:
            return
    except OSError:
        return
    try:
        for i in range(LOG_KEEP - 1, 0, -1):
            src, dst = f"{LOG_FILE}.{i}", f"{LOG_FILE}.{i + 1}"
            if os.path.exists(src):
                os.replace(src, dst)
        os.replace(LOG_FILE, f"{LOG_FILE}.1")
    except OSError:
        pass                            # a failed rotation must never stop us logging


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {redact(msg)}"
    os.makedirs(CONFIG_DIR, exist_ok=True)
    _rotate_log()
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")
    print(line, flush=True)


def ticket(kind, summary, context=None, key=None, force=False, once=True):
    """File an issue ticket for the host's AI dispatcher (an Unraid user script cron
    that runs the Claude Code CLI on each ticket). Tickets land in /config/ai-tickets/
    which the host reads as appdata/vo-merge/ai-tickets/. A (kind,key) pair is filed
    only once (persisted in ai_tickets_filed.json) so a standing condition doesn't
    re-page after being handled. force=True (operator-initiated, e.g. the Review tab's
    Send-to-AI button) skips the once-only guard and overwrites a pending same-kind
    ticket. Returns True if a ticket was filed.

    `once=False` skips ONLY the (kind,key) guard, keeping the "already awaiting dispatch"
    one. `ai_health_check` needs that: it keys its batch on a hash of the record ids, so the
    same record failing again months later produces the same key and was silently refused a
    ticket — while the record had already been stamped ai_status='pending'. With no ticket on
    disk, `undispatched()` couldn't see it either, so the staleness sweep then reported "the AI
    did not respond within 60m" about a page that was never sent. That path does its own
    per-record dedup (ai_seen_records.json), which is what makes this guard redundant there."""
    try:
        seen_path = os.path.join(CONFIG_DIR, "ai_tickets_filed.json")
        try:
            with open(seen_path) as f:
                seen = set(json.load(f))
        except Exception:
            seen = set()
        k = f"{kind}:{key or ''}"
        if once and k in seen and not force:
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
        with open(seen_path, "w") as f:
            json.dump(sorted(seen)[-3000:], f)
        log(f"AI-TICKET {kind}: {summary[:70]}")
        return True
    except Exception as e:
        log(f"ticket() failed: {e}")
        return False


def tail_log(n=300):
    """Last `n` lines, read from the END of the file.

    `f.readlines()[-n:]` materialised the entire log to return 300 lines, and the callers are hot:
    the UI's log poll, and both AI context builders, which filter tail_log(800) per record while
    the 3-minute sweep rebuilds contexts. With rotation capping the file at 8 MB that would be
    survivable, but reading ~64 KB instead of 8 MB is free."""
    if not os.path.exists(LOG_FILE):
        return []
    want = max(1, n)
    try:
        with open(LOG_FILE, "rb") as f:
            f.seek(0, os.SEEK_END)
            end = f.tell()
            block, data, lines = 8192, b"", 0
            pos = end
            while pos > 0 and lines <= want:
                step = min(block, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step)
                data = chunk + data
                lines = data.count(b"\n")
            text = data.decode("utf-8", "replace")
    except OSError:
        return []
    return [ln + "\n" for ln in text.splitlines()[-want:]]


# ---------------------------------------------------------------- killable work
# A merge is a sync detect (several minute-long ffmpeg decodes) plus a remux of a multi-GB file,
# and once it started NOTHING could stop it: the operator watching it go wrong could only wait
# out `mux_timeout_min` (4 hours by default) or restart the container, which loses every other
# in-flight download too. These few functions make a merge interruptible.
#
# `Aborted` deliberately inherits BaseException, not Exception. The merge path is full of
# `except Exception` handlers that turn a failed decode into "this window didn't resolve, try the
# next one" — correct for a failure, exactly wrong for a cancellation, which would be swallowed
# and the merge would grind on through the remaining windows. Only code that explicitly wants to
# know about a cancellation sees one; the same reason KeyboardInterrupt sits where it does.
class Aborted(BaseException):
    """The operator aborted the job running on this thread."""


_JOBS = {}                        # job key -> {"procs": set(Popen), "cancel": bool}
_JOBS_LOCK = threading.Lock()
_CUR = threading.local()          # the job key owned by THIS thread


@contextmanager
def job(key):
    """Mark the calling thread as running job `key`, so its subprocesses can be killed by name.
    The merge worker wraps each claimed record in this."""
    with _JOBS_LOCK:
        _JOBS[key] = {"procs": set(), "cancel": False}
    prev = getattr(_CUR, "key", None)
    _CUR.key = key
    try:
        yield
    finally:
        _CUR.key = prev
        with _JOBS_LOCK:
            _JOBS.pop(key, None)


def _here():
    with _JOBS_LOCK:
        return _JOBS.get(getattr(_CUR, "key", None))


def cancel_job(key):
    """Signal a running job to stop and kill whatever it is currently executing. Returns False
    when no such job is running — the caller then knows the record was not mid-flight."""
    with _JOBS_LOCK:
        j = _JOBS.get(key)
        procs = list(j["procs"]) if j else []
        if j:
            j["cancel"] = True
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass
    if j:
        log(f"abort: {key} signalled, killed {len(procs)} running process(es)")
    return bool(j)


def cancelled():
    j = _here()
    return bool(j and j["cancel"])


def run_proc(cmd, timeout=None, capture_output=False, text=False, **kw):
    """`subprocess.run`, but killable and cancellation-aware.

    Identical contract (a CompletedProcess, TimeoutExpired on timeout, the timeout still enforced
    — see the AST test that requires one on every call), with two additions: the process is
    registered against this thread's job so an abort can kill it, and an abort raises `Aborted`
    rather than returning a mysterious rc=-9 that the caller would read as a corrupt file and
    blocklist a perfectly good release for."""
    if cancelled():
        raise Aborted(f"aborted before starting {cmd[0] if cmd else '?'}")
    if capture_output:
        kw.setdefault("stdout", subprocess.PIPE)
        kw.setdefault("stderr", subprocess.PIPE)
    p = subprocess.Popen(cmd, text=text, **kw)
    j = _here()
    if j is not None:
        with _JOBS_LOCK:
            j["procs"].add(p)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        p.communicate()
        raise
    finally:
        if j is not None:
            with _JOBS_LOCK:
                j["procs"].discard(p)
    if cancelled():
        raise Aborted(f"{cmd[0] if cmd else 'process'} killed by an operator abort")
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)


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
                                   # 1 = the offset/drift above were set DELIBERATELY (an operator
                                   # or the AI via /set_sync) and must be applied as-is. A merge
                                   # also records what it measured, for display — but that is a
                                   # fact about the donor it measured, not an instruction for the
                                   # next one, and treating the two the same is how a re-opened
                                   # record re-merged an unrelated release with the old release's
                                   # offset and skipped detection entirely.
                                   "sync_manual": "INTEGER DEFAULT 0",
                                   # 0 = normal, >0 = jump the queue. A title someone has just
                                   # ASKED for shouldn't wait behind a thousand-item backlog that
                                   # nobody is watching — the search sweep and the merge queue
                                   # both order on this before their usual recency/FIFO rule.
                                   "priority": "INTEGER DEFAULT 0",

                                   # what the FILE actually holds (from mkvmerge, not metadata)
                                   "audio_langs": "TEXT", "sub_langs": "TEXT",
                                   "needs": "TEXT",          # audio | subs | audio+subs
                                   # which target languages are still missing (comma lists)
                                   "need_audio": "TEXT", "need_subs": "TEXT",
                                   "added_subs": "TEXT",
                                   # WHY this record is 'merged': grafted (we added tracks) |
                                   # replaced (we used the download as the file) | already (it
                                   # met its profile on its own — we did nothing). NULL = legacy.
                                   "merge_kind": "TEXT",
                                   # consecutive infrastructure failures (see pipeline.transient)
                                   # — routes retryable failures through self-retry instead of
                                   # minting an `error` that pages the AI for a network blip
                                   "transient_fails": "INTEGER DEFAULT 0",
                                   # how many separate search rounds ended in no_release —
                                   # drives the query ladder and the one-shot AI escalation
                                   "search_rounds": "INTEGER DEFAULT 0",
                                   # when an `ignored` record was last re-examined (revisit_ignored)
                                   "revisit_at": "REAL"})


def _ensure_indexes(c):
    """Indexes for the queries the background jobs run constantly.

    There were none beyond the primary keys, so every `WHERE status=? ORDER BY updated DESC` was a
    full table scan — and those back stage_search, promote_completed (every minute), the stall
    sweep, `_dl_hashes` (which scans once per state per call), the dashboard's 5000-row attention
    query and ai_log. The episodes table holds one row per tracked episode, so a large TV library
    makes that thousands of rows scanned several times a minute, with UI polls on top."""
    for table in ("movies", "episodes"):
        c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_status ON {table}(status)")
        c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_updated ON {table}(updated)")
        # partial: ai_log selects the handful of rows that have ever been escalated
        c.execute(f"CREATE INDEX IF NOT EXISTS idx_{table}_ai_at ON {table}(ai_at) "
                  f"WHERE ai_at IS NOT NULL")
    # the merge queue and the attention panel both sort a status subset by recency
    c.execute("CREATE INDEX IF NOT EXISTS idx_movies_status_updated ON movies(status, updated)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_episodes_status_updated ON episodes(status, updated)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_episodes_series ON episodes(series_id, season)")


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
                                     "sync_manual": "INTEGER DEFAULT 0",   # see movies table
                                     "priority": "INTEGER DEFAULT 0",   # see movies table
                                     # what the FILE actually holds (see movies table)
                                     "audio_langs": "TEXT", "sub_langs": "TEXT",
                                     "needs": "TEXT", "added_subs": "TEXT",
                                     "need_audio": "TEXT", "need_subs": "TEXT",
                                     # series' original language — the merge needs the same
                                     # inputs the scan used, or the two pick different profiles
                                     "orig_lang": "TEXT",
                                     "merge_kind": "TEXT",                    # see movies table
                                     "transient_fails": "INTEGER DEFAULT 0",  # see movies table
                                     "search_rounds": "INTEGER DEFAULT 0",    # see movies table
                                     "revisit_at": "REAL"})                   # see movies table


def init_indexes():
    """Create the query indexes. Separate from init_db/init_tv because it spans both tables, so
    it has to run after each has been created."""
    with db() as c:
        _ensure_indexes(c)


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


def init_history():
    """Two tables that exist only so the UI can draw a line rather than a dot.

    Everything the app kept was a SNAPSHOT: `movies`/`episodes` hold current state, `probes` holds
    what each file contains right now. Nothing recorded WHEN anything changed, so every question of
    the form "is this getting better?" — coverage over time, merges per day, whether the error rate
    is climbing — was unanswerable, and the forecast had to reconstruct a rate from `merged_at`
    alone (which only exists for one of the twelve states).

    - `events` is the transition log, written from the one place every status change goes through
      (`_set_row`, plus the two `claim_*` helpers). Titles are DENORMALISED into it on purpose: a
      pruned record must not take its own history with it, and a chart that silently loses its
      early months is worse than no chart.
    - `coverage_history` is a daily roll-up of the probe inventory, keyed by DAY so re-sampling is
      idempotent — the housekeeping job and the end of every scan both write it, and a busy day
      must not weigh more than a quiet one."""
    with db() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL,
            kind TEXT,               -- 'movie' | 'episode'
            key TEXT,                -- tmdb_id (as text) | episode id
            title TEXT,              -- denormalised: survives the record being pruned
            sub TEXT,                -- "1998" | "S02E07"
            frm TEXT,                -- status before
            sts TEXT,                -- status after
            detail TEXT,             -- the one line that explains it (error / release / langs)
            tag TEXT )""")           # tag = machine-readable secondary classifier (merge_kind)
        _ensure_cols(c, "events", {"tag": "TEXT"})
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_events_sts_ts ON events(sts, ts)")
        c.execute("""CREATE TABLE IF NOT EXISTS coverage_history (
            day TEXT PRIMARY KEY,    -- 'YYYY-MM-DD', UTC
            ts REAL,
            total INTEGER, complete INTEGER,
            missing_audio INTEGER, missing_subs INTEGER, missing_both INTEGER,
            unreadable INTEGER,
            libs TEXT )""")          # libs = JSON {lib: {total, complete}}


# Transitions that say nothing and would drown the log. `searching` is entered and left within one
# sweep for every pending record, so keeping it turns a few hundred meaningful rows a day into tens
# of thousands and makes "what happened to this title" unreadable.
_EVENT_SKIP = {"searching"}


def log_event(kind, key, frm, sts, title=None, sub=None, detail=None, tag=None):
    """Append one state transition. Never raises: history is a nice-to-have, and a failure to
    record one must not roll back the state change it describes."""
    if sts in _EVENT_SKIP and frm in _EVENT_SKIP:
        return
    try:
        with db() as c:
            c.execute("INSERT INTO events (ts,kind,key,title,sub,frm,sts,detail,tag) "
                      "VALUES (?,?,?,?,?,?,?,?,?)",
                      (time.time(), kind, str(key), title, sub, frm, sts,
                       redact(detail) if detail else None, tag))
    except Exception:
        pass


def prune_events(keep_days=90):
    """Cap the transition log. Run daily; returns how many rows went."""
    if keep_days <= 0:
        return 0
    with db() as c:
        cur = c.execute("DELETE FROM events WHERE ts < ?", (time.time() - keep_days * 86400,))
        return cur.rowcount or 0


def snapshot_coverage(row: dict):
    """Upsert TODAY's coverage roll-up. Keyed by day, so the scan that runs at 04:30 and the one
    an operator starts at 19:00 both land on the same row instead of weighting the day twice."""
    day = time.strftime("%Y-%m-%d", time.gmtime())
    with db() as c:
        c.execute("""INSERT INTO coverage_history
                       (day,ts,total,complete,missing_audio,missing_subs,missing_both,unreadable,libs)
                     VALUES (?,?,?,?,?,?,?,?,?)
                     ON CONFLICT(day) DO UPDATE SET
                       ts=excluded.ts, total=excluded.total, complete=excluded.complete,
                       missing_audio=excluded.missing_audio, missing_subs=excluded.missing_subs,
                       missing_both=excluded.missing_both, unreadable=excluded.unreadable,
                       libs=excluded.libs""",
                  (day, time.time(), row.get("total", 0), row.get("complete", 0),
                   row.get("missing_audio", 0), row.get("missing_subs", 0),
                   row.get("missing_both", 0), row.get("unreadable", 0),
                   json.dumps(row.get("libs") or {})))


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


BACKUP_DIR = os.path.join(CONFIG_DIR, "backup")


def backup_db(keep=7):
    """Snapshot the database, keeping the last `keep` daily copies.

    This file is the app's entire memory — the probe cache (the only complete inventory of the
    library) plus every record's pipeline state — and nothing backed it up. It runs in WAL mode,
    so copying the file while the app is live is torn by construction; `VACUUM INTO` takes a
    consistent snapshot through SQLite itself, which is the supported way to do this hot."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"vo-merge-{time.strftime('%Y%m%d')}.db")
    tmp = dest + ".tmp"
    try:
        if os.path.exists(tmp):
            os.remove(tmp)
        with db() as c:
            c.execute("VACUUM INTO ?", (tmp,))
        os.replace(tmp, dest)
    except Exception as e:
        log(f"backup failed: {e}")
        return None
    old = sorted(f for f in os.listdir(BACKUP_DIR)
                 if f.startswith("vo-merge-") and f.endswith(".db"))
    for f in old[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass
    log(f"backup: {dest} ({os.path.getsize(dest) // 1024} KB), keeping {keep}")
    return dest


def verify_or_restore_db():
    """Startup integrity gate: quick_check the DB, and on corruption restore the newest nightly
    snapshot AUTOMATICALLY instead of limping on a broken file.

    The nightly `VACUUM INTO` backups existed but nothing ever read one — so the recovery path
    was a human noticing weird behaviour, diagnosing SQLite corruption, and hand-copying a file
    into place. The DB is the app's entire memory (probe inventory + every record's state);
    losing up to a day of it to the snapshot is strictly better than every query silently
    misbehaving. The corrupt file is quarantined beside the live one (with its -wal/-shm), never
    deleted, so a human can still attempt a finer-grained recovery later.

    Returns one of: "ok" | "absent" | "restored" | "fresh" | "corrupt-unrecovered"."""
    if not os.path.exists(DB_FILE):
        return "absent"
    try:
        conn = sqlite3.connect(DB_FILE, timeout=30)
        try:
            row = conn.execute("PRAGMA quick_check").fetchone()
        finally:
            conn.close()
        if row and str(row[0]).lower() == "ok":
            return "ok"
        problem = str(row[0]) if row else "quick_check returned nothing"
    except Exception as e:
        problem = str(e)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    quarantine = f"{DB_FILE}.corrupt-{stamp}"
    try:
        os.replace(DB_FILE, quarantine)
        for ext in ("-wal", "-shm"):
            if os.path.exists(DB_FILE + ext):
                os.replace(DB_FILE + ext, quarantine + ext)
    except OSError as e:
        log(f"db: CORRUPT ({problem}) and could not be quarantined ({e}) — continuing on it")
        return "corrupt-unrecovered"
    backups = sorted(f for f in (os.listdir(BACKUP_DIR) if os.path.isdir(BACKUP_DIR) else [])
                     if f.startswith("vo-merge-") and f.endswith(".db"))
    if backups:
        src = os.path.join(BACKUP_DIR, backups[-1])
        shutil.copyfile(src, DB_FILE)
        log(f"db: CORRUPT ({problem}) — quarantined to {os.path.basename(quarantine)} and "
            f"restored {backups[-1]}")
        outcome, detail = "restored", f"restored last night's snapshot {backups[-1]}"
    else:
        log(f"db: CORRUPT ({problem}) — quarantined to {os.path.basename(quarantine)}; no "
            f"backup exists, starting fresh (a library re-read rebuilds the inventory)")
        outcome, detail = "fresh", "no backup existed — started fresh"
    try:
        ticket("db-restored", f"database was corrupt ({problem[:120]}) — {detail}",
               {"quarantined": quarantine, "outcome": outcome,
                "note": "records changed since the snapshot re-derive from the next scan; "
                        "the quarantined file is kept for manual recovery"}, key=stamp)
    except Exception:
        pass
    try:
        from . import notify as _notify
        _notify.send("db", f"database {outcome} after corruption",
                     f"{problem[:200]} — {detail}. Quarantined: {quarantine}")
    except Exception:
        pass
    return outcome


def backup_config(keep=7):
    """Nightly copy of config.json beside the DB snapshots. The config's atomic write protects
    against crashes mid-save, not against a bad-but-parseable save — and the broken-config
    ticket points the fixer at these copies. Skipped while the live file is unparseable: copying
    it then would overwrite the day's good snapshot with the very bytes that broke."""
    if _CONFIG_BROKEN["at"] or not os.path.exists(CONFIG_FILE):
        return None
    os.makedirs(BACKUP_DIR, exist_ok=True)
    dest = os.path.join(BACKUP_DIR, f"config-{time.strftime('%Y%m%d')}.json")
    try:
        shutil.copyfile(CONFIG_FILE, dest)
    except OSError as e:
        log(f"config backup failed: {e}")
        return None
    old = sorted(f for f in os.listdir(BACKUP_DIR)
                 if f.startswith("config-") and f.endswith(".json"))
    for f in old[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(BACKUP_DIR, f))
        except OSError:
            pass
    return dest


def probe_stats():
    with db() as c:
        r = c.execute("SELECT COUNT(*) n, SUM(err IS NOT NULL) bad FROM probes").fetchone()
    return {"cached": r["n"] or 0, "unreadable": r["bad"] or 0}


def upsert_episode(e: dict):
    """Insert or refresh an episode's metadata columns. Never touches `status`.

    SELECT-then-INSERT raced: a webhook ingest and the scheduled scan can reach a new title at the
    same moment, and the loser's INSERT raised IntegrityError, aborting that whole scan pass. The
    single ON CONFLICT statement is atomic — `put_probe` already did it this way."""
    cols = ["id", "series_id", "series_title", "tvdb_id", "season", "episode",
            "french_path", "quality", "poster", "series_type"]
    with db() as c:
        c.execute(f"""INSERT INTO episodes ({','.join(cols)},updated)
                      VALUES ({','.join('?' * len(cols))},?)
                      ON CONFLICT(id) DO UPDATE SET
                        series_title=excluded.series_title, tvdb_id=excluded.tvdb_id,
                        french_path=excluded.french_path, quality=excluded.quality,
                        poster=excluded.poster, series_type=excluded.series_type,
                        updated=excluded.updated""",
                  tuple(e.get(k) for k in cols) + (time.time(),))


def set_ep_status(ep_id, status, expect=None, **fields):
    return _set_row("episodes", "id", ep_id, status, fields, expect)


def get_episodes(status=None):
    with db() as c:
        if status:
            rows = c.execute("SELECT * FROM episodes WHERE status=? "
                             "ORDER BY COALESCE(priority,0) DESC, updated DESC",
                             (status,)).fetchall()
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
    """Insert or refresh a movie's metadata columns. Never touches `status`. See upsert_episode
    for why this is one atomic statement rather than SELECT-then-INSERT."""
    cols = ["tmdb_id", "imdb_id", "radarr_id", "title", "original_title", "year",
            "original_lang", "french_path", "quality", "poster"]
    with db() as c:
        c.execute(f"""INSERT INTO movies ({','.join(cols)},updated)
                      VALUES ({','.join('?' * len(cols))},?)
                      ON CONFLICT(tmdb_id) DO UPDATE SET
                        imdb_id=excluded.imdb_id, radarr_id=excluded.radarr_id,
                        title=excluded.title, original_title=excluded.original_title,
                        year=excluded.year, original_lang=excluded.original_lang,
                        french_path=excluded.french_path, quality=excluded.quality,
                        poster=excluded.poster, updated=excluded.updated""",
                  tuple(m.get(k) for k in cols) + (time.time(),))


def _set_row(table, key_col, key, status, fields, expect=None):
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

    Pass `updated=<ts>` explicitly to force a bump.

    `expect` guards the write on the row still being in that status, and returns False when it
    isn't. A scan reads a record, decides what status it should carry, then writes it back — and
    in between, the merge worker can claim `ready -> merging`. Writing unconditionally put `ready`
    back underneath a running merge, and the record was then claimed and merged a second time into
    the same output path. Losing the write is harmless: the scan was only refreshing the language
    columns, and the next pass redoes it."""
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
    where, args = f"{key_col}=?", [key]
    if expect is not None:
        where += " AND status=?"
        args.append(expect)
    with db() as c:
        # Read the row FIRST so the transition can be logged with what it came from. A blind
        # UPDATE can't tell a real state change from a scan re-writing the same status, and that
        # difference is the whole content of the history log. One primary-key read per write.
        before = c.execute(f"SELECT * FROM {table} WHERE {key_col}=?", (key,)).fetchone()
        cur = c.execute(f"UPDATE {table} SET {sets} WHERE {where}", tuple(vals) + tuple(args))
        ok = cur.rowcount == 1
    if ok and before is not None and before["status"] != status:
        _log_transition(table, before, status, fields)
    return ok


def _log_transition(table, before, status, fields):
    """Denormalise a row into one `events` entry. `detail` carries the one line that explains the
    transition — the error for a failure, what was added for a merge, the release for a grab —
    because a bare "error" in a timeline tells you nothing you can act on."""
    if table == "movies":
        kind, key = "movie", before["tmdb_id"]
        title, sub = before["title"], (str(before["year"]) if before["year"] else None)
    else:
        kind, key = "episode", before["id"]
        title = before["series_title"]
        sub = f"S{int(before['season'] or 0):02d}E{int(before['episode'] or 0):02d}"
    detail = fields.get("error")
    tag = None
    if status == "merged":
        # `merged` is the terminal state for three different outcomes and only two are work we
        # did, so the KIND has to survive into the history or every chart drawn from it repeats
        # the "a library re-read looks like thousands of merges" mistake.
        tag = fields.get("merge_kind") or before["merge_kind"] or "grafted"
        if not detail:
            detail = ", ".join(x for x in (fields.get("added_langs"),
                                           fields.get("added_subs")) if x) or None
    if not detail:
        detail = fields.get("progress") or fields.get("candidate_title")
    log_event(kind, key, before["status"], status, title, sub, detail, tag)


def set_status(tmdb_id, status, expect=None, **fields):
    return _set_row("movies", "tmdb_id", tmdb_id, status, fields, expect)


def claim_movie(tmdb_id, from_status, to_status, **fields):
    """Atomically move a movie from one status to another. Returns True only if THIS caller
    won the transition — the merge worker uses it to claim a queued item so a concurrent
    claimer (or a re-entrant sweep) can never merge the same file twice."""
    fields["status"] = to_status
    fields["updated"] = time.time()
    keys = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        before = c.execute("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()
        cur = c.execute(f"UPDATE movies SET {keys} WHERE tmdb_id=? AND status=?",
                        tuple(fields.values()) + (tmdb_id, from_status))
        ok = cur.rowcount == 1
    if ok and before is not None:
        _log_transition("movies", before, to_status, fields)
    return ok


def claim_episode(ep_id, from_status, to_status, **fields):
    """Episode mirror of claim_movie."""
    fields["status"] = to_status
    fields["updated"] = time.time()
    keys = ",".join(f"{k}=?" for k in fields)
    with db() as c:
        before = c.execute("SELECT * FROM episodes WHERE id=?", (ep_id,)).fetchone()
        cur = c.execute(f"UPDATE episodes SET {keys} WHERE id=? AND status=?",
                        tuple(fields.values()) + (ep_id, from_status))
        ok = cur.rowcount == 1
    if ok and before is not None:
        _log_transition("episodes", before, to_status, fields)
    return ok


def get_movies(status=None):
    with db() as c:
        if status:
            # priority first, then the usual recency. A requested title must not sit behind a
            # backlog it can never overtake on `updated` alone.
            rows = c.execute("SELECT * FROM movies WHERE status=? "
                             "ORDER BY COALESCE(priority,0) DESC, updated DESC",
                             (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM movies ORDER BY updated DESC").fetchall()
    return [dict(r) for r in rows]


def get_movie(tmdb_id):
    with db() as c:
        r = c.execute("SELECT * FROM movies WHERE tmdb_id=?", (tmdb_id,)).fetchone()
    return dict(r) if r else None


def set_priority(kind, ident, level):
    """Set a record's QUEUE priority without touching its pipeline state.

    Priority is orthogonal to status: a record keeps its place in the state machine and only
    changes where it sits in the search sweep and the merge queue. Deliberately does not move
    `updated` either — that means "when the state last changed", and bumping it here would
    reshuffle the attention panel for a change that isn't a state change at all.

    It is NOT cleared when the title finishes. If a merged record is later re-opened because its
    file is still short of the profile, something someone asked for is still something someone
    asked for. The cost of being wrong is only ordering."""
    table, key_col = ("movies", "tmdb_id") if kind == "movie" else ("episodes", "id")
    with db() as c:
        cur = c.execute(f"UPDATE {table} SET priority=? WHERE {key_col}=?", (int(level), ident))
        return cur.rowcount == 1


def prioritise_series(series_id, level):
    """Bump every unfinished episode of a series. TV is requested per SHOW, not per episode."""
    with db() as c:
        cur = c.execute("UPDATE episodes SET priority=? WHERE series_id=? "
                        "AND status NOT IN ('merged','ignored')", (int(level), series_id))
        return cur.rowcount


def status_counts():
    with db() as c:
        rows = c.execute("SELECT status, COUNT(*) n FROM movies GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}
