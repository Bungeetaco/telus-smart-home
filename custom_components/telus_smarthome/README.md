# TELUS Smart Home (custom_components/telus_smarthome)

**This is a permanent, hand-maintained fork — not tracked against the
upstream `alarmdotcom` project it started from.** It's installable via HACS
as a custom repository (see the top-level README's "Installing" section),
which gives you a convenient install/update mechanism against *this* repo's
own git tags, but there's no automatic sync with upstream `alarmdotcom` or
any other package manager. Do not delete/replace this directory expecting
some other tool to bring it back "clean" — any future changes need to be
made directly to the files here (or in this project's own repo), the same
way this fork itself was built.

## What this is

A fork of the community `alarmdotcom` Home Assistant integration
(`pyalarmdotcomajax`), patched to work with **TELUS Smart Home**, which is a
white-labeled Alarm.com system that uses its own SSO handoff instead of
Alarm.com's normal login flow. The domain is `telus_smarthome`.

- `telus_auth_patch.py` — monkeypatches `pyalarmdotcomajax` at import time
  (imported for its side effect from `__init__.py` and `config_flow.py`) to
  perform the TELUS-specific SSO handoff before falling into the normal
  Alarm.com login/session code. If TELUS ever changes this SSO flow
  server-side, this is the first place to look — errors from it surface in
  the config flow as `telus_sso_error` (distinct from `invalid_auth`, which
  means the library got a normal "wrong password" response).
- `controller.py` — owns the `pyalarmdotcomajax` `AlarmController`, the
  `DataUpdateCoordinator`, the websocket keep-alive, and simple auth-health
  tracking (`auth_health` / `auth_health_detail` / `last_auth_success`)
  surfaced via the diagnostic sensor in `sensor.py`.
- `config_flow.py` — login (`user`), 2FA (`otp_select_method` /
  `otp_submit`), reauth (`reauth` / `reauth_confirm`), and **reconfigure**
  (`reconfigure`) flows. The reconfigure flow (Settings → Devices & Services
  → this integration's card → ⋮ → *Reconfigure*) lets you update the TELUS
  username/password in place — it does **not** delete/recreate the config
  entry, so all existing entities/devices/automations referencing them are
  preserved (removing and re-adding the integration would regenerate every
  entity ID).
- `sensor.py` — the "Auth Health" diagnostic sensor (`ok` /
  `reauth_required` / `telus_error`), so auth problems are visible as a
  normal HA entity/history graph instead of only in logs or via the reauth
  notification; plus the Recent Events activity-feed sensor, the talkdown
  status/failure sensors, the bridge-served per-camera diagnostic
  (signal / battery / status) and solar-panel sensors, and the per-camera
  stream-health sensors (stream state / dropped frames, off the bridge's
  `/control/status`) — see `docs/ha-integration.md` for the full entity
  list.
- `alarm_control_panel.py`, `lock.py`, `binary_sensor.py`, `cover.py`,
  `light.py`, and `climate.py` mirror the upstream integration's entity
  platforms, built on the shared `base_device.py` base classes;
  `binary_sensor.py` additionally carries the camera-bridge connectivity
  sensor, on the stream-health coordinator it shares with `sensor.py`.
- `switch.py`, `select.py`, `number.py`, and `button.py` carry both the
  upstream-style config-option entities (built on `base_device.py`) and this
  fork's bridge-backed camera/panel settings entities (privacy mode, chime,
  status LED, app-notification toggles, speaker volume, chime type,
  notification format, live resolution, siren buttons, Bluetooth disarming),
  built on `CameraSettingsEntity` and dedicated coordinators instead.
- `camera.py` and `event.py` serve bridge-backed live video, snapshots and
  clip events; `bridge_client.py` and the coordinator modules
  (`camera_settings_coordinator.py`, `arming_devices_coordinator.py`) are the
  shared plumbing to the bridge's loopback control API; `speak_service.py`,
  `bypass_service.py`, `unpair_service.py` and `clip_view.py` provide the
  fork's services and the signed clip-link HTTP view. Details in
  `docs/ha-integration.md`.

## Credential sync with the camera bridge

There is a **separate**, independently-deployed Docker service (the
`camera-bridge/` directory in this same repo -- see its own README) that
streams camera video into Home Assistant via its own Janus/WebRTC bridge. It
holds its own copy of the TELUS username/password (from its own
`secrets.env` file), so a password change made here must reach it too or
camera streaming silently breaks.

That is why both the `reauth` and `reconfigure` flows
(`async_step_final` → `_async_sync_bridge_credentials` in `config_flow.py`)
push updated credentials to the bridge's local control endpoint
(`http://127.0.0.1:8199/control/credentials`, both containers run with
`--network host` so this is real loopback), authenticated with a shared
secret read from `/share/telus_cam_bridge/control_secret`. This push is
**best-effort and non-blocking** — a fallback credentials file is always
written to `/share/telus_cam_bridge/credentials.json` first (via
`hass.async_add_executor_job`, since this is blocking file I/O), which the
bridge also watches/loads on its own, so a credential update is never lost
even if the bridge container happens to be down or is on an older build that
predates the control endpoint.

See `const.py` (`CAMERA_BRIDGE_SHARE_DIR`, `CAMERA_BRIDGE_CONTROL_URL`) and
the bridge's own README for the other half of this contract.

## Known limitations / deferred work

- Wrong-password vs. MFA-challenge vs. server-error differentiation is done
  at the exception-type level (`invalid_auth` / `telus_sso_error` /
  `cannot_connect`), not by inspecting TELUS's actual response bodies —
  doing that safely would require deliberately triggering those responses
  against a live account, which risks rate-limiting/lockout.
