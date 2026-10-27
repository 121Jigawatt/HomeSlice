import asyncio
import hashlib
import ipaddress
import json
import socket
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx


class FetchError(Exception):
    pass


def _host_allowed(host: str, allowed: frozenset[str]) -> bool:
    for entry in allowed:
        if entry.startswith("*."):
            if host.endswith(entry[1:]):
                return True
        elif host == entry:
            return True
    return False


def _check_ip(ip_str: str) -> None:
    ip = ipaddress.ip_address(ip_str.split("%", 1)[0])
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if not ip.is_global or ip.is_multicast:
        raise FetchError(f"Refusing to connect to non-public address {ip}")


class SafeFetcher:
    """Outbound HTTP for untrusted URLs: https only, host allowlist, public IPs only, capped size/redirects."""

    def __init__(
        self,
        allowed_hosts: set[str],
        *,
        max_bytes: int,
        timeout: float = 60.0,
        max_redirects: int = 5,
        transport: httpx.AsyncBaseTransport | None = None,
        resolve: bool = True,
    ) -> None:
        if not allowed_hosts:
            raise ValueError("allowed_hosts must not be empty")
        self._allowed = frozenset(h.lower() for h in allowed_hosts)
        self._max_bytes = max_bytes
        self._timeout = timeout
        self._max_redirects = max_redirects
        self._transport = transport
        self._resolve = resolve
        self._verify_peer = transport is None

    def check_url(self, url: str) -> str:
        parts = urlsplit(url)
        if parts.scheme != "https":
            raise FetchError("Only https URLs are allowed")
        if parts.username or parts.password:
            raise FetchError("Credentials in URLs are not allowed")
        host = (parts.hostname or "").lower().rstrip(".")
        if not host or not _host_allowed(host, self._allowed):
            raise FetchError(f"Host '{host}' is not in the allowlist")
        if parts.port not in (None, 443):
            raise FetchError("Non-standard ports are not allowed")
        return host

    async def _check_dns(self, host: str) -> None:
        if not self._resolve:
            return
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except socket.gaierror as e:
            raise FetchError(f"Cannot resolve {host}") from e
        for info in infos:
            _check_ip(info[4][0])

    def _check_peer(self, response: httpx.Response) -> None:
        if not self._verify_peer:
            return
        stream = response.extensions.get("network_stream")
        addr = stream.get_extra_info("server_addr") if stream else None
        if not addr:
            raise FetchError("Could not verify remote address")
        _check_ip(addr[0])

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=self._transport,
            timeout=self._timeout,
            follow_redirects=False,
            trust_env=False,
            headers={"User-Agent": "HomeSlice/0.1"},
        )

    async def _open(self, client: httpx.AsyncClient, url: str, headers: dict[str, str] | None) -> httpx.Response:
        for _ in range(self._max_redirects + 1):
            host = self.check_url(url)
            await self._check_dns(host)
            request = client.build_request("GET", url, headers=headers)
            response = await client.send(request, stream=True)
            self._check_peer(response)
            if response.is_redirect:
                location = response.headers.get("location", "")
                await response.aclose()
                url = urljoin(url, location)
                # Auth headers must never follow a redirect to another host.
                if headers and urlsplit(url).hostname != host:
                    headers = {k: v for k, v in headers.items() if k.lower() != "authorization"}
                continue
            if response.status_code >= 400:
                await response.aclose()
                raise FetchError(f"HTTP {response.status_code} from {host}")
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > self._max_bytes:
                await response.aclose()
                raise FetchError("Remote file is too large")
            return response
        raise FetchError("Too many redirects")

    async def get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        async with self._client() as client:
            response = await self._open(client, url, headers)
            try:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > self._max_bytes:
                        raise FetchError("Response is too large")
            finally:
                await response.aclose()
        try:
            return json.loads(body)
        except ValueError as e:
            raise FetchError("Invalid JSON response") from e

    async def download(self, url: str, dest: Path, headers: dict[str, str] | None = None) -> tuple[str, int]:
        """Streams to dest (must not exist). Returns (sha256 hex, size)."""
        digest = hashlib.sha256()
        size = 0
        async with self._client() as client:
            response = await self._open(client, url, headers)
            try:
                f = dest.open("xb")
                try:
                    with f:
                        async for chunk in response.aiter_bytes():
                            size += len(chunk)
                            if size > self._max_bytes:
                                raise FetchError("Remote file is too large")
                            digest.update(chunk)
                            f.write(chunk)
                except BaseException:
                    dest.unlink(missing_ok=True)
                    raise
            finally:
                await response.aclose()
        return digest.hexdigest(), size
