"""Diagnostic and data-feed sensor(s) for the TELUS Smart Home integration.

AuthHealthSensor reflects TELUS/Alarm.com session auth health, so the user
can see in Home Assistant (as a normal entity/history graph) when
authentication starts failing, rather than only discovering it via the
reauth persistent-notification or raw logs.

ActivityFeedSensor exposes the full recent Activity list (not just the
single latest entry event.py's TelusActivityEvent tracks) as an attribute,
for a dashboard card to render as a scrollable feed -- see that entity's
own docstring for why an EventEntity alone can't do this.
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import core
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DATA_ACTIVITY_COORDINATOR, DATA_CONTROLLER, DOMAIN
from .controller import AlarmIntegrationController
from .event import ActivityHistoryCoordinator

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

    async_add_entities([
        AuthHealthSensor(controller, config_entry),
        ActivityFeedSensor(activity_coordinator, config_entry),
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
        same shape as control_server.py's /control/history response."""
        return {"events": self.coordinator.data or []}
