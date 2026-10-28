from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import Session
from ..importer.sources import ModelImportError
from ..library.service import TooLarge
from ..library.validation import ValidationError
from .deps import client_ip, require_session

router = APIRouter(prefix="/api/import", tags=["import"])


class PreviewBody(BaseModel):
    url: str = Field(min_length=1, max_length=500)


class ImportBody(BaseModel):
    source: str = Field(pattern="^printables$")
    model_id: str = Field(min_length=1, max_length=12)
    file_ids: list[str] = Field(min_length=1, max_length=20)


@router.post("/preview")
async def preview(body: PreviewBody, request: Request, _: Session = Depends(require_session)) -> dict:
    try:
        return await request.app.state.services.importer.preview(body.url)
    except ModelImportError as e:
        raise HTTPException(400, str(e)) from None


@router.post("", status_code=201)
async def import_files(body: ImportBody, request: Request, session: Session = Depends(require_session)) -> list[dict]:
    services = request.app.state.services
    try:
        records = await services.importer.import_printables(body.model_id, body.file_ids, owner=session.username)
    except TooLarge as e:
        raise HTTPException(413, str(e)) from None
    except (ModelImportError, ValidationError) as e:
        raise HTTPException(400, str(e)) from None
    for record in records:
        services.auth.audit(
            session.username, "model_import", f"{record['id']} {record['source_url']}", client_ip(request)
        )
    return records
