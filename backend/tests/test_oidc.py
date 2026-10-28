import json
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import jwt
import pytest
from conftest import PASSWORD
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

ISSUER = "https://idp.test/application/o/homeslice/"
SECRET = "client-secret-value"


class FakeIdp:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.claims: dict = {}
        self.alg = "RS256"
        self.nonce = ""
        self.token_requests: list[httpx.Request] = []
        self.token_error: dict | None = None
        self.auth_methods: list[str] | None = None

    def id_token(self) -> str:
        now = int(time.time())
        claims = {"iss": ISSUER, "aud": "homeslice", "sub": "abc123", "iat": now, "exp": now + 300, "nonce": self.nonce,
                  "preferred_username": "alice", "groups": ["HomeSlice Makers"], **self.claims}
        if self.alg == "HS256":
            return jwt.encode(claims, SECRET, algorithm="HS256")
        return jwt.encode(claims, self.key, algorithm=self.alg, headers={"kid": "k1"})

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/.well-known/openid-configuration"):
            base = "https://idp.test/application/o"
            doc = {
                "issuer": ISSUER,
                "authorization_endpoint": f"{base}/authorize/",
                "token_endpoint": f"{base}/token/",
                "jwks_uri": f"{base}/homeslice/jwks/",
                "userinfo_endpoint": f"{base}/userinfo/",
            }
            if self.auth_methods:
                doc["token_endpoint_auth_methods_supported"] = self.auth_methods
            return httpx.Response(200, json=doc)
        if path.endswith("/jwks/"):
            jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(self.key.public_key()))
            return httpx.Response(200, json={"keys": [{**jwk, "kid": "k1", "use": "sig"}]})
        if path.endswith("/token/"):
            self.token_requests.append(request)
            if self.token_error:
                return httpx.Response(400, json=self.token_error)
            return httpx.Response(200, json={"access_token": "at", "token_type": "Bearer", "id_token": self.id_token()})
        if path.endswith("/userinfo/"):
            return httpx.Response(200, json={"sub": "abc123", "email": "alice@example.com"})
        return httpx.Response(404)


@pytest.fixture
def idp(authed):
    fake = FakeIdp()
    authed.app.state.services.oidc.transport = httpx.MockTransport(fake.handler)
    roles = {r["name"]: r["id"] for r in authed.get("/api/roles").json()["roles"]}
    r = authed.put("/api/sso", json={
        "enabled": True, "issuer": ISSUER, "client_id": "homeslice", "client_secret": SECRET,
        "base_url": "http://testserver", "label": "authentik",
        "default_roles": [roles["Viewer"]], "group_roles": [{"group": "HomeSlice Makers", "role": roles["Maker"]}],
    })
    assert r.status_code == 200, r.text
    assert r.json()["redirect_uri"] == "http://testserver/api/auth/oidc/callback" and r.json()["has_secret"]
    assert "client_secret" not in r.json()
    fake.roles = roles
    return fake


def sign_in(authed, idp, next_path="/jobs", tamper_state=False):
    c = TestClient(authed.app)
    r = c.get("/api/auth/oidc/login", params={"next": next_path}, follow_redirects=False)
    assert r.status_code == 303, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["code_challenge_method"] == ["S256"] and query["redirect_uri"] == ["http://testserver/api/auth/oidc/callback"]
    idp.nonce = query["nonce"][0]
    state = query["state"][0]
    if tamper_state:
        c.cookies.clear()
    r = c.get("/api/auth/oidc/callback", params={"state": state, "code": "the-code"}, follow_redirects=False)
    return c, r


def error_of(r) -> str:
    return parse_qs(urlsplit(r.headers["location"]).query).get("sso_error", [""])[0]


def test_status_advertises_sso(authed, idp):
    assert TestClient(authed.app).get("/api/auth/status").json()["sso"] == {"enabled": True, "label": "authentik"}


def test_sign_in_creates_user_with_group_roles(authed, idp):
    c, r = sign_in(authed, idp)
    assert r.status_code == 303 and r.headers["location"] == "/jobs", error_of(r)
    status = c.get("/api/auth/status").json()
    assert status["username"] == "alice" and status["auth_source"] == "oidc"
    assert "slice.run" in status["permissions"] and "users.manage" not in status["permissions"]
    token_req = idp.token_requests[0]
    assert token_req.headers["authorization"].startswith("Basic ")
    assert b"code_verifier=" in token_req.content
    alice = next(u for u in authed.get("/api/users").json() if u["username"] == "alice")
    assert alice["auth_source"] == "oidc" and alice["sso_linked"]
    assert TestClient(authed.app).post("/api/auth/login", json={"username": "alice", "password": "!"}).status_code == 401

    # Roles follow the provider's groups at every sign-in.
    idp.claims = {"groups": []}
    c, r = sign_in(authed, idp)
    assert c.get("/api/auth/status").json()["permissions"] == ["library.view", "printers.view"]


