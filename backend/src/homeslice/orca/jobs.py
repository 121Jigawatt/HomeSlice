from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..config import Settings
from ..db import Database
from ..library.service import Library
from .cli import SliceArgsError, SliceSpec, build_args, run_orca, validate_overrides
from .profiles import ProfileError, ProfileStore
from .versions import VersionStore

log = logging.getLogger(__name__)

OUTPUT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
OUTPUT_EXTS = (".gcode", ".3mf", ".json")
_STAT_PATTERNS = {
    "print_time": re.compile(r";\s*(?:model printing time|estimated printing time \(normal mode\))\s*[:=]\s*(.+)"),
    "filament_g": re.compile(r";\s*(?:total filament used \[g\]|filament used \[g\])\s*=\s*([\d.,\s]+)"),
    "filament_mm": re.compile(r";\s*filament used \[mm\]\s*=\s*([\d.,\s]+)"),
    "layers": re.compile(r";\s*total layer number\s*:\s*(\d+)"),
}


@dataclass
class JobRequest:
    model_ids: list[str]
    printer: str
    process: str
    filaments: list[str]
    overrides: dict[str, str] = field(default_factory=dict)
    arrange: bool = True
    orient: bool = False
    plate: int = 0


def parse_gcode_stats(path: Path) -> dict[str, str]:
    stats: dict[str, str] = {}
    size = path.stat().st_size
    with path.open("rb") as f:
        chunks = [f.read(256 * 1024)]
        if size > 512 * 1024:
            f.seek(size - 256 * 1024)
            chunks.append(f.read())
    for chunk in chunks:
        for line in chunk.decode("utf-8", errors="replace").splitlines():
            for key, pattern in _STAT_PATTERNS.items():
                if key not in stats and (m := pattern.match(line)):
                    stats[key] = m.group(1).strip()[:100]
    return stats


