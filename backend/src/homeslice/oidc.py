"""Single sign-on with an OpenID Connect provider such as authentik (authorization code flow with PKCE)."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from urllib.parse import urlencode, urlsplit

import httpx
import jwt

from .crypto import SecretBox
from .db import Database
from .rbac import USERNAME_RE, Rbac, RbacError

log = logging.getLogger(__name__)

STATE_TTL_S = 600
MAX_PENDING = 500
DISCOVERY_TTL_S = 3600
# HS256 is what authentik uses when the provider has no signing key; it's keyed with the client secret.
ASYMMETRIC_ALGS = {"RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"}
ALLOWED_ALGS = ASYMMETRIC_ALGS | {"HS256"}
SETTING = "oidc.config"
SECRET_SETTING = "oidc.client_secret"


class OidcError(Exception):
    """Shown to the person signing in, so keep it free of internals."""


@dataclass
class OidcConfig:
    enabled: bool = False
    issuer: str = ""
    client_id: str = ""
    # Address people open HomeSlice by; the redirect URI is derived from it.
    base_url: str = ""
    label: str = "authentik"
    username_claim: str = "preferred_username"
    create_users: bool = True
    link_existing: bool = False
    default_roles: list[int] = field(default_factory=list)
    # [{"group": "...", "role": role_id}]; when set, roles follow the provider's groups at every sign-in.
    group_roles: list[dict] = field(default_factory=list)
    verify_tls: bool = True

    @property
    def redirect_uri(self) -> str:
        return self.base_url.rstrip("/") + "/api/auth/oidc/callback"


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def safe_next(path: str | None) -> str:
    """Same-origin relative paths only (no open redirects)."""
    if path and path.startswith("/") and not path.startswith("//") and "\\" not in path and len(path) <= 500:
        return path
    return "/"


class Oidc:
    def __init__(self, db: Database, box: SecretBox, rbac: Rbac, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._db = db
        self._box = box
        self._rbac = rbac
        self.transport = transport
        self._discovery: tuple[float, str, dict] | None = None
        self._jwks: dict[str, dict] = {}

    # Configuration

    def config(self) -> OidcConfig:
        raw = self._db.get_setting(SETTING)
        if not raw:
            maker = next((r["id"] for r in self._rbac.roles() if r["name"] == "Maker"), None)
            return OidcConfig(default_roles=[maker] if maker else [])
        data = json.loads(raw)
        return OidcConfig(**{k: v for k, v in data.items() if k in OidcConfig.__dataclass_fields__})

    def public_config(self) -> dict:
        cfg = self.config()
        return {**asdict(cfg), "redirect_uri": cfg.redirect_uri if cfg.base_url else "", "has_secret": bool(self._secret())}

    def _secret(self) -> str:
        token = self._db.get_setting(SECRET_SETTING)
        return self._box.decrypt(token) if token else ""

    def save(self, cfg: OidcConfig, client_secret: str | None) -> None:
        cfg.issuer = cfg.issuer.strip()
        cfg.client_id = cfg.client_id.strip()
        cfg.base_url = cfg.base_url.strip().rstrip("/")
        cfg.label = cfg.label.strip()[:40] or "authentik"
        cfg.username_claim = cfg.username_claim.strip() or "preferred_username"
        if cfg.issuer and urlsplit(cfg.issuer).scheme != "https":
            raise OidcError("The provider address must start with https://")
        if cfg.base_url and urlsplit(cfg.base_url).scheme not in ("https", "http"):
            raise OidcError("The HomeSlice address must start with https://")
        known = {r["id"] for r in self._rbac.roles()}
        if set(cfg.default_roles) - known or any(m.get("role") not in known for m in cfg.group_roles):
            raise OidcError("Unknown role")
        cfg.group_roles = [
            {"group": str(m["group"]).strip()[:200], "role": m["role"]} for m in cfg.group_roles if str(m.get("group", "")).strip()
        ]
        if client_secret is not None:
            self._db.set_setting(SECRET_SETTING, self._box.encrypt(client_secret) if client_secret else "")
        if cfg.enabled and not (cfg.issuer and cfg.client_id and cfg.base_url and self._secret()):
            raise OidcError("Fill in the provider address, client ID, client secret and HomeSlice address first")
        self._db.set_setting(SETTING, json.dumps(asdict(cfg)))
        self._discovery = None
        self._jwks = {}

    # Provider metadata

    def _client(self, cfg: OidcConfig) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self.transport, verify=cfg.verify_tls, timeout=10, follow_redirects=False, headers={"accept": "application/json"}
        )

    async def _get_json(self, cfg: OidcConfig, url: str, **kwargs) -> dict:
        if urlsplit(url).scheme != "https":
            raise OidcError("The provider gave a non-HTTPS address")
        try:
            async with self._client(cfg) as client:
                r = await client.get(url, **kwargs)
        except httpx.HTTPError as e:
            log.warning("OIDC request to %s failed: %s", url, e)
            raise OidcError(f"Couldn't reach {cfg.label}") from None
        if r.status_code != 200 or len(r.content) > 1_000_000:
            log.warning("OIDC request to %s returned %s", url, r.status_code)
            raise OidcError(f"{cfg.label} answered with an error ({r.status_code})")
        try:
            return r.json()
        except ValueError:
            raise OidcError(f"{cfg.label} sent something that isn't JSON") from None

    async def discover(self, cfg: OidcConfig | None = None) -> dict:
        cfg = cfg or self.config()
        if self._discovery and self._discovery[1] == cfg.issuer and time.time() - self._discovery[0] < DISCOVERY_TTL_S:
            return self._discovery[2]
        doc = await self.fetch_discovery(cfg)
        self._discovery = (time.time(), cfg.issuer, doc)
        return doc

    async def fetch_discovery(self, cfg: OidcConfig) -> dict:
        doc = await self._get_json(cfg, cfg.issuer.rstrip("/") + "/.well-known/openid-configuration")
        if str(doc.get("issuer", "")).rstrip("/") != cfg.issuer.rstrip("/"):
            raise OidcError("The provider's issuer doesn't match the address configured in HomeSlice")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not str(doc.get(key, "")).startswith("https://"):
                raise OidcError(f"The provider's {key} is missing or not HTTPS")
        return doc

    # Sign-in

    async def begin(self, next_path: str | None) -> tuple[str, str]:
        """Returns (state, URL to send the browser to)."""
        cfg = self.config()
        if not cfg.enabled:
            raise OidcError("Single sign-on is turned off")
        doc = await self.discover(cfg)
        state, nonce, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(32), secrets.token_urlsafe(64)
        now = time.time()
        with self._db.tx() as conn:
            conn.execute("DELETE FROM oidc_states WHERE created_at < ?", (now - STATE_TTL_S,))
            if conn.execute("SELECT COUNT(*) FROM oidc_states").fetchone()[0] >= MAX_PENDING:
                raise OidcError("Too many sign-ins in progress. Try again in a few minutes.")
            conn.execute(
                "INSERT INTO oidc_states(state, nonce, verifier, next, created_at) VALUES(?, ?, ?, ?, ?)",
                (state, nonce, verifier, safe_next(next_path), now),
            )
        query = urlencode(
            {
                "response_type": "code",
                "client_id": cfg.client_id,
                "redirect_uri": cfg.redirect_uri,
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": _b64(hashlib.sha256(verifier.encode()).digest()),
                "code_challenge_method": "S256",
            }
        )
        sep = "&" if "?" in doc["authorization_endpoint"] else "?"
        return state, f"{doc['authorization_endpoint']}{sep}{query}"

    def _take_state(self, state: str) -> sqlite3.Row:
        with self._db.tx() as conn:
            row = conn.execute("SELECT * FROM oidc_states WHERE state=?", (state,)).fetchone()
            conn.execute("DELETE FROM oidc_states WHERE state=?", (state,))
        if row is None or time.time() - row["created_at"] > STATE_TTL_S:
            raise OidcError("This sign-in link expired. Start again.")
        return row

    async def finish(self, state: str, code: str, browser_state: str | None) -> tuple[dict, str]:
        """Exchanges the code and validates the ID token. Returns (claims, next path)."""
        if not browser_state or not secrets.compare_digest(state, browser_state):
            raise OidcError("This sign-in was started in another browser or tab. Start again.")
        row = self._take_state(state)
        cfg = self.config()
        if not cfg.enabled:
            raise OidcError("Single sign-on is turned off")
        doc = await self.discover(cfg)
        secret = self._secret()
        form = {"grant_type": "authorization_code", "code": code, "redirect_uri": cfg.redirect_uri, "code_verifier": row["verifier"]}
        methods = doc.get("token_endpoint_auth_methods_supported") or ["client_secret_basic"]
        auth = None
        if "client_secret_basic" in methods:
            auth = httpx.BasicAuth(cfg.client_id, secret)
        else:
            form |= {"client_id": cfg.client_id, "client_secret": secret}
        try:
            async with self._client(cfg) as client:
                r = await client.post(doc["token_endpoint"], data=form, auth=auth)
        except httpx.HTTPError as e:
            log.warning("OIDC token request failed: %s", e)
            raise OidcError(f"Couldn't reach {cfg.label}") from None
        if r.status_code != 200:
            log.warning("OIDC token endpoint returned %s: %s", r.status_code, r.text[:300])
            raise OidcError(f"{cfg.label} refused the sign-in. Check the client ID, secret and redirect URI.")
        tokens = r.json()
        claims = await self._verify_id_token(cfg, doc, str(tokens.get("id_token", "")), secret, row["nonce"])
        if doc.get("userinfo_endpoint") and tokens.get("access_token"):
            info = await self._get_json(cfg, doc["userinfo_endpoint"], headers={"authorization": f"Bearer {tokens['access_token']}"})
            if info.get("sub") == claims["sub"]:
                claims = {**info, **claims}
        return claims, row["next"]

    async def _key_for(self, cfg: OidcConfig, doc: dict, kid: str | None, alg: str):
        for attempt in range(2):
            if not self._jwks or attempt:
                keys = (await self._get_json(cfg, doc["jwks_uri"])).get("keys") or []
                self._jwks = {k.get("kid", ""): k for k in keys if isinstance(k, dict)}
            candidates = [k for k in self._jwks.values() if not kid or k.get("kid") == kid]
            if len(candidates) == 1 or (kid and candidates):
                return jwt.PyJWK(candidates[0], algorithm=alg).key
        raise OidcError(f"{cfg.label} signed the sign-in with a key HomeSlice doesn't know")

    async def _verify_id_token(self, cfg: OidcConfig, doc: dict, token: str, secret: str, nonce: str) -> dict:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise OidcError(f"{cfg.label} didn't send a valid ID token") from None
        alg = header.get("alg")
        if alg not in ALLOWED_ALGS:
            raise OidcError(f"{cfg.label} signed the sign-in with an unsupported method ({alg})")
        key = secret.encode() if alg == "HS256" else await self._key_for(cfg, doc, header.get("kid"), alg)
        try:
            claims = jwt.decode(
                token,
                key,
                algorithms=[alg],
                audience=cfg.client_id,
                issuer=doc["issuer"],
                leeway=60,
                options={"require": ["exp", "iat", "iss", "aud", "sub"]},
            )
        except jwt.PyJWTError as e:
            log.warning("OIDC ID token rejected: %s", e)
            raise OidcError(f"The sign-in from {cfg.label} couldn't be verified") from None
        if not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError(f"The sign-in from {cfg.label} couldn't be verified")
        aud = claims["aud"]
        if isinstance(aud, list) and len(aud) > 1 and claims.get("azp") != cfg.client_id:
            raise OidcError(f"The sign-in from {cfg.label} was meant for another app")
        return claims

    # Accounts

    def resolve_user(self, claims: dict) -> int:
        """The HomeSlice user for these claims: linked by subject, else matched or created by username."""
        cfg = self.config()
        sub = str(claims["sub"])
        username = str(claims.get(cfg.username_claim) or "").strip()
        groups = claims.get("groups") or []
        groups = {str(g) for g in groups} if isinstance(groups, list) else set()
        row = self._db.one("SELECT id, username, disabled FROM users WHERE oidc_sub=?", (sub,))
        if row is None:
            if not USERNAME_RE.fullmatch(username):
                raise OidcError(
                    f'Your {cfg.label} username "{username[:40]}" can\'t be used here: 3-32 letters, digits, . _ - only.'
                )
            existing = self._db.one("SELECT id, username, disabled, oidc_sub FROM users WHERE username=?", (username,))
            if existing is not None:
                if not cfg.link_existing or existing["oidc_sub"]:
                    raise OidcError(
                        f'A HomeSlice account called "{username}" already exists. Ask an administrator to allow linking '
                        "existing accounts, or to rename one of them."
                    )
                self._db.execute("UPDATE users SET oidc_sub=? WHERE id=?", (sub, existing["id"]))
                row = existing
            elif not cfg.create_users:
                raise OidcError(f'There\'s no HomeSlice account for "{username}". Ask an administrator to add you.')
            else:
                user = self._rbac.create_sso_user(username, sub, self._roles_for(cfg, groups) or cfg.default_roles)
                return user["id"]
        if row["disabled"]:
            raise OidcError("This account is disabled. Ask an administrator to turn it back on.")
        if cfg.group_roles:
            try:
                self._rbac.update_user(row["id"], self._roles_for(cfg, groups) or cfg.default_roles, None)
            except RbacError as e:
                log.warning("Kept %s's roles: %s", row["username"], e)
        return row["id"]

    @staticmethod
    def _roles_for(cfg: OidcConfig, groups: set[str]) -> list[int]:
        return list(dict.fromkeys(m["role"] for m in cfg.group_roles if m["group"] in groups))
