# Enhancement backlog

The project's single list of planned improvements, deferred ideas, and settled
questions: for the talkdown feature where it began, and for every adjacent
surface the project has since grown into (camera settings, arming devices,
bypass, the bridge and integration themselves).

Standing practice: **every known gap or potential improvement, however minor,
is logged here** rather than dropped, so a future agent (or the maintainer)
can pick it up. "Not implemented" means "not yet," never "unwanted." Add an
entry whenever something is deferred, simplified, or noticed in passing, and
when an entry is closed, keep it: a DONE entry's evidence is what stops the
next person re-deriving a settled question. IDs (`E1`, `E2`, …) are stable
references used across the docs and commit history.

Entry statuses: **open** (not started), **DONE** (shipped, entry records the
evidence), **BLOCKED** (needs hardware/access this account does not have),
**CLOSED: not achievable** (proven impossible, entry records the proof), and
**DECIDED** (a deliberate keep-as-is with reasoning).

## Open items at a glance

Everything not listed here is settled; each entry below carries its own status
and evidence. Grouped by whether anything can actually be done about them.

**Blocking, and actionable now**

- **E80** (DONE, validated live): the audio track is advertised as Opus and
  carries G.711 mu-law at 8 kHz. The sniff commits every session to mu-law
  ("20 of 20 packets"), coverage went from ~6% to ~100% of the timeline,
  `audio_decoder.discarded` is flat at zero, and 8 kHz AAC carries end to end
  (go2rtc `MPEG4-GENERIC/8000`, Frigate copy preset stores it).
- **E81** (DONE, validated live): the sparse-audio sniff livelock. Both cameras
  converged with exactly one bounded rebuild each and zero failed sessions,
  where the living room previously failed every session and published nothing.
- **E62 / E60** (DONE, validated live): the pacing and liveness changes have
  run through multi-hour soaks across many rotations with no publisher loss.
  The remaining audio-attributable residual (~14 drops per rotation at the
  handover) is E76's, not this entry's.
- **E92** (open, and its stated mechanism is WITHDRAWN) - the entry claimed
  video drops track audio STARVATION mid-session. That correlation held for the
  living room and does not hold for the doorbell, which on 2026-08-28 dropped
  613 to 1,959 frames per five minutes WHILE its audio ran normally at +3,900
  to +4,500. The handover trace also clears the rotation itself: 4 to 12 drops
  per rotation, small `worst_gap`, lead healthy at ~1,100 ms by `settled`, and
  audio advancing ~992 ms per ~1,040 ms of wall clock (about 99% coverage), so
  the timeline is not drifting either. What is actually observed is BIMODAL: a
  session either drops ~21 per rotation or sheds ~150 every 22 seconds (38%
  sustained), with no audio signal separating the two. E95 is the cause and is
  fixed; this entry stays open only until a soak confirms the drops fall with it.
- **E90** (open, minor, one point answered) - three follow-ons from E88. Point
  (b), "the pump only tops up on its timeout branch", was measured on 2026-08-28
  and is NOT causing drift: audio frames arrive every ~66 ms while the timeout
  is 50 ms, so the timeout branch does fire between frames and coverage sits at
  about 99%. It was the suspect for E92 and is cleared. The other two stand.
- **E87** (DONE) - the integration harness covers eleven modules and the
  mutation pass has been run: eleven mutations, ten caught, one real survivor
  found and closed (`AttributeBinarySensor.is_on`).
- **E89** (FIXED 2026-08-28, pending live confirmation) - the escape hatch was
  firing on transients that heal themselves: 21 restarts in a day, every
  observed failure run recovering after one to four failures against a threshold
  of five, and the restarts demonstrably not helping. The trigger is now a
  duration with no successful frame (`SESSION_WEDGE_TIMEOUT_S = 1800`) rather
  than a count of failures. CONFIRMED LIVE the same evening: the doorbell reached
  `consecutive_failures=15` and recovered on its own with zero restarts, where
  the old threshold of five would have killed the process three times. The
  `TypeError` this entry also named turned out to be a separate bug in
  `iceServers` parsing, now fixed.

- **E77** (open, minor) - four recorded limits of E76's handover
  instrumentation: it traces proactive rotations only, a record open at camera
  shutdown is dropped, drop timestamps are bounded per session, and the records
  are log-only (with the case for `/control/status` argued and declined).

- **E82** (open, minor): four things E81 deliberately did not do, including that
  the learned audio shape does not survive a container restart (costing one
  rotation of video-only publishing per restart) and that a camera sending fewer
  than 20 audio packets per session never publishes audio at all.

- **E75** (open, minor) - the stack watcher is now versioned at
  `ops/stack-watch.sh` but is untested shell that has already produced three
  false positives; its cron entry is not captured in the repo.

- **E76** (open) - the doorbell's residual drops are the audio path stalling at
  each session rotation: 14 per rotation with audio against 0.8 without,
  measured by A/B on the same camera. The handover window is now instrumented
  (`HANDOVER_TRACE`, off by default): one structured line per rotation carrying
  the audio timeline's advance, the muxer lead and every drop's timing. What is
  left is to run it on the live doorbell and read the records; the suspected
  mechanism stays unconfirmed until then, and no fix is to be attempted before.


- **E93** (FIXED 2026-08-28, deployed, soak in progress) - the codec sniff
  committed to Opus on a mu-law stream roughly 1% of sessions, and each one cost
  that camera its audio for the rest of the process. `OPUS_CONFIDENCE` was 0.75
  tested with `>=`, and the live accidental-success rate is 0.75 to 0.80, so the
  threshold sat ON the collision. Raised to 0.9. The comment's claim that 0.75
  sat "far above" the accidental rate was an assumption and is corrected in
  place with the measurement.

- **E94** (FIXED 2026-08-28, built, not yet deployed) - the audio-format rebuild
  budget was spent by the BOOTSTRAP, so it was always exhausted before a camera
  changed anything and the first anomalous observation was unconditionally
  fatal. It is now forgiven once the shape holds for
  `AUDIO_FORMAT_SETTLED_S`, giving up is a cool-down rather than permanent, and
  a recovery whose shape matches what the publisher was LAUNCHED with still
  rebuilds, because `_drop_audio` is latched one-way.

- **E95** (FIXED 2026-08-28, the root cause behind E92) - the video drops track the gap
  between the DECLARED frame rate and the delivered one, not audio. Observed
  live 2026-08-28 19:26: the living room was publishing `960x540@1fps` while
  shedding 1,216 frames, with `queue maxsize=4`; the doorbell was declared 15fps
  against a `measured_fps` of 58. The mechanism is arithmetic rather than
  speculative: janus_client sizes the video queue FROM the declared rate
  (`round(rate * VIDEO_QUEUE_TARGET_MS / 1000)`, clamped to
  `[VIDEO_QUEUE_MIN_FRAMES, VIDEO_QUEUE_MAX_FRAMES]`), so a declared 1fps gives
  the queue floor of 4 frames, and ffmpeg is launched with `-framerate 1` so it
  also READS at that rate. A low declared rate therefore shrinks the buffer and
  slows the consumer at the same time, and both compound.

  This fits everything E92 could not. It explains the BIMODAL behaviour (a
  session whose declared rate matches delivery drops little; one whose declared
  rate is far below it sheds continuously), it explains the living room being
  clean at 30fps and dropping at 6 and 1, and it explains the doorbell dropping
  while its audio ran perfectly, which is what withdrew E92's mechanism.

  Also seen in the same window, and probably the same cause:
  `ffmpeg has not read a frame from the video pipe in 5s -- the process is alive
  but is publishing nowhere; rebuilding the encoder`, twice on the living room.
  At `-framerate 1` a slow read is what the command line asked for.

  **DIAGNOSED AND FIXED, 2026-08-28.** The measurement was taken at the WRONG
  END OF THE QUEUE, and that closed the loop. `rate.note_frame` was called once
  per frame the streaming loop CONSUMED, while frames are dropped at the
  producer (`janus_client._pump_track`). Those two counts are equal only while
  nothing is dropping, so the rate was read from survivors exactly when it
  mattered, and the result feeds straight back: the declared rate sizes the
  queue and is handed to ffmpeg as `-framerate`, so a low reading makes ffmpeg
  read slowly, which fills the queue, which drops frames, which keeps the
  reading low. The bridge was measuring its own throttle and calling it the
  camera's frame rate.

  The live arithmetic that settled it: the living room was declared 1fps and
  publishing at that rate while dropping +455 and +952 frames per five-minute
  watcher sample, so roughly 3 frames a second were being discarded against
  about 1 delivered. The camera was sending about 4fps and the bridge reported
  1, with `measured_fps` stuck at `None`, and the camera's own alarm.com quality
  setting read "Highest (optimize for clarity)" throughout, so this was not a
  device setting.

  `janus_client` now records an arrival for every decoded frame BEFORE the queue
  can drop it, and the streaming loop drains those into the RateWatcher. Draining
  rather than taking one per consumed frame is what makes it correct: while
  dropping there are more arrivals than consumptions. Regression test drives a
  session that hands over 5fps while the camera sends 20 and requires the
  measurement to report 20; it reports 5 without the fix.

  Still open here: whether `measured_fps` of 58 on the doorbell is a genuine
  burst or the rotation contamination RateWatcher already documents, and whether
  `VIDEO_QUEUE_MIN_FRAMES = 4` is a sensible floor for a camera that swings
  between 1 and 30fps. Neither is addressed by this fix.

- **E96** (open; the CALL no longer depends on it as of 2026-08-29 -- the call
  uplink moved to the device-direct RTSP path, E55/E99. The live VIDEO view
  still uses Janus and still churns; that remains the open item.) - the
  doorbell degrades badly in the evening and the living room does not. Measured
  from four days of watcher history:

  ```
  hour   doorbell    living room (control)
  17:00    100%          100%
  18:00     83%          100%
  19:00     33%           91%
  20:00     50%          100%
  21:00      0%          100%
  ```

  Both cameras run in the SAME process, on the same account, through the same
  code and the same deploys, so a regression in that code takes both down. The
  living room was at its best during exactly the hours the doorbell collapsed,
  which rules the bridge out. On 25th-27th the doorbell held 99% all day and
  100% through those same evening hours, so this is specific to 2026-08-28 and
  to that camera's own path.

  The failure is always the same: Janus reports `webrtcup`, then no RTP arrives
  within `FIRST_FRAME_TIMEOUT_S`. The alarm.com API answers normally throughout
  and issues fresh stream credentials for every attempt, so the account and the
  web tier are fine; what is missing is media from the camera into its
  mountpoint. Runs last 10 to 20 minutes and then clear by themselves (103s,
  550s, 1080s and ~1080s again all measured), which is not the shape of a wedge.

  **This is the exact case SESSION_WEDGE_PEER_FRESH_S was added for**, and it
  validates it: restarting the process cannot reach the far end, and doing so
  would have taken the healthy living room down with it. Before that veto
  existed the old count-based hatch restarted the container 21 times in a day
  over precisely these runs.

  A CAUTION ABOUT THE MEASUREMENT, because it nearly produced the wrong answer:
  aggregating the four days by hour-of-day showed a 19:00-21:00 dip that looked
  like a stable time-of-day pattern and therefore environmental and long-
  standing. Splitting today from 25th-27th showed the prior days were 100% in
  those hours and the dip is entirely today. Same rows, opposite conclusion.
  Group by day before reading a time-of-day shape.

  Not investigated: whether the camera itself (Wi-Fi, power, thermal) or the
  TELUS relay is at fault. Nothing in this repo can distinguish them, and no
  change here would fix either.

- **E97** (open, camera-side, measured) - the living-room camera's NVR audio
  cuts out intermittently and the doorbell's does not, because the living-room
  camera STOPS SENDING audio packets for stretches while its video keeps
  flowing; the bridge fills the gaps with silence by design, and those
  zero-fill stretches are the cutouts. Evidence, all from one evening:

  * Recording segments during the cutouts read `mean_volume: -91.0 dB` with
    `max_volume: -91.0 dB` -- digital zero, which no real microphone produces
    (a live mic's quiet floor on these cameras reads around -70 dB). 3 of the
    40 most recent living-room segments were fully silent; 0 of the doorbell's.
  * The producer-side decoded-audio counter dips in exactly the five-minute
    windows containing the silent segments (+2,408 and +2,305 against a
    +3,400-3,650 norm), while the doorbell holds +3,900-4,500 -- close to the
    continuous-stream rate -- through the same windows. Frames that were sent
    but dropped are counted separately and are negligible, so the shortfall is
    frames never sent.
  * The whole-stack watcher's audio-not-advancing check fired 0 times in the
    same 3 hours: the gaps are shorter than its threshold, which is why this
    was invisible until the NVR made it audible.

  Consistent with the V522IR's known sparse-audio behaviour (the same property
  that caused E81's sniff livelock): the camera appears to suppress its audio
  send when the room is quiet, where the doorbell streams its porch noise floor
  continuously. Nothing bridge-side is dropping this audio; a fix, if one
  exists, is a camera/portal setting or an accepted limitation. Frigate's
  audio-based detection on this camera would also be blind during the gaps.

- **E98** (implemented bridge-side, live calibration pending) - the two cameras' recorded audio levels are
  wildly asymmetric: the living room peaks at -0.2 to -0.6 dB (at or brushing
  full scale, so loud content likely clips) with means of -15 to -39 dB, while
  the doorbell means sit at -47 to -72 dB with peaks near -14 dB at their
  loudest. The doorbell is NOT broken quiet: those levels are the correct
  decode of a quiet porch (the loud pre-E80 readings were decode garbage), and
  the level difference is real microphone/gain difference between the devices.
  Neither camera exposes a microphone-gain entity (`*_speaker_volume` is
  talkdown output, unrelated). The bridge now normalizes every published audio
  stream with an in-process zero-lookahead AGC applied to the decoded PCM in
  the sink (`config.AUDIO_NORMALIZE_TARGET` and friends; the constants carry
  the parameter reasoning). The first implementation appended dynaudnorm to
  the encoder's -af chain instead, and that took both cameras down around the
  clock (2026-08-28 ~22:30 UTC to 2026-08-29 ~11:45 UTC): dynaudnorm's gaussian
  lookahead delays the audio leg of the two-input mux, ffmpeg stops reading the
  video pipe, and the 5 s watchdog killed every session ~45 s after it
  published -- plus the session storm drove RSS into the 1 GiB container limit
  (four memcg OOM kills). Measured: 40+ session failures per 10 min with the
  filter, zero in 15 min without it. The -af chain must never carry a lookahead
  filter (`AUDIO_NORMALIZE_FILTER` now defaults to `anull` and is env-only);
  the AGC has no lookahead and cannot starve the mux. See troubleshooting. What remains is LIVE
  CALIBRATION: measure fresh NVR segments from both cameras after a real
  conversation at each, and move the max-gain cap if the doorbell is still too
  quiet or its porch hiss too loud. The alarm.com portal has not been checked
  for a per-camera microphone-volume setting; if one exists it composes with
  this rather than replacing it.

- **E107** (open, device, doorbell) - the doorbell's own publish leg keeps
  wedging briefly: in the 48 h to 2026-10-05 the HA stream-state sensor
  recorded ~79 doorbell drops of 30-60 s, plus a 17 min run (01:43 UTC) and an
  8 min run (15:05), every one the E103 signature (`webrtcup`, then `no frames
  received before timeout`), while the indoor camera had seven. The doorbell
  answers LAN pings at 160+ ms where every other host on the same LAN answers
  in under 10 ms, so the leading suspect is its Wi-Fi link rather than the
  cloud. Nothing in this repo can fix that; what is open is an access point
  or placement change on site. E103's `no_media` state and opt-in kick now
  cover the long runs, and the short blips stay below
  `NO_MEDIA_STATE_AFTER_S` by design. Recorded so the next "cameras are down"
  is not misread as the E104 edge block, which takes BOTH cameras down
  together.

- **E106** (DONE 2026-10-05, integration) - the push websocket's reconnect
  backs off while connects keep FAILING. `pyalarmdotcomajax` fetches a fresh
  websocket token over REST on every connect attempt, and the attempts seen
  failing live are TCP connect timeouts to `webskt.alarm.com:8443`
  (`ConnectionTimeoutError` in the HA log) arriving in streaks -- up to 16 an
  hour, one every ~36 s on the fixed 30 s delay -- each a request against the
  edge that rate-limits the whole account (E104). `controller.py` now tracks
  whether the attempt in flight ever reached RUNNING: a healthy socket the
  server drops on its routine ~6 min cycle reconnects on the fixed delay as
  before, and an attempt that never connected doubles the delay from the
  configured reconnect timeout up to `WEBSOCKET_RECONNECT_BACKOFF_MAX_SECONDS`
  (300), resetting on the first connect that succeeds. The arithmetic lives in
  `ws_backoff.py` so the stubbed test harness can pin it (`test_ws_backoff.py`)
  without importing `controller.py`. Why the connects time out is NOT known:
  from the same VM the host connects in 60 ms when probed, there is no IPv6
  in play (A record only, no v6 route), and the timeouts come in clusters with
  no local event beside them -- left open as the next question if the streaks
  continue after this lands.

- **E105** (DONE 2026-10-05, integration) - the coordinators honour a bridge
  `429`, and the settings poll is five times slower. A TELUS-backed poll that
  was answered 429 used to raise `UpdateFailed` and come straight back on its
  next interval -- the two 30 s event pollers alone were ~120 knocks an hour
  into the E104 block, each one re-logging the bridge in and re-opening the
  window. `RateLimitHoldMixin` (`bridge_client.py`) on the events, activity
  history, automation rules, camera settings and both arming-device
  coordinators keeps the last page and makes NO bridge call until the bridge's
  `Retry-After` (bounded, 2 h cap) has elapsed; the settings sweep stops at the
  first camera to answer 429 and keeps every camera's page. The page is stale,
  not wrong, so the entities stay available on it. The loopback-only camera
  list and stream health polls are not held. Separately,
  `CAMERA_SETTINGS_POLL_INTERVAL_SECONDS` went 120 -> 600: at 120 s it was the
  account's single largest consumer, seven website reads per camera per cycle
  (~420/h for two cameras, against ~240/h for both event polls together) for
  values that change on the order of weeks, and a write still refreshes itself
  at once. The event polls stay at 30 s on purpose: the doorbell-press and
  clip notifications ride them and the cloud already adds 32-64 s. Pinned in
  `tests/test_bridge_hold.py`; `test_arming_entities.py` updated for the new
  429 rule.

- **E104** (DONE 2026-10-05, bridge) - the process-wide cloud hold
  (`bridge/cloud_hold.py`). Observed live twice in three days (2026-10-03
  10:44-11:14 and 2026-10-05 16:25-16:59 UTC): the alarm.com edge refused the
  account for its stated `Retry-After: 1825` with **403 Forbidden on ordinary
  website GETs while the SSO login kept succeeding**. Every caller read the 403
  as an expired cookie -- re-login, retry, 403, `502` to Home Assistant -- and
  the next poll did it again: 168 re-login warnings in one hour, discovery
  failing every cycle, and **both camera streams down for the full window**
  (the video-source fetch each rotation needs was walking into the block on
  the 2/4/8/30 s schedule). The container was `Up` with zero restarts and the
  bridge connectivity sensor on throughout; HA's own session got the same 403
  (`KeepAlive.aspx`) two minutes earlier. Now every website call -- control
  routes via `_fetch_with_relogin`, the camera loops via
  `get_video_source_with_relogin`, and discovery -- goes through
  `cloud_hold.call_with_relogin`: a 401/403 a fresh login FIXES costs one
  login and one retry as before; one that SURVIVES the login holds every
  website call for `CLOUD_HOLD_INITIAL_S` (60 s), doubling per consecutive
  strike to `CLOUD_HOLD_MAX_S` (30 min), with the first call after expiry as
  the probe and a success resetting the strikes; an explicit edge wait (429,
  parsed `RateLimited`, rate-limited login) holds for the server's own number
  and a longer hold is never shortened. A refused call raises `CloudHeld` (a
  `RateLimited`), so held routes answer `429` + `Retry-After` instead of 502
  and the camera loops sleep the remaining wait through their existing
  `RateLimited` branch. Found along the way and fixed with it:
  `RATE_LIMIT_RETRY_MAX_S` was 30 min on the strength of ~101 s observed
  values, so the real 1825 s window was being clamped to 1802 s -- the bridge
  coming back 23 s before it was told, which is precisely the re-knock the
  clamp's own comment warns about. Now 2 h. Pinned in
  `camera-bridge/tests/test_cloud_hold.py`; the two existing route suites
  clear the hold per app since it outlives the app. What this does NOT do:
  hold the mobile (MWS) reads (they absorb a rate limit per read already) or
  defer the proactive session rotation during a hold (alarm.com kills the
  session server-side at ~180 s anyway, so a deferral would buy seconds).
  Also not done: explain WHY the edge blocks -- the steady-state request
  budget (E105) is the leading suspect, and whether the blocks stop after
  E104-E106 land is the thing to watch.

- **E103** (PARTLY DONE: the `no_media` state and the opt-in kick are
  shipped; the kick has run once against a live wedge and the camera recovered
  seven minutes later, one sample, cause not established; robustness) - a
  camera whose cloud publish leg wedges is
  unreachable by every recovery lever the bridge has, and the bridge retries
  forever with nothing escalating to the device. Observed live for 22+ hours
  on the doorbell (2026-08-30/31): every attempt completed ICE and got
  `webrtcup`, then not one media frame arrived before `FIRST_FRAME_TIMEOUT_S`
  (454 failed sessions in 24 h on the capped backoff). The run began
  mid-stream, on a session that had been publishing normally, moments after an
  audio-shape reconnect. Everything that isolates the fault points at the
  device's publisher rather than the network, cloud path, or bridge: the
  sibling camera streamed normally through the same process the whole time;
  the wedged camera kept serving its other cloud functions (settings reads and
  writes, activity clip uploads 40 minutes into the outage); and its reported
  WiFi signal held its usual level throughout. The E89 wedge hatch correctly
  vetoed a container restart (a peer was delivering, so no local resource was
  wedged) - that design is right and this entry does not reopen it. What is
  missing is escalation AT THE DEVICE once one camera's no-frame run gets
  long. Candidates, all unverified: cycle the camera's live-video-resolution
  setting through the settings API after N minutes without a frame (an encoder
  reconfigure may kick the publisher; needs a live test the next time this
  happens); a distinct `no_media` stream-health state, so an automation can
  page the operator instead of the sensor sitting on `reconnecting`
  indefinitely; and a device reboot command - pyalarmdotcomajax 0.5.13
  exposes none, so the web portal should be surveyed for one
  (`docs/web-portal-api-findings.md`). Until one exists the remedy is
  operator-side: confirm live view also fails in the vendor's own app (which
  isolates the device/cloud from this stack), then power-cycle the camera.

  Shipped on 2026-10-06, two of the three candidates. (1) The `no_media`
  state: `camera_session._is_no_media_failure` names the signature (the
  first-frame wait expiring AFTER `webrtcup`, or a running session going
  silent; ICE never coming up is excluded as the network's fault), and a
  failure run of it lasting `NO_MEDIA_STATE_AFTER_S` (300 s) while a peer is
  fresh sets `/control/status` `state: "no_media"`; the HA Stream state
  sensor carries the value, so an automation can page on it. The threshold
  sits above the doorbell's ordinary 30-60 s blips of the same signature
  (E107). (2) The kick, `device_kick.cycle_live_resolution`: write the live
  resolution to the first OTHER offered option, then back; through the cloud
  hold (E104); opt-in via `DEVICE_KICK_ENABLED` (default false), after
  `DEVICE_KICK_AFTER_S` (600 s) in `no_media`, once per
  `DEVICE_KICK_COOLDOWN_S` (1800 s); every attempt counted in
  `device_kicks` / `last_device_kick_at` / `last_device_kick`; an unsupported
  camera is never written; a failed restore is CRITICAL and names the value
  the camera was left at. Pinned in `tests/test_device_kick.py` and three
  `test_camera_session.py` cases (needs both a healthy peer AND the
  signature; exactly one kick per cooldown; off by default; a failing kick is
  recorded and never ends the loop), and `tests/test_stream_health_sensors.py`
  for the sensor vocabulary. (3) A device reboot command: still none in
  pyalarmdotcomajax 0.5.13 and not surveyed on the portal. The Stream state
  sensor carries `device_kicks`, `last_device_kick_at` and `last_device_kick`
  as attributes, so a phone automation can trigger on the outcome; on the
  reference deployment the kick is enabled and the three automations in
  `ha-config/camera_no_media.yaml` push the no_media entry, each kick result
  and the recovery to the phone under one notification tag.

  **First observed occurrence, with the kick enabled** (2026-10-08 UTC,
  doorbell camera, read back from HA's recorder history of the Stream state
  sensor and its kick attributes; the bridge log had rotated past it by the
  time it was examined, so the sensor record is the only bridge-side
  evidence): 04:02:28 `reconnecting`, 04:03:28 `streaming`, 04:04:58
  `reconnecting`, 04:10:28 `no_media`, kick sent 04:15:15
  (`last_device_kick = "ok: {'restored': '1440x1440', 'via': '960x960'}"`,
  `device_kicks = 1`, the restore write succeeded), 04:21:58 `reconnecting`,
  04:22:28 `streaming`. About 11.5 minutes in `no_media` in all; streaming
  again about seven minutes after the kick. The living-room camera streamed
  throughout, and has had no `no_media` episode since the watch went live.
  The phone got the no_media push at 04:11:28 and the kick-result push at
  04:15:28; the recovery push never fired, because its trigger waited for
  `no_media` to go straight to `streaming` while the sensor always passes
  through `reconnecting` first (fixed in the package: it now fires on leaving
  `no_media` for either state, and the copy in `ha-config/` is the tracked
  one).

  **Limit of the evidence, and what stays open:** one sample. The kick
  correlates with the recovery but does not prove it: this camera's ordinary
  reconnects of the same signature also resolve, usually within a minute, and
  this run had sat for five-plus minutes before the kick, so a recovery
  seven minutes later is consistent with the kick working and with the wedge
  clearing on its own. `DEVICE_KICK_ENABLED` therefore stays off by default,
  and the entry stays partly done until enough occurrences separate the two
  (the recovery push now records the kick count on each one). Also still
  open: whether a kick sent EARLIER (a shorter `DEVICE_KICK_AFTER_S`) shortens
  the outage, which one occurrence cannot say.

- **E102** (open, small, ops) - the stack watcher fails a sample whenever the
  talkdown `failed` counter is NONZERO, but that counter is cumulative per
  process: two failed speaks during a transient outage keep every later
  5-minute sample shouting FAIL until the next container restart resets
  /control/metrics, burying real failures in noise. The watcher already
  persists previous counters for its drop deltas; the talk check should use
  the same delta. Both copies need the change (ops/stack-watch.sh in the repo
  AND the VM copy cron runs).

- **E101** (fixed, integration, auto-recovery) - a clean VM reboot
  (2026-08-29 16:22-17:05 UTC) left DNS briefly unresolvable while HA started;
  the TELUS login's later steps failed and the integration raised
  AuthenticationFailed, locking itself into reauth -- the operator had to
  re-enter credentials that were never wrong, and restart HA. Fixed by
  reserving AuthenticationFailed for step 1's explicit credential rejection;
  flow-completion, SSO-handoff and identity-parse failures now raise
  UnexpectedResponse (-> ConfigEntryNotReady, auto-retried), because after
  step 1 accepts the password no later failure can mean the password is wrong.
  Same principle as the earlier 429 fix, applied to the whole flow. Covered by
  tests/test_login_error_classification.py (proven fail-first).

- **E100** (open, small, robustness) - when Alarm.com serves a gutted
  videoSource (observed live: `janusGatewayUrl: None` with `iceServers`
  present but empty, on one camera while its sibling streamed normally), the
  janus client passes the None straight into `websockets.connect` and fails
  with an `InvalidURI` traceback every 30 s retry. The retry/backoff loop
  handles it correctly -- the camera recovers when the cloud serves a real
  session again -- but the log should say "upstream served no gateway URL"
  once, not stack-trace on every attempt: check `janus_gateway_url` before
  connecting and raise the session-failed path with a plain message.

- **E99** (milestone 1 shipped; milestones 2-3 open) - the ring-to-phone call
  feature. The end state: doorbell ring with no notification response calls the
  operator's phone via VoIP.ms and bridges the call to the doorbell's two audio
  paths (both already G.711 mu-law at 8 kHz, so the splice is packet plumbing).

  **Milestone 1, done:** `bridge/sip_call.py`, a hand-rolled RFC 3261 subset
  (authenticated outbound INVITE, MD5 digest with and without qop, SDP
  offer/answer pinned to PCMU, symmetric RTP tone, BYE), exposed as
  `POST /control/call/test` and inert unless the env provides a sub-account.
  Hand-rolled rather than a SIP library for the same reason talkdown is: one
  scenario, and the libraries that cover it are alpha-quality or a C toolkit
  with its own event loop. The suite runs the whole state machine against a
  fake POP that verifies the digest INDEPENDENTLY (an early fake reused the
  module's own digest function and agreed with a deliberately broken HA2
  order; only an independent implementation can disagree). The suite also
  caught the tone frequency: 440 Hz is 8.8 cycles per 20 ms frame, so the
  repeated frame clicked 50 times a second; 450 Hz is exactly 9.

  **Milestone 2 REBUILT on the direct-LAN path (2026-08-29); uplink proven
  live (PLAY+RTP), full call not yet operator-validated.** The first M2 spliced the call's uplink from the cloud-Janus
  tap and the downlink through the talkdown PUT on the main event loop. Live it
  was laggy and dropped: the uplink gapped when the Janus video session churned
  (E96) and the downlink fragmented when that churn starved the PUT loop on the
  shared event loop. Root cause (operator's insight + the app's behaviour): the
  churn is the bridge relaying a same-LAN doorbell through alarm.com's cloud;
  the app never churns because it uses the device DIRECTLY on the LAN. So the
  call was rebuilt to the app's architecture: the uplink is now
  `rtsp_listen.RtspTunnelListen` (device-direct RTSP-in-HTTPS, E55), the codec
  read from the SDP; each direction is shaped for clarity by `call_shape`
  (low-latency in-process DSP); the call is offered per ADVERTISED capability
  (supports_upstream_audio && supports_downstream_audio), listen endpoint chosen
  by reachability order -- generic, no model/id/IP. The Janus decoded-audio tap
  added for the first M2 is removed. Built + unit-tested (test_rtsp_listen,
  test_call_bridge, both fail-first proven) + deployed; STILL NEEDS a live call
  with the operator present to confirm both directions are clear.

  LISTEN CLIENT STATUS (2026-08-29 passive live validation): the direct-LAN
  tunnel is proven END TO END against the real device -- GET/POST tunnel
  channels, HTTP Digest (with qop echoed) AND RTSP Digest, OPTIONS, DESCRIBE
  (SDP parsed), SETUP (200 + Session), PLAY (200), and live RTP: 65 frames /
  ~4 s of gap-free PCMU decoded to PCM in a passive probe. The earlier PLAY
  `454 Session Not Found` was NOT the base64 framing; it was the aggregate URL.
  This device answers DESCRIBE on `.../api/GetStream?streamID=0` but its media
  live under the server's Content-Base `.../api/GetStream/` (audio track control
  `stream=1`); SETUP and PLAY on the request URL 404/454, on the Content-Base
  they play. rtsp_listen now reads the Content-Base from DESCRIBE and resolves
  SETUP/PLAY against it (pure helpers `content_base_uri`/`track_uri`, unit-tested
  fail-first). The uplink path is therefore functional and cloud-independent.
  What remains is a full bridged CALL validated with the operator present (both
  directions clear, shaping tuned by ear) -- the standing rule forbids placing a
  live call without them.

  LIVE-VALIDATED 2026-08-29 with the operator: a full bridged call completed
  cleanly on the direct-LAN path -- SIP leg gap-free both directions (1753
  sent / 1750 received over 35 s), uplink continuous for the whole call (626
  RTSP frames ~= the full window), downlink PUT with zero stalls and zero
  renegotiations, always-on video streaming throughout, clean remote-BYE
  teardown. Operator verdict: much better, understandable both ways; audio
  fidelity is bounded by the path itself (G.711 mu-law at 8 kHz into a small
  doorbell speaker). The one defect found: ~5 s of one-way downlink delay,
  root-caused to the 4 s elastic buffer cap -- fill and drain both run at real
  time, so backlog from the call's slow first stretch persisted, pinned at the
  cap, for the whole call (the 38 kB of dropped downlink bytes were the same
  event: overflow shed at the cap). Fixed by making the cap the conversational
  latency ceiling it really is (CALL_DOWNLINK_BUFFER_MS 4000 -> 600; the 4 s
  evidence dated from the since-fixed session-storm period). The confirming call (same day) verified the
  delay fix and surfaced three more defects, fixed the same day: (1) the
  downlink turned choppy -- the PUT drain is one synchronous HTTP round trip
  per 40 ms chunk, and with the doorbell's encoder busy (it was pushing 27 fps
  against a declared 15 mid-call) each PUT took ~80 ms, halving the drain and
  shedding ~25 s of a 45 s call at the latency cap. Probed live: the device
  accepts PUT bodies of >=400 ms (HTTP 200) and its ack cost is per REQUEST
  (400 ms body ~9 ms, 40 ms bodies spiking to 86-209 ms), so the live drain
  now batches backlog into bodies up to CALL_MAX_PUT_MS (200 ms) and pacing
  runs on audio-sent rather than PUT count -- the drain keeps real time at any
  plausible RTT. (2) the uplink had its own 4-5 s delay: the unbounded mic
  buffer fills from the moment the RTSP listen opens, seconds before the phone
  answers, and the SIP leg drains at exactly real time, so the pre-answer
  backlog persisted -- the identical equal-rates bug the downlink had, fixed
  the identical way (CALL_UPLINK_BUFFER_MS 600 ms cap, shed oldest). (3) the
  visitor's voice was quiet with the porch background AMPLIFIED above it: an
  ungated AGC rides gain up during speech pauses until noise hits the target.
  The uplink shaper gained a noise gate (downward expander:
  CALL_UPLINK_GATE_THRESHOLD/ATTEN) and a hotter profile (highpass 250 Hz,
  target 0.7, max gain 16). The round-3 call then went badly in two NEW ways, both from
  the round-2 tuning itself: the gate threshold (0.015) had been guessed above
  this very quiet mic's conversational speech -- ambient floor measured live at
  frame-peak p50 0.0012 / p95 0.0027 / p99 0.0055 -- so the visitor was gated
  out entirely (only a direct tap on the mic crossed it); and fixing the
  uplink latency unmasked acoustic ECHO (speaker -> mic -> phone at ~0.6 s,
  loud enough to cross the gate) because no AEC exists anywhere in this path
  (the official app cancels phone-side). Fixes: gate recalibrated from the
  measured floor (0.006, atten 0.25) and downlink-priority echo ducking added
  (CALL_ECHO_DUCK_*): while the caller speaks, the uplink is attenuated hard
  with a hangover -- the intercom-standard half-duplex bias. uplink_ducked_ms
  surfaces in the call result. Round 4 was still bad ("as
  horrible as before" -- operator), despite the mechanics reporting clean:
  ducker active 10.4 s, drain kept up, played 40/40 s. Two structural moves
  followed instead of further knob tuning: (a) the called camera's always-on
  video pipeline is PAUSED for the call's duration
  (CALL_PAUSE_VIDEO_DURING_CALL, lifecycle.deactivate/reactivate) -- the
  device acks audio in 7-9 ms idle but 200 ms-3 s mid-call with the cloud
  session running, and those stalls are what the latency caps shed as chopped
  speech; the official app does not stream cloud video while it talks either.
  (b) the uplink DSP is simplified to a FIXED gain (min==max==4, Shaper's
  min_gain unclamped) with the gate off -- four rounds of AGC+gate tuning on
  this very quiet mic did net harm. And (c) CALL_DEBUG_CAPTURE_DIR records
  uplink-raw / uplink-sent / downlink-sent per call, so the next bad call is
  analyzed from audio, not described from memory. Round 5 ran with captures: every
  transport leg measured clean (SIP gap-free, drain at real time, video
  paused/resumed on cue) yet the operator still could not converse -- and the
  uplink_raw capture showed the mic feed NEAR-SILENT for the whole call
  (median peak -63 dB, a physical tap -26 dB) while a passive doorstep probe
  minutes later carried normal speech at healthy levels through the same
  path, with the speaker playing a test clip fine. Suppression by an unarmed
  silent PUT was ruled out; the remaining suspects are the ARMED full-duplex
  session and/or the paused video pipeline, and a staged 2x2 isolation test
  (`camera-bridge/tools/probe_call_mic_isolation.py` -- armed-PUT x
  video-paused, operator talking continuously at the door) is the next step. SHELVED HERE BY THE OPERATOR (2026-08-30):
  the feature is on the backburner; the call endpoints stay deployed but
  nothing dials without an explicit /control/call/doorbell. The doorbell
  Lovelace view is archived from the dashboard (storage backup kept;
  ha-config/doorbell-dashboard.yaml rebuilds it).

  STILL OPEN from this rebuild:
  (b) the downlink PUT still shares the main event loop, so heavy Janus video
  churn can still slow it; the fix is a dedicated thread for the call, deferred
  because it needs a talk.py cross-thread refactor and live validation.
  (c) audio-shaping defaults are unheard; tune live. (d) an off-LAN fallback:
  when direct-LAN listen is unreachable, fall back to the cloud path (the Janus
  tap removed here) so a bridge not on the device LAN still gets a call --
  genuinely generic, worth restoring as a fallback rather than the default.

  **Superseded M2 note (kept for the record):** `bridge/call_bridge.py` splices an answered call
  to the doorbell -- uplink from a decoded-audio tap on camera_session
  (re-encoded to mu-law), downlink from the SIP leg's RTP into a
  talk_fullduplex.LiveMulawSource that the existing chunked-PUT transport
  drains. `POST /control/call/doorbell` starts it, `/control/call/stop` ends
  it, and either half ending ends the other. Credentials and the gate are
  Home-Assistant-managed (options flow -> call_settings.json +
  /control/call/settings), the same two-channel pattern as the TELUS
  credentials; nothing is hardcoded in the bridge.

  **LIVE QUALITY, measured on a real call 2026-08-28.** The bridged call
  connects both ways (answered, `remote_bye`, talkdown streamed 19.7 s), but
  audio is laggy with dropouts, and the cause is a hardware/cloud ceiling, not
  the wiring. Two mechanisms, from the call's own result:
    * Downlink (voice -> speaker) shed ~50% (`downlink_dropped_bytes: 110560`
      = 13.8 s of a 27.8 s call). The doorbell speaker is an HTTP PUT endpoint
      and the sends are SEQUENTIAL with round trips up to 1518 ms; while a PUT
      is in flight the incoming voice backs up past the source buffer and is
      dropped. This is the endpoint's real-time throughput ceiling -- talkdown
      was built to play finite clips, which wait out each slow PUT because they
      hold the whole clip.
    * Uplink (mic -> caller) is cloud-relayed: the doorbell mic arrives over
      the same Janus path as the video, with the same inherent multi-second
      latency. There is no lower-latency path (E55: the doorbell runs no direct
      media server).
  **THE "PUT THROUGHPUT CEILING" WAS WRONG, corrected 2026-08-29.** The
  operator played a full music clip through the doorbell speaker with no lag or
  dropping days earlier, and a healthy-doorbell call dropped only 3% -- both
  prove the PUT path sustains smooth real-time audio. The 42% loss happened
  only on a call placed right after an E96 reconnect, when the doorbell's PUT
  acks slowed and the live drain fell 10 s behind real-time (visible in the
  progress log: "15s elapsed of 11s decoded"). The clip path never drops
  because it reads ahead from a 30 s lookahead and leads the doorbell's jitter
  buffer; the live path had only a 400 ms buffer and shed the overflow.

  The fix is an ELASTIC downlink buffer, not a transport change (pipelining is
  impossible: the PUT is per-chunk with no sequence numbers, so concurrent
  sends would reorder and corrupt). `CALL_DOWNLINK_BUFFER_MS = 4000` absorbs a
  slow-ack stretch instead of dropping it, at NO latency cost when the doorbell
  is healthy (the buffer drains as fast as it fills; depth bites only during a
  stall). `CALL_LIVE_PREBUFFER_MS = 300` covers ordinary ack jitter so playout
  does not gap; `CALL_UPLINK_TAP_FRAMES = 15` (~300 ms) smooths the cloud-relay
  mic so the caller stops hearing it cut out. Result: a smooth low-latency call
  when the doorbell is healthy (the music-clip experience), degrading to
  briefly-delayed-but-complete during an E96 stretch rather than fragmenting.
  The remaining variable is the doorbell's own health (E96), not the transport.

  **Milestone 3, open:** trigger from the ring automation (ring, no
  notification action within the loiter window, then call), plus the
  operator's decided constraints in the automation notes.

  Sub-account provisioning is deployment state (gitignored env), deliberately
  not recorded here.

**Recently shipped, listed only because the trap is expensive**

- **E104 / E105 / E106** (DONE 2026-10-05) - the alarm.com edge can refuse the
  whole account for ~30 min with **403s that look exactly like an expired
  session cookie** while the login keeps succeeding; re-login-and-retry on
  every poll is the one response that keeps the window open, and it took both
  camera streams down twice in three days with the container `Up` and the
  bridge sensor `on`. The bridge now holds every website call after a 403 that
  survives a fresh login (`cloud_hold.py`), the integration's TELUS-backed
  coordinators pause for a bridge `429`'s `Retry-After`, the settings poll is
  600 s, and the websocket reconnect backs off while connects keep failing.
  Watch whether the blocks stop; if not, the request budget (E105) is the next
  thing to cut. Restarting the bridge during a block only adds a login.

- **E79** (DONE) - Frigate records to the NAS vault pool over virtiofs. If the
  VM ever fails to start with `virtiofsd died unexpectedly`, the real error is in
  `/var/log/syslog`, not the empty virtiofsd log, and the fix is
  `--sandbox=chroot` in `/etc/libvirt/virtiofsd.opt`.


- **E85** (DONE) - E74 is corrected in place and marked superseded; its arena
  diagnosis is disproven by test. The watcher threshold is left at 93 percent
  deliberately until jemalloc's steady-state working set is known from a full
  overnight run.
- **E86** (open, minor, upstream) - aiortc's rate estimator accumulates
  RateBucket objects, ~3 MB per 330 s; invisible under jemalloc, worth an
  upstream check.

- **E84** (open, minor) - four limits of the E83 memory instrumentation, and
  the one live reading it cannot take.

**Blocked on hardware or a device this account does not have**

- **E13** (settled by capability query, not deferred): the VDB750 doorbell
  reports `SupportsWebRTC = false` / `StreamProtocol = 0` (RTSP) in the live MWS
  camera list (queried 2026-08-29). It is an RTSP+HTTP-PUT device; the Janus
  WebRTC the bridge consumes is alarm.com's CLOUD transcode of that RTSP,
  downstream only, not a doorbell WebRTC capability. So there is no webrtc/srtp
  upstream path on this hardware to validate, and the official app uses the
  same HTTP PUT talkback for this doorbell. The webrtc/srtp modules remain
  untestable here for want of a `SupportsWebRTC=true` device.
- **E20**: the audio-conditioning chain is tuned by ear for the VDB750's
  speaker; only matters once a different speaker exists.
- **E33** (one point): whether `shouldDisplayBatteryLevel` is reliable; needs a
  battery-powered camera.
- **E41** (two questions): whether panel-wide pairing off makes the per-device
  toggle inert, and the never-captured `bluetoothDisarmingSettings` write shape.

**Watch-only, no action by design**

- **E57**: `pyalarmdotcomajax` maintenance is paused upstream. Pinning is why
  nothing needs doing; the entry lists the triggers that would reopen it.
- **E107**: the doorbell's 30-60 s `no frames` blips (the E103 signature,
  ~79 in 48 h to 2026-10-05, none of them the E104 block, which takes BOTH
  cameras down together). Its LAN ping sits at 160+ ms, so the fix is at the
  house -- Wi-Fi placement or an access point -- not in this repo.

