import asyncio
import base64
import time
from dataclasses import dataclass, field

import httpx

from nc_autotag.media.quality import DEFAULT_BLUR_VAR, Quality, analyze
from nc_autotag.taxonomy import DELETE_TAG, NONE_OPTION, Taxonomy

MIN_FRAMES_FOR_VOTE = 4
MIN_FRAMES_PASSING = 2
BLUR_TAG = "floue"
ACCIDENT_TAG = "accidentelle"
QUALITY_TAGS = (BLUR_TAG, ACCIDENT_TAG)
REASONS = {BLUR_TAG: "floue", ACCIDENT_TAG: "accident", "capture d'écran": "capture"}
MIN_VIDEO_SECONDS = 1.0


@dataclass
class TagResult:
    scores: dict[str, float]
    selected: dict[str, float]
    latency: float = 0.0
    frames: int = 1
    usage: list[dict] = field(default_factory=list)
    emitted: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)

    @property
    def delete(self) -> bool:
        return bool(self.reasons)


def make_result(
    taxonomy: Taxonomy,
    scores: dict[str, float],
    selected: dict[str, float],
    delete_tag: str = DELETE_TAG,
    extra_reasons: tuple[str, ...] = (),
    **kw,
) -> TagResult:
    """Ajoute les tags émis (emit/also) et les raisons de suppression."""
    reasons = [REASONS[t] for t in selected if t in REASONS]
    reasons = list(dict.fromkeys([*reasons, *extra_reasons]))
    emitted = taxonomy.emitted(selected, delete_tag)
    if reasons and delete_tag not in emitted:
        emitted.append(delete_tag)
    return TagResult(scores, selected, emitted=emitted, reasons=reasons, **kw)


def frame_flags(
    taxonomy: Taxonomy, scores: dict[str, float], q: Quality, blur_var: float
) -> tuple[bool, bool]:
    """(floue, accident) : floue exige d1 ET Laplacien ; une image noire suffit à l'accident."""
    names = {t.name for t in taxonomy.tags}
    blur = (
        BLUR_TAG in names
        and scores.get(BLUR_TAG, 0.0) >= taxonomy.threshold(BLUR_TAG)
        and q.sharpness < blur_var
    )
    accident = q.black or (
        ACCIDENT_TAG in names and scores.get(ACCIDENT_TAG, 0.0) >= taxonomy.threshold(ACCIDENT_TAG)
    )
    return blur, accident


def build_questions(taxonomy: Taxonomy, skip_gate: bool = False) -> dict[str, dict]:
    questions: dict[str, dict] = {}
    for c in taxonomy.categories:
        if skip_gate and c.gate:
            continue
        if c.kind == "choice":
            criteria = {t.name: t.description for t in c.tags}
            criteria[NONE_OPTION] = "none of the other options applies"
            questions[c.id] = {
                "type": "choice",
                "instructions": c.instructions,
                "criteria": criteria,
            }
        else:
            for t in c.tags:
                questions[t.name] = {
                    "type": "noul",
                    "instructions": f"Is the following true for this image: {t.description}?",
                }
    return questions


def parse_answers(taxonomy: Taxonomy, answers: dict) -> dict[str, float]:
    scores: dict[str, float] = {}
    for c in taxonomy.categories:
        if c.kind == "choice":
            probs = answers.get(c.id, {}).get("probabilities", {})
            for t in c.tags:
                scores[t.name] = float(probs.get(t.name, 0.0))
        else:
            for t in c.tags:
                scores[t.name] = float(answers.get(t.name, {}).get("noul", 0.0))
    return scores


def select_tags(taxonomy: Taxonomy, scores: dict[str, float]) -> dict[str, float]:
    return {t.name: scores[t.name] for t in taxonomy.tags if scores.get(t.name, 0) >= t.threshold}


def _mark(taxonomy: Taxonomy, selected: dict[str, float], extra: list[str], tag: str, score: float):
    if tag in {t.name for t in taxonomy.tags}:
        selected[tag] = score
    extra.append(REASONS[tag])


def apply_image_rules(
    taxonomy: Taxonomy,
    scores: dict[str, float],
    selected: dict[str, float],
    q: Quality,
    blur_var: float,
) -> tuple[dict[str, float], list[str]]:
    """Remplace floue/accidentelle de d1 par les règles croisées avec la mesure ; + raisons."""
    out = {k: v for k, v in selected.items() if k not in QUALITY_TAGS}
    extra: list[str] = []
    blur, accident = frame_flags(taxonomy, scores, q, blur_var)
    if blur:
        _mark(taxonomy, out, extra, BLUR_TAG, scores[BLUR_TAG])
    if accident:
        _mark(taxonomy, out, extra, ACCIDENT_TAG, 1.0 if q.black else scores[ACCIDENT_TAG])
    return out, extra


