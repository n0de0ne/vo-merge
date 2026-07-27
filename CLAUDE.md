# CLAUDE.md — vo-merge

Guidance for Claude (and humans) working on this repo.

## What this is

**vo-merge** is a **language companion to Radarr and Sonarr**: they decide *what* the library
holds, vo-merge decides *which languages each file carries*. You declare a target per kind of
title (`lang_profiles` — e.g. films fre+eng, anime fre+eng+jpn), it probes every file to see what
is actually there, finds a release on Prowlarr carrying what's missing, downloads it via
qBittorrent, and **grafts the missing audio and subtitle tracks into the existing library file**
— keeping the existing (usually higher-res) video, A/V-synced — then replaces the file in place
so Plex/Radarr paths stay valid.

The original case was a library of French-only dubs needing English, and that is still the
common one, but nothing in the gap decision, the scoring or the merge is specific to those two
languages any more.

Runs as one Docker container on an Unraid host ("Thor"). Repo: `github.com:alanstrok/vo-merge`.

## Stack & layout

- **Backend**: FastAPI + SQLite + APScheduler (`backend/app/`). Serves the built SPA too.
- **Frontend**: React + Vite + TS SPA (`frontend/src/`), built into `/app/static`.
- **Image** (`Dockerfile`): python:3.12-slim + **mkvtoolnix (mkvmerge)** + **ffmpeg** +
  **Intel iHD VAAPI driver** (non-free) for iGPU decode. Two-stage (node build → python).
- Listens on **8080** inside the container (Unraid template maps host **8090**).

### Backend modules
- `main.py` — FastAPI app + all REST endpoints (`/api/...`) + SPA static serving.
- `core.py` — SQLite (`movies`, `episodes` tables), `DEFAULTS` config, `load/save_config`,
  `set_status`/`set_ep_status` (**dynamic-column UPDATE — pass any column as kwarg**),
  `_ensure_cols` migrations, `log`/`tail_log`, `STATES`.
- `clients.py` — thin HTTP clients: `Prowlarr`, `Radarr`, `Sonarr`, `QBittorrent`, `Plex`.
- `media.py` — **file truth**: `mkvmerge -J` track inventory, ISO-639 normalisation, `und`
  resolution, the gap decision, subtitle ranking, and *arr-path → `/media` mapping.
- `pipeline.py` — movie pipeline: `scan` (Radarr gap) → `candidates`/`search_movie`/`score_release`
  → `grab`/`qb_grab` → `merge_movie` → `finish_movie`. Also shared helpers used by TV.
- `tv.py` — episode pipeline (mirrors pipeline.py); imports shared helpers from it.
- `sync.py` — A/V offset/drift detection: multi-window **consensus** + **linear-drift** fit.
- `offdet_video.py` — video **scene-cut** cross-correlation (primary sync signal).
- `offdet.py` — audio cross-correlation (fallback sync signal).
- `scheduler.py` — APScheduler: `_search_job` (every `search_interval_min`) and `_finish_job`
  (every `finish_interval_min`).

## Pipeline & states

`STATES`: pending → searching → no_release / grabbed → downloading → ready → merging →
merged / review / sync_fail / error / ignored.

1. **scan** — decide the gap by **probing the files** (see "Gap detection" below); Radarr/Sonarr
   supply only metadata. Skip French-origin if `exclude_french_origin`.
2. **search/score** — Prowlarr search; `score_release` asks one language-agnostic question:
   *does this release carry something this file is missing?* (see "Release language scoring"),
   **strongly prefers MULTI** (+200), boosts seeders/quality/id-match. `candidates()` powers
   both the auto-picker and the UI's interactive search.
3. **grab** — `qb_grab()` adds to qB (see VPN gotcha) and records the real infohash.
4. **promote** (`promote_completed`, own 1-min timer) — any download at 100% leaves
   `downloading` immediately: resolve `en_file` and move it to **`ready`** = the merge queue.
5. **finish** (`stage_finish`) — reconcile vanished torrents, **drop stalled torrents** (below),
   re-queue interrupted merges. Calls `promote_completed`; **never merges inline.**
6. **merge** (`merge_worker` → `merge_movie` / `merge_ready_episode`) — see merge rules below.
7. **finish_movie** — replace library file in place, trigger Radarr rescan + Plex scan.

### Merge queue & worker (why `ready` matters)

Merging used to run **inline inside the qB-poll loop**, so a finished download stayed
`downloading` until every earlier item had merged (hours for a 30–50 ep season pack) while
holding a grab slot — the merger could sit idle with completed downloads waiting. Now:

- `ready` is a **real queue** (FIFO by `updated`), not a marker set one line before a merge.
- A single daemon thread (`pipeline.merge_worker`, started in `scheduler.start()`) drains it
  one item at a time, claiming each atomically via `core.claim_movie`/`claim_episode`
  (`UPDATE … WHERE status='ready'` + `rowcount`) so nothing can ever be merged twice.
  `MERGE_WAKE` (an `Event`) makes a promotion start the merge immediately.
- A failed merge marks only that record `error` — it can no longer abort the whole cycle.
- **The in-flight cap counts `downloading` only** (`ACTIVE_STATES`), so a slot frees at 100%
  and new grabs continue while the queue drains. `KEEP_DONOR_STATES` still covers
  `ready`/`merging` so the orphan sweep never deletes a donor out from under the worker.
- Stale `merging` records (>15 min, not in `_merging_now()`) go back to `ready` rather than
  being re-merged inline. SQLite runs in **WAL** — the worker writes concurrently.

## Gap detection — read the files, not the metadata (`media.py`)

`scan_mode` (default **`files`**) decides where "this file is missing English" comes from.
It used to come from metadata *other tools* wrote down, and both sources are import-time
snapshots that fail **silently**:

