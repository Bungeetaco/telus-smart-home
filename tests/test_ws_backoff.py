"""Tests for the push websocket's reconnect delay (`ws_backoff.py`).

Run from the repository root:

    python3 tests/test_ws_backoff.py

A pure function; the stubs are installed only because importing anything under
the package runs its `__init__`, which applies the library monkeypatches. Why
it is pinned at all (backlog E106): every
websocket connect attempt costs a REST token fetch against an edge that
rate-limits the whole account, and the failures seen live are TCP connect
timeouts that arrive in streaks. The invisible failure is the one in the
OTHER direction: a back-off that also slowed the reconnect after the server's
routine ~6 min drop of a HEALTHY socket would leave push dead for minutes at a
time with nothing in the log, which is exactly the state the heartbeat patch
was written to end. So the rules are: a success keeps the fast fixed delay,
failures double it, the cap bounds it, and the cap can never make failures
reconnect faster than successes.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.telus_smarthome_alarmdotcom.ws_backoff import reconnect_delay  # noqa: E402


def test_no_failures_is_the_plain_delay():
    assert reconnect_delay(30, 0, 300) == 30
    assert reconnect_delay(30, -1, 300) == 30, "a negative count is not a failure"
    print("  ok  a socket that connected reconnects on the configured delay")


def test_failures_double_up_to_the_cap():
    seen = [reconnect_delay(30, n, 300) for n in range(1, 7)]
    assert seen == [60, 120, 240, 300, 300, 300], seen
    print("  ok  failed attempts double 30 -> 60 -> 120 -> 240 and stop at the 300 cap")


def test_the_cap_never_undercuts_the_base():
    assert reconnect_delay(30, 3, 10) == 30, (
        "a cap below the base would make failures reconnect FASTER than successes")
    assert reconnect_delay(30, 0, 10) == 30
    print("  ok  a cap below the base is raised to it")


def test_zero_base_stays_zero():
    assert reconnect_delay(0, 5, 300) == 0, "doubling nothing is nothing"
    print("  ok  a zero base (reconnect immediately) is left alone")


def main() -> int:
    failures = 0
    for test in (test_no_failures_is_the_plain_delay,
                 test_failures_double_up_to_the_cap,
                 test_the_cap_never_undercuts_the_base,
                 test_zero_base_stays_zero):
        try:
            test()
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
