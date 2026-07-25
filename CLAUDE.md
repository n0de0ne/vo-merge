# CLAUDE.md — vo-merge

Guidance for Claude (and humans) working on this repo.

## What this is

**vo-merge** is a self-hosted app that makes movie/TV files multi-language. The library
(managed by Radarr/Sonarr, served by Plex) holds many titles that exist only as a **French
dub**. vo-merge finds an **English** (or, as a fallback, the **original-language / VO**)
release on Prowlarr, downloads it via qBittorrent, and **grafts the missing audio track into
the existing library file** — keeping the existing (usually higher-res) video, A/V-synced —
then replaces the library file in place so Plex/Radarr paths stay valid.

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

1. **scan** — pull Radarr movies tagged `vo-gap` (missing English); skip French-origin if
   `exclude_french_origin`. (The `vo-gap` tag is maintained by an external user script — see below.)
2. **search/score** — Prowlarr search; `score_release` rejects French-dub-only, **strongly
   prefers MULTI** (+200), boosts seeders/quality/id-match. `candidates()` powers both the
   auto-picker and the UI's interactive search.
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

## Merge rules (important, non-obvious)

- **Keep the better video, graft the other's audio.** `_video_quality` = (height, bitrate).
  A lower-res MULTI must NOT replace a higher-res library file — it only donates audio.
  `_place_multi` (use the download directly) fires ONLY when the release's video ≥ the library
  file's; otherwise fall through to the graft path.
- **English preferred; VO fallback.** Target track is English; if a title's original language
  isn't English and no English exists, graft the **original-language (VO)** track instead
  (e.g. Norwegian for "Kraken"). `_orig_codes(name)` maps Radarr/Sonarr `originalLanguage`
  → ffprobe audio codes (excludes eng/fre, which are handled directly).
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
- **TV: files parsed but none map to a gap episode** — nearly always a numbering mismatch
  (absolute vs season, e.g. a "Complete Collection S01–S04" pack). Sets the episodes `error`
  with the parsed keys in the message, so it surfaces in Review/AI instead of squatting a slot.

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
| `POST /episode/{id}/assign {path}` | **Absolute-vs-aired-season packs.** Maps ONE donor file to one episode and queues the merge. The agent reads `/context`, works out the mapping, calls this per episode. Previously impossible — this whole failure class was unfixable via the API. |
| `POST /movie\|episode/{id}/set_sync {offset_ms, drift}` | Applies a **known** offset and/or rate stretch with no detection (`drift` = donor_fps/base_fps; 1.0427083 = film→PAL). `_merge_*_impl` honours a stored `sync_drift` when the offset is manual. |
| `POST /search_releases {query}` | The `no_release` backlog. The built-in search composes its own query from the library title, so a title it never matches can never be found however often it re-searches. This runs an arbitrary Prowlarr query (original/romaji/alternate title, no year) and returns links to `/grab`. |
| `POST /movie\|episode/{id}/unfixable {reason}` | Terminal give-up **with a recorded reason** (sets `ignored` + `ai_status=needs_human`), so it doesn't read as an unexamined skip. |

## Config (`core.py:DEFAULTS`, persisted to `/config/config.json`)

Keys you'll touch most: `*_url`/`*_key` for Prowlarr/Radarr/Sonarr/qB/Plex, `en_indexer_ids`,
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
