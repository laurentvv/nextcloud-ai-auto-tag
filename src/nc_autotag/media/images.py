import io

import pillow_heif
from PIL import Image, ImageOps

pillow_heif.register_heif_opener()


def to_jpeg(source: bytes | str, max_side: int = 768, quality: int = 85) -> bytes:
    """Charge une image (octets ou chemin), applique l'orientation EXIF, redimensionne, JPEG."""
    with Image.open(io.BytesIO(source) if isinstance(source, bytes) else source) as im:
        im = ImageOps.exif_transpose(im)
        im = im.convert("RGB")
        im.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
        out = io.BytesIO()
        im.save(out, "JPEG", quality=quality)
        return out.getvalue()
