import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import Session
from ..library.validation import ValidationError
from ..orca.profiles import ProfileError
from ..printers.moonraker import PrinterBusy, PrinterError, PrinterMismatch
from ..printers.queue import QueueError
from .deps import client_ip, require_session

router = APIRouter(prefix="/api", tags=["printers"])

_UNSAFE_NAME = re.compile(r"[^A-Za-z0-9_.-]+")


class PrinterBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    url: str = Field(min_length=1, max_length=200)
    api_key: str | None = Field(default=None, max_length=200)


class SendBody(BaseModel):
    printer_id: str = Field(min_length=1, max_length=64)
    file: str = Field(min_length=1, max_length=128)
    start: bool = False


class PrinterSettingsBody(BaseModel):
    scan_subnet: str | None = Field(default=None, max_length=50)


def remote_name(services, job: dict, file: str) -> str:
    stem = "print"
    model_ids = job["params"].get("model_ids") or []
    if model_ids:
        try:
            record = services.library.get(model_ids[0])
        except ValidationError:
            record = None
        if record:
            stem = PurePosixPath(record["filename"]).stem
    stem = _UNSAFE_NAME.sub("_", stem).strip("._-")[:60] or "print"
    plate = PurePosixPath(file).stem
    return f"{stem}-{plate}-{job['id'][:8]}.gcode"


@router.get("/printers")
def list_printers(request: Request, _: Session = Depends(require_session)) -> list[dict]:
    return request.app.state.services.printers.list()


@router.get("/printers/live")
async def printers_live(request: Request, _: Session = Depends(require_session)) -> list[dict]:
    return await request.app.state.services.printers.live()


@router.get("/printers/{printer_id}/webcam/{index}")
async def printer_webcam(
    printer_id: str,
    index: int,
    request: Request,
    snapshot: bool = False,
    _: Session = Depends(require_session),
) -> StreamingResponse:
    """Relays the printer's own webcam so it can be shown on the HTTPS page."""
    printers = request.app.state.services.printers
    try:
        if printers.get(printer_id) is None:
            raise HTTPException(404, "Not found")
        client, url = await printers.webcam_source(printer_id, index, snapshot)
        http, upstream = await client.open_stream(url)
    except PrinterError as e:
        raise HTTPException(502, str(e)) from None

    async def body():
        try:
            async for chunk in upstream.aiter_bytes():
                if await request.is_disconnected():
                    break
                yield chunk
        finally:
            await upstream.aclose()
            await http.aclose()

    return StreamingResponse(
        body(),
        media_type=upstream.headers.get("content-type"),
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/printers/settings")
def get_printer_settings(request: Request, _: Session = Depends(require_session)) -> dict:
    return {"scan_subnet": request.app.state.services.printers.scan_subnet()}


@router.put("/printers/settings")
def put_printer_settings(
    body: PrinterSettingsBody, request: Request, session: Session = Depends(require_session)
) -> dict:
    services = request.app.state.services
    try:
        subnet = services.printers.set_scan_subnet(body.scan_subnet)
    except PrinterError as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "printer_settings", f"scan_subnet={subnet}", client_ip(request))
    return {"scan_subnet": subnet}


@router.post("/printers", status_code=201)
def add_printer(body: PrinterBody, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    try:
        printer = services.printers.add(body.name, body.url, body.api_key)
    except PrinterError as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "printer_add", f"{printer['name']} {printer['url']}", client_ip(request))
    return printer


@router.delete("/printers/{printer_id}", status_code=204)
def delete_printer(printer_id: str, request: Request, session: Session = Depends(require_session)) -> None:
    services = request.app.state.services
    try:
        deleted = services.printers.delete(printer_id)
    except PrinterError:
        deleted = False
    if not deleted:
        raise HTTPException(404, "Not found")
    services.auth.audit(session.username, "printer_delete", printer_id, client_ip(request))


