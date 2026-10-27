import asyncio
import ipaddress
import socket
import time
import uuid
from typing import Any
from urllib.parse import urlsplit

import httpx

from ..crypto import SecretBox
from ..db import Database
from .moonraker import (
    MoonrakerClient,
    PrinterError,
    check_lan_ip,
    host_label,
    normalize_url,
    parse_scan_subnet,
    probe_hostname,
)

KINDS = ("moonraker",)
SCAN_SUBNET_KEY = "printers.scan_subnet"
SCAN_CONCURRENCY = 64


def parse_printer_id(printer_id: str) -> str:
    try:
        return uuid.UUID(printer_id).hex
    except ValueError:
        raise PrinterError("Unknown printer") from None


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


class Printers:
    def __init__(self, db: Database, secrets: SecretBox) -> None:
        self._db = db
        self._secrets = secrets
        self._locks: dict[str, asyncio.Lock] = {}
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
            "address": row["resolved_ip"],
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
            "resolved_ip": None,
            "created_at": time.time(),
        }
        self._db.execute(
            "INSERT INTO printers(id, name, kind, url, api_key, resolved_ip, created_at) "
            "VALUES(:id, :name, :kind, :url, :api_key, :resolved_ip, :created_at)",
            record,
        )
        return self._public(record)

    def delete(self, printer_id: str) -> bool:
        return self._db.execute("DELETE FROM printers WHERE id=?", (parse_printer_id(printer_id),)) > 0

    def scan_subnet(self) -> str | None:
        return self._db.get_setting(SCAN_SUBNET_KEY) or None

    def set_scan_subnet(self, value: str | None) -> str | None:
        subnet = parse_scan_subnet(value) if value and value.strip() else None
        self._db.set_setting(SCAN_SUBNET_KEY, subnet or "")
        return subnet

    async def client(self, printer_id: str) -> MoonrakerClient:
        pid = parse_printer_id(printer_id)
        row = self._db.one("SELECT * FROM printers WHERE id=?", (pid,))
        if row is None:
            raise PrinterError("Unknown printer")
        api_key = self._secrets.decrypt(row["api_key"]) if row["api_key"] else None
        base = await self._resolve_base(pid, row["url"])
        return MoonrakerClient(base, api_key, transport=self.transport, resolve=self.resolve)

    async def _resolve_base(self, pid: str, url: str) -> str:
        """Hostnames are located by the name Moonraker reports, so DHCP address changes are followed."""
        parts = urlsplit(url)
        host = parts.hostname or ""
        if _is_ip(host):
            return url
        port = f":{parts.port}" if parts.port else ""

        def base_for(ip: str) -> str:
            return f"{parts.scheme}://{ip}{port}"

        want = host_label(host)
        async with self._locks.setdefault(pid, asyncio.Lock()):
            row = self._db.one("SELECT resolved_ip FROM printers WHERE id=?", (pid,))
            cached = row["resolved_ip"] if row else None
            async with httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                trust_env=False,
                limits=httpx.Limits(max_connections=SCAN_CONCURRENCY),
            ) as http:
                if cached and host_label(await probe_hostname(http, base_for(cached)) or "") == want:
                    return base_for(cached)
                found = await self._from_dns(host, parts.port) or await self._scan(http, want, base_for)
            if found is None:
                subnet = self.scan_subnet()
                if subnet is None:
                    raise PrinterError(
                        f"Cannot find {host}. Set the LAN range to search under Settings \u2192 Printers, or use its IP address."
                    )
                raise PrinterError(f"No Klipper printer named '{want}' answered in {subnet}")
            self._db.execute("UPDATE printers SET resolved_ip=? WHERE id=?", (found, pid))
            return base_for(found)

    async def _from_dns(self, host: str, port: int | None) -> str | None:
        if not self.resolve:
            return None
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, port or 80, type=socket.SOCK_STREAM)
        except socket.gaierror:
            return None
        for info in infos:
            ip = info[4][0]
            try:
                check_lan_ip(ip)
            except PrinterError:
                continue
            if ":" not in ip:
                return ip
        return None

    async def _scan(self, http: httpx.AsyncClient, want: str, base_for) -> str | None:
        subnet = self.scan_subnet()
        if subnet is None:
            return None
        sem = asyncio.Semaphore(SCAN_CONCURRENCY)

        async def probe(ip: str) -> str | None:
            async with sem:
                name = await probe_hostname(http, base_for(ip))
            return ip if name and host_label(name) == want else None

        tasks = [asyncio.create_task(probe(str(ip))) for ip in ipaddress.ip_network(subnet).hosts()]
        try:
            for done in asyncio.as_completed(tasks):
                if ip := await done:
                    return ip
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return None
