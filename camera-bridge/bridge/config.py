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
# aiohttp's per-request access log for the control endpoint, opt-in. Off by
# default on purpose: every request is loopback polling from Home Assistant,
# and the routine INFO "aiohttp.access" lines were measured live at ~27% of all
# log output -- against Docker's capped json-file rotation window (10m x 3,
# docker-compose.yml) that is enough to age real warnings out of the retained
# history within days. Failures still log through the route handlers
# themselves, so nothing diagnostic is lost; set true only when debugging the
# control API's request flow itself.
CONTROL_ACCESS_LOG = os.environ.get("CONTROL_ACCESS_LOG", "false").strip().lower() in (
    "1", "true", "yes", "on")

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

# --- Adaptive frame rate (camera_session.RateWatcher) ------------------------
# The frame rate is a DEVICE SETTING, not a property of the camera discovered
# once: the live-video resolution select is a Home Assistant entity, so it can
# be changed from HA, from the TELUS app or from the portal with no restart and
# no signal to this bridge. ffmpeg is told the rate once, on the command line
# (`-framerate`, which is the OUTPUT's constant frame rate, plus `-g` for a ~1s
# GOP), so a stale value re-rates the published stream for as long as the
# process lives -- duplicating frames when it is too high (measured live: a
# camera declared at 29fps logged "More than 1000 frames duplicated" within 2.5
# minutes) and DROPPING them when it is too low. The four constants below are
# what stop the correction for that becoming a rebuild loop, because every
# rebuild tears the publisher down and kicks every RTSP reader off the path.
#
# How long a session must have been delivering frames before any of them are
# measured. The first batch of a session is taken from the first ten frames and
# is demonstrably unreliable -- a rotation's buffered frames arrive in a burst
# and have produced 544fps, 215fps and (live, on the doorbell) 62fps readings
# for cameras really running at 15 and 30. 10s is far past any plausible buffer
# drain while still leaving ~155s of every 165s session measurable.
RATE_RESAMPLE_SETTLE_S = 10.0
# Inter-frame deltas per settled measurement, taken as a median. 90 frames is 3s
# at 30fps and 15s at 6fps -- long enough that no single late or bunched frame
# moves the result, short enough for several independent measurements per
# session even on the slowest camera on this account.
RATE_RESAMPLE_FRAMES = 90
# The hysteresis band: how far a settled measurement has to sit from the rate
# ffmpeg was DECLARED with before it counts as a change rather than as jitter.
#
# ASYMMETRIC, deliberately, because the two errors are not equally bad and
# measurement noise is not symmetric either (the full reasoning, with the
# in-container numbers, is on camera_session._rate_moved):
#   * declaring too HIGH pads the output with duplicate frames -- wasteful, but
#     no content is lost;
#   * declaring too LOW makes ffmpeg DROP real frames -- 1201 written frames
#     encoded as 1002 in the sink's own measurements, 199 of them gone;
#   * burst-contaminated readings are biased HIGH, never low, so "the camera is
#     faster than we thought" is the claim an artifact makes.
# Both bands clear the jitter actually observed on this account: the same camera
# reading 6 and 7 across restarts (a 17% step) and a camera set to 15 delivering
# ~13.4 (11%) must not trigger anything, while the real setting changes are
# -53% (15 -> 7), +100% (15 -> 30) and -24% (the live 29 -> ~22).
RATE_CHANGE_TOLERANCE_DOWN = 0.20
RATE_CHANGE_TOLERANCE_UP = 0.35
# Floor on the gap between two rate-driven publisher rebuilds. A rebuild costs a
# visible glitch, so even a source that defeats the settle window, the median,
# the band AND the two-measurements-must-agree confirmation cannot cost more
# than one glitch per window. 300s is a little under two session rotations.
RATE_REBUILD_MIN_INTERVAL_S = 300
# --- the flap guard: a DELIVERED rate that moves with no setting change -------
# The two-batches-must-agree confirmation converges on a source that alternates
# faster than two settled batches (~12s), and on nothing slower. Observed live
# on this account: the 30fps camera HALVES its delivered rate under marginal
# light and comes back, dwelling minutes at each rate -- long enough for two
# settled batches to agree at both ends, so every dwell confirmed a "change"
# and the mechanism rebuilt the publisher each way at the minimum-interval
# floor, kicking every RTSP reader off the path every few minutes for as long
# as the light stayed marginal.
#
# The flapping SIGNATURE is a confirmed return to the very rate the last rate
# rebuild moved away from, this soon after it. A real second setting change to
# the old value inside half an hour is indistinguishable from that -- and
# resolving the ambiguity toward "flapping" is the safe direction, because the
# resolution below only ever holds the publisher at the HIGHER rate, which pads
# the output with duplicates (the cheap error, measured in ffmpeg_sink's table)
# rather than dropping real frames.
RATE_FLAP_WINDOW_S = 30 * 60
# How long the flap memory lives past the last settled sighting of the flap's
# HIGH rate. While the source keeps visiting the high rate the memory keeps
# renewing, and confirmed changes down to the flap's low rate are refused: the
# publisher stays at the high rate, one resolution instead of a rebuild per
# dwell. A REAL setting change to the low rate stops the high sightings, the
# memory expires after this long of them, and the change is applied -- late,
# and deliberately so: an hour of duplicate padding is cheaper than a night of
# reader-kicking rebuilds, and nothing is lost in the meantime.
RATE_FLAP_HOLD_S = 60 * 60
JANUS_KEEPALIVE_INTERVAL_S = 25     # Janus GC's idle sessions ~60s server-side; keep well under that

