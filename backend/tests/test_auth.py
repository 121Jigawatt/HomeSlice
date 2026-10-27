from conftest import PASSWORD


def test_setup_requires_token(client, settings):
    assert client.get("/api/auth/status").json()["setup_required"] is True
    r = client.post("/api/auth/setup", json={"setup_token": "wrong", "username": "admin", "password": PASSWORD})
    assert r.status_code == 400


def test_setup_only_once(authed, settings):
    assert not (settings.config_dir / "setup-token").exists()
    r = authed.post("/api/auth/setup", json={"setup_token": "x", "username": "evil", "password": PASSWORD})
    assert r.status_code == 400


def test_short_password_rejected(client, settings):
    token = (settings.config_dir / "setup-token").read_text().strip()
    r = client.post("/api/auth/setup", json={"setup_token": token, "username": "admin", "password": "short"})
    assert r.status_code == 400


def test_endpoints_require_auth(client):
    assert client.get("/api/library").status_code == 401
    assert client.get("/api/jobs").status_code == 401
    assert client.get("/api/orca").status_code == 401
    assert client.get("/api/auth/verify").status_code == 401


def test_session_cookie_flags(client, settings):
    token = (settings.config_dir / "setup-token").read_text().strip()
    client.post("/api/auth/setup", json={"setup_token": token, "username": "admin", "password": PASSWORD})
    r = client.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie


def test_csrf_required(authed):
    csrf = authed.headers.pop("X-CSRF-Token")
    assert authed.post("/api/auth/logout").status_code == 403
    authed.headers["X-CSRF-Token"] = "wrong"
    assert authed.post("/api/auth/logout").status_code == 403
    authed.headers["X-CSRF-Token"] = csrf
    assert authed.get("/api/auth/verify").status_code == 204


def test_cross_origin_blocked(authed):
    r = authed.post("/api/auth/logout", headers={"Origin": "https://evil.example"})
    assert r.status_code == 403


def test_login_lockout(authed):
    authed.cookies.clear()
    for _ in range(5):
        assert authed.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    r = authed.post("/api/auth/login", json={"username": "admin", "password": PASSWORD})
    assert r.status_code == 429


def test_logout_invalidates_session(authed):
    assert authed.post("/api/auth/logout").status_code == 204
    assert authed.get("/api/library").status_code == 401


def test_password_change(authed):
    r = authed.post("/api/auth/password", json={"current_password": "bad", "new_password": "x" * 20})
    assert r.status_code == 400
    r = authed.post("/api/auth/password", json={"current_password": PASSWORD, "new_password": "y" * 20})
    assert r.status_code == 204


def test_security_headers(client):
    r = client.get("/api/health")
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert r.headers["x-content-type-options"] == "nosniff"
    assert r.headers["cache-control"] == "no-store"
    assert client.get("/api/docs").status_code == 404
