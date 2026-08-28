# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before editing or running anything. Humans: [`README.md`](README.md) is the friendlier entry point; the deep reference is in [`docs/`](docs).

## What this project is

Two independent pieces that bring a TELUS Smart Home account (a white-labeled Alarm.com deployment, no official API) into Home Assistant:

- `custom_components/telus_smarthome/`: the Home Assistant integration (forked from `pyalarmdotcom/alarmdotcom`, with TELUS SSO login monkeypatched in at import time). Panel, lock, sensor, camera, and event entities; the `telus_smarthome.speak` service; a GUI config/reconfigure flow.
- `camera-bridge/`: a standalone Docker service that logs in to TELUS, pulls WebRTC camera video, and republishes it as local RTSP, plus a loopback HTTP control API. Independent codebase; it re-implements TELUS auth from scratch (no shared dependency with the integration).

They communicate over a loopback control endpoint (`http://127.0.0.1:8199`) and a shared directory (credential fallback + secrets). Everything is reverse-engineered from real browser/app traffic; there is no upstream spec.

## Repo map

| Path | What it is |
| --- | --- |
| `custom_components/telus_smarthome/` | HA integration. Entry: `__init__.py`. Auth patch: `telus_auth_patch.py`. Platforms: `alarm_control_panel.py`, `binary_sensor.py`, `lock.py`, `camera.py`, `event.py`, etc. Bridge client: `bridge_client.py`. Talk service: `speak_service.py`. |
| `camera-bridge/bridge/` | Bridge app. Entry: `main.py`. Video: `camera_session.py`, `janus_client.py`, `ffmpeg_sink.py`. aiortc patch: `aiortc_opus_patch.py`. Auth/API: `telus_auth.py`, `telus_rest.py`, `mobile_auth.py`, `mws_client.py`. Talkdown: `talk.py`, `talk_fullduplex.py`. Outbound call leg: `sip_call.py`. Control API: `control_server.py`. Config: `config.py`. Rotation-window instrumentation (off by default): `handover_trace.py`. Container-memory instrumentation (off by default): `memory_trace.py`. |
| `camera-bridge/{Dockerfile,docker-compose.yml,mediamtx.yml,entrypoint.sh}` | Build + runtime. |
| `tests/` (repo root) | Integration regression suites + `ha_stubs.py`, the `homeassistant.*` stand-in tree that lets them run without Home Assistant installed. |
| `install.sh` (repo root) | Interactive installer for both components. |
| `docs/` | Full documentation set; see `docs/README.md`. |

## Build, run, verify

- **Bridge:** `cd camera-bridge && docker compose up -d --build`. Code is baked into the image, so **any code change needs a rebuild** (`--build`), not just a restart. Health check: `curl -s http://127.0.0.1:8199/control/health` → `{"status":"ok"}`. Requires `--network host` and (for hardware encode) a GPU render node.
- **Integration:** copy `custom_components/telus_smarthome/` into HA's `config/custom_components/`; **integration code changes require a full Home Assistant restart** to load (a config-entry reload does not re-import modules).
- **Syntax check** Python before proposing changes: `python3 -m py_compile <file>`.
- **CI** (`.github/workflows/ci.yml`) runs on PRs: it imports `telus_auth_patch.py` to prove the pyalarmdotcomajax monkeypatch still applies (a compatibility guard fails loudly if an upstream internal moved), installs the bridge deps wheels-only, and runs **every** `camera-bridge/tests/test_*.py` and **every** `tests/test_*.py`.
- **Tests** use local fakes only (no network, no account, no hardware) so run them before proposing a change: `cd camera-bridge && TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_camera_session.py` for the bridge, and `python3 tests/test_stream_health_sensors.py` from the repo root for the integration (each file is a script and ends in `RESULT: PASS`). The integration suites stub `homeassistant.*` rather than installing Home Assistant; see `tests/ha_stubs.py` for why and for the limits of that. They cover the stream-health sensor derivations, the Activity-feed event entities and the signed clip links; everything else in `custom_components/` is still verified by reading the code, compiling, and checking HA logs. New behaviour in a covered module gets a test in the same change, and a new test is not trusted until it has been proven to fail against deliberately broken source: the suites exist for failures that are otherwise silent. See `docs/development.md`.

