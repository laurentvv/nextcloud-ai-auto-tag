import warnings
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

DEFAULT_TAGS = Path("config/tags.yaml")
MAX_QUESTIONS = 12
NONE_OPTION = "aucun"
DELETE_TAG = "à supprimer"


class TaxonomyError(ValueError):
    pass


class TagModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    threshold: float | None = None
    emit: str | None = None
    also: list[str] = []


class CategoryModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    kind: Literal["choice", "noul"]
    instructions: str = ""
    threshold: float | None = None
    gate: bool = False
    exclusive: list[str] = []
    tags: list[TagModel]


class TaxonomyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    default_threshold: float = 0.5
    categories: list[CategoryModel]


@dataclass(frozen=True)
class Tag:
    name: str
    description: str
    threshold: float
    category: str
    emit: str = ""
    also: tuple[str, ...] = ()


@dataclass(frozen=True)
class Category:
    id: str
    kind: str
    instructions: str
    tags: tuple[Tag, ...]
    gate: bool = False
    exclusive: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Taxonomy:
    categories: tuple[Category, ...]

    @property
    def tags(self) -> list[Tag]:
        return [t for c in self.categories for t in c.tags]

    @property
    def question_count(self) -> int:
        return sum(1 if c.kind == "choice" else len(c.tags) for c in self.categories)

    @property
    def gate(self) -> Category | None:
        return next((c for c in self.categories if c.gate), None)

    def threshold(self, tag: str) -> float:
        return next(t.threshold for t in self.tags if t.name == tag)

    def emitted(self, selected: Iterable[str], delete_tag: str = DELETE_TAG) -> list[str]:
        """Tags Nextcloud à poser pour les options retenues (emit puis also, sans doublon)."""
        by_name = {t.name: t for t in self.tags}
        out: list[str] = []
        for name in selected:
            t = by_name[name]
            for n in (t.emit, *t.also):
                n = delete_tag if n == DELETE_TAG else n
                if n not in out:
                    out.append(n)
        return out

    def vocabulary(self, delete_tag: str = DELETE_TAG) -> list[str]:
        return self.emitted([t.name for t in self.tags], delete_tag)


def parse_taxonomy(data: dict) -> Taxonomy:
    try:
        model = TaxonomyModel(**data)
    except ValidationError as e:
        raise TaxonomyError(f"tags.yaml invalide : {e}") from e
    names: set[str] = set()
    cat_ids: set[str] = set()
    categories = []
    for c in model.categories:
        if c.id in cat_ids:
            raise TaxonomyError(f"Catégorie en double : {c.id}")
        cat_ids.add(c.id)
        if c.kind == "choice" and not c.instructions:
            raise TaxonomyError(f"La catégorie {c.id} (choice) exige des instructions")
        tags = []
        for t in c.tags:
            name = t.name.strip().lower()
            if name in names:
                raise TaxonomyError(f"Nom de tag en double : {name}")
            if name == NONE_OPTION:
                raise TaxonomyError(f"« {NONE_OPTION} » est réservé")
            names.add(name)
            thr = next(
                x for x in (t.threshold, c.threshold, model.default_threshold) if x is not None
            )
            emit = (t.emit or name).strip()
            also = tuple(a.strip() for a in t.also)
            tags.append(Tag(name, t.description, thr, c.id, emit, also))
        exclusive = frozenset(n.strip().lower() for n in c.exclusive)
        if c.gate:
            if c.kind != "choice" or not exclusive:
                raise TaxonomyError(f"La catégorie {c.id} (gate) exige kind choice et exclusive")
            if any(x.gate for x in categories):
                raise TaxonomyError("Une seule catégorie gate est autorisée")
        elif exclusive:
            raise TaxonomyError(f"exclusive n'a de sens que pour une catégorie gate ({c.id})")
        unknown = exclusive - {t.name for t in tags}
        if unknown:
            raise TaxonomyError(
                f"exclusive : tag inconnu dans {c.id} : {', '.join(sorted(unknown))}"
            )
        categories.append(Category(c.id, c.kind, c.instructions, tuple(tags), c.gate, exclusive))
    taxonomy = Taxonomy(tuple(categories))
    if taxonomy.question_count > MAX_QUESTIONS:
        warnings.warn(
            f"{taxonomy.question_count} questions (> {MAX_QUESTIONS}) : "
            "chaque question coûte environ 0,12 s par image",
            stacklevel=2,
        )
    return taxonomy


def load_taxonomy(path: Path | str = DEFAULT_TAGS) -> Taxonomy:
    with Path(path).open(encoding="utf-8") as f:
        return parse_taxonomy(yaml.safe_load(f) or {})
