# Enhancement backlog

The project's single list of planned improvements, deferred ideas, and settled
questions — for the talkdown feature where it began, and for every adjacent
surface the project has since grown into (camera settings, arming devices,
bypass, the bridge and integration themselves).

Standing practice: **every known gap or potential improvement, however minor,
is logged here** rather than dropped — so a future agent (or the maintainer)
can pick it up. "Not implemented" means "not yet," never "unwanted." Add an
entry whenever something is deferred, simplified, or noticed in passing, and
when an entry is closed, keep it: a DONE entry's evidence is what stops the
next person re-deriving a settled question. IDs (`E1`, `E2`, …) are stable
references used across the docs and commit history.

Entry statuses: **open** (not started), **DONE** (shipped, entry records the
evidence), **BLOCKED** (needs hardware/access this account does not have),
**CLOSED — not achievable** (proven impossible, entry records the proof), and
**DECIDED** (a deliberate keep-as-is with reasoning).

## Open items at a glance

Everything not listed here is settled; each entry below carries its own status
and evidence.

- **E6** (second half) — whether adaptive prebuffer *helps* on a degraded link;
  needs a talkdown made from outside the house over the `cloud` relay.
- **E13** — validate the webrtc/srtp fallback path; blocked on hardware this
  account does not have.
- **E20** — the audio-conditioning chain is tuned for the VDB750's speaker;
  another device may want different cutoffs/levels.
- **E33** (one point) — whether `shouldDisplayBatteryLevel` is reliable on an
  actual battery camera.
- **E41** (two questions) — whether panel-wide pairing off makes the per-device
  toggle inert, and the uncaptured `bluetoothDisarmingSettings` write shape.

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
  normal path — cancelling the task would have raced the `finally` that issues
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
  | Does the audio endpoint negotiate a codec? | **No.** `?codec=L16`, `?format=`, `?encoding=`, `?payloadType=97`, `X-Audio-Codec`, `Content-Type: audio/L16` — every variant answers exactly like the baseline. It consumes bytes as mu-law regardless. |
  | Could RTSP `ANNOUNCE`+`RECORD` carry RTP/L16? | **No -- there is no RTSP server.** The media port answers RTSP with `HTTP/1.0 400 Bad Request` from `lighttpd/1.4.61`. The `rtsp://` URLs in `<cli>` are consumed over HTTP; the device has no RTSP method set at all. |

  So this doorbell exposes exactly **one** inbound audio path, and that path is
  **mu-law only**. That also explains the app's own behaviour: the reference
  implementation selects `mulawenc` for this device, not `skyL16pay`, because
  `skyL16pay` targets the other device family (the OpenEye/SRTP cameras behind the
  webrtc/srtp transports — see E13). Nothing short of new device firmware changes
  this, so E4 is closed rather than deferred.

  **Consequence:** 8 kHz mu-law is the quality ceiling on this hardware, which is
  what makes the conditioning chain (E-COND) the real lever for how talkdown
  sounds — not the codec. If this project is ever pointed at a different model,
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

- **E6 (measurement DONE; tuning open) Adaptive prebuffer.**
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
  | `put_rtt_max_ms` | ~200 — the FIRST PUT, which pays connection setup and the Digest challenge |
  | `max_late_ms` | ~330 |

  Read together those say the 500 ms lead is about right rather than generous:
  the opening round trip eats roughly 200 ms of it, the worst slot slips ~330 ms,
  and the buffer still absorbs that with around 170 ms to spare. A shorter lead
  would start eating into real underruns on the very first chunks, which is the
  part a listening test would hear.

  `late_chunks` counting more than zero on a healthy short clip is expected for
  the same reason and is not a fault — it is the lead being spent on connection
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
  envelope, endpoint ordering, and the full webrtc/srtp/rtsp records — including
  that the SRTP keys are the base64-*decoded* bytes of `ack`/`vck`, not the
  base64 text — plus the xmlns-namespaced / logged-out / wrong-root edges),
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
  MWS-backed sections (siren/privacy) remain the deliberate exception — a
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
selected five items — the per-request diagnostics timeout, the control-server
429/clamp/log hardening, the clip-view guards, the credentials-fallback
robustness, and the LVSR parser coverage, all shipped — and deliberately
deferred the rest. Logged here per the rule that a gap noticed is a gap
recorded; each open entry carries the reason it was deferred, which is the
evidence the next reviewer needs, and an entry flips to DONE with its evidence
when it ships.

