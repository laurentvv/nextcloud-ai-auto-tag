import subprocess
from pathlib import Path

from nc_autotag.media.images import to_jpeg

MARGIN = 0.05


def probe_duration(path: Path | str) -> float:
    cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(path),
    ]  # fmt: skip
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    return float(out.stdout.strip())


def frame_times(duration: float, k: int) -> list[float]:
    """k instants uniformes, centrés dans la plage [5 %, 95 %] de la durée."""
    lo, hi = duration * MARGIN, duration * (1 - MARGIN)
    return [lo + (hi - lo) * (i + 0.5) / k for i in range(k)]


def extract_frames(
    path: Path | str, k: int = 6, max_side: int = 768, duration: float | None = None
) -> list[bytes]:
    duration = probe_duration(path) if duration is None else duration
    frames = []
    for t in frame_times(duration, k):
        cmd = [
            "ffmpeg", "-v", "error", "-ss", f"{t:.3f}", "-i", str(path),
            "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "2", "-",
        ]  # fmt: skip
        raw = subprocess.run(cmd, capture_output=True, check=True, timeout=120).stdout
        if raw:
            frames.append(to_jpeg(raw, max_side))
    if not frames:
        raise RuntimeError(f"Aucune image extraite de {path}")
    return frames


def probe_pixels(path: Path | str) -> int:
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "stream=width,height", "-of", "csv=p=0:s=x", str(path),
    ]  # fmt: skip
    out = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
    w, h = out.stdout.strip().split("x")[:2]
    return int(w) * int(h)