## High value
_Implemented since live testing:_ **E-COND audio conditioning (with per-request
toggle).** `config.TALK_AUDIO_FILTER` runs an ffmpeg chain on every clip before
encoding -- high-pass 250 Hz (drop bass the small speaker overloads on),
low-pass 2.8 kHz (warmth), gentle compression, hard limiter at 0.5/0.6 (no
clipping). Tuned by ear on the live VDB750; fixed the clipping/harshness raw
full-range music produced. Applied to EVERY clip on the doorbell (full-duplex)
path regardless of source/format/URL (runs inside the encode step). Per-request
control on `POST /control/cameras/{id}/talk`: `"conditioning": false` disables
it, or `"audio_filter": "<ffmpeg -af chain>"` overrides it; omitted = the
config default. **E19 is DONE**: the webrtc/srtp fallback path now
conditions the clip too, so every talk path gets identical treatment. It is
applied once in `talk._run_talk` via the shared `talk_fullduplex.condition_audio`
helper -- the full-duplex path still folds the filter into its own encode pass,
and neither unverified transport module had to be touched. Remaining: **E20**
the tuned chain is VDB750-speaker-specific -- another device may want different
cutoffs/levels, which is what the per-request `audio_filter` override is for.

- **E1 (DONE) Session renegotiation -- long-duration + resilient streaming.**
  The PutAudioStream URL, port and password are per-session, so a long clip or a
  network blip can outlive the session the talk opened, and the failure lands
  mid-clip with the audio half-played. Implemented as follows:
    * `talk_fullduplex.stream_talkdown` takes an optional async `renegotiate`
      callback and owns the audio byte offset. When a session dies mid-stream
      (5xx, a 401 the Digest middleware can't recover, a connection reset or a
      socket-read timeout) it calls the callback for fresh credentials, rebuilds
      the HTTP session, and **resumes from the exact byte offset** -- nothing lost,
      nothing repeated.
    * `talk.py` supplies that callback, keeping all MWS work on its side of the
      boundary: best-effort `EndLiveStream` on the dying session ->
      `GetDoorbellCameraList` for a fresh port/password/URL -> `StartFullDuplex`
      to re-arm. The final teardown follows whichever session ended up live.
    * Bounded by `config.TALK_MAX_RENEGOTIATIONS` (default 2), so a persistently
      broken session fails the request instead of looping.
    * The swap is dead air, not audio time: the safety deadline is extended by
      what the swap cost, and the pacing schedule is re-baselined so the stream
      continues at real time rather than bursting to "catch up" (which would
      overrun the doorbell's small buffer).
    * **Proactive variant** (`session_deadline`): when a session expiry is known,
      the transport swaps `config.TALK_RENEGOTIATE_MARGIN_S` early instead of
      waiting for a failed PUT. `talk.py` derives this from the `<cli>` `cstet`
      timestamp, but ONLY when that value is far enough in the future to be
      meaningful as an expiry -- its semantics are unconfirmed (in one capture it
      sat within seconds of the fetch, which would make it a creation time). If it
      isn't plausible the value is ignored and the reactive path stands. So today
      this is expected to be dormant; it activates by itself if `cstet` turns out
      to be a real expiry, with no code change.
    * Renegotiation is **opt-in**: without a callback, a mid-stream failure stays
      fatal exactly as before.
    * Covered by `camera-bridge/tests/test_talkdown_renegotiation.py` (run in CI):
      a local fake doorbell enforcing real Digest auth dies mid-clip, and the test
      asserts the bytes both sessions received, concatenated, are byte-identical
      to a clean single-pass encode -- plus the bound and the opt-in behaviour.
- **E2 (DONE) Cancel / stop an in-progress talk.**
  `POST /control/cameras/{id}/talk/stop` (and the Home Assistant service
  `telus_smarthome.stop_speaking`) ends the clip currently playing. Implemented as
  a stop *signal*, not task cancellation: `TalkManager` holds an `asyncio.Event`
  per in-flight talk, and the transport checks it at each chunk boundary, so the
  stream stops within one chunk (40 ms) while the MWS teardown still runs on its
  normal path; cancelling the task would have raced the `finally` that issues
  `EndLiveStream`. The in-flight `POST .../talk` returns normally with partial
  stats (`"stopped": true`, `played_seconds`) instead of raising, and stopping
  when nothing is playing is a `404`, not an error, so automations can fire it
  unconditionally. Covered by the test suite (a stop mid-clip must end promptly
  with partial stats and no exception).
- **E3 (DONE) HA integration + speak service.**
  `custom_components/telus_smarthome` v1.5.1 is live (rsynced to
  `~/homeassistant/config/...`, HA restarted); `telus_smarthome.speak` is
  registered and plays through the doorbell end to end via BOTH paths:
  `media_content_id` = a direct URL to a self-hosted TTS engine on the host
  (e.g. `http://127.0.0.1:<tts-port>/speak?text=...`), which bypasses HA TTS
  entirely, and `message` = HA cloud/Google TTS. Two things the
  live test required: (1) a one-char fix -- the handler re-validated `call.data`
  (a ReadOnlyDict) with voluptuous which coerces in place -> crash; fixed by
  validating `dict(call.data)`; (2) the `message` path needs HA's `internal_url`
  set (was None) so the bridge can fetch the absolute `/api/tts_proxy` URL -- set
  to `http://<ha-host-ip>:8123` via WS `config/core/update`. All speak audio gets
  the default conditioning.
  Wiring an actual door-greeting automation is a deployment choice left to the
  operator, not project work: the example `docs/talkdown-automation.yaml` is a
  placeholder, and an install that already runs door-greeting logic elsewhere
  should integrate talkdown there rather than as a standalone rule.

- **E56 (DONE) The doorbell button press is a first-class event.** A press
  carries `event_type_name="DoorbellButtonPress"` (a stable server-side enum)
  alongside `description="Button Push"`. Each doorbell gets an event entity with
  HA's `doorbell` device class, matching **the enum, never the description**:
  this account's activity feed renders text in the account's language and has
  produced both French and English for the same event kinds, so a text match is
  an automation that silently stops working with no error anywhere. Cameras are
  selected by chime support; dedup is the current page's id set (not a
  high-water mark: activity ids are opaque and one press yields
  timestamp-siblings); a restart cannot re-fire an old press; and it
  deliberately does not fire again when the clip is attached a poll later.

## Audio quality / transport
- **E4 (CLOSED -- not achievable on this device) L16 codec.**
  Investigated to a conclusion rather than left open. L16 (16-bit linear,
  `skyL16pay`/pt97) was first implemented and fired at the live ADC-VDB750 over
  `/api/PutAudioStream` in **both endiannesses** -- both came out as noise. The
  follow-up question was whether some *other* path on the device could carry it
  (the vendor's native `libvideo-streamer.so` contains an OSP backchannel:
  `OspBackchannelConnector`, `WebSocketOspConnectClient`, `StartBackchannelRequest`,
  `/ws/ospserver`). Probed directly with
  [`camera-bridge/tools/probe_device_audio.py`](../camera-bridge/tools/probe_device_audio.py):

  | Question | Result |
  | --- | --- |
  | Is there an OSP backchannel? | **No.** `/ws/ospserver` and `/osp` → `404`; `/api/StartBackchannel`, `/api/GetAudioStream`, `/api/PutAudioStreamL16`, `/api/capabilities`, `/api/GetDeviceInfo` all → `{"errorCode":-1,"msg":"Unknown request"}` |
  | Does the audio endpoint negotiate a codec? | **No.** `?codec=L16`, `?format=`, `?encoding=`, `?payloadType=97`, `X-Audio-Codec`, `Content-Type: audio/L16`: every variant answers exactly like the baseline. It consumes bytes as mu-law regardless. |
  | Could RTSP `ANNOUNCE`+`RECORD` carry RTP/L16? | **No -- there is no RTSP server.** The media port answers RTSP with `HTTP/1.0 400 Bad Request` from `lighttpd/1.4.61`. The `rtsp://` URLs in `<cli>` are consumed over HTTP; the device has no RTSP method set at all. |

  So this doorbell exposes exactly **one** inbound audio path, and that path is
  **mu-law only**. That also explains the app's own behaviour: the reference
  implementation selects `mulawenc` for this device, not `skyL16pay`, because
  `skyL16pay` targets the other device family (the OpenEye/SRTP cameras behind the
  webrtc/srtp transports; see E13). Nothing short of new device firmware changes
  this, so E4 is closed rather than deferred.

  **Consequence:** 8 kHz mu-law is the quality ceiling on this hardware, which is
  what makes the conditioning chain (E-COND) the real lever for how talkdown
  sounds, not the codec. If this project is ever pointed at a different model,
  run the probe tool first: a device that answers "Unknown request" for everything
  it does not implement, ignores codec selectors, and speaks HTTP on its media
  port has one path and one codec, exactly like this one.
- **E5 (DONE, with one limit) Streaming encode.**
  The PUT loop pulls from a chunk source rather than a fully-buffered clip.
  `ffmpeg` output drains through a bounded queue sized by
  `TALK_ENCODE_LOOKAHEAD_S`, so the encoder fills it, blocks, and is released at
  playback rate: encoded audio is bounded at roughly 240 KB regardless of clip
  length, and first audio leaves after one chunk decodes instead of after the
  whole file transcodes.

  **Pacing did not have to be compromised for it.** The schedule needs the chunk
  INDEX, never the total; only the safety deadline and the progress log needed a
  total, and both now derive from audio decoded so far. That keeps the property
  that matters -- the deadline advances only while audio actually flows, so a
  wedged connection still fires it.

  Chunks stay exactly 320 bytes (`readexactly`, not `read`). The doorbell has
  only ever been fed whole 40 ms frames and that is not a thing to discover in
  the field.

  A dead encoder raises a distinct error that is never renegotiated or
  reconnected -- a fresh doorbell session cannot fix ffmpeg. Resume needs no
  seek: the unsent chunk is retained and retried, which is the offset E1 already
  resumed from.

  **The limit:** `TALK_MEDIA_MAX_BYTES` is NOT removed. `talk._resolve_audio`
  still buffers the source and spools a temp file, because the webrtc/srtp
  fallback needs bytes. So this delivers latency and encoded-audio memory, not
  the source-side ceiling.

  `TALK_STREAMING_ENCODE` (default true) selects it; both modes run the same
  loop and both are tested for byte- and chunk-identity.

- **E6 (CLOSED: accepted as insurance; benefit unmeasured) Adaptive prebuffer.**
  Every talk reports `put_rtt_p50/p95/max/stdev_ms`, `late_chunks`,
  `max_late_ms` and `prebuffer_ms` **in the talk result**. They are deliberately
  not aggregated into `/control/metrics`, which counts outcomes rather than
  timings; a caller that wants a trend has to collect the per-talk numbers. `late_chunks` excludes the opening prebuffer fill, whose
  chunks go out back-to-back by design -- counting them reported every healthy
  talk as roughly a dozen chunks late.

  Adaptation is opt-in (`TALK_PREBUFFER_ADAPTIVE`, default **off**) and
  one-directional: it may only RAISE the lead to cover the worst observed round
  trip, capped by `TALK_PREBUFFER_MAX_MS`, which also caps how much audio sits
  buffered at the doorbell when a stop arrives.

  **Adaptation has now had its listening test and is audibly neutral on a LAN.**
  A 36-second spoken passage over the `direct` endpoint played 35.9 s of 35.9 s
  with no reconnects or renegotiations and no audible difference, which is the
  expected result rather than a disappointing one: adaptation raised the lead by
  23 ms in total (500 -> 516 -> 523), reacting to worst round trips of 258 and
  262 ms. Twenty-three milliseconds is far below anything a listener can hear, so
  hearing a difference would have meant something was wrong.

  That establishes it does not HURT. It cannot establish that it helps, and no
  test on this LAN can: the mechanism reacts to round trips that outrun the lead,
  and the worst here is roughly half of it. Its value is for a link bad enough
  that the fixed lead runs short -- talkdown from outside the house over the
  `cloud` relay, or Wi-Fi having a bad day.

  **The cost, if it ever does engage:** the lead is also how long a stop takes to
  be heard and how much audio is sitting in the doorbell when one arrives. At the
  `TALK_PREBUFFER_MAX_MS` ceiling that is two seconds. That is the trade, and it
  is why the ceiling exists.

  The 500 ms baseline stays as tuned by ear; adaptation never goes below it.
  `CHUNK_MS` is untouched for the same reason.

  First numbers off the live doorbell, on the LAN, which are what a future tuning
  starts from:

  | | |
  | --- | --- |
  | `put_rtt_p50_ms` | ~8 |
  | `put_rtt_p95_ms` | ~45 |
  | `put_rtt_max_ms` | ~200: the FIRST PUT, which pays connection setup and the Digest challenge |
  | `max_late_ms` | ~330 |

  Read together those say the 500 ms lead is about right rather than generous:
  the opening round trip eats roughly 200 ms of it, the worst slot slips ~330 ms,
  and the buffer still absorbs that with around 170 ms to spare. A shorter lead
  would start eating into real underruns on the very first chunks, which is the
  part a listening test would hear.

  `late_chunks` counting more than zero on a healthy short clip is expected for
  the same reason and is not a fault: it is the lead being spent on connection
  setup, which is what it is for.

