"""Alarmdotcom implementation of an HA select.

Three unrelated sources of selects live here: the Skybell configuration options
that come from pyalarmdotcomajax's device model; the bridge-served camera
settings (the wired chime type of a TELUS doorbell, its notification format and
its live-stream resolution -- docs/talkdown-fullduplex-protocol.md, E27); and
which partition a Bluetooth-paired phone disarms (E43), which is not a camera
setting at all and rides the arming-devices coordinator instead.
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Any

from homeassistant import core
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.components import persistent_notification
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import DeviceInfo, Entity, EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from pyalarmdotcomajax.devices.registry import AllDevices_t
from pyalarmdotcomajax.extensions import (
    CameraSkybellControllerExtension as libCameraSkybellControllerExtension,
)
from pyalarmdotcomajax.extensions import ConfigurationOption as libConfigurationOption
from pyalarmdotcomajax.extensions import (
    ConfigurationOptionType as libConfigurationOptionType,
)

from .arming_devices_coordinator import (
    ArmingDevicesCoordinator,
    arming_device_device_info,
    async_add_arming_device_entities,
    async_get_arming_devices_coordinator,
    async_set_enrolled_partition,
)
from .base_device import ConfigBaseDevice
from .arming_devices_coordinator import (
    PanelBluetoothCoordinator,
    async_add_panel_bluetooth_entities,
    async_get_panel_bluetooth_coordinator,
    async_set_bluetooth_disarming,
)
from .camera_settings_coordinator import (
    CameraSettingsCoordinator,
    CameraSettingsEntity,
    async_add_camera_entities,
    async_bridge_post,
    async_get_camera_settings_coordinator,
)
from .const import (
    CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_VIDEO_URL_TEMPLATE,
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

    @core.callback
    def _build_notification_format(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """One selector per camera that reports a settable notification format."""
        notifications = (camera_data.get("settings") or {}).get("notifications") or {}
        if not notifications.get("supports_format"):
            return []
        return [CameraNotificationFormatSelect(settings_coordinator, camera_id)]

    async_add_camera_entities(
        config_entry, settings_coordinator, async_add_entities, _build_notification_format
    )

    @core.callback
    def _build_live_resolution(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """One selector per camera that offers a choice of live-stream resolution."""
        video = (camera_data.get("settings") or {}).get("video") or {}
        if not video.get("supports_live_resolution"):
            return []
        return [CameraLiveResolutionSelect(settings_coordinator, camera_id)]

    async_add_camera_entities(
        config_entry, settings_coordinator, async_add_entities, _build_live_resolution
    )

    # The panel's Bluetooth disarm window. Its own coordinator, not the camera
    # settings one -- it is a property of the alarm panel, not of any camera.
    panel_bluetooth_coordinator = await async_get_panel_bluetooth_coordinator(
        hass, config_entry)

    @core.callback
    def _build_panel_bluetooth_timeout(settings: dict[str, Any]) -> list[Entity]:
        """The disarm-window picker, when the panel reports the field at all."""
        if not settings.get("supports_timeout"):
            return []
        return [PanelBluetoothTimeoutSelect(panel_bluetooth_coordinator, config_entry)]

    async_add_panel_bluetooth_entities(
        config_entry, panel_bluetooth_coordinator, async_add_entities,
        _build_panel_bluetooth_timeout)

    # Which partition each Bluetooth-paired phone disarms (backlog E43). Nothing
    # to do with cameras -- its own bridge route and its own coordinator, shared
    # with the disarming switch in switch.py.
    arming_coordinator = await async_get_arming_devices_coordinator(hass, config_entry)

    @core.callback
    def _build_enrolled_partition(device_id: str, record: dict[str, Any]) -> list[Entity]:
        """One partition picker per paired phone -- on a MULTI-partition account.

        Two gates, and the second is the interesting one.

        The first is the usual capability check: `enrolled_partition_id` reads
        identically on a record that cannot be saved, so offering the control on
        the value would give someone a picker over which partition their phone
        opens that does nothing at all when they use it.

        The second is a deliberate judgement, not a technical limit: on a
        SINGLE-partition account this control is created only if there is more
        than one partition to choose between. An entity offering exactly one
        option, already selected, cannot express anything -- the only interaction
        it permits is re-selecting what is already true, which is a
        whole-record PUT to a live alarm panel that changes nothing and carries
        every risk such a write carries. It would also appear on the majority of
        installs, since most homes have one partition, as a control that is
        permanently inert. No entity is the honest answer there, and the
        information is not lost: the disarming switch publishes
        `enrolled_partition_id` as an attribute either way.

        The bridge still ACCEPTS the write on a single-partition account -- this
        is a decision about what to put in front of a user, not about what the
        API can do -- and the entity appears by itself if a second partition is
        ever added, because this callback is re-offered every poll until it
        yields something.

        An empty `enrolled_partition_choices` is the bridge failing to read the
        partition list, not an account without partitions, and it correctly lands
        on the same "no entity" answer: a picker with nothing in it is worse than
        none, and it will be re-offered on the next poll.
        """
        if not record.get("supports_enrolled_partition"):
            return []
        if len(record.get("enrolled_partition_choices") or []) < 2:
            return []
        return [ArmingDeviceEnrolledPartitionSelect(
            arming_coordinator, config_entry, device_id)]

    async_add_arming_device_entities(
        config_entry, arming_coordinator, async_add_entities, _build_enrolled_partition)


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
    def _device_label(self) -> str:
        """A name for this camera to put in a notification.

        `device_entry` is populated by HA once the entity is added, but it is not
        guaranteed at every point in an entity's life, and a notification is not
        worth an AttributeError -- so the camera id is the fallback.
        """
        device = self.device_entry
        if device is not None:
            return device.name_by_user or device.name or self._camera_id
        return self._camera_id

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

        # Read BEFORE the write: if this option is one the camera flags as
        # disabling the indoor chime, TELUS turns that chime off as a side effect
        # and does NOT turn it back on when a real type is selected again
        # (verified live -- backlog E37). Nothing surfaces that, so the chime just
        # stops working and the switch quietly reads off.
        #
        # Deliberately not worked around by re-enabling the chime: silently
        # re-arming something the user may have wanted off is the worse failure.
        # Telling them is not.
        disables = next(
            (bool(choice.get("disables_indoor_chime"))
             for choice in self._audio.get("chime_type_choices") or []
             if choice.get("value") == values[option]),
            False,
        )
        chime_was_on = bool(self._audio.get("indoor_chime_on"))

        audio = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"chime_type": values[option]},
        )

        if disables and chime_was_on:
            persistent_notification.async_create(
                self.hass,
                (
                    f"Selecting **{option}** turned off the indoor chime on "
                    f"{self._device_label}, because that setting means there is "
                    "no wired chime to ring.\n\n"
                    "TELUS does this, and choosing a real chime type again does "
                    "**not** turn it back on — switch the indoor chime back on by "
                    "hand if you want it."
                ),
                title="Indoor chime turned off",
                # Per camera, so a second change replaces the first notice rather
                # than stacking another copy of the same warning.
                notification_id=f"{DOMAIN}_{self._camera_id}_indoor_chime_disabled",
            )
        # The route replies with the whole audio section re-read from the camera,
        # so the new type -- and the indoor chime switch's availability, which
        # depends on it -- both update without a poll. See the same trade in
        # number.py's volume write.
        if audio:
            self.coordinator.async_apply_settings_section(self._camera_id, "audio", audio)
        else:
            await self.coordinator.async_request_refresh()


class CameraNotificationFormatSelect(CameraSettingsEntity, SelectEntity):
    """How much TELUS puts in the push notifications it sends to its OWN app.

    Text only, or a thumbnail of what triggered it. This changes what the TELUS
    app shows on a phone; it has no effect on anything Home Assistant sends, and
    it is not a Home Assistant notification setting -- see the switches in
    switch.py that share this section.

    The choices are a fixed pair rather than something the camera publishes: the
    upstream field is an enum whose values the bridge resolves from alarm.com's
    own web bundle, and only the two members alarm.com still offers are exposed.
    See the bridge's notification settings docs for why the other two members
    exist and are withheld.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:message-image-outline"

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the notification format selector for one camera."""
        super().__init__(coordinator, camera_id, "telus_app_notification_format")

    @property
    def _notifications(self) -> dict[str, Any]:
        """The camera's notifications settings section."""
        return self._settings.get("notifications") or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting the format as settable."""
        return super().available and bool(self._notifications.get("supports_format"))

    @property
    def _values_by_label(self) -> dict[str, int]:
        """The offered formats as {label shown: value written}.

        No fallback to bare numbers, unlike the chime type selector: these
        choices are a constant the bridge always sends alongside the flag, not a
        secondary upstream read that can fail. If they are missing, the honest
        result is an empty list rather than a selector of unexplained integers --
        a raw notification-format number means nothing to anyone.
        """
        labelled: dict[str, int] = {}
        for choice in self._notifications.get("format_choices") or []:
            value, label = choice.get("value"), choice.get("label")
            if value is None or not label:
                continue
            labelled[str(label)] = int(value)
        return labelled

    @property
    def options(self) -> list[str]:
        """The formats this camera offers."""
        return list(self._values_by_label)

    @property
    def current_option(self) -> str | None:
        """The label matching the reported format, or None if none does.

        None is expected rather than exceptional here: the upstream enum has a
        member meaning "unknown" that a camera reports when it has no doorbell
        notifications at all, and two more the bridge deliberately does not
        offer. Any of those lands here, and unknown is the truthful rendering.
        """
        current = self._notifications.get("format")
        if current is None:
            return None
        return next(
            (label for label, value in self._values_by_label.items() if value == current),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        """Write the chosen format and adopt the section that comes back."""
        values = self._values_by_label
        if option not in values:
            raise HomeAssistantError(
                f"This camera no longer offers the notification format {option}."
            )

        notifications = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"format": values[option]},
        )
        if notifications:
            self.coordinator.async_apply_settings_section(
                self._camera_id, "notifications", notifications
            )
        else:
            await self.coordinator.async_request_refresh()


class CameraLiveResolutionSelect(CameraSettingsEntity, SelectEntity):
    """The resolution of a camera's LIVE stream -- including the one this
    integration restreams.

    Not a clip setting: recorded clips have their own resolution, and changing
    this does not touch it. This is the live view, and it was confirmed against
    real hardware that it changes what the bridge itself receives -- selecting
    Standard Definition took the bridge's own stream from 1440x1440 to 480x480.
    That is worth knowing in both directions: it is the control for cutting a
    camera's bandwidth, and it is also a way to accidentally make every Home
    Assistant view of that camera worse.

    The bridge asks TELUS for the *highest resolution source available*, which
    reads like it would ignore this setting. It does not -- "highest available"
    turns out to mean highest under the current selection, not the camera's
    absolute maximum.
    """

    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:video-high-definition"

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the live-resolution selector for one camera."""
        super().__init__(coordinator, camera_id, "live_resolution")

    @property
    def _video(self) -> dict[str, Any]:
        """The camera's video settings section."""
        return self._settings.get("video") or {}

    @property
    def available(self) -> bool:
        """Also unavailable while the camera offers no choice of resolution.

        The bridge derives that from the option object itself being empty, which
        is how this API says "no choice here" -- and from a separate server-side
        veto flag. Either one takes the control away rather than leaving a
        selector that cannot be applied.
        """
        return super().available and bool(self._video.get("supports_live_resolution"))

    @property
    def _values_by_label(self) -> dict[str, str]:
        """The offered resolutions as {label shown: value written}.

        Prefers the vendor's own labels ("Full High Definition (1440x1440)") and
        falls back to the bare values, which are the geometries themselves and so
        stay perfectly readable -- unlike the notification format, losing the
        labels here costs clarity but not meaning.
        """
        labelled: dict[str, str] = {}
        for choice in self._video.get("live_resolution_choices") or []:
            value, label = choice.get("value"), choice.get("label")
            if value is None or not label:
                continue
            labelled[str(label)] = str(value)
        if labelled:
            return labelled

        return {str(value): str(value)
                for value in self._video.get("live_resolution_options") or []
                if value is not None}

    @property
    def options(self) -> list[str]:
        """The resolutions this camera offers."""
        return list(self._values_by_label)

    @property
    def current_option(self) -> str | None:
        """The label matching the reported resolution, or None if none does."""
        current = self._video.get("live_resolution")
        if current is None:
            return None
        return next(
            (label for label, value in self._values_by_label.items() if value == current),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        """Write the chosen resolution and adopt the video section that comes back.

        Note the change reaches an already-running stream only when that stream is
        renegotiated; an established session keeps the resolution it started with.
        """
        values = self._values_by_label
        if option not in values:
            raise HomeAssistantError(
                f"This camera no longer offers the live resolution {option}."
            )

        video = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_VIDEO_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"live_resolution": values[option]},
        )
        if video:
            self.coordinator.async_apply_settings_section(self._camera_id, "video", video)
        else:
            await self.coordinator.async_request_refresh()