- **E45 (DONE) A pre-existing `control_secret` file is re-tightened to `0o600`
  when the bridge adopts it.** Previously an adopted file (restored from a
  backup, or written under a permissive umask) kept whatever mode it arrived
  with — a bearer token sitting group/world-readable on the shared bind mount
  until the next scheduled rotation happened to rewrite it. Now
  `control_server._load_or_create_secret` calls `_tighten_secret_mode()` after
  successfully reading an existing non-empty secret file: if the mode has any
  group/other bits set, the file is best-effort `chmod`ed to `0o600`, with a
  warning naming the previous octal mode.

  Best-effort on purpose: an `OSError` from the chmod (e.g. the file is owned
  by another uid on the shared mount) logs a warning with traceback and the
  secret is **still served** — the bridge never regenerates the secret over a
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
  — provoking it needs a file owned by a second uid — so only its warning path
  is verified by reading.

- **E46 (DONE) `mws_client` and `mobile_auth` import defusedxml as a HARD
  top-level dependency** — the decided option, not the warn-once fallback.
  Both modules do `import defusedxml.ElementTree as DET` at module level with
  a comment saying why; the try/except-`ImportError` stdlib `ElementTree`
  fallbacks are deleted, along with `mobile_auth`'s stale fallback comment and
  `mws_client`'s then-unused stdlib `ElementTree` import. Rationale: the
  fallback was dead code in the shipped container (`requirements.txt` pins
  `defusedxml>=0.7.1`, the image installs wheels-only) that silently dropped
  entity-expansion protection for out-of-container runs; a build without
  defusedxml now fails at import time, which the every-module-imports CI job
  turns into a loud PR failure. Pinned by
  `test_parsers.py::test_defusedxml_is_a_hard_import` — a subprocess with a
  meta-path blocker for defusedxml asserts neither module imports — which
  fails against the fallback behaviour and passes against the hard import.

