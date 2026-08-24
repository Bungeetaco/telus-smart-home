# telus-cam-bridge

Standalone Docker service that pulls live video from a TELUS Smart Home
(white-labeled Alarm.com) account and republishes it as RTSP, so Home
Assistant (or anything else) can consume the cameras as normal RTSP camera
entities instead of needing WebRTC/Janus support itself.

**This is not an official TELUS/Alarm.com product** -- it's a reverse-engineered
personal project (see the top-level README) with no upstream to pull updates
from. It's been made installable on a fresh box (see "Installing" below), but
treat this README and the comments in the source as the design doc, not a
maintained public package with support guarantees.

## Network requirement -- read this first

**This container, and your Home Assistant container, must both run with
`--network host`.** Janus/WebRTC negotiates a wide ephemeral UDP port range
for RTP that doesn't work through Docker's default bridge networking + NAT,
and the credential-sync control endpoint (see below) is loopback-only by
design, reachable from Home Assistant's container only because both share the
host's network namespace. If your Home Assistant install doesn't use host
networking (a default HA Container/Docker install typically uses bridge
networking with port mapping instead), this won't work as-is -- `install.sh`
checks for this up front and will tell you plainly rather than let you
discover it halfway through.

## Installing

```bash
cd telus-smart-home
./install.sh
```

Walks through the network pre-flight check, credentials, the shared
directory, and building/starting the container. See the top-level README for
what it does step by step. To recreate manually instead: `cp .env.example
.env`, `cp secrets.env.example secrets.env` (fill in real values -- **if your
password contains a literal `$`, escape it as `$$`**, see the comment in
secrets.env.example for why), then `sudo docker compose up -d --build`.

## Architecture

- **MediaMTX** (`/usr/local/bin/mediamtx`, static binary) runs inside the same
  container and does the RTSP serving. `mediamtx.yml` configures it minimally;
  `entrypoint.sh` generates the RTSP password (see "RTSP authentication"
  below), renders a runtime config with it, starts MediaMTX first and waits for
  its RTSP listener before starting the Python bridge.
- **The Python bridge** (`bridge/`) logs into TELUS's SSO
  (`telus_auth.py`, a from-scratch reimplementation of the OAuth/cookie dance
  -- no `pyalarmdotcomajax` dependency here, unlike the separate HA
  integration), enumerates cameras (`telus_rest.py`), and for each camera
  opens a Janus/WebRTC session (`janus_client.py`) and feeds decoded frames
  into an ffmpeg process that publishes them to MediaMTX over RTSP
  (`ffmpeg_sink.py`). `camera_session.py` owns the per-camera reconnect loop.
- Runs as a single Docker container (`telus-cam-bridge`), `--network host`,
  `--restart unless-stopped`, managed via `docker-compose.yml` (see
  "Installing" above and "Recreating the container" below).

## Hardware encode backends

`config.ENCODE_BACKEND` (env var `ENCODE_BACKEND`, see `.env.example`) picks
which encoder `ffmpeg_sink.py` tries before falling back to software libx264:

- **`vaapi`** (default) -- Intel QuickSync or AMD, via `/dev/dri`. This is
  what this project's own host actually runs and has verified live (hardware
  encode confirmed active in ffmpeg's own log, full pipeline exercised).
  Debian's ffmpeg package already includes both `intel-media-va-driver` and
  `mesa-va-drivers` in the image (see `Dockerfile`) -- which one actually
  gets used is decided by libva against whatever GPU is at the passed-through
  `/dev/dri` device, not by anything this project has to choose.
