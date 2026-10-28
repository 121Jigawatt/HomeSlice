from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from ..auth import AuthError, RateLimited, Session
from .deps import SESSION_COOKIE, check_origin, client_ip, require, require_account, set_session_cookie

router = APIRouter(prefix="/api/auth", tags=["auth"])


class SetupBody(BaseModel):
    setup_token: str = Field(max_length=200)
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class LoginBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)


class PasswordBody(BaseModel):
    current_password: str = Field(max_length=256)
    new_password: str = Field(max_length=256)


@router.get("/status")
def status(request: Request) -> dict:
    services = request.app.state.services
    session = services.auth.get_session(request.cookies.get(SESSION_COOKIE))
    sso = services.oidc.config()
    source = None
    if session:
        row = services.db.one("SELECT auth_source FROM users WHERE id=?", (session.user_id,))
        source = row["auth_source"] if row else None
    return {
        "setup_required": not services.auth.has_users(),
        "authenticated": session is not None,
        "username": session.username if session else None,
        "csrf_token": session.csrf_token if session else None,
        "permissions": sorted(session.permissions) if session else [],
        "must_change_password": session.must_change_password if session else False,
        "auth_source": source,
        "sso": {"enabled": sso.enabled, "label": sso.label} if sso.enabled else None,
    }


@router.post("/setup", status_code=204, dependencies=[Depends(check_origin)])
def setup(body: SetupBody, request: Request) -> None:
    services = request.app.state.services
    try:
        services.auth.create_initial_user(body.setup_token, body.username, body.password)
    except AuthError as e:
        services.auth.audit(None, "setup_failed", str(e), client_ip(request))
        raise HTTPException(400, str(e)) from None
    services.auth.audit(body.username, "setup_complete", "", client_ip(request))


@router.post("/login", dependencies=[Depends(check_origin)])
def login(body: LoginBody, request: Request, response: Response) -> dict:
    services = request.app.state.services
    ip = client_ip(request)
    try:
        token, session = services.auth.login(body.username, body.password, ip)
    except RateLimited as e:
        services.auth.audit(body.username[:64], "login_locked", "", ip)
        raise HTTPException(429, str(e)) from None
    except AuthError as e:
        services.auth.audit(body.username[:64], "login_failed", "", ip)
        raise HTTPException(401, str(e)) from None
    services.auth.audit(session.username, "login", "", ip)
    set_session_cookie(response, services.settings, token)
    return {"username": session.username, "csrf_token": session.csrf_token}


@router.post("/logout", status_code=204)
def logout(request: Request, response: Response, session: Session = Depends(require_account)) -> None:
    request.app.state.services.auth.logout(session)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.post("/password", status_code=204)
def change_password(body: PasswordBody, request: Request, session: Session = Depends(require_account)) -> None:
    services = request.app.state.services
    try:
        services.auth.change_password(session, body.current_password, body.new_password)
    except AuthError as e:
        raise HTTPException(400, str(e)) from None
    services.auth.audit(session.username, "password_changed", "", client_ip(request))


@router.get("/verify", status_code=204)
def verify(_: Session = Depends(require("orca.desktop"))) -> None:
    """Used by the bundled nginx (auth_request) to gate the streamed desktop GUI."""
