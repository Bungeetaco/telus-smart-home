"""Camera audio / image / video settings and diagnostics, over the WEBSITE API.

Backs the settings half of the enhancement backlog's E9 ("volume control -- it
IS settable, via the website API", docs/talkdown-fullduplex-protocol.md): the
speaker volume talkdown plays at lives at
``PUT web/api/video/settings/audioSettings/{id}`` even though the mobile API's
``<duav>`` element reports ``SupportsVolumeAdjustment="false"``. Endpoint and
field inventory: docs/web-portal-api-findings.md.

Four properties of this API drive the whole shape of this module. All four were
established by diffing captured GET responses against the PUT bodies a real
browser sent immediately afterwards (tests/fixtures/), and all four fail
SILENTLY when got wrong -- the endpoint does not complain, the settings simply
never take:

1. **The read is JSON:API but the write is FLAT.** A GET returns
   ``{data: {...}, included: [...]}``; the PUT body is a single flat object --
   not wrapped in ``data``, and not the ``attributes`` sub-object on its own.
   See _flatten and _put_doc.
2. **The settings live in ``included``, not in ``data.attributes``.** The
   attributes of a ``videoConfigs`` document are just ``shouldDisplay*Link``
   flags; the clip settings are a separate resource reached through
   ``data.relationships``. Reading only the attributes finds nothing and reports
   the whole device as unsupported.
3. **A settings PUT replaces the whole object, not a patch.** Every setter here
   GETs the current document, mutates only the requested field(s) in the
   flattened form, and PUTs that entire object back. Sending a partial body
   risks resetting unrelated fields on a camera in someone's home.
4. **Every field is paired with a capability flag, spelled one of two ways** --
   a ``<name>FeatureSupport`` enum where only 2 means editable, or a plain
   ``canSetX``-style truthy flag (see _supported) -- and the same payload shape
   comes back for devices that cannot do the thing. Support is therefore checked
   before a control is reported or applied, and is reported upward as
   ``supports_*`` so a caller never offers a control the device does not have.

Out-of-range and off-list values are REFUSED rather than clamped onto the
nearest legal value: silently writing something other than what the caller asked
for to a camera in someone's home hides the mismatch from them.

Nothing in here writes anything unless a caller explicitly asked for that write;
the getters and ``get_diagnostics`` are pure reads.
"""
import logging
from typing import Any, NamedTuple

import aiohttp

from . import config as c
from .telus_auth import RateLimited, TelusSession

log = logging.getLogger(__name__)

# alarm.com's per-field capability enum. Only 0 (unsupported) and 2
# (supported/editable) were seen in the reference HAR; any other value is
# treated as not-editable, and so is an ABSENT enum -- fail closed, because the
# enum is the only signal distinguishing a camera that has the feature from one
# that merely returns the field, and writing a field a device never advertised
# is the failure mode with no local undo.
_SUPPORTED = 2

_AUDIO_URL = "{base}web/api/video/settings/audioSettings/{cid}"
_IMAGE_URL = "{base}web/api/video/settings/imageSettings/{cid}"
_VIDEO_URL = "{base}web/api/video/settings/videoConfigs/{cid}"
_GENERAL_URL = "{base}web/api/video/settings/generalSettings/{cid}"
_SIGNAL_URL = "{base}web/api/video/signalStrengthInfos/{cid}"
_BATTERY_URL = "{base}web/api/video/batteryStatusInfos/{cid}"
_DEVSTATUS_URL = "{base}web/api/video/settings/deviceStatusInfos/{cid}"

_CALL_VOLUME_MAX_FALLBACK = 10   # observed range 0-10 on the ADC-VDB750
_OUTDOOR_TONE_MAX_FALLBACK = 3   # observed range 0-3 on the same device


class CameraSettingsError(RuntimeError):
    """A settings read/write could not be performed.

    ``unsupported`` is True only when the DEVICE reports no support for the
    field (its ``*FeatureSupport`` enum is not 2, or the field is absent
    entirely) -- i.e. the caller asked for something real that this camera
    cannot do. It is False for a caller mistake (unknown field name, wrong value
    type) and for an upstream failure. Callers map those to different statuses,
    so branch on the flag; never parse the message text.

    ``field`` is the logical field name involved, or None.
    """

    def __init__(self, message: str, *, unsupported: bool = False,
                 field: str | None = None):
        self.unsupported = unsupported
        self.field = field
        super().__init__(message)


class _Field(NamedTuple):
    """One writable settings field: where it lives and what it may hold.

    Several candidate key names are carried per field because only the audio
    object's key names were captured verbatim; the alternatives are the
    spellings alarm.com uses elsewhere in the same payloads, and a different
    camera model returns a different subset anyway. Names are resolved by lookup
    against what the device actually returned, so a wrong guess degrades to
    "unsupported" -- no write -- instead of a write against a misnamed key.
    """

    container: str | None       # nested settings object, or None for the top level
    names: tuple[str, ...]      # candidate API key names for the value itself
    support: tuple[str, ...]    # candidate names for its *FeatureSupport companion
    kind: str                   # "int", "bool" or "str"


