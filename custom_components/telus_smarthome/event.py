"""Video clip event platform for the TELUS Smart Home integration.

Like cameras, video clip events are served by the camera-bridge (see
camera-bridge/ in this project's repo) rather than pyalarmdotcomajax's
device model -- the bridge proxies TELUS's video-events API. One
EventEntity is created per known camera (sharing that camera's device via
identical device identifiers, so it shows up as a second entity on the same
device rather than a separate one), and fires whenever a clip newer than
whatever existed at entity-creation time appears in a poll -- so a restart
never replays old history as "new" events.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant import core
from homeassistant.components.event import EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .bridge_client import bridge_get
from .camera import CameraBridgeCoordinator
from .const import (
    CAMERA_BRIDGE_EVENTS_URL,
    CAMERA_BRIDGE_HISTORY_URL,
    DATA_CAMERA_COORDINATOR,
    DOMAIN,
    VIDEO_EVENTS_POLL_INTERVAL_SECONDS,
)

LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ["person", "vehicle", "animal", "other"]


def _classify(description: str) -> str:
    """Map TELUS's free-text event description to one of EVENT_TYPES.

    TELUS doesn't expose a documented enum for this (eventType is an
    opaque numeric code with no confirmed mapping) -- description text is
    the only reliably-observed signal, e.g. "Déclenché par caméra (Une
    personne)" / "(Un animal)". Matches both the French UI text observed
    live and the English equivalents, in case the account locale ever
    changes.
    """
    text = description.lower()
    if "personne" in text or "person" in text:
        return "person"
    if "véhicule" in text or "vehicule" in text or "vehicle" in text:
        return "vehicle"
    if "animal" in text:
        return "animal"
    return "other"


class VideoEventsCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Polls the camera-bridge's /control/events endpoint.

    Events across all cameras, newest first -- entities filter to their own
    camera_id client-side (see TelusCameraEvent below).
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS video events",
            update_interval=timedelta(seconds=VIDEO_EVENTS_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Fetch the current video event list from the bridge."""

        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_EVENTS_URL, params={"limit": "20"})
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        if body is None:
            # Bridge not up yet -- not fatal, see CameraBridgeCoordinator in
            # camera.py for the identical reasoning.
            return self.data or []

        return body.get("events", [])


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the video event platform."""

    camera_coordinator: CameraBridgeCoordinator = hass.data[DOMAIN][config_entry.entry_id][DATA_CAMERA_COORDINATOR]

    events_coordinator = VideoEventsCoordinator(hass, config_entry)
    await events_coordinator.async_refresh()

    known_camera_ids: set[str] = set()

    @core.callback
    def _add_new_camera_events() -> None:
        """Create an event entity for any camera id not seen before.

        Mirrors camera.py's _add_new_cameras -- a camera added to the TELUS
        account later gets its event entity the same way a camera added
        later gets its camera entity, no restart needed.
        """
        new_ids = set(camera_coordinator.data or {}) - known_camera_ids
        if not new_ids:
            return
        known_camera_ids.update(new_ids)
        async_add_entities(TelusCameraEvent(events_coordinator, cam_id) for cam_id in new_ids)

    _add_new_camera_events()
    config_entry.async_on_unload(camera_coordinator.async_add_listener(_add_new_camera_events))

    # System-wide Activity feed -- one entity for the whole account, unlike
    # TelusCameraEvent above (per-camera, video clips only). See
    # ActivityHistoryCoordinator/TelusActivityEvent below.
    activity_coordinator = ActivityHistoryCoordinator(hass, config_entry)
    await activity_coordinator.async_refresh()
    async_add_entities([TelusActivityEvent(activity_coordinator, config_entry)])


class TelusCameraEvent(CoordinatorEntity[VideoEventsCoordinator], EventEntity):
    """Fires when a new video clip event (person/vehicle/animal/other
    detected) is recorded for one camera."""

    _attr_has_entity_name = True
    _attr_translation_key = "video_event"
    _attr_device_class = EventDeviceClass.MOTION
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: VideoEventsCoordinator, camera_id: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._camera_id = camera_id
        self._attr_unique_id = f"{camera_id}_video_event"
        # Same identifier as camera.py's TelusCamera -- HA merges entities
        # with matching device identifiers onto one device, so this becomes
        # a second entity on the existing camera device rather than a new
        # device of its own.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, camera_id)})

        # Baseline is "whatever's newest right now" -- anything older is
        # history, not a new event, even on first ever startup. This is
        # deliberately re-derived fresh on every entity creation (including
        # after a restart), so a restart can never cause old clips to be
        # replayed as new events.
        self._last_seen_id = self._newest_id_for_camera()

    def _events_for_camera(self) -> list[dict[str, Any]]:
        return [e for e in (self.coordinator.data or []) if e.get("camera_id") == self._camera_id]

    def _newest_id_for_camera(self) -> int | None:
        ids = [int(e["id"]) for e in self._events_for_camera()]
        return max(ids) if ids else None

    @core.callback
    def _handle_coordinator_update(self) -> None:
        """Fire for any event newer than the last one seen, oldest-first."""

        new_events = [
            e for e in self._events_for_camera()
            if self._last_seen_id is None or int(e["id"]) > self._last_seen_id
        ]
        if new_events:
            for event in sorted(new_events, key=lambda e: int(e["id"])):
                self._trigger_event(_classify(event["description"]), {
                    "description": event["description"],
                    "thumbnail_url": event.get("thumbnail_url"),
                    "download_url": event.get("download_url"),
                    "clip_duration_ms": event.get("clip_duration_ms"),
                })
            self._last_seen_id = max(int(e["id"]) for e in new_events)

        super()._handle_coordinator_update()


class ActivityHistoryCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Polls the camera-bridge's /control/history endpoint -- TELUS's
    system-wide Activity feed (arm/disarm, door/lock, camera triggers,
    doorbell presses, etc), not just camera clips (VideoEventsCoordinator
    above is video-only). Newest first, matching the bridge's own ordering.
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS activity history",
            update_interval=timedelta(seconds=VIDEO_EVENTS_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Fetch the current activity feed from the bridge."""

        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_HISTORY_URL, params={"limit": "20"})
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        if body is None:
            # Bridge not up yet -- not fatal, see CameraBridgeCoordinator in
            # camera.py for the identical reasoning.
            return self.data or []

        return body.get("events", [])


