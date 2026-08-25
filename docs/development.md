# Development & releasing

Contributor notes for working on this project and cutting a release.

## Repository layout

- `custom_components/telus_smarthome/` — the Home Assistant custom integration. See [ha-integration.md](ha-integration.md).
- `camera-bridge/` — the standalone camera-bridge container. See [camera-bridge.md](camera-bridge.md).
- `docs/` — this documentation set (start at [README.md](README.md) / [architecture.md](architecture.md)).

## Versioning & releases

This project follows [semantic versioning](https://semver.org/). **The single source of truth for the version is `custom_components/telus_smarthome/manifest.json` (`"version"`)** — Home Assistant displays it, and HACS uses it to detect that an update is available. `hacs.json` carries no version; the camera bridge has no separate version string (it builds from the working tree).

Bump the version on **every** user-visible change — this is easy to forget, so treat it as part of the change, not an afterthought:

| Change | Bump |
| --- | --- |
| **Minor change** — bug fix, tuning, tests, docs, internal cleanup, a small addition nobody has to think about | **PATCH** — `x.y.Z` |
| **Major change** — a new capability, a changed default, anything a user would notice or have to react to | **MINOR** — `x.Y.0` |
| Breaking change (config/entity removal, incompatible behavior) | MAJOR — `X.0.0` |

Bias towards the patch position. A minor bump (`x.Y.0`) is for changes worth
announcing — two-way audio, the stop service, talkdown switching on by default —
not for everything that happens to add a line of code.

Release steps:

1. Bump `"version"` in `custom_components/telus_smarthome/manifest.json`.
2. Commit it: `git commit -m "Bump version to X.Y.Z"` (plus the change itself, if not already committed).
3. Tag it to match, and push:
   ```bash
   git tag vX.Y.Z
   git push origin <branch> --tags
   ```

**Keep the git tag and the manifest version in lockstep** — a checkout of tag `vX.Y.Z` should report version `X.Y.Z` to HA/HACS. Every version bump gets its tag.

A bridge-only change still rides in the same integration version bump — note it in the version's changes so the two components stay releasable together.

## Building & running locally

- **Camera bridge:** `cd camera-bridge && docker compose up -d --build` (needs `--network host` and a GPU render node for hardware encode — see [setup.md](setup.md) and [configuration.md](configuration.md)). Code is baked into the image, so a code change needs a rebuild.
- **Integration:** copy `custom_components/telus_smarthome/` into Home Assistant's `config/custom_components/` and add it via the UI, or install through HACS as a custom repository (see [setup.md](setup.md)). Integration code changes require a Home Assistant restart to load.

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
  does) or a scripted stand-in class. Credentials are dummies — `config.py` only
  checks that they are present at import.
- **Each file is a plain script**, not a pytest module: it prints one line per
  test and ends in `RESULT: PASS` or `RESULT: FAIL (n)`, exiting non-zero on
  failure. There is no test runner to install.
- **Fixtures are captured, not invented.** `camera-bridge/tests/fixtures/` holds anonymized
  request/response bodies taken from real HAR captures, and a guard test fails if
  an account identifier ever lands in one.
- **Every suite's docstring names the invisible failure it exists to catch.**
  These cover behaviour that fails *silently* — a camera that freezes instead of
  erroring, a setting that reads back a value it never wrote — so "what would
  this look like in production" is stated up front rather than left to be
  rediscovered.