@router.get("/printers/{printer_id}/status")
async def printer_status(printer_id: str, request: Request, _: Session = Depends(require_session)) -> dict:
    printers = request.app.state.services.printers
    try:
        if printers.get(printer_id) is None:
            raise HTTPException(404, "Not found")
        client = await printers.run(printer_id, printers.connected_client)
        status = await client.status()
    except PrinterError as e:
        raise HTTPException(502, str(e)) from None
    return {**status, "address": urlsplit(client.base_url).hostname}


@router.post("/jobs/{job_id}/send")
async def send_job(job_id: str, body: SendBody, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    try:
        job_id = uuid.UUID(job_id).hex
    except ValueError:
        raise HTTPException(404, "Not found") from None
    job = services.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "Not found")
    if job["status"] != "done":
        raise HTTPException(409, "Only finished jobs can be sent")
    path = services.jobs.output_path(job_id, body.file) if body.file.endswith(".gcode") else None
    if path is None:
        raise HTTPException(404, "G-code file not found")
    try:
        printer = services.printers.get(body.printer_id)
        if printer is None:
            raise HTTPException(404, "Unknown printer")
        services.printers.check_gcode_fits(printer, job["params"].get("printer"))
        if body.start and (reason := services.printers.needs_attention(body.printer_id)):
            raise HTTPException(409, f"{printer['name']} needs attention: {reason} Confirm the bed is clear first.")
        remote = remote_name(services, job, body.file)
        result = await services.printers.run(
            body.printer_id, lambda client: client.upload(path, remote, start=body.start)
        )
    except PrinterBusy as e:
        raise HTTPException(409, str(e)) from None
    except PrinterMismatch as e:
        raise HTTPException(409, str(e)) from None
    except PrinterError as e:
        raise HTTPException(502, str(e)) from None
    services.auth.audit(
        session.username, "job_send", f"{job_id} -> {printer['name']} start={body.start}", client_ip(request)
    )
    if body.start:
        services.printers.observe_state(printer["id"], "printing")
    return {"printer": printer["name"], **result}


class AttentionBody(BaseModel):
    needs_attention: bool
    reason: str | None = Field(default=None, max_length=200)


class ProfilesBody(BaseModel):
    printer_preset: str | None = Field(default=None, max_length=200)
    process: str | None = Field(default=None, max_length=200)
    filaments: list[str] = Field(default_factory=list, max_length=16)
    bed_type: str | None = Field(default=None, max_length=64)
    bed_coverage: int = Field(default=50, ge=10, le=100)


@router.put("/printers/{printer_id}/profiles")
def set_printer_profiles(
    printer_id: str, body: ProfilesBody, request: Request, session: Session = Depends(require_session)
) -> dict:
    """Links a physical printer to the OrcaSlicer profile it prints, plus defaults for the Slice page."""
    services = request.app.state.services
    try:
        printer = services.printers.get(printer_id)
    except PrinterError:
        printer = None
    if printer is None:
        raise HTTPException(404, "Not found")
    preset = (body.printer_preset or "").strip() or None
    process = (body.process or "").strip() or None
    filaments = [f.strip() for f in body.filaments if f.strip()]
    profiles = services.profiles
    try:
        known = {k: {p["name"] for p in profiles.list(k, True)} for k in ("printer", "process", "filament")}
        if preset and preset not in known["printer"]:
            raise HTTPException(400, f'Unknown printer profile "{preset}"')
        if (process or filaments) and not preset:
            raise HTTPException(400, "Choose the printer profile before default process or filament")
        bad = profiles.incompatible(preset) if preset else {"process": {}, "filament": {}}
    except ProfileError as e:
        raise HTTPException(400, str(e)) from None
    if process and (process not in known["process"] or process in bad["process"]):
        raise HTTPException(400, f'Process "{process}" doesn\'t suit "{preset}"')
    for f in filaments:
        if f not in known["filament"] or f in bad["filament"]:
            raise HTTPException(400, f'Filament "{f}" doesn\'t suit "{preset}"')
    services.printers.set_profiles(
        printer_id, preset, process, filaments, (body.bed_type or "").strip() or None, body.bed_coverage
    )
    services.auth.audit(session.username, "printer_profiles", f"{printer['name']} -> {preset}", client_ip(request))
    return services.printers.get(printer_id)


