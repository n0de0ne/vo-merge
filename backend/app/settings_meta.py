"""What every config key MEANS — the schema a settings page is rendered from.

`core.DEFAULTS` has 111 keys. The old Settings page hand-wrote a form for about forty of them, so
the other seventy — including every autonomy key, every timeout, the notify channel and the whole
disk/recycle policy — could only be changed by editing `/config/config.json` on the host and
restarting. That is not a settings page, it is a shortlist, and it silently decided which parts of
the app an operator was allowed to run.

Describing the keys as DATA fixes that permanently: a key added to DEFAULTS and listed here shows
up in the UI with its help text, its type and its validation, and the frontend needs no change.
`test_settings_meta.py` asserts the two stay in step in both directions — a key in DEFAULTS with
no entry here, or an entry here naming a key that no longer exists, fails the build. That is the
part that stops this file rotting.

Field types the UI knows how to render:
    text · password · number · bool · select · list_str · list_int · profiles
`advanced` keys are hidden behind the page's "Show advanced" toggle: correct defaults that are
easy to make worse. `restart` marks the handful that only take effect on the next container start.
"""
from . import core

# `unit` is rendered after the input; `help` is the sentence under it. Both are written for
# someone who does not already know how this app works — the audience the old form assumed away.
SECTIONS = [
    {
        "id": "general", "label": "General", "icon": "⚙️",
        "blurb": "The master switch and how this app is reached.",
        "fields": [
            dict(key="enabled", type="bool", label="Pipeline enabled",
                 help="The master switch. Off means nothing is searched, grabbed or merged — "
                      "scans and the UI keep working."),
            dict(key="paused", type="bool", label="Paused",
                 help="A brake rather than a switch: no NEW work starts, but anything already "
                      "merging finishes. Aborting a mux mid-write would leave a corrupt library "
                      "file. Also available from the header on every page."),
            dict(key="api_key", type="password", label="API key", secret=True,
                 help="Optional. When set, every API call needs it; this page asks once and "
                      "remembers it in this browser. The *arr webhooks and the container "
                      "healthcheck are exempt. Cross-site requests are refused either way."),
            dict(key="webhook_token", type="text", label="Webhook token",
                 help="Optional shared secret appended to the Radarr/Sonarr webhook URLs as "
                      "?token=… . Left empty, the hooks accept any caller that can reach them."),
            dict(key="api_url", type="text", label="API URL (as the host sees it)",
                 help="Where the on-call AI dispatcher should call back, e.g. "
                      "http://10.0.1.5:8090. Written into every ticket. Left empty, the brief "
                      "carries only the in-container address, which a host script cannot reach."),
            dict(key="docs_path", type="text", label="Docs path (on the host)", advanced=True,
                 help="Where CLAUDE.md lives for the dispatcher to read. Optional."),
        ],
    },
    {
        "id": "media", "label": "Media management", "icon": "📁",
        "blurb": "Where the files are, and which of them this app is responsible for.",
        "fields": [
            dict(key="media_mount", type="text", label="Library mount",
                 help="This container's view of the media share. Every library path is resolved "
                      "under it."),
            dict(key="plex_media_prefix", type="text", label="Plex/*arr path prefix",
                 help="How Plex and the *arrs refer to the same files. Paths they report are "
                      "rewritten from this prefix onto the library mount above."),
            dict(key="downloads_mount", type="text", label="Downloads (this container)",
                 help="Where finished downloads appear to THIS app. It and the qBittorrent save "
                      "path below must resolve to the same files — when they don't, downloads "
                      "complete and then 'never become visible'."),
            dict(key="qb_download_dir", type="text", label="Downloads (qBittorrent's view)",
                 help="The same directory as qBittorrent names it. Films."),
            dict(key="qb_tv_download_dir", type="text", label="TV downloads (qBittorrent's view)",
                 help="As above, for episodes."),
            dict(key="anime_dirs", type="list_str", label="Anime folders",
                 help="Top-level library folders that mean anime. Sonarr's own anime flag and a "
                      "Japanese original language also select the anime profile."),
            dict(key="series_dirs", type="list_str", label="TV folders",
                 help="Top-level library folders that mean TV series."),
            dict(key="scope_films", type="bool", label="Manage films"),
            dict(key="scope_series", type="bool", label="Manage TV & anime",
                 help="Runs the episode pipeline as well as the film one."),
            dict(key="series_pilot", type="list_str", label="Limit to these shows", advanced=True,
                 help="Comma-separated show names. Empty means every show — which is almost "
                      "always what you want; this exists for trialling the TV pipeline."),
            dict(key="scan_mode", type="select", label="How gaps are decided",
                 options=[("files", "Read the files (accurate)"),
                          ("tag", "Trust Radarr/Sonarr metadata (legacy)")],
                 help="'Read the files' probes every library file with mkvmerge, so a stale tag "
                      "or a file the *arrs never analysed cannot hide a gap. The legacy mode "
                      "exists only for comparison — it is wrong in both directions."),
            dict(key="scan_all_movies", type="bool", label="Consider every film",
                 help="In 'read the files' mode, judge every film Radarr knows about rather than "
                      "only vo-gap tagged ones."),
            dict(key="vo_gap_tag", type="text", label="Radarr gap tag", advanced=True),
            dict(key="sonarr_vo_gap_tag", type="text", label="Sonarr gap tag", advanced=True),
        ],
    },
    {
        "id": "profiles", "label": "Language profiles", "icon": "🗣️",
        "blurb": "The end state each kind of title should reach. A file missing any of these is "
                 "a gap, and the missing languages are what gets grafted off the donor.",
        "fields": [
            dict(key="lang_profiles", type="profiles", label="Targets",
                 help="Three-letter codes. 'orig' in the anime audio row means the title's OWN "
                      "original language, resolved per title — it keeps the Japanese track on a "
                      "Japanese show without demanding one from a show made in French."),
            dict(key="want_subs", type="bool", label="Graft subtitles too",
                 help="Take the donor's subtitles while grafting its audio. Costs one extra "
                      "mkvmerge argument, not another download."),
            dict(key="max_sub_tracks", type="number", label="Subtitle tracks per language",
                 min=1, max=10,
                 help="Packs routinely ship six per language (full, forced, SDH, signs…). The "
                      "ranking prefers a full translation and puts signs-and-songs last."),
            dict(key="subs_only_gap", type="bool", label="Chase subtitle-only gaps",
                 help="Download a release for a file that already has every target AUDIO "
                      "language but is missing a target subtitle. Bazarr is the cheaper tool for "
                      "this; turn it on when a subtitle exists only inside a release."),
        ],
    },
    {
        "id": "arr", "label": "Radarr & Sonarr", "icon": "🎬",
        "blurb": "Where titles, ids and original languages come from. Never the gap decision.",
        "fields": [
            dict(key="radarr_url", type="text", label="Radarr URL", test="radarr"),
            dict(key="radarr_key", type="password", label="Radarr API key", secret=True),
            dict(key="sonarr_url", type="text", label="Sonarr URL", test="sonarr"),
            dict(key="sonarr_key", type="password", label="Sonarr API key", secret=True),
            dict(key="tv_pack_threshold", type="number", label="Season-pack threshold", min=1,
                 help="This many gap episodes in one season and a season pack is grabbed instead "
                      "of separate episodes."),
            dict(key="priority_on_import", type="number", label="Priority on import", min=0,
                 help="An import event IS someone asking for a title, so the webhook puts it at "
                      "the front of both queues. 0 disables. Bulk-importing a library makes "
                      "everything priority, which degrades to normal ordering rather than "
                      "breaking anything."),
        ],
    },
    {
        "id": "indexers", "label": "Indexers", "icon": "🔎",
        "blurb": "Prowlarr, and how a release is chosen.",
        "fields": [
            dict(key="prowlarr_url", type="text", label="Prowlarr URL", test="prowlarr"),
            dict(key="prowlarr_key", type="password", label="Prowlarr API key", secret=True),
            dict(key="en_indexer_ids", type="list_int", label="Indexer IDs",
                 help="Prowlarr's numeric indexer IDs. Empty means every configured indexer, "
                      "which is the right default — these IDs are per-instance, so a copied list "
                      "queries indexers that don't exist here and records the empty result as "
                      "'no release exists'."),
            dict(key="multi_indexer_ids", type="list_int", label="Extra MULTI indexers",
                 help="Additional indexers searched for MULTI releases, e.g. FR trackers."),
            dict(key="score_threshold", type="number", label="Score threshold",
                 help="Minimum score a release must reach to be grabbed automatically."),
            dict(key="min_seeders", type="number", label="Minimum seeders", min=0),
            dict(key="search_interval_min", type="number", label="Search sweep", unit="min", min=1,
                 help="How often the library is re-scanned and pending titles searched. The "
                      "*arr webhooks make new imports instant, so this is the safety net."),
            dict(key="max_search_per_run", type="number", label="Searches per sweep", min=1,
                 advanced=True),
            dict(key="no_release_retry_h", type="number", label="Re-search 'no release' after",
                 unit="h", advanced=True,
                 help="Indexers make 'nothing exists' untrue over time. Without a cooldown an "
                      "hourly sweep would re-query hundreds of titles that genuinely don't exist."),
            dict(key="no_release_escalate_rounds", type="number", advanced=True,
                 label="Escalate after N empty rounds",
                 help="Fruitless search rounds before the title is handed to the on-call AI once, "
                      "with an instruction to try queries the built-in ladder cannot compose."),
            dict(key="tried_ttl_days", type="number", label="Blocklist entries expire after",
                 unit="days", advanced=True,
                 help="A rejected release stays blocklisted this long. Without expiry a title "
                      "eventually blocklists everything that exists."),
        ],
    },
    {
        "id": "clients", "label": "Download client", "icon": "⬇️",
        "blurb": "qBittorrent, the in-flight cap, and when a download is declared dead.",
        "fields": [
            dict(key="qb_url", type="text", label="qBittorrent URL", test="qb"),
            dict(key="qb_user", type="text", label="Username"),
            dict(key="qb_pass", type="password", label="Password", secret=True),
            dict(key="qb_category", type="text", label="Category (films)"),
            dict(key="qb_tv_category", type="text", label="Category (TV)"),
            dict(key="grab_mode", type="select", label="Grab mode",
                 options=[("auto", "Automatic"), ("approval", "Ask me first")],
                 help="'Ask me first' parks each choice in `grabbed` until someone approves it."),
            dict(key="max_inflight_downloads", type="number", label="Download slots", min=1,
                 help="How many downloads run at once. A season pack counts as one. The cap "
                      "counts only downloads — a finished one frees its slot immediately, even "
                      "while it waits to merge."),
            dict(key="stall_timeout_min", type="number", label="Stall timeout", unit="min", min=1,
                 help="Idle, seedless and this old → dropped, blocklisted and re-searched."),
            dict(key="meta_timeout_min", type="number", label="Dead-magnet timeout", unit="min",
                 min=1,
                 help="A torrent still fetching metadata has never had a single peer answer it. "
                      "Making these wait the full stall timeout is what produces a queue of dead "
                      "magnets holding every slot."),
            dict(key="dl_max_age_min", type="number", label="Absolute download age cap",
                 unit="min", min=1,
                 help="Even a slow trickle is dropped past this. A release that cannot finish in "
                      "this long is not worth the slot."),
            dict(key="stall_check_interval_min", type="number", label="Stall sweep", unit="min",
                 min=1, advanced=True),
            dict(key="promote_interval_min", type="number", label="Queue check", unit="min", min=1,
                 help="How often finished downloads are moved onto the merge queue."),
            dict(key="delete_donor", type="bool", label="Delete the donor after merging",
                 help="Frees the download once its tracks are in the library file."),
            dict(key="no_seed_public", type="bool", label="Never seed public trackers",
                 advanced=True),
            dict(key="french_trackers", type="list_str", label="Keep seeding these trackers",
                 advanced=True,
                 help="Substrings of tracker URLs whose torrents are left seeding instead of "
                      "deleted."),
            dict(key="donor_keep_days", type="number", label="Keep donors for parked failures",
                 unit="days", advanced=True,
                 help="A record in review/sync_fail/error keeps its download this long, because "
                      "the repair for those states needs the exact files. After that a later "
                      "merge finds it gone and self-heals through the retry path."),
        ],
    },
    {
        "id": "sync", "label": "Merging & sync", "icon": "🎚️",
        "blurb": "How the grafted audio is aligned to the picture, and what happens when it "
                 "cannot be.",
        "fields": [
            dict(key="max_parallel_merges", type="number", label="Simultaneous merges", min=1,
                 help="Merges share the CPU and the iGPU. 1 is safest; raise it only with real "
                      "headroom. Applied live."),
            dict(key="finish_interval_min", type="number", label="Finish sweep", unit="min", min=1),
            dict(key="max_sync_retries", type="number", label="Releases to try", min=1,
                 help="How many different releases a title may go through before it is parked."),
            dict(key="auto_sync", type="bool", label="Automatic sync detection",
                 help="Off means a pair whose framerates differ is rejected instead of measured."),
            dict(key="sync_review", type="bool", label="Park failures for a human",
                 help="A sync that cannot be resolved lands in `review` (donor kept, so the pair "
                      "can still be aligned) rather than failing outright. Safe to leave on when "
                      "the on-call AI is running — something will actually pick it up."),
            dict(key="sync_tolerance_s", type="number", label="Duration tolerance", unit="s",
                 step=0.1, advanced=True),
            dict(key="auto_sync_min_conf", type="number", label="Audio confidence floor",
                 step=0.05, advanced=True),
            dict(key="sync_video_min_conf", type="number", label="Video confidence floor",
                 step=0.05, advanced=True,
                 help="Scene-cut correlation is the primary signal; audio is the fallback."),
            dict(key="sync_windows", type="number", label="Analysis windows", min=2, advanced=True,
                 help="At least two must agree before a constant offset is believed."),
            dict(key="sync_window_start", type="number", label="First window at", unit="s",
                 advanced=True),
            dict(key="sync_window_dur", type="number", label="Window length", unit="s",
                 advanced=True),
            dict(key="sync_max_lag_s", type="number", label="Largest offset searched", unit="s",
                 help="Not a tuning knob — it is a ceiling. A BD-vs-WEB pair routinely differs by "
                      "30-60s (a sponsor card, a 'previously on'), and past this bound the true "
                      "correlation peak is sliced off before it can be found, so a plain constant "
                      "offset reads as 'different cut'."),
            dict(key="sync_wide_probe", type="bool", label="Wide probe before giving up",
                 help="On the attempt that would spend the retry budget, re-run detection once at "
                      "the wider range below. This is the first step of the manual runbook, done "
                      "by the pipeline."),
            dict(key="sync_probe_lag_s", type="number", label="Wide probe range", unit="s"),
            dict(key="sync_ratio_test", type="bool", label="Test rate ratios (PAL)",
                 help="A 25fps transfer runs 4.27% short of 23.976fps. Window matching cannot see "
                      "a rate difference — it is destroyed by one — so the known ratios are "
                      "hypothesis-tested instead."),
            dict(key="sync_ratio_span", type="number", label="Rate-test span", unit="s",
                 advanced=True),
            dict(key="sync_ratio_min_conf", type="number", label="Rate-test confidence floor",
                 step=0.05, advanced=True),
            dict(key="sync_ratio_margin", type="number", label="Rate-test margin", step=0.1,
                 advanced=True,
                 help="The winning ratio must also beat the no-stretch hypothesis by this factor, "
                      "so a stretch is never invented."),
            dict(key="postmerge_qc", type="bool", label="Verify the graft before swapping",
                 help="Cross-correlate the grafted track against the library file's own audio "
                      "INSIDE the output, before it replaces anything. A confident-but-wrong sync "
                      "is otherwise undetectable: the language reads as present, the record "
                      "closes, the donor is deleted."),
            dict(key="qc_min_conf", type="number", label="QC confidence floor", step=0.05,
                 advanced=True,
                 help="Below this the verdict is inconclusive and the merge is ACCEPTED — absence "
                      "of evidence is not evidence of misalignment."),
            dict(key="qc_max_offset_ms", type="number", label="QC reject threshold", unit="ms",
                 advanced=True),
            dict(key="mux_timeout_min", type="number", label="Mux timeout", unit="min",
                 advanced=True,
                 help="A deadlock guard, not a tuning knob. A wedged mkvmerge held the merge "
                      "worker forever, which at one merge slot silently stops all merging."),
            dict(key="sync_decode_timeout_min", type="number", label="Decode timeout", unit="min",
                 advanced=True),
            dict(key="sync_ffmpeg_threads", type="number", label="ffmpeg threads", min=1,
                 advanced=True),
            dict(key="sync_hwaccel", type="select", label="Hardware decode",
                 options=[("vaapi", "VAAPI (Intel iGPU)"), ("qsv", "Quick Sync"), ("none", "None")],
                 help="Decode and downscale stay on the GPU, so only tiny frames reach the CPU — "
                      "about 7x faster on 4K than CPU scaling. Falls back to software on a real "
                      "hardware failure, never on a merely quiet window."),
            dict(key="sync_hwaccel_device", type="text", label="Render device", advanced=True),
        ],
    },
    {
        "id": "lipsync", "label": "Lip-sync", "icon": "👄",
        "blurb": "Correlates mouth movement in the picture against the speech in an audio track. "
                 "Every other check here compares two FILES, so both can be wrong together; this "
                 "one compares audio to the picture, which makes it the only absolute reading — "
                 "and the only one that works on a file with no donor at all.",
        "fields": [
            dict(key="lipsync_enabled", type="bool", label="Enable lip-sync",
                 help="Allows the per-title 'Read the lips' action and the rescue step below."),
            dict(key="lipsync_rescue", type="bool", label="Use it before giving up",
                 help="When the windows and the rate test have both failed, measure each file "
                      "against its own picture and take the difference. This is what turns "
                      "'different cut — pick another or ignore' into a number."),
            dict(key="lipsync_qc", type="bool", label="Verify every graft with it",
                 help="Off by default: it costs a second decode pass per merge, and the "
                      "cross-correlation QC above already catches the common failure. Turn it on "
                      "if you have seen a merge land confidently in the wrong place."),
            dict(key="lipsync_windows", type="number", label="Sampling windows", min=2),
            dict(key="lipsync_window_dur", type="number", label="Window length", unit="s", min=8),
            dict(key="lipsync_fps", type="number", label="Frames sampled", unit="fps", min=6,
                 advanced=True,
                 help="Speech modulates the mouth at 2-8 Hz, so anything above about 10fps is "
                      "comfortably enough and keeps the decode cheap."),
            dict(key="lipsync_max_lag_s", type="number", label="Largest offset searched",
                 unit="s", step=0.5,
                 help="Displacement beyond a few seconds is not a lip-sync problem, it is a "
                      "different cut."),
            dict(key="lipsync_min_conf", type="number", label="Confidence floor", step=0.05,
                 advanced=True),
            dict(key="lipsync_min_windows", type="number", label="Windows that must agree", min=2,
                 advanced=True,
                 help="Windows landing on action, music or a face-less scene resolve nothing and "
                      "are discarded. Requiring several to agree is what stops one of them "
                      "locking onto a musical phrase."),
        ],
    },
    {
        "id": "plex", "label": "Plex", "icon": "🍿",
        "blurb": "A merge rewrites the file in place, so its path never changes and a plain scan "
                 "will not re-read the streams — only an analyze will. Both servers and both "
                 "library copies are refreshed.",
        "fields": [
            dict(key="plex_url", type="text", label="Plex URL", test="plex"),
            dict(key="plex_token", type="password", label="Plex token", secret=True),
            dict(key="plex2_url", type="text", label="Second Plex URL",
                 help="Optional replica server."),
            dict(key="plex2_token", type="password", label="Second Plex token", secret=True,
                 help="The replica's own token — usually a different account."),
        ],
    },
    {
        "id": "autonomy", "label": "Autonomy & alerts", "icon": "🤖",
        "blurb": "What happens when something fails, and how you find out. The goal is that a "
                 "human never does routine repair and is told out-of-band when the machine has "
                 "proven something impossible — or when the machine itself is broken.",
        "fields": [
            dict(key="ai_tickets", type="bool", label="Escalate failures to the on-call AI",
                 help="Writes one ticket per record into /config/ai-tickets/. The resident "
                      "dispatcher runs the Claude CLI on each, acts through this API and reports "
                      "the outcome back."),
            dict(key="ai_stale_min", type="number", label="Reply timeout", unit="min", min=1,
                 help="How long the dispatcher may HOLD a ticket before the record is flagged for "
                      "a human. It measures holding, not queueing, so a backlog bigger than the "
                      "dispatcher's throughput cannot age out records nobody has opened."),
            dict(key="ai_max_tickets", type="number", label="Queued ticket cap", min=1,
                 help="One bad season pack is 400 episodes; filing all of them at once buries the "
                      "queue. Records past the cap stay unpaged and are picked up as it drains."),
            dict(key="ai_dispatcher_alarm_min", type="number", label="Dispatcher alarm after",
                 unit="min",
                 help="Alarm when the oldest queued ticket has waited this long AND the "
                      "dispatcher heartbeat is missing. Both signals, because a live dispatcher "
                      "with a deep queue is slow, not dead."),
            dict(key="notify_url", type="text", label="Alert webhook",
                 help="Discord/Slack webhook, or anything ntfy-shaped. This is for the "
                      "automation's OWN failures — dead dispatcher, broken config, full disk, a "
                      "dependency down, records needing a human. Never per-merge chatter."),
            dict(key="notify_repeat_h", type="number", label="Repeat an alert at most every",
                 unit="h",
                 help="Per KIND, not per message, and re-armed the moment the condition is seen "
                      "healthy. A channel that repeats itself every three minutes gets muted, "
                      "which is worse than no channel."),
            dict(key="dep_down_alarm_min", type="number", label="Dependency-down alarm after",
                 unit="min"),
            dict(key="disk_floor_gb", type="number", label="Disk floor", unit="GB",
                 help="All merging is held below this, and a specific merge whose output cannot "
                      "fit is re-queued un-penalised rather than half-written."),
            dict(key="transient_max", type="number", label="Transient failures tolerated", min=1,
                 advanced=True,
                 help="Consecutive retryable failures — a fetch timeout, a qB blip — before it "
                      "stops counting as transient and becomes a real error."),
            dict(key="recycle_keep_days", type="number", label="Keep replaced originals",
                 unit="days",
                 help="When a download REPLACES a library file, the original moves to a recycle "
                      "folder for this long instead of being destroyed. 0 restores the old "
                      "destructive behaviour. Grafts don't recycle — their output carries every "
                      "track the original had."),
            dict(key="ignored_revisit_days", type="number", label="Re-examine ignored titles",
                 unit="days",
                 help="'No release exists' decays as truth, so a give-up must not be permanent by "
                      "accident."),
            dict(key="ignored_revisit_per_day", type="number", label="Ignored re-checks per day",
                 min=0, advanced=True),
            dict(key="auto_repair", type="bool", label="Repair audio-less files automatically",
                 danger=True,
                 help="DELETES library files that carry no audio at all — through the *arr, so it "
                      "searches for a replacement — because grafting cannot fix a file with "
                      "nothing to sync against. Every candidate is re-probed with the cache "
                      "bypassed first, and files the *arrs don't know about are skipped."),
        ],
    },
    {
        "id": "maintenance", "label": "Maintenance", "icon": "🗄️",
        "blurb": "Backups and how much history is kept.",
        "fields": [
            dict(key="db_backup_keep", type="number", label="Nightly DB backups kept", min=0,
                 help="A VACUUM INTO snapshot of the database and the config each night. Startup "
                      "checks the database and restores last night's copy if it is corrupt, so "
                      "these have a reader. 0 disables."),
            dict(key="history_keep_days", type="number", label="Keep activity history",
                 unit="days", min=0,
                 help="The state-transition log every chart is drawn from. 0 keeps it forever."),
        ],
    },
]

