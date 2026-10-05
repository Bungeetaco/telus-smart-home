"""Tests for the coordinators' rate-limit hold (`bridge_client.RateLimitHoldMixin`).

Run from the repository root:

    python3 tests/test_bridge_hold.py

No Home Assistant, no network, no bridge: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, the coordinators are the REAL classes with their
polling never started, and `bridge_get` is replaced per module with a scripted
stand-in that records every call.

The invisible failure this file exists to catch (backlog E105, observed live
2026-10-05): the alarm.com edge rate-limited the account for ~30 min. The
bridge answered every TELUS-backed route `429` with the edge's own
`Retry-After`, and the coordinators turned each one into an `UpdateFailed` and
came straight back on their next interval -- every 30 s for the two event
pollers -- roughly 120 bridge polls an hour into a window that reopens on every
knock. Nothing raised, the entities just flapped unavailable and back, and the
log line said only "Could not reach camera bridge: 429". Pinned here:

  1. A 429 keeps the last page rather than failing the poll. The data is stale,
     not wrong, and the bridge is reachable, so the entities must stay available
     on what they last knew.
  2. While the hold is active `_async_update_data` returns WITHOUT calling the
     bridge at all. The scripted `bridge_get` counts calls, and the count is
     the assertion.
  3. The hold lasts the bridge's `Retry-After`, bounded, and expires on its
     own: the next cycle after it polls again.
  4. Every TELUS-backed coordinator behaves the same way (events, activity,
     automation rules, both arming-device coordinators, camera settings), and
     the settings sweep stops at the first 429 instead of asking the next
     camera's routes for the same answer.
  5. The two loopback-only coordinators (camera list, stream health) are NOT
     held: nothing Alarm.com-backed is behind them, and holding them would
     blind the stream-health sensors for no gain.
"""
import asyncio
import pathlib
import sys
from typing import Any

import aiohttp
from multidict import CIMultiDict, CIMultiDictProxy

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import fakes  # noqa: E402
import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.telus_smarthome import arming_devices_coordinator as adc  # noqa: E402
from custom_components.telus_smarthome import bridge_client  # noqa: E402
from custom_components.telus_smarthome import camera as camera_module  # noqa: E402
from custom_components.telus_smarthome import camera_settings_coordinator as csc  # noqa: E402
from custom_components.telus_smarthome import event as event_module  # noqa: E402
from custom_components.telus_smarthome import switch as switch_module  # noqa: E402
from custom_components.telus_smarthome.bridge_client import (  # noqa: E402
    RETRY_AFTER_DEFAULT_SECONDS,
    RETRY_AFTER_MAX_SECONDS,
    RateLimitHoldMixin,
    retry_after_seconds,
)


class Clock:
    def __init__(self) -> None:
        self.now = 5000.0

    def __call__(self) -> float:
        return self.now


def http_error(status: int, retry_after: str | None = None) -> aiohttp.ClientResponseError:
    headers = CIMultiDict()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    return aiohttp.ClientResponseError(request_info=None, history=(), status=status,
                                       headers=CIMultiDictProxy(headers))