def aggregate_frames(
    taxonomy: Taxonomy,
    frames: list[dict[str, float]],
    flags: list[tuple[bool, bool]] | None = None,
    duration: float | None = None,
    delete_tag: str = DELETE_TAG,
) -> TagResult:
    """Score = max sur les images ; avec K >= 4, un tag exige >= 2 images au-dessus du seuil.

    Avec flags (floue, accident) par image : vidéo à supprimer si >= 2/3 des images sont
    signalées, ou si la durée est < 1 s. Sans flags, floue/accidentelle suivent le vote.
    """
    need = MIN_FRAMES_PASSING if len(frames) >= MIN_FRAMES_FOR_VOTE else 1
    scores, selected = {}, {}
    for t in taxonomy.tags:
        values = [f.get(t.name, 0.0) for f in frames]
        scores[t.name] = max(values)
        if flags is not None and t.name in QUALITY_TAGS:
            continue
        if sum(v >= t.threshold for v in values) >= need:
            selected[t.name] = scores[t.name]
    extra: list[str] = []
    if flags is not None:
        if sum(b or a for b, a in flags) * 3 >= len(flags) * 2:
            if any(b for b, _ in flags):
                _mark(taxonomy, selected, extra, BLUR_TAG, scores.get(BLUR_TAG, 0.0))
            if any(a for _, a in flags):
                _mark(taxonomy, selected, extra, ACCIDENT_TAG, 1.0)
        if duration is not None and duration < MIN_VIDEO_SECONDS:
            _mark(taxonomy, selected, extra, ACCIDENT_TAG, 1.0)
    return make_result(taxonomy, scores, selected, delete_tag, tuple(extra), frames=len(frames))


class D1Client:
    def __init__(
        self,
        taxonomy: Taxonomy,
        port: int = 8080,
        concurrency: int = 4,
        timeout: float = 120.0,
        transport: httpx.AsyncBaseTransport | None = None,
        blur_var_threshold: float = DEFAULT_BLUR_VAR,
        delete_tag: str = DELETE_TAG,
    ):
        self.taxonomy = taxonomy
        self.blur_var = blur_var_threshold
        self.delete_tag = delete_tag
        self.questions = build_questions(taxonomy)
        self.gate = taxonomy.gate
        gate_id = self.gate.id if self.gate else None
        self.gate_questions = {k: v for k, v in self.questions.items() if k == gate_id}
        self.rest_questions = build_questions(taxonomy, skip_gate=True)
        self.url = f"http://127.0.0.1:{port}/v1/systemone"
        self.sem = asyncio.Semaphore(concurrency)
        self.http = httpx.AsyncClient(timeout=timeout, transport=transport)

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _ask(
        self, jpeg: bytes, questions: dict[str, dict] | None = None
    ) -> tuple[dict[str, float], dict]:
        b64 = base64.b64encode(jpeg).decode()
        payload = {
            "state": None,
            "images": [f"data:image/jpeg;base64,{b64}"],
            "questions": questions or self.questions,
        }
        async with self.sem:
            r = await self.http.post(self.url, json=payload)
        r.raise_for_status()
        body = r.json()
        return parse_answers(self.taxonomy, body["answers"]), body.get("usage", {})

    async def tag_image(self, jpeg: bytes) -> TagResult:
        t0 = time.perf_counter()
        usages = []
        if self.gate:
            scores, usage = await self._ask(jpeg, self.gate_questions)
            usages.append(usage)
            winner = max(self.gate.tags, key=lambda t: scores[t.name])
            if winner.name in self.gate.exclusive and scores[winner.name] >= winner.threshold:
                return make_result(
                    self.taxonomy,
                    scores,
                    {winner.name: scores[winner.name]},
                    self.delete_tag,
                    latency=time.perf_counter() - t0,
                    usage=usages,
                )
            rest, usage = await self._ask(jpeg, self.rest_questions)
            usages.append(usage)
            gate_names = {t.name for t in self.gate.tags}
            scores = {**rest, **{n: scores[n] for n in gate_names}}
        else:
            scores, usage = await self._ask(jpeg)
            usages.append(usage)
        selected, extra = apply_image_rules(
            self.taxonomy,
            scores,
            select_tags(self.taxonomy, scores),
            await asyncio.to_thread(analyze, jpeg),
            self.blur_var,
        )
        return make_result(
            self.taxonomy,
            scores,
            selected,
            self.delete_tag,
            tuple(extra),
            latency=time.perf_counter() - t0,
            usage=usages,
        )

    async def tag_video(self, frames: list[bytes], duration: float | None = None) -> TagResult:
        t0 = time.perf_counter()
        per_frame = await asyncio.gather(*(self._ask(f) for f in frames))
        all_scores = [s for s, _ in per_frame]
        flags = [
            frame_flags(self.taxonomy, s, analyze(f), self.blur_var)
            for s, f in zip(all_scores, frames, strict=True)
        ]
        result = aggregate_frames(self.taxonomy, all_scores, flags, duration, self.delete_tag)
        result.latency = time.perf_counter() - t0
        result.usage = [u for _, u in per_frame]
        return result
