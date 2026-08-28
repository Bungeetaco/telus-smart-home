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

import asyncio
import dataclasses
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
    # Upstream Entity resolves its registry DeviceEntry once added to hass and
    # exposes it as `device_entry`, None until then; None is the honest default
    # for entities that are never "added" here.
    device_entry = None

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
        self._listeners: list[Any] = []
        # How many times a caller ASKED for a refresh (async_request_refresh).
        # Upstream debounces and schedules; the suites only care that the ask
        # happened, so it is recorded rather than acted on.
        self.refresh_requests = 0

    def __class_getitem__(cls, _item: Any) -> Any:
        return cls

    def async_add_listener(self, update_callback: Any, context: Any = None) -> Any:
        """Upstream-exact in the part the suites lean on: the callback fires on
        every data update, and the returned callable unsubscribes it."""
        self._listeners.append(update_callback)

        def _unsub() -> None:
            if update_callback in self._listeners:
                self._listeners.remove(update_callback)

        return _unsub

    def async_update_listeners(self) -> None:
        for update_callback in list(self._listeners):
            update_callback()

    def async_set_updated_data(self, data: Any) -> None:
        """Upstream-exact: adopt the data, mark the poll successful, notify."""
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    async def async_refresh(self) -> None:
        """Upstream's refresh reduced to what the suites exercise: run
        _async_update_data, record success or failure without propagating
        (upstream logs and swallows), and notify listeners either way --
        upstream notifies after every refresh attempt, success or not."""
        try:
            data = await self._async_update_data()
        except Exception:  # noqa: BLE001 - upstream swallows every refresh error
            self.last_update_success = False
        else:
            self.data = data
            self.last_update_success = True
        self.async_update_listeners()

    async def async_request_refresh(self) -> None:
        """Records the ask instead of scheduling one (see refresh_requests)."""
        self.refresh_requests += 1

    async def _async_update_data(self) -> Any:
        raise NotImplementedError


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
    """Stand-in for the `DeviceInfo` TypedDict (a plain dict upstream too).

    Accepts a positional mapping as well as keywords, exactly as a TypedDict
    constructor does -- base_device.py builds the mapping first and passes it
    positionally.
    """

    def __init__(self, mapping: Any = None, **kwargs: Any) -> None:
        super().__init__(dict(mapping or {}), **kwargs)


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
# Platform entity classes and descriptions (binary_sensor / number / select /
# switch), added for the E66 suites
# --------------------------------------------------------------------------

class BinarySensorEntity(Entity):
    """Stand-in for `homeassistant.components.binary_sensor.BinarySensorEntity`."""

    @property
    def is_on(self) -> bool | None:
        return None


class NumberEntity(Entity):
    """Stand-in for `homeassistant.components.number.NumberEntity`.

    The entities under test define their own native_* properties; nothing
    reads upstream's defaulting logic, so none of it is reproduced.
    """


class SelectEntity(Entity):
    """Stand-in for `homeassistant.components.select.SelectEntity`."""


class SwitchEntity(Entity):
    """Stand-in for `homeassistant.components.switch.SwitchEntity`."""


@dataclasses.dataclass
class EntityDescription:
    """Stand-in for `homeassistant.helpers.entity.EntityDescription`.

    Upstream this is a kw_only dataclass behind a frozen-or-thawed metaclass so
    integrations may subclass it with their own plain dataclass mixins; a plain
    mutable dataclass here preserves exactly that subclassing behaviour.
    Only the fields the integration constructs are carried.
    """

    key: str
    device_class: Any = None
    entity_category: Any = None
    has_entity_name: bool = False
    icon: str | None = None
    name: Any = None
    translation_key: str | None = None


@dataclasses.dataclass
class BinarySensorEntityDescription(EntityDescription):
    """Stand-in for the binary_sensor platform's entity description."""


class BinarySensorDeviceClass(enum.StrEnum):
    BATTERY = "battery"
    CO = "carbon_monoxide"
    COLD = "cold"
    CONNECTIVITY = "connectivity"
    DOOR = "door"
    GARAGE_DOOR = "garage_door"
    MOISTURE = "moisture"
    MOTION = "motion"
    PROBLEM = "problem"
    SAFETY = "safety"
    SMOKE = "smoke"
    VIBRATION = "vibration"
    WINDOW = "window"


