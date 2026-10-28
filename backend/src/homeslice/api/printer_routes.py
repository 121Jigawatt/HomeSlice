import re
import uuid
from pathlib import PurePosixPath
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..auth import Session
from ..library.validation import ValidationError
from ..printers.moonraker import PrinterBusy, PrinterError
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


def _remote_name(services, job: dict, file: str) -> str:
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
        client = await printers.client(printer_id)
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
        remote = _remote_name(services, job, body.file)
        result = await (await services.printers.client(body.printer_id)).upload(path, remote, start=body.start)
    except PrinterBusy as e:
        raise HTTPException(409, str(e)) from None
    except PrinterError as e:
        raise HTTPException(502, str(e)) from None
    services.auth.audit(
        session.username, "job_send", f"{job_id} -> {printer['name']} start={body.start}", client_ip(request)
    )
    return {"printer": printer["name"], **result}
