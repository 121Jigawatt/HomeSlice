"""Embeds plate preview images into sliced G-code.

OrcaSlicer's CLI needs an OpenGL context to draw thumbnails and skips them in the headless container, so
Mainsail/Fluidd/KlipperScreen show no preview. This renders the arranged plate from the exported 3MF and writes
standard "; thumbnail begin WxH len" PNG blocks at the sizes the printer profile asks for.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
import re
import uuid
import zipfile
from pathlib import Path

import numpy as np
from PIL import Image

from ..library import threemf
from ..library.thumbnails import _root_model, _ThreeMF, render

log = logging.getLogger(__name__)

DEFAULT_SIZES = [(32, 32), (300, 300)]
MAX_SIDE = 1024
LINE = 78
_SIZE_RE = re.compile(r"(\d{1,4})\s*x\s*(\d{1,4})")


def thumbnail_sizes(printer_json: Path) -> list[tuple[int, int]]:
    """Sizes from the printer profile's `thumbnails` (\"300x300\" list or \"48x48/PNG, 300x300/PNG\" string)."""
    try:
        value = json.loads(printer_json.read_text(encoding="utf-8")).get("thumbnails")
    except (OSError, ValueError):
        value = None
    items = value if isinstance(value, list) else str(value or "").split(",")
    sizes: list[tuple[int, int]] = []
    for item in items:
        if m := _SIZE_RE.search(str(item)):
            w, h = int(m.group(1)), int(m.group(2))
            if 0 < w <= MAX_SIDE and 0 < h <= MAX_SIDE and (w, h) not in sizes:
                sizes.append((w, h))
    return sizes or DEFAULT_SIZES


def _fit(image: Image.Image, w: int, h: int) -> Image.Image:
    box = image.getbbox()
    if box:
        image = image.crop(box)
    pad = max(1, round(min(w, h) * 0.04))
    image.thumbnail((max(1, w - 2 * pad), max(1, h - 2 * pad)), Image.LANCZOS)
    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    canvas.paste(image, ((w - image.width) // 2, (h - image.height) // 2))
    return canvas


def thumbnail_block(image: Image.Image, sizes: list[tuple[int, int]]) -> str:
    parts = ["; THUMBNAIL_BLOCK_START"]
    for w, h in sizes:
        buf = io.BytesIO()
        _fit(image.copy(), w, h).save(buf, "PNG", optimize=True)
        data = base64.b64encode(buf.getvalue()).decode("ascii")
        parts += [";", f"; thumbnail begin {w}x{h} {len(data)}"]
        parts += [f"; {data[i:i + LINE]}" for i in range(0, len(data), LINE)]
        parts += ["; thumbnail end", ";"]
    parts.append("; THUMBNAIL_BLOCK_END")
    return "\n".join(parts) + "\n"


def _has_thumbnails(gcode: Path) -> bool:
    with gcode.open("rb") as f:
        return b"; thumbnail begin" in f.read(512 * 1024)


def embed(gcode: Path, plate_3mf: Path, printer_json: Path, plate: int = 0) -> bool:
    """Adds preview images to gcode in place, drawing only `plate` of a multi-bed export when given.

    Returns False when it already had them or nothing could be drawn.
    """
    if _has_thumbnails(gcode) or not plate_3mf.is_file():
        return False
    if plate and len(threemf.plates(plate_3mf)) > 1:
        image = render(np.concatenate(threemf.item_meshes(plate_3mf, plate)))
    else:
        with zipfile.ZipFile(plate_3mf) as zf:
            image = render(_ThreeMF(zf).triangles(_root_model(zf)))
    block = thumbnail_block(image, thumbnail_sizes(printer_json)).encode("ascii")

    tmp = gcode.with_name(f"{gcode.name}.{uuid.uuid4().hex}.tmp")
    try:
        with gcode.open("rb") as src, tmp.open("wb") as dst:
            # Right after Orca's header block, where slicers normally put thumbnails; else at the very top.
            head = src.read(64 * 1024)
            marker = head.find(b"; HEADER_BLOCK_END")
            if marker >= 0:
                cut = head.find(b"\n", marker) + 1 or len(head)
                dst.write(head[:cut] + block + head[cut:])
            else:
                dst.write(block + head)
            while chunk := src.read(1024 * 1024):
                dst.write(chunk)
        os.replace(tmp, gcode)
    finally:
        tmp.unlink(missing_ok=True)
    return True
