import httpx
import pytest

from fake_nc import FakeNextcloud, nc_client
from nc_autotag import pipeline
from nc_autotag.config import Settings
from nc_autotag.inference.d1 import make_result
from nc_autotag.media.ssh import SftpSource
from nc_autotag.state import State
from nc_autotag.taxonomy import parse_taxonomy
from synth import jpeg, sharp_image
from test_ssh import FakeSftp

TAX = parse_taxonomy(
    {
        "categories": [
            {
                "id": "lieu",
                "kind": "choice",
                "instructions": "Where?",
                "tags": [
                    {"name": "plage", "description": "a beach"},
                    {"name": "montagne", "description": "mountains"},
                ],
            },
            {
                "id": "gens",
                "kind": "noul",
                "tags": [{"name": "personne", "description": "a person", "emit": "gens"}],
            },
            {
                "id": "qualite",
                "kind": "choice",
                "instructions": "Usable?",
                "tags": [{"name": "floue", "description": "blurry", "emit": "à supprimer"}],
            },
        ]
    }
)
DELETE = "à supprimer"
IMG_A = jpeg(sharp_image(1, (300, 200)))
IMG_B = jpeg(sharp_image(2, (300, 200)))


class FakeD1:
    """Résultats prédéfinis, dans l'ordre des fichiers ; une seule inférence à la fois."""

    taxonomy = TAX
    delete_tag = DELETE

    def __init__(self, selected: list[dict[str, float]]):
        self.selected = list(selected)
        self.running = 0
        self.peak = 0

    async def tag_image(self, jpeg_bytes: bytes):
        self.running += 1
        self.peak = max(self.peak, self.running)
        selected = self.selected.pop(0)
        scores = {t.name: selected.get(t.name, 0.1) for t in TAX.tags}
        self.running -= 1
        return make_result(TAX, scores, selected, DELETE)


def fixture_state(files: dict[int, tuple[str, bytes | None]]):
    st = State(":memory:")
    sftp = {}
    for fileid, (path, data) in files.items():
        st.upsert_file("alice", fileid, path, "image/jpeg", f"e{fileid}", 10)
        if data is not None:
            sftp[f"/data/alice/files{path}"] = data
    settings = Settings()
    settings.ssh.data_dir = "/data"
    return st, FakeSftp(sftp), settings


async def run(st, sftp, settings, d1, **kw):
    async with SftpSource(settings.ssh, sftp=sftp) as source:
        return await pipeline.run(settings, st, d1, source, ["alice"], **kw)


async def test_process_stores_everything_and_skips_missing():
    st, sftp, settings = fixture_state(
        {1: ("/Photos/a.jpg", IMG_A), 2: ("/Photos/b.jpg", IMG_B), 3: ("/Shared/c.jpg", None)}
    )
    d1 = FakeD1([{"plage": 0.9, "personne": 0.8}, {"floue": 0.95}])
    report = await run(st, sftp, settings, d1)
    assert report.counts == {"done": 2, "skipped": 1}
    assert st.counts() == {"pending": 0, "done": 2, "error": 0, "skipped": 1}
    assert "absent du disque" in st.get_file("alice", 3).error
    assert st.get_proposals("alice", 1) == {"plage": 0.9, "gens": 0.8}
    assert st.get_file("alice", 2).reasons == "floue"
    assert st.get_proposals("alice", 2) == {}
    assert st.get_file("alice", 1).model_ver == "d1-Omni-2B-Q8_0"
    assert len(st.get_hashes("alice")) == 2
    assert st.get_scores("alice", 1)["plage"] == 0.9
    assert report.to_add == {"plage": 1, "gens": 1, DELETE: 1}
    assert [f.fileid for f in report.delete] == [2]
    assert d1.peak == 1


async def test_error_per_file_and_resume():
    st, sftp, settings = fixture_state({1: ("/a.jpg", b"pas une image"), 2: ("/b.jpg", IMG_B)})
    report = await run(st, sftp, settings, FakeD1([{"plage": 0.9}]))
    assert report.counts == {"error": 1, "done": 1}
    assert "UnidentifiedImageError" in st.get_file("alice", 1).error
    again = await run(st, sftp, settings, FakeD1([]))
    assert again.counts == {}  # rien en attente : repris là où on s'était arrêté


async def test_limit_path_prefix_and_reprocess():
    st, sftp, settings = fixture_state(
        {1: ("/Photos/a.jpg", IMG_A), 2: ("/Photos2/b.jpg", IMG_B), 3: ("/Photos/c.jpg", IMG_A)}
    )
    report = await run(st, sftp, settings, FakeD1([{"plage": 0.9}]), path_prefix="/Photos", limit=1)
    assert report.counts["done"] == 1
    assert [f.fileid for f in st.next_pending()] == [2, 3]
    assert [f.fileid for f in st.next_pending(path_prefix="/Photos")] == [3]
    await run(st, sftp, settings, FakeD1([{}, {}]))
    again = await run(st, sftp, settings, FakeD1([{"montagne": 0.9}]), reprocess=True, limit=1)
    assert again.counts["done"] == 1


