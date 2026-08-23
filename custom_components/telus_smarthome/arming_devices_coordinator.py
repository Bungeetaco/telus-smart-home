"""Pollers and write paths for the panel's Bluetooth disarming setup.

Three settings, all of them the same class of thing -- who may walk up to a house
and have the alarm turn itself off:

* per phone, `disarming_enabled` (backlog E24) -- may THIS phone disarm. The
  switch in switch.py.
* per phone, `enrolled_partition_id` (backlog E43) -- WHICH partition it
  disarms. The select in select.py.
* per system, the panel's own Bluetooth pairing flag and timeout (backlog E41).
  The switch in switch.py and the number in number.py, on a SECOND coordinator
  reading a different bridge route.

An "arming device" is a phone paired to the alarm panel over Bluetooth, and
`isDisarmingEnabledForDevice` decides whether that phone walking up to the house
DISARMS THE ALARM. That makes these the most consequential entities in this
integration after the siren buttons and the bypass service, and the rules those
two follow apply here too:

* Every write is only ever reached from an explicit user action on an entity.
  Nothing in this module writes from a poll, a state read, or a recovery path.
* Each control is gated on the record's own `supports_*` flag, never on the value
  it would show -- a record that cannot be written reports the same value a
  writable one does, so reading the value as permission would offer a control
  that silently does nothing (see arming_devices._supports_toggle and
  _supports_enrolled_partition in the bridge).
* Everything unknown fails closed. A device the bridge stops reporting, a bridge
  too old for a route, and a failed poll all leave the entity UNAVAILABLE rather
  than showing a state, and a missing value reads as unknown rather than as
  "off".

Two things about the panel-level settings must never be told to a user, because
neither is established: that turning system-wide pairing off stops an
already-paired phone from disarming (the portal's per-device page does not
consult that document, but what the panel firmware does with the flag is
UNRESOLVED). Its `timeout` is a disarm window in minutes, from the fixed set
1/5/10/20/30 -- neither the unit nor the set appears in the upstream document, so
the bridge publishes both as `timeout_unit` and `timeout_options` and the select
entity in select.py renders them rather than inventing a range.
The bridge's write shape for that endpoint is INFERRED rather than captured, so
a failure there is at least as likely to be the reconstruction as the panel.

Structurally this follows camera_settings_coordinator.py, which is the template
for a bridge-backed config poller: the same soft-first-refresh-as-a-background-
task, the same "no control secret yet" handling, and the same add-entities-from-a-
listener helper. It does NOT reuse that module's helpers, because they are typed
to the camera coordinator and worded for cameras, and generalizing them is not on
the table here.

Privacy note: `description` is the phone's name as its owner typed it, so it
routinely contains a real person's name ("<someone>'s iPhone"). That is correct
in their own Home Assistant -- it is what makes the switch identifiable -- but it
is never logged above DEBUG here, and log lines that must identify a device at
INFO or above use the opaque device id instead. It can also contain a curly
apostrophe (U+2019), so it is only ever passed through as text: never parsed,
sliced, slugified or used to build an identifier.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

# From bridge_client, the module that defines them, rather than second-hand
# through camera_settings_coordinator (which imports the same name from here for
# its own use) -- switch.py and camera.py already import it this way.
from .bridge_client import bridge_get, read_control_secret
from .const import (
    ARMING_DEVICES_POLL_INTERVAL_SECONDS,
    CAMERA_BRIDGE_ARMING_DEVICE_DISARMING_URL_TEMPLATE,
    CAMERA_BRIDGE_ARMING_DEVICE_PARTITION_URL_TEMPLATE,
    CAMERA_BRIDGE_ARMING_DEVICES_URL,
    CAMERA_BRIDGE_BLUETOOTH_DISARMING_URL,
    DATA_ARMING_DEVICES_COORDINATOR,
    DATA_PANEL_BLUETOOTH_COORDINATOR,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)

# Generous on purpose, and for the same reason bypass_service.py's write timeout
# is: one call here is three live Alarm.com round trips behind one bridge request
# (the membership pre-read, the record GET, then the PUT that replaces it).
# Timing out on a write that is actually succeeding would leave the user unsure
# whether their phone can now disarm the house, which is the one outcome to
# avoid -- and would invite a second write at the same live panel.
_POST_TIMEOUT_S = 30


class ArmingDevicesCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls GET /control/arming-devices.

    `data` is keyed by arming device id; each value is one element of the
    bridge's `devices` array verbatim (see docs/control-api.md). A device the
    bridge stops reporting is simply absent, which is what drives its switch to
    unavailable -- there is deliberately no "last known" fallback for a device
    that has disappeared, because the honest answer for a record this
    consequential is "we do not currently know", not a stale True.
    """

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS Bluetooth arming devices",
            update_interval=timedelta(seconds=ARMING_DEVICES_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch the current arming device list from the bridge."""

        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_ARMING_DEVICES_URL)
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                # The bridge image is built and deployed separately from this
                # integration, so a bridge predating this route is a normal
                # mid-upgrade state rather than an error. It means "this control
                # is not on offer", which must read as unavailable, not as a
                # remembered value -- so the devices are dropped rather than kept.
                LOGGER.debug("Camera bridge has no /control/arming-devices route")
                return {}
            if err.status == 401:
                # Secret on disk is stale relative to what the bridge enforces;
                # self-heals on the next cycle -- same case as camera.py.
                raise UpdateFailed("Camera bridge rejected control secret (401)") from err
            if err.status == 429:
                # Alarm.com's limits are undocumented and a stampede can lock the
                # account (AGENTS.md), so this waits for the next scheduled cycle
                # instead of retrying -- the header is surfaced for the log only.
                raise UpdateFailed(
                    "Alarm.com is rate-limiting this account; arming devices not read"
                ) from err
            raise UpdateFailed(f"Camera bridge returned HTTP {err.status}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        if body is None:
            # No control secret on the shared mount yet -- normal for a minute
            # after either container restarts, and NOT the same as "no devices".
            # Keeping the last page means the switches stay as they were instead
            # of flapping to unavailable and back on every restart.
            return self.data or {}

        devices = body.get("devices") or []
        # An id-less record cannot be addressed by the write route, so it could
        # only produce a switch that 404s; drop it rather than build an entity
        # around a key that is None.
        return {device["id"]: device for device in devices if device.get("id")}

    @callback
    def async_apply_device(self, device_id: str, record: dict[str, Any]) -> None:
        """Fold the record echoed by a write back into `data`.

        Returns None. The write route answers with the device as it stands after
        the PUT, so a toggle can update its entity from that reply instead of
        forcing a poll of a route this coordinator deliberately reads every 15
        minutes. A device the coordinator has since dropped is ignored.

        Trap: only ever pass a body this route documents as the refreshed record
        (one element of the `devices` array). Anything else replaces what the
        switch reads -- including its capability flag -- until the next poll.
        """
        if not record or record.get("id") != device_id:
            # A reply about a different device (or no device) is not evidence
            # about this one. Ignoring it leaves the caller's own refresh as the
            # way this device's state gets re-established.
            return
        data = dict(self.data or {})
        if device_id not in data:
            return
        data[device_id] = record
        self.async_set_updated_data(data)

    async def async_config_entry_first_refresh_soft(self) -> None:
        """Do the first refresh without ever being able to abort platform setup.

        Returns None. Identical in intent to the camera settings coordinator's
        method of the same name: the switch platform also carries the Skybell
        chime switches and the automation rule switches, none of which have
        anything to do with this route, so a bridge that is down (or older than
        it) must not take them down too. The devices simply appear on a later
        poll.

        This covers the EXCEPTION half only; the DURATION half is the caller's,
        which is why async_get_arming_devices_coordinator runs this as a
        background task rather than awaiting it.
        """
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001 - never let this abort a platform
            LOGGER.debug(
                "Initial arming device refresh failed; Bluetooth disarming switches "
                "start absent", exc_info=True,
            )


async def async_get_arming_devices_coordinator(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> ArmingDevicesCoordinator:
    """Return this config entry's shared arming devices coordinator, creating it once.

    Created lazily here rather than in __init__.py for the same reason the camera
    settings coordinator is: it is owned by entity platforms and by nothing else,
    and __init__.py pops the per-entry hass.data dict on unload, so it needs no
    teardown of its own.

    The first poll is scheduled as a BACKGROUND task and deliberately never
    awaited. Bounding it is not the point -- one bridge request is cheap next to
    the camera sweep -- the point is that the request fans out to a live
    Alarm.com read behind a bridge that may be mid-login, and Home Assistant's
    platform-setup budget is 60s for everything the switch platform sets up. The
    existing automation-rule coordinator in switch.py DOES await its first
    refresh; that is not a precedent to copy for a second one on the same
    platform, since the two waits would add up.

    Trap: the returned coordinator normally has NO data yet. Never enumerate
    `coordinator.data` once at setup time -- add entities through
    async_add_arming_device_entities below, which listens.
    """
    store: dict[str, Any] = hass.data[DOMAIN][config_entry.entry_id]

    if (coordinator := store.get(DATA_ARMING_DEVICES_COORDINATOR)) is not None:
        return coordinator

    coordinator = ArmingDevicesCoordinator(hass, config_entry)
    # Published before the task is created, and with no await left anywhere in
    # this function: platforms are set up concurrently, so a suspension point
    # between the lookup above and this assignment would let a second platform
    # build a second poller and double the request rate against TELUS.
    store[DATA_ARMING_DEVICES_COORDINATOR] = coordinator
    # Tied to the config entry, so an unload cancels it rather than leaving a
    # poll running against a bridge nothing is listening to any more.
    config_entry.async_create_background_task(
        hass,
        coordinator.async_config_entry_first_refresh_soft(),
        name=f"{DOMAIN}_arming_devices_first_refresh",
    )
    return coordinator


@callback
def async_add_arming_device_entities(
    config_entry: ConfigEntry,
    coordinator: ArmingDevicesCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[str, dict[str, Any]], Sequence[Entity]],
) -> None:
    """Add entities per arming device as soon as the bridge reports the capability.

    Returns None. `build(device_id, record)` is handed one device's record and
    returns the entities for it, or an empty sequence while the device does not
    report `supports_disarming_toggle` -- HA must not create a control for
    something the record refuses to be written through, and that flag is only
    known once a poll has succeeded. A device is re-offered to `build` on every
    poll until it yields something, then never again, which is what lets a phone
    paired later (or a bridge that comes up after HA) get its switch with no
    restart.

    A near-copy of camera_settings_coordinator.async_add_camera_entities, which
    it deliberately does not call: that helper is typed to the camera settings
    coordinator, and widening it to a second unrelated coordinator would mean
    editing the module four platforms already depend on to save fifteen lines
    here.

    Trap: `build` is called from a coordinator listener, so it must be
    non-blocking and must not assume it is called exactly once per device.
    """
    done: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[Entity] = []
        for device_id, record in (coordinator.data or {}).items():
            if device_id in done:
                continue
            entities = build(device_id, record)
            if entities:
                done.add(device_id)
                new.extend(entities)
        if new:
            async_add_entities(new)

    _add_new()
    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))


class PanelBluetoothCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls GET /control/bluetooth-disarming -- the PANEL-level settings (E41).

    `data` is the route's body verbatim: `pairing_enabled`, `timeout`, their
    `supports_*` flags, `timeout_unit`/`timeout_options`, and the ids of the
    phones the panel says are paired. An empty dict means "we do not currently
    know", which is what drives the entities to unavailable -- there is
    deliberately no remembered value for a setting this consequential.

    Its own coordinator rather than a second job for ArmingDevicesCoordinator:
    that one's `data` is keyed by device id and is indexed directly by several
    entities, so folding a system-wide record into the same dict would put a key
    in it that is not a device.

    Shares the arming devices' 15-minute interval, and for the same reasons --
    this is a configuration record that only moves when a person moves it, the
    poll is not the feedback path for a change made here (a write adopts the
    reply), and every cycle is a live Alarm.com round trip on an account whose
    rate limits are undocumented.
    """

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS panel Bluetooth disarming",
            update_interval=timedelta(seconds=ARMING_DEVICES_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the panel's Bluetooth disarming settings from the bridge."""
        try:
            body = await bridge_get(self.hass, CAMERA_BRIDGE_BLUETOOTH_DISARMING_URL)
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                # Either the bridge predates this route (normal mid-upgrade: the
                # image is deployed separately from this integration) or the panel
                # has no such document. Both mean "not on offer", which must read
                # as no entity rather than as a remembered value.
                LOGGER.debug("Camera bridge has no /control/bluetooth-disarming route")
                return {}
            if err.status == 401:
                raise UpdateFailed("Camera bridge rejected control secret (401)") from err
            if err.status == 429:
                raise UpdateFailed(
                    "Alarm.com is rate-limiting this account; panel Bluetooth "
                    "settings not read"
                ) from err
            raise UpdateFailed(f"Camera bridge returned HTTP {err.status}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

        if body is None:
            # No control secret on the shared mount yet -- normal for a minute
            # after either container restarts, and NOT the same as "no settings".
            return self.data or {}
        return body

    @callback
    def async_apply_settings(self, settings: dict[str, Any]) -> None:
        """Fold the settings echoed by a write back into `data`.

        Returns None. The write route answers with the document as it stands
        after the PUT, so an entity can update from that reply instead of forcing
        a poll of a route this coordinator deliberately reads every 15 minutes.

        Trap: only ever pass a body this route documents as the refreshed
        settings. Anything else replaces what the entities read -- including
        their capability flags -- until the next poll.
        """
        if not settings:
            return
        self.async_set_updated_data(dict(settings))

    async def async_config_entry_first_refresh_soft(self) -> None:
        """Do the first refresh without ever being able to abort platform setup.

        Returns None. Same contract as the arming devices coordinator's method of
        the same name: the switch and number platforms carry Skybell config
        entities and camera controls that have nothing to do with this route, so
        a bridge that is down (or older than it) must not take them down too.
        """
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001 - never let this abort a platform
            LOGGER.debug(
                "Initial panel Bluetooth refresh failed; the panel Bluetooth "
                "entities start absent", exc_info=True,
            )


async def async_get_panel_bluetooth_coordinator(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> PanelBluetoothCoordinator:
    """Return this config entry's shared panel Bluetooth coordinator, creating it once.

    Same lazy-creation pattern as async_get_arming_devices_coordinator, including
    the first poll running as a never-awaited background task, and for the same
    reasons -- read that function's docstring rather than this one for the
    reasoning.

    Trap: the returned coordinator normally has NO data yet. Never enumerate it
    once at setup time; add entities through async_add_panel_bluetooth_entities
    below, which listens.
    """
    store: dict[str, Any] = hass.data[DOMAIN][config_entry.entry_id]

    if (coordinator := store.get(DATA_PANEL_BLUETOOTH_COORDINATOR)) is not None:
        return coordinator

    coordinator = PanelBluetoothCoordinator(hass, config_entry)
    # Published before the task is created, and with no await left anywhere in
    # this function: platforms are set up concurrently, so a suspension point
    # between the lookup above and this assignment would let a second platform
    # build a second poller and double the request rate against TELUS.
    store[DATA_PANEL_BLUETOOTH_COORDINATOR] = coordinator
    config_entry.async_create_background_task(
        hass,
        coordinator.async_config_entry_first_refresh_soft(),
        name=f"{DOMAIN}_panel_bluetooth_first_refresh",
    )
    return coordinator


@callback
def async_add_panel_bluetooth_entities(
    config_entry: ConfigEntry,
    coordinator: PanelBluetoothCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[dict[str, Any]], Sequence[Entity]],
) -> None:
    """Add the panel Bluetooth entities as soon as the bridge reports the capability.

    Returns None. `build(settings)` is handed the whole settings body and returns
    the entities to create, or an empty sequence while the panel does not report
    the field they need -- and it is re-offered on every poll until it yields
    something, then never again. That is what lets a bridge coming up after Home
    Assistant, or a panel whose document is briefly unreadable, still get its
    entities without a restart.

    The singular counterpart of async_add_arming_device_entities: there is one
    such record per system, not one per device, so there is nothing to key on.

    Trap: `build` is called from a coordinator listener, so it must be
    non-blocking and must not assume it is called exactly once.
    """
    done = False

    @callback
    def _add_new() -> None:
        nonlocal done
        if done:
            return
        entities = build(coordinator.data or {})
        if entities:
            done = True
            async_add_entities(entities)

    _add_new()
    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))


@callback
def arming_device_device_info(
    config_entry: ConfigEntry,
    coordinator: ArmingDevicesCoordinator,
    device_id: str,
) -> DeviceInfo:
    """The Home Assistant device one paired phone's entities attach to.

    Its OWN device, not a camera's and not the integration service device. A
    paired phone is a real thing the panel knows about, with an alarm.com device
    id in the same namespace as every other device here, so it is registered the
    way camera.py and base_device.py register theirs: identified by that id,
    named by what the account calls it. Hanging these off the service device
    instead (as the automation rule switches do, having no device of their own to
    name) would pile one entity per household phone onto a single "TELUS Smart
    Home Integration" entry and read as "<the integration> <someone>'s iPhone",
    which says nothing about whose phone opens the house.

    `via_device` points at that service device, which sensor.py's auth health
    sensor creates unconditionally at setup -- so unlike the dangling link
    base_device.py warns about, it always resolves.

    `name` is the phone's description exactly as the account holder typed it,
    passed straight through: it may contain a curly apostrophe (U+2019) and it is
    never parsed or reshaped. The bridge already falls back to the device id when
    the record has no description.

    Shared by the disarming switch and the enrolled-partition select rather than
    written out in each: the two are gated on different flags, so either can
    exist without the other, and whichever appears alone must still name the
    device properly. Two copies would be two chances for one of them to register
    a nameless device.
    """
    return DeviceInfo(
        identifiers={(DOMAIN, device_id)},
        name=(coordinator.data or {}).get(device_id, {}).get("description") or device_id,
        model="Bluetooth arming device",
        via_device=(DOMAIN, f"{config_entry.entry_id}_integration"),
    )


async def _async_bridge_write(
    hass: HomeAssistant,
    url: str,
    payload: dict[str, Any],
    *,
    what: str,
    unsupported: str,
    unknown: str,
) -> dict[str, Any]:
    """POST one of the Bluetooth-disarming write routes and map every refusal.

    Returns the body the route answers with -- the refreshed record for the
    per-device routes, the refreshed settings for the panel one -- so the caller
    can fold it back in rather than forcing a poll.

    Deliberately its own POST rather than camera_settings_coordinator's
    async_bridge_post, for the same reason bypass_service.py has
    `_async_post_bypass`: that helper hard-codes the camera routes' error
    vocabulary. Its 422 is "This camera does not support that control", which is
    simply false here -- there is no camera anywhere in this feature, and a user
    reading it on a switch named after their phone would have no idea what it
    referred to -- and it has no notion of the 429/Retry-After, 400 and 502
    answers these routes define. Teaching it a per-route message table would put
    arming-device branches inside a helper whose every other caller is a camera
    entity, for one caller's benefit.

    `what` names the setting in the messages a user actually sees ("Bluetooth
    disarming", "the enrolled partition"); `unsupported` and `unknown` are the
    422 and 404 wordings, which differ per route and are the two a user is most
    likely to have to act on.

    Trap: this is the write half. Every call changes something on a live alarm
    panel -- whether a phone disarms it, which partition it disarms, or whether
    the panel accepts Bluetooth pairing at all -- so only ever reach it from an
    explicit user action, never from a poll or a recovery path.
    """
    secret = await hass.async_add_executor_job(read_control_secret)
    if secret is None:
        raise HomeAssistantError(
            f"Cannot change {what}: the TELUS camera bridge is not reachable "
            "(no control secret on the shared mount)")

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            # Real JSON types throughout: the bridge answers 400 rather than
            # coercing anything, and none of these is a field to be generous with.
            json=payload,
            headers={"Authorization": f"Bearer {secret}"},
            timeout=aiohttp.ClientTimeout(total=_POST_TIMEOUT_S),
        ) as resp:
            if resp.status == 422:
                # The record's (or the panel's) own capability gate said no. Every
                # entity here is gated on the same flag, so reaching this means it
                # changed under us. Permanent for this record, not transient.
                raise HomeAssistantError(unsupported)
            if resp.status == 404:
                raise HomeAssistantError(unknown)
            if resp.status == 429:
                retry_after = resp.headers.get("Retry-After")
                raise HomeAssistantError(
                    f"Alarm.com is rate-limiting this account; {what} was not "
                    "changed -- try again"
                    + (f" in {retry_after}s" if retry_after else " later"))
            if resp.status == 400:
                raise HomeAssistantError(
                    f"The camera bridge rejected the {what} request as malformed")
            if resp.status == 502:
                raise HomeAssistantError(
                    f"Alarm.com rejected or failed the change; {what} was left as "
                    "it was")
            resp.raise_for_status()
            try:
                return await resp.json()
            except (aiohttp.ClientError, ValueError):
                # 2xx with a body we can't parse: the write LANDED. Reporting a
                # failure would invite a second write at the same live record, so
                # degrade to a warning and let the caller's refresh establish what
                # the setting actually is now.
                LOGGER.warning(
                    "%s write was applied but answered an unreadable body", what)
                return {}
    except HomeAssistantError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(
            f"The {what} request to the camera bridge failed: {err}. The change "
            "may or may not have been applied -- check the setting before "
            "retrying") from err