- **E7 (DONE -- mostly a finding, not code) Digest `stale` and `nc` rollover.**
  From the installed aiohttp:

  - **`stale=true` is already handled.** The middleware does not inspect
    `stale`; any 401 carrying a Digest challenge replaces the cached challenge
    and retries once, with `nc` restarting at 1 -- which is the correct
    response. No code was needed. Pinned by a test whose fake doorbell rotates
    its nonce: seven stale challenges absorbed, clip intact, no reconnects and
    no renegotiations.
  - **`nc` rollover is unreachable.** The counter is `%08x` and increments once
    per request: 2^32 chunks is over five years of continuous audio on a single
    nonce. A guard would be dead code and was not written.

  The real gap was elsewhere. The middleware's retry loop is `range(2)`, so a
  SECOND consecutive 401, or a 401 with no `WWW-Authenticate`, surfaces raw --
  and used to burn a whole renegotiation. It now raises a distinct error handled
  by an in-place reconnect (E11).

- **E8 (CLOSED -- not achievable) Confirm playback from the ack.**
  The entry assumed the doorbell returns a per-chunk JSON ack whose body would
  reveal a silently-dropped-but-200 chunk. **It does not.** Probed live against
  the device with three chunks of mu-law silence:

      HTTP 200
      Content-Type: application/json
      Content-Length: 1
      body: b"\n"
      Server: lighttpd/1.4.61

  A single newline. The device advertises JSON and sends an empty line, on every
  chunk. There is no information in the body to parse, so the HTTP status is the
  only signal available and the transport already checks it. Nothing to build.

  If a silently-dropped chunk is ever suspected, the evidence will have to come
  from somewhere else -- the mobile API's stream logs, or listening.

