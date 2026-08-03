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
              f"(check secrets.env in the camera-bridge directory)")
# NOTE: these two names are intentionally mutable module attributes -- both
# control_server.py (live credential push from HA's reconfigure flow) and
# main.py's fallback-file watcher reassign them at runtime, and telus_auth.py
# reads c.TELUS_EMAIL/c.TELUS_PASSWORD lazily inside login() on every call, so
# a reassignment here takes effect on the next login() with no process restart.

MEDIAMTX_RTSP_HOST = "127.0.0.1"
MEDIAMTX_RTSP_PORT = int(os.environ.get("MEDIAMTX_RTSP_PORT", "8554"))

# Render node ffmpeg_sink.py hands to ffmpeg's -vaapi_device for hardware (QuickSync)
# H.264 encoding. Requires the host's /dev/dri to be passed through to this container
# (see docker-compose.yml) -- overridable since render node numbering isn't guaranteed
# stable across hosts/reboots if more than one GPU is present.
VAAPI_DEVICE = os.environ.get("VAAPI_DEVICE", "/dev/dri/renderD128")

# Which hardware path ffmpeg_sink.py tries before falling back to software
# libx264: "vaapi" (Intel QuickSync or AMD, via /dev/dri -- this project's own
# deployment, unchanged default) or "nvenc" (NVIDIA, via nvidia-container-toolkit
# device injection -- see docker-compose.yml's commented-out NVIDIA section).
# "software" skips the hardware attempt entirely. Deliberately not "auto" --
# probing an unavailable backend still costs a real (if fast, ~0.75s) failed
# subprocess launch every reconnect, and this project only has Intel hardware
# to verify against; an explicit choice avoids silently trying (and failing)
# a backend nobody asked for on hosts with no GPU at all.
ENCODE_BACKEND = os.environ.get("ENCODE_BACKEND", "vaapi").lower()

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
# The control secret previously never rotated automatically (manual only --
# delete the file and restart). HA always reads it fresh off disk per-request
# (bridge_client.py has no in-memory caching on that side), so an in-place
# atomic rewrite here is picked up on HA's very next poll with zero downtime --
# no dual-secret grace period needed. 30 days is arbitrary but reasonable for
# a single-operator local deployment with no external exposure.
CONTROL_SECRET_ROTATE_INTERVAL_S = 30 * 24 * 60 * 60


# Credentials for the bundled MediaMTX, generated once by entrypoint.sh and shared
# with it through /shared/rtsp_password (see mediamtx.yml's authInternalUsers).
# Empty only if this process is somehow started outside that entrypoint, in which
# case the URLs below stay in their old credential-less form rather than emitting
# something guaranteed to be rejected.
RTSP_USER = "bridge"
RTSP_PASSWORD = os.environ.get("RTSP_PASSWORD", "")


def rtsp_path_for(camera_id: str) -> str:
    return "telus_" + camera_id.replace("-", "_")


def rtsp_publish_url(camera_id: str) -> str:
    # Used both as ffmpeg's publish target and as the stream URL reported to Home
    # Assistant via /control/cameras, so the credentials only have to be threaded
    # through this one place. The password is hex, so it needs no URL-escaping.
    auth = f"{RTSP_USER}:{RTSP_PASSWORD}@" if RTSP_PASSWORD else ""
    return f"rtsp://{auth}{MEDIAMTX_RTSP_HOST}:{MEDIAMTX_RTSP_PORT}/{rtsp_path_for(camera_id)}"


# Reconnect / hygiene
RECONNECT_BACKOFF_INITIAL_S = 2
RECONNECT_BACKOFF_MAX_S = 30
# Absolute ceiling on a single Retry-After-driven sleep, shared by main.py's
# startup login retry and camera_session.py's reconnect loop -- both of which
# must sleep for Cloudflare's own Retry-After rather than the fixed backoff
# below, since the short fixed schedule just re-triggers the same rate-limit
# window instead of ever clearing it.
#
# This is a sanity clamp on a garbage/absurd header value, NOT a second-guess
# of a reasonable one. It used to be 300s and was applied as a plain min()
# against the server's value, which meant any window longer than 5 minutes had
# us coming back *sooner than the server explicitly asked* -- precisely the
# behaviour that re-enters a rate-limit window instead of clearing it (see
# telus_auth.RateLimited and the multi-hour outage documented on main.py's
# _login_with_retry). Observed real values are ~101s, so 30 minutes sits far
# past anything legitimate while still bounding an accidental
# "Retry-After: 86400".
RATE_LIMIT_RETRY_MAX_S = 30 * 60
# Small margin added on top of the server's stated wait, so a clock/rounding
# skew of a second or two can't land the retry just inside the window again.
RATE_LIMIT_RETRY_MARGIN_S = 2


def rate_limit_sleep_s(retry_after_s: float) -> float:
    """How long to sleep for a RateLimited exception's Retry-After. Single
    source of truth for both call sites so the two can't drift apart -- they
    previously each open-coded the same min()/margin expression."""
    return min(max(float(retry_after_s), 0.0), RATE_LIMIT_RETRY_MAX_S) + RATE_LIMIT_RETRY_MARGIN_S


# If a single camera's session fails this many times in a row with no
# successful frame batch in between, camera_session.py gives up on in-process
# retrying and hard-exits the whole bridge (os._exit) so Docker's
# `restart: unless-stopped` policy brings up a fully clean process -- see the
# comment at that call site for why. With the backoff sequence above
# (2,4,8,16,30...), 5 consecutive failures is at least ~60s of continuous
# failure before this fires, comfortably past what the backoff alone absorbs
# for an ordinary transient blip.
MAX_CONSECUTIVE_SESSION_FAILURES = 5

