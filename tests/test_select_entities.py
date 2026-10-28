"""Tests for every select entity (`select.py`, backlog E66): the camera chime
type, TELUS-app notification format and live resolution selectors, the
per-phone enrolled-partition picker, and the panel Bluetooth disarm-window
picker.

Run from the repository root:

    python3 tests/test_select_entities.py

No Home Assistant, no network, no hardware: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, `tests/fakes.py` the hass/session fakes, and the REAL
entity and helper code runs against scripted bridge replies.

Why this file exists: selects are where the enum-label lessons live (see
docs/alarm-com-api.md: the website API sends bare ints, the labels come from a
secondary source that can fail), and every mapping here fails silently:

  * a label mapped to the wrong value writes the wrong chime type / partition
    / resolution to real hardware with a 200 in response;
  * losing the labelled list must degrade differently per entity -- the chime
    type falls back to bare numbers (losing the control is worse), the
    notification format refuses (a bare format int means nothing), the
    resolution falls back (the geometry IS readable), the partition picker
    refuses (an unlabelled partition number is a guess about a live panel);
  * TELUS turning the indoor chime off as a side effect of the "no wired
    chime" type is permanent and surfaced ONLY by the persistent notification
    pinned here (E37);
  * the enrolled-partition picker must not exist on a single-partition
    account: its only possible interaction is an inert whole-record PUT.

Pinned here: each selector's label/value mapping and its documented fallback
(or refusal), current_option honesty (unknown rather than a plausible guess),
the stale-option race refusal, exact flat write bodies, echo adoption with
refresh fallback, the chime-disables-indoor-chime notification (fired only
when the chime was on), the partition picker's string partition ids and
availability gates, the panel timeout's unit-labelled options and inferred
write shape, and select.py's platform setup gating (capability gates plus the
fewer-than-two-partitions gate).
"""
import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

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

from custom_components.telus_smarthome_alarmdotcom import arming_devices_coordinator as adc  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom import camera_settings_coordinator as csc  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom import select  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom.const import (  # noqa: E402
    DATA_ARMING_DEVICES_COORDINATOR,
    DATA_CAMERA_COORDINATOR,
    DATA_CAMERA_SETTINGS_COORDINATOR,
    DATA_CONTROLLER,
    DATA_PANEL_BLUETOOTH_COORDINATOR,
    DOMAIN,
)

CAM = "cam-uuid-1"

CHIME_AUDIO = {
    "supports_chime_type": True,
    "chime_type": 1,
    "chime_type_options": [0, 1, 2],
    "chime_type_choices": [
        {"value": 0, "label": "None", "disables_indoor_chime": True},
        {"value": 1, "label": "Mechanical"},
        {"value": 2, "label": "Digital"},
    ],
    "indoor_chime_on": True,
}


def camera_entity(cls, section_name, section):
    hass = FakeHass()
    entry = FakeConfigEntry()
    coordinator = csc.CameraSettingsCoordinator(
        hass, entry, types.SimpleNamespace(data={CAM: {}}))
    coordinator.data = {CAM: {"settings": {section_name: section}, "diagnostics": {}}}
    entity = cls(coordinator, CAM)
    entity.hass = hass
    return entity, coordinator


def scripted_write(module, script):
    session = FakeSession(list(script))
    saved = fakes.patch(module, read_control_secret=ScriptedSecret("control-secret"),
                        async_get_clientsession=fakes.clientsession_for(session))
    return session, lambda: fakes.restore(module, saved)


async def expect_error(coro, *needles):
    try:
        await coro
    except HomeAssistantError as exc:
        for needle in needles:
            assert needle in str(exc), f"error {exc} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"expected HomeAssistantError containing {needles!r}, got success")


