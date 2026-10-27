import struct
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from homeslice.app import create_app
from homeslice.config import Settings

PASSWORD = "correct horse battery"


def binary_stl(triangles: int = 1) -> bytes:
    return b"\0" * 80 + struct.pack("<I", triangles) + b"\0" * 50 * triangles


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        config_dir=tmp_path / "config",
        data_dir=tmp_path / "data",
        orca_datadir=tmp_path / "orca-datadir",
        cookie_secure=False,
        max_upload_mb=1,
    )


@pytest.fixture
def client(settings: Settings):
    app = create_app(settings, start_background=False)
    with TestClient(app) as c:
        yield c


@pytest.fixture
def authed(client: TestClient, settings: Settings) -> TestClient:
    token = (settings.config_dir / "setup-token").read_text().strip()
    r = client.post("/api/auth/setup", json={"setup_token": token, "username": "admin", "password": PASSWORD})
    assert r.status_code == 204, r.text
    r = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 200, r.text
    client.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return client
