"""Diagnostics support for TELUS Smart Home.

IMPORTANT: config_entry.data stores a plaintext TELUS username and password
(see config_flow.py), and the camera-bridge credential sync payload carries
the same plaintext password. A diagnostics dump attached to a GitHub issue or
shared log must never leak these. async_redact_data only redacts by dict key,
so anything derived from the controller object (e.g. user_email) is masked
explicitly below rather than relying on key-based redaction to catch it.
"""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_2FA_COOKIE, CONF_ARM_CODE, DATA_CONTROLLER, DOMAIN
from .controller import AlarmIntegrationController

TO_REDACT = {CONF_USERNAME, CONF_PASSWORD, CONF_2FA_COOKIE, CONF_ARM_CODE}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: ConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""

    controller: AlarmIntegrationController = hass.data[DOMAIN][entry.entry_id][DATA_CONTROLLER]

    return {
        "entry_data": async_redact_data(dict(entry.data), TO_REDACT),
        "entry_options": async_redact_data(dict(entry.options), TO_REDACT),
        # provider_name/user_email/auth_health are derived attributes on the
        # controller object, not entry.data dict keys -- async_redact_data's
        # key-based redaction can't see them. user_email is deliberately
        # omitted entirely rather than partially masked.
        "controller": {
            "provider_name": controller.provider_name,
            "auth_health": controller.auth_health,
            "auth_health_detail": controller.auth_health_detail,
            "last_auth_success": (
                controller.last_auth_success.isoformat() if controller.last_auth_success else None
            ),
            "coordinator_last_update_success": controller.update_coordinator.last_update_success,
        },
    }
