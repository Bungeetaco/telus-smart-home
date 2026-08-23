"""Tests for the bluetooth arming-device client and its control routes
(bridge/arming_devices.py, the /control/arming-devices and
/control/bluetooth-disarming routes).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_arming_devices.py

No network and no live panel. Everything is driven by `fixtures/`, which are the
real captured documents from a browser session against a live TELUS/alarm.com
panel, with the account/unit/system ids, the MAC addresses and every device
description replaced:

    bluetooth_arming_device_get.json   what the server returned (JSON:API)
    bluetooth_arming_device_put.json   what the browser then sent back (FLAT)
    device_catalog_get.json            the manage-devices catalog (discovery)
    bluetooth_disarming_settings_get.json  the PANEL-level object (E41). A GET
        only: NO PUT to that endpoint was ever captured.

The rule from tests/test_camera_settings.py applies here unchanged: if the real
API's shape is in question, the fixtures answer it. Do not invent a payload to
make a test pass -- that is exactly how a settings client ships unable to talk to
the API at all, with fabricated fixtures agreeing with a fabricated model.

That rule is why there is no `bluetooth_disarming_settings_put.json`. The E41
write shape is INFERRED from the sibling endpoint's captured convention, and the
tests for it pin WHAT THIS CLIENT SENDS -- they are not, and must not be read as,
evidence about what that endpoint accepts. Only a live PUT settles that.

What is written here decides whether a paired phone walking up to the house
DISARMS THE ALARM, and which partition it disarms, and alarm.com is the
configuration authority for both (the panel's Control4 endpoint is
local-access-only and independent), so there is no second place a mistake is
corrected from. The properties pinned below all fail SILENTLY -- none of them
raises, none logs an error, each produces a well-formed HTTP exchange:

  1. The write is a FLAT object, not a JSON:API envelope.
  2. The resource type is PLURALIZED on the write, top level and every inlined
     relationship alike.
  3. A PUT REPLACES the record, so the ~30 attributes nobody asked to change must
     come back untouched -- including enrolledPartitionId, and including the
     panel document's enrolledDevices, whose omission would unpair every phone.
  4. A non-bool must be refused BEFORE any request. bool("false") is True, and
     isinstance(True, int) is True.
  5. An id that is not on this system must never produce a PUT -- and neither
     must a partition id this account does not have.
"""
import asyncio
import contextlib
import copy
import inspect
import json
import logging
import os
import pathlib
import sys
import tempfile

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c  # noqa: E402

# Redirected BEFORE create_app is ever called: _load_or_create_secret writes to
# these paths, and a test that rotated the real install's shared secret would
# silently 401 the running Home Assistant until someone restarted the bridge.
_TMP = tempfile.mkdtemp(prefix="telus-arming-test-")
c.SHARED_DIR = _TMP
c.CONTROL_SECRET_PATH = os.path.join(_TMP, "control_secret")

from bridge import arming_devices as ad     # noqa: E402
from bridge import control_server as cs     # noqa: E402
from bridge.telus_auth import RateLimited   # noqa: E402

# Several tests provoke the error paths on purpose and the routes log each at
# warning/exception level. Left alone, a PASSING run prints tracebacks that read
# like failures. Nothing here asserts on log output.
logging.getLogger("bridge.control_server").setLevel(logging.CRITICAL)
logging.getLogger("bridge.arming_devices").setLevel(logging.CRITICAL)

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
SYSTEM = "20000000"
DEV = "100000000-5264"          # the one the capture has a GET/PUT pair for
DEV_OTHER = "100000000-5263"    # catalog-only, proves discovery finds both
UNKNOWN = "100000000-9999"


def fixture(name):
    with open(FIXTURES / f"{name}.json") as fh:
        return json.load(fh)


# ---------------------------------------------------------------------------
# The captured shapes
# ---------------------------------------------------------------------------

def test_flatten_matches_the_browsers_own_request_body():
    """_flatten(GET) must reproduce what the browser PUT after that same GET.

    The single most load-bearing assertion in the file: it is the only thing
    standing between this client and silently sending a shape the endpoint does
    not accept.

    Compared by intersection rather than by an exact key-set match, unlike
    test_camera_settings' equivalent, because this document has a KNOWN delta --
    pinned separately in the next test so it can never widen unnoticed.
    """
    mine = ad._flatten(fixture("bluetooth_arming_device_get"))
    theirs = fixture("bluetooth_arming_device_put")

    assert mine.get("id") == theirs["id"] == DEV
    assert mine.get("type") == theirs["type"] == "devices/bluetooth-arming-devices", (
        f"top-level type is {mine.get('type')!r}, the browser sends "
        f"{theirs['type']!r} -- the write side PLURALIZES it")

    for key, value in theirs.items():
        # webSettings is the one attribute whose VALUE legitimately differs --
        # the browser stringifies it. Pinned on its own, below.
        if key not in mine or isinstance(value, dict) or key == "webSettings":
            continue
        assert mine[key] == value, f"{key}: {mine[key]!r} != the browser's {value!r}"

    # The relationships are inlined by NAME as {type, id} stubs, with the type
    # pluralized there too. `included` is empty in this capture, so a client that
    # only inlined resources it found in `included` would drop both -- and a
    # whole-document PUT that drops a relationship deletes it.
    assert mine["system"] == {"type": "systems/systems", "id": SYSTEM}, mine.get("system")
    assert mine["stateInfo"] == {"type": "devices/state-infos", "id": f"{DEV}-0"}, (
        mine.get("stateInfo"))
    for key in ("system", "stateInfo"):
        assert theirs[key]["type"] == mine[key]["type"], f"{key}.type"


def test_the_known_delta_from_the_browsers_body_is_exactly_this():
    """Pin the two-way difference so a change to _flatten is never silent.

    The browser holds a richer Ember model than one response carries. This client
    resends precisely what the server just said instead, because inventing a
    `state` for a record whose `hasState` is false is a guess and a guess is the
    one thing a whole-document PUT against an alarm panel must not contain.
    Neither direction is verified against the live endpoint; if a live test ever
    shows the endpoint needs the browser's extras, change _flatten and this test
    together -- do not quietly widen it.
    """
    mine = ad._flatten(fixture("bluetooth_arming_device_get"))
    theirs = fixture("bluetooth_arming_device_put")
    assert set(theirs) - set(mine) == {"state", "desiredState", "isRefreshingState"}, (
        sorted(set(theirs) - set(mine)))
    assert set(mine) - set(theirs) == {"deviceModelId", "primaryAssociatedDeviceIds"}, (
        sorted(set(mine) - set(theirs)))
    # ...and inside deviceIcon, the browser adds a cached label. `icon` matches.
    assert theirs["deviceIcon"]["icon"] == mine["deviceIcon"]["icon"] == 324


def test_websettings_int_string_discrepancy_is_real_and_we_resend_the_int():
    """Observed: the read returns the INTEGER 7205, the browser sends the STRING.

    Documented rather than "fixed" -- nothing establishes which form the endpoint
    requires, so the client echoes what the server said. If this ever has to
    change, it changes because a live test showed the integer being rejected.
    """
    got = fixture("bluetooth_arming_device_get")["data"]["attributes"]["webSettings"]
    sent = fixture("bluetooth_arming_device_put")["webSettings"]
    assert got == 7205 and isinstance(got, int)
    assert sent == "7205" and isinstance(sent, str)
    assert ad._flatten(fixture("bluetooth_arming_device_get"))["webSettings"] == 7205


