import asyncio
import uuid
import zipfile

import homeslice.orca.jobs as jobs_module
import numpy as np
import pytest
from test_jobs_m3 import add_presets

from homeslice.app import build_services
from homeslice.library import threemf
from homeslice.library.thumbnails import ThumbnailError, load_stl
from homeslice.orca.cli import RunResult
from homeslice.orca.jobs import JobRequest

NS = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
P = "http://schemas.microsoft.com/3dmanufacturing/production/2015/06"
TRI = [(0, 0, 0), (10, 0, 0), (0, 10, 0)]


def _object_model(oid: int) -> str:
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in TRI)
    return (
        f'<?xml version="1.0"?><model xmlns="{NS}"><resources><object id="{oid}" type="model"><mesh>'
        f'<vertices>{verts}</vertices><triangles><triangle v1="0" v2="1" v3="2"/></triangles></mesh></object>'
        "</resources><build/></model>"
    )


def bambu_project(path, with_bad_settings=True):
    """Shaped like Bambu Studio output: objects in separate files referenced via components, plates in metadata."""
    resources, items, plates = [], [], []
    for n, oid in enumerate((2, 3, 4)):
        resources.append(
            f'<object id="{oid + 10}" type="model"><components>'
            f'<component p:path="/3D/Objects/object_{oid}.model" objectid="{oid}"/></components></object>'
        )
        items.append(f'<item objectid="{oid + 10}" transform="1 0 0 0 1 0 0 0 1 {100 * n} 0 0"/>')
    root = (
        f'<?xml version="1.0"?><model xmlns="{NS}" xmlns:p="{P}" requiredextensions="p"><resources>'
        + "".join(resources)
        + "</resources><build>"
        + "".join(items)
        + "</build></model>"
    )
    settings = (
        '<?xml version="1.0"?><config>'
        '<plate><metadata key="plater_id" value="1"/><metadata key="plater_name" value="20 mm sx"/>'
        '<model_instance><metadata key="object_id" value="12"/></model_instance></plate>'
        '<plate><metadata key="plater_id" value="2"/><metadata key="plater_name" value="bicolor"/>'
        '<model_instance><metadata key="object_id" value="13"/></model_instance>'
        '<model_instance><metadata key="object_id" value="14"/></model_instance></plate>'
        "</config>"
    )
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", root)
        for oid in (2, 3, 4):
            zf.writestr(f"3D/Objects/object_{oid}.model", _object_model(oid))
        zf.writestr("Metadata/model_settings.config", settings)
        if with_bad_settings:
            zf.writestr("Metadata/project_settings.config", '{"raft_first_layer_expansion": "-1"}')
    return path


def test_plates_and_meshes(tmp_path):
    src = bambu_project(tmp_path / "ring.3mf")
    assert threemf.plates(src) == [
        {"index": 1, "name": "20 mm sx", "object_ids": ["12"]},
        {"index": 2, "name": "bicolor", "object_ids": ["13", "14"]},
    ]
    assert len(threemf.item_meshes(src)) == 3
    plate2 = threemf.item_meshes(src, 2)
    assert len(plate2) == 2
    # World-space: the item transforms (x offsets 100 and 200) are applied.
    assert sorted(round(float(m.reshape(-1, 3)[:, 0].min())) for m in plate2) == [100, 200]
    with pytest.raises(ThumbnailError):
        threemf.item_meshes(src, 7)

    paths = threemf.write_items(src, tmp_path / "items", 2)
    assert [p.name for p in paths] == ["object_1.stl", "object_2.stl"]
    assert np.allclose(load_stl(paths[0]).reshape(-1, 3)[:, 0].min(), 100)


def test_plain_3mf_has_no_plates(tmp_path):
    src = tmp_path / "plain.3mf"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("3D/3dmodel.model", _object_model(1).replace("<build/>", '<build><item objectid="1"/></build>'))
    assert threemf.plates(src) == []
    assert len(threemf.item_meshes(src)) == 1


def test_library_exposes_plates_and_preview_mesh(authed, tmp_path):
    data = bambu_project(tmp_path / "ring.3mf").read_bytes()
    r = authed.post("/api/library", params={"filename": "Ring.3mf"}, content=data)
    assert r.status_code == 201, r.text
    mid = r.json()["id"]
    model = next(m for m in authed.get("/api/library").json() if m["id"] == mid)
    assert model["plates"] == [{"index": 1, "name": "20 mm sx", "objects": 1}, {"index": 2, "name": "bicolor", "objects": 2}]

    all_plates = authed.get(f"/api/library/{mid}/mesh")
    plate2 = authed.get(f"/api/library/{mid}/mesh", params={"plate": 2})
    assert all_plates.status_code == plate2.status_code == 200
    count = lambda b: int(np.frombuffer(b[80:84], "<u4")[0])  # noqa: E731
    assert (count(all_plates.content), count(plate2.content)) == (3, 2)
    assert authed.get(f"/api/library/{mid}/mesh", params={"plate": 9}).status_code == 422


def test_3mf_sliced_as_objects_without_project_settings(settings):
    services = build_services(settings)
    try:
        add_presets(settings)
        mid = uuid.uuid4().hex
        services.db.execute(
            "INSERT INTO models(id, filename, ext, size, sha256, created_at) VALUES(?, 'Ring.3mf', '.3mf', 1, 'x', 0)",
            (mid,),
        )
        bambu_project(settings.library_dir / f"{mid}.3mf")
        job_id = services.jobs.submit(JobRequest([mid], "P", "Q", ["F"], arrange=False, copies=[2], model_plates=[2]))
        assert services.jobs.get(job_id)["title"] == "Ring.3mf (bicolor) \u00d72"
        seen: list[list[str]] = []

        async def fake_run(args, cwd, timeout, memory):
            seen.append(args)
            return RunResult(1, False, "")

        services.versions.current_binary = lambda: settings.data_dir / "AppRun"
        original = jobs_module.run_orca
        jobs_module.run_orca = fake_run
        try:
            asyncio.run(services.jobs._run(job_id))
        finally:
            jobs_module.run_orca = original
        args = seen[0]
        models = [a for a in args if a.endswith((".stl", ".3mf")) and a != "result.gcode.3mf"]
        assert len(models) == 4 and all(a.endswith(".stl") for a in models)  # 2 objects x 2 copies
        assert "--arrange" in args  # forced: the objects no longer share a layout
        with pytest.raises(Exception):
            services.jobs.submit(JobRequest([mid], "P", "Q", ["F"], model_plates=[5]))
    finally:
        services.db.close()
