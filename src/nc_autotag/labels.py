import csv
import json
from dataclasses import dataclass
from pathlib import Path

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif", ".gif", ".bmp"}
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".3gp", ".webm"}
SEP = ";"
NO_TAG = "-"
FIELDS = ["path", "predicted_tags", "true_tags", "reasons"]


@dataclass(frozen=True)
class TagStats:
    tag: str
    tp: int
    fp: int
    fn: int
    threshold: float
    best_threshold: float | None
    best_f1: float | None

    @property
    def precision(self) -> float | None:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else None

    @property
    def recall(self) -> float | None:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else None


def scan_media(directory: Path) -> list[Path]:
    exts = IMAGE_EXT | VIDEO_EXT
    return sorted(p for p in directory.rglob("*") if p.is_file() and p.suffix.lower() in exts)


def scores_path(csv_path: Path) -> Path:
    return csv_path.with_suffix(".scores.json")


def parse_tags(cell: str) -> set[str]:
    tags = {t.strip().lower() for t in cell.split(SEP)}
    return tags - {"", NO_TAG}


Item = tuple[str, dict[str, float], list[str], list[str]]


def write_labels(csv_path: Path, items: list[Item]) -> None:
    """items : (chemin, scores, tags Nextcloud retenus, raisons de suppression)."""
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for path, _, selected, reasons in items:
            w.writerow([path, SEP.join(selected), "", SEP.join(reasons)])
    scores = {path: s for path, s, _, _ in items}
    scores_path(csv_path).write_text(
        json.dumps(scores, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def read_labels(csv_path: Path) -> list[tuple[str, set[str], set[str], bool]]:
    """(chemin, prédits, vrais, étiqueté) ; une ligne sans true_tags n'est pas étiquetée."""
    with csv_path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    return [
        (
            r["path"],
            parse_tags(r["predicted_tags"]),
            parse_tags(r["true_tags"]),
            bool(r["true_tags"].strip()),
        )
        for r in rows
    ]


def _f1(tp: int, fp: int, fn: int) -> float:
    return 2 * tp / (2 * tp + fp + fn) if tp else 0.0


def best_threshold(
    tag: str, scores: list[dict[str, float]], truths: list[set[str]]
) -> tuple[float, float] | None:
    """Seuil maximisant F1 (à égalité, le plus haut) ; None sans exemple positif."""
    values = [s.get(tag, 0.0) for s in scores]
    positives = [tag in t for t in truths]
    if not any(positives):
        return None
    best: tuple[float, float] | None = None
    for thr in sorted({v for v in values if v > 0}):
        pred = [v >= thr for v in values]
        tp = sum(p and y for p, y in zip(pred, positives, strict=True))
        fp = sum(p and not y for p, y in zip(pred, positives, strict=True))
        fn = sum(not p and y for p, y in zip(pred, positives, strict=True))
        f1 = _f1(tp, fp, fn)
        if best is None or f1 >= best[1]:
            best = (thr, f1)
    return best


def evaluate(
    rows: list[tuple[str, set[str], set[str], bool]],
    scores: dict[str, dict[str, float]],
    thresholds: dict[str, float],
) -> list[TagStats]:
    labelled = [r for r in rows if r[3]]
    tags = sorted({t for r in labelled for t in r[1] | r[2]})
    stats = []
    for tag in tags:
        tp = sum(tag in pred and tag in true for _, pred, true, _ in labelled)
        fp = sum(tag in pred and tag not in true for _, pred, true, _ in labelled)
        fn = sum(tag not in pred and tag in true for _, pred, true, _ in labelled)
        best = best_threshold(
            tag, [scores.get(r[0], {}) for r in labelled], [r[2] for r in labelled]
        )
        stats.append(
            TagStats(
                tag,
                tp,
                fp,
                fn,
                thresholds.get(tag, 0.5),
                best[0] if best else None,
                best[1] if best else None,
            )
        )
    return stats