## Conventions you MUST follow

- **Commit identity:** commit as the repo's configured author (`git config user.name`/`user.email`). **Never add AI/Claude attribution**: no `Co-Authored-By`, no "Generated with", no assistant trailers of any kind.
- **Commit messages** explain the *why* and the evidence (what changed, what it fixes, how it was verified), not just *what*.
- **Never commit secrets.** `camera-bridge/.env` and `camera-bridge/secrets.env` are gitignored and hold live credentials; the `*.example` files are the templates. Do not print, log, or commit passwords, tokens, session/stream secrets, or the control secret.
- **Keep `docs/` shareable/anonymized.** Docs must describe the software generically and use placeholders (`<ha-host>`, `<doorbell-ip>`, `<unit-id>`, `<did>`, `<mac>`, …): never real IPs, account/device IDs, hostnames, filesystem paths that reveal a user, or secrets. See `docs/development.md`.
- **Documentation is part of the change, not a follow-up, and the whole set must
  stay consistent.** Anything that changes behaviour, configuration, deployment,
  the dependency set, or the shape of a request or response updates `docs/` in the
  SAME commit. Not "a doc": every doc the change makes wrong. A change that
  leaves one page right and another page contradicting it is worse than one that
  updates neither, because the reader cannot tell which to believe.

  Work the list, and touch every entry the change actually affects:

  | Doc | Update it when… |
  | --- | --- |
  | `README.md` (root) | the change is user-visible: a new capability, entity or service, a changed setup step, a new requirement or limitation |
  | `docs/architecture.md` | a component, module, data flow or backend relationship changes, including the mermaid diagram |
  | `docs/camera-bridge.md` | a bridge module is added, removed, or changes responsibility |
  | `docs/ha-integration.md` | an entity, service, coordinator or poll interval changes; keep the entity table exhaustive |
  | `docs/control-api.md` | ANY route, request field, response field or status code changes; this is a contract other code is written against |
  | `docs/configuration.md` | a knob, env var, URL template or interval is added or changed |
  | `docs/troubleshooting.md` | a new failure mode exists, or an old symptom now has a different cause |
  | `docs/*-api-findings.md` | something new is learned about an upstream API, INCLUDING that a previous finding was wrong |
  | `docs/backlog.md` (the enhancement backlog) | an idea is deferred, closed, or proven unachievable, with the evidence |
  | `docs/README.md` | a doc is added, so the index never goes stale |

  **These docs are a source of truth, not a changelog.** They state what the
  system IS and what is KNOWN, in the present tense. What changed, when and why
  belongs in the commit and the PR; that is where anyone asking will look. So:
  no dates on statements, no "corrected on…", no "this used to say…", no "an
  earlier version wrongly claimed…". When something turns out to be wrong,
  **rewrite the page so it states the truth and delete the wrong claim** rather
  than leaving a trail beside it. A page narrating its own edit history buries
  the answer the reader came for.

  The one thing always worth writing down is **the limit of what is known**:
  "unverified", "inferred from the sibling endpoint rather than captured", "needs
  hardware this account does not have". That is not history; it is the current
  state of the evidence, and it is what stops the next person trusting a guess or
  re-deriving a settled question.
