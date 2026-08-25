"""Minimal stand-ins for the Home Assistant APIs the integration imports.

`custom_components/telus_smarthome/` imports `homeassistant.*` at module scope,
so nothing in it can be imported -- let alone exercised -- without something
answering to that name. This module is that something: it registers a small
package tree in `sys.modules` before the integration is imported, so the REAL
integration modules load and the REAL derivations run.

**Why stubs instead of installing Home Assistant.** `pip install homeassistant`
does resolve on the interpreter CI pins, but it pulls in the order of a hundred
transitive packages -- grpcio, boto3, pillow, cryptography, bleak/dbus-fast,
zeroconf, SQLAlchemy, and PyRIC, which has no wheel and builds from source -- to
supply the handful of base classes below. Exercising entity behaviour for real
on top of that needs `pytest-homeassistant-custom-component`, which pins one
exact Home Assistant version and would put this repo on a version-lock treadmill
it gets nothing back from. The logic worth pinning here is pure: a counter
compared across polls, a string classifier, an id-set dedup. None of it needs an
event loop, an entity registry or a state machine, and the bridge's thirteen
suites already establish the plain-script convention this follows.

**The limit of what this proves, stated plainly.** These stubs assert nothing
about Home Assistant's real API. If upstream changes what `CoordinatorEntity`
promises, these tests keep reporting green -- they pin OUR derivations, not our
compatibility with Home Assistant. Two things narrow that gap deliberately:

* Every stub below reproduces the upstream behaviour the integration actually
  leans on, and nothing else -- `CoordinatorEntity.available` really is
  `coordinator.last_update_success`, and `EventEntity._trigger_event` really
  does reject an event type outside `event_types`, because a test that fired an
  undeclared type would otherwise pass here and raise in production.
* Nothing here is auto-magic. There is no `MagicMock`, no `__getattr__` that
  invents attributes on demand. An integration module that starts using a name
  this file does not define fails with a loud ImportError/AttributeError rather
  than silently exercising a fiction.

`pyalarmdotcomajax` gets the same treatment for the three attributes `const.py`
reads off it. `controller.py` is stubbed WHOLESALE, because importing it applies
the websocket monkeypatch and drags the real library in; `sensor.py` uses it as
a type annotation only, and the patch itself is already guarded at import time
and exercised by CI's `check_patch.py` job.

Usage, from a suite in this directory:

    import ha_stubs
    ha_stubs.install()
    from custom_components.telus_smarthome import sensor
"""
from __future__ import annotations

import enum
import pathlib
import sys
import types
from datetime import datetime, timezone
from typing import Any

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------
# Entity base classes
# --------------------------------------------------------------------------

class Entity:
    """Stand-in for `homeassistant.helpers.entity.Entity`.

    Only the parts the integration touches: the `_attr_*` convention, the
    `available` default, and a `async_write_ha_state` that COUNTS rather than
    writes. The count is load-bearing for the doorbell tests -- the entity
    deliberately writes state once per press rather than once per poll, and
    "two presses in one poll collapsed into one state_changed" is exactly the
    regression that would otherwise be invisible.
    """

    _attr_has_entity_name = False
    _attr_entity_category = None
    _attr_device_info = None
    _attr_unique_id = None
    _attr_translation_key = None
    _attr_icon = None
    _attr_device_class = None
    _attr_options = None
    _attr_state_class = None

    def __init__(self) -> None:
        self.state_writes = 0

    def __class_getitem__(cls, _item: Any) -> Any:
        # The integration subscripts these (CoordinatorEntity[X]); upstream
        # they are Generic. Returning the class itself is enough for a base.
        return cls

    @property
    def available(self) -> bool:
        return True

    def async_write_ha_state(self) -> None:
        self.state_writes += 1


class CoordinatorEntity(Entity):
    """Stand-in for `homeassistant.helpers.update_coordinator.CoordinatorEntity`.

    `available` is upstream's real rule (`coordinator.last_update_success`), and
    the default `_handle_coordinator_update` is upstream's real body (write the
    state out) -- both are overridden and called via `super()` by the entities
    under test, so getting them wrong would silently change what the tests mean.
    """

    def __init__(self, coordinator: Any, context: Any = None) -> None:
        super().__init__()
        self.coordinator = coordinator
        self.coordinator_context = context

    @property
    def available(self) -> bool:
        return bool(self.coordinator.last_update_success)

    def _handle_coordinator_update(self) -> None:
        self.async_write_ha_state()