# --- EXPERIMENT: ask the Janus mountpoint for a downstream AUDIO track ------
# OFF BY DEFAULT. For a camera it applies to, janus_client.py adds the audio
# counterparts of the video keys ("audio"/"audioport"/"audiopt"/"audiortpmap")
# to the streaming plugin's mountpoint-create body, and what comes back shows up
# in the offer the gateway sends, logged per session as "janus offer media: ...".
#
# The first question this asked -- will alarm.com's Janus gateway serve a
# downstream audio track at all -- is ANSWERED: it will, offering
# `audio(port=9,opus) video(port=9,H264,rtx)` with the audio m-line FIRST. The
# question that is still open is per-camera and is about the VIDEO: whether a
# given camera keeps delivering frames while an audio m-line is in the offer
# (see the per-camera note below).
#
# Requesting audio is only half of it: CONSUMING what comes back is a second,
# separately-gated step (JANUS_CONSUME_AUDIO below). With this knob on but that
# one off, an offered audio track is still drained and discarded (see
# janus_client.on_track) and the RTSP feed still carries no sound -- this knob
# only changes what is asked for and what the log reports. (Two-way audio TO the
# doorbell is a separate, working path -- see TALKDOWN_ENABLED above.)
#
# It is safe to leave on ONLY because of the fallback in janus_client.py: if
# the create-with-audio is rejected, the create is immediately retried once
# with the byte-identical video-only body this bridge has always sent. A
# rejected experiment therefore never raises, never counts a consecutive
# session failure, and never reaches camera_session.py's
# MAX_CONSECUTIVE_SESSION_FAILURES container-restart escape hatch.
#
# PER-CAMERA, not a plain on/off, because the blast radius has to be
# selectable. Run live against both cameras on this account, the gateway offered
# audio to each of them; the doorbell went on streaming at its usual rate, while
# the slower camera (6fps natively, and slow on unrelated paths) stopped
# producing a first frame at all and reconnected in a loop until the request was
# turned off. That the audio m-line CAUSED the second one is unproven -- it is a
# timeout, not a rejection, on a camera that is slow everywhere -- which is
# exactly why the choice must not be all-or-nothing: a global flag forces
# abandoning the experiment or breaking a working camera to keep it, so the knob
# names the cameras it applies to instead. camera_session.py's first-frame
# timing (both the streaming line and the timeout message) is what separates
# "slower than FIRST_FRAME_TIMEOUT_S" from "not streaming at all" next time.
#
# Accepted forms (see janus_request_audio_for below):
#   unset / "false" / "0" / "no" / "off"   -- off for every camera (the DEFAULT;
#                                             the create body stays byte-identical
#                                             to what this bridge has always sent)
#   "true" / "1" / "yes" / "on" / "all"    -- on for every camera
#   "<id>[,<id>...]"                       -- on for exactly those camera ids,
#                                             matched exactly, whitespace tolerated
JANUS_REQUEST_AUDIO = os.environ.get("JANUS_REQUEST_AUDIO", "false").strip()

# Whole-value keywords. Deliberately matched against the WHOLE setting, never
# against one entry of a comma-separated list, so "all" can only ever mean
# "every camera" and never a camera literally called "all".
_JANUS_AUDIO_OFF_WORDS = ("", "false", "0", "no", "off")
_JANUS_AUDIO_ALL_WORDS = ("true", "1", "yes", "on", "all")


