"""Mint an alarm.com MWS SessionToken for mobile.alarm.com off an already
authenticated TELUS *website* session (bridge.telus_auth.TelusSession).

The website SPA never talks to mobile.alarm.com -- the MWS (Mobile Web Service)
token is what the native alarm.com app uses, and it is the only token that
unlocks the upstream-audio (talkdown) endpoints on GetLiveVideoStream. There is
no website REST endpoint that mints it; the app obtains it by driving the mobile
SSO servlet in a WebView against the same PingFederate IdP session the website
already holds, catching a login-as token off the redirect, and exchanging it via
a LoginAsLogin POST. This module replays that against TelusSession's live SSO
cookie jar.

Wire format reverse-engineered from com.alarm.alarmmobile.android.telus 5.10.7
(see specs/mobile_auth.md for the decompiled-source chain):

  POST https://mobile.alarm.com/MobileServlet/SubmitRequest.aspx
       Content-Type: application/x-www-form-urlencoded
       body: Action=<...>&<device params...>[&SessionToken=<...>]
  Response: XML; the fields we want are ATTRIBUTES on a per-action root element
       (<lalr> for LoginAsLogin, <slnr> for SeamlessLoginNew): st (SessionToken),
       slt (rolling SeamlessLoginToken), dcid (CustomerId), lr (LoginResult,
       2 == success), cvs (ClientVersionStatus, 0/1 == OK).

Two paths, mirroring the app:
  * SeamlessLoginNew -- a single form POST carrying a persisted rolling `slt`,
    no Cloudflare-fronted SSO hop. This is the steady-state / restart path and
    the rate-limit-safe one; preferred whenever we hold an slt.
  * SSO GET -> LoginAsLogin -- the first-time bootstrap (or when the slt is
    rejected). The GET hits the same Cloudflare edge as the website login, so
    it is treated with the same care (single-flight, honor Retry-After).

CRITICAL / UNVERIFIED: the exact redirect chain of
`SSORedirectHandler.ashx?AppType=66` when driven with the website cookie jar has
not been confirmed live (no network calls were made building this). The redirect
walk in `_sso_get_login_as_token` is written to be robust about *where* and under
*what param name* the login-as token appears, but a reviewer should confirm it
against one real GET before relying on the bootstrap path. See that method.
"""
import json
import gzip
import logging
import os
import time
import uuid
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp
# Hard dependency (pinned in requirements.txt): a build without the hardened XML
# parser must fail here at import -- caught by CI's every-module-imports job --
# rather than silently parse with stdlib ElementTree and lose entity-expansion
# protection.
import defusedxml.ElementTree as DET
from yarl import URL

from . import config as c
from .telus_auth import RateLimited  # reuse the existing Cloudflare-429 type

log = logging.getLogger(__name__)

MWS_SUBMIT = "https://mobile.alarm.com/MobileServlet/SubmitRequest.aspx"
MWS_SSO_66 = "https://mobile.alarm.com/MobileServlet/SSO/SSORedirectHandler.ashx?AppType=66"

# Static app secret decoded from BuildConfig h0.f29294x (Haiku). Not user data --
# a build-time constant shipped in every copy of the TELUS alarm.com app -- but
# still never logged (redacted out of any form dump, same as SessionToken/slt).
HAIKU = "uMzUuT0HFZg8cIDxJ2mQVTPh2RIf2IwvkTUe94xwjvLEQnAZrtejsJPOnMLfkNqeiq4DFl"
APP_BUILD = "2260"
APP_VERSION = "5.10.7"

# Cool-down enforced by mint_if_stale after a FAILED mint (MobileLoginError),
# independent of the RateLimited (429) path which honors its own Retry-After.
# The SSO bootstrap is the still-unverified path and the likeliest to fail; a
# failed bootstrap walks up to 10 GETs plus a LoginAsLogin POST against the same
# Cloudflare edge that fronts auth-gateway.telus.com. Without a cooldown a caller
# that re-mints on every doorbell press would replay that whole walk on each
# press -- exactly the request pattern telus_auth.RateLimited documents as having
# caused a multi-hour outage. login_generation only dedups CONCURRENT callers;
# this guards SEQUENTIAL retries after a failure.
LOGIN_FAILURE_COOLDOWN_S = 300

# The app sends its own UA; the SSO GET is browser-shaped (WebView), the MWS
# POSTs are plain form posts. A mobile-app-ish UA keeps both plausible.
_MOBILE_UA = f"alarmmobile-android/{APP_VERSION} (TELUS; Android 14)"

