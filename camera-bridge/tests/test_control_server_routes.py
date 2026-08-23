"""Tests for the control API's camera settings, siren and privacy routes
(bridge/control_server.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_control_server_routes.py

No network and no live account. The routes are exercised through aiohttp's own
test client against a create_app() wired to a fake TELUS session, a fake
MobileSession, and stand-in mws_client/camera_settings functions that record what
they were asked for. The control secret is redirected into a temp directory at
import time, so this never reads or rewrites the real one.

Every property pinned here is one that FAILED SILENTLY in shipped code, which is
the whole reason this file exists: none of these bugs raised, none logged an
error, and each produced a perfectly well-formed HTTP response.

  1. _parse_json_ack returns ``{"ok", "error", "data", "token_status"}`` and the
     per-device records live inside ``data``. Code that iterated the ack itself
     was iterating [bool, None, dict, int|None], matched nothing, and returned
     None every time -- which every call site legitimately reads as "this camera
     has no such record". The settings bundle reported null forever and looked
     entirely correct doing it.
  2. This API sends booleans as the STRINGS "true"/"false", and `not "false"` is
     False in Python. The siren permission gate was a bare truthiness test, so a
     camera that EXPLICITLY DENIED permission passed the gate and would have had
     its siren fired. Nothing in a response body shows that; the only symptom is
     the sound, in someone's home.
  3. Home Assistant reads normalised keys off this route
     (siren.can_manually_trigger, privacy.supported). The bridge published the
     raw upstream spellings, so every lookup missed: no siren button was ever
     created and every privacy switch was permanently unavailable. Both are
     indistinguishable from "this account genuinely cannot do that" -- which is
     the EXPECTED state on the account this was built against, so the bug
     perfectly impersonated the feature.
  4. GetAllSirenDevices/GetAllCameraPrivacyModeInfo are account-wide, but the
     per-camera settings route re-issued both once per camera: 2N identical POSTs
     per poll against an API whose rate limits are undocumented and can lock the
     account. Also invisible in any response.
  5. The one-shot MWS token re-mint keyed on a field the ack did not carry, so
     the branch could never run. A dead recovery path looks exactly like a
     recovery path that was never needed.

The fail-closed asymmetry in 2 and 3 is deliberate and is asserted in BOTH
directions: the siren gate demands an explicit true (absent/unknown => refuse),
the privacy gate refuses only on an explicit false (absent/unknown => allow).
They look like an inconsistency to tidy up. They are not -- one guards a siren,
the other guards a switch that would otherwise be permanently unavailable, and
"simplifying" them to agree would delete the fail-closed half.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
import tempfile
import types

from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c  # noqa: E402

# Redirected BEFORE create_app is ever called: _load_or_create_secret writes to
# these paths, and a test that rotates the real install's shared secret would
# silently 401 the running Home Assistant until someone restarted the bridge.
_TMP = tempfile.mkdtemp(prefix="telus-control-test-")
c.SHARED_DIR = _TMP
c.CONTROL_SECRET_PATH = os.path.join(_TMP, "control_secret")

from bridge import control_server as cs  # noqa: E402
from bridge.mws_client import TOKEN_STATUS_LOGGED_OUT  # noqa: E402
from bridge.telus_auth import RateLimited  # noqa: E402

# Half these tests provoke the error paths on purpose (a 429'd edge, a refused
# write, an upstream that raises), and the route logs each one at warning or
# exception level. Left alone, a PASSING run prints tracebacks that read like
# failures. Nothing here asserts on log output, so the logger is muted rather
# than the assertions weakened.
logging.getLogger("bridge.control_server").setLevel(logging.CRITICAL)

CAM_DENIED = "12345-2048"       # canManuallyTriggerSiren "false" -- the dangerous one
CAM_ALLOWED = "12345-4096"      # canManuallyTriggerSiren "true"

# The shapes docs/mobile-api-findings.md records, wrapped in the ack envelope
# mws_client actually returns. Booleans are the STRINGS the app sends, on
# purpose: a fixture using real JSON booleans here would pass against the bare
# truthiness test this file exists to rule out.
def _siren_ack(**envelope):
    ack = {"ok": True, "error": None, "token_status": None,
           "data": {"AllCameraSirenInfo": [
               {"DeviceId": "2048", "isActive": "false", "timestampEnd": None,
                "canManuallyTriggerSiren": "false"},
               {"DeviceId": "4096", "isActive": "true",
                "timestampEnd": "2026-08-23T20:00:00Z",
                "canManuallyTriggerSiren": "true"}]}}
    ack.update(envelope)
    return ack


def _privacy_ack(**record):
    info = {"DeviceId": "2048", "State": 1}
    info.update(record)
    return {"ok": True, "error": None, "token_status": None,
            "data": {"CameraPrivacyModeInfo": info}}


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def test_records_are_found_inside_the_ack_envelope():
    """The per-device record lives in ack["data"], not in the ack."""
    ack = _siren_ack()
    entry = cs._mws_entry_for_device(ack, CAM_DENIED)
    assert entry is not None and entry["DeviceId"] == "2048", (
        "the siren record was not found inside the ack envelope -- this is the "
        f"bug where every lookup silently returned None: {entry}")
    assert cs._mws_entry_for_device(ack, "4096")["DeviceId"] == "4096", (
        "a bare MWS numeric id must match the same record the website's "
        '"<unit>-<did>" form does')

    privacy = cs._mws_entry_for_device(_privacy_ack(), CAM_DENIED)
    assert privacy is not None and privacy["State"] == 1, (
        f"the privacy record is an OBJECT, not a list -- both must work: {privacy}")

    # A bare action body still works, so the helper stays usable on a payload
    # that never went through _parse_json_ack.
    assert cs._mws_entry_for_device(ack["data"], "2048") is not None

    assert cs._mws_entry_for_device(ack, "9999") is None, (
        "an unmatched camera must be None -- the callers' fail-closed input")

    # Bounded, and the bound is the contract: an unexpectedly deep payload is a
    # miss (which every caller handles) rather than an unbounded tree walk on a
    # poll. Rewrite the depth if a real capture needs it; do not remove it.
    deep = {"ok": True, "error": None,
            "data": {"a": {"b": {"c": {"d": {"DeviceId": "2048"}}}}}}
    assert cs._mws_entry_for_device(deep, "2048") is None, (
        f"search must stop at _MWS_SEARCH_DEPTH={cs._MWS_SEARCH_DEPTH}")
    print("  ok  entry lookup: unwraps the ack, finds list AND object shapes, "
          "matches either half of a globalDeviceId, bounded depth")


def test_string_false_is_not_permission():
    """"false" is a truthy Python string; the normalisers must not be fooled."""
    denied = cs._normalize_siren(cs._mws_entry_for_device(_siren_ack(), "2048"))
    assert denied["can_manually_trigger"] is False, (
        'canManuallyTriggerSiren "false" read as permission to fire a real '
        f"siren -- `not \"false\"` is False in Python: {denied}")
    assert denied["active"] is False, denied

    allowed = cs._normalize_siren(cs._mws_entry_for_device(_siren_ack(), "4096"))
    assert allowed == {"can_manually_trigger": True, "active": True,
                       "timestamp_end": "2026-08-23T20:00:00Z"}, allowed

    # timestamp_end is passed through untouched: the server picks the value AND
    # its format, and an active siren with no timestamp_end is the app's own
    # error case -- "state unknown", never "runs forever".
    assert allowed["timestamp_end"] == "2026-08-23T20:00:00Z"

    on = cs._normalize_privacy({"DeviceId": "1", "State": 1})
    off = cs._normalize_privacy({"DeviceId": "1", "State": 0})
    assert on["active"] is True and off["active"] is False, (on, off)
    assert cs._normalize_privacy({"DeviceId": "1", "State": "true"})["active"] is True
    print('  ok  coercion: the strings "true"/"false" and State 0/1 become real '
          "bools, and timestamp_end is passed through unchanged")


def test_the_two_gates_err_in_opposite_directions():
    """Siren absent => refuse. Privacy absent => allow. Deliberately unequal."""
    silent = cs._normalize_siren({"DeviceId": "1"})
    assert silent["can_manually_trigger"] is False, (
        "a record that says nothing about the permission must NOT grant it: "
        f"{silent}")

    # The opposite default, on purpose: GetAllCameraPrivacyModeInfo returns a
    # privacy-STATE object rather than an enumeration of capable devices, so an
    # absent SupportsPrivacyMode is a gap in a shape recovered from a decompiled
    # app -- not the device saying no. Erring the siren's way here would leave
    # every privacy switch permanently unavailable.
    assert cs._normalize_privacy({"DeviceId": "1"})["supported"] is True, (
        "an absent SupportsPrivacyMode must not read as unsupported")
    assert cs._normalize_privacy(
        {"DeviceId": "1", "SupportsPrivacyMode": "false"})["supported"] is False, (
        'only an EXPLICIT "false" refuses -- and it must, since that is what the '
        "account this was built against actually reports")
    assert cs._normalize_privacy(
        {"DeviceId": "1", "SupportsPrivacyMode": True})["supported"] is True
    print("  ok  asymmetry: siren fails closed on an absent flag, privacy fails "
          "open, and only an explicit false refuses privacy")


def test_token_status_is_read_or_reported_absent():
    """Never invent a token status, and never log one that was never sent."""
    hoisted = {"ok": False, "error": "x", "token_status": TOKEN_STATUS_LOGGED_OUT,
               "data": {}}
    assert cs._mws_token_status(hoisted) == TOKEN_STATUS_LOGGED_OUT, (
        "the status _parse_json_ack surfaces on the ack was not found -- the "
        "re-mint branch in _mws_call goes dead again if this misses")

    nested = {"ok": False, "error": "x", "data": {"token_status": 1}}
    assert cs._mws_token_status(nested) == 1, (
        "_parse_validate_session reports its status inside data; both layouts "
        "must be read so neither parser needs a matching edit here")

    absent = {"ok": False, "error": "nope", "token_status": None, "data": {}}
    assert cs._mws_token_status(absent) is None, (
        "absent must stay None -- reading it as TOKEN_STATUS_OK (0) would make a "
        "logged-out session look healthy")

    note = cs._mws_failure_note(absent)
    assert "token_status" not in note and "nope" in note, (
        f"a log line must not claim a status the server never sent: {note!r}")
    assert "token_status=1" in cs._mws_failure_note(nested)
    print("  ok  token status: read from either layout, absent stays None, and "
          "never logged as a value when it was never sent")


# ---------------------------------------------------------------------------
# Route-level harness
# ---------------------------------------------------------------------------

class FakeMobile:
    """Stands in for the MobileSession the talk manager owns."""

    def __init__(self, session_token="placeholder-session-token"):
        self.session_token = session_token       # not a real token
        self.customer_id = "1234567"
        self.device_uid = "placeholder-uid"
        self.seamless_login_token = None


class FakeTalkManager:
    def __init__(self):
        self._mobile = FakeMobile()


class FakeTelusSession:
    """Only the two attributes the control routes touch."""

    session = object()

    async def login(self):
        raise AssertionError("no route under test may re-login")


async def _settings(_telus, _camera_id):
    return {"supports_call_volume": True, "call_volume_min": 0, "call_volume_max": 10}


@contextlib.asynccontextmanager
async def _app(siren=None, privacy=None, settings_module=None, mint=None,
               talk_manager=...):
    """A live control server with the MWS and website calls stubbed out.

    Yields (client, headers, calls). `siren`/`privacy` are coroutine functions
    standing in for the mws_client actions; `calls` counts what each stub was
    asked to do, which is how the account-wide caching is asserted.

    control_server resolves these as module globals at call time, so patching the
    module attribute is what a real request sees. `cs.camera_settings` is replaced
    wholesale with a shim rather than mutated field-by-field, so the real module
    is never left modified for whatever runs next.
    """
    calls = {"siren": 0, "privacy": 0, "start": 0, "stop": 0, "set_privacy": 0,
             "mint": 0}

    async def default_siren(_sess, _tok, **_kw):
        calls["siren"] += 1
        return _siren_ack()

    async def default_privacy(_sess, _tok, **_kw):
        calls["privacy"] += 1
        return _privacy_ack()

    async def start_siren(_sess, _tok, _did, **_kw):
        calls["start"] += 1
        return {"ok": True, "error": None, "token_status": None, "data": {}}

    async def stop_siren(_sess, _tok, _did, **_kw):
        calls["stop"] += 1
        return {"ok": True, "error": None, "token_status": None, "data": {}}

    async def set_privacy_mode(_sess, _tok, _did, _active, **_kw):
        calls["set_privacy"] += 1
        return {"ok": True, "error": None, "token_status": None, "data": {}}

    async def default_mint(mobile, _lock):
        calls["mint"] += 1
        mobile.session_token = "re-minted-placeholder"

    shim = settings_module or types.SimpleNamespace(
        CameraSettingsError=cs.camera_settings.CameraSettingsError,
        get_audio_settings=_settings, get_image_settings=_settings,
        get_video_config=_settings, get_diagnostics=_settings,
        set_call_volume=cs.camera_settings.set_call_volume)

    saved = {name: getattr(cs, name) for name in
             ("get_siren_devices", "get_privacy_mode_info", "start_siren",
              "stop_siren", "set_privacy_mode", "camera_settings", "mint_if_stale")}
    cs.get_siren_devices = siren or default_siren
    cs.get_privacy_mode_info = privacy or default_privacy
    cs.start_siren = start_siren
    cs.stop_siren = stop_siren
    cs.set_privacy_mode = set_privacy_mode
    cs.camera_settings = shim
    cs.mint_if_stale = mint or default_mint

    registry = {CAM_DENIED: {"id": CAM_DENIED}, CAM_ALLOWED: {"id": CAM_ALLOWED}}
    manager = FakeTalkManager() if talk_manager is ... else talk_manager
    app = cs.create_app(FakeTelusSession(), asyncio.Lock(), registry, {}, None,
                        manager)
    headers = {"Authorization": f"Bearer {app[cs.APP_CURRENT_SECRET]['value']}"}
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, headers, calls
    finally:
        for name, value in saved.items():
            setattr(cs, name, value)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

async def test_settings_publishes_the_keys_home_assistant_reads():
    """The normalised key contract. HA reads these EXACT names off the response.

    button.py gates the siren buttons on siren["can_manually_trigger"] and
    switch.py gates the privacy switch on privacy["supported"]; a rename on
    either side creates no entity at all rather than an error, so this is the
    only place the two halves are ever checked against each other.
    """
    async with _app() as (client, headers, _calls):
        denied = await (await client.get(
            f"/control/cameras/{CAM_DENIED}/settings", headers=headers)).json()
        allowed = await (await client.get(
            f"/control/cameras/{CAM_ALLOWED}/settings", headers=headers)).json()

    assert set(denied["siren"]) == {"can_manually_trigger", "active", "timestamp_end"}, (
        f"the siren section's key set is the contract: {denied['siren']}")
    assert set(denied["privacy"]) == {"supported", "active"}, (
        f"the privacy section's key set is the contract: {denied['privacy']}")
    assert denied["siren"] == {"can_manually_trigger": False, "active": False,
                               "timestamp_end": None}, denied["siren"]
    assert allowed["siren"]["can_manually_trigger"] is True, allowed["siren"]
    assert denied["privacy"] == {"supported": True, "active": True}, denied["privacy"]

    # No record for this camera means null for the WHOLE section, so a caller can
    # tell "the bridge has no data" from "the camera answered no".
    assert allowed["privacy"] is None, (
        f"a camera absent from the record must be null, not a default: {allowed}")
    # The website half is untouched by any of this.
    assert allowed["audio"]["supports_call_volume"] is True
    print("  ok  /settings: publishes exactly {can_manually_trigger, active, "
          "timestamp_end} and {supported, active}, null when there is no record")


async def test_account_wide_reads_are_shared_across_cameras():
    """Two cameras polled at once cost ONE upstream read each, not one apiece."""
    async with _app() as (client, headers, calls):
        # Concurrent on purpose: this is what a cold cache does under Home
        # Assistant's sweep, and without the per-action lock it stampedes into one
        # upstream POST per camera -- the exact burst the cache exists to prevent.
        await asyncio.gather(
            client.get(f"/control/cameras/{CAM_DENIED}/settings", headers=headers),
            client.get(f"/control/cameras/{CAM_ALLOWED}/settings", headers=headers))
        assert calls["siren"] == 1 and calls["privacy"] == 1, (
            "2 concurrent camera polls made more than one account-wide read each; "
            f"at N cameras this is 2N POSTs per poll: {calls}")

        await client.get(f"/control/cameras/{CAM_DENIED}/settings", headers=headers)
        assert calls["siren"] == 1 and calls["privacy"] == 1, (
            f"a poll inside the TTL must not re-read upstream: {calls}")

        # ...but a write invalidates its own action, because the caller is
        # expected to read the state straight back (an accepted MWS command is not
        # proof the camera moved) and must not be answered from before the write.
        await client.post(f"/control/cameras/{CAM_DENIED}/privacy", headers=headers,
                          json={"active": True})
        await client.get(f"/control/cameras/{CAM_DENIED}/settings", headers=headers)
        assert calls["privacy"] > 1, (
            f"the privacy write did not invalidate the cached read: {calls}")
    print("  ok  account-wide reads: one upstream call per action per sweep even "
          "under concurrency, and a write invalidates its own cache entry")


async def test_a_rate_limited_read_is_held_and_does_not_sink_the_response():
    """429 must not re-hammer the edge, and must not take the website half down."""
    async def limited(_sess, _tok, **_kw):
        limited.n += 1
        raise RateLimited(120)
    limited.n = 0

    async with _app(siren=limited) as (client, headers, _calls):
        body = await (await client.get(
            f"/control/cameras/{CAM_DENIED}/settings", headers=headers)).json()
        assert body["siren"] is None, f"a failed MWS read degrades to null: {body}"
        assert body["audio"]["supports_call_volume"] is True, (
            "an MWS outage must not take the website settings down with it -- "
            "that best-effort split is the point of the route")
        assert body["privacy"] is not None, "only the failing action degrades"

        await client.get(f"/control/cameras/{CAM_ALLOWED}/settings", headers=headers)
        assert limited.n == 1, (
            "the next camera in the sweep went straight back at an edge that just "
            f"429'd; the negative result must be cached for Retry-After: {limited.n}")
    print("  ok  rate limiting: the negative result is held for Retry-After and "
          "only the failing section degrades to null")


async def test_a_camera_that_refuses_is_never_sirened():
    """The one that makes noise in someone's home. `not "false"` is False."""
    async with _app() as (client, headers, calls):
        resp = await client.post(f"/control/cameras/{CAM_DENIED}/siren",
                                 headers=headers, json={"action": "start"})
        assert resp.status == 422, (
            f"a camera reporting canManuallyTriggerSiren \"false\" was not "
            f"refused: HTTP {resp.status}")
        assert calls["start"] == 0, (
            "ManualSirenStart was sent to a camera that explicitly denied "
            f"permission -- a real siren would have sounded: {calls}")

        resp = await client.post(f"/control/cameras/{CAM_ALLOWED}/siren",
                                 headers=headers, json={"action": "start"})
        assert resp.status == 200 and calls["start"] == 1, (
            f"a permitted camera must still work: HTTP {resp.status} {calls}")

        # "stop" is deliberately NOT gated: silencing a running siren has to stay
        # possible even when the capability read fails or says no.
        resp = await client.post(f"/control/cameras/{CAM_DENIED}/siren",
                                 headers=headers, json={"action": "stop"})
        assert resp.status == 200 and calls["stop"] == 1, (
            f"stop must never be gated on the permission flag: {resp.status} {calls}")
    print('  ok  siren: an explicit "false" is 422 with nothing fired, an explicit '
          "true fires, and stop is never gated")


