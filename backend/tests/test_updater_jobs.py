import hashlib
import json
import time

import httpx
import pytest
from conftest import binary_stl

from homeslice.config import Settings
from homeslice.net.safe_fetch import SafeFetcher
from homeslice.orca.updater import UpdateError, Updater, pick_asset, to_release_info
from homeslice.orca.versions import VersionError, VersionStore

PAYLOAD = b"fake appimage"


def release(digest: str | None) -> dict:
    asset = {
        "name": "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage",
        "browser_download_url": "https://github.com/OrcaSlicer/OrcaSlicer/releases/download/v2.4.2/x.AppImage",
        "size": len(PAYLOAD),
    }
    if digest:
        asset["digest"] = digest
    return {"tag_name": "v2.4.2", "name": "v2.4.2", "assets": [asset]}


def test_pick_asset_prefers_ubuntu2404():
    assets = [
        {"name": "OrcaSlicer_Windows_x64_V2.4.2.zip"},
        {"name": "OrcaSlicer_Linux_AppImage_Ubuntu2204_V2.4.2.AppImage"},
        {"name": "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage"},
        {"name": "OrcaSlicer_Linux_AppImage_Ubuntu2404_aarch64_V2.4.2.AppImage"},
    ]
    assert pick_asset(assets)["name"] == "OrcaSlicer_Linux_AppImage_Ubuntu2404_V2.4.2.AppImage"


def test_release_info_parses_digest():
    info = to_release_info(release("sha256:" + "a" * 64))
    assert info.asset_sha256 == "a" * 64
    assert info.version_id == "v2.4.2-aaaaaaaaaaaa"
    assert to_release_info(release("md5:abc")).asset_sha256 is None


def make_updater(settings: Settings) -> Updater:
    settings.ensure_dirs()
    fetch = SafeFetcher(
        {"github.com"}, max_bytes=10**6, transport=httpx.MockTransport(lambda r: httpx.Response(200, content=PAYLOAD)), resolve=False
    )
    return Updater(settings, VersionStore(settings.orca_dir), fetcher=fetch, api_fetcher=fetch)


async def test_refuses_unverified(settings):
    with pytest.raises(UpdateError, match="digest"):
        await make_updater(settings).install(to_release_info(release(None)))


async def test_checksum_mismatch(settings):
    with pytest.raises(UpdateError, match="mismatch"):
        await make_updater(settings).install(to_release_info(release("sha256:" + "0" * 64)))
    assert not any(settings.tmp_dir.iterdir())


async def test_checksum_ok_reaches_extract(settings, monkeypatch):
    up = make_updater(settings)
    calls = []

    async def fake_exec(args, cwd, timeout, check=True):
        calls.append(args[-1])
        if args[-1] == "--appimage-extract":
            (cwd / "squashfs-root").mkdir()
            (cwd / "squashfs-root" / "AppRun").write_text("#!/bin/sh")
        return 0, "OrcaSlicer usage"

    monkeypatch.setattr(up, "_exec", fake_exec)
    version = await up.install(to_release_info(release("sha256:" + hashlib.sha256(PAYLOAD).hexdigest())))
    assert calls == ["--appimage-extract", "--help"]
    store = VersionStore(settings.orca_dir)
    assert store.current() == version and store.current_binary() is not None


def test_version_store_rollback(tmp_path):
    store = VersionStore(tmp_path)
    for v in ("a", "b"):
        (store.versions_dir / v).mkdir()
        (store.versions_dir / v / ".homeslice.json").write_text(json.dumps({"installed_at": time.time()}))
    store.activate("a")
    store.activate("b")
    assert store.rollback() == "a" and store.current() == "a"
    with pytest.raises(VersionError):
        store.activate("../../etc")


