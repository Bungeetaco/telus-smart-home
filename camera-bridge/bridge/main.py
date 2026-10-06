import asyncio
import json
import logging
import os

import aiohttp

from . import config as c
from . import control_server
from . import device_kick
from . import memory_trace
from . import restart_guard
from .cloud_hold import CloudHeld, call_with_relogin
from .telus_auth import RateLimited, TelusSession
from .telus_rest import list_cameras, get_video_source
from . import call_bridge
from .camera_session import run_camera_forever
from .lifecycle import CameraLifecycleManager
from .mobile_auth import MobileSession
from .talk import TalkManager

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

INITIAL_LOGIN_RETRIES = 3
INITIAL_LOGIN_BACKOFF_S = (2, 4, 8)


async def get_video_source_with_relogin(telus: TelusSession, camera_id: str, login_lock: asyncio.Lock) -> dict:
    """TELUS session cookies expire; if a video-source fetch 401/403s, re-run the
    SSO login once and retry before giving up (camera_session's own backoff loop
    handles further/persistent failures).

    The re-login goes through relogin_if_stale, which both serializes against every
    other caller of telus.login() (the control endpoint's credential push, the
    fallback-file watcher, the other cameras) and skips the login entirely if one of
    those already refreshed the session while this call was queued -- see that
    helper for why the de-duplication matters as much as the serialization.

    Shared with the control routes through cloud_hold.call_with_relogin, so a
    403 that survives the fresh login (the alarm.com edge refusing the account,
    not an expired cookie) holds EVERY website call -- this one included -- and
    the camera loop sleeps the hold through its existing RateLimited path
    instead of walking the 2/4/8/30 s reconnect schedule into the limiter.
    """
    return await call_with_relogin(telus, login_lock, get_video_source, camera_id,
                                   what=f"{camera_id}: video source fetch")


async def _login_with_retry(telus: TelusSession, login_lock: asyncio.Lock) -> None:
    """Initial login at startup, with retry/backoff. Docker's restart policy is
    `unless-stopped`, so if we just let a transient network blip raise straight
    out of main(), the container hot-loops restarts against TELUS's auth
    endpoint. A few bounded retries absorb any brief outage before we give up
    and let the process (and container restart backoff) take over.

    A RateLimited failure (Cloudflare error 1015 on auth-gateway.telus.com,
    confirmed live 2026-07-21 -- see telus_auth.RateLimited) is handled
    separately from any other failure: sleeps for the server's own
    Retry-After instead of the fixed short schedule below. Discovered the
    hard way the same night this was added -- the previous fixed 2s/4s
    backoff, compounded by several back-to-back container restarts, kept
    re-triggering Cloudflare's rate limit instead of ever clearing it, turning
    one incident into a multi-hour outage. Respecting the server's own stated
    wait time is what actually recovers instead of compounding it further --
    all of it, which is why the sleep is no longer truncated by a local cap
    (see config.rate_limit_sleep_s).

    Giving up at the end still lets the process die into a container restart,
    but only after restart_guard has had its say: that restart re-runs this
    very login, so an upstream that stays down would otherwise get one fresh
    login attempt per restart, forever.
    """
    last_exc: Exception | None = None
    for attempt in range(1, INITIAL_LOGIN_RETRIES + 1):
        try:
            async with login_lock:
                await telus.login()
            return
        except RateLimited as exc:
            last_exc = exc
            delay = c.rate_limit_sleep_s(exc.retry_after_s)  # full server-stated wait + small margin
            if attempt < INITIAL_LOGIN_RETRIES:
                log.warning("Initial TELUS login rate-limited by Cloudflare (attempt %d/%d): "
                            "retrying in %.0fs (server-specified Retry-After=%.0fs, not the usual "
                            "fixed backoff -- see RateLimited)", attempt, INITIAL_LOGIN_RETRIES,
                            delay, exc.retry_after_s)
                await asyncio.sleep(delay)
            else:
                log.error("Initial TELUS login still rate-limited by Cloudflare after %d attempts, "
                          "giving up: %s", INITIAL_LOGIN_RETRIES, exc)
        except Exception as exc:
            last_exc = exc
            if attempt < INITIAL_LOGIN_RETRIES:
                delay = INITIAL_LOGIN_BACKOFF_S[min(attempt - 1, len(INITIAL_LOGIN_BACKOFF_S) - 1)]
                log.warning("Initial TELUS login failed (attempt %d/%d): %s -- retrying in %ss",
                            attempt, INITIAL_LOGIN_RETRIES, exc, delay)
                await asyncio.sleep(delay)
            else:
                log.error("Initial TELUS login failed after %d attempts, giving up: %s",
                          INITIAL_LOGIN_RETRIES, exc)

    # This raise kills the process and `restart: unless-stopped` immediately
    # starts another one that runs this same login again -- the identical
    # restart-loop-into-the-upstream shape camera_session.py's escape hatch
    # has, so it shares the same cross-process guard. A one-off startup failure
    # (booting during a network blip) is inside the guard's free allowance and
    # still dies straight away; only a repeating pattern gets held back, and
    # then Docker cannot recycle us faster than that hold.
    hold_s, exits_in_window = restart_guard.record_exit()
    if hold_s:
        log.critical("Startup login has now failed terminally %d times within the last %ds -- "
                     "holding the process for %.0fs before exiting, so the container's restart "
                     "policy can't keep re-attempting logins against an upstream that is "
                     "plainly still down", exits_in_window, c.RESTART_GUARD_WINDOW_S, hold_s)
        await asyncio.sleep(hold_s)
    raise last_exc