async def test_duplicates_flagged_with_keeper():
    st, sftp, settings = fixture_state(
        {
            1: ("/Photos/a.jpg", IMG_A),
            2: ("/Photos/a copie.jpg", IMG_A),
            3: ("/Photos/b.jpg", IMG_B),
        }
    )
    d1 = FakeD1([{"plage": 0.9}, {"plage": 0.9}, {"montagne": 0.8}])
    report = await run(st, sftp, settings, d1)
    dup = st.get_file("alice", 2)
    assert (dup.reasons, dup.keeper) == ("doublon", 1)
    assert st.get_file("alice", 1).reasons is None
    assert report.duplicates == 1
    assert pipeline.wanted_tags(st, dup, DELETE, 8) == [DELETE, "plage"]


async def test_max_tags_keeps_delete_first_then_best_scores():
    st = State(":memory:")
    st.upsert_file("u", 1, "/a.jpg", "image/jpeg", "e", 1)
    st.mark_done("u", 1)
    st.record_proposals("u", 1, {"a": 0.5, "b": 0.9, "c": 0.7})
    st.set_result("u", 1, ["floue"], 1)
    f = st.get_file("u", 1)
    assert pipeline.wanted_tags(st, f, DELETE, 3) == [DELETE, "b", "c"]


async def test_dry_run_writes_nothing_to_nextcloud():
    fake = FakeNextcloud()
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    report = await run(
        st,
        sftp,
        settings,
        FakeD1([{"plage": 0.9}]),
        make_client=lambda user: nc_client(fake.handler, user),
    )
    assert report.mode == "dry-run" and report.apply_counts is None
    assert fake.requests == []
    assert st.all_assignments() == []
    assert report.pending_apply == 1
    assert st.recent_runs()[0].mode == "dry-run"


async def apply_all(st, settings, fake, run_id=None):
    run_id = run_id or st.start_run("apply")
    todo = pipeline.files_to_apply(st, settings, ["alice"])
    return await pipeline.apply_files(st, todo, run_id, lambda user: nc_client(fake.handler, user))


async def test_apply_journals_only_what_it_added_and_rollback_is_scoped():
    fake = FakeNextcloud({"plage": 3}, manual={1: {"plage", "famille"}})
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    await run(st, sftp, settings, FakeD1([{"plage": 0.9, "personne": 0.8}]))
    counts = await apply_all(st, settings, fake)
    assert counts["added"] == 1 and counts["preexisting"] == 1 and counts["tags_created"] == 1
    assert fake.names(1) == {"plage", "famille", "gens"}
    journal = st.all_assignments()
    assert [(a.tag, a.fileid) for a in journal] == [("gens", 1)]
    assert st.get_file("alice", 1).applied == '["gens", "plage"]'
    assert await apply_all(st, settings, fake) == {"tags_created": 0}  # rien à refaire

    removed = await pipeline.rollback_assignments(
        st, st.all_assignments(), lambda user: nc_client(fake.handler, user)
    )
    assert removed["removed"] == 1
    assert fake.names(1) == {"plage", "famille"}  # le tag manuel reste
    assert st.all_assignments() == []
    assert len(st.all_assignments(only_active=False)) == 1
    assert st.get_file("alice", 1).applied is None  # un nouvel --apply repose les tags
    again = await apply_all(st, settings, fake)
    assert again["added"] == 1 and fake.names(1) == {"plage", "famille", "gens"}


async def test_reprocessing_removes_only_journaled_tags_no_longer_wanted():
    fake = FakeNextcloud(manual={1: {"montagne"}})
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    await run(st, sftp, settings, FakeD1([{"plage": 0.9, "personne": 0.8}]))
    await apply_all(st, settings, fake)
    assert fake.names(1) == {"montagne", "plage", "gens"}
    # l'etag change : le fichier est retraité, d1 ne voit plus que la montagne et « floue »
    st.upsert_file("alice", 1, "/a.jpg", "image/jpeg", "e-new", 10)
    await run(st, sftp, settings, FakeD1([{"montagne": 0.9, "floue": 0.9}]))
    counts = await apply_all(st, settings, fake)
    assert counts["removed"] == 2 and counts["added"] == 1 and counts["preexisting"] == 1
    assert fake.names(1) == {"montagne", DELETE}
    active = [a.tag for a in st.active_assignments("alice", 1)]
    assert active == [DELETE]


async def test_assign_error_is_counted_and_not_marked_applied():
    fake = FakeNextcloud({"plage": 3})
    fake.fail_assign.add(3)
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    await run(st, sftp, settings, FakeD1([{"plage": 0.9}]))
    errors = []
    todo = pipeline.files_to_apply(st, settings, ["alice"])
    counts = await pipeline.apply_files(
        st,
        todo,
        st.start_run("apply"),
        lambda user: nc_client(fake.handler, user),
        on_error=lambda f, msg: errors.append(msg),
    )
    assert counts["errors"] == 1 and counts["added"] == 0
    assert st.all_assignments() == [] and st.get_file("alice", 1).applied is None
    assert len(errors) == 1 and "secret" not in errors[0]


