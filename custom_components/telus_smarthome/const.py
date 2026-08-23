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
# The REST poll is only the fallback behind the push websocket, but the server
# closes that websocket ~6 minutes after every connect (token lifetime -- a
# keepalive ping doesn't prevent it, see telus_ws_heartbeat_patch), so both
# knobs matter for real-world latency: reconnect fast so push is up ~92% of
# the time, and poll at 5 minutes so a change landing in the reconnect gap
# isn't stale for long. Values below 300s for the poll risk Alarm.com
# rate-limiting a live account.
CONF_DEFAULT_UPDATE_INTERVAL_SECONDS = 300  # 5 minutes
CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT = 30  # seconds

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
DATA_ACTIVITY_COORDINATOR = "activity_coordinator"
# Shared CameraSettingsCoordinator instance. Unlike the two above it is NOT
# created in __init__.py: number/switch/button/sensor create it on first use via
# camera_settings_coordinator.async_get_camera_settings_coordinator(), which
# stores it here. __init__.py pops this whole per-entry dict on unload, so it
# needs no teardown of its own.
DATA_CAMERA_SETTINGS_COORDINATOR = "camera_settings_coordinator"
# Shared ArmingDevicesCoordinator instance, created on first use by
# arming_devices_coordinator.async_get_arming_devices_coordinator() exactly like
# the one above. Only switch.py needs it today; it is stored (rather than being a
# local in that platform) so that a second platform -- the system-wide pairing
# switch and disarm-timeout number of backlog E41 are the obvious candidates --
# cannot silently double the poll rate against a rate-limited account.
DATA_ARMING_DEVICES_COORDINATOR = "arming_devices_coordinator"

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

# Talkdown: play a spoken phrase (or an arbitrary audio clip) through one
# camera's speaker via the bridge's two-way-audio endpoint -- {camera_id} is
# str.format()'d in. POST; body is JSON {"audio_url": ...} (HA renders TTS to a
# loopback media-source URL and hands it to the bridge to fetch). Off by default
# on the bridge side (config.TALKDOWN_ENABLED, on by default) -- 403 if disabled. See
# camera-bridge/bridge/control_server.py for the response shape and speak_service.py.
CAMERA_BRIDGE_TALK_URL_TEMPLATE = "http://127.0.0.1:8199/control/cameras/{camera_id}/talk"

# Stops the talk currently playing on one camera -- {camera_id} is str.format()'d
# in. POST, empty body. The in-flight talk ends at the next chunk boundary and
# its own call returns partial stats. 404 when nothing is playing.
CAMERA_BRIDGE_TALK_STOP_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/talk/stop")

# Read-only list of the account's alarm partitions, with the flags that decide
# whether a selective sensor bypass is possible at all
# (has_permission_to_change_state, can_bypass_when_armed,
# has_open_bypassable_sensors) -- see camera-bridge/bridge/control_server.py for
# the response shape. Read on demand by bypass_service.py, NOT polled: every call
# is a live Alarm.com round trip and nothing in HA renders these.
CAMERA_BRIDGE_PARTITIONS_URL = "http://127.0.0.1:8199/control/partitions"

# Selective sensor bypass on one partition -- {partition_id} is str.format()'d
# in, and is the partition's Alarm.com device id (the same id the alarm panel
# entity uses as its unique_id, e.g. "<unit>-127"), not the panel's internal
# partition number. POST; body is JSON {"bypass": [<device id>, ...],
# "unbypass": [...]}, both optional but not both empty (400 otherwise).
#
# This changes what a LIVE alarm panel will and will not trigger on: a bypassed
# zone does not sound the alarm until it is un-bypassed. Only ever call it from
# the explicit telus_smarthome.bypass_sensors service, never from a poll, an arm
# path, or any automatic reaction to state. The separate CONF_FORCE_BYPASS arm
# flag above is a different mechanism and does not go through here.
#
# The bridge pre-reads the partition and answers 422 when the panel's own gates
# refuse (no permission, or armed with canBypassSensorWhenArmed false), 429 with
# a Retry-After header when rate-limited, 404 for an unknown partition and 502
# for an upstream failure.
CAMERA_BRIDGE_PARTITION_BYPASS_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/partitions/{partition_id}/bypass")

