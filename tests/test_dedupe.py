from datetime import datetime

from typer.testing import CliRunner

from nc_autotag.cli import app
from nc_autotag.media import dedupe
from nc_autotag.state import HashRow
from synth import jpeg, sharp_image


def row(fileid, dhash=0xF0F0F0F0F0F0F0F0, sharp=100.0, pixels=1000, at="2026-01-01T10:00:00", **kw):
    base = {"user": "u", "kind": "image", "duration": None, "folder": "/d"}
    base.update(kw)
    return HashRow(fileid=fileid, dhash=dhash, sharpness=sharp, pixels=pixels, taken_at=at, **base)


def test_dhash_stable_malgre_la_compression():
    im = sharp_image(5)
    a, b = dedupe.dhash(jpeg(im, 95)), dedupe.dhash(jpeg(im, 30))
    assert dedupe.hamming(a, b) <= 4
    assert dedupe.hamming(a, dedupe.dhash(jpeg(sharp_image(6)))) > 10


def test_dates_du_nom_de_fichier():
    f = dedupe.time_from_filename
    assert f("IMG_20250328_095902_edit_1.jpg") == datetime(2025, 3, 28, 9, 59, 2)
    assert f("VID_20250404_194658.mp4") == datetime(2025, 4, 4, 19, 46, 58)
    assert f("20231130_172937.jpg") == datetime(2023, 11, 30, 17, 29, 37)
    assert f("20180812125445.mp4") == datetime(2018, 8, 12, 12, 54, 45)
    assert f("Screenshot_20210619-182220.jpg") == datetime(2021, 6, 19, 18, 22, 20)
    assert f("photo.jpg") is None and f("IMG_20251340_000000.jpg") is None


def test_exif_prioritaire_sur_le_nom(tmp_path):
    from PIL import Image

    exif = Image.Exif()
    exif.get_ifd(0x8769)[0x9003] = "2020:05:06 07:08:09"
    path = tmp_path / "IMG_20250101_000000.jpg"
    sharp_image(1, (64, 64)).save(path, exif=exif)
    assert dedupe.taken_at(path) == "2020-05-06T07:08:09"
    other = tmp_path / "IMG_20250101_000000_b.jpg"
    sharp_image(1, (64, 64)).save(other)
    assert dedupe.taken_at(other) == "2025-01-01T00:00:00"


def test_groupe_garde_le_meilleur_puis_le_plus_ancien():
    rows = [
        row(1, sharp=50),
        row(2, sharp=200, at="2026-01-01T10:10:00"),
        row(3, sharp=200, at="2026-01-01T10:05:00"),
        row(4, dhash=0x0F0F0F0F0F0F0F0F),
    ]
    (g,) = dedupe.find_duplicate_groups(rows)
    assert g.keeper.fileid == 3
    assert sorted(r.fileid for r in g.flagged) == [1, 2]
    assert dedupe.duplicate_flags([g]) == {("u", 1): 3, ("u", 2): 3}


def test_pixels_comptent():
    (g,) = dedupe.find_duplicate_groups([row(1, pixels=1000), row(2, pixels=4000)])
    assert g.keeper.fileid == 2


def test_fenetre_de_temps_et_distance():
    far = row(2, at="2026-01-01T12:00:00")
    assert not dedupe.find_duplicate_groups([row(1), far])
    assert dedupe.find_duplicate_groups([row(1), far], window_minutes=180)
    near_bits = row(3, dhash=0xF0F0F0F0F0F0F0F0 ^ 0b1111)
    assert dedupe.find_duplicate_groups([row(1), near_bits])
    assert not dedupe.find_duplicate_groups([row(1), row(4, dhash=0xF0F0F0F0F0F0F0F0 ^ 0b11111)])


def test_sans_date_meme_dossier_seulement():
    a, b = row(1, at=None), row(2, at=None)
    assert dedupe.find_duplicate_groups([a, b])
    assert not dedupe.find_duplicate_groups([a, row(3, at=None, folder="/autre")])
    assert not dedupe.find_duplicate_groups([a, row(4, at=None, folder="/autre")])


def test_videos_duree_et_type():
    v1 = row(1, kind="video", duration=10.0)
    assert dedupe.find_duplicate_groups([v1, row(2, kind="video", duration=10.4)])
    assert not dedupe.find_duplicate_groups([v1, row(3, kind="video", duration=11.0)])
    assert not dedupe.find_duplicate_groups([v1, row(4, kind="image")])


def test_aplats_et_autres_utilisateurs_ignores():
    assert not dedupe.find_duplicate_groups([row(1, dhash=0), row(2, dhash=0)])
    assert not dedupe.find_duplicate_groups([row(1), row(2, user="v")])


def test_pas_de_chainage():
    base = 0xF0F0F0F0F0F0F0F0
    a, b, c = row(1, sharp=300), row(2, dhash=base ^ 0b1111), row(3, dhash=base ^ 0b11110000)
    (g,) = dedupe.find_duplicate_groups([a, b, c])
    assert g.keeper.fileid == 1 and sorted(r.fileid for r in g.flagged) == [2, 3]
    c_far = row(4, sharp=10, dhash=base ^ 0b11111111)  # 8 bits de a, 4 de b
    (g,) = dedupe.find_duplicate_groups([a, b, c_far])
    assert [r.fileid for r in g.flagged] == [2]


def test_cli_duplicates(tmp_path):
    im = sharp_image(7)
    (tmp_path / "IMG_20260101_100000.jpg").write_bytes(jpeg(im, 95))
    (tmp_path / "IMG_20260101_100100.jpg").write_bytes(jpeg(im, 25))
    (tmp_path / "IMG_20260101_100200.jpg").write_bytes(jpeg(sharp_image(8)))
    res = CliRunner().invoke(app, ["duplicates", str(tmp_path)])
    assert res.exit_code == 0, res.output
    assert "1 groupes, 1 à supprimer" in res.output
    assert "doublon" in res.output
