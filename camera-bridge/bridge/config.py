import os
import sys

TELUS_BASE = "https://smarthome-security.telus.com/"
AUTH_GATEWAY = "https://auth-gateway.telus.com"
TELUS_CLIENT_ID = "167c1141-d72c-4d39-8c72-1cebd10e0109"
TELUS_AUTH_PROVIDER = "9AE4AC54755C5EBFC489CB6CFAA69C1C"

try:
    TELUS_EMAIL = os.environ["TELUS_EMAIL"]
    TELUS_PASSWORD = os.environ["TELUS_PASSWORD"]
except KeyError as e:
    sys.exit(f"telus-cam-bridge: required environment variable {e} not set "
              f"(check /opt/telus-cam-bridge/.env)")
# NOTE: these two names are intentionally mutable module attributes -- both
# control_server.py (live credential push from HA's reconfigure flow) and
# main.py's fallback-file watcher reassign them at runtime, and telus_auth.py
# reads c.TELUS_EMAIL/c.TELUS_PASSWORD lazily inside login() on every call, so
# a reassignment here takes effect on the next login() with no process restart.

MEDIAMTX_RTSP_HOST = "127.0.0.1"
MEDIAMTX_RTSP_PORT = int(os.environ.get("MEDIAMTX_RTSP_PORT", "8554"))

# Local control endpoint -- lets the Home Assistant integration's reconfigure
# flow push updated TELUS credentials into this already-running process
# without a container restart. Bound to loopback only; both this container and
# the HA container run with --network host, so 127.0.0.1 is shared between
# them and this is never reachable from outside the host.
CONTROL_HOST = "127.0.0.1"
CONTROL_PORT = int(os.environ.get("CONTROL_PORT", "8199"))

# Shared directory with the HA container (HA side: /share/telus_cam_bridge,
# mounted here at /shared). Holds the control-endpoint's shared secret (this
# bridge generates it on first start) and a fallback credentials file that HA
# writes on every credential update, so this bridge can self-heal on its own
# next startup/poll even if the live push below was missed (bridge down,
# network hiccup, secret mismatch, etc).
SHARED_DIR = "/shared"
CONTROL_SECRET_PATH = f"{SHARED_DIR}/control_secret"
CREDENTIALS_FALLBACK_PATH = f"{SHARED_DIR}/credentials.json"
CREDENTIALS_FALLBACK_POLL_S = 30


def rtsp_path_for(camera_id: str) -> str:
    return "telus_" + camera_id.replace("-", "_")


def rtsp_publish_url(camera_id: str) -> str:
    return f"rtsp://{MEDIAMTX_RTSP_HOST}:{MEDIAMTX_RTSP_PORT}/{rtsp_path_for(camera_id)}"


# Reconnect / hygiene
RECONNECT_BACKOFF_INITIAL_S = 2
RECONNECT_BACKOFF_MAX_S = 30
# The mountpoint is created with "timeout_seconds": 180 (see janus_client.py) --
# that's Alarm.com's own server-side kill timer, observed live to actually fire at
# ~180s. Rotate comfortably before that so the RTSP feed never actually goes dark;
# a mismatch here (this used to be 900s) is exactly what caused the bridge to hang
# forever once the server-side timeout won the race.
#
# Every rotation causes a brief (~5-8s, measured live) gap in the RTSP feed --
# MediaMTX rejects a second concurrent publisher on the same path (confirmed:
# https://github.com/bluenviron/mediamtx/discussions/2765), so a fully gapless
# handoff isn't possible without a bigger dual-path rearchitecture. Given the
# gap self-heals every time, the practical lever is rotation *frequency*, not
# eliminating the gap -- so this sits close to the confirmed ~180s server limit
# (15s margin, matching FRAME_SILENCE_TIMEOUT_S) rather than the much more
# conservative 120s originally chosen, cutting how often the gap happens by
# roughly a quarter.
SESSION_MAX_LIFETIME_S = 165
# If no frame arrives for this long, treat the session as dead and reconnect --
# this must fire well before SESSION_MAX_LIFETIME_S in the worst case (e.g. the
# camera goes offline mid-session), and must be short enough that a live view
# doesn't appear frozen for long before recovering.
FRAME_SILENCE_TIMEOUT_S = 15
WEBRTCUP_TIMEOUT_S = 20
FIRST_FRAME_TIMEOUT_S = 20
JANUS_KEEPALIVE_INTERVAL_S = 25     # Janus GC's idle sessions ~60s server-side; keep well under that
