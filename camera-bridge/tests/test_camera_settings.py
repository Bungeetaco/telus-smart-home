"""Tests for the website camera-settings client (bridge/camera_settings.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_camera_settings.py

No network and no real camera. Everything here is driven by `fixtures/`, which
are real captured request/response pairs from a browser session against a live
ADC-VDB750 doorbell, with the account identifiers and the Wi-Fi SSID replaced.
Both halves of each pair are kept on purpose:

    <name>_get.json   what the server returned  (JSON:API: data + included)
    <name>_put.json   what the browser then sent back  (ONE FLAT OBJECT)

An earlier version of these tests used hand-written payloads and asserted the
round-trip "either way" because no capture had settled the envelope question.
That is precisely how the module shipped unable to talk to the real API at all:
the fabricated fixtures agreed with the fabricated model, and both were wrong.
So the rule for this file is: if the real API's shape is in question, the
fixtures answer it -- do not invent a payload to make a test pass.

The three properties pinned here all fail SILENTLY against a real camera rather
than raising, which is why nothing but a test catches them:

  1. The settings live in JSON:API `included`, reachable only via
     `data.relationships` -- NOT inside `data.attributes`. Read only the
     attributes and every setting reads as absent/unsupported.
  2. The write is a FLAT object, not a JSON:API envelope, and a PUT replaces the
     WHOLE document -- so a partial body resets fields on a camera in someone's
     home.
  3. A camera that CANNOT do a thing returns the same payload shape as one that
     can; only the capability flag differs, and this API spells that flag two
     different ways (`<name>FeatureSupport == 2` vs a `canSetX` truthy flag).
"""
import asyncio
import inspect
import contextlib
import copy
import json
import os
import pathlib
import sys

from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c              # noqa: E402
from bridge import camera_settings as cs    # noqa: E402

FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"
CAM = "100000000-2048"

# fixture stem -> the URL path segment the client reads it from
KINDS = {"audio_settings": "audioSettings",
         "image_settings": "imageSettings",
         "video_configs": "videoConfigs",
         "general_settings": "generalSettings"}


def fixture(name):
    with open(FIXTURES / f"{name}.json") as fh:
        return json.load(fh)


def paths(node, prefix=""):
    """Dotted-path -> leaf value, so whole documents can be diffed at once.

    Lists are leaves: the option lists are opaque here, and their ORDER is part
    of what must survive a round trip.
    """
    flat = {}
    for key, value in node.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(paths(value, f"{path}."))
        else:
            flat[path] = value
    return flat


# --- pure-function tests over the captured documents ------------------------
def test_flatten_reproduces_the_browsers_own_request_body():
    """_flatten(GET) must equal what the browser PUT after that same GET.

    This is the single most load-bearing assertion in the file: it is the only
    thing standing between the client and silently talking a shape the endpoint
    does not accept. The browser adds a few sub-keys of its own (it holds a
    richer model than one response carries), so sub-object contents are compared
    by intersection -- but the TOP-LEVEL key set must match exactly, because
    that set is what says "flat object, relationships inlined by name".
    """
    for stem in KINDS:
        mine = cs._flatten(fixture(f"{stem}_get"))
        theirs = fixture(f"{stem}_put")
        assert set(mine) == set(theirs), (
            f"{stem}: top-level keys differ -- "
            f"missing {sorted(set(theirs) - set(mine))}, "
            f"extra {sorted(set(mine) - set(theirs))}")
        # STRUCTURE is compared, not leaf values: the captured GET and PUT are
        # from different moments in the same session (the browser was sweeping a
        # slider), so callVolumeValue legitimately differs between them. What
        # must match is the shape -- and the id/type each object carries, since
        # the write side pluralizes the type and getting that wrong is invisible
        # until a real endpoint rejects it.
        for key, value in theirs.items():
            if not isinstance(value, dict):
                continue
            assert isinstance(mine.get(key), dict), f"{stem}: {key} is not inlined"
            assert mine[key].get("type") == value.get("type"), (
                f"{stem}: {key}.type is {mine[key].get('type')!r}, "
                f"the browser sends {value.get('type')!r}")
            assert mine[key].get("id") == value.get("id"), f"{stem}: {key}.id"
        assert mine.get("type") == theirs.get("type"), f"{stem}: top-level type"


def test_flatten_ignores_an_unreferenced_included_resource():
    """Only relationships named by `data` are inlined; stray included resources
    are not merged in under a guessed name."""
    doc = fixture("audio_settings_get")
    doc["included"].append({"type": "video/settings/nonsense", "id": "999",
                            "attributes": {"shouldNotAppear": True}})
    flat = cs._flatten(doc)
    assert "shouldNotAppear" not in paths(flat)


