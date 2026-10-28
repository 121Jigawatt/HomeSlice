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
from .cli import BED_TEMP_KEYS, SliceArgsError, SliceSpec, build_args, run_orca, validate_overrides
from .errors import explain, signed_code
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
    # Copies of each model, parallel to model_ids; empty means one of each.
    copies: list[int] = field(default_factory=list)

    def counts(self) -> list[int]:
        return self.copies or [1] * len(self.model_ids)


MAX_COPIES = 100
MAX_OBJECTS = 200

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


DEFAULT_FLUSH_MM3 = 280.0
DEFAULT_FILAMENT_COLOUR = "#F2754E"


def size_for_filaments(process_json: Path, filament_jsons: list[Path]) -> None:
    """Gives each slice one colour per filament and an N×N purge matrix.

    The GUI resizes these when filaments are added; the CLI keeps Orca's 4×4 default and a single colour,
    which breaks MMU firmware (e.g. Happy Hare) that reads them from the G-code.
    """
    n = len(filament_jsons)
    for path in filament_jsons:
        data = json.loads(path.read_text(encoding="utf-8"))
        colour = data.get("filament_colour")
        colour = colour[0] if isinstance(colour, list) and colour else colour
        data["filament_colour"] = [colour if isinstance(colour, str) and colour else DEFAULT_FILAMENT_COLOUR]
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    process = json.loads(process_json.read_text(encoding="utf-8"))
    flush = DEFAULT_FLUSH_MM3
    existing = process.get("flush_volumes_matrix")
    if isinstance(existing, list):
        for v in existing:
            try:
                if float(v) > 0:
                    flush = float(v)
                    break
            except (TypeError, ValueError):
                continue
    process["flush_volumes_matrix"] = [f"{0 if r == c else flush:g}" for r in range(n) for c in range(n)]
    process["flush_volumes_vector"] = ["140"] * (2 * n)
    process_json.write_text(json.dumps(process, indent=2), encoding="utf-8")


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

    def submit(self, req: JobRequest, owner: str | None = None) -> str:
        if not 1 <= len(req.model_ids) <= 50:
            raise SliceArgsError("Select between 1 and 50 models")
        if not 1 <= len(req.filaments) <= 16:
            raise SliceArgsError("Select between 1 and 16 filaments")
        if req.copies and len(req.copies) != len(req.model_ids):
            raise SliceArgsError("Give one copy count per model")
        counts = req.counts()
        if any(not isinstance(c, int) or not 1 <= c <= MAX_COPIES for c in counts):
            raise SliceArgsError(f"Copies must be between 1 and {MAX_COPIES}")
        if sum(counts) > MAX_OBJECTS:
            raise SliceArgsError(f"At most {MAX_OBJECTS} objects can be sliced at once")
        validate_overrides(req.overrides)
        names = []
        for mid in req.model_ids:
            record = self._library.get(mid)
            if record is None:
                raise SliceArgsError("Unknown model")
            names.append(record["filename"])
        if bed_type := req.overrides.get("curr_bed_type"):
            self._check_bed_temps(bed_type, req.filaments)
        job_id = uuid.uuid4().hex
        labels = [f"{name} \u00d7{n}" if n > 1 else name for name, n in zip(names, counts)]
        title = labels[0] if len(labels) == 1 else f"{labels[0]} + {len(labels) - 1} more"
        self._db.execute(
            "INSERT INTO jobs(id, params, status, stage, owner, title, created_at) VALUES(?, ?, 'queued', 'queued', ?, ?, ?)",
            (job_id, json.dumps(asdict(req)), owner, title, time.time()),
        )
        self._queue.put_nowait(job_id)
        return job_id

    def retry(self, job_id: str, owner: str | None = None) -> str:
        row = self._db.one("SELECT params FROM jobs WHERE id=?", (uuid.UUID(job_id).hex,))
        if row is None:
            raise SliceArgsError("Unknown job")
        return self.submit(JobRequest(**json.loads(row["params"])), owner)

    def cleanup(self, older_than_days: int, statuses: tuple[str, ...]) -> int:
        cutoff = time.time() - older_than_days * 86400
        marks = ",".join("?" * len(statuses))
        rows = self._db.all(
            f"SELECT id FROM jobs WHERE status IN ({marks}) AND COALESCE(finished_at, created_at) < ?",
            (*statuses, cutoff),
        )
        for r in rows:
            self.delete(r["id"])
        return len(rows)

    def _check_bed_temps(self, bed_type: str, filaments: list[str]) -> None:
        """A 0 °C plate temperature means the filament isn't meant for that plate; printers reject it."""
        key = BED_TEMP_KEYS.get(bed_type)
        if key is None:
            return
        for name in dict.fromkeys(filaments):
            try:
                value = self._profiles.resolve("filament", name).get(key)
            except ProfileError as e:
                raise SliceArgsError(str(e)) from None
            first = value[0] if isinstance(value, list) and value else value
            try:
                temp = float(first)
            except (TypeError, ValueError):
                continue
            if temp <= 0:
                raise SliceArgsError(f"'{name}' has no bed temperature for {bed_type}. Choose another plate type.")

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
        job["models"] = []
        for mid in job["params"].get("model_ids") or []:
            try:
                record = self._library.get(mid)
            except ValueError:
                record = None
            job["models"].append(
                {
                    "id": mid,
                    "filename": record["filename"] if record else None,
                    "has_thumbnail": bool(record and record["has_thumbnail"]),
                }
            )
        if not job.get("title"):
            names = [m["filename"] or "(deleted model)" for m in job["models"]] or ["Slice job"]
            job["title"] = names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"
        started, finished = job.get("started_at"), job.get("finished_at")
        job["duration"] = finished - started if started and finished else None
        job["error_info"] = (
            explain(job.get("error_code"), job.get("error"), job.get("error_kind") or "orca")
            if job["status"] == "failed"
            else None
        )
        return job

    def list(
        self,
        limit: int = 50,
        offset: int = 0,
        q: str | None = None,
        statuses: list[str] | None = None,
        printer: str | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        where, args = [], []
        if q:
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            where.append("title LIKE ? ESCAPE '\\'")
            args.append(f"%{escaped}%")
        if statuses:
            where.append(f"status IN ({','.join('?' * len(statuses))})")
            args.extend(statuses)
        if printer:
            where.append("json_extract(params, '$.printer') = ?")
            args.append(printer)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        total = self._db.one(f"SELECT COUNT(*) AS n FROM jobs {clause}", tuple(args))["n"]
        rows = self._db.all(
            f"SELECT id FROM jobs {clause} ORDER BY created_at DESC LIMIT ? OFFSET ?", (*args, limit, offset)
        )
        return [j for r in rows if (j := self.get(r["id"]))], total

    def printers_used(self) -> list[str]:
        rows = self._db.all("SELECT DISTINCT json_extract(params, '$.printer') AS p FROM jobs ORDER BY p")
        return [r["p"] for r in rows if r["p"]]

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
                self._finish(job_id, "cancelled", "Cancelled by user", error_kind="cancelled")
            except Exception as e:  # noqa: BLE001
                log.exception("Job %s crashed", job_id)
                self._finish(job_id, "failed", f"Internal error: {type(e).__name__}", error_kind="setup")
            finally:
                self._running.pop(job_id, None)

    def _finish(
        self,
        job_id: str,
        status: str,
        error: str | None = None,
        stats: dict | None = None,
        *,
        error_code: int | None = None,
        error_kind: str | None = None,
    ) -> None:
        self._db.execute(
            "UPDATE jobs SET status=?, stage=?, error=?, error_code=?, error_kind=?, stats=?, finished_at=? WHERE id=?",
            (status, status, error, error_code, error_kind, json.dumps(stats) if stats else None, time.time(), job_id),
        )

    def _stage(self, job_id: str, stage: str) -> None:
        self._db.execute("UPDATE jobs SET stage=? WHERE id=?", (stage, job_id))

    async def _run(self, job_id: str) -> None:
        self._db.execute(
            "UPDATE jobs SET status='running', stage='preparing', started_at=? WHERE id=?", (time.time(), job_id)
        )
        req = JobRequest(**json.loads(self._db.one("SELECT params FROM jobs WHERE id=?", (job_id,))["params"]))
        binary = self._versions.current_binary()
        if binary is None:
            self._finish(job_id, "failed", "OrcaSlicer is not installed yet. Install it under Settings.", error_kind="setup")
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
            size_for_filaments(process, filaments)
            inputs = [
                self._library.path_for(mid) for mid, n in zip(req.model_ids, req.counts()) for _ in range(n)
            ]
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
            self._finish(job_id, "failed", str(e), error_kind="setup")
            return

        self._stage(job_id, "arranging and slicing" if req.arrange else "slicing")
        result = await run_orca(args, jdir, self._settings.slice_timeout_s, self._settings.slice_memory_mb)
        self._stage(job_id, "writing output")
        (jdir / "output.log").write_text(result.output_tail, encoding="utf-8")
        shutil.rmtree(datadir, ignore_errors=True)
        gcodes = sorted(outdir.glob("*.gcode"))
        if result.timed_out:
            self._finish(
                job_id, "failed", f"Slicing timed out after {self._settings.slice_timeout_s}s", error_kind="timeout"
            )
        elif result.returncode != 0 or not gcodes:
            code, message = signed_code(result.returncode), None
            try:
                summary = json.loads((outdir / "result.json").read_text(encoding="utf-8"))
                code = int(summary.get("return_code", code))
                message = str(summary.get("error_string") or "")[:500] or None
            except (OSError, ValueError, TypeError):
                pass
            self._finish(
                job_id,
                "failed",
                message or f"OrcaSlicer exited with code {result.returncode}.",
                error_code=code if code else None,
                error_kind="orca",
            )
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