# Form-field names that never appear in a log line even when a POST is dumped.
_REDACT = frozenset({"SessionToken", "SeamlessLoginToken", "LoginAsLoginToken",
                     "Haiku", "TwoFactorId"})


class MobileLoginError(RuntimeError):
    """An MWS login POST completed but did not yield a usable session (bad
    lr/cvs, or a missing root element). Distinct from RateLimited (which is
    also a RuntimeError but must propagate to a backoff, never be retried
    against the same edge) so login()'s SeamlessLoginNew->bootstrap fallback
    can be narrow: only a genuine rejection falls through to the SSO path, a
    rate-limit does not."""


def _redacted(form: dict) -> dict:
    return {k: ("<redacted>" if k in _REDACT else v) for k, v in form.items()}


def device_fields(mobile_device_uid: str, culture: str = "en-US") -> dict:
    """The device-identity params common to every MWS login action (from
    jl0/a.java + aq0/g.java + mk0/b4.java). TwoFactorId is "<uid>:<2faId>"; a
    fresh device has an empty 2faId, i.e. "<uid>:"."""
    return {
        "UseNewSessionManager": "true",
        "MobileDeviceUid": mobile_device_uid,
        "MobileDeviceOsVersion": "34",
        "MobileDeviceModel": "Pixel 7",
        "MobileManufacturer": "Google",
        "MobileDeviceType": "2",                 # 2 = Android phone (7 = Fire TV)
        "ApplicationBuildNumber": APP_BUILD,
        "BuildString": APP_VERSION,
        "Culture": culture,
        "Haiku": HAIKU,
        "TwoFactorId": f"{mobile_device_uid}:",
        "IncludeAlarmModeEventsFilter": "false",
    }


# --- login-as token extraction from the SSO redirect chain ------------------
# The app reads Uri.getQueryParameter("loginAsKey") off the intercepted
# redirect. We are robust about three things the static analysis could not pin
# down against a live call: (1) the param may sit in the query OR the fragment
# of a hop, (2) the name may be loginAsKey or loginAsLoginToken and casing is
# not guaranteed, (3) on the terminal custom-scheme hop the name may differ
# entirely, in which case the longest opaque value is the token.

def _params_of(url: str) -> dict:
    """All query+fragment params of a URL as {name: [values]}. Handles the
    custom scheme (alarmtelus://loginaslogin?...) since urlsplit still splits a
    '//' scheme's query and fragment."""
    parts = urlsplit(url)
    params: dict = {}
    for chunk in (parts.query, parts.fragment):
        if chunk:
            for k, v in parse_qs(chunk).items():
                params.setdefault(k, []).extend(v)
    return params


def read_body_text(raw: bytes) -> str:
    """Decode an MWS response body, transparently gunzipping when the server
    returns gzip without a Content-Encoding header (SubmitRequest.aspx does this,
    so aiohttp does not auto-decompress and .text() chokes on the 1f 8b magic)."""
    if raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except OSError:
            pass
    return raw.decode("utf-8", errors="replace")


def _named_token(url: str) -> str | None:
    """loginAsKey / loginAsLoginToken from a URL's query or fragment,
    case-insensitively."""
    lower = {k.lower(): v for k, v in _params_of(url).items()}
    for name in ("loginaskey", "loginaslogintoken", "token"):
        vals = lower.get(name)
        if vals and vals[0]:
            return vals[0]
    return None


def _longest_opaque_token(url: str) -> str | None:
    """Fallback for the terminal custom-scheme hop when the token param is named
    something we don't recognise: the login-as token is a long opaque blob, so
    pick the longest value that isn't an obviously-structural param (loginAs is
    a short username/marker, not the key). Only used on a non-http(s) hop, where
    there is nothing else it could sensibly be."""
    best = None
    for k, vals in _params_of(url).items():
        if k.lower() == "loginas":            # marker, not the key
            continue
        for v in vals:
            if v and (best is None or len(v) > len(best)):
                best = v
    # Guard against picking up something trivially short.
    return best if best and len(best) >= 16 else None


