"""Tests for the bridge-served camera settings entities and their shared
plumbing (`camera_settings_coordinator.py`, plus the camera-backed entities in
`number.py` and `switch.py`; backlog E66).

Run from the repository root:

    python3 tests/test_camera_settings_entities.py

No Home Assistant, no network, no camera: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, `tests/fakes.py` the hass/session fakes, and the REAL
coordinator, helper and entity code runs against scripted bridge replies.

Why this file exists: every failure mode here is silent in production.

  * Capability gating decides which controls EXIST: a gate that flips reads as
    "HA offers a switch the camera refuses with a 422" or as "the volume
    slider quietly never appears", neither of which errors anywhere.
  * The write paths POST FLAT objects to the bridge (which reconstructs
    alarm.com's read-JSON:API/write-flat shape upstream -- see
    docs/alarm-com-api.md); a drifted body or a drifted read/write key split
    (the chime route READS indoor_chime_on but WRITES indoor_on) turns a
    working toggle into a 400 or, worse, a write of the wrong field.
  * The echoed-section adoption is what keeps a write from costing a full
    two-route re-poll per camera against a rate-limited live account; folding
    the echo into the wrong section would silently corrupt what every sibling
    entity reads until the next poll.
  * One camera's failed poll used to blank every camera's entities (observed
    live: one 25s diagnostics read, zero entities account-wide).

Pinned here: async_bridge_post's status mapping (422/404/429 with the
Retry-After header AND the body fallback), the coordinator's degradation
rules (no secret keeps the page, one camera's failure keeps its last entry
and never blanks the others, only a total failure marks the coordinator
failed, a 404 on both routes means absent-not-empty), the echoed-section
fold (including that an unknown camera is ignored), the add-as-capability-
appears helper (re-offered until it yields, then never again), the volume and
LED-brightness numbers (gating, device-reported bounds with their documented
fallbacks, 0 is a value and not unknown, exact write bodies, echo adoption
with refresh fallback), every switch toggle's support-flag gate and
read/write key pair, the indoor-chime unavailability rule driven by
disables_indoor_chime (and its stay-usable-when-unknown fallback), the
privacy switch's read-back-instead-of-adopt rule, platform setup gating for
number.py and switch.py (which entities appear for which capability sets,
privacy unconditionally, everything else gated), and the automation rule
switch's pause/resume round trip.
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
    FakeHass,
    FakeResponse,
    FakeSession,
    ScriptedSecret,
)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.telus_smarthome_alarmdotcom import camera_settings_coordinator as csc  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom import number, switch  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom.const import (  # noqa: E402
    DATA_CAMERA_COORDINATOR,
    DATA_CAMERA_SETTINGS_COORDINATOR,
    DATA_CONTROLLER,
    DOMAIN,
)

CAM = "cam-uuid-1"


def make_coordinator(data=None, cameras=(CAM,)):
    hass = FakeHass()
    entry = FakeConfigEntry()
    camera_coordinator = types.SimpleNamespace(data={cam: {} for cam in cameras})
    coordinator = csc.CameraSettingsCoordinator(hass, entry, camera_coordinator)
    coordinator.data = data
    return coordinator


def scripted_write(script):
    """Patch the write path's secret + session; returns (session, restore)."""
    session = FakeSession(list(script))
    saved = fakes.patch(csc, read_control_secret=ScriptedSecret("control-secret"),
                        async_get_clientsession=fakes.clientsession_for(session))
    return session, lambda: fakes.restore(csc, saved)


async def expect_error(coro, *needles):
    try:
        await coro
    except HomeAssistantError as exc:
        for needle in needles:
            assert needle in str(exc), f"error {exc} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"expected HomeAssistantError containing {needles!r}, got success")


