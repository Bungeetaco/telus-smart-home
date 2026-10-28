"""Scripted stand-ins shared by the E66 integration suites.

Not a test file (CI globs test_*.py only): this is the counterpart of
`ha_stubs.py` for the objects the stubs deliberately do NOT provide -- the
hass instance, the entity/device registries, config entries, and an
aiohttp-shaped session whose every response is scripted by the suite. Same
rules as ha_stubs: nothing auto-magic, no MagicMock, no __getattr__. A module
under test that reaches for a name these fakes do not define fails loudly.

The session fakes raise REAL aiohttp exceptions (`ClientResponseError`,
`ContentTypeError`), because the code under test catches those exact types.
Nothing here ever opens a socket.
"""
from __future__ import annotations

from typing import Any

import aiohttp

import ha_stubs


class FakeConfig:
    """hass.config: the two fields the modules under test read."""

    def __init__(self, location_name: str = "Test Home",
                 internal_url: str | None = None) -> None:
        self.location_name = location_name
        self.internal_url = internal_url


class FakeServices:
    """hass.services: a plain dict of registered handlers."""

    def __init__(self) -> None:
        self.registered: dict[tuple[str, str], Any] = {}
        self.schemas: dict[tuple[str, str], Any] = {}

    def has_service(self, domain: str, service: str) -> bool:
        return (domain, service) in self.registered

    def async_register(self, domain: str, service: str, handler: Any,
                       schema: Any = None) -> None:
        self.registered[(domain, service)] = handler
        self.schemas[(domain, service)] = schema

    def async_remove(self, domain: str, service: str) -> None:
        self.registered.pop((domain, service), None)
        self.schemas.pop((domain, service), None)


class FakeStates:
    """hass.states: only async_entity_ids(domain), for the TTS default lookup."""

    def __init__(self, entity_ids: dict[str, list[str]] | None = None) -> None:
        self._entity_ids = dict(entity_ids or {})

    def async_entity_ids(self, domain: str) -> list[str]:
        return list(self._entity_ids.get(domain, []))


class FakeBus:
    """hass.bus: records listeners, returns unsubscribe callables."""

    def __init__(self) -> None:
        self.listeners: list[tuple[str, Any]] = []
        self.once_listeners: list[tuple[str, Any]] = []

    def async_listen(self, event_type: str, listener: Any) -> Any:
        self.listeners.append((event_type, listener))
        return lambda: None

    def async_listen_once(self, event_type: str, listener: Any) -> Any:
        self.once_listeners.append((event_type, listener))
        return lambda: None


class FakeHttp:
    """hass.http: records registered views."""

    def __init__(self) -> None:
        self.views: list[Any] = []

    def register_view(self, view: Any) -> None:
        self.views.append(view)


class FakeHass:
    """The hass surface the modules under test actually touch, nothing more."""

    def __init__(self) -> None:
        self.data: dict[str, Any] = {}
        self.services = FakeServices()
        self.states = FakeStates()
        self.config = FakeConfig()
        self.bus = FakeBus()
        self.http = FakeHttp()
        self.config_entries: Any = None
        # ha_stubs.async_call_later / persistent_notification record here.
        self.later_calls: list[tuple[Any, Any]] = []
        self.persistent_notifications: list[dict[str, Any]] = []

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        # Runs inline: everything the suites push through this is a local,
        # deterministic function (reading a scripted secret, writing a temp
        # file), so a thread pool would only add nondeterminism.
        return func(*args)


class FakeRegistryEntry:
    """One entity-registry row, with upstream's field names."""

    def __init__(self, entity_id: str, unique_id: str | None = None,
                 platform: str = "telus_smarthome_alarmdotcom", config_entry_id: str | None = "entry1",
                 original_name: str | None = None, name: str | None = None) -> None:
        self.entity_id = entity_id
        self.domain = entity_id.split(".")[0]
        self.unique_id = unique_id
        self.platform = platform
        self.config_entry_id = config_entry_id
        self.original_name = original_name
        self.name = name


class FakeEntityRegistry:
    """The registry surface er.async_get(hass) hands back."""

    def __init__(self, entries: list[FakeRegistryEntry] | None = None) -> None:
        self.entities: dict[str, FakeRegistryEntry] = {
            entry.entity_id: entry for entry in entries or []
        }

    def async_get(self, entity_id: str) -> FakeRegistryEntry | None:
        return self.entities.get(entity_id)