class MobileSession:
    """Holds an MWS SessionToken minted off a website TelusSession.

    Reuses the website session's aiohttp ClientSession (and thus its SSO cookie
    jar) for the SSO handoff; the MWS action POSTs are plain form posts on the
    same session. Does NOT own or re-login the website session -- if that is
    stale, the caller lets telus_auth refresh it first (under its own lock).

    login_generation mirrors TelusSession.login_generation exactly: bumped only
    on a fully-successful login, and used by mint_if_stale() below to collapse a
    burst of concurrent talkdown requests after a token expiry into a single
    real login. See TelusSession's attribute comment for why the de-duplication
    matters as much as the serialization.
    """

    def __init__(self, telus, mobile_device_uid: str | None = None,
                 seamless_login_token: str | None = None):
        self._telus = telus                       # bridge.telus_auth.TelusSession
        # Persisted identity/state takes over when the caller doesn't pass it,
        # so restarts re-use the same device and go straight to SeamlessLoginNew.
        stored = _load_state()
        self.device_uid = (mobile_device_uid or stored.get("device_uid")
                           or f"adcbridge-{uuid.uuid4().hex[:16]}")
        self.seamless_login_token = seamless_login_token or stored.get("slt")
        self.session_token: str | None = None
        self.customer_id: int | None = None
        self.login_generation: int = 0
        # monotonic() deadline before which mint_if_stale refuses another mint
        # after a MobileLoginError. 0.0 == no active cooldown.
        self._login_cooldown_until: float = 0.0
        # Persist immediately if we just generated a fresh uid, so even a login
        # that never completes still reuses one stable device next time.
        if self.device_uid != stored.get("device_uid"):
            self._persist()

    @property
    def _http(self) -> aiohttp.ClientSession:
        return self._telus.session

    # ---- public entry point -------------------------------------------------
    async def login(self) -> None:
        """Obtain (or refresh) an MWS SessionToken. Prefer the silent
        SeamlessLoginNew POST; on a genuine rejection (not a rate-limit) fall
        back to the SSO -> LoginAsLogin bootstrap. Bumps login_generation on
        success. Serialization/de-duplication is the caller's job via
        mint_if_stale(); RateLimited propagates so the caller honors Retry-After
        rather than retrying against the same Cloudflare edge."""
        if self.seamless_login_token:
            try:
                await self._seamless_login()
                self.login_generation += 1
                return
            except MobileLoginError as e:
                log.warning("MWS SeamlessLoginNew rejected, falling back to SSO bootstrap: %s", e)
                self.seamless_login_token = None
                self._persist()
        login_as_token = await self._sso_get_login_as_token()
        await self._login_as_login(login_as_token)
        self.login_generation += 1

    # ---- step A: silent re-login (SeamlessLoginNew) -------------------------
    async def _seamless_login(self) -> None:
        form = device_fields(self.device_uid)
        form["Action"] = "SeamlessLoginNew"
        form["SeamlessLoginToken"] = self.seamless_login_token
        body = await self._post_mws(form)
        self._absorb(_parse_login_xml(body, "slnr"))

    # ---- step B: SSO handoff -> LoginAsLoginToken ---------------------------
    async def _sso_get_login_as_token(self) -> str:
        """Drive the mobile SSO servlet with the website's SSO cookie jar and
        pull the login-as token off the redirect chain.

        <<< THIS IS THE ONE PART TO VERIFY WITH A LIVE CALL >>> (see module
        docstring / specs/mobile_auth.md). aiohttp cannot follow the terminal
        non-http(s) scheme (alarmtelus://loginaslogin?...), so redirects are
        disabled and the chain walked by hand, scanning every hop's query AND
        fragment for the token. If the servlet actually needs its own OAuth
        round with a mobile client_id/redirect_uri rather than reusing the
        website grant, this raises and the spec lists the fallback candidates.
        """
        # The app sets this cookie on mobile.alarm.com before loading the SSO
        # URL (AlarmWebViewActivity ~L346, value = "<uid>:<2faId>").
        self._http.cookie_jar.update_cookies(
            {"twoFactorAuthenticationId": f"{self.device_uid}:"},
            response_url=URL("https://mobile.alarm.com/"),
        )
        url = MWS_SSO_66
        for _ in range(10):
            async with self._http.get(
                url, headers={"User-Agent": _MOBILE_UA, "Accept": "*/*"},
                allow_redirects=False,
            ) as resp:
                self._raise_if_rate_limited(resp)
                loc = resp.headers.get("Location", "")
                # The token may already be on the URL we landed on, or on the
                # next hop's Location; check both, query and fragment, by name.
                for candidate in (str(resp.url), loc):
                    tok = _named_token(candidate)
                    if tok:
                        return tok
                if resp.status in (301, 302, 303, 307, 308) and loc:
                    nxt = loc if loc.startswith("http") else urljoin(url, loc)
                    if not nxt.startswith("http"):
                        # Terminal custom-scheme hop (alarmtelus://...): the app
                        # would intercept it here. Named lookup already failed
                        # above, so fall back to the longest opaque param.
                        tok = _longest_opaque_token(nxt)
                        if tok:
                            return tok
                        break
                    url = nxt
                    continue
                break
        raise MobileLoginError(
            "SSO handoff did not yield a login-as token -- needs live "
            "verification of SSORedirectHandler.ashx?AppType=66 (see module docstring)")

    # ---- step C: exchange LoginAsLoginToken -> SessionToken -----------------
    async def _login_as_login(self, login_as_token: str) -> None:
        form = device_fields(self.device_uid)
        form.update({
            "Action": "LoginAsLogin",
            "LoginAsLoginToken": login_as_token,
            "RememberMe": "true",
            "HashCode": "0",
            "IncludeDealerBranding": "true",
            "IncludePushSettings": "false",
            "IncludeDashboard": "false",
            "ForceLoadSafetyButton": "false",
        })
        body = await self._post_mws(form)
        self._absorb(_parse_login_xml(body, "lalr"))

    # ---- shared -------------------------------------------------------------
    @staticmethod
    def _raise_if_rate_limited(resp: aiohttp.ClientResponse) -> None:
        """mobile.alarm.com sits behind the same Cloudflare edge as
        auth-gateway.telus.com, so any hop here can 429 -- checked before any
        step's own status handling, raising the shared telus_auth.RateLimited so
        the caller sleeps the server's own Retry-After (config.rate_limit_sleep_s)
        rather than a fixed short schedule. See telus_auth.RateLimited for why a
        fixed retry turned one bad night into a multi-hour outage."""
        if resp.status == 429:
            raise RateLimited(c.parse_retry_after(resp.headers.get("Retry-After")))

    async def _post_mws(self, form: dict) -> str:
        async with self._http.post(
            MWS_SUBMIT, data=form,                # aiohttp form-encodes the dict
            headers={"User-Agent": _MOBILE_UA, "Accept": "*/*"},
        ) as resp:
            self._raise_if_rate_limited(resp)
            if resp.status != 200:
                # Body may echo the token back; never log it, only the status.
                raise MobileLoginError(
                    f"MWS POST {form.get('Action')} -> HTTP {resp.status}")
            return read_body_text(await resp.read())

    def _absorb(self, parsed: dict) -> None:
        self.session_token = parsed["session_token"]
        if parsed["seamless_login_token"]:
            self.seamless_login_token = parsed["seamless_login_token"]   # rolling
        if parsed["customer_id"] and parsed["customer_id"] > 0:
            self.customer_id = parsed["customer_id"]
        self._persist()
        # Never log the token itself; only whether we now hold one.
        log.info("MWS SessionToken acquired (customer_id=%s, have_slt=%s, have_token=%s)",
                 self.customer_id, bool(self.seamless_login_token), bool(self.session_token))

    def _persist(self) -> None:
        _save_state({"device_uid": self.device_uid, "slt": self.seamless_login_token})


