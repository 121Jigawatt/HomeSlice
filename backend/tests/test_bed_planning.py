import numpy as np
from conftest import binary_stl
from test_jobs_m3 import add_presets
from test_threemf import bambu_project

from homeslice.library import threemf


def test_objects_and_single_item(tmp_path):
    src = bambu_project(tmp_path / "ring.3mf")
    assert threemf.objects(src) == [{"item": 1, "plate": 1}, {"item": 2, "plate": 2}, {"item": 3, "plate": 2}]
    dest = tmp_path / "i3.stl"
    threemf.write_item(src, 3, dest)
    tris = np.frombuffer(dest.read_bytes()[84:], dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    assert len(tris) == 1 and tris["v"][0][:, 0].min() == 200  # world position kept


def test_parts_endpoint_and_item_mesh(authed, tmp_path):
    mid = authed.post("/api/library", params={"filename": "Ring.3mf"}, content=bambu_project(tmp_path / "r.3mf").read_bytes()).json()["id"]
    parts = authed.get(f"/api/library/{mid}/parts").json()
    assert [(p["item"], p["plate"]) for p in parts] == [(1, 1), (2, 2), (3, 2)]
    assert all(p["footprint"] == 100 for p in parts)  # bounding box of the 10 x 10 mm triangle
    mesh = authed.get(f"/api/library/{mid}/mesh", params={"item": 2})
    assert mesh.status_code == 200 and int(np.frombuffer(mesh.content[80:84], "<u4")[0]) == 1
    assert authed.get(f"/api/library/{mid}/mesh", params={"item": 9}).status_code == 422

    stl = authed.post("/api/library", params={"filename": "a.stl"}, content=binary_stl()).json()["id"]
    assert authed.get(f"/api/library/{stl}/parts").json() == [{"item": 0, "plate": 0, "footprint": 0.0}]


def test_slice_coverage_override(authed, settings):
    add_presets(settings)
    mid = authed.post("/api/library", params={"filename": "a.stl"}, content=binary_stl()).json()["id"]
    body = {"model_ids": [mid], "printer": "P", "process": "Q", "filaments": ["F"]}
    job = authed.post("/api/jobs", json={**body, "bed_coverage": 80}).json()
    assert authed.get(f"/api/jobs/{job['id']}").json()["params"]["bed_coverage"] == 80
    default = authed.post("/api/jobs", json=body).json()
    assert authed.get(f"/api/jobs/{default['id']}").json()["params"]["bed_coverage"] == 50
    assert authed.post("/api/jobs", json={**body, "bed_coverage": 5}).status_code == 422