def test_job_fails_cleanly_without_orca(authed, settings):
    datadir = settings.orca_datadir / "user/default"
    for kind, name in (("machine", "P"), ("process", "Q"), ("filament", "F")):
        (datadir / kind).mkdir(parents=True)
        (datadir / kind / f"{name}.json").write_text(json.dumps({"name": name}))
    model = authed.post("/api/library", params={"filename": "m.stl"}, content=binary_stl()).json()
    assert [p["name"] for p in authed.get("/api/profiles/printer").json()] == ["P"]

    r = authed.post("/api/jobs", json={"model_ids": [model["id"]], "printer": "P", "process": "Q", "filaments": ["F"]})
    assert r.status_code == 202, r.text
    job_id = r.json()["id"]
    for _ in range(50):
        job = authed.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("queued", "running"):
            break
        time.sleep(0.05)
    assert job["status"] == "failed" and "not installed" in job["error"]

    bad = authed.post("/api/jobs", json={"model_ids": [model["id"]], "printer": "P", "process": "Q", "filaments": ["F"], "overrides": {"post_process": "x"}})
    assert bad.status_code == 400
    assert authed.get(f"/api/jobs/{job_id}/files/..%2F..%2Fconfig").status_code == 404


def test_rejects_plate_without_bed_temperature(authed, settings):
    d = settings.orca_datadir / "user/default"
    for kind, name in (("machine", "P"), ("process", "Q")):
        (d / kind).mkdir(parents=True, exist_ok=True)
        (d / kind / f"{name}.json").write_text(json.dumps({"name": name}))
    (d / "filament").mkdir(parents=True, exist_ok=True)
    (d / "filament" / "PETG.json").write_text(
        json.dumps({"name": "PETG", "cool_plate_temp_initial_layer": ["0"], "textured_plate_temp_initial_layer": ["70"]})
    )
    model = authed.post("/api/library", params={"filename": "m.stl"}, content=binary_stl()).json()
    body = {"model_ids": [model["id"]], "printer": "P", "process": "Q", "filaments": ["PETG"]}
    r = authed.post("/api/jobs", json={**body, "overrides": {"curr_bed_type": "Cool Plate"}})
    assert r.status_code == 400 and "Cool Plate" in r.json()["detail"]
    r = authed.post("/api/jobs", json={**body, "overrides": {"curr_bed_type": "Textured PEI Plate"}})
    assert r.status_code == 202, r.text


class _FakeRelease:
    tag = "v9.9.9"
    version_id = "v9.9.9-abc"

    def to_dict(self) -> dict:
        return {"tag": self.tag, "version_id": self.version_id}


@pytest.fixture
def checker(settings, monkeypatch):
    from homeslice import app as app_module
    from homeslice.api import orca_routes

    services = app_module.build_services(settings)
    installs: list[str] = []

    async def latest(channel):
        return _FakeRelease()

    async def fake_install(svc, channel):
        installs.append(channel)

    monkeypatch.setattr(services.updater, "latest", latest)
    monkeypatch.setattr(orca_routes, "run_install", fake_install)
    yield app_module, services, installs
    services.db.close()


async def test_first_run_installs_orca(checker):
    app_module, services, installs = checker
    wait = await app_module.check_for_updates(services)
    assert installs == ["stable"]
    # Install was faked, so nothing is current yet: retry soon rather than in hours.
    assert wait == app_module.FIRST_INSTALL_RETRY_S


async def test_existing_install_not_replaced_without_auto_update(checker, monkeypatch):
    app_module, services, installs = checker
    monkeypatch.setattr(services.versions, "current", lambda: "v1.0.0-old")
    wait = await app_module.check_for_updates(services)
    assert installs == [] and wait == services.settings.update_check_interval_h * 3600


async def test_first_run_retries_after_failed_check(checker, monkeypatch):
    app_module, services, installs = checker

    async def failing(channel):
        raise UpdateError("offline")

    monkeypatch.setattr(services.updater, "latest", failing)
    assert await app_module.check_for_updates(services) == app_module.FIRST_INSTALL_RETRY_S
    assert installs == []
