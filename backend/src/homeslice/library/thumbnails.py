"""Server-side model thumbnails: embedded 3MF previews when present, otherwise a flat-shaded render."""

import asyncio
import io
import logging
import os
import re
import uuid
import zipfile
from pathlib import Path, PurePosixPath

import numpy as np
from defusedxml import ElementTree
from PIL import Image

log = logging.getLogger(__name__)

SIZE = 512
SUPERSAMPLE = 2
# Hard cap on triangles considered; above this the mesh is strided (only for absurd files).
MAX_TRIANGLES = 20_000_000
TRI_CHUNK = 250_000
POINT_CHUNK = 4_000_000
# Sample spacing in supersampled pixels; < 1 keeps surfaces hole-free.
SPACING = 0.7
# Embedded previews darker than this (mean luminance of opaque pixels) vanish on the dark UI.
MIN_EMBEDDED_LUMA = 60
# STLs are memory-mapped and subsampled, so only XML/OBJ parsing needs a tighter limit.
MAX_SOURCE_BYTES = 2 * 1024**3
MAX_PARSED_BYTES = 300 * 1024**2
MAX_EMBEDDED_BYTES = 10 * 1024**2
MAX_COMPONENT_DEPTH = 8
# Teal accent (#14b8a6) lit from the camera side.
BASE = np.array([20, 184, 166], dtype=np.float64)
HIGHLIGHT = np.array([153, 246, 228], dtype=np.float64)

_NS = "{http://schemas.microsoft.com/3dmanufacturing/core/2015/02}"
_PATH_ATTR = "{http://schemas.microsoft.com/3dmanufacturing/production/2015/06}path"
_EMBEDDED = ("Metadata/plate_1.png", "Metadata/thumbnail.png", "Metadata/top_1.png")
_STL_RECORD = np.dtype([("normal", "<f4", 3), ("v", "<f4", (3, 3)), ("attr", "<u2")])
_FLOAT = r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?"
_ASCII_VERTEX = re.compile(rf"vertex\s+({_FLOAT})\s+({_FLOAT})\s+({_FLOAT})".encode())


class ThumbnailError(Exception):
    pass


def _cap(tris: np.ndarray) -> np.ndarray:
    if len(tris) > MAX_TRIANGLES:
        tris = tris[:: int(np.ceil(len(tris) / MAX_TRIANGLES))]
    return tris