class NumberMode(enum.StrEnum):
    AUTO = "auto"
    BOX = "box"
    SLIDER = "slider"


class SwitchDeviceClass(enum.StrEnum):
    OUTLET = "outlet"
    SWITCH = "switch"


class ServiceCall:
    """Stand-in for `homeassistant.core.ServiceCall`; handlers read only .data."""

    def __init__(self, domain: str = "", service: str = "", data: Any = None) -> None:
        self.domain = domain
        self.service = service
        self.data = dict(data or {})


class Event:
    """Stand-in for `homeassistant.core.Event`."""

    def __init__(self, event_type: str = "", data: Any = None) -> None:
        self.event_type = event_type
        self.data = dict(data or {})


# --------------------------------------------------------------------------
# voluptuous stand-in
#
# CI installs aiohttp and nothing else (E63's deliberate trade), so the slice
# of voluptuous the service handlers apply to their own call data is
# reproduced here. Everything below mirrors real voluptuous behaviour the
# handlers lean on and is upstream-exact in those parts: dict schemas refuse
# unknown keys (PREVENT_EXTRA is the default), a missing Required key raises,
# an absent Optional with a default gets it (callable defaults invoked), at
# most one key of an Exclusive group may be present, and each present value is
# passed through its validator callable. Nothing else of voluptuous exists, so
# a handler growing a construct this file does not define fails loudly.
# --------------------------------------------------------------------------

class VolInvalid(Exception):
    """Stand-in for `voluptuous.Invalid`."""


_VOL_UNDEFINED = object()


class _VolMarker:
    def __init__(self, schema: Any, default: Any = _VOL_UNDEFINED, msg: Any = None) -> None:
        self.schema = schema
        self.default = default

    def __str__(self) -> str:
        return str(self.schema)


class VolRequired(_VolMarker):
    """Stand-in for `voluptuous.Required`."""


class VolOptional(_VolMarker):
    """Stand-in for `voluptuous.Optional`."""


class VolExclusive(_VolMarker):
    """Stand-in for `voluptuous.Exclusive` (at most one key per group)."""

    def __init__(self, schema: Any, group_of_exclusion: str, msg: Any = None) -> None:
        super().__init__(schema)
        self.group_of_exclusion = group_of_exclusion


class VolSchema:
    """Stand-in for `voluptuous.Schema` -- see the section comment above."""

    def __init__(self, schema: Any, extra: Any = None) -> None:
        self.schema = schema

    def __call__(self, data: Any) -> Any:
        if not isinstance(self.schema, dict):
            return data
        if not isinstance(data, dict):
            raise VolInvalid("expected a dictionary")

        known: dict[Any, tuple[Any, Any]] = {}
        for marker, validator in self.schema.items():
            key = marker.schema if isinstance(marker, _VolMarker) else marker
            known[key] = (marker, validator)

        for key in data:
            if key not in known:
                raise VolInvalid(f"extra keys not allowed @ data[{key!r}]")

        out: dict[Any, Any] = {}
        groups_seen: dict[str, Any] = {}
        for key, (marker, validator) in known.items():
            if key in data:
                if isinstance(marker, VolExclusive):
                    if marker.group_of_exclusion in groups_seen:
                        raise VolInvalid(
                            "two or more values in the same group of exclusion "
                            f"{marker.group_of_exclusion!r}")
                    groups_seen[marker.group_of_exclusion] = key
                out[key] = validator(data[key]) if callable(validator) else data[key]
            elif isinstance(marker, VolRequired):
                if marker.default is not _VOL_UNDEFINED:
                    out[key] = marker.default() if callable(marker.default) else marker.default
                else:
                    raise VolInvalid(f"required key not provided @ data[{key!r}]")
            elif isinstance(marker, VolOptional) and marker.default is not _VOL_UNDEFINED:
                out[key] = marker.default() if callable(marker.default) else marker.default
        return out


# --------------------------------------------------------------------------
# homeassistant.helpers.config_validation (cv) stand-ins. Each mirrors the
# upstream validator's behaviour in the part the handlers lean on, noted per
# function; each is upstream-exact for the inputs the suites drive.
# --------------------------------------------------------------------------

