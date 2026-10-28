import asyncio
import json
import time
import uuid

import homeslice.orca.jobs as jobs_module
from conftest import binary_stl
from test_jobs_m3 import add_presets

from homeslice.app import build_services
from homeslice.orca.cli import RunResult
from homeslice.orca.jobs import JobRequest


def test_copies_validation_and_title(authed, settings):
    add_presets(settings)
    a = authed.post("/api/library", params={"filename": "Benchy.stl"}, content=binary_stl()).json()
    b = authed.post("/api/library", params={"filename": "Clip.stl"}, content=binary_stl()).json()
    body = {"model_ids": [a["id"], b["id"]], "printer": "P", "process": "Q", "filaments": ["F"]}

    r = authed.post("/api/jobs", json={**body, "copies": [3, 1]})
    assert r.status_code == 202, r.text
    job = authed.get(f"/api/jobs/{r.json()['id']}").json()
    assert job["title"] == "Benchy.stl \u00d73 + 1 more" and job["params"]["copies"] == [3, 1]

    for bad in ([3], [0, 1], [101, 1], [3, 1, 1]):
        assert authed.post("/api/jobs", json={**body, "copies": bad}).status_code == 400, bad
    assert authed.post("/api/jobs", json={**body, "copies": [100, 100]}).status_code == 202
    single = authed.post("/api/jobs", json={**body, "model_ids": [a["id"]], "copies": [4]}).json()
    assert authed.get(f"/api/jobs/{single['id']}").json()["title"] == "Benchy.stl \u00d74"


def test_copies_repeat_inputs_and_old_jobs_still_load(settings):
    services = build_services(settings)
    try:
        add_presets(settings)
        ids = []
        for name in ("a.stl", "b.stl"):
            mid = uuid.uuid4().hex
            services.db.execute(
                "INSERT INTO models(id, filename, ext, size, sha256, created_at) VALUES(?, ?, '.stl', 1, 'x', ?)",
                (mid, name, time.time()),
            )
            (settings.library_dir / f"{mid}.stl").write_bytes(binary_stl(3))
            ids.append(mid)
        job_id = services.jobs.submit(JobRequest(ids, "P", "Q", ["F"], copies=[3, 2]))
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
        stls = [a for a in seen[0] if a.endswith(".stl")]
        assert [s.split("\\")[-1].split("/")[-1][:32] for s in stls] == [ids[0]] * 3 + [ids[1]] * 2

        # Jobs saved before copies existed have no "copies" key.
        legacy = JobRequest(**json.loads(json.dumps({"model_ids": ids, "printer": "P", "process": "Q", "filaments": ["F"]})))
        assert legacy.counts() == [1, 1]
    finally:
        services.db.close()