def test_flatten_stubs_a_relationship_with_no_included_resource():
    """A relationship whose resource was not returned becomes an {id, type} stub
    -- what the browser sends for one it did not load. Dropping it instead would
    silently delete that relationship on a whole-document PUT."""
    flat = cs._flatten(fixture("video_configs_get"))
    assert flat["imageSetting"] == {"type": "video/settings/image-settings",
                                    "id": "100000000-2048"}


def test_flatten_survives_a_relationship_with_no_usable_type():
    """A relationship ref whose `type` is null used to raise AttributeError from
    inside _write_type, failing the whole settings read on a document that was
    merely odd rather than broken. Such members exist on the sibling
    arming-devices documents, so this is not hypothetical."""
    doc = fixture("audio_settings_get")
    doc["data"]["relationships"]["brokenRef"] = {"data": {"type": None, "id": "x"}}
    doc["data"]["relationships"]["alsoBroken"] = {"data": {"id": "y"}}
    flat = cs._flatten(doc)
    assert "brokenRef" not in flat and "alsoBroken" not in flat, flat
    # ...and the rest of the document still parses.
    assert cs._parse_audio(flat)["call_volume"] == 10


def test_audio_is_parsed_off_the_real_document():
    """The capture's own values, including the one that motivated all of this:
    call volume IS supported and settable 0-10 on this doorbell, contradicting
    the mobile API's SupportsVolumeAdjustment=false."""
    audio = cs._parse_audio(cs._flatten(fixture("audio_settings_get")))
    assert audio["supports_call_volume"] is True
    assert audio["call_volume_min"] == 0 and audio["call_volume_max"] == 10
    assert audio["call_volume"] == 10
    # The outdoor tone can be switched but NOT levelled on this device -- two
    # different flags, and conflating them would offer a control that does
    # nothing.
    assert audio["supports_outdoor_tone"] is True
    assert audio["supports_outdoor_tone_volume"] is False


def test_an_unsettable_volume_is_reported_as_unknown_not_as_zero():
    """E35. The website reports outdoorToneVolumeValue 0 on a doorbell whose
    outdoor tone is audibly ON, while the mobile API reports level 3 for the same
    setting on the same device at the same moment -- so the website's number is a
    placeholder for a field it does not manage, not a reading.

    What makes that diagnostic rather than a guess: the two APIs agree exactly on
    the volume that IS settable (call volume 10 on both). Only the unsettable one
    disagrees.

    So it is reported as None. A caller that showed "volume 0" for an unmuted
    tone would be stating something false.
    """
    audio = cs._parse_audio(cs._flatten(fixture("audio_settings_get")))
    assert audio["supports_outdoor_tone_volume"] is False, audio
    assert audio["outdoor_tone_volume"] is None, audio
    # The tone itself is on -- which is the contradiction that gave it away.
    assert audio["outdoor_tone_on"] is True, audio
    # The bounds still come through, so a model that CAN set it has its range.
    assert audio["outdoor_tone_volume_max"] == 3, audio

    # ...and where the device does manage the field, the number is reported.
    doc = fixture("audio_settings_get")
    for res in doc["included"]:
        attrs = res.get("attributes") or {}
        if "outdoorToneVolumeFeatureSupport" in attrs:
            attrs["outdoorToneVolumeFeatureSupport"] = 2
            attrs["outdoorToneVolumeValue"] = 2
    audio = cs._parse_audio(cs._flatten(doc))
    assert audio["supports_outdoor_tone_volume"] is True and audio["outdoor_tone_volume"] == 2


def test_diagnostics_says_which_signal_key_it_read():
    """E31. The keys are not the same unit: normalizedSignalStrength is a 0-100
    score, while the spellings kept for untested models may be real dBm. Home
    Assistant records long-term statistics against the declared unit, so a caller
    has to know which one answered rather than guess a label."""
    assert cs._resolve({"normalizedSignalStrength": 76}, cs._SIGNAL_KEYS) == \
        "normalizedSignalStrength"
    assert cs._resolve({"signalStrength": -58}, cs._SIGNAL_KEYS) == "signalStrength"
    assert cs._resolve({"somethingElse": 1}, cs._SIGNAL_KEYS) is None
    # The normalized key must be preferred when a device reports both, since it
    # is the one whose unit is known.
    assert cs._resolve({"signalStrength": -58, "normalizedSignalStrength": 76},
                       cs._SIGNAL_KEYS) == "normalizedSignalStrength"


