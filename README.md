# HomeSlice

Self-hosted, mobile-friendly web front-end for [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer), plus the full OrcaSlicer desktop streamed into the browser. Both use the same profiles.

HomeSlice is an independent project, not affiliated with OrcaSlicer. It **does not fork OrcaSlicer**. It downloads the official Linux release from GitHub, checks it against the SHA-256 that GitHub publishes, and runs it as-is. Staying up to date means picking a channel (stable / pre-release / nightly) in **Settings**.

## How it fits together

| Path | What | Notes |
|---|---|---|
| `/` | Web app (React PWA) | Library, slicing with your presets, jobs, updates |
| `/gui/` | Full OrcaSlicer desktop (KasmVNC) | Requires a HomeSlice login (nginx `auth_request`) |
| `/api/*` | FastAPI backend | Runs the OrcaSlicer CLI in a sandboxed job queue |

Volumes: `/config` holds OrcaSlicer presets, the database, the secret key and the setup token. `/data` holds the model library, job output and installed OrcaSlicer builds.

## Install on TrueNAS SCALE

1. Push this repo to GitHub. The CI workflow builds `ghcr.io/<you>/homeslice`. Make the package public, or add GHCR credentials in TrueNAS.
2. Create datasets such as `tank/apps/homeslice/config` and `tank/apps/homeslice/data`.
3. Go to **Apps → Discover Apps → ⋮ → Install via YAML** and paste [deploy/truenas/docker-compose.yaml](deploy/truenas/docker-compose.yaml) after editing the image name and paths.
4. Open the app logs and copy the **setup token** line. Then browse to `http://<nas>:8080` and create your admin account.
5. Go to **Settings → Check for updates → Install** to download OrcaSlicer.
6. Open **Full Orca** once to import or create your printer, filament and process presets. They then appear on the **Slice** page.

### Remote access (phone away from home)

Use Tailscale or WireGuard; **don't** port-forward. With Tailscale on the NAS, run `tailscale serve --bg 8080`, then set `HOMESLICE_COOKIE_SECURE: "true"` and use the `https://<nas>.<tailnet>.ts.net` URL.

## Security model

- First-run setup needs a one-time token from the container log. There are no default credentials.
- Passwords are hashed with argon2id. Sessions are HttpOnly, `SameSite=Strict` cookies. Every state-changing request needs a CSRF token, and cross-origin requests are blocked.
- Failed logins lock out the IP and the username (app level) and are rate-limited (nginx). Logins, uploads, slices and updates go to an audit log.
- Uploads:
  - extension allowlist, plus checking the file's actual content
  - size cap, enforced while streaming
  - zip-slip, symlink and zip-bomb checks for 3MF files
  - files stored under random names
- Slicing:
  - OrcaSlicer runs as a non-root user with an argument list (no shell)
  - only allowlisted setting overrides
  - per-job temporary data dir (so no plugins load)
  - memory limit and timeout
- Updates are only installed when the download matches GitHub's published SHA-256. Older versions are kept for rollback.
- All outbound HTTP for untrusted URLs goes through `SafeFetcher`: https only, host allowlist, public IPs only, re-checked on every redirect, size cap.
- Only port 8080 (nginx) is published. The raw desktop stream (3000/3001) and the API (8000) aren't reachable from outside the container.

## Development

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"
.\.venv\Scripts\python -m pytest -q
$env:HOMESLICE_CONFIG_DIR="..\dev-data\config"; $env:HOMESLICE_DATA_DIR="..\dev-data\data"; $env:HOMESLICE_ORCA_DATADIR="$env:APPDATA\OrcaSlicer"; $env:HOMESLICE_COOKIE_SECURE="false"
.\.venv\Scripts\uvicorn homeslice.app:create_app --factory --port 8000
```

Frontend (needs Node 22): `cd frontend; npm install; npm run dev`, then open http://localhost:5173.

## Status / roadmap

- [x] Phase 1–2: container, version manager, auth, library, slicing queue, streamed GUI
- [x] Phase 3 (basic): mobile UI for library, slicing, jobs and settings
- [ ] Phase 3: 3D plate viewer and G-code preview
- [ ] Phase 4: printer connectors (Moonraker, OctoPrint, PrusaLink, Bambu LAN, Creality)
- [ ] Phase 5: model browser (Thingiverse, Printables, share-to-app import)
- [ ] Phase 0 checks to run on real hardware: headless CLI inside the container, preset flattening accepted by the CLI
