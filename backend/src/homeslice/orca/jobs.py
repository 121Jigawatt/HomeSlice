from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from ..config import Settings
from ..db import Database
from ..library import threemf
from ..library.service import Library
from ..library.step import StepError
from ..library.thumbnails import ThumbnailError, footprint_mm2
from .cli import BED_TEMP_KEYS, SliceArgsError, SliceSpec, build_args, run_orca, validate_overrides
from .errors import explain, signed_code
from .gcode_thumbs import embed as embed_thumbnails
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
    # 3MF plate per model (0 = every plate on shared beds, -1 = each plate on its own bed), parallel to
    # model_ids; empty means 0 for all.
    model_plates: list[int] = field(default_factory=list)
    # 3MF plates to use per model when model_plates is 0 or -1, parallel to model_ids; empty means all plates.
    plate_selection: list[list[int]] = field(default_factory=list)
    # Start a new bed once object footprints would cover more than this % of it (set per printer).
    bed_coverage: int = 50

    def counts(self) -> list[int]:
        return self.copies or [1] * len(self.model_ids)

    def plates_for(self) -> list[int]:
        return self.model_plates or [0] * len(self.model_ids)

    def selections(self) -> list[list[int]]:
        return self.plate_selection or [[] for _ in self.model_ids]


MAX_COPIES = 100
MAX_OBJECTS = 200
SEPARATE_PLATES = -1
MIN_COVERAGE = 10


def _plate_number(path: Path) -> int:
    m = re.search(r"(\d+)", path.stem)
    return int(m.group(1)) if m else 0


def bed_area(printer_json: Path) -> float:
    """Printable area in mm² from the printer profile's outline (shoelace formula); 256×256 if unknown."""
    try:
        raw = json.loads(printer_json.read_text(encoding="utf-8")).get("printable_area") or []
        pts = [tuple(float(v) for v in str(p).lower().split("x", 1)) for p in raw]
    except (OSError, ValueError):
        pts = []
    if len(pts) < 3:
        return 256.0 * 256.0
    area = sum(x1 * y2 - x2 * y1 for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]))
    return abs(area) / 2 or 256.0 * 256.0



_DURATION = re.compile(r"(\d+(?:\.\d+)?)\s*([dhms])")


def _seconds(text: str) -> float | None:
    parts = _DURATION.findall(text or "")
    if not parts:
        return None
    scale = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    return sum(float(v) * scale[u] for v, u in parts)


def _duration(seconds: float) -> str:
    s = int(round(seconds))
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    return " ".join(f"{v}{u}" for v, u in ((d, "d"), (h, "h"), (m, "m"), (s, "s")) if v) or "0s"


def _total(values: list[str]) -> float:
    total = 0.0
    for v in values:
        for part in v.replace(";", ",").split(","):
            try:
                total += float(part)
            except ValueError:
                continue
    return total