class DataUpdateCoordinator:
    """Stand-in for `homeassistant.helpers.update_coordinator.DataUpdateCoordinator`.

    The integration's coordinators subclass this and call `super().__init__`
    with hass/logger/name/update_interval, so the signature has to accept them.
    No polling happens here: the suites drive `data` directly, which is what a
    poll would have set anyway.
    """

    def __init__(
        self,
        hass: Any,
        logger: Any = None,
        *,
        config_entry: Any = None,
        name: str = "",
        update_interval: Any = None,
        update_method: Any = None,
        always_update: bool = True,
    ) -> None:
        self.hass = hass
        self.logger = logger
        self.config_entry = config_entry
        self.name = name
        self.update_interval = update_interval
        self.data: Any = None
        self.last_update_success = True

    def __class_getitem__(cls, _item: Any) -> Any:
        return cls

    def async_add_listener(self, update_callback: Any, context: Any = None) -> Any:
        return lambda: None


class UpdateFailed(Exception):
    """Stand-in for the coordinator's own failure signal."""


# --------------------------------------------------------------------------
# Platform entity classes
# --------------------------------------------------------------------------

class SensorEntity(Entity):
    """Stand-in for `homeassistant.components.sensor.SensorEntity`."""

    @property
    def native_value(self) -> Any:
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return {}


class EventEntity(Entity):
    """Stand-in for `homeassistant.components.event.EventEntity`.

    `_trigger_event` records instead of writing, and reproduces upstream's
    validation: an event type outside `event_types` raises. That refusal is the
    reason this is not just a list append -- an entity that fired a type it
    never declared would pass a recording-only stub and raise ValueError inside
    Home Assistant.
    """

    _attr_event_types: list[str] = []

    def __init__(self) -> None:
        super().__init__()
        self.triggered_events: list[tuple[str, dict[str, Any]]] = []

    @property
    def event_types(self) -> list[str]:
        return self._attr_event_types

    def _trigger_event(self, event_type: str, event_attributes: dict[str, Any] | None = None) -> None:
        if event_type not in self.event_types:
            raise ValueError(f"Invalid event type {event_type} for {self}")
        self.triggered_events.append((event_type, dict(event_attributes or {})))


class Camera(Entity):
    """Stand-in for `homeassistant.components.camera.Camera`."""


class HomeAssistantView:
    """Stand-in for `homeassistant.components.http.HomeAssistantView`."""

    url: str | None = None
    name: str | None = None
    requires_auth = True


# --------------------------------------------------------------------------
# Enums and simple values
# --------------------------------------------------------------------------

class SensorDeviceClass(enum.StrEnum):
    BATTERY = "battery"
    ENUM = "enum"
    SIGNAL_STRENGTH = "signal_strength"


class SensorStateClass(enum.StrEnum):
    MEASUREMENT = "measurement"
    TOTAL_INCREASING = "total_increasing"


class EventDeviceClass(enum.StrEnum):
    DOORBELL = "doorbell"
    MOTION = "motion"


class EntityCategory(enum.StrEnum):
    CONFIG = "config"
    DIAGNOSTIC = "diagnostic"


class CameraEntityFeature(enum.IntFlag):
    ON_OFF = 1
    STREAM = 2


class DeviceEntryType(enum.StrEnum):
    SERVICE = "service"


class Platform(enum.StrEnum):
    ALARM_CONTROL_PANEL = "alarm_control_panel"
    BINARY_SENSOR = "binary_sensor"
    BUTTON = "button"
    CAMERA = "camera"
    CLIMATE = "climate"
    COVER = "cover"
    EVENT = "event"
    LIGHT = "light"
    LOCK = "lock"
    NUMBER = "number"
    SELECT = "select"
    SENSOR = "sensor"
    SWITCH = "switch"


class HomeAssistant:
    """Stand-in for the core object. Passed around, never called into here."""


class ConfigEntry:
    """Stand-in for a config entry; the suites build their own fakes."""


class HomeAssistantError(Exception):
    """Stand-in for `homeassistant.exceptions.HomeAssistantError`."""


class DeviceInfo(dict):
    """Stand-in for the `DeviceInfo` TypedDict (a plain dict upstream too)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


def callback(func: Any) -> Any:
    """Stand-in for `homeassistant.core.callback` -- upstream tags, never wraps."""
    func._hass_callback = True  # noqa: SLF001 - mirrors upstream's own marker
    return func


def _no_session(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(
        "a test reached the aiohttp session helper; these suites are offline "
        "and must not make a request"
    )


# --------------------------------------------------------------------------
# homeassistant.util.dt
# --------------------------------------------------------------------------

def parse_datetime(value: str) -> datetime | None:
    """Stand-in for `dt_util.parse_datetime`.

    Upstream tries `fromisoformat` first and falls back to a regex for the
    stragglers; both forms TELUS emits (with and without fractional seconds,
    trailing `Z`) go through `fromisoformat` on the interpreters this repo
    targets, so this is the same code path for the inputs that matter. Returns
    None on anything unparseable, exactly as upstream does -- `_event_time`
    depends on that rather than on an exception.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def utc_from_timestamp(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc)