class TelusActivityEvent(CoordinatorEntity[ActivityHistoryCoordinator], EventEntity):
    """Fires for every new entry in TELUS's system-wide Activity feed.

    One entity for the whole account (unlike TelusCameraEvent, which is
    per-camera and video-clip-only) -- this also covers non-video activity
    (arm/disarm, door/lock) that TelusCameraEvent never sees. Entries the
    bridge could correlate to a saved clip (camera triggers, doorbell
    presses) carry the same thumbnail_url/download_url/clip_duration_ms
    attributes as TelusCameraEvent, so a dashboard card can offer the same
    "click to see the clip" behavior for both -- e.g. an "Person at door"
    entry here links to the exact same clip a camera's own event entity
    would show for it.
    """

    _attr_has_entity_name = True
    # None (not "Activity") -- has_entity_name=True + a set name combines with
    # the device's own name ("Activity" below) to produce a redundant
    # entity_id (event.activity_activity, confirmed live 2026-07-21). None
    # here means the entity is named purely from the device, same pattern
    # TelusCameraEvent below relies on for its own camera devices.
    _attr_name = None
    _attr_translation_key = "activity_event"
    _attr_event_types = EVENT_TYPES

    def __init__(self, coordinator: ActivityHistoryCoordinator, config_entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{config_entry.entry_id}_activity"
        # Not part of any existing camera/panel device -- this feed spans the
        # whole system (arm/disarm included), so it gets its own small device
        # rather than incorrectly attaching to one camera or the alarm panel.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_activity")},
            name="Activity",
            manufacturer="TELUS Smart Home",
        )

        # Baseline is "whatever's newest right now", same reasoning as
        # TelusCameraEvent -- a restart must never replay old history as new.
        # Activity-feed ids are opaque strings (e.g. "M-1693490562889"), not
        # sortable integers like video event ids, so track by timestamp
        # instead -- these are fixed-width ISO 8601 strings (with or without
        # a fractional-seconds component, both confirmed live), which still
        # compare correctly with plain string ">" up through whole seconds.
        self._last_seen_timestamp: str | None = self._newest_timestamp()

    def _newest_timestamp(self) -> str | None:
        events = self.coordinator.data or []
        return events[0]["timestamp"] if events else None

    @core.callback
    def _handle_coordinator_update(self) -> None:
        """Fire for any event newer than the last one seen, oldest-first."""

        events = self.coordinator.data or []
        new_events = [
            e for e in events
            if self._last_seen_timestamp is None or e["timestamp"] > self._last_seen_timestamp
        ]
        if new_events:
            for event in sorted(new_events, key=lambda e: e["timestamp"]):
                self._trigger_event(_classify(event["description"]), {
                    "description": event["description"],
                    "device_description": event.get("device_description"),
                    "thumbnail_url": event.get("thumbnail_url"),
                    "download_url": event.get("download_url"),
                    "clip_duration_ms": event.get("clip_duration_ms"),
                })
            self._last_seen_timestamp = max(e["timestamp"] for e in new_events)

        super()._handle_coordinator_update()
