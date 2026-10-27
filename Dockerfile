# syntax=docker/dockerfile:1.7

FROM node:22-bookworm-slim AS frontend
WORKDIR /src
COPY frontend/package*.json ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY frontend/ ./
RUN npm run build

FROM ghcr.io/linuxserver/baseimage-selkies:debiantrixie

# CUSTOM_WS_PORT: Selkies defaults to 8080, which the HomeSlice proxy owns.
ENV TITLE="OrcaSlicer" \
    SUBFOLDER="/gui/" \
    NO_DECOR="true" \
    START_DOCKER="false" \
    CUSTOM_WS_PORT="8082" \
    HOMESLICE_CONFIG_DIR=/config/homeslice \
    HOMESLICE_DATA_DIR=/data \
    HOMESLICE_ORCA_DATADIR=/config/.config/OrcaSlicer \
    HOMESLICE_FRONTEND_DIST=/app/frontend \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Runtime libraries needed by the OrcaSlicer AppImage (GUI and CLI).
# Firefox: Orca's cloud/Google sign-in opens a browser and redirects back to localhost.
RUN apt-get update \
    && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
        python3 python3-venv ca-certificates locales fonts-dejavu-core \
        libgtk-3-0t64 libwebkit2gtk-4.1-0 libgl1 libegl1 libglu1-mesa libosmesa6 \
        libgstreamer1.0-0 libgstreamer-plugins-base1.0-0 gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
        libsecret-1-0 libnotify4 \
        firefox-esr xdg-utils \
    && apt-get purge -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin \
    && rm -f /etc/ssl/private/ssl-cert-snakeoil.key \
    && rm -rf /var/lib/apt/lists/*

COPY backend/pyproject.toml /app/backend/pyproject.toml
COPY backend/src /app/backend/src
RUN python3 -m venv /app/venv && /app/venv/bin/pip install --no-cache-dir /app/backend

COPY --from=frontend /src/dist /app/frontend
COPY docker/root/ /
RUN chmod 0755 /app/bin/* /etc/s6-overlay/s6-rc.d/*/run

EXPOSE 8080 8443
VOLUME ["/config", "/data"]
HEALTHCHECK --interval=30s --timeout=5s --start-period=90s CMD curl -fsS http://127.0.0.1:8080/api/health || exit 1