async def test_privacy_refuses_only_an_explicit_false():
    async def unsupported(_sess, _tok, **_kw):
        return _privacy_ack(SupportsPrivacyMode="false")

    async with _app(privacy=unsupported) as (client, headers, calls):
        resp = await client.post(f"/control/cameras/{CAM_DENIED}/privacy",
                                 headers=headers, json={"active": True})
        assert resp.status == 422 and calls["set_privacy"] == 0, (
            f"an explicit SupportsPrivacyMode false must refuse: {resp.status}")

    # No matching record at all is NOT evidence the camera lacks the feature --
    # this action returns a state object, not a list of capable devices.
    async with _app() as (client, headers, calls):
        resp = await client.post(f"/control/cameras/{CAM_ALLOWED}/privacy",
                                 headers=headers, json={"active": True})
        assert resp.status == 200 and calls["set_privacy"] == 1, (
            f"an unmatched privacy record must allow the write through: {resp.status}")
    print("  ok  privacy: explicit false is 422, an absent record still writes")


async def test_a_logged_out_token_is_re_minted_exactly_once():
    """The re-mint that used to be unreachable, because the key did not exist."""
    replies = [{"ok": False, "error": "logged out",
                "token_status": TOKEN_STATUS_LOGGED_OUT, "data": {}},
               _siren_ack()]

    async def flaky(_sess, _tok, **_kw):
        flaky.n += 1
        return replies.pop(0) if replies else _siren_ack()
    flaky.n = 0

    async with _app(siren=flaky) as (client, headers, calls):
        resp = await client.post(f"/control/cameras/{CAM_ALLOWED}/siren",
                                 headers=headers, json={"action": "start"})
        assert calls["mint"] == 1, (
            "a logged-out ack did not trigger the one-shot re-mint -- this branch "
            f"was dead for as long as it keyed on a missing field: {calls}")
        assert flaky.n == 2, f"the action must be retried exactly once: {flaky.n}"
        assert resp.status == 200 and calls["start"] == 1, (
            f"the retry's result must be honoured: {resp.status} {calls}")
    print("  ok  re-mint: a logged-out token status re-mints once, retries once, "
          "and the retry's answer is used")


