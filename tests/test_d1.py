import asyncio
import json
import os
from pathlib import Path

import httpx
import pytest

from nc_autotag.inference.d1 import D1Client, aggregate_frames, build_questions, parse_answers
from nc_autotag.media.images import to_jpeg
from nc_autotag.taxonomy import load_taxonomy, parse_taxonomy
from synth import black_image, blurred, jpeg, sharp_image

JPEG = jpeg(sharp_image(1, (200, 150)))

TAX = parse_taxonomy(
    {
        "categories": [
            {
                "id": "lieu",
                "kind": "choice",
                "instructions": "Where?",
                "threshold": 0.5,
                "tags": [
                    {"name": "plage", "description": "a beach"},
                    {"name": "ville", "description": "a city"},
                ],
            },
            {
                "id": "gens",
                "kind": "noul",
                "threshold": 0.6,
                "tags": [{"name": "personne", "description": "a person is visible"}],
            },
        ]
    }
)


def answers(plage=0.1, ville=0.1, personne=0.1):
    return {
        "lieu": {
            "type": "choice",
            "choice": "plage",
            "probabilities": {"plage": plage, "ville": ville, "aucun": 1 - plage - ville},
        },
        "personne": {"type": "noul", "noul": personne},
    }


def make_client(handler, **kw):
    return D1Client(TAX, transport=httpx.MockTransport(handler), **kw)


def test_build_questions():
    q = build_questions(TAX)
    assert q["lieu"]["type"] == "choice"
    assert list(q["lieu"]["criteria"]) == ["plage", "ville", "aucun"]
    assert q["personne"]["type"] == "noul"
    assert "a person is visible" in q["personne"]["instructions"]


def test_parse_answers_ignores_aucun_and_missing():
    s = parse_answers(TAX, answers(plage=0.7, ville=0.2, personne=0.9))
    assert s == {"plage": 0.7, "ville": 0.2, "personne": 0.9}
    assert parse_answers(TAX, {}) == {"plage": 0.0, "ville": 0.0, "personne": 0.0}


async def test_tag_image_request_and_selection():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["body"] = json.loads(request.content)
        return httpx.Response(
            200, json={"answers": answers(plage=0.8, personne=0.4), "usage": {"n": 1}}
        )

    client = make_client(handler, port=9999)
    res = await client.tag_image(JPEG)
    await client.aclose()
    assert seen["url"] == "http://127.0.0.1:9999/v1/systemone"
    body = seen["body"]
    assert body["state"] is None
    assert body["images"][0].startswith("data:image/jpeg;base64,")
    assert set(body["questions"]) == {"lieu", "personne"}
    assert res.selected == {"plage": 0.8}
    assert res.scores["personne"] == 0.4
    assert res.usage == [{"n": 1}]


def test_video_aggregation_needs_two_frames_when_k_ge_4():
    frames = [{"plage": 0.9, "personne": 0.1}] + [{"plage": 0.1, "personne": 0.7}] * 2
    frames.append({"plage": 0.2, "personne": 0.2})
    res = aggregate_frames(TAX, frames)
    assert res.scores["plage"] == 0.9
    assert "plage" not in res.selected  # une seule image au-dessus du seuil
    assert res.selected == {"personne": 0.7}


def test_video_aggregation_single_frame_ok_when_k_lt_4():
    res = aggregate_frames(TAX, [{"plage": 0.9}, {"plage": 0.1}, {"plage": 0.1}])
    assert res.selected == {"plage": 0.9}


async def test_tag_video_and_concurrency_limit():
    running = peak = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.02)
        running -= 1
        return httpx.Response(200, json={"answers": answers(plage=0.9), "usage": {}})

    client = make_client(handler, concurrency=2)
    res = await client.tag_video([JPEG] * 6)
    await client.aclose()
    assert peak == 2
    assert res.frames == 6
    assert res.selected == {"plage": 0.9}


