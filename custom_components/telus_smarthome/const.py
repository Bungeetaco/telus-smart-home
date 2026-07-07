"""Const for the Alarmdotcom integration."""

from __future__ import annotations

import logging

from homeassistant.const import Platform
from pyalarmdotcomajax import const as libConst
from pyalarmdotcomajax.devices.sensor import Sensor as libSensor

INTEGRATION_NAME = "Alarm.com"
DOMAIN = "telus_smarthome"
ISSUE_URL = "https://github.com/pyalarmdotcom/alarmdotcom/issues"
STARTUP_MESSAGE = f"""
===================================================================
{DOMAIN}
This is a custom component
If you have any issues with this you need to open an issue here:
{ISSUE_URL}
===================================================================
"""

STATE_MALFUNCTION = "Malfunction"

DEBUG_REQ_EVENT = "telus_smarthome_debug_request"

MIGRATE_MSG_ALERT = (
    "The Alarm.com integration is now configured exclusively via Home Assistant's"
    " integrations page. Please delete the Alarm.com entry from configuration.yaml."
    " Your existing settings have already been migrated."
)

KEEP_ALIVE_INTERVAL_SECONDS = 60
CONF_DEFAULT_UPDATE_INTERVAL_SECONDS = 900  # 15 minutes
CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT = 300  # 5 minutes

LOGGER = logging.getLogger(__package__)

# #
# CONFIGURATION
# #

# Configuration
CONF_2FA_COOKIE = "2fa_cookie"
CONF_OTP = "otp"
CONF_OTP_METHOD = "otp_method"
CONF_OTP_METHODS_LIST = "otp_methods_list"

CONF_ARM_CODE = "arm_code"
CONF_UPDATE_INTERVAL = "update_interval"
CONF_WEBSOCKET_RECONNECT_TIMEOUT = "ws_reconnect_timeout"
CONF_ARM_HOME = "arm_home_options"
CONF_ARM_AWAY = "arm_away_options"
CONF_ARM_NIGHT = "arm_night_options"


CONF_FORCE_BYPASS = "force_bypass"
CONF_SILENT_ARM = "silent_arming"
CONF_NO_ENTRY_DELAY = "no_entry_delay"
CONF_ARM_MODE_OPTIONS = {
    CONF_FORCE_BYPASS: "Force Bypass",
    CONF_SILENT_ARM: "Arm Silently",
    CONF_NO_ENTRY_DELAY: "No Entry Delay",
}

CONF_OPTIONS_DEFAULT = {
    CONF_ARM_CODE: "",
    CONF_ARM_HOME: [],
    CONF_ARM_AWAY: [],
    CONF_ARM_NIGHT: [],
    CONF_UPDATE_INTERVAL: CONF_DEFAULT_UPDATE_INTERVAL_SECONDS,
    CONF_WEBSOCKET_RECONNECT_TIMEOUT: CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT,
}

SENSOR_SUBTYPE_BLACKLIST = [
    libSensor.Subtype.MOBILE_PHONE,  # No purpose
    libSensor.Subtype.PANEL_IMAGE_SENSOR,  # No support yet
    libSensor.Subtype.FIXED_PANIC,  # Doesn't support state reporting
]

DATA_CONTROLLER = "connection"
# Shared CameraBridgeCoordinator instance -- created once in __init__.py so
# camera.py and event.py both read the same camera list rather than each
# polling /control/cameras independently.
DATA_CAMERA_COORDINATOR = "camera_coordinator"

ATTRIB_BATTERY_NORMAL = "Normal"
ATTRIB_BATTERY_LOW = "Low"
ATTRIB_BATTERY_CRITICAL = "Critical"

PLATFORMS = [
    Platform.ALARM_CONTROL_PANEL,
    Platform.BINARY_SENSOR,
    Platform.LOCK,
    Platform.COVER,
    Platform.LIGHT,
    Platform.BUTTON,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.SELECT,
    Platform.CLIMATE,
    Platform.SENSOR,
    Platform.CAMERA,
    Platform.EVENT,
]

