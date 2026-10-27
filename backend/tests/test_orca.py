import json
from pathlib import Path

import pytest

from orcaserver.orca.cli import SliceArgsError, SliceSpec, build_args, validate_overrides
from orcaserver.orca.profiles import ProfileError, ProfileStore


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
    assert "inherits" not in resolved and resolved["name"] == "My Voron"


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