async def async_set_disarming_enabled(
    hass: HomeAssistant, device_id: str, enabled: bool
) -> dict[str, Any]:
    """Turn Bluetooth disarming on or off for ONE paired phone.

    Returns the refreshed record the bridge answers with, for the caller to fold
    back in through ArmingDevicesCoordinator.async_apply_device.

    Trap: this is the write. Each call changes whether a phone walking up to a
    real house disarms a real alarm, and alarm.com is the configuration authority
    for that setting, so only ever call it from an explicit user action.
    """
    return await _async_bridge_write(
        hass,
        CAMERA_BRIDGE_ARMING_DEVICE_DISARMING_URL_TEMPLATE.format(device_id=device_id),
        # A real JSON boolean, never a coercion: bool("false") is True.
        {"enabled": enabled},
        what="Bluetooth disarming",
        unsupported=("This device's Bluetooth disarming setting cannot be changed "
                     "from here"),
        unknown="The camera bridge no longer knows this Bluetooth arming device",
    )


async def async_set_enrolled_partition(
    hass: HomeAssistant, device_id: str, partition_id: str
) -> dict[str, Any]:
    """Move ONE paired phone to a different partition (backlog E43).

    Returns the refreshed record, like async_set_disarming_enabled, for the
    caller to fold back in through ArmingDevicesCoordinator.async_apply_device.

    `partition_id` is the panel's own partition NUMBER as a string, exactly as
    the record reports it and as the bridge publishes it in
    `enrolled_partition_choices`. Never build one; never stringify an int into
    one. The bridge validates it against the account's real partitions and
    refuses anything else, and it fails closed if it cannot read that list.

    Trap: this is a write, and it is the same class of setting as the disarming
    toggle rather than a lesser one -- moving a phone onto a partition it should
    not open has the same outcome as letting a phone disarm that should not.
    Only ever call it from an explicit user action.
    """
    return await _async_bridge_write(
        hass,
        CAMERA_BRIDGE_ARMING_DEVICE_PARTITION_URL_TEMPLATE.format(device_id=device_id),
        {"partition_id": partition_id},
        what="the enrolled partition",
        unsupported=("This device's enrolled partition cannot be changed from "
                     "here"),
        unknown="The camera bridge no longer knows this Bluetooth arming device",
    )


async def async_set_bluetooth_disarming(
    hass: HomeAssistant, **fields: Any
) -> dict[str, Any]:
    """Change the PANEL-level Bluetooth disarming settings (backlog E41).

    Accepted keyword fields: `pairing_enabled` (bool) and `timeout` (int), any
    non-empty subset. Returns the refreshed settings for the caller to fold back
    in through PanelBluetoothCoordinator.async_apply_settings.

    System-wide, not per phone. Two things this must never be described as to a
    user, because neither is established: that turning pairing off stops an
    already-paired phone from disarming (the portal's per-device page does not
    consult this document, but what the panel firmware does with the flag is
    UNRESOLVED).

    Trap: the bridge's write shape for this endpoint is INFERRED from the sibling
    per-device endpoint rather than captured from a real save, so a failure here
    is at least as likely to be the reconstruction as it is to be the panel.
    """
    return await _async_bridge_write(
        hass,
        CAMERA_BRIDGE_BLUETOOTH_DISARMING_URL,
        fields,
        what="the panel's Bluetooth settings",
        unsupported=("This alarm panel does not report that Bluetooth disarming "
                     "setting"),
        unknown="The camera bridge has no panel Bluetooth disarming settings",
    )