# --- Restart-frequency guard for that escape hatch (see restart_guard.py) ---
# The hatch is right for a one-off local wedge and wrong for a sustained
# upstream failure: every failure looks identical from inside the reconnect
# loop, so with `restart: unless-stopped` behind it, an alarm.com/Cloudflare
# outage turned the hatch into an unbounded crash-restart loop (measured on
# this deployment: 89 restarts in 7 days, one stretch looping tightly for 14
# minutes) that re-ran a full SSO login against the very rate limiter it was
# reacting to. The guard below throttles that without removing the hatch.
#
# State file: the hatch kills the process, so no in-memory counter can survive
# to tell a fresh process it's the Nth restart in a row -- the count has to
# live on disk. /shared is the one writable directory that outlives both a
# process exit and a container recreate (bind mount, see docker-compose.yml).
RESTART_GUARD_STATE_PATH = f"{SHARED_DIR}/restart_guard.json"
# Rolling window over which hatch exits are counted; anything older is dropped,
# so genuine one-off wedges weeks apart never accumulate into a throttle.
RESTART_GUARD_WINDOW_S = 60 * 60
# Exits allowed inside that window before throttling starts. A real wedged
# resource is cleared by the very first restart and doesn't come back minutes
# later, so the first few exits stay immediate -- keeping the fast recovery the
# hatch was built for -- and only a *repeating* pattern (the signature of a
# sustained upstream failure) gets held back.
RESTART_GUARD_MAX_EXITS = 3
# Past that threshold, sleep before exiting: 60s, 120s, 240s ... capped. The
# sleep runs in-process, so Docker cannot restart the container until it
# finishes -- that is the hard floor on restart frequency. The cap is well past
# the ~101s Retry-After Cloudflare hands out (telus_auth.RateLimited), so a
# sustained upstream failure stops re-hammering the limiter that caused it.
RESTART_GUARD_BACKOFF_INITIAL_S = 60
RESTART_GUARD_BACKOFF_MAX_S = 15 * 60

# Bounds session.close()'s call into aiortc's pc.close(), which has no timeout
# of its own and is documented (lifecycle.py) as hangable under certain ICE
# states. Comfortably above what a clean close takes (sub-second, normally)
# but short enough that a hung close still gets counted as a failure well
# within the ~60s the escape hatch above needs to fire.
SESSION_CLOSE_TIMEOUT_S = 5
# The mountpoint is created with "timeout_seconds": 180 (see janus_client.py) --
# that's Alarm.com's own server-side kill timer, observed live to actually fire at
# ~180s. Rotate comfortably before that so the RTSP feed never actually goes dark;
# a mismatch here (this used to be 900s) is exactly what caused the bridge to hang
# forever once the server-side timeout won the race.
#
# A fully gapless publisher *handoff* isn't possible -- MediaMTX rejects a
# second concurrent publisher on the same path (confirmed:
# https://github.com/bluenviron/mediamtx/discussions/2765) -- so instead the
# one publisher is kept alive across rotations and the gap is bridged by
# repeating the last frame (see camera_session.py / FRAME_HOLD_MAX_S below).
# This still sits close to the confirmed ~180s server limit (15s margin,
# matching FRAME_SILENCE_TIMEOUT_S) to keep freeze-bridged gaps infrequent.
SESSION_MAX_LIFETIME_S = 165
# Upper bound on how long camera_session.py keeps repeating the last real frame
# into the RTSP publish while a Janus session is rotated/reconnected, measured
# from when the last real frame arrived. Rotation gaps are ~2-8s, so this only
# ever expires when the camera is genuinely gone (e.g. the 2026-07-17 doorbell
# outage) -- at which point the feed is allowed to go dark rather than keep
# presenting a stale frozen image as live.
FRAME_HOLD_MAX_S = 60
# If no frame arrives for this long, treat the session as dead and reconnect --
# this must fire well before SESSION_MAX_LIFETIME_S in the worst case (e.g. the
# camera goes offline mid-session), and must be short enough that a live view
# doesn't appear frozen for long before recovering.
FRAME_SILENCE_TIMEOUT_S = 15
# Upper bound on the larger side of the RTSP-published H.264 frame. Some TELUS
# cameras (the doorbell, in particular) publish natively at 1440x1440 -- fine
# for the RTSP/WebRTC pipeline itself, but decoding that continuously turned
# out to exceed what a modest kiosk box's browser/iGPU pipeline can sustain
# alongside a second camera: confirmed live 2026-07-26/27, the doorbell's
# video element would keep receiving RTP (go2rtc's own consumer stats kept
# climbing) while the picture itself froze for minutes at a time -- a
# decoder/paint stall on the viewer, not a network one. Downscaling at the
# encode step cuts the actual pixel throughput any viewer's decoder has to
# sustain, at the cost of some detail -- cheaper and more reliable than
# relying on every viewing device to keep up with native resolution.
MAX_ENCODE_DIMENSION = 960
WEBRTCUP_TIMEOUT_S = 20
FIRST_FRAME_TIMEOUT_S = 20
JANUS_KEEPALIVE_INTERVAL_S = 25     # Janus GC's idle sessions ~60s server-side; keep well under that

# How often to re-poll TELUS for the camera list after startup, so a camera
# added to the account later gets picked up (and streaming started
# automatically) without a container restart. Existing camera tasks are never
# torn down by this poll, even if a camera briefly stops appearing in a
# response -- only additions are acted on, to avoid killing a working stream
# over a transient API hiccup.
CAMERA_DISCOVERY_INTERVAL_S = 300
