import uuid

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..auth import Session
from ..orca.cli import SliceArgsError
from ..orca.jobs import JobRequest
from ..orca.profiles import KIND_DIRS, ProfileError
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


@router.post("/jobs", status_code=202)
def create_job(body: JobBody, request: Request, session: Session = Depends(require_session)) -> dict:
    services = request.app.state.services
    try:
        job_id = services.jobs.submit(JobRequest(**body.model_dump()))
    except (SliceArgsError, ValueError) as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "slice_submit", job_id, client_ip(request))
    return {"id": job_id}


@router.get("/jobs")
def list_jobs(request: Request, _: Session = Depends(require_session)) -> list[dict]:
    return request.app.state.services.jobs.list()


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
