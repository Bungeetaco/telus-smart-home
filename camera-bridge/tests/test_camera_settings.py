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
         "video_configs": "videoConfigs"}


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

    def _stem(self, kind):
        for stem, segment in KINDS.items():
            if segment == kind:
                return stem
        return None

    async def get(self, request):
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