- **E9 (DONE) Volume control -- it IS settable, via the WEBSITE API.**
  The mobile `<cli>`'s `<duav>` reports `SupportsVolumeAdjustment="false"`, which is
  misleading. `PUT /web/api/video/settings/audioSettings/{device-id}` carries
  `microphoneSpeakerSetting.callVolumeValue` with range 0-10 and
  `callVolumeFeatureSupport: 2` (supported), and a live browser session was observed
  sweeping it. That is the volume talkdown plays at, making it the other half of the
  audio-quality story alongside the conditioning chain (E-COND). The same call also
  exposes the doorbell's own chime (`isOutdoorToneOn`, `outdoorToneVolumeValue` 0-3).
  It uses the cookie + `ajaxrequestuniquekey` session the integration already holds.
  Caveat: settings PUTs send the WHOLE object back, so read-modify-write.
  Implemented in `camera-bridge/bridge/camera_settings.py` (read-modify-write,
  `supports_*` gating) behind `POST /control/cameras/{id}/settings/volume`, and
  surfaced as a speaker-volume number entity in Home Assistant. The chime half
  landed with it. See [control-api.md](control-api.md#camera-settings-diagnostics-siren-and-privacy)
  and [web-portal-api-findings.md](web-portal-api-findings.md).

## Robustness / networking
- **E10 (DONE) LAN vs WAN endpoint selection.**
  Candidates are probed concurrently with a bare TCP connect -- no TLS, no HTTP,
  nothing written, so the probe cannot disturb the audio path -- and consumed in
  preference order. On-LAN, `direct` wins in about a millisecond and nothing is
  added to the happy path; off-LAN, the stall is bounded by
  `TALK_ENDPOINT_PROBE_TIMEOUT_S` instead of an OS-length connect timeout taken
  while holding the per-camera lock.

  `TALK_PUT_ENDPOINT` forces one. An override the doorbell does not advertise
  warns and falls back to detection rather than silencing the doorbell, and if
  nothing is reachable the preferred URL is still returned so the PUT produces
  the real error rather than a synthetic one.

- **E11 (DONE) Reconnect on a dropped keep-alive.**
  Smaller than the entry assumed, for two reasons found by reading E1 first.

  **aiohttp already retries a dropped persistent connection once** for idempotent
  methods, and PUT qualifies -- so a single drop never reaches this transport.
  And E1's renegotiation is the wrong instrument for what does: it costs three
  MWS round trips of dead air and is bounded, so spending one on a dead SOCKET
  can leave none for a genuinely expired session.

  So a dropped connection now gets a new socket on the same credentials and
  retries the pending chunk, up to `TALK_MAX_RECONNECTS` consecutive attempts,
  reset by any successful chunk, before escalating to renegotiation. It
  deliberately does NOT extend the safety deadline the way a renegotiation does,
  so a link that blips forever still terminates.

  **Timeouts are excluded**, even though aiohttp models `ServerTimeoutError` as a
  subclass of `ClientConnectionError`. A timeout means the peer took the socket
  and did not answer, which is session-level, and each retry costs a full
  `sock_read` -- two of them would be ten seconds of silence mid-announcement
  before the renegotiation that was needed anyway. A test pins that
  classification and fails if the aiohttp hierarchy changes underneath it.

- **E12 (DONE) The device certificate is pinned.**
  `ssl=False` is gone from the device endpoints. The pin subclasses
  `aiohttp.Fingerprint`, which does exactly the right two things: passed as
  `ssl=`, the connector selects the UNVERIFIED context, so the self-signed
  certificate still completes a handshake, and then checks the SHA-256 once per
  connection.

  **It defaults to `warn`, not `enforce`,** because the fingerprint came from one
  device. In `warn` the talk connects exactly as before and a mismatch logs both
  fingerprints and the fix, so a firmware update or a replaced doorbell cannot
  silence talkdown. `enforce` refuses -- once, never retried, since every
  reconnect meets the same certificate. An unset or malformed pin degrades to
  learn-and-report: it prints the observed fingerprint to adopt and lets the talk
  through even under `enforce`.

  **The pin is never applied to the `cloud` endpoint.** That is a vendor relay on
  a different host with its own CA-signed certificate; pinning the doorbell's
  fingerprint there would break WAN talkdown outright.
  `TALK_TLS_PIN_ENDPOINT_KINDS` holds the kinds it does apply to, and the pin
  travels with a renegotiated session in case that lands on a different kind.

## Fallback path (other ADC cameras)
- **E13 (BLOCKED -- no hardware; the VDB750 is confirmed NOT such a device)**
  Validate the webrtc/srtp fallback. `talk_webrtc.py` / `talk_srtp.py` were built
  on the pre-capture hypothesis and have never run against a real
  webrtc/srtp-capable ADC camera. **This account has no such device.** Confirmed
  2026-08-29 by querying the live MWS camera list: the VDB750 doorbell reports
  `SupportsWebRTC=false` and `StreamProtocol=0` (RTSP), with upstream audio only
  over the HTTP PUT (`susa=true` via `uae/duae/luae/puae`). The Janus WebRTC the
  bridge consumes for this doorbell is alarm.com's cloud transcode of its RTSP,
  DOWNSTREAM ONLY -- decompiling the Android app (v5.10.7) confirmed the app's
  bidirectional-WebRTC audio-track path
  (`videostreamer/webrtc/.../BasePeerConnectionClientImpl`, a local mic
  `AudioTrack` gated by `supportsUpstreamAudio`) applies to `SupportsWebRTC=true`
  cameras, and for the VDB750 the app uses the identical HTTP PUT the bridge does.
  So no webrtc/srtp UPSTREAM path exists on this doorbell at all; this cannot be
  closed until a `SupportsWebRTC=true` device exists. The modules import cleanly
  in CI, the only assurance available without hardware.

  DO NOT re-investigate "a better talkback transport" for the VDB750: the answer
  is the HTTP PUT, the app agrees, and the device's own capability flags say so.
  The PUT endpoint's real-time throughput ceiling (E99) is a device limit, not a
  wiring choice.

  Two specific questions to settle when such a device is available:

  - **Trickle ICE.** `talk_webrtc.py` is non-trickle: candidates are embedded in
    the answer SDP, which the spec sanctions as the default. Whether the camera
    firmware *requires* per-candidate trickled `{"to": ..., "ice": {...}}` frames
    is unknown, and cannot be determined without a live probe.
  - **JSON framing.** The signalling JSON is emitted with Python's default
    separators; the vendor's own client uses Moshi's compact output. Whether the
    firmware cares about that whitespace difference is likewise unknown.

## Testing / observability
- **E14 (DONE) Repo unit tests** -- `camera-bridge/tests/` runs in CI
  and now covers the talkdown transport (renegotiation resume, the renegotiation
  bound, opt-in behaviour, stop, conditioning), the metrics counters, and the pure
  MWS parsers/helpers: `parse_doorbell_camera_list` (ids, credentials, all four
  endpoint variants, capability flags, plus the no-doorbell / logged-out / wrong-root
  edges), `_parse_action_ack` (that `ts` and `s` are BOTH required, and that a
  teardown parse never raises), `parse_live_video_stream` (the TokenStatus
  envelope, endpoint ordering, and the full webrtc/srtp/rtsp records, including
  that the SRTP keys are the base64-*decoded* bytes of `ack`/`vck`, not the
  base64 text, plus the xmlns-namespaced / logged-out / wrong-root edges),
  `decode_srtp_key` (a 30-byte round-trip, and `None` for a missing or
  non-base64 value), `config.parse_retry_after` (delta-seconds, HTTP-date,
  past date, missing, garbage) and `TalkManager._session_deadline` (an implausible
  `cstet` is ignored). These were previously verified by hand against a live
  capture, so nothing re-checked them when a field name changed. Still open: the
  webrtc/srtp transports have no tests, which is really E13 -- they cannot be
  meaningfully tested without a device that uses them.
- **E15 (DONE) Metrics** -- `GET /control/metrics` exposes talkdown
  attempts, succeeded/stopped/failed/rejected, failures by stable reason label,
  endpoint and codec usage, renegotiations, seconds of audio actually played,
  and the last 20 talks; `/control/status` carries a summary. In-memory (reset
  on restart) and recorded at the single `speak` entry point, so every
  transport and failure mode is counted identically. Reason labels never carry
  exception text (a URL or token could ride along) -- asserted by a test.
  **E15b (DONE)**: surfaced as two diagnostic HA sensors
  (`talkdown_status`, `talkdown_failures`) on their own slow-polling
  coordinator, so a failing talkdown can drive a notification instead of
  needing a curl. A bridge that is down or predates the endpoint leaves them
  unavailable rather than breaking sensor setup. See
  `docs/ha-integration.md` for the entities and an example automation.

- **E63 (DONE) The integration has a test harness.** `tests/` at the repository
  root holds two suites, run by a dedicated `integration-tests` CI job on every
  PR under the same conventions as the bridge's: plain scripts ending in
  `RESULT: PASS` / `RESULT: FAIL (n)`, no network, no account, no hardware, and
  a new `test_*.py` picked up by the glob with no list to update. The job
  installs **aiohttp and nothing else**.

  **Home Assistant is stubbed, not installed.** `tests/ha_stubs.py` registers a
  small `homeassistant.*` package tree in `sys.modules` before the integration
  is imported, so the REAL integration modules load and the REAL derivations run
  against stand-in base classes. Installing Home Assistant was evaluated
  properly rather than assumed impossible: it *does* resolve on the interpreter
  CI pins, but pulls in the order of a hundred transitive packages (grpcio,
  boto3, pillow, cryptography, bleak/dbus-fast, zeroconf, SQLAlchemy, and PyRIC,
  which has no wheel and builds from source) to supply a handful of base
  classes, and exercising entity behaviour for real on top of it needs
  `pytest-homeassistant-custom-component`, which pins one exact Home Assistant
  version and would put this repo on a version-lock treadmill, with a second
  test framework alongside the bridge's plain scripts. The logic worth pinning
  is pure and needs none of it. `pyalarmdotcomajax` is stubbed for the three
  attributes `const.py` reads; `controller.py` is stubbed wholesale, because
  importing it applies the websocket monkeypatch and needs the real library:
  work the existing `check_patch.py` CI job already does.

  **Covered.** `test_stream_health_sensors.py` pins E61's audio-state
  derivation, including all four cases that must never fabricate a `stalled`:
  an unanswered poll (both guards: the `last_poll_ok` flag and the identical
  payload object the coordinator hands back), a failed poll, a counter reset on
  a bridge restart, and the first poll of an entity's life, plus the per-poll
  delta, the `not_consuming` reset of the remembered count, the unknown state
  for a bridge too old to report `audio_consumed`, ENUM-options containment, the
  stream-state and dropped-frames mappings, and the coordinator's two failure
  flags. `test_event_entities.py` pins the doorbell matcher on the untranslated
  `event_type_name` enum (a French-described press fires; a "Button Push"
  *description* with a different enum does not), the per-page id-set dedup
  (restart safety, timestamp siblings, oldest-first firing with one state write
  per press, an empty page neither becoming nor clearing the baseline),
  `_classify` across both locales, `_event_time` ordering across TELUS's two
  timestamp formats, `_is_doorbell`, and `TelusActivityEvent`'s single extra
  fire when a clip attaches late.

  **Every invariant was proven falsifiable.** 39 deliberate source mutations
  were applied one at a time, the suite run, and the mutation reverted: 38 were
  caught. The one survivor is a *finding about the source, not a hole in the
  test*: dropping `_classify`'s French `"personne"` needle changes nothing,
  because `"personne"` contains `"person"` and the English needle already
  matches it. The French **vehicle** needles are genuinely load-bearing
  (`"véhicule"` shares no substring with `"vehicle"`) and their removal is
  caught. That asymmetry is now recorded in the test's own docstring so it is
  not re-derived.

  **Not covered: see E66**, which carries the list rather than leaving it
  implied.

- **E66 (open) The integration test harness covers two modules; the rest of the
  integration is still unpinned.** E63 built the harness and used it on the two
  places where a silent regression was already known to be expensive. It
  deliberately stopped there rather than chasing coverage, so this entry records
  what is NOT covered instead of leaving it implied:

  - `config_flow.py`: the config/reconfigure/reauth flows, including the OTP
    branch. Pure-ish and testable with the same stub tree; the payoff is that a
    broken flow is discovered at setup time by a user, not by CI.
  - `speak_service.py`, `bypass_service.py`, `unpair_service.py`: the service
    handlers. Their schemas and their error mapping (E51's 413/504/400 wording,
    for one) are exactly the kind of thing that reads correct and is not.
  - `switch.py`, `select.py`, `number.py`, `button.py`: the settings-backed
    platforms. `camera_settings_coordinator.py`'s wire shape *is* covered on the
    bridge side (`test_camera_settings.py`); what is unpinned here is the HA-side
    capability gating and option-list handling.
  - `binary_sensor.py`: including the bridge-connectivity entity that shares
    `StreamHealthCoordinator` with the covered sensors.
  - `clip_view.py` is now covered (`tests/test_clip_links.py`): the HMAC's
    id-and-expiry binding, the 401/410 ordering, the log-injection and
    non-ASCII-signature guards, and the bridge-failure status mapping, each
    proven to fail against a deliberately broken view. What remains unpinned
    there is only the aiohttp routing glue (the view registration itself).
  - `arming_devices_coordinator.py`: again, covered bridge-side
    (`test_arming_devices.py`), not HA-side.
  - `__init__.py`: every setup, unload and reload path.

  There is also a **structural** gap that no amount of new suites in this shape
  closes, and it should not be papered over: the stubs assert nothing about Home
  Assistant's real API, so a breaking upstream change to `CoordinatorEntity` or
  `EventEntity` leaves every suite green. Closing that means installing Home
  Assistant, which E63 rejected on cost; the realistic middle ground, if it ever
  matters, is a separate, occasional CI job that installs Home Assistant and only
  *imports* the integration's platform modules against the real base classes,
  the way `bridge-import-smoke` does for the bridge. Not done, and not obviously
  worth it yet.

## Dependency-driven (from the aiortc 1.15 / aiohttp 3.14 upgrade)
- **E21 (DONE) Replaced the hand-rolled HTTP Digest with aiohttp's `DigestAuthMiddleware`.**
  Implemented and verified against the live doorbell (150 chunks streamed clean);
  ~50 lines of bespoke RFC 2617 code (`_Digest`, `_parse_challenge`, `_h`) deleted.
  Original rationale:
  `talk_fullduplex.py` implements RFC 2617 Digest (MD5, `qop=auth`, `nc` counter)
  by hand because aiohttp had no client-side Digest support when it was written.
  aiohttp **3.12** added `DigestAuthMiddleware`, and **3.14** fixed it to sign the
  *encoded* request-target (which matters for the relay `uae` endpoint, whose URL
  carries a `?camera_id=` token). The bridge now runs aiohttp 3.14.x, so this is
  actionable: swapping to the middleware deletes bespoke auth code (`_Digest`,
  `_parse_challenge`, `_h`) and inherits upstream fixes. Verify the doorbell still
  accepts the middleware's header ordering/format before committing to it.
- **E22 (DONE) Bumped PyAV to 17.1.0 for the per-frame memory-leak fix.**
  Note aiortc pins `av<18.0.0`, so 17.1.0 is the ceiling -- an automated 18.x bump
  would conflict. Verify RSS over time. Original rationale: aiortc 1.15.0
  relaxed its PyAV pin to allow 17.x specifically because it *"fixes a per-frame
  memory leak"*. This bridge is the worst case for that leak: every frame of every
  camera crosses into Python (`janus_client` `track.recv` -> `ffmpeg_sink`
  `frame.to_ndarray`), 24/7, in a container capped at 768 MB. `requirements.txt`
  still pins `av==16.1.0`, so the fix is NOT yet in effect. Bump and watch RSS over
  a few days (baseline after the 1.15/3.14 upgrade: ~316 MiB with 2 cameras).
- **E23 (DECIDED: keep, gated) `aiortc.sdp.candidate_from_sdp` is a private-API dependency.**
  There is no public equivalent (confirmed: it is not in aiortc's `__all__`), and
  vendoring an SDP candidate parser would trade a checked dependency for silently
  drifting duplicate logic. Kept, with aiortc marked review-required in Renovate and
  a CI job that imports every bridge module. Re-check on each aiortc bump. Detail:
  `janus_client.py` and `talk_webrtc.py` import it from `aiortc.sdp`, an internal
  module with no stability guarantee, and it sits in the live video path.

  Re-verified against the DEPLOYED bridge on : aiortc 1.15.0,
  `aiortc.sdp.candidate_from_sdp` present with signature `(sdp: str) ->
  RTCIceCandidate`, and still absent from `aiortc.__all__` -- so it remains a
  private API and the decision to keep it stands. Every aiortc bump must re-run
  that three-line check (import it, confirm the signature, confirm it is still
  not in `__all__`).
  Options: keep it and rely on the CI/dep-review gate, or vendor a small local SDP
  candidate parser so the video path stops depending on library internals.

- **E57 (open, watch-only) `pyalarmdotcomajax` maintenance is paused.** The
  upstream maintainer has moved and no longer has an Alarm.com system to test
  against, on an indefinite timeline. The repository is not archived, and its
  last functional commit predates the notice by over a year, so the library has
  been dormant for as long as this project has been active, and the notice
  formalises the status quo.

  No action is required, and pinning is why: `manifest.json` pins an exact
  version, Renovate never auto-merges it, and CI imports all three monkeypatches
  and asserts the private instance attributes the replaced `async_login` depends
  on. Triggers that should reopen this: the pinned version failing on a Python
  version the runtime moves to, a security advisory, or wanting to change code
  currently reached by monkeypatch. **Vendoring, not forking, is the likely
  answer** when one fires: three internals are already monkeypatched precisely
  because they cannot be changed in place, and vendoring would turn those into
  ordinary edits and delete the guard machinery with them.

## Minor
- **E16 (DONE) Cache `GetDoorbellCameraList`** -- a result is reused
  across talks for `config.TALK_DOORBELL_CACHE_TTL_S` (60 s), saving that round
  trip on back-to-back talks (measured: 2937 ms cold vs 2597 ms warm for the same
  clip). Safe because the session is not per-talk in practice -- the doorbell was
  observed serving the same session port for 12+ hours across many talks -- and
  because a stale hit self-corrects: the first PUT fails, which is exactly what
  renegotiation (E1) handles, costing one chunk rather than an error. The cache is
  dropped on any talk failure and at the start of every renegotiation, so a
  session known to be bad is never handed out. `0` disables it.
  *(Note: the original idea was to bound this by `cstet`, but that value proved
  unreliable as an expiry -- see E1 -- so a plain TTL plus self-correction is used
  instead.)*
- **E17 (DONE) `EndFullDuplex` as a teardown fallback** --
  `mws_client.end_full_duplex` exists and is called only when `EndLiveStream`
  fails. Leaving the mic-input path armed is the one teardown failure with a
  lasting effect, so it is worth a second attempt by a different route. It is
  deliberately NOT sent on the happy path: doubling every teardown would double
  MWS traffic against a rate-limited account for no observed benefit.
- **E18 (DONE) `CHUNK_MS` / `PREBUFFER_MS` are documented tunables** --
  moved to `config.TALK_CHUNK_MS` / `config.TALK_PREBUFFER_MS` (the transport
  references them under the short names) and documented in
  [configuration.md](configuration.md), including the trade-off each one makes.

- **E65 (CLOSED: not achievable with this stack) The MediaMTX publish password
  is visible in process arguments.** The RTSP publish URL, credentials
  included, is passed on ffmpeg's command line, so it appears in `docker top`
  and in any process listing that can read the container process's
  `/proc/<pid>/cmdline`.

  This entry originally claimed "ffmpeg accepts the same values through the
  environment or a config file". **Checked against the deployed ffmpeg
  (7.1.5) and disproven**: the RTSP muxer's complete AVOption list is
  `rtpflags`, `rtsp_transport`, `min_port`, `max_port`, `buffer_size`,
  `pkt_size` -- no credential option of any kind -- and ffmpeg has no
  environment or config-file mechanism for RTSP credentials either; for RTSP
  output the URL is the only carrier. The alternatives each add a moving part
  disproportionate to the exposure: a different publish protocol into
  MediaMTX moves the same credential onto a different argv (RTMP URL, SRT
  streamid), and an authenticating local proxy is a new process on a live
  video path.

  The exposure it would have removed is small and stated: the password gates
  loopback RTSP on a `--network host` host, and reading the argv already
  requires being root or the container uid -- close to the set of actors the
  secret file (`0600`) admits anyway. Log lines redact the URL
  (`_redact_url`); the argv is the one place it stays in the clear, accepted.
  Reopen only if ffmpeg grows a credential option for RTSP output or MediaMTX
  grows a local-socket publish path.

## Adjacent alarm.com surfaces (from the web-portal survey)
These are not talkdown, but they are gaps found while working the same account
and there is no better backlog to put them in. Full detail in
[web-portal-api-findings.md](web-portal-api-findings.md).

- **E24 (DONE) Bluetooth arming devices are now a switch per phone.**
  `isDisarmingEnabledForDevice` decides whether a paired phone walking up to the
  house disarms the alarm, so it is treated with siren-level caution: the write
  is reachable only from an explicit toggle, the entity exists only where the
  record reports the capability, and an unknown device id is a 404 rather than a
  blind PUT at an arbitrary device.

  Same wire conventions as the camera settings -- JSON:API read, flat write, type
  pluralized on write, whole-document read-modify-write. Discovery is the device
  catalog, filtered on the resource TYPE rather than the localized category name.

  No `*FeatureSupport` enum exists for this field and the portal renders its
  switch unconditionally, so the capability flag had to be assembled: `canBeSaved`
  (which does discriminate -- true on these records, false on plain mobile devices
  in the same catalog), `canAccessWebSettings`, and the field being a real bool.
  That reasoning is asserted in a test, so if the flag stops discriminating it
  fails loudly instead of quietly offering a control that does nothing.

  Verified live on the real account: both paired phones listed, a toggle
  round-tripped and persisted, a non-bool refused with 400 and an unknown id with
  404. Run while the panel was disarmed.

  The entity is named for the consequence rather than the mechanism -- "Disarm
  alarm on arrival" on a device named after the phone, not "Bluetooth disarming"
  -- and polls every 15 minutes, since nothing but a person can change it.

  Implementing it also turned up a latent bug in `camera_settings._flatten`: it
  accepted a relationship whose `type` was merely present rather than a string,
  which walked into an AttributeError mid-parse. These documents contain such
  members. Fixed and pinned in both modules.

- **E25 (DONE) Selective sensor bypass -- and a correction: it is not
  an arm-time option at all.**
  `POST /web/api/devices/partitions/{id}/bypassSensors` is a **standalone
  partition action, not an arm-time option**:

      {"bypass": "<unit>-1|<unit>-3|<unit>-2", "unbypass": ""}

  Pipe-separated device ids, applied immediately, independent of any arm call --
  the same thing the TELUS app's own bypass screen does. It cannot be folded into
  `CONF_FORCE_BYPASS`, which is a boolean passed to an arm call.

  So it ships as a MANUAL service (`telus_smarthome.bypass_sensors`) and is
  deliberately not wired into the arm path. The hazard the original entry named --
  two *automatic* bypass paths racing over one partition -- is real and is still
  avoided; a manual action alongside an automatic flag is a different thing.
  `alarm_control_panel.py` is untouched.

  Real gates found on the partition object and honoured by the bridge, which
  pre-reads it before writing:

  | Flag | On this panel | Meaning |
  | --- | --- | --- |
  | `canBypassSensorWhenArmed` | **false** | a sensor cannot be bypassed while the partition is armed |
  | `hasPermissionToChangeState` | true | this account may change partition state at all |
  | `limitSelectiveBypassSensors` | false | no cap on how many may be selectively bypassed |
  | `hasOpenBypassableSensors` | false | nothing currently open and bypassable |
  | `hideForceBypass` | true | the portal hides the force-bypass control on this dealer's config |

  The response does NOT report which sensors ended up bypassed, so the service
  triggers a coordinator refresh rather than claiming a result it was not told.

  A bypassed zone does not trigger the alarm until it is un-bypassed. That is
  stated in the service description, not just here.

  **The write takes about ten seconds to reach the sensor.** A bypass returns 200
  immediately, but `isBypassed` is still false at 5s and true at 10s; an
  un-bypass takes the same. The service's post-write refresh is delayed past that
  window, because refreshing the instant the POST returns reads the OLD state and
  paints the sensors unchanged -- which looks exactly like a call that did
  nothing.

  Also worth recording, because it looks like a contradiction: the vendor's own
  portal does NOT offer its bypass button while the panel is disarmed. Its gate
  is `extendedArmingOptions`, which is `{"Disarmed": [], "ArmedStay": [2,1,4],
  "ArmedAway": [2,1,4]}` on this panel -- no options at all in the disarmed
  state. **The endpoint honours the call anyway**, verified above, so that is a
  UI choice rather than an API constraint. Sensors additionally carry
  `supportsBypass` / `supportsImmediateBypass`; on this account the three door,
  patio and motion sensors have both and the life-safety devices (smoke, CO,
  glass-break) do not.

## Camera-settings gaps found while implementing E9

Logged rather than fixed, per the project's rule that a gap noticed is a gap
recorded. None of these block the shipped feature; all were found while grounding
`camera_settings.py` against the browser capture.

- **E26 (DONE) The doorbell's chime now has Home Assistant entities.**
  Two switches per capable camera -- indoor chime and outdoor tone -- over the
  existing `POST .../settings/chime` route, which previously had no caller at all
  (`const.py` defined the URL template and nothing used it). They are separately
  gated: the reference doorbell reports `indoorChimeEnableDisableFeatureSupport`
  and `outdoorToneEnableDisableFeatureSupport` both 2, but
  `outdoorToneVolumeFeatureSupport` 0 -- it can mute the outdoor tone, not level
  it -- so no volume control is offered there.

- **E27 (DONE) The wired chime type is now readable AND writable, with names.**
  This is the style of physical chime the doorbell is wired to, NOT a ringtone.
  The open question was labels: the website API publishes `chimeTypeOptions` as
  bare integers and the web portal names them with a client-side enum, so the
  the option values are not published anywhere in the website payload.

  It did not. The **mobile** API hands the names over directly:
  `GetCurrentDoorbellSettings` returns `<sctoi>` elements carrying `n` (name),
  `v` (value) and `sdict` (`shouldDisableIndoorChimeToggle`). Live from the
  reference doorbell: `-1` = None (`sdict` true), `0` = Mechanical, `1` = Digital.
  The bridge merges these into the audio section as `chime_type_choices` and Home
  Assistant renders a select from them, falling back to the bare integers if that
  secondary read fails -- losing the labels must not lose the control.

  The `sdict` flag is the part worth keeping: it is the server saying which value
  means "no wired chime" on THIS model, which is what lets the indoor-chime
  switch go unavailable in that state without any client hardcoding `-1`.
  See [mobile-api-findings.md](mobile-api-findings.md).

- **E28 (DONE) The option-list ambiguity is resolved: the two clip
  fields genuinely disagree, in the same document, on the same device.**
  Settled from the portal's own dropdown builders, which say which half they
  write; its JavaScript is a public static asset (the method in E40). No captured
  PUT and no second camera model was needed.

      clipResolutionOptions  {"1": "480x480", "4": "960x960", "7": "1440x1440"}
          -> the VALUE is settable. The builder pushes `{value: options[key]}`;
             the key is a resolution-class enum used only to prefix a label.

      clipQualityOptions     {"5": 3, "3": 2, "2": 1}
          -> the KEY is settable. The builder writes `props.id`, which is the
             key; the value is a quality enum used only to look up a label.

  This was genuinely not guessable from the payload: `selectedClipQuality` is
  `"3"`, which is simultaneously a key of that object and a value of it.

  `_Field.options_half` now states it per field from evidence, and the four
  object-valued option lists in this project are all declared: clip resolution
  (values), clip quality (keys), camera-triggered clip duration (keys), live
  resolution (keys). The concrete bug this closes is that a resolution-class enum
  key -- `"7"` -- used to be accepted as a clip resolution.

  A field that declares nothing keeps the old permissive behaviour so an unknown
  camera model is not broken by the tightening, but it now logs a warning naming
  the field, because resolving one needs no device at all.

  Labels are published only where the other half is a real string, which rules
  both clip fields out: their other halves are opaque enum numbers that would
  label worse than the value itself.

- **E29 (DONE) The siren and privacy routes now answer 429 with
  Retry-After.**
  They used to return a bare 502 for an edge rate-limit, which tells a caller
  "upstream broke" when the truth is "come back in N seconds" -- so Home Assistant
  retried on its own schedule and walked straight back into the limiter. Both
  routes' reads and writes now go through one `_rate_limited` helper that emits
  the server's own wait as an integer `Retry-After` header plus a
  `{"error": "rate_limited", "retry_after": N}` body. The same helper now also
  fronts the camera settings and diagnostics reads and both halves of every
  settings write (the pre-read and the setter call); the settings bundle's two
  MWS-backed sections (siren/privacy) remain the deliberate exception: a
  rate-limited MWS read is absorbed internally and those sections degrade to
  `null` rather than failing the response.

- **E30 (DONE) `max_exposure` and `camera_triggered_clip_duration`
  are routed.**
  Both were parsed by `camera_settings` but missing from the control server's
  field specs, so neither was reachable. Both are still unsupported on the two
  cameras here (`canSetMaxExposure` false, and the camera-triggered duration
  offers a single value), so neither has been exercised against hardware -- the
  support gate refuses them on these devices, which is the correct outcome and
  the only one testable here.

- **E31 (DONE) Signal strength is labelled per camera, from what the
  bridge says it read.**
  The unit was the blocker: `normalizedSignalStrength` is a 0-100 score, while the
  fallback spellings kept for untested models may be real dBm, and Home Assistant
  records long-term statistics against the declared unit -- so a wrong label
  poisons history rather than just the display.

  Rather than "confirm every model reports the score", which is not a thing anyone
  can confirm, the bridge now reports `signal_strength_key` alongside the value
  and the sensor labels a percentage ONLY when that key is
  `normalizedSignalStrength`. Anything else stays unlabelled, exactly as before.
  The known case gets its unit; the unknown ones keep the safe answer.

## Gaps found while implementing E26/E27

`GetCurrentDoorbellSettings` returns a large document and this project reads two
members of it. The rest is recorded in
[mobile-api-findings.md](mobile-api-findings.md); the entries below are the parts
that look worth having.

- **E32 (DONE) The status LED is a Home Assistant switch.**
  It lives on the WEBSITE API, in `generalSettings` -> `ledLightSetting`:
  `canTurnOnOffLedLight`, `canChangeLedBrightness`, `canChangeLedColor`,
  `isLedLightEnabled`, `ledBrightness`, `ledLightColor`. Implemented on the same
  read-modify-write path as the other settings.

  This is the STATUS indicator light, not the infrared illuminator -- the IR LEDs
  are `ir_led_enabled` on the image settings, a different field on a different
  endpoint. alarm.com's own interface calls this one "Camera Status LED".

  Both cameras here report `canTurnOnOffLedLight` true and
  `canChangeLedBrightness` false, so both get a switch and neither gets a
  brightness control. A brightness number exists for models that support it and
  **has never run against real hardware**.

- **E33 (DONE, with one point still open) The battery gate is correct
  on the hardware available, and hardened for the case that is not.**
  Verified live on both cameras: `shouldDisplayBatteryLevel` false,
  `batteryLevel` 0, and the bridge correctly reports no battery. The existing gate
  was right.

  Added on top: a NEGATIVE level is now also treated as "no reading". `-1` is this
  API family's "not applicable" -- the mobile document reports `BatteryLevel="-1"`
  with `IsBatteryPowered="false"` for the same device the website reports `0` for
  -- so a model that omits the display flag but answers -1 no longer surfaces a
  nonsense battery of -1%.

  **Still open, and not closable here:** whether `shouldDisplayBatteryLevel` is
  reliable on an actual battery-powered camera. Neither camera on this account has
  a battery. If it ever proves unreliable, the mobile document's
  `IsBatteryPowered` / `CanGetBatteryLevel` pair is the unambiguous tiebreaker.

- **E34 (DONE) Live-video resolution is now a select, and it really
  does change what this bridge restreams.**
  As with E32, the entry was found by way of the MOBILE document
  (`CanSetResolutionAndQuality`), but the WEBSITE has it too and that is what was
  implemented: `videoConfigs` -> `liveVideoConfig` carries `webOptions`
  (value -> label), `selectedWebOption` and an `isWebOptionsDisabled` veto. It is
  the same document the video route already read-modify-writes, so it costs no
  extra request.

  **The premise was worth testing rather than asserting, and it held.** The bridge
  fetches its stream from `liveVideoHighestResSources`, whose name reads like it
  would ignore a resolution preference. It does not: selecting Standard
  Definition and renegotiating took the bridge's own stream from
  `1440x1440@15fps` to `480x480@15fps`, confirmed in its logs. "Highest available"
  means highest under the current selection, not the camera's absolute maximum.
  So this is the control for a camera's bandwidth -- and also a way to make every
  Home Assistant view of that camera worse by accident.

  The change reaches an already-running stream only on renegotiation; an
  established session keeps what it started with.

  Two gating notes: the capability flag IS the option object (`webOptions` empty
  means "no choice offered", which is exactly what `mobileOptions` is on this
  doorbell), and `isWebOptionsDisabled` is the one gate in `camera_settings`
  phrased as a prohibition rather than a capability -- `_Field.forbids` was added
  for it, where an absent flag means allowed rather than denied.

  Only the KEYS of `webOptions` are settable here. That is narrower than the
  general option rule, which has to accept either half of such an object because
  this API is inconsistent about which half it means (see E28) -- so the field
  names its own options key and gets the stricter treatment.

- **E35 (DONE) The outdoor chime level disagreement is resolved: the
  WEBSITE value is a placeholder, and it is no longer reported as a reading.**
  Re-read both APIs against the live doorbell at the same moment. Mobile still
  says `ocl="3"`; website still says `outdoorToneVolumeValue: 0`; both still agree
  it is not settable (`csocl="false"` / `outdoorToneVolumeFeatureSupport: 0`).

  What settles it is the control in the same pair of documents: the volume that IS
  settable agrees exactly across both APIs -- mobile `speakervolume="10"`, website
  `call_volume: 10`. Only the unsettable field disagrees. And the outdoor tone is
  switched ON, which a level of 0 would contradict.

  So the website reports a default for a field it does not manage. The bridge now
  returns `outdoor_tone_volume: null` when `supports_outdoor_tone_volume` is false,
  rather than a 0 that invites a caller to display "volume 0" for an unmuted tone.
  The min/max still come through, so a model that can set it still gets its range.

- **E36 (DONE) `sdcds` is inert: no client reads it, and its most
  likely meaning is recorded.**
  Searched the decompiled app's dex strings directly. `sdict` appears (the parser
  reads it); **`sdcds` appears nowhere at all** -- and neither do `cscd`, `cdv`,
  `mncdv` or `mxcdv`, the chime-DURATION fields carried by the same response. So
  no shipped client of either API reads it, and nothing here depends on it.

  Best-supported reading, by naming convention and by what sits beside it:
  `sdict` = shouldDisableIndoorChimeToggle, so `sdcds` =
  should-disable-chime-duration-setting -- consistent with it being true for None
  and Mechanical and false for Digital, since a digital chime is the kind with a
  programmable duration. **Unconfirmed**, and deliberately not acted on: an
  attribute no client reads is not a rule worth modelling on a guess.

- **E37 (DONE) Selecting the "none" chime type now tells the user what
  it did.**
  The entry left this open pending "a way to warn the user at the point of
  selection". A persistent notification is that affordance, and it is what the
  select now raises -- naming the camera, saying the indoor chime was turned off,
  and saying explicitly that choosing a real chime type again will NOT turn it
  back on.

  Still deliberately not worked around: the integration does not re-enable the
  chime. Silently re-arming something the user may have wanted off is the worse
  failure; telling them is not. The notice is keyed per camera, so changing the
  type twice replaces the notice rather than stacking copies, and it is raised
  only when the chime was actually on beforehand.

- **E38 (DONE) The TELUS-side notification toggles are now switches.**
  Motion and button-press, over the same `generalSettings` document as the status
  LED -- the settings bundle now reads that document once and splits it into two
  sections rather than fetching it twice per camera per poll.

  These are the notifications alarm.com pushes to its OWN app; they do not touch
  anything Home Assistant sends. The realistic use is stopping a doorbell press
  from notifying twice, once per system.

  The gating was the interesting part and is not one flag per toggle: motion and
  button-press share `panelNotificationEnableDisableFeatureSupport`, the smart
  chime has its own, and motion is additionally gated by
  `isMotionNotificationEnabled` -- an availability flag, not a toggle, which the
  portal uses to decide whether to render the row at all. `_Field.requires` was
  added for that second condition. Read off the portal's own bundle rather than
  guessed from the payload; the obvious reading would have offered a smart-chime
  switch this doorbell does not have.

  Confirms the gating works end to end: the doorbell gets two switches and the
  indoor camera, which is not a doorbell and reports every flag false, gets none.

- **E39 (DONE) Solar-panel status is reported per camera.**
  `generalSettings` carries `solarPanelSetting` with `isConnectedToSolarPanel`
  and `connectedToSolarPanelFeatureSupport`, read from the same request that
  already serves the LED and notification sections. Read-only, with no setter:
  whether a panel is bolted to a camera is not a preference.

  An **enum** sensor rather than a binary sensor. Home Assistant renders a binary
  sensor's states through its device class, and there is no class for "an
  accessory is attached" -- a bare On/Off reads as *powered up* or *generating*,
  when all the camera reported is presence. The nearest classes (`plug`,
  `power`, `connectivity`) each describe a live electrical or network condition,
  so borrowing one trades a vague label for a wrong one.

  Both cameras here report the support enum as 0, so **no entity is created on
  this account** -- which is the correct outcome and is asserted as such.

- **E40 (DONE) The notification format is settable.**
  `VideoNotificationFormat` is `Unknown = -1, TextOnly = 0, AttachVideoClip = 1,
  AttachThumbnail = 2`, read out of the portal's own webpack module `288086`.
  That module is in a chunk a normal session never loads, and was fetched
  directly from `/web/system/assets/chunk.<hash>.js` -- **the portal's JavaScript
  is a public static asset**, so an enum with no labels in the API payload can be
  resolved without an account, a session, or a captured UI. The chunk list comes
  from the webpack runtime.

  Only `0` and `2` are offered. The portal builds its own dropdown from those two
  alone, and elsewhere in the same bundle rewrites `AttachVideoClip` to
  `AttachThumbnail` on read, so `1` is a value alarm.com itself treats as stale;
  `Unknown` is a state rather than a choice. The device publishes no option list
  for this field, so the bridge enforces that set or nothing does.

  Note for the next enum: a HAR records what the browser LOADED, not what the app
  CONTAINS, and a captured response body can be stored truncated. Re-fetching the
  assets answers questions the capture cannot.

## Gaps found while implementing E24

- **E41 (DONE) Panel-level Bluetooth disarming.**
  `GET`/`PUT /web/api/settings/panelBluetooth/bluetoothDisarmingSettings/{system_id}`,
  exposed as a pairing switch and a disarm-window select. Separate from the
  per-device toggle, and not its capability gate.

  **The disarm window is in minutes, from a fixed set: 1, 5, 10, 20, 30.**
  Neither the unit nor the set appears anywhere in the document -- both come from
  the vendor portal's own picker, which maps those five through its
  `generic.minutes` string. That is why the bridge reports both and enforces the
  set on write: a caller has no way to derive either from the payload, and the
  panel is not asked to reject a value the vendor never offers.

  **The write shape is inferred, not captured.** No PUT to this endpoint has been
  observed. It follows the flat-body, pluralized-type convention the sibling
  endpoint was observed using. One part is a genuine extrapolation rather than a
  deduction: `enrolledDevices` is the only **to-many** relationship this code
  ever PUTs, and it is sent as a list of `{type, id}` stubs by analogy with the
  captured to-one relationships. Omitting it was the alternative and is worse --
  a whole-document PUT that drops a relationship deletes it, and deleting that
  one unpairs every phone with no undo from this side.

  **Open:** whether `isBluetoothPairingEnabledForSystem: false` makes a
  per-device `isDisarmingEnabledForDevice: true` inert. The per-device settings
  page never reads the panel document, but that is evidence about the portal, not
  about the panel firmware. The two are treated as independent, and nothing tells
  a user that one disables the other.

- **E42 (DONE) Unpairing is a service, never a button.**
  `telus_smarthome.unpair_arming_device`, taking one phone's switch, backed by
  `DELETE /control/arming-devices/{device_id}`.

  **There is no undo from this side.** Re-pairing needs physical access to both
  the phone and the panel. That single fact drives every choice here: a service
  rather than a button, because a button sits on a dashboard beside controls
  that are all trivially reversible and gets pressed by accident, whereas a
  service call has to be written deliberately; one device per call, never a
  list, because a list invites "unpair these three" written once and re-run later
  against a household that has changed; and an id the catalog does not know is a
  404 rather than a delete aimed at whatever that id addresses.

  The phone's name is read BEFORE the delete, so the caller can say which phone
  it removed -- afterwards the record is gone. It is returned but never logged;
  the opaque device id identifies it well enough for anyone reading back, and it
  is a person's name.

  The vendor's panel-wide `deleteAll` is **not implemented at any level**. One
  mis-aimed call would unpair every phone in the household at once, with the same
  absence of an undo. A test fails if a delete-all path ever appears.

- **E43 (DONE) The enrolled partition is writable.**
  `POST /control/arming-devices/{device_id}/enrolled-partition` decides which
  partition a paired phone disarms -- the same class of setting as the disarming
  toggle, and gated with the same care. Options come from the partitions the
  account actually has, never free text.

  Two traps, both closed and tested. `enrolledPartitionId` is the partition
  **number**, not the `<unit>-127` device id sitting on the same record; posting
  the device id is a 400. And a partition list that cannot be read is a 502
  rather than a write to an unverified partition number -- this gate fails
  closed.

  **No entity on a single-partition account.** A picker offering one option that
  is already selected permits nothing but re-selecting what is already true,
  which on a live panel is a whole-record write that changes nothing and carries
  every risk such a write carries. The value is still published as an attribute
  of the disarming switch, the bridge still accepts the write (this is a UI
  judgement, not an API limit), and the entity appears by itself if a second
  partition is ever added.

- **E44 (DONE) The arming-device write body is confirmed by a live PUT.**
  Two details were unverified when the module was written: the browser's PUT
  carries three top-level keys the server never sent (`state: -1`,
  `desiredState: 0`, `isRefreshingState: false`) plus a `deviceIcon.cachedData`
  sub-object, while omitting `deviceModelId` and `primaryAssociatedDeviceIds`;
  and it sends `webSettings` as the string `"7205"` where the read returns the
  integer `7205`.

  The bridge chose to resend exactly what the server said -- no invented `state`
  for a record whose `hasState` is false, and `webSettings` as the integer. **A
  live PUT against the real account returned 200 and the change persisted**, so
  that form is accepted. The browser's extra keys are Ember model state, not
  something the endpoint requires.

  What is still not known, and does not matter here: whether the endpoint would
  *also* accept the browser's form. Nothing needs it to.

## Deferred candidates from the full-tree hardening review

A four-lens review (security, stability, structure, polish) of both codebases
selected five items (the per-request diagnostics timeout, the control-server
429/clamp/log hardening, the clip-view guards, the credentials-fallback
robustness, and the LVSR parser coverage, all shipped) and deliberately
deferred the rest. Logged here per the rule that a gap noticed is a gap
recorded; each open entry carries the reason it was deferred, which is the
evidence the next reviewer needs, and an entry flips to DONE with its evidence
when it ships.

- **E45 (DONE) A pre-existing `control_secret` file is re-tightened to `0o600`
  when the bridge adopts it.** Previously an adopted file (restored from a
  backup, or written under a permissive umask) kept whatever mode it arrived
  with: a bearer token sitting group/world-readable on the shared bind mount
  until the next scheduled rotation happened to rewrite it. Now
  `control_server._load_or_create_secret` calls `_tighten_secret_mode()` after
  successfully reading an existing non-empty secret file: if the mode has any
  group/other bits set, the file is best-effort `chmod`ed to `0o600`, with a
  warning naming the previous octal mode.

  Best-effort on purpose: an `OSError` from the chmod (e.g. the file is owned
  by another uid on the shared mount) logs a warning with traceback and the
  secret is **still served**: the bridge never regenerates the secret over a
  permissions problem, so the HA side keeps authenticating. The helper has its
  own `try` block so a chmod failure cannot fall into the read-error path that
  regenerates the secret. The mode is logged, never the secret. This mirrors
  the `0o600` treatment the HA integration already gives its
  `credentials.json` fallback file; the secret's value, the rotation schedule,
  the generate path (already `0o600`) and the control API contract are
  unchanged.

  Pinned by `test_control_server_routes.py`
  (`test_secret_file_ends_at_0600_however_it_arrived`): a freshly generated
  secret file is born `0o600`, an adopted `0o644` file ends at `0o600` with
  the secret unchanged in memory and on disk, and reloading an already-tight
  file changes nothing. The `OSError` branch is **not exercised by the suite**
  (provoking it needs a file owned by a second uid) so only its warning path
  is verified by reading.

- **E46 (DONE) `mws_client` and `mobile_auth` import defusedxml as a HARD
  top-level dependency**: the decided option, not the warn-once fallback.
  Both modules do `import defusedxml.ElementTree as DET` at module level with
  a comment saying why; the try/except-`ImportError` stdlib `ElementTree`
  fallbacks are deleted, along with `mobile_auth`'s stale fallback comment and
  `mws_client`'s then-unused stdlib `ElementTree` import. Rationale: the
  fallback was dead code in the shipped container (`requirements.txt` pins
  `defusedxml>=0.7.1`, the image installs wheels-only) that silently dropped
  entity-expansion protection for out-of-container runs; a build without
  defusedxml now fails at import time, which the every-module-imports CI job
  turns into a loud PR failure. Pinned by
  `test_parsers.py::test_defusedxml_is_a_hard_import`: a subprocess with a
  meta-path blocker for defusedxml asserts neither module imports, which
  fails against the fallback behaviour and passes against the hard import.

- **E47 (DONE) `JanusCameraSession.close()` no longer leaks the Janus
  websocket, and `_keepalive_loop` no longer dies with an unretrieved
  exception.** Shipped as the dedicated change the entry prescribed (read
  `camera_session.py` and `janus_client.py` in full, `test_camera_session.py`
  run deliberately) with the dropped-frame half of E51 riding along.

  The websocket half: `close()` closes the gateway websocket unconditionally
  via try/finally, so a `pc.close()` that raises, or the
  `SESSION_CLOSE_TIMEOUT_S` `wait_for` cancellation landing mid-close, still
  releases the socket; previously a wedged camera leaked one gateway socket
  per ~165 s rotation. The pc-then-ws ordering is preserved, the guarded ws
  close cannot mask `pc.close()`'s own exception, and `CancelledError` is
  re-raised after the cleanup attempt.

  The keepalive half: `_keepalive_loop` catches `websockets.ConnectionClosed`
  and returns quietly (the recv loop owns marking the session dead, exactly as
  the entry noted), re-raises `CancelledError`, and logs-and-returns on any
  other exception, so a keepalive sent into a session that died between
  keepalives (server force-close ~180 s vs the 25 s interval) no longer
  finishes the task with an exception nothing retrieves, and the recurring
  GC-time "Task exception was never retrieved" tracebacks for ordinary unclean
  rotations are gone. The 25 s interval, close ordering, all timeouts, and the
  rotation machinery are unchanged.

  Pinned by two of the three new `test_camera_session.py` tests, which drive
  the REAL `JanusCameraSession`'s `close()` and keepalive against fake
  pc/websocket objects (still fully offline): the ws close must survive a
  raising and a cancelled `pc.close()`, and the keepalive must die quietly yet
  stay cancellable. Each was proven to fail against the pre-change code before
  passing with it.

- **E48 (DONE) The docs set no longer names `beautifulsoup4` as a live
  dependency.** A fresh grep found four stale spots, all rewritten:
  `development.md`'s Renovate auto-merge example (which now states
  `renovate.json`'s actual policy: low-risk minor/patch auto-merge, with
  pyalarmdotcomajax/aiortc/av/mediamtx/aiohttp/websockets and the grouped
  `python` pair review-required; the old "(python base image, beautifulsoup4)"
  example was doubly wrong, since `python` is review-required) and the
  requirements tables in `configuration.md` and `ha-integration.md`, which
  now list `pyalarmdotcomajax==0.5.13` only, matching the manifest.
  `development.md`'s audit line recording that beautifulsoup4 was dropped as
  unused stays; it states a current fact. Rode along:
  `configuration.md`'s manifest `version` row no longer hardcodes a release
  number (it points at `development.md`'s versioning section instead), so it
  cannot go stale again.

- **E49 (DONE) `mobile_auth`'s cross-module helpers are public:
  `device_fields` and `read_body_text`.** All call sites and comments in
  `mobile_auth` and `mws_client` use the new names; a repo-wide grep confirms
  no code references the old underscore names. The bundled `development.md`
  corrections shipped too: the underscore-import clause is gone from its
  still-open list, and the entity-naming hazard claim was checked by
  repo-wide grep, found FALSE, and replaced with the verified truth:
  `device.__class__.__name__` appears only in `base_device.py` `LOGGER.info`
  lines; entity unique IDs derive from `device.id_` (optionally suffixed with
  a description key or config-option slug) and display names from
  `device.name` plus each platform's `_device_type_name` string literal, so a
  `pyalarmdotcomajax` class rename would affect log lines only. The
  `pylibsrtp` clause remains open there.

- **E50 (DONE) The integration honours the bridge's 429/`Retry-After` contract
  and maps the talk route's remaining documented errors.** Mapping only: no
  retries and no timing changes anywhere.
  `camera_settings_coordinator.async_bridge_post` special-cases 429 before
  `raise_for_status`: it reads the wait from the `Retry-After` header, falling
  back to the JSON body's `retry_after`, and raises "Alarm.com is rate-limiting
  this account; the change was not applied -- try again in {N}s" ("… try again
  later" when no wait is readable), matching `arming_devices_coordinator.py`'s
  vocabulary, so the siren buttons, the privacy switch and every
  camera-settings write get a meaningful toast instead of the raw aiohttp
  "429, message='Too Many Requests'". The integration only reports the
  server's wait; it never retries the write.
  `speak_service._post_talk` maps the talk route's three previously-raw
  documented errors: 413 → the clip exceeds the bridge's size cap
  (`TALK_MEDIA_MAX_BYTES`); 504 → the talk timed out on the bridge before the
  clip finished; 400 → the bridge could not fetch or decode the audio (the
  service always sends a well-formed body, so a 400 on this route can only be
  a fetch/decode failure). The 409/403/422 mappings are unchanged. Wordings
  were checked against `control_server.py`'s actual response bodies and
  `control-api.md`. Verified by `py_compile` and code reading only:
  `speak_service.py` is outside what the integration's test suites cover (E63,
  E66), and this was not run in a live HA instance; live confirmation needs a
  real rate limit or an oversized clip.