# imageSettings. Key names, capability flags and bounds below are taken from
# captured GET/PUT pairs for this endpoint (tests/fixtures/image_settings_*),
# not guessed. Two things differ from the audio/video objects and are easy to
# get wrong: the capability flags are `canSetX` booleans rather than
# `<name>FeatureSupport` enums (and `canSetHdr` is the integer 1, not a bool),
# and the bounds use the PREFIX spelling `minBrightness`/`maxBrightness` rather
# than `brightnessMin`/`brightnessMax`. _supported and _limit handle both; the
# tables must still name the real keys.
_IMAGE_FIELDS: dict[str, _Field] = {
    "brightness": _Field(None, ("brightness",), ("canSetBrightness",), "int"),
    "contrast": _Field(None, ("contrast",), ("canSetContrast",), "int"),
    "saturation": _Field(None, ("saturation",), ("canSetSaturation",), "int"),
    "sharpness": _Field(None, ("sharpness",), ("canSetSharpness",), "int"),
    "is_flipped": _Field(None, ("isFlipped",), ("canFlipImage",), "bool"),
    "hdr_enabled": _Field(None, ("isHdrEnabled",), ("canSetHdr",), "bool"),
    "max_exposure": _Field(None, ("maxExposure",), ("canSetMaxExposure",), "int"),
    "ir_led_enabled": _Field("infraredSetting", ("isInfraredLedEnabled",),
                             ("canControlInfraredLeds",), "bool"),
    "ir_sensitivity": _Field("infraredSetting", ("infraredSensitivity",),
                             ("canControlInfraredSensitivity",), "int"),
    "night_vision_enabled": _Field("infraredSetting", ("isNightVisionEnabled",),
                                   ("canControlNightVision",), "bool"),
}

# videoConfigs. Every one of these lives in the `savedVideoSetting` relationship,
# NOT in the videoConfigs document's own attributes -- that object holds only
# `shouldDisplay*Link` flags. _flatten inlines it under this name.
#
# `keep_clips_days` is the odd one out twice over: its capability flag is the
# bool `isKeepClipsForFeatureSupported` rather than an enum, and -1 is a real,
# meaningful option ("keep forever"), so it must survive range/option checking.
_VIDEO_FIELDS: dict[str, _Field] = {
    "clip_resolution": _Field("savedVideoSetting", ("selectedClipResolution",),
                              ("clipResolutionFeatureSupport",), "str"),
    "clip_quality": _Field("savedVideoSetting", ("selectedClipQuality",),
                           ("clipQualityFeatureSupport",), "str"),
    "keep_clips_days": _Field("savedVideoSetting", ("selectedKeepClipsDays",),
                              ("isKeepClipsForFeatureSupported",), "int"),
    "alarm_clip_duration": _Field("savedVideoSetting", ("selectedAlarmClipsDuration",),
                                  ("alarmClipsDurationFeatureSupport",), "int"),
    "camera_triggered_clip_duration": _Field(
        "savedVideoSetting", ("selectedCameraTriggeredClipsDuration",),
        ("cameraTriggeredClipsFeatureSupport",), "int"),
    "pretrigger_enabled": _Field("savedVideoSetting", ("isPreTriggerRecordingEnabled",),
                                 ("preTriggerRecordingFeatureSupport",), "bool"),
}


# generalSettings -> ledLightSetting. This is the device's STATUS indicator light
# (alarm.com's own UI calls it "Camera Status LED"), not the IR illuminator -- the
# IR LEDs are `ir_led_enabled` in _IMAGE_FIELDS, a different object on a different
# endpoint. Capability flags here are the `canX` boolean style, like imageSettings.
#
# The reference doorbell reports canTurnOnOffLedLight true with
# canChangeLedBrightness false, and publishes NO bounds for ledBrightness -- so
# brightness resolves as unsupported there and _coerce range-checks nothing. A
# model that does support it is expected to publish its own bounds; see
# _read_field, which reports whichever of them it finds.
_LED_FIELDS: dict[str, _Field] = {
    "enabled": _Field("ledLightSetting", ("isLedLightEnabled",),
                      ("canTurnOnOffLedLight",), "bool"),
    "brightness": _Field("ledLightSetting", ("ledBrightness",),
                         ("canChangeLedBrightness",), "int"),
}

# --- low-level HTTP ---------------------------------------------------------
def _raise_for_status(resp: aiohttp.ClientResponse) -> None:
    """Turn a non-2xx settings response into the right exception type.

    429 becomes telus_auth.RateLimited carrying the server's own Retry-After, so
    a caller sleeps the requested wait instead of tight-retrying the same edge.
    401/403 are re-raised as the raw aiohttp.ClientResponseError because
    control_server._fetch_with_relogin keys its one-shot re-login off that type
    and status -- wrapping an expired cookie here would turn a recoverable
    session expiry into a hard upstream failure.
    """
    if resp.status == 429:
        raise RateLimited(c.parse_retry_after(resp.headers.get("Retry-After")))
    if resp.status in (401, 403):
        resp.raise_for_status()
    if resp.status >= 400:
        # Path only, never the full URL: query strings on this API can carry
        # session-scoped material and this string reaches logs and callers.
        raise CameraSettingsError(f"TELUS returned HTTP {resp.status} for {resp.url.path}")