# #
# CAMERA BRIDGE CREDENTIAL SYNC
#
# The camera bridge (camera-bridge/ in this project's repo, a separate
# standalone Docker container -- see its own README) holds its own
# independent copy of the TELUS username/password.
# When credentials are changed here (reauth or reconfigure), we best-effort
# push them to the bridge's local control endpoint, and unconditionally write
# a fallback file the bridge also watches/loads at its own startup and on a
# poll loop, so an update is never silently lost if the bridge happens to be
# down when the push is attempted. See config_flow.py's
# _async_sync_bridge_credentials / _sync_write_fallback_and_read_secret.
# #

# Shared directory: HA's existing `/share` mount (host:
# /var/lib/homeassistant/share) is RW from this container. The bridge
# container mounts the same host subdirectory at /shared. Neither container
# needed a new supervisor-level mount to make this work on the HA side.
CAMERA_BRIDGE_SHARE_DIR = "/share/telus_cam_bridge"

# Both the HA container and the camera bridge container run with
# `--network host`, so 127.0.0.1 here really is the host's loopback that the
# bridge's control server (bound to 127.0.0.1:8199) is listening on.
CAMERA_BRIDGE_CONTROL_URL = "http://127.0.0.1:8199/control/credentials"

# Read-only endpoint the camera platform (camera.py) polls to learn which
# cameras currently exist and their RTSP publish URLs -- see
# camera-bridge/bridge/control_server.py for the response shape.
CAMERA_BRIDGE_CAMERAS_URL = "http://127.0.0.1:8199/control/cameras"

# On-demand still image for one camera -- {camera_id} is str.format()'d in.
# Unlike CAMERA_BRIDGE_CAMERAS_URL, every call here does a real round trip to
# TELUS (the bridge doesn't cache snapshots), so this is only fetched when a
# still is actually requested, not on a poll interval.
CAMERA_BRIDGE_SNAPSHOT_URL_TEMPLATE = "http://127.0.0.1:8199/control/cameras/{camera_id}/snapshot"

# Recent video clip events across all cameras -- see
# camera-bridge/bridge/control_server.py for the response shape.
CAMERA_BRIDGE_EVENTS_URL = "http://127.0.0.1:8199/control/events"

# Video-related automation rules -- see camera-bridge/bridge/control_server.py
# for the response shape.
CAMERA_BRIDGE_AUTOMATIONS_URL = "http://127.0.0.1:8199/control/automations"

# Pause/resume one rule -- {rule_id} is str.format()'d in. POST, empty body.
CAMERA_BRIDGE_AUTOMATION_PAUSE_URL_TEMPLATE = "http://127.0.0.1:8199/control/automations/{rule_id}/pause"
CAMERA_BRIDGE_AUTOMATION_RESUME_URL_TEMPLATE = "http://127.0.0.1:8199/control/automations/{rule_id}/resume"

# How often camera.py re-polls the bridge for its camera list. This is a
# cheap local-loopback HTTP GET against a small in-memory dict on the bridge
# side, so a short interval costs nothing -- kept well under the bridge's own
# CAMERA_DISCOVERY_INTERVAL_S (300s, see camera-bridge/bridge/config.py) so a
# camera the bridge just discovered shows up here on this coordinator's very
# next cycle rather than waiting on two independently-phased 5-minute polls.
CAMERA_DISCOVERY_POLL_INTERVAL_SECONDS = 60

# How often event.py polls for new video clip events. Shorter than the
# camera list poll since this is what determines how quickly a "person
# detected" event shows up in HA after it actually happens.
VIDEO_EVENTS_POLL_INTERVAL_SECONDS = 30

# How often binary_sensor.py polls automation rule pause/active state.
# Infrequent -- these change rarely (a user manually pausing/resuming a
# recording rule), unlike camera discovery or video events.
AUTOMATION_RULES_POLL_INTERVAL_SECONDS = 300

DEVICE_STATIC_ATTRIBUTES = [libConst.ATTR_STATE_TEXT, libConst.ATTR_MAC_ADDRESS]