def _parse_login_xml(body: str, root_tag: str) -> dict:
    """Return the SessionToken/slt/CustomerId from the <root_tag .../> element's
    attributes. Success = a SessionToken (st) is present; lr/cvs are diagnostics
    only (the live account returns lr=0 with a valid session -- see below). Parsed with a defused XML parser rather than
    ElementTree.fromstring so a hostile MWS response can't drive an XML attack."""
    try:
        root = DET.fromstring(body)
    except Exception as e:
        raise MobileLoginError(f"MWS response not parseable as XML: {e}")
    el = root if root.tag == root_tag else root.find(f".//{root_tag}")
    if el is None:
        raise MobileLoginError(f"MWS response missing <{root_tag}> (len={len(body)})")
    a = el.attrib
    # Success predicate is the PRESENCE OF A SessionToken (st), not lr==2. The
    # decompiled il0/h.java R() checks lr==2, but the live TELUS account returns
    # lr=0 with a full, working session (st/slt/dcid all present) -- confirmed
    # end-to-end against a real capture. Gating on lr==2 rejected valid logins
    # (observed: "login failed lr=0 cvs=0" on a session that was actually fine).
    # Trust the token alarm.com actually returned; treat lr/cvs as diagnostics
    # only. An AccountSetupUrl still hard-fails (no mobile provisioning).
    try:
        lr = int(a.get("lr", "-1"))
        cvs = int(a.get("cvs", "-1"))
    except ValueError:
        lr = cvs = -1
    if a.get("asu"):
        raise MobileLoginError(f"MWS <{root_tag}> returned an AccountSetupUrl "
                               "(account not provisioned for mobile)")
    session_token = a.get("st")
    if not session_token:
        raise MobileLoginError(
            f"MWS <{root_tag}> returned no SessionToken (lr={lr} cvs={cvs})")
    if lr != 2 or cvs not in (0, 1):
        log.info("MWS <%s> login ok with non-canonical lr=%s cvs=%s "
                 "(SessionToken present; proceeding)", root_tag, lr, cvs)
    try:
        customer_id = int(a.get("dcid", "-1"))
    except ValueError:
        customer_id = -1
    return {
        "session_token": session_token,
        "seamless_login_token": a.get("slt"),
        "customer_id": customer_id,
    }