async def test_bridge_post_maps_every_refusal():
    hass = FakeHass()
    cases = [
        (FakeResponse(422, {}), "does not support"),
        (FakeResponse(404, {}), "no longer knows"),
        (FakeResponse(429, {}, headers={"Retry-After": "30"}), "in 30s"),
        # No header: the wait must still surface from the body the bridge mirrors.
        (FakeResponse(429, {"error": "rate_limited", "retry_after": 60}), "in 60s"),
        (FakeResponse(429, None), "later"),
        (aiohttp.ClientConnectionError("refused"), "rejected the request"),
    ]
    for scripted, needle in cases:
        session = FakeSession([scripted])
        saved = fakes.patch(csc, read_control_secret=ScriptedSecret("s"),
                            async_get_clientsession=fakes.clientsession_for(session))
        try:
            await expect_error(csc.async_bridge_post(hass, "http://127.0.0.1:8199/x", {}),
                               needle)
        finally:
            fakes.restore(csc, saved)

    saved = fakes.patch(csc, read_control_secret=ScriptedSecret(None))
    try:
        await expect_error(csc.async_bridge_post(hass, "http://127.0.0.1:8199/x", {}),
                           "not reachable")
    finally:
        fakes.restore(csc, saved)


async def test_coordinator_degrades_per_camera_never_account_wide():
    coordinator = make_coordinator(cameras=("cam-a", "cam-b"))
    coordinator.data = {"cam-a": {"settings": {"old": True}, "diagnostics": {}},
                        "cam-b": {"settings": {}, "diagnostics": {}}}

    outcomes = {
        "cam-a": aiohttp.ClientConnectionError("slow"),  # this camera fails...
        "cam-b": {"fresh": True},                        # ...this one answers
    }

    async def scripted_bridge_get(hass, url, timeout=None):
        for camera_id, outcome in outcomes.items():
            if camera_id in url:
                if isinstance(outcome, BaseException):
                    raise outcome
                return dict(outcome)
        raise AssertionError(f"unexpected url {url}")

    saved = fakes.patch(csc, bridge_get=scripted_bridge_get,
                        read_control_secret=ScriptedSecret("s"))
    try:
        data = await coordinator._async_update_data()
    finally:
        fakes.restore(csc, saved)
    assert data["cam-a"] == {"settings": {"old": True}, "diagnostics": {}}, (
        "the failed camera must keep its LAST page -- capability flags do not "
        "change minute to minute, and dropping them reads as 'this camera can "
        "do nothing'")
    assert data["cam-b"]["settings"] == {"fresh": True}, (
        "one camera's failure must never blank the others (observed live: one "
        "25s diagnostics read left zero entities account-wide)")


async def test_coordinator_total_failure_and_404_and_no_secret():
    coordinator = make_coordinator(cameras=("cam-a",))
    coordinator.data = {"cam-a": {"settings": {"old": True}, "diagnostics": {}}}

    async def failing(hass, url, timeout=None):
        raise aiohttp.ClientConnectionError("down")

    saved = fakes.patch(csc, bridge_get=failing, read_control_secret=ScriptedSecret("s"))
    try:
        try:
            await coordinator._async_update_data()
        except ha_stubs.UpdateFailed:
            pass
        else:
            raise AssertionError(
                "every camera failing IS the bridge being down, and the "
                "entities must reflect it")
    finally:
        fakes.restore(csc, saved)

    async def missing(hass, url, timeout=None):
        raise aiohttp.ClientResponseError(request_info=None, history=(), status=404)

    saved = fakes.patch(csc, bridge_get=missing, read_control_secret=ScriptedSecret("s"))
    try:
        data = await coordinator._async_update_data()
    finally:
        fakes.restore(csc, saved)
    assert data == {}, (
        f"got {data!r}: a camera the bridge 404s on both routes must be ABSENT "
        "from data, not present-with-empty-values -- absence is what keeps "
        "'bridge too old' distinguishable from 'camera supports nothing'")

    saved = fakes.patch(csc, read_control_secret=ScriptedSecret(None))
    try:
        data = await coordinator._async_update_data()
    finally:
        fakes.restore(csc, saved)
    assert data == coordinator.data, (
        "no control secret yet (normal for a minute after a restart) must keep "
        "the last page, not read as 'no camera supports anything'")


