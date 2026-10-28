"""Video clip event platform for the TELUS Smart Home integration.

Like cameras, video clip events are served by the camera-bridge (see
camera-bridge/ in this project's repo) rather than pyalarmdotcomajax's
device model -- the bridge proxies TELUS's video-events API. One
EventEntity is created per known camera (sharing that camera's device via
identical device identifiers, so it shows up as a second entity on the same
device rather than a separate one), and fires whenever a clip newer than
whatever existed at entity-creation time appears in a poll -- so a restart
never replays old history as "new" events.

Three entity kinds live here: the per-camera video-clip event, the
system-wide Activity event, and the per-doorbell button-press event
(EventDeviceClass.DOORBELL) -- the last of which is what a ring-triggered
automation should trigger on, since it matches TELUS's stable server-side
event_type_name enum rather than the localized description text the other
two classify on.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import aiohttp
from homeassistant import core
from homeassistant.components.event import DoorbellEventType, EventDeviceClass, EventEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from homeassistant.util import dt as dt_util

from .bridge_client import RateLimitHoldMixin, bridge_get
from .camera import CameraBridgeCoordinator
from .camera_settings_coordinator import (
    async_add_camera_entities,
    async_get_camera_settings_coordinator,
)
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

# TELUS's server-side event-type enum for a doorbell button press, captured live
# from /control/history:
#
#   {"description": "Button Push", "device_id": "<unit>-<did>",
#    "device_description": "Doorbell", "event_type_name": "DoorbellButtonPress"}
#
# Matched on INSTEAD of the description text, deliberately and permanently. This
# account's activity descriptions have been observed in both French ("Déclenché
# par caméra (Une personne)", "Mouvement détecté") and English ("Button Push",
# "Locked (Manually locked)") -- the locale is a server-side account property
# that can change without anything on this side being touched. A free-text match
# would then simply stop firing, with no error anywhere to say so: the exact
# silent failure the bridge's regression suites exist to prevent elsewhere.
# event_type_name is an enum and is not translated.
DOORBELL_PRESS_EVENT_TYPE_NAME = "DoorbellButtonPress"

# The one event type the doorbell entity fires: Home Assistant's standard
# `ring` for the doorbell device class, which every doorbell-class entity must
# declare (a deprecation warning now, refused from 2027.4) and which generic
# doorbell automations key on. A single member because TELUS's feed reports one
# press event with no long/double-press distinction; a second type fired for
# the same press would be a second state write and a second automation run.
DOORBELL_EVENT_TYPES = [DoorbellEventType.RING]


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


def _event_time(event: dict[str, Any]) -> datetime:
    """Parsed timestamp of one Activity-feed entry, for ordering.

    Always returns a datetime -- epoch 0 for a missing or unparseable value --
    so a sort key is uniform in type and one malformed entry can't raise
    TypeError mid-sort.

    Sorting on this rather than on the raw string is load-bearing: TELUS emits
    two timestamp formats (with and without fractional seconds), and
    "...T10:00:00Z" sorts BELOW "...T10:00:00.5Z" as a string, so a string sort
    reorders events across that boundary.
    """
    return dt_util.parse_datetime(event.get("timestamp") or "") or dt_util.utc_from_timestamp(0)


def _is_doorbell(camera_data: dict[str, Any]) -> bool:
    """Whether one camera is a doorbell, from capabilities the bridge already reports.

    A chime is what makes a doorbell a doorbell. The `audio` section's
    `supports_indoor_chime` / `supports_outdoor_tone` / `supports_chime_type`
    resolve alarm.com's `indoorChimeEnableDisableFeatureSupport`,
    `outdoorToneEnableDisableFeatureSupport` and `indoorChimeTypeFeatureSupport`
    -- flags a camera with no bell to ring does not report. Any ONE of the three
    is enough: they are independent of each other (switch.py's `_build_chime`
    gates its two toggles on them separately for that reason), so a unit with an
    outdoor tone but no wired-chime support still counts.

    Deliberately NOT `notifications.supports_button_press`, which looks like the
    obvious flag and is not one: the bridge resolves it from
    `panelNotificationEnableDisableFeatureSupport`, the SAME upstream enum behind
    the motion-notification toggle (see camera-bridge/bridge/camera_settings.py's
    _NOTIFICATION_FIELDS), so it reads true for any camera that can push a
    notification at all.

    Deliberately NOT "has a DoorbellButtonPress ever been seen for this camera"
    either, tempting though it is as a direct signal. The activity feed is a
    bounded 20-entry page, so that test answers "no" for a doorbell nobody has
    rung lately -- the entity would appear and vanish across restarts, taking
    its history and breaking any automation that references it, and the very
    press that created it could not fire it (a freshly created entity baselines
    on the page it was born from). A capability flag is stable, is known at
    startup, and needs nobody to ring the bell first.
    """
    audio = (camera_data.get("settings") or {}).get("audio") or {}
    return bool(
        audio.get("supports_indoor_chime")
        or audio.get("supports_outdoor_tone")
        or audio.get("supports_chime_type")
    )


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


class VideoEventsCoordinator(RateLimitHoldMixin, DataUpdateCoordinator[list[dict[str, Any]]]):
    """Polls the camera-bridge's /control/events endpoint.

    Events across all cameras, newest first -- entities filter to their own
    camera_id client-side (see TelusCameraEvent below).

    A bridge 429 (the alarm.com edge rate-limiting the account, or the
    bridge's own cloud hold) keeps the last page and pauses the poll for the
    stated wait -- see RateLimitHoldMixin. The page is stale, not wrong, so
    the event entities stay available and simply fire nothing until the
    bridge is serving again.
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

        if self._held():
            return self.data or []
        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_EVENTS_URL, params={"limit": "20"})
        except aiohttp.ClientResponseError as err:
            if err.status == 429:
                self._hold_for(err, "video event page")
                return self.data or []
            raise UpdateFailed(f"Camera bridge returned HTTP {err.status}") from err
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

    # Per-doorbell button-press entity. Two coordinators, on purpose:
    #  * WHICH cameras get one is decided by the camera-settings coordinator,
    #    because that is where the chime capability flags live -- the same
    #    capability-gated creation path switch.py/number.py/button.py use, via
    #    async_add_camera_entities (a camera is re-offered to the builder every
    #    poll until it yields, so a doorbell added later needs no restart).
    #  * WHEN it fires comes from the activity coordinator, the only feed that
    #    carries presses. So the entity itself is a CoordinatorEntity on the
    #    activity coordinator, and its availability follows that feed's health
    #    rather than the settings poll's.
    settings_coordinator = await async_get_camera_settings_coordinator(hass, config_entry)

    @core.callback
    def _build_doorbell(camera_id: str, camera_data: dict[str, Any]) -> list[EventEntity]:
        """One doorbell-press entity per camera that reports a chime."""
        if not _is_doorbell(camera_data):
            return []
        return [TelusDoorbellEvent(activity_coordinator, camera_id)]

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_doorbell)


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


