import pytest

from fake_nc import FakeNextcloud, nc_client
from nc_autotag.nextcloud.tags import TagClient, TagError


def make(fake: FakeNextcloud) -> TagClient:
    return TagClient(nc_client(fake.handler))


async def test_list_and_file_tags():
    fake = FakeNextcloud({"plage": 3, "Mer": 4}, manual={42: {"plage"}})
    tc = make(fake)
    assert await tc.list_tags() == {3: "plage", 4: "Mer"}
    assert await tc.file_tags(42) == {"plage"}
    assert await tc.file_tags(43) == set()


async def test_create_201_uses_content_location():
    fake = FakeNextcloud()
    tc = make(fake)
    tag_id = await tc.ensure_tag("à supprimer")
    assert fake.tags[tag_id] == "à supprimer"
    assert [m for m, _ in fake.writes] == ["POST"]
    assert await tc.ensure_tag("à supprimer") == tag_id
    assert [m for m, _ in fake.writes] == ["POST"]


async def test_existing_tag_reused_case_insensitive():
    fake = FakeNextcloud({"Plage": 7})
    tc = make(fake)
    assert await tc.ensure_tag("plage") == 7
    assert fake.writes == []


async def test_create_409_relists_and_reuses():
    fake = FakeNextcloud({"mer": 1})
    tc = make(fake)
    await tc.list_tags()  # le tag apparaît ensuite côté serveur (autre client)
    fake.tag_id("chien", create=True)
    tag_id = await tc.ensure_tag("chien")
    assert fake.tags[tag_id] == "chien"
    assert [m for m, _ in fake.writes] == ["POST"]
    assert len(fake.tags) == 2


async def test_assign_201_and_409_means_preexisting():
    fake = FakeNextcloud({"plage": 3}, manual={1: {"plage"}})
    tc = make(fake)
    assert await tc.assign(2, 3) is True
    assert fake.names(2) == {"plage"}
    assert await tc.assign(1, 3) is False  # déjà posé à la main
    assert await tc.assign(2, 3) is False


async def test_assign_error_raises():
    fake = FakeNextcloud({"plage": 3})
    fake.fail_assign.add(3)
    with pytest.raises(TagError):
        await make(fake).assign(1, 3)


async def test_unassign_204_then_404():
    fake = FakeNextcloud({"plage": 3}, manual={1: {"plage"}})
    tc = make(fake)
    assert await tc.unassign(1, 3) is True
    assert fake.names(1) == set()
    assert await tc.unassign(1, 3) is False


async def test_never_deletes_a_tag():
    fake = FakeNextcloud({"plage": 3}, manual={1: {"plage"}})
    tc = make(fake)
    await tc.ensure_tag("plage")
    await tc.assign(2, 3)
    await tc.unassign(2, 3)
    assert not any(path.endswith("/systemtags/3") for _, path in fake.requests)
