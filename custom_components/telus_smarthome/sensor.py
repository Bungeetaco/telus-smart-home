"""Diagnostic and data-feed sensor(s) for the TELUS Smart Home integration.

AuthHealthSensor reflects TELUS/Alarm.com session auth health, so the user
can see in Home Assistant (as a normal entity/history graph) when
authentication starts failing, rather than only discovering it via the
reauth persistent-notification or raw logs.

TalkdownStatusSensor / TalkdownFailuresSensor surface the bridge's talkdown
counters, so a talk that starts failing is visible as an entity (and can drive a
notification) rather than only as an HTTP error to whoever triggered it.

The camera diagnostic sensors (signal strength, battery, device status) attach
to the bridge-served camera devices themselves. A camera degrading to poor
Wi-Fi is otherwise a completely silent failure mode -- the stream just gets
worse -- which is what these exist to make visible. CameraSolarPanelSensor joins
them from the settings side rather than the diagnostics one (E39), and is
expected to appear on NO camera on most accounts.

The stream-health sensors (CameraStreamStateSensor, CameraDroppedFramesSensor,
CameraAudioStateSensor) also attach to the camera devices, but read the bridge's
own /control/status pipeline diagnostics rather than TELUS-side data -- a stream
stuck reconnecting is otherwise only visible in the bridge's container logs, and
a stream whose audio decoder has died is not visible there either: every video
signal stays green while the published audio is pure silence. Their
StreamHealthCoordinator is shared with binary_sensor.py's bridge-connectivity
entity via async_get_stream_health_coordinator.

ActivityFeedSensor exposes the full recent Activity list (not just the
single latest entry event.py's TelusActivityEvent tracks) as an attribute,
for a dashboard card to render as a scrollable feed -- see that entity's
own docstring for why an EventEntity alone can't do this.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from homeassistant import core
from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    PERCENTAGE,
    EntityCategory,
)
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.entity import DeviceInfo, Entity
from homeassistant.helpers.entity_platform import AddEntitiesCallback, DiscoveryInfoType
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)
from homeassistant.util import dt as dt_util

from .bridge_client import bridge_get
from .camera_settings_coordinator import (
    CameraSettingsCoordinator,
    CameraSettingsEntity,
    async_add_camera_entities,
    async_get_camera_settings_coordinator,
)
from .const import (
    CAMERA_BRIDGE_METRICS_URL,
    CAMERA_BRIDGE_STATUS_URL,
    DATA_ACTIVITY_COORDINATOR,
    DATA_CONTROLLER,
    DATA_STREAM_HEALTH_COORDINATOR,
    DOMAIN,
    STREAM_HEALTH_POLL_INTERVAL_SECONDS,
    TALK_METRICS_POLL_INTERVAL_SECONDS,
)
from .controller import AlarmIntegrationController
from .event import ActivityHistoryCoordinator, _clip_link_for

LOGGER = logging.getLogger(__name__)

AUTH_HEALTH_STATES = ["ok", "reauth_required", "telus_error"]

# The two states CameraSolarPanelSensor can report. Named rather than inlined so
# the entity, its `options` list and strings.json's state block cannot drift --
# an ENUM sensor whose state is not in its own options logs an error on every
# update, and a state missing from the translations renders as a raw slug.
SOLAR_PANEL_CONNECTED = "connected"
SOLAR_PANEL_NOT_CONNECTED = "not_connected"
SOLAR_PANEL_STATES = [SOLAR_PANEL_CONNECTED, SOLAR_PANEL_NOT_CONNECTED]

# Every state camera-bridge/bridge/camera_session.py's run_camera_forever can
# put in a /control/status entry: "connecting" is the initial dict, the other
# three are its _update_status(state=...) calls. Pinned as a list for the same
# reason as SOLAR_PANEL_STATES above -- an ENUM sensor whose state is not in
# its own options logs an error on every update -- but this vocabulary is
# owned by a separately-deployed bridge, so CameraStreamStateSensor maps a
# value outside this list (a future bridge with a new state) to None instead
# of erroring, with the verbatim value still visible in its raw_state
# attribute.
STREAM_STATES = ["connecting", "streaming", "reconnecting", "no_media", "restart_deferred"]

# The four states CameraAudioStateSensor can report. Pinned as named constants
# for the same reason as SOLAR_PANEL_STATES/STREAM_STATES above, but note the
# difference: unlike those two this vocabulary is NOT the bridge's -- it is
# derived here, from `audio_consumed` plus whether `decoded_audio_frames` moved
# between two polls. /control/status has no "is audio alive" field to read.
AUDIO_NOT_CONSUMING = "not_consuming"
AUDIO_NO_AUDIO_YET = "no_audio_yet"
AUDIO_FLOWING = "flowing"
AUDIO_STALLED = "stalled"
AUDIO_STATES = [AUDIO_NOT_CONSUMING, AUDIO_NO_AUDIO_YET, AUDIO_FLOWING, AUDIO_STALLED]


async def async_setup_entry(
    hass: core.HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
    discovery_info: DiscoveryInfoType | None = None,  # pylint: disable=unused-argument
) -> None:
    """Set up the sensor platform."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][config_entry.entry_id][DATA_CONTROLLER]
    activity_coordinator: ActivityHistoryCoordinator = hass.data[DOMAIN][config_entry.entry_id][
        DATA_ACTIVITY_COORDINATOR
    ]

    # The talkdown counters live on the bridge, which may not be running (or may
    # have talkdown disabled). Its own coordinator, refreshed once up front: a
    # failure here must not stop the other sensors from being added, so the
    # entities simply report unavailable until a poll succeeds.
    metrics_coordinator = TalkMetricsCoordinator(hass, config_entry)
    await metrics_coordinator.async_config_entry_first_refresh_soft()

    async_add_entities([
        AuthHealthSensor(controller, config_entry),
        ActivityFeedSensor(activity_coordinator, config_entry),
        TalkdownStatusSensor(metrics_coordinator, config_entry),
        TalkdownFailuresSensor(metrics_coordinator, config_entry),
    ])

    # Per-camera stream-health diagnostics, read off the bridge's own in-memory
    # /control/status dict (loopback-only -- see
    # STREAM_HEALTH_POLL_INTERVAL_SECONDS in const.py). The coordinator is
    # shared with binary_sensor.py's bridge-connectivity entity, hence the
    # lazy getter. The camera set is the status payload's keys, and entities
    # are added from a coordinator listener (camera.py's _add_new_cameras
    # pattern) so a camera the bridge starts reporting later -- or a bridge
    # that comes up after HA -- gets its sensors with no restart.
    stream_health_coordinator = await async_get_stream_health_coordinator(hass, config_entry)
    known_stream_ids: set[str] = set()

    @core.callback
    def _add_stream_health_sensors() -> None:
        """Create the stream-health sensors for any camera id not seen before.

        All three are created for EVERY camera the bridge reports, including the
        audio one for a camera that is not consuming audio -- see
        CameraAudioStateSensor's docstring for why that is deliberate rather
        than the conditional creation the diagnostics/solar sensors below use.
        """
        new_ids = set(stream_health_coordinator.data or {}) - known_stream_ids
        if not new_ids:
            return
        known_stream_ids.update(new_ids)
        entities: list[Entity] = []
        for camera_id in sorted(new_ids):
            entities.append(CameraStreamStateSensor(stream_health_coordinator, camera_id))
            entities.append(CameraDroppedFramesSensor(stream_health_coordinator, camera_id))
            entities.append(CameraAudioStateSensor(stream_health_coordinator, camera_id))
        async_add_entities(entities)

    _add_stream_health_sensors()
    config_entry.async_on_unload(
        stream_health_coordinator.async_add_listener(_add_stream_health_sensors))

    # Per-camera diagnostics, served by the camera-bridge and attached to the
    # camera devices rather than the synthetic integration device above.
    settings_coordinator = await async_get_camera_settings_coordinator(hass, config_entry)

    @core.callback
    def _build_diagnostics(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """One sensor per diagnostic field this camera actually reports."""
        diagnostics = camera_data.get("diagnostics") or {}
        if not diagnostics:
            return []
        # Keyed off what the device reports, not a fixed list: every field of
        # the diagnostics body is nullable (three separate upstream reads back
        # it), and a permanently-unknown Battery sensor on a mains-powered
        # doorbell is noise, not diagnostics. Non-scalar values are skipped
        # because an entity state must be a scalar.
        return [
            sensor_cls(settings_coordinator, camera_id)
            for key, sensor_cls in CAMERA_DIAGNOSTIC_SENSORS.items()
            if _scalar_or_none(diagnostics.get(key)) is not None
        ]

    async_add_camera_entities(
        config_entry, settings_coordinator, async_add_entities, _build_diagnostics
    )

    @core.callback
    def _build_solar(camera_id: str, camera_data: dict[str, Any]) -> list[Entity]:
        """A solar-panel sensor, only for a camera that reports the feature.

        Expect this to yield NOTHING on most accounts, including both cameras on
        the one this was built against: `connectedToSolarPanelFeatureSupport` is
        0 on each, so the bridge reports supports_connected false. That is the
        correct outcome, not a gap -- `isConnectedToSolarPanel` is also false
        there, and a camera with no solar hardware at all is indistinguishable
        from one that has some and is disconnected if you read the value instead
        of the flag. A permanent "Not connected" on every doorbell in every
        install would be noise dressed up as diagnostics.
        """
        solar = (camera_data.get("settings") or {}).get("solar") or {}
        if not solar.get("supports_connected"):
            return []
        return [CameraSolarPanelSensor(settings_coordinator, camera_id)]

    async_add_camera_entities(
        config_entry, settings_coordinator, async_add_entities, _build_solar
    )


class AuthHealthSensor(CoordinatorEntity, SensorEntity):  # type: ignore
    """Diagnostic sensor reflecting TELUS/Alarm.com session auth health.

    Not tied to any single Alarm.com device (unlike the other platforms in
    this integration); it reports on the health of the integration's own
    connection, so it's grouped under a synthetic "integration" device
    rather than a partition/panel device.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "auth_health"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AUTH_HEALTH_STATES

    def __init__(self, controller: AlarmIntegrationController, config_entry: ConfigEntry) -> None:
        """Pass coordinator to CoordinatorEntity."""
        super().__init__(controller.update_coordinator)

        self._controller = controller

        self._attr_unique_id = f"{config_entry.entry_id}_auth_health"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
            name="TELUS Smart Home Integration",
            manufacturer="TELUS",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def native_value(self) -> str:
        """Return the current auth health state."""
        return self._controller.auth_health

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional diagnostic detail."""
        return {
            "detail": self._controller.auth_health_detail,
            "last_success": (
                self._controller.last_auth_success.isoformat() if self._controller.last_auth_success else None
            ),
        }


class ActivityFeedSensor(CoordinatorEntity[ActivityHistoryCoordinator], SensorEntity):
    """Exposes the full recent-Activity list as an attribute for a dashboard
    card to render.

    event.py's TelusActivityEvent (an EventEntity) can only ever hold the
    single most-recently-fired event's attributes -- that's the EventEntity
    model, useful for "notify me when something happens" automations but not
    for a browsable list of recent clips. This sensor shares the exact same
    coordinator (no extra bridge polling) and just reflects its whole current
    page of results, so a card can iterate `events` directly.
    """

    _attr_has_entity_name = True
    _attr_name = "Recent Events"
    _attr_icon = "mdi:history"
    # The events blob is several KB and changes on EVERY 30s poll even when nothing
    # happened, because TELUS re-signs the thumbnail/download URLs each time. Recorded
    # normally that is a multi-KB row written to the database ~2,880 times a day
    # forever, for data that is already ephemeral (the signed URLs expire, so old rows
    # are worthless). The state itself (an event count) still records fine.
    _unrecorded_attributes = frozenset({"events"})

    def __init__(self, coordinator: ActivityHistoryCoordinator, config_entry: ConfigEntry) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)

        self._attr_unique_id = f"{config_entry.entry_id}_activity_feed"
        # Same device as event.py's TelusActivityEvent (identical identifier)
        # so the two show up together rather than as two synthetic devices.
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_activity")},
            name="Activity",
            manufacturer="TELUS Smart Home",
        )

    @property
    def native_value(self) -> int:
        """Number of events in the current page -- a small, glanceable number;
        the actual content lives in extra_state_attributes below."""
        return len(self.coordinator.data or [])

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """The full current page of Activity-feed events, newest first --
        same shape as control_server.py's /control/history response, plus a
        durable `clip_link` per clip-bearing event so a dashboard card renders
        the same self-re-minting link the notification uses instead of the raw
        download_url (which is already stale by the time the card is viewed).
        New dicts, so the shared coordinator data the EventEntity reads is not
        mutated."""
        entry = self.coordinator.config_entry
        return {
            "events": [
                {**e, "clip_link": _clip_link_for(entry, e.get("video_event_id"))}
                for e in (self.coordinator.data or [])
            ]
        }


class TalkMetricsCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the camera-bridge's /control/metrics endpoint.

    Talkdown counters only move when someone triggers a talk, so this polls
    slowly -- it exists so a run of failures becomes visible (and automatable),
    not to watch a clip play.
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS talkdown metrics",
            update_interval=timedelta(seconds=TALK_METRICS_POLL_INTERVAL_SECONDS),
        )

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            payload = await bridge_get(self.hass, CAMERA_BRIDGE_METRICS_URL)
        except Exception as err:  # noqa: BLE001 - aiohttp/timeout variants
            raise UpdateFailed(f"camera bridge metrics unavailable: {err}") from err
        if payload is None:
            # Bridge hasn't written its control secret yet (normal right after a
            # restart) -- keep whatever we had rather than flapping to unavailable.
            return self.data or {}
        return payload.get("talk") or {}

    async def async_config_entry_first_refresh_soft(self) -> None:
        """First refresh that never blocks setup.

        Unlike async_config_entry_first_refresh, a bridge that is down (or older
        than this endpoint) must not abort the whole sensor platform -- these are
        diagnostics, so they just start out unavailable and recover on a later
        poll."""
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Initial talkdown metrics refresh failed; sensors start "
                         "unavailable", exc_info=True)


class _TalkMetricsEntity(CoordinatorEntity[TalkMetricsCoordinator], SensorEntity):
    """Shared plumbing: both talkdown sensors are integration-level diagnostics
    grouped under the same synthetic device as AuthHealthSensor."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: TalkMetricsCoordinator, config_entry: ConfigEntry,
                 key: str) -> None:
        super().__init__(coordinator)
        self._attr_unique_id = f"{config_entry.entry_id}_{key}"
        self._attr_translation_key = key
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{config_entry.entry_id}_integration")},
            name="TELUS Smart Home Integration",
            manufacturer="TELUS",
            entry_type=dr.DeviceEntryType.SERVICE,
        )

    @property
    def _metrics(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    @property
    def available(self) -> bool:
        return super().available and bool(self.coordinator.data)


class TalkdownStatusSensor(_TalkMetricsEntity):
    """How the most recent talk went: `idle`, `ok`, `stopped`, `rejected`, or
    `failed` (with the reason in an attribute).

    The state is deliberately coarse so it is easy to automate on -- the reason,
    counts and recent history ride along as attributes for a notification
    template or a dashboard.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["idle", "ok", "stopped", "rejected", "failed"]

    def __init__(self, coordinator: TalkMetricsCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator, config_entry, "talkdown_status")

    @property
    def native_value(self) -> str:
        recent = self._metrics.get("recent") or []
        if not recent:
            return "idle"
        outcome = str(recent[-1].get("outcome") or "idle")
        # "failed:<reason>" collapses to "failed"; the reason is an attribute.
        return outcome.split(":", 1)[0] if outcome.split(":", 1)[0] in self._attr_options else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._metrics
        recent = m.get("recent") or []
        last = recent[-1] if recent else {}
        return {
            "last_outcome": last.get("outcome"),
            "last_failure_reason": m.get("last_failure_reason"),
            "last_success_at": m.get("last_success_at"),
            "last_failure_at": m.get("last_failure_at"),
            "last_played_seconds": last.get("played_seconds"),
            "last_camera_id": last.get("camera_id"),
            # Talks per transport path (full_duplex / webrtc / srtp) -- which
            # audio path is actually being used is otherwise invisible from HA.
            "by_path": m.get("by_path"),
            # Trimmed: the full window is on the bridge; a handful is plenty for a
            # card and keeps the state machine's attribute payload small.
            "recent": recent[-5:],
        }