- **E51 (DONE: all parts)
  Observability additions that lost the batch-size contest.** The metrics half
  is shipped:
  `TalkMetrics` (`bridge/metrics.py`) keeps a `by_path` counter mirroring the
  existing `by_codec`/`by_endpoint` idiom: incremented once per talk that
  successfully ran a transport, from the result's `path` value (`full_duplex`,
  `webrtc`, `srtp`), including talks ended early by an explicit stop (the
  `by_codec` convention); failed and rejected talks add nothing, and
  serialised as `"by_path"` in the `GET /control/metrics` snapshot. No
  transport code changed: every transport's result dict already carries
  `path`, and the single choke point recording a winning talk is the
  `metrics.talk.success(...)` call in `speak()`. Pinned by
  `test_metrics.py::test_by_path_counter` (increment per path, stopped-talk
  counting, failure/path-less exclusion, snapshot presence, JSON
  serialisability), which was proven to fail (`KeyError('by_path')`) against
  the pre-change code. This makes a silent degradation visible: the
  preferred full-duplex path failing device discovery while every talk
  quietly runs the webrtc/srtp fallback.

  The dropped-frame half shipped with E47's dedicated session, as this entry
  prescribed (it touches the rotation loop's file). `JanusCameraSession` gains
  a public read-only `dropped_frames` property; `camera_session` accumulates
  each ending session's count into a per-camera lifetime total via a
  `_retire_session()` helper wrapping all three `_close_with_timeout` call
  sites (healthy rotation, failure path, restart-guard path), published as an
  additive `dropped_frames` field (present as `0` from the first publish) in
  the `/control/status` per-camera object. Because it accumulates at session
  end, it can lag the live session's drops by up to one rotation; it resets
  with the process like the rest of the status dict. `control_server.py`
  changed in its module-docstring contract only: no route logic. Pinned by a
  new `test_camera_session.py` test (accumulation across sessions, including
  the failure path), proven to fail against the pre-change code. See
  [control-api.md](control-api.md#get-controlstatus).

  The consuming-in-HA idea (held back as a deliberate separate feature) is
  now DONE too, and shipped as **per-camera diagnostic sensors on the camera
  device rather than camera-entity attributes**: a Stream state enum sensor
  (the four `run_camera_forever` states pinned as options; an unknown future
  bridge state degrades to unknown with the verbatim value in a `raw_state`
  attribute, and `fps` / `hardware_encode` / `consecutive_failures` /
  `last_error` / `last_frame_at` ride along as attributes) and a Dropped
  frames `total_increasing` counter, both fed by a `StreamHealthCoordinator`
  polling `GET /control/status` every 30 s (loopback-only, zero alarm.com
  cost) and shared with `binary_sensor.py`, which adds a **camera-bridge
  connectivity entity** on the integration service device: ON while the
  last status poll got an answer, deliberately staying available when the
  poll fails so it can actually say "off". Sensors (not attributes) because
  an enum state and a `total_increasing` counter get recorder history and
  can drive automations directly, which attributes on the camera entity
  cannot. It was the MINOR call: shipped as its own new-capability release.
  See [ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity).

## Gaps found while implementing the stream-health entities

- **E52 (DONE) All ten translation files carry the identical key set.** Each
  of the nine non-English files (`ar,bn,es,fr,hi,pt,ru,ur,zh-Hans`) now holds
  the same 96 leaf keys as `en.json`, verified by a programmatic key-tree diff
  (zero missing, zero extra, per language). The 31 keys that were missing,
  the talkdown sensors, the camera diagnostic and solar sensors, and the
  entire `number` / `switch` / `select` / `button` groups, were translated
  per language against each file's established register (e.g. pt stays
  European Portuguese: "altifalante", "à chegada"; ru renders talkdown as
  "голосовое оповещение"; bn/hi/ur transliterate it as a feature name,
  matching how those files treat technical loanwords; brand terms TELUS /
  Bluetooth / Alarm.com stay untranslated everywhere, per each file's
  existing practice). Translations are model-written, not native-speaker
  reviewed; a fluent speaker may still improve phrasing, but the key sets
  are complete and any future drift is a one-line key-tree diff away from
  being visible.

- **E67 (DONE) The Audio state sensor says why audio stopped.**
  The bridge now distinguishes "the audio pipe wedged and was deliberately
  dropped so video could keep publishing" from "audio simply stopped arriving",
  via `audio_dropped` and `audio_dropped_reason` in `/control/status` (E62). The
  integration reads neither. `CameraAudioStateSensor` reports `stalled` in both
  cases: correct, because the pump does stop feeding audio once the pipe is
  dropped, but it leaves the operator to go read container logs for the cause.

  Both now ride as attributes on the existing sensor, needing no new poller.
  Deliberately *not* a fifth enum state: `stalled` is what an automation should
  act on either way, and splitting it would break any automation already keyed
  on it. Absent on an older bridge reads as `null`, never as a fabricated
  `false`, pinned by a test, along with the dropped-pipe case itself. Whether a
  dropped pipe also deserves its own binary sensor is a separate question:
  probably not, since it should be rare and is visible in the attribute.

## Downstream (listen) audio

- **E53 (ANSWERED: yes, with a caveat) The upstream does serve a downstream
  audio track.** With `JANUS_REQUEST_AUDIO` on, the gateway offers
  `audio(port=9,opus)` alongside the video for **both** cameras on this
  account. The video-only offer seen by default is entirely explained by the
  bridge's own create body, which asks for video only. Two further facts fell
  out of the same observation:

  - **`liveVideoHighestResSources` has no `supportsAudio` and no
    `audioUpStreamUrl` attribute at all.** The real attribute set is
    `coturnAddressesTuplets, errorEnum, iceServers, isMjpeg, janusGatewayUrl,
    janusToken, proxyStreamTimeoutTime, proxyUrl, spsAndPpsRequired,
    urlEncoded, visualVerificationRecord`. Earlier reasoning that treated a
    `supportsAudio=false` as evidence about audio was reasoning about a field
    this endpoint does not return.
  - **THE CAVEAT, and it is serious: asking for audio broke the indoor
    camera.** With audio negotiated, camera 2049 repeatedly failed
    `RuntimeError('no frames received before timeout')` with the ICE
    connection going `closed`, reconnecting every ~23 s and reaching three
    consecutive failures, two short of the escape hatch that hard-exits the
    process. The doorbell (2048) streamed normally at 15 fps throughout the
    same window. The experiment was rolled back and both cameras recovered
    immediately.

  **The failure is not slowness; that is now measured.** With first-frame
  timing instrumented, the indoor camera's *normal* first frame arrives in
  1.2–1.8 s, and it streams at 6–7 fps. When audio was negotiated it waited the
  **full 20 s timeout and received zero frames**: at 6 fps a merely-slow
  camera would still have delivered on the order of a hundred frames in that
  window. So media never arrives for that camera under audio, rather than
  arriving late, and raising the first-frame timeout would not help.

  **The doorbell tolerates audio.** With the knob narrowed to the doorbell
  alone it streams 1440x1440@24fps, first frame 0.7–0.9 s, zero failures, while
  the indoor camera runs untouched on the video-only path. That split is why
  the knob is per-camera.

  **Still unverified:** *why* the indoor camera receives nothing. The leading
  hypothesis is that the audio m-line arriving first changes the answer or the
  track dispatch in a way that camera's relay path does not tolerate; a weaker
  one is that `rtx` appears on the video m-line only in the audio-on offer, so
  "audio" and "rtx" are confounded in the observation. Also unproven: that the
  doorbell stays healthy over days rather than the minutes observed.

- **E54 (DONE, unproven against hardware) Downstream audio is consumed and
  published.** `JANUS_CONSUME_AUDIO` (default off, per-camera, and it requires
  the same camera to be in `JANUS_REQUEST_AUDIO`) pumps the offered track into
  its own bounded queue and publishes it as mono AAC alongside the video on the
  same RTSP path. AAC because it is the only codec both consumers take without
  reconfiguration: Home Assistant's HLS worker accepts `aac`/`mp3` only, and
  Frigate's `-c copy` recording preset needs an mp4-compatible codec.

  **The rotation gap is filled with silence measured in samples, not sleeps.**
  The publisher deliberately outlives every session, so the audio input is
  opened once with the sink and never closed: ffmpeg cannot reopen an input,
  and an EOF there would silence the track for the life of the publisher. The
  hold writes `elapsed_seconds * rate - samples_written` samples, so real audio
  and silence share one wallclock baseline and the gap cannot accumulate drift.

  **A real defect was found by testing against the actual ffmpeg binary rather
  than reasoning about it.** Mirroring the video input's
  `-use_wallclock_as_timestamps` onto the audio input makes ffmpeg stop reading
  the second input a few seconds in and never resume; the pipe then backs up
  at real-time rate forever, which the sink can only read as a dead publisher.
  Audio-only, the same input keeps up perfectly; it is specifically the
  two-input combination, and it survived a matrix of `aresample`,
  `thread_queue_size`, `fps_mode`, `max_interleave_delta` and output-format
  variations. The flag stays on video and is absent from audio, pinned by a
  test asserting it appears exactly once. Measured on ffmpeg n9.0.1; the
  container ships a different build, so `ffmpeg has stopped reading the audio
  pipe` is the line to grep if audio ever backs up.

  **It works, and the cause of the first failed deploy was not what it looked
  like.** Enabling it on the doorbell first produced an encoder restart loop
  (`ffmpeg has stopped reading the audio pipe`, ~550-680 kB queued, every few
  seconds), which was initially attributed to the container's ffmpeg 7.1.5
  differing from the 9.0.1 used offline. That attribution was wrong. The stall
  reproduces **identically on both builds**; the offline run had merely fed
  frames at exactly the declared rate and so never triggered it.

  The real cause: **`-r <fps>` before `-i` sets the rawvideo demuxer's frame
  rate and in doing so silently overrides `-use_wallclock_as_timestamps`.** The
  video timeline then advances 1/fps per frame *written* rather than per second
  *elapsed*. With video alone that is invisible. With a second input it is
  fatal, because the audio timeline really is wallclock (the silence fill sizes
  itself from it): any shortfall between the declared fps and the rate frames
  actually arrive at makes audio outrun video, the muxer's interleaver stops
  accepting audio packets, and ffmpeg stops draining the pipe. Proof is in the
  durations: 11 s of wallclock produced 6.24 s of video, exactly the 15/25
  ratio of written-to-declared fps.

  The fix is one token: the video input drops `-r` when audio is enabled.
  Everything else was ruled out by measurement inside the container:
  `-fps_mode` (both settings), `-max_interleave_delta 0`, `-thread_queue_size`
  up to 4096 on either or both inputs, dropping `-analyzeduration`/`-probesize`,
  reversing input order, and dropping `aresample` all still stall; the output
  muxer is irrelevant (null, mpegts and live RTSP behave the same). Frame
  *count* is unchanged, so keyframe cadence and encode cost are untouched.

  **MediaMTX accepts the second AAC m-line**: confirmed by publishing to a
  throwaway path and reading it back: `h264 960x960` + `aac 48000Hz mono`, no
  ANNOUNCE error, 100 s with zero pipe backlog, and stable under an extreme
  5-vs-25 fps mismatch and a deliberate 5% audio skew. Not yet exercised: Home
  Assistant's HLS worker or Frigate actually consuming the two-stream path.

- **E58 (DONE) The video input declares its rate with `-framerate`, never
  `-r`.** Both halves of that matter, and the second is the one that nearly got
  shipped wrong.

  `-r <fps>` before `-i` forces the input stream's rate and overrides
  `-use_wallclock_as_timestamps`, so the timeline advances per frame *written*
  rather than per second *elapsed*, measured at 24.07 s of media per 40 s of
  wall time when a 15 fps-declared camera delivered 9 fps, exactly the 9/15
  ratio. That is the drift an HLS viewer accumulates.

  But **removing `-r` outright is not the fix.** The rawvideo demuxer's
  `framerate` option defaults to **25**, so with no declaration every stream is
  re-rated to a constant 25 fps: frames duplicated below it and *dropped* above
  it (measured: 1201 written → 1002 encoded, 199 lost, on a 30 fps source), and
  `-g <fps>` then spans 0.24–1.2 s instead of the ~1 s it exists for.
  `-framerate <fps>` sets the demuxer's rate without touching timestamps, which
  is the combination this pipeline needs; it was verified identical to `-r` in
  output rate, frame count and GOP while tracking wallclock, on VAAPI and
  libx264 alike, at 6/15/25/30 fps.

  **This was already live and wrong.** E54's fix removed `-r` from the
  audio-enabled argv only, so from the moment audio was enabled the doorbell
  published **CFR 25 with 0.6 s keyframes** (roughly 67% more encoded frames
  than intended) while E54's note claiming "frame count is unchanged" was
  simply false. Both are corrected.

- **E59 (DONE, confirmed live) aiortc's Opus decoder killed
  its own thread, and the published audio was pure silence.** The entry above
  understated this badly: audio did not "arrive in gaps"; it never arrived at
  all. Measured on the live published stream with `ffmpeg -af volumedetect` over
  200 s: **9,572,352 of 9,572,352 samples at the s16 noise floor**, `max_volume`
  equal to `mean_volume`. Every sample in the doorbell's AAC track was coming
  from the sink's silence fill.

  The cause is a two-file asymmetry in aiortc. `codecs/h264.py` wraps its decode
  in `try/except av.FFmpegError`, logs, and returns `[]`: one packet dropped,
  decoder lives (43 such warnings live, zero dead video threads). `codecs/opus.py`
  has **no handler at all**, and neither does `decoder_worker`'s loop, so one
  malformed packet propagates out of the thread target and the `audio-decoder`
  thread **dies permanently**: it is started once, with no restart path. Live:
  11 sessions, 11 deaths. Two silent consequences followed: the receiver keeps
  queueing to the dead thread (its gate tests the Thread *object*, truthy even
  when dead) at ~8,000 frames per session, and the bridge's audio pump waits
  forever on a `recv()` whose end-sentinel only the dead worker could produce.

  Fixed by `bridge/aiortc_opus_patch.py`, a fourth monkeypatch following the
  integration's `*_patch.py` convention with an import-time compatibility guard,
  including a check that upstream has not grown its own handler, which would
  mean the patch should be deleted. **Not fixed upstream:** 1.15.0 is the latest
  release and `main` is byte-identical.

  **What the status endpoint was doing is worth remembering.** `audio_format`
  was rendered from what ffmpeg had been *launched* with, which falls back to
  `AUDIO_DEFAULT_FORMAT`: literally `("s16", 48000, 2)`. So through eleven
  sessions of total silence it reported a confident `"48000Hz/2ch/s16"`
  alongside `state: streaming` and a fresh `last_frame_at`. Nothing in the
  payload could distinguish flowing audio from audio that had never decoded a
  single frame. It now reports `null` until a frame is genuinely observed, and a
  new `decoded_audio_frames` counter makes flow checkable by polling twice.
  `dropped_audio_frames` could never have caught this: a stream that decodes
  nothing drops nothing.

  **Confirmed live, and the bad packet is identified.** After the fix: zero dead
  decoder threads, `decoded_audio_frames` climbing (730 within three minutes),
  and the published track measures `mean_volume -20.1 dB` / `max_volume 0.0 dB`
  against the -91.0 dB noise floor it sat at before: real sound, not silence.

  The rejected packet is **G.711 µ-law audio on the Opus payload type**:
  `len=512`, bytes clustered on `0x7c`–`0x7f` and `0xfc`–`0xff`, which is µ-law
  near-silence. Neither of the suspected shapes (Ogg `OpusHead`/`OpusTags`, H.264
  NAL bytes); those merely reproduce the same error class.

  **The reading of that packet recorded here was wrong, and the error is the
  interesting part.** "The relay forwards a device-native frame or two before its
  Opus transcode is running, costing a few milliseconds of audio per session"
  was inferred from the log line firing **once per session**, and the log line
  fires once per session because this patch throttles it to once per session.
  The whole stream is µ-law; ~94% of it was being dropped, and the drop was
  invisible because the only evidence was a line whose cadence was set by the
  logger rather than by the stream. See **E80**, which is where the mislabel is
  identified and fixed. Two lessons stay attached to this entry: a throttled log
  line's cadence says nothing about the frequency of what it reports, and a
  guard that discards is not a fix until the discard rate is counted.

  This also revises the live confirmation above. Zero dead decoder threads and
  a track that measures real sound are both true and are still the evidence that
  the thread-death half is fixed; `decoded_audio_frames` climbing at 730 in
  three minutes (4/s) was read as "audio is flowing" when it is 8% of the ~50/s a
  20 ms audio stream implies. The number needed for that judgement was in the
  entry all along.

- **E61 (DONE) Audio health is visible from Home Assistant.** Each camera gets a
  `Audio state` ENUM diagnostic sensor on its own device, fed by the existing
  shared `StreamHealthCoordinator` (no second poller): `not_consuming`,
  `no_audio_yet`, `flowing`, `stalled`. The raw `decoded_audio_frames`, the
  per-poll delta, `audio_format`, `dropped_audio_frames` and `last_audio_at`
  ride as attributes.

  **An enum rather than a counter, deliberately.** Home Assistant has no "this
  number stopped changing" trigger, so a `total_increasing` frame count would
  have left the exact failure it exists to catch (decoder dies, counter
  freezes, `state` stays `streaming`, `last_frame_at` stays fresh) as something
  a human must notice on a graph. That is precisely how eleven silent doorbell
  sessions went unnoticed. Comparing the counter between polls turns *stopped*
  into a state you can automate on.

  Every camera gets one, including cameras with audio switched off (they read
  `not_consuming`): entity creation is sticky, so one that appears and vanishes
  with a bridge env var would break any automation naming it, and "no audio
  entity at all" reads as "fine", the very gap being closed. A stall is never
  reported for an unanswered poll, a failed poll, a counter reset (bridge
  restart), or the first poll of a camera that already has frames.

  **Deferred, recorded rather than lost:** there is no graphable recorder series
  for `decoded_audio_frames` (attribute only; a companion `total_increasing`
  sensor would give one at the cost of a series per camera); the `stalled`
  threshold is one 30 s poll window and is not configurable, so a genuinely
  bursty source needs the automation's `for:` instead; `last_audio_at` is
  observed by the poller so it resets on an HA restart and is only accurate to
  30 s, because the bridge exposes no last-audio-frame timestamp. The
  derivation is now pinned by `tests/test_stream_health_sensors.py` (E63),
  including all four cases where `stalled` must never be reported. **This makes
  dead audio
  visible; it does not recover it**: that is E60/E62.

- **E62 (fixed in the bridge, awaiting live validation) Audio consumption took the doorbell
  off the air, and the bridge reported `streaming` throughout.** Enabled on the
  doorbell it ran correctly for about fourteen minutes (real decoded audio,
  measured) and then collapsed. The sequence, from the container log:

  ```
  22:11:24  ffmpeg publishing 960x960@15fps ... audio aac mono from 48000Hz/2ch/s16
  22:12:54  ffmpeg: More than 1000 frames duplicated
  22:25:57  ffmpeg has stopped reading the audio pipe (636928 bytes queued); restarting the encoder
  22:25:58  ffmpeg: More than 10000 frames duplicated   +  a second pipe stall
  23:14-17  dropped 600 ... 2550 frame(s) -- consumer falling behind (queue maxsize=4)
  23:17:06  session marked dead: track ended: MediaStreamError()
  thereafter  MediaMTX: "no stream is available on path telus_...-2048"
  ```

  **The `-framerate` fix (E58) did not eliminate the pipe stall: it delayed
  it.** The underlying condition is E60. The mechanism written here originally,
  "the per-frame pixel copy cannot keep up at 1440x1440", is **wrong and was
  disproven by measurement**; the copy costs 0.24 ms of a 66.7 ms interval. What
  actually starves the video path is the bursty audio silence top-up. The rest
  of the chain held: `janus_client` drops frames before they reach ffmpeg,
  ffmpeg starves and pads to CFR (10,000+ duplicated frames), and the audio
  timeline outruns the starved video until the interleaver stops accepting
  audio. The encoder restarts that follow eventually left the camera with **no
  publisher at all**.

  **The worst part is the reporting.** Throughout the outage `/control/status`
  said `state: streaming`, `consecutive_failures: 0`, with a fresh
  `last_frame_at`, while MediaMTX had no path and every reader was being
  refused. The frame-silence watchdog watches frames arriving from Janus, not
  whether the publisher still exists, so a dead publisher is invisible. That is
  its own defect and arguably the more dangerous one: **the bridge cannot
  currently tell "publishing" from "publishing to nowhere."**

  **Four changes shipped in response.** Validation is a live multi-session run
  with audio enabled on the doorbell only; what it must show is listed at the
  end of this entry.

  1. **Audio pacing.** `AUDIO_SILENCE_TOPUP_S` 0.5 → 0.05 (half the muxer lead;
     see E60 for why 0.1 measured *worse* than the defect), per-write ceiling
     `_AUDIO_SILENCE_MAX_BURST_S` 2.0 → 0.1, and a new `AUDIO_PUMP_MAX_BURST_S`
     bounding the pump's drain with a yield rather than a real-time pace:
     audio *lagging* the video is the stall condition, so a backlog must be
     delivered as fast as the loop allows. The frame hold also ticks at
     `min(1/fps, TOPUP)`: at 6 fps it was topping up every 167 ms, past the
     lead, so the fix would otherwise have been half-applied on that camera.
  2. **A time-based video queue.** `round(fps × 500 ms)` clamped to 4–8 instead
     of a flat `maxsize=4`. 15 fps → 8 (533 ms), 6 fps → 4 (667 ms, the floor).
     Ceiling arithmetic: 1440×1440 yuv420p is 3.11 MB, so 8 frames is 24.9 MB
     per camera against a 768 MiB limit. Above ~16 fps the memory ceiling binds
     before the time target. This is defence in depth only: a deeper queue
     converts drops into latency, it does not remove a stall.
  3. **A wedged audio pipe degrades to video-only.** `FfmpegSink._drop_audio()`
     closes the audio transport, latches `audio_dropped` /
     `audio_dropped_reason`, and keeps video publishing instead of asking for a
     rebuild. Verified in-container: closing the write end mid-run left video
     consumed normally for a further 435 frames at p50 1.87 ms, zero stalls.
     `audio_consumed` keeps its existing meaning, "configured to".
  4. **Publisher liveness.** `write_frame`'s `stdin.drain()` is bounded by
     `_VIDEO_DRAIN_TIMEOUT_S = 5.0`. A 3 MB raw frame against a 64 kB pipe
     buffer means the drain can only resolve because ffmpeg *read* it, so
     completing is direct evidence of consumption; timing out latches
     `publisher_stalled` → `publisher_alive` false → rebuild. **Unbounded, as it
     was, that await parks the camera's entire loop forever with the status
     frozen on `streaming`**, plausibly the E62 mechanism itself. `/control/status`
     gains `publisher_alive` and `last_publish_at`; the gap between
     `last_publish_at` and `last_frame_at` *is* "streaming" vs "streaming to
     nowhere". Chosen over querying MediaMTX because `mediamtx.yml` disables
     every listener but RTSP and the container is `--network host`, so enabling
     its API to health-check ourselves would open a real host port. **Stated
     limit:** this proves ffmpeg reads its input, not that MediaMTX accepts what
     ffmpeg writes.

  **What to watch when audio is re-enabled (doorbell only, one camera at a
  time):** that camera's `dropped_frames` delta per ~165 s rotation, which
  should fall from 1,650–2,550 to near the audio-off camera's ~1; whether
  `audio_dropped` fires at all (if it does, something *else* is still starving
  the video timeline; check `dropped_frames` first); the gap between
  `last_publish_at` and `last_frame_at` on every poll; and
  `decoded_audio_frames` still climbing.

  **Two gaps recorded rather than dropped:** the HA **Audio state** sensor read
  `stalled` without the cause, because the integration did not surface
  `audio_dropped_reason`; closed by **E67**.
  And every container measurement behind these four changes output to
  `-f null -`, never a live RTSP publish into MediaMTX, so the muxer-target side
  of the pacing is **inferred, not captured**; tracked as **E68**.

- **E60 (DONE, fixed in the bridge) The doorbell's frame drops are
  caused by the audio silence top-up, not by the pixel copy.** Both earlier
  hypotheses are disproven by measurement inside the container, and so is the
  measurement that appeared to disprove the first one.

  **What actually happens:** the bridge hands ffmpeg's audio input **0.5 s of
  PCM in a single burst** (the quiet-audio path: `_pump_audio` waits
  `AUDIO_SILENCE_TOPUP_S` for a decoded frame and on timeout pushes the whole
  wallclock deficit at once), and each burst makes ffmpeg **stop reading the
  video pipe for 165–330 ms**. The video queue holds 4 frames = 267 ms of
  tolerance, so frames drop. One video stall per audio write, lasting roughly
  0.35–0.75× the audio time handed over at once:

  | audio write cadence | `write_frame` p90 | frames >66.7 ms of 900 |
  |---|---|---|
  | video-only (control) | 2.69 ms | 1 |
  | every 20 ms | 2.51 ms | 1 |
  | every 100 ms | 2.46 ms | 1 |
  | **every 500 ms (the defect)** | **166 ms** | **120 (13.3%)** |
  | every 1000 ms | p99 730 ms | 61 |

  `-thread_queue_size` and `-max_interleave_delta` change nothing, and splitting
  one 500 ms burst into 25 back-to-back writes changes nothing: it is temporal
  pacing that matters, not chunking. A doorbell is quiet most of the time, so
  the quiet path is the normal path.

  **Disproven:** the per-frame pixel copy costs **0.24 ms of a 66.7 ms frame
  interval** (0.36%), against a machine that does 28 GB/s of memcpy; and
  pre-scaling the feed to 960x960 changes the stall count by **exactly zero**
  (it halves p50 and nothing else). The old note's "roughly 6 MB of copying per
  second" was wrong twice: it is 3.11 MB *per frame*, ~47 MB/s each way, and
  neither figure is load-bearing. CPU is not the constraint either: 0.016% of
  periods throttled, 11% of the 2-core cap used.

  **A measurement error of mine, corrected.** This entry previously said the
  hypothesis was disproven because "46 warnings and 5,455 dropped frames in the
  first twelve minutes, no better than before". That was **not an audio-off
  control**: it was the first twelve minutes of the audio-*enabled* E62 run. The
  real control is the current audio-off deployment: **2 drops in 21 minutes
  across both cameras**, both at ffmpeg startup. The conclusion drawn from the
  bad control ("the re-rate was not the cause, look at the pixel copy") sent the
  next round of work at the wrong target.

  **Severity was also understated.** Not "~300 per session": the incident log
  shows **1,650–2,550 dropped per ~165 s session, around 70% of frames**, and
  only on the camera with audio enabled. The audio-off camera in the same
  process, same event loop, same host, dropped 1.

  **E60 is a symptom of E62's audio path, not its cause.** The causal claim in
  both entries ran backwards.

  **The mechanism is the LEAD, not the burst size: a second measurement
  corrected the first.** Building the fix started from the table above and its
  reading that "pacing to 100 ms removes the stall entirely". A more faithful
  rig, one reproducing `write_silence_to`'s actual arithmetic (deficit against
  wallclock, the 100 ms prime, the per-call ceiling) rather than writing on a
  fixed cadence, **did not reproduce that row**. It measured 100 ms at **162 of
  900 frames (18%), twice**, i.e. *worse* than the 500 ms defect it was meant
  to fix. The same rig reproduced the shipped defect exactly (120 of 900,
  matching the first table to the frame), which is what makes it the more
  trustworthy of the two:

  | top-up cadence / ceiling | `write_frame` p50/p90/p99 | frames >66.7 ms of 900 |
  |---|---|---|
  | video-only (control) | 1.81 / 1.94 / 2.87 ms | 0 |
  | **0.5 s / 2.0 s (the defect)** | 1.80 / 231.6 / 297.1 ms | **120 (13.3%)** |
  | 0.1 s / 0.1 s | 1.90 / 202.8 / 297.1 ms | 162 (18%) |
  | 0.08 s / 0.1 s | 1.81 / 1.97 / 2.27 ms | 0 |
  | **0.05 s / 0.1 s (shipped)** | 1.81 / 2.08 / 2.83 ms | **0** |
  | 0.025 s / 0.1 s | 1.80 / 2.04 / 3.44 ms | 0 |

  `_AUDIO_PRIME_MS` (100 ms) puts the sample-counted audio timeline *ahead* of
  the wallclock-stamped video, and the muxer can only emit a video packet once
  it holds audio covering that timestamp. That lead decays by elapsed time
  between top-ups, so **the cliff sits exactly at the prime**: a 0.1 s cadence
  hands ffmpeg a fifth as much per write as 0.5 s and stalls *more* frames,
  because it spends the instant before every write at zero lead. Running ahead
  is harmless: it only lengthens the lead. Both tables agree wherever the
  cadence is comfortably under the prime (20–50 ms) and at the 500 ms defect;
  they disagree only at 100 ms, which is the cliff edge itself, and a cliff edge
  is exactly where two rigs should be expected to disagree.

  So the shipped value is `AUDIO_SILENCE_TOPUP_S = 0.05`, **half** the lead, not
  the 0.1 that the first reading implied. 0.1 would also have made the cadence
  equal the ceiling, so a clipped deficit could never be repaid: a permanent,
  accumulating A/V offset that the old 2.0 s ceiling did not have.

  **Fixed in the bridge.** What live validation must show is listed at the end
  of E62, alongside the other three changes that shipped with this one.

- **E55 (REOPENED + IMPLEMENTED 2026-08-29) Device-direct listen audio.** It IS
  worth pursuing after all, for the CALL: the cloud Janus path churns for the
  doorbell (E96), so a live call's uplink now comes from the device-direct
  stream instead. `GET /api/GetStream?streamID=0` on the device's LAN port is
  RTSP-tunnelled-over-HTTPS (`application/x-rtsp-tunnelled`): a long-lived GET
  data channel + a paired POST command channel (Apple scheme), HTTP Digest on
  the tunnel and RTSP Digest inside it (both the per-session vieweruser/pw from
  the camera list), media interleaved on the GET channel. `bridge/rtsp_listen.py`
  implements it and reads the codec from the DESCRIBE SDP. The video pipeline
  still uses Janus; only the call's uplink moved. (Original note kept: the
  downstream half was uncharacterised; it is now.)

  One correction worth keeping, because the opposite is the natural assumption.
  The doorbell **does** expose a device-direct HTTP API on the LAN, and talkdown
  uses it on every call: the `<cli>` element's `duae`/`luae` are
  `https://<doorbell-ip>:41925/api/PutAudioStream`, and the bridge PUTs audio
  straight there whenever it shares the LAN with the device; that traffic never
  traverses alarm.com. What was never characterised is the *downstream* half.
  `GET /api/GetStream?streamID=0` on the same port returns something, but its
  container, and whether it carries audio at all, are unrecorded, and the URLs
  are published with an `rtsp://` scheme the device does not serve (it answers
  RTSP with an HTTP 400 from lighttpd). Reopen only if the Janus path is
  abandoned.

