import asyncio
import hmac
import logging
import platform
import re
import shutil
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ..config import Settings
from ..net.safe_fetch import FetchError, SafeFetcher
from .versions import VersionStore

log = logging.getLogger(__name__)

CHANNELS = ("stable", "prerelease", "nightly")
NIGHTLY_TAG = "nightly-builds"
GITHUB_HOSTS = {
    "api.github.com",
    "github.com",
    "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}
_SHA256_RE = re.compile(r"^sha256:([0-9a-f]{64})$")
_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SAFE_ID_RE = re.compile(r"[^A-Za-z0-9._-]")
MAX_APPIMAGE_BYTES = 1536 * 1024**2


class UpdateError(Exception):
    pass


@dataclass
class ReleaseInfo:
    tag: str
    name: str
    published_at: str
    prerelease: bool
    html_url: str
    notes: str
    asset_name: str | None
    asset_url: str | None
    asset_size: int | None
    asset_sha256: str | None

    @property
    def version_id(self) -> str:
        suffix = (self.asset_sha256 or "unverified")[:12]
        return _SAFE_ID_RE.sub("_", f"{self.tag}-{suffix}")[:96]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "version_id": self.version_id}


def _arch_tokens() -> tuple[set[str], set[str]]:
    machine = platform.machine().lower()
    arm = {"aarch64", "arm64"}
    if machine in arm:
        return arm, set()
    return set(), arm


def pick_asset(assets: list[dict[str, Any]]) -> dict[str, Any] | None:
    want, avoid = _arch_tokens()
    candidates = []
    for a in assets:
        name = str(a.get("name", ""))
        lower = name.lower()
        if not lower.endswith(".appimage") or "linux" not in lower:
            continue
        if any(t in lower for t in avoid) or (want and not any(t in lower for t in want)):
            continue
        # The container runs Ubuntu 24.04, so prefer the matching build.
        score = 2 if "ubuntu2404" in lower else 1 if "ubuntu" not in lower else 0
        candidates.append((score, name, a))
    if not candidates:
        return None
    return max(candidates, key=lambda c: (c[0], c[1]))[2]


def to_release_info(release: dict[str, Any]) -> ReleaseInfo:
    asset = pick_asset(release.get("assets") or [])
    sha = None
    if asset and isinstance(asset.get("digest"), str) and (m := _SHA256_RE.fullmatch(asset["digest"])):
        sha = m.group(1)
    return ReleaseInfo(
        tag=str(release.get("tag_name", "")),
        name=str(release.get("name") or release.get("tag_name") or ""),
        published_at=str(release.get("published_at") or ""),
        prerelease=bool(release.get("prerelease")),
        html_url=str(release.get("html_url") or ""),
        notes=str(release.get("body") or "")[:20000],
        asset_name=asset.get("name") if asset else None,
        asset_url=asset.get("browser_download_url") if asset else None,
        asset_size=asset.get("size") if asset else None,
        asset_sha256=sha,
    )


class Updater:
    def __init__(
        self,
        settings: Settings,
        versions: VersionStore,
        fetcher: SafeFetcher | None = None,
        api_fetcher: SafeFetcher | None = None,
    ) -> None:
        if not _REPO_RE.fullmatch(settings.github_repo):
            raise ValueError("Invalid github_repo")
        self._settings = settings
        self._versions = versions
        self._fetcher = fetcher or SafeFetcher(GITHUB_HOSTS, max_bytes=MAX_APPIMAGE_BYTES, timeout=300)
        self._api = api_fetcher or SafeFetcher({"api.github.com"}, max_bytes=20 * 1024**2, timeout=30)
        self._lock = asyncio.Lock()
        self.progress: dict[str, Any] = {"state": "idle"}

    def _headers(self) -> dict[str, str]:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self._settings.github_token:
            headers["Authorization"] = f"Bearer {self._settings.github_token}"
        return headers

    async def latest(self, channel: str) -> ReleaseInfo:
        if channel not in CHANNELS:
            raise UpdateError("Unknown channel")
        base = f"https://api.github.com/repos/{self._settings.github_repo}/releases"
        try:
            if channel == "stable":
                release = await self._api.get_json(f"{base}/latest", self._headers())
            elif channel == "nightly":
                release = await self._api.get_json(f"{base}/tags/{NIGHTLY_TAG}", self._headers())
            else:
                releases = await self._api.get_json(f"{base}?per_page=20", self._headers())
                release = next(
                    (r for r in releases if r.get("tag_name") != NIGHTLY_TAG and not r.get("draft")), None
                )
                if release is None:
                    raise UpdateError("No releases found")
        except FetchError as e:
            raise UpdateError(f"Could not query GitHub: {e}") from e
        if not isinstance(release, dict):
            raise UpdateError("Unexpected GitHub response")
        return to_release_info(release)

    async def install(self, release: ReleaseInfo, activate: bool = True) -> str:
        if self._lock.locked():
            raise UpdateError("An update is already in progress")
        async with self._lock:
            try:
                return await self._install(release, activate)
            finally:
                self.progress = {"state": "idle"}

    async def _install(self, release: ReleaseInfo, activate: bool) -> str:
        if not release.asset_url:
            raise UpdateError("This release has no Linux AppImage for this architecture")
        if not release.asset_sha256 and not self._settings.allow_unverified_updates:
            raise UpdateError("Release asset has no published SHA-256 digest; refusing to install")

        version_id = release.version_id
        work = self._settings.tmp_dir / f"orca-install-{uuid.uuid4().hex}"
        work.mkdir(parents=True)
        try:
            appimage = work / "OrcaSlicer.AppImage"
            self.progress = {"state": "downloading", "version": version_id}
            try:
                sha, _ = await self._fetcher.download(release.asset_url, appimage)
            except FetchError as e:
                raise UpdateError(f"Download failed: {e}") from e
            if release.asset_sha256 and not hmac.compare_digest(sha, release.asset_sha256):
                raise UpdateError("Checksum mismatch; download discarded")

            self.progress = {"state": "extracting", "version": version_id}
            appimage.chmod(0o755)
            await self._exec([str(appimage), "--appimage-extract"], work, 900)
            root = work / "squashfs-root"
            if not (root / "AppRun").exists():
                raise UpdateError("Extracted AppImage has no AppRun")

            self.progress = {"state": "verifying", "version": version_id}
            rc, out = await self._exec([str(root / "AppRun"), "--help"], work, 120, check=False)
            if rc not in (0, 1):
                raise UpdateError(f"Smoke test failed (exit {rc})")
            log.info("Smoke test output for %s: %s", version_id, out[-500:])

            self._versions.add(
                version_id,
                root,
                {"tag": release.tag, "name": release.name, "sha256": sha, "asset": release.asset_name},
            )
            if activate:
                self._versions.activate(version_id)
            self._versions.prune(self._settings.keep_versions)
            log.info("Installed OrcaSlicer %s", version_id)
            return version_id
        finally:
            shutil.rmtree(work, ignore_errors=True)

    async def _exec(self, args: list[str], cwd: Path, timeout: int, check: bool = True) -> tuple[int, str]:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd,
            env={"PATH": "/usr/bin:/bin", "HOME": str(cwd), "LANG": "C.UTF-8"},
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise UpdateError(f"{Path(args[0]).name} timed out") from None
        text = out.decode("utf-8", errors="replace")[-4000:]
        if check and proc.returncode != 0:
            raise UpdateError(f"{Path(args[0]).name} failed (exit {proc.returncode})")
        return proc.returncode or 0, text
