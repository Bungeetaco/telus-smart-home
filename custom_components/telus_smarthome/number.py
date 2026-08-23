"""Alarmdotcom implementation of an HA number.

Two unrelated sources of numbers live here: the Skybell configuration options
that come from pyalarmdotcomajax's device model, and the bridge-served
speaker/call volume for a TELUS camera (the volume talkdown actually plays at --
see docs/talkdown-fullduplex-protocol.md, E9).
"""

from __future__ import annotations

import logging
from typing import Any

from homeassistant import core
from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from pyalarmdotcomajax.devices.registry import AllDevices_t
from pyalarmdotcomajax.extensions import ConfigurationOption as libConfigurationOption
from pyalarmdotcomajax.extensions import (
    ConfigurationOptionType as libConfigurationOptionType,
)

from .base_device import ConfigBaseDevice
from .camera_settings_coordinator import (
    CameraSettingsCoordinator,
    CameraSettingsEntity,
    async_add_camera_entities,
    async_bridge_post,
    async_get_camera_settings_coordinator,
)
from .const import CAMERA_BRIDGE_CAMERA_VOLUME_URL_TEMPLATE, DATA_CONTROLLER, DOMAIN
from .controller import AlarmIntegrationController

LOGGER = logging.getLogger(__name__)

# TODO: This device contains behavior specific to the Skybell HD. It needs to be made more generic as other devices are supported.


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # pylint: disable=unused-argument
) -> None:
    """Set up the sensor platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]

    for device in controller.api.devices.cameras.values():
        async_add_entities(
            ConfigOptionNumber(
                controller=controller,
                device=device,
                config_option=config_option,
            )
            for config_option in device.settings.values()
            if isinstance(config_option, libConfigurationOption)
            and config_option.option_type is libConfigurationOptionType.BRIGHTNESS
        )

    # Camera speaker volume, served by the camera-bridge -- unrelated to
    # pyalarmdotcomajax's device model, so it rides the shared settings
    # coordinator rather than the controller above.
    settings_coordinator = await async_get_camera_settings_coordinator(hass, config_entry)

    @core.callback
    def _build_volume(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """One volume slider per camera that reports it can change its call volume."""
        audio = (camera_data.get("settings") or {}).get("audio") or {}
        if not audio.get("supports_call_volume"):
            # callVolumeFeatureSupport was 0 (or the device sent no audio
            # settings at all). The payload looks identical either way, so the
            # bridge's boolean is the only thing that distinguishes a camera
            # whose volume is settable from one where a PUT would be refused.
            return []
        return [CameraCallVolumeNumber(settings_coordinator, camera_id)]

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_volume)


class ConfigOptionNumber(ConfigBaseDevice, NumberEntity):  # type: ignore
    """Integration Number Entity."""

    def __init__(
        self,
        controller: AlarmIntegrationController,
        device: AllDevices_t,
        config_option: libConfigurationOption,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(controller, device, config_option)

        if self._config_option.value_max:
            self._attr_native_max_value: float = self._config_option.value_max

        if self._config_option.value_min:
            self._attr_native_min_value: float = self._config_option.value_min

        if self._config_option.value_max and self._config_option.value_min:
            self._attr_mode = NumberMode.SLIDER
        else:
            self._attr_mode = NumberMode.AUTO

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        if self._config_option.option_type == libConfigurationOptionType.BRIGHTNESS:
            return "mdi:brightness-5"

        return super().icon if isinstance(super().icon, str) else None

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""

        # `is not None`, not truthiness: a legitimately-stored 0 (e.g. brightness
        # turned all the way down) is falsy, so the old check reported the entity as
        # unknown instead of 0 -- and the slider then had no value to render.
        current_value = self._config_option.current_value
        return float(current_value) if current_value is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Set new value."""
        await self._device.async_change_setting(self._config_option.slug, int(value))


class CameraCallVolumeNumber(CameraSettingsEntity, NumberEntity):
    """Speaker (call) volume for one bridge-served TELUS camera.

    This is the volume the doorbell plays talkdown audio at. The mobile API
    claims this device can't do it (`SupportsVolumeAdjustment="false"`); the
    website API exposes it as supported with a 0-10 range and was observed
    being swept live, so the bridge's website-API view is what's trusted here --
    see docs/web-portal-api-findings.md.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:volume-high"
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the volume entity for one camera."""
        super().__init__(coordinator, camera_id, "call_volume")

    @property
    def _audio(self) -> dict[str, Any]:
        """The camera's audio settings section."""
        return self._settings.get("audio") or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting the volume as settable."""
        return super().available and bool(self._audio.get("supports_call_volume"))

    @property
    def native_min_value(self) -> float:
        """Lower bound the camera reports, defaulting to the observed 0."""
        return float(self._audio.get("call_volume_min") or 0)

    @property
    def native_max_value(self) -> float:
        """Upper bound the camera reports, defaulting to the observed 10.

        Taken from the device rather than hardcoded: the 0-10 range was read off
        one VDB750, and another model's callVolumeMax may differ.
        """
        maximum = self._audio.get("call_volume_max")
        return float(maximum) if maximum is not None else 10.0

    @property
    def native_value(self) -> float | None:
        """Current volume, or None if the camera hasn't reported one yet."""
        # `is not None`, not truthiness: 0 is a real volume (muted speaker) and
        # would otherwise render the slider as unknown -- the same trap already
        # fixed in ConfigOptionNumber above.
        volume = self._audio.get("call_volume")
        return float(volume) if volume is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Write the new volume through the bridge and adopt what actually landed."""
        audio = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_VOLUME_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"volume": int(value)},
        )
        # The route answers with the audio settings re-read from the camera after
        # the write, so what actually landed is already in hand. Adopting it beats
        # an immediate poll, which would re-read both routes upstream to learn the
        # same thing.
        #
        # NOT because the value may have been clamped: a volume outside the
        # device's call_volume_min..call_volume_max is refused with a 400 (which
        # async_bridge_post raises as a HomeAssistantError), never silently
        # written as a different number. The read-back is about the OTHER audio
        # fields in the section, which the write's read-modify-write may have
        # refreshed.
        if audio:
            self.coordinator.async_apply_settings_section(self._camera_id, "audio", audio)
        else:
            await self.coordinator.async_request_refresh()