def test_capability_flags_read_both_spellings():
    """`<name>FeatureSupport` is an enum where only 2 means editable; everything
    else is a truthy capability flag -- and `canSetHdr` is the integer 1, not a
    bool. Absent means unsupported: unknown must never be offered."""
    assert cs._supported({"xFeatureSupport": 2}, ("xFeatureSupport",)) is True
    assert cs._supported({"xFeatureSupport": 0}, ("xFeatureSupport",)) is False
    # 1 is NOT "supported" for the enum spelling, however truthy it looks.
    assert cs._supported({"xFeatureSupport": 1}, ("xFeatureSupport",)) is False
    assert cs._supported({"canSetHdr": 1}, ("canSetHdr",)) is True
    assert cs._supported({"canSetSharpness": False}, ("canSetSharpness",)) is False
    assert cs._supported({}, ("canSetSharpness",)) is False


def test_bounds_are_read_in_both_spellings_and_absence_is_not_zero():
    """`callVolumeMin` (suffix) and `minBrightness` (prefix) are the same idea.

    An absent bound must be None, not 0: treating it as 0 would refuse every
    negative brightness on a camera whose real range is -5..5, and -- in the
    clamping design this replaced -- would have made a published Max a no-op.
    """
    assert cs._limit({"callVolumeMax": 10}, "callVolume", "Max") == 10
    assert cs._limit({"maxBrightness": 5}, "brightness", "Max") == 5
    assert cs._limit({"minBrightness": -5}, "brightness", "Min") == -5
    assert cs._limit({}, "brightness", "Max") is None


def test_image_and_video_parse_off_the_real_documents():
    """Both tables resolve against the captured devices, and report the
    unsupported-but-present fields as unsupported."""
    image = cs._parse_table(cs._flatten(fixture("image_settings_get")), cs._IMAGE_FIELDS)
    assert image["supports_brightness"] is True
    assert image["brightness_min"] == -5 and image["brightness_max"] == 5
    # Present with a plausible value, but the device says it cannot be set.
    assert image["sharpness"] == 0 and image["supports_sharpness"] is False
    assert image["supports_ir_led_enabled"] is True          # nested infraredSetting
    assert image["supports_night_vision_enabled"] is False

    video = cs._parse_table(cs._flatten(fixture("video_configs_get")), cs._VIDEO_FIELDS)
    # Everything here lives in the savedVideoSetting relationship, not in the
    # videoConfigs attributes -- if the relationship were missed, all of these
    # would be None/False and this is the test that would say so.
    assert video["clip_resolution"] == "1440x1440"
    assert video["supports_clip_resolution"] is True
    assert video["supports_clip_quality"] is False
    assert video["keep_clips_days"] == -1                     # -1 = keep forever
    assert -1 in video["keep_clips_days_options"]


def test_battery_is_none_on_a_mains_powered_camera():
    """batteryLevel is present and 0 on a device with no battery, so the value
    alone cannot say "no battery" -- an entity built on it would sit at 0%."""
    assert cs._battery({"shouldDisplayBatteryLevel": False, "batteryLevel": 0}) is None
    assert cs._battery({"shouldDisplayBatteryLevel": True, "batteryLevel": 82}) == 82
    assert cs._battery({"batteryLevel": 82}) == 82
    # A NEGATIVE level is this API family's "not applicable" rather than a
    # reading: the mobile document says BatteryLevel="-1" with
    # IsBatteryPowered="false" for the same device the website reports 0 for. This
    # covers a model that omits the display flag but still answers -1 (E33).
    assert cs._battery({"batteryLevel": -1}) is None
    assert cs._battery({"shouldDisplayBatteryLevel": True, "batteryLevel": -1}) is None
    # 0 with the flag present-and-true is a flat battery, which IS a reading.
    assert cs._battery({"shouldDisplayBatteryLevel": True, "batteryLevel": 0}) == 0


# --- fake portal ------------------------------------------------------------
class FakePortal:
    """Stand-in for the alarm.com website settings API.

    Serves the captured JSON:API documents, records every request body, and --
    crucially -- REJECTS a PUT that arrives in a JSON:API envelope, the way a
    server that only speaks the flat form would. A fake that accepted both is
    what let the wrong write shape ship.
    """

    def __init__(self):
        self.docs = {stem: fixture(f"{stem}_get") for stem in KINDS}
        self.writes: list[tuple[str, dict]] = []
        self.reads = 0

    def _stem(self, kind):
        for stem, segment in KINDS.items():
            if segment == kind:
                return stem
        return None

    async def get(self, request):
        self.reads += 1
        stem = self._stem(request.match_info["kind"])
        if stem is None:
            return web.json_response({"data": {"id": CAM, "type": "x", "attributes": {}}})
        return web.json_response(copy.deepcopy(self.docs[stem]))

    async def put(self, request):
        kind = request.match_info["kind"]
        body = await request.json()
        if "data" in body and isinstance(body["data"], dict) and "attributes" in body["data"]:
            return web.json_response({"error": "this endpoint takes a flat object"},
                                     status=400)
        self.writes.append((kind, body))
        # Echo the document back the way the real endpoint does, rather than a
        # bare acknowledgement: the setters parse this reply into their return
        # value, so a fake that answers with an empty document would silently
        # exercise only the fallback path.
        stem = self._stem(kind)
        if stem is not None:
            attrs = {k: v for k, v in body.items()
                     if k not in ("id", "type") and not isinstance(v, dict)}
            rels, included = {}, []
            for key, value in body.items():
                if not isinstance(value, dict):
                    continue
                ref = {"type": value.get("type", "x"), "id": value.get("id", CAM)}
                rels[key] = {"data": ref}
                included.append({**ref, "attributes":
                                 {k: v for k, v in value.items()
                                  if k not in ("id", "type")}})
            return web.json_response({
                "data": {"id": body.get("id", CAM), "type": body.get("type", "x"),
                         "attributes": attrs, "relationships": rels},
                "included": included})
        return web.json_response({"data": {"id": CAM, "type": "x", "attributes": {}}})

    def last(self, kind):
        for written_kind, body in reversed(self.writes):
            if written_kind == kind:
                return body
        raise AssertionError(f"no PUT was made to {kind}")