async def test_chime_type_labels_fallback_and_current_option():
    entity, _ = camera_entity(select.CameraChimeTypeSelect, "audio", CHIME_AUDIO)
    assert entity.available
    assert entity.options == ["None", "Mechanical", "Digital"], (
        f"options {entity.options!r}: the portal's own labels, in the camera's order")
    assert entity.current_option == "Mechanical"

    # The labelled list comes from a secondary upstream call that can fail:
    # losing the names must not lose the control.
    bare = {"supports_chime_type": True, "chime_type": 1, "chime_type_options": [0, 1]}
    entity, _ = camera_entity(select.CameraChimeTypeSelect, "audio", bare)
    assert entity.options == ["0", "1"], (
        f"options {entity.options!r}: with no labels the bare numbers still "
        "set the doorbell correctly; a selector reading 0/1 is poor but a "
        "vanished selector is worse")
    assert entity.current_option == "1"

    # A type outside the camera's own list: unknown is the honest answer.
    odd = dict(CHIME_AUDIO, chime_type=9)
    entity, _ = camera_entity(select.CameraChimeTypeSelect, "audio", odd)
    assert entity.current_option is None, (
        "inventing a plausible option would misreport the wiring and make the "
        "next write look like a no-op")

    entity, _ = camera_entity(select.CameraChimeTypeSelect, "audio",
                              {"chime_type": 1, "chime_type_options": [0, 1]})
    assert not entity.available, "no supports_chime_type flag, no control"


async def test_chime_type_write_race_and_disable_notification():
    entity, coordinator = camera_entity(select.CameraChimeTypeSelect, "audio",
                                        CHIME_AUDIO)
    echoed = dict(CHIME_AUDIO, chime_type=2)
    session, restore = scripted_write(csc, [FakeResponse(200, echoed)])
    try:
        await entity.async_select_option("Digital")
    finally:
        restore()
    request = session.requests[0]
    assert request["url"].endswith(f"/cameras/{CAM}/settings/chime")
    assert request["json"] == {"chime_type": 2}, (
        f"body {request['json']!r}: the LABEL must be translated back to the "
        "camera's own value, written flat")
    assert coordinator.data[CAM]["settings"]["audio"] == echoed
    assert entity.hass.persistent_notifications == [], (
        "a harmless chime type change must not raise the indoor-chime warning")

    # Choosing the option the camera flags as disabling the indoor chime, with
    # the chime currently ON: TELUS turns it off and never back on (E37), and
    # the persistent notification is the only surface that tells the user.
    entity, coordinator = camera_entity(select.CameraChimeTypeSelect, "audio",
                                        CHIME_AUDIO)
    session, restore = scripted_write(
        csc, [FakeResponse(200, dict(CHIME_AUDIO, chime_type=0,
                                     indoor_chime_on=False))])
    try:
        await entity.async_select_option("None")
    finally:
        restore()
    notifications = entity.hass.persistent_notifications
    assert len(notifications) == 1, (
        "TELUS just turned the indoor chime off as a side effect and nothing "
        "told the user -- the chime silently stops ringing")
    assert "indoor chime" in notifications[0]["message"]
    assert notifications[0]["notification_id"] == f"{DOMAIN}_{CAM}_indoor_chime_disabled", (
        "the id must be per camera so a second change replaces the notice "
        "instead of stacking copies")

    # Same selection while the chime is already OFF: nothing new was disabled.
    off = dict(CHIME_AUDIO, indoor_chime_on=False)
    entity, _ = camera_entity(select.CameraChimeTypeSelect, "audio", off)
    session, restore = scripted_write(csc, [FakeResponse(200, {})])
    try:
        await entity.async_select_option("None")
    finally:
        restore()
    assert entity.hass.persistent_notifications == [], (
        "warning about a chime that was already off cries wolf")

    # The stale-option race: the camera's list changed between render and click.
    entity, _ = camera_entity(select.CameraChimeTypeSelect, "audio", CHIME_AUDIO)
    await expect_error(entity.async_select_option("Tubular Bells"), "no longer offers")