| Suite | What it pins |
|---|---|
| `test_camera_session.py` | The per-camera reconnect loop: rotation before Alarm.com's ~180s server-side kill, the ffmpeg publisher that outlives it, the frame hold that bridges reconnect gaps, fps measurement, sink repair, the consecutive-failure escape hatch, the per-camera `dropped_frames` accumulation into `/control/status` (on healthy rotation and failure paths alike), the first-frame timing diagnostic on both the success line and the timeout message, the per-camera `JANUS_REQUEST_AUDIO` gating (a named camera gets the audio keys, an unnamed one in the same run keeps the byte-identical video-only body), and the `JANUS_CONSUME_AUDIO` audio-publish path — off by default with an ffmpeg command line pinned byte for byte and free of every audio token, and the offered track still drained; on, the SAME publisher grows a second input rather than being rebuilt; the video input identical either way, declaring the camera's rate with `-framerate <fps>` and never with `-r` (which would override `-use_wallclock_as_timestamps`, publish less media than wall time, and stall the audio pipe when a second input shares that timeline); the rotation gap filled with silence whose sample count comes from elapsed wallclock rather than from counting loop iterations; a changed raw PCM shape forcing a rebuild the way a changed frame size does; every encoder variant (vaapi/nvenc/software) carrying the audio input and encoder; CONSUME-without-REQUEST degrading to off with exactly one warning per camera; and `/control/status` reporting only audio that was actually decoded — `audio_format` null and `decoded_audio_frames` zero for a camera publishing nothing but the sink's silence, rather than the confident `48000Hz/2ch/s16` that the launched-with fallback used to print over eleven consecutive silent sessions. Also the **pacing** the whole audio path stands on, which is invisible in production because it costs *video* frames rather than audio: the silence fill's sample count comes from elapsed wallclock (asserted exactly, against a scripted clock), no single write hands ffmpeg more than the 100 ms lead the pipe is primed with, a clipped deficit is repaid across bounded calls rather than in one burst, a quiet camera is topped up faster than that lead decays, and the frame hold tops up on the audio deadline rather than once per repeated video frame (167 ms on the 6 fps camera). Plus the three guards around the publisher: the decoded-video queue is sized in **time** (~500 ms) rather than in a flat frame count that meant 267 ms on one camera and 667 ms on another; an audio pipe ffmpeg has stopped reading degrades the sink to **video-only** instead of rebuilding the publisher and kicking every RTSP reader; and `write_frame`'s drain is **bounded**, so an ffmpeg that is alive but no longer consuming is detected and rebuilt rather than parking the camera's loop forever with the status frozen on `streaming`. Also drives the real `JanusCameraSession` `close()`/keepalive against fake pc/websocket objects — the gateway websocket must be released even when `pc.close()` raises or is cancelled, and the keepalive task must end quietly (yet stay cancellable) when the websocket drops between keepalives. |
| `test_camera_settings.py` | The website settings wire shape — JSON:API on read, a flat object on write — reconstructed from captured fixtures, plus capability gating, bounds and option-list handling, and the per-request 35s ceiling each diagnostics read carries over the shared session's 15s default (without it, a slow-but-healthy camera's signal read is cancelled by the bridge's own client and answered as a 502). |
| `test_control_server_routes.py` | The camera-settings, siren and privacy control routes: status codes (including the honest 429 + `Retry-After` on a rate-limited settings/diagnostics read or settings write), error vocabulary, and the fields the integration reads back — plus the events/history `limit` clamp, that a failed snapshot fetch is logged without its signed URL, and that the `control_secret` file ends at mode `0o600` however it arrived (generated fresh, or adopted group/world-readable and re-tightened with the secret unchanged). |
| `test_credentials_fallback.py` | The credentials.json fallback reader and watcher: a torn write (invalid UTF-8) or a wrong-shape file must degrade to the .env credentials instead of killing the process into a login-hammering restart loop, and a valid file must still be adopted. |
| `test_arming_devices.py` | The Bluetooth arming-device client and its routes, including the irreversible unpair. |
| `test_partitions.py` | Selective sensor bypass — above all the refusals, each of which is a case where writing anyway would leave a zone silently unprotected. |
| `test_mws_json_actions.py` | The JSON-envelope MWS actions (camera siren, privacy mode) and the dual XML/JSON ack dispatcher. |
| `test_parsers.py` | The pure parsers that turn Alarm.com's XML into the values the bridge acts on. |
| `test_history_events.py` | The Activity-feed parse: that `eventTypeName`/`globalDeviceId` survive the rename into `event_type_name`/`device_id`, which is the entire basis on which Home Assistant turns a feed entry into a doorbell-press event; that the same press parses identically whether its description arrives in French or English (so matching the enum is locale-proof and matching the text would not be); and that clip enrichment fills a correlated entry, leaves an uncorrelated one explicitly null, and costs one batched call per page. |
| `test_talkdown_streaming.py` | Talkdown's streaming encode, schedule-based pacing, in-place recovery, certificate pinning and endpoint selection. |
| `test_talkdown_renegotiation.py` | Talkdown session renegotiation, against a fake doorbell that really checks Digest auth. |
| `test_metrics.py` | The talkdown metrics counters. |
| `test_opus_decoder_patch.py` | `bridge/aiortc_opus_patch.py`, against the real aiortc and the real libopus: that a packet libopus rejects is dropped rather than fatal and the SAME decoder still decodes the next packet; that aiortc's own `decoder_worker` thread survives it and keeps delivering frames (the actual outage — the thread dies once and the published track is silence for the life of the session, while every health signal still says `streaming`); that the drop is logged once per decoder, i.e. once per session, carrying the packet's length and first 16 bytes, since what those bytes are is still unverified; and that the import-time compatibility guard fires on every incompatible upstream shape — class or method moved, module executed twice, upstream grown its own exception handling — while still patching an aiortc shaped like the pin. |