class FakeDeviceEntry:
    """One device-registry row: identifiers, its owning config entry, and a name.

    A device belongs to exactly one config entry upstream (`config_entry_id`);
    `config_entries` is accepted for the suites that still build rows with a
    set and is reduced to its single member.
    """

    def __init__(self, device_id: str, identifiers: set[tuple[str, Any]],
                 config_entries: set[str], name: str | None = None,
                 via_device_id: str | None = None,
                 manufacturer: str | None = None) -> None:
        self.id = device_id
        self.identifiers = identifiers
        self.config_entry_id = next(iter(config_entries), None)
        self.name = name
        self.via_device_id = via_device_id
        # The stamp each registering module puts on its devices (upstream's
        # `DeviceEntry.manufacturer`). Setup's prune pass reads it to tell a
        # library-owned device from one of this fork's own, so the fake row
        # has to carry it.
        self.manufacturer = manufacturer


class FakeDeviceRegistry:
    """The registry surface dr.async_get(hass) hands back.

    Reproduces the upstream behaviour the integration leans on and nothing
    else: a scoped lookup by one identifier plus the owning config entry,
    get-or-create that returns the entry (new or existing) so its `.id` can be
    read back, and removal that raises on a second call. `deleted_devices` is
    an internal implementation detail of the real registry and raises here
    the way core callers raise upstream, so an integration that so much as
    reads it fails the suite instead of logging a warning nobody sees.
    """

    def __init__(self, devices: list[FakeDeviceEntry] | None = None) -> None:
        self.devices: dict[str, FakeDeviceEntry] = {d.id: d for d in devices or []}
        self.removed: list[str] = []
        self.created: list[dict[str, Any]] = []

    @property
    def deleted_devices(self) -> Any:
        raise RuntimeError(
            "deleted_devices is an internal implementation detail of the device "
            "registry with no supported public use")

    def async_remove_device(self, device_id: str) -> None:
        # Upstream raises on a second removal of the same id; reproducing that
        # is the point -- the double-removal KeyError is a shipped bug the
        # setup test guards against reintroducing.
        del self.devices[device_id]
        self.removed.append(device_id)

    def async_get_device_by_identifier(self, identifier: tuple[str, Any],
                                       config_entry_id: str) -> FakeDeviceEntry | None:
        """Upstream-exact: one identifier, scoped to the owning config entry."""
        for device in self.devices.values():
            if device.config_entry_id == config_entry_id and identifier in device.identifiers:
                return device
        return None

    def async_get_or_create(self, *, config_entry_id: str, identifiers: set[tuple[str, Any]],
                            **kwargs: Any) -> FakeDeviceEntry:
        """Return the existing device matching any identifier within the config
        entry, else register a new one. Every call is recorded in `created`
        with its keyword arguments, new or not, so a suite can assert exactly
        what setup registered."""
        self.created.append({"config_entry_id": config_entry_id,
                             "identifiers": identifiers, **kwargs})
        for identifier in identifiers:
            device = self.async_get_device_by_identifier(identifier, config_entry_id)
            if device is not None:
                if kwargs.get("name"):
                    device.name = kwargs["name"]
                if kwargs.get("manufacturer"):
                    device.manufacturer = kwargs["manufacturer"]
                return device
        device = FakeDeviceEntry(f"fake-device-{len(self.devices) + 1}", set(identifiers),
                                 {config_entry_id}, kwargs.get("name"),
                                 kwargs.get("via_device_id"),
                                 kwargs.get("manufacturer"))
        self.devices[device.id] = device
        return device


class FakeConfigEntry:
    """A config entry the flow stubs and __init__ can act on."""

    _counter = 0

    def __init__(self, entry_id: str | None = None, data: dict[str, Any] | None = None,
                 options: dict[str, Any] | None = None, unique_id: str | None = None,
                 title: str = "", version: int = 4, domain: str = "telus_smarthome_alarmdotcom") -> None:
        if entry_id is None:
            FakeConfigEntry._counter += 1
            entry_id = f"entry{FakeConfigEntry._counter}"
        self.entry_id = entry_id
        self.data = dict(data or {})
        self.options = dict(options or {})
        self.unique_id = unique_id
        self.title = title
        self.version = version
        self.domain = domain
        self.background_tasks: list[tuple[Any, str]] = []
        self.on_unload: list[Any] = []

    def async_create_background_task(self, hass: Any, coro: Any, name: str = "") -> Any:
        # Recorded, then closed: the suites assert the task was handed over;
        # actually running a forever-loop here would hang the suite.
        self.background_tasks.append((coro, name))
        coro.close()
        return None

    def async_on_unload(self, unsub: Any) -> None:
        self.on_unload.append(unsub)


