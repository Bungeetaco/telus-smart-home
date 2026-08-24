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
- [Bridge warns that credentials.json is unreadable or corrupt](#bridge-warns-that-credentialsjson-is-unreadable-or-corrupt)
- [Bridge warns it could not re-tighten permissions on the control secret](#bridge-warns-it-could-not-re-tighten-permissions-on-the-control-secret)
- [Alarm.com rate-limiting (HTTP 429)](#alarmcom-rate-limiting-http-429)
- [Doorbell / event snapshots arrive tens of seconds late](#doorbell--event-snapshots-arrive-tens-of-seconds-late)
- [Camera stream freezes or goes dark](#camera-stream-freezes-or-goes-dark)
- [Hardware encode falls back to software (or fails to start)](#hardware-encode-falls-back-to-software-or-fails-to-start)
- [A camera's stream carries no audio, or the audio stops](#a-cameras-stream-carries-no-audio-or-the-audio-stops)
- [Talkdown returns 403 / 409 / 422](#talkdown-returns-403--409--422)
- [Talkdown audio sounds bad on the doorbell speaker](#talkdown-audio-sounds-bad-on-the-doorbell-speaker)
- [Talkdown logs a certificate fingerprint mismatch](#talkdown-logs-a-certificate-fingerprint-mismatch)
- [Talkdown logs that no PutAudioStream endpoint answered](#talkdown-logs-that-no-putaudiostream-endpoint-answered)
- [A camera control is missing, or a settings write returns 422](#a-camera-control-is-missing-or-a-settings-write-returns-422)
- [No camera settings entities appear at all](#no-camera-settings-entities-appear-at-all)
- [A bypass appears to do nothing](#a-bypass-appears-to-do-nothing)
- [A control answers `429`, or Home Assistant keeps retrying into a rate limit](#a-control-answers-429-or-home-assistant-keeps-retrying-into-a-rate-limit)
- [An arming-device write answers `404 unknown_device`](#an-arming-device-write-answers-404-unknown_device)
- [Checking whether talkdown has been failing](#checking-whether-talkdown-has-been-failing)

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

## Bridge warns that credentials.json is unreadable or corrupt

**Symptom.** The bridge log shows `Fallback credentials file present but
unreadable/corrupt; using .env credentials` at startup, or
`credentials.json unreadable/corrupt this cycle, skipping` /
`credentials.json is not a JSON object this cycle, skipping` repeating every
30 s from the fallback watcher. The bridge keeps running.

**Cause.** The credential-fallback file on the shared mount
(`<shared dir>/credentials.json`, written by the HA integration on every
credential update) is corrupt: truncated JSON, invalid UTF-8 from a torn
write, or valid JSON that is not a `{"username", "password", "updated_at"}`
object. The bridge deliberately **degrades instead of exiting** on every one
of these shapes — startup falls back to the `.env` credentials and the
watcher skips the cycle — because the bad file survives a container restart,
so exiting would turn one corrupt file into a `restart: unless-stopped` loop
that re-runs a full SSO login against the Cloudflare-fronted edge on every
cycle (see [rate-limiting](#alarmcom-rate-limiting-http-429)).

HA's own writer uses a temp file plus atomic rename, so it should not
produce a torn file by itself; this hardening is defensive (the corruption
shapes are reproduced in tests, not captured from a live incident). A torn
file in practice points at an unclean host shutdown, another writer, or the
filesystem under the bind mount.

**Fix.** Re-run the integration's **Reconfigure** (or Reauth) flow in Home
Assistant — every successful credential update rewrites the file atomically.
Deleting the file is also safe: the bridge then simply uses its `.env`
credentials until HA next writes one. If the warning recurs without any HA
credential update in between, suspect the shared mount itself (disk full,
filesystem errors) — the file is written by HA and only read by the bridge.

---

## Bridge warns it could not re-tighten permissions on the control secret

**Symptom.** The bridge log shows, at every startup:

```
Could not re-tighten permissions on <shared dir>/control_secret; it may remain group/world-readable
```

with a traceback. The bridge starts normally and the control API works.

**Cause.** The bridge re-tightens an adopted `control_secret` file to mode
`0600` at startup when it finds group/other permission bits set (a file it
generates itself is always born `0600`; a looser one arrives by being restored
from a backup or written under a permissive umask). The chmod is best-effort:
this warning means it failed — typically because the file is owned by a
different uid on the shared bind mount than the one the bridge runs as. The
secret is served unchanged either way; a permissions problem never regenerates
it, so the HA side keeps authenticating. But the file really does remain
readable to other users of the shared directory until fixed.

A one-time
`Re-tightened <shared dir>/control_secret from mode <old mode> to 0600`
warning is the healthy sibling of this message: the chmod succeeded, nothing
to do.

**Fix.** On the host, chown the file to the uid the bridge container runs as
(or remove it and restart the container — the bridge regenerates it at `0600`,
and the HA side picks up the new value on its next request since it re-reads
the file every time). Then check how the loose file got there: a backup/restore
procedure or umask that produces group/world-readable files in the shared
directory will do the same to the next restore.

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
`dropped_frames`, `last_error`) is available from the bridge's
`GET /control/status` endpoint (same auth as the other control endpoints) —
and, with the HA integration installed, as entities: each camera carries a
**Stream state** and a **Dropped frames** diagnostic sensor, and a
**Camera bridge** connectivity sensor says whether the bridge is answering at
all, so this diagnosis usually needs no curl (see
[ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity)).
`dropped_frames` is the lifetime count of frames the ingest queue discarded
because the consumer fell behind — a steadily climbing total on one camera says
the encode side cannot keep up with that stream, where the picture alone just
looks subtly choppy. It updates when each ~165 s session ends, so it can lag
the live session by up to one rotation.

---

## A camera's stream carries no audio, or the audio stops

**Symptom.** A camera published by the bridge plays video but no sound, or the
sound stops while the picture keeps going.

**Cause and fix.** Audio is off by default and needs **two** knobs, both naming
the camera (see [configuration.md](configuration.md)):

1. `JANUS_REQUEST_AUDIO` — asks the gateway for the track at all.
2. `JANUS_CONSUME_AUDIO` — decodes it and publishes it.

Naming a camera in only the second is a misconfiguration, not a partial setup:
no audio track is ever offered for it, so the bridge logs
`JANUS_CONSUME_AUDIO names this camera but JANUS_REQUEST_AUDIO does not` **once**
and treats consumption as off. Check `/control/status` — `audio_consumed` is the
resolved answer, `audio_format` is the shape the running encoder was launched
with (`null` when audio is not being published).

If `audio_consumed` is `true` and there is still no sound:

- **The camera may not be sending any.** The gateway offers the track whether or
  not the camera fills it. The stream still carries an audio track — the bridge
  keeps it alive with silence — so a silent track is not a broken one.
- **The consumer may not accept the codec.** The bridge publishes mono **AAC**
  specifically because HA's HLS worker takes only aac/mp3 and Frigate's `-c copy`
  preset needs an mp4-legal codec. A consumer configured to expect something else
  will drop it.
- **Audio dying is not detected.** The frame-silence watchdog is video-only, so a
  camera whose audio has stopped still reports `state: "streaming"` with a fresh
  `last_frame_at` and nothing reconnects. Watch `dropped_audio_frames` for a
  consumer falling behind; a stream that simply went quiet leaves no counter at
  all. Restarting the container is the blunt recovery.

Two log lines worth grepping: `ffmpeg has stopped reading the audio pipe`
(ffmpeg stopped draining input 1 — the publisher is rebuilt) and
`audio format changed mid-session` (the decoded PCM shape changed, so the
encoder is relaunched with the real one — expected at most once, on the first
session that ever carries audio).

**If `ffmpeg has stopped reading the audio pipe` repeats every few seconds**, the
two inputs' timelines have come apart: the audio timeline is real time, so if the
video timeline advances more slowly than the wall clock, audio outruns video, the
muxer's interleaver stops accepting audio packets, and the pipe backs up until
the sink rebuilds the publisher — kicking every RTSP reader each time, while the
video itself keeps flowing. The known cause is `-r <fps>` on ffmpeg's raw video
input, which overrides `-use_wallclock_as_timestamps` and makes that timeline
advance per frame *written* rather than per second *elapsed*; the audio-enabled
command line omits it for exactly this reason (see
[camera-bridge.md](camera-bridge.md)). Confirm with
`docker exec telus-cam-bridge sh -c 'tr "\0" " " < /proc/<pid>/cmdline'` for the
ffmpeg process: there must be **no `-r` before `-i pipe:0`** on a publisher that
has a second `-i pipe:<n>`. Any *other* cause of a video timeline running behind
real time would produce the same symptom.

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
  reflected per camera in `GET /control/status` (`hardware_encode`), and in HA
  as the same-named attribute on the camera's Stream state sensor.
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
| **409** | A talk is **already in progress** for that camera. The transport is single-session; a second concurrent talk is rejected rather than interleaved. | Wait for the current clip to finish, or cancel it with `POST /control/cameras/{id}/talk/stop` (or the integration's `stop_speaking` service), then retry. |
| **422** | **No upstream audio** could be produced — the request named an `audio_url`/`audio_path` (or TTS `message`) that yielded nothing to send. | Verify the audio source is reachable *from the bridge* and returns real audio. For an HTTP TTS URL, confirm the bridge can fetch it (loopback/`internal_url` reachability); for a file, confirm the path exists inside the container. |

Also note **400**: the endpoint rejects a `codec` other than `mulaw` on this
doorbell model (see next entry), and rejects malformed request bodies.

Triggered through `telus_smarthome.speak` rather than curl, these failures
arrive as `HomeAssistantError` messages naming the camera entity instead of raw
statuses — including **413** (the clip exceeds the bridge's size cap,
`TALK_MEDIA_MAX_BYTES`), **504** (the talk timed out on the bridge before the
clip finished) and **400** (the bridge could not fetch or decode the audio; the
service always sends a well-formed body, so a malformed-body `400` cannot come
from it). The wordings identify the same causes as the table above.

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
  replace it. The `telus_smarthome.speak` service takes the same two optional
  fields and forwards them to the bridge, so the override works from a Home
  Assistant automation as well as from curl. To disable conditioning globally,
  set the bridge's audio-filter config to empty.
- Pacing is schedule-based with a short prebuffer, so long clips play
  drift-free and the device sustains many minutes without a server-side cutoff —
  audio artifacts are about the codec/speaker, not timing.

**Diagnosis.** If you hear *noise* (not just low quality), you're almost
certainly sending non-mu-law audio, or raw audio the device is misinterpreting —
switch to mu-law. If you hear *intelligible but harsh/clipping* audio, adjust or
re-enable conditioning. If it's simply *lo-fi*, that's the 8 kHz mono speaker
ceiling.

---

## Talkdown logs a certificate fingerprint mismatch

**Symptom.** A talk still plays, but the bridge log carries a warning like:

```text
talkdown: the device at <doorbell-ip> presented certificate sha256=<observed> but
TALK_DEVICE_CERT_SHA256 pins <pinned>. If this device was replaced, reset, or had
a firmware update, that is expected -- update TALK_DEVICE_CERT_SHA256 to the
presented value. Otherwise the connection is not the device it claims to be.
(mode=warn -- continuing anyway)
```

**Cause.** The doorbell serves its audio endpoint over HTTPS with a **self-signed**
certificate, so there is no CA to verify it against and the certificate itself is
the only identity it has. The bridge pins its SHA-256 (`TALK_DEVICE_CERT_SHA256`)
and checks it once per connection. The pinned default is the fingerprint observed
on **one** ADC-VDB750 — it is not a vendor-wide constant, and a **firmware
update, a factory reset, or a replacement unit all change it legitimately**.

**Fix.** If you did any of those three, this is expected: copy the presented
fingerprint into `TALK_DEVICE_CERT_SHA256` in the bridge's `.env` and recreate
the container. If you did none of them, the connection is not the device it
claims to be — investigate before dismissing it.

**Note what the default mode does.** `TALK_TLS_PIN_MODE` defaults to `warn`,
which logs and **still connects**, so a mismatch never silences the doorbell. Move
to `enforce` only once the pin is known good for your device — a mismatch there
aborts the talk, which surfaces as `502 telus_error` to the caller and
`transport_error` in the talkdown counters, with the actionable detail in the log
above. The pin is never applied to the `cloud` endpoint: that one is the vendor
relay, a different host with its own CA-signed certificate.

Two related lines mean something different:

- `talkdown: no certificate pin configured; the device at <doorbell-ip> presented
  sha256=<observed> -- set TALK_DEVICE_CERT_SHA256 to this value to pin it` —
  there is nothing to verify against. Paste the value in to start pinning.
- `talkdown: TALK_DEVICE_CERT_SHA256=... is not a 32-byte hex SHA-256; ignoring
  the pin` — the knob is malformed, and talkdown continues **unpinned**. Fix the
  value.

The talk result reports `cert_pinned` (`true` matched, `false` mismatched in warn
mode) when a pin applied, so a mismatch is also visible to whoever triggered the
talk — see [control-api.md](control-api.md#post-controlcamerascamera_idtalk).

---

## Talkdown logs that no PutAudioStream endpoint answered

**Symptom.** Before audio starts, the bridge logs:

```text
talkdown: none of the PutAudioStream endpoints (direct,local,public,cloud)
answered a connect probe; trying direct anyway
```

and the talk then either works anyway or fails with a transport error.

**Cause.** The doorbell advertises up to four routes to its audio endpoint — two
LAN addresses (`direct`, `local`), the site's own WAN address (`public`), and the
vendor cloud relay (`cloud`). The bridge probes them concurrently with a bare TCP
connect (`TALK_ENDPOINT_PROBE_TIMEOUT_S`, 1 s) and uses the first one in
preference order that answers, so that being off the doorbell's LAN does not mean
stalling on a TCP connect to an unreachable RFC1918 address with the per-camera
talk lock held. This message means **none** of them accepted a connection inside
that budget.

Detection is never allowed to be the reason a talk fails, so the first preferred
URL is used anyway and the `PUT` itself produces the real error. That is why this
line can appear on a talk that then works.

**Fix — in order of likelihood:**

- **The bridge is not on the doorbell's LAN and the WAN paths are blocked or
  slow.** Confirm which endpoint should work from the bridge host, then pin it
  with `TALK_PUT_ENDPOINT=cloud` (or `public`) so no time is spent probing routes
  that cannot work.
- **A firewall drops the probe but would pass the real PUT.** Same fix: pin the
  endpoint with `TALK_PUT_ENDPOINT`, or set `TALK_ENDPOINT_PROBE_TIMEOUT_S=0` to
  disable probing entirely and restore a blind walk down
  `TALK_PUT_ENDPOINT_ORDER`.
- **The link is simply slower than one second to accept a connection.** Raise
  `TALK_ENDPOINT_PROBE_TIMEOUT_S`.
- **The doorbell is offline or the session is stale.** Then nothing answers
  because there is nothing there; check the camera is streaming at all.

A pinned endpoint the doorbell does not advertise is ignored with a warning
(`TALK_PUT_ENDPOINT=... is not one of the endpoints this doorbell advertises`)
and auto-detection runs anyway — a mistyped knob must not silence the doorbell.
The endpoint that was actually used is counted in `by_endpoint` at
`GET /control/metrics`.

---

## A camera control is missing, or a settings write returns 422

**Symptom.** A camera has no speaker-volume slider, no siren buttons, or no
battery sensor. Or a write to one of the camera-settings routes comes back
`422 {"error": "unsupported"}` while the same call succeeds on a different
camera.

**Cause.** Usually not a fault: **capability is per device and per account**, and
both halves of the project refuse to pretend otherwise.

- Every website settings field is paired with a `<name>FeatureSupport` enum
  (`0` unsupported, `2` supported/editable), and **the same payload shape comes
  back for a device that cannot do the thing** — so a field being present proves
  nothing. The bridge resolves that enum into a `supports_*` boolean and answers
  `422` rather than sending a `PUT` the camera will ignore.
- The **siren** additionally depends on `canManuallyTriggerSiren`, which is an
  *account permission*, not a device property. A camera with a siren is not
  necessarily one your account may sound.
- **Privacy mode** depends on `SupportsPrivacyMode`, which is `false` on the
  cameras this project was built against.
- Home Assistant deliberately creates **no entity** for a capability the camera
  does not report, so a permanently-broken control never reaches the dashboard.
  Privacy mode is the one exception: its switch is created and reads
  *unavailable*, so it keeps its history and any automation that references it.

**Fix — check what the camera actually reports:**

```bash
curl -s -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/settings | jq '.audio, .image, .video'
```

- A `supports_*` of `false` is the answer: the device does not offer it, and
  there is nothing to fix.
- `supports_*` `true` but no entity in HA → the integration has not been
  restarted since this version was installed (a config-entry reload is not
  enough; see [development.md](development.md)).
- The route itself **404s** → the bridge container predates these routes. Code is
  baked into the image, so rebuild rather than restart:
  `cd camera-bridge && docker compose up -d --build`.
- The entities exist but are **unavailable** → the settings poll is failing, not
  the capability. Check `curl -s $BASE/control/health` and the bridge log; a
  `502 telus_error` in that log points upstream (and a `429` there is
  [rate-limiting](#alarmcom-rate-limiting-http-429), not a broken camera).

**One caution rather than a fix:** a `POST` to these routes changes real
hardware, and `{"action": "start"}` on the siren is audible in someone's home.
Nothing polls a write route — if a setting changed, something asked it to.

---

## No camera settings entities appear at all

Symptom: after a restart there is no speaker-volume slider, no signal-strength
sensor and no privacy switch for **any** camera, and
`custom_components.telus_smarthome.camera_settings_coordinator` logs
`Could not reach camera bridge` even though the bridge answers fine by hand.

These entities are created from the **first successful poll** — the bridge is the
only thing that knows which camera supports what, so nothing can be created until
a poll returns. That makes a failing poll fail *visibly as an absence*, which is
easy to misread as "the feature did not deploy".

Check the poll itself rather than the entities:

```bash
sudo docker logs --tail 2000 homeassistant 2>&1 | grep -a camera_settings_coordinator | tail -5
```

`Finished fetching TELUS camera settings data in N seconds (success: False)` is
the line that matters. If N is around 10 or 20, suspect a **timeout, not an
unreachable bridge** — the message is the same either way, because both arrive as
`aiohttp.ClientError`.

Time the routes directly to find which camera is slow:

```bash
S=$(sudo docker exec telus-cam-bridge cat /shared/control_secret)
curl -s -o /dev/null -w "%{http_code} %{time_total}s\n" -H "Authorization: Bearer $S" \
  http://127.0.0.1:8199/control/cameras/<camera-id>/diagnostics
```

**`/diagnostics` is inherently slow on some models.** It is one request to the
bridge but three sequential alarm.com reads behind it, and alarm.com appears to
query the camera itself for the signal figure. Measured live in one account: 1.6s
on a doorbell and **25s** on an indoor camera. That is normal, not a fault — it is
why the diagnostics fetch uses `CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS` (45s) instead
of the shared 10s bridge timeout. On the bridge side, each of those three upstream
reads carries its own 35s ceiling (`DIAGNOSTICS_READ_TIMEOUT_S` in
`camera_settings.py`), distinct from the bridge's shared 15s upstream-session
timeout — that override is what lets a 25s signal read succeed instead of being
cancelled by the bridge's own client and answered as a 502.

A single camera failing no longer blanks the others: the poll degrades that
camera (keeping its previous reading) and logs
`Camera settings poll failed for <id>`. Only a failure on *every* camera marks the
coordinator failed. If you see one camera named in that warning repeatedly, that
camera is slow or unreachable — the rest of the account is unaffected.

Note also that a poll can succeed and still produce no entities for a given
camera, which is correct: the siren buttons exist only for a camera whose account
reports the `canManuallyTriggerSiren` permission, and an account with no
siren-capable devices gets none. See "A camera control is missing" above.

## A bypass appears to do nothing

Symptom: `telus_smarthome.bypass_sensors` returns without error, but the sensors
look unchanged.

**Give it ten seconds.** The write reaches the panel asynchronously: a bypass
returns `200` immediately, but the sensor still reports `isBypassed: false` about
five seconds later and `true` about ten seconds later. An un-bypass takes the
same. The service already delays its post-write refresh past that window for
exactly this reason — a refresh fired the instant the POST returns reads the old
state and repaints the sensors unchanged, which is indistinguishable from a call
that did nothing.

If it is still unchanged well after that, check the two gates the partition
publishes, which the bridge re-reads before every write:

```bash
S=$(sudo docker exec telus-cam-bridge cat /shared/control_secret)
curl -s -H "Authorization: Bearer $S" http://127.0.0.1:8199/control/partitions
```

`has_permission_to_change_state` false, or `can_bypass_when_armed` false while
the partition is armed, are both refusals — they answer `422`, not a silent
no-op. A panel that reports `canBypassSensorWhenArmed: false` can only be
bypassed while disarmed.

Note also that the vendor's own portal hides its bypass control while the panel
is disarmed, gated on `extendedArmingOptions`. The endpoint honours the call
anyway; that is a UI choice, not an API constraint.

---

## A control answers `429`, or Home Assistant keeps retrying into a rate limit

The camera settings and diagnostics reads, every camera settings write (both its
pre-read and the write itself), and the siren, privacy, partition, bypass,
arming-device and Bluetooth-disarming routes answer `429` with a `Retry-After`
header carrying **the server's own wait**, plus
`{"error": "rate_limited", "retry_after": N}`. Honour that value rather than
retrying on your own schedule — the next attempt otherwise walks straight back
into the limiter, and Alarm.com's limits are undocumented and can lock the
account.

The Home Assistant entities surface this rather than retrying: a rate-limited
siren press, privacy toggle, camera-settings, arming-device or
Bluetooth-disarming write raises a `HomeAssistantError` beginning "Alarm.com is
rate-limiting this account" and carrying the server's wait ("try again in
{N}s", or "try again later" when no wait is readable). The integration never
retries the write itself — repeat the action after the stated interval.

The one absorb: the settings read's two MWS-backed sections (siren and privacy)
swallow a rate-limited MWS read internally — the negative result is cached for
the server's stated wait and those sections degrade to `null` — so the response
stays `200` and the next cycle does not re-hammer the edge.

---

## An arming-device write answers `404 unknown_device`

The device id is not in the account's device catalog. This is deliberate: an id
the catalog does not know is refused rather than turned into a blind `PUT` at
whatever that id happens to address. Re-read the list and use an id from it:

```bash
S=$(sudo docker exec telus-cam-bridge cat /shared/control_secret)
curl -s -H "Authorization: Bearer $S" http://127.0.0.1:8199/control/arming-devices
```

A phone that has been unpaired from the panel disappears from that list, and its
Home Assistant switch goes unavailable rather than continuing to show a position.

---

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

The same data is available without a curl: the `talkdown_status` and
`talkdown_failures` diagnostic sensors expose it in Home Assistant, and can
drive a notification when a talk fails — see
[ha-integration.md](ha-integration.md#talkdown-diagnostics).