## The adaptive frame rate

The frame rate is a device setting, not a constant discovered once; these
entries are the mechanism that keeps it under review
(`camera_session.RateWatcher`), its guards, and what is still open about it.

- **E64 confirmed live, 2026-08-25.** The living room camera's delivered rate
  walked down through the morning, 29 to 10 to 7 to 5 fps, over thirteen rate
  events. The obvious suspicion was a runaway in this code, since every rebuild
  creates a gap in frames and a gap biases the next measurement low, which would
  make the descent self-reinforcing. It is not that: `measured_fps` reads 5
  against a declared 5 and has held there across a dozen consecutive rotations,
  so the code tracked a genuine source-side decline. That is exactly the case it
  was built for. Declared at the morning's 29 against a real 5, the encoder would
  have been duplicating frames continuously; `duplicated_frame_warnings` sat at 6
  for that camera against 1 for the doorbell.

  Related, from the same window: the doorbell's residual drop rate with audio
  consuming is steady at 7 to 10 per minute, about 1.1 percent of frames, over
  six hours with no degradation. That is roughly a 99 percent reduction from the
  E60 defect and it is still not zero. See E62.

- **E64 (DONE) The frame rate is adaptive, not latched.** The measured fps was
  latched for the life of a process, and `-framerate` bakes it into the output.
  The same indoor camera locked `known_fps = 7` for one process lifetime and
  `15` for another with no change to the camera; the doorbell has logged
  `implausible fps measurement 62`; and the live v1.21.0 deployment measured
  **29 fps** on a camera that then logged `More than 1000 frames duplicated`
  within 2.5 minutes: the declared rate exceeding what the source delivers,
  observed rather than reasoned about.

  The rate is a **device setting**, not a property of the camera: the live-video
  resolution select ships as an HA entity (E34), so it can change from HA, the
  TELUS app or the portal with no restart and no signal to the bridge. Latching
  it was therefore wrong by construction, not merely imprecise.

  **Shipped:** `camera_session.RateWatcher` re-measures while the stream runs and
  rebuilds the publisher through the same path a frame-size change takes; on
  this platform the same event, since both come from the resolution setting.

  **Why each threshold is what it is.** Every rebuild kicks every RTSP reader off
  the MediaMTX path, so the cost of a false positive is real and five gates stand
  between a measurement and a rebuild:

  | Gate | Value | Evidence |
  |---|---|---|
  | Settle window | `RATE_RESAMPLE_SETTLE_S = 10 s` | The session-start burst has produced 544, 215 and 62 fps readings. The dangerous ones are the *plausible* burst readings ("38–49 fps for a real 15 fps camera") which no arithmetic can tell from a real change. Leaves ~155 s of every 165 s session measurable. |
  | Batch | `RATE_RESAMPLE_FRAMES = 90`, median | 3 s at 30 fps, 15 s at 6 fps; no single late or bunched frame moves a median of 90. |
  | Hysteresis (down) | `RATE_CHANGE_TOLERANCE_DOWN = 0.20` | Must clear observed jitter (6↔7 across restarts = 17%) while catching the live 29 → ~22 (−24%) implied by that duplication rate. |
  | Hysteresis (up) | `RATE_CHANGE_TOLERANCE_UP = 0.35` | Wider, deliberately; see below. Still catches +100% (15 → 30) and +150% (6 → 15). |
  | Confirmation | 2 consecutive settled batches must agree | One batch acting alone is exactly the defect being fixed. Also what makes a flapping source **converge**: alternating rates never produce two agreeing batches, so nothing is ever confirmed. |
  | Storm bound | `RATE_REBUILD_MIN_INTERVAL_S = 300 s` | Just under two rotations. Delays a correction, never cancels one. |

  **The band is asymmetric, and the direction is settled by measurement, not
  taste.** From `ffmpeg_sink`'s own in-container table: declaring **above** what
  the source delivers pads to CFR with duplicates (361 written → 601 encoded):
  wasteful, nothing lost; declaring **below** it makes ffmpeg **drop** real
  frames (1201 written → 1002 encoded, **199 gone**): content destroyed. And
  measurement error is asymmetric the same way, because buffered frames arrive
  back-to-back and never slower than real time, so a contaminated batch always
  reads *fast*. Both arguments point one way: be readier to lower a declared rate
  than to raise it. A camera *set* to 15 that delivers ~13.4 (−11%) is
  deliberately left alone: 15 is the real setting, and declaring 13 would start
  dropping the frames that do arrive on time.

  **A rate change never takes the camera off the air.** The rebuild is applied at
  the next session rotation, not by raising from the streaming loop the way an
  audio-format change does: the rotation gap is already bridged by the frame
  hold, so the change costs one rebuild glitch rather than a glitch plus a failed
  session plus a backoff. It never contributes to the failure run
  `SESSION_WEDGE_TIMEOUT_S` measures. The pending change is also re-checked
  against the current reading at that point, so a rate that wandered and came
  home rebuilds nothing.

  **Interactions with the two things E60/E62 shipped:**

  - **The time-sized queue is not resized; it is rebuilt.** It is per-session and
    sized at construction from `known_fps`, so the next session after a confirmed
    change simply gets the right depth; the retiring session's queue dies with the
    session, so **no frames are stranded**. Note that 16–30 fps all clamp to the
    8-frame memory ceiling (confirmed live: both cameras log `queue maxsize=8` at
    15 and 29 fps), so a change inside that band alters the depth by nothing,
    and since queue depth is not an input to the rebuild decision, that costs no
    needless rebuild either.
  - **The frame hold's tick is unchanged and cannot be moved past the muxer
    lead.** The hold now repeats at the rate the *running* ffmpeg was declared
    with (`sink.fps`) rather than the latest measurement, because between a
    confirmed change and the rotation that applies it those differ, and a repeated
    frame only means "one frame" to an ffmpeg reading `-framerate N`. The audio
    half is safe at *any* rate by construction: the tick is
    `min(1/fps, AUDIO_SILENCE_TOPUP_S)` and fps is clamped to 1–30, so the tick
    can never exceed 50 ms, half of `_AUDIO_PRIME_MS`. E60's cadence invariant
    holds for every rate the watcher can produce.

  **Tests** (`test_camera_session.py`, four new): the hysteresis band's asymmetry
  and its behaviour on every rate pair this account has actually produced; the
  settle window, confirmation, flapping convergence and storm bound against a
  synthetic clock; the end-to-end rebuild, at a rotation, with the session never
  failed and the next queue re-sized; and the frame hold following the declared
  rate while a change is pending. Each was proven to fail against a targeted
  breakage of exactly the invariant it pins.

  **One test was worthless as first written and was strengthened**, which is
  worth recording because the trap is reusable: the settle-window test used a
  1000 fps burst, which the 1–60 plausibility range rejects wherever it is
  measured, so deleting the settle window entirely left the test green. Rewritten
  with a *plausible* 40 fps burst (the failure the logs actually show), it fails
  as it should. A burst test that is too extreme tests the sanity range, not the
  settle window.

- **E68 (DONE) The pacing fix is confirmed against a live RTSP publish, not
  only `-f null -`.** The original cadence sweep exercised the demuxers, the
  encoder and the interleaver but not the RTSP muxer or MediaMTX's
  consumption of it, so the muxer-target side of the pacing was inferred. The
  same rig arithmetic (the real `FfmpegSink`, the real `write_silence_to`
  deficit-vs-wallclock fill, a synthetic 15 fps feed, 900 frames per run) was
  re-run inside the deployed container publishing a real RTSP ANNOUNCE into
  the live MediaMTX on a scratch path:

  | case | write_frame p50/p90/p99 (ms) | frames >66.7 ms of 900 |
  |---|---|---|
  | video-only control | 1.02 / 1.31 / 1.73 | 0 |
  | the defect (0.5 s / 2.0 s) | 1.22 / 367.2 / 434.0 | **229** |
  | shipped (0.05 s / 0.1 s) | 1.03 / 1.32 / 3.78 | **0** |

  The conclusion is unchanged and now captured end to end: the defect cadence
  stalls the video pipe against a real muxer target too (harder than against
  `-f null -`: 229 of 900 against 120, which is the muxer's added
  backpressure), and the shipped cadence eliminates the stalls entirely.
  Stated limits: the rig used the software encoder (deliberately, so it could
  not contend with the two live cameras' VAAPI sessions on the iGPU; the
  stall under test lives in the demux/interleave/mux path, not the encoder)
  and a 960x960 frame; the drop counts are the load-bearing figures.

- **E69 (DONE) `/control/status` carries the DELIVERED rate beside the declared
  one.** `fps` is what ffmpeg was launched with; `measured_fps` is the bridge's
  last settled measurement of what the camera actually delivers, and
  `rate_pending` is a confirmed change waiting for the rotation that applies
  it. The gap between the first two is the diagnostic an operator wants: it
  says "this publisher is padding 20% of its output" before ffmpeg says
  `More than 1000 frames duplicated`, and it distinguishes a camera whose
  *setting* changed from one failing to deliver its configured rate. The Home
  Assistant Stream state sensor surfaces all three as attributes (attributes,
  not entities: none is worth a recorder series). The fields update as frames
  flow, so they lag a reconnect gap; contract in `control-api.md`, pinned by
  the rate end-to-end test and the stream-health suite.

- **E70 (DONE) ffmpeg's `frames duplicated` warnings are counted.**
  `FfmpegSink._log_stderr` counts every stderr line carrying
  `frames duplicated`; `/control/status` serves the lifetime total as
  `duplicated_frame_warnings`, accumulated across publisher rebuilds exactly
  like `dropped_frames`, and the HA Stream state sensor carries it as an
  attribute. It is the encoder's own corroboration of a too-high declaration,
  from the other side of the pipe than RateWatcher's arithmetic, and it makes
  "did the correction actually remove the padding" answerable from a status
  poll instead of container-log archaeology.

  Deliberately **observability only**, for the reasons that got it rejected as
  a control input: it is one-directional (it only ever fires when the declared
  rate is too HIGH, never when it is too low, and too low is the direction
  that destroys frames), it is not quantitative (it says a mismatch exists,
  not what the rate should be), and it is coarse (fixed 1000-frame
  thresholds, so its timing depends on the rate rather than the size of the
  error). Parsing encoder stderr for control decisions is a step worth taking
  deliberately, not in passing; the settled re-measurement owns corrections.

- **E71 (DONE, observed live 2026-08-28) E64's thresholds are argued from measurement, but the mechanism
  has not been observed correcting a real setting change end to end.** The
  hysteresis values, the asymmetry and the settle window are each grounded in
  numbers that exist (the in-container `-framerate` table, the 6/7/15/29 spread
  across restarts, the live 29 fps camera duplicating 1000+ frames) and the
  logic is pinned by tests that were each proven to fail. What has **not**
  happened is the obvious live confirmation: change the live-video resolution
  from the Home Assistant select on a running bridge, and watch the log go
  `measured Nfps against the declared Mfps` → `frame rate changed` → a new
  publish line at the new rate, with `consecutive_failures` still `0`.

  That is a five-minute check against the real deployment and it is the only
  thing that would confirm the *premise*: that a resolution change actually
  changes the delivered frame rate on this platform, rather than only the frame
  size (which the existing `sink_wh` path would already handle on its own). If it
  turns out the rate does not move with the resolution setting, E64 is still
  correct for the restart-to-restart instability it was written for, but the
  headline justification would need rewriting.

  Everything the check needs is now in place: `/control/status` carries
  `measured_fps`, `rate_pending` and `duplicated_frame_warnings` (E69/E70),
  so the whole sequence is readable from two status polls. The procedure: note
  the camera's `fps`/`measured_fps` and its resolution, flip the camera's
  **Live video resolution** select in Home Assistant, wait out one rotation
  (under ~3 minutes) plus the 10 s settle, and read the log and status again;
  then flip it back. The publisher rebuilds for the frame-size change either
  way; the question is only whether `measured_fps` moves with it.

- **E72 (DONE) The E64/E73 thresholds are pinned as values, not only as
  logic.** The rate tests drive the confirmation, hysteresis, storm-bound and
  flap logic against shrunk knobs, which is the right shape for a unit test,
  and it meant a tuning pass could set a shipped constant to a self-defeating
  value with every suite green: `RATE_RESAMPLE_SETTLE_S = 0.0` (re-admitting
  the session-start burst readings the window exists to exclude) and
  `RATE_REBUILD_MIN_INTERVAL_S = 0` both passed everything.

  `test_the_rate_constants_hold_their_meaningful_ranges` now pins each shipped
  constant inside the range that makes it meaningful, with the reason in the
  assertion message rather than the number repeated as a magic value: the
  settle window clears the burst without eating the session; the batch size
  resists bunching while still fitting two batches per session at the slowest
  real camera; the rebuild floor exceeds one rotation; the flap window exceeds
  the rebuild floor (a reversal the storm bound delayed must still be
  recognised); and the flap hold covers the flap window. Proven to fail
  against both of the originally-green self-defeating values. The
  audio-pacing constants were already value-pinned against the muxer lead.

- **E73 (DONE) A delivered rate that flaps with no setting change is resolved
  once, to the higher rate, and held.** The first live night of E64's adaptive
  rate surfaced the case its confirmation gate cannot handle: the 30 fps
  camera's *delivered* rate halves under marginal light and comes back, dwelling
  minutes at each end. Both dwells outlast two settled batches, so both ends
  confirmed, and the log showed the result: a rate rebuild 29 → 15, another
  15 → 28 six minutes later, then a further confirmed 28 → 15 held back only by
  the 300 s floor, re-confirming and re-logging every ~12 s. Left alone that is
  a reader-kicking publisher rebuild each way per 300 s window for as long as
  the light stays marginal, plus enough warning volume to age real warnings out
  of the container's capped log window.

  The guard (`camera_session.RateWatcher`): a confirmed return to the very rate
  the last rate rebuild moved away from, within `RATE_FLAP_WINDOW_S` (30 min)
  of it, is flapping; a real second setting change back inside half an hour is
  indistinguishable, and the ambiguity is resolved in the direction that costs
  nothing irrecoverable. The flap is resolved **to the higher rate** (held high
  the output pads with duplicates; held low it drops real frames, the same
  measured asymmetry behind the hysteresis band) and held there: confirmed
  changes down to the flap's low rate are refused while settled batches keep
  sighting the high one, and for `RATE_FLAP_HOLD_S` (1 h) past the last
  sighting. A real setting change to the low rate stops the sightings, outlives
  the hold, and applies late rather than never. A confirmed change to a rate
  that is *neither* end of the known flap passes through the ordinary gates
  untouched.

  Also fixed alongside, from the same night's log: the storm bound used to
  **discard** a confirmed change it was merely delaying (the candidate was
  cleared before the interval check), so the same evidence was re-derived and
  re-logged as `the frame rate has moved again`, at warning level, every
  second batch for the whole window. The candidate is now kept through the
  hold (the first agreeing batch past the floor acts on it), the hold is
  logged once per pending change at info level, and refusals of a known flap
  are info once, then debug.

  Costs and limits, stated: the first swing of a flap episode still pays two
  rebuilds (down, then the resolution back up); nothing can know the first
  swing will reverse; while held high through a low dwell, ffmpeg logs
  `More than 1000 frames duplicated` (E70's counter would make this a number);
  and a real setting change to a known flap's low rate is delayed up to
  `RATE_FLAP_HOLD_S` plus the dwell pattern. Pinned by two tests proven to
  fail against the pre-guard code (the resolution + refusal path, and the
  expiry path that separates a real change from a flap); the live confirmation
  rides on the same soak E71 is waiting on.

- **E74 (SUPERSEDED by E83, mechanism disproven) The bridge container's memory
  is a plateau, not a leak.** Read the correction at the end of this entry
  before using anything in it. The observation held; the mechanism named here
  was wrong, and the fix it recommended was never tested by this entry. The
  whole-stack watcher reported `bridge-memory-high` fourteen times overnight,
  crossing its 85 percent threshold from 05:10 onward. Measured rather than
  assumed:

  | window | reading |
  |---|---|
  | 06:10, just after a rebuild | 49.9 percent |
  | 06:10 to 08:10 | +26 points |
  | 08:10 to 10:10 | +6.7 points |
  | 10:10 to 12:40 | flat, oscillating 83 to 90 percent |

  Over a separate 30 minute window spanning about eleven session rotations the
  Python process held **flat at 119 threads and 485 to 493 MB RSS**, with no
  trend in either. No OOM kill and no container restart in six hours. So there
  is no thread accumulation across session rotations, which was the more
  worrying of the two hypotheses, and no unbounded growth.

  The shape fits glibc arena behaviour: 119 threads on an 8 core host against a
  default cap of 8 arenas per core, each able to reach 64 MB, reaching a working
  set and stopping. `MALLOC_ARENA_MAX=2` would reclaim headroom and is worth
  applying at the next natural rebuild, but it is an optimisation and not a
  stability fix. Nothing here needs a forced rebuild.

  **The threshold was the defect.** 85 percent sat below this workload's normal
  operating range, so the watcher was reporting healthy operation as a failure,
  which is the fastest way to make an operator stop reading an alert. Raised to
  93 percent with the measurement recorded in the script itself.

  **Correction, 2026-08-26 (E85).** This entry named glibc per-thread ARENA
  COUNT as the mechanism and recommended `MALLOC_ARENA_MAX=2`. It never tested
  that recommendation. E83 did: the cap was applied live with audio consuming
  and changed the growth rate by nothing measurable (333 to 623 MB over 25
  minutes, the same as without it).

  The real mechanism is glibc heap FRAGMENTATION, not arena count. Measured
  with `mallinfo2` across one 330 s window: python RSS grew 118 MB while
  `in_use` grew 3 MB, `free_not_returned` went 35.9 to 144.6 MB, and only
  2.92 MB of that was `releasable`. Freed memory stranded inside fragmented
  arenas, which `malloc_trim` cannot return. The plateau this entry observed was
  that same fragmentation reaching a ceiling at the lower pre-E80 allocation
  churn; when mu-law quintupled the small-allocation rate, the ceiling moved
  above the container limit and the plateau stopped looking like one.

  The fix is jemalloc, preloaded for the python process only (E83).

  **The lesson this entry is kept for:** it inferred a mechanism from the shape
  of a curve, recommended a remedy, closed itself, and never ran the one
  experiment that would have falsified it. A later session then cited it as
  settled fact and built an hour of reasoning on top. A diagnosis that does not
  test its own recommendation is a hypothesis wearing a diagnosis's clothes.

- **E75 (open, minor) The stack watcher is versioned but not tested or
  reviewed.** `ops/stack-watch.sh` is now in the repository rather than existing
  only on the VM, so it can be reviewed and its history is visible. It is still
  shell written quickly against a live system, and it has already produced three
  false positives that had to be fixed by hand: a raw count of unavailable
  entities (about fifteen are permanently unavailable here by design, since no
  camera on this account is privacy capable), `event.doorbell_button_press`
  reading `unknown` when that is an EventEntity's normal resting state before
  its first event, and the audio sensors reading `unknown` on the first poll of
  a camera that already has frames, which the entity documents as honest rather
  than faulty.

  Each was a case of alerting on a state the system defines as normal. Worth a
  pass that checks the remaining conditions against what the entities and the
  control API actually promise, and worth deciding whether it should be
  installed by a documented procedure rather than by hand. The cron entry lives
  in the maintainer's user crontab and is not captured anywhere in the repo.

- **E76 (open) The doorbell's residual frame drops are the audio path stalling
  at each session rotation, not sustained starvation and not normal noise.**
  After E60 was fixed the doorbell still dropped 7 to 10 frames a minute with
  audio consuming, steady over six hours. Two things were assumed about this and
  both were wrong.

  **It is not spread through the session. Every drop is at the handover.** Every
  drop warning in the log falls between `proactive session rotation` and the new
  session's first frame, for example rotation 12:40:18, drop 12:40:23, first
  frame 12:40:24. About 14 frames, roughly 0.9 s of video, once per 165 s cycle.

  **It is not the cost of rotation either.** A controlled A/B on the same camera,
  same resolution, same rate, same build, switching only the audio knobs:

  | | drops per rotation | share of frames |
  |---|---|---|
  | audio off, 15 min, 5 rotations | **0.8** | 0.05 percent |
  | audio on | **~14** | 0.57 percent |

  So the handover transient is inherent, but audio multiplies it about
  seventeenfold. This is a real audio-attributable residual, not hardware or
  cloud-relay noise, and it should not be written off as such.

  **Probable mechanism, not yet confirmed.** The frame hold that bridges the
  rotation gap already knows audio must stay ahead of video or ffmpeg stops
  reading the video pipe, and it tops audio up on its own deadline. But it stops
  the previous session's audio pump as its first act and the next session's pump
  is not started until the hold has finished, deliberately, so that the two
  writers to the audio pipe are mutually exclusive. If the audio timeline stops
  advancing anywhere inside that serialization window, the muxer lead decays,
  ffmpeg stops reading video, and the queue's 533 ms of tolerance at 15 fps
  overflows. That is E60's mechanism confined to the handover.

  **Do not guess at a fix.** The next step is instrumentation, not a change. It
  is now built and is the state of this entry.

  **The window is measurable, from data, one line per rotation.**
  `HANDOVER_TRACE=true` in the bridge's `.env` (off by default, see
  `handover_trace.py`) makes every proactive rotation emit
  `handover trace {...}`, everything after the prefix being one JSON object.
  Read them with

  ```
  docker logs telus-cam-bridge 2>&1 | grep 'handover trace' \
    | sed 's/.*handover trace //' | jq .
  ```

  Each record carries the window as an ordered list of marks -- `rotation`,
  `pump_stopped`, `hold_first_silence`, `session_retired`, `batch_collected`,
  `hold_stopped`, `publisher_ready`, `pump_created`, `first_write`,
  `batch_written`, `pump_first_write`, `settled` -- and on each of them `dt`
  (wall clock since the previous mark) beside `adv` (audio actually written to
  ffmpeg since it), the muxer `lead`, and the video queue depth. `worst_gap`
  names the interval where the timeline fell furthest behind and by how much;
  `lead_min_ms`/`lead_min_at` say whether the lead reached zero and where;
  `drops` lists every dropped frame in ms from the rotation, split between the
  retiring session and the new one. The serialization window this entry is about
  is `hold_stopped` to `pump_first_write`.

  So the hypothesis is now falsifiable from a record: it predicts
  `worst_gap.from = hold_stopped`, `worst_gap.to = pump_first_write` with
  `adv: 0`, a negative `lead_min_ms`, and `drops.new.at_ms` inside that same
  interval. A record showing the timeline tracking the wall clock all the way
  through, with the drops somewhere else, **refutes** it, and the marks say where
  to look instead. Field-by-field reading in
  `docs/troubleshooting.md`.

  **What it costs.** Off (the default), the whole mechanism is one `is not None`
  test per frame: 6.4 ns against a 66.7 ms frame interval, no allocation, and
  nothing inside `write_frame`. On, a traced rotation costs ~70 microseconds of
  CPU and ~1.5 kB of log, once per 165 s per camera. Nothing it does awaits,
  sleeps or writes, and it schedules no sampler of its own, deliberately: a
  sampler running on the event loop during the very window being measured would
  perturb it.

  **What it still cannot see, so a reading is not over-claimed.** The lead is
  what the BRIDGE has handed ffmpeg, not what ffmpeg's muxer has consumed; the
  muxer's own state is not observable from this process (the same limit E68
  records for the pacing measurements). A stall is located between the two marks
  that bracket it and no more finely. And a rotation that also rebuilds the
  publisher gets a fresh audio pipe with a fresh 100 ms prime, so those records
  (`sink_rebuilt: true`) are not comparable with ordinary ones.

  **Not yet run against the live doorbell.** The instrumentation is exercised by
  `test_camera_session.py` against the offline fakes; no record from the real
  camera has been read yet. Doing that, with audio consuming and against an
  audio-off control on the same camera, is what closes this entry.

  **Severity, honestly.** 0.57 percent against a 0.05 percent floor, bounded, not
  growing over six hours, and invisible to viewers because the frame hold keeps
  the publisher emitting. Worth fixing properly, not worth destabilising a system
  that is finally healthy.

  Two side findings worth keeping. The video drop path logs the first drop of a
  session and then only every 150th, while `/control/status` accumulates across
  sessions, so a session dropping 14 never reaches a second log line: **the log
  understates the drop rate by design** and reading it alone suggests one drop
  per rotation. And the living room camera's zero drops are not health, they are
  slowness: at 5 fps the 4-frame queue floor holds 800 ms where the doorbell's
  8-frame ceiling holds 533 ms, so it absorbs a handover the doorbell cannot.

- **E77 (open, minor) What the handover instrumentation does not cover.** Four
  deliberate limits of E76's `HANDOVER_TRACE`, recorded so they are choices
  rather than surprises. None of them blocks reading a live record; each is a
  small, separable improvement if a reading turns out to need it.

  **It traces PROACTIVE rotations only.** A session that fails and reconnects
  goes through the same handover -- the same frame hold, the same audio-pump
  swap -- and gets no record, because the trace opens at the two `proactive
  session rotation` breaks. Failures are rare on a healthy camera and already
  log a reason, so this keeps a record meaning exactly one thing; if a failure's
  handover ever needs measuring, opening a trace on the failure path too is a
  two-line change.

  **A record open when the camera task is cancelled is dropped**, not emitted.
  The final teardown stops the hold and the pump without closing the record. A
  camera being deactivated mid-rotation has louder news in the log, and emitting
  from inside a `CancelledError` unwind is not something to add for a
  diagnostic's sake.

  **Drop timestamps are bounded** at `_DROP_TIME_MEMORY` (256) per session. Past
  that the record's `in_window` count is a floor rather than the count. The
  `session_total` beside it is exact, so the two together always say when the
  list has been cut.

  **It is log-only, and that is argued rather than assumed.** The case for also
  serving the last record from `/control/status` is real: an operator (and Home
  Assistant) could see it without shell access, which is how E71's procedure was
  made checkable. It loses on three points. A rotation is an EVENT and a status
  poll is a snapshot, so with 165 s rotations against a much faster poll each
  record would be served many times over and a reader would have to diff `at` to
  tell repeats from news. The per-camera status dict is a documented contract the
  integration parses on every poll and derives entities from, and a ~1.5 kB
  nested blob that is null whenever the flag is off costs that contract more than
  it buys. And a record is only interpretable NEXT TO the `proactive session
  rotation` and `dropped N frame(s)` lines it belongs with, which exist only in
  the log. If the live reading does show a need for it, the right shape is a
  small summary (the last rotation's `worst_gap.ms`, `lead_min_ms` and drop
  count) rather than the whole record.

