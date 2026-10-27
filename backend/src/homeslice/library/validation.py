import re
import struct
import unicodedata
import zipfile
from pathlib import Path, PurePosixPath

ALLOWED_EXTS = {".stl", ".3mf", ".obj", ".step", ".stp", ".amf"}
MAX_ZIP_ENTRIES = 10_000
MAX_ZIP_UNCOMPRESSED = 4 * 1024**3
MAX_ZIP_RATIO = 200
_UNSAFE_CHARS = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')


class ValidationError(ValueError):
    pass


def safe_display_name(filename: str) -> str:
    """Display-only name; never used to build filesystem paths."""
    name = unicodedata.normalize("NFC", filename).replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("_", name).strip(" .")
    return name[:200] or "model"


def normalize_ext(filename: str) -> str:
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.lower()
    if suffix not in ALLOWED_EXTS:
        raise ValidationError(f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(ALLOWED_EXTS))}")
    return suffix


def validate_file(path: Path, ext: str) -> None:
    if path.stat().st_size == 0:
        raise ValidationError("File is empty")
    if ext == ".3mf":
        validate_zip(path, require_model=True)
    elif ext == ".stl":
        _validate_stl(path)
    elif ext == ".obj":
        _validate_text(path)
    elif ext in (".step", ".stp"):
        with path.open("rb") as f:
            if not f.read(64).lstrip().startswith(b"ISO-10303-21"):
                raise ValidationError("Not a valid STEP file")
    elif ext == ".amf":
        with path.open("rb") as f:
            head = f.read(4)
        if head.startswith(b"PK"):
            validate_zip(path, require_model=False)
        else:
            _validate_text(path)
    else:
        raise ValidationError("Unsupported file type")


def validate_zip(path: Path, *, require_model: bool) -> None:
    """Rejects zip-slip paths, symlinks, zip bombs and oversized archives."""
    if not zipfile.is_zipfile(path):
        raise ValidationError("Not a valid archive")
    try:
        with zipfile.ZipFile(path) as zf:
            infos = zf.infolist()
    except zipfile.BadZipFile as e:
        raise ValidationError("Corrupt archive") from e
    if len(infos) > MAX_ZIP_ENTRIES:
        raise ValidationError("Archive has too many entries")
    total = 0
    has_model = False
    for info in infos:
        name = info.filename
        parts = PurePosixPath(name.replace("\\", "/")).parts
        if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
            raise ValidationError("Archive contains unsafe paths")
        if (info.external_attr >> 16) & 0o170000 == 0o120000:
            raise ValidationError("Archive contains symlinks")
        if info.compress_size and info.file_size > 1024**2 and info.file_size / info.compress_size > MAX_ZIP_RATIO:
            raise ValidationError("Archive compression ratio is suspicious")
        total += info.file_size
        if total > MAX_ZIP_UNCOMPRESSED:
            raise ValidationError("Archive is too large when extracted")
        if name.lower().startswith("3d/") and name.lower().endswith(".model"):
            has_model = True
    if require_model and not has_model:
        raise ValidationError("3MF archive has no 3D model")


def _validate_stl(path: Path) -> None:
    size = path.stat().st_size
    with path.open("rb") as f:
        head = f.read(84)
        if len(head) == 84:
            (count,) = struct.unpack("<I", head[80:84])
            if 84 + 50 * count == size:
                return
        f.seek(0)
        sample = f.read(4096)
    if sample.lstrip().lower().startswith(b"solid") and b"\x00" not in sample:
        return
    raise ValidationError("Not a valid STL file")


def _validate_text(path: Path) -> None:
    with path.open("rb") as f:
        sample = f.read(65536)
    if b"\x00" in sample:
        raise ValidationError("File does not look like a text model")
