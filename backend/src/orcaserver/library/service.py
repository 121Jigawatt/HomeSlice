import hashlib
import os
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings
from ..db import Database
from .validation import ValidationError, normalize_ext, safe_display_name, validate_file


class TooLarge(ValidationError):
    pass


def parse_model_id(model_id: str) -> str:
    try:
        return uuid.UUID(model_id).hex
    except ValueError:
        raise ValidationError("Invalid model id") from None


class Library:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._max_bytes = settings.max_upload_mb * 1024**2

    def path_for(self, model_id: str) -> Path | None:
        row = self._db.one("SELECT id, ext FROM models WHERE id=?", (parse_model_id(model_id),))
        if row is None:
            return None
        return self._settings.library_dir / f"{row['id']}{row['ext']}"

    async def store(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        *,
        source_url: str | None = None,
        license: str | None = None,
    ) -> dict:
        ext = normalize_ext(filename)
        model_id = uuid.uuid4().hex
        tmp = self._settings.tmp_dir / f"upload-{model_id}{ext}"
        digest = hashlib.sha256()
        size = 0
        try:
            with tmp.open("xb") as f:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise TooLarge(f"File exceeds {self._settings.max_upload_mb} MB limit")
                    digest.update(chunk)
                    f.write(chunk)
            validate_file(tmp, ext)
            final = self._settings.library_dir / f"{model_id}{ext}"
            os.replace(tmp, final)
        finally:
            tmp.unlink(missing_ok=True)
        record = {
            "id": model_id,
            "filename": safe_display_name(filename),
            "ext": ext,
            "size": size,
            "sha256": digest.hexdigest(),
            "source_url": source_url,
            "license": license,
            "created_at": time.time(),
        }
        self._db.execute(
            "INSERT INTO models(id, filename, ext, size, sha256, source_url, license, created_at) "
            "VALUES(:id, :filename, :ext, :size, :sha256, :source_url, :license, :created_at)",
            record,
        )
        return record

    def list(self) -> list[dict]:
        return [dict(r) for r in self._db.all("SELECT * FROM models ORDER BY created_at DESC")]

    def get(self, model_id: str) -> dict | None:
        row = self._db.one("SELECT * FROM models WHERE id=?", (parse_model_id(model_id),))
        return dict(row) if row else None

    def delete(self, model_id: str) -> bool:
        path = self.path_for(model_id)
        if path is None:
            return False
        self._db.execute("DELETE FROM models WHERE id=?", (parse_model_id(model_id),))
        path.unlink(missing_ok=True)
        return True
