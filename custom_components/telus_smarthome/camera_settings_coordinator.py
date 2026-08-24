"""Shared poller for the camera-bridge's per-camera settings and diagnostics.

number.py (speaker volume), switch.py (privacy mode), button.py (siren),
sensor.py (signal strength / battery / device status) and event.py (which
cameras are doorbells, from the chime capability flags) all need the same two
bridge routes for the same set of cameras. One coordinator serves all five:
polling per platform would multiply into that many live TELUS round trips per
cycle against an account whose rate limits are undocumented and can lock it.

event.py is the odd one out and worth knowing about: it reads this coordinator
only to decide WHICH cameras get a doorbell-press entity. The entity it creates
is a CoordinatorEntity on the activity-history coordinator instead, because that
is the feed a press actually arrives on.

The camera list itself is not re-fetched here -- it is read off the existing
CameraBridgeCoordinator, so a camera discovered later gets its settings polled
with no restart, and no second /control/cameras poll exists.
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
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from .bridge_client import bridge_get, read_control_secret
from .camera import CameraBridgeCoordinator
from .const import (
    CAMERA_BRIDGE_CAMERA_DIAGNOSTICS_URL_TEMPLATE,
    CAMERA_BRIDGE_CAMERA_SETTINGS_URL_TEMPLATE,
    CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS,
    CAMERA_SETTINGS_POLL_INTERVAL_SECONDS,
    DATA_CAMERA_COORDINATOR,
    DATA_CAMERA_SETTINGS_COORDINATOR,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)


class CameraSettingsCoordinator(DataUpdateCoordinator[dict[str, dict[str, Any]]]):
    """Polls /control/cameras/{id}/settings and .../diagnostics for every known camera.

    `data` is keyed by camera id; each value is
    `{"settings": {...}, "diagnostics": {...}}` -- the bridge's two response
    bodies verbatim (see docs/control-api.md). A camera the bridge answers 404
    for is simply absent from `data` rather than present-with-empty-values, so
    entity gating can't mistake "bridge too old to have these routes" for "the
    camera says it supports nothing".
    """

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        camera_coordinator: CameraBridgeCoordinator,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS camera settings",
            update_interval=timedelta(seconds=CAMERA_SETTINGS_POLL_INTERVAL_SECONDS),
        )
        self._camera_coordinator = camera_coordinator

    async def _async_update_data(self) -> dict[str, dict[str, Any]]:
        """Fetch settings + diagnostics for each camera the bridge currently reports."""

        camera_ids = sorted(self._camera_coordinator.data or {})
        if not camera_ids:
            return {}

        # Checked once up front rather than inferred from bridge_get returning
        # None per request: a missing secret and a 404 would otherwise look the
        # same here, and "no secret yet" (normal for a minute after either
        # container restarts) must keep the last page, not read as "no camera
        # supports anything" and drop every entity to unavailable.
        if await self.hass.async_add_executor_job(read_control_secret) is None:
            return self.data or {}

        data: dict[str, dict[str, Any]] = {}
        failures: list[str] = []
        previous = self.data or {}
        for camera_id in camera_ids:
            # Sequential, not asyncio.gather: gathering would fire two live
            # TELUS requests per camera in the same instant on every cycle.
            # Nothing here is latency-sensitive, and a synchronized burst is
            # exactly what the rate-limit discipline in docs/troubleshooting.md
            # warns against.
            try:
                entry = await self._async_fetch_camera(camera_id)
            except UpdateFailed as err:
                # ONE camera's failure must not blank the others. This used to
                # propagate, so a single slow camera failed the whole sweep and
                # every camera's entities went unavailable -- and on a cold start,
                # where entities are created from the first successful poll, none
                # were ever created at all. Observed live: one camera's
                # /diagnostics took 25s against a 10s client timeout, and no
                # camera in the account got a single entity.
                LOGGER.warning("Camera settings poll failed for %s: %s", camera_id, err)
                failures.append(camera_id)
                # Keep whatever that camera last reported rather than dropping it:
                # capability flags do not change minute to minute, and a stale
                # supports_* is a better answer than "this camera can do nothing".
                if camera_id in previous:
                    data[camera_id] = previous[camera_id]
                continue
            if entry is not None:
                data[camera_id] = entry

        # Only a total failure is worth marking the coordinator failed -- that is
        # the bridge being down, which the entities should reflect. A partial
        # failure has already been logged and degraded per camera above.
        if failures and len(failures) == len(camera_ids):
            raise UpdateFailed(
                f"Camera settings poll failed for every camera ({', '.join(failures)})")
        return data

    async def _async_fetch_camera(self, camera_id: str) -> dict[str, Any] | None:
        """Return one camera's settings+diagnostics, or None if the bridge has neither."""

        settings = await self._async_get(CAMERA_BRIDGE_CAMERA_SETTINGS_URL_TEMPLATE, camera_id)

        # Diagnostics gets a longer timeout than the shared default because it is
        # not one upstream request: the bridge fans it out to three sequential
        # alarm.com reads (signal, battery, device status), and alarm.com appears
        # to query the camera live for the signal figure. Measured at 1.6s on one
        # camera and 25s on another in the same account, so the default 10s is not
        # a slow-bridge signal here, it is normal for some models.
        diagnostics = await self._async_get(
            CAMERA_BRIDGE_CAMERA_DIAGNOSTICS_URL_TEMPLATE, camera_id,
            timeout=CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS)
        if settings is None and diagnostics is None:
            return None
        return {"settings": settings or {}, "diagnostics": diagnostics or {}}

    async def _async_get(self, url_template: str, camera_id: str, *,
                         timeout: int = 10) -> dict[str, Any] | None:
        """GET one bridge route for one camera.

        Returns None when the bridge answers 404 -- either it doesn't know this
        camera, or it predates these routes entirely (a bridge image is deployed
        and rebuilt separately from this integration, so a mismatch is normal
        mid-upgrade). Both mean "no capability to expose", never an error worth
        marking the other cameras' entities unavailable for. Any other failure
        raises UpdateFailed.
        """
        try:
            return await bridge_get(
                self.hass, url_template.format(camera_id=camera_id), timeout=timeout)
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                LOGGER.debug("Camera bridge has no %s for %s", url_template.rsplit("/", 1)[-1], camera_id)
                return None
            if err.status == 401:
                # Secret on disk is stale relative to what the bridge enforces;
                # self-heals on the next cycle -- same case as camera.py.
                raise UpdateFailed("Camera bridge rejected control secret (401)") from err
            raise UpdateFailed(f"Camera bridge returned HTTP {err.status}") from err
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            raise UpdateFailed(f"Could not reach camera bridge: {err}") from err

    @callback
    def async_apply_settings_section(
        self, camera_id: str, section: str, payload: dict[str, Any]
    ) -> None:
        """Fold a write route's echoed settings section back into `data`.

        Returns None. The volume/chime/image/video routes each answer with the
        section they have just re-read from the camera, so a write can update
        every entity from that reply instead of triggering a full re-poll of
        both routes -- which is the whole reason this coordinator polls slowly.
        A camera the coordinator has since dropped is ignored.

        Trap: pass only a body the route documents as the refreshed section
        (`audio`, `image`, `video`, `led`, `notifications`). Anything else silently
        replaces that
        section for every entity reading it until the next poll.
        """
        data = dict(self.data or {})
        camera = data.get(camera_id)
        if camera is None:
            return
        settings = {**(camera.get("settings") or {}), section: payload}
        data[camera_id] = {**camera, "settings": settings}
        self.async_set_updated_data(data)

    async def async_config_entry_first_refresh_soft(self) -> None:
        """Do the first refresh without ever being able to abort platform setup.

        Returns None. Unlike async_config_entry_first_refresh, a bridge that is
        down (or older than these routes) must not take down the platform it is
        called from -- the Skybell config entities in number.py/switch.py and the
        integration diagnostics in sensor.py have nothing to do with the bridge.
        Entities simply start out absent/unavailable and appear on a later poll.

        This covers the EXCEPTION half of that promise only. The DURATION half is
        the caller's job: async_get_camera_settings_coordinator runs this as a
        background task precisely because swallowing the error here would still
        let a wedged bridge hold platform setup past HA's 60s budget if it were
        awaited inline. Both halves are load-bearing -- keep them.
        """
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001 - never let this abort a platform
            LOGGER.debug(
                "Initial camera settings refresh failed; camera setting entities "
                "start unavailable", exc_info=True,
            )


