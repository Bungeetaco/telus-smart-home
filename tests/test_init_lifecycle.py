"""Tests for the integration's entry lifecycle (`__init__.py`, backlog E66):
setup, the config-entry migrations, and unload.

Run from the repository root:

    python3 tests/test_init_lifecycle.py

No Home Assistant, no network, no account: `tests/ha_stubs.py` supplies the
`homeassistant.*` and `pyalarmdotcomajax` names (including empty stand-ins for
the three import-time patch modules, whose real application CI's
check_patch.py job proves), `tests/fakes.py` the hass/registry fakes, and the
REAL `__init__.py` runs with a scripted controller and an absent bridge.

The module is imported as `custom_components.telus_smarthome.__init__`
explicitly, because the harness deliberately seeds the package without
executing its __init__ (see ha_stubs); importing it by name runs the real
file with its relative imports intact.

Why this file exists: setup's device-pruning pass has already shipped four
distinct silent failures (this fork's own bridge/service devices deleted and
recreated on EVERY restart; then, after that was fixed by id shape, the
bridge cameras and paired phones still deleted on every boot and reload
because their ids share the library's `<unit>-<n>` namespace; removal
decided on whichever identifier an unordered set yielded first; a KeyError on
the second removal aborting setup entirely), and nothing pinned the fixed
behaviour. The clip-link secret's generate-once rule and the bridge-down-at-
startup tolerance are the same shape: regressions produce a working-looking
HA whose damage (dead clip links, lost customisations, no camera entities
ever) surfaces days later.

Pinned here: the auth/transport exception mapping at setup, the clip secret's
generate-once-never-rotate rule, the view's once-per-HA-run registration, the
bridge-down-at-startup tolerance (setup completes, coordinators empty), the
whole device-pruning matrix (and that setup never touches the registry's
internal deleted-device container), Skybell virtual-device creation, the
partition and service devices registered ahead of the platforms so every
`via_device_id` lookup resolves, platform forwarding, service registration, the v1-to-v4 options migrations, and
unload's pop/stop/unregister ordering including the services-outlive-the-
first-entry rule and the clip_view sentinel exclusion.

Deliberately NOT covered: the websocket monitor's runtime behaviour (the
task is handed to HA and pinned only as "created and tied to the entry");
actually running it needs the real pyalarmdotcomajax stack.
"""
import asyncio
import importlib
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

import aiohttp  # noqa: E402

import fakes  # noqa: E402
from fakes import (  # noqa: E402
    FakeConfigEntries,
    FakeConfigEntry,
    FakeDeviceEntry,
    FakeDeviceRegistry,
    FakeHass,
)

from homeassistant.exceptions import (  # noqa: E402
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
)

from custom_components.telus_smarthome import camera as camera_module  # noqa: E402
from custom_components.telus_smarthome import event as event_module  # noqa: E402
from custom_components.telus_smarthome.const import (  # noqa: E402
    ALARM_COM_MANUFACTURER,
    CONF_CLIP_LINK_SECRET,
    DATA_CONTROLLER,
    DOMAIN,
    PLATFORMS,
    SERVICE_BYPASS_SENSORS,
    SERVICE_SPEAK,
    SERVICE_STOP_SPEAKING,
    SERVICE_UNPAIR_ARMING_DEVICE,
    SKYBELL_MANUFACTURER,
)

init_module = importlib.import_module("custom_components.telus_smarthome.__init__")


async def _absent_bridge(hass, url, params=None, timeout=None):
    return None  # no control secret yet: the bridge is down at HA startup


def scripted_controller(initialize_outcome=None, devices=(), cameras=(), partitions=()):
    class _Controller:
        instances = []

        def __init__(self, hass, config_entry):
            self.hass = hass
            self.config_entry = config_entry
            self.stopped = 0
            self.api = types.SimpleNamespace(devices=types.SimpleNamespace(
                all={d.id_: d for d in (*devices, *partitions)},
                cameras={c.id_: c for c in cameras},
                partitions={p.id_: p for p in partitions},
            ))
            _Controller.instances.append(self)

        async def initialize(self):
            if isinstance(initialize_outcome, BaseException):
                raise initialize_outcome

        async def async_start_websocket_monitor(self):
            return None

        async def stop(self):
            self.stopped += 1

    return _Controller


def adc_device(device_id, subtype=None, has_state=True, name="Zone"):
    return types.SimpleNamespace(id_=device_id, device_subtype=subtype,
                                 has_state=has_state, name=name,
                                 mac_address="aa:bb:cc:dd:ee:ff")


