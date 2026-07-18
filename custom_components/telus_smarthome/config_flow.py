"""Config flow to configure Alarmdotcom."""

from __future__ import annotations

from . import telus_auth_patch  # noqa: F401  (applies TELUS SSO monkeypatch on import)

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Literal

import aiohttp
import async_timeout
import voluptuous as vol
from homeassistant import config_entries
from homeassistant.const import CONF_PASSWORD, CONF_UNIT_OF_MEASUREMENT, CONF_USERNAME
from homeassistant.data_entry_flow import FlowResult
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.selector import (
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)
from pyalarmdotcomajax.const import OtpType
from pyalarmdotcomajax.exceptions import AlarmdotcomException, OtpRequired
from pyalarmdotcomajax.exceptions import AuthenticationFailed as libAuthenticationFailed
from pyalarmdotcomajax.exceptions import (
    ConfigureTwoFactorAuthentication as libConfigureTwoFactorAuthentication,
)
from pyalarmdotcomajax.exceptions import (
    UnexpectedResponse as libUnexpectedResponse,
)

from .const import (
    CAMERA_BRIDGE_CONTROL_URL,
    CAMERA_BRIDGE_SHARE_DIR,
    CONF_2FA_COOKIE,
    CONF_ARM_AWAY,
    CONF_ARM_CODE,
    CONF_ARM_HOME,
    CONF_ARM_MODE_OPTIONS,
    CONF_ARM_NIGHT,
    CONF_DEFAULT_UPDATE_INTERVAL_SECONDS,
    CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT,
    CONF_OPTIONS_DEFAULT,
    CONF_OTP,
    CONF_OTP_METHOD,
    CONF_OTP_METHODS_LIST,
    CONF_UPDATE_INTERVAL,
    CONF_WEBSOCKET_RECONNECT_TIMEOUT,
    DOMAIN,
)
from .controller import AlarmIntegrationController

LOGGER = logging.getLogger(__name__)
LegacyArmingOptions = Literal["home", "away", "true", "false"]


def _sync_write_fallback_and_read_secret(payload: dict) -> str | None:
    """Write the camera-bridge credential fallback file and read its control secret.

    Runs in an executor thread (blocking file I/O) — never call directly from the
    event loop. Best-effort: any failure here must not surface as a config flow error,
    since HA's own successful Alarm.com login is what determines reconfigure/reauth
    success, not whether the camera bridge is currently reachable.
    """
    share_dir = Path(CAMERA_BRIDGE_SHARE_DIR)
    try:
        share_dir.mkdir(parents=True, exist_ok=True)
        tmp = share_dir / "credentials.json.tmp"
        # This file holds a plaintext TELUS password -- write it 0o600 like the
        # bridge's own control_secret, not world-readable via the default umask.
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, json.dumps(payload).encode())
            # 0o600 alone isn't enough: this code runs as HA's user (root in
            # the container) but the reader is the bridge's non-root user, so
            # a root-owned file is unreadable there (observed live: the
            # bridge's fallback watcher logging PermissionError every 30s).
            # Match the share dir's owner -- the bridge creates and owns that
            # dir. Best-effort: on a non-root HA install chown may be denied,
            # which just preserves today's behavior.
            try:
                st = os.stat(share_dir)
                os.fchown(fd, st.st_uid, st.st_gid)
            except OSError:
                pass
        finally:
            os.close(fd)
        os.replace(tmp, share_dir / "credentials.json")
    except OSError:
        LOGGER.warning("Could not write camera-bridge credential fallback file at %s.", share_dir)

    try:
        return (share_dir / "control_secret").read_text().strip()
    except OSError:
        return None


class ADCFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):  # type: ignore
    """Handle a Alarmdotcom config flow."""

    VERSION = 4

    def __init__(self) -> None:
        """Initialize the Alarmdotcom flow."""
        self.config: dict = {}
        self.system_id: str | None = None
        self.sensor_data: dict | None = {}
        self._config_title: str | None = None
        self._controller: AlarmIntegrationController
        self._existing_entry: config_entries.ConfigEntry | None = None
        self._success_abort_reason: str = "reauth_successful"
        self._enabled_otp_methods: list[OtpType] = []

        self._force_generic_name: bool = False

        self.otp_method: OtpType | None = None

    @staticmethod
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> ADCOptionsFlowHandler:
        """Tell Home Assistant that this integration supports configuration options."""
        return ADCOptionsFlowHandler(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Gather configuration data when flow is initiated via the user interface."""
        errors = {}

        if user_input is not None:
            username, password = self._normalize_creds(user_input)
            self.config = {
                CONF_USERNAME: username,
                CONF_PASSWORD: password,
                CONF_2FA_COOKIE: user_input.get(CONF_2FA_COOKIE),
            }

            LOGGER.debug("Logging in to Alarm.com...")

            self._controller = AlarmIntegrationController(self.hass, self.config)

            async with async_timeout.timeout(60):
                try:
                    await self._controller.initialize_lite(
                        username=self.config[CONF_USERNAME],
                        password=self.config[CONF_PASSWORD],
                        twofactorcookie=self.config[CONF_2FA_COOKIE],
                    )

                except OtpRequired as exc:
                    LOGGER.debug("OTP code required.")
                    self._enabled_otp_methods = exc.enabled_2fa_methods
                    return await self.async_step_otp_select_method()

                except libConfigureTwoFactorAuthentication:
                    return self.async_abort(reason="must_enable_2fa")

                except (
                    TimeoutError,
                    aiohttp.ClientError,
                    asyncio.exceptions.CancelledError,
                ):
                    LOGGER.warning("%s: user login failed to contact Alarm.com.", __name__)
                    errors["base"] = "cannot_connect"

                except libUnexpectedResponse:
                    LOGGER.warning(
                        "%s: TELUS SSO handoff returned an unexpected response (likely a TELUS-side change,"
                        " not a bad password).",
                        __name__,
                    )
                    errors["base"] = "telus_sso_error"

                except ConfigEntryAuthFailed:
                    LOGGER.warning("%s: user login failed with AuthenticationFailed exception.", __name__)
                    errors["base"] = "invalid_auth"

                except AlarmdotcomException:
                    LOGGER.exception("Got error while initializing Alarm.com.")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_final()

        creds_schema = vol.Schema(
            {
                vol.Required(CONF_USERNAME): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
                ),
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                ),
            }
        )

        return self.async_show_form(step_id="user", data_schema=creds_schema, errors=errors, last_step=False)

    async def async_step_otp_select_method(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Select OTP method when integration configured through UI."""
        errors = {}

        if user_input is not None:
            self.otp_method = OtpType(
                {otp_type.name: otp_type.value for otp_type in OtpType}.get(user_input[CONF_OTP_METHOD])
            )
            if self.otp_method in (OtpType.email, OtpType.sms):
                # Ask Alarm.com to send OTP if selected method is EMAIL or SMS.
                LOGGER.debug(f"Requesting One-Time Password via {self.otp_method.name}...")
                await self._controller.api.async_request_otp(self.otp_method)

            return await self.async_step_otp_submit()

        try:
            # Get list of enabled OTP methods.
            if len(self._enabled_otp_methods) == 1 and self._enabled_otp_methods[0] == OtpType.app:
                # If APP is the only enabled OTP method, use it without prompting user.
                self.otp_method = OtpType.app
                LOGGER.debug(f"Using {self.otp_method.name} for One-Time Password.")
                return await self.async_step_otp_submit()

        except (TimeoutError, aiohttp.ClientError, libUnexpectedResponse):
            LOGGER.exception(
                "%s: OTP submission failed connection exception.",
                __name__,
            )
            errors["base"] = "cannot_connect"

        otp_method_schema = vol.Schema(
            {
                vol.Required(CONF_OTP_METHOD, default=self._enabled_otp_methods[0].name): SelectSelector(
                    SelectSelectorConfig(
                        options=[otp_type.name for otp_type in self._enabled_otp_methods],
                        mode=SelectSelectorMode.DROPDOWN,
                        translation_key=CONF_OTP_METHODS_LIST,
                    )
                ),
            }
        )

        return self.async_show_form(
            step_id="otp_select_method", data_schema=otp_method_schema, errors=errors, last_step=False
        )

    async def async_step_otp_submit(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Gather OTP when integration configured through UI."""
        errors = {}
        if user_input is not None:
            if not self.otp_method:
                raise AttributeError("OTP method not set.")

            try:
                await self._controller.api.async_submit_otp(
                    method=self.otp_method,
                    code=user_input[CONF_OTP],
                    device_name=f"Home Assistant ({self.hass.config.location_name})",
                )

                if cookie := self._controller.api.two_factor_cookie:
                    self.config[CONF_2FA_COOKIE] = cookie
                else:
                    raise libAuthenticationFailed("OTP submission failed. Two-factor cookie not found.")

            # AttributeError raised if api has not been initialized.
            except (TimeoutError, AttributeError, libUnexpectedResponse, aiohttp.ClientError):
                LOGGER.exception(
                    "%s: OTP submission failed with CannotConnect exception.",
                    __name__,
                )
                errors["base"] = "cannot_connect"

            except libAuthenticationFailed:
                LOGGER.exception(
                    "%s: Incorrect OTP code entered.",
                    __name__,
                )
                errors["base"] = "invalid_otp"

            else:
                return await self.async_step_final()

        creds_schema = vol.Schema(
            {
                vol.Required(CONF_OTP): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="one-time-code")
                ),
            }
        )

        return self.async_show_form(step_id="otp_submit", data_schema=creds_schema, errors=errors, last_step=True)

    async def async_step_final(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Create configuration entry using entered data."""

        self._config_title = (
            "Alarm.com"
            if self._force_generic_name
            else f"{self._controller.provider_name}:{self._controller.user_email}"
        )

        # Unique ID is the TELUS/Alarm.com account email. This prevents the
        # same account from being onboarded twice (duplicate device/entity
        # trees), and -- for reauth/reconfigure -- guards against a mistyped
        # or wrong-account credential swap silently repointing an existing
        # entry at a different TELUS account.
        await self.async_set_unique_id(self._controller.user_email)

        if self._existing_entry:
            # Entries created before this unique_id migration have no
            # unique_id yet -- only enforce the account-match guard once the
            # entry actually has one (stamped below via
            # async_update_reload_and_abort's unique_id= kwarg), so the
            # first reauth/reconfigure after upgrading doesn't spuriously
            # abort as a cross-account "mismatch".
            if self._existing_entry.unique_id is not None:
                self._abort_if_unique_id_mismatch()

            LOGGER.debug("Existing entry found — updating in place (%s).", self._success_abort_reason)

            await self._async_sync_bridge_credentials()

            return self.async_update_reload_and_abort(
                self._existing_entry,
                unique_id=self.unique_id,
                title=self._config_title,
                data=self.config,
                reason=self._success_abort_reason,
            )

        self._abort_if_unique_id_configured(error="already_configured_account")

        # Named async_ but doesn't require await!
        return self.async_create_entry(title=self._config_title, data=self.config, options=CONF_OPTIONS_DEFAULT)

    @staticmethod
    def _normalize_creds(user_input: dict[str, Any]) -> tuple[str, str]:
        """Trim whitespace from submitted username/password.

        Guards against a silent "wrong password" caused by a pasted trailing
        newline or leading/trailing space.
        """
        return user_input[CONF_USERNAME].strip(), user_input[CONF_PASSWORD].strip()

    async def _async_sync_bridge_credentials(self) -> None:
        """Push updated credentials to the camera bridge; write a fallback file either way.

        Best-effort only — must never block or fail this config flow. HA's own
        successful Alarm.com login is the sole determinant of reconfigure/reauth
        success, not whether the (separate, independently-deployed) camera bridge
        container happens to be reachable right now.
        """
        payload = {
            "username": self.config[CONF_USERNAME],
            "password": self.config[CONF_PASSWORD],
            "updated_at": time.time(),
        }

        secret = await self.hass.async_add_executor_job(_sync_write_fallback_and_read_secret, payload)

        if secret is None:
            LOGGER.debug(
                "No camera-bridge control secret found on disk yet (bridge may not have started once with the"
                " new mount in place); fallback credentials file was written, skipping live push."
            )
            return

        session = async_create_clientsession(self.hass)
        try:
            async with async_timeout.timeout(5):
                async with session.post(
                    CAMERA_BRIDGE_CONTROL_URL,
                    json={"username": self.config[CONF_USERNAME], "password": self.config[CONF_PASSWORD]},
                    headers={"Authorization": f"Bearer {secret}"},
                ) as resp:
                    if resp.status == 200:
                        LOGGER.info("Camera bridge accepted updated credentials.")
                    else:
                        LOGGER.warning(
                            "Camera bridge rejected credential push (HTTP %s); fallback file was written,"
                            " it will self-heal on the bridge's own poll.",
                            resp.status,
                        )
        except (aiohttp.ClientError, TimeoutError, asyncio.TimeoutError):
            LOGGER.warning(
                "Camera bridge unreachable at %s; fallback file was written, it will pick this up on its"
                " own next poll or restart.",
                CAMERA_BRIDGE_CONTROL_URL,
            )

    # #
    # Reauthentication Steps
    # #

    async def async_step_reauth(self, entry_data: dict[str, Any]) -> FlowResult:
        """Perform reauth upon an API authentication error."""
        LOGGER.debug("Reauthenticating.")
        self._existing_entry = self._get_reauth_entry()
        self._success_abort_reason = "reauth_successful"
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Dialog that informs the user that reauth is required."""
        if user_input is None:
            return self.async_show_form(
                step_id="reauth_confirm",
                data_schema=vol.Schema({}),
            )
        return await self.async_step_user()

    # #
    # Reconfiguration Step
    # #

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Let the user update username/password in place, without deleting/recreating the entry.

        Reachable from Settings > Devices & Services > (integration card) > the
        three-dot menu > Reconfigure. Entities/devices are preserved because this
        updates the existing config entry rather than creating a new one.
        """
        errors: dict[str, str] = {}
        self._existing_entry = self._get_reconfigure_entry()
        self._success_abort_reason = "reconfigure_successful"

        if user_input is not None:
            username, password = self._normalize_creds(user_input)
            self.config = {CONF_USERNAME: username, CONF_PASSWORD: password, CONF_2FA_COOKIE: None}

            LOGGER.debug("Reconfigure: logging in to Alarm.com with updated credentials...")

            self._controller = AlarmIntegrationController(self.hass, self.config)

            async with async_timeout.timeout(60):
                try:
                    await self._controller.initialize_lite(username=username, password=password, twofactorcookie=None)

                except OtpRequired as exc:
                    LOGGER.debug("OTP code required.")
                    self._enabled_otp_methods = exc.enabled_2fa_methods
                    return await self.async_step_otp_select_method()

                except libConfigureTwoFactorAuthentication:
                    return self.async_abort(reason="must_enable_2fa")

                except (
                    TimeoutError,
                    aiohttp.ClientError,
                    asyncio.exceptions.CancelledError,
                ):
                    LOGGER.warning("Reconfigure: could not reach Alarm.com/TELUS.")
                    errors["base"] = "cannot_connect"

                except libUnexpectedResponse:
                    LOGGER.warning(
                        "Reconfigure: TELUS SSO handoff returned an unexpected response (likely a TELUS-side"
                        " change, not a bad password)."
                    )
                    errors["base"] = "telus_sso_error"

                except ConfigEntryAuthFailed:
                    LOGGER.warning("Reconfigure: credentials rejected.")
                    errors["base"] = "invalid_auth"

                except AlarmdotcomException:
                    LOGGER.exception("Reconfigure: unexpected error.")
                    errors["base"] = "unknown"
                else:
                    return await self.async_step_final()

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_USERNAME, default=self._existing_entry.data.get(CONF_USERNAME, "")
                ): TextSelector(TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")),
                # Password is intentionally never pre-filled.
                vol.Required(CONF_PASSWORD): TextSelector(
                    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
                ),
            }
        )

        return self.async_show_form(step_id="reconfigure", data_schema=schema, errors=errors)


