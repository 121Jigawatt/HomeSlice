"""Roles and permissions. Users hold one or more roles; a user's permissions are the union of their roles'."""

from __future__ import annotations

import json
import re
import sqlite3
import time

from argon2 import PasswordHasher

from .db import Database

# key -> (group, label, description). Order here is the order the Settings page shows them in.
PERMISSIONS: dict[str, tuple[str, str, str]] = {
    "library.view": ("Library", "Browse models", "See, preview and download models in the library."),
    "library.upload": ("Library", "Add models", "Upload and import models, and delete their own."),
    "library.delete": ("Library", "Delete any model", "Remove models other people added."),
    "slice.run": ("Slicing", "Slice", "Slice models, and retry, cancel or delete their own jobs."),
    "jobs.view_all": ("Slicing", "See everyone's jobs", "Without this, people only see the jobs they started."),
    "jobs.manage_all": ("Slicing", "Manage everyone's jobs", "Cancel, delete and clean up any job."),
    "printers.view": ("Printers", "See printers", "Printer status, cameras and the print queue."),
    "printers.print": (
        "Printers",
        "Print",
        "Send G-code to printers, queue prints, cancel their own queued prints and confirm a bed is clear.",
    ),
    "printers.queue": ("Printers", "Manage the queue", "Reorder the print queue and cancel anyone's queued prints."),
    "printers.manage": ("Printers", "Set up printers", "Add and remove printers and set their profiles."),
    "orca.desktop": (
        "OrcaSlicer",
        "Full OrcaSlicer",
        "Open the complete OrcaSlicer desktop, which can edit profiles and read the app's files.",
    ),
    "orca.manage": ("OrcaSlicer", "Update OrcaSlicer", "Install, update and roll back OrcaSlicer versions."),
    "activity.view": ("Administration", "See activity", "The activity log of everyone's actions."),
    "users.manage": ("Administration", "Manage users", "Add and remove users, reset passwords and edit roles."),
}

ADMIN_ROLE = "Admin"
BUILTIN_ROLES: list[tuple[str, str, list[str]]] = [
    (ADMIN_ROLE, "Everything, including users and roles.", list(PERMISSIONS)),
    (
        "Operator",
        "Runs the print farm: everything except users, printer setup and OrcaSlicer updates.",
        [
            "library.view", "library.upload", "library.delete", "slice.run", "jobs.view_all", "jobs.manage_all",
            "printers.view", "printers.print", "printers.queue", "orca.desktop", "activity.view",
        ],
    ),
    (
        "Maker",
        "Adds models, slices and prints their own work.",
        ["library.view", "library.upload", "slice.run", "printers.view", "printers.print"],
    ),
    ("Viewer", "Looks around without changing anything.", ["library.view", "printers.view"]),
]

ROLE_NAME_RE = re.compile(r"^[\w .-]{1,40}$")
# Email addresses are allowed, since identity providers often use them as usernames.
USERNAME_RE = re.compile(r"^[A-Za-z0-9_.@+-]{3,64}$")
USERNAME_RULE = "Username must be 3-64 characters: letters, digits, . _ - @ +"


class RbacError(Exception):
    pass


def ensure_builtin_roles(db: Database) -> None:
    """Seeds the default roles on first start; users that predate roles become admins."""
    with db.tx() as conn:
        if conn.execute("SELECT 1 FROM roles LIMIT 1").fetchone():
            return
        now = time.time()
        for name, description, perms in BUILTIN_ROLES:
            conn.execute(
                "INSERT INTO roles(name, description, permissions, builtin, created_at) VALUES(?, ?, ?, ?, ?)",
                (name, description, json.dumps(perms), int(name == ADMIN_ROLE), now),
            )
        conn.execute(
            "INSERT INTO user_roles(user_id, role_id) SELECT u.id, r.id FROM users u, roles r WHERE r.name=?",
            (ADMIN_ROLE,),
        )