async def _get_doc(telus: TelusSession, url: str, *,
                   missing_ok: bool = False) -> dict | None:
    """GET one JSON:API settings/diagnostics document.

    With ``missing_ok`` a 404 returns None instead of raising -- used only for
    the diagnostics endpoints, where a device legitimately has no such record
    (a mains-powered camera has no battery status). 401/403/429/5xx still raise
    in that mode, so an auth or rate-limit problem is never mistaken for
    "device doesn't have this".
    """
    async with telus.session.get(url, headers=telus.api_headers) as resp:
        if missing_ok and resp.status == 404:
            return None
        _raise_for_status(resp)
        return await resp.json(content_type=None)


async def _put_doc(telus: TelusSession, url: str, doc: dict) -> dict | None:
    """PUT a whole settings document back and return the server's version of it.

    The body is the FLATTENED document (see _flatten), sent as the raw JSON
    object -- NOT wrapped in a JSON:API ``{"data": ...}`` envelope, and not the
    ``attributes`` sub-object on its own. This was confirmed by diffing the
    request bodies a real browser session sent against the documents it had just
    GETed: the write side of this API is flat even though the read side is
    JSON:API. Wrapping it in ``data`` produces a request the endpoint does not
    understand.

    Request bodies are plain application/json (aiohttp's ``json=`` sets that)
    even though responses come back as application/vnd.api+json.
    """
    async with telus.session.put(url, json=doc, headers=telus.api_headers) as resp:
        _raise_for_status(resp)
        try:
            return await resp.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            # Some settings PUTs answer 204/empty; the caller falls back to the
            # document it just sent.
            return None


def _write_type(name: str) -> str:
    """The resource type spelling the WRITE side of this API uses.

    alarm.com serves singular JSON:API types (``video/settings/audio-setting``)
    but the browser sends the plural back (``video/settings/audio-settings``),
    consistently, for the top-level document and every inlined relationship
    alike -- an Ember Data convention showing through. Whether the endpoint
    actually validates it is unknown and untested, so this sends exactly what a
    working client was observed to send rather than betting that it does not.
    """
    return name if name.endswith("s") else name + "s"


