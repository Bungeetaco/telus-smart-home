"""Local-only control endpoint so Home Assistant's TELUS Smart Home integration
(its config-flow reconfigure/reauth step) can push updated TELUS credentials
into this already-running bridge process, without restarting the container.

Bound to 127.0.0.1 only. Reachable from the HA container because both
containers run with --network host (confirmed via `docker inspect`) -- never
exposed off the host, never given a Docker port-publish mapping.

Authentication is a random per-install shared secret, generated on first start
and persisted to the shared bind-mounted directory (config.CONTROL_SECRET_PATH)
so the HA side can read it directly off disk (it shares the same mount from
the HA-container side at /share/telus_cam_bridge) rather than needing any kind
of interactive pairing step.

Contract (as implemented -- this is what callers must match):

    POST http://127.0.0.1:<CONTROL_PORT>/control/credentials
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>
    Content-Type: application/json
    Body: {"username": "<telus email>", "password": "<telus password>"}

    Responses:
      200 {"status": "applied"}          -- credentials swapped in, re-login succeeded
      400 {"error": "malformed"}         -- body wasn't valid JSON / missing or
                                             empty username or password
      401 {"error": "bad_secret"}        -- Authorization header missing/wrong
      500 {"error": "internal"}          -- re-login with the new credentials
                                             itself failed (e.g. genuinely wrong
                                             password, or TELUS/network error);
                                             the new credentials are still left
                                             in place in config.py so the
                                             process's normal retry paths keep
                                             trying them rather than silently
                                             reverting to the old (also
                                             possibly-stale) ones

    GET http://127.0.0.1:<CONTROL_PORT>/control/health   (no auth required --
      no secret, this leaks no credential material, just process liveness)
    Response: 200 {"status": "ok"}

    GET http://127.0.0.1:<CONTROL_PORT>/control/status
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"cameras": {"<camera_id>": {"state": "streaming"|"connecting"|
                                                 "reconnecting"|"restart_deferred",
                                                 "consecutive_failures": ...,
                                                 "last_frame_at": <unix ts or null>,
                                                 "fps": ..., "hardware_encode": ...,
                                                 "last_error": <str or null>}, ...}}
      Live per-camera diagnostics maintained by camera_session.py's own
      run_camera_forever loop (main.py threads a shared dict into it) --
      reconnect counts, last-frame age, and current encode mode, without
      needing to grep container logs. "restart_deferred" is the rarest and
      most informative one: this camera hit the MAX_CONSECUTIVE_SESSION_FAILURES
      escape hatch, but it has fired too often lately to be a local wedge, so
      restart_guard.py is holding the process before it exits (see there).

    GET http://127.0.0.1:<CONTROL_PORT>/control/cameras
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"cameras": [{"id": ..., "name": ..., "model": ..., "firmware": ...,
                                 "mac_address": ..., "rtsp_url": ...}, ...]}
      Reflects main.py's in-memory camera_registry, which is populated on
      startup and kept current by discover_and_launch_cameras()'s periodic
      poll -- so a camera added to the TELUS account shows up here within
      config.CAMERA_DISCOVERY_INTERVAL_S of being added, no restart needed on
      either side.

    GET http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/snapshot
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200, Content-Type: image/jpeg, raw JPEG bytes -- a fresh
      on-demand still image fetched from TELUS for this one request (not
      cached; TELUS's snapshot URLs are signed and short-lived, so every
      call here does a real round trip to TELUS, unlike /control/cameras).
      404 {"error": "unknown_camera"} if camera_id isn't in camera_registry.
      502 {"error": "telus_error"} if the TELUS-side fetch itself fails.

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/talk
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>
    Content-Type: application/json
    Body: {"audio_url": "<http(s) url the bridge fetches>"}   and/or
          {"audio_path": "<path readable by the bridge>"}
      Play a short audio clip (typically HA-rendered TTS) through the camera's
      speaker via two-way audio (talkdown). audio_url wins if both are given.
      Off by default: returns 403 until config.TALKDOWN_ENABLED is set, so the
      route ships inert. Single in-flight talk per camera -- a concurrent
      trigger for the same camera is rejected (409), never queued. See talk.py.

    Responses:
      200 {"status": "spoken", "path": "webrtc"|"srtp"}
      400 {"error": "malformed"}          -- bad JSON / neither audio field / bad audio
      401 {"error": "bad_secret"}         -- Authorization missing/wrong
      403 {"error": "talkdown_disabled"}  -- config.TALKDOWN_ENABLED is false
      404 {"error": "unknown_camera"}     -- camera_id not in camera_registry
      409 {"error": "talk_in_progress"}   -- a talk is already in flight for THIS camera
      413 {"error": "media_too_large"}    -- fetched/read audio exceeded the cap
      422 {"error": "no_upstream_audio"}  -- no webrtc/srtp upstream-audio endpoint
      502 {"error": "telus_error"}        -- MWS auth / stream lookup / signaling failed
      504 {"error": "talk_timeout"}       -- session exceeded TALK_SESSION_TIMEOUT_S

    GET http://127.0.0.1:<CONTROL_PORT>/control/events?limit=<N>
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"events": [{"id": ..., "camera_id": ..., "timestamp": ...,
                                "description": ..., "thumbnail_url": ...,
                                "download_url": ..., "clip_duration_ms": ...}, ...]}
      Most recent video clip events across all cameras, newest first.
      thumbnail_url/download_url are themselves signed TELUS relay-host URLs,
      passed through as-is -- fetchable directly (confirmed live, no cookie
      needed), not proxied through this endpoint.

    GET http://127.0.0.1:<CONTROL_PORT>/control/automations
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"rules": [{"id": ..., "name": ..., "is_paused": ...,
                               "can_be_paused": ..., "action": ...,
                               "trigger": ..., "timeframe": ...}, ...]}
      Video-related automation rules (recording schedules etc).

    GET http://127.0.0.1:<CONTROL_PORT>/control/history?limit=<N>
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"events": [{"id": ..., "timestamp": ..., "description": ...,
                                "device_id": ..., "device_description": ...,
                                "event_type_name": ..., "thumbnail_url": ...,
                                "download_url": ..., "clip_duration_ms": ...,
                                "video_event_id": ...}, ...]}
      TELUS's system-wide Activity feed -- arm/disarm, door/lock, camera
      triggers, doorbell presses, etc, newest first. Broader than
      /control/events (which is camera clips only): entries the bridge could
      correlate to a saved clip carry the same thumbnail_url/download_url/
      clip_duration_ms shape as /control/events, so callers can offer the
      same "click to see the clip" behavior here too; entries with no
      correlated clip have those fields as null. video_event_id is the id of
      the correlated saved clip (null when there is none) -- pass it to
      /control/clip to re-mint a fresh download_url, since the one embedded
      here expires within minutes of this fetch (the clip itself does not).

    GET http://127.0.0.1:<CONTROL_PORT>/control/clip/<video_event_id>
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"download_url": ..., "thumbnail_url": ..., "clip_duration_ms": ...}
      Re-mints a FRESH signed download URL for one saved clip, on demand.
      The signed URLs embedded in /control/events and /control/history
      responses expire within minutes of being issued (alarm.com stamps
      each mint into the token; the clip itself never expires --
      ageLimitDays is -1 on every event observed live 2026-08-08), so a
      URL captured at poll time and opened later (e.g. from a phone
      notification) lands on alarm.com's "You don't have permission to
      view this clip" page. Every videoEvents fetch returns freshly-signed
      URLs -- this endpoint is a by-id fetch of exactly one event
      (get_video_events_by_ids, the same call list_history_events already
      uses for enrichment), so callers hit it at click time and redirect
      to the result.
      404 {"error": "unknown_event"} if TELUS returns no event for that id
      (deleted clip, or a bogus id -- TELUS omits it from the response
      rather than erroring).
      502 {"error": "telus_error"} if the TELUS-side fetch itself fails.

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/by-path/<path_name>/activate
    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/by-path/<path_name>/deactivate
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Not meant for Home Assistant, and not called by anything automatically as of
    2026-07-18 -- every registered camera's pipeline is started eagerly by main.py
    and left running indefinitely (see main.py's "always warm" comment), so there's
    no more on-demand start/stop driven by MediaMTX reader counts. These endpoints
    remain as a manual lever (e.g. force one camera's pipeline to restart without
    touching the others, or without a full container restart). <path_name> is the
    RTSP path name (config.rtsp_path_for(camera_id)), kept rather than switched to
    camera_id since MediaMTX's $MTX_PATH is still what identifies a path elsewhere
    in this codebase's conventions.

    Response: 200 {"status": "activating"} / {"status": "deactivating"}
      404 {"error": "unknown_path"} if no known camera maps to that path name.

    POST http://127.0.0.1:<CONTROL_PORT>/control/automations/<rule_id>/pause
    POST http://127.0.0.1:<CONTROL_PORT>/control/automations/<rule_id>/resume
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"status": "applied"}
      502 {"error": "telus_error"} if the TELUS-side call itself fails.
      Doesn't validate rule_id against a known-rules list first (unlike the
      snapshot endpoint's unknown_camera check) -- TELUS itself 404s/errors
      on a bad id and that's surfaced as telus_error, so a second local
      check would just duplicate what TELUS already enforces.
"""
import asyncio
import logging
import os
import secrets as secrets_mod
import time

