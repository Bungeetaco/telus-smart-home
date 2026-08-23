"""The `telus_smarthome.unpair_arming_device` service.

Unpairs a phone from the alarm panel's Bluetooth disarming, so it can no longer
disarm the alarm on approach.

**This cannot be undone from Home Assistant.** Re-pairing requires physical
access to both the phone and the panel — walking to the keypad and running the
pairing flow again. There is no cloud-side undo and nothing this integration can
call to reverse it.

That is the entire reason this is a service and not a button. A button sits on a
dashboard next to controls that are all trivially reversible, and gets pressed by
accident; a service call has to be written deliberately. Everything below follows
from that: it takes exactly one device, it refuses anything it is not certain
about, and it names what it removed in the log while the name is still knowable.

The panel-wide `deleteAll` the vendor's portal also offers is deliberately not
exposed at any level — one mis-aimed call would unpair every phone in the
household at once.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import aiohttp
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .bridge_client import read_control_secret
from .const import (
    CAMERA_BRIDGE_ARMING_DEVICE_URL_TEMPLATE,
    DOMAIN,
    SERVICE_UNPAIR_ARMING_DEVICE,
)

LOGGER = logging.getLogger(__name__)

# One entity, never a list. A list invites "unpair these three" written once and
# re-run later against a household that has changed, and there is no undo for the
# ones that should not have gone.
_UNPAIR_SCHEMA = vol.Schema({vol.Required("entity_id"): cv.entity_id})

# The device disappears from the account the moment this succeeds, and the
# bridge re-reads the catalog before deleting, so the call is two upstream round
# trips rather than one.
_TIMEOUT_S = 30


def _resolve_device_id(hass: HomeAssistant, entity_id: str) -> str:
    """The Alarm.com device id behind one of this integration's switches.

    Refuses anything it is not certain about rather than passing a guess to an
    irreversible delete: an entity the registry does not know, one belonging to
    another integration, or one that is not an arming-device switch.

    The arming-device switches carry a unique id of `<device id>_bluetooth_disarming`
    (see arming_devices_coordinator), which is the only entity family whose id
    maps to a device the unpair route will accept.
    """
    registry = er.async_get(hass)
    entry = registry.async_get(entity_id)
    if entry is None:
        raise HomeAssistantError(f"{entity_id} is not a known entity.")
    if entry.platform != DOMAIN:
        raise HomeAssistantError(
            f"{entity_id} does not belong to the TELUS Smart Home integration."
        )

    suffix = "_bluetooth_disarming"
    unique_id = entry.unique_id or ""
    if not unique_id.endswith(suffix):
        raise HomeAssistantError(
            f"{entity_id} is not a paired-phone switch. Pass the "
            '"Disarm alarm on arrival" switch for the phone to unpair.'
        )
    return unique_id[: -len(suffix)]


async def _async_delete(hass: HomeAssistant, device_id: str) -> dict[str, Any]:
    """DELETE the device through the bridge and return its parsed reply.

    Its own request rather than the camera helper's, for the reason
    bypass_service gives: that helper's error vocabulary is camera-worded and has
    no notion of this route's answers.

    A 2xx whose body cannot be parsed is NOT an error here. The device is gone;
    reporting failure would invite the caller to try again against an id that no
    longer exists, and the second call's 404 would be far more confusing than the
    first call's silence.
    """
    secret = await hass.async_add_executor_job(read_control_secret)
    if secret is None:
        raise HomeAssistantError("The TELUS camera bridge is not reachable.")

    session = async_get_clientsession(hass)
    url = CAMERA_BRIDGE_ARMING_DEVICE_URL_TEMPLATE.format(device_id=device_id)
    try:
        async with session.delete(
            url,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=aiohttp.ClientTimeout(total=_TIMEOUT_S),
        ) as resp:
            if resp.status == 404:
                raise HomeAssistantError(
                    "The panel no longer lists that phone as paired."
                )
            if resp.status == 429:
                raise HomeAssistantError(
                    "Alarm.com is rate-limiting this account; nothing was unpaired. "
                    "Try again later."
                )
            resp.raise_for_status()
            try:
                return await resp.json()
            except (aiohttp.ClientError, ValueError):
                LOGGER.warning(
                    "Unpair of %s succeeded but the bridge's reply was unreadable",
                    device_id,
                )
                return {}
    except HomeAssistantError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(
            f"The TELUS camera bridge rejected the unpair request: {err}"
        ) from err


def async_register_unpair_service(hass: HomeAssistant) -> None:
    """Register the service, once, however many config entries load."""
    if hass.services.has_service(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE):
        return

    async def _handle_unpair(call: ServiceCall) -> None:
        data = _UNPAIR_SCHEMA(dict(call.data))
        entity_id = data["entity_id"]
        device_id = _resolve_device_id(hass, entity_id)

        # Logged before the call, because after it the device is gone from the
        # catalog and this is the only record of what was aimed at. The phone's
        # name is not logged -- it is a person's name; the opaque id identifies
        # it well enough for anyone reading back.
        LOGGER.info("Unpairing arming device %s (%s) -- this cannot be undone",
                    entity_id, device_id)
        result = await _async_delete(hass, device_id)
        LOGGER.info("Unpaired arming device %s (%s)", entity_id, device_id)

        # No coordinator refresh is requested. The arming-device poll re-reads
        # the catalog on its own schedule, the entity's `available` already goes
        # false as soon as the record stops being listed, and forcing a read here
        # would spend a live round trip to confirm something the call just did.
        if result.get("unpaired") not in (None, device_id):
            LOGGER.warning(
                "Bridge reported unpairing %s but %s was requested",
                result.get("unpaired"), device_id,
            )

    hass.services.async_register(
        DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE, _handle_unpair, schema=_UNPAIR_SCHEMA
    )
    LOGGER.debug("Registered %s.%s service", DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE)


def async_remove_unpair_service(hass: HomeAssistant) -> None:
    """Remove the service (called when the last config entry unloads)."""
    if hass.services.has_service(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE):
        hass.services.async_remove(DOMAIN, SERVICE_UNPAIR_ARMING_DEVICE)
