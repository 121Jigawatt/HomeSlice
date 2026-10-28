import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..auth import Session
from ..orca.cli import SliceArgsError
from ..orca.jobs import JobRequest
from ..orca.profiles import KIND_DIRS, ProfileError
from ..printers.moonraker import PrinterError
from .deps import client_ip, require_session

router = APIRouter(prefix="/api", tags=["slicing"])


class JobBody(BaseModel):
    model_ids: list[str] = Field(min_length=1, max_length=50)
    printer: str = Field(min_length=1, max_length=200)
    process: str = Field(min_length=1, max_length=200)
    filaments: list[str] = Field(min_length=1, max_length=16)
    overrides: dict[str, str] = Field(default_factory=dict, max_length=32)
    arrange: bool = True
    orient: bool = False
    plate: int = Field(default=0, ge=0, le=99)
    copies: list[int] = Field(default_factory=list, max_length=50)
    # Physical printer the result is meant for; its printer profile must match `printer`.
    target_printer_id: str | None = Field(default=None, max_length=64)


def _job_id(job_id: str) -> str:
    try:
        return uuid.UUID(job_id).hex
    except ValueError:
        raise HTTPException(404, "Not found") from None


@router.get("/profiles/{kind}")
def list_profiles(kind: str, request: Request, include_system: bool = False, _: Session = Depends(require_session)):
    if kind not in KIND_DIRS:
        raise HTTPException(404, "Unknown profile kind")
    try:
        return request.app.state.services.profiles.list(kind, include_system)
    except ProfileError as e:
        raise HTTPException(400, str(e)) from None


@router.get("/profiles-compat")
def profiles_compat(printer: str, request: Request, _: Session = Depends(require_session)) -> dict:
    """Processes/filaments that don't list this printer as compatible, mapped to a reason."""
    try:
        return request.app.state.services.profiles.incompatible(printer)
    except ProfileError as e:
        raise HTTPException(404, str(e)) from None


@router.get("/printer-bed")
def printer_bed(name: str, request: Request, _: Session = Depends(require_session)) -> dict:
    """Build plate outline (mm) and max height of a printer preset, for the 3D preview."""
    try:
        preset = request.app.state.services.profiles.resolve("printer", name)
    except ProfileError as e:
        raise HTTPException(404, str(e)) from None
    points = []
    for raw in preset.get("printable_area") or []:
        try:
            x, y = (float(v) for v in str(raw).lower().split("x", 1))
        except ValueError:
            continue
        points.append([x, y])
    if len(points) < 3:
        points = [[0.0, 0.0], [256.0, 0.0], [256.0, 256.0], [0.0, 256.0]]
    height = preset.get("printable_height")
    try:
        height = float(height[0] if isinstance(height, list) else height)
    except (TypeError, ValueError, IndexError):
        height = 250.0
    return {"area": points, "height": height}


COMMON_LAYER_HEIGHTS = (0.06, 0.08, 0.10, 0.12, 0.16, 0.20, 0.24, 0.28, 0.32, 0.36, 0.40, 0.48, 0.56, 0.64, 0.72, 0.80)


def _first_float(value, default: float | None = None) -> float | None:
    if isinstance(value, list):
        value = value[0] if value else None
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


@router.get("/layer-heights")
def layer_heights(
    printer: str, request: Request, process: str | None = None, _: Session = Depends(require_session)
) -> dict:
    """Common layer heights the printer profile allows (its min/max layer height, else 25-75% of the nozzle)."""
    profiles = request.app.state.services.profiles
    try:
        preset = profiles.resolve("printer", printer)
    except ProfileError as e:
        raise HTTPException(404, str(e)) from None
    nozzle = _first_float(preset.get("nozzle_diameter"), 0.4) or 0.4
    lo = _first_float(preset.get("min_layer_height")) or round(nozzle * 0.2, 2)
    hi = _first_float(preset.get("max_layer_height")) or round(nozzle * 0.75, 2)
    if hi <= 0:
        hi = round(nozzle * 0.75, 2)
    default = None
    if process:
        try:
            default = _first_float(profiles.resolve("process", process).get("layer_height"))
        except ProfileError:
            default = None
    heights = [h for h in COMMON_LAYER_HEIGHTS if lo - 1e-9 <= h <= hi + 1e-9]
    return {"nozzle": nozzle, "min": lo, "max": hi, "heights": heights, "process_default": default}


