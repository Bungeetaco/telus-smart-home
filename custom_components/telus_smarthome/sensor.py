"""Diagnostic and data-feed sensor(s) for the TELUS Smart Home integration.

AuthHealthSensor reflects TELUS/Alarm.com session auth health, so the user
can see in Home Assistant (as a normal entity/history graph) when
authentication starts failing, rather than only discovering it via the
reauth persistent-notification or raw logs.

TalkdownStatusSensor / TalkdownFailuresSensor surface the bridge's talkdown
counters, so a talk that starts failing is visible as an entity (and can drive a
notification) rather than only as an HTTP error to whoever triggered it.

ActivityFeedSensor exposes the full recent Activity list (not just the
single latest entry event.py's TelusActivityEvent tracks) as an attribute,
for a dashboard card to render as a scrollable feed -- see that entity's
own docstring for why an EventEntity alone can't do this.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant import core
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .bridge_client import bridge_get
from .const import (
    CAMERA_BRIDGE_METRICS_URL,
    DATA_ACTIVITY_COORDINATOR,
    DATA_CONTROLLER,
    DOMAIN,
    TALK_METRICS_POLL_INTERVAL_SECONDS,
)
from .controller import AlarmIntegrationController
from .event import ActivityHistoryCoordinator, _clip_link_for

LOGGER = logging.getLogger(__name__)

AUTH_HEALTH_STATES = ["ok", "reauth_required", "telus_error"]


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # pylint: disable=unused-argument
) -> None:
    """Set up the sensor platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]
    activity_coordinator: ActivityHistoryCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        DATA_ACTIVITY_COORDINATOR
    ]

    # The talkdown counters live on the bridge, which may not be running (or may
    # have talkdown disabled). Its own coordinator, refreshed once up front: a
    # failure here must not stop the other sensors from being added, so the
    # entities simply report unavailable until a poll succeeds.
    metrics_coordinator = TalkMetricsCoordinator(hass, config_entry)
    await metrics_coordinator.async_config_entry_first_refresh_soft()

    async_add_entities([
        AuthHealthSensor(controller, config_entry),
        ActivityFeedSensor(activity_coordinator, config_entry),
        TalkdownStatusSensor(metrics_coordinator, config_entry),
        TalkdownFailuresSensor(metrics_coordinator, config_entry),
    ])


class AuthHealthSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Diagnostic sensor reflecting TELUS/Alarm.com session auth health.

    Not tied to any single Alarm.com device (unlike the other platforms in
    this integration) — it reports on the health of the integration's own
    connection, so it's grouped under a synthetic "integration" device
    rather than a partition/panel device.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "auth_health"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AUTH_HEALTH_STATES

    def __init__(self, controller: AlarmIntegrationController, config_entry: ConfigEntry) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(controller.update_coordinator)

        self._controller = controller

        self._attr_unique_id = f"{config_entry.entry_id}_auth_health"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
            name="TELUS Smart Home Integration",
            manufacturer="TELUS",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        """Return the current auth health state."""
        return self._controller.auth_health

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional diagnostic detail."""
        return {
            "detail": self._controller.auth_health_detail,
            "last_success": (
                self._controller.last_auth_success.isoformat() if self._controller.last_auth_success else None
            ),
        }


class ActivityFeedSensor(CoordinatorEntity[ActivityHistoryCoordinator], SensorEntity):
    """Exposes the full recent-Activity list as an attribute for a dashboard
    card to render.

    event.py's TelusActivityEvent (an EventEntity) can only ever hold the
    single most-recently-fired event's attributes -- that's the EventEntity
    model, useful for "notify me when something happens" automations but not
    for a browsable list of recent clips. This sensor shares the exact same
    coordinator (no extra bridge polling) and just reflects its whole current
    page of results, so a card can iterate `events` directly.
    """

    _attr_has_entity_name = True
    _attr_name = "Recent Events"
    _attr_icon = "mdi:history"
    # The events blob is several KB and changes on EVERY 30s poll even when nothing
    # happened, because TELUS re-signs the thumbnail/download URLs each time. Recorded
    # normally that is a multi-KB row written to the database ~2,880 times a day
    # forever, for data that is already ephemeral (the signed URLs expire, so old rows
    # are worthless). The state itself (an event count) still records fine.
    _unrecorded_attributes = frozenset({"events"})

    def __init__(self, coordinator: ActivityHistoryCoordinator, config_entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{config_entry.entry_id}_activity_feed"
        # Same device as event.py's TelusActivityEvent (identical identifier)
        # so the two show up together rather than as two synthetic devices.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_activity")},
            name="Activity",
            manufacturer="TELUS Smart Home",
        )

    @property
    def native_value(self) -> int:
        """Number of events in the current page -- a small, glanceable number;
        the actual content lives in extra_state_attributes below."""
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The full current page of Activity-feed events, newest first --
        same shape as control_server.py's /control/history response, plus a
        durable `clip_link` per clip-bearing event so a dashboard card renders
        the same self-re-minting link the notification uses instead of the raw
        download_url (which is already stale by the time the card is viewed).
        New dicts, so the shared coordinator data the EventEntity reads is not
        mutated."""
        entry = self.coordinator.config_entry
        return {
            "events": [
                {**e, "clip_link": _clip_link_for(entry, e.get("video_event_id"))}
                for e in (self.coordinator.data or [])
            ]
        }


class TalkMetricsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the camera-bridge's /control/metrics endpoint.

    Talkdown counters only move when someone triggers a talk, so this polls
    slowly -- it exists so a run of failures becomes visible (and automatable),
    not to watch a clip play.
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS talkdown metrics",
            update_interval=timedelta(seconds=TALK_METRICS_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            payload = await bridge_get(self.hass, CAMERA_BRIDGE_METRICS_URL)
        except Exception as err:  # noqa: BLE001 - aiohttp/timeout variants
            raise UpdateFailed(f"camera bridge metrics unavailable: {err}") from err
        if payload is None:
            # Bridge hasn't written its control secret yet (normal right after a
            # restart) -- keep whatever we had rather than flapping to unavailable.
            return self.data or {}
        return payload.get("talk") or {}

    async def async_config_entry_first_refresh_soft(self) -> None:
        """First refresh that never blocks setup.

        Unlike async_config_entry_first_refresh, a bridge that is down (or older
        than this endpoint) must not abort the whole sensor platform -- these are
        diagnostics, so they just start out unavailable and recover on a later
        poll."""
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Initial talkdown metrics refresh failed; sensors start "
                         "unavailable", exc_info=True)


class _TalkMetricsEntity(CoordinatorEntity[TalkMetricsCoordinator], SensorEntity):
    """Shared plumbing: both talkdown sensors are integration-level diagnostics
    grouped under the same synthetic device as AuthHealthSensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TalkMetricsCoordinator, config_entry: ConfigEntry,
                 key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
            name="TELUS Smart Home Integration",
            manufacturer="TELUS",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _metrics(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data)


class TalkdownStatusSensor(_TalkMetricsEntity):
    """How the most recent talk went: `idle`, `ok`, `stopped`, `rejected`, or
    `failed` (with the reason in an attribute).

    The state is deliberately coarse so it is easy to automate on -- the reason,
    counts and recent history ride along as attributes for a notification
    template or a dashboard.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "ok", "stopped", "rejected", "failed"]

    def __init__(self, coordinator: TalkMetricsCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator, config_entry, "talkdown_status")

    @property
    def native_value(self) -> str:
        recent = self._metrics.get("recent") or []
        if not recent:
            return "idle"
        outcome = str(recent[-1].get("outcome") or "idle")
        # "failed:<reason>" collapses to "failed"; the reason is an attribute.
        return outcome.split(":", 1)[0] if outcome.split(":", 1)[0] in self._attr_options else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._metrics
        recent = m.get("recent") or []
        last = recent[-1] if recent else {}
        return {
            "last_outcome": last.get("outcome"),
            "last_failure_reason": m.get("last_failure_reason"),
            "last_success_at": m.get("last_success_at"),
            "last_failure_at": m.get("last_failure_at"),
            "last_played_seconds": last.get("played_seconds"),
            "last_camera_id": last.get("camera_id"),
            # Trimmed: the full window is on the bridge; a handful is plenty for a
            # card and keeps the state machine's attribute payload small.
            "recent": recent[-5:],
        }


class TalkdownFailuresSensor(_TalkMetricsEntity):
    """Count of failed talks since the bridge started.

    Numeric and total_increasing so an automation can trigger on it going up --
    "the doorbell refused to speak" is exactly the kind of thing that otherwise
    goes unnoticed. It resets to 0 when the bridge restarts, which
    total_increasing handles as a reset rather than a spike.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: TalkMetricsCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator, config_entry, "talkdown_failures")

    @property
    def native_value(self) -> int:
        return int(self._metrics.get("failed") or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._metrics
        return {
            "attempts": m.get("attempts"),
            "succeeded": m.get("succeeded"),
            "stopped": m.get("stopped"),
            "rejected_in_progress": m.get("rejected_in_progress"),
            "failures_by_reason": m.get("failures_by_reason"),
            "by_endpoint": m.get("by_endpoint"),
            "by_codec": m.get("by_codec"),
            "renegotiations": m.get("renegotiations"),
            "audio_seconds_played": m.get("audio_seconds_played"),
            "bridge_uptime_seconds": m.get("uptime_seconds"),
        }