def _flatten(doc: dict | None) -> dict:
    """Collapse a JSON:API settings document into the flat object this API writes.

    alarm.com serves these settings as a JSON:API document -- the interesting
    sub-objects (``microphoneSpeakerSetting``, ``savedVideoSetting``,
    ``infraredSetting``, ...) are NOT nested inside ``data.attributes``; they sit
    in the top-level ``included`` array and are referenced from
    ``data.relationships`` by name. Reading only ``data.attributes`` therefore
    yields a handful of link flags and none of the settings, which is the trap
    this function exists to close.

    The flat form is ``data.attributes`` plus ``id``/``type``, with each
    relationship inlined under its relationship name carrying its own
    ``id``/``type``. A relationship whose resource is absent from ``included``
    becomes a ``{"id", "type"}`` stub, which is exactly what the browser sends
    for one it did not load. Verified key-for-key against captured
    request/response pairs for all three settings endpoints (see
    tests/fixtures/).

    Returns {} for anything unrecognized so callers never have to guard the
    shape. Tolerates a collection response, since the diagnostics endpoints
    return ``data`` as a list while the settings endpoints return one object.
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
        if not isinstance(ref, dict) or "type" not in ref or "id" not in ref:
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


def _flatten_result(response: dict | None, sent: dict) -> dict:
    """The flat document a PUT left us in.

    Some settings PUTs answer 204/empty and some echo the document back. Falling
    back to what we just sent is right in both cases: it is the state we asked
    for, and re-reading to find out would cost another request against an API
    with undocumented rate limits.
    """
    if response is None:
        return sent
    flat = _flatten(response)
    # A document carrying nothing but id/type is an acknowledgement, not the
    # settings -- treating it as the new state would report every field as absent
    # and, through _parse_table, as unsupported. Falling back to what we sent is
    # both truthful and the state we asked for.
    if not flat or not (set(flat) - {"id", "type"}):
        return sent
    return flat


def _obj(attrs: dict, container: str | None) -> dict:
    """The nested settings object named by ``container`` (or ``attrs`` itself)."""
    if container is None:
        return attrs
    nested = attrs.get(container)
    return nested if isinstance(nested, dict) else {}


# --- field helpers ----------------------------------------------------------
def _resolve(obj: dict, names: tuple[str, ...]) -> str | None:
    """First candidate key actually present in ``obj``, or None."""
    for name in names:
        if name in obj:
            return name
    return None


def _base(name: str) -> str:
    """The stem that a field's Min/Max/Options companions are built from.

    Two spellings appear in the same API and both are handled here:
    ``callVolumeValue`` pairs with ``callVolumeMin``/``callVolumeMax`` (drop the
    trailing "Value"), while ``selectedClipResolution`` pairs with
    ``clipResolutionOptions`` (drop the leading "selected" and lower-case what
    is left). Anything else keeps its whole name.
    """
    if name.endswith("Value"):
        return name[:-5]
    if name.startswith("selected") and len(name) > 8:
        rest = name[8:]
        return rest[0].lower() + rest[1:]
    return name


def _limit(obj: dict, base: str, which: str) -> int | None:
    """A field's published lower/upper bound, or None if it publishes none.

    ``which`` is "Min" or "Max". Both spellings this API uses are accepted: the
    suffix form (``callVolumeMin``) on the audio/video objects and the prefix
    form (``minBrightness``) on the image object. None means "the device did not
    say", which is NOT the same as zero -- treating an absent bound as 0 would
    clamp every brightness write up to 0 on a camera whose real range is -5..5.
    """
    suffix = f"{base}{which}"
    if suffix in obj:
        return _as_int(obj.get(suffix), None)
    prefix = which.lower() + base[0].upper() + base[1:]
    if prefix in obj:
        return _as_int(obj.get(prefix), None)
    return None


def _options(obj: dict, base: str) -> list | None:
    """The choice list a field publishes, normalized to a list, or None.

    alarm.com publishes these as either a JSON array (``keepClipsDaysOptions``
    is ``[3, 5, ..., -1]``) or an object (``clipResolutionOptions`` is
    ``{"1": "480x480", "4": "960x960", "7": "1440x1440"}``).

    Trap, and the reason both halves of an object are returned: the API is not
    self-consistent about which half of that mapping is the settable value.
    ``selectedClipResolution`` is ``"1440x1440"`` -- a VALUE of the mapping --
    while ``selectedClipQuality`` is ``"3"`` -- a KEY of ``clipQualityOptions``
    (``{"5": 3, "3": 2, "2": 1}``). No capture disambiguates them, so membership
    is checked against keys and values together. That accepts a wrong-half value
    the camera might reject, but it never silently sends something absent from
    the mapping altogether.
    """
    raw = obj.get(f"{base}Options")
    if isinstance(raw, dict) and raw:
        merged = [str(k) for k in raw] + [str(v) for v in raw.values()]
        return list(dict.fromkeys(merged))
    if isinstance(raw, list) and raw:
        return [o.get("value", o.get("id")) if isinstance(o, dict) else o for o in raw]
    return None


def _supported(obj: dict, support: tuple[str, ...]) -> bool:
    """Whether the device advertises this field as editable.

    This API uses TWO capability spellings, and which one applies is decided by
    the key's own name rather than by its value:

    * ``<name>FeatureSupport`` is an enum -- only ``2`` means supported/editable.
      ``0`` is the "has the field but cannot set it" case, and it is truthy
      nowhere but Python, which is the whole reason this is not a bool test.
    * everything else (``canSetBrightness``, ``canFlipImage``,
      ``canControlInfraredLeds``, ``isKeepClipsForFeatureSupported``) is a plain
      capability flag, and is truthy/falsey. It is not always a bool: ``canSetHdr``
      comes back as the integer ``1``.

    False when no companion flag is present at all: absence is unknown, and
    unknown must not be offered or written (see the module docstring).
    """
    for name in support:
        if name not in obj:
            continue
        value = obj.get(name)
        if name.endswith("FeatureSupport"):
            return value == _SUPPORTED
        if isinstance(value, bool):
            return value
        if isinstance(value, int):
            return value != 0
        return bool(value)
    return False


def _as_int(value: Any, default):
    """Int form of an API value, tolerating the string/float/None forms."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce(field_name: str, field: _Field, obj: dict, value_key: str, value: Any) -> Any:
    """Validate one requested value against the device's own published limits.

    Out-of-range and off-list values are REFUSED, never clamped or coerced onto
    the nearest legal value. Silently writing something other than what the
    caller asked for to a camera in someone's home hides the mismatch from them;
    a 400 does not. The device's published range/list is authoritative, and a
    device that publishes neither is not second-guessed -- TELUS is then the
    judge.

    Raises CameraSettingsError with ``unsupported=False`` for a caller mistake
    (wrong type, out of range, off the option list): the value is refused
    because it is wrong, not because the camera lacks the feature.
    """
    if field.kind == "bool":
        if not isinstance(value, bool):
            raise CameraSettingsError(f"{field_name} takes a boolean", field=field_name)
        return value

    base = _base(value_key)
    options = _options(obj, base)

    if field.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise CameraSettingsError(f"{field_name} takes an integer", field=field_name)
        low, high = _limit(obj, base, "Min"), _limit(obj, base, "Max")
        if (low is not None and value < low) or (high is not None and value > high):
            raise CameraSettingsError(
                f"{field_name}={value} is outside the range this camera reports "
                f"({low}..{high})", field=field_name)
        if options is not None and str(value) not in {str(o) for o in options}:
            raise CameraSettingsError(
                f"{field_name}={value} is not one of the values this camera offers "
                f"({', '.join(str(o) for o in options)})", field=field_name)
        return value

    if not isinstance(value, str):
        raise CameraSettingsError(f"{field_name} takes a string", field=field_name)
    if options is not None and value not in {str(o) for o in options}:
        raise CameraSettingsError(
            f"{field_name}={value!r} is not one of the values this camera offers",
            field=field_name)
    return value


def _read_field(obj: dict, field: _Field) -> dict[str, Any]:
    """Value + limits + options for one field, as the flat keys getters return."""
    key = _resolve(obj, field.names)
    if key is None:
        return {}
    base = _base(key)
    out: dict[str, Any] = {"value": obj.get(key)}
    if field.kind == "int":
        out["value"] = _as_int(obj.get(key), 0)
        for which in ("Min", "Max"):
            bound = _limit(obj, base, which)
            if bound is not None:
                out[which.lower()] = bound
    elif field.kind == "bool":
        out["value"] = bool(obj.get(key))
    options = _options(obj, base)
    if options is not None:
        out["options"] = options
    return out


