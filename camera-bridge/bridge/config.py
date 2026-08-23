import logging
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

# Persisted state for mobile_auth.MobileSession, on the same /shared bind mount
# as the credentials fallback above (survives a container recreate). Holds two
# things minted off the website session: a stable synthetic MobileDeviceUid --
# generated once so every restart does not register a fresh alarm.com "device" --
# and the rolling SeamlessLoginToken (slt) from the last MWS login, so a restart
# does a single silent SeamlessLoginNew POST instead of the full Cloudflare-fronted
# SSO -> LoginAsLogin bootstrap. Never holds the SessionToken itself (short-lived,
# always re-minted in memory). See mobile_auth.py.
MOBILE_AUTH_STATE_PATH = f"{SHARED_DIR}/mobile_auth.json"
# The control secret previously never rotated automatically (manual only --
# delete the file and restart). HA always reads it fresh off disk per-request
# (bridge_client.py has no in-memory caching on that side), so an in-place
# atomic rewrite here is picked up on HA's very next poll with zero downtime --
# no dual-secret grace period needed. 30 days is arbitrary but reasonable for
# a single-operator local deployment with no external exposure.
CONTROL_SECRET_ROTATE_INTERVAL_S = 30 * 24 * 60 * 60

# How long /control/clip serves a re-minted download URL from its per-event
# cache before re-fetching from TELUS. Kept well under alarm.com's own signed-URL
# lifetime (minutes) so a cached URL is always still valid when handed out, while
# collapsing a burst of taps on one notification into a single upstream request.
CLIP_URL_CACHE_TTL_S = 60


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


# --- Talkdown (two-way audio to the doorbell speaker) -----------------------
# On-demand, user-triggered talk path (POST /control/cameras/{id}/talk). It uses
# the legacy MOBILE MWS XML API (mobile.alarm.com) authed with an MWS SessionToken
# minted off the same website session (mobile_auth.py) -- the website video path
# is audio-less for the doorbell. This is NOT part of the always-warm pipelines
# and is deliberately excluded from restart_guard: a failed talk fails one request
# and never exits the process. The persisted mobile device-uid + rolling slt live
# in MOBILE_AUTH_STATE_PATH (defined above with the other /shared paths).
#
# ENABLED BY DEFAULT. Talkdown shipped off while it was unproven; it is now
# verified end to end against the doorbell (see docs/talkdown-fullduplex-protocol.md),
# so the control route is armed out of the box. It still only ever plays audio a
# caller explicitly asks it to play -- nothing speaks on its own.
# To disable, set TALKDOWN_ENABLED=false in the bridge's .env and recreate the
# container; the route then returns 403 (talkdown_disabled).
TALKDOWN_ENABLED = os.environ.get("TALKDOWN_ENABLED", "true").strip().lower() in (
    "1", "true", "yes", "on")
