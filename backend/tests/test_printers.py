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
        # When set, only this address answers (simulates one printer on a LAN).
        self.ip: str | None = None
        self.hostname = "SPI-XI"
        self.filename = ""
        self.webcam = {
            "name": "cam",
            "enabled": True,
            "stream_url": "/webcam/?action=stream",
            "snapshot_url": "/webcam/?action=snapshot",
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.ip and request.url.host != self.ip:
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/printer/info":
            return httpx.Response(200, json={"result": {"hostname": self.hostname, "state": "ready"}})
        if request.url.path == "/printer/objects/query":
            status = {
                "print_stats": {"state": self.state, "filename": self.filename, "print_duration": 600.0},
                "display_status": {"progress": 0.25},
                "extruder": {"temperature": 254.6, "target": 255.0},
                "heater_bed": {"temperature": 80.1, "target": 80.0},
            }
            return httpx.Response(200, json={"result": {"status": status}})
        if request.url.path == "/server/files/metadata":
            return httpx.Response(200, json={"result": {"estimated_time": 2400.0}})
        if request.url.path == "/server/webcams/list":
            return httpx.Response(200, json={"result": {"webcams": [self.webcam]}})
        if request.url.path == "/webcam/":
            ctype = "image/jpeg" if request.url.params.get("action") == "snapshot" else "multipart/x-mixed-replace; boundary=frame"
            return httpx.Response(200, headers={"content-type": ctype}, content=b"JPEGDATA")
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


@pytest.fixture
def named_printer(authed):
    services = authed.app.state.services
    fake = FakeMoonraker()
    fake.ip = "10.0.0.9"
    services.printers.transport = httpx.MockTransport(fake)
    services.printers.resolve = False
    assert authed.put("/api/printers/settings", json={"scan_subnet": "10.0.0.0/28"}).status_code == 200
    printer = authed.post("/api/printers", json={"name": "SV08", "url": "http://spi-xi.local"}).json()
    return authed, fake, printer


def test_hostname_found_by_scan_and_cached(named_printer):
    authed, fake, printer = named_printer
    r = authed.get(f"/api/printers/{printer['id']}/status")
    assert r.status_code == 200, r.text
    assert r.json()["address"] == "10.0.0.9" and r.json()["state"] == "standby"
    assert authed.get("/api/printers").json()[0]["address"] == "10.0.0.9"
    fake.requests.clear()
    assert authed.get(f"/api/printers/{printer['id']}/status").status_code == 200
    assert {q.url.host for q in fake.requests} == {"10.0.0.9"}


def test_follows_address_change(named_printer):
    authed, fake, printer = named_printer
    assert authed.get(f"/api/printers/{printer['id']}/status").json()["address"] == "10.0.0.9"
    fake.ip = "10.0.0.3"
    r = authed.get(f"/api/printers/{printer['id']}/status")
    assert r.status_code == 200 and r.json()["address"] == "10.0.0.3"


def test_other_hostname_not_matched(named_printer):
    authed, fake, printer = named_printer
    fake.hostname = "Frank"
    r = authed.get(f"/api/printers/{printer['id']}/status")
    assert r.status_code == 502 and "spi-xi" in r.json()["detail"]


def test_hostname_without_scan_range(authed):
    services = authed.app.state.services
    services.printers.transport = httpx.MockTransport(FakeMoonraker())
    services.printers.resolve = False
    printer = authed.post("/api/printers", json={"name": "SV08", "url": "http://spi-xi.local"}).json()
    r = authed.get(f"/api/printers/{printer['id']}/status")
    assert r.status_code == 502 and "LAN range" in r.json()["detail"]


@pytest.mark.parametrize("subnet", ["8.8.8.0/24", "10.0.0.0/16", "127.0.0.0/24", "nonsense"])
def test_scan_subnet_rejected(authed, subnet):
    assert authed.put("/api/printers/settings", json={"scan_subnet": subnet}).status_code == 400


def test_scan_subnet_set_and_clear(authed):
    r = authed.put("/api/printers/settings", json={"scan_subnet": "10.69.69.7/24"})
    assert r.json() == {"scan_subnet": "10.69.69.0/24"}
    assert authed.put("/api/printers/settings", json={"scan_subnet": ""}).json() == {"scan_subnet": None}


def test_db_migration_adds_column(tmp_path):
    import sqlite3

    from homeslice.db import Database

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE printers(id TEXT PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL, "
        "url TEXT NOT NULL, api_key TEXT, created_at REAL NOT NULL)"
    )
    conn.commit()
    conn.close()
    db = Database(path)
    assert "resolved_ip" in {r["name"] for r in db.all("PRAGMA table_info(printers)")}
    db.close()


def test_live_status_while_printing(printer_setup):
    authed, fake, printer = printer_setup
    fake.state, fake.filename = "printing", "benchy.gcode"
    r = authed.get("/api/printers/live")
    assert r.status_code == 200, r.text
    live = r.json()[0]
    assert live["name"] == "SV08" and live["connected"] is True and live["state"] == "printing"
    assert live["filename"] == "benchy.gcode" and live["progress"] == 0.25
    assert live["remaining"] == 1800.0  # estimated 2400s - 600s elapsed
    assert live["extruder"] == {"temp": 254.6, "target": 255.0} and live["bed"]["target"] == 80.0
    assert live["webcams"] == ["cam"]


def test_live_status_offline(authed):
    services = authed.app.state.services

    def down(request):
        raise httpx.ConnectError("refused", request=request)

    services.printers.transport = httpx.MockTransport(down)
    services.printers.resolve = False
    authed.post("/api/printers", json={"name": "Frank", "url": "http://10.69.69.50"})
    live = authed.get("/api/printers/live").json()[0]
    assert live["connected"] is False and live["state"] == "offline" and "Cannot reach" in live["error"]


def test_webcam_relay(printer_setup):
    authed, _, printer = printer_setup
    r = authed.get(f"/api/printers/{printer['id']}/webcam/0", params={"snapshot": "true"})
    assert r.status_code == 200 and r.content == b"JPEGDATA" and r.headers["content-type"] == "image/jpeg"
    r = authed.get(f"/api/printers/{printer['id']}/webcam/0")
    assert r.status_code == 200 and r.headers["content-type"].startswith("multipart/x-mixed-replace")


def test_webcam_on_other_host_refused(printer_setup):
    authed, fake, printer = printer_setup
    fake.webcam = {"name": "cam", "stream_url": "http://10.0.0.99/stream", "snapshot_url": None}
    r = authed.get(f"/api/printers/{printer['id']}/webcam/0")
    assert r.status_code == 502 and "printer itself" in r.json()["detail"]
    assert authed.get(f"/api/printers/{printer['id']}/webcam/5").status_code == 502


def test_webcam_requires_login(printer_setup):
    authed, _, printer = printer_setup
    authed.cookies.clear()
    assert authed.get(f"/api/printers/{printer['id']}/webcam/0").status_code == 401