class JobManager:
    def __init__(
        self,
        db: Database,
        settings: Settings,
        library: Library,
        profiles: ProfileStore,
        versions: VersionStore,
    ) -> None:
        self._db = db
        self._settings = settings
        self._library = library
        self._profiles = profiles
        self._versions = versions
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._running: dict[str, asyncio.Task] = {}

    def start(self) -> None:
        # Jobs interrupted by a restart can't be resumed.
        self._db.execute(
            "UPDATE jobs SET status='failed', error='Interrupted by restart', finished_at=? "
            "WHERE status IN ('queued', 'running')",
            (time.time(),),
        )
        for _ in range(max(1, self._settings.slice_workers)):
            self._workers.append(asyncio.create_task(self._worker()))

    async def stop(self) -> None:
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)

    def submit(self, req: JobRequest) -> str:
        if not 1 <= len(req.model_ids) <= 50:
            raise SliceArgsError("Select between 1 and 50 models")
        if not 1 <= len(req.filaments) <= 16:
            raise SliceArgsError("Select between 1 and 16 filaments")
        validate_overrides(req.overrides)
        for mid in req.model_ids:
            if self._library.path_for(mid) is None:
                raise SliceArgsError("Unknown model")
        job_id = uuid.uuid4().hex
        self._db.execute(
            "INSERT INTO jobs(id, params, status, created_at) VALUES(?, ?, 'queued', ?)",
            (job_id, json.dumps(asdict(req)), time.time()),
        )
        self._queue.put_nowait(job_id)
        return job_id

    def cancel(self, job_id: str) -> bool:
        task = self._running.get(job_id)
        if task:
            task.cancel()
            return True
        return self._db.execute(
            "UPDATE jobs SET status='cancelled', finished_at=? WHERE id=? AND status='queued'",
            (time.time(), job_id),
        ) > 0

    def job_dir(self, job_id: str) -> Path:
        return self._settings.jobs_dir / uuid.UUID(job_id).hex

    def get(self, job_id: str) -> dict[str, Any] | None:
        row = self._db.one("SELECT * FROM jobs WHERE id=?", (uuid.UUID(job_id).hex,))
        if row is None:
            return None
        job = dict(row)
        job["params"] = json.loads(job["params"])
        job["stats"] = json.loads(job["stats"]) if job["stats"] else None
        job["files"] = self.output_files(job["id"])
        return job

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._db.all("SELECT id FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,))
        return [j for r in rows if (j := self.get(r["id"]))]

    def output_files(self, job_id: str) -> list[str]:
        out = self.job_dir(job_id) / "out"
        if not out.is_dir():
            return []
        return sorted(p.name for p in out.iterdir() if p.is_file() and p.name.endswith(OUTPUT_EXTS))

    def output_path(self, job_id: str, name: str) -> Path | None:
        if not OUTPUT_NAME_RE.fullmatch(name) or name not in self.output_files(job_id):
            return None
        return self.job_dir(job_id) / "out" / name

    def delete(self, job_id: str) -> None:
        self.cancel(job_id)
        self._db.execute("DELETE FROM jobs WHERE id=?", (uuid.UUID(job_id).hex,))
        shutil.rmtree(self.job_dir(job_id), ignore_errors=True)

    async def _worker(self) -> None:
        while True:
            job_id = await self._queue.get()
            row = self._db.one("SELECT status FROM jobs WHERE id=?", (job_id,))
            if row is None or row["status"] != "queued":
                continue
            task = asyncio.create_task(self._run(job_id))
            self._running[job_id] = task
            try:
                await task
            except asyncio.CancelledError:
                if not task.cancelled():
                    raise
                self._finish(job_id, "cancelled", "Cancelled by user")
            except Exception as e:  # noqa: BLE001
                log.exception("Job %s crashed", job_id)
                self._finish(job_id, "failed", f"Internal error: {type(e).__name__}")
            finally:
                self._running.pop(job_id, None)

    def _finish(self, job_id: str, status: str, error: str | None = None, stats: dict | None = None) -> None:
        self._db.execute(
            "UPDATE jobs SET status=?, error=?, stats=?, finished_at=? WHERE id=?",
            (status, error, json.dumps(stats) if stats else None, time.time(), job_id),
        )

    async def _run(self, job_id: str) -> None:
        self._db.execute("UPDATE jobs SET status='running' WHERE id=?", (job_id,))
        req = JobRequest(**json.loads(self._db.one("SELECT params FROM jobs WHERE id=?", (job_id,))["params"]))
        binary = self._versions.current_binary()
        if binary is None:
            self._finish(job_id, "failed", "OrcaSlicer is not installed yet. Install it under Settings.")
            return

        jdir = self.job_dir(job_id)
        profiles_dir, outdir, datadir = jdir / "profiles", jdir / "out", jdir / "datadir"
        for d in (profiles_dir, outdir, datadir):
            d.mkdir(parents=True, exist_ok=True)

        try:
            printer = self._profiles.write_resolved("printer", req.printer, profiles_dir / "printer.json")
            process = self._profiles.write_resolved("process", req.process, profiles_dir / "process.json")
            filaments = [
                self._profiles.write_resolved("filament", name, profiles_dir / f"filament_{i}.json")
                for i, name in enumerate(req.filaments)
            ]
            inputs = [self._library.path_for(mid) for mid in req.model_ids]
            if any(p is None for p in inputs):
                raise SliceArgsError("A selected model no longer exists")
            spec = SliceSpec(
                inputs=[p.resolve() for p in inputs if p],
                printer_json=printer.resolve(),
                process_json=process.resolve(),
                filament_jsons=[f.resolve() for f in filaments],
                overrides=req.overrides,
                arrange=req.arrange,
                orient=req.orient,
                plate=req.plate,
            )
            args = build_args(binary, spec, outdir.resolve(), datadir.resolve(), (jdir / "orca.log").resolve())
        except (ProfileError, SliceArgsError, OSError, ValueError) as e:
            self._finish(job_id, "failed", str(e))
            return

        result = await run_orca(args, jdir, self._settings.slice_timeout_s, self._settings.slice_memory_mb)
        (jdir / "output.log").write_text(result.output_tail, encoding="utf-8")
        shutil.rmtree(datadir, ignore_errors=True)
        gcodes = sorted(outdir.glob("*.gcode"))
        if result.timed_out:
            self._finish(job_id, "failed", f"Slicing timed out after {self._settings.slice_timeout_s}s")
        elif result.returncode != 0 or not gcodes:
            self._finish(job_id, "failed", f"OrcaSlicer exited with code {result.returncode}. See job log.")
        else:
            self._finish(job_id, "done", None, parse_gcode_stats(gcodes[0]))

    def log_tail(self, job_id: str) -> str:
        parts = []
        for name in ("output.log", "orca.log"):
            p = self.job_dir(job_id) / name
            if p.is_file():
                with p.open("rb") as f:
                    f.seek(max(0, p.stat().st_size - 16000))
                    parts.append(f"--- {name} ---\n" + f.read().decode("utf-8", errors="replace"))
        return "\n".join(parts)
