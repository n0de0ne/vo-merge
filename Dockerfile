# ---- stage 1: build the React SPA ----
FROM node:22-slim AS web
WORKDIR /web
# The lockfile is REQUIRED (no glob): with `frontend/package-lock.json*` the build succeeded
# without it and silently resolved fresh ^ ranges, so two builds of the same commit could ship
# different dependency trees. `npm ci` then installs exactly what the lockfile pins.
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build           # tsc --noEmit && vite build -> /web/dist

# ---- stage 2: python runtime + merge tooling ----
FROM python:3.12-slim
# enable non-free (Intel iHD media driver lives there) for both bookworm & trixie layouts
RUN (sed -i 's/ main$/ main contrib non-free non-free-firmware/' /etc/apt/sources.list 2>/dev/null || true) \
 && (sed -i 's/^Components: main.*/Components: main contrib non-free non-free-firmware/' /etc/apt/sources.list.d/debian.sources 2>/dev/null || true) \
 && apt-get update && apt-get install -y --no-install-recommends \
        mkvtoolnix ffmpeg \
        intel-media-va-driver-non-free libva2 libva-drm2 vainfo \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/dist ./static
ENV VO_CONFIG=/config VO_STATIC=/app/static
VOLUME ["/config", "/media", "/downloads"]
EXPOSE 8080

# Runs as root deliberately. This container rewrites files in the operator's media library across
# Unraid bind mounts (/mnt/user/Plex), whose ownership varies per install; a hardcoded USER would
# break writes on some setups, and the PUID/PGID entrypoint that would fix that properly is a
# bigger change than it is worth for a single-user LAN tool. Revisit if this is ever exposed.

# Without this, Unraid reports the container healthy while uvicorn is wedged — which is a real
# state (a hung decode used to park the merge worker forever). /api/health is deliberately
# trivial: no DB, no config, so it measures whether the server is answering, not whether SQLite
# is busy, and it is exempt from api_key so no credential is needed here.
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s --retries=3 \
  CMD python3 -c "import urllib.request as u; u.urlopen('http://127.0.0.1:8080/api/health', timeout=4)" || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
