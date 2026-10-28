import json


def write(settings, kind, data):
    d = settings.orca_datadir / "user/default" / kind
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{data['name']}.json").write_text(json.dumps(data))


def test_layer_heights_follow_printer_limits(authed, settings):
    write(settings, "machine", {"name": "M04", "nozzle_diameter": ["0.4"], "min_layer_height": ["0.08"], "max_layer_height": ["0.28"]})
    write(settings, "machine", {"name": "M06", "nozzle_diameter": ["0.6"]})
    write(settings, "process", {"name": "P", "layer_height": "0.2"})

    r = authed.get("/api/layer-heights", params={"printer": "M04", "process": "P"}).json()
    assert r["heights"] == [0.08, 0.10, 0.12, 0.16, 0.20, 0.24, 0.28]
    assert r["process_default"] == 0.2 and r["nozzle"] == 0.4

    # Without limits in the profile: 20-75% of the nozzle.
    r = authed.get("/api/layer-heights", params={"printer": "M06"}).json()
    assert r["min"] == 0.12 and r["max"] == 0.45 and r["heights"][0] == 0.12 and r["heights"][-1] == 0.40
    assert authed.get("/api/layer-heights", params={"printer": "nope"}).status_code == 404


def test_support_type_override_allowed(authed, settings):
    from homeslice.orca.cli import validate_overrides

    assert validate_overrides({"enable_support": "1", "support_type": "tree(auto)"}) == {
        "enable_support": "1",
        "support_type": "tree(auto)",
    }
