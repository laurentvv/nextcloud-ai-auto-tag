import csv
import json

from typer.testing import CliRunner

from nc_autotag import labels
from nc_autotag.cli import app


def test_parse_tags():
    assert labels.parse_tags(" Plage;ville ;; ") == {"plage", "ville"}
    assert labels.parse_tags("-") == set()


def test_scan_media(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ["a.jpg", "sub/b.HEIC", "c.mp4", "d.txt"]:
        (tmp_path / name).write_bytes(b"x")
    found = sorted(p.name for p in labels.scan_media(tmp_path))
    assert found == ["a.jpg", "b.HEIC", "c.mp4"]


def test_roundtrip_and_stats(tmp_path):
    out = tmp_path / "labels.csv"
    items = [
        ("1.jpg", {"plage": 0.9, "ville": 0.2}, ["plage"], []),
        ("2.jpg", {"plage": 0.7, "ville": 0.1}, ["plage"], []),
        ("3.jpg", {"plage": 0.4, "ville": 0.8}, ["ville"], []),
        ("4.jpg", {"plage": 0.1, "ville": 0.1}, [], ["floue"]),
    ]
    labels.write_labels(out, items)
    assert labels.scores_path(out).name == "labels.scores.json"
    scores = json.loads(labels.scores_path(out).read_text(encoding="utf-8"))
    assert scores["1.jpg"]["plage"] == 0.9
    truths = {"1.jpg": "plage", "2.jpg": "plage", "3.jpg": "plage;ville", "4.jpg": ""}
    with out.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    for r in rows:
        r["true_tags"] = truths[r["path"]]
    with out.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=labels.FIELDS)
        w.writeheader()
        w.writerows(rows)
    parsed = labels.read_labels(out)
    assert [r[3] for r in parsed] == [True, True, True, False]
    stats = {s.tag: s for s in labels.evaluate(parsed, scores, {"plage": 0.5, "ville": 0.5})}
    plage = stats["plage"]
    assert (plage.tp, plage.fp, plage.fn) == (2, 0, 1)
    assert plage.precision == 1.0 and abs(plage.recall - 2 / 3) < 1e-9
    assert (plage.best_threshold, plage.best_f1) == (0.4, 1.0)
    ville = stats["ville"]
    assert (ville.tp, ville.fp, ville.fn) == (1, 0, 0)
    assert (ville.best_threshold, ville.best_f1) == (0.8, 1.0)


def test_best_threshold_without_positive():
    assert labels.best_threshold("x", [{"x": 0.9}], [set()]) is None


def test_eval_cli(tmp_path):
    out = tmp_path / "l.csv"
    labels.write_labels(out, [("a.jpg", {"plage": 0.9}, ["plage"], [])])
    out.write_text("path,predicted_tags,true_tags\na.jpg,plage,plage\n", encoding="utf-8")
    res = CliRunner().invoke(app, ["eval", str(out)])
    assert res.exit_code == 0, res.output
    assert "plage" in res.output and "1 fichiers" in res.output
