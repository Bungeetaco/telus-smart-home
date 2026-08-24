"""telus_smarthome.bypass_sensors -- manually bypass (or un-bypass) individual
alarm sensors on the security panel.

**A bypassed zone does not trigger the alarm until it is un-bypassed.** That
makes this the most consequential call in the integration -- more than the
siren, which is only loud -- so every branch below prefers refusing to guessing:
an entity that isn't ours, a unique id that doesn't resolve to a real Alarm.com
sensor, sensors spread across two partitions, or a partition this account may
not command all raise a HomeAssistantError instead of sending an approximation
to a live panel.

Deliberately a MANUAL service, and deliberately NOT wired into the arm path.
The integration already has `CONF_FORCE_BYPASS`, a per-arm-call flag meaning
"bypass whatever is open while arming"; that is a different mechanism (a boolean
handed to the arm command itself) and `alarm_control_panel.py` is untouched by
this file. The hazard `docs/web-portal-api-findings.md` warned about is two
*automatic* bypass paths racing over one partition, each with its own idea of
which zones are skipped. A manual action alongside an automatic flag is not that
hazard: nothing here ever fires by itself, so the two can only be combined by a
person who asked for both, in that order, on purpose. Folding this into
`CONF_FORCE_BYPASS` was never an option anyway -- upstream this is
`POST /web/api/devices/partitions/{id}/bypassSensors`, a standalone partition
action applied immediately, not an arm-time option (see the E25 entry in
`docs/backlog.md`).

The bridge's `POST /control/partitions/{id}/bypass` does the write. It pre-reads
the partition and enforces the panel's own `canBypassSensorWhenArmed` /
`hasPermissionToChangeState` gates, answering 422 when they refuse; this module
re-checks the same two gates from data it already holds purely so the message
the user sees names WHICH gate said no.

Registered and removed alongside the speak services in `__init__.py`.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.event import async_call_later
from pyalarmdotcomajax.devices.partition import Partition as libPartition

from .bridge_client import bridge_get, read_control_secret
from .const import (
    CAMERA_BRIDGE_PARTITION_BYPASS_URL_TEMPLATE,
    CAMERA_BRIDGE_PARTITIONS_URL,
    DATA_CONTROLLER,
    DOMAIN,
    SERVICE_BYPASS_SENSORS,
)

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids an import cycle at runtime
    from .controller import AlarmIntegrationController

# How long to wait before re-reading the sensors after a bypass. Measured, not
# guessed: against the live panel a bypass was still invisible on the sensor at
# 5s after the POST returned, and visible at 10s. 15 leaves margin without making
# a successful call feel unacknowledged.
_BYPASS_SETTLE_SECONDS = 15

LOGGER = logging.getLogger(__name__)

# Both lists are optional individually; the handler enforces "at least one, and
# non-empty" so the refusal is a readable HomeAssistantError toast rather than a
# voluptuous schema dump. Defaults are [] so the handler never has to distinguish
# "absent" from "empty" -- to this service they mean the same thing.
_BYPASS_SCHEMA = vol.Schema(
    {
        vol.Optional("bypass", default=list): cv.entity_ids,
        vol.Optional("unbypass", default=list): cv.entity_ids,
    }
)

# One call is TWO sequential live Alarm.com round trips on the bridge side (it
# pre-reads the partition, then writes), against an upstream that has been
# measured at 25s for a single read on this account -- see
# CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS in const.py. Generous on purpose: giving up
# on a write to an alarm panel that is actually succeeding leaves the caller with
# no idea whether their zones are bypassed, which is the one outcome to avoid.
_POST_TIMEOUT_S = 30

# The partitions pre-read is a single GET and is not on the write path.
_GET_TIMEOUT_S = 15


def async_register_bypass_service(hass: HomeAssistant) -> None:
    """Register telus_smarthome.bypass_sensors once.

    Idempotent (guarded by has_service), matching async_register_speak_service --
    reloads land in __init__.py again and must not re-register."""
    if hass.services.has_service(DOMAIN, SERVICE_BYPASS_SENSORS):
        return

    async def _handle_bypass(call: ServiceCall) -> None:
        data = _BYPASS_SCHEMA(dict(call.data))
        bypass_entities = _dedupe(data.get("bypass") or [])
        unbypass_entities = _dedupe(data.get("unbypass") or [])

        if not bypass_entities and not unbypass_entities:
            raise HomeAssistantError(
                "bypass_sensors requires at least one entity in 'bypass' or 'unbypass'")

        # Contradictory input, not a merge to be resolved by ordering: the
        # upstream call carries both lists in one body, so "bypass and unbypass
        # the same zone" has no defined outcome on the panel. Refuse.
        if overlap := set(bypass_entities) & set(unbypass_entities):
            raise HomeAssistantError(
                "bypass_sensors: cannot bypass and un-bypass the same sensor in one "
                f"call ({', '.join(sorted(overlap))})")

        registry = er.async_get(hass)
        controller = _resolve_controller(hass, registry, bypass_entities + unbypass_entities)
        sensors = _sensor_index(controller)
        bypass_ids = [
            _resolve_sensor(registry, sensors, entity_id) for entity_id in bypass_entities
        ]
        unbypass_ids = [
            _resolve_sensor(registry, sensors, entity_id) for entity_id in unbypass_entities
        ]

        partition_id = _resolve_partition(controller, sensors, bypass_ids + unbypass_ids)
        partition = await _async_read_partition(hass, partition_id)
        _check_gates(controller, partition, partition_id)

        # Logged BEFORE the write as well as after it. If the POST times out
        # against a bridge that is in fact succeeding, this line is the only
        # record that anything was ever asked for -- and "which zones did I
        # bypass?" with no answer in the log is the failure mode that matters
        # here. The pair also brackets the write, so an intent line with no
        # outcome line beside it is itself the signal that a call went missing.
        LOGGER.info(
            "bypass_sensors: partition %s -- requesting bypass of [%s], un-bypass of [%s]",
            partition_id,
            _describe(bypass_entities, bypass_ids),
            _describe(unbypass_entities, unbypass_ids),
        )

        result = await _async_post_bypass(hass, partition_id, bypass_ids, unbypass_ids)

        LOGGER.info(
            "bypass_sensors: partition %s answered %s -- bypassed [%s], un-bypassed [%s]. "
            "A bypassed sensor will not trigger the alarm until it is un-bypassed.",
            partition_id,
            result.get("status", "ok"),
            _describe(bypass_entities, bypass_ids),
            _describe(unbypass_entities, unbypass_ids),
        )

        # The bridge's reply does NOT say which sensors ended up bypassed (only
        # the refreshed partition), and no websocket push or entity write covers
        # this either -- so without an explicit refresh the sensors keep showing
        # their pre-call state until the next 5-minute poll.
        #
        # DELAYED, not immediate. Measured against the live panel: a bypass takes
        # about ten seconds to become visible on the sensor
        # (`isBypassed` was still false at 5s and true at 10s). Refreshing the
        # instant the POST returns therefore reads the OLD state and paints the
        # sensors as unchanged, which reads as "the service did nothing" for a
        # call that in fact worked. One delayed refresh tells the truth; an
        # immediate one reliably lies.
        #
        # Requested rather than forced, so several calls in an automation
        # coalesce instead of each costing an Alarm.com round trip, and scheduled
        # through the event loop so the service call itself returns promptly.
        async def _refresh_when_the_panel_has_caught_up(_now=None) -> None:
            await controller.update_coordinator.async_request_refresh()

        async_call_later(hass, _BYPASS_SETTLE_SECONDS,
                         _refresh_when_the_panel_has_caught_up)

    hass.services.async_register(
        DOMAIN, SERVICE_BYPASS_SENSORS, _handle_bypass, schema=_BYPASS_SCHEMA
    )
    LOGGER.debug("Registered %s.%s service", DOMAIN, SERVICE_BYPASS_SENSORS)


def async_remove_bypass_service(hass: HomeAssistant) -> None:
    """Remove the service (called when the last config entry unloads)."""
    if hass.services.has_service(DOMAIN, SERVICE_BYPASS_SENSORS):
        hass.services.async_remove(DOMAIN, SERVICE_BYPASS_SENSORS)


def _dedupe(entity_ids: list[str]) -> list[str]:
    """Drop repeats while keeping the caller's order (for readable log lines).

    A repeated entity is a UI/template accident, not an instruction to send the
    same device id to the panel twice."""
    seen: dict[str, None] = {}
    for entity_id in entity_ids:
        seen.setdefault(entity_id, None)
    return list(seen)


def _resolve_controller(
    hass: HomeAssistant, registry: er.EntityRegistry, entity_ids: list[str]
) -> AlarmIntegrationController:
    """Find the one loaded config entry every named entity belongs to.

    Every entity must be one of THIS integration's alarm sensors and all of them
    must come from the same config entry: two entries are two Alarm.com accounts,
    and one bypass call names one partition on one panel. Same entity-registry
    approach speak_service.py uses, with the extra account check that service
    doesn't need.
    """
    entry_ids: set[str] = set()

    for entity_id in entity_ids:
        entity = registry.async_get(entity_id)
        if entity is None:
            raise HomeAssistantError(f"bypass_sensors: {entity_id} is not a known entity")
        if entity.platform != DOMAIN:
            raise HomeAssistantError(
                f"bypass_sensors: {entity_id} is not a TELUS Smart Home entity")
        # Alarm sensors are binary_sensor entities here (see binary_sensor.py).
        # Anything else in this integration -- a lock, a camera switch, the
        # activity feed sensor -- is not a bypassable zone, and its unique id is
        # not a sensor device id.
        if entity.domain != "binary_sensor":
            raise HomeAssistantError(
                f"bypass_sensors: {entity_id} is not a TELUS alarm sensor")
        if entity.config_entry_id is None:
            raise HomeAssistantError(
                f"bypass_sensors: {entity_id} is not attached to a loaded config entry")
        entry_ids.add(entity.config_entry_id)

    if len(entry_ids) > 1:
        raise HomeAssistantError(
            "bypass_sensors: the named sensors belong to different TELUS accounts; "
            "call the service once per account")

    store = hass.data.get(DOMAIN, {}).get(entry_ids.pop())
    # store is None while an entry is unloaded (its entities linger in the
    # registry). Nothing to command in that state.
    controller = (store or {}).get(DATA_CONTROLLER)
    if controller is None:
        raise HomeAssistantError(
            "bypass_sensors: the TELUS Smart Home entry for those sensors is not loaded")
    return controller


def _sensor_index(controller: AlarmIntegrationController) -> dict[str, Any]:
    """Map Alarm.com device id -> library sensor object, for real sensors only.

    Deliberately the same two registries binary_sensor.py builds its "real"
    sensor entities from, so an entity that exists can always be resolved here
    and one that can't (an attribute entity, whose unique id is
    `<device id>_battery`) is rejected rather than having a mangled id posted to
    a panel. Iterates .values() and keys on device.id_ rather than assuming the
    library's registries are plain mappings -- .values() is the only access the
    rest of this integration relies on.
    """
    return {
        device.id_: device
        for device in (
            *controller.api.devices.sensors.values(),
            *controller.api.devices.water_sensors.values(),
        )
    }


def _resolve_sensor(
    registry: er.EntityRegistry, sensors: dict[str, Any], entity_id: str
) -> str:
    """Turn one entity_id into the Alarm.com device id the bridge expects.

    Each entity's unique_id IS the Alarm.com device id (HardwareBaseDevice sets
    `_attr_unique_id = device.id_` -- see base_device.py), so the entity registry
    is an exact lookup table and nothing has to be parsed or guessed. An id that
    the library doesn't currently know as a sensor is an error: sending it anyway
    would be a guess aimed at a live alarm panel.
    """
    entity = registry.async_get(entity_id)
    unique_id = entity.unique_id if entity else None
    if not unique_id or unique_id not in sensors:
        raise HomeAssistantError(
            f"bypass_sensors: {entity_id} does not resolve to an Alarm.com sensor "
            "that can be bypassed")
    return unique_id


def _resolve_partition(
    controller: AlarmIntegrationController, sensors: dict[str, Any], device_ids: list[str]
) -> str:
    """Decide which partition to post to, or refuse.

    The upstream call is per partition, so this must land on exactly one. Sensors
    carry `partition_id` -- the same value base_device.py uses for its
    `via_device` link, i.e. the partition DEVICE's Alarm.com id, which is also
    the alarm panel entity's unique id, so no registry lookup is needed to get
    it. It is not guaranteed to be populated, though: base_device.py already
    guards against it being falsy.

    So: honour it when the named sensors agree; refuse when they disagree; and
    when it is missing, fall back to the account's single partition -- but only
    if there genuinely is exactly one, which is the common case (one panel, one
    partition). With several partitions and an unattributable sensor there is no
    defensible choice left, so the service refuses rather than picking one.
    """
    known_partitions = {device.id_ for device in controller.api.devices.partitions.values()}

    partition_ids: set[str] = set()
    unattributed: list[str] = []
    for device_id in device_ids:
        partition_id = getattr(sensors[device_id], "partition_id", None)
        if partition_id:
            partition_ids.add(str(partition_id))
        else:
            unattributed.append(device_id)

    if len(partition_ids) > 1:
        raise HomeAssistantError(
            "bypass_sensors: the named sensors are on different partitions "
            f"({', '.join(sorted(partition_ids))}); call the service once per partition")

    if partition_ids and not unattributed:
        partition_id = partition_ids.pop()
    elif len(known_partitions) == 1:
        # Either nothing reported a partition, or some sensors did and some
        # didn't -- harmless when the account only HAS one partition, since
        # that is the only place those sensors can be.
        partition_id = next(iter(known_partitions))
    else:
        raise HomeAssistantError(
            "bypass_sensors: could not determine which partition these sensors belong "
            "to, and this account has more than one; refusing to guess")

    if known_partitions and partition_id not in known_partitions:
        raise HomeAssistantError(
            f"bypass_sensors: partition {partition_id} is not one of this account's "
            "partitions")
    return partition_id


async def _async_read_partition(hass: HomeAssistant, partition_id: str) -> dict[str, Any]:
    """Read the bridge's view of this partition before writing to it.

    Only used to word the refusal accurately: the bridge does its own pre-read
    and is the real gate. A partition the bridge doesn't list is refused here so
    the user sees "the bridge doesn't know that partition" instead of a bare 404
    from the write path.
    """
    try:
        body = await bridge_get(hass, CAMERA_BRIDGE_PARTITIONS_URL, timeout=_GET_TIMEOUT_S)
    except aiohttp.ClientResponseError as err:
        if err.status == 404:
            # The bridge image is deployed and rebuilt separately from this
            # integration, so a bridge older than this route is a normal
            # mid-upgrade state, not a bug -- say which side to update.
            raise HomeAssistantError(
                "bypass_sensors: this camera bridge has no /control/partitions route; "
                "rebuild the bridge container") from err
        raise HomeAssistantError(
            f"bypass_sensors: the camera bridge returned HTTP {err.status} reading "
            "partitions") from err
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(
            f"bypass_sensors: could not reach the camera bridge: {err}") from err

    if body is None:
        # bridge_get returns None only when the shared secret isn't on disk yet.
        raise HomeAssistantError(
            "bypass_sensors: the TELUS camera bridge is not reachable "
            "(no control secret on the shared mount)")

    for partition in body.get("partitions") or []:
        if partition.get("id") == partition_id:
            return partition

    raise HomeAssistantError(
        f"bypass_sensors: the camera bridge does not know partition {partition_id}")


def _check_gates(
    controller: AlarmIntegrationController, partition: dict[str, Any], partition_id: str
) -> None:
    """Refuse locally when the panel's own flags say this cannot work.

    Returns None. Not a security control -- the bridge re-checks both flags
    against a fresh read and is what actually protects the panel. This exists so
    the user is told WHICH gate refused, because the bridge collapses both into
    one 422.

    The armed test reads the partition's state from the loaded library object and
    compares it against the library's own DeviceState enum rather than
    re-deriving "armed" from the integer the bridge reports -- that integer's
    meaning is not documented anywhere in this project, and mis-decoding it would
    turn into a wrong refusal (or a wrong reassurance) about a live alarm.
    """
    if partition.get("has_permission_to_change_state") is False:
        raise HomeAssistantError(
            "bypass_sensors: this Alarm.com account does not have permission to change "
            f"partition {partition_id}; grant it in the TELUS/Alarm.com portal")

    if partition.get("can_bypass_when_armed") is not False:
        return

    for device in controller.api.devices.partitions.values():
        if device.id_ != partition_id:
            continue
        state = getattr(device, "state", None)
        if state is not None and state != libPartition.DeviceState.DISARMED:
            raise HomeAssistantError(
                "bypass_sensors: this panel does not allow bypassing sensors while the "
                f"partition is armed ({device.name}); disarm it first")
        break


async def _async_post_bypass(
    hass: HomeAssistant, partition_id: str, bypass_ids: list[str], unbypass_ids: list[str]
) -> dict[str, Any]:
    """POST the bypass/unbypass lists and return the bridge's parsed reply.

    Deliberately its own POST rather than camera_settings_coordinator's
    async_bridge_post, for the same reason speak_service.py has `_post_talk`:
    that helper is written for the camera control routes and hard-codes their
    error vocabulary -- its 422 is "This camera does not support that control",
    which is simply false here, and it has no notion of the 429/Retry-After,
    400 and 502 answers this route defines. Teaching it a per-route message
    table would put four alarm-panel-specific branches inside a helper whose
    every other caller is a camera entity, for one caller's benefit. Keeping the
    write local costs a dozen lines and leaves that helper's existing callers
    exactly as they were.
    """
    secret = await hass.async_add_executor_job(read_control_secret)
    if secret is None:
        raise HomeAssistantError(
            "bypass_sensors: the TELUS camera bridge is not reachable "
            "(no control secret on the shared mount)")

    url = CAMERA_BRIDGE_PARTITION_BYPASS_URL_TEMPLATE.format(partition_id=partition_id)
    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json={"bypass": bypass_ids, "unbypass": unbypass_ids},
            headers={"Authorization": f"Bearer {secret}"},
            timeout=aiohttp.ClientTimeout(total=_POST_TIMEOUT_S),
        ) as resp:
            if resp.status == 422:
                # The bridge's own pre-read found a gate closed. _check_gates
                # already caught the two cases it can see, so reaching here means
                # the panel changed under us (armed between the two reads) or
                # reported something we don't model.
                raise HomeAssistantError(
                    f"bypass_sensors: the panel refused this change on partition "
                    f"{partition_id} (it may have been armed since the last read, or "
                    "this account may not command it)")
            if resp.status == 429:
                # Alarm.com's limits are undocumented and a stampede can lock the
                # account (AGENTS.md), so surface the wait instead of retrying.
                retry_after = resp.headers.get("Retry-After")
                raise HomeAssistantError(
                    "bypass_sensors: Alarm.com is rate-limiting this account; try again"
                    + (f" in {retry_after}s" if retry_after else " later"))
            if resp.status == 404:
                raise HomeAssistantError(
                    f"bypass_sensors: the camera bridge does not know partition "
                    f"{partition_id}")
            if resp.status == 400:
                raise HomeAssistantError(
                    "bypass_sensors: the camera bridge rejected the request as malformed")
            if resp.status == 502:
                raise HomeAssistantError(
                    "bypass_sensors: Alarm.com rejected or failed the bypass; nothing "
                    "was changed")
            resp.raise_for_status()
            try:
                return await resp.json()
            except (aiohttp.ClientError, ValueError):
                # 2xx with a body we can't parse: the write LANDED. Reporting a
                # failure here would invite the caller to fire a second write at
                # a live panel, so degrade to a warning and let the coordinator
                # refresh show what actually happened.
                LOGGER.warning(
                    "bypass_sensors: partition %s applied the change but answered an "
                    "unreadable body", partition_id)
                return {}
    except HomeAssistantError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(
            f"bypass_sensors: the request to the camera bridge failed: {err}. The change "
            "may or may not have been applied -- check the sensors before retrying"
        ) from err


def _describe(entity_ids: list[str], device_ids: list[str]) -> str:
    """Render "entity_id (device id)" pairs for the audit log lines."""
    if not entity_ids:
        return "none"
    return ", ".join(
        f"{entity_id} ({device_id})" for entity_id, device_id in zip(entity_ids, device_ids)
    )