# Keys deliberately not shown: none. Anything in DEFAULTS is either above or listed here with the
# reason it is hidden, so the test can tell "not yet described" from "described as hidden".
HIDDEN = {}


def fields():
    """key -> field spec, flattened."""
    return {f["key"]: dict(f, section=s["id"]) for s in SECTIONS for f in s["fields"]}


def missing():
    """(keys in DEFAULTS with no spec, keys with a spec that no longer exist). The test asserts
    both are empty — which is what keeps this file honest as DEFAULTS grows."""
    have = set(fields()) | set(HIDDEN)
    known = set(core.DEFAULTS)
    return sorted(known - have), sorted(have - known)


def schema(cfg=None, masked=None):
    """The sections, each field carrying its CURRENT value. `masked` is the already-masked config
    the settings endpoint returns, so secrets are never re-read here."""
    cfg = cfg or core.load_config()
    values = masked if masked is not None else cfg
    out = []
    for s in SECTIONS:
        fs = []
        for f in s["fields"]:
            spec = dict(f)
            spec["value"] = values.get(f["key"])
            spec["default"] = core.DEFAULTS.get(f["key"])
            fs.append(spec)
        out.append(dict(s, fields=fs))
    unknown, stale = missing()
    return {"sections": out,
            # Surfaced rather than swallowed: a key with no description would otherwise be
            # invisible in the UI and unreachable except by editing config.json by hand — which
            # is the exact failure this schema exists to end.
            "undescribed": unknown, "stale": stale}
