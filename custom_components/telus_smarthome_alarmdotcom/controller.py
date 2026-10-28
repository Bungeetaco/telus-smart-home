"""Controller interfaces with the Alarm.com API via pyalarmdotcomajax."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta
from types import MappingProxyType
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import CALLBACK_TYPE, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from pyalarmdotcomajax import AlarmController as libAlarmController
from pyalarmdotcomajax import WebSocketState
from pyalarmdotcomajax.exceptions import (
    AlarmdotcomException,
    AuthenticationFailed,
    NotAuthorized,
    UnexpectedResponse,
)

from . import telus_ws_push_patch
from .const import (
    CONF_2FA_COOKIE,
    CONF_DEFAULT_UPDATE_INTERVAL_SECONDS,
    CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT,
    CONF_UPDATE_INTERVAL,
    CONF_WEBSOCKET_RECONNECT_TIMEOUT,
    KEEP_ALIVE_INTERVAL_SECONDS,
    WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
    WEBSOCKET_RECONNECT_BACKOFF_MAX_SECONDS,
    WEBSOCKET_SESSION_TOTAL_TIMEOUT_SECONDS,
)
from .ws_backoff import reconnect_delay

LOGGER = logging.getLogger(__name__)

# How long the websocket may sit in a pre-RUNNING state before we give up on this
# attempt and restart it. Generous relative to a normal connect (sub-second) --
# this is a stuck-forever backstop, not a latency budget.
WEBSOCKET_START_TIMEOUT_SECONDS = 60


class AlarmIntegrationController:
    """Config-entry initiated Alarm Hub."""

    def __init__(self, hass: HomeAssistant, config_entry: ConfigEntry) -> None:
        """Initialize the system."""

        self.hass: HomeAssistant = hass
        self.config_entry: ConfigEntry = config_entry

        self.update_coordinator: DataUpdateCoordinator
        self.api: libAlarmController
        # The aiohttp session this controller created for the library (see
        # initialize_lite). Kept so stop() can release it without closing it.
        self._websession: aiohttp.ClientSession | None = None
        # Partition ids already reported as having no registry device:
        # base_device warns once per partition per controller, not once per
        # entity behind it (a reload builds a new controller and reports again).
        self.unlinked_partitions_warned: set[str] = set()

        self.options: MappingProxyType[str, Any]

        self._stop_keep_alive: CALLBACK_TYPE

        self._ws_state: WebSocketState = WebSocketState.STOPPED
        self._ws_seen_running = False
        self._ws_close_event = asyncio.Event()
        # Reconnect back-off (backlog E106): whether the attempt in flight ever
        # reached RUNNING, and how many attempts in a row have not.
        self._ws_attempt_reached_running = False
        self._ws_connect_failures = 0

        # Diagnostic auth/session health, surfaced via a diagnostic sensor entity
        # (see sensor.py) so the user can see auth is failing from within Home
        # Assistant, rather than only via the reauth notification or raw logs.
        self.auth_health: str = "ok"
        self.auth_health_detail: str | None = None
        self.last_auth_success: datetime | None = None

        LOGGER.debug("%s: Registering update listener.", __name__)

    async def initialize(self) -> None:
        """Initialize connection to Alarm.com."""

        #
        # Create pyalarmdotcomajax controller
        #

        try:
            await self.initialize_lite(
                username=self.config_entry.data[CONF_USERNAME],
                password=self.config_entry.data[CONF_PASSWORD],
                twofactorcookie=self.config_entry.data.get(CONF_2FA_COOKIE),
            )
        except UnexpectedResponse as err:
            # This IS the setup-only path (see initialize_lite's comment on
            # this exact exception) -- translate here, where it belongs, so
            # HA retries with backoff instead of a hard SETUP_ERROR.
            raise ConfigEntryNotReady from err

        #
        # Initialize DataUpdateCoordinator and pull device data
        #

        self.options = self.config_entry.options
        self.config_entry.async_on_unload(self.config_entry.add_update_listener(_async_update_listener))

        update_interval = self.config_entry.options.get(CONF_UPDATE_INTERVAL, CONF_DEFAULT_UPDATE_INTERVAL_SECONDS)

        self.update_coordinator = DataUpdateCoordinator(
            self.hass,
            LOGGER,
            name=self.config_entry.title,
            update_method=self.async_update,
            update_interval=timedelta(seconds=update_interval),
        )

        # Websocket pushes update the library's device objects; entities read
        # those live, so this is what makes a push visible in HA immediately
        # instead of at the next REST poll. Costs no extra API calls.
        telus_ws_push_patch.register_coordinator(self.update_coordinator)

        await self.update_coordinator.async_config_entry_first_refresh()

        #
        # Start keep-alive task
        #

        try:
            # Home Assistant vers >=2023.4
            self._stop_keep_alive = async_track_time_interval(
                hass=self.hass,
                action=self._keep_alive,
                interval=timedelta(seconds=KEEP_ALIVE_INTERVAL_SECONDS),
                name="Alarm.com Session Keep Alive",
            )
        except TypeError:
            # Home Assistant vers <2023.4
            self._stop_keep_alive = async_track_time_interval(
                hass=self.hass,
                action=self._keep_alive,
                interval=timedelta(seconds=KEEP_ALIVE_INTERVAL_SECONDS),
            )

    async def stop(self) -> None:
        """Stop the controller."""

        self.stop_keep_alive()
        self.api.stop_websocket()

        self.release_websession()

    def release_websession(self) -> None:
        """Release the session initialize_lite created, without closing it.

        Home Assistant builds every session it hands out on ONE shared connector
        and wraps `close()` to warn when an integration calls it (backlog E108):
        closing the session would close that connector for every integration
        in the process. `detach()` is the documented release for a session made
        with `auto_cleanup=False`: it drops the connector reference and marks
        the session closed, so a reload leaves no session behind and the
        connector keeps serving everyone else. Safe to call more than once --
        the HA-stop listener and async_unload_entry can both reach it.
        """

        if self._websession is not None:
            self._websession.detach()
            self._websession = None

    def stop_keep_alive(self) -> None:
        """Stop keep-alive task."""

        LOGGER.info("Stopping session keep-alive task.")

        with contextlib.suppress(TypeError):
            self._stop_keep_alive()

    async def initialize_lite(self, username: str, password: str, twofactorcookie: str | None) -> None:
        """Initialize connection to Alarm.com for config entry flow."""

        # Own session (the SSO handshake needs an isolated cookie jar), with
        # auto_cleanup off because this controller releases it itself in
        # release_websession(); with auto_cleanup on, HA would also hold it
        # until shutdown, one per reload.
        #
        # Bound the TCP+TLS connect phase (backlog E106). The library opens the
        # push socket with ws_connect(..., timeout=30), but that float only sets
        # aiohttp's ws CLOSE timeout, not the connect; the connect uses this
        # session's ClientTimeout, whose default sock_connect is 30 s, so a
        # stalled connect to webskt.alarm.com burned a full 30 s before failing.
        # sock_connect bounds only connection establishment -- a running socket
        # (heartbeat) and slow REST responses (total) are untouched. See const.
        self._websession = async_create_clientsession(
            self.hass,
            auto_cleanup=False,
            timeout=aiohttp.ClientTimeout(
                total=WEBSOCKET_SESSION_TOTAL_TIMEOUT_SECONDS,
                sock_connect=WEBSOCKET_CONNECT_TIMEOUT_SECONDS,
            ),
        )
        self.api = libAlarmController(
            username=username,
            password=password,
            twofactorcookie=twofactorcookie,
            websession=self._websession,
        )

        try:
            await self.api.async_login()
        except (TimeoutError, aiohttp.ClientError, asyncio.exceptions.CancelledError) as err:
            raise ConfigEntryNotReady from err
        except UnexpectedResponse:
            # Deliberately NOT translated here. initialize_lite() has two kinds
            # of caller with different needs: the real setup path (initialize()
            # -> __init__.py:async_setup_entry, no DataUpdateCoordinator yet)
            # needs this turned into ConfigEntryNotReady for HA's automatic
            # retry-with-backoff; but config_flow.py also calls this directly
            # during interactive login/reconfigure, where it has its own
            # `except libUnexpectedResponse` handler that shows a specific,
            # already-written "telus_sso_error" translation string (this is a
            # TELUS-SSO-specific integration -- a raw UnexpectedResponse here
            # is exactly the "TELUS changed something server-side" case that
            # string exists for). Translating to any other exception type at
            # this shared call site made that handler unreachable dead code.
            # Re-raise as-is; initialize() below translates for its own
            # (setup-only) callers instead.
            raise
        except (AuthenticationFailed, NotAuthorized) as err:
            raise ConfigEntryAuthFailed from err

    async def _keep_alive(self, now: datetime) -> None:
        """Pass through to pyalarmdotcomajax keep_alive().

        This will be called by async_track_time_interval, which submits a datetime parameter.
        Using now as a dummy to prevent errors.
        """

        try:
            return await self.api.keep_alive()  # type: ignore
        # Network-level failures are not AlarmdotcomException subclasses, so a plain
        # connection blip still produced an uncaught-exception traceback from HA's
        # scheduler every 60 seconds -- the exact noise this handler exists to stop.
        except (AlarmdotcomException, aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
            # Cosmetic only: if the session has actually degraded, the
            # coordinator's own refresh (async_update, every
            # CONF_UPDATE_INTERVAL seconds) will hit the same failure and
            # correctly trigger ConfigEntryAuthFailed/UpdateFailed. Without
            # this, a degraded session logs an uncaught-exception traceback
            # from HA's scheduler every 60 seconds until the next refresh.
            LOGGER.debug("Keep-alive failed; will resolve on next coordinator refresh.", exc_info=True)
            return None

    async def async_update(self) -> None:
        """Pull fresh data from Alarm.com for coordinator."""

        LOGGER.debug("%s: Requesting update from Alarm.com.", __name__)

        try:
            await self.api.async_update()

        except NotAuthorized as err:
            self.auth_health = "reauth_required"
            self.auth_health_detail = "Account has insufficient permissions."
            raise ConfigEntryAuthFailed("Account has insufficient permissions.") from err

        # Typically captured during login. Should only be captured here when
        # updating after migration from configuration.yaml.
        except AuthenticationFailed as err:
            self.auth_health = "reauth_required"
            self.auth_health_detail = "Invalid account credentials found while updating device states."
            raise ConfigEntryAuthFailed("Invalid account credentials found while updating device states.") from err

        except AlarmdotcomException as err:
            self.auth_health = "telus_error"
            self.auth_health_detail = str(err)
            raise UpdateFailed(str(err)) from err

        else:
            self.auth_health = "ok"
            self.auth_health_detail = None
            self.last_auth_success = dt_util.utcnow()

    def _ws_state_handler(self, state: WebSocketState) -> None:
        """Handle websocket state changes in the Alarm.com API."""

        previous = self._ws_state
        self._ws_state = state

        if state in {WebSocketState.DISCONNECTED, WebSocketState.STOPPED}:
            self._ws_close_event.set()
            return

        # Anything Alarm.com pushed while the socket was down is simply gone --
        # it does not replay on reconnect -- so without this the stale state
        # persists until the next REST poll (up to update_interval). Measured
        # live 2026-07-29: a patio door close was pushed one second before the
        # server's routine ~6 minute drop; two seconds later and it would have
        # vanished for a full poll cycle with nothing in the log to show for it.
        # Refresh once per reconnect, not on the initial connect (setup has just
        # polled), so the blind window costs ~1s instead of a poll interval.
        if state == WebSocketState.RUNNING:
            self._ws_attempt_reached_running = True
        if state == WebSocketState.RUNNING and previous not in (None, WebSocketState.STOPPED):
            if self._ws_seen_running:
                self.hass.async_create_task(self.update_coordinator.async_request_refresh())
            self._ws_seen_running = True

    async def async_start_websocket_monitor(self) -> None:
        """Start a websocket connection and monitor it for disconnection.

        __init__.py runs this via a bare asyncio.create_task() with nothing awaiting
        it, so any exception escaping this loop (a bug in start_websocket(), a bad
        state callback, an unexpected exception type from the underlying library)
        would otherwise kill real-time alarm/lock/sensor updates for the rest of
        this HA session with nothing more visible than an easily-missed "Task
        exception was never retrieved" warning -- the same silent-failure shape as
        the camera-bridge's own pipeline, which already guards against it (see
        camera_session.py/lifecycle.py's backstop comments). Catch, log loudly, and
        keep retrying rather than going dark.
        """
        ws_reconnect_timeout = self.config_entry.options.get(
            CONF_WEBSOCKET_RECONNECT_TIMEOUT, CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT
        )

        while True:
            try:
                self._ws_close_event.clear()
                self._ws_attempt_reached_running = False
                self.api.start_websocket(self._ws_state_handler)

                # start_websocket() only spawns a task; if that task dies before it
                # ever reaches ws_connect (network error at connect time, an
                # unexpected library exception), no state callback fires at all --
                # _ws_state_handler only sets the close event for DISCONNECTED and
                # STOPPED, so the state stays STARTING and this await would block
                # forever. Real-time alarm/lock/sensor updates would then be silently
                # dead for the rest of the HA session with nothing in the log.
                # So bound the wait only while still in the pre-running state; once
                # RUNNING is reached, a healthy connection legitimately stays open
                # indefinitely and must not be torn down on a timer.
                while True:
                    try:
                        async with asyncio.timeout(WEBSOCKET_START_TIMEOUT_SECONDS):
                            await self._ws_close_event.wait()
                        break
                    except TimeoutError:
                        if self._ws_state == WebSocketState.RUNNING:
                            await self._ws_close_event.wait()
                            break
                        LOGGER.warning(
                            "Websocket did not reach RUNNING within %ss (state=%s) -- "
                            "restarting it rather than waiting forever",
                            WEBSOCKET_START_TIMEOUT_SECONDS,
                            self._ws_state,
                        )
                        break

                if self._ws_state == WebSocketState.STOPPED:
                    break

                # A socket that connected and was later dropped (the server's
                # routine ~6 min close) reconnects on the fixed delay, as before.
                # An attempt that never reached RUNNING -- a connect timeout, a
                # rejected token -- backs off: each one cost a REST token fetch
                # against an edge that rate-limits the whole account, and they
                # arrive in streaks (backlog E106).
                if self._ws_attempt_reached_running:
                    self._ws_connect_failures = 0
                    delay = ws_reconnect_timeout
                else:
                    self._ws_connect_failures += 1
                    delay = reconnect_delay(
                        ws_reconnect_timeout, self._ws_connect_failures,
                        WEBSOCKET_RECONNECT_BACKOFF_MAX_SECONDS)
                    LOGGER.warning(
                        "Websocket attempt %d did not reach RUNNING (state=%s); "
                        "next attempt in %.0fs",
                        self._ws_connect_failures, self._ws_state, delay)
                await asyncio.sleep(delay)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception(
                    "Websocket monitor loop hit an unexpected error -- retrying in %ss "
                    "rather than silently stopping real-time updates", ws_reconnect_timeout
                )
                await asyncio.sleep(ws_reconnect_timeout)

    @property
    def provider_name(self) -> str:
        """Return the name of the provider."""
        return str(self.api.provider_name)

    @property
    def user_email(self) -> str:
        """Return the user email."""
        return str(self.api.user_email)


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Refresh config entry when users updates configuration options."""
    await hass.config_entries.async_reload(entry.entry_id)
