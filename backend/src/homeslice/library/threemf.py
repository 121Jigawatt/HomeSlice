"""3MF project geometry, with embedded slicer presets excluded from slice inputs."""

from __future__ import annotations

import json
import re
import zipfile
from collections.abc import Iterable
from pathlib import Path
from xml.etree import ElementTree as XML

import numpy as np
from defusedxml import ElementTree

from .step import write_binary_stl
from .thumbnails import MAX_COMPONENT_DEPTH, MAX_PARSED_BYTES, ThumbnailError, _apply, _NS, _root_model, _ThreeMF

MAX_ITEMS = 500
_SETTINGS = "Metadata/model_settings.config"
_PROJECT = "Metadata/project_settings.config"


def has_face_paint(source: Path) -> bool:
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            if not entry.filename.lower().endswith(".model"):
                continue
            with archive.open(entry) as content:
                tail = b""
                while chunk := content.read(1024 * 1024):
                    block = tail + chunk
                    if b"paint_color" in block or b"mmu_segmentation" in block:
                        return True
                    tail = block[-16:]
    return False


def _read_palette(archive: zipfile.ZipFile) -> list[str]:
    try:
        info = archive.getinfo(_PROJECT)
        if info.file_size > 1024 * 1024:
            raise ValueError("oversized settings")
        palette = json.loads(archive.read(info)).get("filament_colour")
    except (KeyError, ValueError, AttributeError):
        raise ThumbnailError("The painted 3MF has no readable source filament palette") from None
    if not isinstance(palette, list) or not 2 <= len(palette) <= 16 or any(
        not isinstance(color, str) or not re.fullmatch(r"#[0-9A-Fa-f]{6}", color) for color in palette
    ):
        raise ThumbnailError("The painted 3MF has no supported source filament palette")
    return palette


def source_palette(source: Path) -> list[str]:
    with zipfile.ZipFile(source) as archive:
        settings = _color_settings(archive)
        assigned = {slot for _, slot in settings.values()}
        assigned.update(slot for parts, _ in settings.values() for slot in parts.values())
        return _read_palette(archive) if any(slot > 1 for slot in assigned) or has_face_paint(source) else []


def _color_settings(archive: zipfile.ZipFile) -> dict[str, tuple[dict[str, int], int]]:
    if _SETTINGS not in archive.namelist():
        return {}
    info = archive.getinfo(_SETTINGS)
    if info.file_size > 4 * 1024 * 1024:
        raise ThumbnailError("3MF object settings are too large")
    try:
        root = ElementTree.fromstring(archive.read(info))
        colors = {}
        for obj in root.findall("object"):
            base = int(_meta(obj).get("extruder", "1")) or 1
            parts = {}
            for part in obj.findall("part"):
                slot = _meta(part).get("extruder")
                if slot:
                    parts[part.attrib["id"]] = int(slot) or base
            colors[obj.attrib["id"]] = (parts, base)
    except (ValueError, KeyError, ElementTree.ParseError):
        raise ThumbnailError("Invalid 3MF object color settings") from None
    return colors


def _remap_paint(color: str, mapping: tuple[int, ...], base: int = 1) -> str:
    nibbles = iter(int(digit, 16) for digit in reversed(color))
    pending = 1
    encoded = []
    try:
        while pending:
            code = next(nibbles)
            pending -= 1
            if sides := code & 3:
                encoded.append(f"{code:X}")
                pending += sides + 1
                continue
            state = next(nibbles) + 3 if code == 12 else code >> 2
            slot = mapping[(state or base) - 1]
            encoded.append(f"{slot << 2:X}" if slot < 3 else f"C{slot - 3:X}")
        if next(nibbles, None) is not None:
            raise ValueError("trailing paint data")
    except (StopIteration, IndexError, ValueError):
        raise ThumbnailError("Invalid paint data or source filament slot") from None
    return "".join(encoded)[::-1]


