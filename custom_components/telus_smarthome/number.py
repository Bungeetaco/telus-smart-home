"""Alarmdotcom implementation of an HA number.

Two unrelated sources of numbers live here: the Skybell configuration options
that come from pyalarmdotcomajax's device model, and the bridge-served camera
settings -- the speaker/call volume (the volume talkdown actually plays at --
see docs/talkdown-fullduplex-protocol.md, E9) and the brightness of the device's
status LED (E32).
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
from .const import (
    CAMERA_BRIDGE_CAMERA_LED_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_VOLUME_URL_TEMPLATE,
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

    @core.callback
    def _build_led_brightness(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """One brightness slider per camera that reports a DIMMABLE status LED."""
        led = (camera_data.get("settings") or {}).get("led") or {}
        # A different flag from the one the status-LED switch in switch.py is
        # gated on: being able to turn the LED off says nothing about being able
        # to dim it. The reference doorbell reports exactly that split
        # (supports_enabled true, supports_brightness false), so no slider is
        # created there -- which is the expected outcome, not a fault.
        if not led.get("supports_brightness"):
            return []
        return [CameraStatusLedBrightnessNumber(settings_coordinator, camera_id)]

    async_add_camera_entities(
        config_entry, settings_coordinator, async_add_entities, _build_led_brightness)


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


class CameraStatusLedBrightnessNumber(CameraSettingsEntity, NumberEntity):
    """Brightness of the status indicator light on one bridge-served TELUS camera.

    The companion to the status-LED on/off switch in switch.py, behind its own
    capability flag: `supports_brightness` is FALSE on the reference doorbell,
    which can turn its LED off but not dim it, so this entity is never created
    there. It exists for a model whose `ledmn`/`ledmx` say otherwise
    (docs/talkdown-fullduplex-protocol.md, E32) -- which also means nothing here
    has ever run against real hardware, hence following CameraCallVolumeNumber
    above field for field rather than inventing anything.

    The status LED again: not the infrared illuminator and not the picture's
    brightness, both of which are separate fields of the `image` section.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:brightness-6"
    _attr_native_step = 1
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the status LED brightness entity for one camera."""
        super().__init__(coordinator, camera_id, "status_led_brightness")

    @property
    def _led(self) -> dict[str, Any]:
        """The camera's status-LED settings section."""
        return self._settings.get("led") or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting the LED as dimmable."""
        return super().available and bool(self._led.get("supports_brightness"))

    @property
    def native_min_value(self) -> float:
        """Lower bound the device reports, defaulting to 0.

        Same reasoning as the speaker volume's floor: 0 is the only sensible
        bottom for a dimmable indicator, and a device that publishes no
        `brightness_min` is not saying its minimum is something else.
        """
        return float(self._led.get("brightness_min") or 0)

    @property
    def native_max_value(self) -> float:
        """Upper bound the device reports, falling back to 100.

        FALLBACK, and unlike the speaker volume's 10 it is NOT an observed
        figure: the reference doorbell publishes neither `brightness_min` nor
        `brightness_max` for this section (it reports the LED as not dimmable at
        all), so no device this was built against has ever named a range. A
        device that does support dimming is expected to publish `ledmn`/`ledmx`
        and never reach this line; the fallback only exists so one that supports
        dimming yet names no bounds still gets a usable slider instead of an
        entity nobody can move.

        Chosen wide rather than narrow, and wide in the direction that fails
        loudly: too low a ceiling silently puts legitimate brightnesses out of
        reach with nothing in the UI to explain why, while too high a one is
        refused by the bridge with a 400 that async_bridge_post surfaces as an
        error the user can actually see. 100 is also Home Assistant's own
        NumberEntity default maximum, so an unbounded device's slider looks
        unremarkable rather than arbitrary.
        """
        maximum = self._led.get("brightness_max")
        return float(maximum) if maximum is not None else 100.0

    @property
    def native_value(self) -> float | None:
        """Current brightness, or None if the camera hasn't reported one yet."""
        # `is not None`, not truthiness: 0 is a real brightness (dimmed all the
        # way down) and would otherwise render the slider as unknown -- the same
        # trap already fixed in ConfigOptionNumber and CameraCallVolumeNumber.
        brightness = self._led.get("brightness")
        return float(brightness) if brightness is not None else None

    async def async_set_native_value(self, value: float) -> None:
        """Write the new brightness through the bridge and adopt what actually landed."""
        led = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_LED_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"brightness": int(value)},
        )
        # Answers with the `led` section re-read from the device after the write,
        # so the switch's on/off flag refreshes out of the same reply instead of
        # costing a second live round trip. A brightness outside the device's
        # published range is refused with a 400 rather than silently clamped, so
        # this read-back is about the section's OTHER fields, not this one --
        # exactly as in CameraCallVolumeNumber above.
        if led:
            self.coordinator.async_apply_settings_section(self._camera_id, "led", led)
        else:
            await self.coordinator.async_request_refresh()