def cv_string(value: Any) -> str:
    """Upstream refuses None and container types, then coerces with str()."""
    if value is None:
        raise VolInvalid("string value is None")
    if isinstance(value, (list, dict)):
        raise VolInvalid("value should be a string")
    return str(value)


def cv_boolean(value: Any) -> bool:
    """Upstream accepts bool and the usual true/false strings; refuses the rest."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.lower().strip()
        if lowered in ("1", "true", "yes", "on", "enable"):
            return True
        if lowered in ("0", "false", "no", "off", "disable"):
            return False
    raise VolInvalid(f"invalid boolean value {value!r}")


def cv_entity_id(value: Any) -> str:
    """Upstream lowercases and requires the domain.object shape; a LIST is
    refused (which is what keeps unpair's single-entity schema single)."""
    value = cv_string(value).lower()
    parts = value.split(".")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise VolInvalid(f"Entity ID {value} is an invalid entity ID")
    return value


def cv_entity_ids(value: Any) -> list[str]:
    """Upstream accepts a comma-separated string or a list of entity ids."""
    if isinstance(value, str):
        value = [item.strip() for item in value.split(",")]
    if not isinstance(value, list):
        raise VolInvalid("expected a list of entity ids")
    return [cv_entity_id(item) for item in value]


def cv_multi_select(options: Any) -> Any:
    def validator(value: Any) -> list[Any]:
        if not isinstance(value, list):
            raise VolInvalid("expected a list")
        for item in value:
            if item not in options:
                raise VolInvalid(f"{item} is not a valid option")
        return value

    return validator


# --------------------------------------------------------------------------
# Config / options flow machinery (homeassistant.config_entries and
# homeassistant.data_entry_flow)
# --------------------------------------------------------------------------

class FlowResult(dict):
    """Stand-in for the FlowResult TypedDict (a plain dict upstream too)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class AbortFlow(Exception):
    """Stand-in for `homeassistant.data_entry_flow.AbortFlow`.

    Upstream the flow MANAGER catches this and turns it into an abort result;
    these suites call flow steps directly, so the raise surfaces and the suite
    asserts on `.reason` instead.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


_UNSET = object()


class _FlowBase:
    """The result constructors ConfigFlow and OptionsFlow share.

    Each returns a FlowResult shaped the way upstream's is (upstream's `type`
    values are the strings "form" / "abort" / "create_entry").
    """

    def async_show_form(self, *, step_id: str, data_schema: Any = None,
                        errors: Any = None, last_step: Any = None) -> FlowResult:
        return FlowResult(type="form", step_id=step_id, data_schema=data_schema,
                          errors=dict(errors or {}), last_step=last_step)

    def async_abort(self, *, reason: str) -> FlowResult:
        return FlowResult(type="abort", reason=reason)

    def async_create_entry(self, *, title: str, data: Any, options: Any = None) -> FlowResult:
        return FlowResult(type="create_entry", title=title, data=data, options=options)


class ConfigFlow(_FlowBase):
    """Stand-in for `homeassistant.config_entries.ConfigFlow`.

    Reproduces upstream's contract exactly where the integration's flow leans
    on it, against a fake `hass.config_entries` the suite supplies (needing
    async_entries / async_get_entry / async_update_entry /
    async_schedule_reload). The parts that are upstream-exact by design:

    * `async_set_unique_id` stores the id and returns any existing entry that
      already holds it.
    * `_abort_if_unique_id_configured` raises AbortFlow(error) when another
      entry holds the flow's unique id.
    * `_abort_if_unique_id_mismatch` compares against the entry named by the
      flow context (the one under reauth/reconfigure).
    * `async_update_reload_and_abort`: `data=` REPLACES entry data wholesale
      while `data_updates=` MERGES into it -- the distinction the config-flow
      suite exists to pin, because the merge is what preserves
      clip_link_secret across a reauth.
    """

    hass: Any = None
    context: Any = None
    unique_id: Any = None
    handler: str = ""

    def __init_subclass__(cls, *, domain: str | None = None, **kwargs: Any) -> None:
        super().__init_subclass__(**kwargs)
        if domain is not None:
            cls.handler = domain

    def _entries(self) -> list[Any]:
        return list(self.hass.config_entries.async_entries(self.handler))

    def _context_entry(self) -> Any:
        entry = self.hass.config_entries.async_get_entry((self.context or {}).get("entry_id"))
        if entry is None:
            raise AssertionError("flow context names no config entry")
        return entry

    async def async_set_unique_id(self, unique_id: str | None) -> Any:
        self.unique_id = unique_id
        for entry in self._entries():
            if unique_id is not None and entry.unique_id == unique_id:
                return entry
        return None

    def _abort_if_unique_id_configured(self, updates: Any = None,
                                       reload_on_update: bool = True, *,
                                       error: str = "already_configured") -> None:
        for entry in self._entries():
            if self.unique_id is not None and entry.unique_id == self.unique_id:
                raise AbortFlow(error)

    def _abort_if_unique_id_mismatch(self, *, reason: str = "unique_id_mismatch") -> None:
        if self._context_entry().unique_id != self.unique_id:
            raise AbortFlow(reason)

    def _get_reauth_entry(self) -> Any:
        return self._context_entry()

    def _get_reconfigure_entry(self) -> Any:
        return self._context_entry()

    def async_update_reload_and_abort(self, entry: Any, *, unique_id: Any = _UNSET,
                                      title: Any = _UNSET, data: Any = _UNSET,
                                      data_updates: Any = _UNSET,
                                      options: Any = _UNSET,
                                      reason: Any = _UNSET) -> FlowResult:
        if data is not _UNSET and data_updates is not _UNSET:
            raise ValueError("Cannot set both data and data_updates")
        kwargs: dict[str, Any] = {}
        if unique_id is not _UNSET:
            kwargs["unique_id"] = unique_id
        if title is not _UNSET:
            kwargs["title"] = title
        if data is not _UNSET:
            kwargs["data"] = data
        if data_updates is not _UNSET:
            kwargs["data"] = {**entry.data, **data_updates}
        if options is not _UNSET:
            kwargs["options"] = options
        self.hass.config_entries.async_update_entry(entry, **kwargs)
        self.hass.config_entries.async_schedule_reload(entry.entry_id)
        return self.async_abort(reason=reason if reason is not _UNSET else "reauth_successful")


class OptionsFlow(_FlowBase):
    """Stand-in for `homeassistant.config_entries.OptionsFlow`.

    `config_entry` is a read-only property here because it is one upstream
    (populated by the flow manager): assigning `self.config_entry = ...` in a
    handler's __init__ raises AttributeError, exactly the live regression the
    options-flow test pins.
    """

    hass: Any = None
    _config_entry_from_manager: Any = None

    @property
    def config_entry(self) -> Any:
        return self._config_entry_from_manager


# --------------------------------------------------------------------------
# Selector stand-ins (schema decoration only; the flows never validate with
# them, so these only carry their config)
# --------------------------------------------------------------------------

class _SelectorBase:
    def __init__(self, config: Any = None) -> None:
        self.config = config


class TextSelector(_SelectorBase):
    pass


class SelectSelector(_SelectorBase):
    pass


class TextSelectorConfig(dict):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class SelectSelectorConfig(dict):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)


