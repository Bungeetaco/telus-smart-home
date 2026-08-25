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

- **E62 / E60**: fixed in the bridge, **awaiting live validation**. Audio
  consumption took the doorbell off the air after ~14 minutes while
  `/control/status` reported `streaming` throughout. Four changes shipped: audio
  pacing at half the muxer lead, a time-based video queue, a wedged audio pipe
  degrading to video-only, and a bounded video drain that makes "publishing to
  nowhere" detectable. Validation is a multi-session soak with audio enabled
  **on the doorbell only**, watching the four signals listed at the end of E62.
- **E66** (open): the integration now has a test harness (E63), but it covers
  three modules. The config flow, the three services, the settings-backed
  platforms and every setup/teardown path are still verified by reading and
  `py_compile` alone.
- **E71** (open): E64's adaptive frame rate has never been watched reacting to
  a real resolution change live. A five-minute operator check; the procedure,
  now fully readable from `/control/status`, is in the entry.

- **E75** (open, minor) - the stack watcher is now versioned at
  `ops/stack-watch.sh` but is untested shell that has already produced three
  false positives; its cron entry is not captured in the repo.

- **E76** (open) - the doorbell's residual drops are the audio path stalling at
  each session rotation: 14 per rotation with audio against 0.8 without,
  measured by A/B on the same camera. Instrument the handover before changing
  anything.

**Blocked on hardware or a device this account does not have**

- **E13**: validate the webrtc/srtp fallback path; needs an ADC camera that
  offers those endpoints.
- **E20**: the audio-conditioning chain is tuned by ear for the VDB750's
  speaker; only matters once a different speaker exists.
- **E33** (one point): whether `shouldDisplayBatteryLevel` is reliable; needs a
  battery-powered camera.
- **E41** (two questions): whether panel-wide pairing off makes the per-device
  toggle inert, and the never-captured `bluetoothDisarmingSettings` write shape.

**Watch-only, no action by design**

- **E57**: `pyalarmdotcomajax` maintenance is paused upstream. Pinning is why
  nothing needs doing; the entry lists the triggers that would reopen it.

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
- **E13 (BLOCKED -- no hardware) Validate the webrtc/srtp fallback.**
  `talk_webrtc.py` / `talk_srtp.py` were built on the pre-capture hypothesis and
  have never run against a real webrtc/srtp-capable ADC camera. **This account has
  no such device** -- both cameras here use the HTTP-PUT path -- so this cannot be
  closed at any level of effort until one exists. The modules import cleanly in
  CI, which is the only assurance available without hardware.

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
  NAL bytes); those merely reproduce the same error class. The doorbell's own
  upstream audio is µ-law, so the likeliest reading is that the relay forwards a
  device-native frame before its Opus transcode is running; that remains
  **unverified**. It costs a few milliseconds of audio per session.

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

- **E55 (CLOSED: not worth pursuing) Device-direct listen audio.** Superseded:
  downstream audio comes over the website/Janus path (E53/E54), so a second
  source buys nothing operational.

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
  session plus a backoff. It never counts toward
  `MAX_CONSECUTIVE_SESSION_FAILURES`. The pending change is also re-checked
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

- **E71 (open) E64's thresholds are argued from measurement, but the mechanism
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

- **E74 (DONE) The bridge container's memory is a plateau, not a leak.** The
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

  **Do not guess at a fix.** The next step is instrumentation, not a change:
  timestamp `_stop_audio_pump` returning, the hold's first silence write, the
  new session's first video write, and the queue depth across that window. If
  the lead does collapse there, the numbers will say so.

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
