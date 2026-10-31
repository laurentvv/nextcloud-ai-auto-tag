import io
from dataclasses import dataclass

from PIL import Image, ImageFilter

ANALYSIS_SIDE = 512
BLACK_MEAN = 12
BLACK_PIXEL = 20
BLACK_SHARE = 0.99
DEFAULT_BLUR_VAR = 30.0

# Le noyau Pillow est borné à [0, 255] : on filtre la partie positive et la partie négative.
_LAP = [0, 1, 0, 1, -4, 1, 0, 1, 0]
_LAP_POS = ImageFilter.Kernel((3, 3), _LAP, scale=1)
_LAP_NEG = ImageFilter.Kernel((3, 3), [-x for x in _LAP], scale=1)


@dataclass(frozen=True)
class Quality:
    sharpness: float
    luminance: float
    black: bool


def to_gray(source: bytes | Image.Image) -> Image.Image:
    """Niveaux de gris, réduit à 512 px max (copie)."""
    im = Image.open(io.BytesIO(source)) if isinstance(source, bytes) else source
    gray = im.convert("L")
    gray.thumbnail((ANALYSIS_SIDE, ANALYSIS_SIDE), Image.Resampling.LANCZOS)
    return gray


def _moments(gray: Image.Image) -> tuple[float, float]:
    h = gray.histogram()
    n = sum(h)
    return sum(i * c for i, c in enumerate(h)) / n, sum(i * i * c for i, c in enumerate(h)) / n


def laplacian_variance(gray: Image.Image) -> float:
    """Netteté : variance du Laplacien (bord d'1 px exclu, Pillow ne le filtre pas)."""
    w, h = gray.size
    if w < 3 or h < 3:
        return 0.0
    box = (1, 1, w - 1, h - 1)
    mp, qp = _moments(gray.filter(_LAP_POS).crop(box))
    mn, qn = _moments(gray.filter(_LAP_NEG).crop(box))
    mean = mp - mn
    return qp + qn - mean * mean


def mean_luminance(gray: Image.Image) -> float:
    return _moments(gray)[0]


def is_black(img: Image.Image) -> bool:
    """Image noire ou presque : luminance moyenne < 12, ou 99 % des pixels < 20."""
    gray = img.convert("L")
    h = gray.histogram()
    return mean_luminance(gray) < BLACK_MEAN or sum(h[:BLACK_PIXEL]) / sum(h) >= BLACK_SHARE


def analyze(source: bytes | Image.Image) -> Quality:
    gray = to_gray(source)
    return Quality(laplacian_variance(gray), mean_luminance(gray), is_black(gray))
