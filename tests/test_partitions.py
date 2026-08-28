"""Tests for selective sensor bypass (bridge/partitions.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_partitions.py

No network and no alarm panel: a local aiohttp fake stands in for the website
API, the same technique the other suites here use.

**A bypassed sensor does not trigger the alarm until it is un-bypassed.** Every
test below exists because of that sentence. The ones that matter most are not the
happy paths -- they are the refusals, because each of them is a case where
writing anyway would leave a zone silently not protecting a house.

Pinned here:

  1. The partition is re-read BEFORE every write, and its own flags decide.
     Discovering that a panel forbade the write by having already attempted it is
     not an acceptable order of operations.
  2. An armed partition whose `canBypassSensorWhenArmed` is false is refused --
     that flag is false on the reference panel, so this is the live case, not a
     hypothetical.
  3. Missing flags refuse rather than permit. An unknown permission is not
     permission.
  4. Ids are joined with "|", and an id CONTAINING a pipe is refused: it would
     split server-side and bypass a sensor nobody named.
"""
import asyncio
import contextlib
import inspect
import os
import pathlib
import sys

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c          # noqa: E402
from bridge import partitions as p      # noqa: E402

PID = "100000000-127"
DISARMED, ARMED_STAY = 1, 2


def partition_doc(**overrides):
    """A partition document shaped like the captured one."""
    attrs = {
        "partitionId": 1,
        "description": "Panel",
        "state": DISARMED,
        "hasPermissionToChangeState": True,
        # False on the reference panel -- the live case, not an edge case.
        "canBypassSensorWhenArmed": False,
        "hasOpenBypassableSensors": False,
        "hasForceArm": False,
        "hideForceBypass": True,
        "limitSelectiveBypassSensors": False,
    }
    attrs.update(overrides)
    return {"data": {"id": PID, "type": "devices/partition", "attributes": attrs}}


class FakePortal:
    """Serves one partition and records every bypass body it is sent."""

    def __init__(self, doc=None):
        self.doc = doc or partition_doc()
        self.posts: list[dict] = []

    async def get_one(self, request):
        return web.json_response(self.doc)

    async def get_list(self, request):
        return web.json_response({"data": [self.doc["data"]]})

    async def post_bypass(self, request):
        self.posts.append(await request.json())
        return web.json_response(self.doc)


class FakeTelus:
    def __init__(self, session):
        self.session = session
        self.api_headers = {"ajaxrequestuniquekey": "test"}


@contextlib.asynccontextmanager
async def portal_at(port, doc=None):
    import aiohttp

    portal = FakePortal(doc)
    app = web.Application()
    app.router.add_get("/web/api/devices/partitions", portal.get_list)
    app.router.add_get("/web/api/devices/partitions/{pid}", portal.get_one)
    app.router.add_post("/web/api/devices/partitions/{pid}/bypassSensors",
                        portal.post_bypass)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    saved = c.TELUS_BASE
    c.TELUS_BASE = f"http://127.0.0.1:{port}/"
    try:
        async with aiohttp.ClientSession() as session:
            yield portal, FakeTelus(session)
    finally:
        c.TELUS_BASE = saved
        await runner.cleanup()


async def expect_error(what, factory, *, unsupported):
    try:
        await factory()
    except p.PartitionError as err:
        assert err.unsupported is unsupported, (
            f"{what}: expected unsupported={unsupported}, got {err.unsupported} ({err})")
        return err
    raise AssertionError(f"{what}: expected PartitionError, none raised")


# --- pure helpers -----------------------------------------------------------
def test_ids_are_joined_and_a_pipe_inside_one_is_refused():
    """The separator is the whole wire format, so an id containing one would
    split server-side into two ids and bypass a sensor nobody named."""
    assert p._joined(["a", "b", "c"]) == "a|b|c"
    assert p._joined([]) == ""
    assert p._joined(None) == ""
    assert p._joined(["  a  "]) == "a"

    for bad in (["a|b"], ["a", "b|c"], [""], ["   "], [None], [3]):
        try:
            p._joined(bad)
        except p.PartitionError as err:
            assert err.unsupported is False, bad
        else:
            raise AssertionError(f"{bad!r} should have been refused")


