import time
import uuid
from typing import Any

import httpx

from ..crypto import SecretBox
from ..db import Database
from .moonraker import MoonrakerClient, PrinterError, normalize_url

KINDS = ("moonraker",)


def parse_printer_id(printer_id: str) -> str:
    try:
        return uuid.UUID(printer_id).hex
    except ValueError:
        raise PrinterError("Unknown printer") from None


class Printers:
    def __init__(self, db: Database, secrets: SecretBox) -> None:
        self._db = db
        self._secrets = secrets
        # Overridable in tests.
        self.transport: httpx.AsyncBaseTransport | None = None
        self.resolve = True

    @staticmethod
    def _public(row: Any) -> dict[str, Any]:
        return {
            "id": row["id"],
            "name": row["name"],
            "kind": row["kind"],
            "url": row["url"],
            "has_api_key": bool(row["api_key"]),
            "created_at": row["created_at"],
        }

    def list(self) -> list[dict[str, Any]]:
        return [self._public(r) for r in self._db.all("SELECT * FROM printers ORDER BY name COLLATE NOCASE")]

    def get(self, printer_id: str) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM printers WHERE id=?", (parse_printer_id(printer_id),))
        return self._public(row) if row else None

    def add(self, name: str, url: str, api_key: str | None, kind: str = "moonraker") -> dict[str, Any]:
        name = name.strip()
        if not 1 <= len(name) <= 64:
            raise PrinterError("Name must be 1-64 characters")
        if kind not in KINDS:
            raise PrinterError("Unsupported printer type")
        record = {
            "id": uuid.uuid4().hex,
            "name": name,
            "kind": kind,
            "url": normalize_url(url),
            "api_key": self._secrets.encrypt(api_key.strip()) if api_key and api_key.strip() else None,
            "created_at": time.time(),
        }
        self._db.execute(
            "INSERT INTO printers(id, name, kind, url, api_key, created_at) "
            "VALUES(:id, :name, :kind, :url, :api_key, :created_at)",
            record,
        )
        return self._public(record)

    def delete(self, printer_id: str) -> bool:
        return self._db.execute("DELETE FROM printers WHERE id=?", (parse_printer_id(printer_id),)) > 0

    def client(self, printer_id: str) -> MoonrakerClient:
        row = self._db.one("SELECT * FROM printers WHERE id=?", (parse_printer_id(printer_id),))
        if row is None:
            raise PrinterError("Unknown printer")
        api_key = self._secrets.decrypt(row["api_key"]) if row["api_key"] else None
        return MoonrakerClient(row["url"], api_key, transport=self.transport, resolve=self.resolve)