def make_hass(entry, devices=()):
    hass = FakeHass()
    hass.config_entries = FakeConfigEntries([entry])
    hass.data["device_registry"] = FakeDeviceRegistry(list(devices))
    return hass


def registered_identifiers(registry):
    return {identifier for device in registry.devices.values()
            for identifier in device.identifiers}


async def run_setup(hass, entry, controller_cls):
    saved_init = fakes.patch(init_module, AlarmIntegrationController=controller_cls)
    saved_camera = fakes.patch(camera_module, bridge_get=_absent_bridge)
    saved_event = fakes.patch(event_module, bridge_get=_absent_bridge)
    try:
        return await init_module.async_setup_entry(hass, entry)
    finally:
        fakes.restore(init_module, saved_init)
        fakes.restore(camera_module, saved_camera)
        fakes.restore(event_module, saved_event)


async def test_setup_maps_auth_and_transport_failures():
    auth_cases = [
        ha_stubs.OtpRequired([]),
        ha_stubs.AuthenticationFailed("bad password"),
        ha_stubs.ConfigureTwoFactorAuthentication(),
    ]
    for outcome in auth_cases:
        entry = FakeConfigEntry()
        hass = make_hass(entry)
        try:
            await run_setup(hass, entry, scripted_controller(outcome))
        except ConfigEntryAuthFailed:
            pass
        else:
            raise AssertionError(
                f"{type(outcome).__name__} must raise ConfigEntryAuthFailed so "
                "HA starts a reauth flow instead of retrying forever")

    transport_cases = [TimeoutError(), aiohttp.ClientError("down"),
                       ha_stubs.AlarmdotcomException("hiccup")]
    for outcome in transport_cases:
        entry = FakeConfigEntry()
        hass = make_hass(entry)
        try:
            await run_setup(hass, entry, scripted_controller(outcome))
        except ConfigEntryNotReady:
            pass
        else:
            raise AssertionError(
                f"{type(outcome).__name__} must raise ConfigEntryNotReady so "
                "HA retries with backoff instead of accusing the credentials")


async def test_setup_succeeds_with_the_bridge_down_and_wires_everything():
    entry = FakeConfigEntry()
    hass = make_hass(entry)
    result = await run_setup(hass, entry, scripted_controller())
    assert result is True, (
        "an unreachable camera bridge at HA startup must never fail the "
        "entire entry -- the alarm panel and locks have nothing to do with it")

    secret = entry.data.get(CONF_CLIP_LINK_SECRET)
    assert secret and len(secret) == 64, (
        f"clip link secret {secret!r}: a fresh entry must be given a 32-byte "
        "hex signing secret before any coordinator can mint links")

    assert len(hass.http.views) == 1, "the clip redirect view must be registered"
    store = hass.data[DOMAIN][entry.entry_id]
    assert store[DATA_CONTROLLER] is not None

    forwarded = hass.config_entries.forwarded
    assert forwarded and forwarded[0][1] == list(PLATFORMS), (
        f"forwarded {forwarded!r}: every platform must be handed to HA")

    for service in (SERVICE_SPEAK, SERVICE_STOP_SPEAKING, SERVICE_BYPASS_SENSORS,
                    SERVICE_UNPAIR_ARMING_DEVICE):
        assert hass.services.has_service(DOMAIN, service), f"{service} not registered"

    names = [name for _coro, name in entry.background_tasks]
    assert f"{DOMAIN}_websocket_monitor" in names, (
        "the websocket monitor must be a config-entry background task -- a "
        "bare create_task survived unload and leaked one permanently "
        "reconnecting monitor per reload")

    # Second setup in the same HA run (a reload): the secret must NOT rotate
    # (delivered clip links die with it), and the view must not re-register
    # (aiohttp raises on duplicate routes).
    result = await run_setup(hass, entry, scripted_controller())
    assert result is True
    assert entry.data[CONF_CLIP_LINK_SECRET] == secret, (
        "a reload rotated the clip link secret -- every link already inside a "
        "delivered phone notification now 401s forever")
    assert len(hass.http.views) == 1, "the clip view was registered twice"