def test_state_must_come_back_to_the_same_browser(authed, idp):
    _, r = sign_in(authed, idp, tamper_state=True)
    assert "another browser" in error_of(r)


@pytest.mark.parametrize(
    "claims, message",
    [
        ({"aud": "someone-else"}, "couldn't be verified"),
        ({"iss": "https://evil.test/"}, "couldn't be verified"),
        ({"exp": int(time.time()) - 600}, "couldn't be verified"),
        ({"nonce": "replayed"}, "couldn't be verified"),
        ({"preferred_username": "no spaces allowed"}, "can't be used here"),
    ],
)
def test_bad_tokens_are_rejected(authed, idp, claims, message):
    idp.claims = claims
    c, r = sign_in(authed, idp)
    assert message in error_of(r)
    assert c.get("/api/auth/status").json()["authenticated"] is False


def test_unsigned_token_is_rejected(authed, idp, monkeypatch):
    monkeypatch.setattr(idp, "id_token", lambda: jwt.encode({"sub": "abc123"}, None, algorithm="none"))
    _, r = sign_in(authed, idp)
    assert "unsupported method" in error_of(r)


def test_hs256_with_client_secret(authed, idp):
    idp.alg = "HS256"
    c, r = sign_in(authed, idp)
    assert r.headers["location"] == "/jobs", error_of(r)


def test_existing_accounts_only_link_when_allowed(authed, idp):
    idp.claims = {"preferred_username": "admin", "sub": "admin-sub"}
    _, r = sign_in(authed, idp)
    assert "already exists" in error_of(r)
    cfg = authed.get("/api/sso").json()
    authed.put("/api/sso", json={**cfg, "link_existing": True})
    c, r = sign_in(authed, idp)
    assert r.headers["location"] == "/jobs", error_of(r)
    assert c.get("/api/auth/status").json()["username"] == "admin"
    # Local password still works for the linked account.
    assert TestClient(authed.app).post("/api/auth/login", json={"username": "admin", "password": PASSWORD}).status_code == 200


def test_no_account_creation_or_disabled(authed, idp):
    cfg = authed.get("/api/sso").json()
    authed.put("/api/sso", json={**cfg, "create_users": False})
    _, r = sign_in(authed, idp)
    assert "no HomeSlice account" in error_of(r)
    authed.put("/api/sso", json={**cfg, "create_users": True})
    sign_in(authed, idp)
    alice = next(u for u in authed.get("/api/users").json() if u["username"] == "alice")
    authed.patch(f"/api/users/{alice['id']}", json={"disabled": True})
    _, r = sign_in(authed, idp)
    assert "disabled" in error_of(r)


def test_provider_errors_and_settings_validation(authed, idp):
    c = TestClient(authed.app)
    r = c.get("/api/auth/oidc/callback", params={"error": "access_denied"}, follow_redirects=False)
    assert error_of(r) == "Sign-in was cancelled."
    cfg = authed.get("/api/sso").json()
    assert authed.post("/api/sso/test", json={"issuer": ISSUER}).json()["issuer"] == ISSUER
    assert authed.post("/api/sso/test", json={"issuer": "https://idp.test/other/"}).status_code == 400
    assert authed.put("/api/sso", json={**cfg, "issuer": "http://idp.test/"}).status_code == 400
    assert authed.put("/api/sso", json={**cfg, "default_roles": [999]}).status_code == 400
    authed.put("/api/sso", json={**cfg, "enabled": False})
    r = c.get("/api/auth/oidc/login", follow_redirects=False)
    assert "turned off" in error_of(r)
    assert c.get("/api/sso").status_code == 401


def test_open_redirects_are_blocked(authed, idp):
    _, r = sign_in(authed, idp, next_path="//evil.test/x")
    assert r.headers["location"] == "/"


def test_provider_token_errors_are_explained(authed, idp):
    idp.token_error = {"error": "invalid_client", "error_description": "Client authentication failed"}
    _, r = sign_in(authed, idp)
    message = error_of(r)
    assert "Client Secret" in message and "Client authentication failed" in message
    idp.token_error = {"error": "invalid_grant"}
    _, r = sign_in(authed, idp)
    assert "redirect URI" in error_of(r)


def test_client_secret_post_and_trimmed_secret(authed, idp):
    idp.auth_methods = ["client_secret_post", "client_secret_basic"]
    authed.app.state.services.oidc._discovery = None
    cfg = authed.get("/api/sso").json()
    authed.put("/api/sso", json={**cfg, "client_secret": f"  {SECRET}\n"})
    _, r = sign_in(authed, idp)
    assert r.headers["location"] == "/jobs", error_of(r)
    sent = parse_qs(idp.token_requests[-1].content.decode())
    assert sent["client_secret"] == [SECRET] and sent["client_id"] == ["homeslice"]
    assert "authorization" not in idp.token_requests[-1].headers
