"""STEP -> STL conversion. OrcaSlicer's CLI and the browser only read meshes, so STEP models are converted once.

Runs the OpenCascade tessellation (cascadio) in a child process so a crash or runaway CAD file can't take down
the server; the parent enforces a timeout and memory limit.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import numpy as np

STEP_EXTS = (".step", ".stp")
CONVERT_TIMEOUT_S = 300
CONVERT_MEMORY_MB = 4096
# Max chord error in mm; fine enough for printing, coarse enough to keep meshes small.
TOL_LINEAR_MM = 0.01
TOL_ANGULAR_RAD = 0.5


class StepError(Exception):
    pass


def write_binary_stl(path: Path, tris: np.ndarray) -> None:
    tris = np.asarray(tris, dtype="<f4")
    normals = np.cross(tris[:, 1] - tris[:, 0], tris[:, 2] - tris[:, 0])
    records = np.zeros(len(tris), dtype=[("n", "<f4", 3), ("v", "<f4", (3, 3)), ("a", "<u2")])
    records["n"], records["v"] = normals, tris
    with path.open("wb") as f:
        f.write(b"HomeSlice STEP conversion".ljust(80, b" "))
        f.write(np.uint32(len(tris)).tobytes())
        f.write(records.tobytes())


def convert(source: Path, dest: Path, timeout: float = CONVERT_TIMEOUT_S) -> None:
    """Converts source (STEP) to a binary STL at dest, atomically."""
    from ..orca.cli import _limits

    tmp = dest.with_name(f"{dest.stem}.{uuid.uuid4().hex}.tmp")
    try:
        try:
            proc = subprocess.run(
                [sys.executable, "-m", "homeslice.library.step", str(source), str(tmp)],
                capture_output=True,
                text=True,
                timeout=timeout,
                preexec_fn=_limits(CONVERT_MEMORY_MB * 1024**2),
            )
        except subprocess.TimeoutExpired:
            raise StepError(f"Converting the STEP file took longer than {timeout:.0f}s") from None
        if proc.returncode != 0 or not tmp.is_file():
            detail = (proc.stderr or proc.stdout or "").strip().splitlines()[-1:] or ["conversion failed"]
            raise StepError(f"Couldn't convert the STEP file: {detail[0][:200]}")
        os.replace(tmp, dest)
    finally:
        tmp.unlink(missing_ok=True)


def _main(source: str, out: str) -> int:
    import cascadio

    from .thumbnails import load_obj

    obj = Path(out).with_suffix(".obj")
    try:
        rc = cascadio.step_to_obj(source, str(obj), tol_linear=TOL_LINEAR_MM, tol_angular=TOL_ANGULAR_RAD, use_colors=False)
        if rc != 0 or not obj.is_file():
            print("The file isn't a readable STEP model", file=sys.stderr)
            return 2
        tris = load_obj(obj)
        if len(tris) == 0:
            print("The STEP file has no solid geometry", file=sys.stderr)
            return 3
        write_binary_stl(Path(out), tris)
        return 0
    finally:
        obj.unlink(missing_ok=True)
        obj.with_suffix(".mtl").unlink(missing_ok=True)


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1], sys.argv[2]))
