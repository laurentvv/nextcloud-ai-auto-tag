import posixpath
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from nc_autotag.config import SshConfig

MB = 1024 * 1024


class SkipFile(Exception):
    """Fichier à ignorer (statut skipped), le message donne la raison."""


@dataclass
class Fetched:
    data: bytes | None = None
    path: Path | None = None
    _tmpdir: Path | None = None

    def cleanup(self) -> None:
        if self._tmpdir is not None:
            shutil.rmtree(self._tmpdir, ignore_errors=True)
            self._tmpdir = None
            self.path = None


def remote_path(cfg: SshConfig, user: str, path: str) -> str:
    """/Photos/a.jpg -> <data_dir>/<user>/files/Photos/a.jpg (ou stockage externe monté)."""
    clean = posixpath.normpath("/" + path.replace("\\", "/").lstrip("/"))
    if clean == "/":
        raise SkipFile(f"chemin invalide : {path}")
    for mount, target in cfg.mounts.items():
        mount = "/" + mount.strip("/")
        if clean == mount or clean.startswith(mount + "/"):
            return posixpath.join(target.rstrip("/"), clean[len(mount) :].lstrip("/"))
    return posixpath.join(cfg.data_dir.rstrip("/"), user, "files", clean.lstrip("/"))


def _safe_name(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)[:120] or "video"


class SftpSource:
    """Lecture seule des originaux par SFTP : images en mémoire, vidéos en fichier temporaire."""

    def __init__(self, cfg: SshConfig, sftp=None, connect=None):
        self.cfg = cfg
        self.sftp = sftp
        self._connect = connect
        self._conn = None
        self._owns_sftp = sftp is None

    async def __aenter__(self) -> SftpSource:
        if self.sftp is None:
            import asyncssh

            cfg = self.cfg
            known_hosts = Path("~/.ssh/known_hosts").expanduser()
            connect = self._connect or asyncssh.connect
            self._conn = await connect(
                cfg.host,
                username=cfg.user,
                client_keys=[str(cfg.key_file.expanduser())],
                known_hosts=str(known_hosts) if known_hosts.exists() else None,
                passphrase=None,
                connect_timeout=15,
            )
            self.sftp = await self._conn.start_sftp_client()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._owns_sftp and self.sftp is not None:
            self.sftp.exit()
        if self._conn is not None:
            self._conn.close()
            await self._conn.wait_closed()

    async def fetch(self, user: str, path: str, is_video: bool) -> Fetched:
        remote = remote_path(self.cfg, user, path)
        limit = (self.cfg.max_video_mb if is_video else self.cfg.max_image_mb) * MB
        try:
            attrs = await self.sftp.stat(remote)
        except (FileNotFoundError, PermissionError) as e:
            raise SkipFile(f"absent du disque du serveur ({type(e).__name__})") from e
        except Exception as e:
            if type(e).__name__ in ("SFTPNoSuchFile", "SFTPNoSuchPath", "SFTPPermissionDenied"):
                raise SkipFile(f"absent du disque du serveur ({type(e).__name__})") from e
            raise
        if attrs.size is not None and attrs.size > limit:
            raise SkipFile(f"trop gros ({attrs.size // MB} Mo > {limit // MB} Mo)")
        if not is_video:
            async with self.sftp.open(remote, "rb") as f:
                return Fetched(data=await f.read())
        tmpdir = Path(tempfile.mkdtemp(prefix="nc-autotag-"))
        target = tmpdir / _safe_name(posixpath.basename(remote))
        try:
            await self.sftp.get(remote, str(target))
        except BaseException:
            shutil.rmtree(tmpdir, ignore_errors=True)
            raise
        return Fetched(path=target, _tmpdir=tmpdir)
