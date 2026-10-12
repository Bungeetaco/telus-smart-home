"""What the TELUS SSO login does with the 2FA-requirements probe's answer
(`telus_auth_patch.py`, the last step of the replacement `async_login`).

Run from the repository root:

    python3 tests/test_login_2fa_probe.py

No Home Assistant, no network, no account: `tests/ha_stubs.py` supplies the
`homeassistant.*` and `pyalarmdotcomajax` names, `load_real_auth_patch()`
loads the REAL patch module against them with a recording stand-in for the
library's server-error handler, and the login runs against a scripted session.

Why this file exists: the invisible failure here is the opposite of silence,
a non-error logged as an error. After the SSO handoff the login asks the
website whether OTP is required; this white-label deployment does not expose
Alarm.com's 2FA engine there and answers a JSON:API error body (status "423")
on EVERY login. The library's handler logs that at ERROR with a traceback
before raising, and the patch then catches it and skips at INFO -- so every
login and every reload wrote an error line into the Home Assistant log for a
response the code itself treats as "no 2FA needed". Nothing broke, which is
exactly why it was never going to be caught by anything but a test: the
login succeeds, and the only symptom is an error that trains the reader to
ignore errors from this integration. The handler's other branches are worse:
its "409" case raises AuthenticationFailed, which the skip path does not
catch and which would demand a needless reauth.

Pinned here: a JSON:API error body never reaches the library's handler, logs
nothing above INFO (one INFO skip line naming the status), and the login
completes; a clean body still reaches the handler and is parsed afterwards
(the suggested-setup flag still raises, a disabled bitmask still completes);
and a transport failure -- a non-JSON body, a connection error -- still takes
the skip path rather than failing the login.
"""
import asyncio
import logging
import pathlib
import sys
import types

import aiohttp

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()
telus_auth_patch = ha_stubs.load_real_auth_patch()

from pyalarmdotcomajax import AlarmController  # noqa: E402  (the stub, now patched)
from pyalarmdotcomajax.exceptions import ConfigureTwoFactorAuthentication  # noqa: E402

USER_ID = "12345678"
ERROR_BODY_423 = {
    "errors": [{"status": "423", "code": "ProcessingError",
                "detail": "The request could not be processed."}]
}
IDENTITY_BODY = {
    "data": [{
        "id": USER_ID,
        "attributes": {
            "logoName": "TELUS",
            "applicationSessionProperties": {
                "inactivityWarningTimeoutMs": 600000,
                "keepAliveUrl": "/web/KeepAlive.aspx",
            },
            "localizeTempUnitsToCelsius": True,
        },
    }],
    "included": [{
        "id": USER_ID, "type": "profile/profile",
        "attributes": {"loginEmailAddress": "user@example.invalid"},
    }],
}


class _Resp:
    """One scripted response: a status, a final URL, and a JSON body (or the
    exception `.json()` raises, the way a non-JSON body does)."""

    def __init__(self, status=200, url="https://x/DetermineLandingPage.aspx",
                 body=None, json_error=None):
        self.status = status
        self.url = url
        self._body = body
        self._json_error = json_error

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._body


class _CookieJar:
    def clear(self):
        pass

    def __iter__(self):
        return iter(())


class _Session:
    """Routes the login's requests by URL substring; steps 1-4 always succeed
    and only the 2FA probe is scripted."""

    def __init__(self, probe):
        self.cookie_jar = _CookieJar()
        self._probe = probe

    def post(self, url, **kw):
        if "smarthome_login_v1" in url or "/v1/flows/login" in url:
            return _Resp()
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kw):
        if "authorization.oauth2" in url:
            return _Resp()
        if "web/api/identities" in url:
            return _Resp(body=IDENTITY_BODY)
        if "twoFactorAuthentication" in url:
            if isinstance(self._probe, Exception):
                raise self._probe
            return self._probe
        raise AssertionError(f"unexpected GET {url}")


def _controller(probe):
    c = AlarmController()
    c._websession = _Session(probe)
    c._username = "user@example.invalid"
    c._password = "hunter2"
    c._ajax_headers = {}
    c._user_email = None
    c._user_profile = {}
    return c


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)


