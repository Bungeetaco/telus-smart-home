"""Tests for the Bluetooth arming-device and panel Bluetooth plumbing
(`arming_devices_coordinator.py`, plus `switch.py`'s ArmingDeviceDisarmingSwitch
and PanelBluetoothPairingSwitch; backlog E66).

Run from the repository root:

    python3 tests/test_arming_entities.py

No Home Assistant, no network, no panel: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, `tests/fakes.py` the hass/session fakes, and the REAL
coordinator, write-helper and entity code runs against scripted bridge replies.

Why this file exists: these entities decide whether a phone walking up to a
real house disarms a real alarm. Everything here is built to fail CLOSED, and
a fail-closed rule that silently stops holding is invisible until the day it
matters:

  * a switch shown for a record that cannot be written is a control that
    does nothing when used -- the capability gate, not the value, is the
    permission;
  * a remembered value for a phone the bridge stopped reporting shows a
    switch position nobody actually knows;
  * the write bodies are FLAT objects with real JSON types ({"enabled": true},
    never a string a bool() coercion would mangle) -- the alarm.com website
    API reads JSON:API but writes flat, and the bridge's routes mirror that
    (see docs/alarm-com-api.md);
  * a mismatched write echo folded into the coordinator would show the
    REQUESTED state as current without evidence it took.

Pinned here: _async_bridge_write's full refusal mapping (422 per-route
wording, 404, 429 with Retry-After, 400, 502, unreadable-2xx-is-success,
transport failure wording that warns the change may have landed), the three
write helpers' exact URLs and flat bodies, both coordinators' degradation
rules (404 route DROPS the data, no-secret keeps it, 401/429 are
UpdateFailed, id-less records are dropped), the echo-fold guards, the
disarming switch's gates / real-False-vs-unknown split / read-only partition
attribute, and the pairing switch's gates and enrolled-ids attribute.
"""
import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

import aiohttp  # noqa: E402

import fakes  # noqa: E402
from fakes import (  # noqa: E402
    FakeConfigEntry,
    FakeDeviceEntry,
    FakeDeviceRegistry,
    FakeHass,
    FakeResponse,
    FakeSession,
    ScriptedSecret,
)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.telus_smarthome import arming_devices_coordinator as adc  # noqa: E402
from custom_components.telus_smarthome import switch  # noqa: E402

PHONE = {"id": "ad42", "description": "Dad's iPhone",
         "supports_disarming_toggle": True, "disarming_enabled": True,
         "enrolled_partition_id": "1"}

PANEL = {"supports_pairing_enabled": True, "pairing_enabled": True,
         "enrolled_device_ids": ["ad42", "ad43"]}


def scripted_write(script):
    session = FakeSession(list(script))
    saved = fakes.patch(adc, read_control_secret=ScriptedSecret("control-secret"),
                        async_get_clientsession=fakes.clientsession_for(session))
    return session, lambda: fakes.restore(adc, saved)


def scripted_read(outcome):
    async def bridge_get(hass, url, params=None, timeout=None):
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    saved = fakes.patch(adc, bridge_get=bridge_get)
    return lambda: fakes.restore(adc, saved)


async def expect_error(coro, *needles):
    try:
        await coro
    except HomeAssistantError as exc:
        for needle in needles:
            assert needle in str(exc), f"error {exc} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"expected HomeAssistantError containing {needles!r}, got success")


async def test_bridge_write_maps_every_refusal():
    hass = FakeHass()
    cases = [
        (FakeResponse(422, {}), "cannot be changed from here"),
        (FakeResponse(404, {}), "no longer knows"),
        (FakeResponse(429, {}, headers={"Retry-After": "45"}), "in 45s"),
        (FakeResponse(429, {}), "later"),
        (FakeResponse(400, {}), "malformed"),
        (FakeResponse(502, {}), "left as it was"),
    ]
    for scripted, needle in cases:
        session, restore = scripted_write([scripted])
        try:
            await expect_error(adc.async_set_disarming_enabled(hass, "ad42", True),
                               needle)
        finally:
            restore()

    session, restore = scripted_write([aiohttp.ClientConnectionError("refused")])
    try:
        message = await expect_error(
            adc.async_set_disarming_enabled(hass, "ad42", True),
            "may or may not have been applied")
    finally:
        restore()
    assert "check the setting" in message, (
        f"{message!r}: a transport failure on a write that may have LANDED "
        "must warn against blind retries at a live panel")

    # An unreadable 2xx is a landed write: {} back, never an error that would
    # invite a second write.
    session, restore = scripted_write([FakeResponse(200, None)])
    try:
        result = await adc.async_set_disarming_enabled(hass, "ad42", True)
    finally:
        restore()
    assert result == {}

    saved = fakes.patch(adc, read_control_secret=ScriptedSecret(None))
    try:
        await expect_error(adc.async_set_disarming_enabled(hass, "ad42", True),
                           "not reachable")
    finally:
        fakes.restore(adc, saved)


