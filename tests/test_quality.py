from PIL import Image

from nc_autotag.media.quality import DEFAULT_BLUR_VAR, analyze, is_black
from synth import black_image, blurred, sharp_image


def test_nette_contre_floue():
    sharp = analyze(sharp_image(0)).sharpness
    soft = analyze(blurred(sharp_image(0), 8)).sharpness
    assert soft < DEFAULT_BLUR_VAR < sharp
    assert sharp > 20 * soft


def test_image_unie_a_nettete_nulle():
    assert analyze(Image.new("RGB", (300, 200), (90, 90, 90))).sharpness == 0.0


def test_noir():
    assert is_black(black_image())
    assert analyze(black_image()).black
    assert not is_black(Image.new("RGB", (100, 100), (40, 40, 40)))
    assert not analyze(sharp_image(0)).black


def test_noir_par_part_de_pixels():
    im = Image.new("L", (100, 100), 15)
    im.paste(255, (0, 0, 100, 1))  # 1 % de pixels clairs : moyenne >= 12 mais 99 % < 20
    assert is_black(im)
    im.paste(255, (0, 0, 100, 3))
    assert not is_black(im)


def test_grande_image_reduite():
    q = analyze(sharp_image(0, (4000, 3000)))
    assert q.sharpness > DEFAULT_BLUR_VAR