async def test_apply_settings_section_folds_only_that_section():
    coordinator = make_coordinator({
        CAM: {"settings": {"audio": {"call_volume": 3}, "led": {"enabled": True}},
              "diagnostics": {"battery": "ok"}}})
    coordinator.async_apply_settings_section(CAM, "audio", {"call_volume": 7})
    assert coordinator.data[CAM]["settings"]["audio"] == {"call_volume": 7}
    assert coordinator.data[CAM]["settings"]["led"] == {"enabled": True}, (
        "folding one section must not touch its siblings")
    assert coordinator.data[CAM]["diagnostics"] == {"battery": "ok"}

    before = dict(coordinator.data)
    coordinator.async_apply_settings_section("ghost-cam", "audio", {"call_volume": 1})
    assert coordinator.data == before, (
        "a camera the coordinator has since dropped must be ignored, not "
        "resurrected from a write's echo")


async def test_add_camera_entities_reoffers_until_the_capability_appears():
    coordinator = make_coordinator({})
    entry = FakeConfigEntry()
    added = []
    offers = []

    def build(camera_id, camera_data):
        offers.append(camera_id)
        if (camera_data.get("settings") or {}).get("capable"):
            return [object()]
        return []

    csc.async_add_camera_entities(entry, coordinator, added.extend, build)
    assert offers == [] and added == []

    coordinator.async_set_updated_data({CAM: {"settings": {}, "diagnostics": {}}})
    assert offers == [CAM] and added == [], "an incapable camera must yield nothing yet"

    coordinator.async_set_updated_data({CAM: {"settings": {"capable": True},
                                              "diagnostics": {}}})
    assert len(added) == 1, (
        "the camera must be RE-offered on every poll until it yields -- that "
        "is what lets a bridge that comes up after HA still produce entities")

    coordinator.async_set_updated_data({CAM: {"settings": {"capable": True},
                                              "diagnostics": {}}})
    assert len(added) == 1 and offers.count(CAM) == 2, (
        "once a camera has yielded entities it must never be offered again, "
        "or every poll duplicates them")


def volume_entity(audio, coordinator=None):
    coordinator = coordinator or make_coordinator(
        {CAM: {"settings": {"audio": audio}, "diagnostics": {}}})
    entity = number.CameraCallVolumeNumber(coordinator, CAM)
    entity.hass = coordinator.hass
    return entity, coordinator


async def test_volume_number_bounds_values_and_gating():
    entity, _ = volume_entity({"supports_call_volume": True, "call_volume": 0,
                               "call_volume_min": 0, "call_volume_max": 20})
    assert entity.available
    assert entity.native_value == 0.0, (
        "0 is a real volume (muted speaker); truthiness turned it into "
        "'unknown' once already and the slider had nothing to render")
    assert entity.native_max_value == 20.0, "the device's own ceiling must win"

    entity, _ = volume_entity({"supports_call_volume": True})
    assert entity.native_value is None
    assert entity.native_min_value == 0.0 and entity.native_max_value == 10.0, (
        "with no reported bounds the observed VDB750 0..10 range is the fallback")

    entity, _ = volume_entity({"supports_call_volume": False, "call_volume": 5})
    assert not entity.available, (
        "callVolumeFeatureSupport 0 sends an identical-looking payload; the "
        "boolean is the only thing separating settable from refused")


async def test_volume_write_posts_flat_body_and_adopts_the_echo():
    entity, coordinator = volume_entity({"supports_call_volume": True, "call_volume": 3})
    echoed = {"supports_call_volume": True, "call_volume": 7, "extra": "refreshed"}
    session, restore = scripted_write([FakeResponse(200, echoed)])
    try:
        await entity.async_set_native_value(7.0)
    finally:
        restore()
    request = session.requests[0]
    assert request["url"] == f"http://127.0.0.1:8199/control/cameras/{CAM}/settings/volume"
    assert request["json"] == {"volume": 7}, (
        f"body {request['json']!r}: the bridge takes a flat integer volume")
    assert coordinator.data[CAM]["settings"]["audio"] == echoed, (
        "the echoed audio section must be adopted -- it is the re-read after "
        "the write, and polling again would cost two live round trips")
    assert coordinator.refresh_requests == 0

    entity, coordinator = volume_entity({"supports_call_volume": True, "call_volume": 3})
    session, restore = scripted_write([FakeResponse(200, {})])
    try:
        await entity.async_set_native_value(5.0)
    finally:
        restore()
    assert coordinator.refresh_requests == 1, (
        "an empty echo must fall back to a real refresh, never to assuming "
        "the write took")