async def test_setup_prunes_only_absent_alarm_devices():
    """Setup removes exactly the devices the pyalarmdotcomajax library used to
    report and no longer does, and nothing else this integration registers.

    Two shipped bugs are pinned here, one per kind of own device. First, the
    devices whose ids are NOT shaped like an Alarm.com id (a camera keyed by a
    UUID, the Integration service device) were deleted on every setup. Second,
    and the one that survived that fix: the bridge-served cameras and the
    paired Bluetooth arming phones carry ids from the SAME `<unit>-<n>`
    namespace as every library device (camera.py and
    arming_devices_coordinator.py register `(DOMAIN, <id>)` with the id the
    bridge reports, and that id is the account's Alarm.com device id), so the
    id-shape test alone judged them absent from the library's list and removed
    all four on every boot and reload. Each was restored by its platform moments
    later with its registry id, area, labels and user name intact (Home
    Assistant restores a deleted device from its deleted-device record), so
    the cost was registry churn, a removed-then-recreated entity per
    entity on them, and four misleading "no longer present on Alarm.com" INFO
    lines per setup. The library's devices are the ones registered under the
    library's manufacturer stamps (`LIBRARY_DEVICE_MANUFACTURERS`: what
    base_device.py, the partition registration and the Skybell virtual device
    stamp); a device carrying any other stamp, or none, is never the library's
    to prune. The stamp is checked at the removal decision rather than as an
    early skip before the identifier loop, so the loop's own keep branches
    (another integration's identifier; an id of this domain the regex does
    not match) and its population of the set the Skybell pass reads still
    run for every device of the entry.

    Proven to fail against: the pre-fix code (four removed); the stamp gate
    widened to every stamp, and removed from the decision (the same four);
    the foreign-identifier keep branch deleted (keep-foreign removed); the
    unshaped-id keep branch deleted (the regex match is dereferenced as
    None); and the stamp check moved to an early skip before the identifier
    loop (a second, virtual device is registered for the shared camera). The
    two keep-branch seeds carry the library's stamp on purpose: unstamped,
    an early skip would protect them and deleting either branch would stay
    green.
    """
    # A camera the library AND the bridge both report (the library lists it
    # in `all` and `cameras`; its registry row was registered by camera.py
    # first and carries the bridge's stamp). It must be neither pruned nor
    # given a second, virtual Skybell device: the identifier loop has to run
    # for it so its id lands in the set the Skybell pass reads, which is why
    # the stamp is checked at the removal decision and not before the loop.
    shared_camera = adc_device("77-89", name="Shared camera")
    devices = [
        adc_device("123-456"),  # still on Alarm.com
        shared_camera,
    ]
    cameras = [adc_device("77-88", name="Skybell"), shared_camera]
    partitions = [adc_device("123-1", name="Main partition")]
    registry_devices = [
        FakeDeviceEntry("keep-present", {(DOMAIN, "123-456")}, {"entryX"},
                        manufacturer=ALARM_COM_MANUFACTURER),
        FakeDeviceEntry("keep-attr", {(DOMAIN, "123-456_low_battery")}, {"entryX"},
                        manufacturer=ALARM_COM_MANUFACTURER),
        FakeDeviceEntry("remove-gone", {(DOMAIN, "123-999")}, {"entryX"},
                        manufacturer=ALARM_COM_MANUFACTURER),
        # A Skybell virtual device whose camera left the library: the library
        # owns it (setup registers it from the library's camera list), so it
        # goes too.
        FakeDeviceEntry("remove-skybell-gone", {(DOMAIN, "77-99")}, {"entryX"},
                        manufacturer=SKYBELL_MANUFACTURER),
        # This fork's own devices. A camera keyed by a non-Alarm.com id, and
        # the Integration service device: deleting them on every setup
        # silently discarded renames and area assignments -- a shipped bug.
        FakeDeviceEntry("keep-bridge-cam-uuid", {(DOMAIN, "cam-uuid-abc")}, {"entryX"},
                        manufacturer="TELUS Smart Home"),
        FakeDeviceEntry("keep-service", {(DOMAIN, "entryX_integration")}, {"entryX"},
                        manufacturer="TELUS"),
        # A bridge-served camera and a paired phone, keyed by the account's
        # Alarm.com device id (the shape the prune regex recognises) but
        # registered by camera.py / arming_device_device_info, not by the
        # library -- removed on every setup by the id-shape test alone.
        FakeDeviceEntry("keep-bridge-cam", {(DOMAIN, "123-2048")}, {"entryX"},
                        name="Living Room", manufacturer="TELUS Smart Home"),
        FakeDeviceEntry("keep-shared-cam", {(DOMAIN, "77-89")}, {"entryX"},
                        name="Shared camera", manufacturer="TELUS Smart Home"),
        FakeDeviceEntry("keep-arming-phone", {(DOMAIN, "123-5263")}, {"entryX"},
                        name="Someone's phone", manufacturer=None),
        # Both stamped as the library's so that ONLY the identifier loop's own
        # keep branches protect them: another integration's identifier, and
        # an id of this domain that is not shaped like an Alarm.com id (the
        # regex yields no match, and the branch that keeps it is also what
        # stops the match being dereferenced).
        FakeDeviceEntry("keep-foreign", {("hue", "123-999")}, {"entryX"},
                        manufacturer=ALARM_COM_MANUFACTURER),
        FakeDeviceEntry("keep-unshaped", {(DOMAIN, "not-an-adc-id")}, {"entryX"},
                        manufacturer=ALARM_COM_MANUFACTURER),
    ]
    entry = FakeConfigEntry(entry_id="entryX")
    hass = make_hass(entry, registry_devices)
    controller_cls = scripted_controller(devices=devices, cameras=cameras,
                                         partitions=partitions)
    # The fake registry's `deleted_devices` raises on any access, the way the
    # real one does for core callers: setup must never touch that container.
    # The purge it used to run there has no public replacement, and Home
    # Assistant restores a deleted device's id/area/name itself when the
    # device comes back, then expires orphaned records on its own clock.
    assert await run_setup(hass, entry, controller_cls)

    registry = hass.data["device_registry"]
    assert registry.removed == ["remove-gone", "remove-skybell-gone"], (
        f"removed {registry.removed!r}: exactly the devices the library no "
        "longer has -- never this fork's own devices (the bridge cameras and "
        "the paired phones share the library's id namespace and are NOT the "
        "library's to prune), never attribute-suffixed ids of present devices, "
        "never another integration's, and never twice")
    for own in ("keep-bridge-cam", "keep-arming-phone", "keep-bridge-cam-uuid",
                "keep-service", "keep-foreign", "keep-unshaped", "keep-shared-cam"):
        assert own in registry.devices, (
            f"{own} was pruned: its platform recreates it moments later, so "
            "the symptom is registry churn plus a misleading 'no longer present "
            "on Alarm.com' line on every boot and reload, not a missing device")
    created = {next(iter(call["identifiers"])) for call in registry.created}
    assert created == {(DOMAIN, "77-88"), (DOMAIN, "123-1"), (DOMAIN, "entryX_integration")}, (
        f"registered {sorted(created)!r}: the Skybell camera absent from the "
        "registry gets its virtual device, each partition and the integration "
        "service device are registered ahead of the platforms (they are the "
        "via devices every other device links to by registry id), and nothing "
        "else is re-created -- in particular no virtual device for the camera "
        "the bridge already registered (a stamp check that skips it before the "
        "identifier loop leaves it out of the set the Skybell pass reads)")
    assert registry.devices["keep-service"].name == "TELUS Smart Home Integration", (
        "the service device registration must land on the EXISTING service "
        "device (same identifier, same config entry), not fork a second one")


