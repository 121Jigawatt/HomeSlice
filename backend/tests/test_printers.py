import json
import time
import uuid

import httpx
import pytest
from conftest import binary_stl

from homeslice.printers.moonraker import MoonrakerClient, PrinterError, check_lan_ip, normalize_url


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://10.69.69.165", "http://10.69.69.165"),
        ("http://10.69.69.165/", "http://10.69.69.165"),
        (" https://printer.lan:7125 ", "https://printer.lan:7125"),
    ],
)
def test_normalize_url(url, expected):
    assert normalize_url(url) == expected


@pytest.mark.parametrize(
    "url",
    ["ftp://10.0.0.5", "http://user:pw@10.0.0.5", "http://10.0.0.5/api?x=1", "10.0.0.5", "http://10.0.0.5:99999"],
)
def test_normalize_url_rejects(url):
    with pytest.raises(PrinterError):
        normalize_url(url)


@pytest.mark.parametrize("ip", ["127.0.0.1", "169.254.169.254", "8.8.8.8", "0.0.0.0", "::1", "::ffff:127.0.0.1"])
def test_non_lan_addresses_rejected(ip):
    with pytest.raises(PrinterError):
        check_lan_ip(ip)


def test_lan_address_allowed():
    check_lan_ip("10.69.69.165")
    check_lan_ip("192.168.1.20")


async def test_loopback_url_refused_before_request():
    called = []
    client = MoonrakerClient("http://127.0.0.1:8000", transport=httpx.MockTransport(lambda r: called.append(r)))
    with pytest.raises(PrinterError, match="not a local network"):
        await client.status()
    assert not called


class FakeMoonraker:
    def __init__(self, state: str = "standby") -> None:
        self.state = state
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.path == "/printer/objects/query":
            status = {"print_stats": {"state": self.state, "filename": ""}, "display_status": {"progress": 0.0}}
            return httpx.Response(200, json={"result": {"status": status}})
        if request.url.path == "/server/files/upload":
            started = b'name="print"' in request.content
            return httpx.Response(201, json={"result": {"print_started": started, "item": {}}})
        return httpx.Response(404, json={"error": {"code": 404, "message": "Not Found"}})


@pytest.fixture
def printer_setup(authed):
    services = authed.app.state.services
    fake = FakeMoonraker()
    services.printers.transport = httpx.MockTransport(fake)
    services.printers.resolve = False
    r = authed.post("/api/printers", json={"name": "SV08", "url": "http://10.69.69.165/", "api_key": "s3cret"})
    assert r.status_code == 201, r.text
    return authed, fake, r.json()


def make_done_job(authed) -> str:
    services = authed.app.state.services
    model = authed.post("/api/library", params={"filename": "Cabinet Holder.stl"}, content=binary_stl()).json()
    job_id = uuid.uuid4().hex
    params = {"model_ids": [model["id"]], "printer": "P", "process": "Q", "filaments": ["F"]}
    services.db.execute(
        "INSERT INTO jobs(id, params, status, created_at) VALUES(?, ?, 'done', ?)",
        (job_id, json.dumps(params), time.time()),
    )
    out = services.jobs.job_dir(job_id) / "out"
    out.mkdir(parents=True)
    (out / "plate_1.gcode").write_text("G28\n")
    return job_id


def test_printer_crud_hides_api_key(printer_setup):
    authed, _, printer = printer_setup
    assert printer["url"] == "http://10.69.69.165" and printer["has_api_key"] is True
    assert "api_key" not in printer
    stored = authed.app.state.services.db.one("SELECT api_key FROM printers WHERE id=?", (printer["id"],))
    assert stored["api_key"] and "s3cret" not in stored["api_key"]
    assert [p["name"] for p in authed.get("/api/printers").json()] == ["SV08"]
    assert authed.delete(f"/api/printers/{printer['id']}").status_code == 204
    assert authed.get("/api/printers").json() == []


def test_add_printer_rejects_bad_url(authed):
    r = authed.post("/api/printers", json={"name": "x", "url": "file:///etc/passwd"})
    assert r.status_code == 400


def test_printer_status(printer_setup):
    authed, fake, printer = printer_setup
    r = authed.get(f"/api/printers/{printer['id']}/status")
    assert r.status_code == 200 and r.json()["state"] == "standby"
    assert fake.requests[-1].headers["x-api-key"] == "s3cret"


def test_send_and_start(printer_setup):
    authed, fake, printer = printer_setup
    job_id = make_done_job(authed)
    r = authed.post(f"/api/jobs/{job_id}/send", json={"printer_id": printer["id"], "file": "plate_1.gcode", "start": True})
    assert r.status_code == 200, r.text
    assert r.json()["print_started"] is True
    upload = fake.requests[-1]
    assert upload.url.path == "/server/files/upload"
    assert f'filename="Cabinet_Holder-plate_1-{job_id[:8]}.gcode"'.encode() in upload.content
    assert b'name="root"' in upload.content and b"G28" in upload.content


def test_send_upload_only(printer_setup):
    authed, fake, printer = printer_setup
    job_id = make_done_job(authed)
    r = authed.post(f"/api/jobs/{job_id}/send", json={"printer_id": printer["id"], "file": "plate_1.gcode"})
    assert r.status_code == 200 and r.json()["print_started"] is False
    assert [q.url.path for q in fake.requests] == ["/server/files/upload"]


def test_refuses_to_start_when_busy(printer_setup):
    authed, fake, printer = printer_setup
    fake.state = "printing"
    job_id = make_done_job(authed)
    r = authed.post(f"/api/jobs/{job_id}/send", json={"printer_id": printer["id"], "file": "plate_1.gcode", "start": True})
    assert r.status_code == 409
    assert all(q.url.path != "/server/files/upload" for q in fake.requests)


def test_send_rejects_bad_file(printer_setup):
    authed, _, printer = printer_setup
    job_id = make_done_job(authed)
    for name in ("result.json", "../plate_1.gcode", "missing.gcode"):
        r = authed.post(f"/api/jobs/{job_id}/send", json={"printer_id": printer["id"], "file": name})
        assert r.status_code == 404, name


def test_send_requires_csrf(printer_setup):
    authed, _, printer = printer_setup
    job_id = make_done_job(authed)
    authed.headers.pop("X-CSRF-Token")
    r = authed.post(f"/api/jobs/{job_id}/send", json={"printer_id": printer["id"], "file": "plate_1.gcode"})
    assert r.status_code == 403