def test_write_type_pluralizes_only_what_needs_it():
    assert ad._write_type("devices/bluetooth-arming-device") == \
        "devices/bluetooth-arming-devices"
    assert ad._write_type("systems/system") == "systems/systems"
    # Already plural: left alone, not doubled.
    assert ad._write_type("devices/state-infos") == "devices/state-infos"


def test_capability_is_built_from_flags_that_actually_discriminate():
    """supports_disarming_toggle, and the evidence behind each half of it.

    canBeSaved is not a constant in this API: the SAME catalog carries it true on
    the bluetooth arming devices and false on the mobile-device records. That is
    what makes it a capability flag rather than decoration.
    """
    catalog = fixture("device_catalog_get")
    saveable = {res["type"]: res["attributes"].get("canBeSaved")
                for res in catalog["included"] if "attributes" in res}
    assert saveable.get("devices/bluetooth-arming-device") is True
    assert saveable.get("devices/mobile-device") is False, (
        "canBeSaved no longer discriminates in this fixture -- the capability "
        "gate's evidence is gone, re-derive it before trusting it")

    attrs = ad._flatten(fixture("bluetooth_arming_device_get"))
    assert ad._supports_toggle(attrs) is True

    # Each half of the gate, alone, is enough to refuse. Absent counts as no.
    for key in ("canBeSaved", "canAccessWebSettings"):
        for bad in (False, None):
            assert ad._supports_toggle({**attrs, key: bad}) is False, (key, bad)
        assert ad._supports_toggle({k: v for k, v in attrs.items() if k != key}) is False

    # A missing or non-bool toggle value means this parser is looking at a record
    # shape it does not understand -- not a record to start writing to.
    assert ad._supports_toggle({k: v for k, v in attrs.items()
                                if k != "isDisarmingEnabledForDevice"}) is False
    assert ad._supports_toggle({**attrs, "isDisarmingEnabledForDevice": "true"}) is False


def test_panel_disarming_settings_is_a_separate_feature_not_a_gate():
    """Why bluetoothDisarmingSettings is NOT consulted by this module.

    It is a panel-level object with its own writable state -- a pairing switch
    and a timeout -- plus a relationship listing the enrolled devices. The
    portal's own page for it saves it as its own record, and the per-device
    settings page renders its toggle without reading it. Nothing in the capture
    makes it a capability gate for the per-device toggle, so treating it as one
    would invent a veto the API does not have.

    Kept as a fixture (and as this test) so the decision is recorded with its
    evidence rather than rediscovered from scratch.
    """
    panel = fixture("bluetooth_disarming_settings_get")["data"]
    assert panel["type"] == "settings/panel-bluetooth/bluetooth-disarming-setting"
    attrs = panel["attributes"]
    # A pairing switch, not a disarming-permission flag, plus a bare `timeout`
    # whose UNIT IS NOT ESTABLISHED -- see the E41 tests below. (An earlier
    # version of this comment said "minutes", which came from the backlog entry
    # and not from anything in the payload.)
    assert set(attrs) == {"deviceId", "isBluetoothPairingEnabledForSystem", "timeout"}
    enrolled = panel["relationships"]["enrolledDevices"]["data"]
    assert {d["id"] for d in enrolled} == {DEV, DEV_OTHER}
    assert all(d["type"] == "devices/bluetooth-arming-device" for d in enrolled)


# ---------------------------------------------------------------------------
# Fake portal
# ---------------------------------------------------------------------------
class FakePortal:
    """Stand-in for the alarm.com website device API.

    Serves the captured documents, records every request body, and -- crucially
    -- REJECTS a PUT that arrives in a JSON:API envelope, the way a server that
    only speaks the flat form would. A fake that accepted both is what lets the
    wrong write shape ship.
    """

    def __init__(self):
        self.devices = {DEV: fixture("bluetooth_arming_device_get")}
        self.catalog = fixture("device_catalog_get")
        self.panel = fixture("bluetooth_disarming_settings_get")
        self.writes: list[tuple[str, dict]] = []
        self.panel_writes: list[dict] = []
        self.reads = 0
        self.catalog_reads = 0
        self.panel_reads = 0
        self.deletes: list[str] = []

    async def delete_device(self, request):
        did = request.match_info["did"]
        self.deletes.append(did)
        if did not in self.devices:
            return web.json_response({"error": "not found"}, status=404)
        del self.devices[did]
        return web.json_response({}, status=204)

    async def get_panel(self, request):
        self.panel_reads += 1
        if request.match_info["sid"] != SYSTEM:
            return web.json_response({"error": "no such system"}, status=404)
        return web.json_response(copy.deepcopy(self.panel))

    async def put_panel(self, request):
        """The PANEL-level PUT, which NOTHING in the capture ever performed.

        This fake is therefore not evidence about the real endpoint and must not
        be read as any -- it exists to prove the client sends the shape it says
        it sends, not that the shape is right. It rejects a JSON:API envelope for
        the same reason the device fake does, and echoes the document back.
        """
        body = await request.json()
        if "data" in body and isinstance(body["data"], dict) and "attributes" in body["data"]:
            return web.json_response({"error": "this endpoint takes a flat object"},
                                     status=400)
        self.panel_writes.append(body)
        attrs = {k: v for k, v in body.items()
                 if k not in ("id", "type", "enrolledDevices")}
        return web.json_response({
            "data": {"id": body.get("id", SYSTEM),
                     "type": "settings/panel-bluetooth/bluetooth-disarming-setting",
                     "attributes": attrs,
                     "relationships": {"enrolledDevices": {
                         "data": [{"type": "devices/bluetooth-arming-device",
                                   "id": m["id"]}
                                  for m in body.get("enrolledDevices") or []]}}},
            "included": []})

    async def get_catalog(self, request):
        self.catalog_reads += 1
        if request.match_info["sid"] != SYSTEM:
            return web.json_response({"error": "no such system"}, status=404)
        return web.json_response(copy.deepcopy(self.catalog))

    async def get_device(self, request):
        self.reads += 1
        doc = self.devices.get(request.match_info["did"])
        if doc is None:
            return web.json_response({"error": "no such device"}, status=404)
        return web.json_response(copy.deepcopy(doc))

    async def put_device(self, request):
        did = request.match_info["did"]
        body = await request.json()
        if "data" in body and isinstance(body["data"], dict) and "attributes" in body["data"]:
            return web.json_response({"error": "this endpoint takes a flat object"},
                                     status=400)
        if did not in self.devices:
            return web.json_response({"error": "no such device"}, status=404)
        self.writes.append((did, body))
        # Echo the record back as JSON:API the way the real endpoint does, rather
        # than a bare acknowledgement: the setter parses this reply into its
        # return value, so a fake answering with an empty document would silently
        # exercise only the fallback path.
        # A dict value is a RELATIONSHIP only if it carries type+id. Some plain
        # attributes are objects too (`deviceIcon`, and `associatedCameraDeviceIds`
        # which is {}), and treating those as relationships is how a parser ends
        # up calling the type-pluralizer on a None.
        def _is_rel(value):
            return isinstance(value, dict) and "type" in value and "id" in value

        attrs = {k: v for k, v in body.items()
                 if k not in ("id", "type") and not _is_rel(v)}
        rels = {key: {"data": {"type": value["type"], "id": value["id"]}}
                for key, value in body.items() if _is_rel(value)}
        return web.json_response({
            "data": {"id": did, "type": "devices/bluetooth-arming-device",
                     "attributes": attrs, "relationships": rels},
            "included": []})

    def last(self, did=DEV):
        for written, body in reversed(self.writes):
            if written == did:
                return body
        raise AssertionError(f"no PUT was made to {did}")