def _parse_table(attrs: dict, table: dict[str, _Field]) -> dict[str, Any]:
    """Flatten a settings object into {name, name_min/_max/_options, supports_name}."""
    parsed: dict[str, Any] = {}
    for name, field in table.items():
        obj = _obj(attrs, field.container)
        read = _read_field(obj, field)
        parsed[f"supports_{name}"] = bool(read) and _supported(obj, field.support)
        if not read:
            parsed[name] = None
            continue
        parsed[name] = read["value"]
        for extra in ("min", "max", "options"):
            if extra in read:
                parsed[f"{name}_{extra}"] = read[extra]
    parsed["raw"] = attrs
    return parsed


async def _apply_table(telus: TelusSession, url: str, table: dict[str, _Field],
                       fields: dict[str, Any], label: str) -> dict:
    """Read-modify-write the fields in ``fields`` onto the document at ``url``.

    Every requested field is validated against the freshly-read document BEFORE
    anything is mutated, so a request that is partly unsupported is rejected
    whole instead of half-applied to a live camera.
    """
    if not fields:
        raise CameraSettingsError(f"no {label} fields given")
    unknown = set(fields) - set(table)
    if unknown:
        raise CameraSettingsError(
            f"unknown {label} field(s): {', '.join(sorted(unknown))}")

    # _flatten, not the raw document: the settings live in `included`, and the
    # flat form is BOTH what we mutate and what we send back -- keeping them the
    # same object is what makes the edit reach the wire.
    doc = _flatten(await _get_doc(telus, url))
    attrs = doc
    pending = []
    for name, value in fields.items():
        field = table[name]
        obj = _obj(attrs, field.container)
        key = _resolve(obj, field.names)
        if key is None or not _supported(obj, field.support):
            raise CameraSettingsError(f"this camera does not support {name}",
                                      unsupported=True, field=name)
        pending.append((obj, key, _coerce(name, field, obj, key, value)))

    for obj, key, value in pending:
        obj[key] = value
    log.info("Applying %s settings on camera: %s", label, ", ".join(sorted(fields)))
    return _flatten_result(await _put_doc(telus, url, doc), doc)


# --- audio ------------------------------------------------------------------
def _parse_audio(attrs: dict) -> dict:
    """Flatten the audioSettings document into the shape callers consume.

    Capability keys here were read off a captured PUT body
    (tests/fixtures/audio_settings_put.json) rather than inferred. The chime
    object names its enums `<thing>EnableDisableFeatureSupport`, which is why
    the obvious `isIndoorChimeOnFeatureSupport` spelling finds nothing.

    Worth knowing about the reference device (an ADC-VDB750 doorbell):
    `callVolumeFeatureSupport` is 2 (settable, 0-10) while
    `outdoorToneVolumeFeatureSupport` is 0 -- the outdoor tone can be turned on
    and off but its volume cannot be changed. The talk/call volume and the
    outdoor tone volume are different things; only the first affects talkdown.
    """
    mic = attrs.get("microphoneSpeakerSetting") or {}
    chime = attrs.get("chimeToneSetting") or {}
    return {
        "supports_call_volume": _supported(mic, ("callVolumeFeatureSupport",)),
        "call_volume": _as_int(mic.get("callVolumeValue"), 0),
        "call_volume_min": _as_int(mic.get("callVolumeMin"), 0),
        # Fall back to the range observed live rather than to 0, which would
        # otherwise refuse every write as out of range.
        "call_volume_max": _as_int(mic.get("callVolumeMax"), _CALL_VOLUME_MAX_FALLBACK),
        "mic_and_speaker_on": bool(mic.get("areMicrophoneAndSpeakerOn")),
        "supports_indoor_chime": _supported(
            chime, ("indoorChimeEnableDisableFeatureSupport",)),
        "indoor_chime_on": bool(chime.get("isIndoorChimeOn")),
        "supports_chime_type": _supported(chime, ("indoorChimeTypeFeatureSupport",)),
        "chime_type": _as_int(chime.get("selectedChimeType"), None),
        "chime_type_options": _options(chime, "chimeType"),
        "supports_outdoor_tone": _supported(
            chime, ("outdoorToneEnableDisableFeatureSupport",)),
        "outdoor_tone_on": bool(chime.get("isOutdoorToneOn")),
        "supports_outdoor_tone_volume": _supported(
            chime, ("outdoorToneVolumeFeatureSupport",)),
        "outdoor_tone_volume": _as_int(chime.get("outdoorToneVolumeValue"), 0),
        "outdoor_tone_volume_min": _as_int(chime.get("outdoorToneVolumeMin"), 0),
        "outdoor_tone_volume_max": _as_int(chime.get("outdoorToneVolumeMax"),
                                           _OUTDOOR_TONE_MAX_FALLBACK),
        "raw": attrs,
    }


