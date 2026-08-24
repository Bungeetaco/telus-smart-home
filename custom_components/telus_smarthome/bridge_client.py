"""Shared helpers for polling the camera-bridge's local control server.

Used throughout the integration -- camera.py, event.py, switch.py,
sensor.py, the settings/arming-device coordinators, and the
speak/clip/bypass/unpair services -- each needs the same "read the shared
secret off disk, then make a Bearer-authed GET" sequence against a
different endpoint, so it lives here once rather than in every caller.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CAMERA_BRIDGE_SHARE_DIR

LOGGER = logging.getLogger(__name__)


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
