import time

import pytest
from conftest import PASSWORD, binary_stl
from fastapi.testclient import TestClient

from homeslice.db import Database
from homeslice.rbac import PERMISSIONS, ensure_builtin_roles

TEMP = "temporary password 1"
NEW = "my own password 22"


def role_id(authed, name):
    return next(r["id"] for r in authed.get("/api/roles").json()["roles"] if r["name"] == name)


def add_user(authed, name, *roles, must_change=False):
    r = authed.post(
        "/api/users",
        json={"username": name, "password": TEMP, "roles": [role_id(authed, n) for n in roles], "must_change_password": must_change},
    )
    assert r.status_code == 201, r.text
    return r.json()


def login(authed, name, password=TEMP) -> TestClient:
    c = TestClient(authed.app)
    r = c.post("/api/auth/login", json={"username": name, "password": password})
    assert r.status_code == 200, r.text
    c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
    return c


def test_first_user_is_admin(authed):
    status = authed.get("/api/auth/status").json()
    assert set(status["permissions"]) == set(PERMISSIONS) and status["must_change_password"] is False
    roles = authed.get("/api/roles").json()
    assert [r["name"] for r in roles["roles"]] == ["Admin", "Operator", "Maker", "Viewer"]
    assert len(roles["permissions"]) == len(PERMISSIONS)


def test_new_user_must_change_password_first(authed):
    add_user(authed, "maker", "Maker", must_change=True)
    c = login(authed, "maker")
    assert c.get("/api/auth/status").json()["must_change_password"] is True
    assert c.get("/api/library").status_code == 403
    assert c.post("/api/auth/password", json={"current_password": TEMP, "new_password": TEMP}).status_code == 400
    assert c.post("/api/auth/password", json={"current_password": TEMP, "new_password": NEW}).status_code == 204
    assert c.get("/api/library").status_code == 200


def test_maker_permissions(authed):
    theirs = authed.post("/api/library", params={"filename": "admin.stl"}, content=binary_stl()).json()["id"]
    add_user(authed, "maker", "Maker")
    c = login(authed, "maker")
    perms = set(c.get("/api/auth/status").json()["permissions"])
    assert "slice.run" in perms and "users.manage" not in perms
    mine = c.post("/api/library", params={"filename": "mine.stl"}, content=binary_stl()).json()["id"]
    assert c.delete(f"/api/library/{theirs}").status_code == 403
    assert c.delete(f"/api/library/{mine}").status_code == 204
    for path in ("/api/users", "/api/roles", "/api/activity", "/api/printers/settings", "/api/auth/verify"):
        assert c.get(path).status_code == 403, path
    assert c.post("/api/orca/rollback").status_code == 403
    assert c.post("/api/printers", json={"name": "x", "url": "http://10.69.69.254"}).status_code == 403
    assert c.post("/api/jobs/cleanup", json={"older_than_days": 0}).status_code == 403


def test_jobs_are_private_without_view_all(authed):
    services = authed.app.state.services
    services.db.execute(
        "INSERT INTO jobs(id, params, status, owner, title, created_at) VALUES(?, ?, 'failed', 'admin', 'A', ?)",
        ("a" * 32, '{"model_ids": [], "printer": "P"}', time.time()),
    )
    add_user(authed, "maker", "Maker")
    add_user(authed, "operator", "Operator")
    maker, op = login(authed, "maker"), login(authed, "operator")
    assert maker.get("/api/jobs").json()["total"] == 0
    for path in ("", "/log", "/files/plate_1.gcode"):
        assert maker.get(f"/api/jobs/{'a' * 32}{path}").status_code == 404
    assert maker.delete(f"/api/jobs/{'a' * 32}").status_code == 404
    assert op.get("/api/jobs").json()["total"] == 1
    assert op.get(f"/api/jobs/{'a' * 32}").status_code == 200
    assert op.delete(f"/api/jobs/{'a' * 32}").status_code == 204