class FakeTelus:
    """The two attributes camera_settings uses off a TelusSession."""

    def __init__(self, session):
        self.session = session
        self.api_headers = {"ajaxrequestuniquekey": "test"}


@contextlib.asynccontextmanager
async def portal_at(port):
    import aiohttp

    portal = FakePortal()
    app = web.Application()
    app.router.add_get("/web/api/video/settings/{kind}/{cid}", portal.get)
    app.router.add_put("/web/api/video/settings/{kind}/{cid}", portal.put)
    app.router.add_get("/web/api/video/{kind}/{cid}", portal.get)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    saved = c.TELUS_BASE
    c.TELUS_BASE = f"http://127.0.0.1:{port}/"
    try:
        async with aiohttp.ClientSession() as session:
            yield portal, FakeTelus(session)
    finally:
        c.TELUS_BASE = saved
        await runner.cleanup()


async def expect_error(what, factory, *, unsupported):
    try:
        await factory()
    except cs.CameraSettingsError as err:
        assert err.unsupported is unsupported, (
            f"{what}: expected unsupported={unsupported}, got {err.unsupported} ({err})")
        return err
    raise AssertionError(f"{what}: expected CameraSettingsError, none raised")


# --- round-trip tests -------------------------------------------------------
async def test_volume_write_is_flat_and_preserves_every_other_field():
    """The whole point of read-modify-write: one field changes, nothing else.

    Diffed against the client's OWN read rather than against the fixture, so the
    assertion is "you sent back what you were given, plus your one edit" -- which
    is what protects the unrelated settings on a live camera.
    """
    async with portal_at(8477) as (portal, telus):
        before = cs._flatten(portal.docs["audio_settings"])
        await cs.set_call_volume(telus, CAM, 4)

        sent = portal.last("audioSettings")
        assert "data" not in sent, "the write must be a flat object, not a JSON:API envelope"
        assert sent["microphoneSpeakerSetting"]["callVolumeValue"] == 4

        changed = {k: v for k, v in paths(sent).items() if paths(before).get(k) != v}
        assert set(changed) == {"microphoneSpeakerSetting.callVolumeValue"}, (
            f"a settings PUT replaces the whole object; these also changed: {changed}")


async def test_chime_write_preserves_the_sibling_audio_object():
    """Changing the chime must resend microphoneSpeakerSetting intact -- if it
    did not, toggling the doorbell chime would reset the talkdown volume."""
    async with portal_at(8478) as (portal, telus):
        before = cs._flatten(portal.docs["audio_settings"])
        await cs.set_chime(telus, CAM, indoor_on=False)

        sent = portal.last("audioSettings")
        assert sent["chimeToneSetting"]["isIndoorChimeOn"] is False
        assert sent["microphoneSpeakerSetting"] == before["microphoneSpeakerSetting"]


async def test_chime_type_write_and_its_option_gate():
    """chime_type is the style of chime the doorbell is WIRED TO, not a ringtone.

    The reference doorbell publishes chimeTypeOptions [-1, 0, 1] with
    indoorChimeTypeFeatureSupport 2, so the write is allowed and an off-list value
    is a caller mistake (400), not a missing capability (422).
    """
    async with portal_at(8485) as (portal, telus):
        before = cs._flatten(portal.docs["audio_settings"])
        await cs.set_chime(telus, CAM, chime_type=1)

        sent = portal.last("audioSettings")
        assert sent["chimeToneSetting"]["selectedChimeType"] == 1
        # Changing the wired-chime type must not disturb the talkdown volume.
        assert sent["microphoneSpeakerSetting"] == before["microphoneSpeakerSetting"]

        await expect_error("chime_type=9",
                           lambda: cs.set_chime(telus, CAM, chime_type=9),
                           unsupported=False)
        await expect_error("chime_type='digital'",
                           lambda: cs.set_chime(telus, CAM, chime_type="digital"),
                           unsupported=False)


