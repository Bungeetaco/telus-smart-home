"""The TELUS Smart Home integration (forked from alarmdotcom for TELUS SSO)."""

from __future__ import annotations

from . import telus_auth_patch  # noqa: F401  (applies TELUS SSO monkeypatch on import)
from . import telus_ws_heartbeat_patch  # noqa: F401  (keepalive pings so the push websocket survives idle)
from . import telus_ws_push_patch  # noqa: F401  (websocket pushes refresh HA entities)

import asyncio
import json
import logging
import re
import secrets

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from pyalarmdotcomajax import OtpRequired
from pyalarmdotcomajax.exceptions import (
    AlarmdotcomException,
    AuthenticationFailed,
    ConfigureTwoFactorAuthentication,
)

from .bypass_service import async_register_bypass_service, async_remove_bypass_service
from .unpair_service import async_register_unpair_service, async_remove_unpair_service
from .camera import CameraBridgeCoordinator
from .clip_view import TelusClipRedirectView
from .const import (
    CONF_ARM_AWAY,
    CONF_ARM_HOME,
    CONF_ARM_NIGHT,
    CONF_CLIP_LINK_SECRET,
    CONF_FORCE_BYPASS,
    CONF_NO_ENTRY_DELAY,
    CONF_SILENT_ARM,
    DATA_ACTIVITY_COORDINATOR,
    DATA_CAMERA_COORDINATOR,
    DATA_CONTROLLER,
    DEBUG_REQ_EVENT,
    DOMAIN,
    PLATFORMS,
    SENSOR_SUBTYPE_BLACKLIST,
    STARTUP_MESSAGE,
)
from .controller import AlarmIntegrationController
from .event import ActivityHistoryCoordinator
from .speak_service import async_register_speak_service, async_remove_speak_service

LOGGER = logging.getLogger(__name__)