def test_viewer_is_read_only(authed):
    add_user(authed, "viewer", "Viewer")
    c = login(authed, "viewer")
    assert c.get("/api/library").status_code == 200
    assert c.post("/api/library", params={"filename": "a.stl"}, content=binary_stl()).status_code == 403
    assert c.post("/api/jobs", json={"model_ids": ["x"], "printer": "P", "process": "Q", "filaments": ["F"]}).status_code == 403
    assert c.get("/api/queue").status_code == 200
    assert c.put("/api/queue/order", json={"printer_id": "x", "ids": []}).status_code == 403


def test_disable_reset_and_delete(authed):
    user = add_user(authed, "maker", "Maker")
    c = login(authed, "maker")
    assert authed.patch(f"/api/users/{user['id']}", json={"disabled": True}).json()["disabled"] is True
    assert c.get("/api/library").status_code == 401
    r = TestClient(authed.app).post("/api/auth/login", json={"username": "maker", "password": TEMP})
    assert r.status_code == 401 and "disabled" in r.json()["detail"]
    authed.patch(f"/api/users/{user['id']}", json={"disabled": False})
    c = login(authed, "maker")
    assert authed.post(f"/api/users/{user['id']}/password", json={"password": "reset password 333"}).status_code == 204
    assert c.get("/api/library").status_code == 401
    assert login(authed, "maker", "reset password 333").get("/api/auth/status").json()["must_change_password"] is True
    assert authed.delete(f"/api/users/{user['id']}").status_code == 204
    assert [u["username"] for u in authed.get("/api/users").json()] == ["admin"]


def test_someone_can_always_manage_users(authed):
    me = authed.get("/api/users").json()[0]
    admin = role_id(authed, "Admin")
    assert authed.patch(f"/api/users/{me['id']}", json={"disabled": True}).status_code == 400
    assert authed.delete(f"/api/users/{me['id']}").status_code == 400
    r = authed.patch(f"/api/users/{me['id']}", json={"roles": []})
    assert r.status_code == 400 and "manage users" in r.json()["detail"]
    assert authed.put(f"/api/roles/{admin}", json={"name": "Admin", "permissions": []}).status_code == 400
    assert authed.delete(f"/api/roles/{admin}").status_code == 400

    r = authed.post("/api/roles", json={"name": "People", "permissions": ["users.manage", "bogus"]})
    assert r.status_code == 400
    people = authed.post("/api/roles", json={"name": "People", "permissions": ["users.manage"]}).json()
    assert authed.post("/api/roles", json={"name": "people", "permissions": []}).status_code == 400
    assert authed.patch(f"/api/users/{me['id']}", json={"roles": [people["id"]]}).status_code == 200
    status = authed.get("/api/auth/status").json()
    assert status["permissions"] == ["users.manage"]
    assert authed.get("/api/library").status_code == 403
    r = authed.put(f"/api/roles/{people['id']}", json={"name": "People", "permissions": ["library.view"]})
    assert r.status_code == 400


def test_custom_role_permissions_apply_immediately(authed):
    role = authed.post("/api/roles", json={"name": "Browser", "permissions": ["library.view"]}).json()
    user = add_user(authed, "pat", "Browser")
    c = login(authed, "pat")
    assert c.get("/api/printers/live").status_code == 403
    authed.put(f"/api/roles/{role['id']}", json={"name": "Browser", "permissions": ["library.view", "printers.view"]})
    assert c.get("/api/printers/live").status_code == 200
    assert authed.delete(f"/api/roles/{role['id']}").status_code == 204
    assert authed.get("/api/users").json()[1]["roles"] == [] and user["username"] == "pat"
    assert c.get("/api/library").status_code == 403


@pytest.mark.parametrize("users", [0, 2])
def test_existing_users_become_admins(tmp_path, users):
    db = Database(tmp_path / "db.sqlite")
    for i in range(users):
        db.execute("INSERT INTO users(username, password_hash, created_at) VALUES(?, 'x', 0)", (f"u{i}",))
    ensure_builtin_roles(db)
    ensure_builtin_roles(db)
    rows = db.all("SELECT r.name FROM user_roles ur JOIN roles r ON r.id = ur.role_id")
    assert [r["name"] for r in rows] == ["Admin"] * users