async def async_get_camera_settings_coordinator(
    hass: HomeAssistant, config_entry: ConfigEntry
) -> CameraSettingsCoordinator:
    """Return this config entry's shared camera settings coordinator, creating it once.

    Created lazily here instead of in __init__.py because this coordinator is
    owned by five entity platforms and by nothing else; __init__.py pops the
    per-entry hass.data dict on unload, so the instance needs no teardown.

    The first poll is scheduled as a BACKGROUND task and deliberately never
    awaited here. _async_update_data walks every camera sequentially over two
    bridge routes, each bounded only by bridge_client's own 10s per-request
    timeout, so a slow or wedged bridge costs up to 20s per camera -- past Home
    Assistant's 60s platform-setup budget with three cameras. Blowing that budget
    aborts whichever of button/number/switch/sensor set up first, taking the
    Debug buttons, the Skybell config entities and the integration diagnostics
    down with it: entities that have nothing to do with the bridge. Degrading to
    "the entities show up / populate shortly" is strictly better than that.

    What that trades away, chosen deliberately: entities gated on the first
    poll's contents are late rather than immediate. The siren buttons are the
    case that matters -- they exist only for a camera reporting the
    canManuallyTriggerSiren permission, so with no data at setup time none are
    created yet. Nothing is lost, because async_add_camera_entities below adds
    from a coordinator listener and re-offers every camera on every poll until it
    yields entities; they appear on the first successful poll instead. The one
    visible consequence is ordering: an automation or dashboard referencing them
    must tolerate the entity not existing for a poll interval after a restart --
    exactly as it already must for a camera added to the account later.

    Trap: the returned coordinator normally has NO data yet -- both because the
    first poll is now in flight and because HA sets platforms up concurrently.
    Never enumerate `coordinator.data` once at setup time; add entities from
    `async_add_camera_entities` below, which listens.
    """
    store: dict[str, Any] = hass.data[DOMAIN][config_entry.entry_id]

    if (coordinator := store.get(DATA_CAMERA_SETTINGS_COORDINATOR)) is not None:
        return coordinator

    coordinator = CameraSettingsCoordinator(hass, config_entry, store[DATA_CAMERA_COORDINATOR])
    # Published before the task is created, and with no await left anywhere in
    # this function: platforms are set up concurrently, so a suspension point
    # between the lookup above and this assignment would let a second platform
    # build a second poller and double the request rate against TELUS.
    store[DATA_CAMERA_SETTINGS_COORDINATOR] = coordinator
    # Tied to the config entry, so an unload cancels it rather than leaving a
    # poll running against a bridge nothing is listening to any more.
    config_entry.async_create_background_task(
        hass,
        coordinator.async_config_entry_first_refresh_soft(),
        name=f"{DOMAIN}_camera_settings_first_refresh",
    )
    return coordinator


