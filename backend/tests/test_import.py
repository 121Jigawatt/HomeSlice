import json

import httpx
import pytest
from conftest import binary_stl

from homeslice.importer.sources import ModelImportError, parse_link
from homeslice.net.safe_fetch import SafeFetcher


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.printables.com/model/3161-3d-benchy", ("printables", "3161")),
        ("https://www.printables.com/en/model/3161-3d-benchy/files", ("printables", "3161")),
        ("https://printables.com/model/3161", ("printables", "3161")),
        ("https://makerworld.com/en/models/22677-some-name#profileId-1", ("makerworld", "22677")),
        ("https://makerworld.com/models/22677", ("makerworld", "22677")),
    ],
)
def test_parse_link(url, expected):
    assert parse_link(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://printables.com.evil.com/model/3161",
        "https://www.printables.com/social/3161",
        "ftp://www.printables.com/model/3161",
        "https://www.thingiverse.com/thing:1",
        "not a url",
    ],
)
def test_parse_link_rejects(url):
    with pytest.raises(ModelImportError):
        parse_link(url)


MODEL = {
    "id": "3161",
    "name": "Benchy",
    "license": {"abbreviation": "CC0", "name": "Public domain"},
    "user": {"publicUsername": "Prusa"},
    "images": [{"filePath": "media/prints/3161/images/a.jpg"}],
    "stls": [
        {"id": "11", "name": "benchy.stl", "fileSize": 300, "filePreviewPath": "media/p.png"},
        {"id": "12", "name": "extras.zip", "fileSize": 10, "filePreviewPath": None},
    ],
}


class FakePrintables:
    def __init__(self, link: str = "https://files.printables.com/media/benchy.stl") -> None:
        self.link = link
        self.mutations: list[dict] = []

    def api(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["query"].lstrip().startswith("mutation"):
            self.mutations.append(body["variables"])
            return httpx.Response(200, json={"data": {"getDownloadLink": {"ok": True, "output": {"link": self.link}}}})
        return httpx.Response(200, json={"data": {"print": MODEL}})

    def files(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=binary_stl(3))


@pytest.fixture
def printables(authed):
    fake = FakePrintables()
    importer = authed.app.state.services.importer
    importer.api = SafeFetcher(
        {"api.printables.com", "makerworld.com"}, max_bytes=10**6, transport=httpx.MockTransport(fake.api), resolve=False
    )
    importer.files = SafeFetcher(
        {"files.printables.com"}, max_bytes=10**6, transport=httpx.MockTransport(fake.files), resolve=False
    )
    return authed, fake


def test_printables_preview(printables):
    authed, _ = printables
    r = authed.post("/api/import/preview", json={"url": "https://www.printables.com/model/3161-benchy"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["title"] == "Benchy" and data["author"] == "Prusa" and data["license"] == "CC0"
    assert data["images"] == ["https://media.printables.com/media/prints/3161/images/a.jpg"]
    assert [(f["name"], f["importable"]) for f in data["files"]] == [("benchy.stl", True), ("extras.zip", False)]
    assert data["downloadable"] is True


def test_printables_import_into_library(printables):
    authed, fake = printables
    r = authed.post("/api/import", json={"source": "printables", "model_id": "3161", "file_ids": ["11"]})
    assert r.status_code == 201, r.text
    record = r.json()[0]
    assert record["filename"] == "benchy.stl"
    assert record["source_url"] == "https://www.printables.com/model/3161" and record["license"] == "CC0"
    assert fake.mutations == [{"id": "11", "printId": "3161"}]
    assert authed.get(f"/api/library/{record['id']}/file").content == binary_stl(3)


def test_import_rejects_file_from_other_model(printables):
    authed, fake = printables
    r = authed.post("/api/import", json={"source": "printables", "model_id": "3161", "file_ids": ["999"]})
    assert r.status_code == 400 and fake.mutations == []


def test_import_rejects_unsupported_file(printables):
    authed, _ = printables
    r = authed.post("/api/import", json={"source": "printables", "model_id": "3161", "file_ids": ["12"]})
    assert r.status_code == 400


def test_import_refuses_link_to_other_host(printables):
    authed, fake = printables
    fake.link = "https://169.254.169.254/latest/meta-data"
    r = authed.post("/api/import", json={"source": "printables", "model_id": "3161", "file_ids": ["11"]})
    assert r.status_code == 400 and "allowlist" in r.json()["detail"]
    assert authed.get("/api/library").json() == []


def test_makerworld_preview_is_not_downloadable(authed):
    design = {
        "title": "Holder",
        "license": "BY-NC-SA",
        "designCreator": {"name": "maker"},
        "coverUrl": "https://makerworld.bblmw.com/c.jpg",
        "designExtension": {
            "design_pictures": [{"url": "https://makerworld.bblmw.com/p.jpg"}, {"url": "javascript:alert(1)"}],
            "model_files": [{"modelName": "a.stl", "modelSize": 5, "thumbnailUrl": "https://makerworld.bblmw.com/t.jpg"}],
        },
    }
    importer = authed.app.state.services.importer
    importer.api = SafeFetcher(
        {"makerworld.com"}, max_bytes=10**6, transport=httpx.MockTransport(lambda r: httpx.Response(200, json=design)), resolve=False
    )
    r = authed.post("/api/import/preview", json={"url": "https://makerworld.com/en/models/22677"})
    assert r.status_code == 200, r.text
    data = r.json()
    assert data["downloadable"] is False and data["files"][0]["importable"] is False
    assert data["images"] == ["https://makerworld.bblmw.com/p.jpg"]
    assert authed.post("/api/import", json={"source": "makerworld", "model_id": "22677", "file_ids": ["0"]}).status_code == 422


def test_post_json_does_not_follow_redirects():
    import asyncio

    from homeslice.net.safe_fetch import FetchError

    fetcher = SafeFetcher(
        {"api.printables.com"},
        max_bytes=1000,
        transport=httpx.MockTransport(lambda r: httpx.Response(307, headers={"location": "/elsewhere"})),
        resolve=False,
    )
    with pytest.raises(FetchError, match="redirect"):
        asyncio.run(fetcher.post_json("https://api.printables.com/graphql/", {"q": 1}))