- Sonarr's `mediaInfo.audioLanguages` is **empty for anything it never analysed**, and the old
  `_no_eng("")` returned False — i.e. "already has English". Those episodes were never fixable.
- Radarr's `vo-gap` tag is maintained by an external host script, so vo-merge inherited its lag.
- Neither notices a file replaced or remuxed outside the *arrs.
- A track tagged `und` (very common on FR rips) has no language at all — unreadable from metadata.

On identical test files the two modes disagree in **both** directions: legacy flags an episode
that already has English (wasted download) *and* skips one that has none (gap missed forever).

So `files` mode reads the container with `mkvmerge -J`. The *arrs are still the source for what
isn't on disk — title, year, tmdb/tvdb id, original language, anime numbering — but never for
the gap decision itself.

- **`media.norm_lang`** collapses 639-2/B (`fre`), 639-2/T (`fra`), 639-1 (`fr`) and plain names
  onto one canonical code, so a set comparison can't be defeated by spelling.
- **`und` tracks** fall back to the track NAME, then the FILE name, for an explicit marker
  (`VFF`, `TRUEFRENCH`, `English`). Still nothing → stays `und`, which `langs()` **excludes**, so
  the file reads as a gap. That errs toward adding a real English track; the opposite error
  leaves a French-only file forever. **`VOSTFR`/`VOST`/`SUBFRENCH` in a filename suppress the
  French hint** — they describe the *subtitles*, so the audio is the original language.
- **`lang_profiles` is the target end state per kind of title**, and the gap is simply
  *target minus present* — for both audio and subtitles:

  | kind | audio | subs |
  |---|---|---|
  | `movie` | fre, eng | fre, eng |
  | `series` | fre, eng | fre, eng |
  | `anime` | fre, eng, **jpn** | fre, eng |

  So a FR anime that already carries its Japanese VO is still missing English → a gap (the
  earlier "English *or* the original language counts as filled" rule silently skipped exactly
  those, which is the case the app exists for). The missing codes are stored per record in
  `need_audio`/`need_subs`, shown in the UI, and are what the merge grafts.
- **`media.kind_of`** picks the profile: Sonarr's own `seriesType == "anime"` wins, then a
  top-level library folder in `anime_dirs`, then a Japanese original language — which is what an
  anime *film* looks like to Radarr, since Radarr has no anime flag.
- **`media.wanted_audio` takes only target languages the base lacks, one track per language**, so
  a 3-dub donor doesn't triple the file size and a Spanish track nobody asked for isn't grafted.
  The original-language VO stays eligible as the fallback when no English exists.
- **A probe failure is not "no English."** Unreadable files are skipped and counted in the scan
  log, never guessed at.
- **Probe cache** (`probes` table, keyed by path, invalidated by size+mtime) — the first pass over
  a big library costs one `mkvmerge` per file; after that only changed files are re-read. A merge
  calls `core.forget_probe()` on the files it rewrote. `POST /api/rescan?forget=true` clears it.
- **Two scan modes, and the difference only shows after an interruption.** `forget=true` is a
  **full re-read**: the probe cache for that scope is dropped first, so every file is read again
  even if unchanged. `forget=false` is **progressive**: the cache is kept, so `mkvmerge` runs only
  for files with no valid probe — new imports, files changed on disk, and *whatever a previous
  pass never reached* because the container restarted mid-scan. It is resumable by construction,
  since each file's result is committed as it is read, so re-running picks up exactly where the
  last one stopped; on an already-probed library it costs a stat per file and no decoding.
  `media.STATS` counts read-vs-reused so a progressive pass reports "read 412, 8998 already
  cached" instead of looking identical to a scan that found nothing to do. Each tab has both
  buttons ("Scan new …" / "Re-read …").
- **`POST /api/rescan?scope=films|anime|series|all`** re-reads ONE library, so each tab has its
  own button (`tv.series_kind` partitions Sonarr into anime vs series, matching the 🎌 Anime and
  📺 TV Shows tabs; anime *films* live in Radarr so they belong to `films`). It deliberately
  ignores `scope_films`/`scope_series` and `series_pilot` — those gate what the pipeline *acts*
  on, whereas a rescan only reads files and records what's missing, so honouring them would
  silently skip most of the library. Records for a disabled scope sit as inventory until it's
  turned on. One scan runs at a time (`SCAN_LOCK`); it runs in a thread (minutes on a first pass)
  and `GET /api/rescan` reports progress + which scope is busy. `scope=all` (the Library tab's
  **"Re-read everything"**) covers films, anime and series in one pass. Every button sends
  `forget=true`, so the probe cache is dropped first and each file really is read again — the
  point of asking for a re-read is usually that you don't trust the cached answer. Each pass also
  prunes what has vanished (above).
- `scan` **only inserts records that have a gap** (a whole library of fine files would flood the
  pipeline), and a tracked record whose gap has since been filled is closed out as `merged`
  instead of being re-searched — which self-heals the DB from the stale-tag era.
- `media.to_media()` maps an *arr path to `/media` and **verifies it exists**, so a mis-mapped
  path is reported in the scan log rather than silently becoming a wrong `french_path`.

## Subtitles

Most French library files have no English subtitles, and the donor downloaded for its audio
usually ships them — so taking them costs one extra mkvmerge argument, not another download.

- `want_subs` (default on) + the profile's `subs` list + `max_sub_tracks` (default 2 per language).
- `_donor_opts()` builds the donor's track selection for both merge paths. Options precede the
  donor filename so they apply to it; the base keeps its own video, audio, subs and chapters.
- **Subtitles get the same `--sync` as the audio** — they're timed to the donor's video, so an
  offset *and* a PAL rate stretch apply identically (verified: a cue at 1000 ms lands at 1293 ms
  under `+250 ms, ×1.0427083`).