- **E78 (DONE) The living room camera has a microphone; it was switched off
  upstream, and TELUS enabled it.** This entry exists because the previous claim
  was wrong and was repeated in several places: that the ADC-V522IR "cannot take
  audio". What was actually observed is that requesting audio on it produces
  zero video frames and a first-frame timeout. That shows the session dies, not
  that the hardware lacks a microphone.

  Its settings say otherwise. Living room (ADC-V522IR, fw `IP8168-ALAM-0100g4`)
  reports `areMicrophoneAndSpeakerOn = False`, `isAudioRecordingOn = False`,
  `canToggleAudioRecording = False`, `supports_call_volume = False`. The doorbell
  (ADC-VDB750) reports `True`, `True`, `True` and `callVolumeFeatureSupport = 2`.
  So audio is disabled for that device at the account level. A camera whose
  microphone is off being asked for an audio-enabled offer, and failing the whole
  session, fits the observation at least as well as missing hardware.

  **Trap, and the reason this entry leads with it: `hasBuiltInMicrophone` is
  `False` for BOTH cameras**, including the doorbell whose microphone is
  streaming AAC into the RTSP path continuously. The field is not populated
  meaningfully on this account. Do not use it as evidence in either direction.

  Not switchable from here. `microphoneAndSpeakerFeatureSupport` is `0` on both
  cameras, and the operator confirmed on 2026-08-25 that no audio control exists
  in either the TELUS app or the alarm.com portal for this camera. A capability
  that is enabled on one device while its own feature-support flag reads "not
  exposed" is provisioned by the provider, not set by the customer. The account
  already reports `hasCustomerAcceptedAudioAgreement = True`, so consent is not
  the blocker.

  **Next step is a support call, not code:** ask TELUS to enable microphone and
  audio on the ADC-V522IR, noting the ADC-VDB750 on the same account already has
  it, so this is per-device provisioning. Be ready for "not offered on that
  model", which is a real answer and would close this.

  Deliberately NOT attempted: writing `mic_and_speaker_on` through the API to see
  if the server accepts it. That is a write to a live account for a question a
  phone call answers, and the operator chose to proceed video-only on
  2026-08-25. If it is ever tried, expect the camera to go dark and revert
  immediately. One avenue is also unfinished: reading the portal's JS bundles to
  see what gates the microphone control would say whether it is dealer-only, and
  the asset path 404s on the obvious URL.

  Consequence today: the living room records video only. If audio is ever
  enabled it is a Frigate config change plus a renegotiation, not a rebuild, and
  recordings already on disk stay valid.

- **E79 (DONE) Frigate records to the NAS over virtiofs, not into the VM image.**
  Recordings previously would have landed in `/home/bungee/frigate/media` on the
  VM's qcow2 disk, which had 16 GB free. They now go to a btrfs RAID1 pool on the
  NAS, reached over virtiofs.

  Shape of it: a `nvr` share pinned to the pool with `shareUseCache="only"` so
  Unraid's mover can never migrate footage onto the array (which is 99 percent
  full), `nodatacow` set on the directory so it is inherited by every file
  Frigate creates, a `<filesystem>` device plus shared `memfd` memory backing in
  the VM XML, an fstab entry with `nofail` so a NAS problem cannot leave the VM
  unbootable, and the Frigate compose bind repointed. `/config` and the SQLite
  database deliberately stay on local disk.

  Recording is event-driven: `continuous.days` and `motion.days` both 0, alerts
  and detections retained 30 days. The doorbell carries
  `preset-record-generic-audio-copy`, because the default `preset-record-generic`
  ends in `-c copy -an` and would have silently dropped the audio the whole
  E60/E62 effort existed to obtain. Verified in a written segment: doorbell is
  `h264` plus `aac` 48 kHz mono, living room `h264` only (see E78).

  **The trap, which cost the only real downtime: `virtiofsd died unexpectedly`.**
  On Unraid, `/usr/local/sbin/virtiofsd` is not the binary, it is a 116 byte
  wrapper that runs a PHP helper to rewrite libvirt's arguments and then execs
  `/usr/bin/virtiofsd`. Its default options include `--syslog`, which is why
  `/var/log/libvirt/qemu/<domain>-fs0-virtiofsd.log` is **empty** and libvirt only
  reports the unhelpful `qemuVirtioFSSetupCgroup: virtiofsd died unexpectedly`.
  The real error is in `/var/log/syslog`:
  `Error entering sandbox: PivotRoot(EINVAL)`. virtiofsd's default sandbox uses
  `pivot_root`, which fails on this mount topology.

  Fix: create `/etc/libvirt/virtiofsd.opt` (Unraid reads it in place of its
  built-in defaults, one option per line) containing `--syslog`,
  `--sandbox=chroot` and `--announce-submounts`. `/etc/libvirt` is a btrfs loop
  mount from `libvirt.img`, so it persists across reboots. Confirmed by running
  `virtiofsd --sandbox=chroot --shared-dir=... --socket-path=...` by hand first:
  it prints `Waiting for vhost-user socket connection...` instead of the sandbox
  error.

  **Operational note that will bite someone: once a manual `<filesystem>` device
  is in the XML, edit that VM in Unraid's XML view only.** The Form view can drop
  manual additions. A backup of the pre-change XML is on the flash drive.

- **E78 resolution, 2026-08-25.** TELUS enabled two-way audio on the ADC-V522IR
  server-side. Their words: it is a server-side setting a previous technician had
  not set up, because their newer cameras usually do not support it. The
  diagnosis in this entry was right and the `hasBuiltInMicrophone` field was as
  meaningless as recorded.

  The flags moved as predicted: `areMicrophoneAndSpeakerOn` False to True,
  `microphoneAndSpeakerFeatureSupport` 0 to 2, `supports_call_volume` False to
  True. The camera then negotiated audio immediately, where before it had
  delivered zero frames and timed out.

  **Three things that would each have cost a session to rediscover.**

  **1. Enabling it was not enough. The portal toggle had to be cycled.** With the
  provisioning in place the camera offered `audio(port=9,opus)`, the track was
  consumed, `webrtcup` succeeded and MediaMTX published two tracks, while
  `decoded_audio_frames` stayed at **0** and `audio_format` stayed **null**
  across five rotations and fifteen minutes. Toggling Microphone/Speaker off and
  back on in the alarm.com portal fixed it on the next session. So the platform
  can report audio enabled while the camera is still not capturing.

  **2. A hypothesis recorded here was wrong, and the wrong version should not be
  repeated.** The obvious suspect for the silence was `isAudioRecordingOn`, the
  one flag still `False` where the doorbell reads `True`, with
  `canToggleAudioRecording` also `False`. It is **not** the gate: audio flows now
  with that flag still `False`. Whatever `isAudioRecordingOn` governs, it is not
  live-stream audio, and a second support call asking for it would have been
  wasted.

  **3. The technician's change silently reset the live video resolution.** The
  camera came back publishing 640x360 where it had been 1280x720, which looked
  like audio costing resolution and was not. `live_resolution` had been reset to
  the low option; setting it back in the portal restored 1280x720 on the next
  renegotiation, exactly as E34 says that setting behaves. **After any
  provider-side change to a device, re-check its other settings**, because a
  technician editing server-side config is exactly when unrelated values move.

  Both cameras now record with audio: verified in written segments as `h264` plus
  `aac` 48 kHz mono. The living room's Frigate input was moved to
  `preset-record-generic-audio-copy` for the same reason as the doorbell's (E79).
  That 48 kHz is the rate the bridge published while it was Opus-decoding a
  µ-law stream; with **E80** it becomes 8 kHz mono, which is the rate the cameras
  actually send. The segments still carry `h264` plus `aac`, which is what the
  `-c copy` recording preset needs.

