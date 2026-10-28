# Troubleshooting

Known issues, gotchas, and how to diagnose them across the two components of
this project: the **Home Assistant integration** (`custom_components/telus_smarthome_alarmdotcom`)
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
- [A doorbell notification fires on every Home Assistant restart](#a-doorbell-notification-fires-on-every-home-assistant-restart)
- [Doorbell / event snapshots arrive tens of seconds late](#doorbell--event-snapshots-arrive-tens-of-seconds-late)
- [Camera stream freezes or goes dark](#camera-stream-freezes-or-goes-dark)
- [A camera reaches `webrtcup` but never delivers a frame](#a-camera-reaches-webrtcup-but-never-delivers-a-frame)
- [Every session publishes, then dies ~5 s later](#every-session-publishes-then-dies-5-s-later)
- [Frames dropped as `consumer falling behind`, on the camera with audio](#frames-dropped-as-consumer-falling-behind-on-the-camera-with-audio)
- [Frames lost at every rotation, in a burst just after the new session starts](#frames-lost-at-every-rotation-in-a-burst-just-after-the-new-session-starts)
- [Measuring what a session rotation costs the audio timeline](#measuring-what-a-session-rotation-costs-the-audio-timeline)
- [Attributing the container's memory](#attributing-the-containers-memory)
- [The status says `streaming` but MediaMTX has no path](#the-status-says-streaming-but-mediamtx-has-no-path)
- [A live view drifts further behind real time the longer it runs](#a-live-view-drifts-further-behind-real-time-the-longer-it-runs)
- [`frame rate changed`: the publisher rebuilds and readers reconnect once](#frame-rate-changed-the-publisher-rebuilds-and-readers-reconnect-once)
- [`More than 1000 frames duplicated` on a healthy-looking camera](#more-than-1000-frames-duplicated-on-a-healthy-looking-camera)
- [Hardware encode falls back to software (or fails to start)](#hardware-encode-falls-back-to-software-or-fails-to-start)
- [A camera's stream carries no audio, or the audio stops](#a-cameras-stream-carries-no-audio-or-the-audio-stops)
- [Audio plays in fragments that jump around](#audio-plays-in-fragments-that-jump-around)
- [A camera publishes video but never gains an audio track](#a-camera-publishes-video-but-never-gains-an-audio-track)
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
connect; this is a **server-side token lifetime**, not a bug in this
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
  reconnect gaps. Lowering it to "get faster updates" is the wrong lever: it
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
container restarts over and over (potentially many times per hour, sometimes
in a tight loop) and the outage seems to last *longer* than the upstream
problem itself.

**Cause.** The bridge has two deliberate process-death paths, each a correct
response to a **local** wedge (a leaked/stuck media resource that only a fresh
process can release; a startup login that landed during a blip):

- the per-camera reconnect loop's escape hatch (`os._exit`), which fires when
  one camera has gone `SESSION_WEDGE_TIMEOUT_S` with no successful frame AND no
  other camera in the process is still delivering (a healthy sibling proves the
  process is not wedged and vetoes the exit), and
- the startup login giving up after its retries.

Under Docker's `restart: unless-stopped`, both are the *wrong* response to a
**sustained upstream** failure: each restart re-runs a full SSO login against
the Cloudflare-fronted auth gateway, re-entering the same rate-limit window.
From inside the reconnect loop the two situations are indistinguishable, and
with a restart policy behind it the sustained case had **no floor**: the
reaction actively prolonged the outage it was reacting to.

**Fix / design: the restart-frequency guard** (`bridge/restart_guard.py`).
Because these paths kill the process, an in-memory counter cannot count them;
the count must outlive the process. The guard keeps a small JSON file of
recent exit timestamps on the `/shared` bind mount (it survives both a process
exit and a container recreate):

- The **first few** exits in a window are **not** delayed: a genuine one-off
  wedge still recycles as fast as ever.
- Beyond that threshold, `record_exit()` returns a **backoff to sleep
  in-process *before* exiting**. Because the sleep runs before the process
  dies, Docker cannot restart the container until it finishes: a hard floor
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
serving**: other cameras, the control endpoint, and Home Assistant's status
polls continue.

**Diagnosis.** Inspect the state file on the shared mount
(`restart_guard.json`, shape `{"exits": [<unix ts>, ...]}`). More than a few
entries within an hour means the guard is (correctly) throttling. Deleting the
file is harmless: it just resets the count. Watch container restart counts
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
of these shapes (startup falls back to the `.env` credentials and the
watcher skips the cycle) because the bad file survives a container restart,
so exiting would turn one corrupt file into a `restart: unless-stopped` loop
that re-runs a full SSO login against the Cloudflare-fronted edge on every
cycle (see [rate-limiting](#alarmcom-rate-limiting-http-429)).

HA's own writer uses a temp file plus atomic rename, so it should not
produce a torn file by itself; this hardening is defensive (the corruption
shapes are reproduced in tests, not captured from a live incident). A torn
file in practice points at an unclean host shutdown, another writer, or the
filesystem under the bind mount.

**Fix.** Re-run the integration's **Reconfigure** (or Reauth) flow in Home
Assistant; every successful credential update rewrites the file atomically.
Deleting the file is also safe: the bridge then simply uses its `.env`
credentials until HA next writes one. If the warning recurs without any HA
credential update in between, suspect the shared mount itself (disk full,
filesystem errors); the file is written by HA and only read by the bridge.

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
this warning means it failed, typically because the file is owned by a
different uid on the shared bind mount than the one the bridge runs as. The
secret is served unchanged either way; a permissions problem never regenerates
it, so the HA side keeps authenticating. But the file really does remain
readable to other users of the shared directory until fixed.

A one-time
`Re-tightened <shared dir>/control_secret from mode <old mode> to 0600`
warning is the healthy sibling of this message: the chmod succeeded, nothing
to do.

**Fix.** On the host, chown the file to the uid the bridge container runs as
(or remove it and restart the container; the bridge regenerates it at `0600`,
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
   `min()` of the server's `Retry-After` against a 5-minute cap, so any window
   **longer** than the cap had the bridge knocking again *sooner than the
   server told it to*, restarting the rate-limit window instead of clearing
   it. This turned one bad night into a multi-hour outage.

**The block does not always say 429.** The edge also refuses the account for
its stated wait (observed: `Retry-After: 1825`) with **`403 Forbidden` on
ordinary website GETs while the SSO login keeps succeeding**. Read as an
expired cookie, that 403 produces re-login, retry, 403 again and a `502` to
Home Assistant, and the next poll does it all over: on the order of 170
re-login warnings an hour, `Camera discovery poll failed` every cycle, and
**both camera streams down for the whole window**, because each session
rotation needs one website call (the video-source fetch) and that call walks
into the block on the 2/4/8/30 s reconnect schedule. The container stays `Up`
with zero restarts and the bridge connectivity sensor stays `on` throughout:
container state tells you nothing here, read the bridge log. The window closes
on its own when the `Retry-After` elapses; restarting the bridge only adds a
login to the limiter. Home Assistant's own session is on the same account and
gets the same 403 (`KeepAlive.aspx`) within a couple of minutes. Both camera
streams going down together is what separates this from a wedged camera (see
[a camera that reaches `webrtcup` but never delivers a frame](#a-camera-reaches-webrtcup-but-never-delivers-a-frame)),
which takes one camera down while the other streams.

**Fix / design.**

- **Honor `Retry-After`.** The bridge waits for the server-specified interval;
  the cap on that wait is 2 hours (`RATE_LIMIT_RETRY_MAX_S`), well above the
  1825 s window above, because a cap under a real wait is itself an early
  re-knock.
- **Treat a 403 that survives a fresh login as the block it is.** The bridge's
  process-wide cloud hold (`cloud_hold.py`, backlog E104) stops every website
  call -- control routes, video-source fetches, discovery -- for 60 s, doubling
  per consecutive strike up to 30 min; the first call after a hold expires is
  the probe. An ordinary cookie expiry still costs one login and one retry.
  Held routes answer `429` with the remaining wait, and the camera loops sleep
  it instead of reconnecting into it. The log says `Holding every TELUS website
  call for Ns: ...` once per hold and `cloud hold cleared` when a call succeeds.
- **Do not tight-retry.** Back off; let the restart guard provide the floor for
  the process-death paths.
- **Don't increase your own request rate to compensate for slow updates.**
  Lowering `update_interval` or the event poll to "catch up" trades directly
  against 429 exposure. Keep the poll at its default and accept the push design
  above. The account's steady-state budget is what causes the block in the
  first place: the camera-settings poll is 600 s because it is seven website
  reads per camera per cycle (~420 reads an hour for two cameras at 120 s,
  more than both 30 s event polls together), and every TELUS-backed
  coordinator pauses for the bridge's `Retry-After` instead of re-polling into
  it (backlog E105).

> On the integration side, wrong-password vs. MFA-challenge vs. server-error is
> deliberately differentiated at the *exception-type* level
> (`invalid_auth` / `telus_sso_error` / `cannot_connect`), **not** by
> inspecting the cloud's actual response bodies; doing the latter safely would
> require deliberately triggering those responses against a live account, which
> risks exactly the rate-limiting/lockout described here.

---

## A doorbell notification fires on every Home Assistant restart

**Symptom.** "Someone rang the doorbell", or anything else an automation does
on a doorbell press, happens right after Home Assistant restarts, with nobody at
the door.

**Cause.** The automation triggers on the state of
`event.<doorbell>_button_press`. An event entity restores its last event when
Home Assistant starts, so its state goes from `unavailable` to the timestamp of
the last real press. To a `state` trigger that is a new state, and filtering
`to`/`not_to` on `unknown`/`unavailable` cannot help: the new state is a
timestamp.

**Fix.** Trigger on `event.received` for the event type `ring` instead (see
[Ringing the doorbell as a trigger](../README.md#ringing-the-doorbell-as-a-trigger)
and `ha-config/telus_doorbell.yaml`). It fires once per new event and never for
the restored one.

---

## Doorbell / event snapshots arrive tens of seconds late

**Symptom.** A cloud "person at the door" / video event, and the snapshot it
carries, lands well after the person has actually come and gone, commonly on
the order of **30–60+ seconds** late.

**Cause.** This latency is upstream and **cannot be tuned away**:

- The integration polls video events on a **fixed** interval (hardcoded in
  `const.py`, used directly by the event coordinator). The options-flow
  "Update Interval" drives only the main data coordinator, **not** video
  events, so changing it does nothing here. The websocket push patch doesn't
  carry these events either; it only re-notifies listeners over objects
  already in memory.
- On top of the poll sits the cloud's own clip-processing delay (roughly
  another ~30 s) before the event/snapshot is even available.

Lowering the poll interval cannot fix it: the upstream processing alone
outlasts the walk from the door, and tripling the API rate against a
rate-limiting cloud is a bad trade (see
[rate-limiting](#alarmcom-rate-limiting-http-429)).

**Fix / design: treat the cloud as a *fallback*, do local detection for the
fast path.** The recommended architecture is:

- Run **local, on-box detection** (e.g. a local NVR/detector consuming the
  bridge's stream) as the **primary**, low-latency source. Detect-to-publish is
  sub-second locally versus ~30–60 s for the cloud event.
- Use the **cloud event as the fallback only**, gated behind a suppression
  window so the same person isn't announced twice: once fast (local) and once
  late (cloud). A "recent detection" window (a few minutes, long enough to
  outlast the cloud lag) suppresses the fallback when local detection already
  fired.
- **Fail-open gating.** Fallback-suppression conditions must fail *open*: after
  a template reload a sensor can read `unknown`, and a strict "is off" test
  would silently mute the fallback. Test `not is_state(..., 'on')` so an
  unknown state still lets the fallback through rather than dropping it.

To feed local detection *without* adding cloud load, consume a **re-served /
multiplexed** copy of the bridge's RTSP stream (one upstream session fanned out
to many local consumers) rather than opening a second direct session; that
keeps you off the cloud entirely for the detection path and can't trip the
[restart guard](#container-restart-loop-against-an-upstream-outage).

**Diagnosis: the expired-token image trap.** When fetching a snapshot with a
stale cloud image token, the server returns **HTTP 200**, *not* 401, with a
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

**Fix / design: proactive session rotation plus frame-silence detection.**
These are **two separate, deliberately redundant safety nets**; do not collapse
them into one:

1. **Proactive rotation**: every camera session is rotated on a fixed cadence
   (`SESSION_MAX_LIFETIME_S`, currently 165 s) *before* the ~180 s server-side
   kill, so the feed never goes dark waiting for a server-initiated teardown.
2. **Frame-silence detection**: if no frame arrives for
   `FRAME_SILENCE_TIMEOUT_S` (15 s), the session is torn down and reconnected
   immediately, without waiting for the rotation deadline. This check runs on
   **every** loop iteration, including when zero frames have ever arrived, which
   is what makes it safe against a camera that stalls at the very start.
3. **Gap bridging**: the ffmpeg publisher **outlives** rotations; each
   rotation/reconnect gap is bridged by **repeating the last real frame** into
   the still-running publisher (up to `FRAME_HOLD_MAX_S`, 60 s). Readers see a
   brief freeze, not a torn-down RTSP path. A genuinely offline camera stops
   being held after 60 s and the feed is allowed to go dark honestly.

Because the publisher now outlives sessions and gaps are frame-held, the
rotation margin can be tightened (to 15 s, matching the frame-silence timeout)
to keep those frame-hold freezes as infrequent as possible.

**Do not regress this.** Don't collapse the two checks, and **don't change
`SESSION_MAX_LIFETIME_S` without re-verifying the cloud's actual server-side
timeout live**: it isn't documented anywhere; the ~180 s value was *observed*.
If you touch `camera_session.py` or `janus_client.py`, read them in full first.

**Diagnosis.** A feed that freezes briefly on a ~15/165 s cadence and recovers
is the rotation/frame-hold working as designed. A feed that freezes for ~60 s
then goes dark is the frame-hold expiring on a genuinely offline camera.
Per-camera health (`state`, `consecutive_failures`, `last_frame_at`, `fps`,
`dropped_frames`, `last_error`) is available from the bridge's
`GET /control/status` endpoint (same auth as the other control endpoints),
and, with the HA integration installed, as entities: each camera carries
**Stream state**, **Dropped frames** and **Audio state** diagnostic sensors,
and a **Camera bridge** connectivity sensor says whether the bridge is
answering at all, so this diagnosis usually needs no curl (see
[ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity)).
`dropped_frames` is the lifetime count of frames the ingest queue discarded
because the consumer fell behind: a steadily climbing total on one camera says
something downstream of the decoder is stalling that stream, where the picture
alone just looks subtly choppy (see the next section for the cause that is not
the encoder). It updates when each ~165 s session ends, so it can lag the live
session by up to one rotation. `publisher_alive` and `last_publish_at` answer the
question the rest of the response cannot: whether ffmpeg is still taking the
frames `last_frame_at` says arrived.

---

## A camera reaches `webrtcup` but never delivers a frame

**Symptom.** One camera's feed is down and stays down: every session attempt
logs `webrtcup`, then fails with `no frames received before timeout (waited
20.0s of 20.0s; webrtcup seen before the wait)`, repeating on the reconnect
backoff indefinitely. RTSP readers log `no stream is available on path
'<path>'`, the camera's **Stream state** sensor sits on `reconnecting`, and
the other camera streams normally the whole time.

**Cause.** The subscriber leg is healthy - ICE completes and the gateway
answers - but no media is being published into the session. When the sibling
camera streams normally through the same process, the bridge, host networking
and cloud path are all demonstrably fine, and the fault is the camera's own
publish leg (or the cloud's session for it). A device in this state can still
look otherwise alive: settings reads/writes and activity clip uploads keep
working and its reported WiFi signal stays normal, because those travel over
HTTP rather than the wedged media path.

**What the bridge does.** Retries forever on the capped backoff. The wedge
escape hatch (see
[Container restart-loop against an upstream outage](#container-restart-loop-against-an-upstream-outage))
is deliberately vetoed while a peer camera is delivering (`no successful
frame for Ns ... NOT restarting`): nothing local is wedged, so a container
restart cannot reach the fault, and neither can any other lever the bridge
has. After `NO_MEDIA_STATE_AFTER_S` of this with the peer still delivering,
the camera's `/control/status` state and its Home Assistant **Stream state**
sensor read **`no_media`** instead of `reconnecting` -- that is the value to
build an alert on, since the short blips of the same signature a weak Wi-Fi
link produces (30-60 s, self-healing) never reach it. With
`DEVICE_KICK_ENABLED` the bridge also cycles the camera's live-resolution
setting once per `DEVICE_KICK_COOLDOWN_S` to force an encoder reconfigure, and
records each attempt in the `device_kick*` status fields; see
[camera-bridge.md](camera-bridge.md#device-side-escalation-device_kickpy)
for what the kick writes and for the limit of what is known about its effect
(one live occurrence, recovery seven minutes after the kick, no proof of
cause). Backlog E103.

**Fix.** Confirm live view also fails in the vendor's own app, which isolates
the device/cloud from this stack; then power-cycle the camera. If the opt-in
kick is enabled, `last_device_kick` says whether it was sent and what it did;
a `failed: ... camera LEFT at ...` value means the restore write failed and
the live resolution must be set back by hand through the settings route or the
vendor app.

---

## Every session publishes, then dies ~5 s later

Both cameras cycle `streaming` → `reconnecting` around the clock. The log shows
the publisher start, MediaMTX accept the publish, then exactly the watchdog
interval later `ffmpeg has not read a frame from the video pipe in 5s -- the
process is alive but is publishing nowhere`, a failed session, and the same
again well under a minute later. `consumer falling behind` drops pile up in
between, and the session storm can push the process into the container memory
limit (memcg OOM kill, then `restart: unless-stopped` brings it back into the
same loop).

The cause is an audio filter with LOOKAHEAD in the publisher's `-af` chain
(`dynaudnorm`, `loudnorm`, anything that buffers a window before emitting its
first sample). The mux interleaves two piped inputs and ffmpeg only keeps
reading the video pipe while the audio stream stays ahead of it; a lookahead
filter delays every audio packet by its window, the mux holds the video back
waiting, and the pipe watchdog reads that as a dead encoder and rebuilds --
forever. This is why `AUDIO_NORMALIZE_FILTER` defaults to `anull` and loudness
normalization is done in process (`AUDIO_NORMALIZE_TARGET`, a zero-lookahead
AGC in the sink). Keep it that way, and treat any new `-af` entry as the prime
suspect whenever this signature appears.

## Frames dropped as `consumer falling behind`, on the camera with audio

**Symptom.** One camera logs `dropped N frame(s) -- consumer falling behind`
continuously (hundreds to thousands per ~165 s session) while the other camera
in the same process drops none. `/control/status` looks healthy otherwise. The
picture is choppy; nothing errors.

**Cause.** Not the encoder, and not the camera's resolution. It is the **audio
input pacing the publisher**. ffmpeg's muxer can only emit a video packet once it
holds audio covering that packet's timestamp, so the audio has to stay ahead of
the video; the audio pipe is primed with a 100 ms lead and that lead decays by
elapsed real time between top-ups. A quiet camera (a doorbell is quiet nearly
all the time) is fed only by the silence top-up, and if that runs less often
than the lead, ffmpeg **stops reading the video pipe** until the next top-up
arrives. The frames that pile up behind it are dropped by the ingest queue, a
layer away from anything audio-shaped.

Measured in the deployed container at 15 fps, 900 frames per 60 s run: a 0.5 s
top-up cadence stalled 120 of 900 frames (13.3%), a 0.1 s cadence stalled 162, and 0.05 s
stalled none. Two hypotheses that look obvious here are **disproven**: the
per-frame pixel copy costs 0.24 ms of a 66.7 ms frame interval, and pre-scaling
the source changes the stall count by exactly zero.

**Fix / design.** `AUDIO_SILENCE_TOPUP_S` is 0.05 s, half the lead, and no
ordinary top-up may hand ffmpeg more than the lead's worth of PCM
(`_AUDIO_SILENCE_MAX_BURST_S`). The frame hold ticks on whichever deadline is
shorter (the camera's frame interval or the top-up cadence) because one top-up
per repeated frame is 167 ms on a 6 fps camera. A deficit past
`_AUDIO_CATCHUP_DEFICIT_S` (0.5 s) is a hole rather than a late top-up and is
closed in one write bounded by `_AUDIO_CATCHUP_MAX_BYTES`, because repaying
seconds at the ordinary ceiling means seconds of negative lead and every frame
behind the video pipe lost for the whole of it. See
[camera-bridge.md](camera-bridge.md#the-audio-path).

**Diagnosis.** Frames dropping on the camera that has `audio_consumed: true`
while a video-only camera in the same process drops none is this, not encode
contention. If both cameras drop, look at the encoder instead.

**What the log does not tell you.** The drop path logs the **first** drop of a
session and then only every 150th, while `/control/status` accumulates across
sessions. A session that drops fourteen therefore leaves exactly one line, and
reading the log alone suggests one drop per rotation. If the drops cluster at
rotations rather than running through the session, the section below is the way
to see it.

---

## Frames lost at every rotation, in a burst just after the new session starts

**Symptom.** With `HANDOVER_TRACE` on, some rotations of the camera that
consumes audio report `hold.silence_calls` of 0 to 2 across a two to three
second window, `hold.video_writes` of 0 or 1, `hold.last_ms` a few tens of
milliseconds into a window whose `hold_stopped` mark is seconds later,
`lead_min_ms` in the negative thousands, and 6 to 19 dropped frames whose
timestamps all land after the new session's audio pump starts, spaced one frame
interval apart. `sink_rebuilt` reads `false` and nothing in the ordinary log says
anything at all.

**Cause.** The frame hold stopped writing. ffmpeg reads the video pipe only
while it holds audio covering the frame's timestamp, so `write_frame`'s drain
does not complete while the audio timeline is behind the wall clock. With the
hold's video write awaited inline, that blocked write also held up the silence
top-up in the same loop pass, and that top-up was the only thing left that could
advance the timeline: the previous session's audio pump is stopped as the hold's
first act and the next one is not created until the hold has finished. The two
writers waited on each other for the rest of the gap. Nothing detected it,
because `write_frame`'s own drain bound is 5 s and the gap is shorter, so the
publisher was never marked stalled and no rebuild was triggered. The drops
themselves come afterwards, while the multi-second deficit is repaid.

**Fix.** The hold writes its repeated frame from a task of its own and reaps it
on a later tick, so the audio top-up runs on every tick whatever the video write
is doing; and a deficit past 0.5 s is closed in one bounded write rather than at
twice real time. Both are described in
[camera-bridge.md](camera-bridge.md#the-audio-path).

**Diagnosis.** `hold.silence_calls` is the number to read: it is the hold's
cadence, and it collapses when the hold stops. `hold.silence_writes` and
`hold.silence_ms` being 0 while `silence_calls` is in the tens is a different and
healthy thing, namely a timeline that is already ahead of the wall clock and owes
nothing.

---

## Measuring what a session rotation costs the audio timeline

**When to use this.** Frames are lost at **session rotations** rather than
throughout a session: the drop warnings sit between `proactive session rotation`
and the new session's first frame, and the camera has audio consuming. Measured
on the doorbell, same camera and rate and build, switching only the audio knobs:
**~14 dropped frames per rotation with audio against 0.8 without**, about 0.9 s
of video once per 165 s cycle.

**Turn it on.** `HANDOVER_TRACE=true` in the bridge's `.env`, then
`docker compose up -d --build`. It is off by default. On, one rotation costs
about 70 µs of CPU and one ~1.5 kB log line per camera; nothing it does awaits,
sleeps or writes, so a multi-hour soak is fine. Leave the audio knobs exactly as
they are: the point is to measure the deployment as it runs.

**Read a record.** Everything after the prefix is one JSON object:

```bash
sudo docker logs telus-cam-bridge 2>&1 \
  | grep 'handover trace' | sed 's/.*handover trace //' | jq .
```

| Field | What it answers |
| --- | --- |
| `marks[]` | The window, in order, from `rotation` to `settled`. Each carries `t` (ms from the rotation), `dt` (ms since the previous mark), **`adv`** (ms of audio actually written to ffmpeg since the previous mark), **`lead`** (ms the audio timeline is ahead of the wallclock-stamped video) and `q` (video frames queued for the publisher). |
| `worst_gap` | The interval where the audio timeline fell furthest behind the wall clock, and the two marks bracketing it. `ms` is `dt - adv`: real time that interval did **not** cover with audio. A few ms is scheduling jitter. |
| `lead_min_ms` / `lead_min_at` | The lowest lead seen, and where. **Negative means the audio no longer covers the video**, which is when ffmpeg stops reading the video pipe (`_AUDIO_PRIME_MS`). A lead far **above** the 100 ms prime is normal and not an instrument fault: the prime is the lead established when the pipe opens, the silence top-up can only raise the lead back to zero, and decoded audio is written as it arrives, so a queued backlog drained by the pump puts the timeline seconds ahead. |
| `drops` | Every dropped frame in the window, in ms from the rotation, split into the `retiring` session and the `new` one. `session_total` beside each is that session's own lifetime count. |
| `hold` | The frame hold's work through the gap, with the **cadence and the content kept apart**. `silence_calls` is how many top-ups it made and `max_gap_ms` the longest interval between two of them; both describe the tick, and `max_gap_ms` must stay under the 100 ms lead. `silence_writes` is how many of those calls actually put samples on the pipe and `silence_ms` how much audio they carried, both read off the sink's sample counter. `video_issued` and `video_writes` are the same distinction for the repeated frames: started, against accepted by ffmpeg. |
| `audio_ms` vs `span_ms` | Audio written across the whole window against wall time across the whole window. **These two should match.** |
| `first_frame_ms` | When the new session's first frame arrived, as opposed to when the loop noticed it. |
| `sink_rebuilt` | Whether this rotation also rebuilt the publisher. A rebuild opens a new audio pipe with a fresh 100 ms prime, so such a record is not comparable with an ordinary rotation. |

The marks worth knowing by name: `pump_stopped` (the previous session's audio
pump has really finished), `hold_first_silence` (the frame hold has taken over
the pipe), `hold_stopped` (the hold has let go and the next pump does not exist
yet), `pump_created` (the new pump is scheduled, not yet running),
`pump_first_write` (it has actually written). `hold_stopped` to
`pump_first_write` is the serialization window: the two writers to the audio
pipe are mutually exclusive by design, and if the audio timeline stops advancing
anywhere in there, that is where it happens.

**What a healthy rotation looks like:** `hold.silence_calls` in the tens with
`max_gap_ms` around 50, `hold.silence_ms` roughly covering the part of `span_ms`
the timeline actually owed, `lead_min_ms` no worse than a few tens of ms
negative, and `drops.total` 0 or 1. `adv` of 0 across a long `dt` is only a fault
when the lead is at or below zero: with the timeline seconds ahead, nothing is
owed and nothing is written, and `hold.silence_calls` beside it is what says the
hold is still running.

**What a frame hold that has stopped looks like** (E88, fixed): `hold.silence_calls`
collapsing to 0 to 2 against a two to three second `span_ms`, `hold.video_writes`
at 0 or 1, `hold.last_ms` tens of milliseconds into a window whose `hold_stopped`
mark is seconds later, `lead_min_ms` in the negative thousands, and every entry in
`drops.new.at_ms` landing after the new pump starts, at frame-interval cadence,
while the deficit is repaid. `sink_rebuilt` reads `false` throughout, because
nothing detected anything: the hold's video write was parked inside a drain whose
own 5 s bound is longer than the gap.

**What a stalled serialization window would look like:** `worst_gap.from =
hold_stopped`, `worst_gap.to = pump_first_write` with `adv: 0`, `lead_min_ms`
negative, and `drops.new.at_ms` inside that same interval. Ten live records show
that window at a fraction of a millisecond, so it is not where this deployment's
drops come from.

**The limits of the measurement**, so a reading is not over-claimed: the lead is
what the *bridge* has handed ffmpeg, not what ffmpeg's muxer has consumed (the
muxer's own state is not observable from this process); a stall is located
between the two marks that bracket it and no more finely; and nothing schedules
a sample of its own, because a sampler running on the event loop during the very
window being measured would perturb it. The records are in the **log only**, not
in `/control/status`: a handover is an event, and a snapshot endpoint polled
continuously would report each one an arbitrary number of times or not at all.

---

## Attributing the container's memory

**When to use this.** The container's memory climbs and it is not known which
process is climbing. The number `docker stats` reports, and the number the stack
watcher alerts on, is the **cgroup's**, and the cgroup holds four processes:
`entrypoint.sh` runs MediaMTX and `python3 -m bridge.main` side by side, and the
bridge launches one ffmpeg per camera. All of them count against
`docker-compose.yml`'s `mem_limit` (768 MiB by default). "The bridge is leaking"
is therefore a claim about a sum.

**Do the free measurement first.** One command decomposes it, with no rebuild,
no restart and no code:

```
sudo docker exec telus-cam-bridge sh -c 'for p in /proc/[0-9]*; do \
  echo "$(cut -d" " -f2 $p/statm) $(cat $p/comm)"; done | sort -rn'
```

The first column is resident pages (multiply by 4096 for bytes). Repeat it ten
minutes later and subtract. If the growing row is `mediamtx` or `ffmpeg`, nothing
in the Python bridge is the cause and no amount of reading `camera_session.py`
will find it.

**Then turn on `MEMORY_TRACE`** if the growing row is `python3`. Set
`MEMORY_TRACE=true` in the bridge's `.env` and `docker compose up -d --build`. It
is off by default, and unlike `HANDOVER_TRACE` it is **not** safe to leave on: it
enables `tracemalloc` (roughly double per-allocation cost, plus a frame stored
per live allocation) and each sample walks every gc-tracked object.

```
sudo docker logs --since 1h telus-cam-bridge | grep 'memory trace' \
  | sed 's/.*memory trace //' | jq .
```

One line every `MEMORY_TRACE_INTERVAL_S` (330 s by default, which is two session
rotations, so any two consecutive records bracket at least one whole rotation per
camera). Read it outermost first:

| Field | What it tells you |
| --- | --- |
| `container.procs` | Every process in the container, largest RSS first, with `container.rss_mb` as the sum over all of them and `container.procs_total` as how many there were. This is the same decomposition as the command above, taken automatically beside everything else. |
| `python.rss_mb`, `python.rss_mb_per_min` | The Python process only, and its rate since the previous record. **This is the number to judge a climb by** -- never `tracemalloc.traced_mb`, which grows because tracing is on. |
| `glibc.in_use_mb` vs `glibc.free_not_returned_mb` | Whether the Python heap is full or the allocator is simply holding freed memory. A large and growing `free_not_returned_mb` against a flat `in_use_mb` is per-thread arena behaviour, not a leak: this process runs ~119 threads, and glibc allows many arenas each able to reach 64 MB. `tracemalloc` cannot see this, because it only tracks the Python allocator. |
| `python.gc_growth` | The object types that grew since the previous record, biggest movers first. A retained decoded audio or video frame appears here as a climbing `AudioFrame` / `VideoFrame` count and nowhere else. |
| `tracemalloc.top` | Allocation sites, sorted by `d_mb` (growth since the previous record) rather than by size, so a large steady buffer does not outrank the thing that is actually growing. The first record has `d_mb: null` on every row, because it has nothing to diff against. |

**What each answer means.** Growth in a non-`python3` row is MediaMTX or ffmpeg
and is not fixable from this repository. Growth in `glibc.free_not_returned_mb`
with a flat `in_use_mb` is allocator behaviour, and `MALLOC_ARENA_MAX=2` in the
container's environment is the lever for it. Growth in `python.gc_growth` with a
matching `tracemalloc.top` row names the retention directly.

**Turn it off again** once the question is answered: `MEMORY_TRACE` unset (or
`false`) and rebuild.

---

## The status says `streaming` but MediaMTX has no path

**Symptom.** `state: "streaming"`, `consecutive_failures: 0` and a fresh
`last_frame_at` for a camera, while MediaMTX logs `no stream is available on
path telus_...` and refuses every reader. Nothing reconnects.

**Cause.** The bridge used to have no way to tell "publishing" from "publishing
to nowhere". The frame-silence watchdog watches frames *arriving from Janus*, and
the publisher-repair check asked only whether the ffmpeg **process** had exited,
which, throughout the outage, it had not.

**Fix / design.** The sink now bounds `stdin.drain()` (`_VIDEO_DRAIN_TIMEOUT_S`,
5 s), so an ffmpeg that has stopped **consuming** frames is detected, marked, and
rebuilt rather than left in place, and, just as importantly, no longer parks the
camera's loop forever on an await with no timeout. `/control/status` carries
`publisher_alive` and `last_publish_at` beside `last_frame_at`.

**Diagnosis.** Poll `/control/status` twice: `last_frame_at` advancing while
`last_publish_at` does not is a publisher that has stopped reading. Note the
limit of the check: it proves ffmpeg is reading its input, not that MediaMTX
still accepts what ffmpeg writes, so an ffmpeg encoding into a socket that goes
nowhere would still read as alive. If the status looks healthy and MediaMTX still
has no path, check MediaMTX's own log for the publisher's ANNOUNCE.

---

## A live view drifts further behind real time the longer it runs

**Symptom.** The picture is fine and nothing errors, but the live view lags real
time and the lag *grows*: a few seconds after a minute, tens of seconds after
several. A clip recorded straight off the RTSP path with `-c copy` is shorter
than the wall time it was recorded over.

**Cause.** The publisher's video timeline is advancing per frame **written**
instead of per second **elapsed**, so it publishes less media than wall time and
a consumer playing it at real speed falls behind by exactly the shortfall. The
known trigger is `-r <fps>` before `-i` on ffmpeg's raw video input: it forces
the input stream's rate and overrides `-use_wallclock_as_timestamps`. The ratio
is exact: a camera fed at 9 fps on an input declared 15 fps publishes 9/15 of
wall time, measured as 24.07 s of media per 40 s.

**Check.** Compare published duration against wall time by recording the path
for a known interval and probing the result:

```sh
docker exec telus-cam-bridge sh -c \
  'ffmpeg -hide_banner -loglevel error -rtsp_transport tcp \
     -i "rtsp://bridge:$(cat /shared/rtsp_password)@127.0.0.1:8554/<path>" \
     -t 30 -c copy -f mpegts /tmp/probe.ts && \
   ffprobe -v error -show_entries format=duration -of csv=p=0 /tmp/probe.ts'
```

A healthy stream reports ≈30 s. Then check the publisher's own command line:
`docker exec telus-cam-bridge sh -c 'tr "\0" " " < /proc/<pid>/cmdline'`, where
there must be **no `-r` before `-i pipe:0`**.

**Fix / design.** The camera's frame rate is declared with the rawvideo demuxer's
`-framerate <fps>` instead, which leaves the timestamps to
`-use_wallclock_as_timestamps`. Note that simply deleting the rate declaration
trades this symptom for another one: the demuxer then falls back to its default
25 fps and re-rates the output, duplicating frames for a slower camera (visible
as `More than 1000 frames duplicated` in the ffmpeg log, plus encode load and
bitrate well above the camera's real rate) or dropping them for a faster one, and
`-g <fps>` no longer spans ~1 s. See
[camera-bridge.md](camera-bridge.md) for the measurements. That duplication
warning has a second, more common cause (a declared rate that is simply higher
than the camera delivers) which is
[its own section below](#more-than-1000-frames-duplicated-on-a-healthy-looking-camera).

---

## `frame rate changed`: the publisher rebuilds and readers reconnect once

**Symptom.** A camera's RTSP readers all reconnect at once, HLS viewers see one
discontinuity, and the log carries:

```
<camera-id>: measured 7fps against the declared 15fps -- waiting for a second measurement to agree before rebuilding the publisher
<camera-id>: frame rate changed (declared 15fps, now measuring 7fps) -- rebuilding the publisher at this rotation so -framerate and -g describe the stream again. Readers will see one reconnect; this is not a fault.
<camera-id>: ffmpeg publishing 960x540@7fps (source 1280x720) to ... (hw/vaapi, video only)
```

**This is not a fault.** It is the bridge doing the right thing. The frame rate
is a *device setting* (the live-video resolution select ships as a Home
Assistant entity) so it can be changed from HA, the TELUS app or the portal with
no restart and no signal to the bridge. ffmpeg is told the rate once, on its
command line (`-framerate`, which is the output's constant frame rate, and `-g`
for a ~1 s GOP), and cannot be told again, so the only way to honour a changed
rate is to rebuild the publisher, exactly as for a changed frame size.

**What to expect.** One reconnect, at a session rotation, and no gap in the
published video beyond the rotation's own brief freeze (the frame hold bridges
it). `/control/status` reports the new rate in `fps` from the next session on.
`consecutive_failures` stays at `0`: a rate change never fails a session and
never counts toward the container-restart escape hatch.

**When it *would* be worth investigating:**

- **It repeats.** A rate change needs two consecutive settled measurements to
  agree, and there is a 300 s floor (`RATE_REBUILD_MIN_INTERVAL_S`) between two
  rate-driven rebuilds, so this line should be rare. A delivered rate that
  genuinely swings across the hysteresis band with no setting change (a real
  thing: the 30 fps camera on this account halves its delivered rate under
  marginal light and comes back, dwelling minutes at each end) is recognised
  by the **flap guard** and produces at most two of these lines per episode
  (the swing down, then the resolution up), followed by
  `the delivered rate is flapping between ...` once, at warning level. From
  then on the publisher is held at the flap's **higher** rate; expect
  `More than 1000 frames duplicated` from ffmpeg during the low dwells, which
  is the deliberate, content-safe trade (see below), and confirmed swings
  back down are refused quietly. If rate-change lines keep repeating *without*
  the flapping warning, check `dropped_frames` and the network path to the
  camera before suspecting the bridge.
- **`the confirmed change to Nfps waits for the rebuild floor`** means the
  storm bound is pacing a confirmed change; it applies on the first agreeing
  measurement after the floor. Routine, logged once per pending change.
- **The new rate looks wrong.** Compare it against the camera's resolution
  setting in Home Assistant. The bridge measures what actually arrives, so a
  measured rate well below the configured one is a delivery problem (upstream
  bandwidth, a struggling camera), not a measurement bug.

---

## `More than 1000 frames duplicated` on a healthy-looking camera

**Symptom.** ffmpeg logs `[vf#0:0] More than 1000 frames duplicated` for one
camera, minutes into a session. The picture is fine and nothing else complains.

**What it means.** ffmpeg is padding the output to keep it CFR, because it is
being told a frame rate **higher than the source actually delivers**. Every
padded frame is encode work and bitrate spent on a frame that carries nothing.

**Two different causes, and they need opposite fixes:**

| Cause | How to tell | Fix |
| --- | --- | --- |
| The declared rate is too high for what this camera delivers | the publish line shows a specific rate (`...@29fps`) and the duplication starts within a few minutes | nothing: the adaptive re-measurement corrects it; see the section above. If it *never* corrects, either the gap is inside the hysteresis band (under 20% low) or the **flap guard** is deliberately holding the publisher at the flap's higher rate (the log said `the delivered rate is flapping between ...` when it engaged); both are the intended, content-safe behaviour |
| No rate was declared at all | `-framerate` missing from the publisher's argv, output lands at exactly 25 fps | a regression: `-framerate <fps>` must be on the raw video input; see [camera-bridge.md](camera-bridge.md) |

**Check** which one it is from the publisher's own command line:

```sh
docker exec telus-cam-bridge sh -c 'tr "\0" " " < /proc/<pid>/cmdline'
```

There must be a `-framerate <fps>` before `-i pipe:0`, and no `-r` there at all.

**Note the direction.** Duplication is the *cheaper* of the two rate errors:
nothing is lost, only wasted. Declaring a rate **lower** than the camera delivers
makes ffmpeg *drop* real frames instead (measured: 1201 written, 1002 encoded,
199 gone) and produces no warning at all. That asymmetry is why the bridge
corrects a too-high declaration at a 20% gap but requires 35% before accepting
that a camera has sped up.

---

## A camera's stream carries no audio, or the audio stops

**Symptom.** A camera published by the bridge plays video but no sound, or the
sound stops while the picture keeps going.

**Cause and fix.** Audio is off by default and needs **two** knobs, both naming
the camera (see [configuration.md](configuration.md)):

1. `JANUS_REQUEST_AUDIO`: asks the gateway for the track at all.
2. `JANUS_CONSUME_AUDIO`: decodes it and publishes it.

Naming a camera in only the second is a misconfiguration, not a partial setup:
no audio track is ever offered for it, so the bridge logs
`JANUS_CONSUME_AUDIO names this camera but JANUS_REQUEST_AUDIO does not` **once**
and treats consumption as off. Check `/control/status`: `audio_consumed` is the
resolved answer, `audio_format` is the shape of a frame the camera has actually
decoded (`null` until one has been), `audio_codec` names the decoder those
frames came from, and `decoded_audio_frames` is the count of those frames.

**Read those two together first.** `audio_consumed: true` with
`audio_format: null` and `decoded_audio_frames: 0` means the bridge has never
decoded a single audio frame from this camera; the published track is silence
the sink generated, not audio the camera sent. If the counter is climbing
between two polls, audio *is* flowing and the problem is downstream.

**Then check the RATE it is climbing at.** Audio arrives in 20 ms frames, so a
healthy stream decodes about **50 frames a second**: two polls 30 s apart should
move `decoded_audio_frames` by ~1500. A counter that climbs at a few a second is
the [codec-mismatch case below](#audio-plays-in-fragments-that-jump-around),
not a quiet room, and it is the one failure here that a moving counter does not
rule out.

With the HA integration installed you do not have to do that comparison by
hand: each camera carries an **Audio state** sensor that does exactly this
across its 30 s polls and reports `not_consuming` / `no_audio_yet` / `flowing`
/ `stalled`, with the raw `decoded_audio_frames`, the per-poll delta and
`audio_format` as attributes (see
[ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity)).
`not_consuming` there is the answer to the two-knob question above; `stalled`
is the decoder-death case below.

If `audio_consumed` is `true` and there is still no sound:

- **An undecodable packet may have killed the decoder thread.** aiortc's Opus
  decoder has no error handling where its H.264 one does, and its decode loop is
  a thread target, so one bad packet ends the session's `audio-decoder` thread
  permanently: while RTP keeps arriving, the video watchdog keeps saying
  `streaming`, and the sink keeps filling the audio input with silence. The
  bridge patches that decoder (`bridge/aiortc_opus_patch.py`); the symptom of
  the patch being absent or having stopped applying is
  `Exception in thread audio-decoder` in the container log, one per session,
  with `decoded_audio_frames` pinned at `0`. See
  [camera-bridge.md](camera-bridge.md#the-audio-track-is-decoded-as-the-codec-that-arrives-not-the-one-advertised).
- **The camera may not be sending any.** The gateway offers the track whether or
  not the camera fills it. A camera that has never decoded a frame is published
  **video-only**, with no audio track at all: the publisher's audio input is
  built from an observed frame or not at all, so there is nothing to keep alive
  with silence and nothing to be wrong about (see
  [A camera publishes video but never gains an audio track](#a-camera-publishes-video-but-never-gains-an-audio-track)).
  `decoded_audio_frames` is what separates this from the decoder-death case
  above: both leave the camera without sound, and before that counter existed
  nothing in the status could tell them apart.
- **The consumer may not accept the codec.** The bridge publishes mono **AAC**
  specifically because HA's HLS worker takes only aac/mp3 and Frigate's `-c copy`
  preset needs an mp4-legal codec. A consumer configured to expect something else
  will drop it.
- **The publisher may have given up on its audio input.** If ffmpeg stopped
  reading the audio pipe, the sink drops audio and keeps publishing video,
  deliberately, since rebuilding the publisher would kick every RTSP reader off
  the path. `/control/status` reports `audio_dropped: true` with an
  `audio_dropped_reason`, and audio stays off for that camera until the publisher
  is next rebuilt for some other reason. This is the one case where
  `decoded_audio_frames` keeps climbing while the published stream carries
  nothing: frames are still being decoded, they just no longer reach ffmpeg.
- **Audio dying is not automatically recovered.** The frame-silence watchdog is
  video-only, so a camera whose audio stops still reports `state: "streaming"`
  with a fresh `last_frame_at` and nothing reconnects. It is *detectable*:
  `decoded_audio_frames` stops advancing between polls, which is what the HA
  **Audio state** sensor reports as `stalled` and what an automation can
  trigger on, but the recovery is still manual, and restarting the container
  is the blunt version of it. Watch
  `dropped_audio_frames` separately for a consumer falling behind; a stream that
  simply went quiet drops nothing.

Four log lines worth grepping:
`audio decoder <tag>: committing this session to` (which codec the session
decided the arriving bytes really are, with the counts it decided on; one line
per session per camera, and the `<tag>` ties it to that session's later drop
lines, since aiortc names every audio decoder thread `audio-decoder` and the
decoder does not know which camera it belongs to),
`dropped an undecodable packet and kept the audio decoder alive`
(the committed decoder throwing away a packet it could not read; the line keeps
carrying the length and first 16 bytes so a *different* undecodable shape would
show itself, and it is throttled, so read the running total from
`/control/status`'s `audio_decoder.discarded` rather than counting lines),
`ffmpeg has stopped reading the audio pipe`
(ffmpeg stopped draining input 1: the audio input is **dropped** and the same
publisher keeps publishing video; the line ends `publishing VIDEO ONLY from this
encoder`, and `/control/status` then carries `audio_dropped: true` with the
reason) and
`the publisher's audio input is now <shape>, was <shape>` (the publisher was
rebuilt at a rotation to read the shape the frames really carry; expected at
most `AUDIO_FORMAT_REBUILD_LIMIT` times per camera per process, and normally
exactly once, when a video-only publisher first gains audio). Its two
neighbours are `the publisher's audio input was launched for ... and the decoded
frames now carry ...` (said once per session while a rebuild is owed: the
mismatched frames are being skipped and their span filled with silence, so the
video is unaffected) and `the decoded audio shape keeps changing ... publishing
VIDEO ONLY for the rest of this process` (the bound was reached and the audio
input was given up on; `/control/status` then carries `audio_dropped: true` with
that reason).

**If `ffmpeg has stopped reading the audio pipe` appears at all**, the two
inputs' timelines have come apart: the audio timeline is real time, so if the
video timeline advances more slowly than the wall clock, audio outruns video, the
muxer's interleaver stops accepting audio packets, and the pipe backs up until
the sink gives up on audio. Audio for that camera stays off until the publisher
is next rebuilt for some other reason; video is unaffected. The known cause is a
video timeline running behind real time: historically `-r <fps>` on ffmpeg's raw
video input, which overrides `-use_wallclock_as_timestamps` and makes that
timeline advance per frame *written* rather than per second *elapsed*; no command
line the bridge builds carries it, on either path, for exactly this reason (see
[camera-bridge.md](camera-bridge.md)). The other way to starve that timeline is
for the camera to be **dropping frames** (`consumer falling behind` below), so
check that first. Confirm the command line with
`docker exec telus-cam-bridge sh -c 'tr "\0" " " < /proc/<pid>/cmdline'` for the
ffmpeg process: there must be **no `-r` before `-i pipe:0`**: the camera's frame
rate is declared as `-framerate <fps>` there instead. Any *other* cause of a
video timeline running behind real time would produce the same symptom.

---

## Hardware encode falls back to software (or fails to start)

**Symptom.** CPU usage is higher than expected on a machine with a capable GPU;
ffmpeg logs show `libx264` rather than a hardware encoder; or a chosen backend
never engages.

**Cause / design.** `ENCODE_BACKEND` (env var, see `.env.example`) only picks
which hardware path is **attempted first**. On each `start()`, an
unavailable/broken device **falls back to software automatically**: the
pipeline keeps running, just on CPU. So "it's on software" is a *symptom of the
hardware path failing*, not a crash.

Backends:

| `ENCODE_BACKEND` | Path | Notes |
| --- | --- | --- |
| `vaapi` (default) | Intel QuickSync / AMD via `/dev/dri` | The verified-live path. Which VA driver is used is decided by libva against the passed-through `/dev/dri` device, not by this project. |
| `nvenc` | NVIDIA via `nvidia-container-toolkit` device injection (not `/dev/dri`) | `h264_nvenc` is already compiled into the image's ffmpeg; you only need the host NVIDIA driver + container toolkit. **Implemented per ffmpeg's docs but not confirmed against real NVIDIA hardware by this project**: treat as unverified. |
| `software` | Always `libx264` | Skips the hardware attempt entirely. |

**Fix / diagnosis.**

- Confirm what actually engaged by reading ffmpeg's own log line for the
  publish process: it names the encoder in use. Hardware-encode status is also
  reflected per camera in `GET /control/status` (`hardware_encode`), and in HA
  as the same-named attribute on the camera's Stream state sensor.
- **VAAPI not engaging:** verify `/dev/dri` is passed into the container and
  the GPU is visible to libva. A device that's present but broken will silently
  fall back to software per `start()`.
- **NVENC not engaging:** verify the host NVIDIA driver and
  `nvidia-container-toolkit` device injection (the NVIDIA section in
  `docker-compose.yml` is commented out by default and must be enabled); this
  path uses device injection, **not** `/dev/dri`.
- If you only need it working and not fast, set `ENCODE_BACKEND=software`
  explicitly to stop chasing a hardware path that isn't available.

> Related but distinct: a passed-through iGPU that hardware-*encodes* fine can
> still be unreliable for hardware-*decode* in a *separate* local detector
> process (surface-sync failures, downloads failing, throttled capture fps).
> That is a decode problem in that other process, not this bridge's encode
> backend: if a local detector is silently missing frames, compare its actual
> capture fps against its configured detect fps and try software decode there.

---

## Audio plays in fragments that jump around

**Symptom.** A camera's stream has sound, but it arrives in short fragments
separated by silence, so speech is unintelligible and sounds like it is skipping
around. Every health signal is green: `state: "streaming"`,
`audio_consumed: true`, `audio_dropped: false`, and `decoded_audio_frames` is
climbing.

**Cause.** The bridge is decoding the audio track as the **wrong codec**. The
Janus offer advertises `audio(port=9,opus)`, and on this account's cameras the
bytes are **G.711 mu-law at 8 kHz**. A decoder that believes the advertised
codec rejects most of the stream, and the small fraction that happens to satisfy
a TOC byte decodes as noise; every rejected packet's span is published as the
sink's generated silence, which is why it sounds like fragments rather than like
an outage. Measured live while this was happening: the doorbell decoded **181
audio frames in 60 s** and the living room **268**, where 20 ms frames are 50/s,
so roughly 6 to 9 percent of the audio timeline was real.

**Diagnosis.** Two fields answer it directly, and neither needs the container
log:

```bash
curl -s -H "Authorization: Bearer $SECRET" $BASE/control/status \
  | jq '{decoder: .audio_decoder,
         cameras: (.cameras | map_values({audio_codec, audio_format,
                                          decoded_audio_frames}))}'
```

- Each camera's **`audio_codec`** names the decoder its frames actually came
  from: `"pcmu"` (G.711 mu-law, 8 kHz mono) or `"opus"` (48 kHz stereo). On this
  account it should read `"pcmu"`.
- The process-wide **`audio_decoder`** block accounts for every audio packet in
  exactly one of `decoded_opus`, `decoded_pcmu` and `discarded`. **A large
  `discarded` beside a small decoded count is the mismatch.** Healthy looks like
  `discarded` flat at or near zero while `decoded_pcmu` climbs at ~50/s per
  consuming camera.

Poll `decoded_audio_frames` twice 30 s apart as a cross-check: ~1500 apart is a
whole timeline; a few tens apart is a fragment stream.

**Why the log alone was not enough.** The decoder's drop line is throttled to
once per session on purpose, because a per-packet WARNING on a 50 packets/s
stream would be its own outage. That makes a *wholesale* mismatch look exactly
like an occasional glitch, which is how this went unnoticed for two days. The
counters exist so it cannot happen again; read them, not the line count.

**Fix.** Nothing to configure. The bridge decides per session, by decoding the
first 20 packets with both candidate decoders and committing to whichever one
actually reads the stream, and it logs the decision once
(`audio decoder <tag>: committing this session to ...`). If `audio_codec` reads
`"opus"` with a large `discarded`, the decision is going the wrong way: capture
the commit line, which carries the counts it decided on and the first packet
libopus rejected, and see
[camera-bridge.md](camera-bridge.md#the-audio-track-is-decoded-as-the-codec-that-arrives-not-the-one-advertised).

---

## A camera publishes video but never gains an audio track

**Symptom.** A camera named by both audio knobs publishes video normally, but
`/control/status` shows `publisher_audio_format: null` and the RTSP stream
carries no audio track. Or: the audio track appears, but only after a couple of
minutes of streaming, and readers are disconnected once when it does.

**This is the design, and the second half of it is convergence, not a fault.**
The publisher's audio input is built from a shape the bridge has actually
**observed** in a decoded frame, and from nothing else. Until there is one the
publisher is video-only. The decoder needs 20 audio packets to work out which
codec is really arriving (the track is advertised as Opus and carries G.711
mu-law), which is 1.28 s of continuous audio at the packetisation these cameras
use, and a quiet room does not send that inside the ~670 ms window in which the
publisher is sized. So on a sparse-audio camera the first session publishes
video only, real frames arrive later in that session, and the publisher is
rebuilt **once, at the next session rotation** to carry them.

What that looks like in the log, in order:

```
<id>: ffmpeg publishing 960x960@15fps ... (hw/vaapi, video only)
<id>: proactive session rotation
<id>: the publisher's audio input is now ('s16', 8000, 1), was absent -- audio-format rebuild 1 of the 1 this camera may spend in one process
<id>: ffmpeg publishing 960x960@15fps ... (hw/vaapi, audio aac mono from 8000Hz/1ch/s16)
```

and in `/control/status`, in the same order: `audio_format: null` →
`audio_format: "8000Hz/1ch/s16"` with `publisher_audio_format` still `null` (the
camera has been heard, the publisher has not been rebuilt yet) →
both set, with `audio_format_rebuilds: 1`. The one reader interruption is the
rebuild; there is no second one.

**A camera that never sends a single audio packet stays video-only forever**, and
that is correct rather than stuck: no frame decoded means no shape, no shape
means no audio input, and nothing fails. `audio_dropped` stays `false`, because
nothing was dropped. Check `decoded_audio_frames`: `0` and not moving is a
camera sending nothing (or a dead decoder thread; see
[A camera's stream carries no audio](#a-cameras-stream-carries-no-audio-or-the-audio-stops)).

**What failure looks like.** Two shapes, both visible in the log:

- **`the decoded audio shape keeps changing ... publishing VIDEO ONLY for the
  rest of this process`.** The bound (`AUDIO_FORMAT_REBUILD_LIMIT`) was reached:
  the shape moved again after the one rebuild it is allowed, so the audio input
  was given up on rather than rebuilding the publisher (and kicking every RTSP
  reader) once per rotation. `/control/status` carries `audio_dropped: true`
  with that reason and `audio_format_rebuilds` at the limit. A shape that moves
  between sessions means the codec sniff is deciding differently on different
  sessions; the `audio decoder <tag>: committing this session to` lines carry
  the counts each decision was made on, and the
  `audio_decoder` block's `discarded` says whether either decoder is really
  reading the stream.
- **`audio-format rebuild N of the 1`, more than once per camera per process.**
  It cannot exceed the limit, so this means the process has restarted, and the
  reason is elsewhere (the container restart-loop and consecutive-failure
  sections above).

**What it must NOT look like**, and what to grep for if audio ever goes wrong
again here: a camera failing session after session with `camera session failed`
while never publishing. Failing a session to repair audio is what this design
removes, because the codec sniff is per session, so a restart destroys the
evidence the repair is waiting on and the camera never converges.

---

## Talkdown returns 403 / 409 / 422

**Symptom.** A two-way audio (talkdown) request to the doorbell control
endpoint (`POST /control/cameras/{id}/talk`) returns an error status instead of
playing.

**Cause / fix, by status:**

| Status | Meaning | Fix |
| --- | --- | --- |
| **403** | Talkdown has been **disabled**. It is enabled by default, so this means `TALKDOWN_ENABLED` is set to a false value in the bridge's `.env`. | Remove that line (or set it `true`) and recreate the container so the change is picked up (`docker compose up -d`). |
| **409** | A talk is **already in progress** for that camera. The transport is single-session; a second concurrent talk is rejected rather than interleaved. | Wait for the current clip to finish, or cancel it with `POST /control/cameras/{id}/talk/stop` (or the integration's `stop_speaking` service), then retry. |
| **422** | **No upstream audio** could be produced: the request named an `audio_url`/`audio_path` (or TTS `message`) that yielded nothing to send. | Verify the audio source is reachable *from the bridge* and returns real audio. For an HTTP TTS URL, confirm the bridge can fetch it (loopback/`internal_url` reachability); for a file, confirm the path exists inside the container. |

Also note **400**: the endpoint rejects a `codec` other than `mulaw` on this
doorbell model (see next entry), and rejects malformed request bodies.

Triggered through `telus_smarthome_alarmdotcom.speak` rather than curl, these failures
arrive as `HomeAssistantError` messages naming the camera entity instead of raw
statuses, including **413** (the clip exceeds the bridge's size cap,
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
lo-fi, or, if you tried to raise quality, pure noise.

**Cause.**

- **The doorbell speaker is small and telephone-grade.** The talkdown path is
  8 kHz mono, inherently lo-fi. Music plays but will sound poor; that's a
  device limit, not a bug.
- **This doorbell model decodes the PUT body as G.711 mu-law regardless of the
  declared codec.** Sending 16-bit linear PCM (L16) over the PUT path produces
  **noise** in *both* byte orders: the device just interprets the bytes as
  mu-law. This is a per-model/firmware limitation, not a cloud-wide one (other
  models may accept L16 over a different transport). The endpoint therefore
  rejects any `codec` other than `mulaw` with **400**.

**Fix / design.**

- **Use mu-law.** It's the only codec that works on this doorbell's PUT path.
- **Default audio conditioning is applied to every clip before encoding**: a
  small-speaker / anti-clipping chain (high-pass, low-pass, compressor,
  limiter) tuned by ear. It's what makes speech intelligible on the small
  speaker. Leave it on unless you have a reason not to.
- **Per-request overrides:** send `"conditioning": false` to disable the
  default chain for one clip, or `"audio_filter": "<ffmpeg -af string>"` to
  replace it. The `telus_smarthome_alarmdotcom.speak` service takes the same two optional
  fields and forwards them to the bridge, so the override works from a Home
  Assistant automation as well as from curl. To disable conditioning globally,
  set the bridge's audio-filter config to empty.
- Pacing is schedule-based with a short prebuffer, so long clips play
  drift-free and the device sustains many minutes without a server-side cutoff:
  audio artifacts are about the codec/speaker, not timing.

**Diagnosis.** If you hear *noise* (not just low quality), you're almost
certainly sending non-mu-law audio, or raw audio the device is misinterpreting:
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
on **one** ADC-VDB750: it is not a vendor-wide constant, and a **firmware
update, a factory reset, or a replacement unit all change it legitimately**.

**Fix.** If you did any of those three, this is expected: copy the presented
fingerprint into `TALK_DEVICE_CERT_SHA256` in the bridge's `.env` and recreate
the container. If you did none of them, the connection is not the device it
claims to be: investigate before dismissing it.

**Note what the default mode does.** `TALK_TLS_PIN_MODE` defaults to `warn`,
which logs and **still connects**, so a mismatch never silences the doorbell. Move
to `enforce` only once the pin is known good for your device; a mismatch there
aborts the talk, which surfaces as `502 telus_error` to the caller and
`transport_error` in the talkdown counters, with the actionable detail in the log
above. The pin is never applied to the `cloud` endpoint: that one is the vendor
relay, a different host with its own CA-signed certificate.

Two related lines mean something different:

- `talkdown: no certificate pin configured; the device at <doorbell-ip> presented
  sha256=<observed> -- set TALK_DEVICE_CERT_SHA256 to this value to pin it`:
  there is nothing to verify against. Paste the value in to start pinning.
- `talkdown: TALK_DEVICE_CERT_SHA256=... is not a 32-byte hex SHA-256; ignoring
  the pin`: the knob is malformed, and talkdown continues **unpinned**. Fix the
  value.

The talk result reports `cert_pinned` (`true` matched, `false` mismatched in warn
mode) when a pin applied, so a mismatch is also visible to whoever triggered the
talk; see [control-api.md](control-api.md#post-controlcamerascamera_idtalk).

---

## Talkdown logs that no PutAudioStream endpoint answered

**Symptom.** Before audio starts, the bridge logs:

```text
talkdown: none of the PutAudioStream endpoints (direct,local,public,cloud)
answered a connect probe; trying direct anyway
```

and the talk then either works anyway or fails with a transport error.

**Cause.** The doorbell advertises up to four routes to its audio endpoint: two
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

**Fix, in order of likelihood:**

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
and auto-detection runs anyway: a mistyped knob must not silence the doorbell.
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
  back for a device that cannot do the thing**, so a field being present proves
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

**Fix, check what the camera actually reports:**

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
Nothing polls a write route: if a setting changed, something asked it to.

---

## No camera settings entities appear at all

Symptom: after a restart there is no speaker-volume slider, no signal-strength
sensor and no privacy switch for **any** camera, and
`custom_components.telus_smarthome_alarmdotcom.camera_settings_coordinator` logs
`Could not reach camera bridge` even though the bridge answers fine by hand.

These entities are created from the **first successful poll**: the bridge is the
only thing that knows which camera supports what, so nothing can be created until
a poll returns. That makes a failing poll fail *visibly as an absence*, which is
easy to misread as "the feature did not deploy".

Check the poll itself rather than the entities:

```bash
sudo docker logs --tail 2000 homeassistant 2>&1 | grep -a camera_settings_coordinator | tail -5
```

`Finished fetching TELUS camera settings data in N seconds (success: False)` is
the line that matters. If N is around 10 or 20, suspect a **timeout, not an
unreachable bridge**: the message is the same either way, because both arrive as
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
on a doorbell and **25s** on an indoor camera. That is normal, not a fault; it is
why the diagnostics fetch uses `CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS` (45s) instead
of the shared 10s bridge timeout. On the bridge side, each of those three upstream
reads carries its own 35s ceiling (`DIAGNOSTICS_READ_TIMEOUT_S` in
`camera_settings.py`), distinct from the bridge's shared 15s upstream-session
timeout: that override is what lets a 25s signal read succeed instead of being
cancelled by the bridge's own client and answered as a 502.

A single camera failing no longer blanks the others: the poll degrades that
camera (keeping its previous reading) and logs
`Camera settings poll failed for <id>`. Only a failure on *every* camera marks the
coordinator failed. If you see one camera named in that warning repeatedly, that
camera is slow or unreachable; the rest of the account is unaffected.

Note also that a poll can succeed and still produce no entities for a given
camera, which is correct: the siren buttons exist only for a camera whose account
reports the `canManuallyTriggerSiren` permission, and an account with no
siren-capable devices gets none. See "A camera control is missing" above.

## A bypass appears to do nothing

Symptom: `telus_smarthome_alarmdotcom.bypass_sensors` returns without error, but the sensors
look unchanged.

**Give it ten seconds.** The write reaches the panel asynchronously: a bypass
returns `200` immediately, but the sensor still reports `isBypassed: false` about
five seconds later and `true` about ten seconds later. An un-bypass takes the
same. The service already delays its post-write refresh past that window for
exactly this reason: a refresh fired the instant the POST returns reads the old
state and repaints the sensors unchanged, which is indistinguishable from a call
that did nothing.

If it is still unchanged well after that, check the two gates the partition
publishes, which the bridge re-reads before every write:

```bash
S=$(sudo docker exec telus-cam-bridge cat /shared/control_secret)
curl -s -H "Authorization: Bearer $S" http://127.0.0.1:8199/control/partitions
```

`has_permission_to_change_state` false, or `can_bypass_when_armed` false while
the partition is armed, are both refusals: they answer `422`, not a silent
no-op. A panel that reports `canBypassSensorWhenArmed: false` can only be
bypassed while disarmed.

Note also that the vendor's own portal hides its bypass control while the panel
is disarmed, gated on `extendedArmingOptions`. The endpoint honours the call
anyway; that is a UI choice, not an API constraint.

---

## A control answers `429`, or Home Assistant keeps retrying into a rate limit

Every route that makes a TELUS website call -- the events, history, clip and
snapshot reads, automation rules and their pause/resume, the camera settings
and diagnostics reads, every camera settings write (both its pre-read and the
write itself), and the siren, privacy, partition, bypass, arming-device and
Bluetooth-disarming routes -- answers `429` with a `Retry-After` header plus
`{"error": "rate_limited", "retry_after": N}`. The wait is **the server's own**
for an explicit edge rate limit, or the remaining time on the bridge's
process-wide cloud hold after a 403 that survived a fresh login (see
[Alarm.com rate-limiting](#alarmcom-rate-limiting-http-429)). Honour that value
rather than retrying on your own schedule; the next attempt otherwise walks
straight back into the limiter, and Alarm.com's limits are undocumented and can
lock the account.

The Home Assistant coordinators honour it: a TELUS-backed poll that is answered
`429` keeps its last page and does not poll again until the wait is over,
logging once `Alarm.com is rate-limiting this account; keeping the last ... and
not polling again for Ns`. The entities stay available on what they last knew;
the camera list and stream-health polls are loopback-only and are not held.

The Home Assistant entities surface a rate-limited **write** rather than
retrying: a rate-limited siren press, privacy toggle, camera-settings,
arming-device or Bluetooth-disarming write raises a `HomeAssistantError`
beginning "Alarm.com is rate-limiting this account" and carrying the server's
wait ("try again in {N}s", or "try again later" when no wait is readable). The
integration never retries the write itself: repeat the action after the stated
interval.

The one absorb: the settings read's two MWS-backed sections (siren and privacy)
swallow a rate-limited MWS read internally (the negative result is cached for
the server's stated wait and those sections degrade to `null`) so the response
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
restarts; see [control-api.md](control-api.md).

The same data is available without a curl: the `talkdown_status` and
`talkdown_failures` diagnostic sensors expose it in Home Assistant, and can
drive a notification when a talk fails; see
[ha-integration.md](ha-integration.md#talkdown-diagnostics).