def janus_request_audio_mode(camera_id) -> str:
    """Which JANUS_REQUEST_AUDIO mode applies to one camera.

    One of:
      "off"      -- the knob is off everywhere
      "all"      -- the knob names every camera
      "listed"   -- the knob names camera ids and this is one of them
      "unlisted" -- the knob names camera ids and this is NOT one of them

    Read live off the module attribute (not captured at import) for the same
    reason TELUS_EMAIL/TELUS_PASSWORD are: config values here are reassignable
    at runtime, and the tests drive this by reassignment. `str()` also means a
    plain bool assigned here still resolves correctly.
    """
    raw = str(JANUS_REQUEST_AUDIO).strip()
    if raw.lower() in _JANUS_AUDIO_OFF_WORDS:
        return "off"
    if raw.lower() in _JANUS_AUDIO_ALL_WORDS:
        return "all"
    named = [token.strip() for token in raw.split(",")]
    return "listed" if str(camera_id) in named else "unlisted"


def janus_request_audio_for(camera_id) -> bool:
    """Should the mountpoint-create body for this camera ask for audio?

    Single source of truth for BOTH places janus_client.py consults the knob --
    building the create body, and gating the video-only fallback check. They
    must not be able to disagree: a camera with audio off that still took the
    fallback path would spend a second create request per session for an
    experiment it is not running.
    """
    return janus_request_audio_mode(camera_id) in ("all", "listed")


# --- Consume the downstream AUDIO track and publish it on the RTSP path ------
# OFF BY DEFAULT, and strictly downstream of JANUS_REQUEST_AUDIO above: this
# knob decides what happens to a track that was already asked for and offered.
# For a camera it applies to, janus_client.py PUMPS the offered audio track
# instead of draining it, and camera_session.py gives the ffmpeg publisher a
# second (audio) input so the same RTSP path carries video + AAC audio. A camera
# it does not apply to keeps exactly today's behaviour: the track is drained and
# discarded, and the published stream is video-only.
#
# CONSUMING REQUIRES REQUESTING. A camera named here but NOT named by
# JANUS_REQUEST_AUDIO would never be offered an audio track at all, so consuming
# it is meaningless -- and building an audio-enabled ffmpeg publisher for a
# stream that can never carry audio is worse than meaningless: ffmpeg would sit
# on a second input that only ever receives the silence fill. That combination
# is therefore reported once per camera and treated as OFF (see
# janus_consume_audio_for), rather than being silently accepted and doing
# nothing visible.
#
# Same three value forms and the same whole-value keyword rule as
# JANUS_REQUEST_AUDIO:
#   unset / "false" / "0" / "no" / "off"   -- off for every camera (the DEFAULT;
#                                             offered audio is drained exactly as
#                                             it has always been)
#   "true" / "1" / "yes" / "on" / "all"    -- on for every camera that also has
#                                             audio REQUESTED
#   "<id>[,<id>...]"                       -- on for exactly those camera ids,
#                                             matched exactly, whitespace tolerated
JANUS_CONSUME_AUDIO = os.environ.get("JANUS_CONSUME_AUDIO", "false").strip()

# One warning per camera for the CONSUME-without-REQUEST combination. Module
# state rather than a per-call log, because janus_consume_audio_for is consulted
# on a path that runs once per camera start-up but could easily be moved onto a
# per-session path later; a misconfiguration must be loud once, not every ~165s.
_CONSUME_WITHOUT_REQUEST_WARNED: set = set()


def janus_consume_audio_mode(camera_id) -> str:
    """Which JANUS_CONSUME_AUDIO mode applies to one camera.

    One of "off" / "all" / "listed" / "unlisted", with exactly the same meanings
    and the same whole-value keyword rule as janus_request_audio_mode -- the two
    knobs deliberately share their value grammar so an operator only has to learn
    it once. Read live off the module attribute for the same reason.

    This reports what the KNOB says in isolation. Whether audio is actually
    consumed also depends on it having been requested; janus_consume_audio_for
    is the answer to that question.
    """
    raw = str(JANUS_CONSUME_AUDIO).strip()
    if raw.lower() in _JANUS_AUDIO_OFF_WORDS:
        return "off"
    if raw.lower() in _JANUS_AUDIO_ALL_WORDS:
        return "all"
    named = [token.strip() for token in raw.split(",")]
    return "listed" if str(camera_id) in named else "unlisted"


