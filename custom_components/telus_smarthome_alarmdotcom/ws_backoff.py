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

The FIRST failure in a streak is retried almost at once instead: measured from
the HA host and from a second machine on the same network, SYNs to the
websocket host are lost independently at a 20-40 % rate while every other
Alarm.com/TELUS host connects every time, and a successful connect completes
in well under a second. One quick retry therefore clears most streaks at the
cost of a single extra token fetch, where the doubling alone left the push
feed blind for the whole first back-off window (the 11-minute startup gap
seen live). The doubling still applies from the second consecutive failure.
"""
from __future__ import annotations

QUICK_RETRY_SECONDS = 2.0


def reconnect_delay(base_s: float, consecutive_failures: int, cap_s: float,
                    quick_retry_s: float = QUICK_RETRY_SECONDS) -> float:
    """Seconds to wait before the next websocket connect attempt.

    ``consecutive_failures`` counts attempts in a row that ended WITHOUT ever
    reaching RUNNING. Zero (the attempt before this one connected) is the plain
    configured delay. One is the quick retry (never longer than the base: a
    base below it is kept, since the operator asked for something faster). From
    two on, each failure doubles the base, bounded by ``cap_s``. The cap is
    never below the base: a cap misconfigured under the base would otherwise
    make failures reconnect FASTER than successes.
    """
    base = max(float(base_s), 0.0)
    cap = max(float(cap_s), base)
    if consecutive_failures <= 0:
        return base
    if consecutive_failures == 1:
        return min(max(float(quick_retry_s), 0.0), base)
    return min(base * (2 ** (consecutive_failures - 1)), cap)
