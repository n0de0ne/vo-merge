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
4. **finish** (`stage_finish`) — poll qB; on complete, set `en_file` and `merge_movie`;
   reconcile vanished torrents; **drop stalled torrents** (see below).
5. **merge** (`merge_movie`) — see merge rules below.
6. **finish_movie** — replace library file in place, trigger Radarr rescan + Plex scan.

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

## Stalled-download handling

`_is_stalled(t, cfg)`: incomplete + active > `stall_timeout_min` + `dlspeed==0` + (no swarm
seeds or qB state in stalledDL/error/missingFiles/metaDL). When stalled: delete from qB,
**blocklist that release** (add `dl_id` to `tried`), re-search for another (better-seeded)
release; give up to `no_release` after `max_sync_retries`. Movies: `drop_stalled`; TV
season-packs: `_drop_stalled_eps`.

## Config (`core.py:DEFAULTS`, persisted to `/config/config.json`)

Keys you'll touch most: `*_url`/`*_key` for Prowlarr/Radarr/Sonarr/qB/Plex, `en_indexer_ids`,
`multi_indexer_ids`, `grab_mode` (auto|approval), `scope_films`/`scope_series`, `min_seeders`,
`score_threshold`, `max_sync_retries`, `sync_*` (windows/window_dur/hwaccel/threads),
`stall_timeout_min`, `search_interval_min`/`finish_interval_min`, `enabled` (master switch).
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
