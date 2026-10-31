import asyncio
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime

import httpx
from pydantic import SecretStr

from nc_autotag.nextcloud.blackout import in_blackout, parse_windows, seconds_until_end

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "PROPFIND", "SEARCH", "REPORT"})
RETRY_STATUS = frozenset({429, 502, 503, 504})
MAX_BACKOFF = 300.0


class NextcloudError(Exception):
    pass


class NcUnavailable(NextcloudError):
    """Serveur indisponible (maintenance, coupure) après toutes les tentatives."""


class ReadOnlyViolation(NextcloudError):
    pass


@dataclass
class Entry:
    href: str
    props: dict[str, str | None] = field(default_factory=dict)
    collection: bool = False


def parse_multistatus(xml: bytes | str) -> list[Entry]:
    """Réponses 207 : propriétés du premier propstat en 200, sans préfixe d'espace de noms."""
    root = ET.fromstring(xml)
    entries = []
    for resp in root.iter(f"{DAV}response"):
        href = (resp.findtext(f"{DAV}href") or "").strip()
        entry = Entry(href)
        for propstat in resp.findall(f"{DAV}propstat"):
            if "200" not in (propstat.findtext(f"{DAV}status") or ""):
                continue
            prop = propstat.find(f"{DAV}prop")
            if prop is None:
                continue
            for el in prop:
                name = el.tag.rsplit("}", 1)[-1]
                if name == "resourcetype":
                    entry.collection = el.find(f"{DAV}collection") is not None
                else:
                    entry.props[name] = (el.text or "").strip() if el.text else None
        entries.append(entry)
    return entries


class NcClient:
    """Client WebDAV d'un compte (Basic, mot de passe d'application) avec reprises et blackout."""

    def __init__(
        self,
        base_url: str,
        user: str,
        password: SecretStr,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        blackout: list[str] | None = None,
        concurrency: int = 4,
        retries: int = 8,
        backoff: float = 5.0,
        read_only: bool = False,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = datetime.now,
        on_wait: Callable[[str, float], None] | None = None,
        timeout: float = 120.0,
    ):
        self.user = user
        self.windows = parse_windows(blackout or [])
        self.retries = retries
        self.backoff = backoff
        self.read_only = read_only
        self.sleep = sleep
        self.now = now
        self.on_wait = on_wait
        self.sem = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            auth=(user, password.get_secret_value()),
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self.http.aclose()

    async def wait_blackout(self) -> None:
        delay = seconds_until_end(self.now(), self.windows)
        if delay > 0:
            if self.on_wait:
                self.on_wait("blackout", delay)
            await self.sleep(delay)

    def blocked(self) -> bool:
        return in_blackout(self.now(), self.windows)

    async def request(self, method: str, url: str, **kw) -> httpx.Response:
        safe = method in SAFE_METHODS
        if not safe and self.read_only:
            raise ReadOnlyViolation(f"{method} interdit en lecture seule")
        attempt = 0
        while True:
            if not safe:
                await self.wait_blackout()
            try:
                async with self.sem:
                    resp = await self.http.request(method, url, **kw)
            except httpx.TransportError as e:
                reason, retry_after = type(e).__name__, None
            else:
                if resp.status_code not in RETRY_STATUS:
                    return resp
                reason, retry_after = f"HTTP {resp.status_code}", resp.headers.get("Retry-After")
            attempt += 1
            if attempt > self.retries:
                raise NcUnavailable(f"{method} {url} : {reason} après {self.retries} reprises")
            delay = min(self.backoff * 2 ** (attempt - 1), MAX_BACKOFF)
            if retry_after and retry_after.isdigit():
                delay = min(max(delay, float(retry_after)), MAX_BACKOFF)
            if self.on_wait:
                self.on_wait(reason, delay)
            await self.sleep(delay)
