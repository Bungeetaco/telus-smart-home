# Camera Bridge

The camera bridge is a standalone Docker service that pulls live video from a
TELUS Smart Home account (a white-labeled Alarm.com deployment) and republishes
it as plain **RTSP** on the local host, so any RTSP consumer (Home Assistant,
`ffplay`, VLC) can treat the cameras as ordinary RTSP entities without having
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

- **MediaMTX**: a single static binary that serves RTSP on `127.0.0.1:8554`.
  It does the actual RTSP serving; the bridge only publishes into it.
- **The Python bridge** (`bridge/`): logs into the TELUS SSO, enumerates the
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
| `arming_devices.py` | website API | Bluetooth arming devices: whether a paired phone may disarm the panel on approach |
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
   credentials. This matters because the bad file survives the restart: a crash
   here would loop the container through a fresh SSO login against the
   Cloudflare-fronted edge on every restart. Pinned by
   `tests/test_credentials_fallback.py`.
2. **Initial login with retry.** `_login_with_retry` runs the SSO login with a
   short bounded backoff (`2, 4, 8s`). A `RateLimited` failure is handled
   separately: it sleeps for the server's own `Retry-After` rather than the
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
   that is not an object) makes it warn and skip that cycle, never die: the
   background tasks run under a bare `asyncio.gather`, so a crashed watcher task
   would take the whole process with it. The control server also runs (see
   [control-api.md](control-api.md)).

### Always-warm cameras

Every registered camera is activated at startup/discovery and **kept running
indefinitely**: there is no on-demand start driven by a reader connecting.
This is a deliberate trade: the old design gated pipelines on MediaMTX
`runOnDemand` hooks (near-zero idle CPU), but a reader then had to wait out a
cold Janus/TELUS negotiation (worst case ~50s) before video appeared. Always-warm
keeps the stream flowing so a reader (or a fresh HA/MediaMTX restart) sees
video immediately. Measured cost: roughly 14–31% of one core and ~326 MB RSS for
two cameras.

`CameraLifecycleManager` (`lifecycle.py`) owns starting each pipeline task and is
idempotent and safe to call `activate()`/`deactivate()` on concurrently for the
same camera id. Nothing calls `deactivate()` automatically anymore; it survives
only as a manual lever exposed via the control API (deactivate-by-path), e.g. to
force one camera to restart its session without touching the others.
`discover_and_launch_cameras` **never removes** a camera that stops appearing in
a response: a flaky API shouldn't kill a working stream; a genuinely removed
camera just keeps retrying harmlessly like any offline one.

## The ~120s / 165s proactive session rotation: do not regress

This is the single most important invariant in the bridge.

Alarm.com kills a video session **server-side at ~180s, unconditionally**,
matching the `"timeout_seconds": 180` the bridge sends in the Janus
mountpoint-create request (`janus_client.py`). Earlier versions used far longer
lifetimes (e.g. 900s) and would go dark and hang once that server-side timer won
the race, a real production bug.

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
tightened from 60s to 15s (matching `FRAME_SILENCE_TIMEOUT_S`) keeping the
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
  when zero frames have ever arrived: that is what makes it safe against a
  camera that stalls immediately.

### Publisher outlives sessions

The `FfmpegSink` publisher is intentionally kept alive across Janus rotations.
If the publisher were torn down with each session, MediaMTX would destroy the
RTSP path the moment ffmpeg's connection EOF'd, kicking every reader and (under
the old on-demand design) firing `runOnUnDemand`, which cancelled the pipeline
task before it could reconnect. Downstream, each teardown injected an HLS
discontinuity that browser HLS players do not survive. A fully gapless publisher
*handoff* is impossible because MediaMTX rejects a second concurrent publisher on
the same path, so instead the one publisher is held and the gap bridged with
repeated frames.