class ArmingDeviceEnrolledPartitionSelect(
    CoordinatorEntity[ArmingDevicesCoordinator], SelectEntity
):
    """Which partition one Bluetooth-paired phone disarms (backlog E43).

    The companion to the "Disarm alarm on arrival" switch in switch.py, and the
    same class of setting rather than a lesser one: that switch decides whether a
    phone may disarm, this decides WHAT it disarms, and moving a phone onto a
    partition it should not open has the same outcome as letting a phone disarm
    that should not. alarm.com is the configuration authority for both.

    Only created on an account with more than one partition -- see
    _build_enrolled_partition in async_setup_entry for why that is a deliberate
    choice and not an oversight.

    It deliberately does NOT extend CameraSettingsEntity like the three selects
    above, for the reasons ArmingDeviceDisarmingSwitch spells out at length: that
    base is camera-shaped in four places at once (its coordinator, its
    section-of-a-/settings-body value lookup, its 422 wording, and how it folds a
    write's reply back), and none of the four fits a record that has no camera
    anywhere near it.
    """

    _attr_has_entity_name = True
    # Configuration, like the switch it sits beside: how the alarm system is set
    # up, not something a dashboard should present next to arm/disarm.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "arming_device_enrolled_partition"
    _attr_icon = "mdi:home-group"

    def __init__(
        self,
        coordinator: ArmingDevicesCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the enrolled-partition selector for one paired phone."""
        super().__init__(coordinator)

        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_enrolled_partition"
        # The same device as the disarming switch, through the shared helper --
        # either entity can exist without the other, so whichever appears alone
        # still has to name the phone properly.
        self._attr_device_info = arming_device_device_info(
            config_entry, coordinator, device_id)

    @property
    def _record(self) -> dict[str, Any]:
        """This phone's whole coordinator record, or {} once the bridge drops it."""
        return (self.coordinator.data or {}).get(self._device_id) or {}

    @property
    def _values_by_label(self) -> dict[str, str]:
        """The offered partitions as {label shown: partition number written}.

        No fallback to bare values, unlike the chime type selector: the choices
        come from the account's own partition list, and an entry with no
        description already falls back to its number on the bridge side, so there
        is nothing here that could arrive labelless. If the list is missing
        entirely the honest result is an empty list, which takes the control
        unavailable rather than offering numbers with no way to tell which
        partition is which.

        Labels are the partitions' own descriptions, passed straight through as
        text: they are whatever the account holder called them and may contain a
        name or a curly apostrophe, so they are never parsed or reshaped.
        """
        labelled: dict[str, str] = {}
        for choice in self._record.get("enrolled_partition_choices") or []:
            value, label = choice.get("value"), choice.get("label")
            if value is None or not label:
                continue
            labelled[str(label)] = str(value)
        return labelled

    @property
    def available(self) -> bool:
        """Unavailable unless the bridge reports this phone as writable AND
        currently offers something to choose between.

        Fails closed on every unknown, like the switch: a failed poll, a phone
        the bridge no longer lists (unpaired, or a system it cannot see), a
        record that stops advertising the field, and a partition list the bridge
        could not read this cycle all land here. Showing a partition for a
        control that decides which one a phone opens, while not actually knowing
        it, is worse than showing nothing.
        """
        return (
            super().available
            and bool(self._record)
            and bool(self._record.get("supports_enrolled_partition"))
            and bool(self._values_by_label)
        )

    @property
    def options(self) -> list[str]:
        """The partitions this account currently offers."""
        return list(self._values_by_label)

    @property
    def current_option(self) -> str | None:
        """The label of the partition this phone is enrolled on, or None.

        None renders as unknown, which is the honest answer for a phone reporting
        a partition that is not in the account's own list: something is out of
        step, and picking a plausible option here would both misreport the panel
        and make the next write look like a no-op.
        """
        current = self._record.get("enrolled_partition_id")
        if current is None:
            return None
        return next(
            (label for label, value in self._values_by_label.items()
             if value == str(current)),
            None,
        )

    async def async_select_option(self, option: str) -> None:
        """Move this phone to the chosen partition and adopt the echoed record.

        Only ever reached from a user picking an option on this entity.
        """
        values = self._values_by_label
        if option not in values:
            # HA rejects an option outside `options` before it gets here, so this
            # only catches the race where the account's partitions changed
            # between the UI rendering the list and the click landing. Better a
            # clear error than writing a stale partition number to a live panel.
            raise HomeAssistantError(
                f"This account no longer offers the partition {option}.")

        # The opaque device id and the partition NUMBER, never the phone's
        # description: at INFO that would otherwise put a household member's name
        # in the log of every install.
        LOGGER.info("Enrolling arming device %s on partition %s",
                    self._device_id, values[option])
        record = await async_set_enrolled_partition(
            self.hass, self._device_id, values[option])
        # The route answers with the record as the server has it after the write,
        # so adopting it beats a poll -- which would cost live Alarm.com round
        # trips to learn what is already in hand, on a coordinator that
        # deliberately polls every 15 minutes. A mismatched or unreadable reply
        # falls through to a real re-read rather than to an assumption that the
        # requested value took.
        if record.get("id") == self._device_id:
            self.coordinator.async_apply_device(self._device_id, record)
        else:
            await self.coordinator.async_request_refresh()


class PanelBluetoothTimeoutSelect(CoordinatorEntity[PanelBluetoothCoordinator],
                                  SelectEntity):
    """How long the panel stays disarmable after a paired phone is seen.

    A SELECT rather than a number, because the panel does not take a free range:
    the vendor offers exactly five windows -- 1, 5, 10, 20 and 30 minutes -- and
    the bridge reports both the unit and that set. Neither appears anywhere in
    the upstream document; both come from the portal's own picker, which is why
    a client cannot derive them and why they are carried in the response.

    Presented as "5 minutes" rather than a bare "5", since a number with no unit
    is exactly what this entity existed to avoid.

    Trap: the bridge's write shape for this endpoint is INFERRED from the sibling
    per-device endpoint rather than captured. A failure here is at least as
    likely to be that reconstruction as the panel.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "panel_bluetooth_timeout"
    _attr_icon = "mdi:timer-cog-outline"

    def __init__(self, coordinator: PanelBluetoothCoordinator,
                 config_entry: ConfigEntry) -> None:
        """Initialize the panel's Bluetooth disarm-window picker."""
        super().__init__(coordinator)
        self._attr_unique_id = f"{config_entry.entry_id}_panel_bluetooth_timeout"
        # The integration's own service device, the same one the pairing switch
        # in switch.py uses: this is a property of the panel's Bluetooth feature,
        # not of any camera and not of any one paired phone.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
        )

    @property
    def _settings(self) -> dict[str, Any]:
        """The panel's Bluetooth disarming settings."""
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        """Unavailable while the panel stops reporting the field, or the read fails."""
        return (super().available and bool(self._settings.get("supports_timeout"))
                and bool(self._values_by_label))

    @property
    def _values_by_label(self) -> dict[str, int]:
        """{"5 minutes": 5, ...}, in the order the panel offered them.

        Labelled from the unit the bridge reports rather than a hardcoded word,
        so a panel that ever reports a different unit does not end up mislabelled
        here -- the one failure this entity is shaped to avoid.
        """
        unit = str(self._settings.get("timeout_unit") or "").strip()
        labelled: dict[str, int] = {}
        for value in self._settings.get("timeout_options") or []:
            try:
                number = int(value)
            except (TypeError, ValueError):
                continue
            labelled[f"{number} {unit}".strip() if unit else str(number)] = number
        return labelled

    @property
    def options(self) -> list[str]:
        """The windows this panel offers."""
        return list(self._values_by_label)

    @property
    def current_option(self) -> str | None:
        """The label matching the reported window, or None if none does.

        None is honest for a panel reporting a window the vendor no longer
        offers: inventing the nearest match would misreport it and make the next
        write look like a no-op.
        """
        current = self._settings.get("timeout")
        if current is None:
            return None
        return next((label for label, value in self._values_by_label.items()
                     if value == current), None)

    async def async_select_option(self, option: str) -> None:
        """Write the chosen window and adopt the document that comes back."""
        values = self._values_by_label
        if option not in values:
            raise HomeAssistantError(
                f"This panel no longer offers a {option} Bluetooth disarm window.")

        LOGGER.info("Setting panel Bluetooth disarm window to %s", option)
        settings = await async_set_bluetooth_disarming(self.hass,
                                                       timeout=values[option])
        # The route answers with the document as the server has it after the
        # write, so the pairing switch beside this one refreshes out of the same
        # reply instead of costing a second live round trip.
        if settings:
            self.coordinator.async_apply_settings(settings)
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