def janus_consume_audio_for(camera_id) -> bool:
    """Should this camera's offered audio track be consumed and published?

    Single source of truth for every consumer of the decision -- camera_session
    passes the answer down into JanusCameraSession rather than letting the
    session re-read the knob, so the component that builds the audio-enabled
    ffmpeg publisher and the component that decides whether to pump or drain the
    track cannot possibly disagree about one camera.

    Degrades to False (with a one-time warning) when consumption is asked for
    without JANUS_REQUEST_AUDIO naming the same camera: there is no audio track
    to consume in that configuration, and quietly returning True would build an
    audio input ffmpeg only ever receives silence on.
    """
    if janus_consume_audio_mode(camera_id) not in ("all", "listed"):
        return False
    if not janus_request_audio_for(camera_id):
        key = str(camera_id)
        if key not in _CONSUME_WITHOUT_REQUEST_WARNED:
            _CONSUME_WITHOUT_REQUEST_WARNED.add(key)
            logging.getLogger(__name__).warning(
                "%s: JANUS_CONSUME_AUDIO names this camera but JANUS_REQUEST_AUDIO "
                "does not, so no audio track is ever offered for it -- treating "
                "audio consumption as OFF for this camera; add it to "
                "JANUS_REQUEST_AUDIO as well to actually publish audio", key)
        return False
    return True