async def test_notification_format_refuses_bare_numbers():
    section = {"supports_format": True, "format": 0,
               "format_choices": [{"value": 0, "label": "Text only"},
                                  {"value": 1, "label": "Text and thumbnail"}]}
    entity, coordinator = camera_entity(select.CameraNotificationFormatSelect,
                                        "notifications", section)
    assert entity.available
    assert entity.options == ["Text only", "Text and thumbnail"]
    assert entity.current_option == "Text only"

    session, restore = scripted_write(csc, [FakeResponse(200, dict(section, format=1))])
    try:
        await entity.async_select_option("Text and thumbnail")
    finally:
        restore()
    request = session.requests[0]
    assert request["url"].endswith(f"/cameras/{CAM}/settings/notifications")
    assert request["json"] == {"format": 1}
    assert coordinator.data[CAM]["settings"]["notifications"]["format"] == 1

    # Unlike the chime type there is NO bare-number fallback: these choices are
    # a constant the bridge always sends beside the flag, and a raw
    # notification-format integer means nothing to anyone.
    entity, _ = camera_entity(select.CameraNotificationFormatSelect,
                              "notifications", {"supports_format": True, "format": 0})
    assert entity.options == [], (
        f"options {entity.options!r}: missing choices must yield an empty "
        "list, not a selector of unexplained integers")


async def test_live_resolution_string_values_and_fallback():
    section = {"supports_live_resolution": True, "live_resolution": "1440x1440",
               "live_resolution_choices": [
                   {"value": "1440x1440", "label": "Full High Definition (1440x1440)"},
                   {"value": "480x480", "label": "Standard Definition (480x480)"}]}
    entity, coordinator = camera_entity(select.CameraLiveResolutionSelect,
                                        "video", section)
    assert entity.current_option == "Full High Definition (1440x1440)"
    session, restore = scripted_write(
        csc, [FakeResponse(200, dict(section, live_resolution="480x480"))])
    try:
        await entity.async_select_option("Standard Definition (480x480)")
    finally:
        restore()
    request = session.requests[0]
    assert request["url"].endswith(f"/cameras/{CAM}/settings/video")
    assert request["json"] == {"live_resolution": "480x480"}, (
        f"body {request['json']!r}: resolutions are STRING geometries, never "
        "coerced to numbers")
    assert coordinator.data[CAM]["settings"]["video"]["live_resolution"] == "480x480"

    # Losing the labels costs clarity but not meaning: the values ARE geometries.
    bare = {"supports_live_resolution": True, "live_resolution": "480x480",
            "live_resolution_options": ["1440x1440", "480x480"]}
    entity, _ = camera_entity(select.CameraLiveResolutionSelect, "video", bare)
    assert entity.options == ["1440x1440", "480x480"]
    assert entity.current_option == "480x480"


PHONE_RECORD = {
    "id": "ad42", "description": "Dad's iPhone",
    "supports_enrolled_partition": True,
    "enrolled_partition_id": "1",
    "enrolled_partition_choices": [
        {"value": "1", "label": "Main house"},
        {"value": "2", "label": "Garage suite"},
    ],
}


def with_service_device(hass, entry):
    """A phone's device links to the integration service device by registry
    id, so the registry (which setup fills before any platform runs) has to
    hold that device for the entity to build its DeviceInfo."""
    hass.data["device_registry"] = FakeDeviceRegistry([FakeDeviceEntry(
        "service-dev", {(DOMAIN, f"{entry.entry_id}_integration")}, {entry.entry_id})])
    return hass


def partition_select(record=PHONE_RECORD):
    hass = FakeHass()
    entry = FakeConfigEntry()
    with_service_device(hass, entry)
    coordinator = adc.ArmingDevicesCoordinator(hass, entry)
    coordinator.data = {record["id"]: dict(record)} if record else {}
    entity = select.ArmingDeviceEnrolledPartitionSelect(coordinator, entry, "ad42")
    entity.hass = hass
    return entity, coordinator


