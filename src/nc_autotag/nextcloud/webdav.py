import asyncio
from dataclasses import dataclass
from urllib.parse import quote, unquote

from nc_autotag.nextcloud.client import Entry, NcClient, NextcloudError, parse_multistatus

PROPS = "<oc:fileid/><d:getetag/><d:getcontenttype/><d:getcontentlength/><d:getlastmodified/>"
NS = 'xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"'
MIME_PREFIXES = ("image/", "video/")


@dataclass(frozen=True)
class RemoteFile:
    fileid: int
    path: str
    etag: str
    mimetype: str
    size: int | None
    modified: str | None


def files_prefix(user: str) -> str:
    return f"/remote.php/dav/files/{quote(user, safe='')}/"


def relative_path(href: str, user: str) -> str | None:
    """Chemin décodé relatif à la racine du compte (« /Photos/été 1.jpg »), None hors /files/."""
    prefix = files_prefix(user)
    i = href.find(prefix)
    if i < 0:
        return None
    return "/" + unquote(href[i + len(prefix) :]).strip("/")


def _etag(value: str | None) -> str:
    value = (value or "").strip()
    if value.startswith("W/"):
        value = value[2:]
    return value.strip('"')


def to_remote_file(entry: Entry, user: str) -> RemoteFile | None:
    mime = entry.props.get("getcontenttype") or ""
    path = relative_path(entry.href, user)
    fileid = entry.props.get("fileid")
    if entry.collection or path is None or not mime.startswith(MIME_PREFIXES):
        return None
    if not fileid or not fileid.isdigit():
        return None
    size = entry.props.get("getcontentlength")
    return RemoteFile(
        int(fileid),
        path,
        _etag(entry.props.get("getetag")),
        mime,
        int(size) if size and size.isdigit() else None,
        entry.props.get("getlastmodified"),
    )


def parse_files(xml: bytes | str, user: str) -> list[RemoteFile]:
    """Images et vidéos d'une réponse SEARCH ou PROPFIND ; trashbin/versions/appdata exclus
    d'office, car hors de /remote.php/dav/files/<user>/."""
    files = (to_remote_file(e, user) for e in parse_multistatus(xml))
    return [f for f in files if f is not None]


def search_body(user: str, mime_like: str, limit: int) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<d:searchrequest {NS}><d:basicsearch>"
        f"<d:select><d:prop>{PROPS}</d:prop></d:select>"
        f"<d:from><d:scope><d:href>/files/{quote(user, safe='')}</d:href>"
        "<d:depth>infinity</d:depth></d:scope></d:from>"
        f"<d:where><d:like><d:prop><d:getcontenttype/></d:prop><d:literal>{mime_like}</d:literal>"
        "</d:like></d:where>"
        f"<d:limit><d:nresults>{limit}</d:nresults></d:limit>"
        "</d:basicsearch></d:searchrequest>"
    )


def propfind_body() -> str:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><d:propfind {NS}><d:prop>'
        f"<d:resourcetype/>{PROPS}</d:prop></d:propfind>"
    )


class SearchIncomplete(NextcloudError):
    pass


async def _search(client: NcClient, user: str, limit: int) -> list[RemoteFile]:
    """SEARCH par type MIME. Le serveur ignore firstresult : pas de pagination, un plafond
    atteint force le repli sur PROPFIND."""
    files: dict[int, RemoteFile] = {}
    for like in ("image/%", "video/%"):
        resp = await client.request(
            "SEARCH",
            "/remote.php/dav/",
            content=search_body(user, like, limit),
            headers={"Content-Type": "text/xml"},
        )
        if resp.status_code != 207:
            raise SearchIncomplete(f"SEARCH : statut HTTP {resp.status_code}")
        entries = parse_multistatus(resp.content)
        if len(entries) >= limit:
            raise SearchIncomplete(f"SEARCH : plafond de {limit} résultats atteint")
        for entry in entries:
            f = to_remote_file(entry, user)
            if f:
                files[f.fileid] = f
    return list(files.values())


async def _walk(client: NcClient, user: str) -> list[RemoteFile]:
    """PROPFIND Depth 1 dossier par dossier, avec la concurrence du client."""
    root = files_prefix(user)
    files: dict[int, RemoteFile] = {}

    async def visit(href: str, tasks: list[asyncio.Task]) -> None:
        resp = await client.request(
            "PROPFIND", href, content=propfind_body(), headers={"Depth": "1"}
        )
        if resp.status_code != 207:
            raise NextcloudError(f"PROPFIND {href} : statut HTTP {resp.status_code}")
        for entry in parse_multistatus(resp.content):
            child = relative_path(entry.href, user)
            if child is None:
                continue
            if entry.collection:
                if entry.href.rstrip("/") != href.rstrip("/") and child != "/":
                    tasks.append(asyncio.create_task(visit(entry.href, tasks)))
                continue
            f = to_remote_file(entry, user)
            if f:
                files[f.fileid] = f

    tasks: list[asyncio.Task] = []
    first = asyncio.create_task(visit(root, tasks))
    tasks.append(first)
    done = 0
    try:
        while done < len(tasks):
            await tasks[done]
            done += 1
    except BaseException:
        for t in tasks:
            t.cancel()
        raise
    return list(files.values())


async def inventory(client: NcClient, user: str, search_limit: int = 200000) -> list[RemoteFile]:
    """Images et vidéos du compte : SEARCH, ou PROPFIND récursif si SEARCH est incomplet."""
    try:
        return await _search(client, user, search_limit)
    except SearchIncomplete:
        return await _walk(client, user)