- **`nvenc`** -- NVIDIA, via `nvidia-container-toolkit` device injection (see
  `docker-compose.yml`'s commented-out NVIDIA section) instead of
  `/dev/dri`. `h264_nvenc` is already compiled into Debian's ffmpeg package
  (confirmed via `ffmpeg -encoders` inside this image), so no extra package
  install is needed for it -- only the host-side NVIDIA driver +
  `nvidia-container-toolkit`. **Not verified against real NVIDIA hardware by
  this project** (Intel-only host) -- treat it as implemented-per-ffmpeg's-own-
  documentation, not confirmed-live the way the VAAPI path is.
- **`software`** -- always libx264, skip the hardware attempt entirely.

Whichever backend is selected, an unavailable/broken device still falls back
to software automatically per-`start()` call (see `ffmpeg_sink.py`'s module
docstring) -- `ENCODE_BACKEND` only picks which hardware path is *attempted*
first.

## The 120s proactive session-rotation fix -- do not regress

Alarm.com kills a video session **server-side at ~180s**, unconditionally,
matching the `"timeout_seconds": 180` sent in the Janus mountpoint-create
request (see `janus_client.py`). This was a real production bug: earlier
versions of this bridge used far longer values and would go dark and hang
once the server-side timer won the race.

The current, tested, working fix (`config.py`'s `SESSION_MAX_LIFETIME_S = 165`
and the rotation loop in `camera_session.py`'s `run_camera_forever`):

- Proactively **rotates every camera session every 165s**, before Alarm.com's
  ~180s server-side kill, so the RTSP feed never goes dark waiting for a
  server-initiated teardown. (This was 120s in an earlier design that tore the
  ffmpeg publisher down along with each session; once the publisher was made to
  outlive rotations and the gap bridged by repeating the last frame, the margin
  could be tightened to 15s -- matching `FRAME_SILENCE_TIMEOUT_S` -- to keep
  those frame-hold gaps as infrequent as possible.)
- Independently detects a **dead session via frame silence**
  (`FRAME_SILENCE_TIMEOUT_S = 15`) -- if no frame arrives for 15s, the session
  is torn down and reconnected immediately, without waiting for the rotation
  deadline. This check runs unconditionally on every loop iteration
  (including when zero frames have ever arrived for that session), which is
  what makes it safe against a camera that stalls immediately.
- Bridges each rotation/reconnect gap by **repeating the last real frame** into
  the still-running publisher (`FRAME_HOLD_MAX_S = 60`), so readers see a brief
  freeze rather than a torn-down RTSP path. A genuinely offline camera stops
  being held after 60s and the feed is allowed to go dark honestly.

**If you touch `camera_session.py` or `janus_client.py`: read them in full
first.** The frame-silence check and the rotation deadline check are two
separate, deliberately-redundant safety nets -- don't collapse them, and don't
change `SESSION_MAX_LIFETIME_S` without re-verifying Alarm.com's actual
server-side timeout live (it's not documented anywhere; 180s was observed).

`tests/test_camera_session.py` holds all three nets to their behaviour against
scripted fake sessions, with no network and no ffmpeg, so a regression fails
there rather than surfacing days later as a frozen feed:

```bash
TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_camera_session.py
```

## RTSP authentication

**As of v1.4.0 the RTSP server requires credentials.** It previously ran on
MediaMTX's default internal user: anonymous *publish and read* on every path.
Binding to `127.0.0.1` is not a substitute for auth here -- this container and
Home Assistant's both run with `--network host`, so "loopback" means every
process and every other host-networked container on the box, and path names are
a pure function of the camera id (`config.rtsp_path_for`), so they are
guessable. Anything local could therefore read the camera feeds, or publish over
a path and replace the live feed with its own.

How it works, end to end:

- `entrypoint.sh` generates a random password on first start and persists it to
  `/shared/rtsp_password` (mode `0600`). It is reused on every later start; the
  bridge never rotates it on its own.
- MediaMTX gets it via `authInternalUsers` in `mediamtx.yml`, whose
  `__RTSP_PASSWORD__` placeholder `entrypoint.sh` substitutes into a runtime
  copy under `/tmp` (MediaMTX does not interpolate environment variables inside
  its config).
- The Python side reads the same value from the `RTSP_PASSWORD` environment
  variable that `entrypoint.sh` exports, and `config.rtsp_publish_url()` builds
  it into the URL used *both* as ffmpeg's publish target and as the `rtsp_url`
  reported over `/control/cameras` -- which is how Home Assistant's camera
  entities authenticate without anything being configured by hand.
- The credentials are redacted from the ffmpeg publish log line, since container
  logs are persisted by Docker's json-file driver.

To rotate: delete `/shared/rtsp_password`, restart this container, then restart
Home Assistant so it re-reads the URLs. To read a stream by hand:

```bash
ffplay "rtsp://bridge:$(sudo cat /var/lib/homeassistant/share/telus_cam_bridge/rtsp_password)@127.0.0.1:8554/telus_<camera_id>"
```

## Credentials

By default, read once at startup from `.env`
(`TELUS_EMAIL` / `TELUS_PASSWORD`, plus `MEDIAMTX_RTSP_PORT` and `LOG_LEVEL`).

**This bridge holds its own separate copy of the TELUS username/password from
the Home Assistant `telus_smarthome` integration.** They used to be
completely disconnected -- changing the password via HA's integration would
silently leave this bridge on the old password until someone noticed camera
streams failing. That gap is now closed by the control endpoint below.

### Control endpoint (credential push from Home Assistant)

`bridge/control_server.py` runs a small `aiohttp` HTTP listener bound to
`127.0.0.1:8199` (loopback only -- never exposed off the host; reachable from
the HA container purely because both containers use `--network host` and
therefore share the same loopback). It lets the HA integration's config-flow
reconfigure/reauth step push a live credential update into this
already-running process, with no container restart:

```
POST http://127.0.0.1:8199/control/credentials
Authorization: Bearer <contents of /shared/control_secret, stripped>
Content-Type: application/json
Body: {"username": "<telus email>", "password": "<telus password>"}

200 {"status": "applied"}    -- credentials swapped in, re-login succeeded
400 {"error": "malformed"}   -- bad/missing JSON, or empty username/password
401 {"error": "bad_secret"}  -- Authorization header missing or wrong
500 {"error": "internal"}    -- re-login with the new credentials failed
                                 (still applied in-process; existing
                                 reconnect loops keep retrying them)

GET http://127.0.0.1:8199/control/health   -- no auth, {"status": "ok"}
```

Credentials are swapped in by reassigning the module attributes
`config.TELUS_EMAIL` / `config.TELUS_PASSWORD` -- `telus_auth.py`'s
`TelusSession.login()` reads them lazily (as `c.TELUS_EMAIL`/`c.TELUS_PASSWORD`
module lookups) on every call rather than capturing them once, so a
reassignment takes effect on the very next `login()` with **no restart
needed**. This was deliberately kept as simple module-attribute mutation
rather than a bigger `CredentialStore`-style refactor -- there was nothing for
a refactor to buy here.

The shared secret is generated once (32 random bytes, hex-encoded) on first
start if it doesn't already exist, and written to
`/shared/control_secret` (mode `0600`) -- see "Shared directory" below.
A pre-existing file the bridge adopts instead of generating (restored from a
backup, or created under a permissive umask) is re-tightened to `0600` at
startup if it is group/world-readable, with a warning naming the old mode; if
the chmod fails (e.g. the file is owned by another uid on the shared mount) a
warning is logged and the secret is served as-is -- a permissions problem
never regenerates it, so the HA side keeps authenticating.
Rotating it is manual: delete the file and restart this container (it
regenerates); the HA side re-reads the file on its next credential push
attempt, since it doesn't cache the secret in memory across HA restarts.

Both `login()` call sites that existed before this endpoint (the initial
startup login, and the 401-triggered relogin inside
`get_video_source_with_relogin`) plus the new control-endpoint login and the
fallback-file watcher's login are all serialized through one shared
`asyncio.Lock` (`login_lock` in `main.py`). `TelusSession.login()` clears the
entire cookie jar with no locking of its own -- without this lock, two
concurrent `login()` calls (e.g. a 401-triggered relogin racing the new
control endpoint) could interleave and leave the session cookie-jar in a
mixed, broken state.

### Shared directory (`/shared` in this container)

Bind-mounted from the host at `/var/lib/homeassistant/share/telus_cam_bridge`
-- the same physical host directory the `homeassistant` container already
mounts read-write at `/share/telus_cam_bridge`, so both containers can read
and write these files without any new host path or supervisor config change.

- `control_secret` -- generated by this bridge on first start (see above).
  The HA integration reads it to authenticate its credential push.
- `rtsp_password` -- generated by `entrypoint.sh` on first start, mode `0600`.
  Read back on every later start and built into the RTSP URLs this bridge
  publishes with and reports over `/control/cameras`. See "RTSP
  authentication" above.
- `restart_guard.json` -- `{"exits": [<unix ts>, ...]}`, written by this bridge
  every time it deliberately ends its own process (camera_session.py's
  escape hatch, or a terminal startup-login failure). It lives here purely
  because it has to outlive the process it is counting; nothing else reads it.
  More than a few entries inside an hour means the bridge stops exiting
  immediately and holds itself up first, so the container can't restart-loop
  against an upstream outage -- see `bridge/restart_guard.py`. Deleting it is
  harmless (it just resets the count).
- `credentials.json` -- `{"username", "password", "updated_at"}`, written by
  the HA integration on **every** successful credential update (reconfigure or
  reauth), regardless of whether the live push to `/control/credentials`
  succeeded. This is the fallback path:
  - **On startup**, this bridge adopts `credentials.json` over `.env` if
    present (`_load_fallback_credentials_if_present` in `main.py`) -- so a
    freshly recreated/restarted container picks up the latest credentials
    even if it missed the live push entirely (e.g. it was down at the time).
  - **While running**, a background task (`watch_credentials_fallback`)
    polls this file every 30s and re-logs-in if `updated_at` has moved past
    what was last applied -- a safety net for a missed live push (bridge
    down, transient network issue between the two `--network host`
    containers, secret mismatch, etc) without waiting for a restart.
  - **A corrupt file never kills the bridge.** Truncated JSON, invalid
    UTF-8 (a torn write on the shared mount), or valid JSON that is not an
    object is logged as a warning and ignored on both paths: startup keeps
    the `.env` credentials, the watcher skips that cycle and keeps polling.
    The bad file outlives any restart, so crashing on it would restart-loop
    the container through repeated SSO logins. Pinned by
    `tests/test_credentials_fallback.py`.

Net effect: a credential change made through HA's UI reaches this bridge
either immediately (live push) or within 30s (fallback watcher) or at next
startup (fallback file adoption) -- whichever happens first -- and HA's own
credential update always succeeds/fails based on HA's own TELUS login only,
never blocked on this bridge being reachable.

## Known limitations

- **No MFA/2FA support.** `telus_auth.py`'s login is username/password only.
  If TELUS ever enables MFA on this account, this bridge's login breaks
  independently of the HA integration (which does support 2FA/OTP). Not
  handled -- implementing this needs a HAR capture of a real MFA challenge
  against this account to know the actual redirect/OTP-submission contract
  (the same way the rest of `telus_auth.py` was built); guessing at that
  contract without one risks a login flow that looks plausible but doesn't
  actually work, or repeated failed attempts against TELUS's live auth
  endpoint. Would need real design work (and that capture) if it ever
  becomes necessary.
- `GET /control/status` (per-camera `state`, `consecutive_failures`,
  `last_frame_at`, `fps`, `hardware_encode`, `dropped_frames`, `last_error`)
  -- added, same auth as every other endpoint. `camera_session.py`'s
  `run_camera_forever` updates its own entry directly; the control server just
  reflects it. `dropped_frames` is the camera's lifetime consumer-fell-behind
  drop total since process start, accumulated as each video session ends.
- The control secret now rotates automatically every
  `config.CONTROL_SECRET_ROTATE_INTERVAL_S` (30 days by default) via an
  atomic rewrite of `control_secret` -- safe with no coordination needed
  since HA's `bridge_client.py` re-reads the file fresh on every request
  rather than caching it. Manual rotation (delete the file, restart the
  container) still works too.

## Recreating the container

The cloned repo directory itself is the deployment location -- there's no
separate "install path" to keep in sync with the source, `docker-compose.yml`
runs directly from here:

```bash
cd camera-bridge   # inside your clone of the telus-smart-home repo
sudo docker compose up -d --build
```

`install.sh` at the repo root does this plus the one-time setup (shared
directory, credentials) in one step -- see the top-level README.

No port-publish flags are needed anywhere, including for the control
endpoint -- `--network host` (set in `docker-compose.yml`) already puts the
container's `127.0.0.1:8199` on the host's own loopback, which the HA
container (also `--network host`) shares directly.