async def get_audio_settings(telus: TelusSession, camera_id: str) -> dict:
    """Speaker/mic and chime settings for one camera.

    Returns ``supports_call_volume``, ``call_volume`` and its
    ``call_volume_min``/``call_volume_max`` bounds, ``mic_and_speaker_on``,
    ``supports_indoor_chime``/``indoor_chime_on``,
    ``supports_outdoor_tone``/``outdoor_tone_on``/``outdoor_tone_volume``/
    ``outdoor_tone_volume_max``, and ``raw`` (the untouched attributes object).

    Trap: the ``supports_*`` flags are not decoration. A camera that cannot set
    call volume returns the same ``callVolumeValue`` shape as one that can, so a
    caller that ignores the flag will happily show a control that does nothing --
    and ``raw`` is the place to look when a model reports a field this parser
    does not recognize.
    """
    url = _AUDIO_URL.format(base=c.TELUS_BASE, cid=camera_id)
    return _parse_audio(_flatten(await _get_doc(telus, url)))


async def set_call_volume(telus: TelusSession, camera_id: str, volume: int) -> dict:
    """Set the speaker/call volume and return the get_audio_settings() shape.

    This is the volume talkdown plays at (backlog E9). A ``volume`` outside the
    camera's advertised [min, max] is REFUSED rather than clamped: how the device
    reacts to an out-of-range value is untested against the live account, and
    quietly writing a different volume than the caller asked for would hide the
    mismatch from them.

    Raises CameraSettingsError with ``unsupported=True`` when the camera reports
    no call-volume support -- check that before offering the control.
    """
    if isinstance(volume, bool) or not isinstance(volume, int):
        raise CameraSettingsError("volume takes an integer", field="call_volume")

    url = _AUDIO_URL.format(base=c.TELUS_BASE, cid=camera_id)
    # _flatten, not the raw document: the settings live in `included`, and the
    # flat form is BOTH what we mutate and what we send back -- keeping them the
    # same object is what makes the edit reach the wire.
    doc = _flatten(await _get_doc(telus, url))
    attrs = doc
    mic = attrs.get("microphoneSpeakerSetting")
    if not isinstance(mic, dict) or not _supported(mic, ("callVolumeFeatureSupport",)):
        raise CameraSettingsError("this camera does not support call volume",
                                  unsupported=True, field="call_volume")

    low = _as_int(mic.get("callVolumeMin"), 0)
    high = _as_int(mic.get("callVolumeMax"), _CALL_VOLUME_MAX_FALLBACK)
    if not low <= volume <= high:
        raise CameraSettingsError(
            f"volume={volume} is outside the range this camera reports ({low}..{high})",
            field="call_volume")
    mic["callVolumeValue"] = volume
    log.info("Setting call volume on camera %s to %s", camera_id, mic["callVolumeValue"])
    return _parse_audio(_flatten_result(await _put_doc(telus, url, doc), doc))


async def set_chime(telus: TelusSession, camera_id: str, *,
                    indoor_on: bool | None = None,
                    chime_type: int | None = None,
                    outdoor_on: bool | None = None,
                    outdoor_volume: int | None = None) -> dict:
    """Set the doorbell's indoor chime / outdoor tone; returns the audio shape.

    Only the arguments passed as non-None are changed. An ``outdoor_volume``
    outside the camera's advertised range (0-3 on the device this was built
    against) is refused, for the same reason as set_call_volume. Note the
    reference doorbell reports `outdoorToneVolumeFeatureSupport` 0 -- it can mute
    the outdoor tone but not change its level -- so this argument raises
    ``unsupported`` there while ``outdoor_on`` works.

    ``chime_type`` is the style of chime the doorbell is WIRED TO, not a ringtone
    -- see CHIME_TYPES. It must be one of the values the camera publishes in
    ``chimeTypeOptions``; anything else is refused as a caller mistake. Setting it
    to CHIME_TYPE_NONE is what the portal treats as "no wired chime", and it then
    disables the indoor-chime toggle (see _parse_audio's indoor_chime_available).

    Raises CameraSettingsError with ``unsupported=True`` if any requested part is
    unsupported -- checked for ALL requested fields before anything is written,
    so a partly-unsupported request leaves the camera untouched rather than
    half-applied. Passing no arguments at all is an error, not a no-op.
    """
    if (indoor_on is None and chime_type is None and outdoor_on is None
            and outdoor_volume is None):
        raise CameraSettingsError("no chime fields given")
    if chime_type is not None and (isinstance(chime_type, bool)
                                   or not isinstance(chime_type, int)):
        raise CameraSettingsError("chime_type takes an integer", field="chime_type")
    if outdoor_volume is not None and (isinstance(outdoor_volume, bool)
                                       or not isinstance(outdoor_volume, int)):
        raise CameraSettingsError("outdoor_volume takes an integer",
                                  field="outdoor_tone_volume")

    url = _AUDIO_URL.format(base=c.TELUS_BASE, cid=camera_id)
    # _flatten, not the raw document: the settings live in `included`, and the
    # flat form is BOTH what we mutate and what we send back -- keeping them the
    # same object is what makes the edit reach the wire.
    doc = _flatten(await _get_doc(telus, url))
    attrs = doc
    chime = attrs.get("chimeToneSetting")
    if not isinstance(chime, dict):
        raise CameraSettingsError("this camera has no chime settings",
                                  unsupported=True, field="chime")

    indoor_ok = _supported(chime, ("indoorChimeEnableDisableFeatureSupport",))
    outdoor_ok = _supported(chime, ("outdoorToneEnableDisableFeatureSupport",))
    volume_ok = _supported(chime, ("outdoorToneVolumeFeatureSupport",))
    type_ok = _supported(chime, ("indoorChimeTypeFeatureSupport",))
    if chime_type is not None and not type_ok:
        raise CameraSettingsError("this camera does not support selecting a chime type",
                                  unsupported=True, field="chime_type")
    if indoor_on is not None and not indoor_ok:
        raise CameraSettingsError("this camera does not support the indoor chime",
                                  unsupported=True, field="indoor_chime_on")
    if outdoor_on is not None and not outdoor_ok:
        raise CameraSettingsError("this camera does not support the outdoor tone",
                                  unsupported=True, field="outdoor_tone_on")
    if outdoor_volume is not None and not volume_ok:
        raise CameraSettingsError("this camera does not support outdoor tone volume",
                                  unsupported=True, field="outdoor_tone_volume")

    if chime_type is not None:
        offered = _options(chime, "chimeType")
        if offered is not None and chime_type not in offered:
            raise CameraSettingsError(
                f"chime_type={chime_type} is not one of the values this camera offers "
                f"({', '.join(str(o) for o in offered)})", field="chime_type")
        chime["selectedChimeType"] = chime_type
    if indoor_on is not None:
        chime["isIndoorChimeOn"] = bool(indoor_on)
    if outdoor_on is not None:
        chime["isOutdoorToneOn"] = bool(outdoor_on)
    if outdoor_volume is not None:
        low = _as_int(chime.get("outdoorToneVolumeMin"), 0)
        high = _as_int(chime.get("outdoorToneVolumeMax"), _OUTDOOR_TONE_MAX_FALLBACK)
        if not low <= outdoor_volume <= high:
            raise CameraSettingsError(
                f"outdoor_volume={outdoor_volume} is outside the range this camera "
                f"reports ({low}..{high})", field="outdoor_tone_volume")
        chime["outdoorToneVolumeValue"] = outdoor_volume
    log.info("Setting chime on camera %s (indoor=%s type=%s outdoor=%s volume=%s)",
             camera_id, indoor_on, chime_type, outdoor_on, outdoor_volume)
    return _parse_audio(_flatten_result(await _put_doc(telus, url, doc), doc))