class FakeTelus:
    """The two things arming_devices uses off a TelusSession.

    api_headers carries Context-Id because that is where this module reads the
    system id from -- deliberately, rather than reaching into the session's
    private _context_id.
    """

    def __init__(self, session, context_id=SYSTEM):
        self.session = session
        self.api_headers = {"ajaxrequestuniquekey": "test"}
        if context_id is not None:
            self.api_headers["Context-Id"] = context_id


@contextlib.asynccontextmanager
async def portal_at(port, context_id=SYSTEM):
    import aiohttp

    portal = FakePortal()
    app = web.Application()
    app.router.add_get("/web/api/devices/bluetoothArmingDevices/{did}", portal.get_device)
    app.router.add_put("/web/api/devices/bluetoothArmingDevices/{did}", portal.put_device)
    app.router.add_delete("/web/api/devices/bluetoothArmingDevices/{did}",
                          portal.delete_device)
    app.router.add_get("/web/api/settings/manageDevices/deviceCatalogs/{sid}",
                       portal.get_catalog)
    app.router.add_get(
        "/web/api/settings/panelBluetooth/bluetoothDisarmingSettings/{sid}",
        portal.get_panel)
    app.router.add_put(
        "/web/api/settings/panelBluetooth/bluetoothDisarmingSettings/{sid}",
        portal.put_panel)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    saved = c.TELUS_BASE
    c.TELUS_BASE = f"http://127.0.0.1:{port}/"
    try:
        async with aiohttp.ClientSession() as session:
            yield portal, FakeTelus(session, context_id)
    finally:
        c.TELUS_BASE = saved
        await runner.cleanup()


async def expect_error(what, factory, *, unsupported):
    try:
        await factory()
    except ad.ArmingDeviceError as err:
        assert err.unsupported is unsupported, (
            f"{what}: expected unsupported={unsupported}, got {err.unsupported} ({err})")
        return err
    raise AssertionError(f"{what}: expected ArmingDeviceError, none raised")


# ---------------------------------------------------------------------------
# Client round trips
# ---------------------------------------------------------------------------

async def test_discovery_finds_both_devices_in_one_request():
    """The catalog inlines each device's full record, so the whole list is ONE
    upstream call rather than one per device against an account whose rate limits
    are undocumented and can lock it."""
    async with portal_at(8541) as (portal, telus):
        devices = await ad.get_arming_devices(telus)
        assert portal.catalog_reads == 1 and portal.reads == 0
        assert [d["id"] for d in devices] == [DEV_OTHER, DEV], devices
        assert devices[1]["description"] == "Sam’s iPhone"
        assert devices[1]["disarming_enabled"] is True
        # A string identifier, not coerced to an int -- the write has to send back
        # exactly what it read.
        assert devices[1]["enrolled_partition_id"] == "1"
        assert all(d["supports_disarming_toggle"] is True for d in devices)


async def test_discovery_filters_on_resource_type_not_on_the_category():
    """The catalog carries sensors, locks, a partition, cameras and the account's
    MOBILE devices too. Only the bluetooth-arming-device type may come back -- a
    mobile-device record looks superficially similar (same managed-device
    attributes, same deviceIcon) and is not one of these."""
    async with portal_at(8542) as (portal, telus):
        devices = await ad.get_arming_devices(telus)
        catalog_types = {res["type"] for res in portal.catalog["included"]}
        assert len(catalog_types) > 3, catalog_types
        assert {d["id"] for d in devices} == {DEV, DEV_OTHER}


async def test_system_id_comes_from_the_sessions_public_headers():
    """Resolved from api_headers' Context-Id, so a re-login that changed systems
    is picked up rather than cached -- and an explicit argument still wins."""
    async with portal_at(8543) as (portal, telus):
        await ad.get_arming_devices(telus)
        assert portal.catalog_reads == 1
        await ad.get_arming_devices(telus, SYSTEM)
        assert portal.catalog_reads == 2

    async with portal_at(8544, context_id=None) as (portal, telus):
        await expect_error("no context id",
                           lambda: ad.get_arming_devices(telus), unsupported=False)
        assert portal.catalog_reads == 0, "must not even try without a system id"


async def test_write_is_flat_and_resends_every_other_attribute_untouched():
    """The whole point of read-modify-write: one field changes, nothing else.

    Diffed against the client's OWN read rather than against the fixture, so the
    assertion is "you sent back what you were given, plus your one edit" -- which
    is what protects the rest of the record, including enrolledPartitionId, on a
    live panel.
    """
    async with portal_at(8545) as (portal, telus):
        before = ad._flatten(portal.devices[DEV])
        result = await ad.set_disarming_enabled(telus, DEV, False)
        body = portal.last()

        # Flat: the JSON:API envelope keys must not appear at the top level. The
        # fake 400s on one anyway, so reaching here already proves it.
        assert "data" not in body and "attributes" not in body
        assert body["type"] == "devices/bluetooth-arming-devices"
        assert body["isDisarmingEnabledForDevice"] is False

        changed = {k for k in set(before) | set(body) if before.get(k) != body.get(k)}
        assert changed == {"isDisarmingEnabledForDevice"}, (
            f"a whole-record PUT changed more than the one field asked for: {changed}")
        # ~30 attributes, not a handful -- if this collapses, the read-modify-write
        # has quietly become a partial body.
        assert len(body) >= 30, sorted(body)
        assert body["enrolledPartitionId"] == "1"
        assert result["disarming_enabled"] is False


async def test_enabling_and_disabling_both_reach_the_wire():
    async with portal_at(8546) as (portal, telus):
        for wanted in (True, False, True):
            result = await ad.set_disarming_enabled(telus, DEV, wanted)
            assert portal.last()["isDisarmingEnabledForDevice"] is wanted
            assert result["disarming_enabled"] is wanted
        # Setting the value it already has still writes: the caller asked for that
        # state and a silent no-op is indistinguishable from a dropped write.
        assert len(portal.writes) == 3


async def test_a_non_bool_is_refused_before_any_request():
    """bool("false") is True, and this field decides whether a phone disarms the
    alarm. Note reads == 0: the refusal happens before the GET, not after it."""
    async with portal_at(8547) as (portal, telus):
        for bad in ("true", "false", 1, 0, None, [], {"enabled": True}):
            await expect_error(f"enabled={bad!r}",
                               lambda b=bad: ad.set_disarming_enabled(telus, DEV, b),
                               unsupported=False)
        assert portal.writes == []
        assert portal.reads == 0, "a bad value must not even cost a read"