async def test_settings_errors_map_to_their_documented_status():
    """CameraSettingsError is a decision, not an upstream failure.

    Folding it into the blanket `except Exception` reported a device that clearly
    said "I cannot do this" as 502 telus_error -- the one status that tells a
    caller to retry, against real hardware.
    """
    err = cs.camera_settings.CameraSettingsError

    def module_raising(exc):
        async def getter(_telus, _camera_id):
            raise exc
        return types.SimpleNamespace(
            CameraSettingsError=err, get_audio_settings=getter,
            get_image_settings=getter, get_video_config=getter,
            get_diagnostics=getter, set_call_volume=cs.camera_settings.set_call_volume)

    cases = [
        (err("no support", unsupported=True, field="volume"), 422, "unsupported"),
        (err("bad value", unsupported=False, field="volume"), 400, "malformed"),
        (RuntimeError("upstream exploded"), 502, "telus_error"),
    ]
    for exc, status, body in cases:
        async with _app(settings_module=module_raising(exc)) as (client, headers, _):
            resp = await client.post(f"/control/cameras/{CAM_DENIED}/settings/volume",
                                     headers=headers, json={"volume": 5})
            got = await resp.json()
            assert resp.status == status and got["error"] == body, (
                f"{type(exc).__name__}(unsupported="
                f"{getattr(exc, 'unsupported', 'n/a')}) -> HTTP {resp.status} "
                f"{got}, expected {status} {body}")

    # The same mapping on the SETTER, not just the pre-read: camera_settings
    # raises rather than clamping an out-of-range value, and the route-level
    # _out_of_range fast path is not the only way that can be reached. Both paths
    # must end at 400 -- an out-of-range write is a caller mistake either way.
    async def setter(_telus, _camera_id, **_fields):
        raise err("out of range", unsupported=False, field="volume")
    setter.__name__ = "set_call_volume"
    module = types.SimpleNamespace(
        CameraSettingsError=err, get_audio_settings=_settings,
        get_image_settings=_settings, get_video_config=_settings,
        get_diagnostics=_settings, set_call_volume=setter)
    async with _app(settings_module=module) as (client, headers, _):
        resp = await client.post(f"/control/cameras/{CAM_DENIED}/settings/volume",
                                 headers=headers, json={"volume": 5})
        assert resp.status == 400, (
            f"a setter-raised out-of-range must be 400, not 502: {resp.status}")
    print("  ok  settings errors: unsupported -> 422, caller mistake -> 400, a "
          "genuine upstream failure -> 502, on both the pre-read and the setter")


