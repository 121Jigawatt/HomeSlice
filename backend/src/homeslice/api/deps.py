import hmac
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

from ..auth import Session

SESSION_COOKIE = "homeslice_session"
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def check_origin(request: Request) -> None:
    """Rejects cross-site state-changing requests (defence in depth alongside SameSite + CSRF token)."""
    origin = request.headers.get("origin")
    if origin is None:
        return
    if urlsplit(origin).netloc.lower() != request.headers.get("host", "").lower():
        raise HTTPException(403, "Cross-origin request blocked")


def require_session(request: Request) -> Session:
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