async def async_setup_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Set up TELUS Smart Home hub from a config entry."""

    LOGGER.info("%s: Initializing Alarmdotcom from config entry.", __name__)

    if DOMAIN not in hass.data:
        # Print startup message
        LOGGER.info(STARTUP_MESSAGE)

    hass.data.setdefault(DOMAIN, {})

    # Per-install secret signing the clip-redirect links (see clip_view.py).
    # Generated once and then persisted in the entry forever -- the
    # signatures live inside already-delivered phone notifications, so a
    # fresh secret would break every clip link older than itself. Done
    # before the coordinators below are created so event.py can always
    # assume the secret exists.
    if CONF_CLIP_LINK_SECRET not in config_entry.data:
        hass.config_entries.async_update_entry(
            config_entry,
            data={**config_entry.data, CONF_CLIP_LINK_SECRET: secrets.token_hex(32)},
        )

    # Views can't be unregistered, so this is once per HA run (not per entry
    # setup -- reloads land here again). The view looks its secrets up from
    # the live config entries on every request, so re-registration would add
    # nothing and aiohttp raises on duplicate routes.
    if not hass.data[DOMAIN].get("clip_view_registered"):
        hass.http.register_view(TelusClipRedirectView(hass))
        hass.data[DOMAIN]["clip_view_registered"] = True

    #
    # Initialize Alarm.com Connection & Data Update Coordinator
    #

    controller = AlarmIntegrationController(hass, config_entry)

    try:
        await controller.initialize()
    except (OtpRequired, AuthenticationFailed) as ex:
        raise ConfigEntryAuthFailed("Authentication failed. Please try logging in again.") from ex
    except ConfigureTwoFactorAuthentication as ex:
        raise ConfigEntryAuthFailed from ex
    except (TimeoutError, AlarmdotcomException, aiohttp.ClientError) as ex:
        raise ConfigEntryNotReady from ex

    # Camera list coordinator: created here (not inside camera.py) and
    # refreshed before platforms are forwarded, so it exists in hass.data
    # for both camera.py and event.py regardless of which one HA's
    # concurrent platform forwarding happens to set up first.
    camera_coordinator = CameraBridgeCoordinator(hass, config_entry)
    # Deliberately async_refresh(), not async_config_entry_first_refresh():
    # the latter raises ConfigEntryNotReady on failure, which would fail
    # the *entire* config entry (alarm panel, lock, every other platform)
    # just because the independently-deployed camera-bridge container
    # happens to be briefly unreachable at HA startup. async_refresh()
    # instead logs and leaves coordinator.data empty; camera/event entities
    # simply appear on a later poll once the bridge responds.
    await camera_coordinator.async_refresh()

    # Same reasoning as camera_coordinator above -- created here (not inside
    # event.py) and shared via hass.data so sensor.py's activity-feed sensor
    # can read the exact same data event.py's TelusActivityEvent already
    # polls, instead of each platform hitting the bridge independently.
    activity_coordinator = ActivityHistoryCoordinator(hass, config_entry)
    await activity_coordinator.async_refresh()

    # Store integration data for use during platform setup.
    hass.data[DOMAIN][config_entry.entry_id] = {
        DATA_CONTROLLER: controller,
        DATA_CAMERA_COORDINATOR: camera_coordinator,
        DATA_ACTIVITY_COORDINATOR: activity_coordinator,
    }

    #
    # Initialize WebSocket listener
    #

    # Tracked via the config entry rather than a bare create_task: the monitor loops
    # forever (it only exits on WebSocketState.STOPPED), so an untracked task
    # survived unload and kept running against a controller whose session had
    # already been closed -- one leaked, permanently-reconnecting task per reload,
    # and every options-flow save, reauth or reconfigure is a reload.
    # async_create_background_task also gives asyncio a strong reference to it,
    # which a bare create_task does not.
    config_entry.async_create_background_task(
        hass,
        controller.async_start_websocket_monitor(),
        name=f"{DOMAIN}_websocket_monitor",
    )

    async def _async_stop_controller(event: Event) -> None:
        """Adapt controller.stop()'s no-arg signature to the bus listener contract,
        which always passes the Event -- a plain lambda wrapper here would silently
        create-and-drop the coroutine without awaiting it since HA's dispatcher only
        awaits listeners that are themselves coroutine functions."""
        await controller.stop()

    config_entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, _async_stop_controller)
    )

    #
    # Delete devices from Home Assistant that are no longer present on Alarm.com.
    #

    device_registry = dr.async_get(hass)

    # Get devices from Alarm.com
    device_ids_via_adc: set[str] = set()
    for device in controller.api.devices.all.values():
        if device.device_subtype not in SENSOR_SUBTYPE_BLACKLIST and device.has_state:
            device_ids_via_adc.add(device.id_)

    # Purge deleted devices from Home Assistant
    for deleted_device in list(device_registry.deleted_devices.values()):
        for identifier in deleted_device.identifiers:
            if identifier[0] == DOMAIN:
                LOGGER.info("Removing orphaned device from Home Assistant: %s", deleted_device.identifiers)
                del device_registry.deleted_devices[deleted_device.id]

    # Will be used during virtual device creation.
    device_ids_via_hass: set[str] = set()

    # Compare against device registry.
    #
    # This pass previously decided removal *inside* the per-identifier loop, which
    # made it wrong in three separate ways:
    #   - Any device whose identifier isn't an Alarm.com "<digits>-<digits>" id was
    #     deleted on EVERY setup. That is every device this fork adds itself: the
    #     bridge-served cameras (keyed by TELUS camera UUID), the Activity feed
    #     device, and the diagnostic "integration" device. They were recreated
    #     immediately afterwards by their platforms, so the symptom wasn't missing
    #     entities but silently discarded user customisations (renames, area
    #     assignments, enabled/disabled state) on every restart.
    #   - A device with several identifiers was judged on whichever one came first
    #     out of an unordered set, so a still-present device could be removed
    #     depending on iteration order.
    #   - Removal ran once per non-matching identifier, and the second call for the
    #     same device raises KeyError, aborting setup entirely.
    # It is now one decision per device, made after inspecting all its identifiers,
    # and it only ever considers devices that look like Alarm.com devices.
    for device_entry in dr.async_entries_for_config_entry(device_registry, config_entry.entry_id):
        keep = False
        has_adc_identifier = False

        for identifier_domain, identifier_id in device_entry.identifiers:
            if identifier_domain != DOMAIN or identifier_id is None:
                keep = True  # not ours to reason about
                break

            # Strip the _debug / _malfunction / etc. suffixes attribute entities add.
            id_matches = re.search(r"([0-9]+-[0-9]+)(?:_[a-zA-Z_]+)*", str(identifier_id))
            if id_matches is None:
                # Not an Alarm.com device id -- one of this fork's own devices. The
                # Alarm.com device list says nothing about whether it's still valid.
                keep = True
                break

            has_adc_identifier = True
            if id_matches.group(1) in device_ids_via_adc:
                device_ids_via_hass.add(identifier_id)
                keep = True
                break

        if keep or not has_adc_identifier:
            continue

        LOGGER.info(
            "Removing device no longer present on Alarm.com: %s (%s | %s)",
            device_entry.name,
            device_entry.identifiers,
            device_entry.id,
        )

        device_registry.async_remove_device(device_entry.id)

    # Create virtual DEVICES.
    # Currently, only Skybell cameras are virtual devices. We support modifying configuration attributes but not viewing video.
    for camera in controller.api.devices.cameras.values():
        # Check if camera already created.
        if camera.id_ in device_ids_via_hass:
            continue

        device_registry.async_get_or_create(
            config_entry_id=config_entry.entry_id,
            connections={(dr.CONNECTION_NETWORK_MAC, str(camera.mac_address))},
            identifiers={(DOMAIN, camera.id_)},
            name=camera.name,
            model="Skybell HD",
            manufacturer="Skybell",
        )

    # Create real devices
    await hass.config_entries.async_forward_entry_setups(config_entry, PLATFORMS)

    # Register the talkdown service (telus_smarthome.speak). Once per HA run and
    # guarded by has_service inside -- reloads land here again but re-registration
    # is a no-op, same spirit as the clip_view "once per HA run" guard above.
    async_register_speak_service(hass)

    # Register the manual selective-sensor-bypass service
    # (telus_smarthome.bypass_sensors). Same once-per-HA-run, has_service-guarded
    # lifecycle as the talkdown services above. It is a MANUAL action only and is
    # deliberately not wired into the arm path -- CONF_FORCE_BYPASS remains the
    # only bypass that happens as a side effect of arming (see bypass_service.py).
    async_register_bypass_service(hass)
    async_register_unpair_service(hass)

    async def handle_telus_smarthome_debug_request_event(event: Event) -> None:
        """Dump debug data when requested via Home Assistant event."""

        event_device = controller.api.devices.get(str(event.data.get("device_id")))

        LOGGER.warning(
            "ALARM.COM DEBUG DATA FOR %s: %s",
            str(event_device.name).upper(),
            json.dumps(event_device.debug_data),
        )

    # Listen for debug entity requests. Wrapped in async_on_unload (same pattern as
    # the EVENT_HOMEASSISTANT_STOP listener above) so a reload doesn't leave a stale
    # duplicate listener registered against a controller whose session may already be
    # closed -- unwrapped, every options-flow save/reauth/reconfigure added another.
    config_entry.async_on_unload(
        hass.bus.async_listen(DEBUG_REQ_EVENT, handle_telus_smarthome_debug_request_event)
    )

    LOGGER.info("%s: Finished initializing Alarmdotcom from config entry.", __name__)

    return True


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate old entry."""

    #
    # To v2
    #

    if config_entry.version == 1:
        LOGGER.debug("Migrating from version %s", config_entry.version)

        v2_options = {**config_entry.options}

        v2_options["use_arm_code"] = bool(config_entry.options.get("arm_code"))

        v2_options["arm_code"] = str(arm_code) if (arm_code := config_entry.options.get("arm_code")) else ""

        config_entry.version = 2

        hass.config_entries.async_update_entry(config_entry, data={**config_entry.data}, options=v2_options)

        LOGGER.info("Migration to version %s successful", config_entry.version)

    #
    # To v3
    #

    if config_entry.version == 2:
        LOGGER.debug("Migrating from version %s", config_entry.version)

        v3_options = {**config_entry.options}

        if not v3_options.get("use_arm_code"):
            v3_options["arm_code"] = None

        # Populate Arm Home
        new_arm_home = []

        if v3_options.get("force_bypass") in ["Stay Only", "Always"]:
            new_arm_home.append("bypass")
        if v3_options.get("silent_arming") in ["Stay Only", "Always"]:
            new_arm_home.append("silent")
        if v3_options.get("no_entry_delay") not in ["Stay Only", "Always"]:
            new_arm_home.append("delay")

        v3_options[CONF_ARM_HOME] = new_arm_home

        # Populate Arm Away
        new_arm_away = []

        if v3_options.get("force_bypass") in ["Away Only", "Always"]:
            new_arm_away.append("bypass")
        if v3_options.get("silent_arming") in ["Away Only", "Always"]:
            new_arm_away.append("silent")
        if v3_options.get("no_entry_delay") not in ["Away Only", "Always"]:
            new_arm_away.append("delay")

        v3_options[CONF_ARM_AWAY] = new_arm_away

        # Populate Arm Night
        new_arm_night = []

        if v3_options.get("force_bypass") == "Always":
            new_arm_night.append("bypass")
        if v3_options.get("silent_arming") == "Always":
            new_arm_night.append("silent")
        if v3_options.get("no_entry_delay") != "Always":
            new_arm_night.append("delay")

        v3_options[CONF_ARM_NIGHT] = new_arm_night

        config_entry.version = 3

        # Purge deprecated config options.

        if v3_options.get("use_arm_code"):
            v3_options["use_arm_code"] = None
        if v3_options.get("force_bypass"):
            v3_options["force_bypass"] = None
        if v3_options.get("silent_arming"):
            v3_options["silent_arming"] = None
        if v3_options.get("no_entry_delay"):
            v3_options["no_entry_delay"] = None

        hass.config_entries.async_update_entry(config_entry, data={**config_entry.data}, options=v3_options)

        LOGGER.info("Migration to version %s successful", config_entry.version)

    #
    # To v4
    #

    if config_entry.version == 3:
        LOGGER.debug("Migrating from version %s", config_entry.version)

        v4_options: dict = {**config_entry.options}

        # Purge config options deprecated and set to None in v3 migration.
        v4_options.pop("use_arm_code", None)
        v4_options.pop("force_bypass", None)
        v4_options.pop("silent_arming", None)
        v4_options.pop("no_entry_delay", None)

        # Make config option names more explicit. This allows for future rollout of selective bypass when arming.
        for arm_mode in (CONF_ARM_HOME, CONF_ARM_AWAY, CONF_ARM_NIGHT):
            if arm_mode in v4_options:
                if "bypass" in v4_options[arm_mode]:
                    v4_options[arm_mode].remove("bypass")
                    v4_options[arm_mode].append(CONF_FORCE_BYPASS)
                if "silent" in v4_options[arm_mode]:
                    v4_options[arm_mode].remove("silent")
                    v4_options[arm_mode].append(CONF_SILENT_ARM)
                if "delay" in v4_options[arm_mode]:
                    v4_options[arm_mode].remove("delay")
                    v4_options[arm_mode].append(CONF_NO_ENTRY_DELAY)

        config_entry.version = 4

        hass.config_entries.async_update_entry(config_entry, data={**config_entry.data}, options=v4_options)

        LOGGER.info("Migration to version %s successful", config_entry.version)

    return True


async def async_unload_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Unload a config entry."""

    controller: AlarmIntegrationController = hass.data[DOMAIN].pop(config_entry.entry_id)[DATA_CONTROLLER]

    # controller.stop() also closes the aiohttp ClientSession (close_websession()) --
    # calling only stop_websocket()/stop_keep_alive() here previously skipped that,
    # leaking one session (and its connector/sockets) per reload (every options-flow
    # save, reauth, or reconfigure).
    await controller.stop()

    unload_ok: bool = await hass.config_entries.async_unload_platforms(config_entry, PLATFORMS)
    if unload_ok:
        LOGGER.debug("%s: Unloaded Alarm.com config entry.", __name__)

    # Remove the shared speak and bypass services once no config entries remain.
    # "clip_view_registered" is a per-HA-run sentinel, not an entry, so exclude it
    # when deciding whether this was the last entry.
    remaining_entries = [key for key in hass.data.get(DOMAIN, {}) if key != "clip_view_registered"]
    if not remaining_entries:
        async_remove_speak_service(hass)
        async_remove_bypass_service(hass)
        async_remove_unpair_service(hass)

    return unload_ok
