"""Alarmdotcom implementation of an HA select.

Two unrelated sources of selects live here: the Skybell configuration options
that come from pyalarmdotcomajax's device model, and the bridge-served wired
chime type of a TELUS doorbell (docs/talkdown-fullduplex-protocol.md, E27).
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from homeassistant import core
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from pyalarmdotcomajax.devices.registry import AllDevices_t
from pyalarmdotcomajax.extensions import (
    CameraSkybellControllerExtension as libCameraSkybellControllerExtension,
)
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
from .const import CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE, DATA_CONTROLLER, DOMAIN
from .controller import AlarmIntegrationController

LOGGER = logging.getLogger(__name__)

# TODO: This device contains behavior specific to the Skybell HD. It needs to be made more generic as other devices are supported.


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # pylint: disable=unused-argument
) -> None:
    """Set up the select platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]

    for device in controller.api.devices.cameras.values():
        async_add_entities(
            ConfigOptionSelect(
                controller=controller,
                device=device,
                config_option=config_option,
            )
            for config_option in device.settings.values()
            if isinstance(config_option, libConfigurationOption)
            and config_option.option_type
            in [
                libConfigurationOptionType.ADJUSTABLE_CHIME,
                libConfigurationOptionType.MOTION_SENSITIVITY,
            ]
        )

    # The doorbell's wired chime type, served by the camera-bridge. Nothing to
    # do with pyalarmdotcomajax's device model above -- it rides the shared
    # settings coordinator instead, exactly as the speaker volume in number.py
    # does.
    settings_coordinator = await async_get_camera_settings_coordinator(hass, config_entry)

    @core.callback
    def _build_chime_type(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """One selector per camera that reports its wired chime type as settable."""
        audio = (camera_data.get("settings") or {}).get("audio") or {}
        if not audio.get("supports_chime_type"):
            # indoorChimeTypeFeatureSupport was 0, or the camera sent no audio
            # section at all. The payload looks identical either way, so the
            # bridge's flag is the only thing separating a doorbell whose chime
            # type can be written from one where the write is refused.
            return []
        return [CameraChimeTypeSelect(settings_coordinator, camera_id)]

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_chime_type)