class TextSelectorType(enum.StrEnum):
    TEXT = "text"
    PASSWORD = "password"


class SelectSelectorMode(enum.StrEnum):
    DROPDOWN = "dropdown"
    LIST = "list"


def selector_selector(config: Any) -> _SelectorBase:
    """Stand-in for `homeassistant.helpers.selector.selector`."""
    return _SelectorBase(config)


# --------------------------------------------------------------------------
# Misc helpers the service / flow / platform modules import
# --------------------------------------------------------------------------

class NoURLAvailableError(Exception):
    """Stand-in for `homeassistant.helpers.network.NoURLAvailableError`."""


def get_url(hass: Any, **kwargs: Any) -> str:
    """Stand-in for `homeassistant.helpers.network.get_url`: upstream reads the
    instance URLs off hass.config; here `hass.config.internal_url` is the whole
    story, and None raises exactly as upstream does with no URL configured."""
    url = getattr(hass.config, "internal_url", None)
    if url:
        return str(url)
    raise NoURLAvailableError()


def er_async_get(hass: Any) -> Any:
    """Stand-in for `homeassistant.helpers.entity_registry.async_get`: upstream
    keeps the registry in hass.data too. Suites install their own fake registry
    under this key; a missing one fails loudly."""
    return hass.data["entity_registry"]