async def test_chime_type_is_refused_when_the_camera_cannot_select_one():
    """The type has its OWN feature enum (indoorChimeTypeFeatureSupport). A camera
    that can toggle its chime but not choose a type must refuse the type write and
    still accept the toggle -- conflating the two flags would offer a control that
    silently does nothing."""
    async with portal_at(8486) as (portal, telus):
        chime = portal.docs["audio_settings"]["included"]
        for res in chime:
            attrs = res.get("attributes") or {}
            if "indoorChimeTypeFeatureSupport" in attrs:
                attrs["indoorChimeTypeFeatureSupport"] = 0

        await expect_error("type unsupported",
                           lambda: cs.set_chime(telus, CAM, chime_type=1),
                           unsupported=True)
        assert portal.writes == []

        # The on/off toggle is a different feature and still works.
        await cs.set_chime(telus, CAM, indoor_on=False)
        assert portal.last("audioSettings")["chimeToneSetting"]["isIndoorChimeOn"] is False


async def test_chime_write_needs_at_least_one_field():
    """Passing nothing is a caller mistake, not a no-op that round-trips to a real
    camera for no reason."""
    async with portal_at(8487) as (portal, telus):
        await expect_error("empty", lambda: cs.set_chime(telus, CAM), unsupported=False)
        assert portal.writes == []


async def test_video_write_reaches_the_nested_relationship():
    """The value the caller set must land inside savedVideoSetting, and the
    other inlined relationships must come back unharmed."""
    async with portal_at(8479) as (portal, telus):
        before = cs._flatten(portal.docs["video_configs"])
        await cs.set_video_config(telus, CAM, keep_clips_days=30)

        sent = portal.last("videoConfigs")
        assert sent["savedVideoSetting"]["selectedKeepClipsDays"] == 30
        assert sent["liveVideoConfig"] == before["liveVideoConfig"]
        assert sent["pirVideoConfig"] == before["pirVideoConfig"]


async def test_led_toggles_and_preserves_the_rest_of_general_settings():
    """The status LED lives in generalSettings beside the doorbell's notification
    toggles and its solar-panel status. A PUT replaces the whole document, so
    switching the light must resend both of those untouched -- otherwise turning
    off an indicator light would quietly reset someone's notifications."""
    async with portal_at(8488) as (portal, telus):
        before = cs._flatten(portal.docs["general_settings"])
        result = await cs.set_led_settings(telus, CAM, enabled=False)

        sent = portal.last("generalSettings")
        assert "data" not in sent, "the write must be flat, not a JSON:API envelope"
        assert sent["ledLightSetting"]["isLedLightEnabled"] is False
        assert sent["doorbellNotificationSetting"] == before["doorbellNotificationSetting"]
        assert sent["solarPanelSetting"] == before["solarPanelSetting"]

        changed = {k: v for k, v in paths(sent).items() if paths(before).get(k) != v}
        assert set(changed) == {"ledLightSetting.isLedLightEnabled"}, changed
        assert result["enabled"] is False, result


async def test_led_brightness_is_refused_on_a_camera_that_cannot_dim():
    """canTurnOnOffLedLight and canChangeLedBrightness are separate flags, and the
    reference doorbell reports true/false: it can switch the light but not dim
    it. Conflating them would offer a slider that silently does nothing."""
    async with portal_at(8489) as (portal, telus):
        led = cs._parse_table(cs._flatten(portal.docs["general_settings"]), cs._LED_FIELDS)
        assert led["supports_enabled"] is True, led
        assert led["supports_brightness"] is False, led

        await expect_error("brightness",
                           lambda: cs.set_led_settings(telus, CAM, brightness=5),
                           unsupported=True)
        assert portal.writes == []

        # ...and a request mixing the two changes nothing at all.
        await expect_error("mixed",
                           lambda: cs.set_led_settings(telus, CAM,
                                                       enabled=True, brightness=5),
                           unsupported=True)
        assert portal.writes == []


async def test_notification_gating_is_not_one_flag_per_toggle():
    """Three toggles, TWO upstream enums, and one extra per-device flag.

    Motion and button-press share `panelNotificationEnableDisableFeatureSupport`;
    the smart chime has its own; and motion is additionally gated by
    `isMotionNotificationEnabled`, which the portal treats as "does this device do
    motion notifications at all" rather than as a toggle. Collapsing that into
    one flag per toggle -- the obvious reading of the payload -- would offer a
    smart-chime switch this doorbell does not have.
    """
    async with portal_at(8491) as (portal, telus):
        parsed = cs._parse_notifications(cs._flatten(portal.docs["general_settings"]))
        assert parsed["supports_motion"] is True, parsed
        assert parsed["supports_button_press"] is True, parsed
        assert parsed["supports_smart_chime"] is False, parsed

        await expect_error("smart_chime",
                           lambda: cs.set_notification_settings(telus, CAM,
                                                                smart_chime=True),
                           unsupported=True)
        assert portal.writes == []


