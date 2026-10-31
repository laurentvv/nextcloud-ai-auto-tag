import tomllib
from pathlib import Path

from pydantic import BaseModel, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from nc_autotag.media.quality import DEFAULT_BLUR_VAR
from nc_autotag.nextcloud.blackout import parse_windows
from nc_autotag.taxonomy import DELETE_TAG

DEFAULT_CONFIG = Path("config/settings.toml")


class NextcloudConfig(BaseModel):
    base_url: str = "https://cloud.example.com"
    users: list[str] = ["alice", "bob"]
    concurrency: int = 4
    search_limit: int = 200000
    blackout: list[str] = ["03:00-05:30"]

    @field_validator("blackout")
    @classmethod
    def _check_blackout(cls, value: list[str]) -> list[str]:
        parse_windows(value)
        return value


class SshConfig(BaseModel):
    host: str = "192.168.1.10"
    user: str = "root"
    key_file: Path = Path("~/.ssh/nc_autotag_ed25519")
    data_dir: str = "/var/www/html/nextcloud/data"
    max_video_mb: int = 300
    max_image_mb: int = 100
    # Stockages externes : point de montage Nextcloud -> dossier sur le serveur.
    mounts: dict[str, str] = {}


class LlamaConfig(BaseModel):
    server_exe: Path = Path(r"C:\llama.cpp\llama-server.exe")
    model: Path = Path(r"D:\models\d1-Omni-2B-Q8_0.gguf")
    mmproj: Path = Path(r"D:\models\mmproj-d1-Omni-2B-Q8_0.gguf")
    port: int = 8080
    ub: int = 8192
    image_max_side: int = 768


class QualityConfig(BaseModel):
    blur_var_threshold: float = DEFAULT_BLUR_VAR
    dup_max_hamming: int = 4
    dup_window_minutes: int = 30


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore")

    delete_tag_name: str = DELETE_TAG
    max_tags_per_file: int = 8
    quality: QualityConfig = QualityConfig()
    nextcloud: NextcloudConfig = NextcloudConfig()
    ssh: SshConfig = SshConfig()
    llama: LlamaConfig = LlamaConfig()
    state_db: Path = Path("state.db")


def load_settings(path: Path | None = None) -> Settings:
    toml_path = path or DEFAULT_CONFIG
    data = {}
    if toml_path.exists():
        with toml_path.open("rb") as f:
            data = tomllib.load(f)
    return Settings(**data)
