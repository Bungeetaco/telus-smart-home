"""Tests for `telus_smarthome_alarmdotcom.speak` / `stop_speaking` (`speak_service.py`, backlog E66).

Run from the repository root:

    python3 tests/test_speak_service.py

No Home Assistant, no network, no doorbell: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, `tests/fakes.py` the hass/session fakes, and the REAL
service handlers run against a scripted bridge.

Why this file exists: speak makes real audio come out of a doorbell in
someone's home, and each of its failure modes is invisible from the caller's
side until a person is standing at the door wondering why nothing played:

  * an omitted optional field that starts being SENT (rather than left absent)
    silently overrides the bridge's own audio-conditioning defaults for every
    automation that never asked;
  * the 413/504/400 mapping (E51) is the only way a user learns that their
    clip was too big, played too long, or could not be fetched -- unmapped they
    all read as one generic failure;
  * a 409 (talk already in flight) must NOT be an error, or an automation
    retries into the same busy camera;
  * target resolution decides which device in the house speaks.

Pinned here: registration/removal lifecycle (and that this stays a SERVICE,
not an entity), schema validation (message/media_content_id exclusivity),
audio-url resolution (http passthrough, TTS render, relative-to-absolute with
a clear error when HA has no internal URL), default-target selection
(doorbells first, all bridge cameras as fallback), the exact POST body and
70s timeout, the whole status mapping, and stop_speaking's deliberate
404-tolerance.
"""
import asyncio
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

import aiohttp  # noqa: E402

import fakes  # noqa: E402
from fakes import (  # noqa: E402
    FakeEntityRegistry,
    FakeHass,
    FakeRegistryEntry,
    FakeResponse,
    FakeSession,
    FakeStates,
    ScriptedSecret,
)

from homeassistant.exceptions import HomeAssistantError  # noqa: E402

from custom_components.telus_smarthome_alarmdotcom import speak_service  # noqa: E402
from custom_components.telus_smarthome_alarmdotcom.const import (  # noqa: E402
    DOMAIN,
    SERVICE_SPEAK,
    SERVICE_STOP_SPEAKING,
)

DOORBELL = FakeRegistryEntry("camera.front_doorbell", unique_id="cam-uuid-door",
                             original_name="Front Doorbell")
GARAGE_CAM = FakeRegistryEntry("camera.garage", unique_id="cam-uuid-garage",
                               original_name="Garage")
FOREIGN_CAM = FakeRegistryEntry("camera.other", unique_id="x", platform="generic")


def make_hass(entries=(DOORBELL, GARAGE_CAM), tts=("tts.google_en",)):
    hass = FakeHass()
    hass.data["entity_registry"] = FakeEntityRegistry(list(entries))
    hass.states = FakeStates({"tts": list(tts)})
    hass.config.internal_url = "http://ha.internal:8123"
    return hass


class _Ctx:
    """Registers the services over a scripted bridge for one test."""

    def __init__(self, hass=None, script=None, secret="control-secret"):
        self.hass = hass or make_hass()
        self.session = FakeSession(list(script or []))
        self._saved = fakes.patch(
            speak_service,
            read_control_secret=ScriptedSecret(secret),
            async_get_clientsession=fakes.clientsession_for(self.session),
            generate_media_source_id=_scripted_tts_id,
            media_source=types.SimpleNamespace(async_resolve_media=_scripted_resolve),
        )
        speak_service.async_register_speak_service(self.hass)
        self.speak = self.hass.services.registered[(DOMAIN, SERVICE_SPEAK)]
        self.stop = self.hass.services.registered[(DOMAIN, SERVICE_STOP_SPEAKING)]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        fakes.restore(speak_service, self._saved)


TTS_CALLS: list[dict] = []


def _scripted_tts_id(hass, message, engine=None, language=None):
    TTS_CALLS.append({"message": message, "engine": engine, "language": language})
    return f"media-source://tts/{engine}?message={message}&language={language}"


async def _scripted_resolve(hass, media_id, target):
    # HA's tts proxy hands back an app-relative URL; the bridge needs absolute.
    return types.SimpleNamespace(url="/api/tts_proxy/rendered.mp3")


def call(data):
    return ha_stubs.ServiceCall(DOMAIN, SERVICE_SPEAK, data)


async def expect_error(coro, *needles):
    try:
        await coro
    except HomeAssistantError as exc:
        for needle in needles:
            assert needle in str(exc), f"error {exc} lacks {needle!r}"
        return str(exc)
    raise AssertionError(f"expected HomeAssistantError containing {needles!r}, got success")


