"""Alarmdotcom implementation of an HA thermostat."""

from __future__ import annotations

import logging

from homeassistant import core
from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACMode,
)
from homeassistant.components.climate.const import (
    ATTR_HVAC_MODE,
    ATTR_TARGET_TEMP_HIGH,
    ATTR_TARGET_TEMP_LOW,
    FAN_AUTO,
    FAN_ON,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from pyalarmdotcomajax.devices.registry import AllDevices_t
from pyalarmdotcomajax.devices.thermostat import Thermostat as libThermostat
from pyalarmdotcomajax.exceptions import NotAuthorized

from .base_device import HardwareBaseDevice
from .const import DATA_CONTROLLER, DOMAIN
from .controller import AlarmIntegrationController

LOGGER = logging.getLogger(__name__)

FAN_CIRCULATE = "Circulate"


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the light platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]

    async_add_entities(
        Climate(
            controller=controller,
            device=device,
        )
        for device in controller.api.devices.thermostats.values()
    )


class Climate(HardwareBaseDevice, ClimateEntity):  # type: ignore
    """Integration Climate Entity."""

    _device_type_name: str = "Thermostat"
    _device: libThermostat

    _raw_attribs: libThermostat.ThermostatAttributes

    def __init__(
        self,
        controller: AlarmIntegrationController,
        device: AllDevices_t,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(controller, device)

        self._attr_target_temperature_step = 1.0
        self._determine_features()

    def _legacy_refresh_attributes(self) -> None:
        """Update HA when device is updated."""

        #
        # Reported Values
        #

        self._attr_current_temperature = self._device.attributes.temp_average

        if self._device.attributes.supports_humidity:
            self._attr_current_humidity = self._device.attributes.humidity

        #
        # HVAC Mode
        #

        # Set HVAC mode
        if self._device.state in [
            libThermostat.DeviceState.AUX_HEAT,
            libThermostat.DeviceState.HEAT,
        ]:
            self._attr_hvac_mode = HVACMode.HEAT
        elif self._device.state == libThermostat.DeviceState.COOL:
            self._attr_hvac_mode = HVACMode.COOL
        elif self._device.state == libThermostat.DeviceState.AUTO:
            self._attr_hvac_mode = HVACMode.HEAT_COOL
        elif (
            self._device.state == libThermostat.DeviceState.OFF
            and self._device.attributes.fan_mode == libThermostat.FanMode.ON
        ):
            self._attr_hvac_mode = HVACMode.FAN_ONLY
        elif self._device.state == libThermostat.DeviceState.OFF:
            self._attr_hvac_mode = HVACMode.OFF
        else:
            self._attr_hvac_mode = None

        #
        # Target Temps
        #

        # Reset all temperature target parameters before re-setting.
        self._attr_target_temperature_high = None
        self._attr_target_temperature_low = None
        self._attr_target_temperature = None
        # max/min are removed rather than set to None. ClimateEntity.max_temp/min_temp
        # fall back to sane defaults only when the _attr_ is ABSENT (a hasattr check);
        # a None satisfies hasattr and is then returned as the actual limit, which
        # breaks the thermostat card's range and async_set_temperature's validation
        # for every state where the branches below don't assign one -- e.g. whenever
        # the thermostat is simply off.
        self.__dict__.pop("_attr_max_temp", None)
        self.__dict__.pop("_attr_min_temp", None)

        # Set target temperature parameters
        if not self._device.attributes.supports_setpoints:
            pass
        elif self._device.state in [
            libThermostat.DeviceState.AUX_HEAT,
            libThermostat.DeviceState.HEAT,
        ]:
            self._attr_target_temperature = self._device.attributes.heat_setpoint
            self._attr_max_temp = self._device.attributes.max_heat_setpoint
            self._attr_min_temp = self._device.attributes.min_heat_setpoint
        elif self._device.state == libThermostat.DeviceState.COOL:
            self._attr_target_temperature = self._device.attributes.cool_setpoint
            self._attr_max_temp = self._device.attributes.max_cool_setpoint
            self._attr_min_temp = self._device.attributes.min_cool_setpoint
        elif self._device.state == libThermostat.DeviceState.AUTO:
            self._attr_target_temperature_high = self._device.attributes.cool_setpoint
            self._attr_target_temperature_low = self._device.attributes.heat_setpoint
            self._attr_max_temp = self._device.attributes.max_cool_setpoint
            self._attr_min_temp = self._device.attributes.min_heat_setpoint

        #
        # Fan Mode
        #

        if self._device.attributes.fan_mode == libThermostat.FanMode.AUTO:
            self._attr_fan_mode = FAN_AUTO
        elif self._device.attributes.fan_mode == libThermostat.FanMode.ON:
            self._attr_fan_mode = FAN_ON
        else:
            self._attr_fan_mode = None

        # Aux heat: no longer represented as its own entity attribute -- core removed
        # is_aux_heat/AUX_HEAT entirely. The AUX_HEAT device state is already mapped
        # onto HVACMode.HEAT above, which is how core expects it to be surfaced now.

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set HVAC mode."""

        try:
            if hvac_mode == HVACMode.COOL:
                await self._device.async_set_attribute(state=libThermostat.DeviceState.COOL)
            elif hvac_mode == HVACMode.HEAT:
                await self._device.async_set_attribute(state=libThermostat.DeviceState.HEAT)
            elif hvac_mode == HVACMode.HEAT_COOL:
                await self._device.async_set_attribute(state=libThermostat.DeviceState.AUTO)
            elif hvac_mode == HVACMode.FAN_ONLY:
                await self._device.async_set_attribute(state=libThermostat.DeviceState.OFF)
                await self.async_set_fan_mode(FAN_ON)
            elif hvac_mode == HVACMode.OFF:
                await self._device.async_set_attribute(state=libThermostat.DeviceState.OFF)
                await self.async_set_fan_mode(FAN_AUTO)
        except NotAuthorized as err:
            self._show_permission_error("set the HVAC mode on")
            raise HomeAssistantError(
                f"Not authorized to set the HVAC mode on {self._device.name}."
            ) from err

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """Change fan mode."""

        max_fan_duration = (
            0
            if self._device.attributes.supports_fan_indefinite
            or not self._device.attributes.supported_fan_durations
            else max(self._device.attributes.supported_fan_durations)
        )

        try:
            if fan_mode == FAN_ON:
                await self._device.async_set_attribute(fan=(libThermostat.FanMode.ON, max_fan_duration))
            elif fan_mode == FAN_AUTO:
                await self._device.async_set_attribute(fan=(libThermostat.FanMode.AUTO, 0))
        except NotAuthorized as err:
            self._show_permission_error("set the fan mode on")
            raise HomeAssistantError(
                f"Not authorized to set the fan mode on {self._device.name}."
            ) from err

    async def async_set_temperature(self, **kwargs) -> None:  # type: ignore
        """Set new target temperature."""

        try:
            # Change HVAC Mode First
            if hvac_mode := kwargs.get(ATTR_HVAC_MODE):
                await self.async_set_hvac_mode(hvac_mode)
                await self.async_update()

            # Heat/cool setpoint
            heat_setpoint = None
            cool_setpoint = None
            if self.hvac_mode == HVACMode.HEAT:
                heat_setpoint = kwargs.get(ATTR_TEMPERATURE)
            elif self.hvac_mode == HVACMode.COOL:
                cool_setpoint = kwargs.get(ATTR_TEMPERATURE)
            else:
                heat_setpoint = kwargs.get(ATTR_TARGET_TEMP_LOW)
                cool_setpoint = kwargs.get(ATTR_TARGET_TEMP_HIGH)

            if heat_setpoint is not None:
                await self._device.async_set_attribute(heat_setpoint=heat_setpoint)
            if cool_setpoint is not None:
                await self._device.async_set_attribute(cool_setpoint=cool_setpoint)
        except NotAuthorized as err:
            self._show_permission_error("set the temperature on")
            raise HomeAssistantError(
                f"Not authorized to set the temperature on {self._device.name}."
            ) from err

    def _determine_features(self) -> None:
        """Determine which features are available for thermostat."""
        #
        # UNIT OF MEASUREMENT
        #

        self._attr_temperature_unit = (
            UnitOfTemperature.CELSIUS if self._device.attributes.uses_celsius else UnitOfTemperature.FAHRENHEIT
        )

        #
        # SUPPORTED FEATURES
        #

        supported_features = 0

        if self._device.attributes.supports_setpoints:
            supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE

        if self._device.attributes.supports_auto:
            supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE_RANGE

        if self._device.attributes.supports_fan_mode:
            supported_features |= ClimateEntityFeature.FAN_MODE

        # ClimateEntityFeature.AUX_HEAT has been REMOVED from Home Assistant core
        # (confirmed absent on 2026.7.1) along with the whole aux-heat concept --
        # referencing it raised AttributeError, so any thermostat reporting
        # supports_heat_aux failed to be created at all. Nothing replaces it in core;
        # aux heat is still surfaced through the HEAT hvac mode, which
        # _legacy_refresh_attributes already maps AUX_HEAT state onto.
        self._attr_supported_features = supported_features

        #
        # HVAC MODES
        #

        hvac_modes = [HVACMode.OFF]

        if self._device.attributes.supports_heat:
            hvac_modes.append(HVACMode.HEAT)

        if self._device.attributes.supports_cool:
            hvac_modes.append(HVACMode.COOL)

        if self._device.attributes.supports_auto:
            hvac_modes.append(HVACMode.HEAT_COOL)

        if self._device.attributes.supports_fan_mode:
            hvac_modes.append(HVACMode.FAN_ONLY)

        self._attr_hvac_modes = hvac_modes

        #
        # FAN MODES
        #

        if self._device.attributes.supports_fan_mode:
            self._attr_fan_modes = [FAN_AUTO, FAN_ON]