async def test_an_unknown_device_id_never_produces_a_put():
    """A PUT here REPLACES the record, so a typo'd id must never reach one."""
    async with portal_at(8548) as (portal, telus):
        await expect_error("unknown id",
                           lambda: ad.set_disarming_enabled(telus, UNKNOWN, True),
                           unsupported=False)
        assert portal.writes == []
        await expect_error("unknown id read",
                           lambda: ad.get_arming_device(telus, UNKNOWN),
                           unsupported=False)


async def test_a_record_that_cannot_be_saved_is_unsupported_and_is_not_written():
    """The capability gate, end to end. `unsupported` is what separates "this
    record cannot do it" (422) from "you asked wrong" (400), so it is asserted
    rather than just the raise."""
    async with portal_at(8549) as (portal, telus):
        portal.devices[DEV]["data"]["attributes"]["canBeSaved"] = False
        await expect_error("canBeSaved false",
                           lambda: ad.set_disarming_enabled(telus, DEV, True),
                           unsupported=True)
        assert portal.writes == []
        assert (await ad.get_arming_device(telus, DEV))["supports_disarming_toggle"] \
            is False


async def test_auth_failures_are_left_unwrapped_for_the_relogin_path():
    """401/403 must escape as aiohttp.ClientResponseError, because
    control_server._fetch_with_relogin keys its one-shot re-login off that exact
    type and status. Wrapping an expired cookie here turns a recoverable session
    expiry into a hard upstream failure -- and 429 must become RateLimited so the
    caller sleeps the server's own wait instead of walking back into the edge."""
    import aiohttp
    from bridge.telus_auth import RateLimited

    class _Resp:
        def __init__(self, status, headers=None):
            self.status = status
            self.headers = headers or {}
            self.url = type("U", (), {"path": "/web/api/devices/x"})()

        def raise_for_status(self):
            raise aiohttp.ClientResponseError(None, (), status=self.status)

    for status in (401, 403):
        try:
            ad._raise_for_status(_Resp(status))
        except aiohttp.ClientResponseError as err:
            assert err.status == status
        else:
            raise AssertionError(f"{status} must stay a ClientResponseError")

    try:
        ad._raise_for_status(_Resp(429, {"Retry-After": "37"}))
    except RateLimited as err:
        assert err.retry_after_s == 37, err.retry_after_s
    else:
        raise AssertionError("429 must become RateLimited")

    try:
        ad._raise_for_status(_Resp(500))
    except ad.ArmingDeviceError as err:
        assert err.unsupported is False
        assert "smarthome" not in str(err), "the message must not carry the full URL"
    else:
        raise AssertionError("5xx must become ArmingDeviceError")


async def test_enrolled_partition_write_changes_only_that_field():
    """E43. Same read-modify-write as the disarming toggle, same protection.

    Diffed against the client's own read, so the assertion is "you sent back what
    you were given, plus your one edit" -- which is what keeps
    isDisarmingEnabledForDevice (and the other ~30 attributes) intact while
    moving a phone between partitions.
    """
    async with portal_at(8551) as (portal, telus):
        before = ad._flatten(portal.devices[DEV])
        assert before["enrolledPartitionId"] == "1"

        result = await ad.set_enrolled_partition(telus, DEV, "2")
        body = portal.last()
        assert "data" not in body and "attributes" not in body
        changed = {k for k in set(before) | set(body) if before.get(k) != body.get(k)}
        assert changed == {"enrolledPartitionId"}, changed
        assert body["enrolledPartitionId"] == "2"
        # In particular the disarming flag is untouched: these two writes share
        # one endpoint and one whole-record PUT, so a partition change that also
        # flipped the disarming toggle would be indistinguishable from a
        # deliberate one.
        assert body["isDisarmingEnabledForDevice"] is True
        assert result["enrolled_partition_id"] == "2"
        assert result["supports_enrolled_partition"] is True


async def test_enrolled_partition_refuses_a_non_string_before_any_request():
    """It is an IDENTIFIER. str(1) would look harmless and would change the JSON
    type of a field on a whole-record PUT to a live alarm panel."""
    async with portal_at(8552) as (portal, telus):
        for bad in (1, 1.0, True, None, "", "   ", ["1"], {"partition_id": "1"}):
            await expect_error(f"partition_id={bad!r}",
                               lambda b=bad: ad.set_enrolled_partition(telus, DEV, b),
                               unsupported=False)
        assert portal.writes == []
        assert portal.reads == 0, "a bad value must not even cost a read"


async def test_enrolled_partition_capability_is_its_own_gate():
    """Gated on enrolledPartitionId, NOT on the disarming flag.

    A record can be missing either one, and gating the partition write on the
    other field would offer the control on the strength of something unrelated.
    The two record-level flags are shared, because there is one save.
    """
    attrs = ad._flatten(fixture("bluetooth_arming_device_get"))
    assert ad._supports_enrolled_partition(attrs) is True

    # The record-level flags, shared with the disarming toggle. Absent counts as no.
    for key in ("canBeSaved", "canAccessWebSettings"):
        for bad in (False, None):
            assert ad._supports_enrolled_partition({**attrs, key: bad}) is False
        assert ad._supports_enrolled_partition(
            {k: v for k, v in attrs.items() if k != key}) is False

    # The field itself: a non-empty string or nothing doing.
    for bad in (1, None, "", "  ", True):
        assert ad._supports_enrolled_partition(
            {**attrs, "enrolledPartitionId": bad}) is False, bad

    # The two gates are independent in BOTH directions.
    no_toggle = {k: v for k, v in attrs.items() if k != "isDisarmingEnabledForDevice"}
    assert ad._supports_toggle(no_toggle) is False
    assert ad._supports_enrolled_partition(no_toggle) is True
    no_partition = {k: v for k, v in attrs.items() if k != "enrolledPartitionId"}
    assert ad._supports_toggle(no_partition) is True
    assert ad._supports_enrolled_partition(no_partition) is False

    async with portal_at(8553) as (portal, telus):
        portal.devices[DEV]["data"]["attributes"]["enrolledPartitionId"] = None
        await expect_error("no enrolled partition",
                           lambda: ad.set_enrolled_partition(telus, DEV, "2"),
                           unsupported=True)
        assert portal.writes == []


# ---------------------------------------------------------------------------
# E41 -- the PANEL-level document. THE WRITE SHAPE HERE IS INFERRED.
# ---------------------------------------------------------------------------

async def test_panel_read_reports_the_captured_document():
    """The one part of E41 that rests on captured evidence."""
    async with portal_at(8554) as (portal, telus):
        settings = await ad.get_bluetooth_disarming_settings(telus)
        assert portal.panel_reads == 1
        assert settings["id"] == SYSTEM
        assert settings["pairing_enabled"] is True
        assert settings["supports_pairing_enabled"] is True
        assert settings["timeout"] == 5
        assert settings["supports_timeout"] is True
        assert set(settings["enrolled_device_ids"]) == {DEV, DEV_OTHER}
        # Scoped to the session's own system, like the catalog read.
        assert (await ad.get_bluetooth_disarming_settings(telus, SYSTEM))["id"] == SYSTEM

    async with portal_at(8555, context_id=None) as (portal, telus):
        await expect_error("no context id",
                           lambda: ad.get_bluetooth_disarming_settings(telus),
                           unsupported=False)
        assert portal.panel_reads == 0