def _load_fallback_credentials_if_present() -> str | None:
    """Adopt credentials.json (written by HA's config flow on every credential
    update) if present, overriding the .env values. Returns its 'updated_at'
    marker (or None if no file / unreadable) so the watcher below knows what
    it's already seen and doesn't immediately re-apply the same thing."""
    try:
        with open(c.CREDENTIALS_FALLBACK_PATH) as f:
            data = json.load(f)
        # Parse BOTH fields before assigning EITHER. Assigning inline meant a file
        # whose "password" was missing or a non-string left TELUS_EMAIL already
        # overwritten while TELUS_PASSWORD kept the .env value -- a mismatched pair
        # that can only ever fail to log in. AttributeError/TypeError are caught
        # alongside KeyError because valid JSON with the wrong *shape* (null, a
        # number, a nested object) is exactly as likely as a truncated file, and
        # previously escaped this handler entirely to kill the process at import
        # time -- a crash loop, since the bad file is still there on restart.
        # ValueError rather than the narrower json.JSONDecodeError for the same
        # reason: a torn write on the shared mount (invalid UTF-8 mid-file)
        # raises UnicodeDecodeError out of json.load, which is a ValueError but
        # NOT a JSONDecodeError, and took the same crash-loop route. Same
        # pattern as restart_guard._read_recent.
        username = data["username"].strip()
        password = data["password"].strip()
    except FileNotFoundError:
        return None
    except (ValueError, KeyError, AttributeError, TypeError, OSError):
        log.warning("Fallback credentials file present but unreadable/corrupt; "
                     "using .env credentials", exc_info=True)
        return None

    c.TELUS_EMAIL, c.TELUS_PASSWORD = username, password
    log.info("Loaded credentials from fallback file (updated_at=%s), overriding .env",
              data.get("updated_at"))
    return data.get("updated_at")


def _load_call_settings_if_present() -> str | None:
    """Adopt call_settings.json (written by HA's options flow) if present,
    overriding the .env bootstrap -- the call-feature twin of the credentials
    fallback above, with the same shape-guard reasoning. Returns the
    'updated_at' marker for the watcher."""
    try:
        with open(c.CALL_SETTINGS_PATH) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except FileNotFoundError:
        return None
    except (ValueError, OSError):
        log.warning("call_settings.json present but unreadable/corrupt; using "
                    ".env call settings", exc_info=True)
        return None
    applied = call_bridge.apply_settings(data)
    log.info("Loaded call settings from fallback file (updated_at=%s, fields=%s)",
             data.get("updated_at"), applied)
    return data.get("updated_at")


