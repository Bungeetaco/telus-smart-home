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
None for an undecodable state), that an undecodable state is logged once per
episode at INFO naming the device (not once per evaluation as "sensor None"),
the stale-state availability guard (both legs), the door/window name classification (multilingual, including that the
French needle requires its article) and the subtype-to-device-class table,
and the connectivity sensor's three-state contract plus its service-device
identity.

Deliberately NOT covered: async_setup_entry, which fans out through
controller device registries and sensor.py's shared coordinator getter;
sensor.py has its own suite and the setup wiring is exercised there and by
the other platforms' setup tests.
"""
import asyncio
import logging
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


def make_attribute_sensor(description_key="malfunction", **device_kwargs):
    """An AttributeBinarySensor built from the real description table.

    Exists because a mutation pass found this class's `is_on` unexercised while
    both other `is_on` sites in the module were pinned: inverting it changed no
    test. These are the malfunction and battery sub-entities that ride on every
    device, so a silent inversion would report a healthy sensor as faulty
    across the whole install.
    """
    kwargs = dict(id_="dev-attr", name="Front Door", partition_id=None,
                  device_subtype=Subtype.CONTACT_SENSOR, state=State.CLOSED,
                  malfunction=None, battery_critical=None, battery_low=None)
    kwargs.update(device_kwargs)
    device = ha_stubs.LibSensor(**kwargs)
    description = next(d for d in binary_sensor.ATTRIBUTE_BINARY_SENSORS
                       if d.key == description_key)
    controller = types.SimpleNamespace(
        update_coordinator=ha_stubs.DataUpdateCoordinator(None))
    return binary_sensor.AttributeBinarySensor(
        controller=controller, device=device, description=description), device


async def test_attribute_sensor_reads_its_description_value_fn():
    """The malfunction and battery attribute sensors must report the device's
    own flag, not its negation and not a constant."""
    entity, device = make_attribute_sensor("malfunction", malfunction=True)
    assert entity.is_on is True, (
        f"a malfunctioning device read as {entity.is_on}")

    entity, device = make_attribute_sensor("malfunction", malfunction=False)
    assert entity.is_on is False, (
        f"a healthy device read as {entity.is_on}: an inverted attribute sensor "
        "reports every healthy device as faulty")

    entity, device = make_attribute_sensor("battery", battery_low=True)
    assert entity.is_on is True, "a low battery must read on"
    print("  ok  attribute sensors report their description's value_fn verbatim")

class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


async def test_unmappable_state_is_logged_once_per_episode_and_names_the_device():
    """A zone whose library state maps to neither on nor off (UNKNOWN, as a
    dropped-out zone reports) is logged at INFO ONCE and at DEBUG after that,
    and the line names the device. Pinned because the line was emitted on
    every evaluation of `is_on` -- which HA calls at least twice per
    coordinator refresh (`available` and the state) -- so one zone sitting in
    UNKNOWN produced thousands of identical INFO lines a day, and because it
    named the entity by `_attr_name`, which is None under `has_entity_name`,
    so every line read "sensor None" and said nothing about WHICH zone.
    """
    logger = logging.getLogger(binary_sensor.__name__)
    saved_level = logger.level
    capture = _Capture()
    logger.addHandler(capture)
    logger.setLevel(logging.DEBUG)
    try:
        entity, device, _ = make_sensor(name="Patio Door", state=State.UNKNOWN)
        # Two refreshes, each evaluating `is_on` the way HA does: once for
        # `available`, once for the state.
        for _ in range(2):
            assert not entity.available
            assert entity.is_on is None, "an unmappable state stays unknown"

        def lines(level):
            return [r.getMessage() for r in capture.records if r.levelno == level]

        info = lines(logging.INFO)
        assert len(info) == 1, (
            f"{len(info)} INFO lines for one UNKNOWN episode (four evaluations): "
            f"{info!r} -- one zone in UNKNOWN for a day logged thousands")
        assert "Patio Door" in info[0] and "None" not in info[0], (
            f"{info[0]!r}: the line must name the device, never 'sensor None'")
        assert "UNKNOWN" in info[0], f"{info[0]!r}: the raw state must be named"
        assert len(lines(logging.DEBUG)) == 3, (
            f"{lines(logging.DEBUG)!r}: the repeats are still visible at DEBUG")

        # Back to a mappable state, then the SAME unmappable state again: a
        # new episode, logged once more -- the zone dropping out twice is two
        # findings. (Re-entering the same state is the ordering that proves
        # the episode reset: a memo that is never cleared on a mapped state
        # still logs a DIFFERENT state, so that step alone pins nothing.)
        device.state = State.CLOSED
        assert entity.is_on is False
        device.state = State.UNKNOWN
        assert entity.is_on is None
        assert entity.is_on is None
        assert len(lines(logging.INFO)) == 2, (
            f"{lines(logging.INFO)!r}: re-entering UNKNOWN after a real state "
            "is a new episode and is reported once")

        # A different unmappable state is a new finding and logs again.
        device.state = "DeviceState.OFFLINE"
        assert entity.is_on is None
        assert entity.is_on is None
        info = lines(logging.INFO)
        assert len(info) == 3 and "OFFLINE" in info[2], (
            f"{info!r}: a change to another unmappable state must log once more")
    finally:
        logger.removeHandler(capture)
        logger.setLevel(saved_level)


TESTS = (
    test_is_on_mapping,
    test_unmappable_state_is_logged_once_per_episode_and_names_the_device,
    test_available_guards_against_stale_state,
    test_contact_sensor_door_window_classification,
    test_subtype_device_class_table,
    test_connectivity_sensor_reports_a_down_bridge_instead_of_vanishing,
    test_attribute_sensor_reads_its_description_value_fn,
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
