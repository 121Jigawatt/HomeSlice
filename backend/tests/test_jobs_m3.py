import json
import time
import uuid

import pytest
from conftest import binary_stl

from homeslice.orca.errors import explain, signed_code
from homeslice.orca.profiles import ProfileStore


def test_signed_code_and_explain():
    assert signed_code(239) == -17 and signed_code(1) == 1
    info = explain(-17, "The selected printer is not compatible with the process preset in the 3mf.")
    assert info["title"] == "Process doesn't match the printer" and "greyed out" in info["fix"]
    assert info["code"] == -17 and info["raw"].startswith("The selected printer")
    assert explain(-9999, "weird")["title"] == "Slicing failed"
    assert explain(None, "x", "timeout")["title"] == "Slicing timed out"


def add_presets(settings):
    d = settings.orca_datadir / "user/default"
    for kind, name in (("machine", "P"), ("process", "Q"), ("filament", "F")):
        (d / kind).mkdir(parents=True, exist_ok=True)
        (d / kind / f"{name}.json").write_text(json.dumps({"name": name}))


def insert_job(services, title, status, printer="P", age_days=0.0, error_code=None):
    job_id = uuid.uuid4().hex
    params = {"model_ids": [], "printer": printer, "process": "Q", "filaments": ["F"]}
    ts = time.time() - age_days * 86400
    services.db.execute(
        "INSERT INTO jobs(id, params, status, title, error, error_code, error_kind, created_at, started_at, finished_at) "
        "VALUES(?, ?, ?, ?, ?, ?, 'orca', ?, ?, ?)",
        (job_id, json.dumps(params), status, title, "boom" if status == "failed" else None, error_code, ts, ts, ts + 42),
    )
    services.jobs.job_dir(job_id).mkdir(parents=True)
    return job_id


def test_job_list_filters_and_pagination(authed):
    s = authed.app.state.services
    insert_job(s, "Benchy.stl", "done")
    insert_job(s, "Holder 50%.stl", "failed", printer="SV08", error_code=-17)
    for i in range(5):
        insert_job(s, f"Part {i}.stl", "done", age_days=1)

    page = authed.get("/api/jobs", params={"limit": 3}).json()
    assert page["total"] == 7 and len(page["items"]) == 3 and set(page["printers"]) == {"P", "SV08"}
    assert authed.get("/api/jobs", params={"limit": 3, "offset": 6}).json()["items"][0]["title"].startswith("Part")
    assert [j["title"] for j in authed.get("/api/jobs", params={"q": "bench"}).json()["items"]] == ["Benchy.stl"]
    # LIKE wildcards in the query are literal.
    assert [j["title"] for j in authed.get("/api/jobs", params={"q": "50%"}).json()["items"]] == ["Holder 50%.stl"]
    assert authed.get("/api/jobs", params={"q": "%"}).json()["total"] == 1
    failed = authed.get("/api/jobs", params={"status": "failed,bogus"}).json()
    assert failed["total"] == 1
    info = failed["items"][0]["error_info"]
    assert info["title"] == "Process doesn't match the printer" and info["code"] == -17
    assert failed["items"][0]["duration"] == 42
    assert authed.get("/api/jobs", params={"printer": "SV08"}).json()["total"] == 1


def test_cleanup(authed):
    s = authed.app.state.services
    old_done = insert_job(s, "old.stl", "done", age_days=40)
    old_failed = insert_job(s, "oldf.stl", "failed", age_days=40)
    recent = insert_job(s, "new.stl", "done", age_days=1)
    r = authed.post("/api/jobs/cleanup", json={"older_than_days": 30, "include_failed": False})
    assert r.json() == {"removed": 1}
    assert not s.jobs.job_dir(old_done).exists() and s.jobs.job_dir(recent).exists()
    assert authed.post("/api/jobs/cleanup", json={"older_than_days": 30}).json() == {"removed": 1}
    assert s.jobs.get(old_failed) is None


