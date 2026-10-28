"""Monkeypatch pyalarmdotcomajax to authenticate via TELUS Smart Home's SSO
instead of alarm.com's native login form.

TELUS Smart Home is a white-labeled Alarm.com dealer portal hosted at
smarthome-security.telus.com. The REST/websocket API is identical to
alarm.com's own; only the login handshake differs. Reverse-engineered from
a HAR capture of a real browser login:

  1. POST auth-gateway.telus.com/v1/flows/smarthome_login_v1  (submits credentials)
  2. POST auth-gateway.telus.com/v1/flows/login?acr=ci-loa2-smarthome
  3. GET  auth-gateway.telus.com/as/authorization.oauth2 (...)  -- OAuth2 code grant,
     redirect chain lands on smarthome-security.telus.com/web/api/TokenSso.ashx
     which sets the real dealer-portal session cookie.

This file is imported by custom_components/telus_smarthome_alarmdotcom/__init__.py before the
integration constructs any AlarmController, so the patch is in place first.

CAVEAT: this only patches the *library* (pyalarmdotcomajax) at Python-import
time from a file living in custom_components (which persists across HA Core
container recreation), so it survives Core updates. It does NOT handle a TELUS
MFA/OTP challenge at auth-gateway.telus.com -- none was observed in the
captured login, but if TELUS enables 2FA on the gateway later this will need
to be extended.
"""
from __future__ import annotations

import contextlib
import logging
from datetime import datetime

import aiohttp

import pyalarmdotcomajax
import pyalarmdotcomajax.const as c
from pyalarmdotcomajax import AlarmController, extensions
from pyalarmdotcomajax.const import OtpType
from pyalarmdotcomajax.exceptions import (
    AuthenticationFailed,
    NotAuthorized,
    ConfigureTwoFactorAuthentication,
    UnexpectedResponse,
)

def _is_transient_login_status(status: int) -> bool:
    """True for TELUS login-gateway statuses that mean "try again later", never
    "bad credentials" -- 429 is the one actually observed live (hit repeatedly
    during a day of frequent bridge/HA restarts), 5xx included on the same
    reasoning since a server-side failure can't mean the password was wrong
    either. Anything else (400/401/403/etc) keeps the original
    AuthenticationFailed treatment."""
    return status == 429 or status >= 500

log = logging.getLogger(__name__)


def _jsonapi_error_status(json_rsp: object) -> str | None:
    """The `status` of the first JSON:API error in a response body, or None
    when the body carries no `errors` (or is not a JSON object at all). The
    2FA probe below decides on this BEFORE handing the body to the library,
    whose handler logs an error body at ERROR with a traceback first."""
    if not isinstance(json_rsp, dict):
        return None
    errors = json_rsp.get("errors")
    if not errors:
        return None
    first = errors[0] if isinstance(errors, list) and errors else None
    status = first.get("status") if isinstance(first, dict) else None
    return str(status) if status is not None else "unknown"


TELUS_BASE = "https://smarthome-security.telus.com/"
AUTH_GATEWAY = "https://auth-gateway.telus.com"
TELUS_CLIENT_ID = "167c1141-d72c-4d39-8c72-1cebd10e0109"
TELUS_AUTH_PROVIDER = "9AE4AC54755C5EBFC489CB6CFAA69C1C"

_BROWSER_HEADERS = {
    "Origin": "https://www.telus.com",
    "Referer": "https://www.telus.com/",
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.7",
    "sec-ch-ua": '"Not;A=Brand";v="8", "Chromium";v="150", "Brave";v="150"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"macOS"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-site",
    "sec-gpc": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
    ),
}

# Headers for the top-level "navigation" style GET (the OAuth authorize redirect),
# as opposed to the XHR/fetch-style JSON POSTs above.
_NAV_HEADERS = {
    **_BROWSER_HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}

# Patch the base URL used to build every other API/template URL in the library.
c.URL_BASE = TELUS_BASE

# ...but URL_BASE only covers URLs the library *formats* from it at call time. The
# camera-settings extension bakes a fully-qualified alarm.com URL into a class
# attribute at its own import time -- which has already happened by the time this
# module runs (importing AlarmController above pulls in extensions) -- so it would
# keep pointing at www.alarm.com no matter what URL_BASE says, and every Skybell
# config option (the switch/select/number platforms) would silently talk to the
# wrong host. Rewrite the host portion in place, preserving the path.
for _ext_cls in (extensions.CameraSkybellControllerExtension,):
    _endpoint = getattr(_ext_cls, "ENDPOINT", None)
    if isinstance(_endpoint, str) and "alarm.com" in _endpoint:
        _ext_cls.ENDPOINT = _endpoint.replace(
            "https://www.alarm.com/", TELUS_BASE
        ).replace("https://alarm.com/", TELUS_BASE)
        log.debug("Repointed %s.ENDPOINT to %s", _ext_cls.__name__, _ext_cls.ENDPOINT)


