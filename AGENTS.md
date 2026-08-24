# AGENTS.md

Guidance for AI coding agents working in this repository. Read this before editing or running anything. Humans: [`README.md`](README.md) is the friendlier entry point; the deep reference is in [`docs/`](docs).

## What this project is

Two independent pieces that bring a TELUS Smart Home account (a white-labeled Alarm.com deployment, no official API) into Home Assistant:

- `custom_components/telus_smarthome/` — the Home Assistant integration (forked from `pyalarmdotcom/alarmdotcom`, with TELUS SSO login monkeypatched in at import time). Panel, lock, sensor, camera, and event entities; the `telus_smarthome.speak` service; a GUI config/reconfigure flow.
- `camera-bridge/` — a standalone Docker service that logs in to TELUS, pulls WebRTC camera video, and republishes it as local RTSP, plus a loopback HTTP control API. Independent codebase; it re-implements TELUS auth from scratch (no shared dependency with the integration).

They communicate over a loopback control endpoint (`http://127.0.0.1:8199`) and a shared directory (credential fallback + secrets). Everything is reverse-engineered from real browser/app traffic; there is no upstream spec.

## Repo map

| Path | What it is |
| --- | --- |
| `custom_components/telus_smarthome/` | HA integration. Entry: `__init__.py`. Auth patch: `telus_auth_patch.py`. Platforms: `alarm_control_panel.py`, `binary_sensor.py`, `lock.py`, `camera.py`, `event.py`, etc. Bridge client: `bridge_client.py`. Talk service: `speak_service.py`. |
| `camera-bridge/bridge/` | Bridge app. Entry: `main.py`. Video: `camera_session.py`, `janus_client.py`, `ffmpeg_sink.py`. Auth/API: `telus_auth.py`, `telus_rest.py`, `mobile_auth.py`, `mws_client.py`. Talkdown: `talk.py`, `talk_fullduplex.py`. Control API: `control_server.py`. Config: `config.py`. |
| `camera-bridge/{Dockerfile,docker-compose.yml,mediamtx.yml,install.sh}` | Build + runtime. |
| `docs/` | Full documentation set — see `docs/README.md`. |

## Build, run, verify

- **Bridge:** `cd camera-bridge && docker compose up -d --build`. Code is baked into the image, so **any code change needs a rebuild** (`--build`), not just a restart. Health check: `curl -s http://127.0.0.1:8199/control/health` → `{"status":"ok"}`. Requires `--network host` and (for hardware encode) a GPU render node.
- **Integration:** copy `custom_components/telus_smarthome/` into HA's `config/custom_components/`; **integration code changes require a full Home Assistant restart** to load (a config-entry reload does not re-import modules).
- **Syntax check** Python before proposing changes: `python3 -m py_compile <file>`.
- **CI** (`.github/workflows/ci.yml`) runs on PRs: it imports `telus_auth_patch.py` to prove the pyalarmdotcomajax monkeypatch still applies (a compatibility guard fails loudly if an upstream internal moved), installs the bridge deps wheels-only, and runs **every** `camera-bridge/tests/test_*.py`.
- **Tests** use local fakes only — no network, no account, no hardware — so run them before proposing a bridge change: `cd camera-bridge && TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_camera_session.py` (each file is a script and ends in `RESULT: PASS`). They cover the bridge; the integration has none, so verify it by reading the code, compiling, and checking HA logs. New behaviour in a covered module gets a test in the same change — the suites exist for failures that are otherwise silent. See `docs/development.md`.

## Conventions you MUST follow

- **Commit identity:** commit as the repo's configured author (`git config user.name`/`user.email`). **Never add AI/Claude attribution** — no `Co-Authored-By`, no "Generated with", no assistant trailers of any kind.
- **Commit messages** explain the *why* and the evidence (what changed, what it fixes, how it was verified), not just *what*.
- **Never commit secrets.** `camera-bridge/.env` and `camera-bridge/secrets.env` are gitignored and hold live credentials; the `*.example` files are the templates. Do not print, log, or commit passwords, tokens, session/stream secrets, or the control secret.
- **Keep `docs/` shareable/anonymized.** Docs must describe the software generically and use placeholders (`<ha-host>`, `<doorbell-ip>`, `<unit-id>`, `<did>`, `<mac>`, …) — never real IPs, account/device IDs, hostnames, filesystem paths that reveal a user, or secrets. See `docs/development.md`.
- **Documentation is part of the change, not a follow-up — and the whole set must
  stay consistent.** Anything that changes behaviour, configuration, deployment,
  the dependency set, or the shape of a request or response updates `docs/` in the
  SAME commit. Not "a doc" — every doc the change makes wrong. A change that
  leaves one page right and another page contradicting it is worse than one that
  updates neither, because the reader cannot tell which to believe.

  Work the list, and touch every entry the change actually affects:

  | Doc | Update it when… |
  | --- | --- |
  | `README.md` (root) | the change is user-visible — a new capability, entity or service, a changed setup step, a new requirement or limitation |
  | `docs/architecture.md` | a component, module, data flow or backend relationship changes — including the mermaid diagram |
  | `docs/camera-bridge.md` | a bridge module is added, removed, or changes responsibility |
  | `docs/ha-integration.md` | an entity, service, coordinator or poll interval changes — keep the entity table exhaustive |
  | `docs/control-api.md` | ANY route, request field, response field or status code changes — this is a contract other code is written against |
  | `docs/configuration.md` | a knob, env var, URL template or interval is added or changed |
  | `docs/troubleshooting.md` | a new failure mode exists, or an old symptom now has a different cause |
  | `docs/*-api-findings.md` | something new is learned about an upstream API, INCLUDING that a previous finding was wrong |
  | the enhancement backlog | an idea is deferred, closed, or proven unachievable — with the evidence |
  | `docs/README.md` | a doc is added, so the index never goes stale |

  **These docs are a source of truth, not a changelog.** They state what the
  system IS and what is KNOWN, in the present tense. What changed, when and why
  belongs in the commit and the PR — that is where anyone asking will look. So:
  no dates on statements, no "corrected on…", no "this used to say…", no "an
  earlier version wrongly claimed…". When something turns out to be wrong,
  **rewrite the page so it states the truth and delete the wrong claim** rather
  than leaving a trail beside it. A page narrating its own edit history buries
  the answer the reader came for.

  The one thing always worth writing down is **the limit of what is known** —
  "unverified", "inferred from the sibling endpoint rather than captured", "needs
  hardware this account does not have". That is not history; it is the current
  state of the evidence, and it is what stops the next person trusting a guess or
  re-deriving a settled question.