class ScriptedBridge:
    """A bridge_get stand-in answering from a script, counting every call."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    async def __call__(self, hass, url, *, params=None, timeout=None):
        self.calls.append(url)
        answer = self.answers.pop(0) if self.answers else None
        if isinstance(answer, BaseException):
            raise answer
        return answer


def scripted(module, *answers):
    bridge = ScriptedBridge(*answers)
    saved = fakes.patch(module, bridge_get=bridge)
    return bridge, (lambda: fakes.restore(module, saved))


def clocked(coordinator, clock):
    coordinator._hold_clock = clock
    return coordinator


# ---------------------------------------------------------------------------
# The parser and the mixin on their own
# ---------------------------------------------------------------------------

def test_retry_after_is_read_bounded_and_defaulted():
    assert retry_after_seconds(http_error(429, "120")) == 120.0
    assert retry_after_seconds(http_error(429, "1825")) == 1825.0
    assert retry_after_seconds(http_error(429, "0")) == 1.0, "zero must not mean 'now'"
    assert retry_after_seconds(http_error(429, "999999")) == RETRY_AFTER_MAX_SECONDS
    assert retry_after_seconds(http_error(429, "soon")) == RETRY_AFTER_DEFAULT_SECONDS
    assert retry_after_seconds(http_error(429)) == RETRY_AFTER_DEFAULT_SECONDS
    assert retry_after_seconds(
        aiohttp.ClientResponseError(request_info=None, history=(), status=429)
    ) == RETRY_AFTER_DEFAULT_SECONDS, "a 429 with no headers object at all"
    print("  ok  Retry-After: parsed, floored at 1s, capped, defaulted when unreadable")


def test_mixin_holds_for_the_wait_never_shortens_and_expires():
    class Holder(RateLimitHoldMixin):
        name = "test"

    clock = Clock()
    holder = clocked(Holder(), clock)
    assert not holder._held()
    assert holder._hold_for(http_error(429, "90"), "x") == 90.0
    assert holder._held() and abs(holder._hold_remaining() - 90.0) < 1e-6
    holder._hold_for(http_error(429, "10"), "x")
    assert abs(holder._hold_remaining() - 90.0) < 1e-6, "a shorter wait must not shorten the hold"
    clock.now += 89.9
    assert holder._held()
    clock.now += 0.2
    assert not holder._held(), "the hold must expire on its own"
    print("  ok  the mixin holds for the stated wait, never shortens it, and expires")


# ---------------------------------------------------------------------------
# The coordinators
# ---------------------------------------------------------------------------

async def _check_list_coordinator(module, cls, page_key, label):
    """events / history / automation rules: a list under `page_key`."""
    clock = Clock()
    coordinator = clocked(cls(fakes.FakeHass(), fakes.FakeConfigEntry()), clock)
    first_page = [{"id": "a"}]
    bridge, restore = scripted(
        module, {page_key: first_page}, http_error(429, "120"), {page_key: [{"id": "b"}]})
    try:
        assert await coordinator._async_update_data() == first_page
        coordinator.data = first_page
        # The 429: last page kept, hold started, NOT UpdateFailed.
        assert await coordinator._async_update_data() == first_page, (
            f"{label}: a 429 must keep the last page")
        assert coordinator._held() and abs(coordinator._hold_remaining() - 120) < 1e-6
        assert len(bridge.calls) == 2
        # Held: three more cycles, zero bridge calls.
        for _ in range(3):
            assert await coordinator._async_update_data() == first_page
        assert len(bridge.calls) == 2, f"{label}: held cycles must not call the bridge"
        # Expired: polls again and adopts the new page.
        clock.now += 121
        assert await coordinator._async_update_data() == [{"id": "b"}]
        assert len(bridge.calls) == 3
    finally:
        restore()
    print(f"  ok  {label}: 429 keeps the page, holds 120s without polling, resumes after")


async def test_event_and_rule_coordinators_hold():
    await _check_list_coordinator(event_module, event_module.VideoEventsCoordinator,
                                  "events", "video events")
    await _check_list_coordinator(event_module, event_module.ActivityHistoryCoordinator,
                                  "events", "activity history")
    await _check_list_coordinator(switch_module, switch_module.AutomationRulesCoordinator,
                                  "rules", "automation rules")


async def test_a_held_coordinator_with_no_page_yet_returns_an_empty_page():
    coordinator = clocked(event_module.VideoEventsCoordinator(
        fakes.FakeHass(), fakes.FakeConfigEntry()), Clock())
    bridge, restore = scripted(event_module, http_error(429, "60"))
    try:
        assert await coordinator._async_update_data() == [], (
            "a 429 on the very first poll is an empty page, not a crash")
        assert coordinator._held()
    finally:
        restore()
    print("  ok  a 429 before any page was read yields an empty page")


async def test_other_bridge_errors_still_fail_the_poll():
    coordinator = event_module.VideoEventsCoordinator(fakes.FakeHass(), fakes.FakeConfigEntry())
    for err in (http_error(502), http_error(401), aiohttp.ClientConnectionError("down"),
                asyncio.TimeoutError()):
        bridge, restore = scripted(event_module, err)
        try:
            try:
                await coordinator._async_update_data()
            except ha_stubs.UpdateFailed:
                pass
            else:
                raise AssertionError(f"{err!r} must still be UpdateFailed")
            assert not coordinator._held(), f"{err!r} must not start a hold"
        finally:
            restore()
    print("  ok  502 / 401 / transport failures are still UpdateFailed and never hold")


async def test_arming_coordinators_hold():
    clock = Clock()
    phone = {"id": "ad42", "description": "phone", "disarming_enabled": True}
    coordinator = clocked(adc.ArmingDevicesCoordinator(fakes.FakeHass(), fakes.FakeConfigEntry()),
                          clock)
    coordinator.data = {"ad42": dict(phone)}
    bridge, restore = scripted(adc, http_error(429, "900"), {"devices": [dict(phone)]})
    try:
        assert await coordinator._async_update_data() == {"ad42": dict(phone)}
        assert coordinator._held() and len(bridge.calls) == 1
        assert await coordinator._async_update_data() == {"ad42": dict(phone)}
        assert len(bridge.calls) == 1, "held: no bridge call"
        clock.now += 901
        assert await coordinator._async_update_data() == {"ad42": dict(phone)}
        assert len(bridge.calls) == 2
    finally:
        restore()

    panel = clocked(adc.PanelBluetoothCoordinator(fakes.FakeHass(), fakes.FakeConfigEntry()),
                    clock)
    panel.data = {"pairing_enabled": True}
    bridge, restore = scripted(adc, http_error(429, "30"))
    try:
        assert await panel._async_update_data() == {"pairing_enabled": True}
        assert panel._held()
        assert await panel._async_update_data() == {"pairing_enabled": True}
        assert len(bridge.calls) == 1
    finally:
        restore()
    print("  ok  arming devices + panel Bluetooth: 429 keeps the record and holds")


async def test_settings_sweep_stops_at_the_first_429_and_keeps_every_camera():
    clock = Clock()
    cameras = fakes.FakeConfigEntry()
    camera_coordinator = type("C", (), {"data": {"cam1": {}, "cam2": {}, "cam3": {}}})()
    coordinator = clocked(csc.CameraSettingsCoordinator(
        fakes.FakeHass(), cameras, camera_coordinator), clock)
    previous = {
        "cam1": {"settings": {"old": 1}, "diagnostics": {}},
        "cam2": {"settings": {"old": 2}, "diagnostics": {}},
        "cam3": {"settings": {"old": 3}, "diagnostics": {}},
    }
    coordinator.data = dict(previous)
    saved_secret = fakes.patch(csc, read_control_secret=fakes.ScriptedSecret())
    # cam1 settings OK, cam1 diagnostics OK, cam2 settings -> 429. cam3 never asked.
    bridge, restore = scripted(
        csc, {"new": 1}, {"sig": 1}, http_error(429, "1825"), {"never": "asked"})
    try:
        data = await coordinator._async_update_data()
        assert data["cam1"] == {"settings": {"new": 1}, "diagnostics": {"sig": 1}}
        assert data["cam2"] == previous["cam2"], "the camera that hit the 429 keeps its page"
        assert data["cam3"] == previous["cam3"], "the camera never asked keeps its page"
        assert len(bridge.calls) == 3, f"the sweep must stop at the 429: {bridge.calls}"
        assert coordinator._held() and abs(coordinator._hold_remaining() - 1825) < 1e-6
        # Held: the whole sweep is skipped and the stored page comes back as is
        # (a real refresh stores the page; here the suite does).
        coordinator.data = data
        assert await coordinator._async_update_data() == data
        assert len(bridge.calls) == 3
    finally:
        restore()
        fakes.restore(csc, saved_secret)
    print("  ok  settings sweep: stops at the first 429, keeps every camera, holds 1825s")


async def test_loopback_only_coordinators_are_not_held():
    """The camera list is a bridge-memory read: a 429 cannot come from it, and
    it must keep the plain UpdateFailed contract rather than grow a hold."""
    coordinator = camera_module.CameraBridgeCoordinator(fakes.FakeHass(), fakes.FakeConfigEntry())
    assert not isinstance(coordinator, RateLimitHoldMixin)
    bridge, restore = scripted(camera_module, http_error(429, "60"))
    try:
        try:
            await coordinator._async_update_data()
        except ha_stubs.UpdateFailed:
            pass
        else:
            raise AssertionError("the camera-list coordinator keeps the UpdateFailed contract")
    finally:
        restore()
    print("  ok  the loopback-only camera-list coordinator is unchanged")


async def main() -> int:
    tests = [
        test_retry_after_is_read_bounded_and_defaulted,
        test_mixin_holds_for_the_wait_never_shortens_and_expires,
        test_event_and_rule_coordinators_hold,
        test_a_held_coordinator_with_no_page_yet_returns_an_empty_page,
        test_other_bridge_errors_still_fail_the_poll,
        test_arming_coordinators_hold,
        test_settings_sweep_stops_at_the_first_429_and_keeps_every_camera,
        test_loopback_only_coordinators_are_not_held,
    ]
    failures = 0
    for test in tests:
        try:
            result = test()
            if asyncio.iscoroutine(result):
                await result
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