async def test_motion_needs_its_second_flag_too():
    """With the shared enum supported but isMotionNotificationEnabled false, the
    motion toggle is unsupported while button-press stays available -- the whole
    reason _Field carries `requires`."""
    async with portal_at(8492) as (portal, telus):
        for res in portal.docs["general_settings"]["included"]:
            attrs = res.get("attributes") or {}
            if "isMotionNotificationEnabled" in attrs:
                attrs["isMotionNotificationEnabled"] = False

        parsed = cs._parse_notifications(cs._flatten(portal.docs["general_settings"]))
        assert parsed["supports_motion"] is False, parsed
        assert parsed["supports_button_press"] is True, parsed

        await expect_error("motion",
                           lambda: cs.set_notification_settings(telus, CAM, motion=True),
                           unsupported=True)
        assert portal.writes == []

        # ...and the sibling toggle still writes.
        await cs.set_notification_settings(telus, CAM, button_press=True)
        sent = portal.last("generalSettings")
        assert sent["doorbellNotificationSetting"]["isButtonPressNotificationsOn"] is True


async def test_notification_write_preserves_the_led_in_the_same_document():
    """The notification toggles and the status LED share one generalSettings
    document, so a notification write must resend the LED untouched -- otherwise
    silencing a doorbell would turn its light back on."""
    async with portal_at(8493) as (portal, telus):
        before = cs._flatten(portal.docs["general_settings"])
        # True, not False: the fixture already has it off, and a write that
        # changes nothing would pass this assertion for the wrong reason.
        await cs.set_notification_settings(telus, CAM, button_press=True)

        sent = portal.last("generalSettings")
        assert sent["ledLightSetting"] == before["ledLightSetting"]
        assert sent["solarPanelSetting"] == before["solarPanelSetting"]
        changed = {k: v for k, v in paths(sent).items() if paths(before).get(k) != v}
        assert set(changed) == {
            "doorbellNotificationSetting.isButtonPressNotificationsOn"}, changed


async def test_notification_format_offers_only_the_two_live_members():
    """VideoNotificationFormat is Unknown -1, TextOnly 0, AttachVideoClip 1,
    AttachThumbnail 2 -- read out of the portal's own webpack module, not guessed.

    Only 0 and 2 may be written. The portal builds its dropdown from those two
    alone, and elsewhere in the same bundle it rewrites AttachVideoClip to
    AttachThumbnail whenever it reads one, so 1 is a value alarm.com itself treats
    as stale. -1 is a null state -- a camera with no doorbell notifications
    reports it -- not a choice. The device publishes no option list of its own, so
    if this set is not enforced here nothing enforces it.
    """
    async with portal_at(8494) as (portal, telus):
        parsed = cs._parse_notifications(cs._flatten(portal.docs["general_settings"]))
        assert parsed["format"] == 0, parsed
        assert parsed["supports_format"] is True, parsed
        assert parsed["format_choices"] == [
            {"value": 0, "label": "Text Only"},
            {"value": 2, "label": "Attach a Thumbnail"}], parsed

        # The two offered values write.
        result = await cs.set_notification_settings(telus, CAM, format=2)
        assert portal.last("generalSettings")[
            "doorbellNotificationSetting"]["notificationFormat"] == 2
        assert result["format"] == 2, result

        # The legacy member and the null state do not.
        for bad in (1, -1, 3):
            await expect_error(f"format={bad}",
                               lambda b=bad: cs.set_notification_settings(
                                   telus, CAM, format=b),
                               unsupported=False)
        await expect_error("format=True",
                           lambda: cs.set_notification_settings(telus, CAM,
                                                                format=True),
                           unsupported=False)
        assert len(portal.writes) == 1, (
            f"only the legal write should have reached the camera: {portal.writes}")


async def test_general_settings_reads_both_sections_in_one_request():
    """The bundle asks for the LED and the notifications on every poll for every
    camera; they are one upstream document and must cost one request."""
    async with portal_at(8495) as (portal, telus):
        portal.reads = 0
        both = await cs.get_general_settings(telus, CAM)
        assert set(both) == {"led", "notifications"}, both
        assert both["led"]["supports_enabled"] is True, both
        assert both["notifications"]["supports_button_press"] is True, both
        assert portal.reads == 1, (
            f"expected one generalSettings GET, made {portal.reads}")