class ADCOptionsFlowHandler(config_entries.OptionsFlow):  # type: ignore
    """Handle option configuration via Integrations page."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow.

        Deliberately does NOT set self.config_entry: recent HA core versions
        made OptionsFlow.config_entry a read-only property, populated
        automatically by the flow manager. Assigning to it here raises
        AttributeError ("property ... has no setter") -- confirmed live, this
        broke the options flow (gear icon) entirely until fixed. The
        `config_entry` parameter itself is still used below/by the framework;
        only the manual self-assignment was the problem.
        """
        self.options = dict(config_entry.options)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First screen for configuration options. Sets arming code."""
        errors: dict = {}

        if user_input is not None:
            if user_input.get(CONF_ARM_CODE) == "CLEAR!":
                user_input[CONF_ARM_CODE] = ""
            self.options.update(user_input)
            return await self.async_step_modes()

        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_ARM_CODE,
                    default=("" if not (arm_code_raw := self.options.get(CONF_ARM_CODE)) else arm_code_raw),
                ): selector.selector({"text": {"type": "password"}}),
                vol.Required(
                    CONF_UPDATE_INTERVAL,
                    default=self.options.get(CONF_UPDATE_INTERVAL, CONF_DEFAULT_UPDATE_INTERVAL_SECONDS),
                ): selector.selector(
                    {
                        "number": {
                            "mode": "box",
                            CONF_UNIT_OF_MEASUREMENT: "seconds",
                        }
                    }
                ),
                vol.Required(
                    CONF_WEBSOCKET_RECONNECT_TIMEOUT,
                    default=self.options.get(
                        CONF_WEBSOCKET_RECONNECT_TIMEOUT, CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT
                    ),
                ): selector.selector(
                    {
                        "number": {
                            "mode": "box",
                            CONF_UNIT_OF_MEASUREMENT: "seconds",
                        }
                    }
                ),
            }
        )

        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            errors=errors,
            last_step=False,
        )

    async def async_step_modes(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """First screen for configuration options. Sets arming mode profiles."""
        errors: dict = {}

        if user_input is not None:
            self.options.update(user_input)
            return self.async_create_entry(title="", data=self.options)

        schema = vol.Schema(
            {
                vol.Required(
                    CONF_ARM_HOME,
                    default=self.options.get(CONF_ARM_HOME, CONF_OPTIONS_DEFAULT[CONF_ARM_HOME]),
                ): cv.multi_select(CONF_ARM_MODE_OPTIONS),
                vol.Required(
                    CONF_ARM_AWAY,
                    default=self.options.get(CONF_ARM_AWAY, CONF_OPTIONS_DEFAULT[CONF_ARM_AWAY]),
                ): cv.multi_select(CONF_ARM_MODE_OPTIONS),
                vol.Required(
                    CONF_ARM_NIGHT,
                    default=self.options.get(CONF_ARM_NIGHT, CONF_OPTIONS_DEFAULT[CONF_ARM_NIGHT]),
                ): cv.multi_select(CONF_ARM_MODE_OPTIONS),
            }
        )

        return self.async_show_form(
            step_id="modes",
            data_schema=schema,
            errors=errors,
            last_step=True,
        )
