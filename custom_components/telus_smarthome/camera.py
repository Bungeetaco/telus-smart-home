"""Camera platform for the TELUS Smart Home integration.

Cameras aren't part of pyalarmdotcomajax's device model -- they're served
entirely by the standalone camera-bridge container (see camera-bridge/ in
this project's repo), which owns the TELUS video API session, the WebRTC
(Janus) to RTSP transcoding, and its own periodic TELUS camera-list poll.
This platform's job is just to mirror whatever the bridge currently knows
about, via its local /control/cameras endpoint, into HA camera entities --
including picking up cameras added to the TELUS account after startup,
without a restart on either side.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

import aiohttp
from homeassistant import core
from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .const import (
    CAMERA_BRIDGE_CAMERAS_URL,
    CAMERA_BRIDGE_SHARE_DIR,
    CAMERA_DISCOVERY_POLL_INTERVAL_SECONDS,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)


def _read_control_secret() -> str | None:
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


class CameraBridgeCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls the camera-bridge's /control/cameras endpoint.

    Keyed by camera id. A short poll interval is what lets a camera added to
    the TELUS account later be auto-discovered and appear as an HA entity
    with no restart: the bridge's own discovery poll finds it first, and this
    coordinator's next cycle picks it up from there.
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS camera bridge",
            update_interval=timedelta(seconds=CAMERA_DISCOVERY_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch the current camera list from the bridge."""

        secret = await self.hass.async_add_executor_job(_read_control_secret)
        if secret is None:
            # Bridge container isn't up yet / hasn't generated its secret
            # yet. Not an error worth surfacing as UpdateFailed (that would
            # mark every camera entity unavailable on every HA restart until
            # the bridge happens to win the race) -- just report no cameras
            # this cycle; already-created entities keep their last state.
            return self.data or {}

        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                CAMERA_BRIDGE_CAMERAS_URL,
                headers={"Authorization": f"Bearer {secret}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 401:
                    # Secret on disk is stale relative to what the bridge is
                    # actually enforcing (e.g. bridge's shared volume was
                    # wiped and it regenerated a new one) -- will self-heal
                    # next cycle once the file catches up; not fatal.
                    raise UpdateFailed("Camera bridge rejected control secret (401)")
                resp.raise_for_status()
                body = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        return {cam["id"]: cam for cam in body.get("cameras", [])}


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the camera platform."""

    coordinator = CameraBridgeCoordinator(hass, config_entry)
    # Deliberately async_refresh(), not async_config_entry_first_refresh():
    # the latter raises ConfigEntryNotReady on failure, which would fail
    # the *entire* config entry (alarm panel, lock, every other platform)
    # just because the independently-deployed camera-bridge container
    # happens to be briefly unreachable at HA startup. async_refresh()
    # instead logs and leaves coordinator.data empty; camera entities
    # simply appear on a later poll once the bridge responds.
    await coordinator.async_refresh()

    known_ids: set[str] = set()

    @core.callback
    def _add_new_cameras() -> None:
        """Create entities for any camera id not seen before.

        Registered as a coordinator listener, so this runs on every poll --
        the mechanism that makes newly-added TELUS cameras show up in HA
        automatically, without a config entry reload or restart.
        """
        new_ids = set(coordinator.data or {}) - known_ids
        if not new_ids:
            return
        known_ids.update(new_ids)
        LOGGER.info("Adding %d newly discovered TELUS camera(s): %s", len(new_ids), sorted(new_ids))
        async_add_entities(TelusCamera(coordinator, cam_id) for cam_id in new_ids)

    _add_new_cameras()
    config_entry.async_on_unload(coordinator.async_add_listener(_add_new_cameras))


class TelusCamera(CoordinatorEntity[CameraBridgeCoordinator], Camera):
    """A camera served by the TELUS camera-bridge over RTSP.

    No still-image URL is exposed by the bridge, so thumbnails/snapshots are
    grabbed from the live RTSP stream itself via the `stream` integration
    (use_stream_for_stills=True) -- the same mechanism the Generic Camera
    integration used for these cameras previously. The base Camera class
    defaults use_stream_for_stills to False (expecting a still-image URL)
    and, unlike most Camera properties, does not support an `_attr_`
    override -- it must be overridden as a property directly, or every
    snapshot/thumbnail request 500s with NotImplementedError since we have
    no async_camera_image override either.
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    @property
    def use_stream_for_stills(self) -> bool:
        """Grab thumbnails/snapshots from the live RTSP stream."""
        return True

    def __init__(self, coordinator: CameraBridgeCoordinator, camera_id: str) -> None:
        """Initialize the camera entity."""
        CoordinatorEntity.__init__(self, coordinator)
        Camera.__init__(self)

        self._camera_id = camera_id
        self._attr_unique_id = camera_id

        cam = coordinator.data[camera_id]
        connections = {(dr.CONNECTION_NETWORK_MAC, cam["mac_address"])} if cam.get("mac_address") else set()
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, camera_id)},
            connections=connections,
            name=cam["name"],
            manufacturer="TELUS Smart Home",
            model=cam.get("model"),
            sw_version=cam.get("firmware"),
        )

    @property
    def _camera(self) -> dict[str, Any] | None:
        """Current bridge-reported data for this camera, if still present."""
        return self.coordinator.data.get(self._camera_id)

    @property
    def available(self) -> bool:
        """Unavailable if the coordinator itself is unhealthy, or the bridge
        stops reporting this specific camera (e.g. removed from the TELUS
        account) -- matching main.py's discovery loop, which never tears
        down a camera on the bridge side just because it briefly stops
        appearing in a response, so this generally only trips on a real
        removal, not a transient hiccup.
        """
        return super().available and self._camera is not None

    async def stream_source(self) -> str | None:
        """Return the RTSP URL the camera-bridge is publishing to."""
        cam = self._camera
        return cam["rtsp_url"] if cam else None
