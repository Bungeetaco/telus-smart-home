"""Selective sensor bypass on an alarm partition, over the WEBSITE API.

Backs backlog item E25. **A bypassed sensor does not trigger the alarm until it
is un-bypassed.** That single sentence is why this module refuses more than it
accepts: every gate here fails closed, and nothing is written that the partition
itself did not say was allowed.

What this is NOT: it is not the integration's `force_bypass` arming option, and
it must never become a second automatic version of it. `force_bypass` is a
boolean passed to an arm call meaning "bypass whatever is open as you arm".
This endpoint is a standalone action taken at a moment of someone's choosing:

    POST web/api/devices/partitions/{id}/bypassSensors
    {"bypass": "<id>|<id>", "unbypass": ""}

Pipe-separated device ids, applied immediately, unrelated to any arm call -- the
same thing the vendor's own bypass screen does. The two mechanisms coexist
because only one of them is automatic. Two automatic ones racing over a single
partition is the hazard the backlog entry warned about, and it is still avoided.

The gates come from the partition object itself, read before every write:

  * ``hasPermissionToChangeState`` -- whether this account may touch the
    partition at all.
  * ``canBypassSensorWhenArmed`` -- FALSE on the reference panel, meaning a
    sensor cannot be bypassed while the partition is armed. Honoured rather than
    attempted: the failure mode of guessing here is a zone that silently stops
    protecting an armed house.

The response does not report which sensors ended up bypassed, so neither does
this module. It reports what the partition says about itself afterwards and
leaves the sensor states to whatever polls them.
"""
import logging

import aiohttp

from . import config as c
from .telus_auth import RateLimited, TelusSession

log = logging.getLogger(__name__)

_PARTITION_TYPE = "devices/partition"
_LIST_URL = "{base}web/api/devices/partitions"
_ONE_URL = "{base}web/api/devices/partitions/{pid}"
_BYPASS_URL = "{base}web/api/devices/partitions/{pid}/bypassSensors"

# alarm.com's partition state enum. Only "is it disarmed" is needed here, and
# only 1 has been observed live (on a panel reporting itself disarmed), so the
# test is written the safe way round: anything that is not definitely disarmed is
# treated as armed, because that is the direction whose mistake is harmless.
_STATE_DISARMED = 1


class PartitionError(RuntimeError):
    """A partition read/bypass could not be performed.

    Same contract as camera_settings.CameraSettingsError and
    arming_devices.ArmingDeviceError, because control_server maps all three
    through one `_settings_error` helper.

    ``unsupported`` is True only when the PARTITION says the operation cannot be
    done -- no permission, or armed on a panel that forbids bypassing while
    armed. It is False for a caller mistake (no sensors given, a malformed id)
    and for an upstream failure.
    """

    def __init__(self, message: str, *, unsupported: bool = False,
                 field: str | None = None):
        self.unsupported = unsupported
        self.field = field
        super().__init__(message)