# Read-only list of the phones paired to the panel for Bluetooth disarming, each
# with the flag deciding whether its toggle may be written at all
# (supports_disarming_toggle) -- see camera-bridge/bridge/control_server.py for
# the response shape. An account with no paired phones answers an empty list,
# never a 404, so an empty list really does mean "nothing is paired".
CAMERA_BRIDGE_ARMING_DEVICES_URL = "http://127.0.0.1:8199/control/arming-devices"

# Turns Bluetooth disarming on or off for ONE paired phone -- {device_id} is
# str.format()'d in, and is the arming device's Alarm.com device id
# ("<unit>-<did>"). POST; body is JSON {"enabled": <bool>}, a real JSON boolean
# (the bridge answers 400 rather than coercing a truthy string). Answers with the
# refreshed record, i.e. one element of the list above.
#
# This decides whether a paired phone walking up to the house DISARMS THE ALARM.
# Alarm.com is the configuration authority for it -- the panel's Control4
# endpoint is local-access-only and wired independently -- so there is no second
# place a mistake here gets corrected from. Only ever call it from an explicit
# user action on the switch entity, never from a poll or any reaction to state.
#
# The bridge refuses in three places before it writes: a non-boolean body is 400,
# a device id that is not in the list above is 404 (rather than a blind PUT at an
# arbitrary device on a live panel), and a record whose own
# supports_disarming_toggle is false is 422. It also answers 429 with a
# Retry-After header when rate-limited and 502 for an upstream failure.
CAMERA_BRIDGE_ARMING_DEVICE_DISARMING_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/arming-devices/{device_id}/disarming")

# telus_smarthome service names (registered in __init__.py via speak_service.py
# and bypass_service.py).
SERVICE_SPEAK = "speak"
SERVICE_STOP_SPEAKING = "stop_speaking"
SERVICE_BYPASS_SENSORS = "bypass_sensors"

# Talkdown counters (attempts/outcomes/failure reasons/durations) -- see
# camera-bridge/bridge/metrics.py. Surfaced as the two diagnostic sensors in
# sensor.py so a failing talkdown is visible in Home Assistant (and can drive a
# notification) instead of needing a curl against the bridge.
CAMERA_BRIDGE_METRICS_URL = "http://127.0.0.1:8199/control/metrics"

# How often sensor.py polls those counters. They only change when someone
# triggers a talk, which is rare, so this is deliberately slow -- it exists to
# notice failures, not to watch a clip play.
TALK_METRICS_POLL_INTERVAL_SECONDS = 120

# Recent video clip events across all cameras -- see
# camera-bridge/bridge/control_server.py for the response shape.
CAMERA_BRIDGE_EVENTS_URL = "http://127.0.0.1:8199/control/events"

# Video-related automation rules -- see camera-bridge/bridge/control_server.py
# for the response shape.
CAMERA_BRIDGE_AUTOMATIONS_URL = "http://127.0.0.1:8199/control/automations"

# System-wide Activity feed (arm/disarm, door/lock, camera triggers, etc) --
# broader than CAMERA_BRIDGE_EVENTS_URL, which is camera clips only. See
# camera-bridge/bridge/control_server.py for the response shape.
CAMERA_BRIDGE_HISTORY_URL = "http://127.0.0.1:8199/control/history"

# Re-mints a fresh signed download URL for one saved clip --
# {video_event_id} is str.format()'d in. The signed URLs in event
# attributes expire within minutes of the poll that fetched them (the clip
# itself never does), so clip_view.py calls this at click time and
# redirects to the result. See camera-bridge/bridge/control_server.py for
# the response shape.
CAMERA_BRIDGE_CLIP_URL_TEMPLATE = "http://127.0.0.1:8199/control/clip/{video_event_id}"