class TalkdownFailuresSensor(_TalkMetricsEntity):
    """Count of failed talks since the bridge started.

    Numeric and total_increasing so an automation can trigger on it going up --
    "the doorbell refused to speak" is exactly the kind of thing that otherwise
    goes unnoticed. It resets to 0 when the bridge restarts, which
    total_increasing handles as a reset rather than a spike.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: TalkMetricsCoordinator, config_entry: ConfigEntry) -> None:
        super().__init__(coordinator, config_entry, "talkdown_failures")

    @property
    def native_value(self) -> int:
        return int(self._metrics.get("failed") or 0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        m = self._metrics
        return {
            "attempts": m.get("attempts"),
            "succeeded": m.get("succeeded"),
            "stopped": m.get("stopped"),
            "rejected_in_progress": m.get("rejected_in_progress"),
            "failures_by_reason": m.get("failures_by_reason"),
            "by_endpoint": m.get("by_endpoint"),
            "by_codec": m.get("by_codec"),
            "renegotiations": m.get("renegotiations"),
            "audio_seconds_played": m.get("audio_seconds_played"),
            "bridge_uptime_seconds": m.get("uptime_seconds"),
        }


class StreamHealthCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Polls the camera-bridge's /control/status endpoint.

    `data` is the payload's "cameras" dict, keyed by camera id -- the same ids
    the camera platform gets from /control/cameras. The route answers from an
    in-memory dict the bridge's own camera loops maintain, entirely over
    loopback with zero alarm.com cost, which is why this polls at 30s where
    the TELUS-backed coordinators are deliberately slow.

    Also the truth source for binary_sensor.py's bridge-connectivity entity:
    `last_poll_ok` records whether the most recent completed poll actually got
    an answer out of the bridge (None until one completes at all). Its own
    flag rather than a read of last_update_success, because a missing control
    secret is a *successful* update here -- the sensors keep their last data,
    see _async_update_data -- while for connectivity it must read as "the
    bridge is not answering".
    """

    def __init__(self, hass: core.HomeAssistant, config_entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            LOGGER,
            config_entry=config_entry,
            name="TELUS camera stream health",
            update_interval=timedelta(seconds=STREAM_HEALTH_POLL_INTERVAL_SECONDS),
        )
        self.last_poll_ok: bool | None = None

    async def _async_update_data(self) -> dict[str, Any]:
        try:
            payload = await bridge_get(self.hass, CAMERA_BRIDGE_STATUS_URL)
        except Exception as err:  # noqa: BLE001 - aiohttp/timeout variants
            self.last_poll_ok = False
            raise UpdateFailed(f"camera bridge status unavailable: {err}") from err
        if payload is None:
            # Bridge hasn't written its control secret yet (normal right after a
            # restart) -- keep whatever we had rather than flapping to unavailable.
            self.last_poll_ok = False
            return self.data or {}
        self.last_poll_ok = True
        return payload.get("cameras") or {}

    async def async_config_entry_first_refresh_soft(self) -> None:
        """First refresh that never blocks setup.

        Same promise as TalkMetricsCoordinator's: a bridge that is down (or
        older than this endpoint) must not abort a whole platform -- these are
        diagnostics, so they start out unavailable (or, for the connectivity
        entity, report the outage) and recover on a later poll."""
        try:
            await self.async_refresh()
        except Exception:  # noqa: BLE001
            LOGGER.debug("Initial stream health refresh failed; stream-health "
                         "sensors start unavailable", exc_info=True)


