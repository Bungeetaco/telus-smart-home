# Troubleshooting

Known issues, gotchas, and how to diagnose them across the two components of
this project: the **Home Assistant integration** (`custom_components/telus_smarthome`)
and the **camera bridge** (`camera-bridge/`). Each entry is written as
**symptom → cause → fix**. Everything here is about the software's behavior
against the white-labeled Alarm.com cloud; none of it depends on a specific
deployment.

For context on the moving parts, see [camera-bridge.md](camera-bridge.md) and
the component READMEs.

## Contents

- [Push websocket drops every few minutes](#push-websocket-drops-every-few-minutes)
- [Container restart-loop against an upstream outage](#container-restart-loop-against-an-upstream-outage)
- [Alarm.com rate-limiting (HTTP 429)](#alarmcom-rate-limiting-http-429)
- [Doorbell / event snapshots arrive tens of seconds late](#doorbell--event-snapshots-arrive-tens-of-seconds-late)
- [Camera stream freezes or goes dark](#camera-stream-freezes-or-goes-dark)
- [Hardware encode falls back to software (or fails to start)](#hardware-encode-falls-back-to-software-or-fails-to-start)
- [Talkdown returns 403 / 409 / 422](#talkdown-returns-403--409--422)
- [Talkdown audio sounds bad on the doorbell speaker](#talkdown-audio-sounds-bad-on-the-doorbell-speaker)

---

## Push websocket drops every few minutes

**Symptom.** Real-time state updates (door, lock, sensor changes) stop
arriving after a connection has been up for a few minutes, then resume. In the
Home Assistant log, entity states appear to only refresh on a slow cadence
rather than the instant a device changes.

**Cause.** The cloud closes the push websocket a few minutes after every
connect — this is a **server-side token lifetime**, not a bug in this
integration. A keepalive/heartbeat ping does **not** extend it; the server
tears the socket down regardless. Between the drop and the next reconnect,
device states only refresh on the REST poll (`update_interval`, default
300 s), so a change can take from tens of seconds up to a couple of minutes to
surface.

**Fix / design.** The integration is built to ride this out rather than
prevent it:

- Keep `ws_reconnect_timeout` **low** (30 s) so a dropped socket is
  re-established quickly.
- Keep `update_interval` at **300 s**. It is the safety net that fills the
  reconnect gaps. Lowering it to "get faster updates" is the wrong lever — it
  increases API rate against a rate-limiting cloud (see
  [rate-limiting](#alarmcom-rate-limiting-http-429)) without fixing the
  underlying drop.

**Diagnosis technique.** To tell a **push** apart from a **REST poll** in the
Home Assistant log: a poll logs *every* device "Updated …" in the same
millisecond (a bulk refresh), while a genuine push touches a **single**
device. If you only ever see bulk millisecond-aligned updates, pushes are not
getting through and you are running on the poll alone.

> Historical note: a separate defect in the upstream library used to crash the
> websocket read loop outright on any message referencing a device the library
> didn't recognize (an `UnboundLocalError` escaping the read loop), which made
> the socket die within seconds of every relevant event rather than lasting
> its full token lifetime. That is patched locally. If you see the socket
> dying almost immediately after connect (not after a few minutes), suspect a
> regression in that patch, not the token-lifetime drop described here.

---

## Container restart-loop against an upstream outage

**Symptom.** During a sustained cloud/auth-gateway outage, the bridge
container restarts over and over — potentially many times per hour, sometimes
in a tight loop — and the outage seems to last *longer* than the upstream
problem itself.

**Cause.** The bridge has two deliberate process-death paths, each a correct
response to a **local** wedge (a leaked/stuck media resource that only a fresh
process can release; a startup login that landed during a blip):

- the per-camera reconnect loop's "too many consecutive session failures"
  escape hatch (`os._exit`), and
- the startup login giving up after its retries.

Under Docker's `restart: unless-stopped`, both are the *wrong* response to a
**sustained upstream** failure: each restart re-runs a full SSO login against
the Cloudflare-fronted auth gateway, re-entering the same rate-limit window.
From inside the reconnect loop the two situations are indistinguishable, and
with a restart policy behind it the sustained case had **no floor** — the
reaction actively prolonged the outage it was reacting to.

**Fix / design — the restart-frequency guard** (`bridge/restart_guard.py`).
Because these paths kill the process, an in-memory counter cannot count them;
the count must outlive the process. The guard keeps a small JSON file of
recent exit timestamps on the `/shared` bind mount (it survives both a process
exit and a container recreate):

- The **first few** exits in a window are **not** delayed — a genuine one-off
  wedge still recycles as fast as ever.
- Beyond that threshold, `record_exit()` returns a **backoff to sleep
  in-process *before* exiting**. Because the sleep runs before the process
  dies, Docker cannot restart the container until it finishes — a hard floor
  on restart frequency. The delay doubles per extra exit, capped at a maximum.
- The exit is recorded **before** the sleep, so a `docker restart` or OOM kill
  that cuts the sleep short still counts. Reads, appends, and writes happen
  with no `await` between them, so two cameras hitting the hatch in the same
  event loop can't lose each other's entry. The state file is written via a
  temp file + atomic rename, so a process dying mid-write can't leave a
  half-written file that disables the guard.
- **Fail-open by design.** Any error reading or writing the state file
  degrades to "exit immediately" (the pre-guard behavior). A broken guard must
  never be the reason a genuinely wedged bridge can't recycle. Future-dated
  timestamps (an NTP step just after boot) are dropped rather than left pinned
  in the window.

While the guard is holding a restart off, the **rest of the bridge keeps
serving** — other cameras, the control endpoint, and Home Assistant's status
polls continue.

**Diagnosis.** Inspect the state file on the shared mount
(`restart_guard.json`, shape `{"exits": [<unix ts>, ...]}`). More than a few
entries within an hour means the guard is (correctly) throttling. Deleting the
file is harmless — it just resets the count. Watch container restart counts
(`docker ps` / `docker inspect`) to confirm the floor is engaging rather than
a free-running loop.

---

## Alarm.com rate-limiting (HTTP 429)

**Symptom.** Logins, REST polls, or event fetches begin failing; the cloud
returns **HTTP 429**. Aggressively retrying makes it worse and the lockout
window seems to keep resetting instead of clearing.

**Cause.** The cloud rate-limits and answers with a `Retry-After`. Two ways to
get this wrong:

1. Tight-retrying (or restart-looping, see
   [above](#container-restart-loop-against-an-upstream-outage)) knocks again
   before the window has elapsed, restarting it.
2. Capping the honored wait too low. The bridge's retry wait was once a plain
   `min()` of the server's `Retry-After` against a 5-minute cap — so any window
   **longer** than the cap had the bridge knocking again *sooner than the
   server told it to*, restarting the rate-limit window instead of clearing
   it. This turned one bad night into a multi-hour outage.

**Fix / design.**

- **Honor `Retry-After`.** The bridge waits for the server-specified interval;
  the cap on that wait is now high enough (30 minutes) that it does not truncate
  realistic windows and cause an early re-knock.
- **Do not tight-retry.** Back off; let the restart guard provide the floor for
  the process-death paths.
- **Don't increase your own request rate to compensate for slow updates.**
  Lowering `update_interval` or the event poll to "catch up" trades directly
  against 429 exposure. Keep the poll at its default and accept the push design
  above.

> On the integration side, wrong-password vs. MFA-challenge vs. server-error is
> deliberately differentiated at the *exception-type* level
> (`invalid_auth` / `telus_sso_error` / `cannot_connect`), **not** by
> inspecting the cloud's actual response bodies — doing the latter safely would
> require deliberately triggering those responses against a live account, which
> risks exactly the rate-limiting/lockout described here.

---

## Doorbell / event snapshots arrive tens of seconds late

**Symptom.** A cloud "person at the door" / video event, and the snapshot it
carries, lands well after the person has actually come and gone — commonly on
the order of **30–60+ seconds** late.

**Cause.** This latency is upstream and **cannot be tuned away**:

- The integration polls video events on a **fixed** interval (hardcoded in
  `const.py`, used directly by the event coordinator). The options-flow
  "Update Interval" drives only the main data coordinator, **not** video
  events, so changing it does nothing here. The websocket push patch doesn't
  carry these events either — it only re-notifies listeners over objects
  already in memory.
- On top of the poll sits the cloud's own clip-processing delay (roughly
  another ~30 s) before the event/snapshot is even available.

Lowering the poll interval cannot fix it: the upstream processing alone
outlasts the walk from the door, and tripling the API rate against a
rate-limiting cloud is a bad trade (see
[rate-limiting](#alarmcom-rate-limiting-http-429)).

**Fix / design — treat the cloud as a *fallback*, do local detection for the
fast path.** The recommended architecture is:

- Run **local, on-box detection** (e.g. a local NVR/detector consuming the
  bridge's stream) as the **primary**, low-latency source. Detect-to-publish is
  sub-second locally versus ~30–60 s for the cloud event.
- Use the **cloud event as the fallback only**, gated behind a suppression
  window so the same person isn't announced twice — once fast (local) and once
  late (cloud). A "recent detection" window (a few minutes, long enough to
  outlast the cloud lag) suppresses the fallback when local detection already
  fired.
- **Fail-open gating.** Fallback-suppression conditions must fail *open*: after
  a template reload a sensor can read `unknown`, and a strict "is off" test
  would silently mute the fallback. Test `not is_state(..., 'on')` so an
  unknown state still lets the fallback through rather than dropping it.

To feed local detection *without* adding cloud load, consume a **re-served /
multiplexed** copy of the bridge's RTSP stream (one upstream session fanned out
to many local consumers) rather than opening a second direct session — that
keeps you off the cloud entirely for the detection path and can't trip the
[restart guard](#container-restart-loop-against-an-upstream-outage).

**Diagnosis — the expired-token image trap.** When fetching a snapshot with a
stale cloud image token, the server returns **HTTP 200**, *not* 401 — with a
small (~6 KB) grey placeholder body. Naive "2xx == success" checking silently
writes junk. **Never treat a 2xx as proof of a real image.** Validate the
payload itself: require a sane minimum byte count (e.g. ≥ 12 000 bytes) **and**
JPEG magic bytes (`\xff\xd8`). Re-mint the token (re-auth / re-fetch the event)
if validation fails.

---

## Camera stream freezes or goes dark

**Symptom.** An RTSP feed freezes on one frame, then recovers; or a feed goes
fully dark. In earlier builds the feed would hang indefinitely.

**Cause.** The cloud kills a video session **server-side at ~180 s**,
unconditionally (matching the `timeout_seconds` sent in the session-create
request). If the bridge waits for the server to tear the session down, it loses
the race and the feed hangs. A camera can also stall immediately, delivering
zero frames for a session that never gets a teardown signal.

**Fix / design — proactive session rotation plus frame-silence detection.**
These are **two separate, deliberately redundant safety nets**; do not collapse
them into one:

1. **Proactive rotation** — every camera session is rotated on a fixed cadence
   (`SESSION_MAX_LIFETIME_S`, currently 165 s) *before* the ~180 s server-side
   kill, so the feed never goes dark waiting for a server-initiated teardown.
2. **Frame-silence detection** — if no frame arrives for
   `FRAME_SILENCE_TIMEOUT_S` (15 s), the session is torn down and reconnected
   immediately, without waiting for the rotation deadline. This check runs on
   **every** loop iteration, including when zero frames have ever arrived, which
   is what makes it safe against a camera that stalls at the very start.
3. **Gap bridging** — the ffmpeg publisher **outlives** rotations; each
   rotation/reconnect gap is bridged by **repeating the last real frame** into
   the still-running publisher (up to `FRAME_HOLD_MAX_S`, 60 s). Readers see a
   brief freeze, not a torn-down RTSP path. A genuinely offline camera stops
   being held after 60 s and the feed is allowed to go dark honestly.

Because the publisher now outlives sessions and gaps are frame-held, the
rotation margin can be tightened (to 15 s, matching the frame-silence timeout)
to keep those frame-hold freezes as infrequent as possible.

**Do not regress this.** Don't collapse the two checks, and **don't change
`SESSION_MAX_LIFETIME_S` without re-verifying the cloud's actual server-side
timeout live** — it isn't documented anywhere; the ~180 s value was *observed*.
If you touch `camera_session.py` or `janus_client.py`, read them in full first.

**Diagnosis.** A feed that freezes briefly on a ~15/165 s cadence and recovers
is the rotation/frame-hold working as designed. A feed that freezes for ~60 s
then goes dark is the frame-hold expiring on a genuinely offline camera.
Per-camera health (`state`, `consecutive_failures`, `last_frame_at`, `fps`,
`last_error`) is available from the bridge's `GET /control/status` endpoint
(same auth as the other control endpoints).

---

## Hardware encode falls back to software (or fails to start)

**Symptom.** CPU usage is higher than expected on a machine with a capable GPU;
ffmpeg logs show `libx264` rather than a hardware encoder; or a chosen backend
never engages.

**Cause / design.** `ENCODE_BACKEND` (env var, see `.env.example`) only picks
which hardware path is **attempted first**. On each `start()`, an
unavailable/broken device **falls back to software automatically** — the
pipeline keeps running, just on CPU. So "it's on software" is a *symptom of the
hardware path failing*, not a crash.

Backends:

| `ENCODE_BACKEND` | Path | Notes |
| --- | --- | --- |
| `vaapi` (default) | Intel QuickSync / AMD via `/dev/dri` | The verified-live path. Which VA driver is used is decided by libva against the passed-through `/dev/dri` device, not by this project. |
| `nvenc` | NVIDIA via `nvidia-container-toolkit` device injection (not `/dev/dri`) | `h264_nvenc` is already compiled into the image's ffmpeg; you only need the host NVIDIA driver + container toolkit. **Implemented per ffmpeg's docs but not confirmed against real NVIDIA hardware by this project** — treat as unverified. |
| `software` | Always `libx264` | Skips the hardware attempt entirely. |

**Fix / diagnosis.**

- Confirm what actually engaged by reading ffmpeg's own log line for the
  publish process — it names the encoder in use. Hardware-encode status is also
  reflected per camera in `GET /control/status` (`hardware_encode`).
- **VAAPI not engaging:** verify `/dev/dri` is passed into the container and
  the GPU is visible to libva. A device that's present but broken will silently
  fall back to software per `start()`.
- **NVENC not engaging:** verify the host NVIDIA driver and
  `nvidia-container-toolkit` device injection (the NVIDIA section in
  `docker-compose.yml` is commented out by default and must be enabled) — this
  path uses device injection, **not** `/dev/dri`.
- If you only need it working and not fast, set `ENCODE_BACKEND=software`
  explicitly to stop chasing a hardware path that isn't available.

> Related but distinct: a passed-through iGPU that hardware-*encodes* fine can
> still be unreliable for hardware-*decode* in a *separate* local detector
> process (surface-sync failures, downloads failing, throttled capture fps).
> That is a decode problem in that other process, not this bridge's encode
> backend — if a local detector is silently missing frames, compare its actual
> capture fps against its configured detect fps and try software decode there.

---

## Talkdown returns 403 / 409 / 422

**Symptom.** A two-way audio (talkdown) request to the doorbell control
endpoint (`POST /control/cameras/{id}/talk`) returns an error status instead of
playing.

**Cause / fix — by status:**

| Status | Meaning | Fix |
| --- | --- | --- |
| **403** | Talkdown has been **disabled**. It is enabled by default, so this means `TALKDOWN_ENABLED` is set to a false value in the bridge's `.env`. | Remove that line (or set it `true`) and recreate the container so the change is picked up (`docker compose up -d`). |
| **409** | A talk is **already in progress** for that camera. The transport is single-session; a second concurrent talk is rejected rather than interleaved. | Wait for the current clip to finish (or cancel it, if a cancel path is available) before starting another. |
| **422** | **No upstream audio** could be produced — the request named an `audio_url`/`audio_path` (or TTS `message`) that yielded nothing to send. | Verify the audio source is reachable *from the bridge* and returns real audio. For an HTTP TTS URL, confirm the bridge can fetch it (loopback/`internal_url` reachability); for a file, confirm the path exists inside the container. |

Also note **400**: the endpoint rejects a `codec` other than `mulaw` on this
doorbell model (see next entry), and rejects malformed request bodies.

**Auth reminder.** The talk endpoint (like the other control endpoints) is
Bearer-authed with the bridge control secret from the shared directory
(`control_secret`). A missing/wrong secret is a `401`, not one of the above.

---

## Talkdown audio sounds bad on the doorbell speaker

**Symptom.** Audio played through the doorbell is muffled, harsh, clipping,
lo-fi, or — if you tried to raise quality — pure noise.

**Cause.**

- **The doorbell speaker is small and telephone-grade.** The talkdown path is
  8 kHz mono — inherently lo-fi. Music plays but will sound poor; that's a
  device limit, not a bug.
- **This doorbell model decodes the PUT body as G.711 mu-law regardless of the
  declared codec.** Sending 16-bit linear PCM (L16) over the PUT path produces
  **noise** in *both* byte orders — the device just interprets the bytes as
  mu-law. This is a per-model/firmware limitation, not a cloud-wide one (other
  models may accept L16 over a different transport). The endpoint therefore
  rejects any `codec` other than `mulaw` with **400**.

**Fix / design.**

- **Use mu-law.** It's the only codec that works on this doorbell's PUT path.
- **Default audio conditioning is applied to every clip before encoding** — a
  small-speaker / anti-clipping chain (high-pass, low-pass, compressor,
  limiter) tuned by ear. It's what makes speech intelligible on the small
  speaker. Leave it on unless you have a reason not to.
- **Per-request overrides:** send `"conditioning": false` to disable the
  default chain for one clip, or `"audio_filter": "<ffmpeg -af string>"` to
  replace it. To disable conditioning globally, set the bridge's audio-filter
  config to empty.
- Pacing is schedule-based with a short prebuffer, so long clips play
  drift-free and the device sustains many minutes without a server-side cutoff —
  audio artifacts are about the codec/speaker, not timing.

**Diagnosis.** If you hear *noise* (not just low quality), you're almost
certainly sending non-mu-law audio, or raw audio the device is misinterpreting —
switch to mu-law. If you hear *intelligible but harsh/clipping* audio, adjust or
re-enable conditioning. If it's simply *lo-fi*, that's the 8 kHz mono speaker
ceiling.

## Checking whether talkdown has been failing

A failed talk surfaces as a single HTTP error to whoever triggered it, so a
pattern is easy to miss. The bridge keeps counters:

```bash
curl -s -H "Authorization: Bearer $SECRET" http://127.0.0.1:8199/control/metrics
```

`failures_by_reason` tells you which way it is failing (`no_upstream_audio`
suggests the device stopped advertising a talk path; `transport_error` or
`network_error` point at the doorbell or the LAN; `media_error` means the audio
you supplied could not be fetched or decoded), and `recent` shows the last 20
talks with their outcomes. The counters are in-memory and reset when the bridge
restarts — see [control-api.md](control-api.md).
