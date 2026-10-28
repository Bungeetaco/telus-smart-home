"""Shared helpers for polling the camera-bridge's local control server.

Used throughout the integration -- camera.py, event.py, switch.py,
sensor.py, the settings/arming-device coordinators, and the
speak/clip/bypass/unpair services -- each needs the same "read the shared
secret off disk, then make a Bearer-authed GET" sequence against a
different endpoint, so it lives here once rather than in every caller.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CAMERA_BRIDGE_SHARE_DIR

LOGGER = logging.getLogger(__name__)

# Bounds on a bridge-stated Retry-After before a coordinator honours it. The
# floor keeps a "0" from meaning "poll again now"; the ceiling mirrors the
# bridge's own RATE_LIMIT_RETRY_MAX_S, a guard against an absurd header rather
# than a second-guess of a real one. The observed window is 1825 s, which is
# why neither cap is 30 minutes: a cap under the real window has the poller
# back 25 s early, re-opening the window it was waiting out.
RETRY_AFTER_DEFAULT_SECONDS = 60.0
RETRY_AFTER_MAX_SECONDS = 2 * 60 * 60.0


def retry_after_seconds(err: aiohttp.ClientResponseError) -> float:
    """The wait a bridge 429 asked for, from its Retry-After header.

    The bridge emits delta-seconds only (control_server._rate_limited), so an
    unreadable or missing value is treated as the default rather than parsed
    as an HTTP-date.
    """
    raw = (err.headers or {}).get("Retry-After") if err.headers is not None else None
    try:
        wait = float(raw) if raw is not None else RETRY_AFTER_DEFAULT_SECONDS
    except (TypeError, ValueError):
        wait = RETRY_AFTER_DEFAULT_SECONDS
    return min(max(wait, 1.0), RETRY_AFTER_MAX_SECONDS)


class RateLimitHoldMixin:
    """Honour a bridge `429 rate_limited` by NOT polling for its Retry-After.

    For the coordinators whose bridge route is a live TELUS website call
    (events, activity history, automation rules, camera settings, arming
    devices). The bridge answers 429 with the alarm.com edge's own wait, or
    with the remaining wait of its process-wide cloud hold (backlog E104),
    and either way the right response is the same: keep what was last read
    and do not ask again until the wait is over. Before this, a 429 was an
    `UpdateFailed` and the coordinator came straight back on its next
    interval -- every 30 s for the two event coordinators -- which is exactly
    the re-knocking that keeps an edge rate-limit window open (observed live
    2026-10-05: ~120 bridge polls per hour into a 30 min block).

    The hold is per coordinator rather than shared because each one is
    answered by the bridge separately, and the bridge is the single place
    that knows whether the edge is still refusing. Mix in BEFORE the
    coordinator base class. Call `_held()` first thing in
    `_async_update_data` and return the last data when it is true; call
    `_hold_for(err, what)` on a 429 and return the last data as well -- the
    data is stale, not wrong, and the bridge is reachable, so this is not an
    update failure that should flip entities to unavailable.
    """

    _hold_until: float = 0.0
    # Overridable for the tests, which drive the hold against a scripted clock.
    _hold_clock = staticmethod(time.monotonic)

    def _hold_remaining(self) -> float:
        return max(0.0, self._hold_until - self._hold_clock())

    def _held(self) -> bool:
        return self._hold_remaining() > 0

    def _hold_for(self, err: aiohttp.ClientResponseError, what: str) -> float:
        """Start (or extend) the hold from a bridge 429. Returns the wait."""
        wait = retry_after_seconds(err)
        until = self._hold_clock() + wait
        if until > self._hold_until:
            self._hold_until = until
            LOGGER.warning(
                "%s: Alarm.com is rate-limiting this account; keeping the last %s "
                "and not polling again for %.0fs (the bridge's Retry-After)",
                getattr(self, "name", type(self).__name__), what, wait)
        return wait


def read_control_secret() -> str | None:
    """Read the bridge's control-endpoint shared secret off the shared mount.

    Blocking file I/O -- must run in an executor, never called directly from
    the event loop. Returns None if the bridge hasn't started/generated its
    secret yet; callers treat that as "nothing to report this cycle" rather
    than an error, since it's a normal, temporary state right after either
    container (re)starts.
    """
    try:
        return (Path(CAMERA_BRIDGE_SHARE_DIR) / "control_secret").read_text().strip()
    except OSError:
        return None


async def bridge_get(
    hass: HomeAssistant, url: str, *, params: dict[str, str] | None = None, timeout: int = 10
) -> dict[str, Any] | None:
    """GET an authenticated bridge control-server endpoint and return its
    parsed JSON body.

    Returns None if the bridge hasn't generated its secret yet (not started,
    or a startup race) -- callers treat that the same as a temporary empty
    result, not an error. Raises aiohttp.ClientError / asyncio.TimeoutError
    on any other failure (bridge unreachable, non-2xx, etc); callers decide
    whether that's fatal for their own purposes.
    """
    secret = await hass.async_add_executor_job(read_control_secret)
    if secret is None:
        return None

    session = async_get_clientsession(hass)
    async with session.get(
        url,
        params=params,
        headers={"Authorization": f"Bearer {secret}"},
        timeout=aiohttp.ClientTimeout(total=timeout),
    ) as resp:
        resp.raise_for_status()
        return await resp.json()
