# CLAUDE.md — vo-merge

Guidance for Claude (and humans) working on this repo.

## What this is

**vo-merge** is a **language companion to Radarr and Sonarr**: they decide *what* the library
holds, vo-merge decides *which languages each file carries*. You declare a target per kind of
title (`lang_profiles` — e.g. films fre+eng, anime fre+eng+original), it probes every file to see what
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
- **Frontend**: React + Vite + TS SPA (`frontend/src/`), built into `/app/static`. Laid out like
  Sonarr/Radarr — a persistent left rail, a thin top bar carrying global state, and a hash-routed
  page (see "The SPA" below).
- **Image** (`Dockerfile`): python:3.12-slim + **mkvtoolnix (mkvmerge)** + **ffmpeg** +
  **Intel iHD VAAPI driver** (non-free) for iGPU decode. Two-stage (node build → python).
- Listens on **8080** inside the container (Unraid template maps host **8090**).

### Backend modules
- `main.py` — FastAPI app + all REST endpoints (`/api/...`) + SPA static serving.
- `inventory.py` — the ONE classifier over the `probes` table (`build`, `totals`, `DID_WORK`).
  Lived inside main.py, so it was reachable only from a request handler; the daily history sampler
  needs the same answer, and a second definition of "complete" is the one duplication this app
  can't afford.
- `history.py` — the only endpoints with a time axis (see "Charts need a time axis" below).
- `problems.py` — the failure taxonomy: which cause a failure has, and the remedy that fixes it.
- `lipsync.py` — audio-vs-PICTURE sync, the only absolute sync signal here.
- `settings_meta.py` — every config key described as data, so the settings page renders itself.
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

### The SPA (`frontend/src/`)

It was one 2,330-line `App.tsx` with a row of tabs. The shape is now:

```
App.tsx            the shell: rail + top bar + hash route + API-key gate + error banner
lib/router.ts      a hash router in thirty lines
lib/poll.ts        usePoll, the global error sink, runAction, useStored
lib/format.ts      every formatter, so a table cell and a tooltip can't disagree
components/ui.tsx  Pill, Poster, RowMenu, Tile, Modal, DownloadBar, Tracks, Act…
components/charts.tsx   the SVG chart kit (no chart library — 500KB to draw four charts)
components/modals.tsx   ReleaseModal, SyncEditor (ported verbatim), LipSyncModal
pages/*.tsx        Overview, Films, Series, Library, Activity, Problems, Settings, System
```

Three things the old shape made impossible, in rough order of how often they hurt:

- **Nothing was linkable.** The current tab lived in `useState`, so Back did nothing, a reload
  always landed on the Overview, and "look at this failing title" was a set of instructions rather
  than a URL. Every page, filter and search term is now in the hash.
- **There was nowhere to put a count.** A rail carries the Problems badge on every page — the one
  number that means *you* have work, as opposed to the machine having work.
- **Activity did not exist.** The queue was a cramped modal and there was no history at all.

Chart rules worth not undoing: colour follows the ENTITY, never its rank (filtering a series out
must not repaint the survivors); the categorical palette is validated as a set against the panel
surface and used in fixed order; status (complete / short / broken) uses a **reserved** palette and
always ships a label with a count, never hue alone; a `null` in a series breaks the line rather
than plotting at zero; and every chart has a table twin behind a toggle, which is what makes a
colour-encoded chart acceptable at all. Never two y-axes on one plot.

## Pipeline & states

`STATES`: pending → searching → no_release / grabbed → downloading → ready → merging →
merged / review / sync_fail / error / ignored.