def test_the_timeout_unit_and_options_come_from_the_portal_not_the_payload():
    """The panel document says nothing about what `timeout` means.

    No unit field, no bounds, no option list, and the only observed value is 5 --
    so a client reading only the payload cannot label it or know what it may set
    it to. Both come from the portal's own disarming-settings controller, which
    builds its picker as `[1, 5, 10, 20, 30].map(n => intl.t("generic.minutes",
    {num: n}))` and renders the current value through the same string.

    That is why the bridge reports them: they are knowledge a caller has no way
    to derive, and the alternative -- every client inventing its own label and
    range -- is how a number ends up presented in the wrong unit.
    """
    attrs = fixture("bluetooth_disarming_settings_get")["data"]["attributes"]
    assert set(attrs) == {"deviceId", "isBluetoothPairingEnabledForSystem", "timeout"}
    assert isinstance(attrs["timeout"], int)
    # Still nothing in the payload -- which is the point.
    for suffix in ("Min", "Max", "Options", "Unit", "Units", "Seconds", "Minutes"):
        assert not any(key.endswith(suffix) for key in attrs), (suffix, sorted(attrs))

    parsed = ad._parse_panel(ad._flatten(fixture("bluetooth_disarming_settings_get")))
    assert parsed["timeout_unit"] == "minutes", parsed
    assert parsed["timeout_options"] == [1, 5, 10, 20, 30], parsed
    # No min/max: it is a set, not a range, and presenting it as a range would
    # offer windows the vendor does not.
    assert "timeout_min" not in parsed and "timeout_max" not in parsed, parsed


async def test_unpair_names_the_phone_and_refuses_an_id_it_cannot_find():
    """Unpairing has NO undo from this side -- re-pairing needs the phone and the
    panel in hand -- so every refusal here matters more than the success path.

    The description is read BEFORE the delete: afterwards the record is gone and
    a caller could not say which phone it removed.
    """
    async with portal_at(8561) as (portal, telus):
        result = await ad.unpair_arming_device(telus, DEV)
        assert portal.deletes == [DEV], portal.deletes
        assert result["unpaired"] == DEV
        assert result["description"], "the name must survive the delete"

    async with portal_at(8562) as (portal, telus):
        # An id the catalog does not know must never become a DELETE aimed at
        # whatever that id addresses. The ordinary cause is a typo; the
        # consequence is not recoverable.
        await expect_error("unknown id",
                           lambda: ad.unpair_arming_device(telus, "100000000-9999"),
                           unsupported=False)
        assert portal.deletes == [], "nothing may be deleted on a miss"


async def test_unpair_is_one_device_and_there_is_no_delete_all():
    """The vendor's portal also offers a panel-wide deleteAll. It is deliberately
    not implemented at any level: one mis-aimed call would unpair every phone in
    the household at once, with the same absence of an undo."""
    import inspect
    source = inspect.getsource(ad)
    assert "deleteAll" not in source.replace("``devices/bluetooth-arming-devices/deleteAll``", ""), \
        "a delete-all path appeared in arming_devices"
    assert not any(name.lower().startswith("unpair_all") or "delete_all" in name.lower()
                   for name in dir(ad)), dir(ad)


async def test_panel_write_is_flat_pluralized_and_whole_document():
    """What this client SENDS. Not evidence about what the endpoint ACCEPTS.

    No PUT to this endpoint was ever captured, so this pins the reconstruction --
    the flat body, the pluralized type, every attribute resent, and the
    enrolledDevices relationship preserved -- so that it cannot drift silently
    before someone gets to test it against a real panel.
    """
    async with portal_at(8556) as (portal, telus):
        before = ad._flatten(portal.panel)
        result = await ad.set_bluetooth_disarming_settings(telus,
                                                           pairing_enabled=False)
        body = portal.panel_writes[-1]

        assert "data" not in body and "attributes" not in body
        assert body["type"] == \
            "settings/panel-bluetooth/bluetooth-disarming-settings", body["type"]
        assert body["id"] == SYSTEM
        assert body["isBluetoothPairingEnabledForSystem"] is False

        changed = {k for k in set(before) | set(body) if before.get(k) != body.get(k)}
        assert changed == {"isBluetoothPairingEnabledForSystem"}, changed
        # deviceId is resent as the INTEGER the server sent, not stringified --
        # same policy as webSettings on the per-device endpoint.
        assert body["deviceId"] == 127 and isinstance(body["deviceId"], int)
        assert body["timeout"] == 5

        # The to-many relationship: preserved, inlined by name, every member
        # pluralized. THIS IS THE GUESS. Dropping it is not an option -- a
        # whole-document PUT that omits a relationship deletes it, and deleting
        # this one unpairs every phone on the system with no undo from here
        # (backlog E42) -- but the LIST-of-stubs shape is extrapolated from the
        # to-one relationships that were captured, not observed.
        assert [m["id"] for m in body["enrolledDevices"]] == [DEV, DEV_OTHER]
        assert all(m["type"] == "devices/bluetooth-arming-devices"
                   for m in body["enrolledDevices"]), body["enrolledDevices"]

        assert result["pairing_enabled"] is False

        # Both fields at once, and the timeout alone.
        await ad.set_bluetooth_disarming_settings(telus, pairing_enabled=True,
                                                  timeout=10)
        assert portal.panel_writes[-1]["timeout"] == 10
        assert portal.panel_writes[-1]["isBluetoothPairingEnabledForSystem"] is True
        await ad.set_bluetooth_disarming_settings(telus, timeout=1)
        assert portal.panel_writes[-1]["timeout"] == 1


async def test_panel_write_refuses_wrong_types_and_an_empty_request():
    """bool("false") is True and isinstance(True, int) is True -- both traps are
    closed BEFORE the read, so a bad value never costs a request."""
    async with portal_at(8557) as (portal, telus):
        for kwargs in ({"pairing_enabled": "true"}, {"pairing_enabled": 1},
                       {"pairing_enabled": 0}, {"timeout": True},
                       {"timeout": "5"}, {"timeout": 5.0}, {}):
            await expect_error(f"panel write {kwargs}",
                               lambda k=kwargs: ad.set_bluetooth_disarming_settings(
                                   telus, **k),
                               unsupported=False)
        assert portal.panel_writes == []
        assert portal.panel_reads == 0


