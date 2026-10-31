import warnings
from pathlib import Path

import pytest

from nc_autotag.taxonomy import TaxonomyError, load_taxonomy, parse_taxonomy


def cat(id_, kind, names, **kw):
    return {
        "id": id_,
        "kind": kind,
        "instructions": "q",
        "tags": [{"name": n, "description": "d"} for n in names],
        **kw,
    }


def test_real_file_is_valid_and_quiet():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        t = load_taxonomy(Path("config/tags.yaml"))
    assert t.question_count <= 12
    names = [x.name for x in t.tags]
    assert len(names) == len(set(names)) >= 40
    assert all(n == n.lower() for n in names)


def test_threshold_resolution():
    t = parse_taxonomy(
        {
            "default_threshold": 0.4,
            "categories": [
                cat("a", "choice", ["x"]),
                cat("b", "choice", ["y"], threshold=0.7),
                {
                    **cat("c", "noul", []),
                    "tags": [{"name": "z", "description": "d", "threshold": 0.9}],
                },
            ],
        }
    )
    assert [t.threshold(n) for n in "xyz"] == [0.4, 0.7, 0.9]
    assert t.question_count == 3


def test_duplicate_tag_rejected():
    with pytest.raises(TaxonomyError, match="double"):
        parse_taxonomy({"categories": [cat("a", "choice", ["x"]), cat("b", "noul", ["X"])]})


def test_reserved_and_missing_instructions():
    with pytest.raises(TaxonomyError, match="réservé"):
        parse_taxonomy({"categories": [cat("a", "choice", ["aucun"])]})
    bad = cat("a", "choice", ["x"], instructions="")
    with pytest.raises(TaxonomyError, match="instructions"):
        parse_taxonomy({"categories": [bad]})


def test_too_many_questions_warns():
    with pytest.warns(UserWarning, match="questions"):
        parse_taxonomy({"categories": [cat("a", "noul", [f"t{i}" for i in range(13)])]})


def test_cle_inconnue_refusee():
    data = {
        "categories": [
            {"id": "x", "kind": "choice", "tags": [{"name": "a", "description": "d", "oups": None}]}
        ]
    }
    with pytest.raises(TaxonomyError):
        parse_taxonomy(data)


def test_descriptions_par_defaut_non_tronquees():
    tax = load_taxonomy()
    desc = {t.name: t.description for t in tax.tags}
    assert desc["montagne"] == "mountains, alpine landscape or ski slopes"


def test_gate_valide_et_premiere_dans_le_vrai_fichier():
    tax = load_taxonomy()
    assert tax.categories[0].gate
    assert tax.gate.exclusive == {"capture d'écran", "document", "carte"}


def test_gate_regles():
    with pytest.raises(TaxonomyError, match="gate"):
        parse_taxonomy({"categories": [cat("a", "choice", ["x"], gate=True)]})
    with pytest.raises(TaxonomyError, match="inconnu"):
        parse_taxonomy({"categories": [cat("a", "choice", ["x"], gate=True, exclusive=["y"])]})
    with pytest.raises(TaxonomyError, match="exclusive"):
        parse_taxonomy({"categories": [cat("a", "choice", ["x"], exclusive=["x"])]})
    ok = parse_taxonomy(
        {"categories": [cat("a", "choice", ["x", "y"], gate=True, exclusive=["x"])]}
    )
    assert ok.gate.id == "a" and ok.gate.exclusive == {"x"}


def test_emit_et_also():
    t = parse_taxonomy(
        {
            "categories": [
                {
                    **cat("a", "choice", []),
                    "tags": [
                        {"name": "x", "description": "d", "emit": "poubelle"},
                        {"name": "y", "description": "d", "emit": "poubelle"},
                        {"name": "z", "description": "d", "also": ["poubelle", "autre"]},
                        {"name": "w", "description": "d"},
                    ],
                }
            ]
        }
    )
    assert t.emitted(["x", "y"]) == ["poubelle"]
    assert t.emitted(["z", "w"]) == ["z", "poubelle", "autre", "w"]
    assert t.vocabulary() == ["poubelle", "z", "autre", "w"]
    with pytest.raises(TaxonomyError, match="double"):
        parse_taxonomy({"categories": [cat("a", "choice", ["x", "X"])]})


def test_vrai_fichier_qualite_et_capture():
    t = load_taxonomy()
    assert t.question_count <= 12
    assert t.threshold("floue") == t.threshold("accidentelle") == 0.8
    assert t.emitted(["floue", "accidentelle"]) == ["à supprimer"]
    assert t.emitted(["capture d'écran"]) == ["capture d'écran", "à supprimer"]
    assert t.emitted(["floue"], "to delete") == ["to delete"]
    assert t.vocabulary().count("à supprimer") == 1
