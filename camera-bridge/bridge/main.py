import asyncio
import json
import logging
import os

import aiohttp

from . import config as c
from . import control_server
from .telus_auth import TelusSession
from .telus_rest import list_cameras, get_video_source
from .camera_session import run_camera_forever

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                     format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)

INITIAL_LOGIN_RETRIES = 3
INITIAL_LOGIN_BACKOFF_S = (2, 4, 8)


async def get_video_source_with_relogin(telus: TelusSession, camera_id: str, login_lock: asyncio.Lock) -> dict:
    """TELUS session cookies expire; if a video-source fetch 401/403s, re-run the
    SSO login once and retry before giving up (camera_session's own backoff loop
    handles further/persistent failures).

    The re-login is serialized against every other caller of telus.login()
    (the control endpoint's credential push, and the fallback-file watcher)
    via login_lock, since TelusSession.login() clears the whole cookie jar
    with no locking of its own -- two concurrent logins would race and could
    leave the session with a mix of cookies from two different login attempts.
    """
    try:
        return await get_video_source(telus, camera_id)
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            log.warning("%s: video source fetch got %s, re-logging in to TELUS", camera_id, err.status)
            async with login_lock:
                await telus.login()
            return await get_video_source(telus, camera_id)
        raise


async def _login_with_retry(telus: TelusSession, login_lock: asyncio.Lock) -> None:
    """Initial login at startup, with retry/backoff. Docker's restart policy is
    `unless-stopped`, so if we just let a transient network blip raise straight
    out of main(), the container hot-loops restarts against TELUS's auth
    endpoint. A few bounded retries absorb any brief outage before we give up
    and let the process (and container restart backoff) take over."""
    last_exc: Exception | None = None
    for attempt in range(1, INITIAL_LOGIN_RETRIES + 1):
        try:
            async with login_lock:
                await telus.login()
            return
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
    raise last_exc


def _load_fallback_credentials_if_present() -> str | None:
    """Adopt credentials.json (written by HA's config flow on every credential
    update) if present, overriding the .env values. Returns its 'updated_at'
    marker (or None if no file / unreadable) so the watcher below knows what
    it's already seen and doesn't immediately re-apply the same thing."""
    try:
        with open(c.CREDENTIALS_FALLBACK_PATH) as f:
            data = json.load(f)
        c.TELUS_EMAIL = data["username"].strip()
        c.TELUS_PASSWORD = data["password"].strip()
        log.info("Loaded credentials from fallback file (updated_at=%s), overriding .env",
                  data.get("updated_at"))
        return data.get("updated_at")
    except FileNotFoundError:
        return None
    except (json.JSONDecodeError, KeyError, OSError):
        log.warning("Fallback credentials file present but unreadable/corrupt; "
                     "using .env credentials", exc_info=True)
        return None


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
        except (json.JSONDecodeError, OSError):
            log.warning("credentials.json unreadable/corrupt this cycle, skipping", exc_info=True)
            continue

        updated_at = data.get("updated_at")
        if updated_at in (None, last_seen):
            continue
        try:
            username, password = data["username"].strip(), data["password"].strip()
        except (KeyError, AttributeError):
            log.warning("credentials.json missing username/password this cycle, skipping")
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

    async with TelusSession() as telus:
        await _login_with_retry(telus, login_lock)

        control_runner = await control_server.start_control_server(telus, login_lock)
        try:
            cameras = await list_cameras(telus)
            log.info("Discovered %d cameras: %s", len(cameras),
                      [(cam["id"], cam["description"]) for cam in cameras])

            if not cameras:
                log.error("No cameras returned by TELUS (filterByLiveViewDisplayable=true) -- nothing to do.")
                return

            tasks = [
                asyncio.ensure_future(run_camera_forever(
                    telus, cam,
                    lambda t, cid: get_video_source_with_relogin(t, cid, login_lock)))
                for cam in cameras
            ]
            tasks.append(asyncio.ensure_future(
                watch_credentials_fallback(telus, login_lock, last_seen_creds_ts)))
            await asyncio.gather(*tasks)
        finally:
            await control_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
