# VO Merger

A self-hosted companion to Radarr/Sonarr that builds **multi-language movie files** automatically.

It finds movies you only have in French (via a Radarr tag), searches your *English* indexers
through Prowlarr for a matching release, downloads it via qBittorrent, then **losslessly muxes**
your French/original-language audio onto the English release — giving you one file with all
languages, in sync. Managed from a web UI.

## How it works

```
Radarr (vo-gap tag, non-French-origin)        ← detect French-only movies
        │
        ▼
Prowlarr search on English indexers           ← match by tmdb/imdb id, score by
        │                                         seeders + resolution/source match
        ▼
qBittorrent grab (audio-merge category)        ← auto-grab best candidate (score≥threshold)
        │
        ▼
mkvmerge: English base + FR/VO audio tracks    ← sync-gated (duration + framerate);
        │                                         failures parked for a manual ms offset
        ▼
swap into library + Radarr rescan              ← hardlink-aware; English torrent keeps seeding
```

The container bundles `mkvmerge` + `ffprobe` — no external transcoder dependency.

## Architecture

- **Backend** — FastAPI + SQLite (`backend/app/`): `core` (config/state/state-machine),
  `clients` (Prowlarr/Radarr/qBittorrent/Plex), `pipeline` (scan→search→grab→merge→finish),
  `scheduler` (APScheduler two-stage loop), `main` (REST API + serves the SPA).
- **Frontend** — React + Vite + TypeScript (`frontend/`): Dashboard, Sync-failures queue,
  Settings (with connection tests), Logs.

## Run

```bash
docker build -t vo-merge .
docker run -d --name vo-merge \
  -p 8080:8080 \
  -v /path/to/appdata/vo-merge:/config \
  -v /mnt/user/Plex:/media \
  -v /mnt/user/Telechargements:/downloads \
  ghcr.io/alanstrok/vo-merge:latest
```

Then open `http://<host>:8080`, fill in Settings (Prowlarr/Radarr/qBittorrent/Plex), and
enable the pipeline. Start with **Films only** and confirm one title end-to-end before
turning on the batch.

### Mounts

| Container | Host | Purpose |
|---|---|---|
| `/config` | `appdata/vo-merge` | config.json + SQLite state + log |
| `/media` | `/mnt/user/Plex` | library (read/write merged output) |
| `/downloads` | `/mnt/user/Telechargements` | qBittorrent download dir (read) |

## CI

`.github/workflows/docker-publish.yml` builds and pushes to
`ghcr.io/<owner>/vo-merge` on pushes to `main` and on `v*` tags.

## License

MIT
