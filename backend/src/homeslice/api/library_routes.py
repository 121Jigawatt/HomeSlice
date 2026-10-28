import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse

from ..auth import Session
from ..library.service import TooLarge
from ..library.step import StepError
from ..library.thumbnails import ThumbnailError
from ..library.validation import ValidationError
from .deps import client_ip, require_session

router = APIRouter(prefix="/api/library", tags=["library"])


@router.get("")
def list_models(request: Request, _: Session = Depends(require_session)) -> list[dict]:
    return request.app.state.services.library.list()


@router.post("", status_code=201)
async def upload_model(
    request: Request,
    filename: str = Query(min_length=1, max_length=255),
    session: Session = Depends(require_session),
) -> dict:
    """Raw-body upload (application/octet-stream) so size limits apply while streaming."""
    services = request.app.state.services
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > services.settings.max_upload_mb * 1024**2:
        raise HTTPException(413, "File too large")
    try:
        record = await services.library.store(filename, request.stream(), owner=session.username)
    except TooLarge as e:
        raise HTTPException(413, str(e)) from None
    except ValidationError as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "model_upload", record["id"], client_ip(request))
    return record


@router.get("/{model_id}/file")
def download_model(model_id: str, request: Request, _: Session = Depends(require_session)) -> FileResponse:
    library = request.app.state.services.library
    try:
        record = library.get(model_id)
    except ValidationError:
        raise HTTPException(404, "Not found") from None
    path = library.path_for(model_id) if record else None
    if record is None or path is None or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, filename=record["filename"], media_type="application/octet-stream")


@router.get("/{model_id}/mesh")
async def model_mesh(
    model_id: str, request: Request, plate: int = Query(0, ge=0, le=999), _: Session = Depends(require_session)
) -> FileResponse:
    """The model as an STL the browser can draw: STEP converted, 3MF projects flattened (optionally one plate)."""
    library = request.app.state.services.library
    try:
        if library.get(model_id) is None:
            raise HTTPException(404, "Not found")
        path = await asyncio.to_thread(library.preview_mesh, model_id, plate)
    except ValidationError:
        raise HTTPException(404, "Not found") from None
    except (StepError, ThumbnailError) as e:
        raise HTTPException(422, str(e)) from None
    if path is None or not path.is_file():
        raise HTTPException(404, "Not found")
    return FileResponse(path, media_type="application/octet-stream")


@router.get("/{model_id}/thumbnail")
def model_thumbnail(model_id: str, request: Request, _: Session = Depends(require_session)) -> FileResponse:
    library = request.app.state.services.library
    try:
        record = library.get(model_id)
    except ValidationError:
        raise HTTPException(404, "Not found") from None
    if record is None or not record["has_thumbnail"]:
        raise HTTPException(404, "No thumbnail")
    return FileResponse(library.thumbnails.path(record["id"]), media_type="image/png")


@router.delete("/{model_id}", status_code=204)
def delete_model(model_id: str, request: Request, session: Session = Depends(require_session)) -> None:
    services = request.app.state.services
    try:
        deleted = services.library.delete(model_id)
    except ValidationError:
        deleted = False
    if not deleted:
        raise HTTPException(404, "Not found")
    services.auth.audit(session.username, "model_delete", model_id, client_ip(request))
