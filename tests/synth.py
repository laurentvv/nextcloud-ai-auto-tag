import io
import random

from PIL import Image, ImageFilter


def sharp_image(seed: int = 0, size: tuple[int, int] = (640, 480)) -> Image.Image:
    """Patchs aléatoires à bords francs (netteté élevée), reproductibles."""
    rnd = random.Random(seed)
    small = Image.frombytes("RGB", (32, 24), rnd.randbytes(32 * 24 * 3))
    return small.resize(size, Image.Resampling.NEAREST)


def blurred(im: Image.Image, radius: float = 8) -> Image.Image:
    return im.filter(ImageFilter.GaussianBlur(radius))


def black_image(size: tuple[int, int] = (640, 480)) -> Image.Image:
    return Image.new("RGB", size, (2, 2, 2))


def jpeg(im: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()
