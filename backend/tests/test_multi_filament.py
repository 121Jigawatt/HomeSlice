import json

from homeslice.orca.jobs import size_for_filaments


def test_size_for_filaments(tmp_path):
    process = tmp_path / "process.json"
    process.write_text(json.dumps({"name": "P", "flush_volumes_matrix": ["0", "300", "300", "0"]}))
    fils = []
    for i, colour in enumerate([["#112233"], None, "", ["#445566"]]):
        f = tmp_path / f"filament_{i}.json"
        data = {"name": f"F{i}"}
        if colour is not None:
            data["filament_colour"] = colour
        f.write_text(json.dumps(data))
        fils.append(f)

    size_for_filaments(process, fils)

    colours = [json.loads(f.read_text())["filament_colour"] for f in fils]
    assert colours == [["#112233"], ["#F2754E"], ["#F2754E"], ["#445566"]]
    p = json.loads(process.read_text())
    assert len(p["flush_volumes_matrix"]) == 16 and len(p["flush_volumes_vector"]) == 8
    assert p["flush_volumes_matrix"][:5] == ["0", "300", "300", "300", "300"]
    assert all(p["flush_volumes_matrix"][i * 5] == "0" for i in range(4))


def test_size_for_single_filament(tmp_path):
    process = tmp_path / "process.json"
    process.write_text(json.dumps({"name": "P"}))
    f = tmp_path / "filament_0.json"
    f.write_text(json.dumps({"name": "F", "filament_colour": ["#ABCDEF"]}))
    size_for_filaments(process, [f])
    assert json.loads(process.read_text())["flush_volumes_matrix"] == ["0"]
