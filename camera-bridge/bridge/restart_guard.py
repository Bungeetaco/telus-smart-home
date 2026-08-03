"""Restart-frequency guard for the deliberate process-death paths.

Two places in this bridge end the process on purpose so Docker's
`restart: unless-stopped` brings up a clean one: camera_session.py's
MAX_CONSECUTIVE_SESSION_FAILURES escape hatch (os._exit) and main.py's
_login_with_retry giving up at startup. Both are correct responses to a
*local* problem -- a leaked/wedged aiortc resource that only a fresh process
can release, a boot that landed during a blip -- and both are the wrong
response to a *sustained upstream* failure, where the restart just re-runs a
full SSO login against the Cloudflare-fronted auth gateway and re-triggers the
same rate-limit window (the compounding failure main.py's _login_with_retry
documents as having turned one bad night into a multi-hour outage).

From inside the reconnect loop the two are indistinguishable, and with a
restart policy behind them the second case had no floor at all: measured on
this deployment, 89 container restarts in 7 days including one stretch that
looped tightly for 14 minutes -- the reaction actively prolonging the outage it
was reacting to.

Because these paths work by killing the process, nothing in memory can count
them; the count has to outlive the process. This module keeps a small JSON file
of recent exit timestamps on the /shared bind mount (survives a process exit
and a container recreate alike) and, once more than RESTART_GUARD_MAX_EXITS
have happened inside RESTART_GUARD_WINDOW_S, hands the caller a backoff to
sleep *before* exiting. The sleep runs in-process, so Docker can't restart the
container until it finishes -- a hard floor on restart frequency -- while the
rest of the bridge (the other cameras, the control endpoint, HA's status polls)
keeps serving in the meantime.

The first RESTART_GUARD_MAX_EXITS exits in a window are deliberately NOT
delayed, so a genuine one-off wedge still recycles as fast as it always did.

Fail-open by design: any problem reading or writing the state file degrades to
"exit immediately", i.e. the pre-guard behaviour. A broken guard must never be
the reason a genuinely wedged bridge can't recycle.
"""

import json
import logging
import os
import time

from . import config as c

log = logging.getLogger(__name__)


def _read_recent(now: float) -> list[float]:
    """Recent exit timestamps still inside the window, oldest first."""
    try:
        with open(c.RESTART_GUARD_STATE_PATH) as f:
            stamps = [float(t) for t in json.load(f)["exits"]]
    except FileNotFoundError:
        return []
    except (json.JSONDecodeError, KeyError, AttributeError, TypeError, ValueError, OSError):
        # Same shape-tolerance as main.py's credentials-file handling: valid
        # JSON of the wrong shape is as likely as a truncated file, and neither
        # may be allowed to raise out of the escape hatch's call site.
        log.warning("Restart-guard state at %s unreadable/corrupt; treating this as the "
                    "first exit in the window", c.RESTART_GUARD_STATE_PATH, exc_info=True)
        return []
    # Wall clock, not monotonic: the point is to survive process death, and
    # monotonic() means nothing across processes. Entries dated in the future
    # (an NTP step on a host that just booted) are dropped rather than left
    # pinned inside the window forever.
    return sorted(t for t in stamps if now - c.RESTART_GUARD_WINDOW_S <= t <= now)


def _write(stamps: list[float]) -> None:
    try:
        os.makedirs(c.SHARED_DIR, exist_ok=True)
        tmp_path = f"{c.RESTART_GUARD_STATE_PATH}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"exits": stamps}, f)
        # Atomic rename, same pattern as control_server.py's secret write: a
        # process that dies mid-write (entirely plausible here -- we are about
        # to exit on purpose) must never leave a half-written state file that
        # disables the guard on the next start.
        os.replace(tmp_path, c.RESTART_GUARD_STATE_PATH)
    except OSError:
        log.warning("Could not persist restart-guard state to %s; this exit won't be "
                    "counted by the next process", c.RESTART_GUARD_STATE_PATH, exc_info=True)


def record_exit() -> tuple[float, int]:
    """Record a deliberate exit and return (seconds_to_sleep_first, exits_in_window).

    A return of 0.0 means "exit now" -- the pre-guard behaviour, which is what
    the first RESTART_GUARD_MAX_EXITS exits in a window get. Beyond that the
    delay doubles per extra exit, capped at RESTART_GUARD_BACKOFF_MAX_S.

    The exit is recorded *before* the caller sleeps, not after: if the sleep is
    cut short by a `docker restart` or an OOM kill, the restart still happened
    and must still count. Reading, appending and writing all happen without an
    await in between, so two cameras hitting the hatch in the same event loop
    can't interleave and lose each other's entry.
    """
    now = time.time()
    recent = _read_recent(now)
    recent.append(now)
    _write(recent)

    if len(recent) <= c.RESTART_GUARD_MAX_EXITS:
        return 0.0, len(recent)
    over = len(recent) - c.RESTART_GUARD_MAX_EXITS
    delay = min(c.RESTART_GUARD_BACKOFF_INITIAL_S * 2 ** (over - 1),
                c.RESTART_GUARD_BACKOFF_MAX_S)
    return float(delay), len(recent)