The sink is only actually replaced when it has genuinely died
(`sink.proc is None`, the process exited, or it is no longer **consuming**;
see below), the camera's frame size or frame rate changed, or its raw audio
shape did (a bounded number of times per process; see "Downstream (listen)
audio"); otherwise the same RTSP session carries straight through the rotation
and readers never notice.

### Publisher liveness: "publishing" vs "publishing to nowhere"

The ffmpeg process being alive is necessary but **not sufficient** evidence that
anything is being published. A camera can report `state: "streaming"`,
`consecutive_failures: 0` and a fresh `last_frame_at` while MediaMTX has no path
at all and every reader is refused: the frame-silence watchdog watches frames
*arriving from Janus*, and the repair check above used to ask only whether the
process had exited.

What is checkable from inside the bridge is whether ffmpeg is still **consuming
what it is fed**. `FfmpegSink.write_frame` bounds `stdin.drain()` with
`_VIDEO_DRAIN_TIMEOUT_S` (5 s): a whole raw frame is ~3 MB against a 64 kB pipe
buffer, so the drain resolving means ffmpeg read it, and the drain *not* resolving
for 5 s means it has stopped. That latches `publisher_stalled`, so
`sink.publisher_alive` goes false, the session fails, and the repair check
rebuilds the publisher. Unbounded (which is what it was) that same await parks
the camera's whole loop forever, with no timeout, no failure and no reconnect, and
the status frozen on its last good values.

`/control/status` carries both halves: `last_frame_at` (a frame arrived *from the
camera*) and `last_publish_at` (ffmpeg *took* it), plus `publisher_alive`. The gap
between those two timestamps is the distinction the bridge previously could not
make.

MediaMTX's own API would answer the question more directly and is deliberately
not used: `mediamtx.yml` disables every listener except RTSP (`api: no`,
`metrics: no`, `hls: no`, …, each with its reasoning), and the container runs with
`--network host`, so enabling the API to health-check ourselves would open a real
host port. The consumption check costs nothing per frame and needs no listener.
Its limit, stated plainly: it proves ffmpeg is reading its input, not that
MediaMTX still accepts what ffmpeg writes. An ffmpeg happily encoding into a
socket that goes nowhere would still look alive.

> If you touch `camera_session.py` or `janus_client.py`, read them in full first.
> Do not change `SESSION_MAX_LIFETIME_S` without re-verifying Alarm.com's actual
> server-side timeout live: it is undocumented; 180s was observed, not promised.

## Janus / WebRTC ingest

`janus_client.JanusCameraSession` speaks Janus's WebSocket JSON-RPC protocol to
the Alarm.com-hosted streaming-plugin gateway and wires the media through
`aiortc`. The negotiation sequence (transaction ids, field names) was verified
byte-for-byte against a captured session:

1. `create` a Janus session.
2. `attach` the `janus.plugin.streaming` plugin (`force-bundle`, `force-rtcp-mux`).
3. `create` an RTP mountpoint from the video source's `proxy_url`
   (`type: rtp`, H264/90000, `videopt: 126`, `timeout_seconds: 180`).
4. `watch` the mountpoint: an `ack` arrives first, then a separate `event`
   carrying the JSEP offer; the client drains the transaction's queue until it
   sees `jsep` (an error reply carries no jsep, and is raised immediately rather
   than waiting out the 15s timeout).
5. Apply the remote offer, apply any trickled ICE candidates (buffered if they
   arrived before the remote description was set), build and send the answer
   (aiortc embeds its own candidates in the SDP; no outbound trickle needed).
6. `start`, then wait for `webrtcup` (`WEBRTCUP_TIMEOUT_S = 20`).

Decoded video frames flow through a small bounded queue whose depth is expressed
in **time, not frames**: `janus_client.video_queue_maxsize(fps)` returns
`round(fps × VIDEO_QUEUE_TARGET_MS / 1000)` clamped to 4–8 frames, so ~500 ms of
tolerance means the same thing on every camera (a flat 4 frames is 267 ms at
15 fps and 667 ms at 6 fps: one number meaning two different things). The rate
comes from the camera's own measurement, which `camera_session` keeps under
review (see "The frame rate is adaptive, not latched") and passes into each new
session; the first session of a camera, before anything has been measured, takes
the same 15 fps assumption the fps fallback does. Because the depth is fixed at
construction and the session is rebuilt every rotation, a rate that changes
mid-deployment re-sizes the queue at the next rotation with no resize path and no
frames stranded. The 8-frame ceiling is memory: a decoded 1440×1440 yuv420p frame is
3.11 MB, so 8 frames is 24.9 MB per camera against the container's 768 MiB limit
(~435 MiB in use), and 16 would be 49.8 MB for a queue that is not meant to fill.
Above ~16 fps that ceiling binds before the time target does, deliberately.

The depth is **defence in depth, not a fix for a stalled consumer**: a deeper
queue does not remove a stall, it converts dropped frames into latency, because
every frame it holds is a frame the viewer sees late.

When the consumer falls behind, the **oldest** queued frame is dropped rather than
blocking the decoder (a live view cares about latency, not completeness) and
the drop count is logged (throttled) so a backlog is visible rather than silent.
Each session exposes its own count as the read-only
`JanusCameraSession.dropped_frames` property; when a session ends,
`camera_session` folds it into a per-camera lifetime total published as
`dropped_frames` in `GET /control/status` (see
[control-api.md](control-api.md#get-controlstatus)), so a chronic backlog is a
number something can alert on rather than a log-grep question.
A keepalive is sent every `JANUS_KEEPALIVE_INTERVAL_S` (25s), well under Janus's
~60s idle-session GC. The keepalive task ends quietly when the websocket drops
between keepalives (an ordinary unclean rotation: the server force-closes
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
fps is estimated from the **median inter-frame delta**, not total-span math: on a
rotation, buffered frames can arrive in a near-instant burst, and a single
burst endpoint would otherwise contaminate the whole estimate. A measurement is
only "locked in" as this camera's `known_fps` if it is plausible (1–60, and
within 50% of any prior known value); the 15fps fallback is never promoted to
`known_fps`, so a camera whose first reading was burst-contaminated can still
correct itself later.

That first batch **bootstraps** the rate. It does not fix it.

### The frame rate is adaptive, not latched

**The frame rate is a device setting, not a property of the camera.** The
live-video resolution select ships as a Home Assistant entity, so the rate can
change from HA, from the TELUS app or from the portal, with no restart and no
signal to the bridge. The same two cameras on this account have come up at 6, 7,
15 and 29 fps across restarts.

That matters because the rate is not merely reported anywhere: `-framerate <fps>`
is the **output's constant frame rate** and the basis for `-g`, so a stale
declaration re-rates the published stream for as long as the process lives.
Both directions of error are silent and neither is harmless:

| Declared vs. delivered | What ffmpeg does | Cost |
| --- | --- | --- |
| **Too high** | pads to CFR with duplicate frames (361 written → 601 encoded) | wasted encode; observed live as `More than 1000 frames duplicated` within 2.5 min on a camera declared at 29 fps |
| **Too low** | **drops** real frames (1201 written → 1002 encoded) | 199 frames of content gone, permanently |

So `camera_session.RateWatcher` keeps the rate under review for as long as the
camera streams, and a material change **rebuilds the publisher exactly the way a
frame-size change does**, which on this platform is the same event, since both
come from the resolution setting.

**The lifecycle of a rate:**

1. **Bootstrap.** The first batch of a session sets `known_fps` if the camera has
   none, guarded by the ±50% tolerance above. This measurement can never move a
   known rate far, and deliberately so: 15 → 7 is a 53% drop that the tolerance
   rejects, and loosening it would let burst artifacts back in.
2. **Settle.** Nothing is re-measured until the session has been delivering
   frames for `RATE_RESAMPLE_SETTLE_S` (10 s). The buffered frames at a session
   start are the burst that has produced 544, 215 and (live) 62 fps readings for
   cameras really running at 15 and 30, and the dangerous ones are the
   *plausible* burst readings, "38–49 fps for a real 15 fps camera", which no
   arithmetic can distinguish from a real setting change.
3. **Measure.** The median of `RATE_RESAMPLE_FRAMES` (90) inter-frame deltas:
   3 s at 30 fps, 15 s at 6 fps. The median, again, so no single late or bunched
   frame moves the result.
4. **Band.** The reading only counts as a change if it leaves an **asymmetric**
   hysteresis band: `RATE_CHANGE_TOLERANCE_DOWN` (20%) and
   `RATE_CHANGE_TOLERANCE_UP` (35%). Asymmetric because declaring too low
   destroys frames while declaring too high only duplicates them, **and** because
   burst contamination biases a reading *high* and never low: "the camera sped
   up" is the claim an artifact makes. Both bands clear the jitter this account
   actually shows (6↔7 across restarts, 17%; a camera set to 15 delivering ~13.4,
   11%) and none of them should be corrected: 15 is the real setting, and
   declaring 13 would start dropping the frames that do arrive on time.
5. **Confirm.** Two consecutive settled batches must agree before anything acts.
   This converges a **fast**-flapping source: a rate alternating quicker than
   two settled batches (~12 s) never produces two agreeing batches, so it is
   never confirmed and never rebuilds. It does **not** converge a slow one:
   that is the flap guard's job, below.
6. **The flap guard.** A source can swing its *delivered* rate with no setting
   change at all, dwelling minutes at each end; observed live: the 30 fps
   camera on this account halves its delivered rate under marginal light and
   comes back. Both dwells outlast two settled batches, so both ends confirm,
   and without this gate the publisher rebuilt each way at the minimum-interval
   floor for as long as the light stayed marginal. The flapping **signature** is
   a confirmed return to the very rate the last rate rebuild moved away from,
   within `RATE_FLAP_WINDOW_S` (30 min) of it. It is resolved **once, to the
   higher of the two rates**: held high the output pads with duplicates
   (nothing lost, per the table above), held low it would drop real frames;
   and then held there: confirmed changes back down to the flap's low rate are
   refused for as long as settled batches keep sighting the high one, and for
   `RATE_FLAP_HOLD_S` (1 h) past the last such sighting. A *real* setting
   change to the low rate stops the sightings, outlives the hold, and is
   applied late rather than never: an hour of duplicate padding is the cost,
   chosen over a night of reader-kicking rebuilds. The resolution is logged
   once as a warning (`the delivered rate is flapping between …`); the
   refusals that follow are routine and are not warning-level noise.
7. **Apply, at a rotation.** A confirmed change updates `known_fps` immediately
   and sets a flag; the publisher is rebuilt at the **next session rotation**,
   through the same `sink_wh`-style check a frame-size change uses. It is never
   raised from the streaming loop, because **a rate change must never take the
   camera off the air**: the rotation gap is already bridged by the frame hold,
   so the change costs the one glitch a rebuild costs, not a glitch plus a
   failed session plus a backoff. An audio-format change is applied at the same
   point, by the same rule, and for the same reason. The flag is
   re-checked against the current reading at that point, so a rate that wandered
   and came home rebuilds nothing.
8. **Bound.** `RATE_REBUILD_MIN_INTERVAL_S` (300 s, a little under two rotations)
   is the floor between two rate-driven rebuilds, so even a source that defeats
   every gate above cannot cost more than one glitch per window. It delays a
   correction; it never cancels one: the confirmed evidence is kept through
   the hold, so the first agreeing batch after the floor acts on it.

**What a rate change does to the queue and the frame hold:**

- **The decoded-frame queue is not resized; it is rebuilt.** It is per-session and
  sized in *time* at construction (`video_queue_maxsize(known_fps)`), so the next
  session after a confirmed change simply gets the right depth and the old queue
  dies with the session that owned it. **No frames are stranded**: the retiring
  session's queue is discarded with the session, exactly as at every other
  rotation. Note that between 16 and 30 fps the depth is pinned at the 8-frame
  memory ceiling, so a rate change inside that band changes the queue depth by
  nothing at all; the queue depth is not an input to the rebuild decision, so
  that costs no needless rebuild.
- **The frame hold keeps repeating at the rate the *running* ffmpeg was declared
  with** (`sink.fps`), not the newly measured one. Between a confirmed change and
  the rotation that applies it those two deliberately differ, and a repeated frame
  only means "one frame" to an ffmpeg reading `-framerate N`.
- **The audio top-up cadence is unaffected at any rate.** The hold ticks at
  `min(1/fps, AUDIO_SILENCE_TOPUP_S)`, and the rate is clamped to 1–30, so the
  video interval is at most 1/30 = 33 ms and the tick can never exceed the 50 ms
  top-up, half the 100 ms muxer lead the whole pacing fix depends on. No measured
  rate can push the audio half of that loop past the lead.
- **The hold's audio top-up is never gated behind its video write.** The
  repeated frame is written by a task of its own and reaped on a later tick, so
  the top-up runs on every tick whatever the video write is doing. That is
  load-bearing rather than tidy: `write_frame`'s drain completes only once ffmpeg
  has read the frame, and ffmpeg reads the video pipe only while it holds audio
  covering that frame's timestamp, so a video write awaited inline blocks on the
  very audio write that would release it. The previous session's pump is already
  stopped and the next one does not exist yet, so nothing else can advance the
  timeline and neither writer moves again. Exactly one video write is in flight
  at a time (a second would be a second writer to the raw video pipe) and it is
  always finished before the hold task is, so the guarantee that nothing is
  writing to the publisher once the hold has stopped is unchanged.

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
| `-bf 0` | No B-frames: HA's PyAV stream worker must not have to reorder frames without DTS info. |
| `-g <fps>` | GOP tied to the measured fps (not a fixed frame count) → consistent ~1s keyframe interval regardless of camera frame rate, and clean alignment with HA's 2.0s HLS segmenter. |
| `-pkt_size 1200` | Keep RTP payloads under MediaMTX's repacketization threshold; letting MediaMTX repacketize strips DTS continuity ("No dts in N consecutive packets" in HA). |
| `-use_wallclock_as_timestamps 1` | Makes the video timeline advance with real time, including while held frames are repeated during a rotation gap. **Video input only**: the audio input's timeline is its own sample count (see "Downstream (listen) audio"). |
| `-framerate <fps>` before `-i` | Declares the camera's frame rate to the **rawvideo demuxer**, so the output stays CFR at that rate and `-g <fps>` above spans the ~1s it is chosen for. Without it, the demuxer's default of 25 fps applies to every camera regardless of its real rate, which duplicates frames for a slower one and *drops* them for a faster one. It does **not** touch the packet timestamps, which is what separates it from `-r` below. |
| `-r <fps>` before `-i` | **Never used, on either command line.** It forces the input *stream's* frame rate and in doing so *overrides* `-use_wallclock_as_timestamps`: the timeline then advances by 1/fps per frame **written** instead of per second **elapsed**, so a camera delivering fewer frames than the fps it was measured at publishes proportionally less media than wall time and an HLS consumer falls progressively behind live. With a second input it is worse than silent; see below. |
| `-rtsp_transport tcp` | RTSP over TCP to MediaMTX. |

When `JANUS_CONSUME_AUDIO` applies to the camera, the same subprocess gets a
second raw-PCM input on its own fd and the output gains
`-map 0:v:0 -map 1:a:0 -c:a aac -ac 1 -ar <rate> -b:a 64k -af aresample=async=1`.
The audio input also carries `-analyzeduration 0 -probesize 32`: raw PCM's
parameters are fully described by `-f`/`-ar`/`-ac`, so there is nothing to probe
for, and without it ffmpeg would wait on input 1 before starting the output at
all. See "Downstream (listen) audio" below for the rest of that path.

#### How the video input declares its frame rate

The video input is **identical on both command lines** (the audio fragments
above are the only difference between them) and how it declares the camera's
frame rate is load-bearing in two opposite directions.

`-r <fps>` before `-i` forces the input stream's frame rate and overrides
`-use_wallclock_as_timestamps`, so the timeline advances 1/fps per frame
*written* rather than per second *elapsed*. On its own that is a silent defect:
a camera delivering fewer frames than the fps it was measured at publishes less
media than wall time, nothing errors, and an HLS consumer drifts further behind
live for as long as the stream runs. With a second input it is fatal, because
the audio timeline really is real time (the silence fill is sized from the
wallclock): audio outruns video, the muxer's interleaver stops accepting audio
packets, ffmpeg stops draining the pipe, and the sink rebuilds the publisher
every few seconds, kicking every RTSP reader each time.

Declaring nothing is not the alternative. The rawvideo demuxer's own `framerate`
option **defaults to 25** (`ffmpeg -h demuxer=rawvideo`), so an input with no
rate declaration is re-rated to a constant 25 fps whatever the camera actually
does: frames are duplicated for a slower camera and *dropped* for a faster one,
and `-g <fps>` then spans a different number of seconds than the ~1 s it exists
to produce. `-framerate <fps>` declares the rate to the demuxer without touching
the timestamps, which is the combination this pipeline needs.

Measured inside the deployed container image (ffmpeg 7.1.5, `h264_vaapi`,
1280×720 → 960×540, publishing live RTSP into MediaMTX), 40 s of wall time per
run. The `libx264` path was measured alongside it and matches row for row.

| Declared / fed | Rate declaration | Media per 40 s wall | Frames written → encoded | Output rate | Keyframe gap |
|---|---|---|---|---|---|
| 15 fps / 9 fps | `-r 15` | 24.07 s | 361 → 361 | 15 fps | 1.0 s |
| 15 fps / 9 fps | *(none)* | 40.04 s | 361 → 1001 | 25 fps | 0.6 s |
| 15 fps / 9 fps | `-framerate 15` | 40.07 s | 361 → 601 | 15 fps | 1.0 s |
| 30 fps / 30 fps | `-r 30` | 40.03 s | 1201 → 1201 | 30 fps | 1.0 s |
| 30 fps / 30 fps | *(none)* | 40.08 s | 1201 → 1002 (**199 dropped**) | 25 fps | 1.2 s |
| 30 fps / 30 fps | `-framerate 30` | 40.03 s | 1201 → 1201 | 30 fps | 1.0 s |
| 6 fps / 6 fps | `-r 6` | 40.17 s | 241 → 241 | 6 fps | 1.0 s |
| 6 fps / 6 fps | *(none)* | 40.08 s | 241 → 1002 | 25 fps | 0.24 s |
| 6 fps / 6 fps | `-framerate 6` | 40.17 s | 241 → 241 | 6 fps | 1.0 s |

The `-r` rows at a frame shortfall are the defect: 40 s of wall time published as
24 s of media, exactly the 9/15 ratio of written-to-declared fps. The *(none)*
rows are why removing the flag is not enough by itself.

With the audio input attached, same build, declared 15 fps while frames were
written at 9 fps, the shortfall that stalls:

- `-r 15`: the audio backlog crossed the sink's 512 kB limit at 9.6 s, video
  timeline 24.07 s per 40 s of wall time.
- *(none)*: backlog flat at 0 bytes across a 100 s RTSP publish, timeline
  100.0 s/100.0 s, and ffmpeg logs `More than 1000 frames duplicated`, the
  re-rate to 25 fps.
- `-framerate 15`: backlog flat at 0 bytes across a 100 s RTSP publish, timeline
  100.0 s/100.0 s, output CFR 15 fps, no duplication warning.

**This is not a quirk of the image's ffmpeg version:** the same reproduction and
the same behaviour hold on ffmpeg n9.0.1. Everything else that looks like a
candidate still stalls at ~10 s on both builds while `-r` is present:
`-thread_queue_size` on either or both inputs, dropping
`-analyzeduration`/`-probesize`, `-use_wallclock_as_timestamps` on both inputs or
on neither, `-fps_mode passthrough`/`vfr`, `-max_interleave_delta 0`, dropping
`-af aresample`, and swapping the input order.

Frames larger than `MAX_ENCODE_DIMENSION` (960px on the larger side) are
downscaled at the encode step. Some cameras publish natively at 1440×1440, which
is fine for the pipeline but can exceed what a modest viewer's decoder can sustain
continuously alongside a second camera (observed as a decoder/paint stall: RTP
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
server's `Retry-After`. Every retry path (startup login and the reconnect loop)
sleeps for that stated wait via `config.rate_limit_sleep_s`, **not** the fixed
`2/4/8/16/30s` backoff, because a short fixed schedule just re-enters the same
rate-limit window instead of clearing it (this compounding is what turned one
incident into a multi-hour outage). The sleep is clamped only by
`RATE_LIMIT_RETRY_MAX_S` (30 min) as a guard against an absurd header value, plus
a small margin. A rate-limit failure is also **not** counted toward the
consecutive-failure escape hatch, since restarting the container only re-runs the
login and re-triggers the window.

## Login serialization

All `TelusSession.login()` call sites (startup, the 401-triggered relogin, the
control-endpoint credential push, and the fallback-file watcher) are serialized
through one shared `asyncio.Lock` (`login_lock` in `main.py`). `login()` clears
the entire cookie jar with no locking of its own, so two concurrent logins could
otherwise interleave into a broken cookie state. On top of the lock,
`relogin_if_stale` compares a `login_generation` counter across the lock boundary
and **skips** a redundant re-login if another caller already refreshed the
session, collapsing the burst of simultaneous 401s that happens when the session
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
| `RESTART_GUARD_MAX_EXITS` | 3 | Exits allowed in the window before throttling: kept immediate so a genuine one-off wedge recycles as fast as ever. |
| `RESTART_GUARD_BACKOFF_INITIAL_S` | 60s | First throttled hold, doubling per extra exit (60, 120, 240 …). |
| `RESTART_GUARD_BACKOFF_MAX_S` | 900s | Cap: well past Cloudflare's ~101s Retry-After. |

Beyond the free allowance, `record_exit()` hands the caller a hold to sleep
**before** exiting. The sleep runs in-process, so Docker physically cannot restart
the container until it finishes (a hard floor on restart frequency) while the
other cameras and the control endpoint keep serving. The exit is recorded before
the sleep (a `docker restart` or OOM kill mid-sleep still counts), reads/writes
happen without an intervening `await` (two cameras can't lose each other's entry),
and the state file is written atomically (`.tmp` + `os.replace`). It is
**fail-open**: any error reading or writing the state degrades to "exit
immediately": a broken guard must never be why a genuinely wedged bridge can't
recycle. The escape hatch never *cancels* an exit; a real wedge still gets its
fresh process, just not at loop speed.

The hatch itself fires at `SESSION_WEDGE_TIMEOUT_S = 1800`: thirty minutes in which
one camera produced no successful frame batch, the clock starting at the first
failure after the last success and cleared by any success. It is a duration
rather than a count of failures because those are not the same question: a
count cannot separate a wedged local resource from a flaky cloud, since both
produce failures, and only whether the camera comes back separates them.
Every session attempt is bounded by the webrtcup, first-frame and frame-silence
timeouts, so a run cannot reach thirty minutes without many attempts inside it.
A recent frame from ANOTHER camera in the same process
(`SESSION_WEDGE_PEER_FRESH_S`) vetoes the exit entirely: a wedged resource is a
property of the process and takes every camera down with it, so a process still
publishing for a sibling is demonstrably not wedged, and restarting it cannot
reach the far end of the one path that is failing. On a single-camera
deployment there is no peer and the duration alone decides. The calibration
evidence for both constants lives in `config.py` beside them and in the
backlog (E89).
It uses `os._exit()` (not a
graceful shutdown) deliberately: a graceful path runs through the very cleanup
code suspected of being wedged, so a hard exit lets the OS reclaim every
fd/socket/child unconditionally.

## The outbound call leg (`bridge/sip_call.py`)

A minimal SIP/RTP user agent that places one authenticated outbound call
through a VoIP.ms POP: INVITE with MD5 digest auth (with and without
`qop=auth`), SDP offer/answer pinned to G.711 mu-law (PCMU), symmetric RTP,
BYE. Exposed as `POST /control/call/test`, which rings the configured target
and plays a tone on answer; inert unless the deployment provides a sub-account
(see `docs/configuration.md`). `bridge/rtsp_listen.py` is the call's UPLINK: it pulls the doorbell's own audio
directly on the LAN over the device's RTSP-in-HTTPS tunnel (the app's path),
bypassing the cloud Janus relay whose churn otherwise fragments a call. It reads
the codec from the stream's SDP (never assumed), demuxes interleaved RTP from
RTSP replies, and decodes to PCM. `bridge/call_shape.py` conditions each
direction for clarity with low-latency in-process DSP (high-pass + soft AGC),
tunable and defaulting to sensible values.

`bridge/call_bridge.py` orchestrates the full bridged call. Uplink: the
doorbell mic arrives over the direct-LAN listen (`rtsp_listen`), is shaped
(`call_shape`), passes the echo ducker (`EchoDucker` -- while decoded caller
audio is active the uplink is attenuated hard, the half-duplex bias every
intercom uses, because no AEC exists anywhere in this path), and waits in a
capped `UplinkMulawBuffer` (shed-oldest; the cap is the uplink's mouth-to-ear
latency ceiling) for the SIP leg to pull 20 ms frames. Downlink: the SIP
leg's received RTP is shaped and fed to a `talk_fullduplex.LiveMulawSource`
(same shed-oldest cap) that talkdown's chunked-PUT transport drains, batching
backlog into larger PUT bodies so the drain holds real time when the device
acks slowly. Every leg is mu-law at 8 kHz, so nothing transcodes. For the
call's duration the called camera's always-on video pipeline is paused and
then resumed (`CALL_PAUSE_VIDEO_DURING_CALL`) so the device's encoder serves
the call, and `CALL_DEBUG_CAPTURE_DIR` can record all three streams for
analysis. One call at a time; either half ending ends the other. Started
with `POST /control/call/doorbell`, stopped with `/control/call/stop`.
(The feature is currently shelved with one open defect; backlog E99 carries
the live-round evidence and the resume point.)

The feature's credentials and gate are Home-Assistant-managed exactly like the
TELUS credentials: the options flow writes `call_settings.json` to the shared
dir and live-pushes `/control/call/settings`, and `config.py` only bootstraps
from the env. No value is hardcoded in the bridge.

Hand-rolled subset of RFC 3261 rather than a SIP library, for the same reason
talkdown re-implements its protocol: the leg needs exactly one scenario, and
the libraries that cover it are alpha-quality or C toolkits with their own
event loop to embed. UAC only -- nothing listens for inbound calls and
REGISTER is never sent (registration routes inbound calls; an authenticated
INVITE stands alone). One call at a time, enforced with a lock at the control
route. The SIP password is used for digest computation only and never appears
in a message or a log.

`tests/test_sip_call.py` runs the whole state machine against a fake POP that
verifies the digest with an independent implementation, answers with SDP and
collects the RTP; see `docs/development.md` for what silent failure each
assertion exists to catch.

## Container and runtime

Defined by `Dockerfile`, `entrypoint.sh`, and `docker-compose.yml`.

- **Base image**: `python:3.14-slim`. Installs `ffmpeg`, `intel-media-va-driver`,
  and `mesa-va-drivers` (userspace VAAPI drivers for Intel and AMD, keyed off the
  runtime PCI device, so both ship unconditionally). `h264_vaapi`/`h264_nvenc` are
  already in Debian's ffmpeg; no extra package is needed for NVIDIA. All Python
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
  `render` GID (`RENDER_GID`, default `992`; must match `getent group render` on
  the host). The commented-out `deploy.resources` block is the opt-in path for
  NVIDIA instead.
- **Shared directory**: the host path (`HOST_SHARED_DIR`, default
  `/var/lib/homeassistant/share/telus_cam_bridge`, the Supervised layout) is bind
  mounted at `/shared`, the same physical directory the HA container mounts, so
  both read/write `rtsp_password`, `control_secret`, `credentials.json`,
  `restart_guard.json`, and `mobile_auth.json` without extra host config.
- **Resource caps**: `mem_limit` 768m and `cpus` 2.0 by default, generous
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
because Compose interpolates `$` in `env_file` values: a password with a literal
`$` must be escaped as `$$`; `install.sh` does this automatically). Key
environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `TELUS_EMAIL` / `TELUS_PASSWORD` | (none) | SSO credentials (required; secrets.env). |
| `ENCODE_BACKEND` | `vaapi` | Hardware encode path (`vaapi` / `nvenc` / `software`). |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | Render node for VAAPI. |
| `MEDIAMTX_RTSP_PORT` | `8554` | RTSP listen port. |
| `CONTROL_PORT` | `8199` | Loopback control API port (see [control-api.md](control-api.md)). |
| `LOG_LEVEL` | `INFO` | Python logging level. |
| `CONTROL_ACCESS_LOG` | `false` | Per-request access logging on the control endpoint; off by default (see [configuration.md](configuration.md)). |
| `HANDOVER_TRACE` | `false` | One structured line per session rotation describing the handover window; off by default (see [the section below](#instrumenting-the-handover-window)). |
| `MEMORY_TRACE` | `false` | One structured line per interval saying which process in the container holds the memory; off by default, and **not** safe to leave on (see [the section below](#instrumenting-the-containers-memory)). |
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
device id, the cause, and, where the code knows it, what happens next.
Per-request access lines are off by default (`CONTROL_ACCESS_LOG`) so routine
HA polling cannot age real warnings out of that window. Log lines never carry
secrets: signed snapshot URLs, media-URL query strings (which can hold signed
tokens), RTSP/PUT credentials and session tokens are all redacted before
logging.

A few INFO diagnostics ride along, one line each per session start (so about one
per rotation, per camera):

- **The first-frame latency**, carried on the existing streaming line:
  `streaming 1440x1440@15fps (first frame after 1.6s)`, measured from the moment
  the loop starts waiting for a first frame. It shares a line with the size/fps
  it belongs to rather than taking one of its own. This is the margin against
  `FIRST_FRAME_TIMEOUT_S` (20s): a camera whose first frame lands at 1.6s and one
  whose first frame lands at 15s look identical in every other line, but only the
  second is one blip from the reconnect loop below.
- **Which `JANUS_REQUEST_AUDIO` mode applied to this camera**:
  `JANUS_REQUEST_AUDIO mode 'listed' -> mountpoint create WITH audio`, with the
  mode being `off`, `all`, `listed` or `unlisted`. The knob is per-camera, so the
  env var alone no longer says which cameras it reached.
- **The Janus offer's media summary**: the media kinds, ports and codec names
  the gateway offered, e.g. `janus offer media: video(port=9,H264)`. It is a
  *derived summary by construction, and must stay one*: a raw SDP carries
  `a=ice-ufrag`, `a=ice-pwd`, `a=fingerprint` and `a=candidate`, so only `m=`
  and `a=rtpmap:` lines are read and every token emitted is filtered against a
  conservative pattern. Turning the root log level up to `DEBUG` is **not** an
  equivalent way to see this; aiortc logs the entire raw SDP at debug, which
  would put ICE credentials into the container log.
- **The videoSource attribute inventory**: the attribute *key names* the
  live-video source document carries, plus `supportsAudio` and whether
  `audioUpStreamUrl` is present. Key names and one boolean only; never a value,
  because the same document holds the Janus token and the ICE server
  credentials.

### Instrumenting the handover window

`HANDOVER_TRACE` (off by default, implemented in `bridge/handover_trace.py`)
adds **one structured line per session rotation, per camera**:
`handover trace {...}`, where everything after the prefix is a single JSON
object. It exists because the rotation window is where the doorbell's remaining
frame drops live, and because the ordinary log cannot show that: the drop path
logs the first drop of a session and then every 150th, so a session that drops
fourteen leaves one line and no way to place it against anything.

The record carries, for one rotation: when the previous session's audio pump
actually stopped, when the frame hold took over the audio pipe, how often it
ticked and how much audio it actually wrote through the gap, the **muxer lead**
at every step (how far the audio
timeline runs ahead of the wallclock-stamped video, the quantity every pacing
constant here is chosen against), when the new session's first frame arrived and
when it was written to ffmpeg, when its pump first wrote, the video queue depth
at each of those, and the timestamp of every dropped frame relative to the
rotation. Each mark pairs `dt` (wall clock since the previous mark) with `adv`
(audio actually written since it), so an interval where the timeline stopped
advancing is visible as `adv` of zero across a long `dt`.

**A call and a write are different facts and the record keeps them apart.**
`hold.silence_calls` is the hold's cadence, `hold.silence_writes` is how many of
those calls put samples on the pipe, and `hold.silence_ms` is how much audio they
carried, read off the sink's own sample counter. They differ routinely and
legitimately: a top-up made while the audio timeline is still ahead of the wall
clock writes nothing and succeeds. The same rule applies to the repeated video
frames, where `hold.video_issued` counts the writes started and
`hold.video_writes` counts the ones ffmpeg accepted, so a video pipe ffmpeg has
stopped reading is visible from inside the gap rather than only from the frames
that go missing afterwards.

Three properties are deliberate:

- **It is off by default and costs nothing when off.** `handover_trace.begin()`
  returns `None` and every call site is an `is not None` test on that value:
  6.4 ns per frame against a 66.7 ms frame interval, no allocation, and nothing
  at all inside `write_frame`. Instrumentation that perturbs a window whose
  problem is frames going missing inside it would be worse than none.
- **Nothing it does awaits, sleeps or writes**, and it schedules no sampler of
  its own; marks are taken where the loop already is. On, a whole traced
  rotation costs ~70 µs of CPU and ~1.5 kB of log.
- **The records go to the log, not to `/control/status`.** A handover is an
  event; `/control/status` is a snapshot contract Home Assistant polls
  continuously and derives entities from, so a record served there would be
  reported an arbitrary number of times or not at all depending on how the poll
  cadence fell against the 165 s rotation, and a debug blob that is null on
  almost every poll would cost that contract more than it buys. In the log the
  records sit in order, next to the `proactive session rotation` and
  `dropped N frame(s)` lines they have to be read beside.

Its limits are stated where it is read: the lead is what the bridge has handed
ffmpeg, not what ffmpeg's muxer has consumed; a stall is located between the two
marks that bracket it and no more finely. Field-by-field, with what a healthy
rotation and a stalled one look like:
[troubleshooting.md](troubleshooting.md#measuring-what-a-session-rotation-costs-the-audio-timeline).

### Instrumenting the container's memory

`MEMORY_TRACE` (off by default, implemented in `bridge/memory_trace.py`) adds
**one structured line every `MEMORY_TRACE_INTERVAL_S`**: `memory trace {...}`,
where everything after the prefix is a single JSON object.

It exists because the container's memory figure is a **sum over four
processes**, and nothing else in this repository decomposes it. `entrypoint.sh`
runs MediaMTX and `python3 -m bridge.main` side by side, and the bridge launches
one ffmpeg per camera; all of them count against `docker-compose.yml`'s
`mem_limit`. So "the bridge is leaking" is a claim about that sum, and the
candidate mechanisms behind an audio-correlated climb have different fixes in
different places: MediaMTX buffering a second (AAC) track per stream for its
readers, ffmpeg's two-input mux holding packets to interleave them, the Python
heap retaining decoded frames, or glibc holding freed memory in per-thread
arenas rather than returning it. Three of those four are not fixable from this
repository at all.

The record therefore answers four questions, outermost first:

| Field | Answers |
| --- | --- |
| `container.procs` | **which process** holds the megabytes, largest first, with `container.rss_mb` as the sum over all of them. If the growth is not in `python3`, nothing else in the record matters. |
| `glibc` | **the heap or the allocator**: `in_use_mb` against `free_not_returned_mb`. Freed memory sitting in arenas is a large and growing `free_not_returned_mb` against a flat `in_use_mb`, which is the plateau behaviour E74 measured. `tracemalloc` cannot see this at all, because it only tracks the Python allocator. |
| `python.gc_growth` | **what kind of object** grew since the previous record, by type. A retained `av.AudioFrame` per decoded frame shows here and nowhere else. |
| `tracemalloc.top` | **which line** allocated it, sorted by `d_mb` (growth since the previous record) rather than by size, so a large steady buffer does not outrank the thing that is actually growing. |

Consecutive records are `MEMORY_TRACE_INTERVAL_S` apart, and the default (330 s)
is two of the ~165 s session rotations, so any two of them bracket at least one
whole rotation per camera: a per-rotation retention appears as a step between
records rather than as noise inside one.

Two properties are deliberate, and one of them is the opposite of
`HANDOVER_TRACE`'s:

- **It is off by default and builds nothing when off.** `memory_trace.start()`
  returns `None`, no sampler task exists, and `tracemalloc` is never enabled.
- **It is NOT safe to leave on.** `tracemalloc` roughly doubles per-allocation
  cost and stores a frame per live allocation, and every sample walks every
  gc-tracked object. It also adds to the number under investigation, so read
  `python.rss_mb` from the record and never `tracemalloc.traced_mb` when
  deciding whether a climb is still there. Turn it on to attribute a climb,
  then turn it off.

Reading one: [troubleshooting.md](troubleshooting.md#attributing-the-containers-memory).

### Downstream (listen) audio

By default the bridge publishes **video only**, and the Janus mountpoint it
creates asks for video only. Two independent per-camera knobs move it off that
default, and they are separate on purpose: asking for the track and publishing
it have different risks:

| Knob | What it does |
| --- | --- |
| `JANUS_REQUEST_AUDIO` | adds the audio keys to the mountpoint-create body, so the gateway offers a downstream audio track |
| `JANUS_CONSUME_AUDIO` | decodes that track and publishes it on the same RTSP path as the video |

A camera that offers an audio track nothing is consuming has that track
**drained and discarded** rather than merely ignored: aiortc auto-accepts every
media section the remote offers and decodes it on its own thread into an
unbounded queue, so a track nobody reads would grow for the whole life of each
session, a per-rotation memory sawtooth. Draining holds it at zero; the
decoder thread itself is released when the session closes. (Stopping the track
instead would be worse than doing nothing: it stops the queue being readable
without stopping it being filled.) Consuming empties the same queue; it just
keeps the frames.

`JANUS_CONSUME_AUDIO` **requires** `JANUS_REQUEST_AUDIO` to name the same
camera. A camera named only by the consume knob is never offered an audio track
at all, so that combination is logged once and treated as off: building an
audio-enabled publisher whose second input can only ever carry silence would be
worse than doing nothing, because the mux then has to interleave the video
against a stream that carries no content.

#### The audio path

The decoded audio track is pumped into a bounded per-session queue (drop-oldest,
with its own counter: a live stream cares about staying near real time, not
about completeness) and written into a **second input on the same ffmpeg
process** that is already publishing the video. Output is **mono AAC** at the
sample rate the decoded frames actually carry, never an assumed one. AAC is
forced by the consumers, not chosen for quality: HA's HLS stream worker accepts
only aac/mp3, and Frigate's `-c copy` recording preset needs an mp4-legal codec.

Five properties of that path are load-bearing:

- **The audio input paces the whole publisher, and it must stay AHEAD of the
  video.** ffmpeg's muxer can only emit a video packet once it holds audio
  covering that packet's timestamp. The audio pipe is primed with
  `_AUDIO_PRIME_MS` (100 ms) of silence the instant it opens, which is both what
  lets `avformat_find_stream_info` complete and the **lead** the audio keeps over
  the wallclock-stamped video. That lead decays by elapsed real time between
  top-ups, so `AUDIO_SILENCE_TOPUP_S` (0.05 s) must stay comfortably under it,
  and an ordinary top-up may never exceed it either
  (`_AUDIO_SILENCE_MAX_BURST_S` = 0.1 s).

  **The lead is a floor, not a fixed quantity**, and the two writers move it in
  opposite ways. `write_silence_to` tops the timeline up *to* the wall clock, so
  it can raise the lead to zero and no further: it stops the lead going negative,
  it does not restore the prime. `write_audio` has no wallclock term at all and
  writes whatever a decoded frame carries, whenever it arrives, so a queued
  backlog drained by the audio pump legitimately puts the timeline **seconds
  ahead** of real time. Both matter when reading a handover record: a `lead` far
  above 100 ms is a correct reading, and a frame hold that ticks perfectly while
  writing nothing at all is what a big positive lead looks like from the other
  side.

  **A deficit past `_AUDIO_CATCHUP_DEFICIT_S` (0.5 s) is closed in one write**,
  not repaid at the ordinary ceiling. The two regimes exist because the damage is
  in the *duration* of a negative lead rather than in the deficit itself: at the
  shipped cadence the ordinary ceiling repays at about twice real time, so
  clearing a 3 s hole would mean 3 s of ffmpeg not reading the video pipe and
  every frame behind it lost. That is not in tension with the table below, it is
  its other half: what stalls video is the instant of zero lead before a write,
  never the size of the write, and a catch-up cannot even run ahead, since it
  still only tops up to the wall clock. The catch-up's own bound is in **bytes**
  (`_AUDIO_CATCHUP_MAX_BYTES`, a quarter of `_AUDIO_PIPE_MAX_BUFFER`) rather than
  in seconds, because "a long stall must not dump minutes of silence in one
  write" is a quantity of bytes: the same three seconds is 48 kB at the 8 kHz
  mono these cameras send and 576 kB at 48 kHz stereo. Anything past the bound is
  still owed and still repaid across later calls.

  **Rebasing the audio origin instead, and accepting the gap as dead air, is
  wrong here** and is worth stating because it looks like the obvious shortcut.
  The audio input carries no `-use_wallclock_as_timestamps`, so its PTS *are* its
  sample count: skipping N seconds of samples does not skip N seconds of
  presentation time, it makes the audio stream N seconds shorter than the
  wallclock-stamped video beside it, and every later sample is presented N
  seconds early for the life of the publisher. `-af aresample=async=1` cannot
  correct that, because it fills and trims against the audio's own timeline
  rather than against the video's. A slow repayment is a bounded, one-off video
  stall; a rebase is a permanent A/V offset.

  Getting this wrong is **invisible from the audio side**: the published audio is
  correct and the published video is correct, and the only symptom is video
  frames going missing a layer away, as `dropped N frame(s) -- consumer falling
  behind`. Measured in the deployed container (ffmpeg 7.1.5, h264_vaapi,
  1440×1440 → 960×960 at 15 fps, 900 frames per 60 s run, timing the write that
  feeds the video pipe). **The limit of this evidence:** the runs drive the real
  command line with a synthetic feed and output to `-f null -`, not a live RTSP
  publish into MediaMTX, so they measure the demux/interleave behaviour and not
  whatever a real muxer target adds. The shipped 0.5 s row reproduces the live
  defect's drop rate to the frame, which is the reason to trust the rest:

  | Silence top-up cadence | write_frame p50/p90/p99 (ms) | Frames stalled past one frame interval |
  | --- | --- | --- |
  | video-only (control) | 1.81 / 1.94 / 2.87 | 0 of 900 |
  | 0.5 s | 1.80 / 231.6 / 297.1 | 120 of 900 (13.3%) |
  | 0.1 s (= the lead) | 1.90 / 202.8 / 297.1 | 162 of 900 (18%) |
  | 0.08 s | 1.81 / 1.97 / 2.27 | 0 of 900 |
  | 0.05 s (shipped) | 1.84 / 2.03 / 3.56 | 0 of 900 |
  | 0.025 s | 1.80 / 2.04 / 3.44 | 0 of 900 |

  The cliff is at the lead, not at any particular write size: a 0.1 s cadence
  hands ffmpeg a fifth as much per write as 0.5 s and stalls *more* frames.
  Handing over more than is owed is harmless by comparison: the same rig fed at
  twice real time showed no video stall at all, but 10.9 MB of audio backed up in
  the bridge because ffmpeg stopped reading a pipe running a minute ahead, which
  is what the per-call ceiling and `_AUDIO_PIPE_MAX_BUFFER` bound. Real decoded
  audio arrives in ~20 ms frames and is written as it arrives, so it satisfies
  the cadence on its own; the top-up only has to cover the quiet, which on a
  doorbell is nearly all of the time.
- **An audio input that goes bad degrades the sink to video-only.** ffmpeg
  backing up past `_AUDIO_PIPE_MAX_BUFFER` (512 kB, ~2.7 s of un-read audio),
  the pipe closing under the sink, the pipe failing to open at `start()`, or a
  persistent run of decoded frames whose bytes disagree with their own declared
  shape (`_AUDIO_MALFORMED_LIMIT`, ~1 s of them): any of these drops the audio
  input and leaves the video publish untouched (`FfmpegSink._drop_audio`), the
  same judgement `start()` already makes for an unsupported sample format.
  Rebuilding the publisher instead destroys the MediaMTX path, kicks every RTSP
  reader and puts a discontinuity in front of every HLS viewer, which trades a
  working video feed for an audio input that has already gone, and for the
  malformed-frame case a rebuild cannot even help, since the relaunched command
  line would be byte-identical (the frames' *format* matched; only their size
  lied). A **one-off** malformed frame is not a drop at all: it is skipped and
  its span filled with silence from the wall clock, so the audio's lead over the
  video (the thing that keeps ffmpeg reading the video pipe) is unaffected.
  The degradation is latched for the life of that publisher (ffmpeg cannot
  reopen an input) and is reported as `audio_dropped` / `audio_dropped_reason`
  in `/control/status`.

- **One publisher, two inputs: the audio fd outlives every session.** The
  ffmpeg process deliberately survives the ~165 s rotations (see the rotation
  section below for why tearing it down blacks the path), and ffmpeg opens each
  input exactly once and cannot reopen one. So the audio pipe is opened **with
  the sink and never closed or reopened per session**: closing it at the end of
  a session would EOF that input permanently, with no way to attach the next
  session's audio to the running publisher. It is an anonymous `os.pipe()`
  passed to the child through `pass_fds` and addressed as `-i pipe:<n>`, not a
  FIFO: opening a FIFO for write blocks until a reader opens it, and ffmpeg
  only reaches input 1 after input 0 has produced a frame, which is a real
  deadlock rather than a theoretical one.
- **Rotation gaps are filled with silence, sized by the wall clock.** The video
  side bridges a gap by repeating the last frame; there is no audio equivalent,
  and without a fill the video would advance while the audio stalled. On a
  single-output mux that is not "a silence"; it is a permanently growing A/V
  offset, and it also holds the video back while the muxer waits on the quiet
  input. The fill therefore writes exactly
  `elapsed_seconds × sample_rate − samples_already_written` samples, derived
  from elapsed real time rather than from counting loop iterations: a repeated
  video frame is one frame whenever it lands, so `asyncio.sleep` jitter is
  invisible there, but a sample stream's duration *is* its length, so the same
  jitter accumulates as real drift. What the per-call ceiling clips is still
  owed; the next call recomputes it against the wall clock and keeps repaying,
  so nothing is lost and A/V sync is unaffected. The hold ticks at whichever is
  shorter (the camera's frame interval or the top-up cadence) and writes video
  on its own schedule, because one top-up per repeated frame is 167 ms on the
  6 fps camera, well past the lead. It rides the existing frame-hold lifecycle,
  which also guarantees the previous session's audio pump has stopped before the
  fill starts: one writer on the pipe at a time. A camera that is offered audio
  and then sends none gets the same top-up from the pump itself, so the muxer
  never waits on a permanently quiet input.
- **The audio input is sized from an observation or not at all, and a changed
  shape is repaired at a rotation.** ffmpeg is told the pipe's sample format,
  rate and channel count once on the command line and reads it as a fixed-size
  sample stream; a frame in a different shape does not fail, it reinterprets
  every later sample from the wrong offset: noise, plus permanent drift on a
  changed rate. So the shape has to be right, and the only shape known to be
  right is one that came off a real decoded frame -- read off the session after
  the first video frames land, without consuming it, and remembered across
  rotations because it is a property of the device rather than of a session.

  **A camera with nothing decoded yet gets a VIDEO-ONLY publisher.** Its audio
  input is added later, by one rebuild at a rotation, once real frames have said
  what shape they are; the per-session audio pump runs either way and is what
  observes them. A camera that never sends a single audio packet therefore
  publishes video, forever, with no audio input, no rebuild and no failed
  session.

  This is not a fallback, it is the whole mechanism, and the alternative is a
  livelock rather than a slower start. The input used to be sized from the
  48 kHz stereo shape the SDP implies whenever nothing had decoded, which on this
  account's cameras is wrong (the frames are 8 kHz mono, because the track
  advertised as Opus carries G.711 µ-law -- see "The audio track is decoded as
  the codec that arrives" below). The first real frame then tripped the pipe's
  format guard, the guard failed the session, and the restart threw away the
  decoder's per-session codec sniff -- so the next sizing window was blind for
  the same reason as the last. On a camera whose audio is sparse, that cycle
  never ended and the camera published nothing at all.

  **Rebuilds for the audio shape are counted and bounded**
  (`AUDIO_FORMAT_REBUILD_LIMIT`, 1 per camera per process). Learning the shape
  once is the expected cost; a shape that keeps moving is not worth a publisher
  teardown -- every RTSP reader kicked -- per rotation, so past the bound the
  audio input is dropped in place and the camera publishes video only for the
  rest of the process, visibly, in `audio_dropped` / `audio_dropped_reason`.
  Until the rebuild lands, a frame in the wrong shape is skipped and its span
  filled with silence, so the muxer lead the video pipe rides on is unaffected.

  Nothing in the pacing is written in terms of a particular rate: the prime, the
  per-call ceiling and the silence fill are all sample counts derived from the
  observed rate, so 8 kHz gives the same 100 ms lead as 48 kHz.

The audio stream's timeline is its own sample count, and the video's is the wall
clock (which is why no video input carries `-r`; see "How the video input
declares its frame rate" above). Stamping the audio input with the wallclock too
would tie its timeline to *when a write happened to be flushed* rather than to
how many samples exist, so a
burst after a stall would compress into no time at all. That is what makes the
wallclock-derived silence count load-bearing rather than cosmetic: the sample
count is the only thing tying the audio to real time, and therefore to the video
beside it. The asymmetry is a design choice, not a workaround: with no `-r` on
the video input the pipeline also stays drained with the flag on both inputs
(measured on 7.1.5; never run live that way). `-af aresample=async=1` (fill-and-trim only, no
stretching) absorbs what is left. The audio input and encoder are
spliced into the `common_in`/`common_out` fragments every encoder variant is
built from, so a hardware path that publishes audio while the software fallback
silently does not is not expressible.

`/control/status` reports `audio_consumed`, `audio_format`, `audio_codec`,
`publisher_audio_format`, `audio_format_rebuilds`, `decoded_audio_frames`,
`dropped_audio_frames`, `audio_dropped` and
`audio_dropped_reason` per camera, plus a process-wide `audio_decoder` block of
decode counters (see
[control-api.md](control-api.md)). `audio_format` is the shape of a frame that
was actually **decoded**, and is `null` until one has been, never the shape the
publisher was launched with. That shape has its own field,
`publisher_audio_format`, which is `null` while the publisher is video-only: the
two differing is what "a rebuild is owed at the next rotation" looks like from
outside, and `audio_format_rebuilds` is how many of the allowed rebuilds this
camera has spent. `audio_codec` names the decoder those frames came from,
`pcmu` or `opus`, which
is a different question from their shape and the one that went unanswered while
the bridge spent two days decoding µ-law as Opus.
`decoded_audio_frames` is what makes flow checkable at a glance: consumption on
and that counter stuck at `0` is a silent stream. It counts what this camera
DECODES, which for the rotation between learning a shape and rebuilding for it
is briefly more than what reaches the RTSP path; `publisher_audio_format` is the
field that says whether the publisher is carrying any. `audio_consumed` keeps
saying what the camera is **configured** to do; `audio_dropped` is what says the
publisher gave up on an input it had. Note that the frame-silence watchdog is
**video-only**: audio can stop while a camera still reports `streaming`.

#### The audio track is decoded as the codec that arrives, not the one advertised

`bridge/aiortc_opus_patch.py` replaces `aiortc.codecs.opus.OpusDecoder.decode`
at import time, and without it downstream audio does not work on this account's
cameras. It does two jobs.

**1. The codec mislabel.** The Janus offer advertises `audio(port=9,opus)`. The
bytes that arrive are **G.711 µ-law at 8 kHz**. aiortc believes the advertised
codec, so libopus rejects most of the stream and the small fraction that happens
to satisfy a TOC byte decodes as noise. Measured live with only the
packet-survival wrapper in place: the doorbell decoded **181 audio frames in
60 s** and the living room **268**, where 20 ms frames are 50/s. Roughly 6 to 9
percent of the published audio timeline was real and the rest was the sink's
silence fill, which is why it sounded like fragments jumping around rather than
like an outage.

The evidence that the payload is µ-law rather than damaged Opus:

- The dropped payloads themselves. The doorbell's head is
  `7effff7f7efe7ffffffffefefeff7efe`: alternating `0x7e`/`0x7f`/`0xfe`/`0xff` is
  µ-law encoding near-silence, which is what a doorbell microphone produces
  nearly all the time. The living room's is `332038a8a9bd2223989e1e289e9f3a1d`,
  ordinary varying audio as µ-law.
- The official Android app instantiates **no audio codec at all** (only
  `video/avc` and `video/raw`), which is what G.711 decoded in software looks
  like. Its `AudioFlinger` sets `notificationFrames to 160`, i.e. 20 ms at
  8 kHz, and two `AudioTrack` sessions delivered 252,160 and 212,480 frames:
  31.5 s and 26.6 s at 8 kHz, matching two roughly 30-second viewings. At 48 kHz
  those would be 5 s, i.e. the app would be as broken as the bridge was.
- Talkdown to the same doorbell works as µ-law at 8 kHz, and L16 is impossible
  on the device. The camera is a µ-law device in both directions.

So the decoder is chosen by **sniffing** the first `SNIFF_PACKETS` (20) packets
of each session with both candidates and committing to one for that session's
life. Deciding once rather than per packet is deliberate: per-packet fallback
pays for two decodes forever and can flap mid-stream, and on this pipeline a
flap is not cosmetic, a frame in the other codec's shape (48 kHz stereo against
8 kHz mono) trips `write_audio`'s format guard and rebuilds the publisher,
kicking every RTSP reader. Nothing is emitted until the decision is made, for
the same reason: one spurious Opus frame ahead of a µ-law stream is enough to
launch ffmpeg with the wrong `-ar`/`-ac`. The sniff window's frames are
**buffered, not dropped**, and the winner's are handed over the moment it is
chosen, so measuring costs no audio.

The 20-packet window is ~400 ms at 20 ms framing, which is inside the
ten-video-frame batch (~670 ms at 15 fps) that the publisher's sizing peek
happens at the end of -- so a camera whose audio is continuous publishes it from
its very first session. **That is a latency property, not a correctness one.**
Sparse audio misses the window by a long way (20 packets is 1.28 s of continuous
audio at the 64 ms packetisation these cameras appear to use, and a quiet room
does not send that), and what a missed window costs is one rotation of
video-only publishing, not a mis-sized publisher: see "The audio input is sized
from an observation or not at all" above. Shortening the sniff to widen the
margin is explicitly rejected -- it weakens the codec decision to make a race
less likely rather than removing it, and the race is already gone.

The committed decoder is aiortc's own `aiortc.codecs.g711.PcmuDecoder`, not a
hand-rolled µ-law table, so its frames carry the `pts`, `time_base`, sample
format and layout the rest of the pipeline expects (s16, mono, 8000 Hz,
interleaved). Its `pts` is the RTP timestamp read on a 1/8000 time base; whether
the gateway stamps at 8 kHz or at the advertised 48 kHz is **unverified**, and
harmless either way, because nothing downstream reads it (the ffmpeg audio input
carries no `-use_wallclock_as_timestamps` and its timeline is its own sample
count).

**2. Surviving a packet nothing can decode.** Upstream, aiortc's two decoders
are not written the same way. `H264Decoder.decode` wraps its decode in
`try/except av.FFmpegError`, logs and returns no frames, so a bad packet costs
one packet. `OpusDecoder.decode` has **no handler**, and neither does
`rtcrtpreceiver.decoder_worker`, which is a *thread target*. So one undecodable
packet kills that session's `audio-decoder` thread outright, and the thread is
started exactly once per receiver with no restart path.

Nothing above notices. The receiver gates further RTP on the Thread *object*,
which stays truthy after the thread dies, so packets keep being queued to nobody
(~8,000 per session); the frame-silence watchdog is video-only; and a sink that
already has an audio input keeps it alive with silence, so the RTSP path carries
a perfectly valid, perfectly silent AAC track. (A decoder that dies before ever
producing a frame now leaves the publisher video-only instead, since the audio
input is only ever built from an observed frame; that is visible as
`publisher_audio_format: null`, but it is still not a fault this bridge can
repair.) Measured on the live published stream before
any of this: **9,572,352 of 9,572,352 samples at the s16 noise floor**, with
`max_volume == mean_volume` (not one nonzero sample) across eleven consecutive
sessions, each of which logged `Exception in thread audio-decoder` and left zero
live audio-decoder threads. The bridge's own audio pump also leaked a task per
session, waiting forever on a `recv()` whose end-of-stream sentinel only the dead
worker could produce.

That survival behaviour is the floor and is unchanged: a packet the **committed**
decoder rejects is dropped, the thread lives, and video publishing is untouched,
mirroring h264.py exactly. A camera that really does send Opus commits to Opus
and behaves as it did before the sniff existed.

**Counters, because one log line per session is how this hid.** The drop log has
to be once per session (a per-packet WARNING on a ~50 packets/s stream is its own
outage), and that is precisely why a *wholesale* mismatch read like an occasional
glitch for two days. So every packet's outcome is counted as well as logged.
`/control/status` serves the process-wide totals under `audio_decoder`
(`decoded_opus`, `decoded_pcmu`, `discarded`, `sessions_opus`, `sessions_pcmu`,
plus the `sniff_packets`/`opus_confidence` constants), and every packet lands in
exactly one of the first three. The per-camera answer is each camera's own
`audio_codec` field, derived from the decoded frames' shape: aiortc pins its Opus
decoder to 48 kHz stereo and its µ-law decoder to 8 kHz mono, so the shape
identifies the decoder exactly. It cannot be read from the decoder object
directly, because aiortc creates it inside `decoder_worker`, a thread no
`RTCRtpReceiver` keeps a reference to.

The patch carries an import-time compatibility guard, on the same terms as the
integration's three `telus_*_patch.py` modules: it refuses to apply if
`OpusDecoder` or its `decode` moved, if it is being applied twice, if upstream
has grown its own exception handling (which would most likely mean upstream
fixed the dead-thread half), if `g711.PcmuDecoder` moved, or if either decoder's
declared sample rate moved (which would break the shape-to-codec mapping the
status field depends on). A silent degradation to Opus-only is the
6-percent-real-audio outage all over again, so the µ-law half is as loud as the
Opus half. Neither aiortc 1.15.0 (the pin) nor upstream `main` fixes the missing
handler today; both `OpusDecoder.decode` and `decoder_worker` are unchanged.

#### What is established about asking for the track

`JANUS_REQUEST_AUDIO` (see [configuration.md](configuration.md)) is a
**per-camera** knob, and that is the part worth knowing before turning it on:
asking is not free for every camera.

What is established: asked for audio, this gateway *does* offer one: the offer
comes back as `audio(port=9,opus) video(port=9,H264,rtx)`, with the audio
m-line **first**, for both cameras on this account. What happened next differed
per camera: the doorbell went on delivering video at its usual rate, while the
slower of the two (6 fps natively, and slow on unrelated paths; its
diagnostics-signal read takes ~25 s against the doorbell's ~1.6 s) stopped
producing a first frame at all and cycled through
`no frames received before timeout` roughly every 23 s until the request was
turned off again.

What is **not** established is that the audio m-line *caused* that. The same
camera is slow everywhere, the failure is a timeout rather than a rejection, and
the session teardown line that accompanies it (`ice connection state: closed`)
is logged at the same instant as the failure; it is the consequence, not the
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
- **`nvenc` is unverified live**: implemented per ffmpeg's documentation, not
  exercised end-to-end against real NVIDIA hardware.
- The bridge holds its **own copy** of the TELUS credentials, separate from any
  Home Assistant integration; keeping the two in sync is the job of the control
  endpoint and the credential-fallback file (see
  [control-api.md](control-api.md)).
