"""3MF projects (Bambu Studio / OrcaSlicer / PrusaSlicer) as plain meshes, optionally one plate at a time.

OrcaSlicer's CLI either rejects a project's embedded print settings (e.g. Bambu values out of Orca's range) or
crashes on Bambu Studio's multi-file layout, and HomeSlice slices with the user's own profiles anyway. So each
build item is written out as a world-space STL, and the slicer only ever sees meshes.
"""

from __future__ import annotations

import zipfile
from collections.abc import Iterable
from pathlib import Path

import numpy as np
from defusedxml import ElementTree

from .step import write_binary_stl
from .thumbnails import ThumbnailError, _apply, _root_model, _ThreeMF

MAX_ITEMS = 500
_SETTINGS = "Metadata/model_settings.config"


def _meta(el) -> dict[str, str]:
    return {m.get("key", ""): m.get("value", "") for m in el.findall("metadata")}


def plates(source: Path) -> list[dict]:
    """Plates saved by Bambu Studio / OrcaSlicer: [{index, name, object_ids}]; empty when there are none."""
    with zipfile.ZipFile(source) as zf:
        if _SETTINGS not in zf.namelist():
            return []
        try:
            root = ElementTree.fromstring(zf.read(_SETTINGS))
        except ElementTree.ParseError:
            return []
    out = []
    for plate in root.findall("plate"):
        meta = _meta(plate)
        try:
            index = int(meta.get("plater_id", "0"))
        except ValueError:
            continue
        ids = [_meta(mi).get("object_id") for mi in plate.findall("model_instance")]
        ids = [i for i in ids if i]
        if index > 0 and ids:
            out.append({"index": index, "name": meta.get("plater_name", "").strip(), "object_ids": ids})
    return sorted(out, key=lambda p: p["index"])


def item_meshes(source: Path, plate: int | Iterable[int] = 0) -> list[np.ndarray]:
    """World-space triangles per build item; plate > 0 (or a set of plates) keeps only those plates' objects."""
    chosen = {plate} if isinstance(plate, int) else set(plate)
    chosen.discard(0)
    wanted: set[str] | None = None
    if chosen:
        saved = {p["index"]: p for p in plates(source)}
        if missing := chosen - saved.keys():
            raise ThumbnailError(f"This 3MF has no plate {min(missing)}")
        wanted = {oid for i in chosen for oid in saved[i]["object_ids"]}
    with zipfile.ZipFile(source) as zf:
        model = _ThreeMF(zf)
        _, items = model._read(_root_model(zf))
        if len(items) > MAX_ITEMS:
            raise ThumbnailError(f"3MF has more than {MAX_ITEMS} objects")
        meshes = []
        for path, oid, matrix in items:
            if wanted is not None and oid not in wanted:
                continue
            parts = [_apply(p, matrix) for p in model._object(path, oid, 0)]
            if parts:
                meshes.append(np.concatenate(parts))
    if not meshes:
        raise ThumbnailError("No printable objects found in this 3MF" + (" on the chosen plates" if chosen else ""))
    return meshes


def write_items(source: Path, dest_dir: Path, plate: int | Iterable[int] = 0) -> list[Path]:
    """One STL per object (so the slicer can arrange them); returns the paths."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for n, tris in enumerate(item_meshes(source, plate)):
        path = dest_dir / f"object_{n + 1}.stl"
        write_binary_stl(path, tris)
        paths.append(path)
    return paths


def write_combined(source: Path, dest: Path, plate: int = 0) -> None:
    """All objects in one STL at their saved positions (for the browser preview)."""
    write_binary_stl(dest, np.concatenate(item_meshes(source, plate)))


def objects(source: Path) -> list[dict]:
    """Build items in file order: [{item (1-based), plate (0 if on no saved plate)}]."""
    on_plate = {oid: p["index"] for p in plates(source) for oid in p["object_ids"]}
    with zipfile.ZipFile(source) as zf:
        _, items = _ThreeMF(zf)._read(_root_model(zf))
    if len(items) > MAX_ITEMS:
        raise ThumbnailError(f"3MF has more than {MAX_ITEMS} objects")
    return [{"item": n, "plate": on_plate.get(oid or "", 0)} for n, (_, oid, _) in enumerate(items, start=1)]


def write_item(source: Path, item: int, dest: Path) -> None:
    """One build item (1-based) as a world-space STL."""
    with zipfile.ZipFile(source) as zf:
        model = _ThreeMF(zf)
        _, items = model._read(_root_model(zf))
        if not 1 <= item <= len(items):
            raise ThumbnailError(f"This 3MF has no object {item}")
        path, oid, matrix = items[item - 1]
        parts = [_apply(p, matrix) for p in model._object(path, oid, 0)]
    if not parts:
        raise ThumbnailError(f"Object {item} has no geometry")
    write_binary_stl(dest, np.concatenate(parts))