async def test_led_brightness_number_gating_fallback_and_write():
    coordinator = make_coordinator(
        {CAM: {"settings": {"led": {"supports_brightness": True, "brightness": 0}},
               "diagnostics": {}}})
    entity = number.CameraStatusLedBrightnessNumber(coordinator, CAM)
    entity.hass = coordinator.hass
    assert entity.available
    assert entity.native_value == 0.0, "0 is dimmed-all-the-way, not unknown"
    assert entity.native_max_value == 100.0, (
        "no device has ever named a range here; 100 is the documented "
        "fail-loud-not-silent fallback ceiling")

    echoed = {"supports_brightness": True, "brightness": 40, "supports_enabled": True}
    session, restore = scripted_write([FakeResponse(200, echoed)])
    try:
        await entity.async_set_native_value(40.0)
    finally:
        restore()
    request = session.requests[0]
    assert request["url"].endswith(f"/cameras/{CAM}/settings/led")
    assert request["json"] == {"brightness": 40}
    assert coordinator.data[CAM]["settings"]["led"] == echoed, (
        "the echoed led section also refreshes the on/off switch's flag; "
        "adopting it saves that entity a live round trip")

    # The gate is supports_brightness, NOT the switch's supports_enabled: the
    # reference doorbell can switch its LED but not dim it.
    coordinator = make_coordinator(
        {CAM: {"settings": {"led": {"supports_enabled": True,
                                    "supports_brightness": False}},
               "diagnostics": {}}})
    entity = number.CameraStatusLedBrightnessNumber(coordinator, CAM)
    assert not entity.available


def toggle(cls, audio=None, section=None, section_name="audio", coordinator=None):
    data = {section_name: section if section is not None else audio}
    coordinator = coordinator or make_coordinator(
        {CAM: {"settings": data, "diagnostics": {}}})
    entity = cls(coordinator, CAM)
    entity.hass = coordinator.hass
    return entity, coordinator


async def test_switch_toggles_read_write_key_pairs_and_gates():
    # Indoor chime: reads indoor_chime_on, WRITES indoor_on -- the bridge's
    # read and write vocabularies differ on this route.
    entity, coordinator = toggle(switch.CameraIndoorChimeSwitch,
                                 audio={"supports_indoor_chime": True,
                                        "indoor_chime_on": False})
    assert entity.available
    assert entity.is_on is False, (
        "False is a real answer ('the chime is off') and must not read as the "
        "unknown a missing field produces")
    echoed = {"supports_indoor_chime": True, "indoor_chime_on": True}
    session, restore = scripted_write([FakeResponse(200, echoed)])
    try:
        await entity.async_turn_on()
    finally:
        restore()
    request = session.requests[0]
    assert request["url"].endswith(f"/cameras/{CAM}/settings/chime")
    assert request["json"] == {"indoor_on": True}, (
        f"body {request['json']!r}: the chime route WRITES indoor_on, not the "
        "indoor_chime_on it reads back")
    assert coordinator.data[CAM]["settings"]["audio"] == echoed

    entity, _ = toggle(switch.CameraIndoorChimeSwitch, audio={"indoor_chime_on": True})
    assert not entity.available, "no support flag, no control"
    entity, _ = toggle(switch.CameraIndoorChimeSwitch,
                       audio={"supports_indoor_chime": True})
    assert entity.is_on is None, "a missing field is unknown, never off"

    # Outdoor tone: its own flag and its own write key.
    entity, _ = toggle(switch.CameraOutdoorToneSwitch,
                       audio={"supports_outdoor_tone": True, "outdoor_tone_on": True})
    assert entity.available and entity.is_on is True
    session, restore = scripted_write([FakeResponse(200, {})])
    try:
        await entity.async_turn_off()
    finally:
        restore()
    assert session.requests[0]["json"] == {"outdoor_on": False}

    # Status LED: led section, same name for all three keys.
    entity, _ = toggle(switch.CameraStatusLedSwitch, section_name="led",
                       section={"supports_enabled": True, "enabled": True})
    assert entity.available and entity.is_on is True
    session, restore = scripted_write([FakeResponse(200, {})])
    try:
        await entity.async_turn_off()
    finally:
        restore()
    assert session.requests[0]["url"].endswith(f"/cameras/{CAM}/settings/led")
    assert session.requests[0]["json"] == {"enabled": False}

    # TELUS-app notification toggles: one field name for flag, state and write.
    entity, _ = toggle(switch.CameraButtonPressAppNotificationSwitch,
                       section_name="notifications",
                       section={"supports_button_press": True, "button_press": True})
    assert entity.available and entity.is_on is True
    session, restore = scripted_write([FakeResponse(200, {})])
    try:
        await entity.async_turn_off()
    finally:
        restore()
    assert session.requests[0]["url"].endswith(f"/cameras/{CAM}/settings/notifications")
    assert session.requests[0]["json"] == {"button_press": False}


