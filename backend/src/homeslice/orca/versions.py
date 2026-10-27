import json
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

VERSION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")


class VersionError(Exception):
    pass


class VersionStore:
    """Installed OrcaSlicer builds under <orca_dir>/versions/<id>, with an atomic 'current' pointer file."""

    def __init__(self, orca_dir: Path) -> None:
        self._root = orca_dir
        self._versions = orca_dir / "versions"
        self._versions.mkdir(parents=True, exist_ok=True)

    @property
    def versions_dir(self) -> Path:
        return self._versions

    def _dir(self, version_id: str) -> Path:
        if not VERSION_ID_RE.fullmatch(version_id) or version_id in (".", ".."):
            raise VersionError("Invalid version id")
        return self._versions / version_id

    def _read_pointer(self, name: str) -> str | None:
        p = self._root / name
        if not p.is_file():
            return None
        value = p.read_text().strip()
        return value if VERSION_ID_RE.fullmatch(value) else None

    def _write_pointer(self, name: str, value: str) -> None:
        tmp = self._root / f".{name}.tmp"
        tmp.write_text(value)
        os.replace(tmp, self._root / name)

    def current(self) -> str | None:
        cur = self._read_pointer("current.txt")
        return cur if cur and (self._versions / cur).is_dir() else None

    def previous(self) -> str | None:
        return self._read_pointer("previous.txt")

    def current_binary(self) -> Path | None:
        cur = self.current()
        if cur is None:
            return None
        binary = self._versions / cur / "AppRun"
        return binary if binary.exists() else None

    def system_profile_dirs(self) -> list[Path]:
        cur = self.current()
        if cur is None:
            return []
        candidates = [
            self._versions / cur / "resources" / "profiles",
            self._versions / cur / "usr" / "share" / "OrcaSlicer" / "resources" / "profiles",
        ]
        return [c for c in candidates if c.is_dir()]

    def metadata(self, version_id: str) -> dict[str, Any]:
        meta = self._dir(version_id) / ".homeslice.json"
        try:
            return json.loads(meta.read_text())
        except (OSError, ValueError):
            return {}

    def installed(self) -> list[dict[str, Any]]:
        cur = self.current()
        items = []
        for d in self._versions.iterdir():
            if d.is_dir() and VERSION_ID_RE.fullmatch(d.name):
                items.append({"id": d.name, "current": d.name == cur, **self.metadata(d.name)})
        return sorted(items, key=lambda i: i.get("installed_at", 0), reverse=True)

    def add(self, version_id: str, extracted_root: Path, meta: dict[str, Any]) -> None:
        dest = self._dir(version_id)
        if dest.exists():
            shutil.rmtree(dest)
        os.replace(extracted_root, dest)
        (dest / ".homeslice.json").write_text(json.dumps({**meta, "installed_at": time.time()}))

    def activate(self, version_id: str) -> None:
        if not self._dir(version_id).is_dir():
            raise VersionError("Version is not installed")
        cur = self.current()
        if cur == version_id:
            return
        if cur:
            self._write_pointer("previous.txt", cur)
        self._write_pointer("current.txt", version_id)

    def rollback(self) -> str:
        prev = self.previous()
        if not prev or not (self._versions / prev).is_dir():
            raise VersionError("No previous version to roll back to")
        self.activate(prev)
        return prev

    def prune(self, keep: int) -> None:
        protected = {self.current(), self.previous()}
        items = self.installed()
        for item in items[keep:]:
            if item["id"] not in protected:
                shutil.rmtree(self._versions / item["id"], ignore_errors=True)