- **Tests are part of the change too, on the same terms as the docs.** The bridge
  has a real regression suite (`camera-bridge/tests/`, eleven files, run in full by
  CI) covering behaviour that fails *silently* in production — a camera that
  freezes instead of erroring, a setting that reads back a value it never wrote,
  an audio stream that drifts. **Run it before proposing a bridge change**, and
  when the change touches something a suite covers, update the suite in the SAME
  commit:

  | Situation | What the change must include |
  | --- | --- |
  | New behaviour in a module a suite already covers | a test for it, in that suite |
  | An invariant deliberately changed (a timeout, a wire shape, a refusal) | the test updated to pin the NEW invariant, and its docstring rewritten to say why |
  | A test starts failing | either the code is wrong and gets fixed, or the invariant genuinely moved and the test is rewritten — **deliberately, and stated in the commit message** |
  | A new module with behaviour that can fail quietly | a new suite, following the conventions below, plus its row in `docs/development.md`'s test table |
  | A fixture's upstream shape changes | the fixture recaptured and re-anonymized, never hand-edited to fit |

  **Never make a test pass by weakening what it asserts** — not by deleting it,
  skipping it, loosening a bound until it stops complaining, or narrowing a
  scenario until the interesting case is gone. A red test is information; the
  only honest responses are to fix the code or to change the invariant on
  purpose and say so.

  Conventions: no network, no account, no hardware (local aiohttp fakes or
  scripted stand-in classes); each file is a plain script ending in
  `RESULT: PASS` / `RESULT: FAIL (n)` and exiting non-zero; fixtures are captured
  and anonymized, never invented; and **each suite's docstring names the
  invisible failure it exists to catch**, because "what does this look like in
  production" is the part that is expensive to rediscover. A new suite is picked
  up by CI automatically (it globs `tests/test_*.py`).
- **Versioning:** the version lives only in `custom_components/telus_smarthome/manifest.json`, with a matching `git tag vX.Y.Z`. **Bump the PATCH position (`x.y.Z`) for minor changes** — fixes, tuning, tests, docs, internal cleanup. **Bump MINOR (`x.Y.0`) only for major changes** — a new capability, a changed default, anything a user would notice or react to. Bias towards patch. Details in `docs/development.md`.

## Operational cautions (this talks to a LIVE account)

- **Rate limits can lock the account.** Alarm.com's limits are undocumented; a stampede of logins or polls can trigger `429` and, worse, an account lock. Honor `Retry-After`, never tight-retry against the edge, and don't lower poll intervals casually. The code already funnels 429s into a backoff — preserve that.
- **The push websocket drops ~every 6 minutes.** That's a server-side token lifetime, not a bug or an idle timeout. The reconnect/keepalive logic is intentional; don't "fix" it by mirroring a browser's websocket URL params (tested, rejected by the server).
- **Session rotation is load-bearing.** The bridge proactively rotates each camera's video session (~165s) ahead of the server's forced close, with several redundant safety nets and a disk-persisted restart guard (`restart_guard.py`). Read `camera_session.py` and its comments fully before touching it — the comments document previously-shipped bugs and why the current logic looks the way it does. `camera-bridge/tests/test_camera_session.py` turns those comments into assertions; run it after any change there.
- **Talkdown opens a real device audio session** and is enabled by default (`TALKDOWN_ENABLED`). It only plays what a caller explicitly asks it to, but firing it makes real audio come out of a doorbell in someone's home -- don't trigger it casually while testing. It's device-specific (confirmed on the ADC-VDB750).
- **Two auth stacks, on purpose.** The website AJAX API (state + video) and the legacy mobile "MWS" XML API (two-way audio) are separate flows with separate quirks — see `docs/alarm-com-api.md` before changing either.

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

## Where to read more

Start at [`docs/README.md`](docs/README.md). For a change in a given area: `docs/ha-integration.md`, `docs/camera-bridge.md`, `docs/control-api.md`, `docs/alarm-com-api.md`, `docs/configuration.md`, `docs/troubleshooting.md`. Each component also has its own README (`custom_components/telus_smarthome/README.md`, `camera-bridge/README.md`).

## If TELUS/Alarm.com breaks it

If auth, the portal, or the camera platform changes, the debugging starting point is a fresh traffic capture compared against what `telus_auth_patch.py` / `camera-bridge/bridge/telus_auth.py` / `janus_client.py` currently expect. These files document the exact request/response shapes they were built against.
