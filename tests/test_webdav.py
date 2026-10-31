import httpx
import pytest

from fake_nc import nc_client
from nc_autotag.nextcloud import webdav
from nc_autotag.nextcloud.client import NcUnavailable, ReadOnlyViolation
from nc_autotag.nextcloud.webdav import parse_files, relative_path

BASE = "/remote.php/dav/files/alice"

SEARCH_XML = f"""<?xml version="1.0"?>
<d:multistatus xmlns:d="DAV:" xmlns:s="http://sabredav.org/ns" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
<d:response><d:href>{BASE}/Photos/%C3%A9t%C3%A9%202021/IMG%20001.jpg</d:href><d:propstat><d:prop>
<oc:fileid>5285</oc:fileid><d:getetag>&quot;a158287cc20a243e602b1cf128cef453&quot;</d:getetag>
<d:getcontenttype>image/jpeg</d:getcontenttype><d:getcontentlength>224286</d:getcontentlength>
<d:getlastmodified>Sat, 27 Nov 2021 20:11:59 GMT</d:getlastmodified></d:prop>
<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>{BASE}/Vid%C3%A9os/No%C3%ABl.mp4</d:href><d:propstat><d:prop>
<oc:fileid>6001</oc:fileid><d:getetag>W/&quot;abc&quot;</d:getetag>
<d:getcontenttype>video/mp4</d:getcontenttype><d:getcontentlength>1048576</d:getcontentlength>
<d:getlastmodified>Sun, 28 Nov 2021 10:00:00 GMT</d:getlastmodified></d:prop>
<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>/remote.php/dav/trashbin/alice/trash/old.jpg</d:href><d:propstat><d:prop>
<oc:fileid>7</oc:fileid><d:getetag>&quot;t&quot;</d:getetag><d:getcontenttype>image/jpeg</d:getcontenttype>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>{BASE}/notes.txt</d:href><d:propstat><d:prop>
<oc:fileid>8</oc:fileid><d:getetag>&quot;n&quot;</d:getetag><d:getcontenttype>text/plain</d:getcontenttype>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>{BASE}/Photos/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/></d:resourcetype><oc:fileid>9</oc:fileid>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""


def test_parse_search_decodes_and_filters():
    files = parse_files(SEARCH_XML, "alice")
    assert [(f.fileid, f.path, f.mimetype, f.size) for f in files] == [
        (5285, "/Photos/été 2021/IMG 001.jpg", "image/jpeg", 224286),
        (6001, "/Vidéos/Noël.mp4", "video/mp4", 1048576),
    ]
    assert files[0].etag == "a158287cc20a243e602b1cf128cef453"
    assert files[1].etag == "abc"
    assert files[0].modified == "Sat, 27 Nov 2021 20:11:59 GMT"


def test_relative_path():
    assert relative_path(f"{BASE}/a%20b/c.jpg", "alice") == "/a b/c.jpg"
    assert relative_path(f"https://x{BASE}/c.jpg", "alice") == "/c.jpg"
    assert relative_path("/remote.php/dav/trashbin/alice/x.jpg", "alice") is None
    assert relative_path(f"{BASE}/", "alice") == "/"


def search_handler(calls: list):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path, request.content.decode()))
        return httpx.Response(207, text=SEARCH_XML)

    return handler


async def test_inventory_search_request_and_result():
    calls: list = []
    client = nc_client(search_handler(calls), read_only=True)
    files = await webdav.inventory(client, "alice", search_limit=1000)
    await client.aclose()
    assert {f.fileid for f in files} == {5285, 6001}
    assert [c[0] for c in calls] == ["SEARCH", "SEARCH"]
    body = calls[0][2]
    assert "<d:href>/files/alice</d:href>" in body
    assert "<d:literal>image/%</d:literal>" in body
    assert "<d:literal>video/%</d:literal>" in calls[1][2]
    assert "<d:nresults>1000</d:nresults>" in body
    assert calls[0][1] == "/remote.php/dav/"


FOLDER_ROOT = f"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
<d:response><d:href>{BASE}/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>{BASE}/Photos%20%C3%A9t%C3%A9/</d:href><d:propstat><d:prop>
<d:resourcetype><d:collection/></d:resourcetype><oc:fileid>2</oc:fileid></d:prop>
<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>{BASE}/root.png</d:href><d:propstat><d:prop><d:resourcetype/>
<oc:fileid>10</oc:fileid><d:getetag>&quot;r&quot;</d:getetag><d:getcontenttype>image/png</d:getcontenttype>
<d:getcontentlength>5</d:getcontentlength></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""

FOLDER_SUB = f"""<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">
<d:response><d:href>{BASE}/Photos%20%C3%A9t%C3%A9/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype>
</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
<d:response><d:href>{BASE}/Photos%20%C3%A9t%C3%A9/a%20b.jpg</d:href><d:propstat><d:prop><d:resourcetype/>
<oc:fileid>11</oc:fileid><d:getetag>&quot;s&quot;</d:getetag><d:getcontenttype>image/jpeg</d:getcontenttype>
<d:getcontentlength>7</d:getcontentlength></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>
</d:multistatus>"""


async def test_inventory_falls_back_to_propfind_when_search_fails():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.raw_path.decode(), request.headers.get("Depth")))
        if request.method == "SEARCH":
            return httpx.Response(400)
        if request.url.path.endswith("/alice/"):
            return httpx.Response(207, text=FOLDER_ROOT)
        return httpx.Response(207, text=FOLDER_SUB)

    client = nc_client(handler, read_only=True)
    files = await webdav.inventory(client, "alice")
    await client.aclose()
    assert sorted((f.fileid, f.path) for f in files) == [
        (10, "/root.png"),
        (11, "/Photos été/a b.jpg"),
    ]
    propfinds = [s for s in seen if s[0] == "PROPFIND"]
    assert len(propfinds) == 2 and all(p[2] == "1" for p in propfinds)


async def test_inventory_falls_back_when_search_hits_limit():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "SEARCH":
            return httpx.Response(207, text=SEARCH_XML)  # 5 réponses
        return httpx.Response(207, text=FOLDER_SUB)

    client = nc_client(handler, read_only=True)
    files = await webdav.inventory(client, "alice", search_limit=5)
    await client.aclose()
    assert [f.fileid for f in files] == [11]


async def test_retry_on_503_then_success():
    attempts = []
    slept = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            return httpx.Response(503, headers={"Retry-After": "30"})
        return httpx.Response(207, text=SEARCH_XML)

    async def sleep(s: float) -> None:
        slept.append(s)

    client = nc_client(handler, sleep=sleep, backoff=2.0)
    resp = await client.request("PROPFIND", "/x")
    await client.aclose()
    assert resp.status_code == 207 and len(attempts) == 3
    assert slept == [30.0, 30.0]


async def test_maintenance_gives_up_after_retries():
    client = nc_client(lambda r: httpx.Response(503), retries=2)
    with pytest.raises(NcUnavailable):
        await client.request("PROPFIND", "/x")
    await client.aclose()


async def test_read_only_refuses_writes():
    seen = []
    client = nc_client(lambda r: seen.append(r) or httpx.Response(204), read_only=True)
    with pytest.raises(ReadOnlyViolation):
        await client.request("DELETE", "/x")
    await client.aclose()
    assert seen == []
