import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY,
    username TEXT UNIQUE NOT NULL COLLATE NOCASE,
    password_hash TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions(
    token_hash TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    csrf_token TEXT NOT NULL,
    created_at REAL NOT NULL,
    expires_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS login_failures(key TEXT NOT NULL, at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS idx_login_failures ON login_failures(key, at);
CREATE TABLE IF NOT EXISTS audit_log(
    id INTEGER PRIMARY KEY,
    at REAL NOT NULL,
    actor TEXT,
    action TEXT NOT NULL,
    detail TEXT,
    ip TEXT
);
CREATE TABLE IF NOT EXISTS models(
    id TEXT PRIMARY KEY,
    filename TEXT NOT NULL,
    ext TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    source_url TEXT,
    license TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS jobs(
    id TEXT PRIMARY KEY,
    params TEXT NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    stats TEXT,
    created_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS printers(
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    api_key TEXT,
    resolved_ip TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS print_queue(
    id TEXT PRIMARY KEY,
    printer_id TEXT NOT NULL REFERENCES printers(id) ON DELETE CASCADE,
    job_id TEXT NOT NULL,
    file TEXT NOT NULL,
    position REAL NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    owner TEXT,
    created_at REAL NOT NULL,
    sent_at REAL
);
CREATE INDEX IF NOT EXISTS idx_print_queue ON print_queue(printer_id, status, position);
CREATE TABLE IF NOT EXISTS roles(
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL COLLATE NOCASE,
    description TEXT NOT NULL DEFAULT '',
    permissions TEXT NOT NULL,
    builtin INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS user_roles(
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES roles(id) ON DELETE CASCADE,
    PRIMARY KEY(user_id, role_id)
);
"""

# Columns added after a table first shipped: (table, column, type).
ADDED_COLUMNS = [
    ("printers", "resolved_ip", "TEXT"),
    ("models", "owner", "TEXT"),
    ("jobs", "owner", "TEXT"),
    ("jobs", "title", "TEXT"),
    ("jobs", "stage", "TEXT"),
    ("jobs", "started_at", "REAL"),
    ("jobs", "error_code", "INTEGER"),
    ("jobs", "error_kind", "TEXT"),
    # 1 while the bed may still hold a finished print; blocks remote and queued starts.
    ("printers", "needs_attention", "INTEGER NOT NULL DEFAULT 0"),
    ("printers", "attention_reason", "TEXT"),
    ("printers", "last_state", "TEXT"),
    # The OrcaSlicer printer preset this machine prints; G-code sliced with any other preset is refused.
    ("printers", "printer_preset", "TEXT"),
    ("printers", "default_process", "TEXT"),
    ("printers", "default_filaments", "TEXT"),
    ("printers", "default_bed_type", "TEXT"),
    ("printers", "bed_coverage", "INTEGER NOT NULL DEFAULT 50"),
    ("users", "disabled", "INTEGER NOT NULL DEFAULT 0"),
    # Set when an admin creates the account or resets its password; cleared once the user picks their own.
    ("users", "must_change_password", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "last_login", "REAL"),
]


class Database:
    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            for table, column, coltype in ADDED_COLUMNS:
                existing = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
                if column not in existing:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {coltype}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")

    def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, params).fetchone()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, params).rowcount

    def get_setting(self, key: str, default: str | None = None) -> str | None:
        row = self.one("SELECT value FROM settings WHERE key=?", (key,))
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        self.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def close(self) -> None:
        with self._lock:
            self._conn.close()