@router.post("/jobs", status_code=202)
def create_job(body: JobBody, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    if body.target_printer_id:
        try:
            target = services.printers.get(body.target_printer_id)
            if target is None:
                raise HTTPException(400, "Unknown target printer")
            services.printers.check_gcode_fits(target, body.printer)
        except PrinterError as e:
            raise HTTPException(400, str(e)) from None
    try:
        job_id = services.jobs.submit(
            JobRequest(**body.model_dump(exclude={"target_printer_id"})), owner=session.username
        )
    except (SliceArgsError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "slice_submit", job_id, client_ip(request))
    return {"id": job_id}


JOB_STATUSES = {"queued", "running", "done", "failed", "cancelled"}


@router.get("/jobs")
def list_jobs(
    request: Request,
    q: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None, max_length=100),
    printer: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=30, ge=1, le=100),
    _: Session = Depends(require_session),
) -> dict:
    statuses = [s for s in (status or "").split(",") if s in JOB_STATUSES] or None
    jobs = request.app.state.services.jobs
    items, total = jobs.list(limit=limit, offset=offset, q=q or None, statuses=statuses, printer=printer or None)
    return {"items": items, "total": total, "printers": jobs.printers_used()}


class CleanupBody(BaseModel):
    older_than_days: int = Field(ge=0, le=3650)
    include_failed: bool = True


@router.post("/jobs/cleanup")
def cleanup_jobs(body: CleanupBody, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    statuses = ("done", "failed", "cancelled") if body.include_failed else ("done",)
    removed = services.jobs.cleanup(body.older_than_days, statuses)
    services.auth.audit(session.username, "jobs_cleanup", f"{removed} older than {body.older_than_days}d", client_ip(request))
    return {"removed": removed}


@router.post("/jobs/{job_id}/retry", status_code=202)
def retry_job(job_id: str, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    try:
        new_id = services.jobs.retry(_job_id(job_id), owner=session.username)
    except (SliceArgsError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "slice_retry", f"{job_id} -> {new_id}", client_ip(request))
    return {"id": new_id}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request, _: Session = Depends(require_session)) -> dict:
    job = request.app.state.services.jobs.get(_job_id(job_id))
    if job is None:
        raise HTTPException(404, "Not found")
    return job


@router.get("/jobs/{job_id}/log", response_class=PlainTextResponse)
def job_log(job_id: str, request: Request, _: Session = Depends(require_session)) -> str:
    return request.app.state.services.jobs.log_tail(_job_id(job_id))


@router.get("/jobs/{job_id}/files/{name}")
def job_file(job_id: str, name: str, request: Request, _: Session = Depends(require_session)) -> FileResponse:
    path = request.app.state.services.jobs.output_path(_job_id(job_id), name)
    if path is None:
        raise HTTPException(404, "Not found")
    return FileResponse(path, filename=name, media_type="application/octet-stream")


@router.post("/jobs/{job_id}/cancel", status_code=204)
def cancel_job(job_id: str, request: Request, _: Session = Depends(require_session)) -> None:
    if not request.app.state.services.jobs.cancel(_job_id(job_id)):
        raise HTTPException(409, "Job is not running")


@router.delete("/jobs/{job_id}", status_code=204)
def delete_job(job_id: str, request: Request, _: Session = Depends(require_session)) -> None:
    request.app.state.services.jobs.delete(_job_id(job_id))