async def test_enrolled_partition_select_mapping_and_gates():
    entity, _ = partition_select()
    assert entity.available
    assert entity.options == ["Main house", "Garage suite"]
    assert entity.current_option == "Main house"
    assert entity._attr_device_info.get("name") == "Dad's iPhone", (
        "the entity must attach to the phone's own device, named as the "
        "account holder typed it")

    # A partition not in the account's own list: unknown, not a guess.
    entity, _ = partition_select(dict(PHONE_RECORD, enrolled_partition_id="9"))
    assert entity.current_option is None

    # No choices list (the bridge could not read the partitions): the control
    # goes unavailable rather than offering an empty picker.
    entity, _ = partition_select(dict(PHONE_RECORD, enrolled_partition_choices=[]))
    assert not entity.available

    # No supports flag: the record reads identically but cannot be saved.
    entity, _ = partition_select(dict(PHONE_RECORD, supports_enrolled_partition=False))
    assert not entity.available

    # The bridge dropped the phone entirely (unpaired): fail closed.
    entity, _ = partition_select(None)
    assert not entity.available


async def test_enrolled_partition_write_adopts_matching_echo_only():
    entity, coordinator = partition_select()
    echoed = dict(PHONE_RECORD, enrolled_partition_id="2")
    session, restore = scripted_write(adc, [FakeResponse(200, echoed)])
    try:
        await entity.async_select_option("Garage suite")
    finally:
        restore()
    request = session.requests[0]
    assert request["url"] == "http://127.0.0.1:8199/control/arming-devices/ad42/enrolled-partition"
    assert request["json"] == {"partition_id": "2"}, (
        f"body {request['json']!r}: the partition NUMBER as a string, exactly "
        "as the choices carry it -- never rebuilt, never coerced to int")
    assert coordinator.data["ad42"]["enrolled_partition_id"] == "2"
    assert coordinator.refresh_requests == 0

    # An echo about a DIFFERENT device is not evidence about this one.
    entity, coordinator = partition_select()
    session, restore = scripted_write(adc, [FakeResponse(200, {"id": "ad99"})])
    try:
        await entity.async_select_option("Garage suite")
    finally:
        restore()
    assert coordinator.data["ad42"]["enrolled_partition_id"] == "1", (
        "a mismatched echo must not be folded in")
    assert coordinator.refresh_requests == 1, (
        "...and a real re-read must be requested instead of assuming the "
        "requested value took")

    entity, _ = partition_select()
    await expect_error(entity.async_select_option("Attic"), "no longer offers")


PANEL_SETTINGS = {
    "supports_pairing_enabled": True, "pairing_enabled": True,
    "supports_timeout": True, "timeout": 5,
    "timeout_unit": "minutes", "timeout_options": [1, 5, 10, 20, 30],
}


def timeout_select(settings=PANEL_SETTINGS):
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="entryP")
    coordinator = adc.PanelBluetoothCoordinator(hass, entry)
    coordinator.data = dict(settings) if settings else {}
    entity = select.PanelBluetoothTimeoutSelect(coordinator, entry)
    entity.hass = hass
    return entity, coordinator


