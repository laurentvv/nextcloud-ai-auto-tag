import pytest

from nc_autotag.state import State


@pytest.fixture
def st():
    s = State(":memory:")
    yield s
    s.close()


def test_upsert_new_unchanged_changed(st):
    assert st.upsert_file("alice", 1, "/a.jpg", "image/jpeg", "e1", 10) == "new"
    assert st.next_pending()[0].fileid == 1
    st.mark_done("alice", 1, "d1-v1")
    assert st.upsert_file("alice", 1, "/a.jpg", "image/jpeg", "e1", 10) == "unchanged"
    assert st.get_file("alice", 1).status == "done"
    assert st.upsert_file("alice", 1, "/b.jpg", "image/jpeg", "e2", 12) == "changed"
    row = st.get_file("alice", 1)
    assert (row.status, row.path, row.etag, row.error) == ("pending", "/b.jpg", "e2", None)
    assert row.model_ver == "d1-v1"


def test_pk_is_user_and_fileid(st):
    st.upsert_file("alice", 1, "/a.jpg", "image/jpeg", "e", 1)
    st.upsert_file("bob", 1, "/a.jpg", "image/jpeg", "e", 1)
    assert len(st.next_pending()) == 2
    assert [f.user for f in st.next_pending(user="bob")] == ["bob"]


def test_status_and_limit(st):
    for i in range(5):
        st.upsert_file("u", i, f"/{i}.jpg", "image/jpeg", "e", 1)
    st.mark_done("u", 0)
    st.mark_error("u", 1, "boom")
    st.mark_skipped("u", 2, "trop gros")
    assert [f.fileid for f in st.next_pending(limit=1)] == [3]
    assert st.get_file("u", 1).error == "boom"
    assert st.counts() == {"pending": 2, "done": 1, "error": 1, "skipped": 1}


def test_scores_replaced(st):
    st.upsert_file("u", 1, "/a.jpg", "image/jpeg", "e", 1)
    st.record_scores("u", 1, {"plage": 0.9, "mer": 0.4})
    st.record_scores("u", 1, {"plage": 0.8})
    assert st.get_scores("u", 1) == {"plage": 0.8}


def test_journal_and_rollback_bookkeeping(st):
    run = st.start_run("apply")
    st.journal_assignment(run, "u", 1, "plage", 12)
    st.journal_assignment(run, "u", 2, "mer", 13)
    other = st.start_run("apply")
    st.journal_assignment(other, "u", 1, "chien", 14)
    acts = st.assignments_for_run(run)
    assert [(a.fileid, a.tag, a.tag_id) for a in acts] == [(1, "plage", 12), (2, "mer", 13)]
    st.mark_removed(acts[:1])
    assert [a.tag for a in st.assignments_for_run(run)] == ["mer"]
    assert len(st.assignments_for_run(run, only_active=False)) == 2
    assert [a.tag for a in st.active_assignments("u", 1)] == ["chien"]
    assert st.last_run_id() == other


def test_hashes_roundtrip_64_bits(st):
    big = (1 << 64) - 5
    st.upsert_file("u", 1, "/Photos/a.jpg", "image/jpeg", "e", 1)
    st.upsert_hash("u", 1, "image", big, 120.5, 12_000_000, None, "2026-01-02T10:00:00")
    st.upsert_hash("u", 2, "video", 7, None, None, 3.2, None)
    st.upsert_hash("u", 1, "image", big, 130.0, 12_000_000, None, "2026-01-02T10:00:00")
    rows = st.get_hashes("u")
    assert [(r.fileid, r.kind, r.dhash) for r in rows] == [(1, "image", big), (2, "video", 7)]
    assert rows[0].sharpness == 130.0 and rows[0].folder == "/Photos"
    assert rows[1].duration == 3.2 and rows[1].folder == ""
    assert st.get_hashes("autre") == []