async def test_indoor_chime_unavailable_while_the_chime_type_disables_it():
    audio = {"supports_indoor_chime": True, "indoor_chime_on": True,
             "chime_type": 0,
             "chime_type_choices": [
                 {"value": 0, "label": "None", "disables_indoor_chime": True},
                 {"value": 1, "label": "Mechanical"}]}
    entity, _ = toggle(switch.CameraIndoorChimeSwitch, audio=audio)
    assert not entity.available, (
        "the selected chime type says there is no wired chime to ring; the "
        "portal greys this control out and so must we")

    audio = dict(audio, chime_type=1)
    entity, _ = toggle(switch.CameraIndoorChimeSwitch, audio=audio)
    assert entity.available

    # The labelled list comes from a secondary upstream call that can fail;
    # not knowing must leave the switch usable, not guess it unavailable.
    audio = {"supports_indoor_chime": True, "indoor_chime_on": True, "chime_type": 0}
    entity, _ = toggle(switch.CameraIndoorChimeSwitch, audio=audio)
    assert entity.available


async def test_privacy_switch_reads_back_instead_of_adopting():
    entity, coordinator = toggle(switch.CameraPrivacyModeSwitch, section_name="privacy",
                                 section={"supported": True, "active": False})
    assert entity.available and entity.is_on is False
    session, restore = scripted_write([FakeResponse(200, {"ok": True})])
    try:
        await entity.async_turn_on()
    finally:
        restore()
    assert session.requests[0]["url"].endswith(f"/cameras/{CAM}/privacy")
    assert session.requests[0]["json"] == {"active": True}
    assert coordinator.refresh_requests == 1, (
        "privacy was never observed live; an accepted request is not proof the "
        "camera moved, so the state must be READ back, not adopted")

    entity, _ = toggle(switch.CameraPrivacyModeSwitch, section_name="privacy",
                       section={"supported": False, "active": False})
    assert not entity.available
    entity, _ = toggle(switch.CameraPrivacyModeSwitch, section_name="privacy", section=None)
    assert not entity.available


async def run_platform_setup(module, settings_payloads):
    """Run a platform's REAL async_setup_entry, then feed the settings
    coordinator one poll and return the entities that appeared."""
    hass = FakeHass()
    entry = FakeConfigEntry()
    camera_coordinator = types.SimpleNamespace(data={cam: {} for cam in settings_payloads})
    controller = types.SimpleNamespace(
        api=types.SimpleNamespace(devices=types.SimpleNamespace(cameras={})))
    hass.data[DOMAIN] = {entry.entry_id: {
        DATA_CONTROLLER: controller,
        DATA_CAMERA_COORDINATOR: camera_coordinator,
    }}
    added = []

    def async_add_entities(new):
        added.extend(new)

    saved = fakes.patch(switch, bridge_get=_absent_bridge)
    try:
        await module.async_setup_entry(hass, entry, async_add_entities)
    finally:
        fakes.restore(switch, saved)

    coordinator = hass.data[DOMAIN][entry.entry_id][DATA_CAMERA_SETTINGS_COORDINATOR]
    coordinator.async_set_updated_data({
        cam: {"settings": payload, "diagnostics": {}}
        for cam, payload in settings_payloads.items()
    })
    return added


async def _absent_bridge(hass, url, params=None, timeout=None):
    return None  # the bridge is down at HA startup; setup must survive that


