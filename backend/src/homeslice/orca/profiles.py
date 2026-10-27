import json
import logging
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

KIND_DIRS = {"printer": "machine", "filament": "filament", "process": "process"}
MAX_PROFILE_BYTES = 2 * 1024**2
MAX_INHERIT_DEPTH = 20
CACHE_TTL_S = 60


class ProfileError(Exception):
    pass


@dataclass(frozen=True)
class PresetRef:
    name: str
    kind: str
    source: str
    path: Path
    visible: bool
    inherits: str | None


def _read_json(path: Path) -> dict[str, Any]:
    if path.stat().st_size > MAX_PROFILE_BYTES:
        raise ProfileError(f"Profile file too large: {path.name}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ProfileError(f"Profile is not an object: {path.name}")
    return data


class ProfileStore:
    """Indexes Orca presets by name. Paths are only ever taken from the index, never from user input."""

    def __init__(self, orca_datadir: Path, extra_system_dirs: list[Path] | None = None) -> None:
        self._datadir = orca_datadir
        self._extra_system_dirs = extra_system_dirs or []
        self._index: dict[tuple[str, str], PresetRef] = {}
        self._scanned_at = 0.0
        self._lock = threading.Lock()

    def set_extra_system_dirs(self, dirs: list[Path]) -> None:
        with self._lock:
            self._extra_system_dirs = dirs
            self._scanned_at = 0.0

    def _scan_tree(self, root: Path, source: str, index: dict[tuple[str, str], PresetRef]) -> None:
        if not root.is_dir():
            return
        for kind, sub in KIND_DIRS.items():
            pattern = f"*/{sub}/**/*.json" if source == "system" else f"{sub}/**/*.json"
            for path in root.glob(pattern):
                try:
                    data = _read_json(path)
                except (OSError, ValueError, ProfileError) as e:
                    log.debug("Skipping profile %s: %s", path, e)
                    continue
                name = data.get("name") or path.stem
                if not isinstance(name, str):
                    continue
                key = (kind, name)
                if source == "system" and key in index:
                    continue
                visible = source == "user" or str(data.get("instantiation", "true")).lower() == "true"
                inherits = data.get("inherits") if isinstance(data.get("inherits"), str) else None
                index[key] = PresetRef(name, kind, source, path, visible, inherits or None)

    def _ensure_index(self) -> dict[tuple[str, str], PresetRef]:
        with self._lock:
            if time.monotonic() - self._scanned_at < CACHE_TTL_S and self._index:
                return self._index
            index: dict[tuple[str, str], PresetRef] = {}
            user_root = self._datadir / "user"
            if user_root.is_dir():
                for user_dir in sorted(p for p in user_root.iterdir() if p.is_dir()):
                    self._scan_tree(user_dir, "user", index)
            for root in [self._datadir / "system", *self._extra_system_dirs]:
                self._scan_tree(root, "system", index)
            self._index = index
            self._scanned_at = time.monotonic()
            return index

    def invalidate(self) -> None:
        with self._lock:
            self._scanned_at = 0.0

    def list(self, kind: str, include_system: bool = False) -> list[dict[str, Any]]:
        if kind not in KIND_DIRS:
            raise ProfileError("Unknown profile kind")
        refs = [
            r
            for (k, _), r in self._ensure_index().items()
            if k == kind and r.visible and (include_system or r.source == "user")
        ]
        return [
            {"name": r.name, "kind": r.kind, "source": r.source, "inherits": r.inherits}
            for r in sorted(refs, key=lambda r: (r.source != "user", r.name.lower()))
        ]

    def resolve(self, kind: str, name: str) -> dict[str, Any]:
        """Flattens the inherits chain into one self-contained preset."""
        index = self._ensure_index()
        chain: list[dict[str, Any]] = []
        seen: set[str] = set()
        current: str | None = name
        while current:
            if current in seen or len(chain) >= MAX_INHERIT_DEPTH:
                raise ProfileError(f"Inheritance loop or chain too deep at '{current}'")
            seen.add(current)
            ref = index.get((kind, current))
            if ref is None:
                raise ProfileError(f"{kind} preset '{current}' not found")
            data = _read_json(ref.path)
            chain.append(data)
            parent = data.get("inherits")
            current = parent if isinstance(parent, str) and parent else None
        merged: dict[str, Any] = {}
        for data in reversed(chain):
            merged.update(data)
        merged.pop("inherits", None)
        merged["name"] = name
        merged["from"] = "User"
        return merged

    def write_resolved(self, kind: str, name: str, dest: Path) -> Path:
        dest.write_text(json.dumps(self.resolve(kind, name), indent=2), encoding="utf-8")
        return dest