# --- image ------------------------------------------------------------------
async def get_image_settings(telus: TelusSession, camera_id: str) -> dict:
    """Image settings for one camera.

    Returns one entry per accepted key of set_image_settings (``brightness``,
    ``contrast``, ``saturation``, ``sharpness``, ``is_flipped``, ``hdr_enabled``,
    ``ir_led_enabled``, ``ir_sensitivity``, ``night_vision_enabled``), each with a
    ``supports_<key>`` flag and, where the camera publishes them,
    ``<key>_min``/``<key>_max``/``<key>_options``. A key this camera does not
    report at all is None with ``supports_<key>`` False. ``raw`` carries the
    untouched attributes object.
    """
    url = _IMAGE_URL.format(base=c.TELUS_BASE, cid=camera_id)
    return _parse_table(_flatten(await _get_doc(telus, url)), _IMAGE_FIELDS)


async def set_image_settings(telus: TelusSession, camera_id: str, **fields) -> dict:
    """Change image settings; returns the get_image_settings() shape.

    Accepted keys: brightness, contrast, saturation, sharpness, is_flipped,
    hdr_enabled, ir_led_enabled, ir_sensitivity, night_vision_enabled. Numeric
    values are clamped into the camera's advertised range.

    Raises CameraSettingsError -- with ``unsupported=True`` when the camera
    reports no support for a requested field, without it for an unknown key or a
    wrong value type. Support is checked for every field before any is written,
    so a rejected request changes nothing.
    """
    url = _IMAGE_URL.format(base=c.TELUS_BASE, cid=camera_id)
    flat = await _apply_table(telus, url, _IMAGE_FIELDS, fields, "image")
    return _parse_table(flat, _IMAGE_FIELDS)


# --- video ------------------------------------------------------------------
async def get_video_config(telus: TelusSession, camera_id: str) -> dict:
    """Clip/recording configuration for one camera.

    Same flattened shape as get_image_settings, over the accepted keys of
    set_video_config (``clip_resolution``, ``clip_quality``,
    ``pretrigger_enabled``, ``keep_clips_days``, ``alarm_clip_duration``), plus
    ``<key>_options`` for the fields the camera publishes a choice list for, and
    ``raw``.
    """
    url = _VIDEO_URL.format(base=c.TELUS_BASE, cid=camera_id)
    return _parse_table(_flatten(await _get_doc(telus, url)), _VIDEO_FIELDS)


async def set_video_config(telus: TelusSession, camera_id: str, **fields) -> dict:
    """Change clip/recording configuration; returns the get_video_config() shape.

    Accepted keys: clip_resolution, clip_quality, pretrigger_enabled,
    keep_clips_days, alarm_clip_duration. A value for a field whose ``*Options``
    list the camera publishes must be one of those options, and numeric values
    are clamped into the advertised range.

    Raises CameraSettingsError, with ``unsupported=True`` for a field (or an
    option value) this camera does not offer. All-or-nothing, like
    set_image_settings.
    """
    url = _VIDEO_URL.format(base=c.TELUS_BASE, cid=camera_id)
    flat = await _apply_table(telus, url, _VIDEO_FIELDS, fields, "video")
    return _parse_table(flat, _VIDEO_FIELDS)