async def watch_call_settings(last_seen) -> None:
    """The call-settings twin of watch_credentials_fallback: catches a push
    the bridge was down for. No re-login side effect -- applying is just
    config assignment -- so this is the whole loop."""
    while True:
        await asyncio.sleep(c.CREDENTIALS_FALLBACK_POLL_S)
        try:
            with open(c.CALL_SETTINGS_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            log.warning("call_settings.json unreadable/corrupt this cycle, "
                        "skipping", exc_info=True)
            continue
        if not isinstance(data, dict):
            log.warning("call_settings.json is not a JSON object this cycle, skipping")
            continue
        updated_at = data.get("updated_at")
        if updated_at in (None, last_seen):
            continue
        last_seen = updated_at
        applied = call_bridge.apply_settings(data)
        log.info("Detected updated call settings (updated_at=%s, fields=%s)",
                 updated_at, applied)


def _camera_registry_entry(cam: dict) -> dict:
    """Build the dict shape exposed via the control server's /control/cameras
    endpoint -- everything the HA integration needs to create a camera entity
    without itself talking to TELUS's API."""
    return {
        "id": cam["id"],
        "name": cam["description"],
        "model": cam["model"],
        "firmware": cam["firmware"],
        "mac_address": cam["mac_address"],
        "rtsp_url": c.rtsp_publish_url(cam["id"]),
    }


async def discover_and_launch_cameras(
    telus: TelusSession,
    camera_registry: dict[str, dict],
    lifecycle: CameraLifecycleManager,
    login_lock: asyncio.Lock,
) -> None:
    """Poll TELUS for the camera list forever, registering any camera not already
    known and starting its pipeline immediately. A camera newly added to the account
    is picked up (and streaming) within CAMERA_DISCOVERY_INTERVAL_S, no restart needed.
    Deliberately never removes a camera that stops appearing in a response -- TELUS's
    API being briefly flaky shouldn't break an otherwise-working camera; one actually
    removed from the account just keeps its pipeline retrying harmlessly forever (same
    as any other camera that goes offline -- see camera_session.py's own backoff loop).

    Cameras are started here (and once in main()'s initial pass) and never
    deliberately stopped again -- see main()'s "always warm" comment for why this
    replaced the old MediaMTX-runOnDemand-driven start/stop.
    """
    while True:
        try:
            cameras = await call_with_relogin(telus, login_lock, list_cameras,
                                              what="camera discovery")
        except CloudHeld as exc:
            # The edge is refusing the account and the hold already said so once
            # at WARNING; a traceback per cycle for the duration would be the
            # log storm the hold exists to end. Sleep the usual interval: the
            # registry is complete for every camera that matters, and a camera
            # added during a hold is picked up on the first cycle after it.
            log.info("Camera discovery skipped (%s), next attempt in %ss",
                     exc, c.CAMERA_DISCOVERY_INTERVAL_S)
            await asyncio.sleep(c.CAMERA_DISCOVERY_INTERVAL_S)
            continue
        except RateLimited as exc:
            log.warning("Camera discovery rate limited by the edge (Retry-After=%.0fs), "
                        "will retry in %ss", exc.retry_after_s, c.CAMERA_DISCOVERY_INTERVAL_S)
            await asyncio.sleep(c.CAMERA_DISCOVERY_INTERVAL_S)
            continue
        except Exception:
            log.exception("Camera discovery poll failed, will retry in %ss",
                          c.CAMERA_DISCOVERY_INTERVAL_S)
            await asyncio.sleep(c.CAMERA_DISCOVERY_INTERVAL_S)
            continue

        for cam in cameras:
            cam_id = cam["id"]
            if cam_id in camera_registry:
                continue
            log.info("Discovered new camera: %s (%s)", cam_id, cam["description"])
            entry = _camera_registry_entry(cam)
            camera_registry[cam_id] = entry
            await lifecycle.activate(entry)

        await asyncio.sleep(c.CAMERA_DISCOVERY_INTERVAL_S)


async def watch_credentials_fallback(telus: TelusSession, login_lock: asyncio.Lock, last_seen) -> None:
    """Safety net for when the control endpoint's live push (control_server.py)
    is missed -- bridge container was down, network hiccup between the HA and
    bridge containers, etc. Polls the same fallback file that HA's config flow
    always writes on a successful credential update, and re-logs-in if it sees
    a newer 'updated_at' than what we last applied."""
    while True:
        await asyncio.sleep(c.CREDENTIALS_FALLBACK_POLL_S)
        try:
            with open(c.CREDENTIALS_FALLBACK_PATH) as f:
                data = json.load(f)
        except FileNotFoundError:
            continue
        except (ValueError, OSError):
            # ValueError, not just json.JSONDecodeError: a torn write on the
            # shared mount raises UnicodeDecodeError (a ValueError that is NOT
            # a JSONDecodeError), which used to kill this task -- and, via the
            # bare asyncio.gather in main(), the whole process.
            log.warning("credentials.json unreadable/corrupt this cycle, skipping", exc_info=True)
            continue

        if not isinstance(data, dict):
            # Valid JSON of the wrong shape ([] , a string, a number) would
            # otherwise raise AttributeError on the .get() below, before the
            # shape-guarded username/password extraction ever runs.
            log.warning("credentials.json is not a JSON object this cycle, skipping")
            continue

        updated_at = data.get("updated_at")
        if updated_at in (None, last_seen):
            continue
        try:
            username, password = data["username"].strip(), data["password"].strip()
        except (KeyError, AttributeError, TypeError):
            log.warning("credentials.json missing/malformed username/password this cycle, skipping")
            continue

        if (username, password) == (c.TELUS_EMAIL, c.TELUS_PASSWORD):
            # Already in effect -- almost always because HA's live push to
            # /control/credentials beat this poll to it (the config flow writes this
            # file first, then pushes). Record the marker so we stop re-reading it,
            # but skip the login: it would clear a perfectly good cookie jar and add
            # a pointless second hit to the Cloudflare-fronted auth gateway within
            # 30s of the push that already succeeded.
            last_seen = updated_at
            log.debug("Credentials file updated_at moved but credentials are already "
                       "applied (live push); skipping redundant re-login")
            continue

        last_seen = updated_at
        log.info("Detected updated credentials file (updated_at=%s), re-logging in", updated_at)
        c.TELUS_EMAIL, c.TELUS_PASSWORD = username, password
        async with login_lock:
            try:
                await telus.login()
            except Exception:
                log.exception("Re-login with fallback-file credentials failed")


async def main():
    last_seen_creds_ts = _load_fallback_credentials_if_present()
    login_lock = asyncio.Lock()
    camera_registry: dict[str, dict] = {}
    # Live per-camera diagnostics (state, consecutive_failures, last_frame_at,
    # fps, hardware_encode, last_error), kept current by run_camera_forever
    # itself and exposed read-only via the control server's /control/status --
    # see control_server.py's module docstring for the response shape.
    camera_status: dict[str, dict] = {}

    async with TelusSession() as telus:
        await _login_with_retry(telus, login_lock)

        # Owns starting each camera's pipeline (Janus WebRTC session + decode/encode)
        # -- see lifecycle.py. As of 2026-07-18, every registered camera is activated
        # eagerly (below, and in discover_and_launch_cameras for later discoveries)
        # and kept running indefinitely -- there's no more on-demand start driven by a
        # MediaMTX reader connecting. This trades the old design's near-zero idle CPU
        # for always-warm streams: HA (or a fresh MediaMTX/HA restart) never has to
        # wait out a cold Janus/TELUS negotiation (worst case ~50s per camera) before
        # video appears, since the pipeline is typically already flowing by the time
        # anything asks for it. lifecycle.deactivate() still exists and stays safe to
        # call (e.g. via the control server's by-path endpoint for manual ops), it's
        # just no longer invoked automatically by anything.
        lifecycle = CameraLifecycleManager(
            lambda camera: run_camera_forever(
                telus, camera,
                lambda t, cid: get_video_source_with_relogin(t, cid, login_lock),
                camera_status,
                # Device-side escalation for a wedged publish leg (backlog
                # E103), inert unless DEVICE_KICK_ENABLED. Through the cloud
                # hold like every other website call, so a held edge refuses
                # the kick instead of the kick walking into the block.
                kick_device=lambda cid: call_with_relogin(
                    telus, login_lock, device_kick.cycle_live_resolution, cid,
                    what=f"{cid}: device kick")))

        # Talkdown (two-way audio) manager, handed to the control server the same
        # way `lifecycle` is. Constructed only when the feature is enabled so it
        # ships inert. It reuses the SAME login_lock as telus_auth (threaded
        # through, not re-created) so a website re-login and a mobile MWS re-mint
        # can never interleave, and drives a MobileSession minted off this same
        # website session. See talk.py / mobile_auth.py.
        talk_manager = None
        if c.TALKDOWN_ENABLED:
            talk_manager = TalkManager(telus, MobileSession(telus), login_lock)
            log.info("Talkdown enabled: POST /control/cameras/{id}/talk is armed")

        control_runner = await control_server.start_control_server(
            telus, login_lock, camera_registry, camera_status, lifecycle, talk_manager
        )
        # None unless config.MEMORY_TRACE is on, in which case it samples on its
        # OWN timer (see memory_trace.py). Bound BEFORE the try so the finally
        # can always cancel it, and started outside the camera loops
        # deliberately: the question it answers is about the whole container,
        # including the processes this one does not own, so it has to keep
        # sampling while a camera is reconnecting.
        memory_task = memory_trace.start()
        try:
            # First discovery pass runs synchronously so /control/cameras is
            # populated before we consider startup done, rather than racing the HA
            # integration's first poll against an empty registry. Every subsequent
            # pass happens inside discover_and_launch_cameras's own loop.
            #
            # A failure here must NOT propagate: unhandled, a transient TELUS API
            # blip in the seconds right after a successful login killed the whole
            # process, and Docker's `restart: unless-stopped` then brought it back
            # into a *fresh SSO login* -- turning one flaky response into a restart
            # loop that hammers the Cloudflare-fronted auth gateway, which is the
            # documented route to a rate-limit lockout. The discovery loop started
            # below already retries this exact call every
            # CAMERA_DISCOVERY_INTERVAL_S with its own error handling, so degrade to
            # an empty registry and let it recover in place.
            try:
                initial_cameras = await list_cameras(telus)
            except Exception:
                log.exception("Initial camera discovery failed; starting with an empty registry "
                              "and retrying on the discovery loop's own %ss cycle",
                              c.CAMERA_DISCOVERY_INTERVAL_S)
                initial_cameras = []
            else:
                log.info("Discovered %d cameras: %s", len(initial_cameras),
                          [(cam["id"], cam["description"]) for cam in initial_cameras])
                if not initial_cameras:
                    log.warning("No cameras returned by TELUS (filterByLiveViewDisplayable=true) "
                                "on startup -- will keep polling every %ss in case one is added.",
                                c.CAMERA_DISCOVERY_INTERVAL_S)
            for cam in initial_cameras:
                entry = _camera_registry_entry(cam)
                camera_registry[cam["id"]] = entry
                # Fire-and-forget in effect (activate() just kicks off a task and
                # returns), so this loop doesn't serialize on Janus/TELUS negotiation
                # for one camera before starting the next -- all cameras' cold starts
                # happen concurrently, not one after another.
                await lifecycle.activate(entry)

            discovery_task = asyncio.ensure_future(
                discover_and_launch_cameras(telus, camera_registry, lifecycle, login_lock))
            call_settings_task = asyncio.ensure_future(
                watch_call_settings(_load_call_settings_if_present()))
            fallback_task = asyncio.ensure_future(
                watch_credentials_fallback(telus, login_lock, last_seen_creds_ts))

            # Both tasks loop forever, so this blocks forever as intended. Per-camera
            # pipeline tasks started later by lifecycle.activate() are inherently
            # transient (started/stopped on demand) so, unlike before, there's no
            # fixed set of them to include here -- CameraLifecycleManager logs any
            # unexpected exception from one itself (see its deactivate()).
            await asyncio.gather(discovery_task, fallback_task,
                                 call_settings_task)
        finally:
            if memory_task is not None:
                memory_task.cancel()
            control_runner.app[control_server.APP_SECRET_ROTATION_TASK].cancel()
            await control_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