class EntityRegistry:
    """Marker for `er.EntityRegistry` annotations; suites build their own fakes."""


def dr_async_get(hass: Any) -> Any:
    """Stand-in for `homeassistant.helpers.device_registry.async_get`, same
    hass.data convention as er_async_get above."""
    return hass.data["device_registry"]


def dr_async_entries_for_config_entry(registry: Any, config_entry_id: str) -> list[Any]:
    """Upstream-exact: the devices whose config_entries contain the entry id."""
    return [
        device for device in registry.devices.values()
        if config_entry_id in device.config_entries
    ]


def async_call_later(hass: Any, delay: Any, action: Any) -> Any:
    """Stand-in for `homeassistant.helpers.event.async_call_later`: records the
    (delay, action) pair on hass.later_calls instead of scheduling. A fake hass
    without that list fails loudly. Returns an unsubscribe no-op, as upstream
    returns an unsubscribe callable."""
    hass.later_calls.append((delay, action))
    return lambda: None


def persistent_notification_async_create(hass: Any, message: str, title: str | None = None,
                                         notification_id: str | None = None) -> None:
    """Stand-in for `persistent_notification.async_create`: records on
    hass.persistent_notifications; a fake hass without the list fails loudly."""
    hass.persistent_notifications.append(
        {"message": message, "title": title, "notification_id": notification_id}
    )


def _unscripted_media(*_args: Any, **_kwargs: Any) -> Any:
    raise AssertionError(
        "a test reached an unscripted media_source/tts helper; the suite must "
        "replace it on the module under test"
    )


# --------------------------------------------------------------------------
# pyalarmdotcomajax stand-ins beyond what const.py needs
# --------------------------------------------------------------------------

class _LibDeviceBase:
    """Instances double as fake library devices: attributes arrive as kwargs."""

    def __init__(self, **attributes: Any) -> None:
        for key, value in attributes.items():
            setattr(self, key, value)


class LibSensor(_LibDeviceBase):
    """Stand-in for `pyalarmdotcomajax.devices.sensor.Sensor`.

    Subtype carries the members const.py blacklists AND the ones
    binary_sensor.py maps to device classes; DeviceState carries the states
    is_on reads. Values are locally consistent, not upstream's numbers --
    nothing in the integration reads the numeric values.
    """

    class Subtype(enum.Enum):
        CONTACT_SENSOR = 1
        CONTACT_SHOCK_SENSOR = 2
        MOTION_SENSOR = 3
        SMOKE_DETECTOR = 4
        CO_DETECTOR = 5
        PANIC_BUTTON = 6
        FIXED_PANIC = 7
        GLASS_BREAK_DETECTOR = 8
        FREEZE_SENSOR = 9
        MOBILE_PHONE = 10
        PANEL_MOTION_SENSOR = 11
        PANEL_GLASS_BREAK_DETECTOR = 12
        PANEL_IMAGE_SENSOR = 13

    class DeviceState(enum.Enum):
        UNKNOWN = 0
        CLOSED = 1
        OPEN = 2
        IDLE = 3
        ACTIVE = 4


class LibWaterSensor(_LibDeviceBase):
    """Stand-in for `pyalarmdotcomajax.devices.water_sensor.WaterSensor`."""

    class DeviceState(enum.Enum):
        UNKNOWN = 0
        DRY = 1
        WET = 2


class LibPartition(_LibDeviceBase):
    """Stand-in for `pyalarmdotcomajax.devices.partition.Partition`."""

    class DeviceState(enum.Enum):
        UNKNOWN = 0
        DISARMED = 1
        ARMED_STAY = 2
        ARMED_AWAY = 3
        ARMED_NIGHT = 4


class OtpType(enum.Enum):
    """Stand-in for `pyalarmdotcomajax.const.OtpType`. The flow only round-trips
    member NAMES through the UI; the values are local."""

    disabled = 0
    app = 1
    sms = 2
    email = 4


class AlarmdotcomException(Exception):
    """Stand-in root of pyalarmdotcomajax's exception tree."""


class AuthenticationFailed(AlarmdotcomException):
    pass


class OtpRequired(AuthenticationFailed):
    """Carries enabled_2fa_methods exactly as the flow reads it off upstream."""

    def __init__(self, enabled_2fa_methods: Any = None) -> None:
        super().__init__("OTP required")
        self.enabled_2fa_methods = list(enabled_2fa_methods or [])


