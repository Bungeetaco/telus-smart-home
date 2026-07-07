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
"""
import logging
import os
import secrets as secrets_mod

from aiohttp import web

from . import config as c

log = logging.getLogger(__name__)


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


def create_app(telus_session, login_lock, camera_registry: dict) -> web.Application:
    secret = _load_or_create_secret()

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_cameras(request: web.Request) -> web.Response:
        provided = request.headers.get("Authorization", "")
        if not secrets_mod.compare_digest(provided, f"Bearer {secret}"):
            log.warning("Control endpoint: rejected /control/cameras request with missing/bad secret")
            return web.json_response({"error": "bad_secret"}, status=401)

        return web.json_response({"cameras": list(camera_registry.values())})

    async def handle_credentials(request: web.Request) -> web.Response:
        provided = request.headers.get("Authorization", "")
        if not secrets_mod.compare_digest(provided, f"Bearer {secret}"):
            log.warning("Control endpoint: rejected request with missing/bad secret")
            return web.json_response({"error": "bad_secret"}, status=401)

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
    return app


async def start_control_server(telus_session, login_lock, camera_registry: dict) -> web.AppRunner:
    runner = web.AppRunner(create_app(telus_session, login_lock, camera_registry))
    await runner.setup()
    site = web.TCPSite(runner, c.CONTROL_HOST, c.CONTROL_PORT)
    await site.start()
    log.info("Control endpoint listening on %s:%d", c.CONTROL_HOST, c.CONTROL_PORT)
    return runner
