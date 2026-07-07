import json
import logging

from . import config as c

log = logging.getLogger(__name__)


async def list_cameras(telus) -> list[dict]:
    """Returns [{id, description, mac_address, model, firmware}, ...]."""
    url = f"{c.TELUS_BASE}web/api/video/devices/cameras"
    async with telus.session.get(url, params={"filterByLiveViewDisplayable": "true"},
                                  headers=telus.video_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()
    cams = []
    for cam in body["data"]:
        a = cam["attributes"]
        cams.append({
            "id": cam["id"],
            "description": a.get("description") or cam["id"],
            "mac_address": a["macAddress"],
            "model": a["deviceModel"],
            "firmware": a["firmwareVersion"],
        })
    return cams


async def get_video_source(telus, camera_id: str) -> dict:
    """Returns proxyUrl / janusGatewayUrl / janusToken / iceServers (parsed) for one camera."""
    url = f"{c.TELUS_BASE}web/api/video/videoSources/liveVideoHighestResSources/{camera_id}"
    async with telus.session.get(url, headers=telus.video_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()
    attrs = body["data"]["attributes"]
    return {
        "proxy_url": attrs["proxyUrl"],
        "janus_gateway_url": attrs["janusGatewayUrl"],
        "janus_token": attrs["janusToken"],
        "ice_servers": json.loads(attrs["iceServers"]),
    }