# --- status LED -------------------------------------------------------------
async def get_led_settings(telus: TelusSession, camera_id: str) -> dict:
    """The device's status-indicator LED, from the generalSettings document.

    Returns ``supports_enabled``/``enabled`` and
    ``supports_brightness``/``brightness`` (plus ``brightness_min``/``_max`` if
    the camera publishes them), and ``raw``.

    Not to be confused with the infrared illuminator, which is `ir_led_enabled`
    in the IMAGE settings -- a different field on a different endpoint. This one
    is the little light that says the camera is powered/recording.

    The same generalSettings document also carries the doorbell's notification
    toggles and its solar-panel status, neither of which is modelled here; `raw`
    holds them for anyone who wants to look.
    """
    url = _GENERAL_URL.format(base=c.TELUS_BASE, cid=camera_id)
    return _parse_table(_flatten(await _get_doc(telus, url)), _LED_FIELDS)


async def set_led_settings(telus: TelusSession, camera_id: str, **fields) -> dict:
    """Turn the status LED on/off or set its brightness; returns the read shape.

    Accepted keys: ``enabled`` (bool), ``brightness`` (int).

    Raises CameraSettingsError with ``unsupported=True`` when the camera reports
    it cannot do the requested part -- the reference doorbell can toggle the LED
    but not dim it, so ``brightness`` raises there while ``enabled`` works. Both
    are checked before anything is written, so a partly-unsupported request
    leaves the device untouched.

    Read-modify-write like every other setter here: the PUT replaces the whole
    generalSettings document, so the notification and solar-panel objects
    alongside the LED are resent exactly as they were read.
    """
    url = _GENERAL_URL.format(base=c.TELUS_BASE, cid=camera_id)
    flat = await _apply_table(telus, url, _LED_FIELDS, fields, "LED")
    return _parse_table(flat, _LED_FIELDS)


# --- diagnostics ------------------------------------------------------------
# Key names below come from captured responses for the three endpoints, not from
# guesswork: the signal document reports `normalizedSignalStrength` (0-100) and a
# coarse `signalStrengthEnum`, NOT `signalStrength`/`rssi`. Candidate lists are
# kept because a different camera model returns a different subset, and `raw`
# always carries whatever actually came back.
_SIGNAL_KEYS = ("normalizedSignalStrength", "signalStrength", "signalStrengthValue")
_BATTERY_KEYS = ("batteryLevel", "batteryPercentage", "batteryLevelValue")
_STATUS_KEYS = ("connectionStatus", "deviceStatus", "status", "deviceStatusValue")


def _pick(attrs: dict, keys: tuple[str, ...]) -> Any:
    """First present candidate key's value, or None if the camera reports none."""
    for key in keys:
        if key in attrs:
            return attrs[key]
    return None


def _battery(attrs: dict) -> Any:
    """Battery level, or None on a camera that has no battery.

    `batteryLevel` is present and 0 on mains-powered devices, so the value alone
    cannot distinguish "no battery" from "flat battery" -- an entity built on it
    would read a permanent 0%. alarm.com's own UI gates on
    `shouldDisplayBatteryLevel`, so that flag is honoured here: when it is
    present and false, this returns None and the caller creates no entity.
    """
    if attrs.get("shouldDisplayBatteryLevel") is False:
        return None
    return _pick(attrs, _BATTERY_KEYS)


async def get_diagnostics(telus: TelusSession, camera_id: str) -> dict:
    """Signal strength, battery and device status for one camera.

    Returns ``{"signal_strength": ..., "battery": ..., "device_status": ...,
    "raw": {"signal_strength": {...}, "battery": {...}, "device_status": {...}}}``.

    ``signal_strength`` is alarm.com's normalized 0-100 figure, not dBm.
    ``battery`` is None on a mains-powered camera (see _battery). Any of the
    three is None when the camera has no such record (battery status 404s on
    some models) or reports it under a field name this parser does not know --
    ``raw`` always holds whatever came back, so callers must not create an
    entity for a value that is None.

    Note ``raw`` includes the camera's Wi-Fi SSID and encryption type; it is
    diagnostic detail about someone's home network, so do not log it or put it
    in an entity attribute without deciding that on purpose.

    The three reads are issued sequentially rather than concurrently: this is
    polled state, and alarm.com's rate limits are undocumented and can lock the
    account, so it is not worth turning one poll into a three-request burst.
    """
    parts = {}
    for name, template in (("signal_strength", _SIGNAL_URL),
                           ("battery", _BATTERY_URL),
                           ("device_status", _DEVSTATUS_URL)):
        url = template.format(base=c.TELUS_BASE, cid=camera_id)
        parts[name] = _flatten(await _get_doc(telus, url, missing_ok=True))

    return {
        "signal_strength": _pick(parts["signal_strength"], _SIGNAL_KEYS),
        "battery": _battery(parts["battery"]),
        "device_status": _pick(parts["device_status"], _STATUS_KEYS),
        "raw": parts,
    }
