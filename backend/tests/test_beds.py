import asyncio
import json
import time
import uuid

import homeslice.orca.jobs as jobs_module
import pytest
from test_jobs_m3 import add_presets
from test_printers import FakeMoonraker, make_done_job, printer_setup  # noqa: F401
from test_threemf import bambu_project

from homeslice.app import build_services
from homeslice.library.step import write_binary_stl
from homeslice.orca.cli import RunResult
from homeslice.library.thumbnails import footprint_mm2
from homeslice.orca.jobs import JobRequest, bed_area, combine_stats

SQUARE_100 = [  # 92 x 92 mm, so 100 x 100 mm footprint with the 8 mm arrange spacing; two triangles
    [[0, 0, 0], [92, 0, 0], [92, 92, 0]],
    [[0, 0, 0], [92, 92, 0], [0, 92, 5]],
]


def test_bed_area_and_footprint(tmp_path):
    p = tmp_path / "printer.json"
    p.write_text(json.dumps({"printable_area": ["0x0", "350x0", "350x350", "0x350"]}))
    assert bed_area(p) == 350 * 350
    p.write_text(json.dumps({}))
    assert bed_area(p) == 256 * 256
    m = tmp_path / "m.stl"
    write_binary_stl(m, SQUARE_100)
    assert footprint_mm2(m) == pytest.approx(10_000)


def test_combine_stats():
    single = {"print_time": "1h", "filament_g": "5"}
    assert combine_stats([single]) == single
    total = combine_stats(
        [
            {"print_time": "1h 30m 10s", "filament_g": "10.5", "filament_mm": "100", "layers": "50"},
            {"print_time": "45m 50s", "filament_g": "2.25", "filament_mm": "20", "layers": "80"},
        ]
    )
    assert total["plates"] == 2 and total["print_time"] == "2h 16m"
    assert total["filament_g"] == "12.75" and total["filament_mm"] == "120.00" and total["layers"] == "80"


def _run_capturing(services, settings, job_id):
    calls: list[list[str]] = []

    async def fake_run(args, cwd, timeout, memory):
        calls.append(args)
        out = args[args.index("--outputdir") + 1]
        from pathlib import Path

        (Path(out) / "plate_1.gcode").write_text("; total layer number: 3\n; estimated printing time (normal mode) = 10m\nG28\n")
        return RunResult(0, False, "")

    services.versions.current_binary = lambda: settings.data_dir / "AppRun"
    original = jobs_module.run_orca
    jobs_module.run_orca = fake_run
    try:
        asyncio.run(services.jobs._run(job_id))
    finally:
        jobs_module.run_orca = original
    return calls


@pytest.fixture
def svc(settings):
    services = build_services(settings)
    add_presets(settings)
    machine = settings.orca_datadir / "user/default/machine/P.json"
    machine.write_text(json.dumps({"name": "P", "printable_area": ["0x0", "200x0", "200x200", "0x200"]}))
    yield services
    services.db.close()


def _stl_model(services, settings):
    mid = uuid.uuid4().hex
    services.db.execute(
        "INSERT INTO models(id, filename, ext, size, sha256, created_at) VALUES(?, 'sq.stl', '.stl', 1, 'x', ?)",
        (mid, time.time()),
    )
    write_binary_stl(settings.library_dir / f"{mid}.stl", SQUARE_100)
    return mid


def test_copies_split_into_beds_by_coverage(svc, settings):
    mid = _stl_model(svc, settings)
    # Bed 200x200 = 40,000 mm2; each copy covers 10,000 -> 50% allows 2 per bed.
    job_id = svc.jobs.submit(JobRequest([mid], "P", "Q", ["F"], copies=[5], bed_coverage=50))
    calls = _run_capturing(svc, settings, job_id)
    assert [sum(a.endswith(".stl") for a in c) for c in calls] == [2, 2, 1]
    job = svc.jobs.get(job_id)
    assert job["status"] == "done", job["error"]
    assert [f for f in job["files"] if f.endswith(".gcode")] == ["plate_1.gcode", "plate_2.gcode", "plate_3.gcode"]
    assert job["stats"]["plates"] == 3 and job["stats"]["print_time"] == "30m"


def test_full_coverage_keeps_one_run(svc, settings):
    mid = _stl_model(svc, settings)
    job_id = svc.jobs.submit(JobRequest([mid], "P", "Q", ["F"], copies=[4], bed_coverage=100))
    calls = _run_capturing(svc, settings, job_id)
    assert len(calls) == 1 and sum(a.endswith(".stl") for a in calls[0]) == 4
    assert "plates" not in svc.jobs.get(job_id)["stats"]


def test_each_3mf_plate_on_its_own_bed(svc, settings):
    mid = uuid.uuid4().hex
    svc.db.execute(
        "INSERT INTO models(id, filename, ext, size, sha256, created_at) VALUES(?, 'Ring.3mf', '.3mf', 1, 'x', 0)", (mid,)
    )
    bambu_project(settings.library_dir / f"{mid}.3mf")
    job_id = svc.jobs.submit(JobRequest([mid], "P", "Q", ["F"], model_plates=[-1]))
    assert svc.jobs.get(job_id)["title"] == "Ring.3mf (each plate)"
    calls = _run_capturing(svc, settings, job_id)
    assert [sum(a.endswith(".stl") for a in c) for c in calls] == [1, 2]


def test_bed_coverage_validated(svc, settings):
    mid = _stl_model(svc, settings)
    with pytest.raises(Exception):
        svc.jobs.submit(JobRequest([mid], "P", "Q", ["F"], bed_coverage=5))


def test_printer_bed_coverage_setting_and_reslice(printer_setup, settings):
    authed, _, printer = printer_setup
    services = authed.app.state.services
    assert printer["bed_coverage"] == 50
    d = settings.orca_datadir / "user/default"
    for kind, data in (
        ("machine", {"name": "Big"}),
        ("process", {"name": "BigProc"}),
        ("filament", {"name": "BigFil"}),
    ):
        (d / kind).mkdir(parents=True, exist_ok=True)
        (d / kind / f"{data['name']}.json").write_text(json.dumps(data))
    r = authed.put(
        f"/api/printers/{printer['id']}/profiles",
        json={"printer_preset": "Big", "process": "BigProc", "filaments": ["BigFil"], "bed_coverage": 70},
    )
    assert r.status_code == 200 and r.json()["bed_coverage"] == 70
    assert authed.put(f"/api/printers/{printer['id']}/profiles", json={"bed_coverage": 5}).status_code == 422

    job_id = make_done_job(authed)  # sliced for "P"
    params = services.jobs.get(job_id)["params"]
    params["overrides"] = {"layer_height": "0.16"}
    services.db.execute("UPDATE jobs SET params=? WHERE id=?", (json.dumps(params), job_id))
    r = authed.post(f"/api/jobs/{job_id}/reslice", json={"printer_id": printer["id"]})
    assert r.status_code == 202, r.text
    new = services.jobs.get(r.json()["id"])["params"]
    assert (new["printer"], new["process"], new["filaments"]) == ("Big", "BigProc", ["BigFil"])
    assert new["overrides"]["layer_height"] == "0.16" and new["bed_coverage"] == 70

    services.printers.set_profiles(printer["id"], "Big", None, [], None)
    r = authed.post(f"/api/jobs/{job_id}/reslice", json={"printer_id": printer["id"]})
    assert r.status_code == 400 and "default process" in r.json()["detail"]