async def test_mws_routes_report_unavailable_without_a_talk_manager():
    """The talk manager owns the MWS SessionToken; without it these two cannot run.

    503, not 502: nothing upstream failed, and a caller must not retry into it.
    The settings bundle takes the opposite line and still answers, with the two
    MWS sections null.
    """
    async with _app(talk_manager=None) as (client, headers, calls):
        for route, payload in ((f"/control/cameras/{CAM_ALLOWED}/siren",
                                {"action": "start"}),
                               (f"/control/cameras/{CAM_DENIED}/privacy",
                                {"active": True})):
            resp = await client.post(route, headers=headers, json=payload)
            assert resp.status == 503, f"{route} -> {resp.status}, expected 503"
            assert (await resp.json())["error"] == "mws_unavailable"

        body = await (await client.get(
            f"/control/cameras/{CAM_DENIED}/settings", headers=headers)).json()
        assert body["siren"] is None and body["privacy"] is None, body
        assert body["audio"]["supports_call_volume"] is True, (
            "TALKDOWN_ENABLED=false must not break the website settings")
        assert calls["siren"] == 0 and calls["start"] == 0, (
            f"nothing may reach MWS without a session token: {calls}")
    print("  ok  no talk manager: siren/privacy are 503 mws_unavailable and "
          "/settings still answers with both MWS sections null")


async def main():
    failures = 0
    tests = (test_records_are_found_inside_the_ack_envelope,
             test_string_false_is_not_permission,
             test_the_two_gates_err_in_opposite_directions,
             test_token_status_is_read_or_reported_absent,
             test_settings_publishes_the_keys_home_assistant_reads,
             test_account_wide_reads_are_shared_across_cameras,
             test_a_rate_limited_read_is_held_and_does_not_sink_the_response,
             test_a_camera_that_refuses_is_never_sirened,
             test_privacy_refuses_only_an_explicit_false,
             test_a_logged_out_token_is_re_minted_exactly_once,
             test_settings_errors_map_to_their_documented_status,
             test_mws_routes_report_unavailable_without_a_talk_manager)
    for test in tests:
        try:
            result = test()
            if asyncio.iscoroutine(result):
                await result
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