async def test_http_error_raises():
    client = make_client(lambda r: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        await client.tag_image(JPEG)
    await client.aclose()


def _server_ok() -> bool:
    try:
        return httpx.get("http://127.0.0.1:8080/health", timeout=1).json().get("status") == "ok"
    except Exception:
        return False


@pytest.mark.live
@pytest.mark.skipif(not _server_ok(), reason="llama-server absent sur 127.0.0.1:8080")
async def test_live_real_image():
    sample = os.environ.get("NC_AUTOTAG_SAMPLE_DIR")
    images = sorted(Path(sample).glob("IMG_*.jpg")) if sample else []
    if not images:
        pytest.skip("NC_AUTOTAG_SAMPLE_DIR non défini ou sans image")
    client = D1Client(load_taxonomy(Path("config/tags.yaml")))
    res = await client.tag_image(to_jpeg(str(images[0]), 768))
    await client.aclose()
    assert set(res.scores) == {t.name for t in client.taxonomy.tags}
    assert all(0.0 <= s <= 1.0 for s in res.scores.values())


GATED = parse_taxonomy(
    {
        "categories": [
            {
                "id": "contenu",
                "kind": "choice",
                "instructions": "Kind?",
                "threshold": 0.6,
                "gate": True,
                "exclusive": ["document"],
                "tags": [
                    {"name": "document", "description": "a paper"},
                    {"name": "selfie", "description": "a selfie"},
                ],
            },
            {
                "id": "lieu",
                "kind": "choice",
                "instructions": "Where?",
                "threshold": 0.5,
                "tags": [{"name": "plage", "description": "a beach"}],
            },
        ]
    }
)


def gated_handler(doc, calls):
    def handler(request: httpx.Request) -> httpx.Response:
        qs = set(json.loads(request.content)["questions"])
        calls.append(qs)
        if "contenu" in qs:
            probs = {"document": doc, "selfie": 0.1, "aucun": 0.9 - doc}
            return httpx.Response(200, json={"answers": {"contenu": {"probabilities": probs}}})
        return httpx.Response(
            200, json={"answers": {"lieu": {"probabilities": {"plage": 0.8}}}, "usage": {}}
        )

    return handler


async def test_gate_skips_other_questions():
    calls = []
    client = D1Client(GATED, transport=httpx.MockTransport(gated_handler(0.9, calls)))
    res = await client.tag_image(JPEG)
    await client.aclose()
    assert calls == [{"contenu"}]
    assert res.selected == {"document": 0.9}
    assert res.scores["plage"] == 0.0


async def test_gate_passes_through_to_remaining_questions():
    calls = []
    client = D1Client(GATED, transport=httpx.MockTransport(gated_handler(0.3, calls)))
    res = await client.tag_image(JPEG)
    await client.aclose()
    assert calls == [{"contenu"}, {"lieu"}]
    assert res.selected == {"plage": 0.8}
    assert res.scores["document"] == 0.3


async def test_video_asks_full_set_without_gate():
    calls = []
    client = D1Client(GATED, transport=httpx.MockTransport(gated_handler(0.9, calls)))
    await client.tag_video([JPEG])
    await client.aclose()
    assert calls == [{"contenu", "lieu"}]


REAL = load_taxonomy(Path("config/tags.yaml"))
BLURRY = jpeg(blurred(sharp_image(2), 8))
SHARP = jpeg(sharp_image(3))
BLACK = jpeg(black_image())


def quality_handler(floue=0.0, accidentelle=0.0, capture=0.0):
    def handler(request: httpx.Request) -> httpx.Response:
        probs = {
            "floue": floue,
            "accidentelle": accidentelle,
            "capture d'écran": capture,
            "aucun": 0.0,
        }
        answers = {"qualite": {"probabilities": probs}, "contenu": {"probabilities": probs}}
        return httpx.Response(200, json={"answers": answers, "usage": {}})

    return handler


async def tag(jpeg_bytes, **kw):
    client = D1Client(REAL, transport=httpx.MockTransport(quality_handler(**kw)))
    res = await client.tag_image(jpeg_bytes)
    await client.aclose()
    return res


async def test_flou_exige_d1_et_laplacien():
    res = await tag(BLURRY, floue=0.9)
    assert res.delete and res.reasons == ["floue"]
    assert res.emitted == ["à supprimer"]
    assert "floue" in res.selected
    assert not (await tag(SHARP, floue=0.9)).delete  # d1 seul ne suffit pas
    assert not (await tag(BLURRY, floue=0.5)).delete  # Laplacien seul non plus


async def test_accident_noir_seul_ou_d1():
    res = await tag(BLACK)
    assert res.delete and res.reasons == ["accident"]
    assert (await tag(SHARP, accidentelle=0.9)).reasons == ["accident"]
    assert not (await tag(SHARP, accidentelle=0.5)).delete


async def test_capture_emet_aussi_a_supprimer():
    res = await tag(SHARP, capture=0.9)
    assert res.selected == {"capture d'écran": 0.9}
    assert res.emitted == ["capture d'écran", "à supprimer"]
    assert res.reasons == ["capture"]


async def test_delete_tag_name_configurable():
    client = D1Client(
        REAL, transport=httpx.MockTransport(quality_handler(floue=0.9)), delete_tag="to delete"
    )
    res = await client.tag_image(BLURRY)
    await client.aclose()
    assert res.emitted == ["to delete"]


def test_video_supprimee_si_deux_tiers_signalees():
    frames = [{"floue": 0.9}] * 6
    four = [(True, False)] * 4 + [(False, False)] * 2
    three = [(True, False)] * 3 + [(False, False)] * 3
    res = aggregate_frames(REAL, frames, four, 5.0)
    assert res.delete and res.reasons == ["floue"] and res.emitted == ["à supprimer"]
    assert not aggregate_frames(REAL, frames, three, 5.0).delete
    mixed = [(True, False)] * 2 + [(False, True)] * 2 + [(False, False)] * 2
    assert aggregate_frames(REAL, frames, mixed, 5.0).reasons == ["floue", "accident"]


def test_video_courte_supprimee():
    res = aggregate_frames(REAL, [{}] * 3, [(False, False)] * 3, 0.5)
    assert res.delete and res.reasons == ["accident"]
    assert not aggregate_frames(REAL, [{}] * 3, [(False, False)] * 3, 1.0).delete


async def test_tag_video_flou_avec_vraies_images():
    client = D1Client(REAL, transport=httpx.MockTransport(quality_handler(floue=0.9)))
    res = await client.tag_video([BLURRY] * 6, 10.0)
    await client.aclose()
    assert res.delete and res.reasons == ["floue"]
    client = D1Client(REAL, transport=httpx.MockTransport(quality_handler(floue=0.9)))
    res = await client.tag_video([SHARP] * 6, 10.0)
    await client.aclose()
    assert not res.delete