### The integration suites

`tests/` at the repository root covers `custom_components/telus_smarthome/`.
Same conventions as the bridge's — plain scripts, one line per test, ending in
`RESULT: PASS` / `RESULT: FAIL (n)` and exiting non-zero — and the same
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
  in the order of a hundred transitive packages — grpcio, boto3, pillow,
  cryptography, bleak/dbus-fast, zeroconf, SQLAlchemy, and PyRIC, which has no
  wheel and builds from source — to supply a handful of base classes.
- Exercising entity behaviour for real on top of that needs
  `pytest-homeassistant-custom-component`, which pins one exact Home Assistant
  version. That is a version-lock treadmill this repo gets nothing back from, and
  a second test framework alongside the bridge's plain scripts.
- The logic worth pinning is pure — a counter compared across polls, a string
  classifier, an id-set dedup. None of it needs an event loop, an entity
  registry or a state machine.

**The limit of that choice, stated so nobody mistakes it for coverage it isn't:
the stubs assert nothing about Home Assistant's real API.** If upstream changes
what `CoordinatorEntity` promises, these suites stay green. They pin *our*
derivations, not our compatibility with Home Assistant. Two things narrow the
gap deliberately, and both must be preserved when the stubs are extended:

- Each stub reproduces the upstream behaviour the integration actually leans on
  and nothing else — `CoordinatorEntity.available` really is
  `coordinator.last_update_success`, and `EventEntity._trigger_event` really does
  reject a type outside `event_types`.
- Nothing in the stub tree is auto-magic: no `MagicMock`, no `__getattr__` that
  invents attributes. An integration module that starts using a name the stubs
  do not define fails loudly rather than exercising a fiction. The same applies
  to the three-attribute `pyalarmdotcomajax` stub. (`controller.py` is stubbed
  wholesale, because importing it applies the websocket monkeypatch and needs the
  real library; that patch is guarded at import time and exercised by CI's
  `check_patch.py` job instead.)

| Suite | What it pins |
|---|---|
| `test_stream_health_sensors.py` | The bridge-fed stream-health sensors. Above all `CameraAudioStateSensor`'s derivation: the four states, and the four ways a false `stalled` could be fabricated — an unanswered poll (both the `last_poll_ok` flag and the identical payload object it hands back), a failed poll, a counter reset on a bridge restart, and the first poll of the entity's life. Also the per-poll delta (suppressed rather than negative across a reset), that `not_consuming` drops the remembered count so re-enabling audio starts a fresh comparison, that a bridge too old to report `audio_consumed` reads unknown rather than a fabricated `not_consuming`, and that every derivable state is inside `_attr_options` (an ENUM sensor whose state falls outside its options errors on every update). Plus `CameraStreamStateSensor`'s payload mapping and its degradation of an unknown bridge state to unknown-with-`raw_state`, `CameraDroppedFramesSensor`'s mapping and `total_increasing` class, and the coordinator's two failure flags — an unanswered `/control/status` keeps the previous payload rather than flapping every entity to unavailable, a transport failure raises `UpdateFailed`, and both record `last_poll_ok = False`. |
| `test_event_entities.py` | The Activity-feed event entities. The doorbell matcher keys on TELUS's untranslated `event_type_name` enum: a French-described press fires, and an entry whose *description* says "Button Push" while its enum says otherwise does not — this account's feed has been observed in both French and English for the same event kinds, and a text match would simply stop firing the day the account locale changed. Also the per-page id-set dedup: a restart baselines on the current page and replays nothing, two presses sharing one timestamp both fire (a high-water mark would suppress the siblings forever), an empty page is a failed poll that neither becomes nor clears the baseline, and two presses in one poll fire oldest-first with one state write each. Plus `_classify` across both locales and the containment of its output in `EVENT_TYPES`, `_event_time` ordering across the two timestamp formats TELUS emits (a string sort gets them backwards), `_is_doorbell`'s any-one-of-three chime capabilities, and `TelusActivityEvent`'s single extra fire when TELUS attaches a clip a poll or two after the entry. |