def test_missing_flags_refuse_rather_than_permit():
    """An unknown permission is not permission, and an unknown "may I bypass
    while armed" is not a yes. Both default to the refusing value."""
    parsed = p._parse({"id": PID, "attributes": {}})
    assert parsed["has_permission_to_change_state"] is False, parsed
    assert parsed["can_bypass_when_armed"] is False, parsed
    # An unreported state is not "disarmed" either.
    assert parsed["is_disarmed"] is False, parsed


# --- behaviour --------------------------------------------------------------
async def test_a_disarmed_partition_accepts_the_bypass():
    async with portal_at(8601) as (portal, telus):
        result = await p.bypass_sensors(telus, PID, bypass=["100000000-1",
                                                            "100000000-3"])
        assert portal.posts == [{"bypass": "100000000-1|100000000-3",
                                 "unbypass": ""}], portal.posts
        assert result["id"] == PID and result["is_disarmed"] is True


async def test_an_armed_partition_that_forbids_it_is_refused_before_any_write():
    """The live case: canBypassSensorWhenArmed is false on the reference panel.

    Writing anyway would be asking a panel to stop watching a zone while the
    house is armed -- exactly the state in which that matters most.
    """
    async with portal_at(8602, partition_doc(state=ARMED_STAY)) as (portal, telus):
        await expect_error("armed",
                           lambda: p.bypass_sensors(telus, PID,
                                                    bypass=["100000000-1"]),
                           unsupported=True)
        assert portal.posts == [], "nothing may reach the panel"


async def test_an_armed_partition_that_allows_it_is_accepted():
    """...and the gate is the flag, not the arming state on its own."""
    doc = partition_doc(state=ARMED_STAY, canBypassSensorWhenArmed=True)
    async with portal_at(8603, doc) as (portal, telus):
        await p.bypass_sensors(telus, PID, bypass=["100000000-1"])
        assert len(portal.posts) == 1


async def test_no_permission_is_refused():
    doc = partition_doc(hasPermissionToChangeState=False)
    async with portal_at(8604, doc) as (portal, telus):
        await expect_error("no permission",
                           lambda: p.bypass_sensors(telus, PID,
                                                    bypass=["100000000-1"]),
                           unsupported=True)
        assert portal.posts == []


async def test_naming_nothing_is_a_caller_mistake_not_a_no_op():
    """An empty request must not reach a live alarm panel to achieve nothing."""
    async with portal_at(8605) as (portal, telus):
        await expect_error("empty", lambda: p.bypass_sensors(telus, PID),
                           unsupported=False)
        await expect_error("both empty",
                           lambda: p.bypass_sensors(telus, PID, bypass=[],
                                                    unbypass=[]),
                           unsupported=False)
        assert portal.posts == []


async def test_a_bad_id_is_refused_before_the_partition_is_even_read():
    """Validation is cheapest-first: an unusable id costs no upstream request."""
    async with portal_at(8606) as (portal, telus):
        await expect_error("piped id",
                           lambda: p.bypass_sensors(telus, PID,
                                                    bypass=["100000000-1|evil"]),
                           unsupported=False)
        assert portal.posts == []


async def test_unbypass_travels_in_its_own_field():
    async with portal_at(8607) as (portal, telus):
        await p.bypass_sensors(telus, PID, unbypass=["100000000-1"])
        assert portal.posts == [{"bypass": "", "unbypass": "100000000-1"}]


async def test_a_non_partition_id_is_refused():
    """A caller must not reach the bypass POST holding something else's id."""
    doc = {"data": {"id": PID, "type": "devices/sensor", "attributes": {}}}
    async with portal_at(8608, doc) as (portal, telus):
        await expect_error("not a partition",
                           lambda: p.bypass_sensors(telus, PID,
                                                    bypass=["100000000-1"]),
                           unsupported=False)
        assert portal.posts == []


async def test_the_list_route_reports_the_gates():
    async with portal_at(8609) as (_portal, telus):
        found = await p.get_partitions(telus)
        assert len(found) == 1, found
        assert set(found[0]) >= {"id", "partition_id", "description", "state",
                                 "is_disarmed", "has_permission_to_change_state",
                                 "can_bypass_when_armed",
                                 "has_open_bypassable_sensors"}, found[0]


async def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        try:
            if inspect.iscoroutinefunction(fn):
                await fn()
            else:
                fn()
        except AssertionError as exc:
            print(f"  FAIL {name}: {exc}")
            failures += 1
        else:
            print(f"  ok  {name}")
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
