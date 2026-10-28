import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from .config import Settings
from .crypto import write_private_file
from .db import Database
from .rbac import ADMIN_ROLE, USERNAME_RE, Rbac, ensure_builtin_roles, permissions_for

log = logging.getLogger(__name__)

MAX_PASSWORD_LENGTH = 256


class AuthError(Exception):
    pass


class RateLimited(AuthError):
    pass


@dataclass(frozen=True)
class Session:
    user_id: int
    username: str
    csrf_token: str
    token_hash: str
    permissions: frozenset[str] = frozenset()
    must_change_password: bool = False

    def can(self, permission: str) -> bool:
        return permission in self.permissions


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthService:
    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        self._ph = PasswordHasher()
        # Verified against when the user doesn't exist, so timing doesn't reveal valid usernames.
        self._dummy_hash = self._ph.hash(secrets.token_urlsafe(16))
        ensure_builtin_roles(db)
        self.rbac = Rbac(db, self._ph, self.validate_password)

    @property
    def _setup_token_file(self) -> Path:
        return self._settings.config_dir / "setup-token"

    def has_users(self) -> bool:
        return self._db.one("SELECT 1 FROM users LIMIT 1") is not None

    def ensure_setup_token(self) -> str | None:
        """First run only: a one-time token, printed to the container log, is required to create the admin."""
        if self.has_users():
            self._setup_token_file.unlink(missing_ok=True)
            return None
        if not self._setup_token_file.exists():
            write_private_file(self._setup_token_file, secrets.token_urlsafe(24).encode())
        token = self._setup_token_file.read_text().strip()
        log.warning("HomeSlice first-run setup token: %s", token)
        return token

    def validate_password(self, password: str) -> None:
        if len(password) < self._settings.min_password_length:
            raise AuthError(f"Password must be at least {self._settings.min_password_length} characters")
        if len(password) > MAX_PASSWORD_LENGTH:
            raise AuthError("Password is too long")

    def create_initial_user(self, setup_token: str, username: str, password: str) -> None:
        if not self._setup_token_file.exists():
            raise AuthError("Setup is already complete")
        expected = self._setup_token_file.read_text().strip()
        if not hmac.compare_digest(expected.encode(), setup_token.strip().encode()):
            raise AuthError("Invalid setup token")
        if not USERNAME_RE.fullmatch(username):
            raise AuthError("Username must be 3-32 characters: letters, digits, . _ -")
        self.validate_password(password)
        pw_hash = self._ph.hash(password)
        with self._db.tx() as conn:
            if conn.execute("SELECT 1 FROM users LIMIT 1").fetchone():
                raise AuthError("Setup is already complete")
            user_id = conn.execute(
                "INSERT INTO users(username, password_hash, created_at) VALUES(?, ?, ?)",
                (username, pw_hash, time.time()),
            ).lastrowid
            conn.execute(
                "INSERT INTO user_roles(user_id, role_id) SELECT ?, id FROM roles WHERE name=?", (user_id, ADMIN_ROLE)
            )
        self._setup_token_file.unlink(missing_ok=True)

    def _failure_keys(self, username: str, ip: str) -> list[str]:
        return [f"ip:{ip}", f"user:{username.lower()}"]

    def _is_locked(self, keys: list[str], now: float) -> bool:
        cutoff = now - self._settings.login_window_s
        self._db.execute("DELETE FROM login_failures WHERE at < ?", (cutoff,))
        for key in keys:
            row = self._db.one("SELECT COUNT(*) AS n FROM login_failures WHERE key=? AND at>=?", (key, cutoff))
            if row and row["n"] >= self._settings.login_max_failures:
                return True
        return False

    def login(self, username: str, password: str, ip: str) -> tuple[str, Session]:
        now = time.time()
        keys = self._failure_keys(username, ip)
        if self._is_locked(keys, now):
            raise RateLimited("Too many failed attempts. Try again later.")

        row = self._db.one(
            "SELECT id, username, password_hash, disabled, must_change_password FROM users WHERE username=?", (username,)
        )
        ok = False
        if len(password) <= MAX_PASSWORD_LENGTH:
            try:
                ok = self._ph.verify(row["password_hash"] if row else self._dummy_hash, password) and row is not None
            except (VerificationError, InvalidHashError):
                ok = False
        if not ok:
            with self._db.tx() as conn:
                conn.executemany("INSERT INTO login_failures(key, at) VALUES(?, ?)", [(k, now) for k in keys])
            raise AuthError("Invalid username or password")

        self._db.execute("DELETE FROM login_failures WHERE key=?", (keys[1],))
        if row["disabled"]:
            raise AuthError("This account is disabled. Ask an administrator to turn it back on.")
        if self._ph.check_needs_rehash(row["password_hash"]):
            self._db.execute("UPDATE users SET password_hash=? WHERE id=?", (self._ph.hash(password), row["id"]))
        self._db.execute("UPDATE users SET last_login=? WHERE id=?", (now, row["id"]))
        return self._create_session(row["id"], row["username"], bool(row["must_change_password"]), now)

    def _create_session(self, user_id: int, username: str, must_change: bool, now: float) -> tuple[str, Session]:
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        self._db.execute(
            "INSERT INTO sessions(token_hash, user_id, csrf_token, created_at, expires_at, last_seen) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (token_hash, user_id, csrf, now, now + self._settings.session_ttl_hours * 3600, now),
        )
        return token, Session(user_id, username, csrf, token_hash, permissions_for(self._db, user_id), must_change)

    def get_session(self, token: str | None) -> Session | None:
        if not token or len(token) > 128:
            return None
        now = time.time()
        token_hash = _hash_token(token)
        row = self._db.one(
            "SELECT s.user_id, s.csrf_token, s.expires_at, s.last_seen, u.username, u.disabled, u.must_change_password "
            "FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash=?",
            (token_hash,),
        )
        if row is None:
            return None
        idle_limit = self._settings.session_idle_hours * 3600
        if row["disabled"] or row["expires_at"] < now or now - row["last_seen"] > idle_limit:
            self._db.execute("DELETE FROM sessions WHERE token_hash=?", (token_hash,))
            return None
        if now - row["last_seen"] > 60:
            self._db.execute("UPDATE sessions SET last_seen=? WHERE token_hash=?", (now, token_hash))
        return Session(
            row["user_id"],
            row["username"],
            row["csrf_token"],
            token_hash,
            permissions_for(self._db, row["user_id"]),
            bool(row["must_change_password"]),
        )

    def logout(self, session: Session) -> None:
        self._db.execute("DELETE FROM sessions WHERE token_hash=?", (session.token_hash,))

    def change_password(self, session: Session, old: str, new: str) -> None:
        row = self._db.one("SELECT password_hash FROM users WHERE id=?", (session.user_id,))
        try:
            self._ph.verify(row["password_hash"], old)
        except (VerificationError, InvalidHashError):
            raise AuthError("Current password is incorrect") from None
        self.validate_password(new)
        if session.must_change_password and hmac.compare_digest(old.encode(), new.encode()):
            raise AuthError("Choose a new password, not the one you were given")
        with self._db.tx() as conn:
            conn.execute(
                "UPDATE users SET password_hash=?, must_change_password=0 WHERE id=?",
                (self._ph.hash(new), session.user_id),
            )
            conn.execute(
                "DELETE FROM sessions WHERE user_id=? AND token_hash<>?", (session.user_id, session.token_hash)
            )

    def audit(self, actor: str | None, action: str, detail: str = "", ip: str | None = None) -> None:
        self._db.execute(
            "INSERT INTO audit_log(at, actor, action, detail, ip) VALUES(?, ?, ?, ?, ?)",
            (time.time(), actor, action, detail[:1000], ip),
        )