async def test_setup_registers_the_via_devices_before_forwarding_platforms():
    """Every Alarm.com device links to its partition's device, and every paired
    phone to the integration service device, by registry id (`via_device_id`).
    The id is looked up when the entity is built, and the platforms are set up
    concurrently -- so unless setup registers both anchors BEFORE it forwards
    the platforms, whichever platform's entities are built first find no
    partition device and silently come up unlinked."""
    entry = FakeConfigEntry(entry_id="entryY")
    hass = make_hass(entry)
    partitions = [adc_device("55-1", name="Upstairs"), adc_device("55-2", name="Garage")]
    seen_at_forward = []
    forward = hass.config_entries.async_forward_entry_setups

    async def snapshotting_forward(config_entry, platforms):
        seen_at_forward.append(registered_identifiers(hass.data["device_registry"]))
        await forward(config_entry, platforms)

    hass.config_entries.async_forward_entry_setups = snapshotting_forward
    assert await run_setup(hass, entry, scripted_controller(partitions=partitions))

    assert seen_at_forward, "platforms were never forwarded"
    expected = {(DOMAIN, "55-1"), (DOMAIN, "55-2"), (DOMAIN, "entryY_integration")}
    assert expected <= seen_at_forward[0], (
        f"registered before the platforms: {sorted(seen_at_forward[0])!r} -- "
        f"missing {sorted(expected - seen_at_forward[0])!r}")
    registry = hass.data["device_registry"]
    partition = registry.async_get_device_by_identifier((DOMAIN, "55-1"), "entryY")
    assert partition is not None and partition.name == "Upstairs"
    service = registry.async_get_device_by_identifier((DOMAIN, "entryY_integration"), "entryY")
    assert service is not None and service.name == "TELUS Smart Home Integration"
    assert all(call.get("entry_type") == ha_stubs.DeviceEntryType.SERVICE
               for call in registry.created
               if (DOMAIN, "entryY_integration") in call["identifiers"]), (
        "the service device must be registered as a SERVICE entry, matching "
        "what every platform's DeviceInfo for it declares")