@callback
def async_add_camera_entities(
    config_entry: ConfigEntry,
    coordinator: CameraSettingsCoordinator,
    async_add_entities: AddEntitiesCallback,
    build: Callable[[str, dict[str, Any]], Sequence[Entity]],
) -> None:
    """Add entities per camera as soon as the bridge reports the capability they need.

    Returns None. `build(camera_id, data)` is handed one camera's
    `{"settings":…, "diagnostics":…}` and returns the entities for it, or an
    empty sequence while the capability isn't reported -- HA must not create an
    entity for something a camera cannot do, and the support flags are only
    known once a poll has succeeded. A camera is re-offered to `build` on every
    poll until it yields something, then never again, which is what lets a
    camera added to the account later (or a bridge that comes up after HA) get
    its entities with no restart.

    Trap: `build` is called from a coordinator listener, so it must be
    non-blocking and must not assume it is called exactly once per camera.
    """
    done: set[str] = set()

    @callback
    def _add_new() -> None:
        new: list[Entity] = []
        for camera_id, camera_data in (coordinator.data or {}).items():
            if camera_id in done:
                continue
            entities = build(camera_id, camera_data)
            if entities:
                done.add(camera_id)
                new.extend(entities)
        if new:
            async_add_entities(new)

    _add_new()
    config_entry.async_on_unload(coordinator.async_add_listener(_add_new))