class FakeConfigEntries:
    """hass.config_entries: entries plus the calls the flow stubs make."""

    def __init__(self, entries: list[FakeConfigEntry] | None = None) -> None:
        self.entries = list(entries or [])
        self.updates: list[tuple[FakeConfigEntry, dict[str, Any]]] = []
        self.reloads: list[str] = []
        self.forwarded: list[tuple[Any, Any]] = []
        self.unloaded: list[tuple[Any, Any]] = []

    def async_entries(self, domain: str | None = None) -> list[FakeConfigEntry]:
        if domain is None:
            return list(self.entries)
        return [e for e in self.entries if e.domain == domain]

    def async_get_entry(self, entry_id: Any) -> FakeConfigEntry | None:
        for entry in self.entries:
            if entry.entry_id == entry_id:
                return entry
        return None

    def async_update_entry(self, entry: FakeConfigEntry, *, data: Any = None,
                           options: Any = None, unique_id: Any = None,
                           title: Any = None) -> None:
        changes: dict[str, Any] = {}
        if data is not None:
            entry.data = dict(data)
            changes["data"] = dict(data)
        if options is not None:
            entry.options = dict(options)
            changes["options"] = dict(options)
        if unique_id is not None:
            entry.unique_id = unique_id
            changes["unique_id"] = unique_id
        if title is not None:
            entry.title = title
            changes["title"] = title
        self.updates.append((entry, changes))

    def async_schedule_reload(self, entry_id: str) -> None:
        self.reloads.append(entry_id)

    async def async_forward_entry_setups(self, entry: Any, platforms: Any) -> None:
        self.forwarded.append((entry, list(platforms)))

    async def async_unload_platforms(self, entry: Any, platforms: Any) -> bool:
        self.unloaded.append((entry, list(platforms)))
        return True


class FakeResponse:
    """One scripted HTTP response, aiohttp-shaped.

    `body` is the dict json() returns; body=None makes json() raise the real
    aiohttp.ContentTypeError (an unparseable 2xx, which several write paths
    must treat as success). raise_for_status() raises the real
    aiohttp.ClientResponseError for status >= 400, as aiohttp does.
    """

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None,
                 headers: dict[str, str] | None = None) -> None:
        self.status = status
        self._body = body
        self.headers = dict(headers or {})

    async def json(self) -> dict[str, Any]:
        if self._body is None:
            raise aiohttp.ContentTypeError(request_info=None, history=())
        return self._body

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=None, history=(), status=self.status)

    async def __aenter__(self) -> "FakeResponse":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


class FakeSession:
    """An aiohttp-session stand-in whose responses are a scripted queue.

    Each element of `script` is either a FakeResponse or an exception instance
    to raise at request time (aiohttp.ClientError subclasses, TimeoutError).
    Every request is recorded in `requests` as a dict with method, url, json
    body, headers and timeout, which is how the suites pin the exact write
    shape that goes to the bridge.
    """

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.requests: list[dict[str, Any]] = []

    def _request(self, method: str, url: str, json: Any = None, headers: Any = None,
                 timeout: Any = None, params: Any = None) -> FakeResponse:
        self.requests.append({
            "method": method, "url": url, "json": json,
            "headers": dict(headers or {}), "timeout": timeout, "params": params,
        })
        if not self.script:
            raise AssertionError(f"unscripted {method} {url}")
        item = self.script.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    def post(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._request("POST", url, **kwargs)

    def get(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._request("GET", url, **kwargs)

    def delete(self, url: str, **kwargs: Any) -> FakeResponse:
        return self._request("DELETE", url, **kwargs)


def patch(module: Any, **replacements: Any) -> dict[str, Any]:
    """Replace module attributes, returning the originals for restore().

    The modules under test bind helpers at import time (`from x import y`), so
    the suites patch the module's OWN binding, never the stub tree -- a patch
    here cannot leak into another module's behaviour.
    """
    saved = {}
    for name, value in replacements.items():
        saved[name] = getattr(module, name)
        setattr(module, name, value)
    return saved


def restore(module: Any, saved: dict[str, Any]) -> None:
    for name, value in saved.items():
        setattr(module, name, value)


class ScriptedSecret:
    """A read_control_secret stand-in: returns the scripted value."""

    def __init__(self, value: str | None = "test-secret") -> None:
        self.value = value

    def __call__(self) -> str | None:
        return self.value


def clientsession_for(session: FakeSession) -> Any:
    """A drop-in for async_get_clientsession returning `session`."""

    def _get(hass: Any) -> FakeSession:
        return session

    return _get
