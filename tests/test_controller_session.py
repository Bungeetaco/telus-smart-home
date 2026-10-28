"""Tests for the controller's ownership of its aiohttp session (`controller.py`).

Run from the repository root:

    python3 tests/test_controller_session.py

Why it is pinned (backlog E108): Home Assistant builds every session it hands
out on one connector shared by the whole process, and wraps the session's
`close()` to warn when an integration calls it, because closing the session
closes that connector for everyone. The controller used to end its life with
the library's `close_websession()`, which is exactly that call: Home Assistant
logged "closes the Home Assistant aiohttp session" at every unload and stop.
The invisible failure in the other direction is the one the old call was
guarding against: a session that is neither closed nor released outlives the
reload that replaced it, one per options save, reauth or reconfigure. So the
rules are: the controller creates its own session with `auto_cleanup=False`
(it owns the lifetime), hands that same object to the library, and on stop
releases it with `detach()` -- never `close()` -- and releasing twice is
harmless because unload and the HA-stop listener can both get there.
"""

import asyncio
import pathlib
import sys
from typing import Any

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

from fakes import FakeConfigEntry, FakeHass, patch, restore  # noqa: E402

controller_module = ha_stubs.load_real_controller()


class RecordingSession:
    """An aiohttp ClientSession stand-in that only records its lifecycle calls."""

    def __init__(self) -> None:
        self.detached = 0
        self.closed = 0

    def detach(self) -> None:
        self.detached += 1

    async def close(self) -> None:
        self.closed += 1


class RecordingApi:
    """The pyalarmdotcomajax controller surface stop() touches."""

    def __init__(self, websession: Any) -> None:
        self.websession = websession
        self.websocket_stops = 0
        self.session_closes = 0

    def stop_websocket(self) -> None:
        self.websocket_stops += 1

    async def close_websession(self) -> None:
        self.session_closes += 1
        await self.websession.close()


def _controller() -> controller_module.AlarmIntegrationController:
    hass = FakeHass()
    entry = FakeConfigEntry(entry_id="e1", data={"username": "u", "password": "p"})
    return controller_module.AlarmIntegrationController(hass, entry)


async def test_initialize_lite_owns_its_session_and_hands_it_to_the_library():
    created: list[dict[str, Any]] = []
    session = RecordingSession()
    built: list[dict[str, Any]] = []

    def fake_create(hass: Any, **kwargs: Any) -> RecordingSession:
        created.append(kwargs)
        return session

    class FakeLib:
        def __init__(self, **kwargs: Any) -> None:
            built.append(kwargs)

        async def async_login(self) -> None:
            return None

    ctrl = _controller()
    saved = patch(controller_module, async_create_clientsession=fake_create,
                  libAlarmController=FakeLib)
    try:
        await ctrl.initialize_lite(username="u", password="p", twofactorcookie=None)
    finally:
        restore(controller_module, saved)

    assert len(created) == 1, f"expected exactly one session creation; got {created}"
    kw = created[0]
    assert kw.get("auto_cleanup") is False, (
        f"the controller must create its session with auto_cleanup=False, so it "
        f"owns the lifetime instead of HA also holding it until shutdown; got {created}")
    # E106: the session must bound the push websocket's TCP+TLS connect phase so a
    # stalled connect to webskt.alarm.com fails fast (sock_connect) instead of
    # burning aiohttp's 30 s default and driving the reconnect backoff to its cap,
    # while keeping the overall response budget (total) explicit.
    timeout = kw.get("timeout")
    assert timeout is not None, (
        f"the controller must bound the session's connect phase (E106); got {created}")
    assert timeout.sock_connect == controller_module.WEBSOCKET_CONNECT_TIMEOUT_SECONDS, (
        f"sock_connect must bound the connect phase to "
        f"{controller_module.WEBSOCKET_CONNECT_TIMEOUT_SECONDS}s (E106); "
        f"got {timeout.sock_connect}")
    assert timeout.total == controller_module.WEBSOCKET_SESSION_TOTAL_TIMEOUT_SECONDS, (
        f"total must stay at the explicit response budget "
        f"{controller_module.WEBSOCKET_SESSION_TOTAL_TIMEOUT_SECONDS}s; got {timeout.total}")
    assert built[0]["websession"] is session, (
        "the library must be handed the very session the controller created")
    assert ctrl._websession is session, (
        "the controller must keep the session so stop() can release it")
    print("  ok  initialize_lite creates an owned session (auto_cleanup=False) and gives it to the library")


async def test_stop_releases_the_session_and_never_closes_it():
    ctrl = _controller()
    session = RecordingSession()
    ctrl._websession = session
    ctrl.api = RecordingApi(session)
    ctrl._stop_keep_alive = lambda: None

    await ctrl.stop()

    assert ctrl.api.websocket_stops == 1
    assert session.detached == 1, "stop() must detach the owned session"
    assert session.closed == 0 and ctrl.api.session_closes == 0, (
        "stop() must never close the session: HA built it on the process-wide "
        "shared connector, and close() closes that connector for every integration")
    assert ctrl._websession is None, "a released session must not be reachable again"
    print("  ok  stop() detaches the owned session and never closes it")


async def test_release_is_idempotent_and_safe_before_initialize():
    ctrl = _controller()
    ctrl.release_websession()  # never initialized: nothing to release, no error

    session = RecordingSession()
    ctrl._websession = session
    ctrl.api = RecordingApi(session)
    ctrl._stop_keep_alive = lambda: None
    await ctrl.stop()
    await ctrl.stop()  # the HA-stop listener after an unload, or vice versa

    assert session.detached == 1, (
        f"detach once, however many times stop() runs; got {session.detached}")
    assert session.closed == 0
    print("  ok  releasing twice detaches once; releasing before initialize is a no-op")


TESTS = (
    test_initialize_lite_owns_its_session_and_hands_it_to_the_library,
    test_stop_releases_the_session_and_never_closes_it,
    test_release_is_idempotent_and_safe_before_initialize,
)


def main() -> int:
    failures = 0
    for test in TESTS:
        try:
            asyncio.run(test())
        except Exception as err:  # noqa: BLE001
            failures += 1
            print(f"  FAIL {test.__name__}: {type(err).__name__}: {err}")
    print("RESULT: " + ("PASS" if failures == 0 else f"FAIL ({failures})"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
