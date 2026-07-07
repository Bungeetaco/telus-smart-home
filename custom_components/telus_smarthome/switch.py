"""Alarmdotcom implementation of an HA switch."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant import core
from homeassistant.components.switch import SwitchDeviceClass, SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from pyalarmdotcomajax.extensions import (
    CameraSkybellControllerExtension as libCameraSkybellControllerExtension,
)
from pyalarmdotcomajax.extensions import ConfigurationOption as libConfigurationOption
from pyalarmdotcomajax.extensions import (
    ConfigurationOptionType as libConfigurationOptionType,
)

from .base_device import ConfigBaseDevice
from .bridge_client import bridge_get, read_control_secret
from .const import (
    AUTOMATION_RULES_POLL_INTERVAL_SECONDS,
    CAMERA_BRIDGE_AUTOMATION_PAUSE_URL_TEMPLATE,
    CAMERA_BRIDGE_AUTOMATION_RESUME_URL_TEMPLATE,
    CAMERA_BRIDGE_AUTOMATIONS_URL,
    DATA_CONTROLLER,
    DOMAIN,
)
from .controller import AlarmIntegrationController

LOGGER = logging.getLogger(__name__)

# TODO: This device contains behavior specific to the Skybell HD. It needs to be made more generic as other devices are supported.


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the sensor platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]

    for device in controller.api.devices.cameras.values():
        async_add_entities(
            ConfigOptionSwitch(
                controller=controller,
                device=device,
                config_option=config_option,
            )
            for config_option in device.settings.values()
            if isinstance(config_option, libConfigurationOption)
            and config_option.option_type is libConfigurationOptionType.BINARY_CHIME
        )

    # Video automation rules (recording schedules etc), served by the
    # camera-bridge -- unrelated to pyalarmdotcomajax's device model, so
    # this uses its own small coordinator rather than the controller above.
    automation_coordinator = AutomationRulesCoordinator(hass, config_entry)
    await automation_coordinator.async_refresh()

    known_rule_ids: set[str] = set()

    @core.callback
    def _add_new_rules() -> None:
        new_ids = {r["id"] for r in (automation_coordinator.data or [])} - known_rule_ids
        if not new_ids:
            return
        known_rule_ids.update(new_ids)
        LOGGER.info("Adding %d newly discovered TELUS automation rule(s): %s", len(new_ids), sorted(new_ids))
        async_add_entities(
            AutomationRuleSwitch(automation_coordinator, config_entry, rule_id) for rule_id in new_ids
        )

    _add_new_rules()
    config_entry.async_on_unload(automation_coordinator.async_add_listener(_add_new_rules))


class AutomationRulesCoordinator(DataUpdateCoordinator[list[dict[str, Any]]]):
    """Polls the camera-bridge's /control/automations endpoint.

    Infrequent poll: pause/resume is normally a manual user action (via
    this switch, or the Alarm.com app), not something that changes on its
    own like camera discovery does. A pause/resume through this switch
    triggers its own immediate refresh rather than waiting on this poll
    (see AutomationRuleSwitch.async_turn_on/off).
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS automation rules",
            update_interval=timedelta(seconds=AUTOMATION_RULES_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> list[dict[str, Any]]:
        """Fetch the current automation rule list from the bridge."""

        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_AUTOMATIONS_URL)
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        if body is None:
            # Bridge not up yet -- not fatal, see CameraBridgeCoordinator in
            # camera.py for the identical reasoning.
            return self.data or []

        return body.get("rules", [])


class AutomationRuleSwitch(CoordinatorEntity[AutomationRulesCoordinator], SwitchEntity):
    """Pause/resume one TELUS video automation rule (recording schedule).

    On means active (not paused). canBePaused is reported by TELUS per rule;
    not currently enforced client-side -- if a future rule reports
    canBePaused=False, the pause/resume call will just fail server-side and
    surface as a HomeAssistantError, same as any other rejected command in
    this integration.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: AutomationRulesCoordinator, config_entry: ConfigEntry, rule_id: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._rule_id = rule_id
        self._attr_unique_id = f"{rule_id}_automation_rule"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
            name="TELUS Smart Home Integration",
            manufacturer="TELUS",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _rule(self) -> dict[str, Any] | None:
        """Current bridge-reported data for this rule, if still present."""
        return next((r for r in (self.coordinator.data or []) if r["id"] == self._rule_id), None)

    @property
    def available(self) -> bool:
        """Unavailable if the coordinator is unhealthy or TELUS stops
        reporting this rule (e.g. deleted in the Alarm.com app)."""
        return super().available and self._rule is not None

    @property
    def name(self) -> str:
        """Human-readable rule name, e.g. 'Porte d'entrée'."""
        rule = self._rule
        return rule["name"] if rule else self._rule_id

    @property
    def is_on(self) -> bool | None:
        """On means active (not paused)."""
        rule = self._rule
        return not rule["is_paused"] if rule else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Human-readable description of what this rule does, matching what
        the Alarm.com web UI shows for it."""
        rule = self._rule
        if rule is None:
            return {}
        return {
            "action": rule.get("action"),
            "trigger": rule.get("trigger"),
            "timeframe": rule.get("timeframe"),
            "can_be_paused": rule.get("can_be_paused"),
        }

    async def _async_call_bridge(self, url_template: str, verb: str) -> None:
        secret = await self.hass.async_add_executor_job(read_control_secret)
        if secret is None:
            raise HomeAssistantError(
                f"Cannot {verb} rule {self.name}: camera bridge is not reachable."
            )

        session = async_get_clientsession(self.hass)
        try:
            async with session.post(
                url_template.format(rule_id=self._rule_id),
                headers={"Authorization": f"Bearer {secret}"},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                resp.raise_for_status()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise HomeAssistantError(f"Failed to {verb} rule {self.name}: {err}") from err

        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Resume the rule."""
        await self._async_call_bridge(CAMERA_BRIDGE_AUTOMATION_RESUME_URL_TEMPLATE, "resume")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Pause the rule."""
        await self._async_call_bridge(CAMERA_BRIDGE_AUTOMATION_PAUSE_URL_TEMPLATE, "pause")


class ConfigOptionSwitch(ConfigBaseDevice, SwitchEntity):  # type: ignore
    """Integration Switch Entity."""

    _attr_device_class = SwitchDeviceClass.SWITCH

    @property
    def is_on(self) -> bool:
        """Return true if the binary sensor is on."""
        return self._config_option.current_value is libCameraSkybellControllerExtension.ChimeOnOff.ON

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        if self._config_option.option_type is libConfigurationOptionType.BINARY_CHIME:
            return "mdi:bell" if self.is_on else "mdi:bell-off"

        return super().icon if isinstance(super().icon, str) else None

    async def async_turn_on(self, **kwargs) -> None:  # type: ignore
        """Turn on."""
        await self._device.async_change_setting(
            self._config_option.slug,
            libCameraSkybellControllerExtension.ChimeOnOff.ON,
        )

        await self._controller.update_coordinator.async_refresh()

    async def async_turn_off(self, **kwargs) -> None:  # type: ignore
        """Turn off."""
        await self._device.async_change_setting(
            self._config_option.slug,
            libCameraSkybellControllerExtension.ChimeOnOff.OFF,
        )

        await self._controller.update_coordinator.async_refresh()