import aiohttp
from aiohttp import web

from . import config as c
from . import talk as talk_mod
from . import talk_fullduplex
from .telus_auth import relogin_if_stale
from .telus_rest import (
    get_snapshot_url,
    get_video_events_by_ids,
    list_automation_rules,
    list_history_events,
    list_video_events,
    pause_rule,
    resume_rule,
)

log = logging.getLogger(__name__)


async def _fetch_with_relogin(telus_session, login_lock, coro_fn, *args):
    """Call coro_fn(telus_session, *args); on a 401/403 (expired session
    cookie), re-login once (serialized against every other login() caller
    via login_lock, same as main.py's get_video_source_with_relogin) and
    retry exactly once before giving up.
    """
    try:
        return await coro_fn(telus_session, *args)
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            log.warning("Control endpoint: %s got %s, re-logging in to TELUS",
                        coro_fn.__name__, err.status)
            await relogin_if_stale(telus_session, login_lock)
            return await coro_fn(telus_session, *args)
        raise


def _load_or_create_secret() -> str:
    try:
        with open(c.CONTROL_SECRET_PATH) as f:
            secret = f.read().strip()
            if secret:
                return secret
            log.warning("%s exists but is empty; regenerating", c.CONTROL_SECRET_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("Could not read %s; regenerating in place", c.CONTROL_SECRET_PATH)

    os.makedirs(c.SHARED_DIR, exist_ok=True)
    secret = secrets_mod.token_hex(32)
    tmp_path = f"{c.CONTROL_SECRET_PATH}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.replace(tmp_path, c.CONTROL_SECRET_PATH)
    log.info("Generated new control-endpoint shared secret at %s", c.CONTROL_SECRET_PATH)
    return secret


def _write_secret(secret: str) -> None:
    os.makedirs(c.SHARED_DIR, exist_ok=True)
    tmp_path = f"{c.CONTROL_SECRET_PATH}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.replace(tmp_path, c.CONTROL_SECRET_PATH)


async def _rotate_secret_periodically(current_secret: dict) -> None:
    """Regenerate the shared secret every CONTROL_SECRET_ROTATE_INTERVAL_S and
    atomically rewrite it to disk. No coordination or grace period with the HA
    side is needed: HA's bridge_client reads the secret fresh off disk on every
    single request rather than caching it, so the very next poll after a rotation
    picks up the new value. Not strictly zero-downtime -- an HA request that read
    the old secret microseconds before os.replace lands can 401 once -- but that
    is at most one request per 30-day rotation and every caller already retries."""
    while True:
        await asyncio.sleep(c.CONTROL_SECRET_ROTATE_INTERVAL_S)
        new_secret = secrets_mod.token_hex(32)
        try:
            _write_secret(new_secret)
        except OSError:
            # Keep serving the CURRENT secret and try again next interval. Updating
            # the in-memory value after a failed write would be far worse than not
            # rotating: HA reads the secret from this very file, so every subsequent
            # request would 401 until someone noticed. An unhandled raise here was
            # also silently fatal -- it killed the rotation task for the life of the
            # process with no retry and no log, since nothing awaits this task.
            log.exception("Control endpoint: failed to write rotated secret to %s; keeping "
                          "the current secret and retrying in %ss",
                          c.CONTROL_SECRET_PATH, c.CONTROL_SECRET_ROTATE_INTERVAL_S)
            continue
        current_secret["value"] = new_secret
        log.info("Control endpoint: rotated shared secret (scheduled %ss rotation)",
                  c.CONTROL_SECRET_ROTATE_INTERVAL_S)


def create_app(telus_session, login_lock, camera_registry: dict, camera_status: dict, lifecycle, talk_manager=None) -> web.Application:
    # Held in a dict (not a bare local) so _rotate_secret_periodically can
    # update it in place and every closure below sees the new value -- a bare
    # `secret = ...` reassignment elsewhere wouldn't be visible to these
    # already-created closures.
    current_secret = {"value": _load_or_create_secret()}

    # video_event_id -> (monotonic_ts, response_payload). See handle_clip.
    _clip_cache: dict[str, tuple[float, dict]] = {}

    def _camera_for_path(path_name: str) -> dict | None:
        return next(
            (cam for cam in camera_registry.values() if c.rtsp_path_for(cam["id"]) == path_name),
            None,
        )

    def _check_auth(request: web.Request) -> web.Response | None:
        """Returns a 401 response if the request's Bearer token doesn't match,
        else None. Shared by every authed endpoint below."""
        provided = request.headers.get("Authorization", "")
        try:
            matches = secrets_mod.compare_digest(provided, f"Bearer {current_secret['value']}")
        except TypeError:
            # compare_digest refuses non-ASCII str operands outright. A header with a
            # stray non-ASCII byte is simply a failed authentication, but unguarded it
            # escaped as a 500 -- contradicting the documented contract (401) and
            # turning a trivially-triggerable bad header into an error-level traceback.
            matches = False
        if not matches:
            log.warning("Control endpoint: rejected %s request with missing/bad secret", request.path)
            return web.json_response({"error": "bad_secret"}, status=401)
        return None

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_cameras(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        return web.json_response({"cameras": list(camera_registry.values())})

    async def handle_status(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        return web.json_response({"cameras": camera_status})

    async def handle_snapshot(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        camera_id = request.match_info["camera_id"]
        if camera_id not in camera_registry:
            return web.json_response({"error": "unknown_camera"}, status=404)

        try:
            snapshot_url = await _fetch_with_relogin(telus_session, login_lock, get_snapshot_url, camera_id)
            async with telus_session.session.get(snapshot_url) as resp:
                resp.raise_for_status()
                jpeg_bytes = await resp.read()
        except Exception:
            log.exception("Control endpoint: on-demand snapshot fetch failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.Response(body=jpeg_bytes, content_type="image/jpeg")

    async def handle_talk(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        # Off by default -- the wiring ships inert until someone opts in, so a
        # deployed-but-unconfigured talk path can never be triggered.
        if not c.TALKDOWN_ENABLED:
            return web.json_response({"error": "talkdown_disabled"}, status=403)
        if talk_manager is None:
            # Enabled but not constructed (shouldn't happen -- main.py builds it
            # whenever the flag is on); fail safe rather than 500.
            return web.json_response({"error": "talkdown_disabled"}, status=403)

        camera_id = request.match_info["camera_id"]
        if camera_id not in camera_registry:
            return web.json_response({"error": "unknown_camera"}, status=404)

        try:
            body = await request.json()
            audio_url = (body.get("audio_url") or "").strip()
            audio_path = (body.get("audio_path") or "").strip()
            codec = (body.get("codec") or "").strip().lower() or None
            conditioning = body.get("conditioning")
            audio_filter_req = body.get("audio_filter")
        except Exception:
            log.warning("Control endpoint: malformed talk request for %s", camera_id,
                        exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)

        if not audio_url and not audio_path:
            return web.json_response({"error": "malformed"}, status=400)
        if audio_url and not audio_url.startswith(("http://", "https://")):
            return web.json_response({"error": "malformed"}, status=400)
        if codec is not None and codec not in talk_fullduplex.CODECS:
            return web.json_response({"error": "bad_codec"}, status=400)
        # Audio conditioning is on by default (config.TALK_AUDIO_FILTER). A request
        # may disable it ("conditioning": false) or override the chain
        # ("audio_filter": "<ffmpeg -af string>"). audio_filter=None -> config default.
        if conditioning is not None and not isinstance(conditioning, bool):
            return web.json_response({"error": "malformed"}, status=400)
        if audio_filter_req is not None and not isinstance(audio_filter_req, str):
            return web.json_response({"error": "malformed"}, status=400)
        if isinstance(audio_filter_req, str):
            audio_filter = audio_filter_req            # explicit override (may be "")
        elif conditioning is False:
            audio_filter = ""                          # explicit disable
        else:
            audio_filter = None                        # config default

        try:
            result = await talk_manager.speak(
                camera_id,
                audio_url=audio_url or None,
                audio_path=audio_path or None,
                codec=codec,
                audio_filter=audio_filter,
            )
        except talk_mod.TalkInProgress:
            return web.json_response({"error": "talk_in_progress"}, status=409)
        except talk_mod.TalkMediaTooLarge:
            return web.json_response({"error": "media_too_large"}, status=413)
        except talk_mod.TalkMediaError:
            log.warning("Control endpoint: bad talk audio for %s", camera_id, exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        except talk_mod.NoUpstreamAudio:
            return web.json_response({"error": "no_upstream_audio"}, status=422)
        except asyncio.TimeoutError:
            log.warning("Control endpoint: talk timed out for %s", camera_id)
            return web.json_response({"error": "talk_timeout"}, status=504)
        except Exception:
            log.exception("Control endpoint: talk failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"status": "spoken", **result})

    async def handle_events(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20

        try:
            events = await _fetch_with_relogin(telus_session, login_lock, list_video_events, limit)
        except Exception:
            log.exception("Control endpoint: video events fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"events": events})

    async def handle_clip(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        video_event_id = request.match_info["video_event_id"]

        # Short TTL cache of re-minted responses, keyed by event id. The signed
        # URL alarm.com returns is valid for minutes, so serving the same one for
        # CLIP_URL_CACHE_TTL_S turns a burst of taps on one notification (or a
        # browser that re-requests on tab-restore) into a single TELUS round trip
        # instead of one per tap -- the endpoint is otherwise a 1:1 unauthenticated
        # proxy into TELUS's videoEvents API for any holder of a valid link.
        now = time.monotonic()
        cached = _clip_cache.get(video_event_id)
        if cached is not None and now - cached[0] < c.CLIP_URL_CACHE_TTL_S:
            return web.json_response(cached[1])

        try:
            videos = await _fetch_with_relogin(
                telus_session, login_lock, get_video_events_by_ids, [video_event_id])
        except Exception:
            log.exception("Control endpoint: fresh clip URL fetch failed for %s", video_event_id)
            return web.json_response({"error": "telus_error"}, status=502)

        video = videos.get(video_event_id)
        if video is None or not video.get("download_url"):
            return web.json_response({"error": "unknown_event"}, status=404)

        payload = {
            "download_url": video["download_url"],
            "thumbnail_url": video["thumbnail_url"],
            "clip_duration_ms": video["clip_duration_ms"],
        }
        # Evict expired entries opportunistically so the cache can't grow without
        # bound across many distinct clips over the process's month-plus lifetime.
        for vid in [k for k, (ts, _) in _clip_cache.items() if now - ts >= c.CLIP_URL_CACHE_TTL_S]:
            del _clip_cache[vid]
        _clip_cache[video_event_id] = (now, payload)
        return web.json_response(payload)

    async def handle_automations(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            rules = await _fetch_with_relogin(telus_session, login_lock, list_automation_rules)
        except Exception:
            log.exception("Control endpoint: automation rules fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"rules": rules})

    async def handle_history(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20

        try:
            events = await _fetch_with_relogin(telus_session, login_lock, list_history_events, limit)
        except Exception:
            log.exception("Control endpoint: activity history fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"events": events})

    async def _handle_rule_action(request: web.Request, action_fn) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        rule_id = request.match_info["rule_id"]
        try:
            await _fetch_with_relogin(telus_session, login_lock, action_fn, rule_id)
        except Exception:
            log.exception("Control endpoint: %s failed for rule %s", action_fn.__name__, rule_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"status": "applied"})

    async def handle_pause_rule(request: web.Request) -> web.Response:
        return await _handle_rule_action(request, pause_rule)

    async def handle_resume_rule(request: web.Request) -> web.Response:
        return await _handle_rule_action(request, resume_rule)

    async def handle_activate_by_path(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        camera = _camera_for_path(request.match_info["path_name"])
        if camera is None:
            return web.json_response({"error": "unknown_path"}, status=404)
        await lifecycle.activate(camera)
        return web.json_response({"status": "activating"})

    async def handle_deactivate_by_path(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        camera = _camera_for_path(request.match_info["path_name"])
        if camera is None:
            return web.json_response({"error": "unknown_path"}, status=404)
        await lifecycle.deactivate(camera["id"])
        return web.json_response({"status": "deactivating"})

    async def handle_credentials(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            body = await request.json()
            username = body["username"].strip()
            password = body["password"].strip()
            if not username or not password:
                raise ValueError("empty username or password")
        except Exception:
            log.warning("Control endpoint: malformed credential-push request", exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)

        c.TELUS_EMAIL, c.TELUS_PASSWORD = username, password
        try:
            async with login_lock:
                await telus_session.login()
        except Exception:
            log.exception("Control endpoint: re-login with pushed credentials failed "
                           "(credentials have still been updated in-process; existing "
                           "reconnect/retry loops will keep retrying them)")
            return web.json_response({"error": "internal"}, status=500)

        log.info("Control endpoint: credentials updated and re-login succeeded")
        return web.json_response({"status": "applied"})

    app = web.Application()
    app["current_secret"] = current_secret
    app.router.add_post("/control/credentials", handle_credentials)
    app.router.add_get("/control/health", handle_health)
    app.router.add_get("/control/cameras", handle_cameras)
    app.router.add_get("/control/status", handle_status)
    app.router.add_get("/control/cameras/{camera_id}/snapshot", handle_snapshot)
    app.router.add_post("/control/cameras/{camera_id}/talk", handle_talk)
    app.router.add_get("/control/events", handle_events)
    app.router.add_get("/control/clip/{video_event_id}", handle_clip)
    app.router.add_get("/control/automations", handle_automations)
    app.router.add_get("/control/history", handle_history)
    app.router.add_post("/control/automations/{rule_id}/pause", handle_pause_rule)
    app.router.add_post("/control/automations/{rule_id}/resume", handle_resume_rule)
    app.router.add_post("/control/cameras/by-path/{path_name}/activate", handle_activate_by_path)
    app.router.add_post("/control/cameras/by-path/{path_name}/deactivate", handle_deactivate_by_path)
    return app


async def start_control_server(
    telus_session, login_lock, camera_registry: dict, camera_status: dict, lifecycle,
    talk_manager=None,
) -> web.AppRunner:
    app = create_app(telus_session, login_lock, camera_registry, camera_status, lifecycle,
                     talk_manager)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, c.CONTROL_HOST, c.CONTROL_PORT)
    await site.start()
    log.info("Control endpoint listening on %s:%d", c.CONTROL_HOST, c.CONTROL_PORT)
    # Stashed in the app's own dict-style storage (confirmed live: web.AppRunner
    # itself rejects arbitrary attribute assignment -- AttributeError, no
    # __slots__ escape hatch) rather than a bare fire-and-forget ensure_future,
    # so main.py can cancel it in its shutdown path and asyncio has a live
    # reference to it for its full (month-plus) lifetime.
    app["secret_rotation_task"] = asyncio.ensure_future(
        _rotate_secret_periodically(app["current_secret"]))
    return runner
