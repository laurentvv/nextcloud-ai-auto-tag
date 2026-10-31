import shutil
import subprocess
from pathlib import Path

import httpx
import pytest

from nc_autotag.inference.d1 import D1Client
from nc_autotag.media.video import extract_frames, probe_duration, probe_pixels
from test_d1 import REAL, quality_handler

pytestmark = pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg absent"
)


def make_video(path: Path, seconds: float) -> Path:
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"testsrc=duration={seconds}:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", str(path)],
        check=True,
    )  # fmt: skip
    return path


async def test_video_courte_proposee_a_supprimer(tmp_path):
    video = make_video(tmp_path / "court.mp4", 0.5)
    duration = probe_duration(video)
    assert duration < 1.0
    assert probe_pixels(video) == 320 * 240
    frames = extract_frames(video, k=3, duration=duration)
    client = D1Client(REAL, transport=httpx.MockTransport(quality_handler()))
    res = await client.tag_video(frames, duration)
    await client.aclose()
    assert res.delete and res.reasons == ["accident"] and res.emitted == ["à supprimer"]


async def test_video_normale_conservee(tmp_path):
    video = make_video(tmp_path / "normal.mp4", 3)
    duration = probe_duration(video)
    frames = extract_frames(video, k=3, duration=duration)
    client = D1Client(REAL, transport=httpx.MockTransport(quality_handler()))
    res = await client.tag_video(frames, duration)
    await client.aclose()
    assert not res.delete
