import io
import struct
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
from conftest import binary_stl
from PIL import Image

from homeslice.library.thumbnails import SIZE, ThumbnailError, generate, load_obj, load_stl, render


def cube_stl(path: Path) -> Path:
    v = np.array([[x, y, z] for x in (0, 10) for y in (0, 10) for z in (0, 10)], dtype=np.float32)
    faces = [(0, 1, 3), (0, 3, 2), (4, 6, 7), (4, 7, 5), (0, 4, 5), (0, 5, 1), (2, 3, 7), (2, 7, 6), (0, 2, 6), (0, 6, 4), (1, 5, 7), (1, 7, 3)]
    body = b"".join(struct.pack("<3f", 0, 0, 0) + v[list(f)].tobytes() + b"\0\0" for f in faces)
    path.write_bytes(b"\0" * 80 + struct.pack("<I", len(faces)) + body)
    return path


def opaque_pixels(path: Path) -> int:
    img = Image.open(path)
    assert img.size == (SIZE, SIZE) and img.mode == "RGBA"
    return int((np.array(img)[:, :, 3] > 0).sum())


def test_stl_render(tmp_path):
    src = cube_stl(tmp_path / "cube.stl")
    assert load_stl(src).shape == (12, 3, 3)
    assert generate(src, ".stl", tmp_path / "out.png")
    # Visible object, transparent background corners.
    assert opaque_pixels(tmp_path / "out.png") > SIZE * SIZE * 0.1
    assert Image.open(tmp_path / "out.png").getpixel((0, 0))[3] == 0


def test_ascii_stl_and_obj(tmp_path):
    (tmp_path / "a.stl").write_text(
        "solid t\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 10 0 0\nvertex 0 10 5\nendloop\nendfacet\nendsolid t\n"
    )
    assert load_stl(tmp_path / "a.stl").shape == (1, 3, 3)
    (tmp_path / "a.obj").write_text("v 0 0 0\nv 10 0 0\nv 10 10 0\nv 0 10 5\nf 1 2 3 4\n")
    assert load_obj(tmp_path / "a.obj").shape == (2, 3, 3)
    assert generate(tmp_path / "a.obj", ".obj", tmp_path / "o.png")


def model_xml(objects: str, items: str) -> bytes:
    return (
        '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02" '
        'xmlns:p="http://schemas.microsoft.com/3dmanufacturing/production/2015/06">'
        f"<resources>{objects}</resources><build>{items}</build></model>"
    ).encode()


TRI = (
    '<mesh><vertices><vertex x="0" y="0" z="0"/><vertex x="10" y="0" z="0"/><vertex x="0" y="10" z="10"/></vertices>'
    '<triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh>'
)


def test_3mf_with_components_in_other_part(tmp_path):
    src = tmp_path / "m.3mf"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("3D/Objects/object_1.model", model_xml(f'<object id="1">{TRI}</object>', ""))
        zf.writestr(
            "3D/3dmodel.model",
            model_xml(
                '<object id="2"><components><component p:path="/3D/Objects/object_1.model" objectid="1" '
                'transform="1 0 0 0 1 0 0 0 1 50 0 0"/></components></object>',
                '<item objectid="2"/>',
            ),
        )
    assert generate(src, ".3mf", tmp_path / "t.png") and opaque_pixels(tmp_path / "t.png") > 1000


def test_3mf_prefers_embedded_preview(tmp_path):
    buf = io.BytesIO()
    Image.new("RGBA", (300, 200), (255, 0, 0, 255)).save(buf, "PNG")
    src = tmp_path / "e.3mf"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("3D/3dmodel.model", model_xml("", ""))
        zf.writestr("Metadata/plate_1.png", buf.getvalue())
    assert generate(src, ".3mf", tmp_path / "e.png")
    assert Image.open(tmp_path / "e.png").getpixel((SIZE // 2, SIZE // 2)) == (255, 0, 0, 255)


def test_3mf_xml_entities_rejected(tmp_path):
    src = tmp_path / "x.3mf"
    evil = b'<?xml version="1.0"?><!DOCTYPE m [<!ENTITY e SYSTEM "file:///etc/passwd">]><model>&e;</model>'
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("3D/3dmodel.model", evil)
    with pytest.raises(Exception) as info:
        generate(src, ".3mf", tmp_path / "x.png")
    assert "Entities" in type(info.value).__name__ or "Forbidden" in type(info.value).__name__
    assert not (tmp_path / "x.png").exists()


def test_step_has_no_thumbnail(tmp_path):
    (tmp_path / "a.step").write_text("ISO-10303-21;")
    assert generate(tmp_path / "a.step", ".step", tmp_path / "s.png") is False


def test_degenerate_mesh():
    with pytest.raises(ThumbnailError):
        render(np.zeros((3, 3, 3)))


def wait_for_thumbnails(services) -> None:
    # The app runs on TestClient's own event loop, so poll instead of awaiting its tasks.
    for _ in range(400):
        if not services.library.thumbnails.busy:
            return
        time.sleep(0.05)
    raise AssertionError("thumbnail generation did not finish")


def test_upload_of_degenerate_model_has_no_thumbnail(authed):
    r = authed.post("/api/library", params={"filename": "m.stl"}, content=binary_stl(1))
    model_id = r.json()["id"]
    wait_for_thumbnails(authed.app.state.services)
    # binary_stl(1) is one zero-area triangle, so nothing can be drawn.
    assert authed.get(f"/api/library/{model_id}/thumbnail").status_code == 404


def test_thumbnail_endpoint_and_cleanup(authed, tmp_path):
    services = authed.app.state.services
    content = cube_stl(tmp_path / "c.stl").read_bytes()
    model_id = authed.post("/api/library", params={"filename": "cube.stl"}, content=content).json()["id"]
    wait_for_thumbnails(services)
    path = services.library.thumbnails.path(model_id)
    assert authed.get("/api/library").json()[0]["has_thumbnail"] is True
    r = authed.get(f"/api/library/{model_id}/thumbnail")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    authed.delete(f"/api/library/{model_id}")
    assert not path.exists()
