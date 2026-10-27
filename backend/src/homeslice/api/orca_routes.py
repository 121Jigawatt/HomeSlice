import asyncio
import json
import logging

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..auth import Session
from ..orca.updater import CHANNELS, UpdateError
from ..orca.versions import VersionError
from .deps import client_ip, require_session

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/orca", tags=["orca"])


class ChannelBody(BaseModel):
    channel: str | None = None


class SettingsBody(BaseModel):
    channel: str
    auto_update: bool = False


class ActivateBody(BaseModel):
    version_id: str


def get_channel(services) -> str:
    channel = services.db.get_setting("orca.channel", "stable")
    return channel if channel in CHANNELS else "stable"


@router.get("")
def orca_status(request: Request, _: Session = Depends(require_session)) -> dict:
    s = request.app.state.services
    latest = s.db.get_setting("orca.latest")
    return {
        "current": s.versions.current(),
        "previous": s.versions.previous(),
        "installed": s.versions.installed(),
        "channel": get_channel(s),
        "channels": list(CHANNELS),
        "auto_update": s.db.get_setting("orca.auto_update", "0") == "1",
        "latest": json.loads(latest) if latest else None,
        "progress": s.updater.progress,
        "last_error": s.db.get_setting("orca.last_error"),
    }


@router.put("/settings", status_code=204)
def update_settings(body: SettingsBody, request: Request, session: Session = Depends(require_session)) -> None:
    if body.channel not in CHANNELS:
        raise HTTPException(400, "Unknown channel")
    s = request.app.state.services
    s.db.set_setting("orca.channel", body.channel)
    s.db.set_setting("orca.auto_update", "1" if body.auto_update else "0")
    s.auth.audit(session.username, "orca_settings", f"{body.channel} auto={body.auto_update}", client_ip(request))


@router.post("/check")
async def check(body: ChannelBody, request: Request, _: Session = Depends(require_session)) -> dict:
    s = request.app.state.services
    channel = body.channel or get_channel(s)
    try:
        info = (await s.updater.latest(channel)).to_dict()
    except UpdateError as e:
        raise HTTPException(502, str(e)) from None
    s.db.set_setting("orca.latest", json.dumps({**info, "channel": channel}))
    return info


async def run_install(services, channel: str) -> None:
    try:
        release = await services.updater.latest(channel)
        await services.updater.install(release)
        services.profiles.set_extra_system_dirs(services.versions.system_profile_dirs())
        services.db.set_setting("orca.last_error", "")
    except UpdateError as e:
        log.warning("OrcaSlicer install failed: %s", e)
        services.db.set_setting("orca.last_error", str(e))


@router.post("/install", status_code=202)
async def install(body: ChannelBody, request: Request, session: Session = Depends(require_session)) -> dict:
    s = request.app.state.services
    channel = body.channel or get_channel(s)
    if channel not in CHANNELS:
        raise HTTPException(400, "Unknown channel")
    if s.updater.progress.get("state") != "idle":
        raise HTTPException(409, "An update is already in progress")
    s.auth.audit(session.username, "orca_install", channel, client_ip(request))
    s.updater.progress = {"state": "starting"}
    task = asyncio.create_task(run_install(s, channel))
    s.background_tasks.add(task)
    task.add_done_callback(s.background_tasks.discard)
    return {"state": "started"}


@router.post("/activate", status_code=204)
def activate(body: ActivateBody, request: Request, session: Session = Depends(require_session)) -> None:
    s = request.app.state.services
    try:
        s.versions.activate(body.version_id)
    except VersionError as e:
        raise HTTPException(400, str(e)) from None
    s.profiles.set_extra_system_dirs(s.versions.system_profile_dirs())
    s.auth.audit(session.username, "orca_activate", body.version_id, client_ip(request))


@router.post("/rollback")
def rollback(request: Request, session: Session = Depends(require_session)) -> dict:
    s = request.app.state.services
    try:
        version = s.versions.rollback()
    except VersionError as e:
        raise HTTPException(400, str(e)) from None
    s.profiles.set_extra_system_dirs(s.versions.system_profile_dirs())
    s.auth.audit(session.username, "orca_rollback", version, client_ip(request))
    return {"current": version}
