import json
import re

import httpx
from pydantic import SecretStr

from nc_autotag.nextcloud.client import NcClient

NS = 'xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns"'


def multistatus(*responses: str) -> str:
    return f'<?xml version="1.0"?><d:multistatus {NS}>{"".join(responses)}</d:multistatus>'


def response(href: str, props: str) -> str:
    return (
        f"<d:response><d:href>{href}</d:href><d:propstat><d:prop>{props}</d:prop>"
        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>"
    )


class FakeNextcloud:
    """Serveur systemtags minimal et sans état partagé hors de l'objet ; journalise les requêtes."""

    def __init__(
        self, tags: dict[str, int] | None = None, manual: dict[int, set[str]] | None = None
    ):
        self.tags: dict[int, str] = {i: n for n, i in (tags or {}).items()}
        self.rels: dict[int, set[int]] = {}
        self.next_id = max(self.tags, default=0) + 100
        self.requests: list[tuple[str, str]] = []
        self.fail_assign: set[int] = set()
        for fileid, names in (manual or {}).items():
            for name in names:
                self.rels.setdefault(fileid, set()).add(self.tag_id(name, create=True))

    def tag_id(self, name: str, create: bool = False) -> int | None:
        for i, n in self.tags.items():
            if n.casefold() == name.casefold():
                return i
        if not create:
            return None
        self.next_id += 1
        self.tags[self.next_id] = name
        return self.next_id

    def names(self, fileid: int) -> set[str]:
        return {self.tags[i] for i in self.rels.get(fileid, set())}

    @property
    def writes(self) -> list[tuple[str, str]]:
        return [r for r in self.requests if r[0] not in ("GET", "PROPFIND", "SEARCH")]

    def handler(self, request: httpx.Request) -> httpx.Response:
        method, path = request.method, request.url.path
        self.requests.append((method, path))
        if path == "/remote.php/dav/systemtags/":
            if method == "PROPFIND":
                rows = [response(path, "")]
                for i, n in self.tags.items():
                    rows.append(
                        response(
                            f"{path}{i}",
                            f"<oc:id>{i}</oc:id><oc:display-name>{n}</oc:display-name>"
                            "<oc:user-visible>true</oc:user-visible>"
                            "<oc:user-assignable>true</oc:user-assignable>",
                        )
                    )
                return httpx.Response(207, text=multistatus(*rows))
            if method == "POST":
                name = json.loads(request.content)["name"]
                if self.tag_id(name) is not None:
                    return httpx.Response(409)
                new = self.tag_id(name, create=True)
                return httpx.Response(
                    201, headers={"Content-Location": f"/remote.php/dav/systemtags/{new}"}
                )
        m = re.fullmatch(r"/remote\.php/dav/systemtags-relations/files/(\d+)", path)
        if m and method == "PROPFIND":
            ids = self.rels.get(int(m[1]), set())
            rows = [response(path + "/", "")]
            rows += [response(f"{path}/{i}", f"<oc:id>{i}</oc:id>") for i in sorted(ids)]
            return httpx.Response(207, text=multistatus(*rows))
        m = re.fullmatch(r"/remote\.php/dav/systemtags-relations/files/(\d+)/(\d+)", path)
        if m:
            fileid, tag_id = int(m[1]), int(m[2])
            if method == "PUT":
                if tag_id in self.fail_assign:
                    return httpx.Response(403)
                if tag_id in self.rels.get(fileid, set()):
                    return httpx.Response(409)
                self.rels.setdefault(fileid, set()).add(tag_id)
                return httpx.Response(201)
            if method == "DELETE":
                if tag_id not in self.rels.get(fileid, set()):
                    return httpx.Response(404)
                self.rels[fileid].discard(tag_id)
                return httpx.Response(204)
        return httpx.Response(500, text=f"inattendu : {method} {path}")


def nc_client(handler, user: str = "alice", **kw) -> NcClient:
    async def no_sleep(_: float) -> None:
        return None

    kw.setdefault("sleep", no_sleep)
    return NcClient(
        "https://nc.test", user, SecretStr("secret"), transport=httpx.MockTransport(handler), **kw
    )
