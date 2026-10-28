from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from ..auth import AuthError, Session
from ..rbac import PERMISSIONS, RbacError
from .deps import client_ip, require

router = APIRouter(prefix="/api", tags=["users"])
manage = require("users.manage")


class UserBody(BaseModel):
    username: str = Field(max_length=64)
    password: str = Field(max_length=256)
    roles: list[int] = Field(default_factory=list, max_length=50)
    must_change_password: bool = True


class UserUpdateBody(BaseModel):
    roles: list[int] | None = Field(default=None, max_length=50)
    disabled: bool | None = None


class ResetBody(BaseModel):
    password: str = Field(max_length=256)
    must_change_password: bool = True


class RoleBody(BaseModel):
    name: str = Field(max_length=64)
    description: str = Field(default="", max_length=200)
    permissions: list[str] = Field(default_factory=list, max_length=100)


def _fail(e: Exception) -> HTTPException:
    if isinstance(e, LookupError):
        return HTTPException(404, str(e.args[0]) if e.args else "Not found")
    return HTTPException(400, str(e))


@router.get("/users")
def list_users(request: Request, _: Session = Depends(manage)) -> list[dict]:
    return request.app.state.services.auth.rbac.users()


@router.post("/users", status_code=201)
def create_user(body: UserBody, request: Request, session: Session = Depends(manage)) -> dict:
    services = request.app.state.services
    try:
        user = services.auth.rbac.create_user(body.username, body.password, body.roles, body.must_change_password)
    except (RbacError, AuthError) as e:
        raise _fail(e) from None
    services.auth.audit(session.username, "user_create", user["username"], client_ip(request))
    return user


@router.patch("/users/{user_id}")
def update_user(user_id: int, body: UserUpdateBody, request: Request, session: Session = Depends(manage)) -> dict:
    services = request.app.state.services
    if user_id == session.user_id and body.disabled:
        raise HTTPException(400, "You can't disable your own account")
    try:
        user = services.auth.rbac.update_user(user_id, body.roles, body.disabled)
    except (RbacError, LookupError) as e:
        raise _fail(e) from None
    changes = []
    if body.roles is not None:
        names = {r["id"]: r["name"] for r in services.auth.rbac.roles()}
        changes.append("roles=" + ",".join(names.get(r, "?") for r in user["roles"]))
    if body.disabled is not None:
        changes.append("disabled" if body.disabled else "enabled")
    services.auth.audit(session.username, "user_update", f"{user['username']} {' '.join(changes)}", client_ip(request))
    return user


@router.post("/users/{user_id}/password", status_code=204)
def reset_password(user_id: int, body: ResetBody, request: Request, session: Session = Depends(manage)) -> None:
    services = request.app.state.services
    if user_id == session.user_id:
        raise HTTPException(400, "Change your own password under Settings → Change password")
    try:
        services.auth.rbac.reset_password(user_id, body.password, body.must_change_password)
    except (AuthError, LookupError) as e:
        raise _fail(e) from None
    services.auth.audit(session.username, "user_password_reset", str(user_id), client_ip(request))


@router.delete("/users/{user_id}", status_code=204)
def delete_user(user_id: int, request: Request, session: Session = Depends(manage)) -> None:
    services = request.app.state.services
    if user_id == session.user_id:
        raise HTTPException(400, "You can't delete your own account")
    try:
        username = services.auth.rbac.delete_user(user_id)
    except (RbacError, LookupError) as e:
        raise _fail(e) from None
    services.auth.audit(session.username, "user_delete", username, client_ip(request))


@router.get("/roles")
def list_roles(request: Request, _: Session = Depends(manage)) -> dict:
    return {
        "roles": request.app.state.services.auth.rbac.roles(),
        "permissions": [
            {"key": key, "group": group, "label": label, "description": description}
            for key, (group, label, description) in PERMISSIONS.items()
        ],
    }


@router.post("/roles", status_code=201)
def create_role(body: RoleBody, request: Request, session: Session = Depends(manage)) -> dict:
    services = request.app.state.services
    try:
        role = services.auth.rbac.create_role(body.name, body.description, body.permissions)
    except RbacError as e:
        raise _fail(e) from None
    services.auth.audit(session.username, "role_create", role["name"], client_ip(request))
    return role


@router.put("/roles/{role_id}")
def update_role(role_id: int, body: RoleBody, request: Request, session: Session = Depends(manage)) -> dict:
    services = request.app.state.services
    try:
        role = services.auth.rbac.update_role(role_id, body.name, body.description, body.permissions)
    except (RbacError, LookupError) as e:
        raise _fail(e) from None
    services.auth.audit(
        session.username, "role_update", f"{role['name']}: {','.join(role['permissions'])}", client_ip(request)
    )
    return role


@router.delete("/roles/{role_id}", status_code=204)
def delete_role(role_id: int, request: Request, session: Session = Depends(manage)) -> None:
    services = request.app.state.services
    try:
        name = services.auth.rbac.delete_role(role_id)
    except (RbacError, LookupError) as e:
        raise _fail(e) from None
    services.auth.audit(session.username, "role_delete", name, client_ip(request))
