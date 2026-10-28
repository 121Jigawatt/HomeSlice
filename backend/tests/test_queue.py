import time

from test_printers import FakeMoonraker, make_done_job, printer_setup  # noqa: F401


def dispatch(authed) -> list[str]:
    services = authed.app.state.services
    services.printers._live_at = 0.0  # Skip the shared 2 s live cache.
    return authed.portal.call(services.queue.dispatch)


def live(authed) -> dict:
    authed.app.state.services.printers._live_at = 0.0
    return authed.get("/api/printers/live").json()[0]


def queue(authed, job_id, printer, file="plate_1.gcode"):
    r = authed.post("/api/queue", json={"job_id": job_id, "printer_id": printer["id"], "file": file})
    assert r.status_code == 201, r.text
    return r.json()


def uploads(fake: FakeMoonraker) -> int:
    return sum(1 for q in fake.requests if q.url.path == "/server/files/upload")


def test_finished_print_flags_bed_and_blocks_start(printer_setup):
    authed, fake, printer = printer_setup
    fake.state = "printing"
    assert live(authed)["needs_attention"] is False
    fake.state = "complete"
    status = live(authed)
    assert status["needs_attention"] is True and "may still be on the bed" in status["attention_reason"]

    job_id = make_done_job(authed)
    body = {"printer_id": printer["id"], "file": "plate_1.gcode", "start": True}
    r = authed.post(f"/api/jobs/{job_id}/send", json=body)
    assert r.status_code == 409 and "bed is clear" in r.json()["detail"]
    # Upload without starting is still allowed.
    assert authed.post(f"/api/jobs/{job_id}/send", json={**body, "start": False}).status_code == 200

    r = authed.put(f"/api/printers/{printer['id']}/attention", json={"needs_attention": False})
    assert r.status_code == 200 and r.json()["needs_attention"] is False
    # Still "complete", but already acknowledged: no re-flag.
    assert live(authed)["needs_attention"] is False
    assert authed.post(f"/api/jobs/{job_id}/send", json=body).status_code == 200


def test_first_sight_of_finished_printer_is_flagged(printer_setup):
    authed, fake, _ = printer_setup
    fake.state = "complete"
    assert live(authed)["needs_attention"] is True


def test_manual_flag_with_reason(printer_setup):
    authed, _, printer = printer_setup
    r = authed.put(f"/api/printers/{printer['id']}/attention", json={"needs_attention": True, "reason": "Nozzle clog"})
    assert r.json()["attention_reason"] == "Nozzle clog"
    assert live(authed)["attention_reason"] == "Nozzle clog"


def test_new_print_at_printer_clears_automatic_flag_only(printer_setup):
    authed, fake, printer = printer_setup
    fake.state = "complete"
    assert live(authed)["needs_attention"] is True
    fake.state = "printing"  # Started at the printer, so the bed was cleared.
    assert live(authed)["needs_attention"] is False

    authed.put(f"/api/printers/{printer['id']}/attention", json={"needs_attention": True, "reason": "Nozzle clog"})
    fake.state = "complete"
    live(authed)
    fake.state = "printing"
    assert live(authed)["attention_reason"] == "Nozzle clog"


def test_queue_dispatches_one_print_then_waits_for_clear_bed(printer_setup):
    authed, fake, printer = printer_setup
    first = queue(authed, make_done_job(authed), printer)
    second = queue(authed, make_done_job(authed), printer)

    assert dispatch(authed) == [first["id"]]
    assert uploads(fake) == 1
    fake.state = "printing"
    assert dispatch(authed) == []
    fake.state = "complete"
    assert dispatch(authed) == []  # Bed flagged when the print ended.
    assert uploads(fake) == 1

    authed.put(f"/api/printers/{printer['id']}/attention", json={"needs_attention": False})
    assert dispatch(authed) == [second["id"]]
    items = {i["id"]: i for i in authed.get("/api/queue").json()}
    assert items[first["id"]]["status"] == "sent" and items[second["id"]]["status"] == "sent"
    assert items[first["id"]]["printer_name"] == "SV08"


