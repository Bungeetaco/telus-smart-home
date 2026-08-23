"""Tests for the JSON-envelope MWS actions: camera siren and privacy mode.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_mws_json_actions.py

These actions are the odd ones out in mws_client: ordinary form-urlencoded MWS
POSTs carrying the standard token envelope, but the reply is JSON rather than XML
(recovered from the decompiled app -- no live command was ever captured on the
account, see docs/mobile-api-findings.md). Two things are worth pinning because
nothing else can catch them:

  * the JSON envelope (``isSuccess`` / ``ErrorMessage``) has to map onto the same
    ok/error contract the XML acks produce, or every caller needs to know which
    response family it is holding; and
  * ``IsActive`` goes on the wire as the STRING "true"/"false". ``str(True)`` is
    "True", which the server does not recognise -- and the failure mode is a
    privacy-mode command that reports success and changes nothing.

No network and no live account: a local aiohttp server stands in for the MWS
servlet and records the form each action posts. Every value here is a
placeholder; no real session token exists in this file.
"""
import asyncio
import contextlib
import json
import os
import pathlib
import sys

import aiohttp
from aiohttp import web

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import mws_client  # noqa: E402

TOKEN = "placeholder-session-token"          # not a real token
CUSTOMER = "1234567"
DEVICE = "2048"


def _ack(payload):
    """Run mws_client._parse_json_ack over a DECODED payload.

    This used to try the raw-body form first and fall back to the dict form, on
    the grounds that either was a reasonable interface. That hedge was broken:
    _parse_json_ack fails CLOSED on an unrecognised payload rather than raising,
    so the string attempt returned an ok=False ack instead of falling through,
    and every mapping assertion below was silently checking the rejection path.
    The interface is settled -- _post_mws_json decodes, _parse_json_ack takes the
    dict -- so this now says so, and the fail-closed behaviour is pinned
    separately in test_json_ack_mapping.
    """
    return mws_client._parse_json_ack(payload)


class FakeMws:
    """Stands in for MobileServlet/SubmitRequest.aspx: records each posted form
    and replies with a canned JSON envelope, chosen per Action."""

    def __init__(self, replies):
        self.replies = replies              # {Action: response dict}
        self.forms: list[dict] = []
        self.content_types: list[str] = []

    def form_for(self, action):
        for form in self.forms:
            if form.get("Action") == action:
                return form
        raise AssertionError(f"no {action} request was posted (saw "
                             f"{[f.get('Action') for f in self.forms]})")

    async def handle(self, request):
        # Recorded rather than asserted here: an assert inside the handler would
        # surface as an HTTP 500 and a confusing client-side error instead of the
        # test's own failure message.
        self.content_types.append(request.headers.get("Content-Type", ""))
        form = dict(await request.post())
        self.forms.append(form)
        return web.json_response(self.replies.get(form.get("Action"), {"isSuccess": True}))


async def _serve(fake, port):
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", fake.handle)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner


@contextlib.asynccontextmanager
async def _mws(replies, port):
    fake = FakeMws(replies)
    runner = await _serve(fake, port)
    original = mws_client.MWS_URL
    mws_client.MWS_URL = f"http://127.0.0.1:{port}/MobileServlet/SubmitRequest.aspx"
    try:
        async with aiohttp.ClientSession() as session:
            yield fake, session
    finally:
        mws_client.MWS_URL = original
        await runner.cleanup()


def test_json_ack_mapping():
    """isSuccess drives ok; ErrorMessage becomes the error, empty string aside."""
    ok = _ack({"isSuccess": True, "ErrorMessage": "", "DidSeamlesslyLogin": False})
    assert ok["ok"] is True, ok
    assert not ok["error"], f"an empty ErrorMessage must not become an error: {ok}"

    refused = _ack({"isSuccess": False, "ErrorMessage": "Not permitted",
                    "DidSeamlesslyLogin": False})
    assert refused["ok"] is False, refused
    assert refused["error"] == "Not permitted", refused

    # A failure with no message is still a failure -- ok must come from isSuccess,
    # not from "did the server bother to explain itself".
    silent = _ack({"isSuccess": False})
    assert silent["ok"] is False, silent

    # ...and the converse: a payload arriving with data but no isSuccess is not a
    # success by default. The siren commands run against a live alarm device;
    # "assume it worked" is the wrong default there.
    assumed = _ack({"AllCameraSirenInfo": []})
    assert assumed["ok"] is False, assumed

    # Fail closed on a shape that is not an envelope at all -- an HTML error page
    # or a raw body handed in by mistake must not read as success.
    for junk in ('{"isSuccess": true}', None, [], 42):
        rejected = _ack(junk)
        assert rejected["ok"] is False, f"{junk!r} must not parse as success"
        assert rejected["data"] == {}, rejected
    print("  ok  json ack: isSuccess -> ok, ErrorMessage -> error, empty message "
          "isn't an error, missing isSuccess isn't a success")