async def test_migrations_v1_to_v4():
    entry = FakeConfigEntry(version=1, options={
        "arm_code": 1234,
        "force_bypass": "Stay Only",
        "silent_arming": "Always",
        "no_entry_delay": "Away Only",
    })
    hass = make_hass(entry)
    assert await init_module.async_migrate_entry(hass, entry)
    assert entry.version == 4, f"stopped at version {entry.version}"
    options = entry.options
    assert options["arm_code"] == "1234", (
        "a numeric arm code from v1 must survive as the string the panel is "
        "sent, not be dropped")
    # NOTE on the delay flags: the v2-to-v3 step appends "delay" to a mode
    # when the LEGACY no_entry_delay setting did NOT cover it (i.e. the mode
    # kept its entry delay), and v3-to-v4 renames "delay" to "no_entry_delay",
    # which alarm_control_panel.py then treats as "arm with NO entry delay".
    # That inversion is inherited migration behaviour, pinned here as it IS;
    # see the E66 close-out in docs/backlog.md for the finding.
    assert options["arm_home_options"] == [
        "force_bypass", "silent_arming", "no_entry_delay"], (
        f"arm_home {options['arm_home_options']!r}")
    assert options["arm_away_options"] == ["silent_arming"], (
        f"arm_away {options['arm_away_options']!r}")
    assert options["arm_night_options"] == ["silent_arming", "no_entry_delay"], (
        f"arm_night {options['arm_night_options']!r}")
    for legacy in ("use_arm_code", "force_bypass", "silent_arming", "no_entry_delay"):
        assert legacy not in options, f"legacy option {legacy} survived to v4"

    # An empty v1 entry: no code, no legacy flags; every mode keeps its delay.
    entry = FakeConfigEntry(version=1, options={})
    hass = make_hass(entry)
    assert await init_module.async_migrate_entry(hass, entry)
    assert entry.version == 4
    assert entry.options["arm_code"] is None
    assert entry.options["arm_home_options"] == ["no_entry_delay"]
    assert entry.options["arm_away_options"] == ["no_entry_delay"]
    assert entry.options["arm_night_options"] == ["no_entry_delay"]


async def test_unload_stops_the_controller_and_keeps_shared_services_until_last():
    def build_loaded_entry(entry_id):
        entry = FakeConfigEntry(entry_id=entry_id)
        controller = types.SimpleNamespace(stopped=0)

        async def stop():
            controller.stopped += 1

        controller.stop = stop
        return entry, controller

    entry1, controller1 = build_loaded_entry("e1")
    entry2, controller2 = build_loaded_entry("e2")
    hass = FakeHass()
    hass.config_entries = FakeConfigEntries([entry1, entry2])
    hass.data[DOMAIN] = {
        "clip_view_registered": True,
        "e1": {DATA_CONTROLLER: controller1},
        "e2": {DATA_CONTROLLER: controller2},
    }
    for service in (SERVICE_SPEAK, SERVICE_STOP_SPEAKING, SERVICE_BYPASS_SENSORS,
                    SERVICE_UNPAIR_ARMING_DEVICE):
        hass.services.async_register(DOMAIN, service, lambda call: None)

    assert await init_module.async_unload_entry(hass, entry1)
    assert controller1.stopped == 1, (
        "unload must call controller.stop() -- stopping only the websocket "
        "leaked one aiohttp session per reload, and every options save is a reload")
    assert "e1" not in hass.data[DOMAIN]
    assert hass.config_entries.unloaded[-1][1] == list(PLATFORMS)
    assert hass.services.has_service(DOMAIN, SERVICE_SPEAK), (
        "the shared services must survive while another entry is loaded")

    assert await init_module.async_unload_entry(hass, entry2)
    for service in (SERVICE_SPEAK, SERVICE_STOP_SPEAKING, SERVICE_BYPASS_SENSORS,
                    SERVICE_UNPAIR_ARMING_DEVICE):
        assert not hass.services.has_service(DOMAIN, service), (
            f"{service} survived the LAST entry's unload -- the "
            "clip_view_registered sentinel must not count as an entry")


TESTS = (
    test_setup_maps_auth_and_transport_failures,
    test_setup_succeeds_with_the_bridge_down_and_wires_everything,
    test_setup_prunes_only_absent_alarm_devices,
    test_setup_registers_the_via_devices_before_forwarding_platforms,
    test_migrations_v1_to_v4,
    test_unload_stops_the_controller_and_keeps_shared_services_until_last,
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