async def test_led_rejects_a_wrong_typed_value():
    async with portal_at(8490) as (portal, telus):
        await expect_error("enabled=1",
                           lambda: cs.set_led_settings(telus, CAM, enabled=1),
                           unsupported=False)
        await expect_error("no fields",
                           lambda: cs.set_led_settings(telus, CAM),
                           unsupported=False)
        assert portal.writes == []


async def test_live_resolution_is_gated_on_the_option_object_itself():
    """The LIVE stream's resolution, in a different relationship of the same
    document as the clip settings.

    Its capability flag IS the option object: `webOptions` maps value -> label and
    an EMPTY object is how this API says "no choice offered" -- which is exactly
    what `mobileOptions` is on this doorbell. `isWebOptionsDisabled` is a separate
    server-side veto, and is the one gate in this module phrased as a prohibition
    rather than a capability, so an absent one means allowed.
    """
    async with portal_at(8496) as (portal, telus):
        video = cs._parse_table(cs._flatten(portal.docs["video_configs"]), cs._VIDEO_FIELDS)
        assert video["supports_live_resolution"] is True, video
        assert video["live_resolution"] == "1440x1440", video
        assert video["live_resolution_options"] == ["1440x1440", "960x960", "480x480"]
        # Labelled the way the vendor's own UI labels them, not bare geometry.
        assert video["live_resolution_choices"][0] == {
            "value": "1440x1440", "label": "Full High Definition (1440x1440)"}

        await cs.set_video_config(telus, CAM, live_resolution="480x480")
        sent = portal.last("videoConfigs")
        assert sent["liveVideoConfig"]["selectedWebOption"] == "480x480"
        # A different relationship of the same document -- the clip settings ride
        # along untouched.
        assert sent["savedVideoSetting"] == cs._flatten(
            portal.docs["video_configs"])["savedVideoSetting"]


async def test_live_resolution_refuses_a_value_the_camera_does_not_offer():
    """Only the KEYS of webOptions are settable. The general option rule accepts
    either half of such an object because this API is inconsistent about which it
    means -- here it is not ambiguous, so the label must not be accepted as a
    value."""
    async with portal_at(8497) as (portal, telus):
        for bad in ("2160x2160", "Full High Definition (1440x1440)", "hd"):
            await expect_error(f"live_resolution={bad}",
                               lambda b=bad: cs.set_video_config(telus, CAM,
                                                                 live_resolution=b),
                               unsupported=False)
        assert portal.writes == []


async def test_live_resolution_unsupported_when_no_options_or_disabled():
    """An empty option object and an explicit veto both mean no control."""
    async with portal_at(8498) as (portal, telus):
        for res in portal.docs["video_configs"]["included"]:
            attrs = res.get("attributes") or {}
            if "webOptions" in attrs:
                attrs["webOptions"] = {}
        video = cs._parse_table(cs._flatten(portal.docs["video_configs"]), cs._VIDEO_FIELDS)
        assert video["supports_live_resolution"] is False, video

        for res in portal.docs["video_configs"]["included"]:
            attrs = res.get("attributes") or {}
            if "webOptions" in attrs:
                attrs["webOptions"] = {"960x960": "High Definition (960x960)"}
                attrs["isWebOptionsDisabled"] = True
        video = cs._parse_table(cs._flatten(portal.docs["video_configs"]), cs._VIDEO_FIELDS)
        assert video["supports_live_resolution"] is False, video

        await expect_error("vetoed",
                           lambda: cs.set_video_config(telus, CAM,
                                                       live_resolution="960x960"),
                           unsupported=True)
        assert portal.writes == []


async def test_out_of_range_is_refused_and_nothing_is_written():
    """Refused, not clamped: silently writing a different value than asked hides
    the mismatch. unsupported=False marks it a caller mistake (400), not a
    missing capability (422)."""
    async with portal_at(8480) as (portal, telus):
        await expect_error("volume 99", lambda: cs.set_call_volume(telus, CAM, 99),
                           unsupported=False)
        await expect_error("volume -1", lambda: cs.set_call_volume(telus, CAM, -1),
                           unsupported=False)
        assert portal.writes == [], "a refused value must not reach the camera"


async def test_unsupported_field_is_refused_before_the_write():
    """The device reports sharpness and outdoor-tone volume with plausible
    values but no capability. Writing anyway is a real request to a real camera,
    so it must fail BEFORE the PUT -- and be flagged unsupported (422), not
    malformed."""
    async with portal_at(8481) as (portal, telus):
        await expect_error("sharpness",
                           lambda: cs.set_image_settings(telus, CAM, sharpness=1),
                           unsupported=True)
        await expect_error("outdoor volume",
                           lambda: cs.set_chime(telus, CAM, outdoor_volume=1),
                           unsupported=True)
        assert portal.writes == []


