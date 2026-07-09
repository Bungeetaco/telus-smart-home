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

from .bridge_client import bridge_get, read_control_secret
from .const import (
    CAMERA_BRIDGE_CAMERAS_URL,
    CAMERA_BRIDGE_SNAPSHOT_URL_TEMPLATE,
    CAMERA_DISCOVERY_POLL_INTERVAL_SECONDS,
    DATA_CAMERA_COORDINATOR,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)


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

        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_CAMERAS_URL)
        except aiohttp.ClientResponseError as err:
            if err.status == 401:
                # Secret on disk is stale relative to what the bridge is
                # actually enforcing (e.g. bridge's shared volume was wiped
                # and it regenerated a new one) -- will self-heal next cycle
                # once the file catches up; not fatal.
                raise UpdateFailed("Camera bridge rejected control secret (401)") from err
            raise UpdateFailed(f"Camera bridge returned HTTP {err.status}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        if body is None:
            # Bridge container isn't up yet / hasn't generated its secret
            # yet. Not an error worth surfacing as UpdateFailed (that would
            # mark every camera entity unavailable on every HA restart until
            # the bridge happens to win the race) -- just report no cameras
            # this cycle; already-created entities keep their last state.
            return self.data or {}

        return {cam["id"]: cam for cam in body.get("cameras", [])}


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the camera platform."""

    # Created once and already refreshed in __init__.py's async_setup_entry
    # (before platforms are forwarded), and shared with event.py -- both
    # need the same camera list, and creating it there means it exists
    # regardless of which platform HA happens to set up first.
    coordinator: CameraBridgeCoordinator = hass.data[DOMAIN][config_entry.entry_id][DATA_CAMERA_COORDINATOR]

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

    Stills/snapshots prefer TELUS's own on-demand snapshot API (via the
    bridge's /control/cameras/<id>/snapshot -- faster, no stream spin-up
    needed) and fall back to grabbing a frame from the live RTSP stream if
    that fails for any reason (bridge down, TELUS-side error, etc). This
    means use_stream_for_stills stays at the base Camera class's default of
    False -- we have our own async_camera_image override instead, which is
    the *other* half of that same either/or switch in HA core's
    _async_get_image (see camera_image()/async_camera_image() below for why
    both halves matter).
    """

    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

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

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a still image, preferring TELUS's on-demand snapshot API.

        Falls back to grabbing a frame from the live RTSP stream (creating
        one if not already running) if the snapshot call fails for any
        reason -- this replicates what HA core's private
        _async_get_stream_image() does for use_stream_for_stills=True
        cameras, since that helper isn't part of the public Camera API and
        importing a private module-level function from HA core is asking
        for a silent breakage on some future core update.
        """
        secret = await self.hass.async_add_executor_job(read_control_secret)
        if secret is not None:
            session = async_get_clientsession(self.hass)
            try:
                async with session.get(
                    CAMERA_BRIDGE_SNAPSHOT_URL_TEMPLATE.format(camera_id=self._camera_id),
                    headers={"Authorization": f"Bearer {secret}"},
                    # Observed live: TELUS's own snapshot latency varies a lot
                    # by camera (Doorbell ~0.3s, Living Room ~6s, occasionally
                    # more) -- 10s was too tight and caused spurious
                    # fallbacks. 20s matches this codebase's existing
                    # convention for "TELUS can be slow to first respond"
                    # (see FIRST_FRAME_TIMEOUT_S/WEBRTCUP_TIMEOUT_S in
                    # camera-bridge/bridge/config.py).
                    timeout=aiohttp.ClientTimeout(total=20),
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.read()
                    # raise_for_status() only catches a non-2xx from the *bridge*; the
                    # bridge itself forwards whatever TELUS's signed snapshot URL
                    # returns with a hardcoded image/jpeg content-type (see
                    # control_server.py's handle_snapshot) even if TELUS's own
                    # response wasn't actually a valid JPEG (e.g. an HTML error page).
                    # Without this check that garbage would be handed to HA core as
                    # a "successful" camera image.
                    if data[:2] == b"\xff\xd8":
                        return data
                    LOGGER.debug(
                        "Snapshot response for %s wasn't a valid JPEG (%d bytes), "
                        "falling back to a live stream frame", self._camera_id, len(data),
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError):
                LOGGER.debug(
                    "On-demand snapshot failed for %s, falling back to a live stream frame",
                    self._camera_id, exc_info=True,
                )

        if not self.stream and CameraEntityFeature.STREAM in self.supported_features:
            self.stream = await self.async_create_stream()
        if self.stream:
            return await self.stream.async_get_image(width=width, height=height)
        return None
