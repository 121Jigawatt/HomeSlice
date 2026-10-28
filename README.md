# HomeSlice

Self-hosted, mobile-friendly web front-end for [OrcaSlicer](https://github.com/OrcaSlicer/OrcaSlicer), plus the full OrcaSlicer desktop streamed into the browser. Both use the same profiles.

HomeSlice is an independent project, not affiliated with OrcaSlicer. It **does not fork OrcaSlicer**. It downloads the official Linux release from GitHub, checks it against the SHA-256 that GitHub publishes, and runs it as-is. Staying up to date means picking a channel (stable / pre-release / nightly) in **Settings**.

## How it fits together

| Path | What | Notes |
|---|---|---|
| `/` | Web app (React PWA) | Library, slicing with your presets, jobs, updates |
| `/gui/` | Full OrcaSlicer desktop (Selkies) | Requires a HomeSlice login (nginx `auth_request`) |
| `/api/*` | FastAPI backend | Runs the OrcaSlicer CLI in a sandboxed job queue |

Volumes: `/config` holds OrcaSlicer presets, the database, the secret key and the setup token. `/data` holds the model library, job output and installed OrcaSlicer builds.

## Install on TrueNAS SCALE

1. Push this repo to GitHub. The CI workflow builds `ghcr.io/<you>/homeslice`. Make the package public, or add GHCR credentials in TrueNAS.
2. Go to **Apps → Discover Apps → ⋮ → Install via YAML** and paste [deploy/truenas/docker-compose.yaml](deploy/truenas/docker-compose.yaml). Storage is created automatically in the apps pool's `ix-apps` dataset. To use your own datasets instead, replace the two named volumes with host paths.
3. Open the app logs and copy the **setup token** line. Then browse to `https://<nas>:8443`, accept the certificate warning, and create your admin account.
4. OrcaSlicer (stable channel) is downloaded and installed automatically on first start. Progress and the installed version are shown under **Settings**, where you can also switch channels or roll back.
5. Open **Settings → OrcaSlicer engine → Open full OrcaSlicer** once to import or create your printer, filament and process presets. They then appear on the **Slice** page. The Library page shows a checklist until this is all done.

### HTTPS certificate and installing as an app

On first start HomeSlice creates its own small certificate authority (`/config/ssl/homeslice-ca.pem`) and uses it to issue the HTTPS certificate. Put every address you open HomeSlice by into `HOMESLICE_TLS_HOSTS` (comma-separated IPs or hostnames, e.g. `10.69.69.12,homeslice.lan`); the certificate is re-issued automatically when that list changes and before it expires.

Until a device trusts that authority, the browser shows a warning and HomeSlice can't be installed as an app or open offline. The app shows a banner on such devices with a **Download certificate** button (`/homeslice-ca.crt`) and step-by-step instructions for iPhone/iPad, Android, Windows, macOS and Firefox. After that, use **Add to Home Screen** (iOS) or **Install app** (Chrome/Edge). The CA key never leaves `/config/ssl` and is readable only by root inside the container.

### Push notifications (not implemented: findings)

Web Push is possible but only under conditions that don't hold for most installs today:

- It needs a service worker, which browsers only allow on a **trusted** certificate, so every device must first install the HomeSlice CA (above).
- On iPhone/iPad it works only for a site **added to the Home Screen** (iOS 16.4+), and the user must allow notifications from inside that installed app.
- The NAS must reach the browser vendors' push services on the internet (Apple, Google, Mozilla), and HomeSlice would need VAPID keys plus a subscription store.

Until then, finished slices, failed slices and send results are shown as in-app notifications, and printer state changes are announced to screen readers. Notifications can be revisited once the trusted-certificate setup has been used for a while.

### Remote access (phone away from home)

Use Tailscale or WireGuard; **don't** port-forward. With Tailscale on the NAS, run `tailscale serve --bg https+insecure://localhost:8443` and use the `https://<nas>.<tailnet>.ts.net` URL.

### Users and roles

The setup account is an **Admin**. Under **Settings → Users**, admins add people with a temporary password (they choose their own at first sign-in) and give them one or more roles. Built-in roles:

| Role | Can |
| --- | --- |
| Admin | Everything, including users and roles. Can't be edited or deleted. |
| Operator | Everything except users, printer setup and OrcaSlicer updates. |
| Maker | Add models, slice, print, and manage their own models, jobs and queued prints. |
| Viewer | Browse the library and see printers. |

**Settings → Roles** lets admins edit these or create their own from 14 permissions. People without "See everyone's jobs" only see the jobs they started. The full OrcaSlicer desktop has its own permission, because it can edit profiles and read the app's files. HomeSlice refuses changes that would leave nobody able to manage users. Disabling a user or resetting their password signs them out everywhere.

### Single sign-on (authentik)

HomeSlice signs people in with OpenID Connect (authorization code flow with PKCE).

1. In authentik, create an **OAuth2/OpenID Provider**: client type Confidential, a signing key, and the redirect URI shown under **Settings → Single sign-on** (`https://<homeslice>/api/auth/oidc/callback`). Create an **Application** for it and bind the users or groups who may sign in.
2. In HomeSlice, enter the provider's **OpenID Configuration Issuer**, client ID and client secret, then turn it on. The sign-in page then shows **Sign in with authentik**; password sign-in keeps working.
3. New people get an account on first sign-in (optional), with roles from authentik groups you map to HomeSlice roles, or a default role. With group mappings set, roles follow authentik at every sign-in.

Existing HomeSlice accounts are only linked to an authentik identity of the same username if you allow it under **Advanced**. The client secret is encrypted at rest.

## Security model

- First-run setup needs a one-time token from the container log. There are no default credentials.
- Passwords are hashed with argon2id. Sessions are HttpOnly, `SameSite=Strict` cookies. Every state-changing request needs a CSRF token, and cross-origin requests are blocked.
- Every API route checks the user's permissions on the server; the UI only hides what a user can't use.
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
- Only port 8443 (nginx, HTTPS) is published. The raw desktop stream (3000/3001) and the API (8000) aren't reachable from outside the container.

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
- [x] Phase 3: mobile, tablet and desktop UI for library, slicing, jobs and settings; 3D plate viewer and G-code preview
- [x] Phase 4 (part): Klipper/Moonraker printers (Mainsail, Fluidd): live status, webcams, send/auto-start, per-printer queue with a "bed is clear" safety check
- [ ] Phase 4: other printer connectors (OctoPrint, PrusaLink, Bambu LAN, Creality)
- [x] Phase 5 (part): Printables / MakerWorld link import
- [x] Multiple user accounts with roles (RBAC)
- [ ] Push notifications (see findings above)
- [ ] Phase 0 checks to run on real hardware: headless CLI inside the container, preset flattening accepted by the CLI
