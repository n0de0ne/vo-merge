# VO Merger

A self-hosted **language companion to Radarr and Sonarr**. They decide *what* your library holds;
vo-merge decides *which languages each file carries*.

You declare a target per kind of title — films FR+EN, anime FR+EN+original — and it probes every
file to see what is actually there, finds a release carrying what's missing, downloads it, and
**grafts the missing audio and subtitle tracks into your existing library file**, keeping the
video you already have and correcting any A/V offset. The file is replaced in place, so Plex and
Radarr paths stay valid.

The original case was a French-only library needing English, and that is still the common one,
but nothing in the gap decision, the scoring or the merge is specific to those two languages.

## How it works

```
probe every library file (mkvmerge -J)     ← the gap comes from the FILE, never from *arr
        │                                     metadata, which is an import-time snapshot
        ▼                                     that fails silently
target minus present  →  need_audio/need_subs
        │
        ▼
Prowlarr search, scored                    ← "does this release carry something this file
        │                                     is missing?" MULTI strongly preferred
        ▼
qBittorrent grab (audio-merge category)
        │
        ▼
sync detection: scene-cut correlation      ← constant offset, linear drift, or a PAL rate
        │                                     ratio; low confidence → review, never a guess
        ▼
mkvmerge: your video + the donor's tracks  ← --sync applied to audio AND subtitles
        │
        ▼
replace in place + Radarr/Sonarr rescan + Plex analyze
```

The container bundles `mkvmerge`, `ffmpeg` and the Intel iHD VAAPI driver — decode for sync
detection is offloaded to the iGPU.

## What makes a file "complete"

`lang_profiles` is the target end state per kind of title, and the gap is simply *target minus
present*, for both audio and subtitles:

| kind | audio | subs |
|---|---|---|
| `movie` | fre, eng | fre, eng |
| `series` | fre, eng | fre, eng |
| `anime` | fre, eng, **orig** | fre, eng |

`orig` resolves per title from the *arr's `originalLanguage`, so a Japanese anime targets `jpn`
and a French one (Arcane) doesn't — a literal `jpn` would be a gap no release could ever fill.

Two things that look like details and are not: a track tagged `und` counts as no language at all,
and an "English Signs" subtitle is **not** an English subtitle — it translates on-screen text, not
dialogue, so it never satisfies the target.

## Architecture

- **Backend** — FastAPI + SQLite (`backend/app/`): `core` (config, state, the DB), `media` (file
  truth: what languages a file actually carries), `clients` (Prowlarr/Radarr/Sonarr/qB/Plex),
  `pipeline` (movies) and `tv` (episodes), `sync`/`offdet*` (A/V offset detection),
  `scheduler` (APScheduler + the merge worker), `main` (REST API, serves the SPA).
- **Frontend** — React + Vite + TypeScript (`frontend/`): Overview, Films, Anime, TV, Library
  coverage, Review queue, Settings, Logs.
- **Tests** — `backend/tests/`, run with `python -m pytest` from `backend/`.

## Run

```bash
docker run -d --name vo-merge \
  -p 8090:8080 \
  -v /path/to/appdata/vo-merge:/config \
  -v /mnt/user/Plex:/media \
  --device /dev/dri:/dev/dri \
  ghcr.io/n0de0ne/vo-merge:latest
```

Then open `http://<host>:8090`, fill in Settings (Prowlarr/Radarr/Sonarr/qBittorrent/Plex), run a
library re-read from the Library tab, and enable the pipeline. Start with **Films only** and
confirm one title end to end before turning on the rest.

### Mounts

| Container | Host | Purpose |
|---|---|---|
| `/config` | `appdata/vo-merge` | config.json, SQLite state, log, nightly DB backups |
| `/media` | `/mnt/user/Plex` | the library, and the donor downloads beneath it |

The container and qBittorrent **must see the donor files at the same path**
(`downloads_mount` here == `qb_download_dir` there), or a finished download can't be found.

### Optional API key

`api_key` in Settings is empty by default, which keeps the API open on your LAN. Set it and every
request needs `X-API-Key` (the UI will prompt once and remember). `/api/hook/*` stays exempt so
Radarr and Sonarr webhooks keep working — those authenticate with `webhook_token` instead — and
`/api/health` is exempt so the container healthcheck needs no credential.

Cross-origin state-changing requests are refused regardless of whether a key is set.

## CI

- `.github/workflows/ci.yml` — pytest + `compileall` for the backend, `tsc --noEmit` + build for
  the frontend. Runs on every push and pull request.
- `.github/workflows/docker-publish.yml` — builds and pushes to `ghcr.io/n0de0ne/vo-merge` on
  `main`, on `v*` tags, and on `claude/**` branches so a branch can be pulled onto the server
  before it merges. Only `main` publishes `:latest`.

The image name is pinned to a literal owner **on purpose**: GHCR does not redirect a renamed
owner the way git repos do, so a rename silently sends builds to a new path while the old one
answers `manifest unknown`. If the account is renamed, change the workflow and the Unraid
template together.

## License

MIT