async def test_registration_is_idempotent_and_service_shaped():
    with _Ctx() as ctx:
        first = ctx.speak
        speak_service.async_register_speak_service(ctx.hass)  # a reload lands here again
        assert ctx.hass.services.registered[(DOMAIN, SERVICE_SPEAK)] is first, (
            "re-registration on reload must be a no-op, not a second handler")
        speak_service.async_remove_speak_service(ctx.hass)
        assert not ctx.hass.services.has_service(DOMAIN, SERVICE_SPEAK)
        assert not ctx.hass.services.has_service(DOMAIN, SERVICE_STOP_SPEAKING)
    # Deliberately a service, not an entity: no platform hook to set one up.
    assert not hasattr(speak_service, "async_setup_entry"), (
        "speak grew a platform hook -- it is deliberately service-shaped")


async def test_message_and_media_content_id_are_exclusive():
    with _Ctx() as ctx:
        try:
            await ctx.speak(call({"message": "hi", "media_content_id": "http://x/a.mp3"}))
        except ha_stubs.VolInvalid:
            pass
        else:
            raise AssertionError(
                "message AND media_content_id were accepted together -- the "
                "schema declares them exclusive")
        await expect_error(ctx.speak(call({})), "either 'message' or 'media_content_id'")


async def test_http_media_url_passes_through_untouched():
    with _Ctx(script=[FakeResponse(200, {})]) as ctx:
        await ctx.speak(call({"media_content_id": "https://cdn.example/bark.mp3",
                              "entity_id": ["camera.front_doorbell"]}))
        request = ctx.session.requests[0]
    assert request["url"].endswith("/control/cameras/cam-uuid-door/talk")
    assert request["json"] == {"audio_url": "https://cdn.example/bark.mp3"}, (
        f"body {request['json']!r}: an already-fetchable URL must be handed "
        "over verbatim, and omitted conditioning/audio_filter must stay ABSENT "
        "so the bridge's own defaults remain the contract")
    assert request["headers"].get("Authorization") == "Bearer control-secret"
    assert request["timeout"].total == 70, (
        "the POST timeout must outlast the bridge's own 60s talk timeout, or "
        "HA gives up on talks that are succeeding")


async def test_message_renders_via_tts_and_becomes_absolute():
    with _Ctx(script=[FakeResponse(200, {})]) as ctx:
        await ctx.speak(call({"message": "parcel at the door",
                              "entity_id": ["camera.front_doorbell"]}))
        request = ctx.session.requests[0]
    assert request["json"]["audio_url"] == "http://ha.internal:8123/api/tts_proxy/rendered.mp3", (
        f"got {request['json']!r} -- the tts proxy URL is app-relative and the "
        "bridge is a separate process, so it must be absolutized with HA's "
        "internal URL")


async def test_default_tts_engine_is_first_available():
    TTS_CALLS.clear()
    with _Ctx(script=[FakeResponse(200, {})]) as ctx:
        await ctx.speak(call({"message": "hello", "entity_id": ["camera.front_doorbell"]}))
    assert TTS_CALLS and TTS_CALLS[0]["engine"] == "tts.google_en", (
        f"tts calls {TTS_CALLS!r}: with no engine given, the first available "
        "tts.* entity is the default")

    hass = make_hass(tts=())
    with _Ctx(hass=hass) as ctx:
        await expect_error(ctx.speak(call({"message": "hello"})), "No TTS engine")


async def test_missing_internal_url_is_a_clear_error():
    hass = make_hass()
    hass.config.internal_url = None
    with _Ctx(hass=hass) as ctx:
        message = await expect_error(ctx.speak(call({"message": "hi"})), "internal URL")
    assert "Settings" in message, (
        f"{message!r} should tell the user WHERE to fix it, not just that it broke")


async def test_optional_fields_are_forwarded_only_when_set():
    with _Ctx(script=[FakeResponse(200, {})]) as ctx:
        await ctx.speak(call({"media_content_id": "http://x/a.mp3",
                              "entity_id": ["camera.front_doorbell"],
                              "conditioning": False, "audio_filter": ""}))
        request = ctx.session.requests[0]
    assert request["json"] == {"audio_url": "http://x/a.mp3", "conditioning": False,
                               "audio_filter": ""}, (
        f"body {request['json']!r}: explicitly-set falsy values are real "
        "instructions (conditioning False turns the ffmpeg chain off; an empty "
        "audio_filter disables conditioning) and must be forwarded as given")


