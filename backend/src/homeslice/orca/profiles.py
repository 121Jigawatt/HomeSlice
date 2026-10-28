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
MAX_APP_CONFIG_BYTES = 20 * 1024**2
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
    printer_model: str | None = None
    printer_variant: str | None = None
    # None = not set (inherit from parent); () = explicitly compatible with every printer.
    compatible_printers: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Selections:
    """What the user enabled in Orca's configuration wizard (OrcaSlicer.conf)."""

    printers: frozenset[tuple[str, str]]
    filaments: frozenset[str]


def _str_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


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
        self._selections: Selections | None = None
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
                compat = data.get("compatible_printers")
                index[key] = PresetRef(
                    name,
                    kind,
                    source,
                    path,
                    visible,
                    inherits or None,
                    _str_or_none(data.get("printer_model")),
                    _str_or_none(data.get("printer_variant")),
                    tuple(c for c in compat if isinstance(c, str)) if isinstance(compat, list) else None,
                )

    def _load_selections(self) -> Selections | None:
        path = self._datadir / "OrcaSlicer.conf"
        try:
            if path.stat().st_size > MAX_APP_CONFIG_BYTES:
                return None
            text = path.read_text(encoding="utf-8")
        except OSError:
            return None
        # Orca appends a "# MD5 checksum" line after the JSON.
        body = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        try:
            data = json.loads(body)
        except ValueError:
            log.warning("Could not parse %s; showing all system presets", path)
            return None
        if not isinstance(data, dict):
            return None
        printers: set[tuple[str, str]] = set()
        for entry in data.get("models") or []:
            if isinstance(entry, dict) and isinstance(entry.get("model"), str):
                for nozzle in str(entry.get("nozzle_diameter", "")).split(";"):
                    if nozzle.strip():
                        printers.add((entry["model"], nozzle.strip()))
        filaments = {f for f in data.get("filaments") or [] if isinstance(f, str)}
        return Selections(frozenset(printers), frozenset(filaments))

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
            self._selections = self._load_selections()
            self._scanned_at = time.monotonic()
            return index

    def invalidate(self) -> None:
        with self._lock:
            self._scanned_at = 0.0

    @staticmethod
    def _inherited(index: dict[tuple[str, str], PresetRef], ref: PresetRef | None, attr: str) -> Any:
        for _ in range(MAX_INHERIT_DEPTH):
            if ref is None:
                return None
            if value := getattr(ref, attr):
                return value
            ref = index.get((ref.kind, ref.inherits)) if ref.inherits else None
        return None

    @staticmethod
    def _compat_list(index: dict[tuple[str, str], PresetRef], ref: PresetRef | None) -> tuple[str, ...]:
        for _ in range(MAX_INHERIT_DEPTH):
            if ref is None:
                return ()
            if ref.compatible_printers is not None:
                return ref.compatible_printers
            ref = index.get((ref.kind, ref.inherits)) if ref.inherits else None
        return ()

    def _chain_names(self, index: dict[tuple[str, str], PresetRef], kind: str, name: str) -> list[str]:
        names, ref = [], index.get((kind, name))
        for _ in range(MAX_INHERIT_DEPTH):
            if ref is None or ref.name in names:
                break
            names.append(ref.name)
            ref = index.get((kind, ref.inherits)) if ref.inherits else None
        return names

    def incompatible(self, printer: str) -> dict[str, dict[str, str]]:
        """Process/filament presets whose compatible_printers list excludes this printer, with a reason."""
        index = self._ensure_index()
        chain = set(self._chain_names(index, "printer", printer))
        if not chain:
            raise ProfileError(f"printer preset '{printer}' not found")
        result: dict[str, dict[str, str]] = {"process": {}, "filament": {}}
        for (kind, name), ref in index.items():
            if kind not in result or not ref.visible:
                continue
            compat = self._compat_list(index, ref)
            if compat and not chain.intersection(compat):
                more = f" and {len(compat) - 1} other printer{'s' if len(compat) > 2 else ''}" if len(compat) > 1 else ""
                result[kind][name] = f"Made for {compat[0]}{more}"
        return result

    def _enabled_printers(self, index: dict[tuple[str, str], PresetRef], sel: Selections) -> set[str]:
        """System printer names that are enabled, or that a user printer is based on."""
        names: set[str] = set()
        for (kind, _), ref in index.items():
            if kind != "printer" or not ref.visible:
                continue
            if ref.source == "system":
                model = self._inherited(index, ref, "printer_model")
                variant = self._inherited(index, ref, "printer_variant")
                if (model, variant) in sel.printers:
                    names.add(ref.name)
                continue
            parent = index.get(("printer", ref.inherits)) if ref.inherits else None
            for _ in range(MAX_INHERIT_DEPTH):
                if parent is None or parent.source == "system":
                    break
                parent = index.get(("printer", parent.inherits)) if parent.inherits else None
            if parent is not None:
                names.add(parent.name)
        return names

    def list(self, kind: str, include_system: bool = False) -> list[dict[str, Any]]:
        if kind not in KIND_DIRS:
            raise ProfileError("Unknown profile kind")
        index = self._ensure_index()
        sel = self._selections
        printers = self._enabled_printers(index, sel) if include_system and sel else set()

        def system_enabled(ref: PresetRef) -> bool:
            if sel is None:
                return True
            if kind == "printer":
                return ref.name in printers
            if kind == "filament":
                return ref.name in sel.filaments
            compat = self._compat_list(index, ref)
            return not compat or any(p in printers for p in compat)

        refs = [
            r
            for (k, _), r in index.items()
            if k == kind and r.visible and (r.source == "user" or (include_system and system_enabled(r)))
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
        system_name: str | None = None
        current: str | None = name
        while current:
            if current in seen or len(chain) >= MAX_INHERIT_DEPTH:
                raise ProfileError(f"Inheritance loop or chain too deep at '{current}'")
            seen.add(current)
            ref = index.get((kind, current))
            if ref is None:
                raise ProfileError(f"{kind} preset '{current}' not found")
            if system_name is None and ref.source == "system":
                system_name = current
            data = _read_json(ref.path)
            chain.append(data)
            parent = data.get("inherits")
            current = parent if isinstance(parent, str) and parent else None
        merged: dict[str, Any] = {}
        for data in reversed(chain):
            merged.update(data)
        merged.pop("inherits", None)
        merged["name"] = name
        # Orca's CLI matches compatible_printers against the system name, which it reads from
        # `inherits` for user presets, so keep the nearest system ancestor there.
        if system_name == name:
            merged["from"] = "system"
        else:
            merged["from"] = "User"
            if system_name:
                merged["inherits"] = system_name
        return merged

    def write_resolved(self, kind: str, name: str, dest: Path) -> Path:
        dest.write_text(json.dumps(self.resolve(kind, name), indent=2), encoding="utf-8")
        return dest
