import re
import uuid
from collections.abc import AsyncIterator
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from ..library.service import Library
from ..library.validation import ALLOWED_EXTS
from ..net.safe_fetch import FetchError, SafeFetcher

PRINTABLES_API = "https://api.printables.com/graphql/"
PRINTABLES_MEDIA = "https://media.printables.com/"
MAKERWORLD_API = "https://makerworld.com/api/v1/design-service/design/"

API_HOSTS = {"api.printables.com", "makerworld.com"}
FILE_HOSTS = {"files.printables.com"}
MAX_FILES_PER_IMPORT = 20

_PRINTABLES_PATH = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?model/(\d{1,12})(?:[-/].*)?$", re.I)
_MAKERWORLD_PATH = re.compile(r"^/(?:[a-z]{2}(?:-[a-z]{2})?/)?models/(\d{1,12})(?:[-/].*)?$", re.I)

_PRINT_QUERY = """
query($id: ID!) {
  print(id: $id) {
    id name
    license { name abbreviation }
    user { publicUsername }
    images { filePath }
    stls { id name fileSize filePreviewPath }
  }
}
"""

_LINK_MUTATION = """
mutation($id: ID!, $printId: ID!) {
  getDownloadLink(id: $id, printId: $printId, fileType: stl, source: model_detail) {
    ok
    output { link }
  }
}
"""


class ModelImportError(Exception):
    pass


def parse_link(url: str) -> tuple[str, str]:
    """Returns (source, model id) for a Printables or MakerWorld model URL."""
    parts = urlsplit(url.strip())
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https"):
        raise ModelImportError("Paste a link starting with https://")
    if host in ("printables.com", "www.printables.com") and (m := _PRINTABLES_PATH.match(parts.path)):
        return "printables", m.group(1)
    if host in ("makerworld.com", "www.makerworld.com") and (m := _MAKERWORLD_PATH.match(parts.path)):
        return "makerworld", m.group(1)
    raise ModelImportError("Paste a Printables or MakerWorld model link")


def _importable(name: str) -> bool:
    return PurePosixPath(name.lower()).suffix in ALLOWED_EXTS


def _https_or_none(url: Any) -> str | None:
    return url if isinstance(url, str) and url.startswith("https://") else None


