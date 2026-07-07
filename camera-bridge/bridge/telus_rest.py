import json
import logging

from . import config as c

log = logging.getLogger(__name__)


async def list_cameras(telus) -> list[dict]:
    """Returns [{id, description, mac_address, model, firmware}, ...]."""
    url = f"{c.TELUS_BASE}web/api/video/devices/cameras"
    async with telus.session.get(url, params={"filterByLiveViewDisplayable": "true"},
                                  headers=telus.api_headers) as resp:
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
    async with telus.session.get(url, headers=telus.api_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()
    attrs = body["data"]["attributes"]
    return {
        "proxy_url": attrs["proxyUrl"],
        "janus_gateway_url": attrs["janusGatewayUrl"],
        "janus_token": attrs["janusToken"],
        "ice_servers": json.loads(attrs["iceServers"]),
    }


async def get_snapshot_url(telus, camera_id: str) -> str:
    """Returns a one-shot signed JPEG URL for an on-demand still image.

    The URL points at a separate relay host (not smarthome-security.telus.com)
    and needs no cookie/auth of its own -- confirmed live: a bare
    aiohttp.get() with no headers on the returned URL succeeds. It is
    short-lived (observed working immediately after issuance; not confirmed
    how long it stays valid), so callers must fetch it promptly and never
    cache it across requests.
    """
    url = f"{c.TELUS_BASE}web/api/video/snapshots/{camera_id}"
    async with telus.session.get(url, headers=telus.api_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()
    attrs = body["data"]["attributes"]
    if attrs.get("error"):
        raise RuntimeError(f"TELUS snapshot error for {camera_id}: {attrs['error']}")
    return attrs["url"]


async def list_video_events(telus, limit: int = 20) -> list[dict]:
    """Most recent video clip events across all cameras on the account, newest
    first. TELUS's videoEvents endpoint has no per-camera filter param --
    each event carries its own camera id in relationships.camera.data.id, so
    callers filter client-side. thumbnail/download URLs are themselves
    signed, short-lived, single-use-ish links (same relay host as
    get_snapshot_url) -- passed through as-is rather than proxied, since
    they're valid to fetch directly from a browser.
    """
    url = f"{c.TELUS_BASE}web/api/video/videoEvents"
    params = {"page": "1", "pageSize": str(limit), "search": "", "startTime": "", "endTime": ""}
    async with telus.session.get(url, params=params, headers=telus.api_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()

    events = []
    for item in body.get("data", []):
        a = item["attributes"]
        cam_rel = item.get("relationships", {}).get("camera", {}).get("data") or {}
        events.append({
            "id": item["id"],
            "camera_id": cam_rel.get("id"),
            "timestamp": a["timestamp"],
            "description": a.get("description") or "",
            "thumbnail_url": a.get("thumbnail"),
            "download_url": a.get("downloadUrl"),
            "clip_duration_ms": a.get("clipDurationMs"),
        })
    return events


async def list_automation_rules(telus) -> list[dict]:
    """Video-related automation rules (recording schedules etc) -- the same
    filter[deviceType]=15 the web UI uses; omitting it returns HTTP 423
    (confirmed live) rather than an unfiltered list, so this isn't optional.
    Read-only: canBePaused is reported by TELUS but no pause/resume POST has
    been captured/implemented yet.
    """
    url = f"{c.TELUS_BASE}web/api/automation/rules/rules"
    params = {"filter[deviceType]": "15"}
    async with telus.session.get(url, params=params, headers=telus.api_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()

    rules = []
    for item in body.get("data", []):
        a = item["attributes"]
        rules.append({
            "id": item["id"],
            "name": a["name"],
            "is_paused": a["isPaused"],
            "can_be_paused": a["canBePaused"],
            "action": (a.get("action") or {}).get("description"),
            "trigger": (a.get("triggerCondition") or {}).get("description"),
            "timeframe": (a.get("timeframe") or {}).get("description"),
        })
    return rules
