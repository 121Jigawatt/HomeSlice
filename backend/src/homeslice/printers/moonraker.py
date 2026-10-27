import asyncio
import ipaddress
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

# Klipper print_stats states in which starting a new print is safe.
IDLE_STATES = {"standby", "complete", "cancelled", "error"}


class PrinterError(Exception):
    pass


class PrinterBusy(PrinterError):
    pass


def normalize_url(url: str) -> str:
    """Base URL only (scheme, host, port); paths, queries and credentials are rejected."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https"):
        raise PrinterError("Printer address must start with http:// or https://")
    if parts.username or parts.password:
        raise PrinterError("Credentials in the address are not allowed")
    if not parts.hostname:
        raise PrinterError("Printer address needs a host")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise PrinterError("Use just the printer's address, e.g. http://10.0.0.5")
    try:
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        raise PrinterError("Invalid port") from None
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"{parts.scheme}://{host}{port}"


def check_lan_ip(ip_str: str) -> None:
    """Printers must be on the LAN. Loopback is refused because it would bypass the proxy's auth."""
    ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_unspecified:
        raise PrinterError(f"{ip} is not a local network address")


class MoonrakerClient:
    """Klipper/Moonraker HTTP API (used by Mainsail and Fluidd)."""

    def __init__(
        self,
        base_url: str,
        api_key: str | None = None,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve: bool = True,
        timeout: float = 10.0,
    ) -> None:
        self._base = normalize_url(base_url)
        self._api_key = api_key
        self._transport = transport
        self._resolve = resolve
        self._timeout = timeout

    async def _check_host(self) -> None:
        if not self._resolve:
            return
        host = urlsplit(self._base).hostname or ""
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            check_lan_ip(host)
            return
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror:
            raise PrinterError(
                f"Cannot resolve {host}. Use the printer's IP address; .local names don't resolve inside the container."
            ) from None
        for info in infos:
            check_lan_ip(info[4][0])

    def _check_peer(self, response: httpx.Response) -> None:
        if self._transport is not None:
            return
        stream = response.extensions.get("network_stream")
        addr = stream.get_extra_info("server_addr") if stream else None
        if not addr:
            raise PrinterError("Could not verify the printer's address")
        check_lan_ip(addr[0])

    async def _request(self, method: str, path: str, *, timeout: float | None = None, **kwargs: Any) -> Any:
        await self._check_host()
        headers = {"X-Api-Key": self._api_key} if self._api_key else {}
        async with httpx.AsyncClient(
            base_url=self._base,
            transport=self._transport,
            timeout=timeout or self._timeout,
            follow_redirects=False,
            trust_env=False,
            headers=headers,
        ) as client:
            try:
                response = await client.send(client.build_request(method, path, **kwargs), stream=True)
            except httpx.HTTPError as e:
                raise PrinterError(f"Cannot reach printer ({type(e).__name__})") from None
            try:
                self._check_peer(response)
                await response.aread()
            finally:
                await response.aclose()
        if response.status_code in (401, 403):
            raise PrinterError("Printer rejected the request; check its API key")
        try:
            body = response.json()
        except ValueError:
            body = None
        if response.status_code >= 400:
            message = ""
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                message = str(body["error"].get("message", ""))[:200]
            raise PrinterError(f"Printer returned HTTP {response.status_code}" + (f": {message}" if message else ""))
        if not isinstance(body, dict):
            raise PrinterError("Unexpected response from printer")
        return body.get("result", body)

    async def status(self) -> dict[str, Any]:
        result = await self._request("GET", "/printer/objects/query?print_stats&display_status")
        objects = result.get("status", {}) if isinstance(result, dict) else {}
        stats = objects.get("print_stats", {})
        display = objects.get("display_status", {})
        return {
            "state": str(stats.get("state", "unknown")),
            "filename": stats.get("filename") or None,
            "progress": display.get("progress"),
            "message": stats.get("message") or None,
        }

    async def upload(self, path: Path, remote_name: str, *, start: bool) -> dict[str, Any]:
        if start:
            state = (await self.status())["state"]
            if state not in IDLE_STATES:
                raise PrinterBusy(f"Printer is {state}; not starting a new print")
        data = {"root": "gcodes"}
        if start:
            data["print"] = "true"
        with path.open("rb") as f:
            result = await self._request(
                "POST",
                "/server/files/upload",
                files={"file": (remote_name, f, "application/octet-stream")},
                data=data,
                timeout=600,
            )
        return {
            "filename": remote_name,
            "print_started": bool(result.get("print_started")) if isinstance(result, dict) else False,
        }