- `media.sub_rank()` picks *which* track when a pack ships six: full translation > forced/signs >
  SDH, and text beats image (PGS/VobSub). Grafted subs are **never default-flagged** — a default
  subtitle starts burned-in for every viewer.
- `subs_only_gap` (default **off**): a file with English audio but no English subs is not worth a
  whole download on its own. Turn it on to chase those too.
- A donor with no new audio but wanted subs still merges (subtitle-only graft); "nothing to add"
  only closes the record when there's neither.

## Instant pickup — *arr webhooks

The scheduled `_search_job` (every `search_interval_min`, default 60) already re-scans and picks
up newly imported media, and the probe cache makes that sweep nearly free. But it is a *sweep*,
so a new file waits up to an hour, and the scheduled scan honours `scope_series` + `series_pilot`.

`POST /api/hook/radarr` and `POST /api/hook/sonarr` turn an import into a scanned, queued record
in seconds. Point Radarr/Sonarr **Connect → Webhook** (POST, *On Import* + *On Upgrade*) at them.

- **Nothing in the payload is required.** *arr payload shapes differ by version, so the hook reads
  only `eventType` and the id, then fetches the authoritative record (`Radarr.movie(id)` /
  `Sonarr.series_one(id)`) and judges the FILE exactly like a scan does.
- Films reuse `pipeline.ingest_movie` — the same function the sweep calls per movie — so an import
  and a sweep can never disagree. TV runs `tv.scan(only_series=…)`, one targeted pass whose probe
  cache means only the file that actually changed costs an `mkvmerge`.
- `refresh=True` bypasses the probe cache: an import just rewrote the file and a fast disk can
  land the new one inside the cache's 1-second mtime tolerance.
- **`series_pilot` does not gate a single-series hook** — you asked about *this* import.
- `Test` returns OK (so the *arr Test button works), `Grab`/`Health`/etc. are ignored, and the
  delete events call `core.forget_probe()` so a removed file never answers from a stale probe.
- The hook answers immediately and ingests on a worker thread — a probe plus a Prowlarr search is
  far slower than an *arr webhook timeout. The follow-up search honours the same brakes as
  everything else (`hold_reason`, `SEARCH_LOCK`, the in-flight cap).
- `webhook_token` (empty = no check) adds `?token=…` if you ever expose the endpoint.

## What "merged" actually means (`merge_kind`)

`merged` is the terminal state for **three** different outcomes, and only two are work vo-merge
did. Conflating them made a library re-read look like thousands of merges in a day and filled
"Recently merged" with titles vo-merge never touched:

| `merge_kind` | what happened | `added_langs` |
|---|---|---|
| `grafted` | tracks muxed into the library file | the languages added |
| `replaced` | the download's video was ≥ the library's, so it *became* the file (`_place_multi`, TV direct remux) | `""` |
| `already` | the file already met its profile — the scan just closed the record out | `""` |

**Recently merged lists only rows with EVIDENCE the file changed** — `merge_kind='replaced'`
(which legitimately records no added languages, so it can only be recognised by its kind) or a
non-empty `added_langs`/`added_subs` (which also covers legacy rows written before the column
existed, with no migration). `already` fails both tests, which is the point; so does a `grafted`
row that recorded nothing added, since it says nothing about what the app did. The same predicate
drives the 24 h / 7 d counters. The header shows the whole breakdown, and rows badge audio and
subtitle languages separately — a subtitle-only graft has an empty `added_langs` and used to
render as a bare title.

## Language coverage & the Library tab (`/api/coverage`, `/api/library`)

"How much of the library is actually correct?" cannot be answered from `movies`/`episodes` —
`scan()` deliberately inserts a record only when a file HAS a gap, so those tables are a list of
problems, not an inventory. The **`probes` table is the only complete inventory**: every file the
scanner has read, with the languages read off it, gap or no gap.

`main._inventory(cfg)` is the single classifier over that table — it maps each probe to its
library folder, picks the profile that folder's kind targets (`anime_dirs` / `series_dirs` decide
which) and reports what's present vs missing. **Both endpoints read it**, so the number in the
chart and the list you can act on can never disagree about what "complete" means. The `-EN`
mirrors are skipped in one place — they're symlinks to the same files and would double-count.

- **`GET /api/coverage`** aggregates: per library total, `complete`, `missing_audio`,
  `missing_subs`, `missing_both`, `unreadable`, plus a per-language count for every target code.
  Rendered on the Overview (and under the Library tab) as a stacked bar + per-language mini bars.
- **`GET /api/library?state=complete|incomplete|unreadable|all&lib=&q=&limit=&offset=`** lists the
  files themselves — coverage says *how much*, this says *which ones*, which is the only form you
  can act on. `counts` covers the whole `lib`+`q` selection rather than the returned page, so the
  tab headers stay honest while paging. The **Library tab** is the UI: a
  Target-not-met / Complete / Unreadable switch, a library filter, a path search, and the global
  **"Re-read everything"** button.

Coverage only reflects what has been **probed**, so it is empty until a scan or a re-read has run,
and it grows as the library is read.

### "Unreadable" is four different things (`media.audit`)

Every probe failure used to collapse into one of two labels, and `no audio track` was the catch-all
— which is dangerous, because that is the one diagnosis that reads as "this file is broken". An
mkvmerge track list is empty for **four** unrelated reasons, and only one of them is about the file:

| `probes.err` | what it means | act on it? |
|---|---|---|
| `no audio track` | mkvmerge read the container, ffprobe agrees: **zero audio streams** | yes — broken |
| `audio mkvmerge can't read (N stream(s) per ffprobe)` | a codec we can't mux, in a container we can read | no — the file plays |
| `unsupported container [(N audio stream(s) per ffprobe)]` | `container.recognized/supported` is false — mkvmerge can't parse the format at all, so its empty track list says nothing | no |
| `unreadable` | neither tool could open it | no |

