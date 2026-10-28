"""The reconnect delay for the push websocket, as a pure function.

Kept out of controller.py so it can be pinned by the integration test harness
(tests/ha_stubs.py stubs controller.py wholesale because importing it applies
the websocket monkeypatches and drags the real library in).

Backlog E106: every websocket connect attempt fetches a fresh token over REST
(pyalarmdotcomajax.WebSocketClient._connect), and the attempts seen failing
live are TCP connect timeouts to webskt.alarm.com arriving in streaks -- up to
16 an hour, one every ~36 s on the fixed reconnect delay. Each of those is one
more request against an edge that rate-limits the whole account. The delay
below doubles per consecutive failed attempt and resets once a connection
reaches RUNNING; a healthy socket that the server drops on its routine ~6 min
cycle is not a failure and keeps the fast fixed delay.
"""
from __future__ import annotations


def reconnect_delay(base_s: float, consecutive_failures: int, cap_s: float) -> float:
    """Seconds to wait before the next websocket connect attempt.

    ``consecutive_failures`` counts attempts in a row that ended WITHOUT ever
    reaching RUNNING. Zero (the attempt before this one connected) is the plain
    configured delay; each failure doubles it, bounded by ``cap_s``. The cap is
    never below the base: a cap misconfigured under the base would otherwise
    make failures reconnect FASTER than successes.
    """
    base = max(float(base_s), 0.0)
    cap = max(float(cap_s), base)
    if consecutive_failures <= 0:
        return base
    return min(base * (2 ** consecutive_failures), cap)
