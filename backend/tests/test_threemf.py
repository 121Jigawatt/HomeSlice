import asyncio
import json
import uuid
import zipfile
from pathlib import Path

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


@pytest.mark.parametrize("mixed", [False, True])
def test_painted_project_slices_with_target_profiles(settings, monkeypatch, mixed):
    services = build_services(settings)
    try:
        add_presets(settings)
        model_id = uuid.uuid4().hex
        services.db.execute(
            "INSERT INTO models(id, filename, ext, size, sha256, created_at) VALUES(?, 'Painted.3mf', '.3mf', 1, 'x', 0)",
            (model_id,),
        )
        source = settings.library_dir / f"{model_id}.3mf"
        root = (
            f'<model xmlns="{NS}" xmlns:p="{P}"><resources><object id="2" type="model"><components>'
            '<component objectid="1" p:path="/3D/Objects/painted.model"/></components></object></resources>'
            '<build><item objectid="2" transform="1 0 0 0 1 0 0 0 1 120 40 0"/></build></model>'
        )
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr("3D/3dmodel.model", root)
            archive.writestr("3D/Objects/painted.model", _object_model(1).replace('v1="0"', 'v1="0" paint_color="8"'))
            archive.writestr("Metadata/project_settings.config", json.dumps({
                "printer_settings_id": "Original Printer", "filament_colour": ["#ff0000", "#0000ff"],
                "post_process": ["not imported"],
            }))
            archive.writestr("Metadata/custom_gcode_per_layer.xml", "not imported")

        with pytest.raises(ThumbnailError, match="Choose 2 filament slots"):
            threemf.write_painted_project(source, settings.data_dir / "wrong.3mf", 1)

        seen = []

        async def fake_run(args, cwd, timeout, memory):
            seen.append(args)
            output = Path(args[args.index("--outputdir") + 1])
            (output / "plate_1.gcode").write_text("; estimated printing time (normal mode) = 10m\nG28\n")
            return RunResult(0, False, "")

        monkeypatch.setattr(jobs_module, "run_orca", fake_run)
        monkeypatch.setattr(services.versions, "current_binary", lambda: settings.data_dir / "AppRun")
        models, slots = [model_id], [1]
        if mixed:
            from test_beds import _stl_model

            models = [_stl_model(services, settings), model_id, _stl_model(services, settings)]
            slots = [2, 1, 1]
        job_id = services.jobs.submit(JobRequest(models, "P", "Q", ["F", "F"], arrange=False,
                             model_filaments=slots, filament_colors=["", "#ffffff"], bed_coverage=100))
        asyncio.run(services.jobs._run(job_id))
        job = services.jobs.get(job_id)
        assert job["status"] == "done", job["error"]
        assert len(seen) == (2 if mixed else 1)
        if mixed:
            assert job["files"] == ["plate_1.gcode", "plate_2.gcode"]
            mesh_args = seen[1]
            assert mesh_args[mesh_args.index("--load-filament-ids") + 1] == "2,1"
            assert [Path(path).stem for path in mesh_args[-2:]] == [models[0], models[2]]
        args = seen[0]
        assert "--arrange" in args and "--enable-prime-tower=1" in args
        assert "--load-filament-ids" not in args
        printer, process = args[args.index("--load-settings") + 1].split(";")
        assert json.loads(Path(printer).read_text())["name"] == "P"
        assert json.loads(Path(process).read_text())["name"] == "Q"
        filaments = args[args.index("--load-filaments") + 1].split(";")
        assert [json.loads(Path(path).read_text())["filament_colour"] for path in filaments] == [["#ff0000"], ["#ffffff"]]
        rebuilt = Path(args[-1])
        assert rebuilt != source and rebuilt.suffix == ".3mf"
        with zipfile.ZipFile(rebuilt) as archive:
            assert set(archive.namelist()) == {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"}
            mesh = archive.read("3D/3dmodel.model")
            assert b'paint_color="8"' in mesh and b"p:path" not in mesh
            assert b'name="Application"' not in mesh
        assert threemf.item_meshes(rebuilt)[0][0, 0].tolist() == [120, 40, 0]
        swapped = settings.data_dir / "swapped.3mf"
        assert threemf.write_painted_project(source, swapped, 2, copies=2, slot_map=[2, 1]) == ["#0000ff", "#ff0000"]
        assert len(threemf.objects(swapped)) == 2
        with zipfile.ZipFile(swapped) as archive:
            assert b'paint_color="4"' in archive.read("3D/3dmodel.model")
    finally:
        services.db.close()


def test_remap_subdivided_paint():
    assert threemf._remap_paint("841", (2, 3)) == "0C81"
    with pytest.raises(ThumbnailError, match="Invalid paint"):
        threemf._remap_paint("41", (2, 1))


def test_part_colors_and_selected_plate(tmp_path):
    source = tmp_path / "parts.3mf"
    root = (
        f'<model xmlns="{NS}" xmlns:p="{P}"><resources>'
        '<object id="2" type="model"><components><component objectid="1" p:path="/3D/part.model"/></components></object>'
        '<object id="3" type="model"><components><component objectid="1" p:path="/3D/part.model"/></components></object>'
        '</resources><build><item objectid="2"/><item objectid="3" transform="1 0 0 0 1 0 0 0 1 100 0 0"/></build></model>'
    )
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("3D/3dmodel.model", root)
        archive.writestr("3D/part.model", _object_model(1))
        archive.writestr("Metadata/project_settings.config", '{"filament_colour":["#ff0000","#0000ff"]}')
        archive.writestr("Metadata/model_settings.config", '<config><object id="3"><part id="1" subtype="normal_part">'
                         '<metadata key="extruder" value="2"/></part></object>' + ''.join(
                             f'<plate><metadata key="plater_id" value="{index}"/><model_instance>'
                             f'<metadata key="object_id" value="{index + 1}"/></model_instance></plate>'
                             for index in (1, 2)
                         ) + '</config>')
    assert threemf.source_palette(source) == ["#ff0000", "#0000ff"]
    output = tmp_path / "plate.3mf"
    threemf.write_painted_project(source, output, 2, selection=[2], copies=2)
    meshes = threemf.item_meshes(output)
    assert len(meshes) == 2 and all(mesh[0, 0].tolist() == [100, 0, 0] for mesh in meshes)
    with zipfile.ZipFile(output) as archive:
        assert b'paint_color="8"' in archive.read("3D/3dmodel.model")
    threemf.write_painted_project(source, output, 2, slot_map=[2, 1])
    assert len(threemf.item_meshes(output)) == 2