async def test_panel_timeout_select_unit_labels_and_write():
    entity, coordinator = timeout_select()
    assert entity.available
    assert entity.options == ["1 minutes", "5 minutes", "10 minutes",
                              "20 minutes", "30 minutes"], (
        f"options {entity.options!r}: labelled from the unit the bridge "
        "reports, never a hardcoded word -- a unitless number is the failure "
        "this entity is shaped to avoid")
    assert entity.current_option == "5 minutes"

    echoed = dict(PANEL_SETTINGS, timeout=10)
    session, restore = scripted_write(adc, [FakeResponse(200, echoed)])
    try:
        await entity.async_select_option("10 minutes")
    finally:
        restore()
    request = session.requests[0]
    assert request["url"] == "http://127.0.0.1:8199/control/bluetooth-disarming"
    assert request["json"] == {"timeout": 10}, (
        f"body {request['json']!r}: the flat inferred write shape (see the "
        "entity docstring: reconstructed from the sibling endpoint, not captured)")
    assert coordinator.data["timeout"] == 10

    # A window the vendor no longer offers: unknown, not the nearest match.
    entity, _ = timeout_select(dict(PANEL_SETTINGS, timeout=7))
    assert entity.current_option is None

    # No unit reported: bare numbers beat a wrong word.
    unitless = dict(PANEL_SETTINGS, timeout_unit=None)
    entity, _ = timeout_select(unitless)
    assert entity.options[:2] == ["1", "5"]

    entity, _ = timeout_select(dict(PANEL_SETTINGS, supports_timeout=False))
    assert not entity.available
    entity, _ = timeout_select({})
    assert not entity.available


async def test_select_platform_setup_gating():
    hass = FakeHass()
    entry = FakeConfigEntry()
    with_service_device(hass, entry)
    controller = types.SimpleNamespace(
        api=types.SimpleNamespace(devices=types.SimpleNamespace(cameras={})))
    hass.data[DOMAIN] = {entry.entry_id: {
        DATA_CONTROLLER: controller,
        DATA_CAMERA_COORDINATOR: types.SimpleNamespace(data={CAM: {}}),
    }}
    added = []
    await select.async_setup_entry(hass, entry, added.extend)
    store = hass.data[DOMAIN][entry.entry_id]

    store[DATA_CAMERA_SETTINGS_COORDINATOR].async_set_updated_data({
        CAM: {"settings": {"audio": {"supports_chime_type": True},
                           "notifications": {"supports_format": True},
                           "video": {}},
              "diagnostics": {}}})
    kinds = sorted(type(entity).__name__ for entity in added)
    assert kinds == ["CameraChimeTypeSelect", "CameraNotificationFormatSelect"], (
        f"created {kinds!r}: chime and format are capability-gated in, the "
        "resolution selector stays out while supports_live_resolution is absent")

    # One partition to choose between: the picker must NOT exist -- its only
    # possible interaction is re-selecting what is already true, a whole-record
    # PUT to a live panel that changes nothing.
    added.clear()
    one_choice = dict(PHONE_RECORD,
                      enrolled_partition_choices=PHONE_RECORD[
                          "enrolled_partition_choices"][:1])
    store[DATA_ARMING_DEVICES_COORDINATOR].async_set_updated_data(
        {"ad42": one_choice})
    assert added == [], (
        "a single-partition account got a partition picker -- an entity "
        "offering exactly one option, already selected, cannot express anything")

    # A second partition appears: the picker appears by itself, no restart.
    store[DATA_ARMING_DEVICES_COORDINATOR].async_set_updated_data(
        {"ad42": dict(PHONE_RECORD)})
    kinds = [type(entity).__name__ for entity in added]
    assert kinds == ["ArmingDeviceEnrolledPartitionSelect"], f"created {kinds!r}"

    added.clear()
    store[DATA_PANEL_BLUETOOTH_COORDINATOR].async_set_updated_data(
        dict(PANEL_SETTINGS))
    kinds = [type(entity).__name__ for entity in added]
    assert kinds == ["PanelBluetoothTimeoutSelect"], (
        f"created {kinds!r}: the disarm-window picker appears once the panel "
        "reports the field")


TESTS = (
    test_chime_type_labels_fallback_and_current_option,
    test_chime_type_write_race_and_disable_notification,
    test_notification_format_refuses_bare_numbers,
    test_live_resolution_string_values_and_fallback,
    test_enrolled_partition_select_mapping_and_gates,
    test_enrolled_partition_write_adopts_matching_echo_only,
    test_panel_timeout_select_unit_labels_and_write,
    test_select_platform_setup_gating,
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