# --- persisted state on the /shared bind mount ------------------------------
# Same atomic-write + fail-open discipline as restart_guard.py: a missing or
# corrupt file degrades to "no persisted state" (fresh uid, full bootstrap)
# rather than raising, and writes go through a temp file + os.replace so a crash
# mid-write can't leave a half-written state that breaks the next start. The
# SessionToken itself is deliberately NOT persisted -- it's short-lived and
# always re-minted in memory; only the stable device uid and the rolling slt are.

def _load_state() -> dict:
    try:
        with open(c.MOBILE_AUTH_STATE_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("state file is not a JSON object")
        return data
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, ValueError, OSError):
        log.warning("Mobile-auth state at %s unreadable/corrupt; starting from a fresh "
                    "device identity", c.MOBILE_AUTH_STATE_PATH, exc_info=True)
        return {}


def _save_state(state: dict) -> None:
    try:
        os.makedirs(c.SHARED_DIR, exist_ok=True)
        tmp_path = f"{c.MOBILE_AUTH_STATE_PATH}.tmp"
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp_path, c.MOBILE_AUTH_STATE_PATH)
    except OSError:
        # Fail-open: persistence is an optimization (silent restarts), never a
        # correctness requirement -- an unwritable /shared just means the next
        # start does a fresh bootstrap.
        log.warning("Could not persist mobile-auth state to %s; next start will "
                    "re-bootstrap", c.MOBILE_AUTH_STATE_PATH, exc_info=True)


async def mint_if_stale(mobile: "MobileSession", login_lock) -> None:
    """Re-mint the MWS token unless another caller already refreshed it while we
    waited -- the mobile-side twin of telus_auth.relogin_if_stale, sharing the
    SAME login_lock so a website re-login and a mobile re-mint can't interleave.

    On a token expiry every queued talkdown request would otherwise run its own
    SeamlessLoginNew/SSO login; comparing login_generation across the lock
    boundary collapses that burst into one. See relogin_if_stale for the full
    rationale (it applies identically, and more sharply here since the bootstrap
    path hits the same Cloudflare edge as the website login).

    Beyond de-duplicating concurrent callers, this refuses to re-attempt a mint
    within LOGIN_FAILURE_COOLDOWN_S of a previous MobileLoginError (the failure
    path login_generation does NOT guard), so a caller that re-triggers on every
    doorbell press cannot hammer the SSO edge after a failure. A RateLimited (429)
    is NOT converted into this cooldown -- it carries its own Retry-After and must
    propagate unchanged for the caller's existing backoff."""
    seen = mobile.login_generation
    async with login_lock:
        if mobile.login_generation != seen:
            log.debug("Skipping redundant MWS re-mint; token already refreshed by another caller")
            return
        now = time.monotonic()
        if now < mobile._login_cooldown_until:
            remaining = mobile._login_cooldown_until - now
            raise MobileLoginError(
                f"MWS mint in cooldown after a recent failure; {remaining:.0f}s "
                f"remaining (avoids re-walking the unverified SSO bootstrap "
                f"against the Cloudflare edge)")
        try:
            await mobile.login()
        except MobileLoginError:
            # A genuine mint failure (bad lr/cvs, missing token, or an exhausted
            # SSO walk): start the cooldown so sequential retries back off.
            # RateLimited is deliberately NOT caught here -- it propagates with
            # its own Retry-After.
            mobile._login_cooldown_until = time.monotonic() + LOGIN_FAILURE_COOLDOWN_S
            raise