async def test_write_helpers_urls_and_flat_json_bodies():
    hass = FakeHass()

    session, restore = scripted_write([FakeResponse(200, dict(PHONE))])
    try:
        await adc.async_set_disarming_enabled(hass, "ad42", False)
    finally:
        restore()
    request = session.requests[0]
    assert request["url"] == "http://127.0.0.1:8199/control/arming-devices/ad42/disarming"
    assert request["json"] == {"enabled": False}, (
        f"body {request['json']!r}: a real JSON boolean, never a coercion -- "
        "bool('false') is True")
    assert request["json"]["enabled"] is False

    session, restore = scripted_write([FakeResponse(200, dict(PHONE))])
    try:
        await adc.async_set_enrolled_partition(hass, "ad42", "2")
    finally:
        restore()
    request = session.requests[0]
    assert request["url"] == (
        "http://127.0.0.1:8199/control/arming-devices/ad42/enrolled-partition")
    assert request["json"] == {"partition_id": "2"}
    assert isinstance(request["json"]["partition_id"], str), (
        "the partition NUMBER travels as the string the record reports")

    session, restore = scripted_write([FakeResponse(200, dict(PANEL))])
    try:
        await adc.async_set_bluetooth_disarming(hass, pairing_enabled=False)
    finally:
        restore()
    request = session.requests[0]
    assert request["url"] == "http://127.0.0.1:8199/control/bluetooth-disarming"
    assert request["json"] == {"pairing_enabled": False}, (
        f"body {request['json']!r}: only the fields the caller set -- this "
        "write shape is INFERRED from the sibling endpoint, and inventing "
        "extra fields widens the reconstruction risk")


async def test_arming_coordinator_degradation_rules():
    hass = FakeHass()
    entry = FakeConfigEntry()
    coordinator = adc.ArmingDevicesCoordinator(hass, entry)
    coordinator.data = {"ad42": dict(PHONE)}

    # A bridge predating the route: the control is not on offer, and that must
    # read as UNAVAILABLE (data dropped), never as a remembered True.
    restore = scripted_read(
        aiohttp.ClientResponseError(request_info=None, history=(), status=404))
    try:
        assert await coordinator._async_update_data() == {}, (
            "a 404 route must DROP the devices -- a stale 'this phone may "
            "disarm' is the one answer worse than none")
    finally:
        restore()

    # No secret yet (restart window): keep the page, do not flap.
    restore = scripted_read(None)
    try:
        assert await coordinator._async_update_data() == {"ad42": dict(PHONE)}
    finally:
        restore()

    restore = scripted_read(
        aiohttp.ClientResponseError(request_info=None, history=(), status=401))
    try:
        try:
            await coordinator._async_update_data()
        except ha_stubs.UpdateFailed:
            pass
        else:
            raise AssertionError("a 401 must be UpdateFailed, not data")
    finally:
        restore()

    # A 429 used to be UpdateFailed too. It now keeps the last page and holds
    # the poll for the bridge's Retry-After (RateLimitHoldMixin, backlog E105):
    # the bridge is reachable and the record is stale rather than wrong, and a
    # coordinator that came straight back on its next cycle was one more knock
    # on the edge limiter. Pinned fully in test_bridge_hold.py; here only the
    # degradation rule itself.
    restore = scripted_read(
        aiohttp.ClientResponseError(request_info=None, history=(), status=429))
    try:
        assert await coordinator._async_update_data() == {"ad42": dict(PHONE)}, (
            "a 429 must keep the last page, not fail the poll")
        assert coordinator._held(), "a 429 must start the poll hold"
    finally:
        restore()
    coordinator._hold_until = 0.0  # clear the hold for the checks below

    # An id-less record cannot be addressed by the write route; drop it.
    restore = scripted_read({"devices": [dict(PHONE), {"description": "ghost"}]})
    try:
        data = await coordinator._async_update_data()
    finally:
        restore()
    assert set(data) == {"ad42"}, f"kept {set(data)!r}"


