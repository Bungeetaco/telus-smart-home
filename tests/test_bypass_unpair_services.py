"""Tests for `telus_smarthome_alarmdotcom.bypass_sensors` and `.unpair_arming_device`
(`bypass_service.py` / `unpair_service.py`, backlog E66).

Run from the repository root:

    python3 tests/test_bypass_unpair_services.py

No Home Assistant, no network, no panel: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, `tests/fakes.py` the hass/registry/session fakes, and
the REAL service handlers run against a scripted bridge.

Why this file exists: these are the two most consequential calls in the
integration -- a bypassed zone does not trigger the alarm until un-bypassed,
and an unpaired phone cannot be re-paired without walking to the panel. Both
are built to refuse rather than guess, and every refusal is a branch that
reads correct and can rot silently: the day one stops firing, a wrong device
id goes to a LIVE panel and nothing anywhere errors. Both are also
deliberately SERVICES rather than entities (a button gets pressed by
accident; a service call has to be written), and that shape is pinned too.

Pinned here, bypass: every refusal (no sensors, overlap, unknown/foreign/
non-sensor entities, mixed accounts, unloaded entry, unresolvable unique id,
partition disagreement/indeterminacy, unknown partition), both permission
gates with their distinct wordings, the pre-read failure mapping, the exact
flat POST body, the write-path status mapping (422/429+Retry-After/404/400/
502), the unreadable-2xx-is-success rule (a false failure invites a second
write at a live panel), and the DELAYED 15s refresh (an immediate one reads
the pre-bypass state back and paints the call as a no-op).

Pinned here, unpair: the single-entity schema (a list must not validate), the
suffix-based resolution refusals, the DELETE shape, the 404/429 mapping, the
unreadable-2xx rule, and that no refresh is spent confirming the deletion.
"""
import asyncio
import logging
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

import aiohttp  # noqa: E402

import fakes  # noqa: E402
from fakes import (  # noqa: E402
    FakeEntityRegistry,
    FakeHass,
    FakeRegistryEntry,
    FakeResponse,
    FakeSession,
    ScriptedSecret,
)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402
from pyalarmdotcomajax.devices.partition import Partition as libPartition  # noqa: E402

from custom_components.telus_smarthome_alarmdotcom import bypass_service, unpair_service  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom.const import (  # noqa: E402
    DATA_CONTROLLER,
    DOMAIN,
    SERVICE_BYPASS_SENSORS,
    SERVICE_UNPAIR_ARMING_DEVICE,
)


class RefreshRecorder:
    def __init__(self):
        self.requests = 0

    async def async_request_refresh(self):
        self.requests += 1


GOOD_PARTITION = {"id": "p1", "has_permission_to_change_state": True,
                  "can_bypass_when_armed": True}


_DEFAULT_READ = object()


def make_env(*, entries=None, sensors=None, partitions=None,
             partition_read=_DEFAULT_READ, script=None, secret="control-secret",
             store=True):
    """A hass wired the way __init__.py leaves it, plus the scripted bridge."""
    hass = FakeHass()
    if entries is None:
        entries = [
            FakeRegistryEntry("binary_sensor.front_door", unique_id="s1"),
            FakeRegistryEntry("binary_sensor.back_door", unique_id="s2"),
        ]
    hass.data["entity_registry"] = FakeEntityRegistry(entries)

    if sensors is None:
        sensors = {
            "s1": types.SimpleNamespace(id_="s1", partition_id="p1"),
            "s2": types.SimpleNamespace(id_="s2", partition_id="p1"),
        }
    if partitions is None:
        partitions = {"p1": ha_stubs.LibPartition(
            id_="p1", name="Panel", state=libPartition.DeviceState.DISARMED)}

    controller = types.SimpleNamespace(
        api=types.SimpleNamespace(devices=types.SimpleNamespace(
            sensors=dict(sensors), water_sensors={}, partitions=dict(partitions))),
        update_coordinator=RefreshRecorder(),
    )
    if store:
        hass.data[DOMAIN] = {"entry1": {DATA_CONTROLLER: controller}}
    else:
        hass.data[DOMAIN] = {}

    session = FakeSession(list(script or []))

    if partition_read is _DEFAULT_READ:
        partition_read = {"partitions": [dict(GOOD_PARTITION)]}

    async def scripted_bridge_get(hass_, url, timeout=None):
        if isinstance(partition_read, BaseException):
            raise partition_read
        return partition_read

    saved = fakes.patch(
        bypass_service,
        bridge_get=scripted_bridge_get,
        read_control_secret=ScriptedSecret(secret),
        async_get_clientsession=fakes.clientsession_for(session),
    )
    bypass_service.async_register_bypass_service(hass)
    handler = hass.services.registered[(DOMAIN, SERVICE_BYPASS_SENSORS)]
    return types.SimpleNamespace(hass=hass, session=session, controller=controller,
                                 handler=handler, saved=saved)