# Per-install random secret (config entry data) that signs the /api
# clip-redirect links clip_view.py serves. Deliberately NOT the bridge's
# rotating control secret: these signatures live inside already-delivered
# phone notifications, so rotating the key would break every clip link
# older than the rotation interval -- the exact bug this feature removes.
CONF_CLIP_LINK_SECRET = "clip_link_secret"

# How long a signed clip link stays valid, baked into each link's signature at
# creation and re-checked on every tap. Wide enough that tapping an old
# notification still works, but finite so an exfiltrated link is not a
# perpetual bearer token to security-camera footage. Bounded exposure comes
# from this per-link expiry, not from rotating the (never-rotated) secret.
CLIP_LINK_TTL_SECONDS = 90 * 24 * 60 * 60  # 90 days

# Pause/resume one rule -- {rule_id} is str.format()'d in. POST, empty body.
CAMERA_BRIDGE_AUTOMATION_PAUSE_URL_TEMPLATE = "http://127.0.0.1:8199/control/automations/{rule_id}/pause"
CAMERA_BRIDGE_AUTOMATION_RESUME_URL_TEMPLATE = "http://127.0.0.1:8199/control/automations/{rule_id}/resume"

# #
# CAMERA SETTINGS, DIAGNOSTICS AND DEVICE COMMANDS
#
# The bridge exposes the website API's camera-settings surface (speaker/call
# volume, chime, image and video config), its read-only diagnostics endpoints,
# and the mobile API's siren / privacy-mode commands and status-LED settings.
# {camera_id} is str.format()'d into each. See camera-bridge/bridge/control_server.py for the
# response shapes and docs/control-api.md for the error vocabulary.
#
# Two properties of that surface shape everything built on it here:
#  * Every settings field is paired with a "<name>FeatureSupport" enum, and the
#    same payload shape comes back for a device that CANNOT do the thing. The
#    bridge resolves those into plain supports_* booleans and answers 422 on a
#    write to an unsupported field -- so entities are gated on the boolean and
#    never created for a capability a camera doesn't report.
#  * Every one of these is a live call against a real camera. The GETs are
#    polled (slowly, below); the POSTs are only ever issued from an explicit
#    user action on an entity, never as a side effect of a read.
# #

CAMERA_BRIDGE_CAMERA_SETTINGS_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings")
CAMERA_BRIDGE_CAMERA_DIAGNOSTICS_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/diagnostics")

# POST, body {"volume": int} -- the 0-10 speaker/call volume talkdown plays at.
CAMERA_BRIDGE_CAMERA_VOLUME_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings/volume")
# POST, body {"indoor_on": bool?, "outdoor_on": bool?, "chime_type": int?,
# "outdoor_volume": int?} -- any subset. Answers with the whole refreshed
# `audio` section, which the caller folds back in rather than re-polling.
CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings/chime")
# POST, body of brightness/contrast/saturation/sharpness/is_flipped/hdr_enabled/
# ir_led_enabled/ir_sensitivity/night_vision_enabled. No entity drives these yet
# -- listed so the bridge's control surface is documented in one place.
CAMERA_BRIDGE_CAMERA_IMAGE_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings/image")
# POST, body of clip_resolution/clip_quality/pretrigger_enabled/keep_clips_days/
# alarm_clip_duration. Also not yet driven by an entity.
CAMERA_BRIDGE_CAMERA_VIDEO_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings/video")
# POST, body {"enabled": bool?, "brightness": int?} -- any subset. The device's
# own status indicator light (the mobile API's canturnonoffdeviceled / ledi /
# ledo / ledmn / ledmx), NOT the infrared illuminator, which is ir_led_enabled
# on the image route above. Answers with the whole refreshed `led` section, which
# the caller folds back in rather than re-polling.
CAMERA_BRIDGE_CAMERA_LED_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings/led")
# POST, body {"motion": bool?, "button_press": bool?, "smart_chime": bool?} --
# any subset. These are the push notifications TELUS sends to its OWN mobile
# app, nothing to do with Home Assistant's notifications; the reason to touch
# them from here is to stop being told twice about one doorbell press. Answers
# with the whole refreshed `notifications` section, which the caller folds back
# in rather than re-polling.
# The section also reports a read-only `format` enum (Unknown/TextOnly/
# AttachThumbnail). Neither the web bundle nor the decompiled app defines its
# numeric values, so the 0 the reference doorbell reports cannot be told apart
# from "unknown" -- no entity writes it, and this route accepts no field for it.
CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/settings/notifications")
# POST, body {"action": "start"|"stop"}. This makes a real siren sound in
# someone's home -- only ever fired from a button press.
CAMERA_BRIDGE_CAMERA_SIREN_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/siren")
# POST, body {"active": bool}.
CAMERA_BRIDGE_CAMERA_PRIVACY_URL_TEMPLATE = (
    "http://127.0.0.1:8199/control/cameras/{camera_id}/privacy")

