"""Diagnostic sensor(s) for the TELUS Smart Home integration.

Currently exposes a single entity reflecting TELUS/Alarm.com session auth
health, so the user can see in Home Assistant (as a normal entity/history
graph) when authentication starts failing, rather than only discovering it
via the reauth persistent-notification or raw logs.
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

from .const import DATA_CONTROLLER, DOMAIN
from .controller import AlarmIntegrationController

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

    async_add_entities([AuthHealthSensor(controller, config_entry)])


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
