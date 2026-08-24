# Camera Bridge

The camera bridge is a standalone Docker service that pulls live video from a
TELUS Smart Home account (a white-labeled Alarm.com deployment) and republishes
it as plain **RTSP** on the local host, so any RTSP consumer — Home Assistant,
`ffplay`, VLC — can treat the cameras as ordinary RTSP entities without having
to speak WebRTC/Janus itself. It also exposes a small loopback **HTTP control
API** used by the companion Home Assistant integration.

This document covers the video pipeline and its runtime. For the full HTTP
surface (endpoints, request/response shapes, auth), see
[control-api.md](control-api.md).

> This is a reverse-engineered project, not an official TELUS/Alarm.com product.
> The message sequences and field names were verified by capture against the
> real service; there is no upstream to pull fixes from.

## What it does

At a high level the bridge runs two cooperating processes inside one container:

- **MediaMTX** — a single static binary that serves RTSP on `127.0.0.1:8554`.
  It does the actual RTSP serving; the bridge only publishes into it.
- **The Python bridge** (`bridge/`) — logs into the TELUS SSO, enumerates the
  account's cameras, and for each one opens a Janus/WebRTC session, decodes the
  frames, and feeds them to an `ffmpeg` process that publishes to MediaMTX over
  RTSP.

`entrypoint.sh` starts MediaMTX first, waits for its RTSP listener, then starts
the Python bridge. If either process dies, the container exits and Docker's
`restart: unless-stopped` policy brings both back up together.

Alongside the video pipeline it is also a **settings and configuration client**
for the account, which is what the loopback control API exposes to Home
Assistant. Those parts do not touch video at all:

| Module | What it speaks to | What it covers |
| --- | --- | --- |
| `camera_settings.py` | website API | speaker/call volume, chime, image, clip and live-stream resolution, status LED, TELUS-side push notifications, diagnostics |
| `arming_devices.py` | website API | Bluetooth arming devices — whether a paired phone may disarm the panel on approach |
| `partitions.py` | website API | alarm partitions and selective sensor bypass |
| `mws_client.py` | mobile MWS API | talkdown, camera siren, privacy mode, and the chime-type option labels the website only publishes as bare integers |

