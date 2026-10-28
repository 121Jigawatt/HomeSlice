import asyncio
import ipaddress
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx

# Klipper print_stats states in which starting a new print is safe.
IDLE_STATES = {"standby", "complete", "cancelled", "error"}
PROBE_TIMEOUT = 1.5
MAX_SCAN_ADDRESSES = 1024


class PrinterError(Exception):
    pass


class PrinterBusy(PrinterError):
    pass


class PrinterMismatch(PrinterError):
    """G-code was sliced for a different printer profile than this machine uses."""


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


def parse_scan_subnet(value: str) -> str:
    try:
        net = ipaddress.ip_network(value.strip(), strict=False)
    except ValueError:
        raise PrinterError("Enter a range like 10.69.69.0/24") from None
    if net.version != 4 or not net.is_private or net.is_loopback or net.is_link_local:
        raise PrinterError("Range must be a private IPv4 network")
    if net.num_addresses > MAX_SCAN_ADDRESSES:
        raise PrinterError("Range is too large (at most /22)")
    return str(net)


def host_label(name: str) -> str:
    """'spi-xi.local' and 'SPI-XI' both become 'spi-xi'."""
    return name.strip().rstrip(".").split(".")[0].lower()


async def probe_hostname(client: httpx.AsyncClient, base: str) -> str | None:
    """Hostname a Moonraker instance reports, or None if nothing usable answers."""
    try:
        response = await client.get(f"{base}/printer/info", timeout=PROBE_TIMEOUT)
        if response.status_code != 200:
            return None
        hostname = response.json().get("result", {}).get("hostname")
    except (httpx.HTTPError, ValueError, AttributeError):
        return None
    return hostname if isinstance(hostname, str) else None


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

    @property
    def base_url(self) -> str:
        return self._base

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

    async def telemetry(self) -> dict[str, Any]:
        result = await self._request(
            "GET", "/printer/objects/query?print_stats&display_status&virtual_sdcard&extruder&heater_bed"
        )
        objects = result.get("status", {}) if isinstance(result, dict) else {}

        def obj(name: str) -> dict[str, Any]:
            value = objects.get(name)
            return value if isinstance(value, dict) else {}

        stats = obj("print_stats")
        progress = obj("display_status").get("progress")
        if not progress:
            progress = obj("virtual_sdcard").get("progress")
        return {
            "state": str(stats.get("state", "unknown")),
            "filename": stats.get("filename") or None,
            "message": stats.get("message") or None,
            "progress": _num(progress),
            "elapsed": _num(stats.get("print_duration")),
            "extruder": _temps(obj("extruder")),
            "bed": _temps(obj("heater_bed")),
        }

    async def estimated_time(self, filename: str) -> float | None:
        try:
            result = await self._request("GET", "/server/files/metadata", params={"filename": filename})
        except PrinterError:
            return None
        return _num(result.get("estimated_time")) if isinstance(result, dict) else None

    async def webcams(self) -> list[dict[str, Any]]:
        try:
            result = await self._request("GET", "/server/webcams/list")
        except PrinterError:
            return []
        cams = result.get("webcams") if isinstance(result, dict) else None
        return [
            {
                "name": str(c.get("name") or f"Camera {i + 1}")[:64],
                "stream_url": c.get("stream_url") if isinstance(c.get("stream_url"), str) else None,
                "snapshot_url": c.get("snapshot_url") if isinstance(c.get("snapshot_url"), str) else None,
            }
            for i, c in enumerate(cams or [])
            if isinstance(c, dict) and c.get("enabled", True)
        ]

    def webcam_url(self, raw: str) -> str:
        """Webcam URLs may be relative or point at another port, but never at another host."""
        url = urljoin(self._base + "/", raw.strip())
        parts, base = urlsplit(url), urlsplit(self._base)
        if parts.scheme not in ("http", "https") or parts.username or parts.password:
            raise PrinterError("Unsupported webcam address")
        if parts.hostname != base.hostname:
            raise PrinterError("Webcam must be served by the printer itself")
        check_lan_ip(parts.hostname or "")
        return url

    async def open_stream(self, url: str) -> tuple[httpx.AsyncClient, httpx.Response]:
        """Caller must close both. Only image or MJPEG responses are accepted."""
        client = httpx.AsyncClient(
            transport=self._transport,
            timeout=httpx.Timeout(10.0, read=30.0),
            follow_redirects=False,
            trust_env=False,
        )
        try:
            response = await client.send(client.build_request("GET", url), stream=True)
            self._check_peer(response)
            ctype = response.headers.get("content-type", "").lower()
            if response.status_code != 200 or not (
                ctype.startswith("image/") or ctype.startswith("multipart/x-mixed-replace")
            ):
                await response.aclose()
                raise PrinterError(f"Webcam returned HTTP {response.status_code} ({ctype or 'no type'})")
        except httpx.HTTPError as e:
            await client.aclose()
            raise PrinterError(f"Cannot reach webcam ({type(e).__name__})") from None
        except BaseException:
            await client.aclose()
            raise
        return client, response


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _temps(heater: dict[str, Any]) -> dict[str, float | None] | None:
    if not heater:
        return None
    return {"temp": _num(heater.get("temperature")), "target": _num(heater.get("target"))}
