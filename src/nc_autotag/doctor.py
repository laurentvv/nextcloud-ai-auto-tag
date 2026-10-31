import asyncio
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from nc_autotag.config import Settings
from nc_autotag.secrets import get_app_password

OK, FAIL, SKIP = "OK", "FAIL", "SKIP"


@dataclass
class Result:
    name: str
    status: str
    message: str = ""


def check_credentials(settings: Settings) -> list[Result]:
    results = []
    for user in settings.nextcloud.users:
        found = get_app_password(user) is not None
        results.append(
            Result(
                f"credentials:{user}",
                OK if found else FAIL,
                "mot de passe d'application trouvé"
                if found
                else "mot de passe d'application absent",
            )
        )
    return results


def check_webdav(settings: Settings, transport: httpx.BaseTransport | None = None) -> list[Result]:
    results = []
    for user in settings.nextcloud.users:
        name = f"webdav:{user}"
        password = get_app_password(user)
        if password is None:
            results.append(Result(name, SKIP, "pas d'identifiants"))
            continue
        url = f"{settings.nextcloud.base_url.rstrip('/')}/remote.php/dav/files/{user}/"
        try:
            with httpx.Client(timeout=10, transport=transport) as client:
                resp = client.request(
                    "PROPFIND",
                    url,
                    headers={"Depth": "0"},
                    auth=(user, password.get_secret_value()),
                )
        except httpx.HTTPError as exc:
            results.append(Result(name, FAIL, f"erreur réseau ({type(exc).__name__})"))
            continue
        if resp.status_code == 207:
            results.append(Result(name, OK, "PROPFIND 207"))
        elif resp.status_code == 401:
            results.append(Result(name, FAIL, "401 : identifiants refusés"))
        else:
            results.append(Result(name, FAIL, f"statut HTTP {resp.status_code} (207 attendu)"))
    return results


async def _ssh_probe(settings: Settings) -> None:
    import asyncssh

    ssh = settings.ssh
    known_hosts = Path("~/.ssh/known_hosts").expanduser()
    async with await asyncio.wait_for(
        asyncssh.connect(
            ssh.host,
            username=ssh.user,
            client_keys=[str(ssh.key_file.expanduser())],
            known_hosts=str(known_hosts) if known_hosts.exists() else None,
            passphrase=None,
            connect_timeout=10,
        ),
        timeout=15,
    ) as conn:
        async with conn.start_sftp_client() as sftp:
            await sftp.listdir(ssh.data_dir)


def check_ssh(settings: Settings) -> list[Result]:
    name = "ssh"
    key = settings.ssh.key_file.expanduser()
    if not key.exists():
        return [Result(name, FAIL, f"clé SSH introuvable : {key}")]
    try:
        asyncio.run(_ssh_probe(settings))
    except Exception as exc:
        detail = str(exc).splitlines()[0][:100] if str(exc) else ""
        return [Result(name, FAIL, f"{type(exc).__name__} {detail}".strip())]
    return [Result(name, OK, f"{settings.ssh.data_dir} lisible")]


def check_files(settings: Settings) -> list[Result]:
    llama = settings.llama
    results = []
    for label, path in (
        ("llama-server", llama.server_exe),
        ("modèle", llama.model),
        ("mmproj", llama.mmproj),
    ):
        exists = Path(path).is_file()
        results.append(
            Result(
                f"file:{label}",
                OK if exists else FAIL,
                str(path) if exists else f"introuvable : {path}",
            )
        )
    return results


def check_ffmpeg(settings: Settings) -> list[Result]:
    results = []
    for tool in ("ffmpeg", "ffprobe"):
        path = shutil.which(tool)
        results.append(Result(f"bin:{tool}", OK if path else FAIL, path or "absent du PATH"))
    return results


def check_llama_health(
    settings: Settings, transport: httpx.BaseTransport | None = None
) -> list[Result]:
    name = "llama-health"
    url = f"http://127.0.0.1:{settings.llama.port}/health"
    try:
        with httpx.Client(timeout=3, transport=transport) as client:
            resp = client.get(url)
    except httpx.HTTPError:
        return [Result(name, SKIP, "llama-server non démarré")]
    if resp.status_code == 200:
        return [Result(name, OK, "serveur prêt")]
    return [Result(name, FAIL, f"statut HTTP {resp.status_code}")]


CHECKS: dict[str, Callable[[Settings], list[Result]]] = {
    "credentials": check_credentials,
    "webdav": check_webdav,
    "ssh": check_ssh,
    "files": check_files,
    "ffmpeg": check_ffmpeg,
    "llama": check_llama_health,
}


def run_checks(settings: Settings, only: list[str] | None = None) -> list[Result]:
    results = []
    for name, fn in CHECKS.items():
        if only and name not in only:
            continue
        results.extend(fn(settings))
    return results
