# Development & releasing

Contributor notes for working on this project and cutting a release.

## Repository layout

- `custom_components/telus_smarthome_alarmdotcom/`: the Home Assistant custom integration. See [ha-integration.md](ha-integration.md).
- `camera-bridge/`: the standalone camera-bridge container. See [camera-bridge.md](camera-bridge.md).
- `docs/`: this documentation set (start at [README.md](README.md) / [architecture.md](architecture.md)).

## Versioning & releases

This project follows [semantic versioning](https://semver.org/). **The single source of truth for the version is `custom_components/telus_smarthome_alarmdotcom/manifest.json` (`"version"`)**: Home Assistant displays it, and HACS uses it to detect that an update is available. `hacs.json` carries no version; the camera bridge has no separate version string (it builds from the working tree).

Bump the version on **every** user-visible change: this is easy to forget, so treat it as part of the change, not an afterthought:

| Change | Bump |
| --- | --- |
| **Minor change**: bug fix, tuning, tests, internal cleanup, a small addition nobody has to think about | **PATCH**: `x.y.Z` |
| **Documentation-only commit** | **No bump, no tag**: it rides untagged on master and ships with the next code release |
| **Major change**: a new capability, a changed default, anything a user would notice or have to react to | **MINOR**: `x.Y.0` |
| Breaking change (config/entity removal, incompatible behavior) | MAJOR: `X.0.0` |

Bias towards the patch position. A minor bump (`x.Y.0`) is for changes worth
announcing (two-way audio, the stop service, talkdown switching on by default)
not for everything that happens to add a line of code.

Release steps:

1. Bump `"version"` in `custom_components/telus_smarthome_alarmdotcom/manifest.json`.
2. Commit it: `git commit -m "Bump version to X.Y.Z"` (plus the change itself, if not already committed).
3. Tag it to match, and push:
   ```bash
   git tag vX.Y.Z
   git push origin <branch> --tags
   ```

**Keep the git tag and the manifest version in lockstep**: a checkout of tag `vX.Y.Z` should report version `X.Y.Z` to HA/HACS. Every version bump gets its tag.

A bridge-only change still rides in the same integration version bump; note it in the version's changes so the two components stay releasable together.

## Building & running locally

- **Camera bridge:** `cd camera-bridge && docker compose up -d --build` (needs `--network host` and a GPU render node for hardware encode; see [setup.md](setup.md) and [configuration.md](configuration.md)). Code is baked into the image, so a code change needs a rebuild.
- **Integration:** copy `custom_components/telus_smarthome_alarmdotcom/` into Home Assistant's `config/custom_components/` and add it via the UI, or install through HACS as a custom repository (see [setup.md](setup.md)). Integration code changes require a Home Assistant restart to load.

## Tests

There are two suites, one per component, run by two separate CI jobs on every
pull request. Both follow the same conventions and neither needs a test runner
installed.

| Where | Covers | Run it |
|---|---|---|
| `camera-bridge/tests/` | the bridge | `cd camera-bridge && TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_camera_session.py` |
| `tests/` (repo root) | the Home Assistant integration | `python3 tests/test_stream_health_sensors.py`, from the repo root |

CI runs **every** `test_*.py` in each directory, so a new file is picked up with
no list to update.

### The bridge suites

Conventions every suite follows:

- **No network, no account, no hardware.** Upstream is either a local aiohttp
  fake that enforces the real protocol (including HTTP Digest, where the device
  does) or a scripted stand-in class. Credentials are dummies: `config.py` only
  checks that they are present at import.
- **Each file is a plain script**, not a pytest module: it prints one line per
  test and ends in `RESULT: PASS` or `RESULT: FAIL (n)`, exiting non-zero on
  failure. There is no test runner to install.
- **Fixtures are captured, not invented.** `camera-bridge/tests/fixtures/` holds anonymized
  request/response bodies taken from real HAR captures, and a guard test fails if
  an account identifier ever lands in one.
- **Every suite's docstring names the invisible failure it exists to catch.**
  These cover behaviour that fails *silently* (a camera that freezes instead of
  erroring, a setting that reads back a value it never wrote) so "what would
  this look like in production" is stated up front rather than left to be
  rediscovered.

| Suite | What it pins |
|---|---|
| `test_camera_session.py` | The per-camera reconnect loop, including the device-side `no_media` state and the opt-in kick (both a healthy peer and the no-media signature are required; exactly one kick per cooldown; off by default; a failing kick is recorded and never ends the loop): rotation before Alarm.com's ~180s server-side kill, the ffmpeg publisher that outlives it, the frame hold that bridges reconnect gaps, fps measurement, including that the rate is **adaptive rather than latched**, since it is a device setting that changes with no restart: the session-start burst is not measured at all (pinned with a *plausible* 40 fps burst, because an implausible one only tests the sanity range), one settled batch never acts alone, a flapping source converges instead of oscillating, the hysteresis band is asymmetric because declaring too low DROPS frames while declaring too high only duplicates them, the rebuild happens at a rotation rather than by failing the session, and the frame hold keeps repeating at the rate the running ffmpeg was declared with while a change is pending; sink repair, the consecutive-failure escape hatch, the per-camera `dropped_frames` accumulation into `/control/status` (on healthy rotation and failure paths alike), the first-frame timing diagnostic on both the success line and the timeout message, the per-camera `JANUS_REQUEST_AUDIO` gating (a named camera gets the audio keys, an unnamed one in the same run keeps the byte-identical video-only body), and the `JANUS_CONSUME_AUDIO` audio-publish path: off by default with an ffmpeg command line pinned byte for byte and free of every audio token, and the offered track still drained; on, the SAME publisher grows a second input rather than being rebuilt; the video input identical either way, declaring the camera's rate with `-framerate <fps>` and never with `-r` (which would override `-use_wallclock_as_timestamps`, publish less media than wall time, and stall the audio pipe when a second input shares that timeline); the rotation gap filled with silence whose sample count comes from elapsed wallclock rather than from counting loop iterations; a changed raw PCM shape forcing a rebuild the way a changed frame size does, at a ROTATION rather than by failing the session, and bounded per camera per process (a shape that keeps moving degrades to video-only instead of buying a reader-kicking rebuild per rotation); the publisher built video-only until a real audio frame has been decoded, so a camera whose codec sniff cannot finish inside the sizing window converges on two publishers instead of looping on one per session, and a camera that never sends an audio packet publishes video forever with no audio input and no failed session; every encoder variant (vaapi/nvenc/software) carrying the audio input and encoder; CONSUME-without-REQUEST degrading to off with exactly one warning per camera; and `/control/status` reporting only audio that was actually decoded: `audio_format` null and `decoded_audio_frames` zero for a camera that has decoded nothing, rather than the confident `48000Hz/2ch/s16` that the launched-with fallback used to print over eleven consecutive silent sessions, and `publisher_audio_format` describing the running publisher beside it. Also that the whole audio path works at the rate the cameras REALLY send, 8 kHz mono rather than the 48 kHz stereo every audio constant was written beside: the rate reaching both the raw input and the AAC encoder on all three encoder variants, the pipe primed with 100 ms of TIME rather than a fixed sample count, the wallclock fill and its per-call ceiling holding exactly, the cold-start rebuild from a video-only publisher to the observed shape, and `/control/status` naming the codec (`audio_codec: "pcmu"`) beside the shape. Also the **pacing** the whole audio path stands on, which is invisible in production because it costs *video* frames rather than audio: the silence fill's sample count comes from elapsed wallclock (asserted exactly, against a scripted clock), no single write hands ffmpeg more than the 100 ms lead the pipe is primed with, a clipped deficit is repaid across bounded calls rather than in one burst, a quiet camera is topped up faster than that lead decays, and the frame hold tops up on the audio deadline rather than once per repeated video frame (167 ms on the 6 fps camera). Plus the three guards around the publisher: the decoded-video queue is sized in **time** (~500 ms) rather than in a flat frame count that meant 267 ms on one camera and 667 ms on another; an audio pipe ffmpeg has stopped reading degrades the sink to **video-only** instead of rebuilding the publisher and kicking every RTSP reader; and `write_frame`'s drain is **bounded**, so an ffmpeg that is alive but no longer consuming is detected and rebuilt rather than parking the camera's loop forever with the status frozen on `streaming`. Also drives the real `JanusCameraSession` `close()`/keepalive against fake pc/websocket objects: the gateway websocket must be released even when `pc.close()` raises or is cancelled, and the keepalive task must end quietly (yet stay cancellable) when the websocket drops between keepalives. Finally the **handover instrumentation** (`HANDOVER_TRACE`, off by default): that the default emits nothing and builds nothing, since a diagnostic on by default would be measuring a window it is itself changing; that a traced rotation emits one record whose marks span the whole window, in the order the loop actually does things (the hold stops the previous audio pump *before* it writes its first silence, and inverting that shows up as a wrong reading rather than a cosmetic one); that the record's `adv` comes from the audio timeline rather than from time passing, so an interval where the timeline stopped is visible as such; that the muxer lead is derived from the sink's own sample counter and is allowed to go NEGATIVE, which is the failure it exists to report; that a drop from before the rotation is not counted inside the window; that a retired session is snapshotted rather than held, so the record cannot keep a 25 MB decoded-frame queue alive; and that drop timestamps are kept only while tracing is on. Finally the **retention bound** the audio path has to hold across rotations (E83): after a dozen rotations of consumed audio, no retired session is still reachable (one owns a decoded-frame queue of up to 8 x 3.11 MB, so holding one holds that with it), no decoded audio frame outlives the write that consumed it past the session's own bounded queue, and the object count does not grow with the number of frames DECODED -- which is the shape of the climb E83 measured, about 6 kB per decoded frame. |
| `test_camera_settings.py` | The website settings wire shape (JSON:API on read, a flat object on write) reconstructed from captured fixtures, plus capability gating, bounds and option-list handling, and the per-request 35s ceiling each diagnostics read carries over the shared session's 15s default (without it, a slow-but-healthy camera's signal read is cancelled by the bridge's own client and answered as a 502). |
| `test_control_server_routes.py` | The camera-settings, siren and privacy control routes: status codes (including the honest 429 + `Retry-After` on a rate-limited settings/diagnostics read or settings write), error vocabulary, and the fields the integration reads back, plus the events/history `limit` clamp, that a failed snapshot fetch is logged without its signed URL, that `/control/status` serves the process-wide audio decode counters whole and live (the log line that hid a wholesale codec mismatch is once per session, so the counters are the thing that has to stay reachable), and that the `control_secret` file ends at mode `0o600` however it arrived (generated fresh, or adopted group/world-readable and re-tightened with the secret unchanged). |
| `test_credentials_fallback.py` | The credentials.json fallback reader and watcher: a torn write (invalid UTF-8) or a wrong-shape file must degrade to the .env credentials instead of killing the process into a login-hammering restart loop, and a valid file must still be adopted. |
| `test_device_kick.py` | `bridge/device_kick.py`, the opt-in escalation for a wedged camera: the cycle is exactly alternate-then-original with the original taken from the device, an unsupported camera (not settable, one option, no options, no value) gets zero writes, a failed first write changes nothing, and a failed restore raises naming the value the camera was left at. Pinned because both failure shapes are silent in production: a no-op kick that counts as sent, and a camera silently left re-configured. |
| `test_cloud_hold.py` | The process-wide cloud hold (`bridge/cloud_hold.py`, backlog E104) and the one re-login-and-retry path every website call takes: an expired cookie still costs one login and one retry with no hold; a 401/403 that survives the fresh login holds, and the website is not touched while held (the scripted call's count is the assertion); the hold doubles per strike to the cap and a success resets it; an explicit edge wait (429 header, parsed `RateLimited`, rate-limited login, 429 on the retry) holds for the server's margined wait and a longer hold is never shortened; other errors pass through; and the control routes answer a held call `429` + `Retry-After`, never the `502` that sent Home Assistant straight back. Also pins that `RATE_LIMIT_RETRY_MAX_S` does not truncate the observed 1825 s window. |
| `test_arming_devices.py` | The Bluetooth arming-device client and its routes, including the irreversible unpair. |
| `test_partitions.py` | Selective sensor bypass: above all the refusals, each of which is a case where writing anyway would leave a zone silently unprotected. |
| `test_mws_json_actions.py` | The JSON-envelope MWS actions (camera siren, privacy mode) and the dual XML/JSON ack dispatcher. |
| `test_parsers.py` | The pure parsers that turn Alarm.com's XML into the values the bridge acts on. |
| `test_history_events.py` | The Activity-feed parse: that `eventTypeName`/`globalDeviceId` survive the rename into `event_type_name`/`device_id`, which is the entire basis on which Home Assistant turns a feed entry into a doorbell-press event; that the same press parses identically whether its description arrives in French or English (so matching the enum is locale-proof and matching the text would not be); and that clip enrichment fills a correlated entry, leaves an uncorrelated one explicitly null, and costs one batched call per page. |
| `test_talkdown_streaming.py` | Talkdown's streaming encode, schedule-based pacing, in-place recovery, certificate pinning and endpoint selection. |
| `test_talkdown_renegotiation.py` | Talkdown session renegotiation, against a fake doorbell that really checks Digest auth. |
| `test_sip_call.py` | The VoIP.ms call leg, against a fake POP that independently verifies the digest, answers with SDP and collects the RTP: a wrong digest, an unusable SDP answer, RTP from the wrong socket or a missing BYE each fail silently in production as a phone that rings wrong or never rings. |
| `test_call_bridge.py` | The call bridge's wiring: the mu-law table against the reference encoder, the live source's silence-fill and freshest-audio-wins buffer, the decoded-audio tap's bound and cleanup, and that a bridged call wires both directions and tears the tap and talk leg down on hangup, talkdown failure or a failed dial -- each of which otherwise looks healthy while one half is silent. |
| `test_metrics.py` | The talkdown metrics counters. |
| `test_memory_trace.py` | `bridge/memory_trace.py`, the off-by-default container-memory instrumentation. That it is off by default and builds nothing when off, since `tracemalloc` running by accident would add allocation cost and traced memory to the very process whose memory is under investigation, and the only symptom would be a slightly worse curve -- indistinguishable from the leak being measured. That a record NAMES what actually grew: a deliberate 8 MB allocation between two records has to be the top row by growth (not by size) and its type has to appear among the gc movers, because a record that emits well-formed JSON while pointing at the sampler's own allocations would send a session at the wrong target. That the record decomposes the processes sharing the container's `mem_limit`, driven against a **scripted** `/proc` rather than the real one -- the largest-first check passed against a build with the ranking deleted when it read the host's own table, because this host's first entries are the test process followed by kernel threads with no RSS, which is already descending. That the sampler keeps exactly one previous snapshot, so watching for a leak is not itself one. And that every path is total and the record survives `json.dumps`, since it runs in a task inside `main()`'s gather and is emitted as one log line. |
| `test_opus_decoder_patch.py` | `bridge/aiortc_opus_patch.py`, against the real aiortc, the real libopus and the real libavcodec mu-law decoder. The codec mislabel: that both cameras' CAPTURED mu-law payloads are rejected by libopus (the counterfactual) and are then decoded as 8 kHz mono s16 for **100%** of packets rather than the ~6% the advertised codec managed; that a real Opus stream still commits to Opus and loses nothing, because the sniff window HOLDS its frames rather than dropping them; that an accidental Opus success inside a mu-law stream neither flips the decision nor escapes as a 48 kHz frame (one such frame rebuilds the publisher and kicks every RTSP reader); that every packet is counted in exactly one of `decoded_opus`/`decoded_pcmu`/`discarded`, which is what makes a wholesale mismatch impossible to miss where one log line per session was not; that a decoded frame's shape names the decoder that produced it, which is how the per-camera `audio_codec` is derived; that a packet the committed decoder rejects is dropped rather than fatal and the SAME decoder still decodes the next packet; that aiortc's own `decoder_worker` thread survives it, keeps delivering frames, and holds one codec for the session (the actual outage: the thread dies once and the published track is silence for the life of the session, while every health signal still says `streaming`); that the commit is logged once per session and the drop once per session with the packet's length and first 16 bytes (they are what identified the packet as G.711 mu-law); and that the import-time compatibility guard fires on every incompatible upstream shape (either decoder class or method moved, module executed twice, upstream grown its own exception handling, either declared sample rate moved) while still patching an aiortc shaped like the pin. |

### The integration suites

`tests/` at the repository root covers `custom_components/telus_smarthome_alarmdotcom/`.
Same conventions as the bridge's (plain scripts, one line per test, ending in
`RESULT: PASS` / `RESULT: FAIL (n)` and exiting non-zero) and the same
requirement that each suite's docstring names the invisible failure it exists to
catch. Run one from the repo root:

```bash
python3 tests/test_stream_health_sensors.py
```

The only third-party package needed is **aiohttp**, which is what CI installs.

**Home Assistant is deliberately not installed; it is stubbed.** The integration
imports `homeassistant.*` at module scope, so something has to answer to that
name. `tests/ha_stubs.py` registers a small package tree in `sys.modules` before
the integration is imported, so the **real** integration modules load and the
real derivations run against stand-in base classes. The reasoning, in full:

- `pip install homeassistant` does resolve on the interpreter CI pins, but pulls
  in the order of a hundred transitive packages: grpcio, boto3, pillow,
  cryptography, bleak/dbus-fast, zeroconf, SQLAlchemy, and PyRIC, which has no
  wheel and builds from source, to supply a handful of base classes.
- Exercising entity behaviour for real on top of that needs
  `pytest-homeassistant-custom-component`, which pins one exact Home Assistant
  version. That is a version-lock treadmill this repo gets nothing back from, and
  a second test framework alongside the bridge's plain scripts.
- The logic worth pinning is pure: a counter compared across polls, a string
  classifier, an id-set dedup. None of it needs an event loop, an entity
  registry or a state machine.

**The limit of that choice, stated so nobody mistakes it for coverage it isn't:
the stubs assert nothing about Home Assistant's real API.** If upstream changes
what `CoordinatorEntity` promises, these suites stay green. They pin *our*
derivations, not our compatibility with Home Assistant. Two things narrow the
gap deliberately, and both must be preserved when the stubs are extended:

- Each stub reproduces the upstream behaviour the integration actually leans on
  and nothing else: `CoordinatorEntity.available` really is
  `coordinator.last_update_success`, and `EventEntity._trigger_event` really does
  reject a type outside `event_types`.
- Nothing in the stub tree is auto-magic: no `MagicMock`, no `__getattr__` that
  invents attributes. An integration module that starts using a name the stubs
  do not define fails loudly rather than exercising a fiction. The same applies
  to the three-attribute `pyalarmdotcomajax` stub. (`controller.py` and the three
  import-time patch modules are stubbed wholesale, because importing them applies
  the monkeypatches and needs the real library; the patches are guarded at import
  time and exercised by CI's `check_patch.py` job instead. Two suites load a real
  file over those stubs on purpose: `ha_stubs.load_real_controller()` and
  `ha_stubs.load_real_auth_patch()` give the library stub the names the file
  reads at import and load it in place, and the stand-ins they add are scripted
  and recording, never magic.)

| Suite | What it pins |
|---|---|
| `test_stream_health_sensors.py` | The bridge-fed stream-health sensors, including that `no_media` is a reportable state rather than one that degrades to unknown. Above all `CameraAudioStateSensor`'s derivation: the four states, and the four ways a false `stalled` could be fabricated: an unanswered poll (both the `last_poll_ok` flag and the identical payload object it hands back), a failed poll, a counter reset on a bridge restart, and the first poll of the entity's life. Also the per-poll delta (suppressed rather than negative across a reset), that `not_consuming` drops the remembered count so re-enabling audio starts a fresh comparison, that a bridge too old to report `audio_consumed` reads unknown rather than a fabricated `not_consuming`, and that every derivable state is inside `_attr_options` (an ENUM sensor whose state falls outside its options errors on every update). Plus `CameraStreamStateSensor`'s payload mapping and its degradation of an unknown bridge state to unknown-with-`raw_state`, `CameraDroppedFramesSensor`'s mapping and `total_increasing` class, and the coordinator's two failure flags: an unanswered `/control/status` keeps the previous payload rather than flapping every entity to unavailable, a transport failure raises `UpdateFailed`, and both record `last_poll_ok = False`. |
| `test_event_entities.py` | The Activity-feed event entities. The doorbell matcher keys on TELUS's untranslated `event_type_name` enum: a French-described press fires, and an entry whose *description* says "Button Push" while its enum says otherwise does not; this account's feed has been observed in both French and English for the same event kinds, and a text match would simply stop firing the day the account locale changed. Also the per-page id-set dedup: a restart baselines on the current page and replays nothing, two presses sharing one timestamp both fire (a high-water mark would suppress the siblings forever), an empty page is a failed poll that neither becomes nor clears the baseline, and two presses in one poll fire oldest-first with one state write each. Plus `_classify` across both locales and the containment of its output in `EVENT_TYPES`, `_event_time` ordering across the two timestamp formats TELUS emits (a string sort gets them backwards), `_is_doorbell`'s any-one-of-three chime capabilities, and `TelusActivityEvent`'s single extra fire when TELUS attaches a clip a poll or two after the entry. |
| `test_bridge_hold.py` | `RateLimitHoldMixin` (`bridge_client.py`, backlog E105) on the real TELUS-backed coordinators: a bridge `429` keeps the last page instead of failing the poll, the coordinator makes **no bridge call** while held (the scripted `bridge_get` counts calls), the hold lasts the bridge's bounded `Retry-After` and expires on its own, the settings sweep stops at the first 429 and keeps every camera, other errors are still `UpdateFailed`, and the loopback-only camera-list coordinator is unchanged. |
| `test_ws_backoff.py` | `ws_backoff.reconnect_delay` (backlog E106): a socket that connected reconnects on the configured delay, failed attempts double it up to the cap, and the cap can never make failures reconnect faster than successes. |
| `test_clip_links.py` | The signed clip links (`clip_view.py`): the one unauthenticated, internet-reachable surface the integration adds. The HMAC binds the event id AND the expiry (a signature for one id authorises nothing about its near-sequential neighbour; a client cannot stretch its own link's life), the 401-vs-410 split is only reachable with a valid mac, a non-ASCII signature is a clean 401 rather than the 500 `hmac.compare_digest` would raise, a malformed event id is refused without echoing its bytes into the log, any configured entry's secret validates, every bridge failure maps to its documented status (404/503/504) instead of a traceback, and every response -- success and refusal alike -- carries the no-store/no-referrer hygiene headers. |
| `test_device_links.py` | The device-registry links (`base_device.py`, `arming_devices_coordinator.py`'s `arming_device_device_info`): `DeviceInfo` carries `via_device_id`, the partition's (or the integration service device's) registry id looked up scoped to the owning config entry, and never the deprecated identifier-tuple form; a device whose partition has no registry entry yet comes up unlinked rather than failing to build, and the warning for that is logged once per partition per controller rather than once per entity (every sensor, lock and attribute entity behind the partition takes the path); the partition never links to itself; a device with no partition carries no link. Nothing about a wrong link errors in production: the device page simply shows no "connected via", or the platform fails at setup. |
| `test_controller_session.py` | The controller's ownership of its aiohttp session (backlog E108): it creates the session with `auto_cleanup=False` and hands that same object to the library, `stop()` releases it with `detach()` and never `close()` (Home Assistant's sessions share one connector, and closing a session closes it for everyone), and releasing twice detaches once. |
| `test_config_flow.py` | The GUI config/reconfigure/reauth flow (`config_flow.py`): the happy path (title, unique id, trimmed credentials, default options), the full exception-to-error-key mapping on the user and reconfigure steps (whether an unreachable TELUS reads as "cannot connect" or as a wrong-password accusation), the OTP branches, the reauth/reconfigure merge-not-replace invariant (a reauth that replaced entry data would drop `clip_link_secret` and 401 every clip link already delivered to a phone), the cross-account and duplicate-account aborts, session cleanup per retyped password, the options flow (its handler never assigns the read-only `config_entry` property), and the credential fallback file's mode, atomicity and never-raise contract. |
| `test_speak_service.py` | `telus_smarthome_alarmdotcom.speak` / `stop_speaking` (`speak_service.py`): registration and removal (and that this stays a service, not an entity), schema validation (message/media_content_id exclusivity), audio-url resolution (http passthrough, TTS render, relative-to-absolute with a clear error when HA has no internal URL), default-target selection (doorbells first, every bridge camera as fallback), the exact POST body and 70 s timeout, the whole status mapping (the 413/504/400 wording is the only way a user learns their clip was too big, played too long or could not be fetched; a 409 busy camera is not an error, or an automation retries into it), and `stop_speaking`'s deliberate 404 tolerance. |
| `test_bypass_unpair_services.py` | `telus_smarthome_alarmdotcom.bypass_sensors` and `.unpair_arming_device` (`bypass_service.py`, `unpair_service.py`), the two most consequential calls in the integration: every bypass refusal (no sensors, overlap, unknown/foreign/non-sensor entities, mixed accounts, unloaded entry, partition disagreement, unknown partition), both permission gates, the pre-read failure mapping, the exact flat POST body, the write-path status mapping (422/429 + `Retry-After`/404/400/502), the unreadable-2xx-is-success rule (a false failure invites a second write at a live panel) and the delayed 15 s refresh (an immediate one reads the pre-bypass state back and paints the call as a no-op); for unpair, the single-entity schema (a list must not validate), the resolution refusals, the DELETE shape, the 404/429 mapping and that no refresh is spent confirming the deletion. |
| `test_camera_settings_entities.py` | The bridge-served camera settings plumbing (`camera_settings_coordinator.py`) and the camera-backed entities in `number.py` and `switch.py`: `async_bridge_post`'s status mapping (422/404/429 with the `Retry-After` header and the body fallback), the coordinator's degradation rules (no secret keeps the page, one camera's failure keeps its last entry and never blanks the others, a 404 on both routes is absent-not-empty), the echoed-section fold, the add-as-capability-appears helper, the volume and LED-brightness numbers (gating, device-reported bounds and their fallbacks, 0 is a value, exact write bodies), every switch toggle's support-flag gate and read/write key pair (the chime route reads `indoor_chime_on` but writes `indoor_on`), the indoor-chime unavailability rule, the privacy switch's read-back rule, and platform setup gating. Every failure here is silent: a gate that flips is a switch the camera refuses with a 422, or a slider that quietly never appears. |
| `test_select_entities.py` | Every select entity (`select.py`): the camera chime type, notification format and live resolution selectors, the per-phone enrolled-partition picker and the panel Bluetooth disarm-window picker -- each selector's label/value mapping and its documented fallback or refusal when the labelled list is lost (a label mapped to the wrong value writes the wrong chime type, partition or resolution to real hardware with a 200 in response), `current_option` honesty, the stale-option race refusal, exact flat write bodies, echo adoption with refresh fallback, the chime-disables-indoor-chime notification (E37), the partition picker's availability gates and its absence on a single-partition account, and `select.py`'s setup gating. |
| `test_arming_entities.py` | The Bluetooth arming-device and panel Bluetooth plumbing (`arming_devices_coordinator.py`, plus `switch.py`'s disarming and pairing switches), which decide whether a phone walking up to a real house disarms a real alarm and are built to fail CLOSED: `_async_bridge_write`'s full refusal mapping (422 per-route wording, 404, 429 with `Retry-After`, 400, 502, unreadable-2xx-is-success, transport-failure wording that warns the change may have landed), the write helpers' exact URLs and flat bodies with real JSON types, both coordinators' degradation rules (a 404 route drops the data, no secret keeps it, 401/429 are `UpdateFailed`, id-less records are dropped), the echo-fold guards, and each switch's capability gates (the gate, not the value, is the permission) and real-false-vs-unknown split. |
| `test_binary_sensors.py` | `binary_sensor.py`: the `is_on` mapping across sensor and water-sensor states (None for an undecodable state), that an undecodable state is logged ONCE per episode at INFO naming the device and at DEBUG after that (`is_on` runs at least twice per refresh, so one zone sitting in UNKNOWN wrote the same "sensor None" line thousands of times a day), the stale-state availability guard on both legs (this platform once kept presenting a door's last "closed" as current after TELUS auth died, with nothing indicating staleness), the door/window name classification across languages and the subtype-to-device-class table, and the bridge-connectivity sensor's deliberately inverted availability contract (it stays available while the bridge is down, because an entity that goes unavailable exactly when it should say "off" can never say "off"). `async_setup_entry` is deliberately not covered. |
| `test_init_lifecycle.py` | The entry lifecycle (`__init__.py`): the auth/transport exception mapping at setup, the clip-link secret's generate-once-never-rotate rule, the view's once-per-HA-run registration, the bridge-down-at-startup tolerance, the whole device-pruning matrix (which has shipped four distinct silent failures: this fork's own bridge and service devices deleted and recreated on every restart, and, after that was fixed by id shape, the bridge cameras and paired phones still deleted on every boot and reload because their ids share the library's `<unit>-<n>` namespace -- the gate is now the library's manufacturer stamps), Skybell virtual-device creation, the partition and service devices registered ahead of the platforms so every `via_device_id` lookup resolves, platform forwarding, service registration, the v1-to-v4 options migrations, and unload's pop/stop/unregister ordering. The websocket monitor's runtime behaviour is deliberately not covered (it needs the real library). |
| `test_login_error_classification.py` | What a TELUS login failure is allowed to mean (`telus_auth_patch.py`, backlog E101): step 1's explicit credential rejection is the ONLY `AuthenticationFailed`; once the password has been accepted, every later failure (flow completion, SSO landing elsewhere) is `UnexpectedResponse`, so the entry auto-retries instead of sitting dead behind a reauth prompt that re-entering the correct password was never going to fix. Needs the real `pyalarmdotcomajax`, so it skips (explicitly) in the stubbed job and runs in the patch-smoke job. |
| `test_login_2fa_probe.py` | What the login does with the 2FA-requirements probe's answer (`telus_auth_patch.py`, the step after the SSO handoff), loaded through `ha_stubs.load_real_auth_patch()` with a recording stand-in for the library's server-error handler: a JSON:API error body (the `423` this white-label answers on every login) never reaches that handler -- which logs such a body at ERROR with a traceback before raising, an error line per login and reload for a non-error -- and is one INFO skip line naming the status; a clean body still reaches the handler and is parsed afterwards; a non-JSON body and a connection error still take the skip path rather than failing the login. |

**Not covered by these suites**: verify these by reading the code, compiling it,
and checking Home Assistant's logs, and see `docs/backlog.md` (E66): the
library-backed platforms `alarm_control_panel.py`, `lock.py`, `light.py`,
`cover.py` and `climate.py`; `camera.py`'s camera entity (only its loopback
camera-list coordinator is exercised, by `test_bridge_hold.py`); `button.py`;
`sensor.py` beyond the stream-health sensors (the auth-health, activity-feed,
talkdown-metrics and camera diagnostic sensors); `base_device.py` beyond the
partition link and its once-per-partition warning; `controller.py` beyond its
session ownership (login, reauth and the websocket monitor); `bridge_client.py`'s
`bridge_get` and `read_control_secret` (only the hold mixin is pinned);
`diagnostics.py` and `device_type_langs.py`; the rest of `telus_auth_patch.py`'s
SSO flow (the identity parse) and both websocket patches, whose only check is
CI's import-time guard (`check_patch.py`); and, in `__init__.py`, the websocket
monitor's runtime behaviour.

### Keeping the tests current

Tests are part of a change on the same terms as the docs: **anything that alters
behaviour a suite covers updates that suite in the same commit.** The whole point
of these suites is that the behaviour underneath them fails quietly (a frozen
camera, a setting that reads back a value it never wrote) so a suite that has
drifted out of step with the code is not merely stale, it is actively
misleading: it reports green over the exact class of bug it was written to catch.

| Situation | What the change must include |
|---|---|
| New behaviour in a module a suite already covers | a test for it, in that suite |
| An invariant deliberately changed: a timeout, a wire shape, a refusal | the test updated to pin the **new** invariant, and its docstring rewritten to say why |
| A test starts failing | either the code is wrong and gets fixed, or the invariant genuinely moved and the test is rewritten, deliberately, and said out loud in the commit message |
| A new module whose failures would be quiet | a new suite following the conventions above, and its row added to the table |
| An upstream response shape changes | the fixture recaptured and re-anonymized, never hand-edited to fit |

**Never make a test pass by weakening what it asserts.** Not by deleting it, not
by skipping it, not by loosening a bound until it stops complaining, not by
narrowing a scenario until the interesting case is gone. A red test is
information. The only two honest responses are to fix the code, or to change the
invariant on purpose and say so.

**Prove a new test can actually fail.** A test that has never been red is an
assertion nobody has checked. For each invariant a new test claims to pin, break
that invariant in the source, confirm the suite reports the failure, then revert,
and treat a test that stays green against deliberately broken code as
*worthless until strengthened*, not as a curiosity. The first thing that check
catches is the cheapest mistake: a new test that was never added to its file's
`TESTS` list "passes" by never running at all, and the deliberate-break run is
what exposes it (it has happened here). Also clear `__pycache__` around these
experiments: CPython validates bytecode by size and mtime, so reverting to a
file of identical size within the same second can silently run the broken
version's cached module. Both integration suites and
`test_camera_session.py` were validated this way, and it has repeatedly found
tests that only looked strict; it also documents which halves of a condition are
genuinely load-bearing (in `_classify`, the French *person* needle is redundant
because `"personne"` contains `"person"`, while the *vehicle* needles are not;
that is recorded in the test itself so nobody re-derives it).

Two practical notes. A new `test_*.py` in either tests directory is picked up by
CI automatically: there is no list to add it to, which is why the tables above
are the thing that goes stale and has to be updated by hand. And a test asserting on timing needs
enough margin to survive a loaded CI runner while still failing loudly on the
regression it targets; prefer a scenario where the right and wrong answers are
far apart over a tight numeric bound that merely looks precise.

## Anonymization note for docs

The docs in this folder are written to be shareable: they describe the software generically and use placeholders (`<ha-host>`, `<doorbell-ip>`, `<unit-id>`, `<did>`, `<mac>`, …) instead of any real deployment's IPs, account/device IDs, hostnames, paths, or secrets. Keep new docs to the same standard.


## Dependency updates (automated)

Upstream versions are surfaced automatically, but applied deliberately -- the
integration **monkeypatches `pyalarmdotcomajax` internals**, so a blind bump can
break login. The setup:

- **[Renovate](https://docs.renovatebot.com/)** (`renovate.json`) opens update
  PRs for everything pinned: `pyalarmdotcomajax` (via Renovate's built-in
  `homeassistant-manifest` manager, which reads the `requirements[]` array in
  `manifest.json`), the bridge's `requirements.txt`,
  the Dockerfile base image, and the `MEDIAMTX_VERSION` Dockerfile `ARG` (a
  custom regex manager against GitHub releases -- the only pin with no built-in
  equivalent). *Enable it by installing the Renovate GitHub App on the repo* --
  the config alone does nothing without it. (Dependabot is a zero-install
  fallback but can't cover the MediaMTX `ARG`.)
- **Auto-merge policy** (in `renovate.json`): low-risk minor/patch updates
  auto-merge once CI is green; **high-risk ones (`pyalarmdotcomajax`, `aiortc`,
  `av`, `mediamtx`, `aiohttp`, `websockets`) and the `python` interpreter/base-image
  pair never auto-merge** -- they get a `review-required` label.
- **CI smoke test** (`.github/workflows/ci.yml`) runs on every PR:
  1. installs the integration requirements and imports `telus_auth_patch.py`,
     which runs its compatibility guard -- so a `pyalarmdotcomajax` bump that
     moves an internal the patch depends on **fails the PR loudly** instead of
     breaking login at runtime;
  2. installs the bridge `requirements.txt` with `--only-binary :all:`, catching
     an `aiortc`/`av` bump that has no manylinux wheel (which would otherwise try
     to build from source in the image).

**When a `pyalarmdotcomajax` bump PR appears:** if CI is red, the guard caught a
moved internal -- update `_telus_async_login` / the patched attributes in
`telus_auth_patch.py` to match the new version, then bump. If CI is green, the
patch still applies, but still skim the upstream changelog before merging.


## Dependency hardening

A dependency-fragility audit of both trees drove the following changes. The theme:
this project's integration deliberately monkeypatches a library, so the risk is not
"a bump fails loudly" but "a bump changes an internal and something breaks quietly."

**Applied:**

- **Compatibility guards on all three monkeypatches.** `telus_auth_patch.py` now also
  guards the private coroutine it calls, the `pyalarmdotcomajax.const` values it
  rewrites, and the extension class attribute it overwrites;
  `telus_ws_heartbeat_patch.py` and `telus_ws_push_patch.py` gained guards of their own
  (they had none, and both wrap private coroutines). The CI smoke test now imports all
  three and, because the replaced `async_login` populates ~11 PRIVATE INSTANCE
  attributes that no class-level check can see, constructs a throwaway controller and
  asserts each one.
- **A fourth monkeypatch, on the bridge side: `bridge/aiortc_opus_patch.py`.** It does
  two things, and neither is optional for downstream audio on this account. First, the
  gateway advertises the audio track as Opus and sends **G.711 mu-law at 8 kHz**, so the
  patch sniffs the first 20 packets of each session with both decoders (aiortc's own
  `OpusDecoder` and `g711.PcmuDecoder`), commits to one for the session's life, and holds
  the sniffed frames until the decision so no frame escapes in the losing codec's shape.
  Second, aiortc's `OpusDecoder.decode` has no error handling where its
  `H264Decoder.decode` does, and `rtcrtpreceiver.decoder_worker` -- a thread target --
  has none either, so one undecodable packet permanently kills a session's
  `audio-decoder` thread and the published audio track becomes pure silence with every
  health signal still green. The repo does not vendor aiortc, so this follows the
  integration's model: an import-time replacement carrying an import-time compatibility
  guard, which refuses to apply if either decoder class or method moved, if it is being
  applied twice, if upstream has grown its own exception handling (the shape a fix to the
  second half would take), or if either decoder's declared sample rate moved (the
  status field's shape-to-codec mapping depends on 8000/48000). The guard is exercised by
  the every-bridge-module-imports CI job and by `tests/test_opus_decoder_patch.py`. Not
  fixed in aiortc 1.15.0 (the pin) or in upstream `main`.
- **Stopped mutating aiohttp's `ClientSession`.** The heartbeat patch used to assign
  `session.ws_connect = ...` and `session._telus_hb_patched = True`; aiohttp warns on
  any attribute outside its `ATTRS` and that surface is slated to close in 4.0. It now
  wraps the session in a small transparent proxy that injects `heartbeat=`, keeping the
  mutation on the library object we already patch.
- **`web.AppKey` instead of plain-string `Application` keys** in the bridge's control
  server (aiohttp emits `NotAppKeyWarning` for str keys; `AppKey` has existed since 3.9).
- **One `Retry-After` parser** (`config.parse_retry_after`) replacing three copies that
  understood only delta-seconds. RFC 9110 also permits an HTTP-date, which previously
  fell through to a fixed 60s -- the exact "fixed short retry re-triggers the same
  window" behaviour that once caused a multi-hour outage. It now parses both and logs
  when it falls back.
- **Socket timeouts on the talkdown PUT session.** The safety deadline is only checked
  between chunks, so a single wedged PUT could pin the per-camera talk lock for
  aiohttp's 300s default; now `sock_connect`/`sock_read` are 5s.
- **`DigestAuthMiddleware`** replaces the hand-rolled RFC 2617 implementation (see [backlog.md](backlog.md) E21).
- **Hardened XML parsing** in `mws_client.py` and `mobile_auth.py`: both import
  `defusedxml` unconditionally at module level, so a build without the hardened
  parser fails at import (the every-module-imports CI job catches it) instead of
  silently parsing with stdlib `ElementTree` and losing entity-expansion
  protection. `defusedxml` and `yarl` (imported directly but previously
  undeclared) are declared in `requirements.txt`.
- **Public aiortc imports** (`MediaStreamError`/`MediaStreamTrack` come from the package
  root) and corrected `websockets` type annotations (`ClientConnection`; the legacy
  `WebSocketClientProtocol` alias has not been what `connect()` returns since 14.0).
- **Typo-proof `UnkonwnDevice` import** -- the name is misspelled upstream, so a future
  spelling fix would have been an `ImportError` at integration load.
- **Dropped the unused `beautifulsoup4` requirement** (nothing imports it -- and it was
  in Renovate's auto-merge group, i.e. auto-merging a dependency nothing uses).
- **Renovate:** `aiohttp` and `websockets` added to the review-required group; they carry
  the actual deprecation exposure.
- **New CI job: every bridge module must import.** `py_compile` only catches syntax
  errors; a broken import list is a runtime `NameError` that surfaces as a
  restart-looping container. This was added after exactly that happened during the audit
  fixes.

**Considered and deliberately NOT changed:**

- **`climate.py`'s `self.__dict__.pop("_attr_max_temp")`.** The audit suggested assigning
  the default explicitly, but `ClimateEntity.max_temp` is a *property object*, not a
  value, and HA's default converts `DEFAULT_MAX_TEMP` into the entity's temperature unit.
  Assigning either would break Fahrenheit setups. Removing the attribute so HA's own
  default property applies is the correct behaviour and is already documented in place.
- **`clip_view.py`'s hand-rolled HMAC signing.** HA ships `async_sign_path`, but its
  signatures are bound to a refresh token and die when it is revoked -- incompatible with
  the requirement that links inside already-delivered notifications keep working. The
  local implementation embeds the expiry in the MAC and uses `compare_digest`. Keep it;
  revisit only if the notification-longevity requirement is dropped.

**Still open (low priority):** `pylibsrtp` is pinned though only referenced in a
commented-out fallback (it arrives transitively via aiortc anyway). (Entity IDs are
not exposed to a `pyalarmdotcomajax` class rename: `device.__class__.__name__`
appears only in `base_device.py` log lines; unique IDs come from `device.id_` and
display names from `device.name` plus each platform's `_device_type_name` literal.)



**Python moves in lockstep.** CI's `setup-python` mirrors the runtime image
(`camera-bridge/Dockerfile`'s `FROM python:<ver>-slim`); testing on an
interpreter the container does not run is worse than not testing. Renovate
reports both under the dep name `python` and is configured to group them into a
single reviewed PR, so never bump one alone. A runtime interpreter bump for a
24/7 service is not auto-merged.

## Keeping the docs current

Documentation is part of the change, and the whole set has to stay consistent.
Any change to behaviour, configuration, deployment, or the shape of a request or
response updates the docs **in the same commit** -- and updates *every* page the
change makes wrong, not just the nearest one. A set where one page is right and
another contradicts it is worse than one where neither was touched, because a
reader has no way to tell which to trust.

The checklist of which page to touch for which kind of change lives in
[`AGENTS.md`](../AGENTS.md), so humans and agents work from one list rather than
two that drift apart. In short:

- **Root `README.md`** for anything user-visible -- a new capability, entity or
  service, a changed installation step, a new requirement or limitation. Purely
  internal refactors don't need it.
- **[architecture.md](architecture.md)** and **[camera-bridge.md](camera-bridge.md)**
  when modules, flows or component relationships move -- including the diagram.
- **[ha-integration.md](ha-integration.md)** for entities, services, coordinators
  and poll intervals; keep the entity table exhaustive.
- **[control-api.md](control-api.md)** for **any** change to a route or its
  request/response fields. That page is a contract other code is written against.
- **[configuration.md](configuration.md)** for a new knob, env var or interval;
  **[troubleshooting.md](troubleshooting.md)** for a new failure mode; the
  `*-api-findings.md` pages when something is learned about an upstream API; the
  enhancement backlog ([backlog.md](backlog.md)) when an idea is
  deferred or closed, rather than losing it.
- **[README.md](README.md)** (this folder's index) whenever a doc is added, so
  the index never goes stale.

**These docs are a source of truth, not a changelog.** They describe what the
system *is* and what is *known*, in the present tense. What changed, when, and
why belongs in the commit and the pull request, which is where anyone asking that
question will look. Concretely, that means no dates on statements, no "corrected
on…", no "this used to say…", no "an earlier version wrongly claimed…". When
something here turns out to be wrong, **rewrite it so the page states the truth**
and delete the wrong claim; do not leave a trail beside it. A page that narrates
its own edit history buries the answer a reader came for.

The one thing that is always worth writing down is **the limit of what is
known**: "unverified", "inferred from the sibling endpoint rather than captured",
"needs hardware this account does not have". That is not history, it is the
current state of the evidence, and it is what stops the next person either
trusting a guess or re-deriving a question that was already asked.

Keep it all anonymized and shareable (placeholders, never real IPs, account or
device IDs, hostnames or secrets).