class Importer:
    def __init__(self, library: Library, tmp_dir: Path, max_file_bytes: int) -> None:
        self._library = library
        self._tmp_dir = tmp_dir
        self.api = SafeFetcher(API_HOSTS, max_bytes=10 * 1024**2, timeout=30)
        self.files = SafeFetcher(FILE_HOSTS, max_bytes=max_file_bytes, timeout=600)

    async def preview(self, url: str) -> dict[str, Any]:
        source, model_id = parse_link(url)
        try:
            if source == "printables":
                return await self._printables_preview(model_id)
            return await self._makerworld_preview(model_id)
        except FetchError as e:
            raise ModelImportError(f"Could not load the model: {e}") from None

    async def _printables_model(self, model_id: str) -> dict[str, Any]:
        data = await self.api.post_json(PRINTABLES_API, {"query": _PRINT_QUERY, "variables": {"id": model_id}})
        model = (data.get("data") or {}).get("print") if isinstance(data, dict) else None
        if not isinstance(model, dict):
            raise ModelImportError("Model not found on Printables")
        return model

    async def _printables_preview(self, model_id: str) -> dict[str, Any]:
        model = await self._printables_model(model_id)
        license_ = model.get("license") or {}
        files = []
        for stl in model.get("stls") or []:
            name = str(stl.get("name") or "")
            preview = stl.get("filePreviewPath")
            files.append(
                {
                    "id": str(stl.get("id")),
                    "name": name,
                    "size": stl.get("fileSize"),
                    "preview": PRINTABLES_MEDIA + preview if isinstance(preview, str) and preview else None,
                    "importable": _importable(name),
                }
            )
        images = [PRINTABLES_MEDIA + i["filePath"] for i in model.get("images") or [] if i.get("filePath")]
        return {
            "source": "printables",
            "id": model_id,
            "url": f"https://www.printables.com/model/{model_id}",
            "title": model.get("name") or f"Printables model {model_id}",
            "author": (model.get("user") or {}).get("publicUsername"),
            "license": license_.get("abbreviation") or license_.get("name"),
            "images": images[:12],
            "files": files,
            "downloadable": True,
            "note": None,
        }

    async def _makerworld_preview(self, model_id: str) -> dict[str, Any]:
        data = await self.api.get_json(MAKERWORLD_API + model_id, {"Accept": "application/json"})
        if not isinstance(data, dict) or "title" not in data:
            raise ModelImportError("Model not found on MakerWorld")
        ext = data.get("designExtension") or {}
        files = [
            {
                "id": str(i),
                "name": str(f.get("modelName") or ""),
                "size": f.get("modelSize"),
                "preview": _https_or_none(f.get("thumbnailUrl")),
                "importable": False,
            }
            for i, f in enumerate(ext.get("model_files") or [])
        ]
        images = [u for p in ext.get("design_pictures") or [] if (u := _https_or_none(p.get("url")))]
        if not images and (cover := _https_or_none(data.get("coverUrl"))):
            images = [cover]
        return {
            "source": "makerworld",
            "id": model_id,
            "url": f"https://makerworld.com/en/models/{model_id}",
            "title": data.get("title") or f"MakerWorld model {model_id}",
            "author": (data.get("designCreator") or {}).get("name"),
            "license": data.get("license"),
            "images": images[:12],
            "files": files,
            "downloadable": False,
            "note": "MakerWorld requires a login to download. Open the model on MakerWorld to get the files.",
        }

    async def import_printables(self, model_id: str, file_ids: list[str], owner: str | None = None) -> list[dict[str, Any]]:
        if not re.fullmatch(r"\d{1,12}", model_id):
            raise ModelImportError("Invalid model id")
        if not 1 <= len(file_ids) <= MAX_FILES_PER_IMPORT:
            raise ModelImportError(f"Select between 1 and {MAX_FILES_PER_IMPORT} files")
        try:
            model = await self._printables_model(model_id)
            license_ = model.get("license") or {}
            license_name = license_.get("abbreviation") or license_.get("name")
            # Only files that really belong to this model can be fetched.
            available = {str(s.get("id")): str(s.get("name") or "") for s in model.get("stls") or []}
            records = []
            for file_id in dict.fromkeys(file_ids):
                name = available.get(file_id)
                if name is None:
                    raise ModelImportError("A selected file is not part of this model")
                if not _importable(name):
                    raise ModelImportError(f"{name} is not a supported model file")
                link = await self._printables_link(file_id, model_id)
                records.append(
                    await self._download_into_library(
                        link, name, f"https://www.printables.com/model/{model_id}", license_name, owner
                    )
                )
            return records
        except FetchError as e:
            raise ModelImportError(f"Download failed: {e}") from None

    async def _printables_link(self, file_id: str, model_id: str) -> str:
        data = await self.api.post_json(
            PRINTABLES_API, {"query": _LINK_MUTATION, "variables": {"id": file_id, "printId": model_id}}
        )
        result = ((data.get("data") or {}).get("getDownloadLink") or {}) if isinstance(data, dict) else {}
        link = (result.get("output") or {}).get("link")
        if not result.get("ok") or not isinstance(link, str):
            raise ModelImportError("Printables did not provide a download link")
        return link

    async def _download_into_library(
        self, link: str, name: str, source_url: str, license_name: str | None, owner: str | None = None
    ) -> dict[str, Any]:
        tmp = self._tmp_dir / f"import-{uuid.uuid4().hex}"
        try:
            await self.files.download(link, tmp)

            async def chunks() -> AsyncIterator[bytes]:
                with tmp.open("rb") as f:
                    while chunk := f.read(1024 * 1024):
                        yield chunk

            return await self._library.store(name, chunks(), source_url=source_url, license=license_name, owner=owner)
        finally:
            tmp.unlink(missing_ok=True)
