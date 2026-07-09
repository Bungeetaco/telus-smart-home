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

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/by-path/<path_name>/activate
    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/by-path/<path_name>/deactivate
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Not meant for Home Assistant -- these exist purely for MediaMTX's own
    runOnDemand/runOnUnDemand hooks (see mediamtx.yml, activate_camera.sh,
    deactivate_camera.sh) to start/stop a camera's streaming pipeline on demand,
    driven by MediaMTX's own reader-count tracking. <path_name> is the RTSP path
    name MediaMTX passes via $MTX_PATH (config.rtsp_path_for(camera_id)), not the
    camera_id itself -- MediaMTX only knows the path, not our camera ids.

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
import logging
import os
import secrets as secrets_mod

import aiohttp
from aiohttp import web

from . import config as c
from .telus_rest import (
    get_snapshot_url,
    list_automation_rules,
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
            async with login_lock:
                await telus_session.login()
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


def create_app(telus_session, login_lock, camera_registry: dict, lifecycle) -> web.Application:
    secret = _load_or_create_secret()

    def _camera_for_path(path_name: str) -> dict | None:
        return next(
            (cam for cam in camera_registry.values() if c.rtsp_path_for(cam["id"]) == path_name),
            None,
        )

    def _check_auth(request: web.Request) -> web.Response | None:
        """Returns a 401 response if the request's Bearer token doesn't match,
        else None. Shared by every authed endpoint below."""
        provided = request.headers.get("Authorization", "")
        if not secrets_mod.compare_digest(provided, f"Bearer {secret}"):
            log.warning("Control endpoint: rejected %s request with missing/bad secret", request.path)
            return web.json_response({"error": "bad_secret"}, status=401)
        return None

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_cameras(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        return web.json_response({"cameras": list(camera_registry.values())})

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

    async def handle_automations(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            rules = await _fetch_with_relogin(telus_session, login_lock, list_automation_rules)
        except Exception:
            log.exception("Control endpoint: automation rules fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"rules": rules})

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
    app.router.add_post("/control/credentials", handle_credentials)
    app.router.add_get("/control/health", handle_health)
    app.router.add_get("/control/cameras", handle_cameras)
    app.router.add_get("/control/cameras/{camera_id}/snapshot", handle_snapshot)
    app.router.add_get("/control/events", handle_events)
    app.router.add_get("/control/automations", handle_automations)
    app.router.add_post("/control/automations/{rule_id}/pause", handle_pause_rule)
    app.router.add_post("/control/automations/{rule_id}/resume", handle_resume_rule)
    app.router.add_post("/control/cameras/by-path/{path_name}/activate", handle_activate_by_path)
    app.router.add_post("/control/cameras/by-path/{path_name}/deactivate", handle_deactivate_by_path)
    return app


async def start_control_server(telus_session, login_lock, camera_registry: dict, lifecycle) -> web.AppRunner:
    runner = web.AppRunner(create_app(telus_session, login_lock, camera_registry, lifecycle))
    await runner.setup()
    site = web.TCPSite(runner, c.CONTROL_HOST, c.CONTROL_PORT)
    await site.start()
    log.info("Control endpoint listening on %s:%d", c.CONTROL_HOST, c.CONTROL_PORT)
    return runner