- **Tests are part of the change too, on the same terms as the docs.** There are
  two regression suites, both run in full by CI: `camera-bridge/tests/`
  (fourteen files) for the bridge, and `tests/` at the repo root for the
  integration. Both cover behaviour that fails *silently* in production: a
  camera that freezes instead of erroring, a setting that reads back a value it
  never wrote, an audio stream that drifts, a doorbell event that quietly stops
  firing when the account locale changes. **Run the relevant one before
  proposing a change**, and when the change touches something a suite covers,
  update the suite in the SAME commit:

  | Situation | What the change must include |
  | --- | --- |
  | New behaviour in a module a suite already covers | a test for it, in that suite |
  | An invariant deliberately changed (a timeout, a wire shape, a refusal) | the test updated to pin the NEW invariant, and its docstring rewritten to say why |
  | A test starts failing | either the code is wrong and gets fixed, or the invariant genuinely moved and the test is rewritten, **deliberately, and stated in the commit message** |
  | A new module with behaviour that can fail quietly | a new suite, following the conventions below, plus its row in `docs/development.md`'s test table |
  | A fixture's upstream shape changes | the fixture recaptured and re-anonymized, never hand-edited to fit |

  **Never make a test pass by weakening what it asserts**: not by deleting it,
  skipping it, loosening a bound until it stops complaining, or narrowing a
  scenario until the interesting case is gone. A red test is information; the
  only honest responses are to fix the code or to change the invariant on
  purpose and say so.

  **A new test is not trusted until it has been proven to fail.** Break the
  invariant it claims to pin, watch the suite go red, revert. A test that stays
  green against deliberately broken code is worthless until strengthened; say
  so rather than counting it.

  Conventions: no network, no account, no hardware (local aiohttp fakes or
  scripted stand-in classes); each file is a plain script ending in
  `RESULT: PASS` / `RESULT: FAIL (n)` and exiting non-zero; fixtures are captured
  and anonymized, never invented; and **each suite's docstring names the
  invisible failure it exists to catch**, because "what does this look like in
  production" is the part that is expensive to rediscover. A new suite is picked
  up by CI automatically (both jobs glob `tests/test_*.py`). The integration
  suites additionally **stub `homeassistant.*` rather than installing Home
  Assistant**: `tests/ha_stubs.py` carries the reasoning and the rule that
  nothing in that tree may be auto-magic.
- **Versioning:** the version lives only in `custom_components/telus_smarthome/manifest.json`, with a matching `git tag vX.Y.Z`. **Bump the PATCH position (`x.y.Z`) for minor changes**: fixes, tuning, tests, internal cleanup. **Bump MINOR (`x.Y.0`) only for major changes**: a new capability, a changed default, anything a user would notice or react to. Bias towards patch. **Documentation-only commits do not bump or tag at all**: they ride untagged on master and ship with the next code release. Details in `docs/development.md`.

## Operational cautions (this talks to a LIVE account)

- **Rate limits can lock the account.** Alarm.com's limits are undocumented; a stampede of logins or polls can trigger `429` and, worse, an account lock. Honor `Retry-After`, never tight-retry against the edge, and don't lower poll intervals casually. The code already funnels 429s into a backoff; preserve that.
- **The push websocket drops ~every 6 minutes.** That's a server-side token lifetime, not a bug or an idle timeout. The reconnect/keepalive logic is intentional; don't "fix" it by mirroring a browser's websocket URL params (tested, rejected by the server).
- **Session rotation is load-bearing.** The bridge proactively rotates each camera's video session (~165s) ahead of the server's forced close, with several redundant safety nets and a disk-persisted restart guard (`restart_guard.py`). Read `camera_session.py` and its comments fully before touching it: the comments document previously-shipped bugs and why the current logic looks the way it does. `camera-bridge/tests/test_camera_session.py` turns those comments into assertions; run it after any change there.
- **Talkdown opens a real device audio session** and is enabled by default (`TALKDOWN_ENABLED`). It only plays what a caller explicitly asks it to, but firing it makes real audio come out of a doorbell in someone's home -- don't trigger it casually while testing. It's device-specific (confirmed on the ADC-VDB750).
- **Two auth stacks, on purpose.** The website AJAX API (state + video) and the legacy mobile "MWS" XML API (two-way audio) are separate flows with separate quirks; see `docs/alarm-com-api.md` before changing either.

## Deliberate designs -- do not "simplify" these

Each of these looks like something to clean up and is not. See
`docs/development.md` ("Dependency hardening") for the full reasoning.

- **`clip_view.py` signs its own clip links** rather than using HA's
  `async_sign_path`. HA's signatures are bound to a refresh token and break when it
  is revoked; these links live inside already-delivered phone notifications and must
  keep working, so the expiry is embedded in the MAC instead.
- **`climate.py` deletes `_attr_max_temp`/`_attr_min_temp` from `__dict__`** instead
  of assigning them. HA's `ClimateEntity.max_temp` default is a property that converts
  the default into the entity's unit; assigning a value (or the property object) breaks
  non-Celsius setups. The attribute must be ABSENT for the default to apply.
