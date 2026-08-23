"""Bluetooth arming devices -- the phones allowed to DISARM the alarm.

Backs enhancement-backlog item E24. ``isDisarmingEnabledForDevice`` decides
whether a paired phone walking up to the house disarms the panel, so this module
is written to the same rules as the siren route: it reads freely, it writes only
when a caller explicitly asked for that exact write, it checks a capability flag
first, and every ambiguity resolves to "refuse".

alarm.com is the CONFIGURATION AUTHORITY for this setting. The Control4 endpoint
on the same panel is local-access-only and independent of it, so what is written
here is the real setting and not a cloud mirror of one -- there is no second
place it can be corrected from and no local undo.

Everything below is taken from a captured browser session against a live panel
(tests/fixtures/bluetooth_arming_device_*.json,
tests/fixtures/device_catalog_get.json), not from a spec -- there is none. Four
properties of the API shape this module, and they are the SAME four
``camera_settings`` documents for the video settings endpoints; this endpoint
lives under ``web/api/devices/`` rather than ``web/api/video/`` but speaks the
identical dialect:

1. **The read is JSON:API, the write is FLAT.** ``GET`` returns
   ``{data: {...}, included: [...]}``; the ``PUT`` body is a single flat object
   -- not wrapped in ``data``, not the ``attributes`` sub-object on its own, with
   each relationship inlined under its own name. See _flatten.
2. **The resource type is PLURALIZED on the write.** The read says
   ``devices/bluetooth-arming-device``; the browser sends back
   ``devices/bluetooth-arming-devices``, and does the same to every inlined
   relationship (``systems/system`` -> ``systems/systems``,
   ``devices/state-info`` -> ``devices/state-infos``). See _write_type.
3. **A PUT replaces the WHOLE object.** The captured body carries all ~30 of the
   document's other attributes alongside the one that changed, so this is a
   read-modify-write. A partial body risks resetting unrelated fields on a real
   panel -- including, in the worst case, the enrolled partition.
4. **Capability before control.** The same payload shape comes back for a record
   that cannot be written, so support is decided from the record's own flags
   before anything is offered or applied, and an absent flag counts as "no".
   See _supports_toggle for exactly which flags and why those.

This module deliberately does NOT import from camera_settings even though the
two solve the same problem: that module is scoped to the ``web/api/video``
settings documents and its helpers carry assumptions (``*FeatureSupport`` enums,
per-field option lists) this endpoint has none of. _write_type and _flatten below
are small, deliberate re-implementations of its equivalents, kept in step with
them by tests/test_arming_devices.py rather than by a shared import.

Nothing here creates, renames, unpairs or deletes a device. The portal offers all
four (``destroyRecord``, ``devices/bluetoothArmingDevices/deleteAll``); they are
out of scope on purpose, because none of them is undoable from this side.
"""
import logging

import aiohttp

from . import config as c
from .telus_auth import RateLimited, TelusSession

log = logging.getLogger(__name__)

# The JSON:API resource type every bluetooth arming device carries. Discovery
# filters the catalog on THIS rather than on the "Bluetooth Devices" category
# (id 7 in the capture): the category id and name are presentation, and a
# localized portal returns a translated name, while the type is the wire
# contract the write side also depends on.
_TYPE = "devices/bluetooth-arming-device"

_DEVICE_URL = "{base}web/api/devices/bluetoothArmingDevices/{did}"
_CATALOG_URL = "{base}web/api/settings/manageDevices/deviceCatalogs/{sid}"

# The one attribute this module ever writes. Named once so the read path, the
# capability check and the write path cannot drift apart.
_ENABLED = "isDisarmingEnabledForDevice"


class ArmingDeviceError(RuntimeError):
    """An arming-device read/write could not be performed.

    Mirrors camera_settings.CameraSettingsError's contract exactly, because
    control_server maps both through the same _settings_error helper.

    ``unsupported`` is True only when the DEVICE RECORD itself says the toggle
    cannot be written (see _supports_toggle) -- i.e. the caller asked for
    something real that this record cannot do. It is False for a caller mistake
    (a non-bool value, an id that does not exist) and for an upstream failure.
    Callers map those to different statuses, so branch on the flag; never parse
    the message text.

    ``field`` is the logical field name involved, or None.
    """

    def __init__(self, message: str, *, unsupported: bool = False,
                 field: str | None = None):
        self.unsupported = unsupported
        self.field = field
        super().__init__(message)


