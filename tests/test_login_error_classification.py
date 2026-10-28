"""What a TELUS login failure is allowed to mean (backlog E101).

THE INVISIBLE FAILURE: a transient flow failure classified as bad credentials
puts the config entry into reauth -- the integration then sits dead behind a
manual credential prompt that re-entering the (correct) password was never
going to fix, instead of auto-retrying its way back once the transient clears.
Measured live: a VM reboot raced DNS and cost a needless manual reauth plus an
HA restart. The contract pinned here: step 1's explicit credential rejection
is the ONLY AuthenticationFailed; once step 1 has accepted the password,
every later failure raises UnexpectedResponse (-> ConfigEntryNotReady,
auto-retried).

Run from the repo root: python3 tests/test_login_error_classification.py
"""

import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

# The real patch module, imported directly by file path: ha_stubs (used by the
# sibling suites) STUBS the import-time patch modules so they can run without
# pyalarmdotcomajax, but this suite exists precisely to exercise the real
# login flow, so it needs the real module -- and therefore the real library.
# When the library is absent (the stub-based CI job), skip explicitly rather
# than silently: the requirements-check CI job runs this suite for real.
import importlib.util

try:
    from pyalarmdotcomajax.exceptions import (  # noqa: E402
        AuthenticationFailed,
        UnexpectedResponse,
    )
except ImportError:
    print("SKIP: pyalarmdotcomajax not installed in this environment; the")
    print("integration-requirements CI job runs this suite with it present.")
    print("\nRESULT: PASS")
    sys.exit(0)

_spec = importlib.util.spec_from_file_location(
    "telus_auth_patch_real",
    pathlib.Path(__file__).resolve().parents[1]
    / "custom_components" / "telus_smarthome_alarmdotcom" / "telus_auth_patch.py")
telus_auth_patch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(telus_auth_patch)


class _Resp:
    def __init__(self, status=200, url="https://x/DetermineLandingPage.aspx"):
        self.status = status
        self.url = url

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _CookieJar:
    def clear(self):
        pass

    def __iter__(self):
        return iter(())


class _Session:
    """Routes the login flow's three requests by URL substring."""

    def __init__(self, step1, step2=None, step3=None):
        self.cookie_jar = _CookieJar()
        self._step1, self._step2, self._step3 = step1, step2, step3

    def post(self, url, **kw):
        if "smarthome_login_v1" in url:
            return self._step1
        if "/v1/flows/login" in url:
            return self._step2 or _Resp()
        raise AssertionError(f"unexpected POST {url}")

    def get(self, url, **kw):
        if "authorization.oauth2" in url:
            return self._step3 or _Resp()
        raise AssertionError(f"unexpected GET {url}")


def _fake_controller(session):
    class _C:  # bare attribute holder; the login only touches these
        pass
    c = _C()
    c._websession = session
    c._username = "user@example.invalid"
    c._password = "hunter2"
    c._ajax_headers = {}
    return c


async def _login_raises(session):
    try:
        # Through the same attribute HA calls, which also proves the
        # monkeypatch actually applied to AlarmController.
        await telus_auth_patch.AlarmController.async_login(_fake_controller(session))
    except Exception as exc:  # noqa: BLE001
        return exc
    return None


async def test_step1_rejection_is_the_only_authentication_failure():
    status = next(s for s in (401, 403, 400)
                  if not telus_auth_patch._is_transient_login_status(s))
    exc = await _login_raises(_Session(step1=_Resp(status=status)))
    assert isinstance(exc, AuthenticationFailed), (
        f"an explicit credential rejection (HTTP {status}) must raise "
        f"AuthenticationFailed, got {type(exc).__name__}")
    print("  ok  step-1 rejection still demands reauth (and only it)")


async def test_flow_completion_failure_after_accepted_credentials_retries():
    exc = await _login_raises(_Session(step1=_Resp(200), step2=_Resp(500)))
    assert not isinstance(exc, AuthenticationFailed), (
        "a flow-completion failure AFTER the password was accepted raised "
        "AuthenticationFailed -- the integration locks into a needless reauth "
        "instead of auto-retrying (the E101 incident)")
    assert isinstance(exc, UnexpectedResponse), f"got {type(exc).__name__}"
    print("  ok  step-2 failure after accepted credentials auto-retries")


async def test_sso_handoff_landing_elsewhere_retries():
    exc = await _login_raises(_Session(
        step1=_Resp(200), step2=_Resp(200),
        step3=_Resp(200, url="https://smarthome-security.telus.com/mfa-challenge")))
    assert not isinstance(exc, AuthenticationFailed), (
        "a wrong SSO landing page raised AuthenticationFailed -- reauth cannot "
        "fix a flow change or an MFA interstitial")
    assert isinstance(exc, UnexpectedResponse), f"got {type(exc).__name__}"
    print("  ok  step-3 wrong landing auto-retries")


TESTS = (
    test_step1_rejection_is_the_only_authentication_failure,
    test_flow_completion_failure_after_accepted_credentials_retries,
    test_sso_handoff_landing_elsewhere_retries,
)


async def main():
    failures = 0
    for t in TESTS:
        try:
            await t()
        except AssertionError as exc:
            print(f"  FAIL {t.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
