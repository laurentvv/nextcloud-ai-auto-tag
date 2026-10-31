from nc_autotag.nextcloud.client import NcClient, NextcloudError, parse_multistatus

TAGS_URL = "/remote.php/dav/systemtags/"
RELATIONS_URL = "/remote.php/dav/systemtags-relations/files/"
NS = 'xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"'
LIST_BODY = (
    f'<?xml version="1.0" encoding="UTF-8"?><d:propfind {NS}><d:prop>'
    "<oc:id/><oc:display-name/><oc:user-visible/><oc:user-assignable/></d:prop></d:propfind>"
)
IDS_BODY = (
    f'<?xml version="1.0" encoding="UTF-8"?><d:propfind {NS}><d:prop><oc:id/></d:prop></d:propfind>'
)
CREATE_BODY = {"userVisible": True, "userAssignable": True, "canAssign": True}


class TagError(NextcloudError):
    pass


def _flag(value: str | None) -> bool:
    return (value or "").lower() == "true"


class TagClient:
    """Tags système Nextcloud (globaux). Ne supprime jamais un tag."""

    def __init__(self, client: NcClient):
        self.client = client
        self.by_id: dict[int, str] = {}
        self.assignable: dict[str, int] = {}  # nom en minuscules -> id (tags posables)
        self.blocked: set[str] = set()  # noms existants mais invisibles ou non assignables

    async def list_tags(self) -> dict[int, str]:
        resp = await self.client.request(
            "PROPFIND", TAGS_URL, content=LIST_BODY, headers={"Depth": "1"}
        )
        if resp.status_code != 207:
            raise TagError(f"Liste des tags : statut HTTP {resp.status_code}")
        self.by_id, self.assignable, self.blocked = {}, {}, set()
        for e in parse_multistatus(resp.content):
            raw = e.props.get("id")
            name = e.props.get("display-name")
            if not raw or not raw.isdigit() or not name:
                continue
            tag_id = int(raw)
            self.by_id[tag_id] = name
            key = name.casefold()
            if _flag(e.props.get("user-visible")) and _flag(e.props.get("user-assignable")):
                self.assignable.setdefault(key, tag_id)
            else:
                self.blocked.add(key)
        return self.by_id

    def find(self, name: str) -> int | None:
        """Id d'un tag existant, sans tenir compte de la casse."""
        return self.assignable.get(name.casefold())

    def name_of(self, tag_id: int) -> str | None:
        return self.by_id.get(tag_id)

    async def file_tag_ids(self, fileid: int) -> set[int]:
        resp = await self.client.request(
            "PROPFIND", f"{RELATIONS_URL}{fileid}", content=IDS_BODY, headers={"Depth": "1"}
        )
        if resp.status_code == 404:
            raise TagError(f"Fichier {fileid} introuvable")
        if resp.status_code != 207:
            raise TagError(f"Tags du fichier {fileid} : statut HTTP {resp.status_code}")
        ids = set()
        for e in parse_multistatus(resp.content):
            raw = e.props.get("id")
            if raw and raw.isdigit():
                ids.add(int(raw))
        return ids

    async def file_tags(self, fileid: int) -> set[str]:
        """Noms (minuscules) des tags du fichier ; relit la liste si un id est inconnu."""
        ids = await self.file_tag_ids(fileid)
        if ids - self.by_id.keys():
            await self.list_tags()
        return {self.by_id[i].casefold() for i in ids if i in self.by_id}

    async def ensure_tag(self, name: str) -> int:
        """Réutilise le tag existant (casse ignorée), sinon le crée ; 409 : relit et réutilise."""
        if not self.by_id:
            await self.list_tags()
        existing = self.find(name)
        if existing is not None:
            return existing
        if name.casefold() in self.blocked:
            raise TagError(f"Le tag « {name} » existe mais n'est pas assignable")
        resp = await self.client.request("POST", TAGS_URL, json={"name": name, **CREATE_BODY})
        if resp.status_code == 201:
            location = resp.headers.get("Content-Location", "")
            tail = location.rstrip("/").rsplit("/", 1)[-1]
            if not tail.isdigit():
                raise TagError(f"Création du tag « {name} » : identifiant absent de la réponse")
            tag_id = int(tail)
            self.by_id[tag_id] = name
            self.assignable[name.casefold()] = tag_id
            return tag_id
        if resp.status_code == 409:
            await self.list_tags()
            existing = self.find(name)
            if existing is None:
                raise TagError(f"Conflit à la création du tag « {name} » mais tag introuvable")
            return existing
        raise TagError(f"Création du tag « {name} » : statut HTTP {resp.status_code}")

    async def assign(self, fileid: int, tag_id: int) -> bool:
        """True si l'affectation vient d'être posée ; False si elle existait déjà (409)."""
        resp = await self.client.request("PUT", f"{RELATIONS_URL}{fileid}/{tag_id}")
        if resp.status_code in (201, 204):
            return True
        if resp.status_code == 409:
            return False
        raise TagError(f"Affectation {tag_id} -> fichier {fileid} : statut HTTP {resp.status_code}")

    async def unassign(self, fileid: int, tag_id: int) -> bool:
        """True si retirée ; False si elle n'existait plus (404)."""
        resp = await self.client.request("DELETE", f"{RELATIONS_URL}{fileid}/{tag_id}")
        if resp.status_code in (200, 204):
            return True
        if resp.status_code == 404:
            return False
        raise TagError(f"Retrait {tag_id} du fichier {fileid} : statut HTTP {resp.status_code}")
