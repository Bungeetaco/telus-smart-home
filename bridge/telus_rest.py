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
    # DIAGNOSTIC: does this response carry an audio-capability field the bridge
    # has never parsed? Only the KEY NAMES are logged -- the values are the
    # relay proxyUrl, the Janus gateway URL, the Janus token and the ICE server
    # credentials, none of which may ever reach a log. The two exceptions are a
    # bool and a presence test, both safe by construction. One line per session
    # start (~1 per SESSION_MAX_LIFETIME_S per camera).
    log.info("%s: videoSource attributes=%s supportsAudio=%r audioUpStreamUrl=%s",
             camera_id, sorted(attrs), attrs.get("supportsAudio"),
             "present" if attrs.get("audioUpStreamUrl") is not None else "absent")
    return {
        "proxy_url": attrs["proxyUrl"],
        "janus_gateway_url": attrs["janusGatewayUrl"],
        "janus_token": attrs["janusToken"],
        "ice_servers": _parse_ice_servers(camera_id, attrs.get("iceServers")),
    }


def _parse_ice_servers(camera_id: str, raw) -> list:
    """The videoSource's iceServers field, which is NOT always a JSON string.

    It is normally a JSON-encoded list. Live on this account it also comes back
    as null, with the KEY still present in the response -- so `attrs["iceServers"]`
    succeeds and `json.loads(None)` then raises
    `TypeError: the JSON object must be str, bytes or bytearray, not NoneType`,
    which failed the whole session. That is backlog E89's TypeError, and it took
    until 2026-08-28 to see a traceback for it because the failure surfaced only
    as a `last_error` string on the camera's status.

    An empty list is the right answer rather than a failure. aiortc treats it as
    "no TURN/STUN configured" and still gathers host and server-reflexive
    candidates, and this account's own ICE logs show DIRECT candidate pairs
    reaching SUCCEEDED against the camera's public address, so a session with no
    ICE servers has a real chance of connecting. If it does not, it fails on the
    existing webrtcup/first-frame timeouts with their own clear errors, which is
    no worse than the instant crash it replaces and is strictly better whenever
    the direct path works.

    Logged at WARNING because it is a server-side oddity worth seeing, not a
    routine branch.
    """
    if raw is None or raw == "":
        log.warning("%s: the videoSource carried no iceServers (field present but "
                    "empty); continuing with none, so ICE has only host and "
                    "server-reflexive candidates for this session", camera_id)
        return []
    if isinstance(raw, (list, tuple)):
        # Already decoded by the JSON layer above. Nothing to parse, and calling
        # json.loads on it would raise the same TypeError this exists to stop.
        return list(raw)
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("%s: the videoSource's iceServers could not be parsed (%s); "
                    "continuing with none for this session", camera_id, type(raw).__name__)
        return []
    return list(parsed) if isinstance(parsed, (list, tuple)) else []


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


def _parse_video_event(item: dict) -> dict:
    """Shared by list_video_events (paginated) and get_video_events_by_ids
    (batch-by-id, used to enrich Activity-feed entries below) -- both hit the
    same videoEvents endpoint and get back the identical item shape, just
    filtered differently server-side."""
    a = item["attributes"]
    cam_rel = item.get("relationships", {}).get("camera", {}).get("data") or {}
    return {
        "id": item["id"],
        "camera_id": cam_rel.get("id"),
        "timestamp": a["timestamp"],
        "description": a.get("description") or "",
        "thumbnail_url": a.get("thumbnail"),
        "download_url": a.get("downloadUrl"),
        "clip_duration_ms": a.get("clipDurationMs"),
    }


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
    return [_parse_video_event(item) for item in body.get("data", [])]


