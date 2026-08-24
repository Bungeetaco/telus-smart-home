"""Click-time clip redirect: /api/telus_smarthome/clip/<video_event_id>?exp=<ts>&sig=...

The signed alarm.com download URLs carried in event attributes expire within
minutes of the poll that fetched them (alarm.com stamps each mint into the
token; the clip itself never expires). A phone notification built from those
attributes therefore holds a link that is already 30-60s old at delivery and
dead by the time anyone taps it -- alarm.com's "You don't have permission to
view this clip" page. But every by-id videoEvents fetch returns freshly-signed
URLs, so the durable link is one that re-mints on demand: event.py embeds a
signed path to this view in the event attributes (clip_link), the notification
points its tap action at it (prefixed with the externally-reachable HA URL),
and this view exchanges it for a fresh alarm.com URL at tap time via the
bridge's /control/clip endpoint, then 302-redirects the browser there.

Auth: requires_auth=False, because the tap opens in a plain browser (or the
companion app's webview) with no way to attach an Authorization header, and
remote traffic reaches this instance as 127.0.0.1 so source-based trust is
useless. Instead each link carries an HMAC-SHA256 over "<video_event_id>:<exp>"
under a per-install random secret (CONF_CLIP_LINK_SECRET in the config entry).
Event ids are near-sequential, so without the signature this endpoint would let
anyone enumerate the clip archive.

The `exp` (unix seconds) is signed INTO the mac and re-checked on every request,
so a leaked link is a bearer token only until it expires (CLIP_LINK_TTL, wide
enough that tapping an old notification still works) rather than forever -- a
security-camera clip should not be reachable indefinitely from one exfiltrated
URL. The secret itself is persisted and never rotated (rotation would break
every already-delivered notification, the exact case this view exists to fix);
bounded exposure comes from the per-link expiry instead of from rotating the key.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging

import aiohttp
from aiohttp import web
from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .bridge_client import bridge_get
from .const import (
    CAMERA_BRIDGE_CLIP_URL_TEMPLATE,
    CONF_CLIP_LINK_SECRET,
    DOMAIN,
)

LOGGER = logging.getLogger(__name__)

CLIP_VIEW_PATH = f"/api/{DOMAIN}/clip"

# Headers on every response: keep the freshly-minted alarm.com URL (in the 302
# Location) out of shared caches, and stop the clip's own referrer from leaking
# the signed link onward to alarm.com's relay host.
_SAFE_HEADERS = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}


def clip_link_sig(secret: str, video_event_id: str, expiry: int) -> str:
    """Signature binding one video_event_id AND its expiry to this install's
    link secret. Binding the expiry into the mac is what makes it tamper-proof:
    a client cannot extend its own link's life without the secret."""
    msg = f"{video_event_id}:{expiry}".encode()
    return hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()


def make_clip_link(secret: str, video_event_id: str, expiry: int) -> str:
    """Relative signed path for one clip, valid until `expiry` (unix seconds) --
    callers (notifications) prefix whatever base URL reaches this instance from
    where the link will be opened; the path itself doesn't care which ingress."""
    sig = clip_link_sig(secret, video_event_id, expiry)
    return f"{CLIP_VIEW_PATH}/{video_event_id}?exp={expiry}&sig={sig}"


class TelusClipRedirectView(HomeAssistantView):
    """Exchange a signed, unexpired clip link for a freshly-minted alarm.com URL."""

    url = CLIP_VIEW_PATH + "/{video_event_id}"
    name = f"api:{DOMAIN}:clip"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _sig_valid(self, video_event_id: str, expiry: int, provided_sig: str) -> bool:
        # Checked against every configured entry's secret (there is almost
        # always exactly one) rather than a single cached value, so entry
        # reloads never leave the view holding a stale secret.
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            secret = entry.data.get(CONF_CLIP_LINK_SECRET)
            if not secret:
                continue
            try:
                matches = hmac.compare_digest(
                    clip_link_sig(secret, video_event_id, expiry), provided_sig
                )
            except TypeError:
                # compare_digest rejects a non-ASCII str operand outright. A
                # `?sig=` with a stray non-ASCII byte is simply a failed auth,
                # but unguarded it escaped as a 500 + error traceback on an
                # internet-facing, unauthenticated endpoint -- trivially
                # scriptable log-flood. Mirror control_server._check_auth.
                matches = False
            if matches:
                return True
        return False

    async def get(self, request: web.Request, video_event_id: str) -> web.Response:
        """Validate the signature and expiry, re-mint via the bridge, redirect."""
        try:
            expiry = int(request.query.get("exp", ""))
        except ValueError:
            LOGGER.warning("Rejected clip link with missing/bad expiry for event %s", video_event_id)
            return web.Response(status=401, text="Invalid clip link.", headers=_SAFE_HEADERS)

        if not self._sig_valid(video_event_id, expiry, request.query.get("sig", "")):
            LOGGER.warning("Rejected clip link with missing/bad signature for event %s", video_event_id)
            return web.Response(status=401, text="Invalid clip link.", headers=_SAFE_HEADERS)

        # Expiry is checked AFTER the signature so an attacker learns nothing
        # from the 401-vs-410 split without a valid mac in the first place.
        if expiry < int(dt_util.utcnow().timestamp()):
            return web.Response(
                status=410, text="This clip link has expired. Newer notifications carry fresh links.",
                headers=_SAFE_HEADERS)

        try:
            body = await bridge_get(
                self._hass,
                CAMERA_BRIDGE_CLIP_URL_TEMPLATE.format(video_event_id=video_event_id),
                # 30s, not bridge_get's 10s default: the bridge may have to
                # re-login to TELUS on a stale session (up to 4 sequential
                # Cloudflare-fronted requests) before it can re-mint. A tap is
                # interactive and rare, so the longer ceiling is cheap here.
                timeout=30,
            )
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                return web.Response(
                    status=404, text="This clip no longer exists on alarm.com.", headers=_SAFE_HEADERS)
            LOGGER.warning("Bridge clip re-mint failed for event %s: %s", video_event_id, err)
            return web.Response(
                status=503, text="Could not fetch the clip from alarm.com. Try again in a minute.",
                headers=_SAFE_HEADERS)
        except asyncio.TimeoutError:
            # Distinct from a connection failure: the bridge answered slowly,
            # almost always a TELUS re-login in progress. One re-tap recovers.
            LOGGER.warning("Bridge slow re-minting clip %s (likely TELUS re-login)", video_event_id)
            return web.Response(
                status=504, text="The camera bridge is busy (re-logging in to TELUS). Try again in a moment.",
                headers=_SAFE_HEADERS)
        except aiohttp.ClientError as err:
            LOGGER.warning("Camera bridge unreachable for clip %s: %s", video_event_id, err)
            return web.Response(
                status=503, text="Camera bridge is unreachable. Try again in a minute.",
                headers=_SAFE_HEADERS)

        if body is None:
            # Bridge hasn't written its control secret yet (it just restarted).
            return web.Response(
                status=503, text="Camera bridge is still starting up. Try again in a minute.",
                headers=_SAFE_HEADERS)

        return web.Response(
            status=302, headers={"Location": body["download_url"], **_SAFE_HEADERS})
