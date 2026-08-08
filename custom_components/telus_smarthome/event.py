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

from homeassistant.util import dt as dt_util

from .bridge_client import bridge_get
from .camera import CameraBridgeCoordinator
from .clip_view import make_clip_link
from .const import (
    CAMERA_BRIDGE_EVENTS_URL,
    CAMERA_BRIDGE_HISTORY_URL,
    CLIP_LINK_TTL_SECONDS,
    CONF_CLIP_LINK_SECRET,
    DATA_ACTIVITY_COORDINATOR,
    DATA_CAMERA_COORDINATOR,
    DOMAIN,
    VIDEO_EVENTS_POLL_INTERVAL_SECONDS,
)

LOGGER = logging.getLogger(__name__)

EVENT_TYPES = ["person", "vehicle", "animal", "other"]


def _clip_link_for(config_entry: ConfigEntry, video_event_id: str | None) -> str | None:
    """Signed durable link to this clip's redirect view, or None for
    entries with no correlated clip.

    The download_url attribute alongside it expires within minutes of the
    poll that fetched it; this path re-mints a fresh URL at click time (see
    clip_view.py), so it's what notifications should put behind their tap
    action -- prefixed with whatever base URL reaches this HA instance from
    the phone.
    """
    if video_event_id is None:
        return None
    secret = config_entry.data.get(CONF_CLIP_LINK_SECRET)
    if not secret:
        return None
    # Expiry is stamped at link-creation time and signed into the link, so a
    # notification delivered now stays tappable for the whole TTL window.
    expiry = int(dt_util.utcnow().timestamp()) + CLIP_LINK_TTL_SECONDS
    return make_clip_link(secret, str(video_event_id), expiry)


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
    # TelusCameraEvent above (per-camera, video clips only). Created in
    # __init__.py (not here) and shared via hass.data, same reasoning as
    # camera_coordinator above -- sensor.py's activity-feed sensor reads the
    # exact same coordinator instance instead of polling the bridge again.
    activity_coordinator: ActivityHistoryCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        DATA_ACTIVITY_COORDINATOR
    ]
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
        # ...but only if there was actually anything to take a baseline FROM. The
        # camera list and the event list come from two independent coordinators, so
        # this entity is routinely created (camera list already populated) while the
        # events coordinator is still empty -- e.g. the bridge came up a moment later,
        # or its first /control/events poll failed. A None baseline then means "fire
        # everything", so the first successful poll replayed up to 20 historical clips
        # as brand-new events, triggering every automation attached to them. Adopt the
        # first non-empty page as the baseline instead.
        self._baseline_pending = not (coordinator.data or [])

    def _events_for_camera(self) -> list[dict[str, Any]]:
        return [e for e in (self.coordinator.data or []) if e.get("camera_id") == self._camera_id]

    def _newest_id_for_camera(self) -> int | None:
        ids = [int(e["id"]) for e in self._events_for_camera()]
        return max(ids) if ids else None

    @core.callback
    def _handle_coordinator_update(self) -> None:
        """Fire for any event newer than the last one seen, oldest-first."""

        if self._baseline_pending:
            if self.coordinator.data:
                self._baseline_pending = False
                self._last_seen_id = self._newest_id_for_camera()
            super()._handle_coordinator_update()
            return

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
                    # /control/events items ARE videoEvents, so their id is
                    # the video event id (unlike the Activity feed below,
                    # where id is the activity entry's own id).
                    "video_event_id": event["id"],
                    "clip_link": _clip_link_for(self.coordinator.config_entry, event["id"]),
                })
                # Write state after EACH event, not once at the end. _trigger_event
                # only stores __last_event_attributes (it does not write state),
                # so a single trailing write would collapse a multi-event poll to
                # its last event -- every earlier clip's state_changed, and any
                # automation keyed on it, would be lost. One write per event gives
                # each its own state_changed.
                self.async_write_ha_state()
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

        # Baseline is "whatever's on the current page right now", same reasoning as
        # TelusCameraEvent -- a restart must never replay old history as new.
        #
        # Tracked as a set of ids rather than a high-water-mark timestamp. Activity
        # ids are opaque strings (e.g. "M-1693490562889") so they can't be compared
        # for recency, but timestamps turned out to be worse: a strict ">" against
        # the newest-seen timestamp permanently discards any event sharing that exact
        # timestamp, and a doorbell press reliably produces several entries within the
        # same second -- so the first one seen suppressed its siblings forever. String
        # comparison is also unsound across the two formats TELUS actually emits
        # (with and without fractional seconds): "...T10:00:00Z" sorts BELOW
        # "...T10:00:00.5Z", so a fractional-second event could re-fire every poll.
        # Membership in the last page has neither problem, and the page is bounded
        # (20 events) so the set can't grow.
        self._seen_ids: set[str] = {e["id"] for e in (coordinator.data or [])}
        # See TelusCameraEvent for why an empty first page can't be used as a baseline.
        self._baseline_pending = not (coordinator.data or [])

    @core.callback
    def _handle_coordinator_update(self) -> None:
        """Fire for any event not seen on the previous page, oldest-first."""

        events = self.coordinator.data or []

        if self._baseline_pending:
            if events:
                self._baseline_pending = False
                self._seen_ids = {e["id"] for e in events}
            super()._handle_coordinator_update()
            return

        new_events = [e for e in events if e["id"] not in self._seen_ids]
        if new_events:
            # TELUS returns the page newest-first; fire oldest-first so a listener
            # sees them in the order they actually happened. Sort by PARSED
            # datetime, not the raw string: TELUS emits two timestamp formats
            # (with and without fractional seconds), and "...T10:00:00Z" sorts
            # BELOW "...T10:00:00.5Z" as a string -- the same unsoundness the
            # _seen_ids comment documents for equality. Always return a datetime
            # (epoch 0 for an unparseable value) so the key type is uniform and a
            # single malformed entry can't raise TypeError mid-sort.
            def _when(ev: dict[str, Any]):
                return dt_util.parse_datetime(ev["timestamp"]) or dt_util.utc_from_timestamp(0)

            for event in sorted(new_events, key=_when):
                self._trigger_event(_classify(event["description"]), {
                    "description": event["description"],
                    "device_description": event.get("device_description"),
                    "thumbnail_url": event.get("thumbnail_url"),
                    "download_url": event.get("download_url"),
                    "clip_duration_ms": event.get("clip_duration_ms"),
                    "video_event_id": event.get("video_event_id"),
                    "clip_link": _clip_link_for(
                        self.coordinator.config_entry, event.get("video_event_id")),
                })
                # One state write per event -- see TelusCameraEvent for why a
                # single trailing write drops every event but the last when a
                # poll returns several new activity entries at once.
                self.async_write_ha_state()

        # Rebase onto the current page. Anything that has scrolled off it is older
        # than everything still on it, so it can never legitimately reappear as new.
        # Guarded on non-empty so a transient empty response can't clear the set and
        # cause the whole next page to re-fire.
        if events:
            self._seen_ids = {e["id"] for e in events}

        super()._handle_coordinator_update()