# Sample format assumed for the audio input while NO audio frame has been decoded
# yet. It is only ever a bootstrap value: camera_session takes the real format off
# the first decoded frame and rebuilds the publisher if this guess was wrong (the
# raw-PCM pipe guard in ffmpeg_sink.write_audio is what forces that). It matches
# what the gateway negotiates -- the offer is `audio(port=9,opus)`, and aiortc
# decodes Opus to 48 kHz stereo signed-16 -- so in practice the guess is right and
# no rebuild happens.
AUDIO_DEFAULT_FORMAT = ("s16", 48000, 2)
# Bound on the per-session decoded-audio queue in janus_client. Audio arrives in
# 20 ms frames, so this is ~1s of buffer: enough to ride out an encode hiccup,
# short enough that a stalled consumer drops audio (and says so) instead of
# growing without limit, exactly as the video queue does.
AUDIO_QUEUE_MAXSIZE = 50
# How long the per-session audio pump waits for a decoded frame before topping the
# publisher up with silence. This is NOT a failure timeout -- nothing is declared
# dead, no session is failed. It exists because the ffmpeg mux has two inputs and
# one output: the muxer can only emit a video packet once it holds audio covering
# that packet's timestamp, so the audio input has to stay AHEAD of the video or
# ffmpeg stops reading the video pipe altogether and the camera drops frames.
#
# This is therefore paced against ffmpeg_sink._AUDIO_PRIME_MS (100 ms), which is
# the lead the pipe is primed with: the lead decays by elapsed real time between
# top-ups, so the cadence must stay comfortably UNDER it. Half of it leaves ~50 ms
# of lead in hand for scheduling jitter.
#
# It shipped at 0.5, which is five times the lead, and that is the whole of E60:
# for 400 ms of every 500 ms cycle the audio covered nothing past the video, and
# ffmpeg stopped reading the video pipe until the next top-up arrived. Measured in
# the deployed container (900 frames per 60s run at 15fps, 1440x1440 -> 960x960,
# h264_vaapi, timing the write_frame that feeds the video pipe):
#
#   cadence      write_frame p50/p90/p99 (ms)     frames dropped behind a stall
#   video-only         1.81 / 1.94 / 2.87                 0 of 900
#   0.5  (the defect)  1.80 / 231.6 / 297.1             120 of 900 (13.3%)
#   0.1  (= the lead)  1.90 / 202.8 / 297.1             162 of 900 (18%)
#   0.08               1.81 / 1.97 / 2.27                 0 of 900
#   0.05 (this)        1.84 / 2.03 / 3.56                 0 of 900
#   0.025              1.80 / 2.04 / 3.44                 0 of 900
#
# The cliff is at the lead, not at any particular write size -- 0.1 hands ffmpeg
# a fifth as much per write as 0.5 and stalls MORE frames, because it is the
# moment the lead reaches zero that stops the video pipe. Below the lead the
# stalls disappear entirely, and going further down (0.025) buys nothing but
# wakeups. Real decoded audio arrives in ~20 ms frames and is written as it
# arrives, so it satisfies this on its own; the top-up only has to cover the
# quiet, which on a doorbell is nearly all of the time.
AUDIO_SILENCE_TOPUP_S = 0.05
# How much audio the per-session pump may write in one uninterrupted pass before
# it yields to the event loop.
#
# The pump drains a queue that holds up to AUDIO_QUEUE_MAXSIZE frames (~1s), and
# every frame costs an executor round trip to convert to PCM -- the SAME default
# executor write_frame's per-frame pixel copy uses. Draining a backlog in one
# unbroken run therefore lands a burst of executor work in front of the video
# path at exactly the moment it is already behind. Yielding every ~100 ms of
# audio bounds that.
#
# It is a YIELD, not a real-time pace, and the difference is measured: audio that
# runs BEHIND the video is what stalls the video pipe (see AUDIO_SILENCE_TOPUP_S
# above), so a backlog must be drained as fast as the loop allows, not spoon-fed
# at real time. Audio that runs AHEAD is harmless by comparison -- the same rig
# fed audio at twice real time showed no video stall at all.
AUDIO_PUMP_MAX_BURST_S = 0.1

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
# E10 -- runtime reachability instead of a blind walk down that order.
# The order above is a PREFERENCE, not a reachability claim: `direct`/`local` are
# LAN addresses that simply do not answer when the bridge is off the doorbell's
# LAN, and on this install `public` is the site's OWN WAN address (hairpin NAT
# may or may not loop it back). Walking the list blindly still "works" on-LAN --
# the first entry is the right one -- but off-LAN it stalls on a TCP connect to
# an unreachable RFC1918 address for however long the OS takes to give up, with
# the talk request (and the per-camera lock) held the whole time.
#
# So: probe the candidates CONCURRENTLY with a plain TCP connect and take the
# first one in preference order that answers. A probe is a bare connect --
# no TLS, no HTTP, nothing written -- so it cannot disturb the audio path.
# Set to 0 to disable probing and restore the pure static walk.
TALK_ENDPOINT_PROBE_TIMEOUT_S = float(os.environ.get("TALK_ENDPOINT_PROBE_TIMEOUT_S", "1.0"))
# Static override for when detection is wrong (a firewall that drops the probe
# but passes the real PUT, an endpoint that answers but cannot carry audio).
# One of the TALK_PUT_ENDPOINT_ORDER keys; empty = auto-detect. An override that
# the doorbell record does not carry a URL for is IGNORED with a warning rather
# than failing the talk -- a bad env var must not silence the doorbell.
TALK_PUT_ENDPOINT = os.environ.get("TALK_PUT_ENDPOINT", "").strip().lower()
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
# E7 + E11 -- cheap local recovery, tried BEFORE a full renegotiation.
# Renegotiation (above) is the heavy hammer: it tears down the MWS session,
# re-runs GetDoorbellCameraList and re-arms the mic path -- three round trips of
# dead air -- and it is bounded, so spending one on a problem that did not need
# it can cost the talk later. Two failures do NOT need new credentials:
#   * a dropped keep-alive connection (E11) -- the socket died, the session did
#     not; a new socket fixes it;
#   * a 401 the Digest middleware could not recover (E7) -- e.g. a rotated nonce
#     the middleware's own single retry did not clear, or a 401 carrying no
#     WWW-Authenticate for it to re-challenge from. A fresh HTTP session means a
#     fresh Digest handshake (and an `nc` counter restarting at 1) on the SAME
#     per-session password.
# Both are retried in place, keeping the credentials, before escalating. The
# counter is CONSECUTIVE -- any successful chunk resets it -- and the safety
# deadline is deliberately NOT extended for a reconnect (unlike a renegotiation),
# so a link that blips forever still terminates. 0 disables and every failure
# escalates straight to renegotiation, exactly as before this existed.
TALK_MAX_RECONNECTS = 2
# --- E5: streaming encode --------------------------------------------------
# Feed the PUT loop from ffmpeg's stdout as it decodes, instead of waiting for
# the whole clip to be encoded into RAM first. Cuts latency-to-first-audio (the
# doorbell starts speaking after one chunk is ready, not after the entire file
# is transcoded) and bounds encoded-audio memory at the lookahead below rather
# than at the whole clip.
#
# Kept as a switch because it restructures the loop that drives a LIVE audio
# path: if it ever misbehaves in the field, TALK_STREAMING_ENCODE=false in the
# bridge .env restores the buffer-then-send behaviour with no code change. Both
# modes run the same send/pacing loop and are covered by the same tests.
TALK_STREAMING_ENCODE = os.environ.get("TALK_STREAMING_ENCODE", "true").strip().lower() in (
    "1", "true", "yes", "on")
