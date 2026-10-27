import httpx
import pytest

from orcaserver.net.safe_fetch import FetchError, SafeFetcher


def fetcher(handler, **kw) -> SafeFetcher:
    return SafeFetcher(
        kw.pop("hosts", {"example.com", "cdn.example.com"}),
        max_bytes=kw.pop("max_bytes", 1000),
        transport=httpx.MockTransport(handler),
        resolve=False,
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://example.com/x",
        "https://evil.com/x",
        "https://user:pw@example.com/x",
        "https://example.com:8443/x",
        "file:///etc/passwd",
    ],
)
def test_url_rules(url):
    with pytest.raises(FetchError):
        fetcher(lambda r: httpx.Response(200)).check_url(url)


async def test_private_ip_blocked():
    f = SafeFetcher({"localhost", "127.0.0.1"}, max_bytes=10)
    with pytest.raises(FetchError, match="non-public"):
        await f.get_json("https://127.0.0.1/")
    with pytest.raises(FetchError, match="non-public"):
        await f.get_json("https://localhost/")


async def test_redirect_to_disallowed_host():
    def handler(request):
        return httpx.Response(302, headers={"location": "https://169.254.169.254/latest"})

    with pytest.raises(FetchError, match="allowlist"):
        await fetcher(handler).get_json("https://example.com/a")


async def test_auth_header_dropped_on_cross_host_redirect(tmp_path):
    seen = {}

    def handler(request):
        if request.url.host == "example.com":
            return httpx.Response(302, headers={"location": "https://cdn.example.com/file"})
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, content=b"abc")

    sha, size = await fetcher(handler).download(
        "https://example.com/a", tmp_path / "f", headers={"Authorization": "Bearer secret"}
    )
    assert size == 3 and seen["auth"] is None


async def test_size_cap(tmp_path):
    dest = tmp_path / "f"
    with pytest.raises(FetchError, match="too large"):
        await fetcher(lambda r: httpx.Response(200, content=b"x" * 5000)).download("https://example.com/a", dest)
    assert not dest.exists()


async def test_existing_file_not_clobbered(tmp_path):
    dest = tmp_path / "f"
    dest.write_text("keep")
    with pytest.raises(FileExistsError):
        await fetcher(lambda r: httpx.Response(200, content=b"x")).download("https://example.com/a", dest)
    assert dest.read_text() == "keep"


async def test_too_many_redirects():
    with pytest.raises(FetchError, match="redirects"):
        await fetcher(lambda r: httpx.Response(302, headers={"location": "/again"})).get_json("https://example.com/")
