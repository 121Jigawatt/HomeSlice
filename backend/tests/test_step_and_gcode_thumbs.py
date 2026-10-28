import asyncio
import base64
import json
import zipfile

import numpy as np
import pytest
from conftest import binary_stl
from PIL import Image

from homeslice.library import step as step_module
from homeslice.library.step import StepError, write_binary_stl
from homeslice.library.thumbnails import load_stl
from homeslice.orca import gcode_thumbs

CUBE = np.array(
    [
        [[0, 0, 0], [10, 0, 0], [10, 10, 0]],
        [[0, 0, 0], [10, 10, 0], [0, 10, 0]],
        [[0, 0, 5], [10, 10, 5], [10, 0, 5]],
        [[0, 0, 5], [0, 10, 5], [10, 10, 5]],
        [[0, 0, 0], [0, 0, 5], [10, 0, 5]],
        [[0, 0, 0], [10, 0, 5], [10, 0, 0]],
    ],
    dtype=np.float32,
)


def test_write_binary_stl_roundtrip(tmp_path):
    p = tmp_path / "c.stl"
    write_binary_stl(p, CUBE)
    assert np.allclose(load_stl(p), CUBE)


def test_step_conversion_reports_bad_files(tmp_path):
    bad = tmp_path / "bad.step"
    bad.write_text("ISO-10303-21;\nthis is not a real STEP file\n")
    with pytest.raises(StepError):
        step_module.convert(bad, tmp_path / "out.stl", timeout=120)
    assert not (tmp_path / "out.stl").exists()


def test_step_model_sliced_and_previewed_from_converted_mesh(authed, settings, monkeypatch):
    calls = []

    def fake_convert(source, dest, timeout=0):
        calls.append(source)
        write_binary_stl(dest, CUBE)

    monkeypatch.setattr("homeslice.library.service.step_convert", fake_convert)
    step = b"ISO-10303-21;\nHEADER;\nENDSEC;\nDATA;\nENDSEC;\nEND-ISO-10303-21;\n"
    r = authed.post("/api/library", params={"filename": "Bracket.step"}, content=step)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    services = authed.app.state.services
    for _ in range(100):
        if not services.library.thumbnails.busy:
            break
        authed.portal.call(asyncio.sleep, 0.05)
    assert authed.get("/api/library").json()[0]["has_thumbnail"] is True

    mesh = authed.get(f"/api/library/{mid}/mesh")
    assert mesh.status_code == 200 and mesh.content[80:84] == np.uint32(len(CUBE)).tobytes()
    assert services.library.mesh_path(mid).name == f"{mid}.mesh.stl"
    assert len(calls) == 1  # converted once, then cached

    assert authed.delete(f"/api/library/{mid}").status_code == 204
    assert not (settings.library_dir / f"{mid}.mesh.stl").exists()


def test_mesh_endpoint_returns_original_for_stl(authed):
    mid = authed.post("/api/library", params={"filename": "a.stl"}, content=binary_stl()).json()["id"]
    assert authed.get(f"/api/library/{mid}/mesh").content == binary_stl()


def test_backfill_retries_missing_thumbnails(authed, settings):
    services = authed.app.state.services
    mid = authed.post("/api/library", params={"filename": "a.stl"}, content=binary_stl()).json()["id"]
    for _ in range(100):
        if not services.library.thumbnails.busy:
            break
        authed.portal.call(asyncio.sleep, 0.05)
    services.library.thumbnails.delete(mid)
    assert services.library.thumbnails.missing([{"id": mid, "ext": ".stl"}]) == [{"id": mid, "ext": ".stl"}]


def _plate_3mf(path):
    model = (
        '<?xml version="1.0"?><model xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02"><resources>'
        '<object id="1" type="model"><mesh><vertices>'
        + "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in CUBE.reshape(-1, 3))
        + "</vertices><triangles>"
        + "".join(f'<triangle v1="{3 * i}" v2="{3 * i + 1}" v3="{3 * i + 2}"/>' for i in range(len(CUBE)))
        + '</triangles></mesh></object></resources><build><item objectid="1"/></build></model>'
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", model)


def test_thumbnail_sizes_from_profile(tmp_path):
    p = tmp_path / "printer.json"
    p.write_text(json.dumps({"thumbnails": ["300x300", "400x300", "32x32"]}))
    assert gcode_thumbs.thumbnail_sizes(p) == [(300, 300), (400, 300), (32, 32)]
    p.write_text(json.dumps({"thumbnails": "48x48/PNG, 300x300/PNG"}))
    assert gcode_thumbs.thumbnail_sizes(p) == [(48, 48), (300, 300)]
    p.write_text(json.dumps({"thumbnails": "99999x2"}))
    assert gcode_thumbs.thumbnail_sizes(p) == gcode_thumbs.DEFAULT_SIZES


def test_embeds_thumbnails_after_header(tmp_path):
    plate = tmp_path / "result.gcode.3mf"
    _plate_3mf(plate)
    printer = tmp_path / "printer.json"
    printer.write_text(json.dumps({"thumbnails": "48x48/PNG, 300x300/PNG"}))
    gcode = tmp_path / "plate_1.gcode"
    gcode.write_text("; HEADER_BLOCK_START\n; total layer number: 1\n; HEADER_BLOCK_END\n\nG28\nG1 X1 E1\n")

    assert gcode_thumbs.embed(gcode, plate, printer) is True
    text = gcode.read_text()
    assert text.index("; HEADER_BLOCK_END") < text.index("; THUMBNAIL_BLOCK_START") < text.index("G28")
    blocks = text.split("; thumbnail begin ")[1:]
    assert [b.split(" ", 1)[0] for b in blocks] == ["48x48", "300x300"]
    head, rest = blocks[1].split("\n", 1)
    data = "".join(line[2:] for line in rest.split("; thumbnail end")[0].splitlines())
    assert len(data) == int(head.split()[1])
    img = Image.open(__import__("io").BytesIO(base64.b64decode(data)))
    assert img.size == (300, 300) and img.getbbox() is not None
    assert text.endswith("G28\nG1 X1 E1\n")

    # Already has thumbnails: left alone.
    assert gcode_thumbs.embed(gcode, plate, printer) is False
    assert gcode.read_text().count("; THUMBNAIL_BLOCK_START") == 1