async def test_panel_write_is_gated_on_the_field_being_there_at_all():
    """This document publishes NO capability flag -- no FeatureSupport enum, no
    canSetX, no canBeSaved. The only gate available is "the field is present and
    holds the type this client understands", and it must fail closed.

    Each field is gated independently: a panel that reports the pairing flag but
    no timeout must still let the pairing flag be written.
    """
    async with portal_at(8558) as (portal, telus):
        del portal.panel["data"]["attributes"]["timeout"]
        await expect_error("no timeout field",
                           lambda: ad.set_bluetooth_disarming_settings(telus,
                                                                       timeout=5),
                           unsupported=True)
        assert portal.panel_writes == []
        # ...and the other field still works.
        await ad.set_bluetooth_disarming_settings(telus, pairing_enabled=False)
        assert portal.panel_writes[-1]["isBluetoothPairingEnabledForSystem"] is False

        # A mixed request is refused WHOLE: both fields are checked before
        # anything is mutated, so a partly-unsupported request leaves the panel
        # untouched rather than half-applied.
        portal.panel_writes.clear()
        await expect_error("mixed request",
                           lambda: ad.set_bluetooth_disarming_settings(
                               telus, pairing_enabled=True, timeout=5),
                           unsupported=True)
        assert portal.panel_writes == []

    async with portal_at(8559) as (portal, telus):
        portal.panel["data"]["attributes"]["isBluetoothPairingEnabledForSystem"] = "true"
        await expect_error("non-bool pairing flag",
                           lambda: ad.set_bluetooth_disarming_settings(
                               telus, pairing_enabled=True),
                           unsupported=True)
        assert portal.panel_writes == []
        read = await ad.get_bluetooth_disarming_settings(telus)
        assert read["supports_pairing_enabled"] is False, read


async def test_panel_timeout_accepts_only_the_windows_the_vendor_offers():
    """A set, not a range. The panel publishes neither, so this client is the
    only thing between a caller and a window the vendor never offers.

    Refused as a caller mistake (400, not 422): the field is supported, the value
    just is not one of the five.
    """
    async with portal_at(8560) as (portal, telus):
        for value in (1, 5, 10, 20, 30):
            await ad.set_bluetooth_disarming_settings(telus, timeout=value)
            assert portal.panel_writes[-1]["timeout"] == value

        writes = len(portal.panel_writes)
        for value in (0, -1, 3, 15, 60, 3600):
            await expect_error(f"timeout={value}",
                               lambda v=value: ad.set_bluetooth_disarming_settings(
                                   telus, timeout=v),
                               unsupported=False)
        assert len(portal.panel_writes) == writes, (
            "a window the vendor does not offer must not reach the panel")


# ---------------------------------------------------------------------------
# Control routes
# ---------------------------------------------------------------------------
class FakeTelusSession:
    """Only what the routes touch. A re-login here would mean the route hit a
    401 path it was not supposed to."""

    session = object()

    async def login(self):
        raise AssertionError("no route under test may re-login")


def _partition(number, description):
    """One element of the shape partitions.get_partitions returns.

    `partition_id` is the panel's own partition NUMBER (an int here, as the
    upstream record reports it) and `id` is the alarm.com device id -- two
    DIFFERENT identifiers on the same record, and enrolledPartitionId is the
    former as a string. The stub keeps them distinguishable on purpose: a route
    that matched on `id` would build a picker whose every option it then refused.
    """
    return {"id": f"100000000-{126 + number}", "partition_id": number,
            "description": description, "state": 1, "is_disarmed": True,
            "has_permission_to_change_state": True, "can_bypass_when_armed": False,
            "has_open_bypassable_sensors": False, "raw": {}}


@contextlib.asynccontextmanager
async def routes_at(devices=None, setter=None, partitions=None, panel=None):
    """A live control server with bridge.arming_devices (and partitions) stubbed.

    control_server resolves both modules as globals at call time, so replacing
    the attributes is what a real request sees. They are swapped wholesale and
    restored in a finally, so the real modules are never left modified for
    whatever runs next in the same process.

    `partitions` defaults to a TWO-partition account, because that is the case
    where the enrolled-partition control is meaningful; single-partition and
    unreadable-partition-list accounts are exercised by passing them explicitly.
    """
    calls = {"list": 0, "set": [], "partitions": 0, "enroll": [],
             "panel_read": 0, "panel_write": []}
    if partitions is None:
        partitions = [_partition(1, "Home"), _partition(2, "Garage")]

    async def get_arming_devices(_telus, _system_id=None):
        calls["list"] += 1
        # An Exception passed as `devices` is RAISED rather than returned, which
        # is how the 429/502 mappings are exercised without a fake upstream.
        if isinstance(devices, Exception):
            raise devices
        return copy.deepcopy(devices)

    async def set_disarming_enabled(_telus, device_id, enabled):
        calls["set"].append((device_id, enabled))
        if isinstance(setter, Exception):
            raise setter
        return _device(device_id, disarming_enabled=enabled)

    async def set_enrolled_partition(_telus, device_id, partition_id):
        calls["enroll"].append((device_id, partition_id))
        if isinstance(setter, Exception):
            raise setter
        return _device(device_id, enrolled_partition_id=partition_id)

    async def get_bluetooth_disarming_settings(_telus, _system_id=None):
        calls["panel_read"] += 1
        if isinstance(panel, Exception):
            raise panel
        return _panel_settings()

    async def set_bluetooth_disarming_settings(_telus, **fields):
        calls["panel_write"].append(fields)
        if isinstance(panel, Exception):
            raise panel
        return _panel_settings(**fields)

    async def get_partitions(_telus):
        calls["partitions"] += 1
        if isinstance(partitions, Exception):
            raise partitions
        return copy.deepcopy(partitions)

    import types
    shim = types.SimpleNamespace(
        ArmingDeviceError=ad.ArmingDeviceError,
        get_arming_devices=get_arming_devices,
        set_disarming_enabled=set_disarming_enabled,
        set_enrolled_partition=set_enrolled_partition,
        get_bluetooth_disarming_settings=get_bluetooth_disarming_settings,
        set_bluetooth_disarming_settings=set_bluetooth_disarming_settings)
    saved = (cs.arming_devices, cs.partitions_mod)
    cs.arming_devices = shim
    cs.partitions_mod = types.SimpleNamespace(
        PartitionError=cs.partitions_mod.PartitionError,
        get_partitions=get_partitions)
    app = cs.create_app(FakeTelusSession(), asyncio.Lock(), {}, {}, None, None)
    headers = {"Authorization": f"Bearer {app[cs.APP_CURRENT_SECRET]['value']}"}
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, headers, calls
    finally:
        cs.arming_devices, cs.partitions_mod = saved


def _panel_settings(**over):
    settings = {"id": SYSTEM, "pairing_enabled": True,
                "supports_pairing_enabled": True, "timeout": 5,
                "supports_timeout": True, "timeout_unit": None,
                "enrolled_device_ids": [DEV, DEV_OTHER], "raw": {}}
    if "pairing_enabled" in over:
        settings["pairing_enabled"] = over["pairing_enabled"]
    if "timeout" in over:
        settings["timeout"] = over["timeout"]
    return settings


def _device(did=DEV, **over):
    dev = {"id": did, "description": "Sam’s iPhone", "disarming_enabled": True,
           "enrolled_partition_id": "1", "supports_disarming_toggle": True,
           "supports_enrolled_partition": True, "raw": {}}
    dev.update(over)
    return dev


async def test_route_lists_devices_and_requires_the_bearer_token():
    async with routes_at([_device(), _device(DEV_OTHER)]) as (client, headers, calls):
        assert (await client.get("/control/arming-devices")).status == 401
        resp = await client.get("/control/arming-devices", headers=headers)
        assert resp.status == 200
        body = await resp.json()
        assert [d["id"] for d in body["devices"]] == [DEV, DEV_OTHER]
        assert body["devices"][0]["supports_disarming_toggle"] is True
        assert calls["list"] == 1