- **`aiortc.sdp.candidate_from_sdp` is imported from a private module** because aiortc
  exposes no public equivalent. It is deliberately kept under the CI import job + the
  Renovate review-required gate rather than vendored.
- **The three `pyalarmdotcomajax` monkeypatches each carry an import-time compatibility
  guard.** If you change what a patch touches, update that module's guard AND
  `.github/scripts/check_patch.py` (which also asserts the private *instance*
  attributes the replaced `async_login` depends on).
- **`camera-bridge/bridge/aiortc_opus_patch.py` replaces `aiortc.codecs.opus.OpusDecoder.decode`.**
  It does two jobs. The gateway advertises the audio track as Opus and sends **G.711
  mu-law at 8 kHz**, so the patch sniffs the first packets of each session with both
  decoders, commits to one, and holds the sniffed frames until it decides (a frame in
  the losing codec's shape rebuilds the publisher and kicks every RTSP reader). And
  upstream's Opus decode has no error handling (its H.264 twin does) while
  `decoder_worker` (a thread target) has none either, so one undecodable packet
  permanently kills a session's `audio-decoder` thread and the published audio becomes
  pure silence with every health signal still green. Same model as the three above:
  import-time patch, import-time guard, exercised by the every-bridge-module-imports CI
  job and `tests/test_opus_decoder_patch.py`. Backlog E59 and E80 carry the evidence.
  Do not "simplify" the sniff into a per-packet fallback, and do not delete the module
  when upstream fixes the missing handler (the guard fires on that) without keeping the
  codec selection.

## Settled dead ends -- do not reopen

Each of these was investigated to a conclusion, at real cost. Re-investigating
them has already burned sessions. The evidence lives where the pointer says;
read it before deciding the conclusion no longer applies.

- **Device-direct listen audio from the doorbell.** The doorbell runs no RTSP
  server: its media port answers RTSP with an HTTP 400 from lighttpd, and the
  published `rtsp://` URLs are consumed as HTTPS `GET /api/GetStream`. Listen
  audio comes over the website/Janus path. Backlog E55; reopen only if the
  Janus path is abandoned.
- **L16 audio on the talkdown PUT path.** Tested against the live device in
  both byte orders; both decode as noise, because this doorbell model decodes
  the PUT body as G.711 mu-law regardless of Content-Type. A per-model limit,
  not project-wide. Backlog E4.
- **"Fixing" the ~6-minute push-websocket drop.** It is a server-side token
  lifetime. Mirroring a browser's websocket URL params was tested and rejected
  by the server. The reconnect logic is the design.
- **A gapless RTSP publisher handoff at rotation.** MediaMTX rejects a second
  concurrent publisher on the same path (upstream discussion linked from
  `config.py`). The one-publisher-plus-frame-hold design is the answer, not a
  workaround.
- **`-r` before `-i` on the video input, or declaring no rate at all.**
  Measured three ways in-container (the table in `ffmpeg_sink.py`): `-r`
  overrides `-use_wallclock_as_timestamps` and publishes less media than wall
  time; no declaration re-rates everything to the rawvideo demuxer's 25 fps
  default. `-framerate` is the only correct form, and this is not an
  ffmpeg-version quirk (reproduced identically on 7.1.5 and n9.0.1).
- **Topping the audio pipe up at or above the 100 ms muxer lead.** The lead is
  the mechanism, not the burst size: a 0.1 s cadence measured WORSE than the
  0.5 s defect it replaced (162 vs 120 stalled frames of 900). The shipped
  cadence is half the lead. Backlog E60 carries both measurement tables and
  their reconciliation.
- **The per-frame pixel copy as the frame-drop cause.** It costs 0.24 ms of a
  66.7 ms frame interval, and pre-scaling the feed changed the stall count by
  exactly zero. Backlog E60 records the disproof.
- **Matching activity events on description text.** The feed renders text in
  the account's language (both French and English observed on one account for
  the same event kinds). Match `event_type_name`, the untranslated enum.
- **Latching the frame rate once per process.** It is a device setting; the
  rate is adaptive (backlog E64), and a delivered rate that swings with no
  setting change is real and handled by the flap guard (E73). Do not
  "simplify" either back out.