async def test_apply_device_guards():
    hass = FakeHass()
    coordinator = adc.ArmingDevicesCoordinator(hass, FakeConfigEntry())
    coordinator.data = {"ad42": dict(PHONE)}

    coordinator.async_apply_device("ad42", {"id": "ad99", "disarming_enabled": False})
    assert coordinator.data["ad42"]["disarming_enabled"] is True, (
        "an echo naming a DIFFERENT device is not evidence about this one")

    coordinator.async_apply_device("ad42", {})
    assert coordinator.data["ad42"] == dict(PHONE)

    coordinator.async_apply_device("ghost", {"id": "ghost"})
    assert "ghost" not in coordinator.data, (
        "a device the coordinator has dropped must not be resurrected by an echo")

    updated = dict(PHONE, disarming_enabled=False)
    coordinator.async_apply_device("ad42", updated)
    assert coordinator.data["ad42"]["disarming_enabled"] is False


def with_service_device(hass, entry):
    """A phone's device links to the integration service device by registry
    id, so the registry (which setup fills before any platform runs) has to
    hold that device for the entity to build its DeviceInfo."""
    hass.data["device_registry"] = FakeDeviceRegistry([FakeDeviceEntry(
        "service-dev", {(adc.DOMAIN, f"{entry.entry_id}_integration")}, {entry.entry_id})])
    return hass


def disarming_switch(record=PHONE, coordinator=None):
    hass = FakeHass()
    entry = FakeConfigEntry()
    with_service_device(hass, entry)
    coordinator = coordinator or adc.ArmingDevicesCoordinator(hass, entry)
    coordinator.data = {record["id"]: dict(record)} if record else {}
    entity = switch.ArmingDeviceDisarmingSwitch(coordinator, entry, "ad42")
    entity.hass = hass
    return entity, coordinator


async def test_disarming_switch_fails_closed_on_every_unknown():
    entity, coordinator = disarming_switch()
    assert entity.available and entity.is_on is True
    assert entity.extra_state_attributes == {"enrolled_partition_id": "1"}, (
        "the partition is published read-only here (E43): re-enrolling is not "
        "a side effect of a switch")

    entity, _ = disarming_switch(dict(PHONE, supports_disarming_toggle=False))
    assert not entity.available, (
        "the value reads identically on a record that cannot be saved; the "
        "capability flag is the permission")

    entity, _ = disarming_switch(None)
    assert not entity.available, "a phone the bridge stopped listing is unknown, not off"

    entity, coordinator = disarming_switch()
    coordinator.last_update_success = False
    assert not entity.available, "a failed poll fails closed"

    record = dict(PHONE)
    del record["disarming_enabled"]
    entity, _ = disarming_switch(record)
    assert entity.is_on is None, (
        "a bridge too old to send the field is unknown, never 'off'")
    entity, _ = disarming_switch(dict(PHONE, disarming_enabled=False))
    assert entity.is_on is False, "False is a real answer: this phone cannot disarm"


async def test_disarming_switch_write_adopts_matching_echo_only():
    entity, coordinator = disarming_switch()
    echoed = dict(PHONE, disarming_enabled=False)
    session, restore = scripted_write([FakeResponse(200, echoed)])
    try:
        await entity.async_turn_off()
    finally:
        restore()
    assert session.requests[0]["json"] == {"enabled": False}
    assert coordinator.data["ad42"]["disarming_enabled"] is False
    assert coordinator.refresh_requests == 0

    entity, coordinator = disarming_switch()
    session, restore = scripted_write([FakeResponse(200, {})])
    try:
        await entity.async_turn_off()
    finally:
        restore()
    assert coordinator.data["ad42"]["disarming_enabled"] is True, (
        "an unreadable echo must not be folded in")
    assert coordinator.refresh_requests == 1, (
        "...and a real re-read must be requested instead of assuming the "
        "write took")