All three website clients share one shape, described in
[architecture.md](architecture.md#device-settings-and-alarm-configuration) and in
detail in [web-portal-api-findings.md](web-portal-api-findings.md): a JSON:API
read, a **flat** write, whole-document read-modify-write, and a capability flag
checked before any control is offered or applied.

Two of them reach a security system rather than a camera, and are written to
refuse rather than guess: an arming-device write decides whether a phone
disarms the alarm, and a bypassed sensor stops triggering it until it is put
back. Both re-read the record they are about to change and honour what it says.

### Per-camera pipeline

Each camera runs the same always-warm pipeline, owned end to end by
`camera_session.run_camera_forever`:

```
TELUS SSO login (telus_auth.py)
   -> camera discovery         (telus_rest.list_cameras)
   -> per-camera video source  (telus_rest.get_video_source)  proxy_url, janus url, token, ICE
   -> Janus/WebRTC ingest      (janus_client.JanusCameraSession) decoded av.VideoFrames
   -> ffmpeg encode + publish  (ffmpeg_sink.FfmpegSink)         H.264 over RTSP
   -> MediaMTX                                                  rtsp://127.0.0.1:8554/telus_<id>
```

## Startup and lifecycle

`main.py` is the entrypoint (`python -m bridge.main`). Its sequence:

1. **Adopt fallback credentials.** `_load_fallback_credentials_if_present`
   reads `/shared/credentials.json` (written by the HA integration on every
   credential update) and overrides the `.env` values if present, so a freshly
   recreated container picks up the latest credentials even if it missed a live
   push. Both `username` and `password` are parsed before either is assigned, so
   a malformed file can never leave a mismatched pair. **Every corruption shape
   degrades, never crashes:** truncated JSON, invalid UTF-8 (a torn write on the
   shared mount), or valid JSON of the wrong shape (`[]`, `null`, a string) is
   logged as a warning and ignored, and the bridge starts on its `.env`
   credentials. This matters because the bad file survives the restart — a crash
   here would loop the container through a fresh SSO login against the
   Cloudflare-fronted edge on every restart. Pinned by
   `tests/test_credentials_fallback.py`.
2. **Initial login with retry.** `_login_with_retry` runs the SSO login with a
   short bounded backoff (`2, 4, 8s`). A `RateLimited` failure is handled
   separately — it sleeps for the server's own `Retry-After` rather than the
   fixed schedule (see [Rate limiting](#rate-limiting-cloudflare)). If login
   fails terminally, the process defers to the [restart guard](#restart-guard)
   before dying into a container restart.
3. **Discover cameras and activate pipelines.** The first discovery pass runs
   synchronously so the control API's `/control/cameras` is populated before
   startup is considered done. A failure here degrades to an empty registry
   rather than propagating (a transient API blip right after login must not
   kill the process and trigger a fresh-login restart loop). Every discovered
   camera is activated eagerly.
4. **Start background loops.** `discover_and_launch_cameras` re-polls TELUS every
   `CAMERA_DISCOVERY_INTERVAL_S` (300s) for newly added cameras;
   `watch_credentials_fallback` polls `credentials.json` every 30s for credential
   changes; an unreadable or corrupt file (including invalid UTF-8 or valid JSON
   that is not an object) makes it warn and skip that cycle, never die — the
   background tasks run under a bare `asyncio.gather`, so a crashed watcher task
   would take the whole process with it. The control server also runs (see
   [control-api.md](control-api.md)).

### Always-warm cameras

Every registered camera is activated at startup/discovery and **kept running
indefinitely** — there is no on-demand start driven by a reader connecting.
This is a deliberate trade: the old design gated pipelines on MediaMTX
`runOnDemand` hooks (near-zero idle CPU), but a reader then had to wait out a
cold Janus/TELUS negotiation (worst case ~50s) before video appeared. Always-warm
keeps the stream flowing so a reader — or a fresh HA/MediaMTX restart — sees
video immediately. Measured cost: roughly 14–31% of one core and ~326 MB RSS for
two cameras.

`CameraLifecycleManager` (`lifecycle.py`) owns starting each pipeline task and is
idempotent and safe to call `activate()`/`deactivate()` on concurrently for the
same camera id. Nothing calls `deactivate()` automatically anymore; it survives
only as a manual lever exposed via the control API (deactivate-by-path), e.g. to
force one camera to restart its session without touching the others.
`discover_and_launch_cameras` **never removes** a camera that stops appearing in
a response — a flaky API shouldn't kill a working stream; a genuinely removed
camera just keeps retrying harmlessly like any offline one.

## The ~120s / 165s proactive session rotation — do not regress

This is the single most important invariant in the bridge.

Alarm.com kills a video session **server-side at ~180s, unconditionally**,
matching the `"timeout_seconds": 180` the bridge sends in the Janus
mountpoint-create request (`janus_client.py`). Earlier versions used far longer
lifetimes (e.g. 900s) and would go dark and hang once that server-side timer won
the race — a real production bug.

The fix, in `config.py` and `camera_session.run_camera_forever`, is three
deliberately redundant safety nets:

| Mechanism | Constant | Behavior |
|---|---|---|
| Proactive rotation | `SESSION_MAX_LIFETIME_S = 165` | Every session is torn down and reconnected at 165s, comfortably before the ~180s server-side kill, so the feed never waits on a server-initiated teardown. |
| Frame-silence watchdog | `FRAME_SILENCE_TIMEOUT_S = 15` | If no frame arrives for 15s, the session is treated as dead and reconnected immediately, without waiting for the rotation deadline. |
| Frame-hold bridge | `FRAME_HOLD_MAX_S = 60` | The last real frame is repeated into the still-running publisher during a rotation/reconnect gap, so readers see a brief freeze rather than a torn-down path. After 60s with no live frame, the hold ends and the feed is allowed to go dark honestly. |

Why 165s and not the earlier 120s: the original rotation design tore the ffmpeg
publisher down along with each session. Once the publisher was made to **outlive
rotations** (see below) and the gap bridged by held frames, the margin could be
tightened from 60s to 15s — matching `FRAME_SILENCE_TIMEOUT_S` — keeping the
freeze-bridged gaps as infrequent as possible while staying safely under 180s.

`camera-bridge/tests/test_camera_session.py` enforces all three against
scripted fake sessions, so a regression fails CI rather than surfacing days
later as a frozen feed.

Two checks that must never be collapsed or reordered:

- The **rotation-deadline check runs even when no frame ever arrives.** The
  session loop computes `remaining_to_rotate` at the top of every iteration and
  breaks when it hits zero; a frame read that times out re-checks the deadline
  before raising a silence error. Checking the deadline only *after* a frame
  landed is exactly what let the bridge hang forever once frames stopped.
- The **frame-silence check runs unconditionally on every iteration**, including
  when zero frames have ever arrived — that is what makes it safe against a
  camera that stalls immediately.

### Publisher outlives sessions

The `FfmpegSink` publisher is intentionally kept alive across Janus rotations.
If the publisher were torn down with each session, MediaMTX would destroy the
RTSP path the moment ffmpeg's connection EOF'd — kicking every reader and (under
the old on-demand design) firing `runOnUnDemand`, which cancelled the pipeline
task before it could reconnect. Downstream, each teardown injected an HLS
discontinuity that browser HLS players do not survive. A fully gapless publisher
*handoff* is impossible because MediaMTX rejects a second concurrent publisher on
the same path, so instead the one publisher is held and the gap bridged with
repeated frames.

The sink is only actually replaced when it has genuinely died
(`sink.proc is None`, or the process exited) or the camera's frame size changed —
otherwise the same RTSP session carries straight through the rotation and readers
never notice.

> If you touch `camera_session.py` or `janus_client.py`, read them in full first.
> Do not change `SESSION_MAX_LIFETIME_S` without re-verifying Alarm.com's actual
> server-side timeout live — it is undocumented; 180s was observed, not promised.

## Janus / WebRTC ingest

`janus_client.JanusCameraSession` speaks Janus's WebSocket JSON-RPC protocol to
the Alarm.com-hosted streaming-plugin gateway and wires the media through
`aiortc`. The negotiation sequence (transaction ids, field names) was verified
byte-for-byte against a captured session:

1. `create` a Janus session.
2. `attach` the `janus.plugin.streaming` plugin (`force-bundle`, `force-rtcp-mux`).
3. `create` an RTP mountpoint from the video source's `proxy_url`
   (`type: rtp`, H264/90000, `videopt: 126`, `timeout_seconds: 180`).
4. `watch` the mountpoint — an `ack` arrives first, then a separate `event`
   carrying the JSEP offer; the client drains the transaction's queue until it
   sees `jsep` (an error reply carries no jsep, and is raised immediately rather
   than waiting out the 15s timeout).
5. Apply the remote offer, apply any trickled ICE candidates (buffered if they
   arrived before the remote description was set), build and send the answer
   (aiortc embeds its own candidates in the SDP; no outbound trickle needed).
6. `start`, then wait for `webrtcup` (`WEBRTCUP_TIMEOUT_S = 20`).

Decoded video frames flow through a small bounded queue (`maxsize=4`). When the
consumer falls behind, the **oldest** queued frame is dropped rather than
blocking the decoder — a live view cares about latency, not completeness — and
the drop count is logged (throttled) so a backlog is visible rather than silent.
Each session exposes its own count as the read-only
`JanusCameraSession.dropped_frames` property; when a session ends,
`camera_session` folds it into a per-camera lifetime total published as
`dropped_frames` in `GET /control/status` (see
[control-api.md](control-api.md#get-controlstatus)), so a chronic backlog is a
number something can alert on rather than a log-grep question.
A keepalive is sent every `JANUS_KEEPALIVE_INTERVAL_S` (25s), well under Janus's
~60s idle-session GC. The keepalive task ends quietly when the websocket drops
between keepalives (an ordinary unclean rotation — the server force-closes
sessions at ~180s against the 25s interval); detecting and reporting a dead
session is the recv loop's job, not the keepalive's.

The session's `frames()` async generator races each frame read against a "dead"
signal (track ended, ICE/connection failed, websocket closed) so it never blocks
forever. `session.close()` calls aiortc's `pc.close()`, which is known to hang
under certain ICE states and has no timeout of its own; `camera_session` always
wraps it in `_close_with_timeout` (`SESSION_CLOSE_TIMEOUT_S = 5`) so a hang
surfaces as one counted failure rather than an unrecoverable wedge, and always
closes the dying session **before** the backoff sleep (a still-running session
keeps decoding RTP nobody drains for the whole sleep). Inside `close()`, the
Janus-gateway websocket is closed unconditionally (try/finally): a `pc.close()`
that raises, or the `_close_with_timeout` cancellation landing mid-close, still
releases the socket instead of leaking one per rotation on a wedged camera. The
ordering (peer connection first, then websocket) is preserved, the websocket
close never masks `pc.close()`'s own exception, and a cancellation is re-raised
after the cleanup attempt.

### fps and resolution learning

On connect, the loop collects up to ~1s or 10 frames to learn the camera's real
size and frame rate (observed to vary by model, e.g. 1440×1440@15 vs 1280×720@30).
fps is estimated from the **median inter-frame delta**, not total-span math — on a
rotation, buffered frames can arrive in a near-instant burst, and a single
burst endpoint would otherwise contaminate the whole estimate. A measurement is
only "locked in" as this camera's `known_fps` if it is plausible (1–60, and
within 50% of any prior known value); the 15fps fallback is never promoted to
`known_fps`, so a camera whose first reading was burst-contaminated can still
correct itself later.

## ffmpeg encode and RTSP publish

`ffmpeg_sink.FfmpegSink` feeds decoded `av.VideoFrame` objects to an `ffmpeg`
subprocess over `pipe:0` (raw `yuv420p`), which encodes H.264 and publishes via
**RTSP ANNOUNCE** (ffmpeg's RTSP muxer cannot act as a server, so MediaMTX is the
server and ffmpeg the publisher).

The per-frame pixel copy (`to_ndarray().tobytes()`) is a real synchronous CPU
cost (~3 MB per frame for a 1440×1440 camera) and is moved to a thread via
`run_in_executor` so it doesn't stall the event loop shared by both cameras, the
Janus signaling, and the control server.

Load-bearing ffmpeg flags, found empirically against this exact stack:

| Flag | Purpose |
|---|---|
| `-bf 0` | No B-frames — HA's PyAV stream worker must not have to reorder frames without DTS info. |
| `-g <fps>` | GOP tied to the measured fps (not a fixed frame count) → consistent ~1s keyframe interval regardless of camera frame rate, and clean alignment with HA's 2.0s HLS segmenter. |
| `-pkt_size 1200` | Keep RTP payloads under MediaMTX's repacketization threshold; letting MediaMTX repacketize strips DTS continuity ("No dts in N consecutive packets" in HA). |
| `-use_wallclock_as_timestamps 1` | Keeps PTS advancing, including while held frames are repeated during a rotation gap. |
| `-rtsp_transport tcp` | RTSP over TCP to MediaMTX. |

Frames larger than `MAX_ENCODE_DIMENSION` (960px on the larger side) are
downscaled at the encode step. Some cameras publish natively at 1440×1440, which
is fine for the pipeline but can exceed what a modest viewer's decoder can sustain
continuously alongside a second camera (observed as a decoder/paint stall — RTP
keeps arriving while the picture freezes). Downscaling cuts the pixel throughput
every viewer's decoder must handle. Dimensions are rounded down to even numbers
(h264/yuv420p requires it).

### Hardware encode backends

`config.ENCODE_BACKEND` (env `ENCODE_BACKEND`) selects which encoder is
**attempted** before falling back to software `libx264`:

| Value | Encoder | Device path | Notes |
|---|---|---|---|
| `vaapi` (default) | `h264_vaapi` | `/dev/dri` (`VAAPI_DEVICE`, default `/dev/dri/renderD128`) | Intel QuickSync or AMD via Mesa. Which driver libva actually uses is decided against the passed-through GPU, not chosen by the project. Adds `format=nv12,hwupload` and `-async_depth 1` (force sequential encode; the default 2-frame pipeline only helps throughput across many concurrent streams, not this one-process-per-camera design). This is the only path verified live. |
| `nvenc` | `h264_nvenc` | nvidia-container-toolkit device injection | Accepts the raw `yuv420p` pipe directly (no `hwupload`). Uses `-preset p1 -tune ll`. `h264_nvenc` is already compiled into the image's ffmpeg; only the host NVIDIA driver + toolkit are needed. **Not verified against real NVIDIA hardware** by this project (Intel-only host). |
| `software` | `libx264` | none | `-preset ultrafast -tune zerolatency`. Skips the hardware attempt entirely. |

`ENCODE_BACKEND` is deliberately not `auto`: probing an unavailable backend still
costs a real (~0.75s) failed subprocess launch every reconnect, so an explicit
choice avoids silently trying a backend nobody asked for on a GPU-less host.

Selection and fallback logic, per `start()` call:

- `software` → always libx264; raise if even that fails to start.
- otherwise → try the selected hardware encoder; if it doesn't survive the
  0.75s startup grace (missing/broken device, driver crash, or an RTSP ANNOUNCE
  to a momentarily-down MediaMTX), fall back to `libx264`.

The fallback is **re-attempted on every `start()`** (i.e. every reconnect/
rotation) rather than remembered as permanently broken, so hardware encoding
resumes automatically the moment the device comes back. `FfmpegSink.hardware_encode`
reflects which path is live and is surfaced via the status endpoint.

The published RTSP URL carries the MediaMTX password, so it is **redacted** from
the ffmpeg publish log line (container logs are persisted by Docker's json-file
driver).

## RTSP authentication

The published streams require credentials. Binding to `127.0.0.1` is not a
substitute for auth here: this container and the HA container both run
`--network host`, so "loopback" means every process and every other host-networked
container on the box, and path names are a pure function of the camera id
(`config.rtsp_path_for` → `telus_<camera_id>`), so they are guessable. Without
auth, anything local could read the feeds or publish over a path and replace the
live feed.

How it fits together:

- `entrypoint.sh` generates a random hex password on first start and persists it
  to `/shared/rtsp_password` (mode `0600`), reusing it on every later start. It
  is never rotated automatically.
- MediaMTX receives it via `authInternalUsers` in `mediamtx.yml`, whose
  `__RTSP_PASSWORD__` placeholder `entrypoint.sh` substitutes into a runtime copy
  under `/tmp` (MediaMTX does not interpolate env vars in its config). The
  configured user `bridge` has publish+read, restricted to `127.0.0.1/32`.
- The Python side reads the same value from the exported `RTSP_PASSWORD` env var;
  `config.rtsp_publish_url()` builds it into the URL used **both** as ffmpeg's
  publish target and as the `rtsp_url` reported over `/control/cameras`, so HA's
  camera entities authenticate with no manual configuration.

To rotate: delete `/shared/rtsp_password`, restart the bridge container, then
restart the RTSP consumer so it re-reads the URLs. To read a stream by hand:

```bash
ffplay "rtsp://bridge:$(sudo cat <shared-dir>/rtsp_password)@127.0.0.1:8554/telus_<camera_id>"
```

## Rate limiting (Cloudflare)

The TELUS auth gateway is fronted by Cloudflare, which can return HTTP 429
(error 1015) on the login POST. `telus_auth.py` raises `RateLimited` carrying the
server's `Retry-After`. Every retry path — startup login and the reconnect loop —
sleeps for that stated wait via `config.rate_limit_sleep_s`, **not** the fixed
`2/4/8/16/30s` backoff, because a short fixed schedule just re-enters the same
rate-limit window instead of clearing it (this compounding is what turned one
incident into a multi-hour outage). The sleep is clamped only by
`RATE_LIMIT_RETRY_MAX_S` (30 min) as a guard against an absurd header value, plus
a small margin. A rate-limit failure is also **not** counted toward the
consecutive-failure escape hatch, since restarting the container only re-runs the
login and re-triggers the window.

## Login serialization

All `TelusSession.login()` call sites — startup, the 401-triggered relogin, the
control-endpoint credential push, and the fallback-file watcher — are serialized
through one shared `asyncio.Lock` (`login_lock` in `main.py`). `login()` clears
the entire cookie jar with no locking of its own, so two concurrent logins could
otherwise interleave into a broken cookie state. On top of the lock,
`relogin_if_stale` compares a `login_generation` counter across the lock boundary
and **skips** a redundant re-login if another caller already refreshed the
session — collapsing the burst of simultaneous 401s that happens when the session
expires (every camera 401s at once) into a single real login. `login_generation`
is bumped only as the last statement of a fully successful login, so a partial
failure never looks like a usable refreshed session.

Credentials themselves are plain mutable module attributes (`c.TELUS_EMAIL` /
`c.TELUS_PASSWORD`) that `login()` reads lazily on every call, so the control
endpoint or the fallback watcher can swap them in with no restart. See
[control-api.md](control-api.md) for the credential-push flow.

## Restart guard

`restart_guard.py` throttles the two deliberate process-death paths:
`camera_session`'s consecutive-failure escape hatch (`os._exit`) and `main.py`'s
terminal startup-login failure. Both are correct responses to a *local* problem
(a leaked/wedged aiortc resource that only a fresh process clears, a boot during
a blip) and the wrong response to a *sustained upstream* failure, where the
restart just re-runs a full SSO login against the rate limiter. From inside the
loop the two cases are indistinguishable, and with `restart: unless-stopped`
behind them the second had no floor at all (measured: 89 restarts in 7 days,
including a 14-minute tight loop).

Because these paths kill the process, the count has to outlive it. The guard
keeps a small JSON file of recent exit timestamps on the `/shared` bind mount
(survives both a process exit and a container recreate):

| Constant | Value | Meaning |
|---|---|---|
| `RESTART_GUARD_WINDOW_S` | 3600s | Rolling window; older exits are dropped. |
| `RESTART_GUARD_MAX_EXITS` | 3 | Exits allowed in the window before throttling — kept immediate so a genuine one-off wedge recycles as fast as ever. |
| `RESTART_GUARD_BACKOFF_INITIAL_S` | 60s | First throttled hold, doubling per extra exit (60, 120, 240 …). |
| `RESTART_GUARD_BACKOFF_MAX_S` | 900s | Cap — well past Cloudflare's ~101s Retry-After. |

Beyond the free allowance, `record_exit()` hands the caller a hold to sleep
**before** exiting. The sleep runs in-process, so Docker physically cannot restart
the container until it finishes — a hard floor on restart frequency — while the
other cameras and the control endpoint keep serving. The exit is recorded before
the sleep (a `docker restart` or OOM kill mid-sleep still counts), reads/writes
happen without an intervening `await` (two cameras can't lose each other's entry),
and the state file is written atomically (`.tmp` + `os.replace`). It is
**fail-open**: any error reading or writing the state degrades to "exit
immediately" — a broken guard must never be why a genuinely wedged bridge can't
recycle. The escape hatch never *cancels* an exit; a real wedge still gets its
fresh process, just not at loop speed.

The consecutive-failure hatch itself fires at
`MAX_CONSECUTIVE_SESSION_FAILURES = 5` — with the `2/4/8/16/30s` backoff that is
~60s of continuous failure with no successful frame in between, comfortably past
what the backoff alone absorbs for a transient blip. It uses `os._exit()` (not a
graceful shutdown) deliberately: a graceful path runs through the very cleanup
code suspected of being wedged, so a hard exit lets the OS reclaim every
fd/socket/child unconditionally.

## Container and runtime

Defined by `Dockerfile`, `entrypoint.sh`, and `docker-compose.yml`.

- **Base image**: `python:3.14-slim`. Installs `ffmpeg`, `intel-media-va-driver`,
  and `mesa-va-drivers` (userspace VAAPI drivers for Intel and AMD, keyed off the
  runtime PCI device, so both ship unconditionally). `h264_vaapi`/`h264_nvenc` are
  already in Debian's ffmpeg — no extra package is needed for NVIDIA. All Python
  deps resolve to manylinux wheels (`pip install --only-binary :all:`), so no
  build toolchain ships in the image.
- **MediaMTX**: pinned static binary downloaded at build time (the pinned
  version is the `MEDIAMTX_VERSION` ARG in the `Dockerfile`).
- **Non-root**: runs as a dedicated `bridge` user (uid/gid 1500). All ports are
  >1024; everything written lives under `/app` or the bind-mounted `/shared`
  (host-side ownership must match this uid).
- **Network mode**: `--network host` is **required**. Janus/WebRTC negotiates a
  wide ephemeral UDP RTP range that doesn't work through Docker's default bridge +
  NAT, and the loopback-only control endpoint is reachable from the HA container
  only because both share the host's network namespace. `install.sh` pre-flight
  checks that HA also uses host networking.
- **`/dev/dri` passthrough**: the render node is passed through for hardware
  encode, with `group_add` granting the non-root user access via the host's
  `render` GID (`RENDER_GID`, default `992` — must match `getent group render` on
  the host). The commented-out `deploy.resources` block is the opt-in path for
  NVIDIA instead.
- **Shared directory**: the host path (`HOST_SHARED_DIR`, default
  `/var/lib/homeassistant/share/telus_cam_bridge`, the Supervised layout) is bind
  mounted at `/shared` — the same physical directory the HA container mounts, so
  both read/write `rtsp_password`, `control_secret`, `credentials.json`,
  `restart_guard.json`, and `mobile_auth.json` without extra host config.
- **Resource caps**: `mem_limit` 768m and `cpus` 2.0 by default — generous
  headroom over measured usage, so a runaway leak is OOM-killed into a clean
  restart rather than consuming the host. Logs use the json-file driver with
  rotation (`10m` × 3).
- **Process supervision**: `entrypoint.sh` uses bash `wait -n` to block on
  *either* MediaMTX or the bridge; whichever dies first takes the whole container
  down, and `restart: unless-stopped` brings both back together. It is
  deliberately not `set -e` (which would make the post-`wait` diagnostics and
  cleanup unreachable).

### Configuration

Non-secret settings live in `.env`, credentials in `secrets.env` (kept separate
because Compose interpolates `$` in `env_file` values — a password with a literal
`$` must be escaped as `$$`; `install.sh` does this automatically). Key
environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `TELUS_EMAIL` / `TELUS_PASSWORD` | — | SSO credentials (required; secrets.env). |
| `ENCODE_BACKEND` | `vaapi` | Hardware encode path (`vaapi` / `nvenc` / `software`). |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | Render node for VAAPI. |
| `MEDIAMTX_RTSP_PORT` | `8554` | RTSP listen port. |
| `CONTROL_PORT` | `8199` | Loopback control API port (see [control-api.md](control-api.md)). |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `CONTROL_ACCESS_LOG` | `false` | Per-request access logging on the control endpoint; off by default (see [configuration.md](configuration.md)). |
| `HOST_SHARED_DIR` | Supervised path | Host side of the `/shared` mount. |
| `RENDER_GID` | `992` | Host `render` group GID for `/dev/dri` access. |
| `BRIDGE_MEM_LIMIT` / `BRIDGE_CPU_LIMIT` | `768m` / `2.0` | Container resource caps. |
| `TALKDOWN_ENABLED` | `true` | Two-way audio, enabled by default; set `false` to disable (the talk route then returns `403 talkdown_disabled`). |

### Logs

The bridge logs to stdout/stderr only (no file handlers), so
`sudo docker logs telus-cam-bridge` is the one place its logs live.
`docker-compose.yml` caps the json-file driver at 10 MB × 3 files, so retained
history is bounded at ~30 MB and rotation needs no host configuration. Every
line is written to be greppable during an incident: it carries the camera or
device id, the cause, and — where the code knows it — what happens next.
Per-request access lines are off by default (`CONTROL_ACCESS_LOG`) so routine
HA polling cannot age real warnings out of that window. Log lines never carry
secrets: signed snapshot URLs, media-URL query strings (which can hold signed
tokens), RTSP/PUT credentials and session tokens are all redacted before
logging.

A few INFO diagnostics ride along, one line each per session start (so about one
per rotation, per camera):

- **The first-frame latency**, carried on the existing streaming line —
  `streaming 1440x1440@15fps (first frame after 1.6s)`, measured from the moment
  the loop starts waiting for a first frame. It shares a line with the size/fps
  it belongs to rather than taking one of its own. This is the margin against
  `FIRST_FRAME_TIMEOUT_S` (20s): a camera whose first frame lands at 1.6s and one
  whose first frame lands at 15s look identical in every other line, but only the
  second is one blip from the reconnect loop below.
- **Which `JANUS_REQUEST_AUDIO` mode applied to this camera** —
  `JANUS_REQUEST_AUDIO mode 'listed' -> mountpoint create WITH audio`, with the
  mode being `off`, `all`, `listed` or `unlisted`. The knob is per-camera, so the
  env var alone no longer says which cameras it reached.
- **The Janus offer's media summary** — the media kinds, ports and codec names
  the gateway offered, e.g. `janus offer media: video(port=9,H264)`. It is a
  *derived summary by construction, and must stay one*: a raw SDP carries
  `a=ice-ufrag`, `a=ice-pwd`, `a=fingerprint` and `a=candidate`, so only `m=`
  and `a=rtpmap:` lines are read and every token emitted is filtered against a
  conservative pattern. Turning the root log level up to `DEBUG` is **not** an
  equivalent way to see this — aiortc logs the entire raw SDP at debug, which
  would put ICE credentials into the container log.
- **The videoSource attribute inventory** — the attribute *key names* the
  live-video source document carries, plus `supportsAudio` and whether
  `audioUpStreamUrl` is present. Key names and one boolean only; never a value,
  because the same document holds the Janus token and the ICE server
  credentials.

### Downstream (listen) audio

The bridge publishes **video only**, and by default the Janus mountpoint it
creates asks for video only. A camera that offers an audio track anyway has that track
**drained and discarded** rather than merely ignored: aiortc auto-accepts every
media section the remote offers and decodes it on its own thread into an
unbounded queue, so a track nobody reads would grow for the whole life of each
session — a per-rotation memory sawtooth. Draining holds it at zero; the
decoder thread itself is released when the session closes. (Stopping the track
instead would be worse than doing nothing: it stops the queue being readable
without stopping it being filled.)

`JANUS_REQUEST_AUDIO` (see [configuration.md](configuration.md)) is the
experiment that asks for a downstream audio track anyway, and reads the answer
off the offer summary above. It is a **per-camera** knob, and that is the part
worth knowing before turning it on: asking is not free for every camera.

What is established: asked for audio, this gateway *does* offer one — the offer
comes back as `audio(port=9,opus) video(port=9,H264,rtx)`, with the audio
m-line **first**, for both cameras on this account. What happened next differed
per camera: the doorbell went on delivering video at its usual rate, while the
slower of the two (6 fps natively, and slow on unrelated paths — its
diagnostics-signal read takes ~25 s against the doorbell's ~1.6 s) stopped
producing a first frame at all and cycled through
`no frames received before timeout` roughly every 23 s until the request was
turned off again.

What is **not** established is that the audio m-line *caused* that. The same
camera is slow everywhere, the failure is a timeout rather than a rejection, and
the session teardown line that accompanies it (`ice connection state: closed`)
is logged at the same instant as the failure — it is the consequence, not the
cause. That ambiguity is why the knob names cameras instead of being global: the
experiment can run on the camera that tolerates it without putting the other
one's video at risk, and the first-frame timing on both the success and failure
lines (above) is what would separate "slower than the timeout" from "not
streaming at all" the next time it is tried.

This is all separate from talkdown, which plays audio *out* of a doorbell and
works today.

### Recreating the container

The cloned repo directory *is* the deployment location; `docker-compose.yml` runs
directly from it:

```bash
cd camera-bridge
sudo docker compose up -d --build
```

## Known limitations

- **No MFA/2FA.** `telus_auth.py` is username/password only. If MFA is enabled on
  the account, this bridge's login breaks; adding it would require a real capture
  of the MFA challenge to know the actual redirect/OTP contract.
- **`nvenc` is unverified live** — implemented per ffmpeg's documentation, not
  exercised end-to-end against real NVIDIA hardware.
- The bridge holds its **own copy** of the TELUS credentials, separate from any
  Home Assistant integration; keeping the two in sync is the job of the control
  endpoint and the credential-fallback file (see
  [control-api.md](control-api.md)).