def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Turn a non-2xx response into the right exception type.

    Same policy as the sibling modules: 429 becomes telus_auth.RateLimited
    carrying the server's own Retry-After, and 401/403 are re-raised as the raw
    aiohttp.ClientResponseError because control_server._fetch_with_relogin keys
    its one-shot re-login off that exact type and status.
    """
    if resp.status == 429:
        raise RateLimited(c.parse_retry_after(resp.headers.get("Retry-After")))
    if resp.status in (401, 403):
        resp.raise_for_status()
    if resp.status >= 400:
        # Path only, never the full URL: query strings on this API can carry
        # session-scoped material and this string reaches logs and callers.
        raise PartitionError(f"TELUS returned HTTP {resp.status} for {resp.url.path}")


async def _get(telus: TelusSession, url: str) -> dict:
    async with telus.session.get(url, headers=telus.api_headers) as resp:
        _raise_for_status(resp)
        return await resp.json(content_type=None)


def _parse(item: dict) -> dict:
    """One partition record, reduced to what a caller needs to decide anything.

    `raw` carries the rest -- this object has ~50 attributes and most of them are
    generic managed-device plumbing.
    """
    attrs = item.get("attributes") or {}
    state = attrs.get("state")
    return {
        "id": item.get("id"),
        "partition_id": attrs.get("partitionId"),
        "description": attrs.get("description"),
        "state": state,
        "is_disarmed": state == _STATE_DISARMED,
        # The two gates. Both default to the REFUSING value when the partition
        # does not report them: an unknown permission is not permission, and an
        # unknown "may I bypass while armed" is not a yes.
        "has_permission_to_change_state": bool(
            attrs.get("hasPermissionToChangeState")),
        "can_bypass_when_armed": bool(attrs.get("canBypassSensorWhenArmed")),
        # Reported because it explains an empty-handed bypass rather than gating
        # one: a caller naming specific sensors does not need anything to be open.
        "has_open_bypassable_sensors": bool(attrs.get("hasOpenBypassableSensors")),
        "raw": attrs,
    }


async def get_partitions(telus: TelusSession) -> list[dict]:
    """Every partition on the account, in the _parse shape."""
    body = await _get(telus, _LIST_URL.format(base=c.TELUS_BASE))
    data = body.get("data")
    items = data if isinstance(data, list) else [data] if isinstance(data, dict) else []
    return [_parse(item) for item in items
            if isinstance(item, dict) and item.get("type") == _PARTITION_TYPE]


async def get_partition(telus: TelusSession, partition_id: str) -> dict:
    """One partition, in the _parse shape.

    Raises PartitionError if the response is not a partition -- a caller must not
    reach the bypass POST holding something else's id.
    """
    body = await _get(telus, _ONE_URL.format(base=c.TELUS_BASE, pid=partition_id))
    data = body.get("data")
    if not isinstance(data, dict) or data.get("type") != _PARTITION_TYPE:
        raise PartitionError(f"{partition_id} is not a partition")
    return _parse(data)


def _joined(ids) -> str:
    """The pipe-separated form this endpoint takes, or "" for nothing.

    Refuses an id containing the separator itself: a pipe inside one would split
    into two ids server-side and bypass a sensor nobody named. Refuses empty and
    non-string entries for the same class of reason -- everything sent here ends
    up naming a zone on a live alarm system.
    """
    out = []
    for raw in ids or []:
        if not isinstance(raw, str) or not raw.strip():
            raise PartitionError("sensor ids must be non-empty strings",
                                 field="sensors")
        value = raw.strip()
        if "|" in value:
            raise PartitionError(f"sensor id {value!r} contains the '|' separator",
                                 field="sensors")
        out.append(value)
    return "|".join(out)


async def bypass_sensors(telus: TelusSession, partition_id: str, *,
                         bypass=None, unbypass=None) -> dict:
    """Bypass and/or un-bypass named sensors on one partition.

    Returns the partition in the _parse shape, re-read from the response.

    Raises PartitionError with ``unsupported=True`` when the partition says the
    operation is not allowed -- no permission, or armed on a panel whose
    ``canBypassSensorWhenArmed`` is false -- and without it for a caller mistake
    (naming no sensors at all, or an unusable id).

    The partition is read FIRST, every time. This is not a performance question:
    the alternative is discovering that a panel forbade the write by having
    already attempted it.
    """
    bypass_ids = _joined(bypass)
    unbypass_ids = _joined(unbypass)
    if not bypass_ids and not unbypass_ids:
        raise PartitionError("no sensors given to bypass or un-bypass",
                             field="sensors")

    partition = await get_partition(telus, partition_id)
    if not partition["has_permission_to_change_state"]:
        raise PartitionError(
            f"this account may not change partition {partition_id}",
            unsupported=True, field="partition")
    if not partition["is_disarmed"] and not partition["can_bypass_when_armed"]:
        raise PartitionError(
            f"partition {partition_id} is armed and reports it cannot bypass a "
            "sensor while armed", unsupported=True, field="partition")

    # Logged before the call and at info, naming what was asked for: a bypass
    # nobody can find afterwards in the log is the failure mode that matters on a
    # security system.
    log.info("Bypassing sensors on partition %s: bypass=[%s] unbypass=[%s]",
             partition_id, bypass_ids, unbypass_ids)

    url = _BYPASS_URL.format(base=c.TELUS_BASE, pid=partition_id)
    async with telus.session.post(
            url, json={"bypass": bypass_ids, "unbypass": unbypass_ids},
            headers=telus.api_headers) as resp:
        _raise_for_status(resp)
        try:
            body = await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            body = None

    data = (body or {}).get("data")
    if isinstance(data, dict) and data.get("type") == _PARTITION_TYPE:
        return _parse(data)
    # The endpoint echoed the partition in every captured call; falling back to
    # the pre-read keeps the response shape stable if it ever stops, and says
    # nothing about the sensors either way -- which is honest, because this
    # endpoint never reports which ones ended up bypassed.
    return partition
