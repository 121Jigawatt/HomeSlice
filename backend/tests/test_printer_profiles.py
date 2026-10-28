import json

import pytest

from test_printers import FakeMoonraker, make_done_job, printer_setup  # noqa: F401
from test_queue import dispatch, queue, uploads


@pytest.fixture
def presets(settings):
    d = settings.orca_datadir / "user/default"

    def w(path, data):
        p = d / path
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))

    w("machine/My SV08.json", {"name": "My SV08"})
    w("machine/Frank.json", {"name": "Frank"})
    w("process/Fine SV08.json", {"name": "Fine SV08", "compatible_printers": ["My SV08"]})
    w("process/Fine Frank.json", {"name": "Fine Frank", "compatible_printers": ["Frank"]})
    w("filament/PETG.json", {"name": "PETG"})
    return d


def send(authed, job_id, printer, start=False):
    return authed.post(
        f"/api/jobs/{job_id}/send", json={"printer_id": printer["id"], "file": "plate_1.gcode", "start": start}
    )


def test_send_refused_for_other_printer_profile(printer_setup):
    authed, fake, printer = printer_setup
    job_id = make_done_job(authed)
    authed.app.state.services.printers.set_profiles(printer["id"], "Something else", None, [], None)
    r = send(authed, job_id, printer)
    assert r.status_code == 409 and "Re-slice it for SV08" in r.json()["detail"]
    assert uploads(fake) == 0


def test_send_refused_without_printer_profile(printer_setup):
    authed, fake, printer = printer_setup
    job_id = make_done_job(authed)
    authed.app.state.services.printers.set_profiles(printer["id"], None, None, [], None)
    r = send(authed, job_id, printer)
    assert r.status_code == 409 and "no OrcaSlicer printer profile" in r.json()["detail"]
    assert uploads(fake) == 0


def test_queue_refuses_mismatch_and_rechecks_at_dispatch(printer_setup):
    authed, fake, printer = printer_setup
    services = authed.app.state.services
    item = queue(authed, make_done_job(authed), printer)

    services.printers.set_profiles(printer["id"], "Other", None, [], None)
    r = authed.post("/api/queue", json={"job_id": make_done_job(authed), "printer_id": printer["id"], "file": "plate_1.gcode"})
    assert r.status_code == 409

    assert dispatch(authed) == []
    got = next(i for i in authed.get("/api/queue").json() if i["id"] == item["id"])
    assert got["status"] == "failed" and "Re-slice" in got["error"]
    assert uploads(fake) == 0


def test_slice_for_target_printer_must_match(printer_setup):
    authed, _, printer = printer_setup
    model = authed.get("/api/library").json()
    if not model:
        make_done_job(authed)
        model = authed.get("/api/library").json()
    body = {"model_ids": [model[0]["id"]], "printer": "Other", "process": "Q", "filaments": ["F"]}
    r = authed.post("/api/jobs", json={**body, "target_printer_id": printer["id"]})
    assert r.status_code == 400 and "Re-slice it for SV08" in r.json()["detail"]


def test_set_printer_profiles(printer_setup, presets):
    authed, _, printer = printer_setup
    url = f"/api/printers/{printer['id']}/profiles"
    ok = {"printer_preset": "My SV08", "process": "Fine SV08", "filaments": ["PETG"], "bed_type": "Textured PEI Plate"}
    r = authed.put(url, json=ok)
    assert r.status_code == 200, r.text
    got = r.json()
    assert got["printer_preset"] == "My SV08" and got["default_process"] == "Fine SV08"
    assert got["default_filaments"] == ["PETG"] and got["default_bed_type"] == "Textured PEI Plate"
    assert authed.get("/api/printers").json()[0]["printer_preset"] == "My SV08"

    assert authed.put(url, json={**ok, "printer_preset": "Nope"}).status_code == 400
    r = authed.put(url, json={**ok, "process": "Fine Frank"})
    assert r.status_code == 400 and "doesn't suit" in r.json()["detail"]
    assert authed.put(url, json={"process": "Fine SV08"}).status_code == 400
    # Clearing is allowed.
    assert authed.put(url, json={}).json()["printer_preset"] is None