- **E47 (DONE) `JanusCameraSession.close()` no longer leaks the Janus
  websocket, and `_keepalive_loop` no longer dies with an unretrieved
  exception.** Shipped as the dedicated change the entry prescribed — read
  `camera_session.py` and `janus_client.py` in full, `test_camera_session.py`
  run deliberately — with the dropped-frame half of E51 riding along.

  The websocket half: `close()` closes the gateway websocket unconditionally
  via try/finally, so a `pc.close()` that raises, or the
  `SESSION_CLOSE_TIMEOUT_S` `wait_for` cancellation landing mid-close, still
  releases the socket — previously a wedged camera leaked one gateway socket
  per ~165 s rotation. The pc-then-ws ordering is preserved, the guarded ws
  close cannot mask `pc.close()`'s own exception, and `CancelledError` is
  re-raised after the cleanup attempt.

  The keepalive half: `_keepalive_loop` catches `websockets.ConnectionClosed`
  and returns quietly (the recv loop owns marking the session dead, exactly as
  the entry noted), re-raises `CancelledError`, and logs-and-returns on any
  other exception — so a keepalive sent into a session that died between
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
  `renovate.json`'s actual policy — low-risk minor/patch auto-merge, with
  pyalarmdotcomajax/aiortc/av/mediamtx/aiohttp/websockets and the grouped
  `python` pair review-required; the old "(python base image, beautifulsoup4)"
  example was doubly wrong, since `python` is review-required) and the
  requirements tables in `configuration.md` and `ha-integration.md`, which
  now list `pyalarmdotcomajax==0.5.13` only, matching the manifest.
  `development.md`'s audit line recording that beautifulsoup4 was dropped as
  unused stays — it states a current fact. Rode along:
  `configuration.md`'s manifest `version` row no longer hardcodes a release
  number (it points at `development.md`'s versioning section instead), so it
  cannot go stale again.

- **E49 (DONE) `mobile_auth`'s cross-module helpers are public:
  `device_fields` and `read_body_text`.** All call sites and comments in
  `mobile_auth` and `mws_client` use the new names; a repo-wide grep confirms
  no code references the old underscore names. The bundled `development.md`
  corrections shipped too: the underscore-import clause is gone from its
  still-open list, and the entity-naming hazard claim was checked by
  repo-wide grep, found FALSE, and replaced with the verified truth —
  `device.__class__.__name__` appears only in `base_device.py` `LOGGER.info`
  lines; entity unique IDs derive from `device.id_` (optionally suffixed with
  a description key or config-option slug) and display names from
  `device.name` plus each platform's `_device_type_name` string literal, so a
  `pyalarmdotcomajax` class rename would affect log lines only. The
  `pylibsrtp` clause remains open there.

- **E50 (DONE) The integration honours the bridge's 429/`Retry-After` contract
  and maps the talk route's remaining documented errors.** Mapping only — no
  retries and no timing changes anywhere.
  `camera_settings_coordinator.async_bridge_post` special-cases 429 before
  `raise_for_status`: it reads the wait from the `Retry-After` header, falling
  back to the JSON body's `retry_after`, and raises "Alarm.com is rate-limiting
  this account; the change was not applied -- try again in {N}s" ("… try again
  later" when no wait is readable), matching `arming_devices_coordinator.py`'s
  vocabulary — so the siren buttons, the privacy switch and every
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
  `control-api.md`. Verified by `py_compile` and code reading only — the
  integration has no test suite and this was not run in a live HA instance;
  live confirmation needs a real rate limit or an oversized clip.

- **E51 (DONE — all parts)
  Observability additions that lost the batch-size contest.** The metrics half
  is shipped:
  `TalkMetrics` (`bridge/metrics.py`) keeps a `by_path` counter mirroring the
  existing `by_codec`/`by_endpoint` idiom — incremented once per talk that
  successfully ran a transport, from the result's `path` value (`full_duplex`,
  `webrtc`, `srtp`), including talks ended early by an explicit stop (the
  `by_codec` convention); failed and rejected talks add nothing — and
  serialised as `"by_path"` in the `GET /control/metrics` snapshot. No
  transport code changed: every transport's result dict already carries
  `path`, and the single choke point recording a winning talk is the
  `metrics.talk.success(...)` call in `speak()`. Pinned by
  `test_metrics.py::test_by_path_counter` (increment per path, stopped-talk
  counting, failure/path-less exclusion, snapshot presence, JSON
  serialisability), which was proven to fail (`KeyError('by_path')`) against
  the pre-change code. This makes a silent degradation visible — the
  preferred full-duplex path failing device discovery while every talk
  quietly runs the webrtc/srtp fallback.

  The dropped-frame half shipped with E47's dedicated session, as this entry
  prescribed (it touches the rotation loop's file). `JanusCameraSession` gains
  a public read-only `dropped_frames` property; `camera_session` accumulates
  each ending session's count into a per-camera lifetime total via a
  `_retire_session()` helper wrapping all three `_close_with_timeout` call
  sites (healthy rotation, failure path, restart-guard path), published as an
  additive `dropped_frames` field — present as `0` from the first publish — in
  the `/control/status` per-camera object. Because it accumulates at session
  end, it can lag the live session's drops by up to one rotation; it resets
  with the process like the rest of the status dict. `control_server.py`
  changed in its module-docstring contract only — no route logic. Pinned by a
  new `test_camera_session.py` test (accumulation across sessions, including
  the failure path), proven to fail against the pre-change code. See
  [control-api.md](control-api.md#get-controlstatus).

  The consuming-in-HA idea — held back as a deliberate separate feature — is
  now DONE too, and shipped as **per-camera diagnostic sensors on the camera
  device rather than camera-entity attributes**: a Stream state enum sensor
  (the four `run_camera_forever` states pinned as options; an unknown future
  bridge state degrades to unknown with the verbatim value in a `raw_state`
  attribute, and `fps` / `hardware_encode` / `consecutive_failures` /
  `last_error` / `last_frame_at` ride along as attributes) and a Dropped
  frames `total_increasing` counter, both fed by a `StreamHealthCoordinator`
  polling `GET /control/status` every 30 s (loopback-only, zero alarm.com
  cost) and shared with `binary_sensor.py`, which adds a **camera-bridge
  connectivity entity** on the integration service device — ON while the
  last status poll got an answer, deliberately staying available when the
  poll fails so it can actually say "off". Sensors — not attributes — because
  an enum state and a `total_increasing` counter get recorder history and
  can drive automations directly, which attributes on the camera entity
  cannot. It was the MINOR call: shipped as its own new-capability release.
  See [ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity).

## Gaps found while implementing the stream-health entities

- **E52 (DONE) All ten translation files carry the identical key set.** Each
  of the nine non-English files (`ar,bn,es,fr,hi,pt,ru,ur,zh-Hans`) now holds
  the same 96 leaf keys as `en.json`, verified by a programmatic key-tree diff
  (zero missing, zero extra, per language). The 31 keys that were missing —
  the talkdown sensors, the camera diagnostic and solar sensors, and the
  entire `number` / `switch` / `select` / `button` groups — were translated
  per language against each file's established register (e.g. pt stays
  European Portuguese: "altifalante", "à chegada"; ru renders talkdown as
  "голосовое оповещение"; bn/hi/ur transliterate it as a feature name,
  matching how those files treat technical loanwords; brand terms TELUS /
  Bluetooth / Alarm.com stay untranslated everywhere, per each file's
  existing practice). Translations are model-written, not native-speaker
  reviewed — a fluent speaker may still improve phrasing, but the key sets
  are complete and any future drift is a one-line key-tree diff away from
  being visible.
