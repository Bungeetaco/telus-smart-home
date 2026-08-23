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


class _CameraChimeToggle(CameraSettingsEntity, SwitchEntity):
    """Shared half of the doorbell's two chime on/off toggles.

    Both read one boolean out of the camera's audio settings section, are gated
    on their own support flag, and write one field of the same chime route, so
    the subclasses below carry nothing but those three names.
    """

    _attr_device_class = SwitchDeviceClass.SWITCH
    # Configuration, not a primary control -- the same tier as the speaker
    # volume in number.py: how the doorbell behaves, not something a dashboard
    # should lead with.
    _attr_entity_category = EntityCategory.CONFIG

    # Set by each subclass: the capability flag that decides whether the control
    # exists at all, the field the current state is read from, and the field the
    # write route expects it back under (the three are deliberately not the same
    # name -- the bridge's read and write vocabularies differ).
    _support_key: str
    _state_key: str
    _write_key: str

    @property
    def _audio(self) -> dict[str, Any]:
        """The camera's audio settings section."""
        return self._settings.get("audio") or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting this toggle as settable."""
        return super().available and bool(self._audio.get(self._support_key))

    @property
    def is_on(self) -> bool | None:
        """True while the chime is enabled; None if the camera hasn't said."""
        # `is not None`, not truthiness: False is a real answer ("chime off") and
        # must not read as "unknown" the way a missing field does.
        value = self._audio.get(self._state_key)
        return bool(value) if value is not None else None

    async def _async_set_chime(self, enabled: bool) -> None:
        """Write this one field and adopt the audio section that comes back.

        Only ever reached from a user toggling the entity: this makes (or stops
        making) a real chime sound in someone's home.
        """
        audio = await async_bridge_post(
            self.hass,
            CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE.format(camera_id=self._camera_id),
            {self._write_key: enabled},
        )
        # The route answers with the whole audio section re-read from the camera
        # after the write, so adopting it beats an immediate poll -- which would
        # cost two live TELUS round trips to learn what is already in hand. It
        # also matters here beyond this one field: the write is a
        # read-modify-write of the camera's whole chime config, and the chime
        # type the select entity shows rides in the same section.
        if audio:
            self.coordinator.async_apply_settings_section(self._camera_id, "audio", audio)
        else:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable the chime."""
        await self._async_set_chime(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Silence the chime."""
        await self._async_set_chime(False)


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

        audio = self._audio
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
