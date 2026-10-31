import io
import shutil
import subprocess

import pytest
from PIL import Image

from nc_autotag.media.images import to_jpeg
from nc_autotag.media.video import extract_frames, frame_times


def _size(data: bytes) -> tuple[int, int]:
    im = Image.open(io.BytesIO(data))
    assert im.format == "JPEG"
    return im.size


def _encode(im: Image.Image, fmt: str, **kw) -> bytes:
    buf = io.BytesIO()
    im.save(buf, fmt, **kw)
    return buf.getvalue()


def test_large_image_is_resized():
    data = _encode(Image.new("RGB", (4000, 2000), "red"), "PNG")
    assert _size(to_jpeg(data, 768)) == (768, 384)


def test_small_image_not_upscaled():
    assert _size(to_jpeg(_encode(Image.new("RGB", (200, 100)), "JPEG"), 768)) == (200, 100)


def test_exif_orientation_applied():
    im = Image.new("RGB", (400, 200), "blue")
    exif = Image.Exif()
    exif[0x0112] = 6  # rotation 90 degrés
    data = _encode(im, "JPEG", exif=exif)
    assert _size(to_jpeg(data, 768)) == (200, 400)


def test_rgba_and_webp():
    data = _encode(Image.new("RGBA", (300, 300), (0, 255, 0, 128)), "WEBP")
    assert _size(to_jpeg(data)) == (300, 300)


def test_heic():
    im = Image.new("RGB", (1000, 500), "green")
    try:
        data = _encode(im, "HEIF")
    except Exception as e:
        pytest.skip(f"encodeur HEIC indisponible : {e}")
    assert _size(to_jpeg(data, 768)) == (768, 384)


def test_frame_times_avoid_edges():
    ts = frame_times(100.0, 6)
    assert len(ts) == 6
    assert ts == sorted(ts)
    assert ts[0] > 5 and ts[-1] < 95


@pytest.mark.skipif(
    not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="ffmpeg absent"
)
def test_extract_frames(tmp_path):
    video = tmp_path / "t.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=3:size=1280x720:rate=10",
         "-pix_fmt", "yuv420p", str(video)],
        check=True,
    )  # fmt: skip
    frames = extract_frames(video, k=4, max_side=512)
    assert len(frames) == 4
    assert all(_size(f) == (512, 288) for f in frames)
    assert len(set(frames)) > 1