def as_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


# --------------------------------------------------------------------------
# Installation
# --------------------------------------------------------------------------

def _module(name: str, **attrs: Any) -> types.ModuleType:
    mod = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(mod, key, value)
    sys.modules[name] = mod
    return mod


def _package(name: str, path: pathlib.Path) -> types.ModuleType:
    """Register `name` as a package whose submodules load from `path`.

    Used to reach the integration's modules without executing its `__init__`.
    """
    mod = types.ModuleType(name)
    mod.__path__ = [str(path)]  # type: ignore[attr-defined]
    mod.__package__ = name
    sys.modules[name] = mod
    return mod


_INSTALLED = False


def install() -> None:
    """Register the stub tree in `sys.modules` and put the repo on `sys.path`.

    Idempotent, so a suite importing another suite's helpers cannot install
    twice and hand out two different `DeviceInfo` classes.
    """
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    # The integration package is seeded by hand, with `__path__` pointing at the
    # real directory: submodules below it are then found and imported for real,
    # but `custom_components/telus_smarthome/__init__.py` never runs. That file
    # applies all three pyalarmdotcomajax monkeypatches on import and needs the
    # real library present to do it -- work that belongs to CI's check_patch.py
    # job, and that would otherwise have to succeed before a single pure
    # derivation could be tested.
    _package("custom_components", REPO_ROOT / "custom_components")
    _package(
        "custom_components.telus_smarthome",
        REPO_ROOT / "custom_components" / "telus_smarthome",
    )

    core = _module(
        "homeassistant.core",
        HomeAssistant=HomeAssistant,
        callback=callback,
        CALLBACK_TYPE=Any,
    )
    util_dt = _module(
        "homeassistant.util.dt",
        parse_datetime=parse_datetime,
        utcnow=utcnow,
        utc_from_timestamp=utc_from_timestamp,
        as_utc=as_utc,
        UTC=timezone.utc,
    )
    _module("homeassistant.util", dt=util_dt)
    _module("homeassistant", core=core)

    _module("homeassistant.const", PERCENTAGE="%", EntityCategory=EntityCategory,
            Platform=Platform, CONF_PASSWORD="password", CONF_USERNAME="username")
    _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
            ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (HomeAssistantError,), {}),
            ConfigEntryNotReady=type("ConfigEntryNotReady", (HomeAssistantError,), {}))
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry)

    _module("homeassistant.components")
    _module("homeassistant.components.sensor", SensorEntity=SensorEntity,
            SensorDeviceClass=SensorDeviceClass, SensorStateClass=SensorStateClass)
    _module("homeassistant.components.event", EventEntity=EventEntity,
            EventDeviceClass=EventDeviceClass)
    _module("homeassistant.components.camera", Camera=Camera,
            CameraEntityFeature=CameraEntityFeature)
    _module("homeassistant.components.http", HomeAssistantView=HomeAssistantView)

    device_registry = _module(
        "homeassistant.helpers.device_registry",
        DeviceEntryType=DeviceEntryType,
        CONNECTION_NETWORK_MAC="mac",
    )
    _module("homeassistant.helpers", device_registry=device_registry)
    _module("homeassistant.helpers.entity", Entity=Entity, DeviceInfo=DeviceInfo)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=Any,
            DiscoveryInfoType=Any)
    _module("homeassistant.helpers.update_coordinator",
            CoordinatorEntity=CoordinatorEntity,
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=UpdateFailed)
    _module("homeassistant.helpers.aiohttp_client",
            async_get_clientsession=_no_session,
            async_create_clientsession=_no_session)
    _module("homeassistant.helpers.event", async_track_time_interval=_no_session)

    # `const.py` reads exactly these three names off pyalarmdotcomajax. Anything
    # else it grows will fail loudly here rather than quietly resolve.
    lib_sensor_subtype = enum.Enum(
        "Subtype", ["MOBILE_PHONE", "PANEL_IMAGE_SENSOR", "FIXED_PANIC"]
    )
    lib_sensor = type("Sensor", (), {"Subtype": lib_sensor_subtype})
    _module("pyalarmdotcomajax")
    _module("pyalarmdotcomajax.const", ATTR_STATE_TEXT="state_text",
            ATTR_MAC_ADDRESS="mac_address")
    _module("pyalarmdotcomajax.devices")
    _module("pyalarmdotcomajax.devices.sensor", Sensor=lib_sensor)

    # controller.py applies the websocket monkeypatch at import and needs the
    # real library to do it. sensor.py only names the class in an annotation.
    _module("custom_components.telus_smarthome.controller",
            AlarmIntegrationController=type("AlarmIntegrationController", (), {}))