@router.put("/printers/{printer_id}/attention")
def set_attention(
    printer_id: str, body: AttentionBody, request: Request, session: Session = Depends(require_session)
) -> dict:
    services = request.app.state.services
    try:
        printer = services.printers.get(printer_id)
    except PrinterError:
        printer = None
    if printer is None:
        raise HTTPException(404, "Not found")
    reason = (body.reason or "").strip() or "Marked as needing attention."
    services.printers.set_attention(printer_id, body.needs_attention, reason)
    action = "bed_flagged" if body.needs_attention else "bed_cleared"
    services.auth.audit(session.username, action, printer["name"], client_ip(request))
    return services.printers.get(printer_id)


class QueueBody(BaseModel):
    job_id: str = Field(min_length=1, max_length=64)
    printer_id: str = Field(min_length=1, max_length=64)
    file: str = Field(min_length=1, max_length=128)


class ReorderBody(BaseModel):
    printer_id: str = Field(min_length=1, max_length=64)
    ids: list[str] = Field(max_length=500)


@router.get("/queue")
def list_queue(request: Request, _: Session = Depends(require_session)) -> list[dict]:
    return request.app.state.services.queue.list()


@router.post("/queue", status_code=201)
def add_to_queue(body: QueueBody, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    try:
        job_id = uuid.UUID(body.job_id).hex
    except ValueError:
        raise HTTPException(404, "Not found") from None
    job = services.jobs.get(job_id)
    if job is None or job["status"] != "done":
        raise HTTPException(409, "Only finished jobs can be queued")
    try:
        target = services.printers.get(body.printer_id)
        if target is None:
            raise QueueError("Unknown printer")
        services.printers.check_gcode_fits(target, job["params"].get("printer"))
    except PrinterMismatch as e:
        raise HTTPException(409, str(e)) from None
    except (QueueError, PrinterError) as e:
        raise HTTPException(400, str(e)) from None
    try:
        item = services.queue.add(job_id, body.printer_id, body.file, session.username)
    except (QueueError, PrinterError) as e:
        raise HTTPException(400, str(e)) from None
    printer = services.printers.get(item["printer_id"])
    services.auth.audit(
        session.username, "queue_add", f"{job.get('title') or job_id} -> {printer['name']}", client_ip(request)
    )
    return item


@router.put("/queue/order", status_code=204)
def reorder_queue(body: ReorderBody, request: Request, _: Session = Depends(require_session)) -> None:
    try:
        request.app.state.services.queue.reorder(body.printer_id, body.ids)
    except (QueueError, PrinterError) as e:
        raise HTTPException(409, str(e)) from None


@router.delete("/queue/{item_id}", status_code=204)
def cancel_queued(item_id: str, request: Request, session: Session = Depends(require_session)) -> None:
    services = request.app.state.services
    try:
        cancelled = services.queue.cancel(item_id)
    except QueueError:
        cancelled = False
    if not cancelled:
        raise HTTPException(404, "Not waiting in the queue")
    services.auth.audit(session.username, "queue_cancel", item_id, client_ip(request))


@router.get("/activity")
def activity(
    request: Request,
    offset: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    _: Session = Depends(require_session),
) -> dict:
    db = request.app.state.services.db
    rows = db.all(
        "SELECT id, at, actor, action, detail FROM audit_log ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset)
    )
    total = db.one("SELECT COUNT(*) AS n FROM audit_log")["n"]
    return {"items": [dict(r) for r in rows], "total": total}
