"""Tests for `binary_sensor.py` (backlog E66): the alarm-sensor entity's state
and classification, and the bridge-connectivity sensor's deliberate
availability contract.

Run from the repository root:

    python3 tests/test_binary_sensors.py

No Home Assistant, no network, no panel: `tests/ha_stubs.py` supplies the
`homeassistant.*` and `pyalarmdotcomajax` names and the REAL entity code runs
over fake library devices.

Why this file exists: two of this platform's rules exist precisely because
their failure is invisible.

  * `available` must go false when the coordinator is unhealthy OR the state
    is undecodable: this platform once returned only `is_on is not None`, so
    when TELUS auth died every door/smoke/glass-break sensor kept presenting
    its last state as current -- a stale "closed" on a door sensor, with
    nothing anywhere indicating staleness.
  * CameraBridgeConnectivitySensor deliberately BREAKS the coordinator
    availability contract: an entity that goes unavailable exactly when it
    should say "off" can never say "off" at all, so it stays available while
    the bridge is down and only reads unavailable before the first completed
    poll attempt. Restoring the usual contract would make the sensor useless
    while looking perfectly healthy.

Pinned here: the is_on mapping across sensor and water-sensor states (with
None for an undecodable state), the stale-state availability guard (both
legs), the door/window name classification (multilingual, including that the
French needle requires its article) and the subtype-to-device-class table,
and the connectivity sensor's three-state contract plus its service-device
identity.

Deliberately NOT covered: async_setup_entry, which fans out through
controller device registries and sensor.py's shared coordinator getter;
sensor.py has its own suite and the setup wiring is exercised there and by
the other platforms' setup tests.
"""
import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

from homeassistant.components.binary_sensor import BinarySensorDeviceClass  # noqa: E402

from custom_components.telus_smarthome import binary_sensor  # noqa: E402
from fakes import FakeConfigEntry  # noqa: E402

Subtype = ha_stubs.LibSensor.Subtype
State = ha_stubs.LibSensor.DeviceState
WaterState = ha_stubs.LibWaterSensor.DeviceState


def make_sensor(name="Front Door", subtype=Subtype.CONTACT_SENSOR,
                state=State.CLOSED, water=False):
    cls = ha_stubs.LibWaterSensor if water else ha_stubs.LibSensor
    device = cls(id_="dev-1", name=name, partition_id=None, device_subtype=subtype,
                 state=state, malfunction=None, battery_critical=None,
                 battery_low=None)
    controller = types.SimpleNamespace(
        update_coordinator=ha_stubs.DataUpdateCoordinator(None))
    entity = binary_sensor.BinarySensor(controller=controller, device=device)
    return entity, device, controller


async def test_is_on_mapping():
    cases = [
        (State.CLOSED, False, False),
        (State.IDLE, False, False),
        (State.OPEN, True, False),
        (State.ACTIVE, True, False),
        (WaterState.DRY, False, True),
        (WaterState.WET, True, True),
    ]
    for state, expected, water in cases:
        entity, _, _ = make_sensor(state=state, water=water)
        assert entity.is_on is expected, f"{state} read as {entity.is_on}"

    entity, _, _ = make_sensor(state=State.UNKNOWN)
    assert entity.is_on is None, (
        "an undecodable raw state must be None (unknown), never guessed into "
        "open or closed")


async def test_available_guards_against_stale_state():
    entity, _, controller = make_sensor(state=State.CLOSED)
    assert entity.available

    controller.update_coordinator.last_update_success = False
    assert not entity.available, (
        "a dead poll (TELUS auth failed, polling stopped) must read "
        "unavailable -- this platform once kept presenting the last state as "
        "current, which for a door sensor is a stale 'closed' exactly when it "
        "matters")

    entity, _, _ = make_sensor(state=State.UNKNOWN)
    assert not entity.available, (
        "a healthy poll with an undecodable state is still not a state worth "
        "presenting")


