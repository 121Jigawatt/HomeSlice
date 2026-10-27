import json
from pathlib import Path

import pytest

from homeslice.orca.cli import SliceArgsError, SliceSpec, build_args, validate_overrides
from homeslice.orca.profiles import ProfileError, ProfileStore


def write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))


@pytest.fixture
def datadir(tmp_path: Path) -> Path:
    d = tmp_path / "orca"
    write(d / "system/Voron/machine/fdm_common.json", {"name": "fdm_common", "instantiation": "false", "a": 1, "b": 1})
    write(
        d / "system/Voron/machine/Voron 0.4.json",
        {"name": "Voron 0.4", "inherits": "fdm_common", "instantiation": "true", "b": 2},
    )
    write(d / "user/default/machine/My Voron.json", {"name": "My Voron", "inherits": "Voron 0.4", "c": 3})
    write(d / "user/default/machine/Loop A.json", {"name": "Loop A", "inherits": "Loop B"})
    write(d / "user/default/machine/Loop B.json", {"name": "Loop B", "inherits": "Loop A"})
    return d


def test_resolve_inheritance(datadir):
    resolved = ProfileStore(datadir).resolve("printer", "My Voron")
    assert resolved["a"] == 1 and resolved["b"] == 2 and resolved["c"] == 3
    assert resolved["name"] == "My Voron"
    # Orca derives the system name from `inherits` for user presets.
    assert resolved["from"] == "User" and resolved["inherits"] == "Voron 0.4"


def test_resolve_system_preset(datadir):
    resolved = ProfileStore(datadir).resolve("printer", "Voron 0.4")
    assert resolved["from"] == "system" and "inherits" not in resolved
    assert resolved["a"] == 1 and resolved["b"] == 2


def test_listing(datadir):
    store = ProfileStore(datadir)
    assert [p["name"] for p in store.list("printer")] == ["Loop A", "Loop B", "My Voron"]
    names = [p["name"] for p in store.list("printer", include_system=True)]
    assert "Voron 0.4" in names and "fdm_common" not in names


def test_loop_and_missing(datadir):
    store = ProfileStore(datadir)
    with pytest.raises(ProfileError, match="loop"):
        store.resolve("printer", "Loop A")
    with pytest.raises(ProfileError, match="not found"):
        store.resolve("printer", "../../etc/passwd")


def spec(tmp_path: Path, **kw) -> SliceSpec:
    return SliceSpec(
        inputs=[tmp_path / "m.stl"],
        printer_json=tmp_path / "p.json",
        process_json=tmp_path / "q.json",
        filament_jsons=[tmp_path / "f.json"],
        **kw,
    )


def test_build_args(tmp_path):
    args = build_args(tmp_path / "AppRun", spec(tmp_path, overrides={"layer_height": "0.2"}), tmp_path, tmp_path, tmp_path / "l")
    assert "--layer-height=0.2" in args
    assert "--allow-newer-file" in args
    assert args[args.index("--slice") + 1] == "0"
    assert args[-1] == str(tmp_path / "m.stl")


@pytest.mark.parametrize(
    "overrides",
    [
        {"post_process": "rm -rf /"},
        {"layer_height": "5"},
        {"sparse_infill_density": "15%; --post-process=x"},
        {"support_type": "bogus"},
    ],
)
def test_overrides_rejected(overrides):
    with pytest.raises(SliceArgsError):
        validate_overrides(overrides)


def test_unsafe_paths_rejected(tmp_path):
    with pytest.raises(SliceArgsError):
        build_args(tmp_path / "AppRun", spec(tmp_path, ), tmp_path / "a;b", tmp_path, tmp_path / "l")
    s = spec(tmp_path)
    s.inputs = [Path("-evil.stl")]
    with pytest.raises(SliceArgsError):
        build_args(tmp_path / "AppRun", s, tmp_path, tmp_path, tmp_path / "l")


@pytest.fixture
def vendor_dir(tmp_path: Path) -> Path:
    d = tmp_path / "orca2"
    s = d / "system/Sovol"
    write(s / "machine/fdm_sv08.json", {"name": "fdm_sv08", "instantiation": "false", "printer_model": "Sovol SV08"})
    for nozzle in ("0.4", "0.6"):
        write(
            s / f"machine/Sovol SV08 {nozzle} nozzle.json",
            {"name": f"Sovol SV08 {nozzle} nozzle", "inherits": "fdm_sv08", "printer_variant": nozzle},
        )
    write(s / "machine/Other 0.4.json", {"name": "Other 0.4", "printer_model": "Other", "printer_variant": "0.4"})
    write(s / "process/SV08 0.4.json", {"name": "SV08 0.4", "compatible_printers": ["Sovol SV08 0.4 nozzle"]})
    write(s / "process/SV08 0.6.json", {"name": "SV08 0.6", "compatible_printers": ["Sovol SV08 0.6 nozzle"]})
    write(s / "process/Other.json", {"name": "Other", "compatible_printers": ["Other 0.4"]})
    write(s / "filament/Sovol SV08 PETG.json", {"name": "Sovol SV08 PETG"})
    write(s / "filament/Generic ABS.json", {"name": "Generic ABS"})
    write(d / "user/default/machine/My SV08.json", {"name": "My SV08", "inherits": "Sovol SV08 0.6 nozzle"})
    conf = {
        "models": [{"vendor": "Sovol", "model": "Sovol SV08", "nozzle_diameter": "0.4"}],
        "filaments": ["Sovol SV08 PETG"],
    }
    (d / "OrcaSlicer.conf").write_text(json.dumps(conf, indent=4) + "\n# MD5 checksum ABC\n")
    return d


def names(store: ProfileStore, kind: str) -> list[str]:
    return [p["name"] for p in store.list(kind, include_system=True)]


def test_system_presets_follow_orca_selections(vendor_dir):
    store = ProfileStore(vendor_dir)
    assert names(store, "printer") == ["My SV08", "Sovol SV08 0.4 nozzle", "Sovol SV08 0.6 nozzle"]
    # 0.6 is not enabled, but the user's printer is based on it, so its processes stay visible.
    assert names(store, "process") == ["SV08 0.4", "SV08 0.6"]
    assert names(store, "filament") == ["Sovol SV08 PETG"]
    assert [p["name"] for p in store.list("printer")] == ["My SV08"]


def test_all_system_presets_without_orca_config(vendor_dir):
    (vendor_dir / "OrcaSlicer.conf").unlink()
    assert "Other 0.4" in names(ProfileStore(vendor_dir), "printer")


def test_bed_type_override():
    assert validate_overrides({"curr_bed_type": "Textured PEI Plate"}) == {"curr_bed_type": "Textured PEI Plate"}
    with pytest.raises(SliceArgsError):
        validate_overrides({"curr_bed_type": "Glass; --post-process=x"})