1. **scan** — decide the gap by **probing the files** (see "Gap detection" below); Radarr/Sonarr
   supply only metadata. **Every file is targeted** — the profile decides what a title should
   carry, and where it was made has no bearing on that.
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
  and new grabs continue while the queue drains. `KEEP_DONOR_STATES` covers `ready`/`merging`
  so the orphan sweep never deletes a donor out from under the worker — and **`error` too**,
  because the AI's advertised repair for a numbering mismatch is `POST /episode/{id}/assign
  {path}`, which needs those exact files, and the host dispatcher arrives minutes to hours later.
  Deleting them 3 minutes after the error made that repair path dead on arrival.
- **Nothing merges inline — including the API.** `/merge`, `/sync`, `/set_sync` and
  `sync_probe apply` all go through `pipeline.enqueue_merge`, which sets `ready`, wakes the
  worker and returns (409 if the worker already holds the record). Calling `merge_movie()`
  directly took the MERGE_GATE slot but neither *claimed* the record nor registered it in
  `_MERGING_NOW`, so an operator or AI merge raced the worker: both wrote the same
  `_merged/<name>.mkv` and both moved it onto the library file.
- Stale `merging` records (>15 min, not in `_merging_now()`) go back to `ready` rather than
  being re-merged inline. The 15-minute test needs a real heartbeat: `_set_row` deliberately
  holds `updated` when the status is unchanged, so a merging record's timestamp is frozen at the
  moment it started and any merge past 15 minutes looked interrupted. `pipeline._beat` forces
  the bump (safe: a `merging` record is in neither the attention panel nor the FIFO queue).
  SQLite runs in **WAL** — the worker writes concurrently.

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
  | `anime` | fre, eng, **orig** | fre, eng |

  So a FR anime that already carries its Japanese VO is still missing English → a gap (the
  earlier "English *or* the original language counts as filled" rule silently skipped exactly
  those, which is the case the app exists for). The missing codes are stored per record in
  `need_audio`/`need_subs`, shown in the UI, and are what the merge grafts.
- **`orig` is a target, not a language** (`media._resolve_targets`). The anime profile's third
  audio slot shipped as a literal `jpn`, because anime is Japanese — so the slot's *meaning*
  ("keep the ORIGINAL audio") and its value were indistinguishable. They are not: **Arcane is
  filed as anime and made in French**, so a literal `jpn` is a gap no release on earth can fill.
  Its 18 episodes searched forever and ended up `ignored` while their FR+EN audio *and* subs were
  already complete. `orig` resolves per title from Radarr/Sonarr's `originalLanguage` — Japanese
  anime still targets `jpn`, a French one targets French (which it has), a Korean one `kor`. An
  **unknown** original language (Radarr reports `"?"`) drops the slot rather than inventing a
  target; `norm_lang`'s 3-char fallback would otherwise have made `"?"` itself a language.
  `core.migrate_config()` rewrites a persisted `jpn` to `orig` once, at startup — behaviour for
  Japanese anime is unchanged, so there is nothing to decide. A deliberately-typed third language
  (e.g. `ger`) is left alone and still targeted.
- **A settled record whose file a scan now finds COMPLETE is closed out** (`pipeline.CLOSEABLE`),
  including `ignored`. That is not a contradiction of "`ignored` survives a rescan": that rule is
  about not re-*searching* a deliberate give-up, and a file meeting its profile has no work left.
  Leaving it flagged — usually with the AI's give-up verdict still attached — just misreports a
  finished title. This is what clears the Arcane episodes.
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

### External subtitles count (`media.sidecar_subs`)

Bazarr — and most subtitle tooling — writes subtitles as **files beside the media**
(`Some Film (2020).en.srt`), not muxed into the container. `mkvmerge -J` reports only what is
inside, so counting embedded tracks alone means every subtitle Bazarr ever fetched still reads as
missing: coverage never improves, and with `subs_only_gap` on vo-merge would download a release
for a subtitle already sitting on disk.

The library layout is `/media/<Films|Series|Anime>/<title or show>/[Season NN/]<file>`, with
sidecars in the same folder sharing the file's stem, so "same directory, same stem" finds them.
`audit()` returns the UNION of embedded tracks and sidecars — both play in Plex, so both satisfy
the profile.

- `<stem>.en.srt`, `<stem>.eng.forced.srt`, `<stem>.fr.sdh.srt` → forced/sdh/hi/cc/signs and
  friends are qualifiers, not languages, and are skipped.
- **Only a language `_ALIAS` actually knows counts.** `norm_lang` falls back to the first three
  characters of anything unrecognised, which would read a bare `<stem>.srt` as the language
  "srt" — and mark the file as already subtitled. A sidecar with no recognisable language token
  stays `und`, i.e. excluded, exactly like an untagged embedded track.
- A neighbouring episode's sidecar can't be attributed to this file: the stem must match.

**The probe cache had to learn about them.** It is keyed on the media file's size and mtime, and
neither changes when Bazarr drops an `.srt` next to it — so a progressive scan would answer from
a probe taken before the subtitle existed and never re-read it. `probes.sidecars` stores a
fingerprint of the sidecar set (name + size); `core.get_probe(path, sidecars=…)` treats a
mismatch as a miss. Verified: adding *and* removing an `.srt` both force a re-read while the
`.mkv` is untouched.

A season folder is listed once, not once per episode (`_scandir`, 30 s TTL — short because the
webhook path probes outside any scan pass and a permanent entry would go stale).


Most French library files have no English subtitles, and the donor downloaded for its audio
usually ships them — so taking them costs one extra mkvmerge argument, not another download.

- `want_subs` (default on) + the profile's `subs` list + `max_sub_tracks` (default 2 per language).
- `_donor_opts()` builds the donor's track selection for both merge paths. Options precede the
  donor filename so they apply to it; the base keeps its own video, audio, subs and chapters.
- **Subtitles get the same `--sync` as the audio** — they're timed to the donor's video, so an
  offset *and* a PAL rate stretch apply identically (verified: a cue at 1000 ms lands at 1293 ms
  under `+250 ms, ×1.0427083`).
- `media.sub_rank()` picks *which* track when a pack ships six: full translation > SDH > forced >
  **signs & songs**, and text beats image (PGS/VobSub). Grafted subs are **never default-flagged**
  — a default subtitle starts burned-in for every viewer.

### "English Signs" is not an English subtitle (`media.is_signs`)

A *signs & songs* track translates on-screen text and the OP/ED lyrics — **nothing that is
spoken**. It exists for viewers watching a dub, who need the billboards translated and nothing
else. It is tagged `eng` like any other subtitle, so it used to satisfy the profile's `eng`
subtitle target, and Blue Lock S01E03 ended up with exactly one English subtitle track — "English
Signs" — while reading as complete. Someone who doesn't understand the audio cannot watch the
episode with it.

Three places had to agree, or the fix leaks:

- **`media.langs()` excludes a signs-only track** from that language's subtitle set, exactly like
  `und`. So the gap stays open, coverage counts the file as incomplete, and (since `merged` is
  re-opened when a scan still sees a gap) the record gets another release. A language that *also*
  has a real track is of course still present — only signs-only is excluded.
- **`sub_rank` sorts signs LAST, below even forced.** A forced track at least subtitles the
  dialogue it covers. Signs used to sort *ahead* of forced, which is how a donor carrying both
  handed over the signs track.
- **`wanted_subs` takes nothing at all** for a language the donor covers only with signs. Grafting
  it would neither close the gap nor help anyone — and since the gap stays open, the next donor
  would graft its signs track too, and the one after that, until the file carries five useless
  tracks and still isn't subtitled. With nothing to add, the existing "nothing to add" path
  blocklists that release and searches for another.

Sidecars follow the same rule: `<stem>.en.signs.ass` doesn't count (`_SIGNS_QUAL`), while
`<stem>.en.srt` does. Detection is by track/file name (`signs`, `songs`, `S&S` — mkvmerge has no
flag for it); `Designs` and `English SDH` are correctly not matched.

**Files already probed keep their cached answer** — the probe cache stores the resulting language
codes, not the track list — so a library grafted before this fix needs a **re-read** (Library tab
→ "Re-read everything", which drops the cache) before those records re-open.
- **`subs_only_gap` (default on): chasing a subtitle-only gap.** Bazarr is the cheaper tool —
  a 50 KB `.srt` beats a multi-GB release — but it searches subtitle *providers*, and when the
  subtitle exists only inside a *release*, an indexer is the only place to get it. Two scoring
  rules invert when the audio is already complete and only a subtitle is missing:
  - **the language judgement must include `need_subs`.** Otherwise `need_audio` is empty, so
    `useless_release` reads every language-marked release as "a dub we already have" and REJECTS
    a plainly useful `Movie.2019.ENGLISH.1080p`.
  - **video quality stops mattering, and size starts.** A 2160p remux is 60 GB and its English
    subtitle track is byte-identical to the 900 MB WEB-DL's, so the resolution bonus is dropped
    and small releases are preferred. MULTI drops from +200 to +40 for the same reason: at +200
    it swamped the size preference and a 38 GB pack beat a 2 GB one for a few KB of text.
  `_place_multi` can't misfire on the result — it only replaces the library file when the
  release's video is at least as good, and a deliberately tiny release never is.
- A donor with no new audio but wanted subs still merges (subtitle-only graft); "nothing to add"
  only closes the record when there's neither.

## Charts need a time axis, and nothing had one (`history.py`)

Everything this app stored was a SNAPSHOT: `movies`/`episodes` are current state, `probes` is what
each file holds right now. So every question of the form *is this getting better?* was
unanswerable, and the one feature that needed a rate — the forecast — had to reconstruct it from
`merged_at`, the single timestamp that happens to persist, which exists for one of the twelve
states. Two series fix that permanently:

- **`events`** is the transition log. It is written from `core._set_row` (plus the two `claim_*`
  helpers) — the one place every status change already passes through — which is why it cannot
  drift from reality. `_set_row` now reads the row before updating it, because a blind UPDATE
  can't tell a real transition from a scan re-writing the same status, and that difference is the
  entire content of the log — but where the write was GUARDED (`expect=`, or the `claim_*`
  helpers' `from_status`) that guard is the authoritative `from`, since the read is a separate
  statement the merge worker can commit between. Titles are **denormalised into the row**:
  `prune_library` deletes records for files that have left the library, and a chart that silently
  loses its early months is worse than no chart. Entering `searching` is skipped (every pending
  record is claimed in and out of it on every sweep, which would turn hundreds of meaningful rows
  a day into tens of thousands) — but LEAVING it is kept, because `searching → no_release` is the
  outcome of the search and the whole reason to look.
  `merge_kind` rides along as `tag`, so a library re-read closing out thousands of already-correct
  files does not draw as thousands of merges.
- **`coverage_history`** is a daily roll-up of `inventory.totals`, keyed by DAY so re-sampling is
  idempotent — every rescan samples, and a busy day must not weigh more than a quiet one. Sampled
  at the end of every scan, by the daily housekeeping job, and once at startup if the table is
  empty but the library has been probed (otherwise an existing install stares at a blank chart
  until the next scan).

**The two series are read differently, and conflating them is how a chart starts lying.** A day
with no coverage SAMPLE comes back `null` — nobody probed the library that day; the library did
not become 0% complete, and plotting it at the baseline shows a collapse that never happened. A
day with no EVENTS really is zero, and filling it in is the point: leaving it out compresses the
axis and turns a quiet week into a vertical cliff. `test_revamp.py` pins both directions.

**`inventory.totals` classifies identically to `main.coverage`, down to the elif order, and the
trend line divides by the same denominator the panel does** (every probed file, unreadable
included). Both rules were briefly "improved" here — skipping a subtitle-only shortfall when
`subs_only_gap` is off, and dropping unreadable files so the chart could reach 100% — and each
change was defensible alone while making the chart disagree with the panel drawn directly above
it about the same library on the same day. Two numbers labelled "complete" that differ is the
exact failure this module was extracted to prevent: the rule changes in both places or in neither.

## "When will the library be at 90%?" (`GET /api/forecast?target=`)

Answerable from what is already kept, and worth wiring up precisely because the intuitive answer
is wrong: the coverage percentage moves slowly enough that eyeballing it tells you nothing.

- **Denominator**: the `probes` inventory, via `_inventory` — the same classifier `/coverage` and
  `/library` use, so the forecast can't disagree with the chart above it about what "complete"
  means.
- **Rate**: real completions only, using the module-level `DID_WORK` predicate that also drives
  Recently merged and the 24h/7d counters (hoisted out of the dashboard for exactly this reason).
  Divided by the span actually OBSERVED, not the nominal window — a 30-day rate on a three-day-old
  install must not divide by 30. The `+1` in that span is not a fudge: a merge two days ago means
  activity across three days, so first-to-now understates it by one.
- **`blocked`** is the honest part. The remaining incomplete files are not uniformly reachable —
  `no_release` has nothing to find, `ignored` is a deliberate give-up, and an unreadable file is
  not a language problem at all. When those alone put the target out of reach the ETA is
  **withheld** and the reason says what needs unblocking, rather than emitting a confident date
  the pipeline cannot deliver. Same when nothing has completed in 30 days: no rate, no date.

The Overview panel shows the working (files left, measured rate) rather than just a date, and
renders the no-date case differently, because an absent estimate must not look like a confident
one. It is a straight-line projection and says so.

## Priority — a requested title must not queue behind the backlog

Both queues are ordered by recency: `stage_search` walks `pending` by `updated`, and the merge
queue is FIFO. That is right for a backlog nobody is waiting on, and wrong for the one case where
someone IS: a title just requested, imported French-only, and wanted tonight. With a few thousand
records ahead of it, a `updated`-ordered queue may never reach it.

`priority` (INTEGER, 0 = normal) is consulted **before** the usual rule in both places —
`core.get_movies`/`get_episodes` order `COALESCE(priority,0) DESC, updated DESC`, and
`pipeline.merge_queue` sorts on it first. Both matter: getting a requested film downloaded
promptly achieves nothing if it then queues behind thirty season-pack episodes, each of which is a
sync detect plus a remux. Higher levels sort ahead of lower, so `1` is fine for "soon" and a
larger number for "now".

- **Set by hand**: `POST /movie|episode/{id}/priority {"level": 1}`, `POST /tv/{series_id}/priority`
  for a whole show (TV is requested per series, not per episode — and it skips
  `merged`/`ignored`, which have nothing left to do). The Films row menu has ★ Prioritise, and a
  prioritised row is starred.
- **Set automatically by the *arr webhook** (`priority_on_import`, default 1). An import event IS
  someone asking for a title, so the hook that already turns an import into a scanned record now
  also puts it at the front. Bulk-importing a whole library makes everything priority, which
  degrades to the old ordering rather than breaking anything — turn it off for the duration if
  that matters.

`core.set_priority` deliberately touches neither `status` nor `updated`: priority is orthogonal to
the state machine, and `updated` means "when the state last changed", so moving it here would
reshuffle the attention panel for something that is not a state change. Priority is **not** cleared
when a title finishes — if a merged record is later re-opened because its file is still short of
the profile, something someone asked for is still something someone asked for.

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
- `webhook_token` (empty = no check) adds `?token=…` if you ever expose the endpoint. It is
  exempt from `api_key` — the *arrs can't be taught a custom header.

## API access (`main._guard`)

The API had no authentication of any kind, which is defensible for a LAN-only tool right up to
the point where its endpoints delete media (`/library/repair`), rewrite service credentials
(`/settings`) and drop torrents. Two checks now sit in one middleware on the `api` sub-app:

- **Origin refusal, always on.** Several of those endpoints take only query parameters, so a
  plain HTML form on any page the operator visits could fire them cross-site — and since a form
  POST triggers no preflight, CORS (correctly absent) never got a say. A browser always sends
  `Origin` on a state-changing request and cannot forge it, so refusing a *present and mismatched*
  Origin closes that shape while leaving every real caller alone: the SPA matches `Host`, and
  curl / the host AI dispatcher send no Origin at all.
- **`api_key`, off by default.** Empty preserves the historical open-on-the-LAN behaviour. Set it
  and every call needs `X-API-Key` (or `?apikey=`); the SPA prompts once and keeps it in
  localStorage, and a 401 clears it so a rotated key re-prompts instead of leaving blank panels.
  `/hook/*` and `/health` are exempt — the *arrs and the container healthcheck can't send headers.

**Path containment (`main._under`).** `os.path.join(root, user_path)` **discards `root` when
`user_path` is absolute**, so a join is not a containment check even though it looks like one.
That is how `GET //config/config.json` served the API keys, the qB password and both Plex tokens
(reproduced against a live server; dot-segment traversal was already normalised away, which is why
it survived casual testing). Both the SPA file server and `/episode/{id}/assign` now resolve the
path and require it to stay under a trusted root — for `assign`, one of the real donor
directories, so a *library* file can no longer be nominated as the donor for another episode.

## Nothing runs unbounded (`pipeline.run_mux`, `sync._decode_timeout`)

No mux or decode had a timeout. A wedged mkvmerge or ffmpeg — a stalled `/mnt/user` read, or the
iGPU driver hanging a decode — parked the merge worker **forever**, and at the default
`max_parallel_merges: 1` that silently stops all merging. Worse, the worker had already registered
the record in `_MERGING_NOW`, which is exactly what shields it from the stale-merge requeue, so
the one recovery mechanism was disabled precisely when it was needed.

- `mux_timeout_min` (240) and `sync_decode_timeout_min` (30) bound the two classes. A decode
  killed on timeout reports `offdet_video.TIMEOUT_RC` so `scene_cuts` does **not** then retry in
  software — that fallback would hang for just as long a second time.
- `run_mux` owns all four mux sites and **removes the partial output on every failure path**. On
  rc≥2 (disk full being the classic cause) the half-written file used to be left in
  `<libdir>/_merged/`, where Plex can index it as an alternate version and where it compounds the
  very disk-full that produced it, once per retry.
- A test walks the AST of every backend module and fails if any `subprocess` call lacks a
  `timeout` — grep can't see the multi-line ones.

## Two things that look like one thing

- **A measured sync offset is not an instruction (`sync_manual`).** A merge records what it
  measured, and a stored non-zero `sync_offset_ms` used to make the next merge skip detection
  entirely. Since a scan re-opens a `merged` record whose file still has a gap, the routine path
  was: merge at +38 s against one donor, re-open for a missing subtitle, grab an *unrelated*
  donor, mux it with a blind 38-second shift, mark it `merged`. Only `/set_sync` sets
  `sync_manual=1`, and only that skips detection. `pipeline.DONOR_RESET` clears every
  donor-specific field as one unit — the sync fields were exactly the ones each retry path forgot.
- **An unreachable indexer is not a verdict (`pipeline.SearchUnavailable`).** `candidates()` and
  `tv._search()` returned `[]` on any exception, which the callers read as "nothing suitable
  exists" and wrote `no_release` — a state that then sits out `no_release_retry_h` (24 h). One
  Prowlarr restart during a sweep could park a large slice of the backlog for a day on a decision
  nobody made. Now they raise; records stay `pending`, the sweep abandons early rather than
  burning its budget confirming the indexer is down, and the interactive endpoints return 503.

## Writes that must not clobber (`core._set_row(expect=…)`)

A scan reads a record, decides what status it should carry, then writes it back — and the merge
worker can claim `ready → merging` in between. Writing unconditionally put `ready` back underneath
a running merge, and the record was claimed and merged a second time into the same output path.
`set_status`/`set_ep_status` take `expect=` and return whether the write applied; losing it is
harmless, because the scan was only refreshing language columns and the next pass redoes it.

Also here: a **re-opened record gets `attempts=0`**. Nothing reset it, so a record re-opened at
`attempts=4` grabbed one release and hit `max_sync_retries` on its first stall — an effective
budget of 1 per 24-hour cooldown instead of the configured 4.

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
  **The target list is not uniform inside a library** — the anime profile's `orig` slot resolves
  per title, so Blue Lock targets `jpn` and Arcane doesn't — so the counters accumulate the UNION
  of targets and each language is scored against `audio_of`/`subs_of` (how many files actually
  target it), not against the library total. Sizing them from the first file seen threw a
  `KeyError` on the first title wanting something extra, and since the panel swallowed a failed
  fetch the entire chart silently vanished; it now shows the error instead of rendering nothing.
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

### Every file is inventory, and every file is targeted

`exclude_french_origin` used to skip French-origin titles entirely. It has been **removed**, and
the two bugs it caused are worth remembering because both are easy to reintroduce:

- It `return`ed **before the file was ever probed**, so those titles were absent from the `probes`
  table — which is the library inventory. They vanished from the Library tab, the per-library file
  counts and the coverage denominator: Films reported 2,041 files against 2,429 on disk. A whole
  French-origin *series* disappeared the same way in `tv.scan`.
- The premise contradicted the profile model. `lang_profiles` says what a title should CARRY;
  where it was made has no bearing on that. A French film with French audio and no English is
  missing English exactly like any other film — and in practice those are titles that benefit
  most, since a French-original release rarely ships an English track.

So there is no "not targeted" state: a probed file is complete, incomplete, or unreadable.
`pipeline.SCAN_SKIPS` (surfaced on `GET /api/rescan` as `skips`) records the reasons an *arr-known
file still didn't become an inventory row — `no_file` / `unmapped` / `unreadable` — so a file count
that doesn't match the library can be explained instead of guessed at. `unmapped` in particular
means `media.to_media` couldn't find the path under `/media`, which is a mount/prefix problem
rather than a missing file.

### A scan adds what's new; the prune drops what's gone

Nothing used to remove a probe or a record, so a title deleted from the library kept being counted
— and kept dragging the coverage percentage down — forever. `pipeline.prune_library` is the one
shared implementation, run by **every rescan and by the daily housekeeping job** — the prune only
lived inside the operator's re-read before, so on an unattended install a deletion was never
swept at all. Two guards keep it from eating the DB:

- **The mount must look alive.** If `/media` is missing or empty (an unmounted share) every path
  reads as gone, so the prune is skipped and logged rather than run.
- **Mid-flight records are never pruned** (`core._PRUNE_SKIP`: downloading / ready / merging /
  grabbed / searching) — a merge swaps a new file in, so the library path can be briefly absent.

The webhooks cover the *arr-driven case instantly (a delete event calls `core.forget_probe`);
the daily prune is what catches files removed behind the *arrs' backs. `SCAN_STATE` carries
`pruned` / `pruned_records` and the button reports "N deleted entries removed" alongside the gap
count.

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
- `media.useless_release(title, need, orig=, need_subs=)` — reject when a release advertises
  audio and **none** of it is a language this file still needs. A release advertising nothing
  (`Movie.2019.1080p.BluRay`, the common shape) says nothing about its audio and is never
  rejected; nor is MULTI. **VOST/VOSTFR is no longer a free pass**: those tags are a concrete,
  checkable claim — the audio is the ORIGINAL language, the subs French (VOSTFR) or English
  (VOSTA) — so with the title's original language known, a VOSTFR that fills no remaining gap is
  rejected. That is the Colony bug: a Korean film missing only the French DUB grabbed a VOSTFR
  (Korean audio + French subs, definitionally unable to help) and sank an hour of sync into it.
  Unknown original (`orig=None`/`"?"`) keeps the old conservative pass.
- `media.lang_hits(title, need)` — +60 (films) / +120 (TV candidates) per missing language the
  name advertises, so a dub of a language we need beats an unmarked release, and MULTI still
  beats both.
- **The merge itself re-asks the question of the FILES** (`pipeline.graft_gains`, before sync
  detection — the expensive part): the OUTPUT (base languages + everything grafted) must give
  the LIBRARY file something it still lacks, whichever file won the video comparison. This is
  the second half of the Colony bug: the release won the quality comparison, became the base,
  and `wanted_audio` — which asks what the *base* lacks — happily grafted the library's own
  English track while the output still lacked `fre` and the library's video got replaced by the
  rip. The deliberate VO fallback stays a gain (Kraken's Norwegian); anything else that adds
  nothing needed is rejected and blocklisted before a single decode runs.

**Only the tag zone is inspected** — everything after the first year / SxxExx / resolution /
source token. That's where a scene release states its audio, and scanning only there is what
stops a film called *The German Doctor* from reading as a German dub. Verified both ways: that
title alone advertises nothing, while `The German Doctor 2013 GERMAN 1080p` advertises `ger`.
Two-letter codes (NL, DE, IT…) are deliberately absent from the table — they collide with
source/resolution tokens, and a false positive here **rejects** a good release.

**`VOF` is a French-audio marker, and it matched nothing for a long time.** "Version Originale
Française" = a French-ORIGINAL title, so the release carries French and nothing else — but the
bare `VF` alternative refuses a letter *before* it (the `O` blocks it) and the VOST-family `VO`
refuses one *after* (the `F` blocks it), so VOF fell between both guards, advertised no language
at all, and was scored on seeders alone. A French film missing ENGLISH would happily grab one.
It now sits in the `fre` markers (and in `_NAME_HINTS`, so a VOF filename also resolves an
untagged `und` audio track to French). Note the deliberate cost: a French BluRay of a French
film often *does* embed English subtitles its name never mentions, so a subtitle-only gap loses
that occasional donor — the same trade `FRENCH` already made, and Bazarr is the cheaper tool for
subtitles anyway.

`_place_multi` (films) and the direct-remux branch (TV) ask the same generalised question of the
*probed* release: `media.gap_langs(...)` reporting no audio gap means the download alone
satisfies the profile. Both were literal `fre`+`eng` checks before.

## Merge rules (important, non-obvious)

- **Replacing outright is only for a self-sufficient release.** `_place_multi` (films) and the
  TV direct-remux branch skip the mux and let the download BECOME the library file — which also
  throws the library file away. The test was `gap_langs(...)[0]`, i.e. **audio only**, so a MULTI
  with no subtitle tracks would replace a file that had French subs and silently drop them. It
  now requires the release to meet the whole profile (audio AND subs) *and* to carry everything
  the library file already had. `gap_langs` is deliberately not reused: it suppresses a
  subtitle-only shortfall when `subs_only_gap` is off, exactly the case this must not ignore.
  Sidecar `.srt` files survive either way — the replacement keeps the library file's name, so
  they still match its stem.
- **A settled state is not a promise that the file meets its target.** `stage_search` only ever
  reads `pending`, so every other settled state is a dead end for a file that still has a gap.
  Two must be re-opened, on different terms (`pipeline.reopen_status`, shared by both scans):
  - **`merged`** never meant "complete", only "a merge ran" — the file can still be short of what
    the donor didn't carry. Re-opened as soon as a scan notices; "done while incomplete" is just
    wrong. These are the "merged but still missing English" rows.
  - **`no_release`** means nothing suitable existed *when we looked*, which indexers make untrue
    over time. Re-opened after `no_release_retry_h` (default 24), measured from `updated` —
    which, since it only moves on a real state change, is exactly when the record entered
    `no_release`. Without a cooldown an hourly sweep would re-query hundreds of titles that
    genuinely don't exist.
  - **`ignored` is in neither set**: it is a deliberate give-up (a human, or the AI via
    `/unfixable`) and must survive a rescan.
- **`POST /api/recheck?scope=`** ("Re-check finished", on the Films and TV tabs) is the operator
  saying *treat everything below target now*: it walks only the settled records instead of the
  whole library and ignores the cooldown. The release already tried stays blocklisted (`tried`),
  so a re-opened record searches for a DIFFERENT one; only `attempts` is refreshed.
- **`finish_movie` re-reads the file it just wrote.** Otherwise the record keeps the languages
  recorded at SCAN time, so a merge that added English still displayed the pre-merge track list —
  and one that fell short looked complete — until the next library scan.
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
- **Sync confidence gates outcome, but a human is the LAST resort.** `sync.detect()` returns
  `(offset_ms|None, conf, method, drift)`. None / low-confidence / inconsistent →
  `reject_and_retry`, which blocklists that release and fetches a DIFFERENT one, up to
  `max_sync_retries`; only once that budget is spent does it land in `review` (if `sync_review`)
  or `sync_fail`. `sync_review` used to short-circuit on the FIRST failure, so the four-release
  budget was never spent and every mismatch became a manual "pick another release" that nothing
  in the pipeline would ever do for you — the one instruction an unattended system cannot follow.
  TV had no retry path at all and was terminal on one try. `review` KEEPS the donor, because the
  point of that state is that someone can still align this exact pair with `/set_sync`.
  The message itself is now actionable: it states both framerates and the exact stretch to apply
  (`_sync_fail_reason`, snapped to the textbook transfer ratio since ffprobe rounds 23.976), so
  the on-call AI can go straight to `/set_sync {"drift": …}` rather than being told to pick a
  release by hand. `merge_movie` sets status to **`merging` BEFORE** detection and streams a per-window
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

### The offset search has a ceiling (`sync_max_lag_s`)

`detect_offset_video_ms` cross-correlates the two files' scene-cut patterns and takes the argmax
over lags within **±`sync_max_lag_s`**. That bound was hard-coded at **20s**, which is a real
ceiling and not a tuning knob: a BD-vs-WEB anime pair routinely differs by **30–60s** — a sponsor
or logo card the WEB version carries, a "previously on" the BD drops. Past the bound the true
correlation peak is sliced off *before* the argmax, so every window locks onto noise (conf ~0.06–0.09,
under the 0.3 per-window gate), the windows disagree, and `detect()` concluded **"different cut"** —
for a plain constant offset that `--sync` would have fixed.

Verified end to end on real files (donor = base + 40s of leader): at ±20s it reports +8.84s with
conf 0.09 and the window is discarded; at ±60s and above it reports −40.00s with conf 0.89.

Widening is **free** — the FFT already covers the whole window, only the slice the argmax runs
over changes — and **safe**: the confidence is normalised, so unrelated files don't correlate at
any lag (0/200 random cut-train pairs cleared the 0.3 gate at ±20, ±90 or ±180s), and the
multi-window consensus (several windows agreeing within 150ms) still has to be satisfied. Default
is now **120s**, configurable. The reject log says how far it searched instead of guessing
"different cut?".

### Measuring an offset without merging (`POST /movie|episode/{id}/sync_probe`)

The AI could re-run detection (`/sync`, which fails the same way) or apply an offset (`/set_sync`)
— but it had no way to **ask what the offset is**, so on a "couldn't sync" it could only guess or
give up. That is why those tickets came back as "different cut; manual pick or ignore".

`sync_probe` runs detection with a much wider `max_lag_s` (default 300, up to 900) and **reports
the result without merging**; `apply: true` merges with what it finds. It returns the duration
delta alongside the offset, which is the discriminator:

- duration differs **and** the windows agree on an offset → extra material at the head/tail, which
  `--sync` fixes;
- duration differs and the windows **disagree** → material inserted in the middle, a genuinely
  different cut that no single offset can align.

Both ticket templates advertise it as the FIRST call to make on a sync failure.

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

## The donor's path goes stale (`pipeline.resolve_donor_path`)

`en_file` is captured when a download completes, from the torrent's `content_path`. But qB then
**moves** the finished torrent out of its incomplete directory into the completed one, and the
merge — which runs later, off the `ready` queue — still holds the pre-move path. `os.path.exists`
fails and the record is marked `merge: missing en_file` while the donor is sitting on disk,
intact, under its new name. It is a stale cached path, not a missing file.

This is **not** the Unraid mover: both containers read through `/mnt/user`, a FUSE union the
mover is transparent to. Nothing here is mover-aware, and nothing should be.

Both merge entry points (`_merge_movie_impl`, `merge_ready_episode`) therefore re-ask qB before
declaring the donor gone — qB is the thing that moved it and always knows where it is now:

1. the cached path still exists → return it (no qB call at all);
2. otherwise `torrent(dl_hash)` → `content_path`, then `save_path`, each through `_qb_to_local`
   (qB says `/data/...`, we read `/media/...`), taking the largest video when it's a directory;
3. otherwise the **file list**, which is relative to `save_path` and so survives a rename of the
   torrent's own root folder;
4. nothing resolves → `None`, and the caller's existing error path is still exactly right.

A resolved path is written back to the record, so the next stage and the operator see the truth.
`os.path.exists` is kept as the final gate on both paths. `clients.QBittorrent.torrent(hash)`
exists for step 2: `hashes=` on `/torrents/info` costs one small request and finds the torrent
whatever category it is in.

The orphan sweep is the other way a donor goes missing, and is already guarded:
`KEEP_DONOR_STATES` covers `ready`/`merging`, and a record in `grabbed` carries no `dl_hash` yet
(it is written at `downloading`), so the sweep cannot match one — the 30-minute `added_on` grace
is what covers that window.

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

A record landing in `error`/`review`/`sync_fail` is auto-escalated to the on-call AI dispatcher
and its outcome is written back so failures surface for a human:

1. **Auto-page** — `pipeline.ai_health_check` (stall sweep, every 3 min) picks up NEW records and
   stamps them `ai_status='pending'` (+`ai_at`). Manual escalation: `POST /api/movie/{id}/ai` and
   `POST /api/episode/{id}/ai` (the Review tab's 🤖 button).
2. **Act** — the dispatcher (see below) acts via the REST actions in the brief (`/sync`,
   `/another`, `/research`, `/ignore`, `/retry`, …).
3. **Report back** — the agent POSTs `/api/movie/{id}/ai_result` or `/api/episode/{id}/ai_result`
   with `{status: resolved|failed|needs_human, verdict, action_taken}`. This stamps `ai_status`/
   `ai_verdict`/`ai_at` and **leaves the pipeline `status` untouched** (so the specific failure is
   preserved and no re-page loop is triggered). No host-script change is needed — the callback is
   just another action in the ticket.
4. **Manual review** — records with `ai_status` `failed`/`needs_human` are highlighted at the top
   of the **Review tab** (movies + TV episodes). If the dispatcher never calls back within
   `ai_stale_min` (default 60) while still in a problem state, `ai_health_check` flips it to
   `needs_human` — catching a silently crashed AI.

### The dispatcher is a supervised component, not a silent host cron (`agent.py`, `deploy/dispatcher/`)

vo-merge writes **one ticket per record** into `/config/ai-tickets/` (`review-m{id}` /
`review-e{id}` — the same shape for an auto-page and the operator's Send-to-AI button). The
consumer is the **resident dispatcher** (`deploy/dispatcher/`), which speaks a take/ack
protocol — claim a ticket by renaming it into `claimed/`, delete it on the callback, retry a
crashed run and dead-letter it into `dead/` after `MAX_ATTEMPTS`, and **touch `.heartbeat`
every cycle**. Two editions, same protocol:

- **`dispatcher.sh` on the Unraid host (the deployed one)** — a User Scripts loop ("At First
  Array Start Only", or hourly with `ONESHOT=1`) running the host's already-logged-in `claude`
  CLI, i.e. the operator's **Claude subscription, not API-key billing**. This replaces the
  legacy ticket cron: same tickets directory, but the legacy script never wrote a heartbeat or
  claimed/dead-lettered, so a dead cron was indistinguishable from a slow one.
- **`dispatcher.py` in a container** — for non-Unraid hosts; auths by mounting the host's
  `~/.claude` login (subscription) or an `ANTHROPIC_API_KEY`.

Why this shape — the previous one failed silently: the host cron died, tickets piled up, every
record aged past `ai_stale_min`, and the whole backlog flipped to "AI did not respond", which
read exactly like an agent that examined each one and gave up. That was the state this install
was found in. Now:

- **A dead fixer is an EVENT.** `pipeline.check_dispatcher` (3-min sweep) alarms out-of-band
  when the oldest queued ticket exceeds `ai_dispatcher_alarm_min` AND the heartbeat is
  absent/stale — both signals, because a live sidecar with a deep queue is slow, not dead, and
  a healthy hourly cron keeps no heartbeat but drains the queue. The default (120) sits above
  common cron intervals so a legacy setup doesn't false-alarm.
- **The queue is capped and honest.** `ai_health_check` files at most `ai_max_tickets` queued
  tickets; overflow records stay UNSTAMPED (never `ai_status='pending'` without a ticket on
  disk — the staleness timer must not run on a page that was never sent) and get picked up as
  the queue drains. A record that recovers on its own has its queued ticket withdrawn — it
  describes a solved problem, and its presence would block the record's next page — and the
  never-delivered `pending` stamp cleared with it.
- **`agent.movie_context` / `episode_context` build the brief in ONE place**, so an
  operator-escalated record and an auto-escalated one can't be given different instructions.
- **`agent.status()`** reports queued/claimed/dead counts, the oldest queued age, and
  `heartbeat_age` — dead-vs-slow is a reported fact, not an inference.

**The `ai_stale_min` timeout measures how long the dispatcher has HELD a ticket**, not how long
the ticket has existed (`agent.undispatched()`, consulted by the staleness sweep) — a backlog
bigger than the dispatcher's throughput must not age out records no agent has opened. The
staleness flip covers **movies and episodes alike in `error`/`review`/`sync_fail`/`no_release`**
(episodes-in-review used to be missed and showed "AI working" forever), and each flip triggers
the needs-human digest on the notify channel.

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
| `POST /search_releases {query}` | The `no_release` backlog. The built-in search now walks a **query ladder** itself (original title → *arr title → alternate titles, once `search_rounds` ≥ 1), so this is the step BEYOND the ladder: an arbitrary Prowlarr query for whatever the AI can think of that Radarr/Sonarr didn't know. Records that exhaust `no_release_escalate_rounds` fruitless rounds are paged once with exactly this instruction. |
| `POST /movie\|episode/{id}/unfixable {reason}` | Terminal give-up **with a recorded reason** (sets `ignored` + `ai_status=needs_human`), so it doesn't read as an unexamined skip. `ignored` is re-examined every `ignored_revisit_days` with one cheap search (capped/day) — "no release exists" decays as truth, so the verdict must not be permanent by accident. |

## Failures have CAUSES, and each cause has a remedy (`problems.py`, `GET /api/problems`)

The Review tab listed failures one per row, newest first, with the raw `error` string and four
generic buttons. That is fine for five failures and useless for four hundred: one bad season pack
produces forty rows saying the same sentence, and the actual decision — *this whole group is a PAL
transfer, apply the ratio* — is invisible because nothing ever puts the forty rows next to each
other. Worse, the generic buttons don't match the causes: "Retry" on a numbering mismatch
re-downloads a pack that will fail identically, and "Pick another" on a vanished donor is right but
nobody could know that from the message. So records that had a mechanical fix sat in the backlog
looking impossible.

`problems.classify(status, error)` maps a failure onto one of ~18 causes, matched against the
**literal strings** `pipeline.py` and `tv.py` write. `test_revamp.py` parametrises every one of
those strings, so changing a message without updating the taxonomy fails the build — a taxonomy
that has silently stopped matching production is worse than none, because the page still looks
authoritative. Each cause carries `why` (what happened), `fix` (what will help) and an ordered
list of remedies, plus a severity that sorts **fixable ahead of judgement ahead of external** —
the page exists to empty the mechanical bucket, and burying it under a large judgement group is
how a backlog stops looking actionable.

Two things make this more than a nicer list:

- **The PAL ratio is already in the error string.** `_sync_fail_reason` writes the arithmetic
  ratio into the message so the on-call AI needn't re-derive it; `problems.drift_of` reads it back,
  and "apply the computed rate" becomes a button that fixes a whole season at once. It is offered
  ONLY when a record in the group actually quotes a ratio — a button that does nothing is worse
  than no button.
- **Explicit keys beat the group.** `POST /problems/act` takes either a `code` (the whole group)
  or `keys` (a selection), and keys win. That is the difference between retrying six records and
  retrying four hundred, and it must never be decided by accident.

Slow remedies (anything that decodes video) run in a background thread behind one lock with the
same progress-state shape the rescan and repair passes use — two bulk passes over the same records
would race each other's writes, and the second would act on records the first had already moved.

## Lip-sync: the only ABSOLUTE sync signal (`lipsync.py`)

Every other measurement here is RELATIVE. `offdet_video` correlates the donor's scene cuts against
the base's; `offdet` correlates their audio envelopes; `qc_grafted_audio` correlates the grafted
track against the base's own track inside the output. **All three are satisfied by two files that
agree with each other and are both wrong** — a donor with a 40s leader and a base with the same
leader align perfectly — and none of them can say anything at all about a file with one audio
track. Correlating mouth movement in the PICTURE against the speech envelope of a track asks a
different question, and the picture is ground truth.

- **Finding the mouth without a face detector.** A talking mouth is the thing in frame whose motion
  sits at the syllable rate (2–8 Hz), so instead of detecting a face we per-pixel FFT the motion
  volume and keep the pixels whose energy lives in that band. The per-frame median is subtracted
  first — a pan or a shake moves every pixel at once and would otherwise be the strongest
  "speaker" in frame. opencv sharpens this when importable but is **not a dependency**: OpenCV 5
  removed `CascadeClassifier` and the bundled cascade data, so shipping it would have pinned us to
  4.x and a routine dependabot bump would have silently disabled the better path.
- **The search range comes from the AUDIO.** The visual window is short (video decode is the
  expensive part) and the speech envelope is decoded padded by ±`max_lag` on each side; the
  correlation slides the short signal over the long one. So widening the search to ±60s costs one
  more ffmpeg *audio* pass, not five times the frames. Normalisation is against the **local** audio
  norm (cumulative sums), not the whole envelope — otherwise one loud passage scores every lag
  near it highly and the peak lands on volume rather than on alignment.
- **The sign is the correction, not the displacement.** `measure()` returns what `--sync` should
  ADD to the audio's timestamps; negative means the audio plays late and must be pulled earlier.
  Getting this backwards would double the error rather than cancel it, so it is pinned by a test
  over +/−/0 displacements.
- **The rescue is ONE measurement, not a difference of two.** `rescue()` correlates the BASE's
  picture against the DONOR's audio directly, which is exactly the number the merge needs.
  Measuring each file against its own picture would give each file's internal displacement and say
  nothing about the content offset *between* them. It also never compares the two PICTURES —
  precisely the comparison that fails on a different cut. It is skipped when the framerates differ
  and no stretch was found: lip-sync returns one number, and a constant cannot correct a rate
  difference.
- **What it honestly cannot do.** On original-language audio the phonemes match the lips and it is
  good to a frame or two. On a DUB it still works but coarser (~±150 ms), because dubbing matches
  *when* each character speaks rather than how their lips move — which is enough for the failures
  that matter, and is not a lip-sync QUALITY judgement. It fails, and says so, on films with
  little on-screen dialogue, narration over cutaways, animation, and windows landing on action or
  music. Hence the multi-window consensus: windows that don't resolve are discarded and
  `lipsync_min_windows` must agree.

Wired in three places: `POST /movie|episode/{id}/lipsync` (measure, optionally apply), the rescue
rung below `wide_probe_rescue` in both merge paths, and an optional post-merge gate
(`lipsync_qc`, off by default — it costs a second decode pass, and inconclusive ACCEPTS, exactly
like the existing QC).

## Settings are described as data (`settings_meta.py`, `GET /api/settings/schema`)

`DEFAULTS` has 111 keys; the old form hand-wrote about forty. The other seventy — every autonomy
key, every timeout, the notify channel, the whole disk and recycle policy — could only be changed
by editing `/config/config.json` on the host and restarting, which silently decided which parts of
this app an operator was allowed to run. Each key now carries a group, label, type, help text and
an `advanced`/`danger`/`secret` flag, and the page renders itself from that; adding a key to
DEFAULTS and describing it here is the whole change. `test_revamp.py` asserts the two stay in step
**in both directions** — an undescribed key and a described key that no longer exists both fail.
The schema takes its current values from the already-masked config, never the raw one.

## Autonomy & failure management

The design goal after the July 2026 autonomy review (`docs/AUTONOMY.md`): **a human never does
routine repair, and is told out-of-band when the machine has proven something impossible or the
machine itself is broken.** The escalation ladder, in order — each rung strictly cheaper than
the next:

1. **Self-retry** (`pipeline.transient`): failures a retry fixes — a grab fetch timeout, a qB
   blip, an unreadable-base probe — go back for another automatic attempt instead of minting an
   `error` (which pages the AI within 3 minutes, i.e. burns an agent run on a network hiccup).
   Bounded by `transient_max` CONSECUTIVE failures (`transient_fails` column, reset by
   `DONOR_RESET`); an unbroken run means the condition isn't transient and becomes a real error
   carrying the count.
2. **A different release**: failures whose automatic fix is *another donor* — donor vanished
   before the merge, donor unreadable, mux failure (mkvmerge can't parse the download),
   post-merge QC rejection, TV's "carries none of the missing languages" — all route through
   `reject_and_retry`/`_reject_and_retry_ep` (blocklist + re-search + the `max_sync_retries`
   budget) instead of parking as `error` and handing the AI a `/retry` that could have been a
   state transition.
3. **The pipeline's own last call** (`pipeline.wide_probe_rescue`): on the attempt that would
   SPEND the sync budget, detection re-runs once at ±`sync_probe_lag_s` (300s) — the
   deterministic first line of the AI runbook ("call /sync_probe FIRST"), executed by the
   pipeline. A parked record now really means "windows disagree even at ±300s".
4. **The agent** (per-record tickets, above): judgement calls — numbering mismatches the
   automatic translation can't cover, alternate names beyond the ladder, known rate stretches.
5. **`unfixable`/`needs_human` + the notify digest**: the terminal rung, delivered to a human
   out-of-band, with the machine's reasoning recorded.

**Crash recovery is complete.** `stage_finish` always re-queued stale `merging` and reconciled
`downloading`; `pipeline.sweep_stuck` (3-min sweep) now recovers `searching`/`grabbed` too —
they had NO reader at all, and a `grabbed` record has no `dl_hash` yet, so even the orphan
sweep couldn't see one. Recovery never blocklists (the release may never have been grabbed);
approval-mode `grabbed` is exempt (a human deciding is not a crash). Dead merge-worker threads
are respawned by the same sweep (`ensure_merge_workers` used to run only on settings changes).

**The output is verified before it becomes the library** (`pipeline.qc_grafted_audio`). A
confident-but-wrong sync was the one failure nothing could ever detect: the language reads as
present, the record closes, the donor is deleted — only a human watching the film would notice,
which is the definition of non-autonomous. After every audio graft (movie + TV), the grafted
track is cross-correlated against the base's own track *inside the mux output* (mkvmerge orders
tracks by input, so base audio = 0..n-1, first grafted = n; different languages correlate
through music/effects — the signal `resync_movie` always used). Rejection needs CONFIDENT
evidence (`qc_min_conf` + `qc_max_offset_ms`); inconclusive accepts, so quiet films don't burn
their budget on absent evidence. A wrong drift lands in the inconclusive bucket — narrower
coverage, stated honestly.

**What a replacement discards is reversible** (`pipeline.recycle`). `_place_multi` and the TV
direct remux are the only outcomes that throw the library file's content away; the original now
moves to `<media>/.vo-merge-recycle/<relpath>` (mtime restamped so the TTL counts from
recycling) and the daily housekeeping job purges past `recycle_keep_days`. Grafts keep the
cheap delete — their output carries every track the base had.

**Nothing loops forever.** The blocklist ages (`tried` entries are `[rid, ts]`,
`pipeline.tried_active` filters by `tried_ttl_days`; legacy strings stay blocked until a
rewrite stamps them, so an upgrade doesn't un-blocklist years of known-bad releases at once).
`search_rounds` counts fruitless rounds per record: round ≥ 1 widens the query ladder to the
*arrs' alternate titles, and round ≥ `no_release_escalate_rounds` pages the AI once (the
`ai_status IS NULL` gate makes it one-shot — the verdict is durable across cooldown cycles).

**Nothing fills the disk.** `check_disk` (watchdog) holds ALL merging below `disk_floor_gb`;
`disk_headroom_ok` refuses a specific pair whose output can't fit (inputs' sum + floor),
re-queues it un-penalised and cools the worker off 15 min (`DISK_STATE["hold_until"]`) — rc≥2
half-writes no longer compound the disk-full that causes them. Donors kept for parked failure
states (`review`/`sync_fail`/`error` — kept for `/assign`//`set_sync`) are freed after
`donor_keep_days`; a later merge finds them gone and self-heals through the donor-vanished
reject path.

**State survives corruption.** `core.verify_or_restore_db` (startup) runs `PRAGMA quick_check`;
a corrupt DB is quarantined (never deleted) and last night's `VACUUM INTO` snapshot restored
automatically — the backups finally have a reader. `config.json` gets the same nightly copy
(`core.backup_config`, skipped while the live file is unparseable so the good snapshot
survives), and an unparseable config — which stops the whole pipeline via the `enabled: False`
fallback — files a `config-broken` ticket and alarms, using the last config that PARSED for the
notify URL the broken file is hiding (`core._LAST_GOOD_CFG`).

### The alarm channel (`notify.py`)

Everything else in the app is pull-based, which is useless for the class of failure where the
pipeline (or its fixer) has stopped doing anything. `notify_url` (empty = off) takes a
Discord/Slack webhook (their JSON envelope) or anything ntfy-shaped (plain POST + `Title`
header). **Rate-limited per KIND, not per message** (`notify_repeat_h`, state persisted across
restarts), re-armed the moment a condition is observed healthy — a channel that repeats itself
every 3 minutes gets muted by its human, which is worse than no channel. The five kinds:
dispatcher dead, config broken, disk floor, `dep-*` (a dependency continuously unreachable past
`dep_down_alarm_min` — `pipeline.check_deps` remembers DURATION, which the per-cycle
"qB error, returning" logs never could; surfaced on `/api/status` as `deps_down`), and the
needs-human digest. Never per-merge chatter.

**Who watches the watcher:** all of the above lives inside vo-merge, so vo-merge's own death is
the one failure it can never report. The sidecar dispatcher closes that loop — it pings
`/api/health` each cycle (`VO_URL`) and raises the same out-of-band alarm (`NOTIFY_URL`) when
vo-merge stays unreachable past `VO_DOWN_ALARM_MIN`, so the two processes watch each other. A
whole-host outage still needs an external uptime ping; no in-host software can report the
host's own death.

Post-merge QC is **drift-aware**: `sync.audio_windows` hands `qc_grafted_audio` the raw
per-window residuals, and a line through them whose span exceeds `qc_max_offset_ms` across the
runtime rejects the merge — a wrong STRETCH ratio (windows disagreeing *linearly*) used to be
indistinguishable from noise and sailed through the consensus check. Scattered disagreement is
still inconclusive-accept; two points must span 2× the limit, since any two points fit a line.

## Config (`core.py:DEFAULTS`, persisted to `/config/config.json`)

Keys you'll touch most: `scan_mode` (**files**|tag), `scan_all_movies`, `lang_profiles`, `anime_dirs`, `want_subs`/`max_sub_tracks`/`subs_only_gap`, `*_url`/`*_key` for Prowlarr/Radarr/Sonarr/qB/Plex, `en_indexer_ids`,
`multi_indexer_ids`, `grab_mode` (auto|approval), `scope_films`/`scope_series`, `min_seeders`,
`score_threshold`, `max_sync_retries`, `sync_*` (windows/window_dur/hwaccel/threads,
`sync_ratio_test`/`sync_ratio_span`/`sync_ratio_min_conf`/`sync_ratio_margin` for the PAL path),
`stall_timeout_min`/`dl_max_age_min`, `search_interval_min`/`finish_interval_min`/
`promote_interval_min`, `max_inflight_downloads` (download slots) /`max_parallel_merges`
(concurrent merges, applied live via `MERGE_GATE`), `enabled` (master switch),
`ai_tickets`/`ai_stale_min`/`ai_max_tickets`/`ai_dispatcher_alarm_min` (AI-review escalation,
see above), `api_key` (empty = open on the LAN), `mux_timeout_min`/`sync_decode_timeout_min`
(deadlock guards, not tuning knobs), `db_backup_keep` (nightly `VACUUM INTO /config/backup/`,
now DB + config, with an auto-restore reader at startup), and `api_url`/`docs_path` (what the AI
ticket tells the dispatcher — these used to be one install's hard-coded LAN address and
`/mnt/nvme` path).

Autonomy keys (see "Autonomy & failure management"): `notify_url`/`notify_repeat_h` (the alarm
channel), `dep_down_alarm_min`, `disk_floor_gb`, `transient_max`, `sync_wide_probe`/
`sync_probe_lag_s`, `postmerge_qc`/`qc_min_conf`/`qc_max_offset_ms`, `recycle_keep_days`,
`tried_ttl_days`, `no_release_escalate_rounds`, `donor_keep_days`, `ignored_revisit_days`/
`ignored_revisit_per_day`, `auto_repair`. All are validated like every other key; the newer ones
are edited via `config.json` / `POST /api/settings` until the Settings page grows fields for
them. **The autonomous posture** is: `enabled: true`, `grab_mode: "auto"`, `ai_tickets: true`, a
running dispatcher (`deploy/dispatcher/dispatcher.sh` on the Unraid host — the CLI's
subscription auth, no API key), and a `notify_url` — `sync_review: true` is then safe, because
`review` parks records for an actor that actually exists.
Most of these are editable in the UI under **Settings → Queues & limits**.

**Settings are validated** (`main._validate_settings`): each value is coerced to the type of its
default and the scheduler-driving keys are clamped to a minimum. `save_config` filtered on key
NAME only, so `search_interval_min: 0` became a one-second full sweep (APScheduler coerces a zero
interval to 1 s) and a non-numeric value raised only *after* being persisted — killing every
subsequent startup inside `scheduler.start()` until `config.json` was hand-edited on the host.
Keys with a meaningful 0 or negative (`no_release_retry_h`) are deliberately not clamped.

**Config writes are atomic** (temp file + `os.replace`), and a `config.json` that exists but
won't parse is reported loudly and **refused as a save target**. `json.dump` straight onto the
file truncates first, so a crash mid-write left a partial file; `load_config` then silently fell
back to DEFAULTS and the next save — which starts from that same fallback — wrote the defaults
back over every URL and key the operator had entered.

Secrets are masked in the `GET /api/settings` response, driven from `core._SECRET_KEYS` rather
than a hand-written list — which is how `plex2_token` (a real Plex account token) came to be
returned in cleartext beside five masked siblings. `webhook_token` stays visible on purpose
(`main._VISIBLE_SECRETS`): the UI renders it as part of the webhook URL.

**Defaults are not install-specific.** `en_indexer_ids` is empty (Prowlarr's numeric IDs are
per-instance, and an empty list means "every configured indexer" — it used to ship one install's
IDs, so a fresh deployment queried indexers that didn't exist and recorded the empty result as
"no release exists"), and `series_pilot` is empty rather than three personal show names.

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

## Tests

`backend/tests/`, run with `python -m pytest` from `backend/`. There were none for a long time,
and every defect in the July 2026 review was the kind a unit test catches at the point of writing.

- **`test_media.py`** covers the gap decision itself — `norm_lang`, signs-vs-real subtitles,
  `sub_rank`, release-name parsing, the `orig` token, `wanted_audio`/`wanted_subs`. These are
  pure functions whose fixtures are strings, which is why the suite starts here: the whole
  "which languages is this file missing" decision is testable with no disk and no indexer.
- **`test_regressions.py`** pins each fixed defect, named for the failure it prevents. Add to it
  rather than starting a new file — a test that says *why* is the only durable form of these
  notes.
- **`test_revamp.py`** covers the pieces the UI revamp added. Three of them guard against ROT
  rather than a specific bug, which is why they are worth keeping even though nothing is broken
  today: the settings schema falling behind `DEFAULTS` (both directions), the error taxonomy
  falling behind the literal strings `pipeline.py`/`tv.py` write (every one is parametrised), and
  a chart series treating a missing sample as a zero. The lip-sync tests exercise the correlation
  core directly with synthetic signals — no ffmpeg, no media — including the sign convention and
  the "does not invent an offset from unrelated signals" case.

Write the test first when a bug is silent in production (a desynced file marked `merged`, a
secrets file served over HTTP). Those are exactly the ones nobody notices twice.

**Type-checking is part of the frontend build**: `npm run build` is `tsc --noEmit && vite build`,
and `strict` is on. esbuild strips types without checking them, so without the `tsc` step a type
error builds and ships perfectly happily.

## CI

- `.github/workflows/ci.yml` — pytest + `compileall` (backend) and the typed build (frontend), on
  every push **and pull request**, so a branch outside the publish filter is still checked.
  Nothing validated anything before: the only workflow built the image and pushed it.
- `.github/workflows/docker-publish.yml` builds and pushes to GHCR on `main`, on `v*` tags, and on
  `claude/**` branches, so a feature branch can be pulled onto Unraid before it merges. Only `main`
  publishes `:latest`; a branch build is tagged with its sanitised branch name
  (`ghcr.io/n0de0ne/vo-merge:claude-<branch>`).
- `.github/dependabot.yml` — monthly grouped bumps for pip, npm and actions. Pinning with nothing
  that ever moves the pins is how `requests` sat on CVE-2024-47081 for a year.

**The image name is pinned to a literal owner on purpose.** It used to be
`ghcr.io/${{ github.repository_owner }}/vo-merge`, which silently followed the `alanstrok` →
`n0de0ne` account rename: new builds went to the new path while the Unraid template kept pulling
the old one. **GHCR does not redirect a renamed owner the way git repos do** — the old path
answers `manifest unknown`, Unraid reports `TOTAL DATA PULLED: 0 B`, and the server keeps running
a stale image with no obvious error. If the account is renamed again, change the workflow and the
Unraid template together.