`media.probe()` therefore returns `ok` (mkvmerge's own recognized+supported), and
`media.ffprobe_audio()` asks a second tool. mkvmerge is the authority on what we can **mux**; it
is not the authority on what the file **contains**. Verified on real files: a video-only MKV →
`no audio track`; an FLV named `.mkv` **that has audio** → `unsupported container (1 audio stream)`;
19 bytes of text named `.mkv` → `unreadable`.

### Replacing audio-less files (`POST /api/library/repair`)

A file with no audio at all can never be fixed by grafting — there is nothing to sync against and
nothing to keep — so the only repair is a fresh copy. This deletes the operator's media, so it is
narrow by construction:

- **Only `no audio track`.** The other three errors above are statements about our tools.
- **Every candidate is re-probed with the cache bypassed** before anything is touched; a stale
  probe row can never authorise a deletion.
- **A file the *arrs don't know about is skipped** — deleting it would just lose the title, since
  nothing would search for a replacement.
- **Deletion goes through the *arr** (`Radarr.delete_movie_file` / `Sonarr.delete_episode_file`,
  then `MoviesSearch`/`EpisodeSearch`). Unlinking the file ourselves would leave the *arr believing
  it still has it, and it would never search — the whole point of removing it.
- **`dry_run` is the default** and changes nothing; the UI always plans before it offers the run.

A real run re-probes every candidate, so it runs in a thread under `SCAN_LOCK` (one heavy file
pass at a time) with `GET /api/library/repair` reporting progress. The Library tab's Unreadable
view breaks the count down by error and only offers the panel when something is genuinely
audio-less.

### A scan adds what's new; the prune drops what's gone

Nothing used to remove a probe or a record, so a title deleted from the library kept being counted
— and kept dragging the coverage percentage down — forever. Every rescan now starts with
`core.prune_missing_probes()` + `core.prune_missing_records()`. Two guards keep that from eating
the DB:

- **The mount must look alive.** If `/media` is missing or empty (an unmounted share) every path
  reads as gone, so the prune is skipped and logged rather than run.
- **Mid-flight records are never pruned** (`core._PRUNE_SKIP`: downloading / ready / merging /
  grabbed / searching) — a merge swaps a new file in, so the library path can be briefly absent.

`SCAN_STATE` carries `pruned` / `pruned_records` and the button reports "N deleted entries
removed" alongside the gap count.

## `updated` and `merged_at` mean specific things (`core._set_row`)

Two timestamps that look incidental decide what the whole Overview shows, and both used to be
re-stamped by writes that changed nothing — which is why "Needs attention" and "Recently merged"
reshuffled at intervals matching the background jobs rather than matching events.

- **`updated` = when the pipeline STATE last changed**, not when the row was last touched. It
  sorts Needs attention and orders the `ready` merge queue. But `ingest_movie` re-writes every
  record with its CURRENT status just to refresh `audio_langs`/`need_audio`, and `ai_health_check`
  re-writes error records every 3 min to stamp `ai_status` — so a record untouched for days
  jumped to the top whenever a scan or the sweep ran. `_set_row` writes
  `updated = CASE WHEN status=? THEN updated ELSE ?` — SQLite evaluates every SET expression
  against the ORIGINAL row, so the CASE sees the stored status even though the same statement
  assigns a new one. Pass `updated=<ts>` to force a bump.
- **`merged_at` is stamped on the TRANSITION into `merged`, and never again.** The old
  `fields.setdefault("merged_at", now)` only checked whether the CALLER passed one, not whether
  the row already had one, so every later write with `status='merged'` — including a scan
  re-reading an already-merged file — reset it to now. Old merges kept floating back into
  "Recently merged" and the 24 h / 7 d counters counted them again. Keying off the transition
  (rather than "is it NULL") also leaves pre-column rows alone, so an upgrade doesn't dump the
  back catalogue into "Recently merged" at once; those fall back to `updated`.

Both effects compounded in the dashboard's attention query, which read only the 400
most-recently-`updated` failing episodes before grouping — with ~400 failing episodes (one bad
season pack) that window both truncated the panel and slid on every unrelated write. It now reads
all of them (5000 backstop) and groups in Python.

`claim_movie`/`claim_episode` always bump, correctly: they are guarded by `WHERE status=<from>`,
so they only ever write a real transition.

## Pause & holds

`paused` (header button, `POST /api/pause`) is a brake, not a kill switch: no NEW searches,
grabs or merges start, but work already in flight finishes — aborting `mkvmerge` mid-write would
leave a corrupt library file — so load drops as the current merge ends, and the header says how
many are still going. **Scans keep running while paused**, which is the point: pausing is how you
let a library re-read finish undisturbed.

`pipeline.hold_reason()` is the single answer every stage and the UI share: `"paused"`, or
`"scanning"` when `SCAN_LOCK` is held. Searches also hold off during a rescan — grabbing off a
half-finished scan picks releases for gaps that may not exist and burns slots the scan is about
to re-price.

## Plex refresh — analyze, and in BOTH libraries

A merge rewrites the library file **in place**, so its path and name never change and a plain
`scan` will not re-read the streams — only `analyze` does. Two things this got wrong:
- the `-EN` mirror only ever got a bare `scan_path`, so a graft into a title already in the EN
  library (e.g. adding subtitles to a file that already had English audio) never showed up there;
- `_rating_key` returned only the FIRST match, but a mirrored title exists **twice** — once in
  `Films`/`Series`/`Anime` and once in `Films-EN`/`Series-EN`/`Anime-EN`, as two items with two
  keys — so whichever Plex listed first was analysed and the other stayed stale.

Now `mirror_to_en()` creates the symlink and *returns* the EN folder, and `plex_refresh(cfg,
[fr_dir, en_dir], …)` scans both folders and analyses **every** matching ratingKey, on **every**
configured PMS (`plex_url` + `plex2_url`). Verified: 2 servers x 2 folders scanned, 2 servers x 2
library copies analysed.

## Release language scoring (`media._DUB_MARKERS`)

Scoring used to know exactly two things — "French dub" (reject) and "English/MULTI" (boost) —
which is right for one library shape and blind to every other: a profile targeting German or
Spanish got no signal at all, and a Spanish-dub release wasn't recognised as a dub. Now the
markers are a **per-language table**, so the same rule works for any profile:

- `media.release_langs(title)` → *(languages advertised, multi, original)*.
- `media.useless_release(title, need)` — reject when a release advertises dubs and **none** is a
  language this file still needs. A release advertising nothing (`Movie.2019.1080p.BluRay`, the
  common shape) says nothing about its audio and is never rejected; nor is MULTI, nor VOST/VOSTFR
  (which state the audio is *original*, i.e. not a dub).
- `media.lang_hits(title, need)` — +60 (films) / +120 (TV candidates) per missing language the
  name advertises, so a dub of a language we need beats an unmarked release, and MULTI still
  beats both.

**Only the tag zone is inspected** — everything after the first year / SxxExx / resolution /
source token. That's where a scene release states its audio, and scanning only there is what
stops a film called *The German Doctor* from reading as a German dub. Verified both ways: that
title alone advertises nothing, while `The German Doctor 2013 GERMAN 1080p` advertises `ger`.
Two-letter codes (NL, DE, IT…) are deliberately absent from the table — they collide with
source/resolution tokens, and a false positive here **rejects** a good release.

`_place_multi` (films) and the direct-remux branch (TV) ask the same generalised question of the
*probed* release: `media.gap_langs(...)` reporting no audio gap means the download alone
satisfies the profile. Both were literal `fre`+`eng` checks before.

## Merge rules (important, non-obvious)

- **Keep the better video, graft the other's audio.** `_video_quality` = (height, bitrate).
  A lower-res MULTI must NOT replace a higher-res library file — it only donates audio.
  `_place_multi` (use the download directly) fires ONLY when the release's video ≥ the library
  file's; otherwise fall through to the graft path.
- **English preferred; VO fallback.** Target track is English; if a title's original language
  isn't English and no English exists, graft the **original-language (VO)** track instead
  (e.g. Norwegian for "Kraken"). `_orig_codes(name)` maps Radarr/Sonarr `originalLanguage`
  → ffprobe audio codes (excludes eng/fre, which are handled directly).
- **French is targetable too, but only when it's actually missing.** A French-dub-only release
  is normally rejected outright (`score_release`, `tv._search`): the library file already IS the
  French dub, so such a release adds nothing and would just burn a slot. That guard is why
  French could never be downloaded. It now consults the record's `need_audio`, so an English-only
  or JP-only file — where `fre` genuinely is the gap — can grab a French release. It earns no
  MULTI bonus (250 vs 50), so a MULTI or English release still wins whenever one exists. With no
  `need_audio` recorded the old conservative behaviour applies.
- **"Nothing to add" is decided from the file, not from "is English present".** Both merge paths
  used to demand English and error otherwise, which rejected a donor carrying exactly the
  language the record was short of. Now, when the donor contributes no track, `media.gap_langs`
  re-checks the base: still missing something → blocklist that release and try another; profile
  met → mark it done.
- **Framerate mismatch → drift, not reject.** Don't reject on differing fps; let
  `sync.detect()` measure a **linear drift** and apply `mkvmerge --sync TID:offset,num/den`.
  Only bail if auto-sync is off, or if fps differ but no reliable drift could be measured.
- **Sync confidence gates outcome.** `sync.detect()` returns `(offset_ms|None, conf, method,
  drift)`. None / low-confidence / inconsistent → `review` (if `sync_review`) or retry another
  release. `merge_movie` sets status to **`merging` BEFORE** detection and streams a per-window
  `progress` field (UI polls every 8s).
- Output replaces the library (French) file in place; forced `.mkv`; named after the library file.

## Sync detection (sync.py / offdet_video.py)

- Several analysis **windows** across the runtime; per-window video scene-cut offset.
  Majority agree → constant offset; fall on a line (R²≥0.93) → linear drift; inconsistent → reject.
- **Decode is offloaded to the iGPU and downscaled ON the GPU**: `scale_vaapi/scale_qsv` +
  `hwdownload` so only tiny frames reach the CPU. ~7× faster on 4K than `-hwaccel vaapi` alone
  (which downloads full 4K surfaces for a software scale). Software fallback ONLY on real
  hwaccel failure (rc≠0 / 0 cuts) — never on a merely low-action window.
- Audio cross-correlation (`offdet.py`) is the fallback when video windows don't resolve.

### Rate-ratio detection — the PAL path (`sync.ratio_detect` / `offdet_video.ratio_scan`)

A PAL transfer plays 24/23.976fps content at 25fps, so the FR copy runs **~4.27% short**. Window
matching can't see a rate difference — worse, it's *destroyed* by one: at 4.27% the cut pattern
smears ~20s **inside** a single 480s window, so every window's correlation peak flattens, few
clear the confidence gate, and the survivors' offsets fan out over tens of seconds
(the observed "deltas 67–333s" — that is a PAL signature, **not** a different cut).

So we don't measure the stretch, we **hypothesis-test** it:
- `RATE_RATIOS` holds the ratios that physically occur (25/23.976, 23.976/25, 25/24, 24/25,
  24/23.976, 23.976/24) plus 1.0 as a control; `ratio_candidates()` also derives the exact ratio
  from the two files' measured fps and from their duration ratio.
- `ratio_scan()` extracts scene cuts **once per file** (the only expensive part — one ffmpeg pass
  each) over `sync_ratio_span` seconds, then per ratio just rescales the donor's time axis and
  FFT-correlates. A dozen hypotheses cost ~nothing on top of the two decodes.
- `k` maps donor→base (`base_t = k*donor_t + offset`), i.e. **k = donor_fps / base_fps**, which is
  exactly what `mkvmerge --sync TID:offset,k` applies. It's persisted in `sync_drift`.
- **Two guards against inventing a stretch**: the winner must clear `sync_ratio_min_conf` (0.35)
  *and* beat the no-stretch hypothesis by `sync_ratio_margin` (1.3×). On synthetic PAL data the
  true ratio scores 0.97 vs 0.04 for 1:1, and the adjacent 25/24 ratio only 0.08 — the
  discrimination is sharp.
- Runs as a **fast path** when fps are known to differ (skips five doomed window scans) and as a
  **fallback** when windows disagree or don't resolve — so it still fires when `probe()` returns
  no fps (`fps_close` fails open on `None`).
- `verify_hint` now quick-verifies a *ratio* hint too, so a PAL season pack doesn't redo the full
  scan for every episode.

## Stalled-download handling

`_is_stalled(t, cfg)`: incomplete + active > `stall_timeout_min` (default **5**) + `dlspeed==0` +
(no swarm seeds or qB state in `DEAD_DL_STATES`). **Absolute cap:** a download
active past `dl_max_age_min` (default 720 = 12h) is dropped too, even if it's still trickling —
a release that can't finish in that long isn't worth the slot.

Three details that decide whether a dead torrent is actually caught:
- **Dead magnets get their own, much shorter timeout** (`meta_timeout_min`, default **2**). A
  torrent still in `metaDL`/`forcedMetaDL` has no metadata, so no peer has ever answered it —
  there is no slow download to be patient with. Making these wait the full `stall_timeout_min`
  is what produces the "0% · fetching metadata · 0 seeds" pileup that parks dead magnets in
  every grab slot.
- **`_swarm_seeds()` treats qB's `num_complete: -1` as *unknown*, not as a seed count.** Reading
  it naively makes an un-scraped swarm look seeded, so `seeds == 0` never fires and a genuinely
  dead torrent is only caught if its state string happens to match.
- **`DEAD_DL_STATES` includes `pausedDL`/`stoppedDL`** — nothing in vo-merge can resume a
  torrent, so a paused donor would otherwise hold its slot forever. When stalled: delete from qB,
**blocklist that release** (add `dl_id` to `tried`), re-search for another (better-seeded)
release; give up to `no_release` after `max_sync_retries`. Movies: `drop_stalled`; TV
season-packs: `_drop_stalled_eps`. A download that **completes but yields no usable video** is
NOT left stuck in `downloading`: movies call `reject_and_retry`, TV calls `_drop_stalled_eps`
(both blocklist the release and re-search).

Completed-download dead-ends all now terminate instead of looping forever in `downloading`:
- **path never becomes visible** (mount race) — retried `NOT_VISIBLE_MAX` (10) promote passes,
  then `error`.
- **TV: files parsed but none map to a gap episode** — after aired↔absolute translation has been
  tried (below), the pack genuinely doesn't hold these episodes. Sets the episodes `error` naming
  what the download holds vs what's needed (`tv.fmt_se`, e.g. "download has S01E01-E06, this
  needs S01E138-E145"), so it surfaces in Review/AI instead of squatting a slot.
  **"Claimed nothing" is not the same as "matched nothing".** `promote_completed` tracks `mapped`
  (a file found a library episode) separately from `claimed` (it won the race to queue it). A
  file whose target is already `ready`/`merging` is a no-op; treating that as failure — which the
  `claimed == 0` test used to do — overwrote correctly queued records with `error` and produced
  the tell-tale message with *identical* parsed and wanted lists. `promote_completed` also runs
  behind `PROMOTE_LOCK`/`TV_PROMOTE_LOCK`, because it is driven by its own 1-min timer AND by
  `stage_finish` every 10 min, so the two passes overlapped and raced each other's claims.

## Episode numbering: aired ↔ absolute (anime)

Anime libraries are routinely filed with **absolute numbering flattened into S01** (E01…E51…)
while releases use **aired seasons** (absolute 51 = S04E15). Neither side is wrong — they're two
numbering schemes for the same episode — but with no translation table:
- no donor file can map (`S04E15.mkv` vs a library record keyed `(1, 51)`), and
- the search composes `Title S01E51`, which no indexer can match, so it falls back to the S01
  pack and **re-grabs the same pack forever** — every episode past S01's real length errors out.

Sonarr already knows both numbers (`absoluteEpisodeNumber` on `/api/v3/episode?seriesId=`), so
`tv.py` reads the table (cached per series, 1 h; a Sonarr failure is *not* cached) and translates
at every point where one side's `(season, episode)` meets the other's:

| Helper | Used by |
|---|---|
| `_numbering(sid)` → `(aired2abs, abs2aired)` | everything below; two empty dicts for plain TV |
| `_release_se(ep)` → the S/E a **release** uses | `stage_search` grouping + query, `_assign_pack`, `season_candidates`, `episode_candidates` |
| `_abs_num(ep)` | matching absolute-numbered singles (`Title - 51`) via `_abs_ep_re` |
| `_alt_keys(sid, s, e)` → library keys a donor file could also mean | `promote_completed`'s gap lookup |

Two properties keep this from inventing mappings:
- **Only S01 records are read as possibly-absolute.** Within aired season 1 the absolute number
  *is* the episode number, so translating an S01 key is the identity right up to where the
  library's flattened numbering runs past season 1 — exactly where the mismatch starts.
- **`_alt_keys` is consulted only after a direct match failed**, so a correct direct mapping can
  never be displaced by a translated one. No absolute numbers (plain TV) or Sonarr unreachable →
  every helper returns the identity and behaviour is exactly as before.

`_pack_seasons` also takes a **lone season token in the release title over the caller's default**
(an absolute-as-S01 library asks for "season 1" but the pack says S04), and `_assign_pack` claims
by *release* season — so an S04 pack claims E37…E52 rather than all of S01 or nothing at all.
`GET /api/episode/{id}/context` returns a `numbering` block and `/api/tv/episodes` stamps an
`aired` field, so both the AI and the UI can see which scheme a record is filed under.

## AI-assisted review (round-trip)

A record landing in `error`/`review`/`sync_fail` is auto-escalated to the host AI dispatcher and
its outcome is written back so failures surface for a human:

1. **Auto-page** — `pipeline.ai_health_check` (stall sweep, every 3 min) files an `errors-review`
   ticket for NEW records and stamps them `ai_status='pending'` (+`ai_at`). Manual escalation:
   `POST /api/movie/{id}/ai` and `POST /api/episode/{id}/ai` (the Review tab's 🤖 button).
2. **Act** — the host Unraid cron runs the Claude Code CLI on the ticket; the agent acts via the
   REST actions embedded in the ticket (`/sync`, `/another`, `/research`, `/ignore`, `/retry`, …).
3. **Report back** — the agent POSTs `/api/movie/{id}/ai_result` or `/api/episode/{id}/ai_result`
   with `{status: resolved|failed|needs_human, verdict, action_taken}`. This stamps `ai_status`/
   `ai_verdict`/`ai_at` and **leaves the pipeline `status` untouched** (so the specific failure is
   preserved and no re-page loop is triggered). No host-script change is needed — the callback is
   just another action in the ticket.
4. **Manual review** — records with `ai_status` `failed`/`needs_human` are highlighted at the top
   of the **Review tab** (movies + TV episodes). If the dispatcher never calls back within
   `ai_stale_min` (default 60) while still in a problem state, `ai_health_check` flips it to
   `needs_human` — catching a silently crashed AI.

**"Needs attention" means NEEDS YOU, not "something failed."** Every failure reaches the AI within
3 minutes, so a panel listing all of them is mostly a list of things already being worked on. It
shows only `ai_status IN (failed, needs_human)` plus `review` (a human decision by definition);
anything still with the AI is counted ("N with the AI"), not listed. With `ai_tickets` off nothing
could ever reach those states, so the filter falls back to showing everything.

**`GET /api/ai_log?outcome=resolved|failed|needs_human|all`** is what the AI actually reported,
newest first, and it needs its own query for a structural reason: a callback deliberately leaves
the pipeline `status` untouched, so a record the AI FIXED has usually moved on — back to
`pending`, or `downloading`, or `merged`. Nothing that selects by pipeline status can find it, so
the Review tab (which lists problems by status) would never show a single thing the AI solved:
its work was invisible exactly when it succeeded, and the only trace left in the UI was its
failures. The Review tab's **"🤖 What the AI did"** panel renders it — each row pairs the verdict
with the record's CURRENT status, so "resolved · now merged" and "resolved · still error" read
differently. Only records with a real `ai_at` are returned.

**The On-call AI panel says whether the dispatcher is alive.** It runs on the host, outside this
app, so the only evidence is whether it calls back. `resolved`/`failed` are verdicts it produced
itself; `needs_human` is *mostly the no-callback flip*. A wall of `needs_human` with
`last_callback: null` means the dispatcher never ran — which, unless reported separately, looks
exactly like "it examined everything and gave up". The panel says which it is.

New DB columns: `ai_status`, `ai_verdict`, `ai_at` on both `movies` and `episodes` (via
`_ensure_cols`). Gated by the existing `ai_tickets` flag.

### What the agent can actually DO (the action surface)

Diagnosis was never the bottleneck — acting was. Every ticket now advertises these, and
`GET /api/movie|episode/{id}/context` is the "read this first" call: it returns the record, a
probe of both files (fps/duration/audio tracks), the matching log lines, and — for episodes —
**every donor file with the (season, episode) `_parse_se` read from it, plus the series' episode
list**. Comparing those two lists *is* the diagnosis for a numbering mismatch.

| Endpoint | Fixes |
|---|---|
| `POST /episode/{id}/assign {path}` | **Numbering the automatic aired↔absolute translation can't cover** (Sonarr has no absolute numbers, or the pack numbers its files a third way). Maps ONE donor file to one episode and queues the merge. The agent reads `/context`, works out the mapping, calls this per episode. |
| `POST /movie\|episode/{id}/set_sync {offset_ms, drift}` | Applies a **known** offset and/or rate stretch with no detection (`drift` = donor_fps/base_fps; 1.0427083 = film→PAL). `_merge_*_impl` honours a stored `sync_drift` when the offset is manual. |
| `POST /search_releases {query}` | The `no_release` backlog. The built-in search composes its own query from the library title, so a title it never matches can never be found however often it re-searches. This runs an arbitrary Prowlarr query (original/romaji/alternate title, no year) and returns links to `/grab`. |
| `POST /movie\|episode/{id}/unfixable {reason}` | Terminal give-up **with a recorded reason** (sets `ignored` + `ai_status=needs_human`), so it doesn't read as an unexamined skip. |

## Config (`core.py:DEFAULTS`, persisted to `/config/config.json`)

Keys you'll touch most: `scan_mode` (**files**|tag), `scan_all_movies`, `lang_profiles`, `anime_dirs`, `want_subs`/`max_sub_tracks`/`subs_only_gap`, `*_url`/`*_key` for Prowlarr/Radarr/Sonarr/qB/Plex, `en_indexer_ids`,
`multi_indexer_ids`, `grab_mode` (auto|approval), `scope_films`/`scope_series`, `min_seeders`,
`score_threshold`, `max_sync_retries`, `sync_*` (windows/window_dur/hwaccel/threads,
`sync_ratio_test`/`sync_ratio_span`/`sync_ratio_min_conf`/`sync_ratio_margin` for the PAL path),
`stall_timeout_min`/`dl_max_age_min`, `search_interval_min`/`finish_interval_min`/
`promote_interval_min`, `max_inflight_downloads` (download slots) /`max_parallel_merges`
(concurrent merges, applied live via `MERGE_GATE`), `enabled` (master switch),
`ai_tickets`/`ai_stale_min` (AI-review escalation, see below).
Most of these are editable in the UI under **Settings → Queues & limits**.
Secrets are masked in the GET /api/settings response.

### Paths / mounts (all three must line up)
- `/media` = host `/mnt/user/Plex` (Radarr/Sonarr/Plex report `/data/...` — see `plex_media_prefix`).
- `/downloads/audio-merge` (movies) and `/downloads/audio-merge-tv` (TV) — **this container and
  qB must see the same files at the same path** (`downloads_mount` == `qb_download_dir`).
- `/config` — SQLite DB, config.json, logs, preview cache.

## Gotchas (most cost real debugging time)

- **qB is behind a VPN killswitch (Gluetun) and CANNOT reach LAN Prowlarr.** So vo-merge must
  **fetch the .torrent itself and upload the bytes to qB** (or pass a magnet). `qb_grab()` does
  this: resolve link (`_fetch_torrent` follows http→magnet/.torrent), upload, then **confirm the
  real infohash by diffing the category's hash set before/after** (qB's `/torrents/add` never
  returns the hash — do NOT trust any `added_torrent_ids` field).
- **`docker exec` needs `-i`** to receive a heredoc on stdin (no `-i` → empty stdin → silent no-op).
- **A `&`-backgrounded process inside a Bash tool call dies when the call returns.** To run a
  long merge out-of-band, fire it through the API so it runs in uvicorn:
  `urllib.request.urlopen("http://localhost:8080/api/movie/<id>/merge", data=b"", timeout=4)`
  (the client times out; the sync endpoint keeps running server-side).
- **`set_status`/`set_ep_status` write whatever columns you pass** — add the column via
  `_ensure_cols` first or the UPDATE throws.
- Container `ps` is unreliable (minimal image); check `/proc/*/cmdline` or `docker stats`.

## Build / deploy / iterate

- **Hot-patch a running container** (fast dev loop):
  `docker cp backend/app/<f>.py vo-merge:/app/app/<f>.py && docker restart vo-merge`
  (syntax-check first: `docker exec -i vo-merge python3 -c "import ast,sys;ast.parse(open('/app/app/<f>.py').read())"`).
  Hot-patches survive restart but **a container rebuild reverts them — commit to git.**
- **Frontend**: build with a throwaway node container, copy `dist/` into `/app/static`:
  `docker run --rm -v "$PWD/frontend":/app -w /app node:22-slim sh -c "npm install && npm run build"`
  then `docker cp frontend/dist/. vo-merge:/app/static/` (clear old `static/assets/*` first).
- **git** (push only when asked): `export GIT_SSH_COMMAND="ssh -o UserKnownHostsFile=/root/.ssh/known_hosts -o StrictHostKeyChecking=no"`; branch is `main`.
  End commit messages with `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`.

## External pieces that live OUTSIDE this repo (on the Unraid host)

These are Unraid **User Scripts** under `/boot/config/plugins/user.scripts/scripts/`, not in git:
- **`plex-en-filter`** ("Plex VO/EN Mirror + gap tagger") — builds filtered `Films-EN/Series-EN/
  Anime-EN` symlink libraries (a file is kept if it has English **or** original-language audio,
  read from Plex's own SQLite DB; original language from Radarr/Sonarr). Also maintains the
  **`vo-gap`** tag that vo-merge's `scan` consumes. REPORT-mode default.
- **`sonarr-import-cleaner`** — resolves Sonarr's stuck manual-import queue (import-to-replace
  when a release adds audio, blocklist-but-keep-seeding the rest).

The host-level CLAUDE.md (`/mnt/user/CLAUDE.md`) documents the Unraid server itself.

## Retrying failures

`POST /api/retry_errors` re-queues **every** failed record — movies and episodes, `error` and
`sync_fail` — and the Review tab's "↻ Retry N failed" button calls it. Per-record:
`pipeline.retry_movie` / `tv.retry_episode`, which **blocklist the release that failed** (append
`dl_id` to `tried`), **drop its donor from qB**, clear the grab fields and reset `attempts`
before going back to `pending`. Two reasons that matters:
- a bare flip to `pending` (what `/movie/{id}/retry` used to do) re-searches and can pick the
  very same broken release, so "retry" looped;
- `attempts` gates `reject_and_retry` → `sync_fail`, so without the reset a `sync_fail` record
  would fail straight back to `sync_fail` on its first hiccup. An operator asking for a retry
  means "try again", so the budget is refreshed while `tried` (the blocklist) is kept.

`review` is deliberately excluded — it means a human must decide, not that something failed.

## CI

`.github/workflows/docker-publish.yml` builds and pushes to GHCR on `main`, on `v*` tags, and on
`claude/**` branches, so a feature branch can be pulled onto Unraid before it merges. Only `main`
publishes `:latest`; a branch build is tagged with its sanitised branch name
(`ghcr.io/n0de0ne/vo-merge:claude-<branch>`).

**The image name is pinned to a literal owner on purpose.** It used to be
`ghcr.io/${{ github.repository_owner }}/vo-merge`, which silently followed the `alanstrok` →
`n0de0ne` account rename: new builds went to the new path while the Unraid template kept pulling
the old one. **GHCR does not redirect a renamed owner the way git repos do** — the old path
answers `manifest unknown`, Unraid reports `TOTAL DATA PULLED: 0 B`, and the server keeps running
a stale image with no obvious error. If the account is renamed again, change the workflow and the
Unraid template together.