async def test_contact_sensor_door_window_classification():
    cases = [
        ("Front Door", BinarySensorDeviceClass.DOOR),
        ("Kitchen Window", BinarySensorDeviceClass.WINDOW),
        # The French needle carries its article: "la fenêtre" matches only
        # with it present (see the finding in this test's suite docstring).
        ("La fenêtre du salon", BinarySensorDeviceClass.WINDOW),
        ("Zone 7", None),
    ]
    for name, expected in cases:
        entity, _, _ = make_sensor(name=name)
        assert entity.device_class == expected, (
            f"contact sensor named {name!r} classified as {entity.device_class!r}, "
            f"expected {expected!r} -- the name is the only signal alarm.com "
            "gives for door-vs-window")

    entity, _, _ = make_sensor(name="Front Door",
                               subtype=Subtype.CONTACT_SHOCK_SENSOR)
    assert entity.device_class == BinarySensorDeviceClass.DOOR, (
        "the shock variant of a contact sensor classifies the same way")


async def test_subtype_device_class_table():
    cases = [
        (Subtype.SMOKE_DETECTOR, BinarySensorDeviceClass.SMOKE),
        (Subtype.CO_DETECTOR, BinarySensorDeviceClass.CO),
        (Subtype.PANIC_BUTTON, BinarySensorDeviceClass.SAFETY),
        (Subtype.GLASS_BREAK_DETECTOR, BinarySensorDeviceClass.VIBRATION),
        (Subtype.PANEL_GLASS_BREAK_DETECTOR, BinarySensorDeviceClass.VIBRATION),
        (Subtype.MOTION_SENSOR, BinarySensorDeviceClass.MOTION),
        (Subtype.PANEL_MOTION_SENSOR, BinarySensorDeviceClass.MOTION),
        (Subtype.FREEZE_SENSOR, BinarySensorDeviceClass.COLD),
    ]
    for subtype, expected in cases:
        entity, _, _ = make_sensor(name="Zone", subtype=subtype)
        assert entity.device_class == expected, (
            f"{subtype} classified as {entity.device_class!r}")

    entity, _, _ = make_sensor(name="Basement leak", subtype=None, water=True,
                               state=WaterState.DRY)
    assert entity.device_class == BinarySensorDeviceClass.MOISTURE, (
        "a water sensor is MOISTURE regardless of subtype")


class FakeStreamHealthCoordinator(ha_stubs.DataUpdateCoordinator):
    """Only the two fields the connectivity sensor reads: the coordinator
    contract fields plus sensor.py's last_poll_ok flag."""

    def __init__(self, last_poll_ok):
        super().__init__(None)
        self.last_poll_ok = last_poll_ok


async def test_connectivity_sensor_reports_a_down_bridge_instead_of_vanishing():
    entry = FakeConfigEntry(entry_id="entryC")

    # Before any poll attempt has completed: nothing to report yet.
    entity = binary_sensor.CameraBridgeConnectivitySensor(
        FakeStreamHealthCoordinator(None), entry)
    assert not entity.available

    # A completed but FAILED poll: the bridge being down IS the reportable
    # state. The plain coordinator contract would mark this unavailable, and
    # an entity that is unavailable exactly when it should say "off" can never
    # say "off" at all.
    coordinator = FakeStreamHealthCoordinator(False)
    coordinator.last_update_success = False
    entity = binary_sensor.CameraBridgeConnectivitySensor(coordinator, entry)
    assert entity.available, (
        "the connectivity sensor went unavailable while the bridge is down -- "
        "the one moment it exists to report")
    assert entity.is_on is False

    entity = binary_sensor.CameraBridgeConnectivitySensor(
        FakeStreamHealthCoordinator(True), entry)
    assert entity.available and entity.is_on is True

    assert entity._attr_unique_id == "entryC_camera_bridge"
    identifiers = entity._attr_device_info["identifiers"]
    assert identifiers == {("telus_smarthome", "entryC_integration")}, (
        f"device {identifiers!r}: this reports on the integration's own "
        "plumbing and must join the synthetic Integration service device, not "
        "any TELUS device")


TESTS = (
    test_is_on_mapping,
    test_available_guards_against_stale_state,
    test_contact_sensor_door_window_classification,
    test_subtype_device_class_table,
    test_connectivity_sensor_reports_a_down_bridge_instead_of_vanishing,
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
