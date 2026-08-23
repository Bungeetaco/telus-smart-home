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
from homeassistant.const import EntityCategory
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo, Entity
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

from .arming_devices_coordinator import (
    ArmingDevicesCoordinator,
    async_add_arming_device_entities,
    async_get_arming_devices_coordinator,
    async_set_disarming_enabled,
)
from .base_device import ConfigBaseDevice
from .bridge_client import bridge_get, read_control_secret
from .camera_settings_coordinator import (
    CameraSettingsCoordinator,
    CameraSettingsEntity,
    async_add_camera_entities,
    async_bridge_post,
    async_get_camera_settings_coordinator,
)
from .const import (
    AUTOMATION_RULES_POLL_INTERVAL_SECONDS,
    CAMERA_BRIDGE_AUTOMATION_PAUSE_URL_TEMPLATE,
    CAMERA_BRIDGE_AUTOMATION_RESUME_URL_TEMPLATE,
    CAMERA_BRIDGE_AUTOMATIONS_URL,
    CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_LED_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_PRIVACY_URL_TEMPLATE,
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

    # Camera privacy mode, served by the camera-bridge off the mobile API.
    settings_coordinator = await async_get_camera_settings_coordinator(hass, config_entry)

    @core.callback
    def _build_privacy(camera_id: str, _camera_data: dict[str, Any]) -> list[Entity]:
        """One privacy switch per bridge-reported camera, supported or not."""
        # The deliberate exception to "no entity for an unreported capability":
        # privacy support is re-read every poll, so "this camera can't do it" and
        # "the bridge didn't answer this cycle" arrive identically. An entity that
        # comes and goes takes its history and any automation referencing it with
        # it, which is worse than one that reads unavailable for a poll or two.
        return [CameraPrivacyModeSwitch(settings_coordinator, camera_id)]

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_privacy)

    @core.callback
    def _build_chime(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """The doorbell's two chime toggles, each only if this camera reports it."""
        audio = (camera_data.get("settings") or {}).get("audio") or {}
        # Unlike privacy above, these are gated: the two <name>FeatureSupport
        # enums are the only thing separating a doorbell whose chime can be
        # toggled from one that sends the identical payload and refuses the
        # write with a 422. They are also independent of each other -- the
        # reference doorbell reports both, but an indoor-only unit is exactly
        # the shape the flags exist to describe.
        entities: list[Entity] = []
        if audio.get("supports_indoor_chime"):
            entities.append(CameraIndoorChimeSwitch(settings_coordinator, camera_id))
        if audio.get("supports_outdoor_tone"):
            entities.append(CameraOutdoorToneSwitch(settings_coordinator, camera_id))
        return entities

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_chime)

    @core.callback
    def _build_led(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """The status-LED switch, only for a camera that reports it can switch it."""
        led = (camera_data.get("settings") or {}).get("led") or {}
        # Gated like the chime toggles rather than offered unconditionally like
        # privacy above: canturnonoffdeviceled is the only thing separating a
        # device whose indicator light can be switched from one that publishes
        # the identical `led` shape and refuses the write with a 422. A bridge
        # older than the `led` section sends no section at all, which reads the
        # same way and correctly yields no entity.
        if not led.get("supports_enabled"):
            return []
        return [CameraStatusLedSwitch(settings_coordinator, camera_id)]

    async_add_camera_entities(config_entry, settings_coordinator, async_add_entities, _build_led)

    @core.callback
    def _build_app_notifications(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """The TELUS-app push notification toggles this camera reports it can set."""
        notifications = (camera_data.get("settings") or {}).get("notifications") or {}
        # Gated individually, like the chime toggles: upstream, motion and button
        # press share one support enum while smart chime has its own, and motion
        # carries a further per-device availability flag on top. The bridge has
        # already collapsed all of that into these three booleans, so this trusts
        # them rather than re-deriving the rules. The reference doorbell reports
        # the first two and not the third and so gets exactly two switches; a
        # bridge older than this section sends no section at all, which reads the
        # same way and correctly yields nothing.
        entities: list[Entity] = []
        if notifications.get("supports_motion"):
            entities.append(CameraMotionAppNotificationSwitch(settings_coordinator, camera_id))
        if notifications.get("supports_button_press"):
            entities.append(CameraButtonPressAppNotificationSwitch(settings_coordinator, camera_id))
        if notifications.get("supports_smart_chime"):
            entities.append(CameraSmartChimeAppNotificationSwitch(settings_coordinator, camera_id))
        return entities

    async_add_camera_entities(
        config_entry, settings_coordinator, async_add_entities, _build_app_notifications)

    # Bluetooth arming devices: whether a paired phone may disarm the panel on
    # approach. Nothing to do with cameras -- its own bridge route, its own
    # coordinator, its own device per phone.
    arming_coordinator = await async_get_arming_devices_coordinator(hass, config_entry)

    @core.callback
    def _build_arming_device(device_id: str, record: dict[str, Any]) -> list[Entity]:
        """One disarming switch per paired phone that reports it can be written.

        Gated, never unconditional: `disarming_enabled` reads identically on a
        record that cannot be saved, so offering the switch on the value would
        give someone a control over whether their alarm auto-disarms that does
        nothing at all when they use it. No entity is the honest answer.
        """
        if not record.get("supports_disarming_toggle"):
            return []
        return [ArmingDeviceDisarmingSwitch(arming_coordinator, config_entry, device_id)]

    async_add_arming_device_entities(
        config_entry, arming_coordinator, async_add_entities, _build_arming_device)


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


class CameraPrivacyModeSwitch(CameraSettingsEntity, SwitchEntity):
    """Privacy mode for one bridge-served TELUS camera.

    On means privacy mode is active, i.e. the camera has stopped capturing.
    Privacy mode is gated per camera by the account's `SupportsPrivacyMode`
    flag, which was false for every camera on the account this was built
    against, so the unsupported case is the expected one: the switch stays
    unavailable rather than offering a control the camera would refuse.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the privacy switch for one camera."""
        super().__init__(coordinator, camera_id, "privacy_mode")

    @property
    def _privacy(self) -> dict[str, Any]:
        """The camera's privacy section of the bridge's settings response."""
        return self._settings.get("privacy") or {}

    @property
    def available(self) -> bool:
        """Unavailable while the camera reports privacy mode as unsupported."""
        return super().available and bool(self._privacy.get("supported"))

    @property
    def is_on(self) -> bool | None:
        """True while privacy mode is active; None if the state isn't known."""
        active = self._privacy.get("active")
        return bool(active) if active is not None else None

    async def _async_set_privacy(self, active: bool) -> None:
        await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_PRIVACY_URL_TEMPLATE.format(camera_id=self._camera_id),
            {"active": active},
        )
        # Read the state back rather than assuming the command took: this
        # command was never observed live on a real account (the app exposes no
        # UI for it), so an accepted request is not proof the camera moved.
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn privacy mode on (the camera stops capturing)."""
        await self._async_set_privacy(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn privacy mode off (the camera resumes capturing)."""
        await self._async_set_privacy(False)


class _CameraSettingToggle(CameraSettingsEntity, SwitchEntity):
    """One boolean field of one bridge-served camera settings section.

    Every camera-settings switch below is the same three moving parts: it exists
    only while the camera reports a supports_* flag, it reads one boolean out of
    one section of the /settings body, and a user toggling it writes that one
    field to the POST route that owns the section, adopting the refreshed
    section the route answers with. Only the section, the route and the three
    field names differ, so they live here once and each subclass carries little
    more than those names.

    `_state_key` and `_write_key` are separate from each other and from
    `_support_key` on purpose: the bridge's read and write vocabularies are not
    the same everywhere (the chime route takes `indoor_on` for what it reports
    as `indoor_chime_on`). Where a section does name all three alike, the
    subclass says so rather than the base assuming it.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH
    # Configuration, not a primary control -- the same tier as the speaker
    # volume in number.py: how the doorbell behaves, not something a dashboard
    # should lead with.
    _attr_entity_category = EntityCategory.CONFIG

    # Set by each subclass.
    _section: str  # /settings section the state is read out of
    _url_template: str  # POST route owning that section, {camera_id} unformatted
    _support_key: str  # flag deciding whether the control exists at all
    _state_key: str  # field the current state is read from
    _write_key: str  # field the write route expects it back under

    @property
    def _section_data(self) -> dict[str, Any]:
        """This toggle's settings section, or {} if the camera didn't report it."""
        return self._settings.get(self._section) or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting this toggle as settable."""
        return super().available and bool(self._section_data.get(self._support_key))

    @property
    def is_on(self) -> bool | None:
        """True while the setting is enabled; None if the camera hasn't said."""
        # `is not None`, not truthiness: False is a real answer ("it's off") and
        # must not read as "unknown" the way a missing field does.
        value = self._section_data.get(self._state_key)
        return bool(value) if value is not None else None

    async def _async_set(self, enabled: bool) -> None:
        """Write this one field and adopt the section that comes back.

        Only ever reached from a user toggling the entity: every one of these
        changes a real setting on a real device or account.
        """
        section = await async_bridge_post(
            self.hass,
            self._url_template.format(camera_id=self._camera_id),
            {self._write_key: enabled},
        )
        # These routes answer with the whole section re-read after the write, so
        # adopting it beats an immediate poll -- which would cost live TELUS
        # round trips to learn what is already in hand. It also matters beyond
        # this one field: each write is a read-modify-write of the whole
        # section, and sibling entities read the rest of it (the chime type the
        # select shows, the LED brightness the number shows).
        if section:
            self.coordinator.async_apply_settings_section(self._camera_id, self._section, section)
        else:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn this setting on."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn this setting off."""
        await self._async_set(False)


class _CameraChimeToggle(_CameraSettingToggle):
    """Shared half of the doorbell's two chime on/off toggles.

    Both read one boolean out of the camera's audio settings section, are gated
    on their own support flag, and write one field of the same chime route, so
    the subclasses below carry nothing but those three names. Toggling either
    makes (or stops making) a real sound in someone's home.
    """

    _section = "audio"
    _url_template = CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE


class CameraIndoorChimeSwitch(_CameraChimeToggle):
    """Whether the doorbell rings the indoor chime it is wired to."""

    _support_key = "supports_indoor_chime"
    _state_key = "indoor_chime_on"
    _write_key = "indoor_on"
    _attr_icon = "mdi:bell-ring"

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the indoor chime switch for one camera."""
        super().__init__(coordinator, camera_id, "indoor_chime")

    @property
    def available(self) -> bool:
        """Also unavailable while the selected chime type is one that disables it.

        Picking the "no wired chime" type leaves nothing for this toggle to ring,
        and the portal greys its control out; mirroring that beats offering a
        switch whose position means nothing.

        The rule is read off the chime type the camera currently reports, never
        off a hardcoded number: the bridge carries `disables_indoor_chime` per
        option precisely so no client has to know which value means "none" on a
        given model. If the labelled option list is missing (it comes from a
        secondary upstream call that can fail) or the current type isn't in it,
        the honest answer is that we don't know -- so leave the switch usable
        rather than guessing it into unavailability.
        """
        if not super().available:
            return False

        audio = self._section_data
        chime_type = audio.get("chime_type")
        selected = next(
            (
                choice
                for choice in (audio.get("chime_type_choices") or [])
                if choice.get("value") == chime_type
            ),
            None,
        )
        if selected is None:
            return True
        return not selected.get("disables_indoor_chime")


class CameraOutdoorToneSwitch(_CameraChimeToggle):
    """Whether the doorbell plays its own tone out of its speaker.

    Independent of the indoor chime: this is the sound at the door, audible to
    whoever pressed the button, and a doorbell can be set to one, both or
    neither. Its *volume* is a separate control behind a separate flag
    (`supports_outdoor_tone_volume`), which the reference doorbell reports as
    unsupported -- so no entity offers it (docs backlog E26).
    """

    _support_key = "supports_outdoor_tone"
    _state_key = "outdoor_tone_on"
    _write_key = "outdoor_on"
    _attr_icon = "mdi:bullhorn-outline"

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the outdoor tone switch for one camera."""
        super().__init__(coordinator, camera_id, "outdoor_tone")


class CameraStatusLedSwitch(_CameraSettingToggle):
    """The status indicator light on one bridge-served TELUS camera.

    Alarm.com's portal calls this the "Camera Status LED": the small light on
    the device's face that shows it is powered and recording. Turning it off
    changes nothing about what the camera does, only whether it advertises
    itself -- which is the usual reason to want it off. The camera keeps
    working; it just stops showing that it is.

    It is emphatically NOT the infrared illuminator (that is `ir_led_enabled` in
    the `image` section, a separate field behind a separate flag) and not a
    floodlight -- the reference doorbell has none.

    This deliberately did NOT share the chime base when it was added: that base
    existed for two subclasses of one section and one route, and parameterizing
    the section and the URL onto it would have bought a single caller nothing.
    The TELUS-app notification toggles below are the second caller that changes
    the arithmetic, so the generic half moved up into _CameraSettingToggle and
    this reads and writes the `led` section through it. Behaviour, unique id and
    translation key are unchanged. The echoed section it adopts also carries the
    brightness the number entity in number.py reads, which this write's
    read-modify-write may have refreshed.
    """

    _section = "led"
    _url_template = CAMERA_BRIDGE_CAMERA_LED_URL_TEMPLATE
    _support_key = "supports_enabled"
    _state_key = "enabled"
    _write_key = "enabled"
    _attr_icon = "mdi:led-on"

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the status LED switch for one camera."""
        super().__init__(coordinator, camera_id, "status_led")


class _CameraNotificationToggle(_CameraSettingToggle):
    """Shared half of the doorbell's TELUS-app push notification toggles.

    These control what TELUS/Alarm.com pushes to ITS OWN mobile app. They are
    not Home Assistant notifications, automations or events, and turning one off
    changes nothing about what this integration reports -- the realistic reason
    to want one off is to stop being told twice about the same doorbell press,
    once by the TELUS app and once by an HA automation. That is why every entity
    name here says "TELUS app" instead of a bare "Motion notifications", which
    on a device called Doorbell would read like a Home Assistant switch.

    Unlike the chime route, this section names the support flag, the state and
    the write field after one thing, so a subclass varies only `_field`.

    The section also reports a read-only `format` enum, which deliberately gets
    no entity: see CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE in const.py.
    """

    _section = "notifications"
    _url_template = CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE

    _field: str  # the notifications field, under the same name for read and write

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize one TELUS-app notification toggle for one camera."""
        super().__init__(coordinator, camera_id, f"telus_app_{self._field}_notifications")

        self._support_key = f"supports_{self._field}"
        self._state_key = self._field
        self._write_key = self._field


class CameraMotionAppNotificationSwitch(_CameraNotificationToggle):
    """Whether TELUS pushes this camera's motion alerts to the TELUS app."""

    _field = "motion"
    _attr_icon = "mdi:motion-sensor"


class CameraButtonPressAppNotificationSwitch(_CameraNotificationToggle):
    """Whether TELUS pushes a doorbell-button press to the TELUS app.

    The one most likely to be turned off: a press already reaches Home Assistant
    as an event, so leaving both on means two phone notifications for one visitor.
    """

    _field = "button_press"
    _attr_icon = "mdi:doorbell"


class CameraSmartChimeAppNotificationSwitch(_CameraNotificationToggle):
    """Whether TELUS pushes an alert when the doorbell plays its smart chime.

    Behind its own support flag rather than the one the two above share, and the
    reference doorbell reports it unsupported -- so no entity is created there.
    That is the expected outcome, not a fault.
    """

    _field = "smart_chime"
    _attr_icon = "mdi:bell-alert-outline"


class ArmingDeviceDisarmingSwitch(CoordinatorEntity[ArmingDevicesCoordinator], SwitchEntity):
    """Whether one paired phone disarms the alarm when it arrives.

    On means that phone walking up to the house disarms the panel over
    Bluetooth. This is the most consequential switch in the integration -- the
    siren buttons are only loud, this one decides whether the alarm is on at all
    when someone approaches -- and alarm.com is the configuration authority for
    it (the panel's Control4 endpoint is local-access-only and independent), so
    there is no second place a mistake here gets corrected from.

    It deliberately does NOT extend _CameraSettingToggle above, despite being
    another "one boolean of one bridge record" switch. That base is not merely
    camera-flavoured, it is camera-shaped in four places that all have to be
    true at once: it is a CameraSettingsEntity, so it takes the camera settings
    coordinator and attaches to a camera's device; it reads its value out of a
    named SECTION of a /settings body, where this record is flat; it writes
    through async_bridge_post, whose 422 wording is about cameras; and it folds
    the reply back with async_apply_settings_section. Fitting this to it would
    mean either parameterizing all four onto a base that four camera platforms
    depend on -- a file this change does not own -- or subclassing it and
    overriding every one, which is the same code with a misleading ancestor.
    What the two actually share is the CoordinatorEntity + SwitchEntity pair
    below and roughly ten lines, so this carries them itself.

    The device it belongs to is the phone; see _attr_device_info below.
    """

    _attr_has_entity_name = True
    _attr_device_class = SwitchDeviceClass.SWITCH
    # Configuration, like the camera setting toggles: this is how the alarm
    # system is set up, not a control a dashboard should present alongside
    # arm/disarm. Being in the config bucket does not make it minor -- it makes
    # it something a user finds where they went looking for it.
    _attr_entity_category = EntityCategory.CONFIG
    _attr_translation_key = "arming_device_disarming"
    _attr_icon = "mdi:cellphone-key"

    def __init__(
        self,
        coordinator: ArmingDevicesCoordinator,
        config_entry: ConfigEntry,
        device_id: str,
    ) -> None:
        """Initialize the disarming switch for one paired phone."""
        super().__init__(coordinator)

        self._device_id = device_id
        self._attr_unique_id = f"{device_id}_bluetooth_disarming"

        # Its OWN device, not a camera's and not the integration service device.
        # A paired phone is a real thing the panel knows about, with an
        # alarm.com device id in the same namespace as every other device here,
        # so it is registered the way camera.py and base_device.py register
        # theirs: identified by that id, named by what the account calls it.
        # Hanging these off the service device instead (as the automation rule
        # switches do, having no device of their own to name) would pile one
        # switch per household phone onto a single "TELUS Smart Home
        # Integration" entry and read as "<the integration> <someone>'s iPhone",
        # which says nothing about whose phone opens the house.
        #
        # via_device points at that service device, which sensor.py's auth
        # health sensor creates unconditionally at setup -- so unlike the
        # dangling link base_device.py warns about, it always resolves.
        #
        # `name` is the phone's description exactly as the account holder typed
        # it, passed straight through: it may contain a curly apostrophe
        # (U+2019) and it is never parsed or reshaped. The bridge already falls
        # back to the device id when the record has no description.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device_id)},
            name=(coordinator.data or {}).get(device_id, {}).get("description") or device_id,
            model="Bluetooth arming device",
            via_device=(DOMAIN, f"{config_entry.entry_id}_integration"),
        )

    @property
    def _record(self) -> dict[str, Any]:
        """This phone's whole coordinator record, or {} once the bridge drops it."""
        return (self.coordinator.data or {}).get(self._device_id) or {}

    @property
    def available(self) -> bool:
        """Unavailable unless the bridge currently reports this phone as writable.

        Fails closed on every unknown: a failed poll, a bridge that no longer
        lists this phone (unpaired, or a system it can't see), and a record that
        stops advertising the toggle all land here. Showing a position for a
        switch that decides whether the alarm auto-disarms, while not actually
        knowing it, is worse than showing nothing.
        """
        return (
            super().available
            and bool(self._record)
            and bool(self._record.get("supports_disarming_toggle"))
        )

    @property
    def is_on(self) -> bool | None:
        """True while this phone may disarm the panel; None if it isn't known."""
        # `is not None`, not truthiness: False is a real answer ("this phone
        # cannot disarm") and must not read as "unknown" the way a bridge too old
        # to send the field does.
        value = self._record.get("disarming_enabled")
        return bool(value) if value is not None else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The partition this phone is enrolled on, which the panel decides.

        Read-only here on purpose (backlog E43): the bridge reports it and does
        not write it, and re-enrolling a phone on a different partition is not
        something to do as a side effect of a switch.
        """
        partition_id = self._record.get("enrolled_partition_id")
        return {"enrolled_partition_id": partition_id} if partition_id is not None else {}

    async def _async_set(self, enabled: bool) -> None:
        """Write the flag and adopt the record that comes back.

        Only ever reached from a user toggling this entity.
        """
        # Deliberately the opaque device id, never the description: at INFO this
        # would otherwise put a household member's name in the log of every
        # install. The description is debug-only (see arming_devices_coordinator).
        LOGGER.info(
            "Setting Bluetooth disarming %s for arming device %s",
            "on" if enabled else "off", self._device_id,
        )
        record = await async_set_disarming_enabled(self.hass, self._device_id, enabled)
        # The route answers with the record as the server has it after the write,
        # so adopting it beats a poll -- which would cost live Alarm.com round
        # trips to learn what is already in hand, on a coordinator that
        # deliberately polls every 15 minutes. An unreadable or mismatched reply
        # falls through to a real re-read rather than to an assumption that the
        # requested value took.
        if record.get("id") == self._device_id:
            self.coordinator.async_apply_device(self._device_id, record)
        else:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Let this phone disarm the panel when it arrives."""
        await self._async_set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Stop this phone disarming the panel when it arrives."""
        await self._async_set(False)


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