async def _login(probe):
    """Run the patched login (through the attribute HA calls) and return
    (exception or None, captured log records across every logger)."""
    AlarmController.server_error_calls.clear()
    root = logging.getLogger()
    capture = _Capture()
    previous_level = root.level
    root.addHandler(capture)
    root.setLevel(logging.DEBUG)
    try:
        try:
            await AlarmController.async_login(_controller(probe))
        except Exception as exc:  # noqa: BLE001
            return exc, capture.records
        return None, capture.records
    finally:
        root.removeHandler(capture)
        root.setLevel(previous_level)


def _probe_handler_calls():
    return [body for body, name in AlarmController.server_error_calls
            if name == "2FA requirements"]


def _skip_lines(records):
    return [r for r in records
            if r.name == telus_auth_patch.__name__ and r.levelno == logging.INFO
            and "2FA requirements check not available" in r.getMessage()]


async def test_error_body_is_skipped_at_info_and_never_reaches_the_library_handler():
    exc, records = await _login(_Resp(body=ERROR_BODY_423))
    assert exc is None, f"an error body from the 2FA probe must not fail the login, got {exc!r}"
    assert _probe_handler_calls() == [], (
        "the JSON:API error body reached the library's handler, which logs it "
        "at ERROR with a traceback before raising -- the patch must decide first")
    loud = [r for r in records if r.levelno > logging.INFO]
    assert not loud, (
        "a response the login treats as 'no 2FA needed' logged above INFO: "
        f"{[(r.name, r.levelname, r.getMessage()) for r in loud]}")
    skips = _skip_lines(records)
    assert len(skips) == 1 and "423" in skips[0].getMessage(), (
        f"expected one INFO skip line naming the status, got "
        f"{[r.getMessage() for r in skips]}")
    print("  ok  a JSON:API error body is one INFO skip line; the library handler never sees it")


async def test_clean_body_still_reaches_the_handler_and_is_parsed():
    exc, records = await _login(_Resp(body={"data": {"attributes": {"showSuggestedSetup": True}}}))
    assert isinstance(exc, ConfigureTwoFactorAuthentication), (
        f"a clean body must be parsed after the handler: expected the suggested-setup "
        f"refusal, got {exc!r}")
    assert len(_probe_handler_calls()) == 1, (
        f"a clean body must still be handed to the library handler once, got "
        f"{len(_probe_handler_calls())} calls")
    assert not _skip_lines(records), "a clean body is not a skip"

    exc, records = await _login(_Resp(body={"data": {"attributes": {"enabledTwoFactorTypes": 0}}}))
    assert exc is None, f"a clean body with 2FA disabled must complete the login, got {exc!r}"
    assert len(_probe_handler_calls()) == 1
    print("  ok  a clean body still goes through the library handler and is parsed")


async def test_transport_failure_still_takes_the_skip_path():
    for probe, label in (
        (_Resp(body=None, json_error=ValueError("not JSON")), "a non-JSON body"),
        (aiohttp.ClientConnectionError("boom"), "a connection error"),
    ):
        exc, records = await _login(probe)
        assert exc is None, f"{label} on the 2FA probe must not fail the login, got {exc!r}"
        assert _probe_handler_calls() == [], f"{label} never has a body to hand over"
        assert len(_skip_lines(records)) == 1, (
            f"{label} must log the INFO skip line, got "
            f"{[(r.levelname, r.getMessage()) for r in records if r.name == telus_auth_patch.__name__]}")
        loud = [r for r in records if r.levelno > logging.INFO]
        assert not loud, f"{label} logged above INFO: {[r.getMessage() for r in loud]}"
    print("  ok  a non-JSON body and a connection error both take the INFO skip path")


TESTS = (
    test_error_body_is_skipped_at_info_and_never_reaches_the_library_handler,
    test_clean_body_still_reaches_the_handler_and_is_parsed,
    test_transport_failure_still_takes_the_skip_path,
)


async def main():
    failures = 0
    for test in TESTS:
        try:
            await test()
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
