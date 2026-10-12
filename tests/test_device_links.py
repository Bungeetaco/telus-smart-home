"""Tests for the device-registry links the integration's devices carry
(`base_device.py`'s partition link, `arming_devices_coordinator.py`'s
service-device link).

Run from the repository root:

    python3 tests/test_device_links.py

No Home Assistant, no network, no account: `tests/ha_stubs.py` supplies the
`homeassistant.*` and `pyalarmdotcomajax` names, `tests/fakes.py` the hass and
device-registry fakes, and the REAL entity code builds its `DeviceInfo`.

Why this file exists: a device's link to the device "behind" it (every sensor,
lock and camera behind its partition; every paired phone behind the integration
service device) is `via_device_id`, a registry id resolved when the entity is
built. Upstream deprecated the identifier-tuple form (`via_device`) because
identifiers are only unique per config entry, and refuses it from Home
Assistant 2027.8. Nothing about a wrong link errors: a device that resolves no
id simply shows no "connected via" on its page, and a lookup that raises takes
the whole platform down at setup. Both are the kind of failure that is only
noticed by someone looking for it.

Pinned here: that `DeviceInfo` carries `via_device_id` (the partition's
registry id, scoped to the owning config entry) and never the deprecated
identifier form; that a device whose partition has no registry entry yet comes
up UNLINKED rather than failing to build; that the partition device itself
never links to itself; that a device with no partition carries no link; and the
same resolution for a paired phone against the integration service device.
"""
import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

from fakes import FakeConfigEntry, FakeDeviceEntry, FakeDeviceRegistry, FakeHass  # noqa: E402

from custom_components.telus_smarthome import arming_devices_coordinator as adc  # noqa: E402
from custom_components.telus_smarthome.base_device import HardwareBaseDevice  # noqa: E402
from custom_components.telus_smarthome.const import DOMAIN  # noqa: E402

PARTITION_ID = "123-1"
SENSOR_ID = "123-456"


def make_controller(entry, devices=()):
    hass = FakeHass()
    hass.data["device_registry"] = FakeDeviceRegistry(list(devices))
    return types.SimpleNamespace(
        hass=hass, config_entry=entry,
        update_coordinator=ha_stubs.DataUpdateCoordinator(hass))


def lib_device(device_id, partition_id):
    return ha_stubs.LibSensor(id_=device_id, name="Front Door", partition_id=partition_id,
                              device_subtype=ha_stubs.LibSensor.Subtype.CONTACT_SENSOR,
                              state=ha_stubs.LibSensor.DeviceState.CLOSED, malfunction=None,
                              battery_critical=None, battery_low=None)


async def test_device_links_to_its_partition_by_registry_id():
    entry = FakeConfigEntry(entry_id="entryA")
    partition = FakeDeviceEntry("partition-dev", {(DOMAIN, PARTITION_ID)}, {"entryA"})
    # Same identifier under ANOTHER config entry: identifiers are only unique
    # per entry, which is the whole reason the tuple form was deprecated. The
    # lookup must resolve the one this entry owns.
    foreign = FakeDeviceEntry("foreign-dev", {(DOMAIN, PARTITION_ID)}, {"entryB"})
    controller = make_controller(entry, [foreign, partition])

    entity = HardwareBaseDevice(controller, lib_device(SENSOR_ID, PARTITION_ID))
    info = entity._attr_device_info
    assert info.get("via_device_id") == "partition-dev", f"device_info {dict(info)!r}"
    assert "via_device" not in info, (
        "the identifier-tuple link is deprecated and refused from 2027.8; "
        "only the registry id form may be set")
    assert info["identifiers"] == {(DOMAIN, SENSOR_ID)}


async def test_device_with_no_partition_entry_yet_comes_up_unlinked():
    """The lookup raises ValueError upstream when the partition device does not
    exist. Setup registers every partition before the platforms run, so this
    is the defensive path -- but it must be a missing link, never a platform
    that fails to set up."""
    entry = FakeConfigEntry(entry_id="entryA")
    controller = make_controller(entry, [])

    entity = HardwareBaseDevice(controller, lib_device(SENSOR_ID, PARTITION_ID))
    info = entity._attr_device_info
    assert "via_device_id" not in info and "via_device" not in info, (
        f"device_info {dict(info)!r}: no partition device means no link, not a "
        "dangling one")
    assert info["identifiers"] == {(DOMAIN, SENSOR_ID)}


async def test_partition_never_links_to_itself_and_no_partition_means_no_link():
    entry = FakeConfigEntry(entry_id="entryA")
    partition = FakeDeviceEntry("partition-dev", {(DOMAIN, PARTITION_ID)}, {"entryA"})
    controller = make_controller(entry, [partition])

    # The partition's own entity: the library reports its partition_id as its
    # own id. A self-link raises HomeAssistantError upstream.
    itself = HardwareBaseDevice(controller, lib_device(PARTITION_ID, PARTITION_ID))
    assert "via_device_id" not in itself._attr_device_info, "a device may not be its own via device"

    # A device the library reports with no partition at all.
    orphan = HardwareBaseDevice(controller, lib_device(SENSOR_ID, None))
    assert "via_device_id" not in orphan._attr_device_info
    assert "via_device" not in orphan._attr_device_info


async def test_arming_device_links_to_the_service_device_by_registry_id():
    entry = FakeConfigEntry(entry_id="entryA")
    hass = FakeHass()
    service = FakeDeviceEntry("service-dev", {(DOMAIN, "entryA_integration")}, {"entryA"})
    hass.data["device_registry"] = FakeDeviceRegistry([service])
    coordinator = adc.ArmingDevicesCoordinator(hass, entry)
    coordinator.data = {"ad42": {"id": "ad42", "description": "Dad's iPhone"}}

    info = adc.arming_device_device_info(entry, coordinator, "ad42")
    assert info.get("via_device_id") == "service-dev", f"device_info {dict(info)!r}"
    assert "via_device" not in info
    assert info["identifiers"] == {(DOMAIN, "ad42")}
    assert info["name"] == "Dad's iPhone"

    # Service device not registered (it always is -- setup registers it before
    # the platforms -- but the defensive path is an unlinked phone, not a
    # select/switch platform that fails to set up).
    hass.data["device_registry"] = FakeDeviceRegistry([])
    info = adc.arming_device_device_info(entry, coordinator, "ad42")
    assert "via_device_id" not in info and "via_device" not in info, f"device_info {dict(info)!r}"
    assert info["name"] == "Dad's iPhone"


TESTS = (
    test_device_links_to_its_partition_by_registry_id,
    test_device_with_no_partition_entry_yet_comes_up_unlinked,
    test_partition_never_links_to_itself_and_no_partition_means_no_link,
    test_arming_device_links_to_the_service_device_by_registry_id,
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