def combine_stats(per_plate: list[dict[str, str]]) -> dict[str, Any]:
    """Totals across beds; single-bed jobs keep Orca's own figures."""
    if len(per_plate) <= 1:
        return per_plate[0] if per_plate else {}
    out: dict[str, Any] = {"plates": len(per_plate)}
    times = [_seconds(s.get("print_time", "")) for s in per_plate]
    if all(t is not None for t in times):
        out["print_time"] = _duration(sum(times))  # type: ignore[arg-type]
    if grams := _total([s["filament_g"] for s in per_plate if "filament_g" in s]):
        out["filament_g"] = f"{grams:.2f}"
    if mm := _total([s["filament_mm"] for s in per_plate if "filament_mm" in s]):
        out["filament_mm"] = f"{mm:.2f}"
    layers = [int(s["layers"]) for s in per_plate if str(s.get("layers", "")).isdigit()]
    if layers:
        out["layers"] = str(max(layers))
    out["per_plate"] = per_plate
    return out

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
        if req.model_plates and len(req.model_plates) != len(req.model_ids):
            raise SliceArgsError("Give one plate per model")
        if req.plate_selection and len(req.plate_selection) != len(req.model_ids):
            raise SliceArgsError("Give one plate selection per model")
        if not MIN_COVERAGE <= req.bed_coverage <= 100:
            raise SliceArgsError(f"Bed coverage must be between {MIN_COVERAGE}% and 100%")
        validate_overrides(req.overrides)
        names = []
        for mid, plate, chosen in zip(req.model_ids, req.plates_for(), req.selections()):
            record = self._library.get(mid)
            if record is None:
                raise SliceArgsError("Unknown model")
            name = record["filename"]
            saved = [p["index"] for p in record.get("plates") or []]
            if chosen and plate > 0:
                raise SliceArgsError(f"{name}: choose one plate or a set of plates, not both")
            if unknown := set(chosen) - set(saved):
                raise SliceArgsError(f"{name} has no plate {min(unknown)}")
            if chosen and len(chosen) < len(saved):
                listed = ", ".join(str(i) for i in sorted(set(chosen)))
                each = ", each on its own bed" if plate == SEPARATE_PLATES and len(set(chosen)) > 1 else ""
                name = f"{name} (plate{'s' if len(set(chosen)) > 1 else ''} {listed}{each})"
            elif plate == SEPARATE_PLATES:
                if not saved:
                    raise SliceArgsError(f"{name} has no saved plates")
                name = f"{name} (each plate)"
            elif plate:
                match = next((p for p in record.get("plates") or [] if p["index"] == plate), None)
                if match is None:
                    raise SliceArgsError(f"{name} has no plate {plate}")
                name = f"{name} ({match['name'] or f'plate {plate}'})"
            names.append(name)
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
        names = [p.name for p in out.iterdir() if p.is_file() and p.name.endswith(OUTPUT_EXTS)]
        return sorted(names, key=lambda n: [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", n)])

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
            groups = await self._plate_groups(req, jdir, printer)
            if any(p is None for g in groups for p in g):
                raise SliceArgsError("A selected model no longer exists")
        except (ProfileError, SliceArgsError, StepError, ThumbnailError, zipfile.BadZipFile, OSError, ValueError) as e:
            self._finish(job_id, "failed", str(e), error_kind="setup")
            return

        gcodes: list[Path] = []
        logs: list[str] = []
        for r, group in enumerate(groups, start=1):
            run_out = outdir / f"run_{r}" if len(groups) > 1 else outdir
            run_out.mkdir(parents=True, exist_ok=True)
            spec = SliceSpec(
                inputs=[p.resolve() for p in group if p],
                printer_json=printer.resolve(),
                process_json=process.resolve(),
                filament_jsons=[f.resolve() for f in filaments],
                overrides=req.overrides,
                # Separate object files have no shared layout, so they must be arranged.
                arrange=req.arrange or len(group) > 1,
                orient=req.orient,
                plate=req.plate,
            )
            log_file = jdir / f"orca_{r}.log"
            try:
                args = build_args(binary, spec, run_out.resolve(), datadir.resolve(), log_file.resolve())
            except SliceArgsError as e:
                self._finish(job_id, "failed", str(e), error_kind="setup")
                return
            label = "arranging and slicing" if spec.arrange else "slicing"
            self._stage(job_id, f"{label} bed group {r} of {len(groups)}" if len(groups) > 1 else label)
            result = await run_orca(args, jdir, self._settings.slice_timeout_s, self._settings.slice_memory_mb)
            logs.append(f"=== bed group {r} ===\n{result.output_tail}" if len(groups) > 1 else result.output_tail)
            (jdir / "output.log").write_text("\n".join(logs), encoding="utf-8")
            if log_file.is_file():
                with (jdir / "orca.log").open("a", encoding="utf-8", errors="replace") as combined:
                    combined.write(log_file.read_text(encoding="utf-8", errors="replace"))
                log_file.unlink(missing_ok=True)
            run_gcodes = sorted(run_out.glob("plate_*.gcode"), key=_plate_number)
            if result.timed_out:
                self._finish(
                    job_id, "failed", f"Slicing timed out after {self._settings.slice_timeout_s}s", error_kind="timeout"
                )
                return
            if result.returncode != 0 or not run_gcodes:
                code, message = signed_code(result.returncode), None
                try:
                    summary = json.loads((run_out / "result.json").read_text(encoding="utf-8"))
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
                return
            self._stage(job_id, "writing output")
            plate_3mf = run_out / "result.gcode.3mf"
            for gcode in run_gcodes:
                try:
                    await asyncio.to_thread(embed_thumbnails, gcode, plate_3mf, printer, _plate_number(gcode))
                except Exception as e:  # noqa: BLE001 - a missing preview must not fail a good slice
                    log.info("No G-code thumbnail for %s: %s", job_id, e)
                final = outdir / f"plate_{len(gcodes) + 1}.gcode"
                if gcode != final:
                    os.replace(gcode, final)
                gcodes.append(final)
            if run_out != outdir:
                if plate_3mf.is_file():
                    os.replace(plate_3mf, outdir / f"result_{r}.gcode.3mf")
                shutil.rmtree(run_out, ignore_errors=True)
        shutil.rmtree(datadir, ignore_errors=True)
        self._finish(job_id, "done", None, combine_stats([parse_gcode_stats(g) for g in gcodes]))

    async def _plate_groups(self, req: JobRequest, jdir: Path, printer_json: Path) -> list[list[Path | None]]:
        """Splits the objects into bed groups, each sliced by its own Orca run.

        A 3MF sliced with "each plate separately" keeps its plates; everything else is packed in order and a new
        bed starts once footprints would cover more than req.bed_coverage % of the bed. Orca still spills anything
        that physically doesn't fit onto further beds within a run.
        """
        fixed: list[list[Path | None]] = []
        loose: list[Path | None] = []
        for i, (mid, n, plate, chosen) in enumerate(
            zip(req.model_ids, req.counts(), req.plates_for(), req.selections())
        ):
            source = self._library.path_for(mid)
            if source is not None and source.suffix == ".3mf" and plate == SEPARATE_PLATES:
                for p in await asyncio.to_thread(threemf.plates, source):
                    if chosen and p["index"] not in chosen:
                        continue
                    parts = await asyncio.to_thread(
                        threemf.write_items, source, jdir / "inputs" / f"{i}_plate{p['index']}", p["index"]
                    )
                    fixed += [list(parts) for _ in range(n)]
            elif source is not None and source.suffix == ".3mf":
                parts = await asyncio.to_thread(threemf.write_items, source, jdir / "inputs" / str(i), plate or chosen)
                loose += list(parts) * n
            else:
                loose += [await asyncio.to_thread(self._library.mesh_path, mid)] * n
        if len(loose) <= 1:
            return fixed + ([loose] if loose else [])
        limit = bed_area(printer_json) * max(MIN_COVERAGE, min(100, req.bed_coverage)) / 100
        footprints: dict[Path, float] = {}
        groups: list[list[Path | None]] = []
        current: list[Path | None] = []
        covered = 0.0
        for path in loose:
            if path is not None and path not in footprints:
                footprints[path] = await asyncio.to_thread(footprint_mm2, path)
            area = footprints.get(path, 0.0) if path is not None else 0.0
            if current and covered + area > limit:
                groups.append(current)
                current, covered = [], 0.0
            current.append(path)
            covered += area
        groups.append(current)
        return fixed + groups

    def log_tail(self, job_id: str) -> str:
        parts = []
        for name in ("output.log", "orca.log"):
            p = self.job_dir(job_id) / name
            if p.is_file():
                with p.open("rb") as f:
                    f.seek(max(0, p.stat().st_size - 16000))
                    parts.append(f"--- {name} ---\n" + f.read().decode("utf-8", errors="replace"))
        return "\n".join(parts)