async def _telus_async_login(self: AlarmController) -> None:
    """Replacement for AlarmController.async_login that authenticates via TELUS SSO."""

    log.debug("Attempting to log in to Alarm.com via TELUS Smart Home SSO")

    # TELUS's identity gateway rejects a login attempt outright ("Logout or clear
    # cookies first") if stale session cookies from a prior login are still present.
    # This matters for re-login on session expiry: the AlarmController's aiohttp
    # session is dedicated to this integration instance (created via
    # async_create_clientsession, not shared with the rest of HA), so it's safe to
    # wipe it clean before every login attempt, not just the first one.
    self._websession.cookie_jar.clear()

    redirect_uri = f"{TELUS_BASE}web/api/TokenSso.ashx?AuthProvider={TELUS_AUTH_PROVIDER}&sso_version=2"
    state = f"{TELUS_BASE}web/DetermineLandingPage.aspx"

    try:
        # Step 1: submit credentials to TELUS's identity gateway.
        async with self._websession.post(
            url=f"{AUTH_GATEWAY}/v1/flows/smarthome_login_v1",
            json={"authdomain": "telus", "loginid": self._username, "password": self._password},
            headers=_BROWSER_HEADERS,
        ) as resp:
            if resp.status != 200:
                if _is_transient_login_status(resp.status):
                    # Previously always raised AuthenticationFailed here, which
                    # __init__.py maps to ConfigEntryAuthFailed -- putting the config
                    # entry into reauth/setup_error and demanding the user re-enter
                    # credentials that were never actually wrong. UnexpectedResponse
                    # instead maps to ConfigEntryNotReady, HA's normal
                    # automatically-retried-with-backoff state. Confirmed live
                    # 2026-07-18: this status hit repeatedly after a day of frequent
                    # bridge/HA restarts, needing a manual wait-then-reload each time.
                    log.warning("TELUS smarthome_login_v1 returned %s (transient) -- "
                                "will retry, not a credentials problem", resp.status)
                    raise UnexpectedResponse(f"TELUS login gateway returned {resp.status}")
                log.error("TELUS smarthome_login_v1 failed with status %s", resp.status)
                raise AuthenticationFailed("Invalid TELUS username or password.")
            # (A body check for "error" used to live here guarded by
            # `resp.status != 200`, inside a branch only reachable WHEN the status is
            # 200 -- it could never fire. Removed rather than re-enabled: a substring
            # match for "error" anywhere in a successful response body is far too
            # eager to turn into an authentication failure.)

        # Step 2: complete the login flow (issues the gateway session).
        # NOTE: must send an explicit empty JSON body (not a truly bodiless POST) --
        # the gateway 415s a request with no Content-Type header at all.
        async with self._websession.post(
            url=f"{AUTH_GATEWAY}/v1/flows/login",
            params={"acr": "ci-loa2-smarthome"},
            json={},
            headers=_BROWSER_HEADERS,
        ) as resp:
            if resp.status != 200:
                # Credentials were already ACCEPTED by step 1's 200; a failure
                # here is the gateway's flow being unavailable, never proof of a
                # bad password. UnexpectedResponse maps to ConfigEntryNotReady
                # (auto-retry); AuthenticationFailed would demand a needless
                # reauth -- measured live 2026-08-29: a VM boot raced DNS, the
                # flow failed transiently, and the integration locked itself
                # behind a manual reauth instead of recovering on its own.
                log.warning("TELUS login flow completion failed with status %s "
                            "-- will retry, not a credentials problem", resp.status)
                raise UnexpectedResponse("TELUS login flow did not complete.")

        # Step 3: OAuth2 authorization code grant. Follow the full redirect chain --
        # auth-gateway -> TokenSso.ashx -> DetermineLandingPage.aspx -- which is what
        # actually sets the dealer-portal session cookie on smarthome-security.telus.com.
        async with self._websession.get(
            url=f"{AUTH_GATEWAY}/as/authorization.oauth2",
            params={
                "response_type": "code",
                "scope": "adc openid",
                "redirect_uri": redirect_uri,
                "state": state,
                "client_id": TELUS_CLIENT_ID,
                "acr_values": "ci-loa2-smarthome",
                "service_type": "adc",
                "from": "login",
            },
            headers=_NAV_HEADERS,
            allow_redirects=True,
        ) as resp:
            final_url = str(resp.url)
            if "DetermineLandingPage" not in final_url and "web/system" not in final_url:
                # Same reasoning as step 2: the password was already accepted,
                # so a wrong landing page is a flow change, an infra hiccup, or
                # an MFA interstitial -- re-entering the same credentials fixes
                # none of those, so this must not trigger reauth.
                log.error("TELUS SSO handoff did not land on the expected page: %s "
                          "-- will retry (a persistent repeat of this line may be "
                          "an MFA challenge this patch doesn't handle)", final_url)
                raise UnexpectedResponse("TELUS SSO handoff did not complete.")

    except aiohttp.ClientError as err:
        log.exception("Network error during TELUS SSO login.")
        raise UnexpectedResponse from err

    # Extract the anti-forgery cookie set during the SSO handoff, same as upstream
    # does right after its own login POST. Not always present depending on vendor.
    with contextlib.suppress(StopIteration):
        afg_cookie = next(c for c in self._websession.cookie_jar if c.key == "afg")
        self._ajax_headers["ajaxrequestuniquekey"] = afg_cookie.value

    self._last_session_refresh = datetime.now()
    log.debug("Logged in to Alarm.com via TELUS SSO.")

    #
    # Step 4 (unchanged from upstream): fetch identity/profile info.
    #
    async with self._websession.get(
        url=c.IDENTITIES_URL_TEMPLATE.format(c.URL_BASE, ""),
        headers=self._ajax_headers,
    ) as resp:
        json_rsp = await resp.json()

        try:
            self._provider_name = json_rsp["data"][0]["attributes"]["logoName"]
        except (KeyError, IndexError):
            self._provider_name = "TELUS"

        try:
            self._user_id = json_rsp["data"][0]["id"]

            for inclusion in json_rsp["included"]:
                if inclusion["id"] == self._user_id and inclusion["type"] == "profile/profile":
                    self._user_email = inclusion["attributes"]["loginEmailAddress"]

            if not self._user_email:
                raise UnexpectedResponse("Failed to get user's email address.")
        except KeyError as err:
            # A malformed identity response is upstream having a moment (or a
            # schema change), not a credentials problem -- the login itself
            # already succeeded to get this far.
            log.exception("Failed to get user's identity info.")
            raise UnexpectedResponse from err

        try:
            self._session_refresh_interval_ms = json_rsp["data"][0]["attributes"][
                "applicationSessionProperties"
            ]["inactivityWarningTimeoutMs"]
            if not self._session_refresh_interval_ms:
                raise KeyError
        except KeyError:
            self._session_refresh_interval_ms = self.SESSION_REFRESH_DEFAULT_INTERVAL_MS

        try:
            self._keep_alive_url = json_rsp["data"][0]["attributes"]["applicationSessionProperties"][
                "keepAliveUrl"
            ]
            if not self._keep_alive_url:
                raise KeyError
        except KeyError:
            self._keep_alive_url = self.KEEP_ALIVE_DEFAULT_URL

        with contextlib.suppress(KeyError):
            self._user_profile["uses_celsius"] = json_rsp["data"][0]["attributes"][
                "localizeTempUnitsToCelsius"
            ]

    #
    # Step 5: check whether OTP is required. TELUS's white-label deployment doesn't
    # expose alarm.com's native 2FA engine for this endpoint (auth already happened at
    # TELUS's own gateway in steps 1-3), so treat any failure here as "no 2FA needed"
    # rather than a fatal error.
    #
    try:
        async with self._websession.get(
            url=self.LOGIN_2FA_DETAIL_URL_TEMPLATE.format(c.URL_BASE, self._user_id),
            headers=self._ajax_headers,
        ) as resp:
            json_rsp = await resp.json()
        if (error_status := _jsonapi_error_status(json_rsp)) is not None:
            # This account answers the probe with a JSON:API error body (status
            # "423") on EVERY login. The library's _async_handle_server_errors
            # logs such a body at ERROR with a traceback before it raises, for a
            # response the except clause below then skips at INFO -- an error
            # line per login/reload for a non-error. Its "409" branch is worse:
            # AuthenticationFailed, which nothing below catches and which would
            # demand a needless reauth. So the decision is made here and the
            # library only ever sees a body without errors.
            log.info(
                "2FA requirements check not available for this TELUS account (the "
                "endpoint answered a JSON:API error, status %s) -- skipping.",
                error_status,
            )
            return
        await self._async_handle_server_errors(json_rsp, "2FA requirements", False)
    except (
        aiohttp.ClientResponseError,
        NotAuthorized,
        # A non-JSON body raises from resp.json() itself (ValueError /
        # ContentTypeError), and a transport failure is a ClientError; both are
        # the normal shape of "this white-label doesn't expose alarm.com's 2FA
        # engine", which the comment above says to treat as "no 2FA needed" --
        # they used to escape this handler and turn a good login into a hard
        # failure. NotAuthorized / UnexpectedResponse stay for whatever the
        # library's handler may still raise on a body without errors.
        UnexpectedResponse,
        aiohttp.ClientError,
        ValueError,
    ) as err:
        log.info("2FA requirements check not available for this TELUS account (%s) -- skipping.", err)
        return

    if not (attribs := json_rsp.get("data", {}).get("attributes")):
        log.info("2FA requirements response had no usable data -- assuming no 2FA needed.")
        return

    if attribs.get("showSuggestedSetup") is True:
        raise ConfigureTwoFactorAuthentication

    enabled_otp_types_bitmask = attribs.get("enabledTwoFactorTypes")
    # Guard BEFORE the comprehension. The falsy/missing check used to sit in the
    # condition below, i.e. after the bitmask had already been &-ed against every
    # OtpType -- so a response omitting enabledTwoFactorTypes (or sending null,
    # which is exactly what "2FA not configured" looks like) raised TypeError from
    # the comprehension. Nothing maps that to a config-flow error, so it surfaced as
    # a bare "unknown" failure on an account with no 2FA at all.
    if not enabled_otp_types_bitmask:
        return

    enabled_2fa_methods = [
        otp_type for otp_type in OtpType if bool(enabled_otp_types_bitmask & otp_type.value)
    ]

    if (
        (OtpType.disabled in enabled_2fa_methods)
        or (attribs.get("isCurrentDeviceTrusted") is True)
        or not enabled_2fa_methods
    ):
        return

    log.info("Requires two-factor authentication. Enabled methods are %s", enabled_2fa_methods)
    from pyalarmdotcomajax.exceptions import OtpRequired

    raise OtpRequired(enabled_2fa_methods)