def cleanup(env):
    fakes.restore(bypass_service, env.saved)


def call(data):
    return ha_stubs.ServiceCall(DOMAIN, SERVICE_BYPASS_SENSORS, data)


async def expect_error(coro, *needles):
    try:
        await coro
    except HomeAssistantError as exc:
        for needle in needles:
            assert needle in str(exc), f"error {exc} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"expected HomeAssistantError containing {needles!r}, got success")


async def test_bypass_is_service_shaped_and_idempotent():
    env = make_env()
    try:
        first = env.handler
        bypass_service.async_register_bypass_service(env.hass)
        assert env.hass.services.registered[(DOMAIN, SERVICE_BYPASS_SENSORS)] is first
        bypass_service.async_remove_bypass_service(env.hass)
        assert not env.hass.services.has_service(DOMAIN, SERVICE_BYPASS_SENSORS)
    finally:
        cleanup(env)
    assert not hasattr(bypass_service, "async_setup_entry"), (
        "bypass grew a platform hook -- it is deliberately a manual service, "
        "never an entity a dashboard can offer by accident")


async def test_empty_and_contradictory_calls_are_refused():
    env = make_env()
    try:
        await expect_error(env.handler(call({})), "at least one entity")
        await expect_error(
            env.handler(call({"bypass": ["binary_sensor.front_door"],
                              "unbypass": ["binary_sensor.front_door"]})),
            "cannot bypass and un-bypass the same sensor")
        assert env.session.requests == [], "a refusal must never reach the panel"
    finally:
        cleanup(env)


async def test_entity_resolution_refusals():
    entries = [
        FakeRegistryEntry("binary_sensor.front_door", unique_id="s1"),
        FakeRegistryEntry("binary_sensor.foreign", unique_id="x", platform="zwave"),
        FakeRegistryEntry("lock.front", unique_id="l1"),
        FakeRegistryEntry("binary_sensor.orphan", unique_id="s9", config_entry_id=None),
        FakeRegistryEntry("binary_sensor.battery", unique_id="s1_battery"),
    ]
    env = make_env(entries=entries)
    try:
        await expect_error(env.handler(call({"bypass": ["binary_sensor.ghost"]})),
                           "not a known entity")
        await expect_error(env.handler(call({"bypass": ["binary_sensor.foreign"]})),
                           "not a TELUS Smart Home entity")
        await expect_error(env.handler(call({"bypass": ["lock.front"]})),
                           "not a TELUS alarm sensor")
        await expect_error(env.handler(call({"bypass": ["binary_sensor.orphan"]})),
                           "not attached to a loaded config entry")
        # An ATTRIBUTE entity's unique id is `<device>_battery`: posting it
        # would aim a mangled id at a live panel.
        await expect_error(env.handler(call({"bypass": ["binary_sensor.battery"]})),
                           "does not resolve to an Alarm.com sensor")
        assert env.session.requests == []
    finally:
        cleanup(env)


async def test_two_accounts_and_unloaded_entries_are_refused():
    entries = [
        FakeRegistryEntry("binary_sensor.front_door", unique_id="s1",
                          config_entry_id="entry1"),
        FakeRegistryEntry("binary_sensor.cottage", unique_id="s9",
                          config_entry_id="entry2"),
    ]
    env = make_env(entries=entries)
    try:
        await expect_error(
            env.handler(call({"bypass": ["binary_sensor.front_door",
                                         "binary_sensor.cottage"]})),
            "different TELUS accounts")
    finally:
        cleanup(env)

    env = make_env(store=False)
    try:
        await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                           "not loaded")
    finally:
        cleanup(env)