async def test_route_write_applies_and_echoes_the_device():
    async with routes_at([_device()]) as (client, headers, calls):
        resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                 json={"enabled": False}, headers=headers)
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["disarming_enabled"] is False
        assert calls["set"] == [(DEV, False)]


async def test_route_refuses_a_non_bool_before_touching_upstream():
    """A truthy string is a 400, never a coercion -- and it must not even cost
    the pre-read, let alone a write."""
    async with routes_at([_device()]) as (client, headers, calls):
        for bad in ("true", "false", 1, 0, None, [], {}):
            resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                     json={"enabled": bad}, headers=headers)
            assert resp.status == 400, (bad, resp.status)
            assert (await resp.json())["error"] == "malformed"
        # A body with no "enabled" key at all, and one that is not JSON.
        for kwargs in ({"json": {}}, {"data": "not json"}):
            resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                     headers=headers, **kwargs)
            assert resp.status == 400, kwargs
        assert calls["set"] == [] and calls["list"] == 0


async def test_route_404s_an_unknown_device_instead_of_writing_to_it():
    """The membership check, which is the reason the pre-read exists at all: a
    PUT here replaces the whole record, so an id that is not on this system must
    never reach one."""
    async with routes_at([_device()]) as (client, headers, calls):
        resp = await client.post(f"/control/arming-devices/{UNKNOWN}/disarming",
                                 json={"enabled": True}, headers=headers)
        assert resp.status == 404
        assert (await resp.json())["error"] == "unknown_device"
        assert calls["list"] == 1 and calls["set"] == [], (
            "an unknown device id must never produce a write")


async def test_route_422s_a_device_that_reports_no_toggle():
    async with routes_at([_device(supports_disarming_toggle=False)]) as (
            client, headers, calls):
        resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                 json={"enabled": True}, headers=headers)
        assert resp.status == 422
        assert (await resp.json())["error"] == "unsupported"
        assert calls["set"] == []


async def test_route_maps_the_error_contract_the_way_the_settings_routes_do():
    """unsupported -> 422, everything else in ArmingDeviceError -> 400, upstream
    failure -> 502, edge rate limit -> 429 with the server's own Retry-After.

    The 429 in particular used to be answered as a 502 elsewhere in this API,
    which tells a caller "upstream broke" when the truth is "come back in N
    seconds" -- so it retried on its own schedule and walked straight back into
    the limiter."""
    from bridge.telus_auth import RateLimited

    async with routes_at([_device()],
                         setter=ad.ArmingDeviceError("nope", unsupported=True)) as (
            client, headers, _calls):
        resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                 json={"enabled": True}, headers=headers)
        assert resp.status == 422 and (await resp.json())["error"] == "unsupported"

    async with routes_at([_device()], setter=ad.ArmingDeviceError("bad value")) as (
            client, headers, _calls):
        resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                 json={"enabled": True}, headers=headers)
        assert resp.status == 400 and (await resp.json())["error"] == "malformed"

    async with routes_at([_device()], setter=RuntimeError("upstream")) as (
            client, headers, _calls):
        resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                 json={"enabled": True}, headers=headers)
        assert resp.status == 502 and (await resp.json())["error"] == "telus_error"

    async with routes_at([_device()], setter=RateLimited(42)) as (
            client, headers, _calls):
        resp = await client.post(f"/control/arming-devices/{DEV}/disarming",
                                 json={"enabled": True}, headers=headers)
        assert resp.status == 429
        assert resp.headers["Retry-After"] == "42"
        assert (await resp.json())["retry_after"] == 42

    async with routes_at(RateLimited(7)) as (client, headers, _calls):
        resp = await client.get("/control/arming-devices", headers=headers)
        assert resp.status == 429 and resp.headers["Retry-After"] == "7"

    async with routes_at(RuntimeError("upstream")) as (client, headers, _calls):
        resp = await client.get("/control/arming-devices", headers=headers)
        assert resp.status == 502 and (await resp.json())["error"] == "telus_error"


async def test_an_account_with_no_paired_phones_is_an_empty_list_not_a_404():
    async with routes_at([]) as (client, headers, calls):
        resp = await client.get("/control/arming-devices", headers=headers)
        assert resp.status == 200 and (await resp.json())["devices"] == []
        # ...and it costs no partition read: there is nothing to stamp them onto.
        assert calls["partitions"] == 0


# ---------------------------------------------------------------------------
# E43 -- the enrolled-partition route
# ---------------------------------------------------------------------------

async def test_every_arming_device_response_carries_the_partition_choices():
    """The list route AND both write routes, because the record is one contract.

    Not cosmetic: the HA side folds a write's echoed record straight back into
    what its entities read, so a reply missing the choices would empty the picker
    it had just been used from. That exact bug already happened once on the
    camera audio section's chime labels.
    """
    async with routes_at([_device(), _device(DEV_OTHER)]) as (client, headers, calls):
        body = await (await client.get("/control/arming-devices",
                                       headers=headers)).json()
        expected = [{"value": "1", "label": "Home"},
                    {"value": "2", "label": "Garage"}]
        assert all(d["enrolled_partition_choices"] == expected
                   for d in body["devices"]), body
        # The partition NUMBER, not the "<unit>-127" device id on the same record.
        assert calls["partitions"] == 1, "one read for the whole list, not one each"

        for url, payload in ((f"/control/arming-devices/{DEV}/disarming",
                              {"enabled": False}),
                             (f"/control/arming-devices/{DEV}/enrolled-partition",
                              {"partition_id": "2"})):
            echoed = await (await client.post(url, json=payload,
                                              headers=headers)).json()
            assert echoed["enrolled_partition_choices"] == expected, (url, echoed)


async def test_choices_degrade_to_empty_when_the_partition_read_fails():
    """A read that cannot list the partitions still returns the devices.

    Empty means "no picker", not "a picker with nothing in it" -- and it is
    deliberately NOT what the write route does with the same failure.
    """
    async with routes_at([_device()],
                         partitions=RuntimeError("upstream")) as (client, headers, _c):
        body = await (await client.get("/control/arming-devices",
                                       headers=headers)).json()
        assert body["devices"][0]["enrolled_partition_choices"] == []
        assert body["devices"][0]["supports_enrolled_partition"] is True


async def test_enrolled_partition_route_applies_and_echoes_the_device():
    async with routes_at([_device()]) as (client, headers, calls):
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "2"}, headers=headers)
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["enrolled_partition_id"] == "2"
        assert calls["enroll"] == [(DEV, "2")]