**Not covered by these suites** — verify these by reading the code, compiling it,
and checking Home Assistant's logs, and see `docs/backlog.md` (E66):
`config_flow.py`, the service handlers (`speak_service.py`, `bypass_service.py`,
`unpair_service.py`), the settings-backed platforms (`switch.py`, `select.py`,
`number.py`, `button.py`), `binary_sensor.py`, `clip_view.py`'s request handling
(only the link minting is exercised), `arming_devices_coordinator.py`, and every
setup/teardown path in `__init__.py`.

### Keeping the tests current

Tests are part of a change on the same terms as the docs: **anything that alters
behaviour a suite covers updates that suite in the same commit.** The whole point
of these suites is that the behaviour underneath them fails quietly — a frozen
camera, a setting that reads back a value it never wrote — so a suite that has
drifted out of step with the code is not merely stale, it is actively
misleading: it reports green over the exact class of bug it was written to catch.

| Situation | What the change must include |
|---|---|
| New behaviour in a module a suite already covers | a test for it, in that suite |
| An invariant deliberately changed — a timeout, a wire shape, a refusal | the test updated to pin the **new** invariant, and its docstring rewritten to say why |
| A test starts failing | either the code is wrong and gets fixed, or the invariant genuinely moved and the test is rewritten — deliberately, and said out loud in the commit message |
| A new module whose failures would be quiet | a new suite following the conventions above, and its row added to the table |
| An upstream response shape changes | the fixture recaptured and re-anonymized, never hand-edited to fit |

**Never make a test pass by weakening what it asserts.** Not by deleting it, not
by skipping it, not by loosening a bound until it stops complaining, not by
narrowing a scenario until the interesting case is gone. A red test is
information. The only two honest responses are to fix the code, or to change the
invariant on purpose and say so.

**Prove a new test can actually fail.** A test that has never been red is an
assertion nobody has checked. For each invariant a new test claims to pin, break
that invariant in the source, confirm the suite reports the failure, then revert
— and treat a test that stays green against deliberately broken code as
*worthless until strengthened*, not as a curiosity. Both integration suites and
`test_camera_session.py` were validated this way, and it has repeatedly found
tests that only looked strict; it also documents which halves of a condition are
genuinely load-bearing (in `_classify`, the French *person* needle is redundant
because `"personne"` contains `"person"`, while the *vehicle* needles are not —
that is recorded in the test itself so nobody re-derives it).

Two practical notes. A new `test_*.py` in either tests directory is picked up by
CI automatically — there is no list to add it to, which is why the tables above
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
- **A fourth monkeypatch, on the bridge side: `bridge/aiortc_opus_patch.py`.** aiortc's
  `OpusDecoder.decode` has no error handling where its `H264Decoder.decode` does, and
  `rtcrtpreceiver.decoder_worker` -- a thread target -- has none either, so one
  malformed Opus packet permanently kills a session's `audio-decoder` thread and the
  published audio track becomes pure silence with every health signal still green. The
  repo does not vendor aiortc, so this follows the integration's model: an import-time
  replacement carrying an import-time compatibility guard, which refuses to apply if the
  class or method moved, if it is being applied twice, or if upstream has grown its own
  exception handling (the shape a fix would take -- at which point the module should be
  deleted, not layered on top). The guard is exercised by the every-bridge-module-imports
  CI job and by `tests/test_opus_decoder_patch.py`. Not fixed in aiortc 1.15.0 (the pin)
  or in upstream `main`.
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
(`camera-bridge/Dockerfile`'s `FROM python:<ver>-slim`) — testing on an
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
and delete the wrong claim — do not leave a trail beside it. A page that narrates
its own edit history buries the answer a reader came for.

The one thing that is always worth writing down is **the limit of what is
known**: "unverified", "inferred from the sibling endpoint rather than captured",
"needs hardware this account does not have". That is not history, it is the
current state of the evidence, and it is what stops the next person either
trusting a guess or re-deriving a question that was already asked.

Keep it all anonymized and shareable (placeholders, never real IPs, account or
device IDs, hostnames or secrets).
