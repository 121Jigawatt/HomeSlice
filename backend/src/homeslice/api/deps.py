import hmac
import uuid
from collections.abc import Callable
from urllib.parse import urlsplit

from fastapi import HTTPException, Request, Response

from ..auth import Session

SESSION_COOKIE = "homeslice_session"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def set_session_cookie(response: Response, settings, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=settings.session_ttl_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="strict",
        path="/",
    )


def check_origin(request: Request) -> None:
    """Rejects cross-site state-changing requests (defence in depth alongside SameSite + CSRF token)."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "Cross-origin request blocked")


def _session(request: Request) -> Session:
    services = request.app.state.services
    session = services.auth.get_session(request.cookies.get(SESSION_COOKIE))
    if session is None:
        raise HTTPException(401, "Not authenticated")
    if request.method in UNSAFE_METHODS:
        check_origin(request)
        sent = request.headers.get("x-csrf-token", "")
        if not hmac.compare_digest(sent.encode(), session.csrf_token.encode()):
            raise HTTPException(403, "Missing or invalid CSRF token")
    return session


def require_account(request: Request) -> Session:
    """Signed in, even if the user still has to replace a password an admin gave them."""
    return _session(request)


def require_session(request: Request) -> Session:
    session = _session(request)
    if session.must_change_password:
        raise HTTPException(403, "Change your password before continuing")
    return session


def require(*permissions: str) -> Callable[[Request], Session]:
    """A signed-in user holding any of the given permissions."""

    def dependency(request: Request) -> Session:
        session = require_session(request)
        if not any(session.can(p) for p in permissions):
            raise HTTPException(403, "You don't have permission to do that")
        return session

    return dependency


def deny_unless(session: Session, *permissions: str) -> None:
    if not any(session.can(p) for p in permissions):
        raise HTTPException(403, "You don't have permission to do that")


def owns(session: Session, owner: str | None) -> bool:
    return (owner or "").lower() == session.username.lower()


def visible_job(request: Request, session: Session, job_id: str) -> dict:
    """The job, if this user started it or may see everyone's; otherwise 404 so ids don't leak."""
    try:
        job_id = uuid.UUID(job_id).hex
    except ValueError:
        raise HTTPException(404, "Not found") from None
    job = request.app.state.services.jobs.get(job_id)
    if job is None or not (owns(session, job.get("owner")) or session.can("jobs.view_all")):
        raise HTTPException(404, "Not found")
    return job
