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
- **CI** (`.github/workflows/ci.yml`) runs on PRs: it imports `telus_auth_patch.py` to prove the pyalarmdotcomajax monkeypatch still applies (a compatibility guard fails loudly if an upstream internal moved), and installs the bridge deps wheels-only. There is no broader unit-test suite; otherwise verify by reading the code, compiling, and checking bridge/HA logs.

## Conventions you MUST follow

- **Commit identity:** commit as the repo's configured author (`git config user.name`/`user.email`). **Never add AI/Claude attribution** — no `Co-Authored-By`, no "Generated with", no assistant trailers of any kind.
- **Commit messages** explain the *why* and the evidence (what changed, what it fixes, how it was verified), not just *what*.
- **Never commit secrets.** `camera-bridge/.env` and `camera-bridge/secrets.env` are gitignored and hold live credentials; the `*.example` files are the templates. Do not print, log, or commit passwords, tokens, session/stream secrets, or the control secret.
- **Keep `docs/` shareable/anonymized.** Docs must describe the software generically and use placeholders (`<ha-host>`, `<doorbell-ip>`, `<unit-id>`, `<did>`, `<mac>`, …) — never real IPs, account/device IDs, hostnames, filesystem paths that reveal a user, or secrets. See `docs/development.md`.
- **Documentation is part of the change, not a follow-up.** After any change that
  alters behaviour, configuration, deployment, or the dependency set, update
  `docs/` in the SAME commit, and update the root `README.md` when the change is
  user-visible (a new capability, a changed setup step, a new requirement or
  limitation). Internal refactors that a user would never notice need only the
  `docs/` side. If a change adds a knob, it belongs in `docs/configuration.md`; a
  new failure mode belongs in `docs/troubleshooting.md`; a deferred idea belongs
  in the enhancement backlog rather than being dropped.
- **Versioning:** the version lives only in `custom_components/telus_smarthome/manifest.json`, with a matching `git tag vX.Y.Z`. **Bump the PATCH position (`x.y.Z`) for minor changes** — fixes, tuning, tests, docs, internal cleanup. **Bump MINOR (`x.Y.0`) only for major changes** — a new capability, a changed default, anything a user would notice or react to. Bias towards patch. Details in `docs/development.md`.

## Operational cautions (this talks to a LIVE account)

- **Rate limits can lock the account.** Alarm.com's limits are undocumented; a stampede of logins or polls can trigger `429` and, worse, an account lock. Honor `Retry-After`, never tight-retry against the edge, and don't lower poll intervals casually. The code already funnels 429s into a backoff — preserve that.
- **The push websocket drops ~every 6 minutes.** That's a server-side token lifetime, not a bug or an idle timeout. The reconnect/keepalive logic is intentional; don't "fix" it by mirroring a browser's websocket URL params (tested, rejected by the server).
- **Session rotation is load-bearing.** The bridge proactively rotates each camera's video session (~165s) ahead of the server's forced close, with several redundant safety nets and a disk-persisted restart guard (`restart_guard.py`). Read `camera_session.py` and its comments fully before touching it — the comments document previously-shipped bugs and why the current logic looks the way it does.
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