async def async_bridge_post(
    hass: HomeAssistant, url: str, payload: dict[str, Any] | None = None, *, timeout: int = 15
) -> dict[str, Any]:
    """POST a JSON body to one of the bridge's camera control routes.

    Returns the parsed response body -- for the settings routes that is the
    section re-read from the camera after the write, which the caller can hand
    to async_apply_settings_section instead of polling again. Every failure,
    including a reply that isn't JSON, raises HomeAssistantError worded for the
    toast HA shows the user, since every caller is an entity service handler.

    Trap: this is the write half of the control API -- each call changes
    something on a real camera in someone's home (or sounds a siren). Only ever
    call it from an explicit user action, never from a state read or a poll.
    """
    secret = await hass.async_add_executor_job(read_control_secret)
    if secret is None:
        raise HomeAssistantError("The TELUS camera bridge is not reachable.")

    session = async_get_clientsession(hass)
    try:
        async with session.post(
            url,
            json=payload or {},
            headers={"Authorization": f"Bearer {secret}"},
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            if resp.status == 422:
                # The bridge checked the field's <name>FeatureSupport enum and
                # this device reports it as unsupported. Permanent, not transient.
                raise HomeAssistantError("This camera does not support that control.")
            if resp.status == 404:
                raise HomeAssistantError("The camera bridge no longer knows this camera.")
            if resp.status == 429:
                # The alarm.com edge rate-limited the upstream call; the bridge
                # forwards the server's own wait as a Retry-After header and
                # mirrors it in the body ({"error": "rate_limited",
                # "retry_after": <s>}). Surface that wait, never retry it here --
                # every caller is a user action, and an automatic retry is
                # exactly the tight loop the rate-limit discipline forbids.
                retry_after: Any = resp.headers.get("Retry-After")
                if retry_after is None:
                    try:
                        retry_after = (await resp.json()).get("retry_after")
                    except (aiohttp.ClientError, ValueError):
                        retry_after = None
                raise HomeAssistantError(
                    "Alarm.com is rate-limiting this account; the change was not "
                    "applied -- try again"
                    + (f" in {retry_after}s" if retry_after else " later"))
            resp.raise_for_status()
            return await resp.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as err:
        raise HomeAssistantError(f"The TELUS camera bridge rejected the request: {err}") from err


class CameraSettingsEntity(CoordinatorEntity[CameraSettingsCoordinator]):
    """Base for entities backed by the shared camera settings/diagnostics coordinator.

    Attaches to the EXISTING bridge-served camera device by reusing the
    identifier camera.py registers it under, so these appear as extra entities
    on the camera rather than as a second device. Only the identifier is given:
    name/model/firmware belong to camera.py, and repeating them here would let
    this entity fight it over the device's registry entry.
    """

    _attr_has_entity_name = True

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str, key: str) -> None:
        """Initialize the entity for one camera and one capability key."""
        super().__init__(coordinator)

        self._camera_id = camera_id
        self._attr_unique_id = f"{camera_id}_{key}"
        self._attr_translation_key = f"camera_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, camera_id)})

    @property
    def _camera_data(self) -> dict[str, Any]:
        """This camera's whole coordinator entry, or {} once the bridge drops it."""
        return (self.coordinator.data or {}).get(self._camera_id) or {}

    @property
    def _settings(self) -> dict[str, Any]:
        """The camera's /settings body.

        Sections: `audio`, `image`, `video`, `led` and `notifications` from the
        website API, plus
        `siren` and `privacy` from the mobile one -- those two are `None` when
        the bridge has no record rather than absent, which is not the same as
        "the camera says no" (see docs/control-api.md).
        """
        return self._camera_data.get("settings") or {}

    @property
    def _diagnostics(self) -> dict[str, Any]:
        """The camera's /diagnostics body."""
        return self._camera_data.get("diagnostics") or {}

    @property
    def available(self) -> bool:
        """Unavailable when the bridge is unhealthy or stops reporting this camera."""
        return super().available and bool(self._camera_data)