def permissions_for(db: Database, user_id: int) -> frozenset[str]:
    rows = db.all(
        "SELECT r.name, r.permissions FROM roles r JOIN user_roles ur ON ur.role_id = r.id WHERE ur.user_id=?",
        (user_id,),
    )
    perms: set[str] = set()
    for r in rows:
        # The Admin role always means everything, including permissions added in later versions.
        perms.update(PERMISSIONS if r["name"] == ADMIN_ROLE else json.loads(r["permissions"]))
    return frozenset(p for p in perms if p in PERMISSIONS)


def _assert_manager_left(conn: sqlite3.Connection) -> None:
    """Some enabled user must still be able to manage users, or nobody could fix roles again."""
    for row in conn.execute(
        "SELECT r.name, r.permissions FROM roles r JOIN user_roles ur ON ur.role_id = r.id "
        "JOIN users u ON u.id = ur.user_id WHERE u.disabled = 0"
    ):
        if row["name"] == ADMIN_ROLE or "users.manage" in json.loads(row["permissions"]):
            return
    raise RbacError("At least one active user must keep the ability to manage users.")


class Rbac:
    def __init__(self, db: Database, hasher: PasswordHasher, validate_password) -> None:
        self._db = db
        self._ph = hasher
        self._validate_password = validate_password

    # Roles

    def roles(self) -> list[dict]:
        rows = self._db.all(
            "SELECT r.*, (SELECT COUNT(*) FROM user_roles ur WHERE ur.role_id = r.id) AS users "
            "FROM roles r ORDER BY r.builtin DESC, r.id"
        )
        out = []
        for r in rows:
            perms = list(PERMISSIONS) if r["name"] == ADMIN_ROLE else json.loads(r["permissions"])
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "description": r["description"],
                    "permissions": [p for p in PERMISSIONS if p in perms],
                    "builtin": bool(r["builtin"]),
                    "users": r["users"],
                }
            )
        return out

    def _clean_role(self, name: str, permissions: list[str]) -> tuple[str, list[str]]:
        name = name.strip()
        if not ROLE_NAME_RE.fullmatch(name):
            raise RbacError("Role names are 1-40 letters, digits, spaces, . _ -")
        unknown = set(permissions) - PERMISSIONS.keys()
        if unknown:
            raise RbacError(f"Unknown permission {sorted(unknown)[0]}")
        return name, [p for p in PERMISSIONS if p in permissions]

    def create_role(self, name: str, description: str, permissions: list[str]) -> dict:
        name, perms = self._clean_role(name, permissions)
        try:
            with self._db.tx() as conn:
                role_id = conn.execute(
                    "INSERT INTO roles(name, description, permissions, builtin, created_at) VALUES(?, ?, ?, 0, ?)",
                    (name, description.strip()[:200], json.dumps(perms), time.time()),
                ).lastrowid
        except sqlite3.IntegrityError:
            raise RbacError(f'A role called "{name}" already exists') from None
        return self._role(role_id)

    def update_role(self, role_id: int, name: str, description: str, permissions: list[str]) -> dict:
        row = self._db.one("SELECT name FROM roles WHERE id=?", (role_id,))
        if row is None:
            raise LookupError("Unknown role")
        if row["name"] == ADMIN_ROLE:
            raise RbacError("The Admin role always has every permission and can't be changed.")
        name, perms = self._clean_role(name, permissions)
        try:
            with self._db.tx() as conn:
                conn.execute(
                    "UPDATE roles SET name=?, description=?, permissions=? WHERE id=?",
                    (name, description.strip()[:200], json.dumps(perms), role_id),
                )
                _assert_manager_left(conn)
        except sqlite3.IntegrityError:
            raise RbacError(f'A role called "{name}" already exists') from None
        return self._role(role_id)

    def delete_role(self, role_id: int) -> str:
        row = self._db.one("SELECT name FROM roles WHERE id=?", (role_id,))
        if row is None:
            raise LookupError("Unknown role")
        if row["name"] == ADMIN_ROLE:
            raise RbacError("The Admin role can't be deleted.")
        with self._db.tx() as conn:
            conn.execute("DELETE FROM roles WHERE id=?", (role_id,))
            _assert_manager_left(conn)
        return row["name"]

    def _role(self, role_id: int) -> dict:
        return next(r for r in self.roles() if r["id"] == role_id)

    # Users

    def users(self) -> list[dict]:
        rows = self._db.all(
            "SELECT id, username, disabled, must_change_password, created_at, last_login, auth_source, oidc_sub "
            "FROM users ORDER BY username"
        )
        links = self._db.all("SELECT user_id, role_id FROM user_roles")
        out = []
        for r in rows:
            out.append(
                {
                    "id": r["id"],
                    "username": r["username"],
                    "disabled": bool(r["disabled"]),
                    "must_change_password": bool(r["must_change_password"]),
                    "created_at": r["created_at"],
                    "last_login": r["last_login"],
                    "auth_source": r["auth_source"],
                    "sso_linked": bool(r["oidc_sub"]),
                    "roles": [link["role_id"] for link in links if link["user_id"] == r["id"]],
                }
            )
        return out

    def _user(self, user_id: int) -> dict:
        found = next((u for u in self.users() if u["id"] == user_id), None)
        if found is None:
            raise LookupError("Unknown user")
        return found

    def _set_roles(self, conn: sqlite3.Connection, user_id: int, role_ids: list[int]) -> None:
        known = {r["id"] for r in conn.execute("SELECT id FROM roles")}
        if set(role_ids) - known:
            raise RbacError("Unknown role")
        conn.execute("DELETE FROM user_roles WHERE user_id=?", (user_id,))
        conn.executemany(
            "INSERT INTO user_roles(user_id, role_id) VALUES(?, ?)", [(user_id, r) for r in dict.fromkeys(role_ids)]
        )

    def create_user(self, username: str, password: str, role_ids: list[int], must_change_password: bool) -> dict:
        username = username.strip()
        if not USERNAME_RE.fullmatch(username):
            raise RbacError(USERNAME_RULE)
        self._validate_password(password)
        pw_hash = self._ph.hash(password)
        try:
            with self._db.tx() as conn:
                user_id = conn.execute(
                    "INSERT INTO users(username, password_hash, must_change_password, created_at) VALUES(?, ?, ?, ?)",
                    (username, pw_hash, int(must_change_password), time.time()),
                ).lastrowid
                self._set_roles(conn, user_id, role_ids)
        except sqlite3.IntegrityError:
            raise RbacError(f'The username "{username}" is taken') from None
        return self._user(user_id)

    def create_sso_user(self, username: str, sub: str, role_ids: list[int]) -> dict:
        # "!" is never a valid argon2 hash, so these accounts can't sign in with a password.
        try:
            with self._db.tx() as conn:
                user_id = conn.execute(
                    "INSERT INTO users(username, password_hash, auth_source, oidc_sub, created_at) VALUES(?, '!', 'oidc', ?, ?)",
                    (username, sub, time.time()),
                ).lastrowid
                self._set_roles(conn, user_id, role_ids)
        except sqlite3.IntegrityError:
            raise RbacError(f'The username "{username}" is taken') from None
        return self._user(user_id)

    def update_user(self, user_id: int, role_ids: list[int] | None, disabled: bool | None) -> dict:
        self._user(user_id)
        with self._db.tx() as conn:
            if role_ids is not None:
                self._set_roles(conn, user_id, role_ids)
            if disabled is not None:
                conn.execute("UPDATE users SET disabled=? WHERE id=?", (int(disabled), user_id))
                if disabled:
                    conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))
            _assert_manager_left(conn)
        return self._user(user_id)

    def reset_password(self, user_id: int, password: str, must_change_password: bool) -> None:
        self._user(user_id)
        self._validate_password(password)
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=? WHERE id=?",
                (self._ph.hash(password), int(must_change_password), user_id),
            )
            conn.execute("DELETE FROM sessions WHERE user_id=?", (user_id,))

    def delete_user(self, user_id: int) -> str:
        username = self._user(user_id)["username"]
        with self._db.tx() as conn:
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            _assert_manager_left(conn)
        return username