async def test_default_target_prefers_doorbells_then_all_cameras():
    with _Ctx(script=[FakeResponse(200, {})]) as ctx:
        await ctx.speak(call({"media_content_id": "http://x/a.mp3"}))
        urls = [request["url"] for request in ctx.session.requests]
    assert urls == ["http://127.0.0.1:8199/control/cameras/cam-uuid-door/talk"], (
        f"targets {urls!r}: with no entity_id the doorbell is the target, not "
        "every camera in the house")

    hass = make_hass(entries=(GARAGE_CAM, FOREIGN_CAM))
    with _Ctx(hass=hass, script=[FakeResponse(200, {})]) as ctx:
        await ctx.speak(call({"media_content_id": "http://x/a.mp3"}))
        urls = [request["url"] for request in ctx.session.requests]
    assert urls == ["http://127.0.0.1:8199/control/cameras/cam-uuid-garage/talk"], (
        f"targets {urls!r}: with no doorbell, every TELUS bridge camera is "
        "offered -- but never another integration's camera")


async def test_foreign_or_unknown_entities_are_refused():
    with _Ctx() as ctx:
        await expect_error(
            ctx.speak(call({"media_content_id": "http://x/a.mp3",
                            "entity_id": ["camera.other"]})),
            "not a TELUS bridge camera")
        await expect_error(
            ctx.speak(call({"media_content_id": "http://x/a.mp3",
                            "entity_id": ["camera.nonexistent"]})),
            "not a TELUS bridge camera")
        assert ctx.session.requests == [], "a refused target must not reach the bridge"


async def test_missing_control_secret_is_a_clear_error():
    with _Ctx(secret=None) as ctx:
        await expect_error(
            ctx.speak(call({"media_content_id": "http://x/a.mp3"})),
            "not reachable")
        assert ctx.session.requests == []


async def test_talk_status_mapping():
    cases = [
        (403, "talkdown is disabled", True),
        (422, "no upstream (talk) audio path", True),
        (413, "size cap", True),
        (504, "timed out on the bridge", True),
        (400, "could not fetch or decode", True),
        (409, None, False),  # busy camera: skip quietly, never an error
    ]
    for status, needle, is_error in cases:
        with _Ctx(script=[FakeResponse(status, {})]) as ctx:
            coro = ctx.speak(call({"media_content_id": "http://x/a.mp3",
                                   "entity_id": ["camera.front_doorbell"]}))
            if is_error:
                await expect_error(coro, needle)
            else:
                await coro  # must not raise

    with _Ctx(script=[aiohttp.ClientConnectionError("refused")]) as ctx:
        await expect_error(
            ctx.speak(call({"media_content_id": "http://x/a.mp3",
                            "entity_id": ["camera.front_doorbell"]})),
            "speak failed for camera.front_doorbell")


async def test_stop_speaking_tolerates_nothing_playing():
    with _Ctx(script=[FakeResponse(404, {})]) as ctx:
        await ctx.stop(ha_stubs.ServiceCall(DOMAIN, SERVICE_STOP_SPEAKING,
                                            {"entity_id": ["camera.front_doorbell"]}))
        request = ctx.session.requests[0]
    assert request["url"].endswith("/control/cameras/cam-uuid-door/talk/stop"), (
        f"stop hit {request['url']!r}")
    # 404 means nothing was playing; an automation may fire this
    # unconditionally, so it must not raise.

    with _Ctx(script=[FakeResponse(403, {})]) as ctx:
        try:
            await ctx.stop(ha_stubs.ServiceCall(DOMAIN, SERVICE_STOP_SPEAKING,
                                                {"entity_id": ["camera.front_doorbell"]}))
        except HomeAssistantError as exc:
            assert "disabled" in str(exc)
        else:
            raise AssertionError("a 403 (talkdown disabled) must surface, not vanish")


TESTS = (
    test_registration_is_idempotent_and_service_shaped,
    test_message_and_media_content_id_are_exclusive,
    test_http_media_url_passes_through_untouched,
    test_message_renders_via_tts_and_becomes_absolute,
    test_default_tts_engine_is_first_available,
    test_missing_internal_url_is_a_clear_error,
    test_optional_fields_are_forwarded_only_when_set,
    test_default_target_prefers_doorbells_then_all_cameras,
    test_foreign_or_unknown_entities_are_refused,
    test_missing_control_secret_is_a_clear_error,
    test_talk_status_mapping,
    test_stop_speaking_tolerates_nothing_playing,
)


async def main():
    failures = 0
    for test in TESTS:
        try:
            await test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
