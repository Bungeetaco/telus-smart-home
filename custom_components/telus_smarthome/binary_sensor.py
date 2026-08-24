"""Alarmdotcom implementation of an HA binary sensor.

Mostly the pyalarmdotcomajax device-model platform (contact/motion/smoke/etc
sensors plus the virtual battery and malfunction ones). The one exception is
CameraBridgeConnectivitySensor, which reports whether the local camera-bridge
container is answering at all -- see its docstring for why it deliberately
breaks the usual coordinator-availability contract.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Final

from homeassistant import core
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from pyalarmdotcomajax.devices.registry import AllDevices_t
from pyalarmdotcomajax.devices.sensor import Sensor as libSensor
from pyalarmdotcomajax.devices.water_sensor import WaterSensor as libWaterSensor

from .base_device import AttributeBaseDevice, BaseDevice, HardwareBaseDevice
from .const import DATA_CONTROLLER, DOMAIN, SENSOR_SUBTYPE_BLACKLIST
from .controller import AlarmIntegrationController
from .device_type_langs import LANG_DOOR, LANG_WINDOW
from .sensor import StreamHealthCoordinator, async_get_stream_health_coordinator

LOGGER = logging.getLogger(__name__)

# device_class (below) used to loop over each of these ~100-word lists calling
# a separate re.search per word on every property access -- a hot path, since
# HA reads device_class on every state update for every contact sensor.
# Combining each list into one compiled alternation pattern turns that into a
# single-pass search per list instead of up to ~200 individual match attempts.
_LANG_DOOR_PATTERN = re.compile("|".join(re.escape(word) for _, word in LANG_DOOR), re.IGNORECASE)
_LANG_WINDOW_PATTERN = re.compile("|".join(re.escape(word) for _, word in LANG_WINDOW), re.IGNORECASE)


@dataclass
class AlarmdotcomAttributeDescriptionMixin:
    """Functions for an attribute entity."""

    value_fn: Callable[[BaseDevice], bool | None]
    filter_fn: Callable[[AllDevices_t], bool]
    extra_attribs_fn: Callable[[BaseDevice], dict | None]


@dataclass
class AlarmdotcomAttributeDescription(BinarySensorEntityDescription, AlarmdotcomAttributeDescriptionMixin):  # type: ignore
    """Describes an attribute entity."""


ATTRIBUTE_BINARY_SENSORS: Final = [
    AlarmdotcomAttributeDescription(
        key="malfunction",
        name="Malfunction",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.PROBLEM,
        has_entity_name=True,
        extra_attribs_fn=lambda device: {},
        value_fn=lambda device: device.malfunction,
        filter_fn=lambda device: device.malfunction is not None,
    ),
    AlarmdotcomAttributeDescription(
        key="battery",
        name="Battery",
        entity_category=EntityCategory.DIAGNOSTIC,
        device_class=BinarySensorDeviceClass.BATTERY,
        has_entity_name=True,
        extra_attribs_fn=lambda device: {"battery_level": device.battery_level},
        value_fn=lambda device: device.battery_alert,
        filter_fn=lambda device: device.battery_critical is not None and device.battery_low is not None,
    ),
]


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # pylint: disable=unused-argument
) -> None:
    """Set up the sensor platform."""

    # Create "real" Alarm.com sensors.
    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]

    async_add_entities(
        BinarySensor(
            controller=controller,
            device=device,
        )
        for device in [*controller.api.devices.sensors.values(), *controller.api.devices.water_sensors.values()]
        if device.device_subtype not in SENSOR_SUBTYPE_BLACKLIST and device.has_state
    )

    # Create "virtual" low battery and malfunction sensors.

    async_add_entities(
        AttributeBinarySensor(
            controller=controller,
            device=device,
            description=description,
        )
        for description in ATTRIBUTE_BINARY_SENSORS
        for device in controller.api.devices.all.values()
        if (
            description.filter_fn(device)
            and not (isinstance(device, libSensor) and device.device_subtype in SENSOR_SUBTYPE_BLACKLIST)
            and device.has_state
        )
    )

    # Bridge connectivity, on the same StreamHealthCoordinator sensor.py's
    # per-camera stream-health sensors poll -- the lazy getter is what keeps
    # the two concurrently-set-up platforms on ONE poller.
    stream_health_coordinator = await async_get_stream_health_coordinator(hass, config_entry)
    async_add_entities([CameraBridgeConnectivitySensor(stream_health_coordinator, config_entry)])


class BinarySensor(HardwareBaseDevice, BinarySensorEntity):  # type: ignore
    """Binary sensor device class."""

    _device: libSensor

    def __init__(self, controller: AlarmIntegrationController, device: AllDevices_t) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(controller, device)

    @property
    def device_type_name(self) -> str:
        """Return human readable device type name based on device class."""

        return str(self.device_class.value.replace("_", " ").title()) if self.device_class else "Sensor"

    @property
    def device_class(self) -> BinarySensorDeviceClass | None:
        """Return the class of this sensor, from DEVICE_CLASSES."""

        # Contact sensor:

        # Try to determine whether contact sensor is for a window or door by matching strings.
        derived_class: BinarySensorDeviceClass = None
        if (raw_subtype := self._device.device_subtype) in [
            libSensor.Subtype.CONTACT_SENSOR,
            libSensor.Subtype.CONTACT_SHOCK_SENSOR,
        ]:
            name = str(self._device.name)
            if _LANG_DOOR_PATTERN.search(name) is not None:
                derived_class = BinarySensorDeviceClass.DOOR
            if _LANG_WINDOW_PATTERN.search(name) is not None:
                derived_class = BinarySensorDeviceClass.WINDOW

        if derived_class is not None and raw_subtype in [
            libSensor.Subtype.CONTACT_SENSOR,
            libSensor.Subtype.CONTACT_SHOCK_SENSOR,
        ]:
            return derived_class

        # Water sensor:

        if isinstance(self._device, libWaterSensor):
            return BinarySensorDeviceClass.MOISTURE

        # All other sensors:

        if raw_subtype == libSensor.Subtype.SMOKE_DETECTOR:
            return BinarySensorDeviceClass.SMOKE
        if raw_subtype == libSensor.Subtype.CO_DETECTOR:
            return BinarySensorDeviceClass.CO
        if raw_subtype == libSensor.Subtype.PANIC_BUTTON:
            return BinarySensorDeviceClass.SAFETY
        if raw_subtype in [
            libSensor.Subtype.GLASS_BREAK_DETECTOR,
            libSensor.Subtype.PANEL_GLASS_BREAK_DETECTOR,
        ]:
            return BinarySensorDeviceClass.VIBRATION
        if raw_subtype in [
            libSensor.Subtype.MOTION_SENSOR,
            libSensor.Subtype.PANEL_MOTION_SENSOR,
        ]:
            return BinarySensorDeviceClass.MOTION
        if raw_subtype == libSensor.Subtype.FREEZE_SENSOR:
            return BinarySensorDeviceClass.COLD

        return None

    @property
    def available(self) -> bool:
        """Return True if entity is available."""

        # super() carries both the coordinator's health and BaseDevice's
        # device-still-exists check. Returning only `is_on is not None` discarded
        # both, so when TELUS auth failed or polling died these sensors kept
        # presenting their last known state as current -- actively misleading for
        # door, smoke and glass-break sensors, which is exactly when a stale "closed"
        # is worse than an honest "unavailable".
        return super().available and self.is_on is not None

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""

        # LOGGER.info(
        #     "Processing state %s for %s",
        #     self._device.state,
        #     self.name or self._device.name,
        # )

        match self._device.state:
            case libSensor.DeviceState.CLOSED | libSensor.DeviceState.IDLE | libWaterSensor.DeviceState.DRY:
                return False
            case libSensor.DeviceState.OPEN | libSensor.DeviceState.ACTIVE | libWaterSensor.DeviceState.WET:
                return True

        LOGGER.info(
            "Cannot determine binary sensor state for sensor %s. Found raw state of %s.",
            self._attr_name,
            self._device.state,
        )
        return None


class AttributeBinarySensor(AttributeBaseDevice, BinarySensorEntity):  # type: ignore
    """Attribute binary sensor."""

    entity_description: AlarmdotcomAttributeDescription

    def __init__(
        self,
        controller: AlarmIntegrationController,
        device: AllDevices_t,
        description: AlarmdotcomAttributeDescription,
    ) -> None:
        """Pass coordinator to CoordinatorEntity."""

        super().__init__(controller, device, description)

    @property
    def extra_state_attributes(self) -> Mapping[str, Any] | None:
        """Return the state attributes of the entity."""

        extra_attribs: dict[str, Any] = dict(super().extra_state_attributes or {})

        if description_attribs := self.entity_description.extra_attribs_fn(self):
            extra_attribs.update(description_attribs)

        return extra_attribs

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""

        return self.entity_description.value_fn(self)


class CameraBridgeConnectivitySensor(
    CoordinatorEntity[StreamHealthCoordinator], BinarySensorEntity
):
    """Whether the local camera-bridge container is answering its control API.

    ON while the coordinator's last /control/status poll got an answer out of
    the bridge, OFF while it did not (container down, mid-restart, or its
    control secret not on the shared mount yet). Grouped under the same
    synthetic Integration service device as the auth-health and talkdown
    sensors -- it reports on the integration's own plumbing, not on any
    TELUS device.

    Deliberately NOT the plain CoordinatorEntity availability contract: a
    coordinator whose poll fails marks its entities unavailable, and for a
    connectivity sensor "the bridge is down" IS the reportable state, not a
    reason to stop reporting -- an entity that goes unavailable exactly when
    it should say "off" can never say "off" at all. So availability is gated
    only on at least one poll attempt having completed (last_poll_ok is None
    until then), never on the poll having succeeded.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_translation_key = "camera_bridge"

    def __init__(self, coordinator: StreamHealthCoordinator, config_entry: ConfigEntry) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{config_entry.entry_id}_camera_bridge"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
            name="TELUS Smart Home Integration",
            manufacturer="TELUS",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def available(self) -> bool:
        """Available once a poll attempt has COMPLETED, successful or not."""
        return self.coordinator.last_poll_ok is not None

    @property
    def is_on(self) -> bool:
        """True while the bridge answered the most recent status poll."""
        return bool(self.coordinator.last_poll_ok)