# How far ahead of the send schedule the encoder is allowed to run. This is what
# makes streaming safe: ffmpeg decodes ~100x faster than real time, so without a
# bound it would just re-buffer the whole clip; with it, ffmpeg fills the pipe,
# blocks, and is drained at playback rate. It also absorbs decode jitter, so a
# hiccup in ffmpeg never starves the send schedule (which WOULD be audible).
# Approximate -- the OS pipe buffer sits underneath it and adds a little more.
# Anything at or under this length is fully encoded before playback ends, so the
# clip's true duration is known and reported exactly as in buffered mode.
TALK_ENCODE_LOOKAHEAD_S = 30
# A read from the encoder that takes longer than this means ffmpeg is wedged
# (not merely slow -- it is normally hundreds of chunks ahead). Fails the talk
# rather than letting the doorbell run dry with the session held open.
TALK_ENCODE_READ_TIMEOUT_S = 15
# --- E6: prebuffer measurement / adaptation --------------------------------
# TALK_PREBUFFER_MS above was tuned BY EAR against the real speaker, so it is
# not adjusted automatically by default. What is always on is the MEASUREMENT:
# every talk reports observed PUT round-trip latency (p50/p95/max) and how many
# chunks missed their scheduled slot, so a future tuning has data instead of a
# guess. See the result dict / GET /control/metrics.
#
# The adaptation itself is opt-in and deliberately one-directional: it may only
# RAISE the lead, never lower it below the tuned value. A larger lead cannot
# cause an underrun -- it only delays how quickly a stop is heard and how much
# audio sits buffered at the doorbell -- whereas a smaller one can, and nothing
# available offline justifies going below what was tuned by ear.
TALK_PREBUFFER_ADAPTIVE = os.environ.get("TALK_PREBUFFER_ADAPTIVE", "false").strip().lower() in (
    "1", "true", "yes", "on")
# Ceiling on the adaptive lead. Also the ceiling on how much audio the doorbell
# may be holding when a stop arrives, which is why it is not larger.
TALK_PREBUFFER_MAX_MS = 2000
# --- E12: pin the doorbell's self-signed TLS certificate --------------------
# The doorbell serves the PUT endpoint over HTTPS with a SELF-SIGNED cert, so
# ordinary CA verification cannot apply and the transport historically ran with
# verification off entirely. Pinning the certificate itself restores an identity
# check without a CA.
#
# The value below is the SHA-256 of the DER certificate OBSERVED ON ONE DEVICE
# (an ADC-VDB750 on this install). It is NOT a vendor-wide constant: a firmware
# update, a factory reset or a replacement unit can all legitimately present a
# different certificate. That is exactly why the default mode is "warn".
TALK_DEVICE_CERT_SHA256 = os.environ.get(
    "TALK_DEVICE_CERT_SHA256",
    "1ac386f7d20abbf824e25a4e0fee2a2b8472e9f5fd2d5ace6c565fac672a89ad").strip().lower()
# How a certificate mismatch is handled:
#   "warn"    (default) -- connect exactly as before and LOG loudly, with both
#                fingerprints, if the device's certificate is not the pinned one.
#               Detection with no chance of silencing a working doorbell: a
#               firmware update cannot break talkdown, it just prints the new
#               fingerprint to paste into TALK_DEVICE_CERT_SHA256.
#   "enforce" -- a mismatch aborts the connection. Use once the pin is known
#               good for YOUR device; re-pin after any firmware update.
#   "off"     -- no certificate check at all.
# With no pin configured (TALK_DEVICE_CERT_SHA256=""), "warn" and "enforce" both
# degrade to learn-and-report: the observed fingerprint is logged once so it can
# be adopted, and the talk proceeds.
TALK_TLS_PIN_MODE = os.environ.get("TALK_TLS_PIN_MODE", "warn").strip().lower()
# Which endpoint kinds the pin applies to. The device's own certificate is what
# was fingerprinted, so the pin is only meaningful on endpoints that terminate AT
# the device -- direct/local (LAN) and public (the same device via the site's WAN
# address). The "cloud" endpoint is the VENDOR RELAY on a different host with its
# own, CA-signed certificate; pinning the doorbell's fingerprint there would
# break WAN talkdown outright.
TALK_TLS_PIN_ENDPOINT_KINDS = ("direct", "local", "public")
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
