import hashlib
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

from ..config import Settings
from ..db import Database
from .step import STEP_EXTS
from .step import convert as step_convert
from .thumbnails import Thumbnails
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
        self.thumbnails = Thumbnails(settings.thumbs_dir)
        self._convert_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def path_for(self, model_id: str) -> Path | None:
        row = self._db.one("SELECT id, ext FROM models WHERE id=?", (parse_model_id(model_id),))
        if row is None:
            return None
        return self._settings.library_dir / f"{row['id']}{row['ext']}"

    def _converted(self, model_id: str) -> Path:
        return self._settings.library_dir / f"{model_id}.mesh.stl"

    def mesh_path(self, model_id: str) -> Path | None:
        """A file the slicer and browser can read: the original, or for STEP a converted STL (made on first use).

        Blocking; call from a thread. Raises StepError if a STEP file can't be converted.
        """
        path = self.path_for(model_id)
        if path is None or path.suffix not in STEP_EXTS:
            return path
        mid = parse_model_id(model_id)
        converted = self._converted(mid)
        with self._convert_lock(mid):
            if not converted.is_file():
                step_convert(path, converted)
        return converted

    def _convert_lock(self, model_id: str) -> threading.Lock:
        with self._locks_guard:
            return self._convert_locks.setdefault(model_id, threading.Lock())

    def _schedule_thumbnail(self, model_id: str, ext: str) -> None:
        source = self._settings.library_dir / f"{model_id}{ext}"
        prepare = (lambda: self.mesh_path(model_id)) if ext in STEP_EXTS else None
        self.thumbnails.schedule(model_id, source, ext, prepare)

    async def store(
        self,
        filename: str,
        chunks: AsyncIterator[bytes],
        *,
        source_url: str | None = None,
        license: str | None = None,
        owner: str | None = None,
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
            "owner": owner,
            "created_at": time.time(),
        }
        self._db.execute(
            "INSERT INTO models(id, filename, ext, size, sha256, source_url, license, owner, created_at) "
            "VALUES(:id, :filename, :ext, :size, :sha256, :source_url, :license, :owner, :created_at)",
            record,
        )
        self._schedule_thumbnail(model_id, ext)
        return {**record, "has_thumbnail": False}

    def _public(self, row) -> dict:
        return {**dict(row), "has_thumbnail": self.thumbnails.exists(row["id"])}

    def list(self) -> list[dict]:
        return [self._public(r) for r in self._db.all("SELECT * FROM models ORDER BY created_at DESC")]

    def get(self, model_id: str) -> dict | None:
        row = self._db.one("SELECT * FROM models WHERE id=?", (parse_model_id(model_id),))
        return self._public(row) if row else None

    def backfill_thumbnails(self) -> int:
        missing = self.thumbnails.missing([dict(r) for r in self._db.all("SELECT id, ext FROM models")])
        for m in missing:
            self._schedule_thumbnail(m["id"], m["ext"])
        return len(missing)

    def delete(self, model_id: str) -> bool:
        path = self.path_for(model_id)
        if path is None:
            return False
        self._db.execute("DELETE FROM models WHERE id=?", (parse_model_id(model_id),))
        path.unlink(missing_ok=True)
        self._converted(parse_model_id(model_id)).unlink(missing_ok=True)
        self.thumbnails.delete(parse_model_id(model_id))
        return True
