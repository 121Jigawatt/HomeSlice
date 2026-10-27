import io
import zipfile
from pathlib import Path

import pytest
from conftest import binary_stl

from homeslice.library.validation import ValidationError, safe_display_name, validate_zip


def make_zip(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return path


def test_upload_download_delete(authed):
    r = authed.post("/api/library", params={"filename": "../../etc/Benchy.stl"}, content=binary_stl(3))
    assert r.status_code == 201, r.text
    model = r.json()
    assert model["filename"] == "Benchy.stl"
    assert authed.get("/api/library").json()[0]["id"] == model["id"]
    r = authed.get(f"/api/library/{model['id']}/file")
    assert r.status_code == 200 and r.content == binary_stl(3)
    assert authed.delete(f"/api/library/{model['id']}").status_code == 204
    assert authed.get(f"/api/library/{model['id']}/file").status_code == 404


def test_rejects_bad_extension(authed):
    r = authed.post("/api/library", params={"filename": "evil.sh"}, content=b"#!/bin/sh")
    assert r.status_code == 400


def test_rejects_fake_stl(authed):
    r = authed.post("/api/library", params={"filename": "x.stl"}, content=b"\x7fELF" + b"\0" * 200)
    assert r.status_code == 400


def test_rejects_oversize(authed):
    r = authed.post("/api/library", params={"filename": "big.stl"}, content=binary_stl(30000))
    assert r.status_code == 413


def test_invalid_model_id(authed):
    assert authed.get("/api/library/../../secret/file").status_code == 404
    assert authed.delete("/api/library/not-a-uuid").status_code == 404


def test_3mf_zip_slip_rejected(authed, tmp_path):
    p = make_zip(tmp_path / "a.3mf", {"3D/3dmodel.model": b"<model/>", "../../evil.txt": b"x"})
    r = authed.post("/api/library", params={"filename": "a.3mf"}, content=p.read_bytes())
    assert r.status_code == 400


def test_3mf_valid(authed, tmp_path):
    p = make_zip(tmp_path / "ok.3mf", {"3D/3dmodel.model": b"<model/>", "[Content_Types].xml": b"<x/>"})
    r = authed.post("/api/library", params={"filename": "ok.3mf"}, content=p.read_bytes())
    assert r.status_code == 201, r.text


def test_zip_bomb_rejected(tmp_path):
    p = make_zip(tmp_path / "bomb.3mf", {"3D/3dmodel.model": b"\0" * (50 * 1024**2)})
    with pytest.raises(ValidationError, match="ratio"):
        validate_zip(p, require_model=True)


def test_zip_symlink_rejected(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        info = zipfile.ZipInfo("3D/link.model")
        info.external_attr = (0o120777 << 16)
        zf.writestr(info, "/etc/passwd")
    p = tmp_path / "s.3mf"
    p.write_bytes(buf.getvalue())
    with pytest.raises(ValidationError, match="symlink"):
        validate_zip(p, require_model=True)


def test_safe_display_name():
    assert safe_display_name("..\\..\\a<b>.stl") == "a_b_.stl"
    assert safe_display_name("...") == "model"
