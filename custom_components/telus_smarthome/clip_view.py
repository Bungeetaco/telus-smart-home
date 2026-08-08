"""Click-time clip redirect: /api/telus_smarthome/clip/<video_event_id>?sig=...

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
useless. Instead each link carries an HMAC-SHA256 of its video_event_id under
a per-install random secret (CONF_CLIP_LINK_SECRET in the config entry) --
event ids are near-sequential, so without the signature this endpoint would
let anyone enumerate the clip archive. The secret is persisted, never rotated:
signatures live inside already-delivered notifications, and rotation would
re-break exactly the old-notification case this view exists to fix.
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

from .bridge_client import bridge_get
from .const import CAMERA_BRIDGE_CLIP_URL_TEMPLATE, CONF_CLIP_LINK_SECRET, DOMAIN

LOGGER = logging.getLogger(__name__)

CLIP_VIEW_PATH = f"/api/{DOMAIN}/clip"


def clip_link_sig(secret: str, video_event_id: str) -> str:
    """Signature binding one video_event_id to this install's link secret."""
    return hmac.new(secret.encode(), video_event_id.encode(), hashlib.sha256).hexdigest()


def make_clip_link(secret: str, video_event_id: str) -> str:
    """Relative signed path for one clip -- callers (notifications) prefix
    whatever base URL reaches this instance from where the link will be
    opened; the path itself doesn't care which ingress that is."""
    return f"{CLIP_VIEW_PATH}/{video_event_id}?sig={clip_link_sig(secret, video_event_id)}"


class TelusClipRedirectView(HomeAssistantView):
    """Exchange a signed clip link for a freshly-minted alarm.com URL."""

    url = CLIP_VIEW_PATH + "/{video_event_id}"
    name = f"api:{DOMAIN}:clip"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass

    def _sig_valid(self, video_event_id: str, provided_sig: str) -> bool:
        # Checked against every configured entry's secret (there is almost
        # always exactly one) rather than a single cached value, so entry
        # reloads never leave the view holding a stale secret.
        for entry in self._hass.config_entries.async_entries(DOMAIN):
            secret = entry.data.get(CONF_CLIP_LINK_SECRET)
            if secret and hmac.compare_digest(
                clip_link_sig(secret, video_event_id), provided_sig
            ):
                return True
        return False

    async def get(self, request: web.Request, video_event_id: str) -> web.Response:
        """Validate the signature, re-mint via the bridge, redirect."""
        if not self._sig_valid(video_event_id, request.query.get("sig", "")):
            LOGGER.warning("Rejected clip link with missing/bad signature for event %s", video_event_id)
            return web.Response(status=401, text="Invalid clip link.")

        try:
            body = await bridge_get(
                self._hass,
                CAMERA_BRIDGE_CLIP_URL_TEMPLATE.format(video_event_id=video_event_id),
            )
        except aiohttp.ClientResponseError as err:
            if err.status == 404:
                return web.Response(status=404, text="This clip no longer exists on alarm.com.")
            LOGGER.warning("Bridge clip re-mint failed for event %s: %s", video_event_id, err)
            return web.Response(
                status=503, text="Could not fetch the clip from alarm.com. Try again in a minute.")
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            LOGGER.warning("Camera bridge unreachable for clip %s: %s", video_event_id, err)
            return web.Response(
                status=503, text="Camera bridge is unreachable. Try again in a minute.")

        if body is None:
            # Bridge hasn't written its control secret yet (it just restarted).
            return web.Response(
                status=503, text="Camera bridge is still starting up. Try again in a minute.")

        return web.Response(status=302, headers={"Location": body["download_url"]})
