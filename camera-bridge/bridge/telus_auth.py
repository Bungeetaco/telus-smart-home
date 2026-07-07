"""Standalone TELUS Smart Home SSO login, adapted from telus_auth_patch.py's
three-step OAuth/cookie dance, without any pyalarmdotcomajax dependency.

Verified live against smarthome-security.telus.com on 2026-07-07: login,
active-system lookup, camera enumeration, and video-source retrieval all
succeeded with this exact sequence.
"""
import logging

import aiohttp

from . import config as c

log = logging.getLogger(__name__)

_BROWSER_HEADERS = {
    "Origin": "https://www.telus.com",
    "Referer": "https://www.telus.com/",
    "Accept": "*/*",
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
}
_NAV_HEADERS = {**_BROWSER_HEADERS, "Accept": "text/html,application/xhtml+xml",
                "upgrade-insecure-requests": "1"}


class TelusSession:
    """Owns one aiohttp session authenticated against smarthome-security.telus.com."""

    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._ajax_headers: dict[str, str] = {}
        self._context_id: str | None = None

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
        return self

    async def __aexit__(self, *exc):
        await self._session.close()

    async def login(self) -> None:
        """Idempotent -- clears cookies first, safe to call again to re-login."""
        self._session.cookie_jar.clear()
        self._ajax_headers = {}
        redirect_uri = f"{c.TELUS_BASE}web/api/TokenSso.ashx?AuthProvider={c.TELUS_AUTH_PROVIDER}&sso_version=2"
        state = f"{c.TELUS_BASE}web/DetermineLandingPage.aspx"

        async with self._session.post(
            f"{c.AUTH_GATEWAY}/v1/flows/smarthome_login_v1",
            json={"authdomain": "telus", "loginid": c.TELUS_EMAIL, "password": c.TELUS_PASSWORD},
            headers=_BROWSER_HEADERS,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TELUS login step 1 failed: {resp.status}")

        async with self._session.post(
            f"{c.AUTH_GATEWAY}/v1/flows/login",
            params={"acr": "ci-loa2-smarthome"}, json={}, headers=_BROWSER_HEADERS,
        ) as resp:
            if resp.status != 200:
                raise RuntimeError(f"TELUS login step 2 failed: {resp.status}")

        async with self._session.get(
            f"{c.AUTH_GATEWAY}/as/authorization.oauth2",
            params={
                "response_type": "code", "scope": "adc openid",
                "redirect_uri": redirect_uri, "state": state,
                "client_id": c.TELUS_CLIENT_ID, "acr_values": "ci-loa2-smarthome",
                "service_type": "adc", "from": "login",
            },
            headers=_NAV_HEADERS, allow_redirects=True,
        ) as resp:
            final_url = str(resp.url)
            if "DetermineLandingPage" not in final_url and "web/system" not in final_url:
                raise RuntimeError(f"TELUS SSO handoff failed, landed on {final_url} "
                                    f"(possible MFA challenge not handled)")

        for cookie in self._session.cookie_jar:
            if cookie.key == "afg":
                self._ajax_headers["ajaxrequestuniquekey"] = cookie.value
        log.info("TELUS SSO login OK")

        # The video/* endpoints require a "Context-Id" header identifying which
        # alarm.com "system" (site) to operate against -- a real browser sends this
        # on every /web/api/video/* call. It's not the account/user id; it's the id
        # of whichever system has isSelected=true in availableSystemItems. Verified
        # live: this equals the Context-Id a real browser sent in the reference HAR.
        async with self._session.get(
            f"{c.TELUS_BASE}web/api/systems/availableSystemItems",
            params={"searchString": ""},
            headers={**self._ajax_headers, "Accept": "application/vnd.api+json"},
        ) as resp:
            resp.raise_for_status()
            body = await resp.json()
        self._context_id = None
        for sysitem in body.get("data", []):
            if sysitem["attributes"].get("isSelected"):
                self._context_id = sysitem["id"]
                break
        if self._context_id is None and body.get("data"):
            # Fall back to the first (only) system if none is explicitly flagged.
            self._context_id = body["data"][0]["id"]
        log.info("TELUS active system (Context-Id) = %s", self._context_id)

    @property
    def session(self) -> aiohttp.ClientSession:
        return self._session

    @property
    def ajax_headers(self) -> dict[str, str]:
        return self._ajax_headers

    @property
    def api_headers(self) -> dict[str, str]:
        """Headers for any authenticated web/api/* JSON:API call -- video, and
        also automation rules / video events, which use the identical
        Context-Id + Accept contract despite not being under web/api/video/*."""
        headers = {**self._ajax_headers, "Accept": "application/vnd.api+json"}
        if self._context_id is not None:
            headers["Context-Id"] = self._context_id
        return headers