# --- Compatibility guard -------------------------------------------------
# This patch reaches into pyalarmdotcomajax internals: it REPLACES
# AlarmController.async_login and reads several class-level attributes below.
# A new pyalarmdotcomajax version can rename or remove any of them, which would
# otherwise break login silently. Fail LOUDLY here instead, naming exactly what
# changed. (The CI smoke test in .github/ imports this module to exercise this
# guard on every dependency-bump PR -- see docs/development.md / AGENTS.md.)
_REQUIRED_CONTROLLER_ATTRS = (
    "async_login",  # the coroutine we replace
    "SESSION_REFRESH_DEFAULT_INTERVAL_MS",
    "KEEP_ALIVE_DEFAULT_URL",
    "LOGIN_2FA_DETAIL_URL_TEMPLATE",
    "_async_handle_server_errors",  # private coroutine the replacement calls
)
# Module-level constants and the extension class this patch also rewrites. These
# are NOT AlarmController attributes, so they need their own check.
_REQUIRED_CONST_ATTRS = ("URL_BASE", "IDENTITIES_URL_TEMPLATE")
# NOTE: the replacement body additionally populates ~12 private INSTANCE
# attributes (_websession, _ajax_headers, _user_id, _user_email, _keep_alive_url,
# _session_refresh_interval_ms, _provider_name, _user_profile, ...). Those only
# exist after __init__, so they can't be checked here -- the CI smoke test
# (.github/scripts/check_patch.py) constructs a throwaway controller and asserts
# them. Keep the two lists in sync when this patch changes.
_missing = [a for a in _REQUIRED_CONTROLLER_ATTRS if not hasattr(AlarmController, a)]
_missing += [f"const.{a}" for a in _REQUIRED_CONST_ATTRS if not hasattr(c, a)]
if not hasattr(getattr(extensions, "CameraSkybellControllerExtension", None), "ENDPOINT"):
    _missing.append("extensions.CameraSkybellControllerExtension.ENDPOINT")
if _missing:
    raise RuntimeError(
        "TELUS SSO patch is incompatible with the installed pyalarmdotcomajax "
        f"({getattr(pyalarmdotcomajax, '__version__', 'unknown')}): AlarmController "
        f"is missing {_missing}. The pinned version was changed without updating "
        "telus_auth_patch.py -- review the upstream changes before bumping "
        "(see AGENTS.md / docs/development.md)."
    )

AlarmController.async_login = _telus_async_login
log.info("TELUS Smart Home SSO patch applied to pyalarmdotcomajax (URL_BASE=%s)", TELUS_BASE)