class ConfigureTwoFactorAuthentication(AlarmdotcomException):
    pass


class UnexpectedResponse(AlarmdotcomException):
    pass


class UnkonwnDevice(AlarmdotcomException):
    """Upstream's own misspelling, which base_device.py imports first."""


class LibConfigurationOptionType(enum.Enum):
    """Stand-in for `pyalarmdotcomajax.extensions.ConfigurationOptionType`."""

    BINARY_CHIME = 1
    ADJUSTABLE_CHIME = 2
    MOTION_SENSITIVITY = 3
    BRIGHTNESS = 4


class LibConfigurationOption(_LibDeviceBase):
    """Stand-in for `pyalarmdotcomajax.extensions.ConfigurationOption`; the
    platforms isinstance-check it, so fakes must BE one."""


class LibCameraSkybellControllerExtension:
    """Stand-in for the Skybell extension's option enums."""

    class ChimeOnOff(enum.Enum):
        OFF = 0
        ON = 1

    class ChimeAdjustableVolume(enum.Enum):
        OFF = 0
        LOW = 1
        MEDIUM = 2
        HIGH = 3

    class MotionSensitivity(enum.Enum):
        LOW = 1
        MEDIUM = 2
        HIGH = 3


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
        ServiceCall=ServiceCall,
        Event=Event,
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
            Platform=Platform, CONF_PASSWORD="password", CONF_USERNAME="username",
            CONF_UNIT_OF_MEASUREMENT="unit_of_measurement",
            EVENT_HOMEASSISTANT_STOP="homeassistant_stop")
    _module("homeassistant.exceptions", HomeAssistantError=HomeAssistantError,
            ConfigEntryAuthFailed=type("ConfigEntryAuthFailed", (HomeAssistantError,), {}),
            ConfigEntryNotReady=type("ConfigEntryNotReady", (HomeAssistantError,), {}))
    _module("homeassistant.config_entries", ConfigEntry=ConfigEntry,
            ConfigFlow=ConfigFlow, OptionsFlow=OptionsFlow)
    _module("homeassistant.data_entry_flow", FlowResult=FlowResult,
            AbortFlow=AbortFlow)

    _module("homeassistant.components")
    _module("homeassistant.components.sensor", SensorEntity=SensorEntity,
            SensorDeviceClass=SensorDeviceClass, SensorStateClass=SensorStateClass)
    _module("homeassistant.components.event", EventEntity=EventEntity,
            EventDeviceClass=EventDeviceClass)
    _module("homeassistant.components.camera", Camera=Camera,
            CameraEntityFeature=CameraEntityFeature)
    _module("homeassistant.components.http", HomeAssistantView=HomeAssistantView)
    _module("homeassistant.components.binary_sensor",
            BinarySensorEntity=BinarySensorEntity,
            BinarySensorDeviceClass=BinarySensorDeviceClass,
            BinarySensorEntityDescription=BinarySensorEntityDescription)
    _module("homeassistant.components.number", NumberEntity=NumberEntity,
            NumberMode=NumberMode)
    _module("homeassistant.components.select", SelectEntity=SelectEntity)
    _module("homeassistant.components.switch", SwitchEntity=SwitchEntity,
            SwitchDeviceClass=SwitchDeviceClass)
    _module("homeassistant.components.persistent_notification",
            async_create=persistent_notification_async_create)
    # The two media helpers are deliberately unscripted here: any suite driving
    # the speak service replaces them on the module under test, and an
    # unreplaced call fails loudly rather than resolving to a fiction.
    _module("homeassistant.components.media_source",
            async_resolve_media=_unscripted_media)
    _module("homeassistant.components.tts",
            generate_media_source_id=_unscripted_media)

    device_registry = _module(
        "homeassistant.helpers.device_registry",
        DeviceEntryType=DeviceEntryType,
        CONNECTION_NETWORK_MAC="mac",
        async_get=dr_async_get,
        async_entries_for_config_entry=dr_async_entries_for_config_entry,
    )
    _module("homeassistant.helpers", device_registry=device_registry)
    _module("homeassistant.helpers.entity", Entity=Entity, DeviceInfo=DeviceInfo,
            EntityDescription=EntityDescription, EntityCategory=EntityCategory)
    _module("homeassistant.helpers.entity_platform", AddEntitiesCallback=Any,
            DiscoveryInfoType=Any)
    _module("homeassistant.helpers.update_coordinator",
            CoordinatorEntity=CoordinatorEntity,
            DataUpdateCoordinator=DataUpdateCoordinator,
            UpdateFailed=UpdateFailed)
    _module("homeassistant.helpers.aiohttp_client",
            async_get_clientsession=_no_session,
            async_create_clientsession=_no_session)
    _module("homeassistant.helpers.event", async_track_time_interval=_no_session,
            async_call_later=async_call_later)
    _module("homeassistant.helpers.config_validation", string=cv_string,
            boolean=cv_boolean, entity_id=cv_entity_id, entity_ids=cv_entity_ids,
            multi_select=cv_multi_select)
    _module("homeassistant.helpers.entity_registry", async_get=er_async_get,
            EntityRegistry=EntityRegistry)
    _module("homeassistant.helpers.network", get_url=get_url,
            NoURLAvailableError=NoURLAvailableError)
    _module("homeassistant.helpers.selector", selector=selector_selector,
            SelectSelector=SelectSelector, SelectSelectorConfig=SelectSelectorConfig,
            SelectSelectorMode=SelectSelectorMode, TextSelector=TextSelector,
            TextSelectorConfig=TextSelectorConfig, TextSelectorType=TextSelectorType)

    # voluptuous and async_timeout: CI installs aiohttp and nothing else, so
    # both are stubbed. async_timeout.timeout has the same async-context-manager
    # contract as asyncio.timeout on the interpreters this repo targets, so the
    # stdlib one IS the stub.
    _module("voluptuous", Schema=VolSchema, Required=VolRequired,
            Optional=VolOptional, Exclusive=VolExclusive, Invalid=VolInvalid)
    _module("async_timeout", timeout=asyncio.timeout)

    # `const.py` reads three names off pyalarmdotcomajax; the flow, service and
    # platform modules read the exceptions, OtpType, the device-model classes
    # and the Skybell extension enums. Anything else the integration grows will
    # fail loudly here rather than quietly resolve.
    _module("pyalarmdotcomajax", OtpRequired=OtpRequired)
    _module("pyalarmdotcomajax.const", ATTR_STATE_TEXT="state_text",
            ATTR_MAC_ADDRESS="mac_address", OtpType=OtpType)
    _module("pyalarmdotcomajax.exceptions",
            AlarmdotcomException=AlarmdotcomException,
            AuthenticationFailed=AuthenticationFailed,
            OtpRequired=OtpRequired,
            ConfigureTwoFactorAuthentication=ConfigureTwoFactorAuthentication,
            UnexpectedResponse=UnexpectedResponse,
            UnkonwnDevice=UnkonwnDevice)
    _module("pyalarmdotcomajax.devices")
    _module("pyalarmdotcomajax.devices.sensor", Sensor=LibSensor)
    _module("pyalarmdotcomajax.devices.water_sensor", WaterSensor=LibWaterSensor)
    _module("pyalarmdotcomajax.devices.partition", Partition=LibPartition)
    _module("pyalarmdotcomajax.devices.registry", AllDevices_t=Any)
    _module("pyalarmdotcomajax.extensions",
            ConfigurationOption=LibConfigurationOption,
            ConfigurationOptionType=LibConfigurationOptionType,
            CameraSkybellControllerExtension=LibCameraSkybellControllerExtension)

    # controller.py applies the websocket monkeypatch at import and needs the
    # real library to do it. sensor.py only names the class in an annotation.
    _module("custom_components.telus_smarthome.controller",
            AlarmIntegrationController=type("AlarmIntegrationController", (), {}))

    # The three import-time monkeypatch modules are stubbed for the same reason
    # controller.py is: importing any of them patches the real pyalarmdotcomajax,
    # which is not installed here. CI's check_patch.py job imports the real ones
    # against the real library and fails loudly if a patch no longer applies.
    for patch_module in (
        "custom_components.telus_smarthome.telus_auth_patch",
        "custom_components.telus_smarthome.telus_ws_heartbeat_patch",
        "custom_components.telus_smarthome.telus_ws_push_patch",
    ):
        _module(patch_module)