# Prefer the <webrtc> endpoint (aiortc, clean sendrecv Opus) over <srtp> (ffmpeg).
TALK_PREFER_WEBRTC = True
# --- Full-duplex talkdown (ADC-VDB750 doorbell) ----------------------------
# The VDB750 returns ONLY an <rtsp> (video + listen) endpoint from
# GetLiveVideoStream -- no <webrtc>/<srtp>. Its upstream ("talkdown") audio
# path is instead a chunked HTTP PUT of G.711 mu-law / 8 kHz / mono to the
# doorbell's own /api/PutAudioStream endpoint, HTTP-Digest-authed as user
# "vieweruser" with the per-session stream password. Discovery
# (GetDoorbellCameraList) + activation (StartFullDuplex) live in mws_client;
# the PUT transport is talk_fullduplex.py. This is the ONLY working talk path
# for THIS doorbell (proven end-to-end against a real capture); webrtc/srtp
# stay for other/newer ADC cameras. Prefer this path when the device
# advertises upstream audio (susa=true).
TALK_PREFER_FULL_DUPLEX = True
# Order to try the PutAudioStream endpoint variants. The bridge shares the
# doorbell LAN (VLAN 38), so the DIRECT/LOCAL LAN URL (duae/luae) is fastest
# and relay-free; public (puae) then cloud relay (uae, which carries a
# ?camera_id= routing token) are WAN fallbacks. Values are
# parse_doorbell_camera_list put_urls keys.
TALK_PUT_ENDPOINT_ORDER = ("direct", "local", "public", "cloud")
# Audio codec for the full-duplex PUT path.
# On THIS doorbell (ADC-VDB750, fw 02091) the PUT path is mu-law ONLY: L16
# (16-bit linear, skyL16pay/pt97) was tested over the PUT endpoint in both
# endiannesses and came out as noise -- this device decodes the body as
# mu-law regardless of Content-Type. This is a per-model/firmware limit, NOT
# alarm.com-wide: other models may support L16 (newer ADC cameras carry it
# via the webrtc/srtp path). So mu-law is the only working codec here; audio
# quality is handled by TALK_AUDIO_FILTER (below), not the codec. The codec
# plumbing (per-request "codec", primary/fallback) is kept so a working codec
# can be added per device later. Overridable per request via POST "codec".
TALK_CODEC = "mulaw"
TALK_CODEC_FALLBACK = "mulaw"
# ffmpeg audio-conditioning filter applied to ALL talkdown audio before it
# is encoded to the doorbell codec. The VDB750's small speaker clips and
# sounds harsh on raw full-range music/TTS, so by default we high-pass out
# the bass it can't reproduce, low-pass the harsh top end (warmer), gently
# compress the dynamics, and hard-limit below full scale (no clipping).
# Tuned by ear on the live doorbell. Set to "" to disable (pass audio
# through untouched, e.g. for already-conditioned sources).
# Talkdown wire tuning. One PUT carries TALK_CHUNK_MS of audio; the transport
# paces to that schedule, so it is both the chunk size and the granularity at
# which a stop request takes effect. 40ms matches what the app was observed
# sending (sizes are multiples of 10ms). Larger = fewer round trips and less
# overhead, but coarser stop response; smaller = more HTTP requests per second
# at the doorbell.
TALK_CHUNK_MS = 40
# How far ahead of real time the stream stays, so the doorbell always holds a
# jitter buffer. Delivering chunks exactly at playback time underruns into
# audible gaps; this lead is what makes playback continuous. Raising it costs
# proportionally more latency before a stop is heard.
TALK_PREBUFFER_MS = 500
# How long a GetDoorbellCameraList result may be reused across talks, saving
# that round trip (~0.3s of latency before audio starts) on back-to-back
# talks. Safe because the session it carries is NOT per-talk in practice --
# the doorbell was observed serving the same session port for 12+ hours
# across many talks -- and because a stale hit is self-correcting: the first
# PUT fails, which is exactly the case renegotiation handles, costing one
# chunk instead of an error. The cache is dropped on any talk failure and on
# every renegotiation, so a session known to be bad is never reused. Set 0 to
# disable and fetch fresh for every talk.
TALK_DOORBELL_CACHE_TTL_S = 60
# Session renegotiation for a talk that outlives its stream session.
# The doorbell's PutAudioStream URL, port and password are PER-SESSION. A long
# clip (or a network blip) can outlive one, and the failure is mid-stream: the
# PUTs start erroring with the audio half-played. Rather than abort, the
# transport asks its caller for a FRESH session and resumes from the exact byte
# offset it reached, so no audio is lost or repeated. Bounded so a persistently
# broken session fails the request instead of looping.
TALK_MAX_RENEGOTIATIONS = 2
# When the caller can determine a session expiry, renegotiate this many seconds
# BEFORE it rather than waiting for the first failed PUT -- a proactive swap
# costs one silent gap of a few hundred ms instead of a failed chunk plus a
# teardown/setup round trip.
TALK_RENEGOTIATE_MARGIN_S = 20
TALK_AUDIO_FILTER = ("highpass=f=250,lowpass=f=2800,"
                     "acompressor=threshold=-18dB:ratio=3:attack=5:release=150:makeup=2,"
                     "alimiter=level_out=0.5:limit=0.6")
# Bound on GetLiveVideoStream so a wedged MWS call fails the one request instead
# of pinning the per-camera talk lock.
TALK_STREAM_LOOKUP_TIMEOUT_S = 20
# Hard ceiling on a whole talk session (endpoint handshake + clip playback +
# teardown). Comfortably above talk_webrtc's own SESSION_CONNECT_TIMEOUT_S (25s)
# plus a short phrase; on overrun the session is cancelled and torn down.
TALK_SESSION_TIMEOUT_S = 60
# Inner ceiling handed to the SRTP (ffmpeg) client, kept under the session ceiling
# above so ffmpeg's own teardown runs before the outer wait_for would cancel it.
TALK_SRTP_TIMEOUT_S = 45
# Fetching a media_url (a loopback HA TTS / media-source URL): a small clip is
# quick, so a tight timeout keeps a slow/hung source from stalling the talk.
TALK_MEDIA_FETCH_TIMEOUT_S = 10
# Hard cap on fetched/read audio. A 10-30s TTS clip is well under this; the cap is
# enforced while streaming so an oversize body is never buffered whole.
TALK_MEDIA_MAX_BYTES = 64 * 1024 * 1024


# --- shared HTTP helpers ----------------------------------------------------
def parse_retry_after(value, default=60.0):
    """Seconds to wait from a Retry-After header value.

    RFC 9110 allows BOTH delta-seconds ("120") and an HTTP-date
    ("Wed, 21 Oct 2015 07:28:00 GMT"). Cloudflare currently sends delta-seconds,
    but a WAF/edge change to the date form would previously have fallen through
    to a fixed 60s -- exactly the "fixed short retry re-triggers the same window"
    behaviour that once turned one bad night into a multi-hour outage. Parse both,
    and LOG when falling back so it is distinguishable from a missing header.
    """
    if value is None:
        return default
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        from email.utils import parsedate_to_datetime
        from datetime import datetime, timezone
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except Exception:
        logging.getLogger(__name__).warning(
            "Unparseable Retry-After %r; using %.0fs", value, default)
        return default