def load_stl(path: Path) -> np.ndarray:
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(84)
    if len(head) == 84:
        count = int.from_bytes(head[80:84], "little")
        if 84 + 50 * count == size and count > 0:
            records = np.memmap(path, dtype=_STL_RECORD, mode="r", offset=84, shape=(count,))
            return _cap(records["v"])
    data = path.read_bytes()
    coords = np.array(_ASCII_VERTEX.findall(data), dtype=np.float64)
    if len(coords) < 3:
        raise ThumbnailError("No triangles in STL")
    return _cap(coords[: len(coords) // 3 * 3].reshape(-1, 3, 3))


def load_obj(path: Path) -> np.ndarray:
    vertices: list[tuple[float, float, float]] = []
    faces: list[tuple[int, int, int]] = []
    with path.open("rb") as f:
        for raw in f:
            parts = raw.split()
            if not parts:
                continue
            if parts[0] == b"v" and len(parts) >= 4:
                vertices.append((float(parts[1]), float(parts[2]), float(parts[3])))
            elif parts[0] == b"f" and len(parts) >= 4:
                idx = []
                for p in parts[1:]:
                    i = int(p.split(b"/")[0])
                    idx.append(i - 1 if i > 0 else len(vertices) + i)
                faces.extend((idx[0], idx[k], idx[k + 1]) for k in range(1, len(idx) - 1))
                if len(faces) > MAX_TRIANGLES * 4:
                    break
    if not faces:
        raise ThumbnailError("No faces in OBJ")
    v = np.array(vertices, dtype=np.float64)
    f = np.array(faces, dtype=np.int64)
    f = f[(f >= 0).all(axis=1) & (f < len(v)).all(axis=1)]
    return _cap(v[f])


def _matrix(value: str | None) -> np.ndarray | None:
    if not value:
        return None
    nums = [float(x) for x in value.split()]
    if len(nums) != 12:
        return None
    return np.array(nums, dtype=np.float64).reshape(4, 3)


def _apply(tris: np.ndarray, m: np.ndarray | None) -> np.ndarray:
    return tris if m is None else tris @ m[:3] + m[3]


class _ThreeMF:
    def __init__(self, zf: zipfile.ZipFile) -> None:
        self._zf = zf
        self._models: dict[str, tuple[dict, list]] = {}
        self._count = 0

    def _read(self, name: str) -> tuple[dict, list]:
        name = name.lstrip("/")
        if name not in self._models:
            info = self._zf.getinfo(name)
            if info.file_size > MAX_PARSED_BYTES:
                raise ThumbnailError("Model part too large")
            root = ElementTree.fromstring(self._zf.read(name))
            objects = {}
            for obj in root.iter(f"{_NS}object"):
                mesh = obj.find(f"{_NS}mesh")
                comps = [
                    (c.get(_PATH_ATTR) or name, c.get("objectid"), _matrix(c.get("transform")))
                    for c in obj.iter(f"{_NS}component")
                ]
                objects[obj.get("id")] = (mesh, comps)
            items = [
                (i.get(_PATH_ATTR) or name, i.get("objectid"), _matrix(i.get("transform")))
                for i in root.iter(f"{_NS}item")
            ]
            self._models[name] = (objects, items)
        return self._models[name]

    def _mesh(self, mesh) -> np.ndarray:
        verts = mesh.find(f"{_NS}vertices")
        tris = mesh.find(f"{_NS}triangles")
        if verts is None or tris is None:
            return np.empty((0, 3, 3))
        v = np.array([(float(e.get("x")), float(e.get("y")), float(e.get("z"))) for e in verts], dtype=np.float64)
        t = np.array([(int(e.get("v1")), int(e.get("v2")), int(e.get("v3"))) for e in tris], dtype=np.int64)
        if not len(v) or not len(t):
            return np.empty((0, 3, 3))
        t = t[(t >= 0).all(axis=1) & (t < len(v)).all(axis=1)]
        return v[t]

    def _object(self, path: str, oid: str | None, depth: int) -> list[np.ndarray]:
        if depth > MAX_COMPONENT_DEPTH or self._count > MAX_TRIANGLES * 4:
            return []
        objects, _ = self._read(path)
        mesh, comps = objects.get(oid, (None, []))
        parts = []
        if mesh is not None:
            tris = self._mesh(mesh)
            self._count += len(tris)
            parts.append(tris)
        for cpath, cid, m in comps:
            parts.extend(_apply(p, m) for p in self._object(cpath, cid, depth + 1))
        return parts

    def triangles(self, root_name: str) -> np.ndarray:
        _, items = self._read(root_name)
        parts = []
        for path, oid, m in items:
            parts.extend(_apply(p, m) for p in self._object(path, oid, 0))
        if not parts:
            raise ThumbnailError("No meshes in 3MF")
        return _cap(np.concatenate(parts))


def _root_model(zf: zipfile.ZipFile) -> str:
    try:
        rels = ElementTree.fromstring(zf.read("_rels/.rels"))
        for rel in rels:
            if rel.get("Type", "").endswith("/3dmodel"):
                return rel.get("Target", "").lstrip("/")
    except (KeyError, ElementTree.ParseError):
        pass
    return "3D/3dmodel.model"


def _embedded(zf: zipfile.ZipFile) -> Image.Image | None:
    names = set(zf.namelist())
    for name in _EMBEDDED:
        if name in names and zf.getinfo(name).file_size <= MAX_EMBEDDED_BYTES:
            try:
                img = Image.open(io.BytesIO(zf.read(name)))
                if img.width * img.height > 4096 * 4096:
                    continue
                img = _fit(img.convert("RGBA"))
            except (OSError, ValueError):
                continue
            px = np.asarray(img, dtype=np.float64)
            mask = px[:, :, 3] > 0
            luma = px[:, :, :3] @ [0.299, 0.587, 0.114]
            if mask.any() and luma[mask].mean() < MIN_EMBEDDED_LUMA:
                return _tint(px, luma, mask)
            return img
    return None


def _tint(px: np.ndarray, luma: np.ndarray, mask: np.ndarray) -> Image.Image:
    """Recolours a dark preview (e.g. black filament) in the theme accent, keeping its shading."""
    lo, hi = np.percentile(luma[mask], [2, 98])
    shade = ((luma - lo) / max(hi - lo, 1.0)).clip(0, 1)[:, :, None]
    rgb = (BASE * (0.35 + 0.45 * shade) + HIGHLIGHT * (0.2 * shade**4)).clip(0, 255)
    out = np.dstack([rgb, px[:, :, 3]]).astype(np.uint8)
    return Image.fromarray(out, "RGBA")


def _fit(img: Image.Image) -> Image.Image:
    img.thumbnail((SIZE, SIZE), Image.LANCZOS)
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    canvas.paste(img, ((SIZE - img.width) // 2, (SIZE - img.height) // 2))
    return canvas


def _camera() -> tuple[np.ndarray, np.ndarray]:
    # Front-right-top view, Z up (printer coordinates). Columns map a point to (x, y, depth).
    eye = np.array([1.0, -1.3, 1.0])
    forward = -eye / np.linalg.norm(eye)
    right = np.cross(forward, [0.0, 0.0, 1.0])
    right /= np.linalg.norm(right)
    up = np.cross(right, forward)
    light = -forward + np.array([0.0, 0.0, 0.6])
    return np.stack([right, up, forward], axis=1), light / np.linalg.norm(light)


_GRIDS: dict[int, np.ndarray] = {}


def _grid(k: int) -> np.ndarray:
    """Barycentric sample weights covering a triangle with k steps per edge."""
    if (g := _GRIDS.get(k)) is not None:
        return g
    i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
    mask = (i + j) <= k
    a, b = i[mask] / k, j[mask] / k
    g = np.stack([a, b, 1 - a - b], axis=1)
    if k <= 64:
        _GRIDS[k] = g
    return g


def _chunks(tris: np.ndarray):
    for start in range(0, len(tris), TRI_CHUNK):
        chunk = np.asarray(tris[start : start + TRI_CHUNK], dtype=np.float64)
        yield chunk[np.isfinite(chunk).all(axis=(1, 2))]


def render(tris: np.ndarray) -> Image.Image:
    """Z-buffered point-splat render using every triangle; works on memory-mapped meshes chunk by chunk."""
    proj, light = _camera()
    lo, hi = np.full(3, np.inf), np.full(3, -np.inf)
    for chunk in _chunks(tris):
        if len(chunk):
            p = chunk.reshape(-1, 3) @ proj
            lo, hi = np.minimum(lo, p.min(axis=0)), np.maximum(hi, p.max(axis=0))
    if not np.isfinite(lo).all():
        raise ThumbnailError("Empty mesh")
    extent = max(hi[0] - lo[0], hi[1] - lo[1])
    if extent <= 0:
        raise ThumbnailError("Degenerate mesh")
    size = SIZE * SUPERSAMPLE
    scale = size * 0.88 / extent
    cx, cy = (hi[0] + lo[0]) / 2, (hi[1] + lo[1]) / 2
    zbuf = np.full(size * size, np.inf)
    rgb = np.zeros((size * size, 3))

    for chunk in _chunks(tris):
        normals = np.cross(chunk[:, 1] - chunk[:, 0], chunk[:, 2] - chunk[:, 0])
        lengths = np.linalg.norm(normals, axis=1)
        chunk, normals, lengths = chunk[lengths > 0], normals[lengths > 0], lengths[lengths > 0]
        if not len(chunk):
            continue
        shade = np.abs((normals / lengths[:, None]) @ light)[:, None]
        colors = (BASE * (0.35 + 0.45 * shade) + HIGHLIGHT * (0.2 * shade**4)).clip(0, 255)
        p = chunk @ proj
        screen = np.stack(
            [size / 2 + (p[..., 0] - cx) * scale, size / 2 - (p[..., 1] - cy) * scale, p[..., 2]], axis=-1
        )
        edges = np.linalg.norm(screen[:, [1, 2, 0], :2] - screen[:, :, :2], axis=2).max(axis=1)
        steps = np.clip(np.ceil(edges / SPACING), 1, size * 2).astype(np.int64)
        for k in np.unique(steps):
            group = np.nonzero(steps == k)[0]
            grid = _grid(int(k))
            per = max(1, POINT_CHUNK // len(grid))
            for s in range(0, len(group), per):
                idx = group[s : s + per]
                pts = np.einsum("pv,mvc->mpc", grid, screen[idx]).reshape(-1, 3)
                col = np.repeat(colors[idx], len(grid), axis=0)
                ix, iy = np.rint(pts[:, 0]).astype(np.int64), np.rint(pts[:, 1]).astype(np.int64)
                inside = (ix >= 0) & (ix < size) & (iy >= 0) & (iy < size)
                flat, z, col = (iy * size + ix)[inside], pts[inside, 2], col[inside]
                np.minimum.at(zbuf, flat, z)
                won = z <= zbuf[flat]
                rgb[flat[won]] = col[won]

    alpha = np.where(np.isfinite(zbuf), 255, 0).astype(np.uint8)
    pixels = np.dstack([rgb.reshape(size, size, 3).astype(np.uint8), alpha.reshape(size, size)])
    return Image.fromarray(pixels, "RGBA").resize((SIZE, SIZE), Image.LANCZOS)


def generate(source: Path, ext: str, dest: Path) -> bool:
    """Writes dest atomically. Returns False for formats that can't be previewed (e.g. STEP)."""
    if source.stat().st_size > MAX_SOURCE_BYTES:
        return False
    if ext == ".3mf":
        with zipfile.ZipFile(source) as zf:
            image = _embedded(zf) or render(_ThreeMF(zf).triangles(_root_model(zf)))
    elif ext == ".stl":
        image = render(load_stl(source))
    elif ext == ".obj":
        if source.stat().st_size > MAX_PARSED_BYTES:
            return False
        image = render(load_obj(source))
    else:
        return False
    tmp = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex}.tmp")
    try:
        image.save(tmp, "PNG", optimize=True)
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)
    return True


class Thumbnails:
    def __init__(self, thumbs_dir: Path) -> None:
        self._dir = thumbs_dir
        self._sem = asyncio.Semaphore(1)
        self._tasks: set[asyncio.Task] = set()

    def path(self, model_id: str) -> Path:
        return self._dir / f"{model_id}.png"

    def exists(self, model_id: str) -> bool:
        return self.path(model_id).is_file()

    def schedule(self, model_id: str, source: Path, ext: str) -> None:
        task = asyncio.create_task(self._make(model_id, source, ext))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _make(self, model_id: str, source: Path, ext: str) -> None:
        async with self._sem:
            try:
                await asyncio.to_thread(generate, source, ext, self.path(model_id))
            except Exception as e:  # noqa: BLE001 - a bad model must never break uploads
                log.info("No thumbnail for %s: %s", model_id, e)

    @property
    def busy(self) -> bool:
        return bool(self._tasks)

    def delete(self, model_id: str) -> None:
        self.path(model_id).unlink(missing_ok=True)

    def missing(self, models: list[dict]) -> list[dict]:
        return [m for m in models if not self.exists(m["id"]) and PurePosixPath(m["ext"]).suffix in (".stl", ".3mf", ".obj")]
