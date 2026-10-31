from datetime import datetime, time

import pytest

from fake_nc import FakeNextcloud, nc_client
from nc_autotag.nextcloud.blackout import in_blackout, parse_windows, seconds_until_end
from nc_autotag.nextcloud.client import NcClient

W = parse_windows(["03:00-05:30"])


def at(h: int, m: int = 0) -> datetime:
    return datetime(2026, 11, 1, h, m)


def test_parse():
    assert W == [(time(3, 0), time(5, 30))]
    with pytest.raises(ValueError):
        parse_windows(["3h-5h"])
    with pytest.raises(ValueError):
        parse_windows(["03:00-03:00"])


def test_inside_and_edges():
    assert not in_blackout(at(2, 59), W)
    assert in_blackout(at(3, 0), W)
    assert in_blackout(at(5, 29), W)
    assert not in_blackout(at(5, 30), W)


def test_window_across_midnight():
    w = parse_windows(["23:00-01:00"])
    assert in_blackout(at(23, 30), w) and in_blackout(at(0, 30), w)
    assert not in_blackout(at(12), w)
    assert seconds_until_end(at(23, 30), w) == 90 * 60


def test_seconds_until_end():
    assert seconds_until_end(at(2), W) == 0
    assert seconds_until_end(at(3, 30), W) == 2 * 3600
    assert seconds_until_end(at(5, 30), W) == 0


def test_chained_windows():
    w = parse_windows(["03:00-04:00", "04:00-05:00"])
    assert seconds_until_end(at(3, 30), w) == 5400


async def test_writes_wait_for_blackout_reads_do_not():
    fake = FakeNextcloud({"plage": 5})
    clock = [at(3, 30)]
    slept = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock[0] = at(5, 30)

    client: NcClient = nc_client(
        fake.handler, blackout=["03:00-05:30"], sleep=sleep, now=lambda: clock[0]
    )
    await client.request("PROPFIND", "/remote.php/dav/systemtags/")
    assert slept == []
    await client.request("PUT", "/remote.php/dav/systemtags-relations/files/1/5")
    assert slept == [2 * 3600]
    assert fake.writes == [("PUT", "/remote.php/dav/systemtags-relations/files/1/5")]
    await client.aclose()