async def test_enrolled_partition_route_refuses_everything_it_is_unsure_of():
    """Four gates, cheapest first, and none of them may cost a write.

    The last one is the interesting one: a partition id that is not on this
    account is a 400 even though it is a perfectly well-formed string, because
    enrolledPartitionId is a bare panel-side number with no way to tell a valid
    one from a typo by looking at it.
    """
    async with routes_at([_device()]) as (client, headers, calls):
        # 1. Not a non-empty string. Never coerced -- and never reaching upstream.
        for bad in (1, 1.0, True, None, "", "   ", [], {}):
            resp = await client.post(
                f"/control/arming-devices/{DEV}/enrolled-partition",
                json={"partition_id": bad}, headers=headers)
            assert resp.status == 400, (bad, resp.status)
            assert (await resp.json())["error"] == "malformed"
        for kwargs in ({"json": {}}, {"data": "not json"}):
            resp = await client.post(
                f"/control/arming-devices/{DEV}/enrolled-partition",
                headers=headers, **kwargs)
            assert resp.status == 400, kwargs
        assert calls["list"] == 0 and calls["partitions"] == 0 and calls["enroll"] == []

        # 4. A well-formed id this account does not have.
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "99"}, headers=headers)
        assert resp.status == 400 and (await resp.json())["error"] == "malformed"
        assert calls["enroll"] == []
        # ...including the partition's DEVICE id, which is the plausible mix-up.
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "100000000-127"}, headers=headers)
        assert resp.status == 400, await resp.text()
        assert calls["enroll"] == []

    # 2. An id that is not on this system: a 404, never a blind PUT.
    async with routes_at([_device()]) as (client, headers, calls):
        resp = await client.post(
            f"/control/arming-devices/{UNKNOWN}/enrolled-partition",
            json={"partition_id": "2"}, headers=headers)
        assert resp.status == 404 and (await resp.json())["error"] == "unknown_device"
        assert calls["enroll"] == [] and calls["partitions"] == 0

    # 3. A record that says its partition cannot be written.
    async with routes_at([_device(supports_enrolled_partition=False)]) as (
            client, headers, calls):
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "2"}, headers=headers)
        assert resp.status == 422 and (await resp.json())["error"] == "unsupported"
        assert calls["enroll"] == []


async def test_an_unreadable_partition_list_stops_the_enrollment_write():
    """FAIL CLOSED, unlike the read. Not being able to confirm the partition
    exists is not a reason to write to it and hope."""
    async with routes_at([_device()],
                         partitions=RuntimeError("upstream")) as (client, headers, calls):
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "2"}, headers=headers)
        assert resp.status == 502 and (await resp.json())["error"] == "telus_error"
        assert calls["enroll"] == []

    async with routes_at([_device()], partitions=RateLimited(11)) as (
            client, headers, calls):
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "2"}, headers=headers)
        assert resp.status == 429 and resp.headers["Retry-After"] == "11"
        assert calls["enroll"] == []

    # An account that genuinely reports no partitions is indistinguishable from a
    # failed read, and both must stop the write.
    async with routes_at([_device()], partitions=[]) as (client, headers, calls):
        resp = await client.post(
            f"/control/arming-devices/{DEV}/enrolled-partition",
            json={"partition_id": "1"}, headers=headers)
        assert resp.status == 400
        assert calls["enroll"] == []


# ---------------------------------------------------------------------------
# E41 -- the panel-level routes
# ---------------------------------------------------------------------------

async def test_bluetooth_disarming_route_reads_and_writes():
    async with routes_at([_device()]) as (client, headers, calls):
        assert (await client.get("/control/bluetooth-disarming")).status == 401

        body = await (await client.get("/control/bluetooth-disarming",
                                       headers=headers)).json()
        assert body["pairing_enabled"] is True and body["timeout"] == 5
        # Published as NULL, on purpose: the unit was never established.
        assert body["timeout_unit"] is None
        assert calls["panel_read"] == 1

        resp = await client.post("/control/bluetooth-disarming",
                                 json={"pairing_enabled": False, "timeout": 10},
                                 headers=headers)
        assert resp.status == 200, await resp.text()
        assert (await resp.json())["timeout"] == 10
        assert calls["panel_write"] == [{"pairing_enabled": False, "timeout": 10}]
        # No pre-read: there is no membership question here, and a second upstream
        # call per write would check nothing.
        assert calls["panel_read"] == 1


async def test_bluetooth_disarming_route_refuses_a_bad_body():
    """bool is an int subclass in Python, so {"timeout": true} would otherwise
    reach a live panel as 1. _validate_fields is the thing that stops it."""
    async with routes_at([_device()]) as (client, headers, calls):
        for bad in ({"timeout": True}, {"timeout": "5"}, {"timeout": 5.0},
                    {"pairing_enabled": "true"}, {"pairing_enabled": 1},
                    {"nonsense": 1}, {"timeout": 5, "nonsense": 1}, {}):
            resp = await client.post("/control/bluetooth-disarming", json=bad,
                                     headers=headers)
            assert resp.status == 400, (bad, resp.status)
            assert (await resp.json())["error"] == "malformed"
        resp = await client.post("/control/bluetooth-disarming", data="not json",
                                 headers=headers)
        assert resp.status == 400
        assert calls["panel_write"] == []


async def test_bluetooth_disarming_route_maps_the_error_contract():
    """Same vocabulary as every other write route here: 422 unsupported, 400
    caller mistake, 429 with the edge's own Retry-After, 502 upstream.

    The read is the exception and says so in the route: it takes no caller input,
    so a non-`unsupported` failure there is upstream (502), never a 400.
    """
    async with routes_at([_device()],
                         panel=ad.ArmingDeviceError("nope", unsupported=True)) as (
            client, headers, _c):
        resp = await client.post("/control/bluetooth-disarming",
                                 json={"timeout": 5}, headers=headers)
        assert resp.status == 422 and (await resp.json())["error"] == "unsupported"

    async with routes_at([_device()], panel=ad.ArmingDeviceError("bad")) as (
            client, headers, _c):
        resp = await client.post("/control/bluetooth-disarming",
                                 json={"timeout": 5}, headers=headers)
        assert resp.status == 400 and (await resp.json())["error"] == "malformed"

    async with routes_at([_device()], panel=RateLimited(9)) as (client, headers, _c):
        for call in (client.get("/control/bluetooth-disarming", headers=headers),
                     client.post("/control/bluetooth-disarming",
                                 json={"timeout": 5}, headers=headers)):
            resp = await call
            assert resp.status == 429 and resp.headers["Retry-After"] == "9"

    async with routes_at([_device()], panel=RuntimeError("upstream")) as (
            client, headers, _c):
        resp = await client.get("/control/bluetooth-disarming", headers=headers)
        assert resp.status == 502 and (await resp.json())["error"] == "telus_error"
        # An ArmingDeviceError on the READ is 502 too, not 400: the route takes
        # no caller input, so there is nothing a caller can have asked wrong.

    async with routes_at([_device()],
                         panel=ad.ArmingDeviceError("no context id")) as (
            client, headers, _c):
        resp = await client.get("/control/bluetooth-disarming", headers=headers)
        assert resp.status == 502, await resp.text()


# ---------------------------------------------------------------------------
# PII guard
# ---------------------------------------------------------------------------

def test_fixtures_carry_no_account_identifiers():
    """The capture these came from is a real person's home. The unit id, the
    system id, the MAC addresses and every device description were replaced; this
    fails if a future fixture is added without the same treatment."""
    forbidden = ("111937238", "21975797", "5040743424C0", "0002D165F882",
                 "a38b5a30", "E60BF863", "@gmail", "@telus")
    for path in sorted(FIXTURES.glob("*.json")):
        text = path.read_text()
        for needle in forbidden:
            assert needle not in text, f"{path.name} still contains {needle!r}"


async def main():
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if inspect.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"  ok  {name}")
    print("\nall arming_devices tests passed")


if __name__ == "__main__":
    asyncio.run(main())