async def test_partition_resolution():
    # Disagreement across partitions: refuse, naming both.
    sensors = {
        "s1": types.SimpleNamespace(id_="s1", partition_id="p1"),
        "s2": types.SimpleNamespace(id_="s2", partition_id="p2"),
    }
    env = make_env(sensors=sensors)
    try:
        await expect_error(
            env.handler(call({"bypass": ["binary_sensor.front_door"],
                              "unbypass": ["binary_sensor.back_door"]})),
            "different partitions")
    finally:
        cleanup(env)

    # Missing partition_id + exactly one known partition: use it.
    sensors = {"s1": types.SimpleNamespace(id_="s1", partition_id=None)}
    env = make_env(sensors=sensors, script=[FakeResponse(200, {"status": "ok"})])
    try:
        await env.handler(call({"bypass": ["binary_sensor.front_door"]}))
        assert env.session.requests[0]["url"].endswith("/control/partitions/p1/bypass")
    finally:
        cleanup(env)

    # Missing partition_id + several partitions: no defensible choice.
    partitions = {
        "p1": ha_stubs.LibPartition(id_="p1", state=libPartition.DeviceState.DISARMED),
        "p2": ha_stubs.LibPartition(id_="p2", state=libPartition.DeviceState.DISARMED),
    }
    env = make_env(sensors=sensors, partitions=partitions)
    try:
        await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                           "refusing to guess")
    finally:
        cleanup(env)

    # A partition the account does not have: refuse.
    sensors = {"s1": types.SimpleNamespace(id_="s1", partition_id="p9")}
    env = make_env(sensors=sensors)
    try:
        await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                           "not one of this account's partitions")
    finally:
        cleanup(env)


async def test_permission_gates_name_which_gate_refused():
    read = {"partitions": [{"id": "p1", "has_permission_to_change_state": False,
                            "can_bypass_when_armed": True}]}
    env = make_env(partition_read=read)
    try:
        await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                           "permission")
    finally:
        cleanup(env)

    read = {"partitions": [{"id": "p1", "has_permission_to_change_state": True,
                            "can_bypass_when_armed": False}]}
    partitions = {"p1": ha_stubs.LibPartition(
        id_="p1", name="Panel", state=libPartition.DeviceState.ARMED_AWAY)}
    env = make_env(partition_read=read, partitions=partitions)
    try:
        await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                           "disarm it first")
    finally:
        cleanup(env)

    # Same flag, but the partition is DISARMED: the write proceeds.
    env = make_env(partition_read=read, script=[FakeResponse(200, {"status": "ok"})])
    try:
        await env.handler(call({"bypass": ["binary_sensor.front_door"]}))
        assert len(env.session.requests) == 1
    finally:
        cleanup(env)


async def test_partition_preread_failure_mapping():
    cases = [
        (aiohttp.ClientResponseError(request_info=None, history=(), status=404),
         "rebuild the bridge container"),
        (aiohttp.ClientResponseError(request_info=None, history=(), status=500),
         "HTTP 500"),
        (aiohttp.ClientConnectionError("refused"), "could not reach"),
        (None, "no control secret"),
        ({"partitions": [{"id": "other"}]}, "does not know partition"),
    ]
    for read, needle in cases:
        env = make_env(partition_read=read)
        try:
            await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                               needle)
            assert env.session.requests == [], (
                f"pre-read outcome {read!r} must stop the write cold")
        finally:
            cleanup(env)