async def test_a_partly_unsupported_request_writes_nothing():
    """All-or-nothing: brightness alone would succeed, but paired with an
    unsupported sharpness the camera must be left untouched rather than
    half-applied."""
    async with portal_at(8482) as (portal, telus):
        await expect_error("mixed",
                           lambda: cs.set_image_settings(telus, CAM,
                                                         brightness=1, sharpness=1),
                           unsupported=True)
        assert portal.writes == []


def test_the_two_clip_option_objects_disagree_about_which_half_is_settable():
    """E28. Same document, same device, opposite conventions.

    `clipResolutionOptions` is {"1": "480x480", ...} and the VALUE is settable.
    `clipQualityOptions` is {"5": 3, "3": 2, "2": 1} and the KEY is settable.
    Nothing in the payload distinguishes them -- `selectedClipQuality` is "3",
    which is simultaneously a key of that object and a value of it -- so this was
    read out of the portal's own dropdown builders, which push
    `{value: options[key]}` for the resolution and write `props.id` (the key) for
    the quality.

    Accepting both halves, which this module used to do, meant a caller could
    write a resolution-class enum number as a resolution.
    """
    video = cs._parse_table(cs._flatten(fixture("video_configs_get")), cs._VIDEO_FIELDS)

    assert video["clip_resolution_options"] == ["480x480", "960x960", "1440x1440"], video
    assert video["clip_resolution"] in video["clip_resolution_options"]
    # The keys of that object are a label-class enum and must NOT be offered.
    for enum_key in ("1", "4", "7"):
        assert enum_key not in video["clip_resolution_options"], video

    assert video["clip_quality_options"] == ["5", "3", "2"], video
    assert video["clip_quality"] in video["clip_quality_options"]
    # ...and its values are a quality enum, equally not settable.
    assert "1" not in video["clip_quality_options"][1:], video

    # An int-typed field's options come back as ints, not the strings a JSON
    # object's keys always are, so a caller can echo one straight back.
    assert video["camera_triggered_clip_duration_options"] == [30], video
    assert video["camera_triggered_clip_duration_choices"] == [
        {"value": 30, "label": "30 seconds"}], video

    # Labels are published only where the other half is a real string. The clip
    # fields' other halves are opaque enum numbers, which would label worse than
    # the value itself.
    assert "clip_resolution_choices" not in video, video
    assert "clip_quality_choices" not in video, video


async def test_a_label_class_enum_key_is_not_a_settable_resolution():
    """The concrete regression: "7" is a KEY of clipResolutionOptions, and used to
    be accepted as a clip resolution."""
    async with portal_at(8499) as (portal, telus):
        for bad in ("7", "1", "4"):
            await expect_error(f"clip_resolution={bad}",
                               lambda b=bad: cs.set_video_config(telus, CAM,
                                                                 clip_resolution=b),
                               unsupported=False)
        assert portal.writes == []

        # ...while a real one still writes.
        await cs.set_video_config(telus, CAM, clip_resolution="960x960")
        assert portal.last("videoConfigs")[
            "savedVideoSetting"]["selectedClipResolution"] == "960x960"


def test_an_unstudied_option_object_still_works_and_says_so(capsys=None):
    """A field with no declared half keeps the permissive behaviour, so an unknown
    camera model is not broken by this tightening -- but it warns, because the
    answer is obtainable from the portal's bundle without a device."""
    field = cs._Field(None, ("mysteryValue",), ("mysteryFeatureSupport",), "str")
    obj = {"mysteryValue": "a", "mysteryFeatureSupport": 2,
           "mysteryOptions": {"a": "Alpha", "b": "Beta"}}
    options = cs._field_options(obj, field, "mysteryValue")
    assert set(options) == {"a", "b", "Alpha", "Beta"}, options


async def test_off_option_value_is_refused():
    """keep_clips_days publishes an explicit option list; a value off it is a
    caller mistake, not a capability gap."""
    async with portal_at(8483) as (portal, telus):
        await expect_error("keep_clips_days=4",
                           lambda: cs.set_video_config(telus, CAM, keep_clips_days=4),
                           unsupported=False)
        assert portal.writes == []


async def test_unknown_field_name_is_a_caller_mistake():
    async with portal_at(8484) as (portal, telus):
        await expect_error("nonsense",
                           lambda: cs.set_image_settings(telus, CAM, nonsense=1),
                           unsupported=False)
        assert portal.writes == []


async def main():
    for name, fn in sorted(globals().items()):
        if not name.startswith("test_"):
            continue
        if inspect.iscoroutinefunction(fn):
            await fn()
        else:
            fn()
        print(f"  ok  {name}")
    print("\nall camera_settings tests passed")


if __name__ == "__main__":
    asyncio.run(main())