async def test_number_platform_setup_gates_on_capabilities():
    added = await run_platform_setup(number, {
        "cam-volume": {"audio": {"supports_call_volume": True}},
        "cam-dimmable": {"led": {"supports_brightness": True}},
        "cam-plain": {"audio": {}, "led": {"supports_enabled": True}},
    })
    kinds = sorted(type(entity).__name__ for entity in added)
    assert kinds == ["CameraCallVolumeNumber", "CameraStatusLedBrightnessNumber"], (
        f"created {kinds!r}: exactly one volume slider for the volume-capable "
        "camera and one brightness slider for the dimmable one; the "
        "switchable-but-not-dimmable LED gets NO slider (that is the "
        "reference doorbell, and it is the expected outcome, not a fault)")


async def test_switch_platform_setup_gates_on_capabilities():
    added = await run_platform_setup(switch, {
        "cam-full": {"audio": {"supports_indoor_chime": True,
                               "supports_outdoor_tone": True},
                     "led": {"supports_enabled": True},
                     "notifications": {"supports_motion": True,
                                       "supports_button_press": True}},
        "cam-bare": {"audio": {}},
    })
    kinds = sorted(type(entity).__name__ for entity in added)
    assert kinds == ["CameraButtonPressAppNotificationSwitch",
                     "CameraIndoorChimeSwitch",
                     "CameraMotionAppNotificationSwitch",
                     "CameraOutdoorToneSwitch",
                     "CameraPrivacyModeSwitch",
                     "CameraPrivacyModeSwitch",
                     "CameraStatusLedSwitch"], (
        f"created {kinds!r}: privacy is the ONE deliberate ungated entity "
        "(support and bridge-silence arrive identically, and an entity that "
        "comes and goes takes its history with it); everything else exists "
        "only behind its own support flag -- no smart-chime switch without "
        "supports_smart_chime, no chime/led/notification toggles on the bare "
        "camera")


async def test_automation_rule_switch_round_trip():
    hass = FakeHass()
    entry = FakeConfigEntry()
    coordinator = switch.AutomationRulesCoordinator(hass, entry)
    coordinator.data = [{"id": "rule1", "name": "Porte d'entree", "is_paused": False,
                         "action": "record", "trigger": "motion",
                         "timeframe": "always", "can_be_paused": True}]
    entity = switch.AutomationRuleSwitch(coordinator, entry, "rule1")
    entity.hass = hass
    assert entity.available and entity.is_on is True
    assert entity.name == "Porte d'entree"
    assert entity.extra_state_attributes["trigger"] == "motion"

    session = FakeSession([FakeResponse(200, {})])
    saved = fakes.patch(switch, read_control_secret=ScriptedSecret("s"),
                        async_get_clientsession=fakes.clientsession_for(session))
    try:
        await entity.async_turn_off()
    finally:
        fakes.restore(switch, saved)
    assert session.requests[0]["url"] == (
        "http://127.0.0.1:8199/control/automations/rule1/pause")
    assert coordinator.refresh_requests == 1, (
        "a pause/resume triggers its own refresh rather than waiting out the "
        "slow poll -- the switch would read wrong for minutes otherwise")

    coordinator.data = []
    assert not entity.available, (
        "a rule TELUS stopped reporting (deleted in the app) must go "
        "unavailable, not freeze on its last state")


TESTS = (
    test_bridge_post_maps_every_refusal,
    test_coordinator_degrades_per_camera_never_account_wide,
    test_coordinator_total_failure_and_404_and_no_secret,
    test_apply_settings_section_folds_only_that_section,
    test_add_camera_entities_reoffers_until_the_capability_appears,
    test_volume_number_bounds_values_and_gating,
    test_volume_write_posts_flat_body_and_adopts_the_echo,
    test_led_brightness_number_gating_fallback_and_write,
    test_switch_toggles_read_write_key_pairs_and_gates,
    test_indoor_chime_unavailable_while_the_chime_type_disables_it,
    test_privacy_switch_reads_back_instead_of_adopting,
    test_number_platform_setup_gates_on_capabilities,
    test_switch_platform_setup_gates_on_capabilities,
    test_automation_rule_switch_round_trip,
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