async def test_successful_bypass_posts_the_flat_body_and_defers_the_refresh():
    env = make_env(script=[FakeResponse(200, {"status": "ok"})])
    try:
        await env.handler(call({
            # The repeat is a UI accident, not two instructions.
            "bypass": ["binary_sensor.front_door", "binary_sensor.front_door"],
            "unbypass": ["binary_sensor.back_door"]}))
        request = env.session.requests[0]
        assert request["method"] == "POST"
        assert request["url"] == "http://127.0.0.1:8199/control/partitions/p1/bypass"
        assert request["json"] == {"bypass": ["s1"], "unbypass": ["s2"]}, (
            f"body {request['json']!r}: the bridge takes the two DEDUPED device "
            "id lists in one flat object")
        assert request["headers"].get("Authorization") == "Bearer control-secret"

        assert env.controller.update_coordinator.requests == 0, (
            "an immediate refresh reads the PRE-bypass state back (measured: "
            "invisible at 5s, visible at 10s) and paints a working call as a no-op")
        assert len(env.hass.later_calls) == 1
        delay, action = env.hass.later_calls[0]
        assert delay == 15, f"the settle delay moved to {delay}"
        await action()
        assert env.controller.update_coordinator.requests == 1, (
            "the delayed callback must ask the coordinator for the refresh "
            "that shows the sensors' true post-bypass state")
    finally:
        cleanup(env)


async def test_bypass_write_status_mapping():
    cases = [
        (FakeResponse(422, {}), "refused this change"),
        (FakeResponse(429, {}, headers={"Retry-After": "30"}), "in 30s"),
        (FakeResponse(429, {}), "later"),
        (FakeResponse(404, {}), "does not know partition"),
        (FakeResponse(400, {}), "malformed"),
        (FakeResponse(502, {}), "nothing was changed"),
        (aiohttp.ClientConnectionError("refused"), "may or may not have been applied"),
    ]
    for scripted, needle in cases:
        env = make_env(script=[scripted])
        try:
            await expect_error(env.handler(call({"bypass": ["binary_sensor.front_door"]})),
                               needle)
        finally:
            cleanup(env)


async def test_bypass_unreadable_2xx_is_success_not_error():
    env = make_env(script=[FakeResponse(200, None)])  # 200 with an unparseable body
    try:
        await env.handler(call({"bypass": ["binary_sensor.front_door"]}))  # must not raise
        assert len(env.hass.later_calls) == 1, (
            "the write LANDED; reporting failure would invite a second write "
            "at a live panel")
    finally:
        cleanup(env)


# ---------------------------------------------------------------------------
# unpair_arming_device
# ---------------------------------------------------------------------------

PHONE_SWITCH = FakeRegistryEntry("switch.dad_phone_disarm",
                                 unique_id="ad42_bluetooth_disarming")


def make_unpair_env(*, entries=(PHONE_SWITCH,), script=None, secret="control-secret"):
    hass = FakeHass()
    hass.data["entity_registry"] = FakeEntityRegistry(list(entries))
    session = FakeSession(list(script or []))
    saved = fakes.patch(
        unpair_service,
        read_control_secret=ScriptedSecret(secret),
        async_get_clientsession=fakes.clientsession_for(session),
    )
    unpair_service.async_register_unpair_service(hass)
    handler = hass.services.registered[(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE)]
    return types.SimpleNamespace(hass=hass, session=session, handler=handler,
                                 saved=saved)


def unpair_call(data):
    return ha_stubs.ServiceCall(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE, data)


async def test_unpair_takes_exactly_one_entity():
    env = make_unpair_env()
    try:
        try:
            await env.handler(unpair_call({"entity_id": ["switch.a", "switch.b"]}))
        except ha_stubs.VolInvalid:
            pass
        else:
            raise AssertionError(
                "a LIST of entities validated -- the schema is single-entity on "
                "purpose, because there is no undo for the ones that should not "
                "have gone")
        assert env.session.requests == []
    finally:
        cleanup_unpair(env)


def cleanup_unpair(env):
    fakes.restore(unpair_service, env.saved)


async def test_unpair_resolution_refusals():
    entries = [
        PHONE_SWITCH,
        FakeRegistryEntry("switch.foreign", unique_id="x_bluetooth_disarming",
                          platform="zwave"),
        FakeRegistryEntry("switch.status_led", unique_id="cam1_status_led"),
    ]
    env = make_unpair_env(entries=entries)
    try:
        await expect_error(env.handler(unpair_call({"entity_id": "switch.ghost"})),
                           "not a known entity")
        await expect_error(env.handler(unpair_call({"entity_id": "switch.foreign"})),
                           "does not belong")
        await expect_error(env.handler(unpair_call({"entity_id": "switch.status_led"})),
                           "not a paired-phone switch")
        assert env.session.requests == [], (
            "a refused resolution must never reach the irreversible DELETE")
    finally:
        cleanup_unpair(env)