async def test_panel_coordinator_and_pairing_switch():
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entryP")
    coordinator = adc.PanelBluetoothCoordinator(hass, entry)
    coordinator.data = dict(PANEL)

    restore = scripted_read(
        aiohttp.ClientResponseError(request_info=None, history=(), status=404))
    try:
        assert await coordinator._async_update_data() == {}, (
            "no route or no document means 'not on offer', which must read as "
            "no entity rather than a remembered value")
    finally:
        restore()

    restore = scripted_read(None)
    try:
        assert await coordinator._async_update_data() == dict(PANEL), (
            "no control secret yet must keep the last page")
    finally:
        restore()

    entity = switch.PanelBluetoothPairingSwitch(coordinator, entry)
    entity.hass = hass
    assert entity.available and entity.is_on is True
    assert entity.extra_state_attributes == {"enrolled_device_ids": ["ad42", "ad43"]}, (
        "opaque ids only -- descriptions are household members' names and this "
        "attribute lands in diagnostics downloads")

    coordinator.data = dict(PANEL, pairing_enabled=False)
    assert entity.is_on is False
    coordinator.data = {"supports_pairing_enabled": True}
    assert entity.is_on is None, "a missing value is unknown, never off"
    coordinator.data = dict(PANEL, supports_pairing_enabled=False)
    assert not entity.available
    coordinator.data = {}
    assert not entity.available

    coordinator.data = dict(PANEL)
    echoed = dict(PANEL, pairing_enabled=False)
    session, restore = scripted_write([FakeResponse(200, echoed)])
    try:
        await entity.async_turn_off()
    finally:
        restore()
    assert session.requests[0]["url"] == "http://127.0.0.1:8199/control/bluetooth-disarming"
    assert session.requests[0]["json"] == {"pairing_enabled": False}
    assert coordinator.data == echoed

    # An empty echo falls through to a real re-read.
    session, restore = scripted_write([FakeResponse(200, None)])
    try:
        await entity.async_turn_on()
    finally:
        restore()
    assert coordinator.refresh_requests == 1


async def test_switch_platform_setup_gates_arming_and_panel_entities():
    hass = FakeHass()
    entry = FakeConfigEntry()
    with_service_device(hass, entry)
    controller = types.SimpleNamespace(
        api=types.SimpleNamespace(devices=types.SimpleNamespace(cameras={})))
    hass.data["telus_smarthome"] = {entry.entry_id: {
        "connection": controller,
        "camera_coordinator": types.SimpleNamespace(data={}),
    }}
    added = []

    async def absent_bridge(hass_, url, params=None, timeout=None):
        return None

    saved = fakes.patch(switch, bridge_get=absent_bridge)
    try:
        await switch.async_setup_entry(hass, entry, added.extend)
    finally:
        fakes.restore(switch, saved)
    store = hass.data["telus_smarthome"][entry.entry_id]

    # A phone that cannot be written yields NO switch; a writable one appears.
    store["arming_devices_coordinator"].async_set_updated_data({
        "ad1": {"id": "ad1", "supports_disarming_toggle": False,
                "disarming_enabled": True},
        "ad2": {"id": "ad2", "supports_disarming_toggle": True,
                "disarming_enabled": True},
    })
    kinds = [type(entity).__name__ for entity in added]
    assert kinds == ["ArmingDeviceDisarmingSwitch"], (
        f"created {kinds!r}: offering the switch on the VALUE would give "
        "someone a control over their alarm that silently does nothing")
    assert added[0]._device_id == "ad2"

    added.clear()
    store["panel_bluetooth_coordinator"].async_set_updated_data(
        {"supports_pairing_enabled": True, "pairing_enabled": True})
    kinds = [type(entity).__name__ for entity in added]
    assert kinds == ["PanelBluetoothPairingSwitch"], f"created {kinds!r}"


TESTS = (
    test_bridge_write_maps_every_refusal,
    test_write_helpers_urls_and_flat_json_bodies,
    test_arming_coordinator_degradation_rules,
    test_apply_device_guards,
    test_disarming_switch_fails_closed_on_every_unknown,
    test_disarming_switch_write_adopts_matching_echo_only,
    test_panel_coordinator_and_pairing_switch,
    test_switch_platform_setup_gates_arming_and_panel_entities,
)


async def main():
    failures = 0
    for test in TESTS:
        try:
            await test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
