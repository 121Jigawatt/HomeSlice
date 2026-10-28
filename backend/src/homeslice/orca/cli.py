import asyncio
import os
import re
import signal
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

_PERCENT = re.compile(r"^\d{1,3}%$")


def _float_range(lo: float, hi: float) -> Callable[[str], str]:
    def check(v: str) -> str:
        f = float(v)
        if not lo <= f <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return repr(f)

    return check


def _int_range(lo: int, hi: int) -> Callable[[str], str]:
    def check(v: str) -> str:
        i = int(v)
        if not lo <= i <= hi:
            raise ValueError(f"must be between {lo} and {hi}")
        return str(i)

    return check


def _enum(*choices: str) -> Callable[[str], str]:
    def check(v: str) -> str:
        if v not in choices:
            raise ValueError(f"must be one of {', '.join(choices)}")
        return v

    return check


def _percent(v: str) -> str:
    if not _PERCENT.fullmatch(v) or int(v[:-1]) > 100:
        raise ValueError("must be a percentage like 15%")
    return v


def _bool(v: str) -> str:
    if v.lower() in ("1", "true"):
        return "1"
    if v.lower() in ("0", "false"):
        return "0"
    raise ValueError("must be true or false")


# Only these settings may be overridden from the web UI; values are re-serialised after validation.
OVERRIDE_ALLOWLIST: dict[str, Callable[[str], str]] = {
    "layer_height": _float_range(0.02, 1.2),
    "initial_layer_print_height": _float_range(0.05, 1.2),
    "wall_loops": _int_range(1, 20),
    "top_shell_layers": _int_range(0, 50),
    "bottom_shell_layers": _int_range(0, 50),
    "sparse_infill_density": _percent,
    "sparse_infill_pattern": _enum(
        "rectilinear", "alignedrectilinear", "grid", "line", "cubic", "triangles", "tri-hexagon",
        "gyroid", "honeycomb", "adaptivecubic", "supportcubic", "lightning", "crosshatch", "3dhoneycomb",
    ),
    "enable_support": _bool,
    "support_type": _enum("normal(auto)", "tree(auto)", "normal(manual)", "tree(manual)"),
    "brim_type": _enum("auto_brim", "brim_ears", "outer_only", "inner_only", "outer_and_inner", "no_brim"),
    "skirt_loops": _int_range(0, 10),
    "enable_prime_tower": _bool,
    "curr_bed_type": _enum(
        "Cool Plate", "Engineering Plate", "High Temp Plate", "Textured PEI Plate", "Textured Cool Plate", "Supertack Plate",
    ),
}

# Filament preset key holding the first-layer bed temperature for each plate type.
BED_TEMP_KEYS = {
    "Cool Plate": "cool_plate_temp_initial_layer",
    "Engineering Plate": "eng_plate_temp_initial_layer",
    "High Temp Plate": "hot_plate_temp_initial_layer",
    "Textured PEI Plate": "textured_plate_temp_initial_layer",
    "Textured Cool Plate": "textured_cool_plate_temp_initial_layer",
    "Supertack Plate": "supertack_plate_temp_initial_layer",
}


class SliceArgsError(ValueError):
    pass


def validate_overrides(overrides: dict[str, str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key, value in overrides.items():
        check = OVERRIDE_ALLOWLIST.get(key)
        if check is None:
            raise SliceArgsError(f"Setting '{key}' cannot be overridden")
        try:
            clean[key] = check(str(value).strip())
        except ValueError as e:
            raise SliceArgsError(f"{key}: {e}") from None
    return clean


@dataclass
class SliceSpec:
    inputs: list[Path]
    printer_json: Path
    process_json: Path
    filament_jsons: list[Path]
    overrides: dict[str, str] = field(default_factory=dict)
    arrange: bool = True
    orient: bool = False
    plate: int = 0


def _safe_path(p: Path) -> str:
    s = str(p)
    # ';' separates file lists in Orca flags; '-' prefix would be parsed as a flag.
    if not p.is_absolute() or ";" in s or s.startswith("-"):
        raise SliceArgsError(f"Unsafe path: {s}")
    return s


def build_args(binary: Path, spec: SliceSpec, outdir: Path, datadir: Path, logfile: Path) -> list[str]:
    if not spec.inputs or not spec.filament_jsons:
        raise SliceArgsError("At least one model and one filament are required")
    if spec.plate < 0 or spec.plate > 99:
        raise SliceArgsError("Invalid plate")
    args = [
        _safe_path(binary),
        "--datadir", _safe_path(datadir),
        "--load-settings", f"{_safe_path(spec.printer_json)};{_safe_path(spec.process_json)}",
        "--load-filaments", ";".join(_safe_path(p) for p in spec.filament_jsons),
        "--outputdir", _safe_path(outdir),
        "--logfile", _safe_path(logfile),
        "--debug", "2",
    ]
    if spec.arrange:
        args += ["--arrange", "1"]
    if spec.orient:
        args += ["--orient", "1"]
    for key, value in validate_overrides(spec.overrides).items():
        args.append(f"--{key.replace('_', '-')}={value}")
    # 3MFs saved by newer Bambu Studio/Orca builds are otherwise refused outright (the GUI only warns).
    args.append("--allow-newer-file")
    args += ["--slice", str(spec.plate), "--export-3mf", "result.gcode.3mf"]
    args += [_safe_path(p) for p in spec.inputs]
    return args


@dataclass
class RunResult:
    returncode: int
    timed_out: bool
    output_tail: str


def _limits(memory_bytes: int) -> Callable[[], None] | None:
    if sys.platform == "win32":
        return None

    def apply() -> None:
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.nice(5)

    return apply


async def run_orca(args: list[str], cwd: Path, timeout_s: int, memory_mb: int) -> RunResult:
    runtime = cwd / "xdg-runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(cwd),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "XDG_RUNTIME_DIR": str(runtime),
    }
    if sys.platform == "win32":
        env = {**os.environ, "HOME": str(cwd)}
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        preexec_fn=_limits(memory_mb * 1024**2),
        start_new_session=sys.platform != "win32",
    )
    timed_out = False
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except (TimeoutError, asyncio.CancelledError) as e:
        timed_out = isinstance(e, TimeoutError)
        _kill_tree(proc)
        out, _ = await proc.communicate()
        if not timed_out:
            raise
    tail = (out or b"")[-8000:].decode("utf-8", errors="replace")
    return RunResult(proc.returncode if proc.returncode is not None else -1, timed_out, tail)


def _kill_tree(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if sys.platform == "win32":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