async def test_privacy_mode_sends_string_bool():
    """IsActive is the string "true"/"false" -- never Python's "True"/"False"."""
    replies = {"SetCameraPrivacyModeState": {
        "isSuccess": True, "ErrorMessage": "",
        "CameraPrivacyModeInfo": {"DeviceId": DEVICE, "State": 1}}}
    async with _mws(replies, 8791) as (fake, session):
        out = await mws_client.set_privacy_mode(session, TOKEN, DEVICE, True,
                                                customer_id=CUSTOMER)
        form = fake.form_for("SetCameraPrivacyModeState")
        assert form["IsActive"] == "true", (
            f"IsActive was sent as {form['IsActive']!r}; the server expects the "
            'lowercase string "true"')
        assert form["DeviceId"] == DEVICE, form
        assert out["ok"] is True, out

        await mws_client.set_privacy_mode(session, TOKEN, DEVICE, False,
                                          customer_id=CUSTOMER)
        off = fake.forms[-1]
        assert off["IsActive"] == "false", (
            f"IsActive was sent as {off['IsActive']!r}; the server expects the "
            'lowercase string "false"')
    print('  ok  privacy mode: IsActive on the wire as "true"/"false", not '
          "str(bool), and the ack maps to ok")


async def test_siren_commands_carry_the_token_envelope():
    """Start/stop use the right Action + DeviceId on the shared token envelope."""
    replies = {"ManualSirenStart": {"isSuccess": True, "ErrorMessage": ""},
               "ManualSirenStop": {"isSuccess": False,
                                   "ErrorMessage": "Siren is not active"}}
    async with _mws(replies, 8792) as (fake, session):
        started = await mws_client.start_siren(session, TOKEN, DEVICE,
                                               customer_id=CUSTOMER)
        assert started["ok"] is True, started
        form = fake.form_for("ManualSirenStart")
        assert form["DeviceId"] == DEVICE, form
        # The envelope is what makes an MWS action authenticate and scope; a
        # JSON-reply action is no different from the XML ones here.
        assert form["SessionToken"] == TOKEN, "SessionToken missing from the envelope"
        assert form["CustomerId"] == CUSTOMER, "CustomerId missing -- account scoping"

        stopped = await mws_client.stop_siren(session, TOKEN, DEVICE,
                                              customer_id=CUSTOMER)
        assert fake.form_for("ManualSirenStop")["DeviceId"] == DEVICE
        assert stopped["ok"] is False and stopped["error"] == "Siren is not active", (
            f"a refused stop must surface the server's reason: {stopped}")

        # Only the RESPONSE is JSON -- the request is the same form-urlencoded
        # POST every other MWS action uses.
        assert all(ct.startswith("application/x-www-form-urlencoded")
                   for ct in fake.content_types), fake.content_types
    print("  ok  siren: ManualSirenStart/Stop carry DeviceId + the token envelope, "
          "and a refusal surfaces the server's ErrorMessage")


async def test_reads_preserve_the_device_payload():
    """The read actions must hand back the per-device data, not just the ack.

    ``canManuallyTriggerSiren`` is a per-camera permission the app checks before
    offering the control at all, and the privacy ``State`` is the entity's value.
    Either one dropped during parsing turns into an entity that lies.
    """
    replies = {
        "GetAllSirenDevices": {
            "isSuccess": True, "ErrorMessage": "",
            "AllCameraSirenInfo": [{"DeviceId": DEVICE, "isActive": False,
                                    "timestampEnd": None,
                                    "canManuallyTriggerSiren": True}]},
        "GetAllCameraPrivacyModeInfo": {
            "isSuccess": True, "ErrorMessage": "",
            "CameraPrivacyModeInfo": {"DeviceId": DEVICE, "State": 0}},
    }
    async with _mws(replies, 8793) as (fake, session):
        sirens = await mws_client.get_siren_devices(session, TOKEN, customer_id=CUSTOMER)
        assert sirens["ok"] is True, sirens
        assert "canManuallyTriggerSiren" in json.dumps(sirens), (
            "the per-camera trigger permission was dropped -- callers would have "
            f"to fire the siren blind: {sirens}")
        assert DEVICE in json.dumps(sirens), sirens

        privacy = await mws_client.get_privacy_mode_info(session, TOKEN,
                                                         customer_id=CUSTOMER)
        assert privacy["ok"] is True, privacy
        assert "CameraPrivacyModeInfo" in json.dumps(privacy), privacy

        # Reads must not carry a DeviceId: these two are account-wide actions,
        # and neither may have any side effect on a device.
        for action in ("GetAllSirenDevices", "GetAllCameraPrivacyModeInfo"):
            assert "DeviceId" not in fake.form_for(action), (
                f"{action} is account-wide; it must not address a device")
    print("  ok  reads: siren permission flag and privacy State survive the parse, "
          "and neither read addresses a device")


async def main():
    failures = 0
    async_tests = (test_privacy_mode_sends_string_bool,
                   test_siren_commands_carry_the_token_envelope,
                   test_reads_preserve_the_device_payload)
    for test in (test_json_ack_mapping,) + async_tests:
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