async def async_get_stream_health_coordinator(
    hass: core.HomeAssistant, config_entry: ConfigEntry
) -> StreamHealthCoordinator:
    """Return this config entry's shared stream-health coordinator, creating it once.

    Same lazy-stash mechanism as async_get_arming_devices_coordinator /
    async_get_camera_settings_coordinator: created here by whichever of
    sensor.py / binary_sensor.py runs first, published to hass.data before any
    await, popped with the whole per-entry dict on unload so it needs no
    teardown of its own.

    Unlike those two, the soft first refresh IS awaited inline: it is one
    loopback request against an in-memory dict, bounded by bridge_client's 10s
    timeout -- nowhere near the 60s platform budget, with no live Alarm.com
    fan-out behind it -- and awaiting it means the per-camera sensors exist at
    setup time on the common path (bridge already up) rather than one poll
    interval later. The second platform to call this may still get the
    coordinator with no data yet, which both consumers tolerate: sensor.py
    adds entities from a listener, and the connectivity entity is unavailable
    until a poll attempt completes.
    """
    store: dict[str, Any] = hass.data[DOMAIN][config_entry.entry_id]

    if (coordinator := store.get(DATA_STREAM_HEALTH_COORDINATOR)) is not None:
        return coordinator

    coordinator = StreamHealthCoordinator(hass, config_entry)
    # Published before the refresh is awaited: platforms are set up
    # concurrently, so a suspension point between the lookup above and this
    # assignment would let binary_sensor.py build a second poller.
    store[DATA_STREAM_HEALTH_COORDINATOR] = coordinator
    await coordinator.async_config_entry_first_refresh_soft()
    return coordinator