# How often camera_settings_coordinator.py re-reads the two GET routes above,
# for every known camera. Deliberately slow: unlike the camera list (a local
# in-memory dict on the bridge), each of these is a live round trip to TELUS,
# so the per-cycle cost is 2 requests per camera. These settings only change
# when someone edits them in the app or the portal, and Alarm.com's rate limits
# are undocumented and can lock the account (docs/troubleshooting.md), so this
# buys responsiveness nobody needs at a real risk. A write through one of the
# POST routes requests its own immediate refresh rather than waiting for this.
CAMERA_SETTINGS_POLL_INTERVAL_SECONDS = 120
# The diagnostics route is three sequential alarm.com reads behind one request,
# and alarm.com seems to query the camera itself for the signal figure. Measured
# live at 1.6s on a doorbell and 25s on an indoor camera in the SAME account, so
# the shared 10s bridge timeout is not a slow-bridge signal here -- it is normal
# for some models, and enforcing 10s failed the poll for every camera at once.
# Comfortably inside the 120s poll interval even if two cameras are both slow.
CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS = 45

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

# How often arming_devices_coordinator.py re-reads the Bluetooth arming device
# list. Fifteen minutes, an order of magnitude slower than the camera settings'
# 120s, and the difference is deliberate rather than copied:
#  * What it watches barely moves. A paired phone's disarming flag is a
#    configuration record that changes only when a person changes it -- here, in
#    the TELUS app, or in the portal. Camera settings at least track a device
#    that can also be touched by a chime or an LED command; this cannot.
#  * The poll is not the feedback path for the one change this integration makes.
#    A write through the switch adopts the record the POST route answers with, so
#    a user toggling it sees the new state immediately at any interval.
#  * Every cycle is a live Alarm.com round trip on an account whose rate limits
#    are undocumented and can lock it (AGENTS.md). This is one request per cycle
#    rather than two per camera, but it is competing for the same budget as the
#    5-minute panel poll and the 30s video-event poll, which watch things that
#    genuinely change -- spending it here would buy responsiveness to an
#    out-of-band edit nobody is waiting on.
# The cost of the slow interval: an edit made in the TELUS app takes up to this
# long to show here, and a newly paired phone takes up to this long to get its
# switch (entities are created from a poll -- see async_add_arming_device_entities).
# Both are acceptable for a record whose value changes on the order of years.
ARMING_DEVICES_POLL_INTERVAL_SECONDS = 900  # 15 minutes

# How often binary_sensor.py polls automation rule pause/active state.
# Infrequent -- these change rarely (a user manually pausing/resuming a
# recording rule), unlike camera discovery or video events.
AUTOMATION_RULES_POLL_INTERVAL_SECONDS = 300

DEVICE_STATIC_ATTRIBUTES = [libConst.ATTR_STATE_TEXT, libConst.ATTR_MAC_ADDRESS]