class ActivityHistoryCoordinator(RateLimitHoldMixin, DataUpdateCoordinator[list[dict[str, Any]]]):
    """Polls the camera-bridge's /control/history endpoint -- TELUS's
    system-wide Activity feed (arm/disarm, door/lock, camera triggers,
    doorbell presses, etc), not just camera clips (VideoEventsCoordinator
    above is video-only). Newest first, matching the bridge's own ordering.

    Same 429 handling as VideoEventsCoordinator (RateLimitHoldMixin): the
    last page is kept and the poll pauses for the bridge's Retry-After.
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

        if self._held():
            return self.data or []
        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_HISTORY_URL, params={"limit": "20"})
        except aiohttp.ClientResponseError as err:
            if err.status == 429:
                self._hold_for(err, "activity page")
                return self.data or []
            raise UpdateFailed(f"Camera bridge returned HTTP {err.status}") from err
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
        # Ids already fired WHILE carrying a clip. TELUS's historyEvents entry can
        # appear a poll or two BEFORE its media relationship is attached, so a
        # doorbell clip's first sighting may have download_url=None (the automation
        # suppresses that, correctly) and the clip only shows up later -- by which
        # time the id is in _seen_ids and would never fire again, permanently losing
        # the clip notification. Tracking whether we have fired an id *with* a clip
        # lets such an event fire exactly once more when the clip finally attaches.
        self._fired_with_clip: set[str] = {
            e["id"] for e in (coordinator.data or []) if e.get("download_url")
        }
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
                self._fired_with_clip = {e["id"] for e in events if e.get("download_url")}
            super()._handle_coordinator_update()
            return

        def _needs_fire(e: dict[str, Any]) -> bool:
            if e["id"] not in self._seen_ids:
                return True
            # Already seen: re-fire only if it has gained a clip it lacked when it
            # first fired (the enrichment race above). Bounded to one extra fire
            # per id by _fired_with_clip, so no duplicate storm.
            return bool(e.get("download_url")) and e["id"] not in self._fired_with_clip

        new_events = [e for e in events if _needs_fire(e)]
        if new_events:
            # TELUS returns the page newest-first; fire oldest-first so a listener
            # sees them in the order they actually happened. Sorted on the parsed
            # datetime rather than the raw string -- see _event_time for why the
            # string is unsound here, the same unsoundness the _seen_ids comment
            # documents for equality.
            for event in sorted(new_events, key=_event_time):
                self._trigger_event(_classify(event["description"]), {
                    "description": event["description"],
                    "device_description": event.get("device_description"),
                    # TELUS's stable server-side enum for what this entry IS
                    # ("DoorbellButtonPress", "AccessControlUnlocked", ...),
                    # unlike `description` above, which is free text and arrives
                    # in the account's own language. Carried so a template or a
                    # condition on this catch-all entity can key off something
                    # that does not translate. A press specifically has its own
                    # entity (TelusDoorbellEvent) and should trigger on that.
                    "event_type_name": event.get("event_type_name"),
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
                if event.get("download_url"):
                    self._fired_with_clip.add(event["id"])

        # Rebase onto the current page. Anything that has scrolled off it is older
        # than everything still on it, so it can never legitimately reappear as new.
        # Guarded on non-empty so a transient empty response can't clear the set and
        # cause the whole next page to re-fire.
        if events:
            self._seen_ids = {e["id"] for e in events}
            # Keep _fired_with_clip bounded to the current page too, so it can't
            # grow without bound over the process lifetime.
            self._fired_with_clip &= self._seen_ids

        super()._handle_coordinator_update()


class TelusDoorbellEvent(CoordinatorEntity[ActivityHistoryCoordinator], EventEntity):
    """Fires when someone presses the button on one doorbell camera.

    The point of this entity is to be the thing a ring-triggered automation
    triggers ON. A press is already visible in TelusActivityEvent's feed, but
    only as a generic `other` event whose description is free text in whatever
    language the account is set to -- so every automation had to pattern-match
    that text, and would have failed silently the day the locale changed. This
    entity matches TELUS's `event_type_name` enum instead
    (DOORBELL_PRESS_EVENT_TYPE_NAME), which does not translate.

    Created only for cameras that report a chime (see `_is_doorbell`), and
    attached to that camera's existing device via the identifier camera.py
    registers it under -- so it appears as another entity on the doorbell, not
    as a device of its own.
    """

    _attr_has_entity_name = True
    _attr_translation_key = "doorbell_press"
    _attr_device_class = EventDeviceClass.DOORBELL
    _attr_event_types = DOORBELL_EVENT_TYPES

    def __init__(self, coordinator: ActivityHistoryCoordinator, camera_id: str) -> None:
        """Initialize the entity for one doorbell camera."""
        super().__init__(coordinator)

        self._camera_id = camera_id
        self._attr_unique_id = f"{camera_id}_doorbell_press"
        # Same identifier camera.py's TelusCamera registers, so HA merges this
        # onto the existing camera device. Identifier only -- name/model/firmware
        # belong to camera.py, and repeating them here would let this entity
        # fight it over the registry entry.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, camera_id)})

        # Dedup + restart safety, reusing TelusActivityEvent's mechanism rather
        # than inventing a second one: the set of press ids on the page as it
        # stands right now is the baseline, and only an id absent from it fires.
        #
        # Why membership in the current page and not a high-water mark: activity
        # ids are opaque strings ("M-1723220865386") so they can't be compared
        # for recency, and a timestamp mark is worse -- a strict ">" permanently
        # discards anything sharing the newest timestamp, and a doorbell press
        # reliably produces several feed entries inside the same second, so the
        # first one seen would suppress its siblings forever.
        #
        # Because the baseline is re-derived at entity creation -- which happens
        # afresh on every HA start, since nothing here is persisted -- a press
        # that is already in the feed when the integration loads is history, not
        # news. That is what stops a restart re-firing the last press (and every
        # automation hanging off it) just because it is the newest thing in the
        # feed.
        self._seen_ids: set[str] = {e["id"] for e in self._presses(coordinator.data or [])}
        # ...unless there was nothing to take a baseline FROM. An empty page
        # means "the bridge isn't up yet / that poll failed", not "no presses
        # exist", and treating it as a baseline would fire every press on the
        # first page that does arrive. Adopt that first non-empty page instead.
        # Same reasoning as TelusCameraEvent's own _baseline_pending.
        self._baseline_pending = not (coordinator.data or [])

    def _presses(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """This camera's button-press entries out of one Activity page.

        Correlated to the camera by `device_id`, which the history feed reports
        as the alarm.com device id ("<unit>-<did>") -- the same id shape the
        bridge's /control/cameras route uses, and therefore expected to be the
        same id this entity was constructed with. Never by `device_description`,
        which is a user-editable label.

        Limit of the evidence: the captured press carries a device_id in that
        shape, but a press and that camera's /control/cameras entry have not been
        observed side by side, so the two ids being byte-identical is inferred
        from the shared id scheme rather than confirmed. If a doorbell entity is
        created and never fires on a real press, this equality is the first thing
        to check -- compare the device_id on a /control/history press against the
        camera's id in /control/cameras.
        """
        return [
            e for e in events
            if e.get("event_type_name") == DOORBELL_PRESS_EVENT_TYPE_NAME
            and e.get("device_id") == self._camera_id
        ]

    @core.callback
    def _handle_coordinator_update(self) -> None:
        """Fire once for each press not on the previous page, oldest-first."""

        events = self.coordinator.data or []

        if self._baseline_pending:
            if events:
                self._baseline_pending = False
                self._seen_ids = {e["id"] for e in self._presses(events)}
            super()._handle_coordinator_update()
            return

        presses = self._presses(events)
        new_presses = [e for e in presses if e["id"] not in self._seen_ids]
        # Oldest-first, so two presses landing in one poll reach a listener in
        # the order they were actually made.
        for press in sorted(new_presses, key=_event_time):
            self._trigger_event(DOORBELL_EVENT_TYPES[0], {
                "event_id": press["id"],
                "timestamp": press.get("timestamp"),
                "camera_id": self._camera_id,
                "device_description": press.get("device_description"),
                # Free text, in the account's language -- carried for display
                # only. Nothing here matches on it; see the module constant.
                "description": press.get("description"),
                "event_type_name": press.get("event_type_name"),
                # Clip fields, same shape as the other two event entities so a
                # dashboard card or notification can treat all three alike.
                # Expect them to be null more often here than on
                # TelusActivityEvent: TELUS attaches the media relationship a
                # poll or two AFTER the history entry appears, and this entity
                # deliberately does not re-fire when it does (a doorbell that
                # rings an automation twice for one press is worse than one
                # whose first notification carries no thumbnail). Use
                # TelusActivityEvent, which does re-fire exactly once on clip
                # arrival, for a clip-carrying notification.
                "thumbnail_url": press.get("thumbnail_url"),
                "download_url": press.get("download_url"),
                "clip_duration_ms": press.get("clip_duration_ms"),
                "video_event_id": press.get("video_event_id"),
                "clip_link": _clip_link_for(
                    self.coordinator.config_entry, press.get("video_event_id")),
            })
            # One state write per press, not one at the end: _trigger_event only
            # stores the attributes, so a single trailing write would collapse a
            # two-press poll into one state_changed and lose the earlier ring.
            self.async_write_ha_state()

        # Rebase onto the presses on the current page. Anything that has scrolled
        # off is older than everything still on it and can never legitimately
        # come back as new, so dropping it can't cause a re-fire -- and this is
        # what keeps the set bounded by the page size (20) forever. Guarded on
        # the raw page being non-empty, not on `presses`: a page with no press on
        # it is a real answer (every earlier press has aged out), while an empty
        # page is a failed poll and must not clear the baseline.
        if events:
            self._seen_ids = {e["id"] for e in presses}

        super()._handle_coordinator_update()