class _StreamHealthEntity(CoordinatorEntity[StreamHealthCoordinator], SensorEntity):
    """Shared plumbing for the per-camera stream-health sensors.

    Attaches to the EXISTING bridge-served camera device by reusing the
    identifier camera.py registers it under (the same convention as
    CameraSettingsEntity, including the identifier-only DeviceInfo:
    name/model/firmware belong to camera.py, and repeating them here would let
    this entity fight it over the device's registry entry). unique_id and
    translation_key follow CameraSettingsEntity's shapes too, so these land on
    the same device card as the camera's other diagnostics.
    """

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: StreamHealthCoordinator, camera_id: str, key: str) -> None:
        """Initialize the entity for one camera."""
        super().__init__(coordinator)

        self._camera_id = camera_id
        self._attr_unique_id = f"{camera_id}_{key}"
        self._attr_translation_key = f"camera_{key}"
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, camera_id)})

    @property
    def _status(self) -> dict[str, Any] | None:
        """This camera's /control/status entry, or None once the bridge drops it."""
        return (self.coordinator.data or {}).get(self._camera_id)

    @property
    def available(self) -> bool:
        """Unavailable when the coordinator has no data at all, or the bridge
        stops reporting this specific camera."""
        return super().available and self._status is not None


class CameraStreamStateSensor(_StreamHealthEntity):
    """Where one camera's video pipeline currently is in its lifecycle:
    `connecting`, `streaming`, `reconnecting`, `no_media` (the camera
    negotiates sessions and delivers no frames while another camera streams
    through the same bridge -- a fault at the device, which is what an
    automation should page on) or `restart_deferred` (the bridge's escape
    hatch fired but restart_guard is holding the process).

    The pipeline's supporting numbers ride along as attributes rather than as
    more entities -- fps in particular is an attribute BY DESIGN: it is the
    rate the camera's publisher is currently DECLARED with. The bridge keeps
    it under review while the stream runs (the rate is a device setting that
    can change at any time) but it only ever moves on a publisher rebuild, so
    it is a stepwise configuration fact, not a live measurement worth a
    recorder series of its own.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STREAM_STATES

    def __init__(self, coordinator: StreamHealthCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, camera_id, "stream_state")

    @property
    def native_value(self) -> str | None:
        """The bridge-reported state, or None for one this build doesn't know.

        The vocabulary is owned by a separately-deployed bridge, and an ENUM
        sensor whose state is outside its own options errors on every single
        update -- so an unknown future state degrades to None (unknown) here,
        with the verbatim value still readable in raw_state below.
        """
        state = (self._status or {}).get("state")
        return state if state in STREAM_STATES else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status or {}
        return {
            "fps": status.get("fps"),
            # The DELIVERED rate (the bridge's last settled measurement),
            # against fps's "what ffmpeg was declared with"; the gap between
            # the two is padding or dropping before ffmpeg complains about it.
            # A confirmed change waiting for the rotation that applies it
            # rides in rate_pending, and duplicated_frame_warnings is the
            # encoder's own corroboration of a too-high declaration. All None/
            # absent on a bridge that predates them.
            "measured_fps": status.get("measured_fps"),
            "rate_pending": status.get("rate_pending"),
            "duplicated_frame_warnings": status.get("duplicated_frame_warnings"),
            "hardware_encode": status.get("hardware_encode"),
            "consecutive_failures": status.get("consecutive_failures"),
            "last_error": status.get("last_error"),
            "last_frame_at": status.get("last_frame_at"),
            # The device-side escalation's record (bridge device_kick.py): how
            # many kicks this bridge run has sent for the camera, when the last
            # one was, and what it reported ("ok: ...", "failed: ...", "not
            # sent: ..."). Attributes rather than entities because they only
            # change on the rare no_media run, and an automation triggering on
            # `last_device_kick` changing is the way the outcome reaches a
            # phone. None on a bridge that predates them.
            "device_kicks": status.get("device_kicks"),
            "last_device_kick_at": status.get("last_device_kick_at"),
            "last_device_kick": status.get("last_device_kick"),
            # The bridge's verbatim state, so a future value this build maps
            # to None is still visible rather than silently indistinguishable
            # from "no data".
            "raw_state": status.get("state"),
        }


class CameraDroppedFramesSensor(_StreamHealthEntity):
    """Lifetime count of frames the bridge dropped for one camera because the
    consumer fell behind (janus_client's latency-over-completeness drop).

    Numeric and total_increasing so an automation can trigger on it climbing --
    sustained drops are a quietly-degrading stream that otherwise only shows as
    throttled bridge log lines. It resets to 0 when the bridge restarts, which
    total_increasing handles as a reset rather than a spike (the same reasoning
    as TalkdownFailuresSensor). The bridge folds each ~165s session's counter
    into the total as the session ends, so the value lags the live session by
    up to one rotation.
    """

    _attr_state_class = SensorStateClass.TOTAL_INCREASING

    def __init__(self, coordinator: StreamHealthCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, camera_id, "dropped_frames")

    @property
    def native_value(self) -> int:
        return int((self._status or {}).get("dropped_frames") or 0)


class CameraAudioStateSensor(_StreamHealthEntity):
    """Whether one camera's audio is actually flowing right now.

    `not_consuming` (the bridge is not asked to consume this camera's audio),
    `no_audio_yet` (it is, and not one frame has ever decoded), `flowing`
    (the decoded-frame counter moved since the previous poll) or `stalled`
    (it did not).

    **Why a derived enum and not the raw counter.** `decoded_audio_frames` is
    already a perfectly good `total_increasing` sensor -- and it would be
    useless for the failure it exists to catch. The bridge's Opus decoder dying
    mid-session (see aiortc_opus_patch.py) does not produce an error, a state
    change or a dropped frame: the counter simply stops moving while `state`
    stays `streaming` and `last_frame_at` stays fresh, and the camera publishes
    silence. Home Assistant cannot trigger on "a number stopped changing" --
    there is no such trigger -- so a counter turns a hard failure into
    something a human has to notice on a graph, which is precisely how eleven
    consecutive silent doorbell sessions went unnoticed. Comparing the counter
    across polls here turns *stopped* into a state, which IS triggerable. The
    raw count, the per-poll delta and the observed `audio_format` ride along as
    attributes, so nothing the bridge reports is lost.

    A binary "audio healthy" was the other candidate and is rejected for the
    same reason CameraSolarPanelSensor is not a binary_sensor: there is no
    device class for it, so it renders as a bare On/Off, and it would have to
    fold "audio is switched off for this camera" and "audio is switched on and
    dead" into the same Off -- collapsing exactly the distinction an operator
    needs.

    **Every camera gets one, consuming or not.** A camera whose audio the
    bridge is not configured to consume (`JANUS_CONSUME_AUDIO` is per-camera
    and off by default) reports `not_consuming`. That is a real, honest,
    translated state, not a broken entity -- and it is the state that answers
    "why is there no audio?" for someone who did not know the knob was off.
    Creating the entity only for
    consuming cameras (the rule the per-camera diagnostics and solar sensors
    follow) would be wrong here for two reasons: entity creation is sticky, so
    an entity that appears and disappears with a bridge env var breaks any
    automation referencing it, and the alternative to appearing is a camera
    with no audio entity at all, which reads as "audio is fine" exactly like
    the gap this closes. The diagnostics/solar rule applies to questions the
    hardware cannot answer at all (no battery, no solar); "audio is switched
    off" is an answer.

    A bridge too old to report `audio_consumed` gets `unknown` rather than a
    fabricated `not_consuming`, the same degradation CameraStreamStateSensor
    uses for a state vocabulary it does not know.
    """

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = AUDIO_STATES
    _attr_icon = "mdi:waveform"

    def __init__(self, coordinator: StreamHealthCoordinator, camera_id: str) -> None:
        super().__init__(coordinator, camera_id, "audio_state")

        # Everything below is bookkeeping for the across-polls comparison, and
        # is mutated ONLY in _refresh_derived (called once per coordinator
        # update). native_value/extra_state_attributes are properties Home
        # Assistant may read any number of times per update, so deriving the
        # state inside one of them would compare the counter against itself and
        # report `stalled` for a perfectly healthy camera.
        self._audio_state: str | None = None
        self._previous_frames: int | None = None
        self._frames_delta: int | None = None
        self._last_audio_at: str | None = None
        # The payload the comparison last advanced on, held by reference so the
        # same poll can never be folded in twice (which would compare a counter
        # against itself and read as `stalled`). Belt and braces next to the
        # once-per-update contract of _handle_coordinator_update.
        self._seen_payload: Any = None

        self._refresh_derived()

    @core.callback
    def _handle_coordinator_update(self) -> None:
        """Fold this poll into the derived state, then write it out."""
        self._refresh_derived()
        super()._handle_coordinator_update()

    @core.callback
    def _refresh_derived(self) -> None:
        """Advance the comparison with this poll's counter.

        Everything here is deliberately conservative about what counts as a
        comparable poll, because a false `stalled` on a healthy camera would
        make the whole entity un-automatable:

        * A failed poll, or one the bridge did not answer (its control secret
          not on the shared mount yet, normal for a few seconds after a bridge
          restart), leaves the coordinator holding the PREVIOUS payload. That
          identical data must not read as "the counter did not move" -- so the
          remembered count and the derived state are both left untouched.
        * A counter that went DOWN is the bridge process restarting (every
          number in /control/status resets with it), not a stall; frames have
          demonstrably arrived since that restart, so it reads `flowing`.
        * The first comparable poll has nothing to compare against. A camera
          already at zero is unambiguous (`no_audio_yet`), but one with frames
          already on the clock is genuinely unknown for one poll interval, and
          says so rather than guessing.
        """
        status = self._status
        if (
            status is None
            or not self.coordinator.last_update_success
            or self.coordinator.last_poll_ok is not True
            # The same payload object twice is not a second observation.
            or self.coordinator.data is self._seen_payload
        ):
            return
        self._seen_payload = self.coordinator.data

        if "audio_consumed" not in status:
            # A bridge predating these fields. Not knowable, not fabricated.
            self._audio_state = None
            self._previous_frames = None
            self._frames_delta = None
            return

        if not status.get("audio_consumed"):
            self._audio_state = AUDIO_NOT_CONSUMING
            # Dropped so that re-enabling audio (which restarts the bridge, and
            # with it the counter) starts a fresh comparison instead of
            # measuring across the gap.
            self._previous_frames = None
            self._frames_delta = None
            return

        frames = int(status.get("decoded_audio_frames") or 0)
        previous = self._previous_frames
        self._previous_frames = frames

        if previous is None:
            self._frames_delta = None
            self._audio_state = AUDIO_NO_AUDIO_YET if frames == 0 else None
            return

        # A decrease is a counter reset, which makes the delta meaningless --
        # report no delta rather than a negative one.
        self._frames_delta = frames - previous if frames >= previous else None

        if frames != previous:
            self._audio_state = AUDIO_FLOWING if frames > 0 else AUDIO_NO_AUDIO_YET
            if frames > 0:
                self._last_audio_at = dt_util.utcnow().isoformat()
        elif frames == 0:
            self._audio_state = AUDIO_NO_AUDIO_YET
        else:
            self._audio_state = AUDIO_STALLED

    @property
    def native_value(self) -> str | None:
        """The derived audio state, or None until it can honestly be told.

        `stalled` reflects ONE poll window (30 s). Session rotation and a
        reconnect are both long enough to span one, so an automation should
        require the state to persist (`for:`) rather than fire on the first
        sample -- see docs/ha-integration.md.
        """
        return self._audio_state

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        status = self._status or {}
        return {
            "audio_consumed": status.get("audio_consumed"),
            # The raw lifetime counter, so the underlying number is never
            # hidden behind the derivation.
            "decoded_audio_frames": status.get("decoded_audio_frames"),
            # How far it moved over the last poll interval; None on the first
            # poll, on a poll the bridge did not answer, and across a counter
            # reset.
            "decoded_audio_frames_last_poll": self._frames_delta,
            # Null until this camera has decoded a frame -- the bridge reports
            # what it OBSERVED, never the format the publisher was launched
            # with, so a value here is proof audio decoded at least once.
            "audio_format": status.get("audio_format"),
            # Which decoder those frames came from ("pcmu"/"opus"), derived by
            # the bridge from the observed frame shape -- the offer advertises
            # Opus while this account's cameras send G.711 mu-law, and this is
            # where that verdict surfaces. Null until a frame has decoded, and
            # None (absent, not fabricated) on a bridge too old to report it,
            # the same rule as audio_dropped below.
            "audio_codec": status.get("audio_codec"),
            "dropped_audio_frames": status.get("dropped_audio_frames"),
            # When the counter was last seen moving, as observed by this poller
            # (so it starts empty after a Home Assistant restart, not at the
            # bridge's own first frame).
            "last_audio_at": self._last_audio_at,
            # Why audio stopped, when the bridge stopped it deliberately. The
            # bridge drops a wedged audio pipe and keeps publishing video rather
            # than restarting the encoder, which reads here as `stalled` --
            # correct, because the pump really has stopped feeding audio, but
            # indistinguishable on its own from audio that merely went quiet.
            # These two say which it was without a trip to the container log.
            # Deliberately NOT a fifth enum state: `stalled` is what an
            # automation should act on either way, and splitting it would break
            # any automation already keyed on it.
            "audio_dropped": status.get("audio_dropped"),
            "audio_dropped_reason": status.get("audio_dropped_reason"),
        }


def _scalar_or_none(value: Any) -> str | float | int | None:
    """Return `value` if Home Assistant can hold it as a state, else None.

    A sensor state must be a scalar, and these fields carry whatever TELUS
    reported -- the bridge models three of them and passes the rest through
    under `raw`. A dict arriving in a modelled field must not turn into a
    permanent stream of "invalid state" errors in the log.
    """
    return value if isinstance(value, (str, int, float)) else None


class _CameraDiagnosticSensor(CameraSettingsEntity, SensorEntity):
    """Shared plumbing for the per-camera diagnostic sensors.

    Subclasses set `_field` (the key in the bridge's /diagnostics body) and are
    registered in CAMERA_DIAGNOSTIC_SENSORS below.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC

    _field: str

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the sensor for one camera."""
        super().__init__(coordinator, camera_id, self._field)

    @property
    def native_value(self) -> str | float | int | None:
        """This camera's current value for the field, or None if unusable."""
        return _scalar_or_none(self._diagnostics.get(self._field))

    @property
    def _is_numeric(self) -> bool:
        """Whether the current value is a number HA can graph.

        `bool` is excluded on purpose: it is an `int` subclass, and a True/False
        status reported as a measurement would be graphed as 1/0.
        """
        value = self._diagnostics.get(self._field)
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    @property
    def state_class(self) -> SensorStateClass | None:
        """MEASUREMENT only while the value is numeric.

        Declared per-update rather than as a class attribute because these
        endpoints may answer with a word ("Good") on one camera and a number on
        another; a numeric state class over a string state makes the recorder
        log an error on every single update.
        """
        return SensorStateClass.MEASUREMENT if self._is_numeric else None


class CameraSignalStrengthSensor(_CameraDiagnosticSensor):
    """Wi-Fi signal strength for one bridge-served TELUS camera.

    Only created for cameras that report one -- a wired camera reports null.

    The unit is decided per camera from what the BRIDGE says it read, not
    assumed. alarm.com's `normalizedSignalStrength` is a 0-100 score (the bars
    the portal draws), so it is labelled a percentage; the other key spellings
    the bridge still accepts for camera models this was never tested against may
    be real dBm, and those get no unit at all.

    Why not just pick one: `SensorDeviceClass.SIGNAL_STRENGTH` means dBm, and a
    0-100 score labelled dBm reads as "58 dBm" -- real Wi-Fi dBm is negative --
    while Home Assistant records long-term statistics against the declared unit,
    so a wrong one poisons the history rather than just the display. Refusing to
    label anything (which this did before the bridge reported the key) was safe
    but lost real information for the case that is actually known.
    """

    _field = "signal_strength"
    _attr_icon = "mdi:wifi"

    # The one key whose unit is known. Anything else stays unlabelled.
    _NORMALIZED_KEY = "normalizedSignalStrength"

    @property
    def native_unit_of_measurement(self) -> str | None:
        """PERCENTAGE for the normalized score, otherwise nothing."""
        diagnostics = self._diagnostics
        if diagnostics.get("signal_strength_key") == self._NORMALIZED_KEY:
            return PERCENTAGE
        return None


class CameraBatterySensor(_CameraDiagnosticSensor):
    """Battery level for one bridge-served TELUS camera.

    Only created for cameras that report a battery at all -- a mains-powered
    doorbell has none, and an always-unknown battery entity on it is noise.
    """

    _field = "battery"

    @property
    def device_class(self) -> SensorDeviceClass | None:
        """BATTERY only for a numeric value, since that class implies a percentage."""
        return SensorDeviceClass.BATTERY if self._is_numeric else None

    @property
    def native_unit_of_measurement(self) -> str | None:
        """Percent for a numeric value; nothing for a status word like "Normal"."""
        return PERCENTAGE if self._is_numeric else None


class CameraDeviceStatusSensor(_CameraDiagnosticSensor):
    """Device status for one bridge-served TELUS camera, as the settings screen shows it."""

    _field = "device_status"
    _attr_icon = "mdi:cctv"

    @property
    def state_class(self) -> SensorStateClass | None:
        """Always None -- a status is a label, never a measurement."""
        return None


class CameraSolarPanelSensor(CameraSettingsEntity, SensorEntity):
    """Whether one bridge-served TELUS camera reports a solar panel attached (E39).

    A SENSOR rather than a binary_sensor, which is worth justifying because the
    underlying field is a plain boolean and binary_sensor is the reflexive choice
    for one.

    Home Assistant renders a binary_sensor's two states through its device class,
    and there is no device class for "is a solar accessory attached". Without one
    it reads as a bare On/Off, which for a wiring fact says the wrong thing -- On
    suggests something is powered up or generating, when all the camera has told
    us is that an accessory is present. An enum sensor with translated states
    says Connected / Not connected, which is exactly and only what the flag
    means. The device classes that come closest (`plug`, `power`, `connectivity`)
    all describe a live electrical or network condition rather than an
    installation, so borrowing one would trade a vague label for a wrong one.

    The secondary reason is structural: every bridge-coordinator-backed camera
    entity in this integration lives here, and binary_sensor.py is exclusively
    the pyalarmdotcomajax device-model platform -- it has no coordinator wiring
    of this kind at all, so putting the first one there would duplicate the
    plumbing rather than reuse it.

    No state class and no statistics: this does not change unless someone
    physically installs or removes a panel.
    """

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_icon = "mdi:solar-panel"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = SOLAR_PANEL_STATES

    def __init__(self, coordinator: CameraSettingsCoordinator, camera_id: str) -> None:
        """Initialize the solar-panel sensor for one camera."""
        super().__init__(coordinator, camera_id, "solar_panel")

    @property
    def _solar(self) -> dict[str, Any]:
        """The camera's solar settings section."""
        return self._settings.get("solar") or {}

    @property
    def available(self) -> bool:
        """Also unavailable if the camera stops reporting the feature.

        Gated on the support flag, never on the value: a camera that cannot
        answer the question sends the same `connected: false` as one that
        answered "no".
        """
        return super().available and bool(self._solar.get("supports_connected"))

    @property
    def native_value(self) -> str | None:
        """"connected" / "not_connected", or None if the camera has not said."""
        # `is not None`, not truthiness: False is a real answer here ("supported,
        # and nothing is attached") and must not read as unknown the way a bridge
        # too old to send the section does.
        value = self._solar.get("connected")
        if value is None:
            return None
        return SOLAR_PANEL_CONNECTED if value else SOLAR_PANEL_NOT_CONNECTED


# Field in the bridge's /diagnostics body -> the sensor that presents it. Used
# by async_setup_entry to create only the sensors a given camera reports.
CAMERA_DIAGNOSTIC_SENSORS: dict[str, type[_CameraDiagnosticSensor]] = {
    "signal_strength": CameraSignalStrengthSensor,
    "battery": CameraBatterySensor,
    "device_status": CameraDeviceStatusSensor,
}
