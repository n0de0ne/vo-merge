# ---- stage 1: build the React SPA ----
FROM node:22-slim AS web
WORKDIR /web
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ ./
RUN npm run build           # -> /web/dist

# ---- stage 2: python runtime + merge tooling ----
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
        mkvtoolnix ffmpeg \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/app ./app
COPY --from=web /web/dist ./static
ENV VO_CONFIG=/config VO_STATIC=/app/static
VOLUME ["/config", "/media", "/downloads"]
EXPOSE 8080
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
