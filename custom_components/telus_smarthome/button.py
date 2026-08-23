"""Alarmdotcom implementation of an HA button.

Two unrelated kinds of button live here: the per-device Debug button (a support
aid that only fires an HA event), and the bridge-served camera siren
start/stop pair, which makes a real siren sound in someone's home.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Final

from homeassistant import core
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from pyalarmdotcomajax.devices.registry import AllDevices_t

from .base_device import AttributeBaseDevice
from .camera_settings_coordinator import (
    CameraSettingsCoordinator,
    CameraSettingsEntity,
    async_add_camera_entities,
    async_bridge_post,
    async_get_camera_settings_coordinator,
)
from .const import (
    CAMERA_BRIDGE_CAMERA_SIREN_URL_TEMPLATE,
    DATA_CONTROLLER,
    DEBUG_REQ_EVENT,
    DOMAIN,
    SENSOR_SUBTYPE_BLACKLIST,
)
from .controller import AlarmIntegrationController

LOGGER = logging.getLogger(__name__)


@dataclass
class AlarmdotcomButtonDescriptionMixin:
    """Functions for an attribute entity."""

    filter_fn: Callable[[AllDevices_t], bool]
    press_fn: Callable[[HomeAssistant, AllDevices_t], Any]


@dataclass
class AlarmdotcomButtonDescription(ButtonEntityDescription, AlarmdotcomButtonDescriptionMixin):  # type: ignore
    """Describes a button entity."""


ATTRIBUTE_BUTTONS: Final = [
    AlarmdotcomButtonDescription(
        key="debug",
        name="Debug",
        has_entity_name=True,
        entity_category=EntityCategory.DIAGNOSTIC,
        press_fn=lambda hass, device: hass.bus.async_fire(DEBUG_REQ_EVENT, {"device_id": device.id_}),
        filter_fn=lambda device: device.has_state is True
        and (getattr(device, "device_subtype") not in SENSOR_SUBTYPE_BLACKLIST),
        icon="mdi:bug",
    ),
]


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,
) -> None:
    """Set up the button platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]

    async_add_entities(
        DebugButton(controller=controller, device=device, description=description)
        for description in ATTRIBUTE_BUTTONS
        for device in controller.api.devices.all.values()
        if description.filter_fn(device)
    )

    # Camera siren, served by the camera-bridge off the mobile API.
    settings_coordinator = await async_get_camera_settings_coordinator(hass, config_entry)

    @core.callback
    def _build_siren(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """Siren start/stop for a camera this account may manually trigger."""
        siren = (camera_data.get("settings") or {}).get("siren") or {}
        # canManuallyTriggerSiren is a per-camera PERMISSION, not a hardware
        # capability -- a camera can advertise a siren this account is not
        # allowed to fire, and the app has a dedicated permission checker for
        # exactly that. Without the permission there is no button at all rather
        # than a button that always errors.
        if not siren.get("can_manually_trigger"):
            return []
        return [
            CameraSirenButton(settings_coordinator, camera_id, "start"),
            CameraSirenButton(settings_coordinator, camera_id, "stop"),
        ]

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_siren)


class DebugButton(AttributeBaseDevice, ButtonEntity):  # type: ignore
    """Integration button entity."""

    entity_description: AlarmdotcomButtonDescription
    _attr_available: bool = True

    async def async_press(self) -> None:
        """Handle the button press."""

        self.entity_description.press_fn(self.hass, self._device)


class CameraSirenButton(CameraSettingsEntity, ButtonEntity):
    """Start or stop the siren on one bridge-served TELUS camera.

    Deliberately two buttons rather than one switch: the siren is
    time-bounded server-side (the API returns a `timestampEnd`, and the
    duration is the server's choice, not ours), so there is no on/off state
    this integration can honestly hold -- "stop" exists to end it early.

    Pressing "start" makes a real siren sound in someone's home. Nothing else
    in this integration fires it, and no poll or state read reaches it.
    """

    _attr_icon = "mdi:bullhorn"

    def __init__(
        self, coordinator: CameraSettingsCoordinator, camera_id: str, action: str
    ) -> None:
        """Initialize one siren button; `action` is "start" or "stop"."""
        super().__init__(coordinator, camera_id, f"siren_{action}")

        self._action = action

    @property
    def available(self) -> bool:
        """Unavailable if the camera stops reporting the manual-trigger permission."""
        siren = self._settings.get("siren") or {}
        return super().available and bool(siren.get("can_manually_trigger"))

    async def async_press(self) -> None:
        """Send the siren command; raises HomeAssistantError if the bridge refuses.

        No coordinator refresh follows on purpose: a Button holds no state, so
        an immediate poll would re-read both routes upstream to change nothing a
        user can see, on an account whose rate limits can lock it.
        """
        await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_SIREN_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"action": self._action},
        )