async def get_video_events_by_ids(telus, ids: list[str]) -> dict[str, dict]:
    """Batch-fetch specific video events by id -- confirmed live via HAR capture
    2026-07-20 (GET videoEvents?ids[]=A&ids[]=B...), same endpoint
    list_video_events pages through, just filtered by explicit id instead.
    Keyed by id for O(1) lookup. Used by list_history_events to enrich
    Activity-feed entries that correlate to a saved clip -- historyEvents
    itself never carries thumbnail/download data (confirmed live:
    attributes.thumbnailUrl is always null there), so this is the only way
    to get it for those entries without an extra request per event.
    """
    if not ids:
        return {}
    url = f"{c.TELUS_BASE}web/api/video/videoEvents"
    params = [("ids[]", str(vid)) for vid in ids]
    async with telus.session.get(url, params=params, headers=telus.api_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()
    return {item["id"]: _parse_video_event(item) for item in body.get("data", [])}


async def list_history_events(telus, limit: int = 20) -> list[dict]:
    """Most recent entries from TELUS's system-wide Activity feed -- arm/disarm,
    door/lock, camera triggers, doorbell presses, etc across the whole
    account, newest first (confirmed live via HAR capture 2026-07-20; TELUS's
    own ordering, not sorted here). Broader than list_video_events, which only
    covers saved camera clips. Entries TELUS can correlate to a saved clip
    (relationships.media) are enriched here with that clip's thumbnail/
    download URL via one batched get_video_events_by_ids call for the whole
    page, so callers get the same clickable-clip shape as list_video_events
    for the subset of activity that's camera-related.
    """
    url = f"{c.TELUS_BASE}web/api/activity/historyEvents"
    params = {"includeCommands": "false", "numberOfEvents": str(limit)}
    async with telus.session.get(url, params=params, headers=telus.api_headers) as resp:
        resp.raise_for_status()
        body = await resp.json()
    raw_events = body.get("data", [])

    def _media_id(item: dict) -> str | None:
        media = item.get("relationships", {}).get("media", {}).get("data") or []
        return media[0]["id"] if media else None

    video_ids = [vid for item in raw_events if (vid := _media_id(item))]
    videos_by_id = await get_video_events_by_ids(telus, video_ids)

    events = []
    for item in raw_events:
        a = item["attributes"]
        video = videos_by_id.get(_media_id(item))
        events.append({
            "id": item["id"],
            "timestamp": a.get("eventDate") or a.get("date"),
            "description": a.get("description") or "",
            "device_id": a.get("globalDeviceId"),
            "device_description": a.get("deviceDescription"),
            "event_type_name": a.get("eventTypeName"),
            "thumbnail_url": video["thumbnail_url"] if video else None,
            "download_url": video["download_url"] if video else None,
            "clip_duration_ms": video["clip_duration_ms"] if video else None,
            # The videoEvents id behind this entry's clip, so callers can
            # re-mint a fresh download_url later via /control/clip/<id> --
            # the signed URLs above expire within minutes of being issued
            # (alarm.com stamps each mint; the clip itself never expires,
            # ageLimitDays is -1), so anything that stores or displays
            # download_url beyond the immediate poll cycle needs this id,
            # not the URL.
            "video_event_id": video["id"] if video else None,
        })
    return events


async def list_automation_rules(telus) -> list[dict]:
    """Video-related automation rules (recording schedules etc) -- the same
    filter[deviceType]=15 the web UI uses; omitting it returns HTTP 423
    (confirmed live) rather than an unfiltered list, so this isn't optional.
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


async def pause_rule(telus, rule_id: str) -> None:
    """Pause a video automation rule. Empty POST body (confirmed live via
    captured HAR) -- TELUS identifies the rule purely from the URL path."""
    url = f"{c.TELUS_BASE}web/api/automation/rules/rules/{rule_id}/pauseRule"
    async with telus.session.post(url, headers=telus.api_headers) as resp:
        resp.raise_for_status()


async def resume_rule(telus, rule_id: str) -> None:
    """Resume a paused video automation rule. Empty POST body, same as
    pause_rule."""
    url = f"{c.TELUS_BASE}web/api/automation/rules/rules/{rule_id}/resumeRule"
    async with telus.session.post(url, headers=telus.api_headers) as resp:
        resp.raise_for_status()