async def test_unpair_deletes_the_stripped_device_id():
    env = make_unpair_env(script=[FakeResponse(200, {"unpaired": "ad42"})])
    try:
        await env.handler(unpair_call({"entity_id": "switch.dad_phone_disarm"}))
        request = env.session.requests[0]
        assert request["method"] == "DELETE", f"used {request['method']}"
        assert request["url"] == "http://127.0.0.1:8199/control/arming-devices/ad42", (
            f"hit {request['url']!r}: the unique id's _bluetooth_disarming "
            "suffix must be stripped to recover the device id")
        assert request["headers"].get("Authorization") == "Bearer control-secret"
        assert env.hass.later_calls == [], (
            "no refresh is spent confirming a deletion the call just performed")
    finally:
        cleanup_unpair(env)


async def test_unpair_warns_when_the_bridge_names_a_different_device():
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record)
    unpair_service.LOGGER.addHandler(handler)
    env = make_unpair_env(script=[FakeResponse(200, {"unpaired": "ad99"})])
    try:
        await env.handler(unpair_call({"entity_id": "switch.dad_phone_disarm"}))
    finally:
        unpair_service.LOGGER.removeHandler(handler)
        cleanup_unpair(env)
    warnings = [r for r in records if r.levelno >= logging.WARNING]
    assert warnings, (
        "the bridge reported unpairing a DIFFERENT device and nothing was "
        "logged -- that is the one trace of a mis-aimed irreversible delete")


async def test_unpair_status_mapping_and_unreadable_success():
    cases = [
        (FakeResponse(404, {}), "no longer lists"),
        (FakeResponse(429, {}), "rate-limiting"),
        (aiohttp.ClientConnectionError("refused"), "rejected the unpair request"),
    ]
    for scripted, needle in cases:
        env = make_unpair_env(script=[scripted])
        try:
            await expect_error(
                env.handler(unpair_call({"entity_id": "switch.dad_phone_disarm"})),
                needle)
        finally:
            cleanup_unpair(env)

    env = make_unpair_env(script=[FakeResponse(200, None)])
    try:
        await env.handler(unpair_call({"entity_id": "switch.dad_phone_disarm"}))
        # The device is gone; an error would invite a retry whose 404 is far
        # more confusing than this call's silence.
    finally:
        cleanup_unpair(env)

    env = make_unpair_env(secret=None)
    try:
        await expect_error(
            env.handler(unpair_call({"entity_id": "switch.dad_phone_disarm"})),
            "not reachable")
        assert env.session.requests == []
    finally:
        cleanup_unpair(env)


async def test_unpair_is_service_shaped_and_idempotent():
    env = make_unpair_env()
    try:
        first = env.handler
        unpair_service.async_register_unpair_service(env.hass)
        assert env.hass.services.registered[(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE)] is first
        unpair_service.async_remove_unpair_service(env.hass)
        assert not env.hass.services.has_service(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE)
    finally:
        cleanup_unpair(env)
    assert not hasattr(unpair_service, "async_setup_entry"), (
        "unpair grew a platform hook -- it is a service and not a button "
        "BECAUSE a button gets pressed by accident and this cannot be undone")


TESTS = (
    test_bypass_is_service_shaped_and_idempotent,
    test_empty_and_contradictory_calls_are_refused,
    test_entity_resolution_refusals,
    test_two_accounts_and_unloaded_entries_are_refused,
    test_partition_resolution,
    test_permission_gates_name_which_gate_refused,
    test_partition_preread_failure_mapping,
    test_successful_bypass_posts_the_flat_body_and_defers_the_refresh,
    test_bypass_write_status_mapping,
    test_bypass_unreadable_2xx_is_success_not_error,
    test_unpair_takes_exactly_one_entity,
    test_unpair_resolution_refusals,
    test_unpair_deletes_the_stripped_device_id,
    test_unpair_warns_when_the_bridge_names_a_different_device,
    test_unpair_status_mapping_and_unreadable_success,
    test_unpair_is_service_shaped_and_idempotent,
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
