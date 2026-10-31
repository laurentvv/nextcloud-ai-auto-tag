from pathlib import Path
from types import SimpleNamespace

import pytest

from nc_autotag.config import SshConfig
from nc_autotag.media.ssh import MB, SftpSource, SkipFile, remote_path

CFG = SshConfig(data_dir="/data", max_video_mb=1, max_image_mb=1, mounts={"/Local": "/mnt/photos"})


class FakeFile:
    def __init__(self, data: bytes):
        self.data = data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    async def read(self) -> bytes:
        return self.data


class FakeSftp:
    def __init__(self, files: dict[str, bytes]):
        self.files = files
        self.calls: list[str] = []

    async def stat(self, path: str):
        self.calls.append(f"stat {path}")
        if path not in self.files:
            raise FileNotFoundError(path)
        return SimpleNamespace(size=len(self.files[path]))

    def open(self, path: str, mode: str = "rb"):
        assert mode == "rb"
        self.calls.append(f"open {path}")
        return FakeFile(self.files[path])

    async def get(self, remote: str, local: str) -> None:
        self.calls.append(f"get {remote}")
        Path(local).write_bytes(self.files[remote])


def test_remote_path():
    assert remote_path(CFG, "alice", "/Photos/a b.jpg") == "/data/alice/files/Photos/a b.jpg"
    assert remote_path(CFG, "alice", "/Local/2020/x.jpg") == "/mnt/photos/2020/x.jpg"
    assert (
        remote_path(CFG, "alice", "/Localement/x.jpg") == "/data/alice/files/Localement/x.jpg"
    )
    assert remote_path(CFG, "alice", "/../../etc/passwd") == "/data/alice/files/etc/passwd"
    with pytest.raises(SkipFile):
        remote_path(CFG, "alice", "/")


async def test_fetch_image_in_memory_and_read_only():
    sftp = FakeSftp({"/data/alice/files/a.jpg": b"jpegdata"})
    async with SftpSource(CFG, sftp=sftp) as src:
        got = await src.fetch("alice", "/a.jpg", is_video=False)
    assert got.data == b"jpegdata" and got.path is None
    assert [c.split()[0] for c in sftp.calls] == ["stat", "open"]


async def test_missing_file_is_skipped():
    async with SftpSource(CFG, sftp=FakeSftp({})) as src:
        with pytest.raises(SkipFile, match="absent"):
            await src.fetch("alice", "/Shared/x.jpg", is_video=False)


async def test_too_big_is_skipped():
    sftp = FakeSftp({"/data/u/files/v.mp4": b"x" * (MB + 1)})
    async with SftpSource(CFG, sftp=sftp) as src:
        with pytest.raises(SkipFile, match="trop gros"):
            await src.fetch("u", "/v.mp4", is_video=True)
    assert [c.split()[0] for c in sftp.calls] == ["stat"]


async def test_video_goes_to_temp_file_and_is_cleaned_up():
    sftp = FakeSftp({"/data/u/files/Vidéos/Noël: 1.mp4": b"video"})
    async with SftpSource(CFG, sftp=sftp) as src:
        got = await src.fetch("u", "/Vidéos/Noël: 1.mp4", is_video=True)
        assert got.data is None and got.path.read_bytes() == b"video"
        assert got.path.name == "Noël_ 1.mp4"
        folder = got.path.parent
        got.cleanup()
    assert not folder.exists()