class CameraChimeTypeSelect(CameraSettingsEntity, SelectEntity):
    """Which kind of physical chime one bridge-served TELUS doorbell is wired to.

    Not a ringtone and not a volume: this tells the doorbell what is on the other
    end of its chime wiring -- a mechanical bell, a digital chime, or nothing --
    which is how it knows how to drive that circuit. Choosing the "no wired
    chime" option is what takes the indoor chime switch in switch.py out of
    play; that entity reads the same flag back out of this option list.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:bell-cog"

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the chime type selector for one camera."""
        super().__init__(coordinator, camera_id, "chime_type")

    @property
    def _audio(self) -> dict[str, Any]:
        """The camera's audio settings section."""
        return self._settings.get("audio") or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting the type as settable."""
        return super().available and bool(self._audio.get("supports_chime_type"))

    @property
    def _values_by_label(self) -> dict[str, int]:
        """The selectable chime types as {label shown: value written}, in the camera's order.

        `chime_type_choices` carries the portal's own names and is what a user
        should be reading, but the bridge builds it from a secondary upstream
        call that can fail, leaving it absent or empty. Losing the names must
        not lose the control, so fall back to `chime_type_options` -- always
        present when the feature is supported -- rendered as bare numbers. A
        selector reading "0 / 1" is poor, but it still sets the doorbell
        correctly, and it is the same underlying value either way.
        """
        labelled: dict[str, int] = {}
        for choice in self._audio.get("chime_type_choices") or []:
            value, label = choice.get("value"), choice.get("label")
            if value is None or not label:
                continue
            labelled[str(label)] = int(value)
        if labelled:
            return labelled

        return {
            str(value): int(value)
            for value in self._audio.get("chime_type_options") or []
            if value is not None
        }

    @property
    def options(self) -> list[str]:
        """The labels the camera currently offers."""
        return list(self._values_by_label)

    @property
    def current_option(self) -> str | None:
        """The label matching the reported chime type, or None if none does.

        None renders as unknown, which is the honest answer: a doorbell reporting
        a type that isn't in its own option list (or none at all) has not told us
        what it is wired to, and picking a plausible default here would both
        misreport the hardware and make the next write look like a no-op.
        """
        chime_type = self._audio.get("chime_type")
        if chime_type is None:
            return None
        return next((label for label, value in self._values_by_label.items() if value == chime_type), None)

    async def async_select_option(self, option: str) -> None:
        """Write the chosen chime type and adopt the audio section that comes back."""
        values = self._values_by_label
        if option not in values:
            # HA rejects an option outside `options` before it gets here, so this
            # only catches the race where the camera's list changed between the
            # UI rendering it and the click landing. Better a clear error than a
            # KeyError trace, or worse, writing a stale number to real hardware.
            raise HomeAssistantError(f"This camera no longer offers the chime type {option}.")

        audio = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"chime_type": values[option]},
        )
        # The route replies with the whole audio section re-read from the camera,
        # so the new type -- and the indoor chime switch's availability, which
        # depends on it -- both update without a poll. See the same trade in
        # number.py's volume write.
        if audio:
            self.coordinator.async_apply_settings_section(self._camera_id, "audio", audio)
        else:
            await self.coordinator.async_request_refresh()


class ConfigOptionSelect(ConfigBaseDevice, SelectEntity):  # type: ignore
    """Integration configuration option entity."""

    def __init__(
        self,
        controller: AlarmIntegrationController,
        device: AllDevices_t,
        config_option: libConfigurationOption,
    ) -> None:
        """Initialize."""
        super().__init__(controller, device, config_option)

        self._attr_entity_category = EntityCategory.CONFIG

        self._select_options_map: dict[
            str,
            libCameraSkybellControllerExtension.MotionSensitivity
            | libCameraSkybellControllerExtension.ChimeAdjustableVolume,
        ] = {}
        if self._config_option.option_type == libConfigurationOptionType.ADJUSTABLE_CHIME:
            self._select_options_map = {
                member.name.title().replace("_", " "): member
                for member in libCameraSkybellControllerExtension.ChimeAdjustableVolume
            }
        elif self._config_option.option_type == libConfigurationOptionType.MOTION_SENSITIVITY:
            self._select_options_map = {
                member.name.title().replace("_", " "): member
                for member in libCameraSkybellControllerExtension.MotionSensitivity
            }
        else:
            LOGGER.exception(
                "%s: Encountered unknown select configuration type when initializing %s.",
                __name__,
                self.unique_id,
            )

        self._attr_options: list = list(self._select_options_map.keys())

    @property
    def icon(self) -> str | None:
        """Return the icon to use in the frontend, if any."""
        if self._config_option.option_type == libConfigurationOptionType.ADJUSTABLE_CHIME:
            if (
                current_value := self._config_option.current_value
            ) == libCameraSkybellControllerExtension.ChimeAdjustableVolume.OFF:
                return "mdi:volume-mute"
            if current_value == libCameraSkybellControllerExtension.ChimeAdjustableVolume.LOW:
                return "mdi:volume-low"
            if current_value == libCameraSkybellControllerExtension.ChimeAdjustableVolume.MEDIUM:
                return "mdi:volume-medium"
            if current_value == libCameraSkybellControllerExtension.ChimeAdjustableVolume.HIGH:
                return "mdi:volume-high"
        elif self._config_option.option_type == libConfigurationOptionType.MOTION_SENSITIVITY:
            return "mdi:tune"

        return super().icon if isinstance(super().icon, str) else None

    @property
    def current_option(self) -> str | None:
        """Return the selected option."""

        if isinstance(current_value := self._config_option.current_value, Enum):
            return current_value.name.title().replace("_", " ")
        return None

    async def async_select_option(self, option: str) -> None:
        """Change the selected option."""

        await self._device.async_change_setting(self._config_option.slug, self._select_options_map[option])
