# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/linuxserver/baseimage-kasmvnc:ubuntunoble

ENV TITLE="OrcaSlicer" \
    SUBFOLDER="/gui/" \
    NO_DECOR="true" \
    ORCASERVER_CONFIG_DIR=/config/orcaserver \
    ORCASERVER_DATA_DIR=/data \
    ORCASERVER_ORCA_DATADIR=/config/.config/OrcaSlicer \
    ORCASERVER_FRONTEND_DIST=/app/frontend \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime libraries needed by the OrcaSlicer AppImage (GUI and CLI).
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates locales fonts-dejavu-core \
        libgtk-3-0t64 libwebkit2gtk-4.1-0 libgl1 libegl1 libglu1-mesa libosmesa6 \
        libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        libsecret-1-0 libnotify4 \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/src /app/backend/src
RUN python3 -m venv /app/venv && /app/venv/bin/pip install --no-cache-dir /app/backend

COPY --from=frontend /src/dist /app/frontend
COPY docker/root/ /
RUN chmod 0755 /app/bin/* /etc/s6-overlay/s6-rc.d/*/run

EXPOSE 8080
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