## Measurement discipline

The repository's own record includes a measurement error that sent a full
round of work at the wrong target (backlog E60: a reading recorded as a
control was actually taken with the treatment enabled). The rules that would
have prevented it:

- **Name the control.** Before reading a comparison, state what the control
  run is and verify from the log what configuration it actually ran under.
  "It was probably off" is how the E60 error happened.
- **A rig earns trust by reproducing the known defect.** When two rigs
  disagree, the one that reproduces the already-measured failure to the frame
  is the one to believe. Two rigs disagreeing exactly at a cliff edge is
  expected, not alarming.
- **Quote one run.** Never blend numbers from different runs into one table;
  if a second run exists, record that it agreed on the load-bearing figure and
  which run the quoted percentiles came from. (The tables in this repo drifted
  apart once exactly this way.)
- **Measure before reasoning** for anything about timing, throughput or
  latency; reasoning first is for logic invariants, and those get pinned by a
  test instead.
- **Pin measured constants as values** where a wrong value is self-defeating,
  the way the audio-pacing constants are pinned against the muxer lead.
  Backlog E72 tracks the constants that are still pinned as logic only.
- **Beware stale bytecode when swapping files of identical size.** CPython's
  pyc check is size+mtime; a reverted file that matches both runs the OLD
  compiled module. Clear `__pycache__` around fail-first experiments.

## Recording findings

- Findings live in `docs/backlog.md` as `E<n>` entries: status in the header
  parenthetical, evidence in the body. **The header must agree with the
  body**; they drifted apart once (a body saying "confirmed live" under a
  header saying "unconfirmed") and the contradiction survived several commits.
- **A disproven hypothesis is knowledge, not history.** Inside a backlog
  entry, "X was the suspected mechanism and is disproven by <measurement>" is
  the limit-of-knowledge record that stops the next session re-deriving it,
  and it stays. Everywhere else in `docs/`, the wrong claim is deleted and the
  page states the current truth (see the docs rules above).
- **Never edit backlog entries by line index or broad splice.** Entry bodies
  have been silently deleted that way and the loss went unnoticed for several
  commits. Use exact-string edits, and verify the entry count before and after
  (`grep -cE '^- \*\*E[0-9]+ ' docs/backlog.md`).
- A correction to a previous session's wrong claim gets the same treatment as
  any other change: every doc it touches, in the same commit.

## The working loop

1. **Orient.** The checkout you are editing may be a staging copy with no
   `.git`; git runs where the deployment lives. Confirm which you are in
   before assuming anything about state, and diff staging against the
   deployment checkout before assuming it is clean.
2. **Read before touching** the files the cautions above name in full.
3. **Change, then prove.** Run the affected suite before proposing anything.
   A new or changed test is proven able to fail: break the invariant, watch
   the suite go red with the message you intended, revert. **A new test must
   also be added to its file's `TESTS` list** -- a test that is not in the
   list passes by never running, and that has happened.
4. **Docs and backlog ride in the same commit**, per the table above.
   Version bump per the versioning rule; the tag matches `manifest.json`.
5. **Deploy deliberately.** Bridge code is baked into the image (rebuild, not
   restart); integration code needs a full Home Assistant restart. Never
   deploy mid-edit, and tell the operator before restarting anything that
   serves a live camera.
6. **Verify live, before and after.** `/control/status` fields, the container
   log lines the docs name, and whatever whole-stack watcher the deployment
   runs. A deploy that was not watched is not verified.

## Where to read more

Start at [`docs/README.md`](docs/README.md). For a change in a given area: `docs/ha-integration.md`, `docs/camera-bridge.md`, `docs/control-api.md`, `docs/alarm-com-api.md`, `docs/configuration.md`, `docs/troubleshooting.md`. Each component also has its own README (`custom_components/telus_smarthome/README.md`, `camera-bridge/README.md`).

## If TELUS/Alarm.com breaks it

If auth, the portal, or the camera platform changes, the debugging starting point is a fresh traffic capture compared against what `telus_auth_patch.py` / `camera-bridge/bridge/telus_auth.py` / `janus_client.py` currently expect. These files document the exact request/response shapes they were built against.