# --- low-level HTTP ---------------------------------------------------------
def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Turn a non-2xx response into the right exception type.

    Identical policy to camera_settings._raise_for_status, and identical for the
    same two reasons: 429 becomes telus_auth.RateLimited carrying the server's
    own Retry-After so a caller sleeps the requested wait instead of walking back
    into the limiter, and 401/403 are re-raised as the raw
    aiohttp.ClientResponseError because control_server._fetch_with_relogin keys
    its one-shot re-login off that exact type and status -- wrapping an expired
    cookie here would turn a recoverable session expiry into a hard failure.
    """
    if resp.status == 429:
        raise RateLimited(c.parse_retry_after(resp.headers.get("Retry-After")))
    if resp.status in (401, 403):
        resp.raise_for_status()
    if resp.status >= 400:
        # Path only, never the full URL: query strings on this API can carry
        # session-scoped material and this string reaches logs and callers.
        raise ArmingDeviceError(f"TELUS returned HTTP {resp.status} for {resp.url.path}")


async def _get_doc(telus: TelusSession, url: str) -> dict:
    """GET one JSON:API document."""
    async with telus.session.get(url, headers=telus.api_headers) as resp:
        _raise_for_status(resp)
        return await resp.json(content_type=None)


async def _put_doc(telus: TelusSession, url: str, doc: dict) -> dict | None:
    """PUT a whole flattened document back and return the server's version of it.

    The body is the FLATTENED document (see _flatten), sent as the raw JSON
    object -- NOT wrapped in a JSON:API ``{"data": ...}`` envelope. Confirmed by
    diffing the request body a real browser sent against the document it had just
    GETed from this same URL.
    """
    async with telus.session.put(url, json=doc, headers=telus.api_headers) as resp:
        _raise_for_status(resp)
        try:
            return await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            # Tolerated in case this endpoint ever answers 204/empty the way some
            # of the video settings PUTs do; the caller then falls back to the
            # document it just sent. The capture always echoed the document back.
            return None


def _write_type(name: str) -> str:
    """The resource type spelling the WRITE side of this API uses.

    alarm.com serves singular JSON:API types (``devices/bluetooth-arming-device``)
    and the browser sends the plural back (``devices/bluetooth-arming-devices``),
    consistently, for the top-level document and every inlined relationship alike
    -- an Ember Data convention showing through. Whether the endpoint validates it
    is unknown and untested, so this sends exactly what a working client was
    observed to send rather than betting that it does not.
    """
    return name if name.endswith("s") else name + "s"


def _flatten(doc: dict | None) -> dict:
    """Collapse a JSON:API device document into the flat object this API writes.

    The flat form is ``data.attributes`` plus ``id``/``type``, with each
    relationship inlined under its relationship name carrying its own
    ``id``/``type``. A relationship whose resource is absent from ``included``
    becomes an ``{"id", "type"}`` stub -- which is exactly what the browser sends
    for one it did not load, and is the whole of what it sends for this document:
    the capture's ``included`` is empty and ``system``/``stateInfo`` arrive as
    stubs. Dropping them instead would delete those relationships on a
    whole-document PUT.

    Returns {} for anything unrecognized so callers never have to guard the shape.

    KNOWN, DELIBERATE DELTA from the browser's own body, pinned by
    tests/test_arming_devices.py rather than left to be rediscovered: the browser
    holds a richer Ember model than one response carries, so it also sends
    ``state``/``desiredState``/``isRefreshingState`` and a ``deviceIcon.cachedData``
    sub-object, and omits ``deviceModelId``/``primaryAssociatedDeviceIds``. This
    resends precisely what the server just said instead, because inventing a
    ``state`` for a record whose ``hasState`` is false is a guess, and a guess is
    the one thing a whole-document PUT against an alarm panel must not contain.
    Unverified against the live endpoint either way.
    """
    data = (doc or {}).get("data")
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return {}

    flat = dict(data.get("attributes") or {})
    if "id" in data:
        flat["id"] = data["id"]
    if "type" in data:
        flat["type"] = _write_type(data["type"])

    included = {}
    for res in (doc or {}).get("included") or []:
        if isinstance(res, dict) and "type" in res and "id" in res:
            included[(res["type"], res["id"])] = res

    for name, body in (data.get("relationships") or {}).items():
        ref = (body or {}).get("data")
        # `type` must be a real string, not merely present: this API also uses
        # plain objects as ATTRIBUTE values (`associatedCameraDeviceIds` is {}),
        # and camera_settings' looser "is the key there" test walks into
        # _write_type with a None and raises AttributeError mid-parse.
        if not isinstance(ref, dict) or not isinstance(ref.get("type"), str) \
                or "id" not in ref:
            continue
        res = included.get((ref["type"], ref["id"]))
        if res is None:
            flat[name] = {"type": _write_type(ref["type"]), "id": ref["id"]}
            continue
        sub = dict(res.get("attributes") or {})
        sub["id"] = res["id"]
        sub["type"] = _write_type(res["type"])
        flat[name] = sub
    return flat


# --- capability -------------------------------------------------------------
def _supports_toggle(attrs: dict) -> bool:
    """Whether this record's disarming toggle may be written.

    This endpoint publishes no ``*FeatureSupport`` enum and no ``canSetX`` flag
    for the toggle itself -- the portal's own template renders the switch
    unconditionally once the settings page is open. So the gate is assembled from
    the two flags that DO decide, both read off the captured payload and both
    demonstrably discriminating within it:

    * ``canBeSaved`` -- the portal's toggle handler is
      ``e.isDisarmingEnabledForDevice = !e.isDisarmingEnabledForDevice; e.save()``,
      and ``canBeSaved`` is the record's own "may this be saved" flag. It is not
      a constant: in the same device catalog the bluetooth arming devices carry
      ``true`` while the ``devices/mobile-device`` records carry ``false``.
    * ``canAccessWebSettings`` -- the manage-devices UI computes the "Device
      Settings" link's visibility as ``!isMobileApp && canAccessWebSettings ||
      isMobileApp && canAccessAppSettings``, i.e. this is the flag deciding
      whether the page the toggle lives on is reachable at all. The WEB half is
      the right one here because this bridge is a website API client, the same
      call camera_settings makes for ``live_resolution``.

    ``isDisarmingEnabledForDevice`` must additionally be present AND a real bool.
    Absence is unknown, and unknown must not be offered or written; a non-bool
    would mean this parser is looking at a record shape it does not understand,
    which is not a record to start writing to.

    Deliberately NOT part of the gate: ``hasPermissionToChangeState`` /
    ``canReceiveCommands``, which govern STATE COMMANDS to the device (this
    record's ``hasState`` is false), not a settings save; and ``canBeRenamed`` /
    ``canBeDeleted``, which gate operations this module does not perform.
    """
    return (isinstance(attrs.get(_ENABLED), bool)
            and bool(attrs.get("canBeSaved"))
            and bool(attrs.get("canAccessWebSettings")))


def _parse(attrs: dict) -> dict:
    """The caller-facing shape, from a device record's attributes (or flat doc).

    ``supports_disarming_toggle`` is not decoration: a record that cannot be
    written returns the same ``isDisarmingEnabledForDevice`` a writable one does,
    so a caller that reads the value as permission will offer a control that
    silently does nothing.

    ``enrolled_partition_id`` is passed through as the STRING the API sends
    ("1"), not coerced to an int -- it is an identifier, and the write path has
    to send back exactly what it read.
    """
    return {
        "id": attrs.get("id"),
        "description": attrs.get("description") or attrs.get("id"),
        "disarming_enabled": bool(attrs.get(_ENABLED)),
        "enrolled_partition_id": attrs.get("enrolledPartitionId"),
        "supports_disarming_toggle": _supports_toggle(attrs),
        # The untouched attributes, for when a panel returns a field this parser
        # does not recognize -- the same escape hatch camera_settings publishes.
        "raw": attrs,
    }


def _system_id(telus: TelusSession, system_id: str | None) -> str:
    """The alarm.com system ("Context-Id") the catalog read is scoped to.

    Resolved from ``telus.api_headers`` rather than from TelusSession's private
    ``_context_id``: that property is the session's own public statement of which
    system it is operating against, it is already the value every other
    authenticated read sends, and reading it here means a re-login that switched
    systems is picked up automatically instead of being cached at import time.
    Nothing outside telus_auth has to learn a new attribute for this.
    """
    resolved = system_id or (telus.api_headers or {}).get("Context-Id")
    if not resolved:
        raise ArmingDeviceError(
            "no TELUS system id available yet (session not logged in?)")
    return resolved


# --- reads ------------------------------------------------------------------
async def get_arming_devices(telus: TelusSession,
                             system_id: str | None = None) -> list[dict]:
    """Every bluetooth arming device on the system, in _parse's shape.

    Read from the manage-devices CATALOG, which returns each device's full record
    inline in ``included`` -- so the whole list costs ONE request rather than one
    per device against an API whose rate limits are undocumented and can lock the
    account.

    ``system_id`` defaults to the session's own Context-Id (see _system_id), so
    the routes can pass nothing and still survive a re-login that changed it.

    This list is also what makes a write safe: control_server checks a requested
    device id against it before calling set_disarming_enabled, so an unknown id
    is a local 404 rather than a blind PUT to an arbitrary device id on a live
    panel.
    """
    url = _CATALOG_URL.format(base=c.TELUS_BASE, sid=_system_id(telus, system_id))
    doc = await _get_doc(telus, url)
    devices = []
    for res in (doc or {}).get("included") or []:
        if not isinstance(res, dict) or res.get("type") != _TYPE:
            continue
        attrs = dict(res.get("attributes") or {})
        attrs["id"] = res.get("id")
        devices.append(_parse(attrs))
    # Stable order regardless of what the catalog happens to return, so a caller
    # diffing two polls sees real changes rather than reordering.
    devices.sort(key=lambda d: str(d["id"]))
    return devices


async def get_arming_device(telus: TelusSession, device_id: str) -> dict:
    """One bluetooth arming device, in the same shape get_arming_devices returns.

    Raises ArmingDeviceError (``unsupported`` False) if the id does not exist --
    the endpoint 404s and _raise_for_status turns that into the caller-mistake
    form, not an upstream failure.
    """
    url = _DEVICE_URL.format(base=c.TELUS_BASE, did=device_id)
    return _parse(_flatten(await _get_doc(telus, url)))


# --- the write --------------------------------------------------------------
async def set_disarming_enabled(telus: TelusSession, device_id: str,
                                enabled: bool) -> dict:
    """Turn bluetooth disarming on or off for ONE paired device.

    This decides whether that phone walking up to the house disarms the alarm.
    Consequently:

    * ``enabled`` must be a real ``bool``. A truthy string or an int is REFUSED
      before any request is made, rather than coerced -- ``bool("false")`` is
      True, and this is not a field to be generous about.
    * The record's own capability is checked against the freshly-read document
      before anything is mutated (see _supports_toggle), and a record that does
      not advertise it raises with ``unsupported=True``.
    * Setting the value it already has still performs the PUT rather than
      short-circuiting: this API is the configuration authority, the caller
      explicitly asked for that state, and a no-op that reports success without
      confirming with the server would be indistinguishable from a write that
      the server silently dropped.

    Read-modify-write: the whole document is sent back with exactly one attribute
    changed, because a PUT here REPLACES the record.

    Returns the same shape as get_arming_device.
    """
    if not isinstance(enabled, bool):
        raise ArmingDeviceError("enabled takes a boolean", field="disarming_enabled")

    url = _DEVICE_URL.format(base=c.TELUS_BASE, did=device_id)
    # _flatten's output is BOTH what gets mutated and what gets sent -- keeping
    # them the same object is what makes the edit reach the wire.
    doc = _flatten(await _get_doc(telus, url))
    if not _supports_toggle(doc):
        raise ArmingDeviceError(
            "this device does not support the bluetooth disarming toggle",
            unsupported=True, field="disarming_enabled")

    # Exactly one attribute is touched; the other ~30 go back as they came.
    #
    # In particular `webSettings` is left as the INTEGER 7205 the read returned,
    # even though the browser was observed sending it as the STRING "7205" on
    # this same endpoint. Nothing establishes which form the endpoint requires,
    # so this echoes what the server itself just said -- the defensible half of
    # an undocumented discrepancy. Pinned by
    # tests/test_arming_devices.py::test_websettings_int_string_discrepancy...;
    # if a live test ever shows the integer being rejected, coerce it there.
    doc[_ENABLED] = enabled
    log.info("Setting bluetooth disarming %s for arming device %s",
             "ON" if enabled else "OFF", device_id)
    response = await _put_doc(telus, url, doc)
    flat = _flatten(response)
    # A reply carrying nothing but id/type is an acknowledgement, not the record;
    # treating it as the new state would report the device as unsupported and its
    # toggle as off. Falling back to what we sent is truthful -- it is the state
    # the server accepted.
    if not flat or not (set(flat) - {"id", "type"}):
        flat = doc
    return _parse(flat)
