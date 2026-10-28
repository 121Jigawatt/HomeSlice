import asyncio
import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .api import auth_routes, import_routes, library_routes, orca_routes, printer_routes, slicing_routes
from .auth import AuthService
from .config import Settings
from .crypto import SecretBox
from .db import Database
from .importer.sources import Importer
from .library.service import Library
from .orca.jobs import JobManager
from .orca.profiles import ProfileStore
from .orca.updater import UpdateError, Updater
from .orca.versions import VersionStore
from .printers.service import Printers

log = logging.getLogger("homeslice")

# Until OrcaSlicer is installed, keep retrying the first install this often.
FIRST_INSTALL_RETRY_S = 300

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data: blob: https:; connect-src 'self'; worker-src 'self' blob:; "
    "frame-src 'self'; frame-ancestors 'self'; object-src 'none'; base-uri 'self'; form-action 'self'"
)
SECURITY_HEADERS = [
    (b"content-security-policy", CSP.encode()),
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"SAMEORIGIN"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=(), payment=()"),
    (b"cross-origin-opener-policy", b"same-origin"),
]


class SecurityHeadersMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        is_api = scope["path"].startswith("/api/")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.extend(SECURITY_HEADERS)
                if is_api:
                    headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_wrapper)


@dataclass
class Services:
    settings: Settings
    db: Database
    secrets: SecretBox
    auth: AuthService
    library: Library
    profiles: ProfileStore
    versions: VersionStore
    updater: Updater
    jobs: JobManager
    printers: Printers
    importer: Importer
    background_tasks: set[asyncio.Task] = field(default_factory=set)


def build_services(settings: Settings) -> Services:
    settings.ensure_dirs()
    db = Database(settings.db_path)
    versions = VersionStore(settings.orca_dir)
    library = Library(db, settings)
    profiles = ProfileStore(settings.orca_datadir, versions.system_profile_dirs())
    secrets = SecretBox(settings.secret_key_file or settings.config_dir / "secret.key")
    return Services(
        settings=settings,
        db=db,
        secrets=secrets,
        auth=AuthService(db, settings),
        library=library,
        profiles=profiles,
        versions=versions,
        updater=Updater(settings, versions),
        jobs=JobManager(db, settings, library, profiles, versions),
        printers=Printers(db, secrets),
        importer=Importer(library, settings.tmp_dir, settings.max_upload_mb * 1024**2),
    )


async def check_for_updates(services: Services) -> float:
    """One update check. Installs OrcaSlicer on first run (or when auto-update is on); returns seconds to wait."""
    from .api.orca_routes import get_channel, run_install

    interval = services.settings.update_check_interval_h * 3600
    try:
        channel = get_channel(services)
        info = await services.updater.latest(channel)
        services.db.set_setting("orca.latest", json.dumps({**info.to_dict(), "channel": channel}))
    except UpdateError as e:
        log.info("Update check failed: %s", e)
        return FIRST_INSTALL_RETRY_S if services.versions.current() is None else interval
    first_run = services.versions.current() is None
    auto = services.db.get_setting("orca.auto_update", "0") == "1"
    idle = services.updater.progress.get("state") == "idle"
    if (first_run or auto) and idle and info.version_id != services.versions.current():
        if first_run:
            log.info("OrcaSlicer is not installed yet; installing %s from the %s channel", info.tag, channel)
        await run_install(services, channel)
    return FIRST_INSTALL_RETRY_S if services.versions.current() is None else interval


async def update_checker(services: Services) -> None:
    while True:
        await asyncio.sleep(await check_for_updates(services))


def _mount_frontend(app: FastAPI, dist: Path) -> None:
    dist = dist.resolve()
    index = dist / "index.html"

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> FileResponse:
        if path.startswith("api/"):
            raise HTTPException(404, "Not found")
        candidate = (dist / path).resolve()
        if path and candidate.is_file() and candidate.is_relative_to(dist):
            return FileResponse(candidate)
        return FileResponse(index, headers={"cache-control": "no-cache"})


def create_app(settings: Settings | None = None, *, start_background: bool = True) -> FastAPI:
    settings = settings or Settings()
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # Printer polling would otherwise log every request.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        services = build_services(settings)
        app.state.services = services
        services.auth.ensure_setup_token()
        services.jobs.start()
        if n := services.library.backfill_thumbnails():
            log.info("Generating %d missing model thumbnails", n)
        if start_background:
            task = asyncio.create_task(update_checker(services))
            services.background_tasks.add(task)
        try:
            yield
        finally:
            for task in list(services.background_tasks):
                task.cancel()
            await services.jobs.stop()
            services.db.close()

    docs = settings.enable_api_docs
    app = FastAPI(
        title="HomeSlice",
        lifespan=lifespan,
        docs_url="/api/docs" if docs else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if docs else None,
    )
    app.add_middleware(SecurityHeadersMiddleware)

    @app.get("/api/health", include_in_schema=False)
    def health() -> dict:
        return {"status": "ok"}

    for router in (
        auth_routes.router,
        library_routes.router,
        import_routes.router,
        slicing_routes.router,
        printer_routes.router,
        orca_routes.router,
    ):
        app.include_router(router)

    if settings.frontend_dist and (settings.frontend_dist / "index.html").is_file():
        _mount_frontend(app, settings.frontend_dist)
    return app