def test_queue_skips_busy_offline_and_error_printers(printer_setup):
    authed, fake, printer = printer_setup
    queue(authed, make_done_job(authed), printer)
    for state in ("printing", "paused", "error"):
        fake.state = state
        assert dispatch(authed) == [], state
    authed.put(f"/api/printers/{printer['id']}/attention", json={"needs_attention": False})
    fake.ip = "10.0.0.250"  # Printer stops answering.
    assert dispatch(authed) == []
    assert uploads(fake) == 0


def test_queue_reorder_and_cancel(printer_setup):
    authed, fake, printer = printer_setup
    a = queue(authed, make_done_job(authed), printer)
    b = queue(authed, make_done_job(authed), printer)
    c = queue(authed, make_done_job(authed), printer)
    r = authed.put("/api/queue/order", json={"printer_id": printer["id"], "ids": [c["id"], a["id"]]})
    assert r.status_code == 204
    waiting = [i["id"] for i in authed.get("/api/queue").json() if i["status"] == "waiting"]
    assert waiting == [c["id"], a["id"], b["id"]]

    assert authed.delete(f"/api/queue/{c['id']}").status_code == 204
    assert authed.delete(f"/api/queue/{c['id']}").status_code == 404
    assert dispatch(authed) == [a["id"]]
    # Sent items can't be cancelled or reordered.
    assert authed.delete(f"/api/queue/{a['id']}").status_code == 404
    r = authed.put("/api/queue/order", json={"printer_id": printer["id"], "ids": [a["id"]]})
    assert r.status_code == 409


def test_queue_rejects_bad_input(printer_setup):
    authed, _, printer = printer_setup
    job_id = make_done_job(authed)
    for file in ("missing.gcode", "../plate_1.gcode", "result.json"):
        r = authed.post("/api/queue", json={"job_id": job_id, "printer_id": printer["id"], "file": file})
        assert r.status_code == 400, file
    r = authed.post("/api/queue", json={"job_id": job_id, "printer_id": "nope", "file": "plate_1.gcode"})
    assert r.status_code == 400


def test_failed_send_marks_item_failed(printer_setup):
    authed, fake, printer = printer_setup
    item = queue(authed, make_done_job(authed), printer)
    real = fake.__call__

    def broken(request):
        if request.url.path == "/server/files/upload":
            import httpx

            return httpx.Response(500, json={"error": {"message": "disk full"}})
        return real(request)

    import httpx

    authed.app.state.services.printers.transport = httpx.MockTransport(broken)
    assert dispatch(authed) == []
    got = next(i for i in authed.get("/api/queue").json() if i["id"] == item["id"])
    assert got["status"] == "failed" and got["error"]


def test_interrupted_send_is_not_retried(printer_setup):
    authed, _, printer = printer_setup
    services = authed.app.state.services
    item = queue(authed, make_done_job(authed), printer)
    services.db.execute("UPDATE print_queue SET status='sending' WHERE id=?", (item["id"],))
    from homeslice.printers.queue import PrintQueue

    PrintQueue(services.db, services.printers, lambda *_: None, lambda *_: "", lambda *_: None)
    row = services.db.one("SELECT status, error FROM print_queue WHERE id=?", (item["id"],))
    assert row["status"] == "failed" and "restart" in row["error"]


def test_activity_log(printer_setup):
    authed, _, printer = printer_setup
    authed.put(f"/api/printers/{printer['id']}/attention", json={"needs_attention": False})
    page = authed.get("/api/activity", params={"limit": 2}).json()
    assert page["total"] >= 3
    assert [i["action"] for i in page["items"]] == ["bed_cleared", "printer_add"]
    assert page["items"][0]["actor"] == "admin" and page["items"][0]["at"] <= time.time()
    assert "ip" not in page["items"][0]