def test_submit_records_owner_and_title_then_retry(authed, settings):
    add_presets(settings)
    model = authed.post("/api/library", params={"filename": "Benchy.stl"}, content=binary_stl()).json()
    assert model["owner"] == "admin"
    body = {"model_ids": [model["id"]], "printer": "P", "process": "Q", "filaments": ["F"]}
    job_id = authed.post("/api/jobs", json=body).json()["id"]
    job = authed.get(f"/api/jobs/{job_id}").json()
    assert job["owner"] == "admin" and job["title"] == "Benchy.stl"
    for _ in range(50):
        job = authed.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job["status"] == "failed" and job["error_info"]["title"] == "Couldn't start slicing"
    retried = authed.post(f"/api/jobs/{job_id}/retry")
    assert retried.status_code == 202 and retried.json()["id"] != job_id


def test_failure_reads_orca_result_json(settings):
    import asyncio

    from homeslice.app import build_services
    from homeslice.orca.cli import RunResult

    services = build_services(settings)
    try:
        add_presets(settings)
        model_path = settings.library_dir / "m.stl"
        model_path.write_bytes(binary_stl(3))
        mid = uuid.uuid4().hex
        services.db.execute(
            "INSERT INTO models(id, filename, ext, size, sha256, created_at) VALUES(?, 'm.stl', '.stl', 1, 'x', ?)",
            (mid, time.time()),
        )
        (settings.library_dir / f"{mid}.stl").write_bytes(binary_stl(3))
        from homeslice.orca.jobs import JobRequest

        job_id = services.jobs.submit(JobRequest([mid], "P", "Q", ["F"]))

        async def fake_run(args, cwd, timeout, memory):
            (cwd / "out" / "result.json").write_text(
                json.dumps({"return_code": -17, "error_string": "The selected printer is not compatible."})
            )
            return RunResult(239, False, "")

        services.versions.current_binary = lambda: settings.data_dir / "AppRun"
        import homeslice.orca.jobs as jobs_module

        original = jobs_module.run_orca
        jobs_module.run_orca = fake_run
        try:
            asyncio.run(services.jobs._run(job_id))
        finally:
            jobs_module.run_orca = original
        job = services.jobs.get(job_id)
        assert job["status"] == "failed" and job["error_code"] == -17
        assert job["error"] == "The selected printer is not compatible."
        assert job["error_info"]["title"] == "Process doesn't match the printer"
    finally:
        services.db.close()


@pytest.fixture
def compat_dir(tmp_path):
    d = tmp_path / "o"

    def w(path, data):
        p = d / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    w("system/V/machine/SV08 0.4 nozzle.json", {"name": "SV08 0.4 nozzle"})
    w("system/V/machine/Other.json", {"name": "Other"})
    w("system/V/process/P SV08.json", {"name": "P SV08", "compatible_printers": ["SV08 0.4 nozzle"]})
    w("system/V/process/P Other.json", {"name": "P Other", "compatible_printers": ["Other", "X", "Y"]})
    w("system/V/process/P Any.json", {"name": "P Any", "compatible_printers": []})
    w("user/default/machine/My SV08.json", {"name": "My SV08", "inherits": "SV08 0.4 nozzle"})
    # A user copy inherits its parent's list when it doesn't set one.
    w("user/default/process/P Other - Copy.json", {"name": "P Other - Copy", "inherits": "P Other"})
    w("user/default/filament/F SV08.json", {"name": "F SV08", "compatible_printers": ["SV08 0.4 nozzle"]})
    return d


def test_incompatible_presets(compat_dir):
    result = ProfileStore(compat_dir).incompatible("My SV08")
    assert result["process"] == {
        "P Other": "Made for Other and 2 other printers",
        "P Other - Copy": "Made for Other and 2 other printers",
    }
    assert result["filament"] == {}
    other = ProfileStore(compat_dir).incompatible("Other")
    assert set(other["process"]) == {"P SV08"} and set(other["filament"]) == {"F SV08"}


def test_compat_endpoint(authed, settings):
    add_presets(settings)
    assert authed.get("/api/profiles-compat", params={"printer": "P"}).json() == {"process": {}, "filament": {}}
    assert authed.get("/api/profiles-compat", params={"printer": "nope"}).status_code == 404