- **E80 (DONE in the bridge, awaiting live validation) The audio track is
  advertised as Opus and carries G.711 mu-law, so ~94% of it was being
  discarded.** Downstream audio from both cameras arrived as short fragments
  separated by silence, which sounded like speech skipping around rather than
  like an outage, while every health signal stayed green: `state: streaming`,
  `audio_consumed: true`, `audio_dropped: false`, and `decoded_audio_frames`
  climbing.

  **Measured.** Over 60 s the doorbell decoded **181** audio frames and the
  living room **268**, where 20 ms audio frames are 50/s. So roughly **6 to 9
  percent** of the published audio timeline was real audio and the rest was the
  sink's silence fill.

  **The cause is a codec mislabel, not a decoder fault.** The Janus offer
  advertises `audio(port=9,opus)`. The bytes are **G.711 mu-law at 8 kHz**.
  aiortc believes the advertised codec, tries to Opus-decode mu-law, and
  `aiortc_opus_patch.py` discards what libopus rejects to keep the decoder
  thread alive (E59). The fraction that happens to satisfy a TOC byte decodes as
  noise, and that fraction is what was getting through.

  Four independent lines of evidence, recorded so none of this is re-derived:

    * **The discarded payloads.** The doorbell's head is
      `7effff7f7efe7ffffffffefefeff7efe`: alternating `0x7e`/`0x7f`/`0xfe`/`0xff`
      is mu-law encoding near-silence, which is what a doorbell microphone
      produces nearly all the time. The living room's is
      `332038a8a9bd2223989e1e289e9f3a1d`, ordinary varying audio as mu-law.
    * **The official Android app, instrumented.** It instantiated **no audio
      codec at all** (only `video/avc` and `video/raw`), which is what G.711
      decoded in software looks like.
    * **`AudioFlinger`'s own numbers from that run.** `notificationFrames to
      160` is 20 ms at 8 kHz. Two `AudioTrack` sessions delivered 252,160 and
      212,480 frames: 31.5 s and 26.6 s at 8 kHz, matching two roughly
      30-second viewings. At 48 kHz they would be 5 s, i.e. the app would be as
      broken as the bridge was, and it is not.
    * **The device is mu-law in both directions.** Talkdown to this doorbell
      works as mu-law at 8 kHz, and L16 is impossible on it (E4).

  **Fixed by sniffing rather than by believing the SDP.** Each session decodes
  its first `SNIFF_PACKETS` (20) packets with **both** candidate decoders,
  commits to one for that session's life, and logs the decision once. The
  committed decoder is aiortc's own `g711.PcmuDecoder`, not a hand-rolled mu-law
  table, so its frames carry the `pts`, `time_base`, format and layout the rest
  of the pipeline expects.

  Four design choices, each deliberate:

    * **Decide once, not per packet.** Trying both decoders forever pays for two
      decodes on every packet and can flap mid-stream, and a flap here is not
      cosmetic: a frame in the other codec's shape (48 kHz stereo against 8 kHz
      mono) trips `write_audio`'s format guard and rebuilds the publisher,
      kicking every RTSP reader, once per rotation forever.
    * **Emit nothing until committed, and HOLD rather than drop.** One spurious
      Opus frame ahead of a mu-law stream is enough to size the ffmpeg audio
      input wrongly, so the sniff window's frames are buffered and the winner's
      are delivered at the commit. Measuring costs no audio.
    * **Opus wins ties, wins when nothing decoded, and wins whenever libopus is
      reading `OPUS_CONFIDENCE` (0.75) of the sample.** Only a stream libopus
      largely *cannot* read is re-labelled, so a camera that really does send
      Opus is unaffected, and a stream neither decoder can read degrades to
      exactly the previous behaviour: discard the packet, keep the decoder
      thread alive, keep publishing video.
    * **20 packets** is ~400 ms at 20 ms framing: wide enough that the decision
      cannot be luck, short enough to land before the ten-video-frame batch
      (~670 ms at 15fps) that sizes the publisher's audio input.

  **Counters, because one log line per session is exactly how this hid.** E59's
  drop line is throttled to once per session by necessity, and that made a
  *wholesale* mismatch read like an occasional glitch for two days.
  `/control/status` now serves a process-wide `audio_decoder` block
  (`decoded_opus`, `decoded_pcmu`, `discarded`, `sessions_opus`,
  `sessions_pcmu`, plus the two constants) in which **every packet lands in
  exactly one bucket**, and each camera gets an `audio_codec` field naming the
  decoder its frames came from. Process-wide is a limit rather than a choice:
  aiortc builds its decoder inside `decoder_worker`, a thread no
  `RTCRtpReceiver` keeps a reference to, so a count cannot honestly be
  attributed to a camera; the per-camera field is derived from the decoded
  frames' shape instead, which identifies the decoder exactly.

  **The sample rate changes with it**, from 48 kHz stereo to 8 kHz mono. Nothing
  in the pacing was rate-specific and this was verified rather than assumed: the
  100 ms prime, the 0.1 s per-call ceiling and `write_silence_to`'s fill are all
  sample counts derived from the observed rate, so the muxer lead E60 depends on
  is unchanged, and the top-up cadence is a time and therefore rate-independent.
  How the publisher is sized for the new shape is **E81's**, not this entry's:
  sizing it from the SDP's 48 kHz stereo shape until a frame arrived is what
  livelocked a sparse-audio camera. It is now built video-only until a real
  frame has been decoded and gains its audio input at a rotation.

  **What to expect live, and it is self-verifying.** `decoded_audio_frames`
  should climb at roughly the packet rate rather than at 3/s: about **50 frames
  per second per consuming camera**, i.e. ~1500 across a 30 s poll, and the
  fraction of the audio timeline that is real audio should go from ~6% to
  essentially 100%. `/control/status` should show `audio_codec: "pcmu"` and
  `audio_format: "8000Hz/1ch/s16"` on both cameras, `audio_decoder.discarded`
  flat at or near zero, and `sessions_pcmu` climbing by one per camera per
  rotation while `sessions_opus` stays at 0. One `committing this session to`
  line per session per camera.

  **Not verified from here, and worth knowing:**

    * **The packet rate itself.** The 6-to-9-percent figures come from decoded
      frames per second against an assumed 50 packets/s. E59 logged a `len=512`
      payload, which at 8 kHz mu-law is 64 ms and would imply ~15.6 packets/s
      and a higher accidental-Opus rate. Either way the sniff's margin holds,
      but the exact discard fraction is only pinned once
      `audio_decoder.discarded` is read live against `decoded_pcmu`.
    * **The RTP clock.** `PcmuDecoder` reads the RTP timestamp on a 1/8000 time
      base; whether the gateway stamps at 8 kHz or at the advertised 48 kHz is
      unknown. Harmless either way, because nothing downstream reads the `pts`:
      the ffmpeg audio input carries no `-use_wallclock_as_timestamps` and its
      timeline is its own sample count.
    * **8 kHz AAC downstream.** The publisher now encodes AAC at 8 kHz. AAC-LC
      supports it and ffmpeg accepts it, but that HA's HLS worker and Frigate's
      `-c copy` preset are as happy with 8 kHz as with 48 kHz has not been
      confirmed on the live stack. First thing to check after deploying: that
      recorded segments still carry an `aac` track and that HA still plays it.
    * **Why the gateway mislabels the track.** Still unknown, and it does not
      need to be known: the bridge decodes what arrives. The plausible reading
      is that the relay forwards the device's native mu-law without transcoding
      while advertising the codec its browser player negotiates.

  **Deferred, recorded rather than lost:**

    * **`audio_codec` is surfaced by the Home Assistant integration** as an
    attribute on each camera's Audio state sensor, passed through verbatim
    (null until a frame decodes, null on a bridge too old to report it, pinned
    by a test). Still open: `flowing` compares the counter against itself, so a
    fragment stream reads `flowing` exactly like a whole one.
      `Audio state` sensor (E61) carries `audio_format` as an attribute but not
      the codec, and its `flowing` state compares a counter against itself, so a
      fragment stream reads `flowing` exactly like a whole one. Adding the
      attribute, or a "frames per second" derivation that could tell them apart,
      is an integration change (and a version bump) rather than a bridge one.
    * **The counters are process-wide.** Per-camera attribution would need
      either a change upstream in aiortc (a receiver that keeps a handle on its
      decoder) or a decoder built by this bridge instead of by
      `decoder_worker`. Neither is worth doing for a counter; the per-camera
      `audio_codec` covers the question that actually gets asked.
    * **`AUDIO_DEFAULT_FORMAT` was left as the SDP's shape**, on the reasoning
      that it is what an unknown camera's SDP claims and that the mismatch
      costs one rebuild. That reasoning was wrong in a way this entry could not
      see: the rebuild it costs is reached by FAILING the session, and the
      restart destroys the per-session sniff. **E81** removes the constant
      rather than retuning it.

- **E81 (DONE in the bridge, awaiting live validation) A camera whose audio is
  sparse cannot finish the codec sniff before the sink is sized, and livelocks.**
  Found by deploying E80. The doorbell, whose audio is continuous, committed to
  mu-law in about two seconds and has run correctly since. The living room did
  not: it failed every session with
  `RuntimeError: audio input needs rebuilding, restarting sink`, reconnected,
  and started over, never converging. It had no publisher at all until audio was
  switched back off for that camera.

  The sequence: `camera_session` sizes the ffmpeg audio input from a peek at the
  first ten video frames. The sniff needs 20 audio packets, which at 64 ms
  packetisation is 1.28 s of *continuous* audio. A quiet room sends far less than
  that in the window, so the peek sees no decoded frame, the sink is built at the
  `AUDIO_DEFAULT_FORMAT` 48 kHz shape, the first real 8 kHz frame then demands a
  rebuild, the rebuild raises from the streaming loop, the session restarts, and
  the sniff begins again from zero. Each restart throws away the sniff progress
  that would have ended it.

  **The load-bearing detail is that the failure mode is a loop, not a stall.**
  Any fix must make progress survive a restart, or must stop an audio-format
  change from failing the session at all.

  Not the sniff length. Shortening it would make the race less likely without
  removing it, and would weaken the codec decision that E80 deliberately made
  strong. `SNIFF_PACKETS` and `OPUS_CONFIDENCE` are unchanged.

  **Fixed by removing the guess, not by winning the race.** Three changes, and
  the first is the one that matters:

    1. **The audio input is sized from an OBSERVED decoded frame or not built at
       all.** A camera with nothing decoded yet gets a **video-only** publisher.
       `AUDIO_DEFAULT_FORMAT` is deleted rather than retuned: while the
       publisher could be built from a shape nobody had seen, something had to
       correct it later, and the correction was the loop. There is now nothing
       to be wrong about. The shape is still remembered across rotations (it is
       a property of the device, not of a session) and still re-checked against
       every decoded frame, so a device that really changes shape is noticed on
       its first frame rather than trusted from an old session.
    2. **An audio-format change never fails a session.** The publisher is
       rebuilt at the next rotation, through the identical path a confirmed
       frame-RATE change takes (E64), where the frame hold already bridges the
       gap. Until then a frame in the wrong shape is skipped and its span filled
       with silence, so the muxer lead the VIDEO pipe rides on (E60) is
       untouched. The per-session audio pump now runs for a video-only publisher
       too: it is what observes the shape, and it keeps the decoded-audio queue
       drained.
    3. **Format rebuilds are counted and bounded**
       (`AUDIO_FORMAT_REBUILD_LIMIT`, 1 per camera per process). Past the bound
       the audio input is dropped in place and the camera publishes video only
       for the rest of the process, exactly as E62 already degrades a wedged
       audio pipe. This is what stops a *different* loop: if the sniff commits
       to a different codec on different sessions, an unbounded repair would buy
       a reader-kicking publisher teardown once per rotation forever.

  **Why not the other two directions.** Carrying sniff state across sessions is
  the one the entry called most direct, and it cannot be done honestly from
  where the state lives: aiortc builds its decoder inside `decoder_worker`, a
  thread that has no idea which camera it belongs to, so per-camera carry-over
  would have to be process-wide, which is exactly the "re-check if the device
  changes" hazard the entry warned about. The FORMAT is carried instead, which
  is the property the publisher actually needs, is already per camera, and is
  re-checked on every decoded frame. Bounding rebuilds alone would have left the
  publisher still being built from a guess, i.e. still one wrong guess away from
  the loop, so it ships as the backstop rather than the fix.

  **What it costs, stated rather than hidden.** A camera whose sniff does not
  finish inside the sizing window now publishes video-only for up to one
  rotation (~165 s) at process start before its audio appears, where the shipped
  code took one failed session (~2 s) to get there on a camera with continuous
  audio. Audio arrives later on the doorbell in exchange for arriving at all on
  the living room. A per-camera format cache that survives a restart would
  remove even that; recorded as **E82** rather than built, because it adds
  persistent state to remove a one-rotation delay.

  **Pinned by tests**, in `camera-bridge/tests/test_camera_session.py`:
  `test_sparse_audio_converges_instead_of_looping_on_a_rebuild` (the live
  sequence: two publishers across many sessions, none failed),
  `test_a_camera_that_never_sends_audio_publishes_video_forever` (the acceptance
  case: one publisher, no audio input, no failure, forever) and
  `test_audio_format_rebuilds_are_bounded_and_degrade_to_video_only`. The last
  one is also the closest **reproduction of the live loop**: with the shipped
  design restored (a guessed shape plus a session-failing repair) it goes red
  with `2 session(s) failed while the audio shape swung about`, which is the
  live signature. All three were proven to fail against deliberately broken
  source, one invariant at a time.

  **Not verified from here.** The exact live mechanism of the forever-loop. Read
  alone, the shipped code should have converged after ONE failed session, since
  the learned shape persists across rotations: the reading that fits "failed
  every session" is that the codec sniff decided differently on different
  sessions, so each session's guess met the other codec's frames. That is
  consistent with sparse audio (the 20 sniffed packets are whatever happened to
  arrive) and it is what the bounded-rebuild test scripts, but it was not
  confirmed against the live camera before audio was switched off for it. Either
  way the fix does not depend on which it was: the publisher is never sized from
  a guess and no audio-format change can fail a session, so both readings are
  removed. What to watch when audio is re-enabled on the living room camera:
  `publisher_audio_format` going from `null` to `"8000Hz/1ch/s16"` exactly once,
  `audio_format_rebuilds` reaching 1 and stopping, no `camera session failed`
  lines for that camera, and `sessions_pcmu` climbing while `sessions_opus`
  stays at 0 -- if `sessions_opus` climbs at all, the flapping-decision reading
  was right and it is worth reading the commit lines' counts.

- **E82 (open, minor) What E81 deliberately did not do.** Four things were
  considered while fixing the sparse-audio livelock and left out on purpose.
  Recorded so none of them is mistaken for an oversight, and so the reasoning is
  available if one of them ever becomes worth doing.

    * **The learned audio shape does not survive a process restart.** It is
      per camera and per process (`known_audio_fmt`), so every container restart
      re-learns it, which costs one rotation (~165 s) of video-only publishing
      per consuming camera on a camera whose codec sniff cannot finish inside
      the ~670 ms sizing window. Persisting it beside `restart_guard.py`'s state
      file would make audio appear on the first session instead. The cost is
      persistent state and a staleness question: an entry would have to be keyed
      by something that changes when the device does (model, firmware, MAC) and
      would still need the first decoded frame to be authoritative over it. A
      wrong cached shape would cost exactly one bounded rebuild, never a loop,
      so the risk is small; the payoff is one rotation at start-up, so the work
      is not obviously worth it either. Deliberately not built.
    * **A camera whose sniff never completes within a single session never
      publishes audio.** The decision needs `SNIFF_PACKETS` (20) packets and the
      decoder is rebuilt with each session, so a camera sending fewer than 20
      audio packets per ~165 s session decodes nothing, ever. It publishes video
      forever and nothing fails, which is the correct degradation, but it is a
      real limit and it is invisible except as `decoded_audio_frames: 0` with
      `audio_consumed: true`. Fixing it means carrying sniff state across
      sessions, which cannot be attributed to a camera from inside aiortc's
      decoder thread (see E81), so it would need a decoder this bridge builds
      itself. Not known to affect any camera on this account: the living room
      camera decoded 268 frames in 60 s when it was measured (E80).
    * **The publisher is not rebuilt mid-session to add audio.** The rebuild
      waits for the next rotation, where the frame hold already bridges the gap
      and the two-writer exclusion is already established. Doing it mid-session
      would shorten the wait to a few seconds but would add a second place that
      builds sinks, inside the loop whose comments enumerate the bugs that have
      already shipped there once each. Deliberately not attempted.
    * **`decoded_audio_frames` counts frames the publisher could not take.** It
      is "this camera decoded audio", not "that audio reached the RTSP path", so
      during the one rotation between learning a shape and rebuilding for it, it
      climbs while the stream still carries no audio. `publisher_audio_format`
      is what distinguishes them, and the HA **Audio state** sensor (E61) does
      not read it, so that sensor says `flowing` for that rotation. A sensor
      change plus a version bump; deferred, not forgotten.

- **E83 (DONE) With audio consuming, the CONTAINER grows about
  12 MB per minute and does not plateau. Which of its four processes grows is
  not known.** Found by the stack watcher, not by looking, and it had already
  taken the bridge to `bridge-unreachable` once before it was noticed.

  **Attribution is from the watcher's own history, not from reasoning.** Before
  v1.24.0 the container climbed to 81 to 85 percent of its 768 MiB limit and
  **plateaued**, exactly as E74 measured and closed. After v1.24.0 deployed at
  17:49 it went 41.6, 65.3, 77.5, 84.8, **99.10** percent and did not stop, and
  logged `bridge-unreachable` at 19:25. A later 13 minute curve measured
  496 MB to 651 MB, **+155 MB**, while `sessions_pcmu` went 10 to 20, with
  thread count flat at 119 throughout, so this is not the accumulation E74
  ruled out.

  **That 13 minute curve is a RATE, and reading it as "15 MB per session" was
  an over-claim.** Ten rotations happened inside it because ten rotations
  happen in thirteen minutes with two cameras on a 165 s cycle; the reading is
  equally consistent with a climb proportional to elapsed time, to decoded
  audio frames, or to rotations, and it does not distinguish them. Stated
  honestly it is **12 MB per minute**, and everything below is written against
  that.

  **It scales with audio volume, which is why it appeared exactly here.** E80
  took real decoded audio from about 3 frames per second to about 15, so
  whatever retains a per-session reference now retains five times as much. At
  roughly 2,475 audio frames per 165 s session, 15 MB works out near 6 kB per
  frame, which is the right order for a retained `av.AudioFrame`. That
  arithmetic is what the offline rigs below were aimed at, and none of them
  found such a retention -- five times the audio also means five times the
  allocation CHURN through the decoder and executor threads, which raises a
  glibc arena's high-water mark without anything being retained at all.

  Audio consumption is **off** on the live deployment as a result. Both cameras
  publish video.

  **The control, stated precisely, because a first reading of it was wrong.**
  With audio off the container is NOT flat: over fourteen minutes it went 343 MB
  to 391 MB, about **3.7 MB per minute**, which is the ordinary warm-up toward
  the plateau E74 measured. With audio on it went 496 MB to 651 MB, about
  **12 MB per minute**, and did not plateau. So audio roughly triples the growth
  rate and removes the ceiling; it does not introduce growth where there was
  none. An earlier note in this entry called the audio-off case flat on the
  strength of four samples spanning four minutes. It was not, and the same
  short-window error produced two other wrong readings in the two days before
  this. **Take a control over at least one full plateau, and say how long the
  window was.**

  **THE NUMBER IS A SUM OVER FOUR PROCESSES, and nothing had ever decomposed
  it.** `entrypoint.sh` runs `mediamtx` and `python3 -m bridge.main` side by
  side, and the bridge launches **one ffmpeg per camera**. All four count
  against docker-compose's `mem_limit`, so the cgroup figure the watcher alerts
  on, and every number in this entry, is their total. "The mu-law audio path
  leaks" -- what this entry was originally titled -- is a claim about the
  Python process that no measurement here has ever made. It is equally
  consistent with:

    * **MediaMTX**, which with audio on carries a second (AAC) track per stream
      and repacketises it for every reader. Written in Go, so its RSS climbs and
      is returned to the OS lazily whatever it does.
    * **ffmpeg**, whose two-input mux buffers one stream's packets to interleave
      them with the other's, bounded only by `-max_interleave_delta` (10 s by
      default). With audio off there is one input and nothing to interleave.
    * **The Python heap**, retaining a decoded frame per packet.
    * **glibc**, holding freed memory in per-thread arenas rather than returning
      it -- which is precisely what E74 identified as this container's plateau,
      at 119 threads. E80 took real decoded audio from ~3 frames/s to ~15, i.e.
      five times the allocation churn through the decoder and executor threads,
      which would raise the arenas' high-water marks without any object being
      retained at all. That is a *higher plateau*, and thirteen minutes cannot
      tell a higher plateau from no plateau.

  Three of those four are not fixable from this repository.

  **Five offline rigs failed to reproduce any of it, and that is evidence, not
  an absence of it.** Each ran the real code with the real audio rates, drove
  many rotations or many minutes, and measured RSS and `gc` object counts by
  type after a forced collection:

  | rig | what was real | result |
  |---|---|---|
  | the decoder patch alone | the real patched `OpusDecoder` and `PcmDecoder`, 2,475 mu-law packets x 8 sessions, all committing to mu-law | **+0.02 MB/session**, object count flat |
  | `janus_client` | the real `JanusCameraSession` audio path (`_pump_audio_track` -> the bounded queue -> `next_audio_frame` -> `close()`) fed real decoded `av.AudioFrame`s, 12 sessions | **+0.01 MB/session**, object count flat |
  | `camera_session` | the real `run_camera_forever` with audio consuming, 55 rotations | **+0.055 MB/rotation**, object count flat within noise |
  | the publisher | the real `FfmpegSink` driving a REAL ffmpeg child (output swapped to `-f null -`), real 1440x1440 frames at 15 fps plus real decoded audio at 15/s, 3.7 minutes | **python +0.00 MB/min, ffmpeg +0.04 MB/min** |
  | aiortc | real `RTCPeerConnection` pairs with a real audio track, created and closed once per rotation, 40 rotations | plateaus at ~140-150 MB with audio and ~150 MB **without**; the warm-up is the same shape either way |

  So the Python bridge retains no per-session and no per-frame object across
  rotations, and the pinned test `test_consumed_audio_retains_nothing_across_rotations`
  now holds it to that. **The sniff's held frames are disproven** as the cause
  by the first rig (and were already two orders of magnitude too small, being
  bounded at 20 packets, ~120 kB per session), and so is a module-level
  structure in the decoder patch keyed by decoder instance: there is none, the
  sniff state is an attribute on the decoder instance and dies with it.

  **Measure, do not guess, and the cheapest measurement needs no code at all.**
  One `docker exec` decomposes the sum:

  ```
  sudo docker exec telus-cam-bridge sh -c 'for p in /proc/[0-9]*; do \
    echo "$(cut -d" " -f2 $p/statm) $(cat $p/comm)"; done | sort -rn'
  ```

  Repeated ten minutes apart and subtracted, that says which process climbs,
  and three of the four answers end the search in this repository immediately.
  **Do that before anything else.**

  Past it, `MEMORY_TRACE` (off by default, `bridge/memory_trace.py`) emits one
  JSON line every `MEMORY_TRACE_INTERVAL_S` (330 s = two rotations, so
  consecutive records always bracket a whole rotation per camera) carrying, in
  the order the questions have to be asked: every container process with its
  RSS; glibc's `mallinfo2`, which separates "the heap is full" from "the
  allocator is holding freed memory" and which `tracemalloc` structurally
  cannot see; the `gc` object types that grew; and the allocation sites that
  grew, ranked by GROWTH rather than by size. It is not safe to leave on --
  `tracemalloc` roughly doubles per-allocation cost and adds a frame per live
  allocation, so it changes the number under investigation; read
  `python.rss_mb` from the record and never `tracemalloc.traced_mb`. Reading
  guide: `docs/troubleshooting.md`, "Attributing the container's memory".

  This entry exists because the last two audio problems were both solved by
  measurement and both nearly solved wrongly by inference, and its own first
  title is now the third example.

  Related: the doorbell was also dropping 34 to 970 video frames per five minute
  window through this period against a 7 to 10 per minute baseline. That is
  almost certainly the memory pressure slowing the consumer rather than a second
  defect, but it has not been separated and should not be assumed.

- **E84 (open, minor) What the E83 memory instrumentation does not do.** Five
  things were considered while building `memory_trace.py` and left out on
  purpose. Recorded so none of them is mistaken for an oversight, and so the
  reasoning is available if one becomes worth doing.

    * **It cannot see inside MediaMTX or ffmpeg.** `container.procs` says which
      process holds the megabytes and stops there. If the answer is `mediamtx`,
      the next step is its own metrics endpoint (which this deployment does not
      enable) or Go's pprof; if it is `ffmpeg`, the levers are
      `-max_interleave_delta` and the audio pipe's depth, neither of which this
      bridge currently sets. Building either into the bridge before knowing
      which process it is would be the guess this whole entry exists to avoid.
    * **`MALLOC_ARENA_MAX` is still not set.** E74 recommended it at "the next
      natural rebuild" as an optimisation and it was never applied, so if the
      climb turns out to be per-thread arenas the fix is a one-line environment
      change that has been available the whole time. It is deliberately NOT
      being applied blind here: applying it now would confound the very
      measurement that would tell us whether it was the cause, and it is not
      free -- two arenas across 119 threads serialises allocation that is
      currently spread across many.
    * **The sampler runs on the event loop.** A record costs a full
      `gc.get_objects()` walk (tens of milliseconds on this heap) plus a
      `tracemalloc` snapshot, once every 330 s. That is a real pause on the
      loop that also drives `write_frame`, so a traced run may drop a frame at
      each sample. Acceptable for a diagnostic that is on for an hour, and the
      reason `HANDOVER_TRACE`'s "nothing here awaits, sleeps or writes, safe to
      leave on" promise is explicitly NOT made for this one. Moving it to a
      thread would need the gc walk to be safe concurrently with the loop
      allocating, which it is not.
    * **`gc_growth` cannot see untracked objects.** `gc.get_objects()` returns
      only container-tracked objects, so a leak of plain `bytes`/`bytearray`
      buffers -- which is what a retained PCM payload would be -- appears in
      `tracemalloc.top` and in `python.rss_mb` but never in `gc_growth`. The
      three views are complementary for exactly this reason, and reading only
      the object census would miss the case.
    * **The offline rigs are not in the repository.** The five rigs that ruled
      out the Python bridge (E83's table) were scratch scripts: a decoder-only
      loop, a `JanusCameraSession` audio loop, the `run_camera_forever`
      harness, a real-ffmpeg publisher fed real frames, and a real aiortc
      peer-connection pair rotated. Only the third became a test
      (`test_consumed_audio_retains_nothing_across_rotations`), because it is
      the only one that runs in under a minute with no ffmpeg binary and no
      network stack. The other four are minutes-long soaks whose pass condition
      is a slope, which is a poor fit for a suite that has to be fast and
      deterministic in CI. If the leak ever IS found in the Python process, the
      rig that finds it should be added rather than re-derived.

- **E83 resolution, 2026-08-26: glibc heap fragmentation, fixed with jemalloc.**
  The memory_trace instrumentation answered it in two samples. Across one 330 s
  window python RSS grew 118 MB while glibc's `in_use` grew **3 MB**:
  `free_not_returned` went 35.9 to 144.6 MB with only 2.92 MB `releasable`. So
  Python was not retaining memory; glibc was holding freed memory, fragmented
  inside its arenas where `malloc_trim` cannot reach it. The cause is the
  allocation pattern: 3.11 MB video frame buffers at 15 fps interleaved with
  about 15/s of small audio allocations, and mu-law quintupling the small half
  is why v1.24.0 surfaced it.

  That explains every prior observation at once: five offline rigs flat (none
  interleave at production scale for long enough), object counts flat (nothing
  was retained), `MALLOC_ARENA_MAX=2` useless (the arena COUNT was never the
  problem, tested and measurably no change), and E74's plateau (the same
  fragmentation reaching a ceiling at the lower pre-E80 churn).

  **Fix: jemalloc, preloaded for the python process only** (`libjemalloc2` in
  the image, `LD_PRELOAD` in `entrypoint.sh`). Not global on purpose: MediaMTX
  is Go and bypasses glibc malloc, and both ffmpegs measured flat, so the change
  is scoped to the one process with the pathology. Missing library degrades to
  glibc with a stderr warning rather than a dead container.

  **Measured against a pre-stated pass criterion, same 25 minute window, same
  18 sessions:** glibc went 333 to 623 MB monotonically, python +15.4 MB per
  session, straight line to OOM. jemalloc went 321 to 439 MB **oscillating**,
  python about +6 MB per session and falling, including a 417 to 336 MB drop at
  one sample, something glibc never once did. The decay returning pages to the
  OS is the exact mechanism mallinfo2 showed missing.

  Audio is ON for both cameras. Watch `bridge-memory-high` in the stack watcher
  over the next days; the threshold conversation belongs to E74's reopening.

- **E85 (open) E74 is wrong and needs reopening.** E74 diagnosed the memory
  plateau as glibc arena behaviour and recommended `MALLOC_ARENA_MAX=2` without
  testing it. E83 tested it: no measurable change. The plateau E74 observed was
  real but the mechanism was fragmentation reaching a ceiling at low allocation
  churn, not arena count. E74's entry should be corrected to say so, its
  watcher-threshold guidance re-derived under jemalloc (the working set may sit
  lower now), and this is also the third entry to carry the same lesson: a
  diagnosis inferred from a curve's shape and closed without testing its own
  recommendation is not a diagnosis.

- **E86 (open, minor, upstream) aiortc accumulates RateBucket objects.**
  `gc_growth` showed +4,004 `RateBucket` and +26,622 tuples in one 330 s window,
  from `aiortc/rate.py` (the remb bandwidth estimator's history). Roughly 3 MB
  per window, real but two orders below the fragmentation, and invisible now
  that jemalloc handles the churn. Worth checking whether aiortc's
  RemoteBitrateEstimator ever prunes its buckets, and filing upstream if not.
  Not load-bearing for this deployment.


- **E87 (DONE) The integration test harness now covers eleven modules.** E66
  left it at three. Added: the config flow (user and reconfigure paths, error
  mapping), the speak/bypass/unpair services, the settings-backed number, select
  and switch platforms, the arming-device entities, the connectivity binary
  sensor, and the setup/unload/reload lifecycle, plus a shared `tests/fakes.py`.
  Eleven suites, all green, and CI picks up new `tests/test_*.py` automatically.

  **Recorded because it is a real gap, not a footnote: the systematic fail-first
  campaign was NOT run.** The agent building these suites was terminated by a
  credit limit at exactly the point it announced the mutation pass. Three
  mutations were then driven by hand against real source and all three were
  caught with precise messages: the config flow's `invalid_auth` mapping
  remapped to `unknown`, the speak service's 413 mapping broken, and the
  connectivity sensor forced permanently unavailable. A fourth attempt proved
  nothing and was discarded, because the mutation never applied (it targeted a
  `raise` in a module that maps errors by assignment) which is the same
  verify-the-mutation-applied trap recorded in E72 and hit twice this week.

  **The mutation pass has since been run** (2026-08-28). Eleven mutations against
  real source across the new suites: the select's `current_option` forced to
  `None`, the number's `native_value` to zero, the switch's and the connectivity
  sensor's `is_on` inverted, the bypass service's 429 mapping broken, unload
  short-circuited to success, the config flow's `invalid_auth` remapped, the
  speak service's 413 mapping broken, and all three `is_on` return sites in
  `binary_sensor.py` inverted one at a time. Ten were caught with precise
  messages.

  **One real survivor, now closed.** `AttributeBinarySensor.is_on` (the
  malfunction and battery sub-entities that ride on every device) could be
  inverted with no test noticing, while the other two `is_on` sites in the same
  module were pinned. An inversion there would have reported every healthy
  device as faulty across the whole install.
  `test_attribute_sensor_reads_its_description_value_fn` closes it, and
  re-running the same mutation now catches it.

  **Two attempted mutations proved nothing and were discarded rather than
  counted**, both the same trap: one targeted a `raise` in a module that maps
  errors by assignment, and one matched the first of three `is_on` definitions
  in a file rather than the one under test. That trap (E72) has now been hit
  four times in a week, and the rule earned is narrow enough to state: **diff
  the file after mutating it and before running anything.** A mutation that
  changes nothing, or changes the wrong site, produces a confident and false
  verdict in whichever direction you were already leaning.

- **E88 (DONE) E76's mechanism is measured; the fix is not written.** Ten
  steady-state handover records from the live doorbell and living room split
  cleanly into two populations. When the frame hold runs properly
  (`silence_writes` 13 to 41, `video_writes` 11 to 26, `max_gap_ms` 51 to 56)
  the muxer lead stays between +1899 ms and -922 ms and drops are 0 to 9. When
  it dies about 60 ms in (`silence_writes=2, video_writes=1` across a 2 to 3
  second window, though `hold_stopped` is not marked until 1.9 to 3.0 s later)
  the lead falls to -1721 to -2905 ms and drops are 6 to 19. Three of five
  doorbell rotations show the early death; zero of five living-room rotations
  do.

  Every drop timestamp lands in the window after the new pump starts, at frame
  interval cadence, which is the bounded silence repayment
  (`_AUDIO_SILENCE_MAX_BURST_S` 0.1 per top-up at 0.05 cadence, about 2x real
  time) holding ffmpeg off the video pipe until a multi-second deficit clears.
  Drop count tracks deficit size.

  **Two anomalies in the instrument itself, to be settled before trusting it
  further:** `worst_gap` is always `connected->batch_collected` with `adv=0.0`
  even on rotations where the hold demonstrably wrote 38 silences inside that
  window, and one record reports `lead_min` of +1899 ms, far above the 100 ms
  prime that `write_silence_to`'s wallclock tracking should permit. Either the
  trace derives audio advance from a source that misses hold writes, or the hold
  writes somewhere other than assumed. A misleading instrument is worse than
  none, so this is part of the fix, not a separate cleanup.

  Not attempted yet: why the hold exits early, and whether a multi-second
  deficit should be repaid at 2x real time at all rather than rebasing the audio
  origin on pump start (dead air nobody hears, against a permanent A/V offset
  from a partial repayment).


- **E71 resolution: the adaptive rate reacts to a real settings change.**
  Observed end to end rather than argued. Starting state: `live_resolution: 1`,
  publishing 1280x720, `fps: 5` and `measured_fps: 5` in agreement. Writing
  `live_resolution: 3` through the control API changed nothing immediately, and
  on the NEXT rotation the source came up **640x360** and the publisher followed
  it, which is exactly the behaviour E34 documents (the setting takes effect on
  stream renegotiation, not on write). `measured_fps` then read **10** against a
  declared 5, so the rate watcher saw the change, and the declared rate was
  still the old one because a confirmed change is applied at the following
  rotation by design (E64) rather than mid-session. Restoring
  `live_resolution: 1` returned 1280x720 on the next rotation.

  So the two behaviours this entry existed to check are both confirmed on live
  hardware: the resolution select really does drive the bridge's own restream,
  and the adaptive rate machinery tracks a genuine device-side change instead of
  latching the rate it first measured.

- **E89 (FIXED 2026-08-28, pending live confirmation) The bridge was restarting
  itself on session failures, and the trigger was the bug.** Two days of watcher
  history show the container exiting and restarting repeatedly. The cause is NOT
  memory: `OOMKilled` is false, the kernel log has no OOM events, and every exit
  is `entrypoint.sh: bridge exited (code 1)`, which is the escape hatch doing
  what it was built to do after five consecutive failures.

  **What it was built to do was the wrong thing.** Measured on 2026-08-28: 21
  restarts in one day, and EVERY observed failure run recovered on its own after
  one to four failures. The threshold was five, so the hatch was firing on the
  tail of a distribution that heals itself. The restarts also did not work: after
  the 16:13:36 exit the same camera failed five MORE times before streaming
  again, which falsifies the hatch's own premise ("a leaked/wedged resource that
  only a fresh process can clear"). Each restart kicked every RTSP reader and
  discarded both cameras' learned audio shape (E82).

  A count cannot separate a wedged local resource from a flaky cloud, because
  both produce failures; only whether the camera comes back separates them. So
  the trigger is now a DURATION with no successful frame,
  `config.SESSION_WEDGE_TIMEOUT_S = 1800`. `MAX_CONSECUTIVE_SESSION_FAILURES` is
  retired.

  **The threshold was set twice, and the first value was wrong.** It went in at
  600s, justified as "about six times the longest transient run observed"
  (~103s). Within the hour the doorbell produced a 550-second run: 13
  consecutive failures from 19:48:08 to 19:57:18 with no successful frame, which
  then recovered unaided and returned to 1440x1440@15fps with no drops. That
  cleared 600s by 50 seconds, a margin of 1.09x rather than 6x, and one slower
  retry would have restarted the container for a camera that was about to fix
  itself. Raised to 1800s, which is 3.3x the worst transient MEASURED rather
  than the worst one known when the constant was written.

  The asymmetry justifies the generous side: a spurious restart kicks every RTSP
  reader on both cameras and discards their learned audio shape to fix nothing,
  while a late one leaves a single camera down longer, and the one confirmed
  wedge never self-cleared at all so any finite threshold catches it.

  Then a THIRD run, 1080 seconds on the same camera, landed inside the new 1800s
  window and recovered unaided as well. Three raises would have been two too
  many, because time was never the thing separating the two cases.

  **A HEALTHY PEER NOW VETOES THE RESTART, and that is the real discriminator.**
  The hatch exists for a leaked or wedged resource that only a fresh process can
  release. Every such resource is a property of the PROCESS, so it takes every
  camera down with it, and the one confirmed incident behaved exactly that way:
  2026-07-20 was 26+ failures ACROSS BOTH CAMERAS that never self-cleared. Every
  run measured on 2026-08-28 was the opposite shape: 103s, 550s and 1080s, all
  on the doorbell, all with the living room streaming throughout, all recovering
  by themselves. A process that is decoding, encoding and publishing for another
  camera right now is not wedged, and restarting it cannot reach the far end of
  the one path that is failing.

  So `config.SESSION_WEDGE_PEER_FRESH_S = 120`: another camera having produced a
  frame that recently vetoes the exit and logs why, once per run. It cannot
  become a permanent exemption, because the reading is the wall-clock time of a
  real frame, so a peer that stops delivering stops vetoing on its own. On a
  single-camera deployment there is no peer and the hatch behaves as before.
  The duration stays as the backstop for the both-cameras case.

  The lesson generalises past this constant: a threshold justified by "N times
  the worst case so far" is only as good as how thoroughly the worst case has
  been sampled. This one was raised twice and was still being chased by the data,
  which was the signal that the measure itself was wrong rather than its value.

  **The `TypeError` was a second, separate bug, and it is now fixed too.** This
  entry previously said it had not recurred in 12 hours and was not driving the
  restarts. The first half of that was WRONG: it recurred at 19:25 on 2026-08-28,
  minutes after the escape-hatch fix deployed, and this time it carried a
  traceback. `telus_rest.get_video_source` called
  `json.loads(attrs["iceServers"])` unguarded, and this account's server
  sometimes returns `iceServers` as null with the KEY STILL PRESENT, so the
  subscript succeeded and `json.loads(None)` raised. It failed the whole session
  every time it happened, and it had gone unexplained for weeks because it
  surfaced only as a `last_error` string. `_parse_ice_servers` now degrades to
  an empty list for null, empty, malformed and already-decoded input. Empty is
  the right answer rather than a failure: aiortc still gathers host and
  server-reflexive candidates, and this account's own ICE logs show DIRECT pairs
  reaching SUCCEEDED, so such a session can still connect.

  **Not yet confirmed live.** The claim to check is a fall in restart count with
  no rise in time-without-frames, and it needs hours rather than minutes.

  The failures behind it, from the watcher's captured `last_error` fields:
  `no frames received before timeout (waited 20.0s of 20.0s; webrtcup seen
  before the wait)` on both cameras, `session dead: track ended:
  MediaStreamError()`, `no frame for 15s, treating session as dead`, and
  **`TypeError('the JSON object must be str, bytes or bytearray, not
  NoneType')`**, which is not an upstream stream failure at all but a JSON load
  handed `None`. That last one deserves its own investigation: it is a plain
  defect, it reached the escape-hatch path, and nothing currently records where
  it was raised.

  Worth separating from E88 before either is worked: E88 is about frames dropped
  inside a healthy handover; this entry is about sessions failing outright and
  taking the container with them. They may share a cause and may not.


- **E83 follow-up, two days of data: bounded, but with no headroom.** The
  25-minute reading that closed E83 said "dramatically better, not yet
  plateaued", and two days of five-minute samples settle it. The container
  oscillates between about **41 and 99 percent** of its 768 MiB limit, and
  crucially **`OOMKilled` is false and the kernel log records no OOM events**:
  jemalloc bounded the growth into a wide sawtooth rather than eliminating it.
  Every container restart in that window came from the session-failure escape
  hatch (E89), not from memory.

  So the fix works and the limit is now the wrong size. 768 MiB was chosen
  against a measured ~326 MB video-only working set; both cameras now consume
  mu-law audio, which adds a second decode path, a second silence-fill writer
  and a second ffmpeg audio input. The limit is raised to 1 GiB to restore
  headroom, and the watcher's 93 percent threshold becomes meaningful again
  rather than firing against the normal operating range.

  Recorded because the shape of the error repeats: a 25 minute window said
  "sawtoothing, envelope still rising" and a two day window says "sawtoothing
  between 41 and 99 with no OOM". Both readings were honest; only the second is
  actionable. **State the window length with the claim.**


- **E88 resolution: the hold deadlocked against itself, and the instrument was
  telling the truth.**

  **Mechanism.** `_hold_frames` awaited `sink.write_frame(last_frame)` inline,
  three lines above the `write_silence_to()` call in the same loop body.
  `write_frame` ends in a bounded drain that completes only when ffmpeg *reads*
  the frame, and ffmpeg reads the video pipe only while it holds audio covering
  that frame's timestamp. So a video write that blocks for want of audio also
  blocked the only writer that could supply it: the previous session's pump is
  stopped by the hold's first act and the next is not created until the hold
  ends. The two writers waited on each other. Nothing reported it because the
  drain's own 5 s bound is longer than a 2 to 3 s handover, so it never timed
  out, `publisher_stalled` never latched, and the task stayed alive until
  `_stop_hold` cancelled it: no exception, no log line, `sink_rebuilt: false`.

  Reproduced offline through the real loop against a publisher applying the
  muxer's actual rule: `silence_calls: 2, video_writes: 1, lead_min_ms: -1231.9`,
  against the live `silence_writes: 2, video_writes: 1, lead_min_ms: -1721 to
  -2905`. **Fix:** the repeated frame is written by a task of its own and reaped
  on a later tick, so the top-up runs every tick regardless. Exactly one video
  write is ever in flight and a `finally` reaps it, so `_stop_hold`'s guarantee
  is unchanged.

  **Both trace anomalies were one fact, and the trace was right.**
  `write_silence_to` tops the timeline up TO the wall clock and returns `True`
  having written nothing when the timeline is already ahead. `hold_silence`
  counted every such call as a write, so "38 silence writes beside `adv: 0.0`"
  was literally true and unreadable. The record now separates `silence_calls`
  (cadence) from `silence_writes` and `silence_ms` (media actually put on the
  pipe), and `video_issued` from `video_writes`. The +1899 ms lead was
  legitimate: `_AUDIO_PRIME_MS` is the lead at pipe open, nothing caps it after,
  and a decoded backlog drained by the pump puts the timeline seconds ahead. It
  is a floor, not a ceiling, and now says so in the code and the docs.

  **This inverts E88's own reading of its data.** The population called healthy
  was not healthy; it was coasting on a large positive lead. Its holds wrote
  nothing either. The difference between the two populations was the size of the
  lead they started with, not whether the hold worked.

  **Deficit bounding: rebasing the audio origin was considered and rejected on
  the mechanism.** The audio input carries no `-use_wallclock_as_timestamps`, so
  its PTS *are* its sample count; skipping N seconds of samples makes the audio
  permanently N seconds short against wallclock-stamped video, for the life of
  the publisher, and `aresample=async=1` cannot correct it because it works
  against the audio's own timeline. The premise that repayment is partial was
  also false: the deficit is recomputed from the wall clock every call, so it
  always converges. The defect was the RATE, so past
  `_AUDIO_CATCHUP_DEFICIT_S` a deficit is now closed in one write rather than at
  2x real time, bounded in BYTES (`_AUDIO_CATCHUP_MAX_BYTES`) because the real
  constraint is pipe buffer, and 3 s is 48 kB at 8 kHz mono but 576 kB at 48 kHz
  stereo.

- **E90 (open, minor) Three follow-ons from E88, none observed failing.**
  (a) `write_silence_to` restores the timeline to the wall clock, i.e. a lead of
  exactly zero, so the prime's 100 ms is spent once at pipe open and never
  re-established: a quiet camera oscillates between 0 and -50 ms. Topping up to
  `now + prime` would keep the designed lead standing, but it is a pacing
  constant change with a measured history (E60) and should not be made blind.
  (b) `_pump_audio` only tops up on its timeout branch, so a camera with
  continuously arriving audio never tops up at all and a deficit inherited from
  a handover persists until it next goes quiet for 50 ms. Not observed, but
  nothing bounds it. (c) `video_issued` is exercised by the hold test but not
  independently fail-first proven, unlike the silence counters.

- **E91 (DONE) The video queue ceiling was sized for a 768 MiB limit and a
  15 fps camera.** After E88 landed, the indoor camera was dropping about 150
  frames every 17 to 24 seconds in **steady state**, nowhere near a handover,
  while the container sat at 31 percent of two cores and 434 MB of 1 GiB. Not
  starved: under-buffered.

  The cause is a rate change, not a regression. That camera began delivering
  **30 fps** where it had been 5 to 10 earlier the same day. `video_queue_maxsize`
  is `round(fps x 500 ms)` clamped to 4 to 8 frames, so at 30 fps the clamp gave
  **267 ms** of tolerance, which is LESS than the 15 fps camera the ceiling was
  sized for. E62 recorded that exact limitation ("above ~16 fps the memory
  ceiling binds before the time target") as a deliberate trade against a 768 MiB
  container. `mem_limit` is now 1 GiB, so the arithmetic that set 8 no longer
  holds.

  Ceiling raised to 16, which restores the 500 ms target up to 32 fps and so
  covers every rate either camera has been observed delivering. 16 frames is
  49.8 MB at 1440x1440 and 21.2 MB at 1280x720, about 71 MB for the pair against
  roughly 590 MB spare.

  The test that guards this **caught the change before the mutation pass did**:
  it asserted the ceiling's worth stayed under 25 MB and failed the moment the
  ceiling moved. That bound is now stated for BOTH cameras rather than one, so a
  future raise cannot silently double the real cost.

  **Still not the fix, by the same reasoning E62 gave.** A deeper queue converts
  dropped frames into latency; it does not stop a writer stalling. What it buys
  is that a 30 fps camera gets the same 500 ms of tolerance as a 15 fps one,
  which is all the sizing rule ever claimed to do.

- **E92 (open) Video drops track audio STARVATION, not the handover, and the
  mechanism is not established.** This is the strongest remaining lead and it is
  recorded as a correlation rather than a diagnosis.

  Measured after E88 and E91 both landed, five-minute watcher windows on the
  indoor camera:

  | window | video drops | audio frames decoded |
  |---|---|---|
  | 03:25 | **+0** | +3,398 |
  | 03:30 | +6 | +1,243 |
  | 03:35 | **+0** | +1,062 |
  | 03:45 | **+383** | **+84** |

  Video drops when audio stops arriving and not otherwise, and a single session
  in that state shed 2,700 frames before its rotation cleared it. The handover
  itself is now clean in the same build (`silence_calls` 599 to 630 with
  matching writes, `lead_min_ms` -6.6 to -46.9, drops 0), so this is a
  MID-SESSION failure and a different thing from what E88 fixed.

  **Ruled out by direct reading at the time**: the sink had not given up on audio
  (`audio_dropped: false` on both cameras), the formats matched
  (`audio_format` == `publisher_audio_format` == `8000Hz/1ch/s16`), the codec was
  committed (`pcmu`), and the container was at 31 percent of two cores with
  590 MB spare, so it is neither the wedged-pipe path, nor a format mismatch, nor
  resource starvation.

  **The obvious suspect is E90(b)** and it should be tested rather than assumed:
  `_pump_audio` only calls `write_silence_to` on its `AUDIO_TIMEOUT` branch, so a
  camera whose audio arrives sporadically, often enough to keep waking the pump
  but carrying far less media than wallclock, would never have its timeline
  topped up. That would starve the muxer of audio, stop it reading video, and
  flood the queue, which is the shape observed. It does not obviously explain the
  earlier record showing `lead_min_ms: -167912.4` with zero audio calls and 53
  video ones, so there may be two things here.

  **Do not fix this by reasoning.** Turn `HANDOVER_TRACE` on, catch a starved
  session, and read what the pump actually did.

- **E92a (open) A handover record reported a lead of -167,912 ms.** One
  `sink_rebuilt: false` rotation on the indoor camera carried
  `lead_min_ms: -167912.4` with `silence_calls: 0` and `video_issued: 53`. That
  is roughly one session lifetime of deficit, which suggests the audio timeline
  did not advance at all for that session while video did, and the hold made no
  audio calls in a window where it made 53 video ones. Not reproduced, not
  explained, and not obviously the same thing E88 fixed, since the fix is
  present in that build. Recorded rather than rationalised.