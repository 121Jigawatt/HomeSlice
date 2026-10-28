import logging
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from ..auth import Session
from ..oidc import OidcConfig, OidcError
from ..rbac import RbacError
from .deps import client_ip, require, set_session_cookie

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["sso"])
STATE_COOKIE = "homeslice_oidc"
STATE_PATH = "/api/auth/oidc"


def _fail(message: str) -> RedirectResponse:
    response = RedirectResponse(f"/?sso_error={quote(message)}", status_code=303)
    response.delete_cookie(STATE_COOKIE, path=STATE_PATH)
    return response


@router.get("/auth/oidc/login", include_in_schema=False)
async def oidc_login(request: Request, next: str | None = Query(default=None, max_length=500)) -> RedirectResponse:
    services = request.app.state.services
    try:
        state, url = await services.oidc.begin(next)
    except OidcError as e:
        return _fail(str(e))
    response = RedirectResponse(url, status_code=303)
    # Lax, not Strict: it has to come back on the provider's top-level redirect to the callback.
    response.set_cookie(
        STATE_COOKIE, state, max_age=600, httponly=True, secure=services.settings.cookie_secure, samesite="lax", path=STATE_PATH
    )
    return response


@router.get("/auth/oidc/callback", include_in_schema=False)
async def oidc_callback(
    request: Request,
    state: str = Query(default="", max_length=200),
    code: str = Query(default="", max_length=2000),
    error: str | None = Query(default=None, max_length=200),
) -> RedirectResponse:
    services = request.app.state.services
    ip = client_ip(request)
    label = services.oidc.config().label
    if error:
        services.auth.audit(None, "sso_failed", f"provider: {error}", ip)
        return _fail("Sign-in was cancelled." if error == "access_denied" else f"{label} couldn't sign you in ({error}).")
    if not state or not code:
        return _fail("The sign-in response was incomplete. Start again.")
    try:
        claims, next_path = await services.oidc.finish(state, code, request.cookies.get(STATE_COOKIE))
        user_id = services.oidc.resolve_user(claims)
    except (OidcError, RbacError) as e:
        services.auth.audit(None, "sso_failed", str(e), ip)
        return _fail(str(e))
    token, session = services.auth.sso_login(user_id)
    services.auth.audit(session.username, "login_sso", label, ip)
    response = RedirectResponse(next_path, status_code=303)
    response.delete_cookie(STATE_COOKIE, path=STATE_PATH)
    set_session_cookie(response, services.settings, token)
    return response


class GroupRole(BaseModel):
    group: str = Field(max_length=200)
    role: int


class SsoBody(BaseModel):
    enabled: bool = False
    issuer: str = Field(default="", max_length=300)
    client_id: str = Field(default="", max_length=200)
    # None keeps the stored secret; "" clears it.
    client_secret: str | None = Field(default=None, max_length=500)
    base_url: str = Field(default="", max_length=300)
    label: str = Field(default="authentik", max_length=40)
    username_claim: str = Field(default="preferred_username", max_length=64)
    create_users: bool = True
    link_existing: bool = False
    default_roles: list[int] = Field(default_factory=list, max_length=50)
    group_roles: list[GroupRole] = Field(default_factory=list, max_length=100)
    verify_tls: bool = True


manage = require("users.manage")


@router.get("/sso")
def get_sso(request: Request, _: Session = Depends(manage)) -> dict:
    return request.app.state.services.oidc.public_config()


@router.put("/sso")
async def put_sso(body: SsoBody, request: Request, session: Session = Depends(manage)) -> dict:
    services = request.app.state.services
    cfg = OidcConfig(**body.model_dump(exclude={"client_secret", "group_roles"}), group_roles=[g.model_dump() for g in body.group_roles])
    try:
        services.oidc.save(cfg, body.client_secret)
        if cfg.enabled:
            await services.oidc.discover()
    except OidcError as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "sso_settings", f"enabled={cfg.enabled} issuer={cfg.issuer}", client_ip(request))
    return services.oidc.public_config()


class TestBody(BaseModel):
    issuer: str = Field(default="", max_length=300)
    verify_tls: bool = True


@router.post("/sso/test")
async def test_sso(body: TestBody, request: Request, _: Session = Depends(manage)) -> dict:
    """Checks a provider's discovery document before the settings are saved."""
    services = request.app.state.services
    cfg = OidcConfig(issuer=body.issuer.strip(), verify_tls=body.verify_tls)
    if urlsplit(cfg.issuer).scheme != "https":
        raise HTTPException(400, "The provider address must start with https://")
    try:
        doc = await services.oidc.fetch_discovery(cfg)
    except OidcError as e:
        raise HTTPException(400, str(e)) from None
    return {"issuer": doc["issuer"], "authorization_endpoint": doc["authorization_endpoint"]}