def write_painted_project(
    source: Path, destination: Path, slots: int, selection: Iterable[int] = (), copies: int = 1,
    slot_map: Iterable[int] = (),
) -> list[str]:
    """Rebuild selected painted geometry as an inline 3MF, without importing its slicer settings."""
    chosen = set(selection)
    saved = {plate["index"]: set(plate["object_ids"]) for plate in plates(source)} if chosen else {}
    if chosen - saved.keys():
        raise ThumbnailError("The painted 3MF has no such plate")
    wanted = set().union(*(saved[index] for index in chosen)) if chosen else None
    with zipfile.ZipFile(source) as archive:
        palette = _read_palette(archive)
        settings = _color_settings(archive)
        if _SETTINGS in archive.namelist():
            model_settings = ElementTree.fromstring(archive.read(_SETTINGS))
            if any(part.get("subtype", "normal_part") != "normal_part" for part in model_settings.iter("part")):
                raise ThumbnailError("Color projects with modifier parts are not supported")
        mapping = tuple(slot_map)
        if not mapping and len(palette) != slots:
            raise ThumbnailError(f"Choose {len(palette)} filament slots in the 3MF's order: {', '.join(palette)}")
        if mapping and (len(mapping) != len(palette) or len(set(mapping)) != len(mapping)
                        or any(not 1 <= slot <= slots for slot in mapping)):
            raise ThumbnailError("Map every source color to a different configured filament slot")
        target_palette = [""] * slots
        for index, color in zip(mapping or range(1, slots + 1), palette):
            target_palette[index - 1] = color
        remapped: dict[tuple[str, int], str] = {}

        source_model = _ThreeMF(archive)
        root_name = _root_model(archive)
        root_info = archive.getinfo(root_name)
        if root_info.file_size > MAX_PARSED_BYTES:
            raise ThumbnailError("Model part too large")
        source_root = ElementTree.fromstring(archive.read(root_info))
        if source_root.get("unit", "millimeter") != "millimeter":
            raise ThumbnailError("Painted 3MF projects must use millimeters")
        _, items = source_model._read(root_name)
        items = [(path, obj, matrix) for path, obj, matrix in items if wanted is None or obj in wanted]
        if not items or not 1 <= len(items) * copies <= 200:
            raise ThumbnailError("Choose between 1 and 200 painted build objects, including copies")
        model = XML.Element("model", xmlns=_NS[1:-1], unit="millimeter")
        XML.SubElement(model, "metadata", name="BambuStudio:3mfVersion").text = "1"
        resources = XML.SubElement(model, "resources")
        object_ids: dict[tuple[str, str | None, str | None], str] = {}

        def transform(matrix: np.ndarray | None) -> dict[str, str]:
            if matrix is None:
                return {}
            if not np.isfinite(matrix).all():
                raise ThumbnailError("Invalid painted object transform")
            return {"transform": " ".join(map(str, matrix.flat))}

        def add_object(path: str, object_id: str | None, root_id: str | None, depth: int = 0) -> str:
            if depth > MAX_COMPONENT_DEPTH or len(object_ids) >= MAX_ITEMS:
                raise ThumbnailError("Painted 3MF has too many nested parts")
            key = (path.lstrip("/"), object_id, root_id)
            if key in object_ids:
                return object_ids[key]
            try:
                mesh, parts = source_model._read(path)[0][object_id]
            except KeyError:
                raise ThumbnailError("Painted 3MF references a missing part") from None
            if (mesh is None) == (not parts):
                raise ThumbnailError("Painted 3MF has an unsupported object layout")
            refs = [(add_object(part_path, part_id, root_id, depth + 1), matrix) for part_path, part_id, matrix in parts]
            new_id = str(len(object_ids) + 1)
            object_ids[key] = new_id
            obj = XML.SubElement(resources, "object", id=new_id, type="model")
            if mesh is None:
                components = XML.SubElement(obj, "components")
                for ref, matrix in refs:
                    XML.SubElement(components, "component", objectid=ref, **transform(matrix))
                return new_id

            vertices = mesh.find(f"{_NS}vertices")
            triangles = mesh.find(f"{_NS}triangles")
            if vertices is None or triangles is None or not len(vertices) or not len(triangles):
                raise ThumbnailError("Painted 3MF contains an empty mesh")
            clean_mesh = XML.SubElement(obj, "mesh")
            clean_vertices = XML.SubElement(clean_mesh, "vertices")
            clean_triangles = XML.SubElement(clean_mesh, "triangles")
            part_slots, base_slot = settings.get(root_id or "", ({}, 1))
            base_slot = part_slots.get(object_id or "", base_slot)
            if not 1 <= base_slot <= len(palette):
                raise ThumbnailError("3MF part references a missing source filament slot")
            for vertex in vertices:
                coords = {axis: vertex.get(axis, "") for axis in ("x", "y", "z")}
                if not all(np.isfinite(float(value)) for value in coords.values()):
                    raise ThumbnailError("Invalid painted mesh vertex")
                XML.SubElement(clean_vertices, "vertex", coords)
            for triangle in triangles:
                indices = {axis: triangle.get(axis, "") for axis in ("v1", "v2", "v3")}
                if not all(0 <= int(value) < len(vertices) for value in indices.values()):
                    raise ThumbnailError("Invalid painted mesh triangle")
                color = triangle.get("paint_color")
                if color and not re.fullmatch(r"[0-9A-Fa-f]{1,4096}", color):
                    raise ThumbnailError("Unsupported painted triangle encoding")
                if mapping or base_slot != 1:
                    original = color or "0"
                    paint_key = (original, base_slot)
                    if paint_key not in remapped:
                        remapped[paint_key] = _remap_paint(original, mapping or tuple(range(1, slots + 1)), base_slot)
                    color = remapped[paint_key]
                if color:
                    indices["paint_color"] = color
                XML.SubElement(clean_triangles, "triangle", indices)
            return new_id

        build = XML.Element("build")
        for path, object_id, matrix in items:
            obj = add_object(path, object_id, object_id)
            for _ in range(copies):
                XML.SubElement(build, "item", objectid=obj, **transform(matrix))
        model.append(build)
        destination.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as clean:
            clean.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
                           '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
                           '<Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>'
                           '</Types>')
            clean.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                           '<Relationship Target="/3D/3dmodel.model" Id="rel0" '
                           'Type="http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"/></Relationships>')
            with clean.open("3D/3dmodel.model", "w") as output:
                XML.ElementTree(model).write(output, encoding="utf-8", xml_declaration=True)
        return target_palette


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
