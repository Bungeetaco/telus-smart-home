"""Bluetooth arming devices -- the phones allowed to DISARM the alarm.

Backs enhancement-backlog items E24, E41 and E43. ``isDisarmingEnabledForDevice``
decides whether a paired phone walking up to the house disarms the panel, so this
module is written to the same rules as the siren route: it reads freely, it
writes only when a caller explicitly asked for that exact write, it checks a
capability flag first, and every ambiguity resolves to "refuse".

Three settings live here, all of them the same class of thing -- who may walk up
to a house and have the alarm turn itself off:

* per device, ``isDisarmingEnabledForDevice`` (E24) -- may THIS phone disarm.
* per device, ``enrolledPartitionId`` (E43) -- WHICH partition it disarms.
* per system, ``web/api/settings/panelBluetooth/bluetoothDisarmingSettings``
  (E41) -- whether the panel accepts Bluetooth pairing at all, plus a bare
  ``timeout``.

**THE PANEL-LEVEL WRITE SHAPE IS INFERRED, NOT CAPTURED.** Everything about the
per-device endpoint below was established by diffing a captured GET against the
PUT a real browser sent next, and then confirmed by a live PUT (backlog E44). No
PUT to the panel-level endpoint was ever captured. What is sent there is the
convention this API uses everywhere else -- the portal's own save is
``e.isBluetoothPairingEnabledForSystem = !…; e.save()``, an Ember model save,
which is what _write_type and _flatten implement -- applied to the one captured
GET (tests/fixtures/bluetooth_disarming_settings_get.json). It is an educated
reconstruction and it has never touched a live panel. See
set_bluetooth_disarming_settings for the two specific parts of it that are
guesses rather than deductions.

``timeout`` is a disarm window in MINUTES, chosen from a fixed set of five
values: **1, 5, 10, 20, 30**. Neither the unit nor the set appears anywhere in
the document, so a client cannot learn them from the payload -- they come from
the portal's own disarming-settings controller, which builds its picker as
``[1, 5, 10, 20, 30].map(n => intl.t("generic.minutes", {num: n}))`` and renders
the current value through the same string. That is why both are reported to
callers here and enforced on write; the panel is not asked to reject a value the
vendor never offers.

**Whether ``isBluetoothPairingEnabledForSystem: false`` makes a per-device
``isDisarmingEnabledForDevice: true`` inert is not known.** The per-device
settings page never consults the panel document, which is why it is not treated
as a capability gate here (see
tests/test_arming_devices.py::test_panel_disarming_settings_is_a_separate_feature),
but "the portal does not read it" is evidence about the PORTAL, not about what
the panel firmware does with the flag. Treat the two as independent, and do not
tell a user that turning one off disables the other.

alarm.com is the CONFIGURATION AUTHORITY for all three. The Control4 endpoint on
the same panel is local-access-only and independent of it, so what is written
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
   See _supports_toggle for exactly which flags and why those. The panel-level
   document publishes no capability flag of any kind, which is its own problem --
   see _supports_panel_field.

This module deliberately does NOT import from camera_settings even though the
two solve the same problem: that module is scoped to the ``web/api/video``
settings documents and its helpers carry assumptions (``*FeatureSupport`` enums,
per-field option lists) this endpoint has none of. _write_type and _flatten below
are small, deliberate re-implementations of its equivalents, kept in step with
them by tests/test_arming_devices.py rather than by a shared import.

Nothing here creates or renames a device. Unpairing is supported, deliberately
as a single-device operation only -- the portal also offers
``devices/bluetooth-arming-devices/deleteAll``, which is not implemented here
because one mis-aimed call would unpair every phone on the account at once.

**Unpairing is not undoable from this side.** Re-pairing needs physical access
to both the phone and the panel, so `unpair_arming_device` is deliberately not
reachable from anything a stray tap can hit.
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
# The PANEL-level document (E41). Scoped to the SYSTEM, not to a device: the id
# in the path and the id the document comes back carrying are both the alarm.com
# system id, i.e. the same value every authenticated read sends as Context-Id.
_PANEL_URL = ("{base}web/api/settings/panelBluetooth/bluetoothDisarmingSettings"
              "/{sid}")

# The attributes this module ever writes. Named once each so the read path, the
# capability check and the write path cannot drift apart.
_ENABLED = "isDisarmingEnabledForDevice"
_PARTITION = "enrolledPartitionId"
_PAIRING = "isBluetoothPairingEnabledForSystem"
_TIMEOUT = "timeout"
# The panel accepts a fixed set of disarm-window values, in minutes. Neither the
# unit nor the set appears in the document; both are what the portal's own
# picker offers. A value outside this set has never been observed being sent.
_TIMEOUT_UNIT = "minutes"
_TIMEOUT_OPTIONS = (1, 5, 10, 20, 30)


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

    def _member(ref):
        """One relationship member, inlined -- or None if it is not one.

        `type` must be a real string, not merely present: this API also uses
        plain objects as ATTRIBUTE values (`associatedCameraDeviceIds` is {}),
        and camera_settings' looser "is the key there" test walks into
        _write_type with a None and raises AttributeError mid-parse.
        """
        if not isinstance(ref, dict) or not isinstance(ref.get("type"), str) \
                or "id" not in ref:
            return None
        res = included.get((ref["type"], ref["id"]))
        if res is None:
            return {"type": _write_type(ref["type"]), "id": ref["id"]}
        sub = dict(res.get("attributes") or {})
        sub["id"] = res["id"]
        sub["type"] = _write_type(res["type"])
        return sub

    for name, body in (data.get("relationships") or {}).items():
        ref = (body or {}).get("data")
        # A TO-MANY relationship: JSON:API spells it as a list, and the only
        # document here that has one is the panel-level object's
        # `enrolledDevices` (E41). It is inlined as a list of the same members a
        # to-one would produce, which is a GUESS about the write shape and is
        # called out as one in set_bluetooth_disarming_settings -- but the
        # alternative, dropping it, is not a guess at all: a whole-document PUT
        # that omits a relationship deletes it, and deleting that one unpairs
        # every phone on the system with no way back that does not involve
        # standing at the panel with each handset (backlog E42). Naming exactly
        # the devices the server just named cannot remove any of them whatever
        # the endpoint does with the shape; omitting them can.
        #
        # Empty lists are preserved as empty lists for the same reason: "the
        # server said none" and "we dropped the key" must not collapse together.
        if isinstance(ref, list):
            members = [m for m in (_member(item) for item in ref) if m is not None]
            flat[name] = members
            continue
        member = _member(ref)
        if member is not None:
            flat[name] = member
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


def _supports_enrolled_partition(attrs: dict) -> bool:
    """Whether this record's enrolled partition may be written (E43).

    The same two record-level flags as _supports_toggle, for the same reasons:
    ``canBeSaved`` is this record's own "may this be saved" flag and
    ``canAccessWebSettings`` decides whether the settings page it lives on is
    reachable by a website client at all. Neither is specific to one attribute --
    they gate the SAVE, and there is exactly one save.

    What differs is the third condition, and it has to: this checks
    ``enrolledPartitionId``, not ``isDisarmingEnabledForDevice``. A record can
    perfectly well be missing one and not the other, and gating the partition
    write on the disarming flag's presence would offer the partition control on
    the strength of an unrelated field.

    The value must be a non-empty STRING. The API sends "1"; a record reporting
    an int, a None or "" is a record shape this parser does not understand, and
    the write path has to send back exactly what it read.

    Note this is deliberately NOT gated on the account having more than one
    partition. That is a real consideration and it belongs to whoever builds a
    control -- a picker with one option is worse than no picker -- but it is a
    fact about the ACCOUNT, not about this record's writability, and conflating
    the two would report a perfectly writable record as unwritable.
    """
    value = attrs.get(_PARTITION)
    return (isinstance(value, str) and bool(value.strip())
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

    It is the panel's own partition NUMBER, which is what partitions._parse
    reports as ``partition_id`` -- NOT the alarm.com device id that module
    reports as ``id`` ("<unit>-127"). Anything matching this against a partition
    list has to compare against the former, as strings.
    """
    return {
        "id": attrs.get("id"),
        "description": attrs.get("description") or attrs.get("id"),
        "disarming_enabled": bool(attrs.get(_ENABLED)),
        "enrolled_partition_id": attrs.get(_PARTITION),
        "supports_disarming_toggle": _supports_toggle(attrs),
        "supports_enrolled_partition": _supports_enrolled_partition(attrs),
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


# --- the writes -------------------------------------------------------------
async def _write_device(telus: TelusSession, device_id: str, changes: dict,
                        *, supports, refusal: str, field: str,
                        announce: str) -> dict:
    """Read-modify-write ``changes`` onto ONE arming device's whole record.

    Shared by both per-device setters because a PUT here REPLACES the record, so
    the read-modify-write, the capability gate and the reply handling all have to
    be identical whichever attribute is being changed -- two copies of this would
    be two chances for one of them to start sending a partial body.

    ``supports`` is the gate to run against the FRESHLY READ document (not
    against a list a caller is holding, which may be a poll old), and ``refusal``
    is what a record that fails it is told. ``field`` names the logical field for
    ArmingDeviceError, which control_server._settings_error reports back.

    ``announce`` is logged at INFO once the gate has passed and immediately
    before the PUT, so the log records writes that were actually attempted rather
    than every request that reached this function. It is the CALLER's job to
    build that string without a device description in it -- those are household
    members' names (see the coordinator's privacy note), so these lines identify
    a device by its opaque id.

    Setting the value a record already has still performs the PUT rather than
    short-circuiting: this API is the configuration authority, the caller
    explicitly asked for that state, and a no-op that reports success without
    confirming with the server would be indistinguishable from a write the
    server silently dropped.
    """
    url = _DEVICE_URL.format(base=c.TELUS_BASE, did=device_id)
    # _flatten's output is BOTH what gets mutated and what gets sent -- keeping
    # them the same object is what makes the edit reach the wire.
    doc = _flatten(await _get_doc(telus, url))
    if not supports(doc):
        raise ArmingDeviceError(refusal, unsupported=True, field=field)

    # Only the named attributes are touched; the other ~30 go back as they came.
    #
    # In particular `webSettings` is left as the INTEGER 7205 the read returned,
    # even though the browser was observed sending it as the STRING "7205" on
    # this same endpoint. Nothing establishes which form the endpoint requires,
    # so this echoes what the server itself just said -- the defensible half of
    # an undocumented discrepancy. Pinned by
    # tests/test_arming_devices.py::test_websettings_int_string_discrepancy...;
    # if a live test ever shows the integer being rejected, coerce it there.
    doc.update(changes)
    log.info("%s", announce)
    response = await _put_doc(telus, url, doc)
    flat = _flatten(response)
    # A reply carrying nothing but id/type is an acknowledgement, not the record;
    # treating it as the new state would report the device as unsupported and its
    # toggle as off. Falling back to what we sent is truthful -- it is the state
    # the server accepted.
    if not flat or not (set(flat) - {"id", "type"}):
        flat = doc
    return _parse(flat)


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

    return await _write_device(
        telus, device_id, {_ENABLED: enabled}, supports=_supports_toggle,
        refusal="this device does not support the bluetooth disarming toggle",
        field="disarming_enabled",
        announce=(f"Setting bluetooth disarming {'ON' if enabled else 'OFF'} "
                  f"for arming device {device_id}"))


async def set_enrolled_partition(telus: TelusSession, device_id: str,
                                 partition_id: str) -> dict:
    """Move ONE paired phone to a different partition (backlog E43).

    ``enrolledPartitionId`` decides WHICH partition that phone disarms, which
    makes it the same class of setting as the disarming toggle rather than a
    lesser one: moving a phone from a partition it should open to one it should
    not is the same outcome as enabling disarming on a phone that should not have
    it. It is treated identically -- capability checked against a fresh read,
    whole-document PUT, nothing written that a caller did not explicitly ask for.

    ``partition_id`` is the panel's own partition NUMBER as a STRING, matching
    what the record reports and what partitions._parse calls ``partition_id``. A
    non-string is REFUSED rather than coerced, before any request is made:
    ``str(1)`` would look harmless, but the write is a whole-record PUT and
    changing an identifier's JSON type on the way through is not something to do
    silently to a live panel. Whether the partition EXISTS is not checked here --
    this module has no partition list and would need a second upstream read to
    get one; control_server does that check, from the list it can already reach,
    before it calls this.

    Raises ArmingDeviceError with ``unsupported=True`` when the record itself
    says it cannot be saved (see _supports_enrolled_partition), and without it
    for a caller mistake.

    Returns the same shape as get_arming_device.
    """
    if not isinstance(partition_id, str) or not partition_id.strip():
        raise ArmingDeviceError(
            "partition_id takes a non-empty string, the partition number as the "
            "record reports it", field="enrolled_partition_id")

    return await _write_device(
        telus, device_id, {_PARTITION: partition_id},
        supports=_supports_enrolled_partition,
        refusal="this device does not support changing its enrolled partition",
        field="enrolled_partition_id",
        # The opaque device id and the partition number -- neither is a person's
        # name, unlike the record's `description`.
        announce=f"Enrolling arming device {device_id} on partition {partition_id}")


# --- the panel-level document (E41) -----------------------------------------
def _supports_panel_field(attrs: dict, name: str, kind: type) -> bool:
    """Whether the panel document's ``name`` may be written.

    This document publishes NO capability flag: its whole attribute set is
    ``deviceId``, ``isBluetoothPairingEnabledForSystem`` and ``timeout``. There
    is no ``*FeatureSupport`` enum, no ``canSetX``, and no ``canBeSaved`` -- so
    unlike every other write in this codebase there is nothing to gate on that
    the server itself provided.

    Rather than invent a gate or drop the idea of one, this uses the only signal
    the document actually carries: the field is present, and it holds a value of
    the type this client understands. That is the same third condition
    _supports_toggle applies on top of its two flags, and it fails closed in the
    two cases that matter -- a panel with no Bluetooth support at all (which
    404s, or answers a document without these fields) and a payload shape that
    has changed under us.

    What it deliberately does NOT do is claim to know whether the PANEL will
    accept the write. It cannot: nothing in the response says so. A caller
    offering a control on the strength of this is offering it on "the document
    exists and looks like the one this was written against", which is weaker
    than the per-device gate and is why it is spelled out here rather than
    hidden behind a boolean named after a capability.

    ``bool`` is checked by identity of type, not by isinstance, for the usual
    reason: ``isinstance(True, int)`` is True, so an int-typed field would accept
    a boolean and a whole-record PUT would write ``timeout: true`` to a panel.
    """
    value = attrs.get(name)
    if kind is bool:
        return isinstance(value, bool)
    return isinstance(value, kind) and not isinstance(value, bool)


def _parse_panel(attrs: dict) -> dict:
    """The caller-facing shape of the panel-level Bluetooth disarming document.

    ``timeout`` is reported as the bare number the panel sent, with NO unit and
    no bounds, because none of the three is published and none was established
    (see the module docstring). A caller that needs a range has to choose one and
    say that it chose it.

    ``enrolled_device_ids`` is the phones the panel says are paired, which is the
    same set get_arming_devices discovers from the catalog -- reported here so a
    caller can tell "this system has Bluetooth pairing configured" from "this
    document exists but nothing is paired to it" without a second read.
    """
    enrolled = attrs.get("enrolledDevices")
    return {
        "id": attrs.get("id"),
        "pairing_enabled": bool(attrs.get(_PAIRING)),
        "supports_pairing_enabled": _supports_panel_field(attrs, _PAIRING, bool),
        "timeout": attrs.get(_TIMEOUT),
        "supports_timeout": _supports_panel_field(attrs, _TIMEOUT, int),
        # Minutes, and only these five. Both come from the portal's own picker,
        # which maps [1, 5, 10, 20, 30] through its "generic.minutes" string.
        # The document itself publishes neither, so a caller has no way to learn
        # them from the payload -- which is exactly why they are reported here.
        "timeout_unit": _TIMEOUT_UNIT,
        "timeout_options": list(_TIMEOUT_OPTIONS),
        "enrolled_device_ids": [m.get("id") for m in enrolled
                                if isinstance(m, dict) and m.get("id")]
        if isinstance(enrolled, list) else [],
        # The untouched flat document, for when a panel returns a field this
        # parser does not recognize -- the same escape hatch _parse publishes.
        "raw": attrs,
    }


async def unpair_arming_device(telus: TelusSession, device_id: str) -> dict:
    """Unpair one Bluetooth arming device. **This cannot be undone from here.**

    Returns ``{"unpaired": <device_id>, "description": <name it had>}`` -- the
    description is read BEFORE the delete so a caller can say which phone it
    removed, which it could not do afterwards.

    Re-pairing needs physical access to the phone and the panel. There is no
    cloud-side undo, no trash, and nothing this bridge can call to reverse it.
    That is why this is one device at a time: the portal also offers a delete-all
    for the whole account, and one mis-aimed call to that would unpair every
    phone in the household at once, so it is not implemented.

    Membership is checked first, against the same catalog the list route reads.
    An id the catalog does not know is refused rather than turned into a DELETE
    at whatever that id happens to address -- the ordinary reason to care about
    that is a typo, and the consequence here is not recoverable.
    """
    known = {d["id"]: d for d in await get_arming_devices(telus)}
    record = known.get(device_id)
    if record is None:
        raise ArmingDeviceError(
            f"{device_id} is not a bluetooth arming device on this account",
            field="device_id")

    url = _DEVICE_URL.format(base=c.TELUS_BASE, did=device_id)
    # Logged at INFO with the id, before the call: an unpair nobody can find
    # afterwards in the log is the failure mode that matters for an operation
    # with no undo. The description is deliberately NOT logged -- it is a
    # person's phone name.
    log.info("Unpairing bluetooth arming device %s", device_id)
    async with telus.session.delete(url, headers=telus.api_headers) as resp:
        _raise_for_status(resp)
        await resp.read()

    return {"unpaired": device_id, "description": record.get("description")}


async def get_bluetooth_disarming_settings(telus: TelusSession,
                                           system_id: str | None = None) -> dict:
    """The PANEL-level Bluetooth disarming settings, in _parse_panel's shape.

    A pure read, and the only part of E41 that rests on captured evidence
    (tests/fixtures/bluetooth_disarming_settings_get.json).

    This is NOT the capability gate for the per-device disarming toggle, and must
    not be used as one: the portal's per-device settings page never consults it,
    and whether the panel firmware treats a system-wide ``false`` as a veto over
    a per-device ``true`` is unresolved (module docstring). Treating it as a gate
    would invent a veto this API has not been shown to have; reporting it as one
    would tell a user something that may be false.

    ``system_id`` defaults to the session's own Context-Id (see _system_id).
    """
    url = _PANEL_URL.format(base=c.TELUS_BASE, sid=_system_id(telus, system_id))
    return _parse_panel(_flatten(await _get_doc(telus, url)))


async def set_bluetooth_disarming_settings(telus: TelusSession, *,
                                           pairing_enabled: bool | None = None,
                                           timeout: int | None = None,
                                           system_id: str | None = None) -> dict:
    """Change the panel-level Bluetooth disarming settings. Returns the read shape.

    **The write shape here is INFERRED, not captured.** Read the module docstring
    before trusting it. Concretely, two parts of the body below are reconstructions
    rather than observations, and a live test would settle both at once:

    1. **``enrolledDevices`` is sent back as a LIST of ``{type, id}`` members.**
       Every relationship this API was observed writing is a to-one, inlined as a
       single such object; nothing establishes what it wants for a to-many. A
       list of the same members is the consistent extrapolation. See _flatten for
       why sending a possibly-wrong shape beats omitting the relationship.
    2. **The resource type is pluralized** to
       ``settings/panel-bluetooth/bluetooth-disarming-settings``, by the same
       _write_type rule the per-device endpoint was observed following. Whether
       this endpoint validates the type at all is unknown.

    Everything else follows the confirmed convention: flat body, whole document,
    every attribute the read returned resent untouched apart from the ones named.

    Only the arguments passed as non-None are changed; passing neither is an
    error, not a no-op. Both are checked against the freshly-read document before
    anything is mutated, so a partly-unsupported request leaves the panel
    untouched rather than half-applied.

    ``timeout`` is checked against _TIMEOUT_OPTIONS. The document publishes no
    bounds and no option list, so this client is the only thing between a caller
    and a value the panel was never offered -- and unlike a range, the offered set
    is known rather than guessed.
    """
    if pairing_enabled is None and timeout is None:
        raise ArmingDeviceError("no bluetooth disarming fields given")
    # Real types, checked before any request. bool("false") is True and
    # isinstance(True, int) is True, so both directions of the usual trap are
    # closed here rather than left to a whole-record PUT at a live panel.
    if pairing_enabled is not None and not isinstance(pairing_enabled, bool):
        raise ArmingDeviceError("pairing_enabled takes a boolean",
                                field="pairing_enabled")
    if timeout is not None and (isinstance(timeout, bool)
                                or not isinstance(timeout, int)):
        raise ArmingDeviceError("timeout takes an integer", field="timeout")
    if timeout is not None and timeout not in _TIMEOUT_OPTIONS:
        offered = ", ".join(str(o) for o in _TIMEOUT_OPTIONS)
        raise ArmingDeviceError(
            f"timeout={timeout} is not one of the disarm windows this panel "
            f"offers ({offered} {_TIMEOUT_UNIT})", field="timeout")

    url = _PANEL_URL.format(base=c.TELUS_BASE, sid=_system_id(telus, system_id))
    # _flatten's output is BOTH what gets mutated and what gets sent.
    doc = _flatten(await _get_doc(telus, url))
    if pairing_enabled is not None and not _supports_panel_field(doc, _PAIRING, bool):
        raise ArmingDeviceError(
            "this panel does not report a bluetooth pairing setting",
            unsupported=True, field="pairing_enabled")
    if timeout is not None and not _supports_panel_field(doc, _TIMEOUT, int):
        raise ArmingDeviceError(
            "this panel does not report a bluetooth disarming timeout",
            unsupported=True, field="timeout")

    if pairing_enabled is not None:
        doc[_PAIRING] = pairing_enabled
    if timeout is not None:
        doc[_TIMEOUT] = timeout
    log.info("Setting panel bluetooth disarming settings (pairing=%s timeout=%s)",
             pairing_enabled, timeout)
    response = await _put_doc(telus, url, doc)
    flat = _flatten(response)
    # Same fallback as the per-device write: an acknowledgement carrying only
    # id/type is not the document, and parsing it as one would report every field
    # as absent and therefore unsupported.
    if not flat or not (set(flat) - {"id", "type"}):
        flat = doc
    return _parse_panel(flat)
