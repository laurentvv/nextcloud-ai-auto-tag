import io
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PIL import Image

from nc_autotag.media import quality
from nc_autotag.media.images import to_jpeg
from nc_autotag.state import HashRow

REASON = "doublon"
EXIF_IFD = 0x8769
EXIF_DATETIME_ORIGINAL = 0x9003
NAME_TIME = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})[_-]?(\d{2})(\d{2})(\d{2})(?!\d)")


@dataclass(frozen=True)
class Signature:
    dhash: int
    sharpness: float
    pixels: int
    taken_at: str | None


@dataclass
class DupGroup:
    keeper: HashRow
    flagged: list[HashRow]


def dhash(source: bytes | Image.Image) -> int:
    """dHash 64 bits : gris 9x8, bit = pixel plus clair que son voisin de droite."""
    im = Image.open(io.BytesIO(source)) if isinstance(source, bytes) else source
    px = im.convert("L").resize((9, 8), Image.Resampling.LANCZOS).tobytes()
    bits = 0
    for y in range(8):
        for x in range(8):
            bits = (bits << 1) | (px[y * 9 + x] > px[y * 9 + x + 1])
    return bits


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def time_from_filename(name: str) -> datetime | None:
    """IMG_YYYYMMDD_HHMMSS, VID_..., YYYYMMDD_HHMMSS, Screenshot_YYYYMMDD-HHMMSS, YYYYMMDDHHMMSS."""
    m = NAME_TIME.search(name)
    if not m:
        return None
    try:
        return datetime(*map(int, m.groups()))
    except ValueError:
        return None


def time_from_exif(path: Path | str) -> datetime | None:
    try:
        with Image.open(path) as im:
            raw = im.getexif().get_ifd(EXIF_IFD).get(EXIF_DATETIME_ORIGINAL)
        return datetime.strptime(str(raw).strip(), "%Y:%m:%d %H:%M:%S") if raw else None
    except Exception:
        return None


def taken_at(path: Path | str, use_exif: bool = True) -> str | None:
    """EXIF DateTimeOriginal, sinon date du nom de fichier, sinon None."""
    when = (time_from_exif(path) if use_exif else None) or time_from_filename(Path(path).name)
    return when.isoformat() if when else None


def image_signature(
    source: Path | str | bytes, jpeg: bytes | None = None, name: str | None = None
) -> Signature:
    """Signature d'une image (chemin ou octets ; avec des octets, `name` sert à la date)."""
    raw = io.BytesIO(source) if isinstance(source, bytes) else source
    jpeg = jpeg or to_jpeg(source if isinstance(source, bytes) else str(source))
    with Image.open(raw) as im:
        pixels = im.width * im.height
    if isinstance(raw, io.BytesIO):
        raw.seek(0)
    name = name or Path(str(source)).name
    when = time_from_exif(raw) or time_from_filename(name)
    return Signature(
        dhash(jpeg), quality.analyze(jpeg).sharpness, pixels, when.isoformat() if when else None
    )


def video_signature(
    path: Path | str, middle_frame: bytes, pixels: int, name: str | None = None
) -> Signature:
    when = time_from_filename(name or Path(path).name)
    return Signature(
        dhash(middle_frame),
        quality.analyze(middle_frame).sharpness,
        pixels,
        when.isoformat() if when else None,
    )


def _score(r: HashRow) -> float:
    return (r.sharpness or 0.0) * (r.pixels or 0)


def _when(r: HashRow) -> datetime | None:
    return datetime.fromisoformat(r.taken_at) if r.taken_at else None


def _same_shot(
    a: HashRow, b: HashRow, max_hamming: int, window_minutes: float, max_duration_diff: float
) -> bool:
    if a.user != b.user or a.kind != b.kind or hamming(a.dhash, b.dhash) > max_hamming:
        return False
    if a.kind == "video" and abs((a.duration or 0.0) - (b.duration or 0.0)) > max_duration_diff:
        return False
    ta, tb = _when(a), _when(b)
    if ta and tb:
        return abs((ta - tb).total_seconds()) <= window_minutes * 60
    return a.folder == b.folder  # date inconnue : même dossier et même empreinte seulement


def find_duplicate_groups(
    rows: list[HashRow],
    max_hamming: int = 4,
    window_minutes: float = 30,
    max_duration_diff: float = 0.5,
) -> list[DupGroup]:
    """Groupes de quasi-doublons ; le meilleur (netteté x pixels, à égalité le plus ancien) reste.

    Chaque fichier signalé est proche du gardé lui-même (pas de chaînage A~B~C).
    Les empreintes à zéro (image unie) sont ignorées : tous les aplats se ressemblent.
    """
    far = datetime.max

    def best_first(r: HashRow):
        return (-_score(r), _when(r) or far, r.fileid)

    pending = sorted((r for r in rows if r.dhash), key=best_first)
    groups = []
    while pending:
        keeper = pending.pop(0)
        same = [
            r for r in pending
            if _same_shot(keeper, r, max_hamming, window_minutes, max_duration_diff)
        ]  # fmt: skip
        if same:
            groups.append(DupGroup(keeper, same))
            pending = [r for r in pending if r not in same]
    return groups


def duplicate_flags(groups: list[DupGroup]) -> dict[tuple[str, int], int]:
    """(user, fileid) à supprimer (raison « doublon ») -> fileid du fichier gardé."""
    return {(r.user, r.fileid): g.keeper.fileid for g in groups for r in g.flagged}