async def test_apply_run_creates_run_with_counts():
    fake = FakeNextcloud()
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    report = await run(
        st,
        sftp,
        settings,
        FakeD1([{"plage": 0.9}]),
        apply=True,
        make_client=lambda user: nc_client(fake.handler, user),
    )
    assert report.apply_counts["added"] == 1
    last = st.recent_runs()[0]
    assert last.mode == "apply" and last.stats["apply"]["added"] == 1
    assert [a.run_id for a in st.all_assignments()] == [report.run_id]
    assert fake.names(1) == {"plage"}


async def test_inference_down_leaves_files_pending():
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})

    class Down(FakeD1):
        async def tag_image(self, jpeg_bytes):
            raise httpx.ConnectError("refused")

    with pytest.raises(pipeline.InferenceUnavailable):
        await run(st, sftp, settings, Down([]))
    assert st.get_file("alice", 1).status == "pending"
    assert st.recent_runs()[0].finished_at is not None


class Clock:
    """Horloge factice : avance d'une minute à chaque lecture."""

    def __init__(self, start):
        self.now = start

    def __call__(self):
        from datetime import timedelta

        self.now += timedelta(minutes=1)
        return self.now


async def test_stop_at_leaves_remaining_pending_and_resumes():
    from datetime import datetime

    st, sftp, settings = fixture_state(
        {1: ("/a.jpg", IMG_A), 2: ("/b.jpg", IMG_B), 3: ("/c.jpg", IMG_A), 4: ("/d.jpg", IMG_B)}
    )
    clock = Clock(datetime(2026, 11, 1, 1, 0))
    report = await run(
        st,
        sftp,
        settings,
        FakeD1([{"plage": 0.9}] * 4),
        stop_at=datetime(2026, 11, 1, 1, 3, 30),
        clock=clock,
    )
    assert report.stopped
    assert 0 < report.counts["done"] < 4
    assert report.remaining == 4 - report.counts["done"]
    assert st.recent_runs()[0].finished_at is not None
    left = report.remaining
    again = await run(st, sftp, settings, FakeD1([{"plage": 0.9}] * 4))
    assert again.counts["done"] == left and not again.stopped and again.remaining == 0


async def test_stop_at_skips_apply():
    from datetime import datetime

    fake = FakeNextcloud()
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A), 2: ("/b.jpg", IMG_B)})
    report = await run(
        st,
        sftp,
        settings,
        FakeD1([{"plage": 0.9}] * 2),
        apply=True,
        stop_at=datetime(2026, 11, 1, 1, 1, 30),
        clock=Clock(datetime(2026, 11, 1, 1, 0)),
        make_client=lambda user: nc_client(fake.handler, user),
    )
    assert report.stopped and report.apply_counts is None and fake.writes == []


async def test_exclude_path_skips_process_and_apply():
    fake = FakeNextcloud()
    st, sftp, settings = fixture_state(
        {
            1: ("/Local/alice/a.jpg", IMG_A),
            2: ("/Photos/b.jpg", IMG_B),
            3: ("/Localx/c.jpg", IMG_A),
        }
    )
    report = await run(
        st,
        sftp,
        settings,
        FakeD1([{"plage": 0.9}] * 2),
        apply=True,
        exclude_paths=["/Local"],
        make_client=lambda user: nc_client(fake.handler, user),
    )
    assert report.counts["done"] == 2
    assert st.get_file("alice", 1).status == "pending"
    assert report.remaining == 0
    assert fake.names(1) == set() and fake.names(2) == {"plage"} and fake.names(3) == {"plage"}


async def test_no_delete_tag_keeps_proposal_and_later_apply_adds_it():
    fake = FakeNextcloud()
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    await run(
        st,
        sftp,
        settings,
        FakeD1([{"plage": 0.9, "floue": 0.9}]),
        apply=True,
        no_delete_tag=True,
        make_client=lambda user: nc_client(fake.handler, user),
    )
    assert fake.names(1) == {"plage"}
    f = st.get_file("alice", 1)
    assert f.reasons == "floue" and f.applied == '["plage"]'
    assert len(pipeline.files_to_apply(st, settings, ["alice"])) == 1
    await apply_all(st, settings, fake)
    assert fake.names(1) == {"plage", DELETE}


async def test_no_delete_tag_never_removes_journaled_delete_tag():
    fake = FakeNextcloud()
    st, sftp, settings = fixture_state({1: ("/a.jpg", IMG_A)})
    await run(st, sftp, settings, FakeD1([{"plage": 0.9, "floue": 0.9}]))
    await apply_all(st, settings, fake)
    assert fake.names(1) == {"plage", DELETE}
    st.set_applied("alice", 1, ["x"])
    todo = pipeline.files_to_apply(st, settings, ["alice"], with_delete=False)
    await pipeline.apply_files(
        st,
        todo,
        st.start_run("apply"),
        lambda user: nc_client(fake.handler, user),
        keep=frozenset([DELETE]),
    )
    assert DELETE in fake.names(1)
