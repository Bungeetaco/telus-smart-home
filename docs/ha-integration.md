# Home Assistant Integration Reference

The `telus_smarthome` custom integration is a Home Assistant **hub** integration
that brings a TELUS Smart Home (white-labeled Alarm.com) account into Home
Assistant. It is a fork of the community `alarmdotcom` integration, re-pointed at
TELUS's SSO login and extended with a set of camera/video features served by a
separate [camera bridge](camera-bridge.md).

Almost all device support (alarm panels, locks, sensors, thermostats, lights,
etc.) comes from the `pyalarmdotcomajax` library. The integration layers on top
of it:

- a **TELUS SSO login** replacement for the library's native Alarm.com login,
- **websocket resilience + push-to-HA** behavior so state changes appear
  immediately, and
- a family of **camera / video-clip / activity-feed** entities that are *not*
  part of the library's device model and instead talk to the local camera
  bridge over loopback HTTP.

This document is the reference for the integration package itself
(`custom_components/telus_smarthome/`). For the standalone container it depends
on, see [camera-bridge.md](camera-bridge.md); for the two-way-audio "talkdown"
service, see [talkdown-DEPLOY.md](talkdown-DEPLOY.md) and
[talkdown-fullduplex-protocol.md](talkdown-fullduplex-protocol.md).

---

## Manifest and dependencies

From `manifest.json`:

| Field | Value |
| --- | --- |
| `domain` | `telus_smarthome` |
| `integration_type` | `hub` |
| `iot_class` | `cloud_polling` |
| `config_flow` | `true` |
| `dependencies` | none |
| `requirements` | `pyalarmdotcomajax==0.5.13` |

The config-entry schema is at **version 4** (see [Config-entry
migrations](#config-entry-migrations)). Despite the `cloud_polling` IoT class,
the integration is push-first in practice (see [The update
model](#the-update-model)); polling is the fallback.

---

## Architecture at a glance

```
┌──────────────────────────── Home Assistant ────────────────────────────┐
│                                                                         │
│  config entry ──► AlarmIntegrationController ──► pyalarmdotcomajax API   │
│                        │                              │                  │
│                        │  (patched at import time)    │                  │
│                        ▼                              ▼                  │
│              DataUpdateCoordinator            REST poll + push websocket │
│                        │                                                 │
│      ┌─────────────────┼───────────────── entity platforms ──────────┐  │
│      │ alarm_control_panel, binary_sensor, lock, cover, light,        │  │
│      │ button, number, switch, select, climate, sensor                │  │
│      └────────────────────────────────────────────────────────────────┘ │
│                                                                         │
│      camera / event platforms + some switch/binary_sensor entities      │
│                        │  (loopback HTTP, Bearer secret)                 │
└────────────────────────┼────────────────────────────────────────────────┘
                         ▼
              Camera bridge container  (127.0.0.1:8199/control/*)
```

The controller and the library device tree drive the "real" Alarm.com devices.
A second, parallel data path (several small `DataUpdateCoordinator`s hitting
the camera bridge's local control server) drives cameras, video-clip events,
the activity feed, and video-automation switches.

---

## Setup and teardown lifecycle

### Import-time monkeypatches

`__init__.py` imports three patch modules **before anything else**, and
`config_flow.py` imports the auth patch too. Each patches `pyalarmdotcomajax` at
Python-import time from within `custom_components/`, so the patches persist
across Home Assistant Core container updates (the library is re-installed on
update, but these files are not). Order matters: the patches must be in place
before any `AlarmController` is constructed.

| Module | Purpose |
| --- | --- |
| `telus_auth_patch` | Replace the library login with TELUS SSO; re-point base URLs |
| `telus_ws_heartbeat_patch` | Add a websocket keepalive ping so the idle socket survives |
| `telus_ws_push_patch` | Keep the socket alive on bad messages; refresh HA on each push |

These are covered in detail under [The update model](#the-update-model) and
[TELUS SSO authentication](#telus-sso-authentication).

### `async_setup_entry` sequence

`async_setup_entry` (in `__init__.py`) runs the following, in order:

1. **Clip-link secret.** If the entry has no `clip_link_secret`, generate a
   32-byte hex secret and persist it into `config_entry.data`. This secret signs
   the clip-redirect links served by `clip_view.py`. It is generated once and
   **never rotated**: the signatures live inside already-delivered phone
   notifications, so a new secret would break every older clip link. It is
   created here, before the coordinators, so `event.py` can always assume it
   exists.
2. **Clip-redirect view.** Register `TelusClipRedirectView` on HA's HTTP server,
   guarded by a per-HA-run sentinel (`clip_view_registered`). Views cannot be
   unregistered and aiohttp raises on duplicate routes, so this happens once per
   HA run, not once per entry setup.
3. **Controller.** Construct `AlarmIntegrationController` and `await
   controller.initialize()`. Exceptions are translated to HA's setup states:
   - `OtpRequired` / `AuthenticationFailed` / `ConfigureTwoFactorAuthentication`
     → `ConfigEntryAuthFailed` (triggers reauth).
   - `TimeoutError` / `AlarmdotcomException` / `aiohttp.ClientError` →
     `ConfigEntryNotReady` (HA retries with backoff).
4. **Camera coordinator.** Construct the shared `CameraBridgeCoordinator` and
   `async_refresh()` it. It is created here (not in `camera.py`) so both
   `camera.py` and `event.py` share one instance regardless of platform setup
   order. `async_refresh()` is used deliberately instead of
   `async_config_entry_first_refresh()`: the latter would fail the *entire*
   config entry (alarm panel, locks, everything) just because the
   independently-deployed bridge is briefly unreachable at startup. Here a bridge
   failure simply leaves the coordinator data empty; camera/event entities appear
   on a later poll.
5. **Activity coordinator.** Construct the shared `ActivityHistoryCoordinator`
   and `async_refresh()` it, same reasoning: shared so `sensor.py`'s activity
   feed reads the same data `event.py` polls.
6. **Store in `hass.data`.** Under `hass.data[DOMAIN][entry_id]` with keys
   `DATA_CONTROLLER`, `DATA_CAMERA_COORDINATOR`, `DATA_ACTIVITY_COORDINATOR`.
7. **Websocket monitor.** Start `controller.async_start_websocket_monitor()` as a
   **tracked background task** via `config_entry.async_create_background_task`.
   Tracking matters: the monitor loops forever, so an untracked task would
   survive unload and keep running against a closed session: one leaked,
   permanently-reconnecting task per reload (and every options save, reauth, or
   reconfigure is a reload).
8. **Shutdown listener.** Register an `EVENT_HOMEASSISTANT_STOP` listener that
   awaits `controller.stop()`, wrapped in `async_on_unload`.
9. **Device pruning.** Reconcile the HA device registry against the current
   Alarm.com device list (see [Device pruning](#device-pruning-and-virtual-devices)).
10. **Virtual devices.** Create device-registry entries for Skybell cameras
    (config-only; video is handled by the bridge, not here).
11. **Forward platforms.** `async_forward_entry_setups(config_entry, PLATFORMS)`
    sets up all 13 entity platforms.
12. **Speak services.** Register the `telus_smarthome.speak` talkdown service
    (and its companion `telus_smarthome.stop_speaking`, which ends whatever is
    currently playing; stopping when nothing is playing is a no-op, so it is
    safe to call unconditionally from an automation)
    (see [talkdown-DEPLOY.md](talkdown-DEPLOY.md)), guarded so reloads are a
    no-op.

    Besides the target/source fields (`entity_id`, `message` /
    `media_content_id`, `engine`, `language`), `speak` takes two **optional**
    audio-shaping fields forwarded verbatim to the bridge's talk route:
    `conditioning` (boolean; `false` disables the bridge's default ffmpeg
    conditioning chain for that one clip) and `audio_filter` (an explicit
    ffmpeg `-af` chain that overrides both; `""` disables conditioning).
    A field the caller did not set is **absent from the POST body**, never
    defaulted: the bridge's own defaults are the contract (see
    [control-api.md](control-api.md#post-controlcamerascamera_idtalk)).
    The talk route's `codec` field is deliberately **not** exposed: only
    mu-law works on the reference doorbell, and a one-option selector would
    only invite a broken value. The passthrough is verified by `py_compile`
    and code reading only: it has not been fired against a live doorbell, and
    `speak_service.py` is outside what the integration's test suites cover
    (see [development.md](development.md#the-integration-suites)).
13. **Bypass service.** Register `telus_smarthome.bypass_sensors`, which bypasses
    or un-bypasses named sensors on the alarm partition. **A bypassed zone does
    not trigger the alarm until it is put back**, which is why this is a service
    rather than a switch on every sensor row: a stray tap should not be able to
    stop a zone protecting the house. It is deliberately separate from the
    `force_bypass` arming option and does not touch the arm path: `force_bypass`
    is automatic at arm time, this is manual, and only one of them being
    automatic is what keeps them from disagreeing.

    Two live-verified details it accounts for: the partition itself is re-read
    before every write and refuses when it says no (notably when armed, on a
    panel reporting `canBypassSensorWhenArmed` false), and the change takes about
    **ten seconds** to reach the sensor, so the post-write refresh is delayed
    past that window rather than firing immediately and reading the old state.

14. **Unpair service.** Register `telus_smarthome.unpair_arming_device`, which
    removes one phone from the panel's Bluetooth disarming so it can no longer
    disarm on approach. **It cannot be undone from Home Assistant**: re-pairing
    needs physical access to both the phone and the panel, which is the whole
    reason it is a service and not a button: a button sits on a dashboard beside
    controls that are all trivially reversible and gets pressed by accident, a
    service call has to be written deliberately.

    It takes exactly one entity, the "Disarm alarm on arrival" switch belonging
    to the phone to remove, never a list, because a list invites "unpair these
    three" written once and re-run later against a household that has changed.
    The entity is resolved through the entity registry and anything uncertain is
    refused: an unknown entity, one belonging to another integration, or one that
    is not an arming-device switch. The vendor's own unpair-all is not exposed at
    any level.

15. **Debug event listener.** Register a listener for `DEBUG_REQ_EVENT` that dumps
    a device's `debug_data` to the log, wrapped in `async_on_unload`.

### Controller initialization

`AlarmIntegrationController.initialize()` (in `controller.py`):

1. Calls `initialize_lite()`, which constructs the `pyalarmdotcomajax`
   `AlarmController` with a **dedicated aiohttp session**
   (`async_create_clientsession`) (the SSO handshake needs an isolated cookie
   jar) and calls `async_login()` (the TELUS-patched login).
2. Reads options, registers a config-entry update listener that reloads on
   options change.
3. Creates the main `DataUpdateCoordinator` with `update_method=async_update`
   and `update_interval` from options (default 300 s).
4. Registers the coordinator with `telus_ws_push_patch` so websocket pushes
   notify its listeners.
5. `await update_coordinator.async_config_entry_first_refresh()`: the first
   REST poll that populates device state.
6. Starts the 60-second keepalive task (`async_track_time_interval`).

`initialize_lite()` is shared between the real setup path and the config flow.
It deliberately does **not** translate `UnexpectedResponse`: the config flow has
its own handler that shows a TELUS-specific error string, while `initialize()`
translates `UnexpectedResponse` → `ConfigEntryNotReady` for its setup-only
callers.

### Device pruning and virtual devices

The device-reconciliation pass removes HA devices no longer present on
Alarm.com. It makes **one decision per device, after inspecting all of its
identifiers**, and only ever considers devices whose identifier looks like an
Alarm.com id (`<digits>-<digits>`, with optional `_debug`/`_malfunction` style
suffixes stripped by regex). Devices this fork creates itself (bridge-served
cameras (keyed by TELUS camera UUID), the Activity device, the diagnostic
"integration" device) are never Alarm.com ids and are always kept. This guards
against silently discarding user customizations (renames, area assignments,
enable/disable state) on every restart.

Skybell cameras are created as **virtual devices** in the registry (model
`Skybell HD`, manufacturer `Skybell`). These represent the *configuration*
surface (the switch/select/number config entities); live video for cameras is
handled by the camera bridge, not by these virtual devices.

### `async_unload_entry`

1. Pop the controller from `hass.data` and `await controller.stop()`.
   `controller.stop()` stops the keepalive, stops the websocket, **and closes the
   aiohttp session** (`close_websession()`). Closing the session on unload
   prevents one leaked session/connector/sockets per reload.
2. `async_unload_platforms(config_entry, PLATFORMS)`.
3. If no config entries remain (excluding the `clip_view_registered` sentinel),
   remove all three shared services: `speak`/`stop_speaking`, `bypass_sensors`,
   and `unpair_arming_device`.

### Config-entry migrations

`async_migrate_entry` migrates older entries forward. All migrations concern the
arming-mode options; credentials and other data are untouched.

| From → To | What changes |
| --- | --- |
| v1 → v2 | Introduce `use_arm_code` boolean; normalize `arm_code` to a string |
| v2 → v3 | Expand the old single force-bypass/silent/no-entry-delay settings into per-mode `arm_home` / `arm_away` / `arm_night` lists; purge deprecated keys |
| v3 → v4 | Drop the deprecated keys entirely; rename list members to the explicit `force_bypass` / `silent_arming` / `no_entry_delay` constants |

---

## The update model

The integration is **push-first with a polling fallback**.

- **Polling fallback.** The main `DataUpdateCoordinator` calls
  `controller.async_update()` every `update_interval` seconds (default **300 s /
  5 min**). Values below 300 s risk Alarm.com rate-limiting a live account.
- **Push.** A single websocket to Alarm.com receives real-time device pushes.
  When a push arrives, the library updates its in-memory device objects and the
  integration notifies coordinator listeners so entities re-read immediately,
  with no extra API calls.

Both matter because the Alarm.com server closes the push websocket roughly every
5–6 minutes (a token lifetime, not an idle timeout a ping alone can defeat).
Reconnecting fast keeps push up most of the time; polling at 5 minutes bounds how
stale a change landing in the reconnect gap can get.

### Websocket monitor and reconnect

`async_start_websocket_monitor()` runs forever as a tracked background task. Each
cycle:

1. Clear the close event and `start_websocket(self._ws_state_handler)`.
2. Wait for the socket to close, but **bound the wait only while pre-`RUNNING`**
   (`WEBSOCKET_START_TIMEOUT_SECONDS` = 60 s). `start_websocket()` only spawns a
   task; if that task dies before reaching `ws_connect`, no state callback fires
   and the state stays `STARTING`; an unbounded wait would then block forever
   and silently kill real-time updates. Once `RUNNING`, a healthy connection may
   stay open indefinitely and is never torn down on a timer.
3. On close (unless state is `STOPPED`), sleep `ws_reconnect_timeout` seconds
   (default **30 s**, configurable) and loop.

The whole loop is wrapped so that any unexpected exception is logged loudly and
retried rather than silently ending real-time updates for the rest of the HA
session. `asyncio.CancelledError` is re-raised (clean shutdown).

The state handler (`_ws_state_handler`) also triggers a one-shot
`async_request_refresh()` on each **reconnect** (not the initial connect; setup
has just polled). Anything Alarm.com pushed while the socket was down is gone
(it does not replay), so this refresh closes the blind window at ~1 second of
cost instead of a full poll interval.

### Keepalive

A 60-second task (`KEEP_ALIVE_INTERVAL_SECONDS`) calls the library's
`keep_alive()`. Failures (including plain network blips, which are not
`AlarmdotcomException` subclasses) are swallowed at debug level: a degraded
session is caught and correctly surfaced by the next coordinator refresh, so the
keepalive does not need to spam an uncaught-exception traceback every 60 s.

### Runtime patches: what they fix

These three patches exist because the stock library, driven by TELUS's server,
left real-time updates dead much of the time. Each is a generic fix any user of
this integration relies on.

#### `telus_auth_patch`: TELUS SSO

Replaces `AlarmController.async_login` with a handshake against TELUS's identity
gateway, and re-points the library's base URLs at TELUS. See [TELUS SSO
authentication](#telus-sso-authentication).

#### `telus_ws_heartbeat_patch`: keep the idle socket alive

The library calls `ws_connect()` with **no heartbeat**, so the websocket carries
zero traffic between pushes. The server drops idle sockets after ~5 minutes,
after which the integration waits a further reconnect timeout, leaving push dead
much of the time and state changes stuck behind the slow REST poll.

The patch wraps the session's `ws_connect` to default `heartbeat=55` seconds.
aiohttp then sends a WS ping every 55 s (well inside the ~5-minute window) and
fails fast if the pong is missing, which both defeats the idle drop and detects
dead connections promptly. A per-session flag (`_telus_hb_patched`) makes the
wrap idempotent.

> A documented dead end: the live portal connects with extra query params
> (`?f=1&…&ver=A`) that the library omits. Mirroring them was **rejected** by the
> server: the socket reached `RUNNING` and closed ~1 ms later, every time. Do
> not "fix" the endpoint to match the browser capture.

#### `telus_ws_push_patch`: resilience + push-to-HA refresh

Wraps the library's `_async_handle_message` to fix two defects that sat between a
device changing and HA showing it:

1. **A library crash killed the connection.** The library's message handler
   could hit an `UnboundLocalError` on an unrecognized device (one absent from
   its registry, e.g. a bridge-served doorbell camera, id like
   `<unit>-<device>`). The exception propagated out of the read loop and dropped
   the socket. Because such a device emits events regularly, real-time updates
   were dead much of the time. The wrapper **swallows per-message errors** so one
   unprocessable message can never end the connection.
2. **Nothing refreshed HA after a push.** The library applied each message to its
   in-memory objects (which entities read live), but no listener was notified, so
   even a successfully-processed push was invisible until the next poll. The
   wrapper **notifies every registered coordinator's listeners** after each
   message. Coordinators are held in a `weakref.WeakSet` (so a reloaded/removed
   entry's coordinator can be collected); the main coordinator registers itself
   via `register_coordinator()` in `controller.initialize()`. Notifying only
   re-reads objects already in memory: no extra API requests, no rate-limit
   contribution.

---

## TELUS SSO authentication

`telus_auth_patch` reverse-engineers TELUS's browser login (TELUS Smart Home is a
white-labeled Alarm.com dealer portal). The REST/websocket API is identical to
Alarm.com's; only the login handshake differs. The replacement
`async_login`:

1. Clears the session cookie jar (TELUS rejects a login if stale session cookies
   are present, important for re-login on session expiry; the dedicated session
   makes wiping safe).
2. **POST** `smarthome_login_v1`: submit credentials.
3. **POST** `flows/login?acr=…`: complete the login flow (must send an explicit
   empty JSON body; a bodiless POST 415s).
4. **GET** the OAuth2 authorization endpoint with `allow_redirects=True` and
   follow the full redirect chain, which lands on the page that sets the real
   dealer-portal session cookie.
5. Extract the anti-forgery (`afg`) cookie, then (unchanged from upstream) fetch
   identity/profile info (provider name, user id, email, session refresh
   interval, keepalive URL, Celsius preference).
6. Check whether OTP is required. This white-label deployment generally doesn't
   expose Alarm.com's native 2FA engine at this endpoint (auth already happened at
   TELUS's gateway), so any failure of the 2FA check is treated as "no 2FA
   needed" rather than a fatal error.

Two important behaviors:

- **Transient vs. credential errors.** A `429` or `5xx` from the login gateway
  is treated as transient (`UnexpectedResponse` → `ConfigEntryNotReady`, HA's
  auto-retry state), not as bad credentials (`AuthenticationFailed` →
  reauth/setup-error). This was confirmed live: `429` hit repeatedly after a day
  of frequent restarts and previously forced a needless manual reauth each time.
- **Base URL re-pointing.** `URL_BASE` is patched to the TELUS host, but that
  only covers URLs the library *formats* at call time. The camera-settings
  extension bakes a fully-qualified Alarm.com URL into a class attribute at its
  own import time, so its `ENDPOINT` host is rewritten in place; otherwise every
  Skybell config option (the switch/select/number platforms) would silently talk
  to the wrong host.

> **Caveat:** the patch does not handle an MFA/OTP challenge at TELUS's own
> gateway. None was observed in the captured login; if TELUS enables gateway 2FA
> later, the patch will need extending. The SSO handoff step explicitly raises a
> descriptive `AuthenticationFailed` ("possibly an MFA challenge this patch
> doesn't handle yet") if the redirect chain doesn't land on the expected page.

---

## Config flow

`config_flow.py` implements the UI-driven setup, reauth, reconfigure, and
options flows.

### Initial setup (`async_step_user`)

Prompts for username/password (both trimmed of whitespace to guard against a
pasted trailing newline). Builds a throwaway controller and calls
`initialize_lite()`. Outcomes:

| Exception | Result |
| --- | --- |
| `OtpRequired` | → OTP method selection step |
| `ConfigureTwoFactorAuthentication` | abort `must_enable_2fa` |
| `TimeoutError` / `ClientError` / `CancelledError` / `ConfigEntryNotReady` | error `cannot_connect` |
| `UnexpectedResponse` | error `telus_sso_error` (a TELUS-side change, not a bad password) |
| `ConfigEntryAuthFailed` | error `invalid_auth` |
| `AlarmdotcomException` | error `unknown` |
| success | → final step |

Each login attempt builds a fresh controller with its own session; the flow
closes the previous attempt's session (`_async_close_controller`) before
replacing it, so retyped passwords / reauth / reconfigure don't leak sessions.

### OTP steps

`async_step_otp_select_method` and `async_step_otp_submit` handle two-factor when
required. If the app method is the only enabled one, it is used without
prompting. On success the two-factor cookie is stored in the entry data. These
steps exist for completeness; TELUS's white-label deployment typically does not
surface 2FA (see [TELUS SSO](#telus-sso-authentication)).

### Final step (`async_step_final`)

- Title is `<provider>:<user_email>` (or the generic name for imports).
- **Unique ID is the account email.** This prevents onboarding the same account
  twice and guards reauth/reconfigure against silently repointing an existing
  entry at a different account (`_abort_if_unique_id_mismatch`, gated so a
  pre-migration entry without a unique id isn't spuriously rejected on its first
  reauth).
- For an existing entry (reauth/reconfigure) it syncs bridge credentials (below),
  closes this flow's controller, and calls `async_update_reload_and_abort` with
  **`data_updates=`, not `data=`**. `data=` would replace entry data wholesale,
  dropping `clip_link_secret`; `data_updates=` merges, preserving the
  never-rotate invariant.

### Reauth and reconfigure

- **Reauth** (`async_step_reauth` → `reauth_confirm` → `async_step_user`) is
  triggered by an API auth failure.
- **Reconfigure** (`async_step_reconfigure`) is reachable from the integration
  card's three-dot menu and lets the user update username/password in place
  without deleting/recreating the entry (entities and devices are preserved). The
  password field is never pre-filled.

### Camera-bridge credential sync

On reauth/reconfigure, `_async_sync_bridge_credentials` best-effort pushes the
new credentials to the bridge and **unconditionally writes a fallback file** the
bridge also watches:

- `_sync_write_fallback_and_read_secret` (in an executor thread) writes
  `credentials.json` to the shared dir with mode `0o600` and chowns it to the
  share dir's owner (the bridge's non-root user must be able to read it; a
  root-owned file caused the bridge's watcher to log `PermissionError` every
  30 s). It writes to a temp file and `os.replace`s it atomically, then reads back
  the bridge's `control_secret`.
- If a secret is found, it does a live `POST` to the bridge control endpoint with
  a `Bearer` header (using the **shared** HA session, not a created one, to avoid
  leaking a session per sync).
- Any failure is non-fatal: HA's own successful login is the sole determinant of
  reconfigure/reauth success. If the live push fails, the fallback file lets the
  bridge self-heal on its next poll or restart.

See [camera-bridge.md](camera-bridge.md) for the bridge side of this handshake.

### Options flow

Two screens (`async_step_init` → `async_step_modes`):

| Option | Meaning | Default |
| --- | --- | --- |
| `arm_code` | Optional code required for arm/disarm/lock (enter `CLEAR!` to clear) | empty |
| `update_interval` | REST poll interval, seconds | 300 |
| `ws_reconnect_timeout` | Websocket reconnect delay, seconds | 30 |
| `arm_home_options` | Multi-select of Force Bypass / Arm Silently / No Entry Delay | `[]` |
| `arm_away_options` | Same, for arm-away | `[]` |
| `arm_night_options` | Same, for arm-night | `[]` |

> The options flow deliberately does **not** assign `self.config_entry`: recent
> HA core made it a read-only property; assigning to it raised `AttributeError`
> and broke the options flow entirely.

---

## Entity platforms

`PLATFORMS` (from `const.py`) lists all 13:

| Platform | Backed by | Devices / capabilities exposed |
| --- | --- | --- |
| `alarm_control_panel` | library partitions | Arm home/away, arm night (if supported), disarm; arming/disarming transitional states; `uncleared_issues` attribute |
| `binary_sensor` | library sensors + water sensors, plus virtual attribute entities + the stream-health coordinator | Door / window / motion / smoke / CO / glass-break (vibration) / moisture / freeze / safety states; plus per-device **Malfunction** and **Battery** diagnostic sensors; plus the **Camera bridge** connectivity diagnostic on the integration service device (bridge-backed; see [Stream health and bridge connectivity](#stream-health-and-bridge-connectivity)) |
| `lock` | library locks | Lock/unlock with locking/unlocking transitional states; optional code |
| `cover` | library garage doors + gates | Open/close with opening/closing states; device class garage or gate |
| `light` | library lights | On/off, brightness (if supported); optimistic state for lights without state tracking |
| `button` | all stateful devices + bridge cameras | One **Debug** button per device (fires `DEBUG_REQ_EVENT` to dump `debug_data`); **siren start/stop** buttons per camera that supports them (bridge-backed) |
| `number` | camera config options + bridge cameras | Skybell **brightness** slider/box config option; camera **speaker volume** and **status-LED brightness** (bridge-backed; see [Camera settings, siren and diagnostics entities](#camera-settings-siren-and-diagnostics-entities)) |
| `switch` | camera config options + bridge automation rules + bridge cameras + arming devices | Skybell binary-chime on/off; **video automation rule** pause/resume; camera **privacy mode**, **indoor chime**, **outdoor tone**, **status LED** and the three **TELUS-app notification** toggles (motion / button press / smart chime); per-phone **Disarm alarm on arrival**; **Panel Bluetooth pairing** (all bridge-backed) |
| `select` | camera config options + bridge cameras + arming devices | Skybell **adjustable chime** volume and **motion sensitivity**; camera **wired chime type**, **TELUS-app notification format** and **live video resolution**; per-phone **enrolled partition**; **Bluetooth disarm window** (all but the Skybell options bridge-backed) |
| `climate` | library thermostats | HVAC mode (heat/cool/auto/fan-only/off), setpoints, fan mode, humidity readout |
| `sensor` | controller + activity coordinator + bridge cameras | **Auth health** diagnostic sensor; **Recent Events** activity-feed sensor; talkdown status/failure sensors; per-camera **signal strength / battery / status** diagnostics and **solar panel** status; per-camera **Stream state**, **Dropped frames** and **Audio state** stream-health diagnostics (bridge-backed) |
| `camera` | camera bridge | Live RTSP stream + on-demand snapshots (bridge-backed) |
| `event` | camera bridge | Per-camera video-clip events, a system-wide activity event, and a per-doorbell **button press** event (device class `doorbell`), all bridge-backed |

Most platforms follow the same shape: an `async_setup_entry` that reads the
controller from `hass.data`, then creates one entity per matching device in
`controller.api.devices.<collection>`.

### Base device classes

`base_device.py` provides the shared `CoordinatorEntity` subclasses:

- **`BaseDevice`**: holds the library device reference, builds `DeviceInfo`
  (manufacturer `Alarm.com`, linking to a partition device via `via_device` only
  when a real partition id exists), and exposes battery-level, battery-alert, and
  malfunction helpers. It registers an external update callback and, on each
  coordinator update, re-fetches its device by id. If the device was removed from
  Alarm.com mid-session it catches `UnkonwnDevice`, marks the entity unavailable
  (`_device_missing`, since `available` is a property and the `_attr_` shorthand
  wouldn't take), and keeps serving the last known object rather than freezing
  updates for *other* entities in the same dispatch cycle.
- **`HardwareBaseDevice`**: for real hardware (panels, sensors, locks, …). Sets
  `unique_id` to the device id and exposes a small set of static raw attributes
  (`state_text`, `mac_address`), also with a removed-device fallback.
- **`AttributeBaseDevice`**: for derived attribute entities (battery,
  malfunction, debug). `unique_id` is `<device_id>_<key>`.
- **`ConfigBaseDevice`**: for configuration-option entities (Skybell settings).
  `unique_id` is `<device_id>_<slug>`, entity category `CONFIG`.

`available` on `BaseDevice` combines the coordinator's health with the
device-still-exists check. Several platforms tighten it further (e.g.
`binary_sensor` also requires `is_on is not None`, so a stale "closed" is
reported honestly as unavailable when auth/polling has failed).

### Alarm control panel

Maps the library partition's `state`/`desired_state` to HA's
`AlarmControlPanelState` via `alarm_state` (the modern property; overriding
`state` is deprecated). When `state == desired_state` it reports the settled
state; when they differ it reports `ARMING`/`DISARMING`. `malfunction` yields
`None` (unknown). Supported features are arm-home + arm-away, plus arm-night when
the partition supports it. Arm/disarm methods pass the per-mode
force-bypass/silent/no-entry-delay options and translate `NotAuthorized` into a
`HomeAssistantError` plus a persistent-notification permission error. An optional
`arm_code` (numeric → `CodeFormat.NUMBER`, else `TEXT`) is validated client-side.

### Binary sensor

Creates "real" sensors from `sensors` + `water_sensors` (excluding blacklisted
subtypes and stateless devices), and "virtual" **Malfunction** and **Battery**
diagnostic sensors from *all* stateful devices. Device class is derived:

- Contact sensors are classified as **door** or **window** by matching the device
  name against precompiled language patterns (from `device_type_langs.py`);
  water sensors → **moisture**; smoke/CO/panic/glass-break/motion/freeze map to
  their obvious classes. The name-matching lists are compiled once into single
  alternation patterns because `device_class` is read on every state update; a
  hot path.

### Lock / Cover / Light / Climate

- **Lock**: lock/unlock with `is_locking`/`is_unlocking` transitional states
  from `state` vs `desired_state`; optional code (`code_format` derived from the
  configured `arm_code`).
- **Cover**: garage doors and gates; `CoverDeviceClass.GARAGE` or `.GATE`;
  open/close with opening/closing states.
- **Light**: on/off and brightness. Uses the modern `ColorMode` enum (the old
  `SUPPORT_BRIGHTNESS`/`COLOR_MODE_*` constants were removed from core and would
  crash every light). Brightness is scaled 0–255 ↔ 0–100. Lights without state
  tracking use `assumed_state` and optimistic local state.
- **Climate**: thermostats. `_legacy_refresh_attributes` maps device state to
  HVAC mode (aux-heat state folds into `HEAT`, since core removed the aux-heat
  concept), computes setpoints and min/max limits per mode, and sets fan mode.
  Feature flags and HVAC/fan mode lists are derived from the device's `supports_*`
  attributes. Setpoint limits are *removed* (not set to `None`) when not
  applicable, so core's sane defaults apply.

### Number / Select / Switch (Skybell config options)

`number.py`, `select.py`, and the `ConfigOptionSwitch` in `switch.py` iterate a
camera's library `settings` and create one entity per configuration option of a
matching type:

| Platform | Config-option types | Notes |
| --- | --- | --- |
| number | `BRIGHTNESS` | Slider when both min/max known, else box |
| select | `ADJUSTABLE_CHIME`, `MOTION_SENSITIVITY` | Options mapped from library enums |
| switch (`ConfigOptionSwitch`) | `BINARY_CHIME` | On/off chime; refreshes the main coordinator after a change |

These are currently Skybell-specific (noted with TODOs to generalize).

### Button

One diagnostic **Debug** button per stateful device. Pressing it fires
`DEBUG_REQ_EVENT` with the device id; the listener in `__init__.py` logs that
device's `debug_data` at warning level, a support aid.

### Sensor

`sensor.py` provides the **Auth Health** sensor plus the two talkdown sensors
(see [Talkdown diagnostics](#talkdown-diagnostics)) under a synthetic **"TELUS
Smart Home Integration"** service device rather than any Alarm.com device; the
**Recent Events** activity-feed sensor on the **Activity** device it shares
with the activity event entity; the per-camera diagnostic (signal strength
/ battery / device status) and solar-panel sensors on the camera devices (see
[Camera settings, siren and diagnostics entities](#camera-settings-siren-and-diagnostics-entities));
and the per-camera **Stream state**, **Dropped frames** and **Audio state**
stream-health sensors, also on the camera devices (see
[Stream health and bridge connectivity](#stream-health-and-bridge-connectivity)).
The two covered here:

- **`AuthHealthSensor`**: an enum diagnostic sensor
  (`ok` / `reauth_required` / `telus_error`) reflecting
  `controller.auth_health`, with `detail` and `last_success` attributes. Lets the
  user see auth failing from inside HA (history graph) instead of only via the
  reauth notification or logs. The controller sets these fields in
  `async_update()`.
- **`ActivityFeedSensor`**: state is the count of events on the current activity
  page; the full page (with a durable `clip_link` per clip-bearing event) is in
  the `events` attribute for a dashboard card to render as a scrollable feed. The
  `events` attribute is marked `_unrecorded_attributes` because it is several KB
  and changes on every 30 s poll (TELUS re-signs URLs each time); recording it
  would write ~2,880 multi-KB rows/day of already-ephemeral data. It shares the
  same `ActivityHistoryCoordinator` as the activity event entity, so it adds no
  extra bridge polling.

---

## Camera and event platforms (bridge-dependent)

Cameras and video-clip events are **not** part of `pyalarmdotcomajax`'s device
model. They are served entirely by the [camera bridge](camera-bridge.md), a
standalone container that owns the TELUS video session, WebRTC (Janus) → RTSP
transcoding, and its own TELUS camera-list poll. These HA platforms are thin
mirrors of the bridge's local control server, reached over loopback HTTP with a
`Bearer` secret read from the shared mount.

The shared helper `bridge_client.py` provides:

- `read_control_secret()`: blocking read of the bridge's `control_secret` off
  the shared dir (executor only); returns `None` if the bridge hasn't generated
  it yet (a normal, temporary state right after either container restarts).
- `bridge_get()`: read the secret, then make an authenticated `GET`; returns
  `None` when the secret isn't ready yet, and raises on other failures.

The philosophy throughout: **a bridge that is down or not yet up must never fail
the whole config entry or mark everything unavailable.** Coordinators return
their last data (or empty) on a missing secret and only surface `UpdateFailed` on
a real error.

### Coordinators and endpoints

| Coordinator | Endpoint | Interval | Consumers |
| --- | --- | --- | --- |
| `CameraBridgeCoordinator` (camera.py) | `/control/cameras` | 60 s | camera.py, event.py |
| `VideoEventsCoordinator` (event.py) | `/control/events` | 30 s | per-camera event entities |
| `ActivityHistoryCoordinator` (event.py) | `/control/history` | 30 s | activity event entity, doorbell-press event entities, activity-feed sensor |
| `AutomationRulesCoordinator` (switch.py) | `/control/automations` | 300 s | automation-rule switches |
| `CameraSettingsCoordinator` (camera_settings_coordinator.py) | `/control/cameras/{id}/settings` + `/diagnostics` | 120 s | volume number, privacy switch, siren buttons, camera diagnostic sensors, and *which cameras are doorbells* (creation gate only; the doorbell event entity itself rides the activity coordinator) |
| `StreamHealthCoordinator` (sensor.py) | `/control/status` | 30 s | per-camera stream-health sensors (stream state, dropped frames, audio state), camera-bridge connectivity binary sensor |
| `TalkMetricsCoordinator` (sensor.py) | `/control/metrics` | 120 s | talkdown status/failure sensors |

The camera-list interval (60 s) is kept below the bridge's own camera-discovery
interval so a camera the bridge just found appears in HA on the next cycle. The
camera and activity coordinators are created in `__init__.py` and shared via
`hass.data` so multiple platforms read the same instance. The stream-health
coordinator's 30 s interval is safe where other intervals are cautious:
`/control/status` answers from an in-memory dict the bridge's own camera loops
maintain, entirely over loopback with **zero Alarm.com requests behind it**, so
the short poll buys "the stream died" being visible within half a minute at no
rate-limit cost;
see [Stream health and bridge connectivity](#stream-health-and-bridge-connectivity)
for how it is shared between `sensor.py` and `binary_sensor.py`.

### Camera platform

`TelusCamera` is a `CoordinatorEntity` + `Camera` created **per bridge-reported
camera**, dynamically: `_add_new_cameras` runs as a coordinator listener on every
poll and adds entities for any newly-discovered camera id, so cameras added to
the TELUS account later appear with no restart. `available` follows the
coordinator health plus whether the bridge still reports that specific camera.

- `stream_source()` returns the RTSP URL the bridge publishes.
- `async_camera_image()` prefers a **warm live-stream frame** (the common case;
  streams stay up 24/7), then falls back to TELUS's **on-demand snapshot** via the
  bridge (`/control/cameras/<id>/snapshot`), then to a **cold-stream keyframe**.
  The snapshot call is bounded at 8 s to stay inside core's 10 s image budget (a
  larger budget previously caused dashboard thumbnails to 500 before any fallback
  could run). It validates the JPEG magic bytes because the bridge forwards
  whatever TELUS returns with a hardcoded `image/jpeg` content type, even an HTML
  error page.

### Event platform

Three entity kinds. The `CameraSettingsCoordinator` also appears here, but only
to decide *which* cameras get a doorbell entity; see below.

- **`TelusCameraEvent`**: one `EventEntity` per camera (sharing the camera's
  device via identical identifiers, so it appears as a second entity on the
  camera device). Fires `person` / `vehicle` / `animal` / `other` (classified
  from TELUS's free-text description, matching French and English) for any clip
  newer than the baseline. The baseline is re-derived on every entity creation so
  a **restart never replays old clips as new events**, and it adopts the first
  non-empty page as its baseline when the event coordinator is still empty at
  creation (otherwise the first successful poll would replay up to 20 historical
  clips and trigger every attached automation). State is written **once per
  event** so a multi-event poll produces one `state_changed` each.
- **`TelusActivityEvent`**: one entity for the whole account, covering the
  system-wide Activity feed (arm/disarm, door/lock, camera/doorbell triggers).
  Baseline is tracked as a **set of ids** rather than a timestamp high-water mark
  (activity ids are opaque strings; duplicate/formatting hazards make timestamps
  unsound). It also re-fires an event **exactly once more** if an entry gains a
  clip it lacked when first seen (`_fired_with_clip`): TELUS attaches the media
  relationship a poll or two after the entry first appears, so this avoids
  permanently losing a doorbell clip notification. Both sets are re-based onto the
  current (bounded, 20-entry) page each poll so they can't grow unbounded. Its
  attributes carry `event_type_name` (TELUS's untranslated server-side enum for
  what the entry *is*) so a template or condition on this catch-all entity can
  key off something the account's language does not change.
- **`TelusDoorbellEvent`**: one `EventEntity` per doorbell, device class
  **`doorbell`**, attached to that camera's existing device. Fires the single
  event type `press`. This is what a ring-triggered automation should trigger
  on; see [Triggering on a doorbell press](#triggering-on-a-doorbell-press)
  below for the mechanics and the rationale.
- All three entity kinds attach a durable, signed **`clip_link`** (via
  `_clip_link_for` → `clip_view.py`) alongside the raw `download_url`. The raw
  URL expires within minutes; the `clip_link` re-mints a fresh URL at click time
  and is what notifications should put behind their tap action.

### Triggering on a doorbell press

A press is also visible in `TelusActivityEvent`'s feed, but only as a generic
`other` event whose `description` is free text, so before this entity existed,
every ring automation had to pattern-match that text.

**Matching is on `event_type_name == "DoorbellButtonPress"`, never on the
description.** This account's activity descriptions have been observed in both
French (`Déclenché par caméra (Une personne)`, `Mouvement détecté`) and English
(`Button Push`, `Locked (Manually locked)`). The language is a server-side
account property, so a text match is a latent failure that would surface as the
doorbell simply never firing again: no error, no log line. `event_type_name` is
a server-side enum and is not translated. The bridge already carries it through
`/control/history` (see [control-api.md](control-api.md#get-controlhistory)); the
entity reads it straight off the coordinator.
`camera-bridge/tests/test_history_events.py` pins the rename it depends on, and
`tests/test_event_entities.py` pins the matcher itself from both sides: a
French-described press fires, and an entry whose description says `Button Push`
while its enum says otherwise does not.

**Which cameras get one.** `_is_doorbell()` gates on the chime capability flags
in the camera's `/settings` `audio` section: `supports_indoor_chime`,
`supports_outdoor_tone` or `supports_chime_type` (any one of the three; they are
independent). A chime is what a camera with no bell to ring does not have. Two
plausible-looking alternatives are deliberately rejected:

- **`notifications.supports_button_press`** resolves from
  `panelNotificationEnableDisableFeatureSupport`, the *same* upstream enum behind
  the motion-notification toggle, so it reads true for any camera that can push a
  notification at all, doorbell or not.
- **"has a press ever been seen for this camera"** is a direct signal but an
  unstable one: the feed is a bounded 20-entry page, so it answers "no" for a
  doorbell nobody has rung lately. The entity would appear and vanish across
  restarts, taking its history and breaking any automation referencing it, and
  the press that created the entity could not fire it.

Creation therefore goes through `async_add_camera_entities` (the same
capability-gated path the siren buttons and chime switches use), so a doorbell
added to the account later gets its entity with no restart, while *firing* is
driven by the activity coordinator, the only feed carrying presses.

**Attribution to a camera** is by `device_id`, which the history feed reports as
the alarm.com device id (`<unit>-<did>`), the same id shape `/control/cameras`
uses, and therefore expected to be the same id the entity was constructed with.
Never by `device_description`, which is a user-editable label.

**Unverified:** a press and its camera's `/control/cameras` entry have not been
observed side by side, so the two ids being byte-identical is inferred from the
shared id scheme rather than confirmed against a capture. If a doorbell entity
exists but never fires on a real press, check that equality first.

**Firing exactly once.** Dedup reuses `TelusActivityEvent`'s mechanism rather
than inventing a second one: a set of the press ids present on the current page,
re-based each poll. Activity ids are opaque strings so they cannot be compared
for recency, and a timestamp high-water mark is worse: a strict `>` permanently
discards anything sharing the newest timestamp, and one press reliably produces
several feed entries inside the same second. The set is bounded by the page size
because anything that scrolls off is older than everything still on the page and
can never legitimately return.

**A restart cannot re-fire an old press.** The baseline is re-derived at entity
creation and nothing is persisted, so every press already in the feed when the
integration loads is history. The empty-page case is handled the same way
`TelusCameraEvent` handles it: an empty first page means "the bridge isn't up
yet", not "no presses exist", so the first *non-empty* page is adopted as the
baseline instead of firing everything on it.

**Attributes:** `event_id`, `timestamp`, `camera_id`, `device_description`,
`description` (display only), `event_type_name`, plus the same
`thumbnail_url` / `download_url` / `clip_duration_ms` / `video_event_id` /
`clip_link` clip shape as the other two event entities. Expect the clip fields to
be `null` more often here: TELUS attaches the media relationship a poll or two
*after* the history entry appears, and this entity deliberately does **not**
re-fire when it does. A doorbell that rings an automation twice for one press is
worse than one whose first notification carries no thumbnail; use
`TelusActivityEvent`, which does re-fire exactly once on clip arrival, for a
clip-carrying notification.

---

## Camera settings, siren and diagnostics entities

A second family of bridge-dependent entities sits on top of the bridge's
camera-settings routes (see
[control-api.md](control-api.md#camera-settings-diagnostics-siren-and-privacy)):
a speaker-volume **Number**, a privacy-mode **Switch**, two siren **Buttons**, the
chime, LED and notification controls, and up to four diagnostic **Sensors**. They
exist because the useful half of the camera's own configuration (how loud
talkdown is heard, whether the chime rings, whether the Wi-Fi is degrading) was
previously reachable only through TELUS's own app or portal.

The camera entities attach to the **camera device** the
[camera platform](#camera-platform) already creates (identical `DeviceInfo`
identifiers), so they appear as extra entities on "Front Door" rather than under
the synthetic integration device that holds Auth Health and the talkdown sensors.
A camera the bridge no longer reports takes its settings entities with it.

The table below also covers the **alarm-side** entities that share this family's
machinery but no camera: the per-phone Bluetooth disarming controls, which live on
a device per paired phone, and the panel-wide Bluetooth settings, which live on
the integration's own service device. Those have their own coordinators and
routes; see [The Bluetooth disarming entities](#the-bluetooth-disarming-entities).

### The settings coordinator

One `CameraSettingsCoordinator`, shared by all four platforms, polls
`/control/cameras/{id}/settings` and `/control/cameras/{id}/diagnostics` per
bridge-reported camera every **120 s**.

That interval is deliberately slow. Each cycle is two bridge requests per camera
and nine upstream TELUS reads behind them: four website settings documents
(audio, image, video, and the `generalSettings` document that carries the LED,
notification and solar sections together), two mobile reads for the siren and
privacy records, and three more for signal, battery and status. These values only change when a person changes them, and Alarm.com's rate limits are undocumented
and can lock an account, so there is nothing to win by polling faster and a real
account-level risk in trying. Nothing in this path touches the always-warm video
pipeline.

Unlike the camera and activity coordinators it is **not** created in
`__init__.py`. `camera_settings_coordinator.async_get_camera_settings_coordinator()`
builds it on first use and stashes it under `DATA_CAMERA_SETTINGS_COORDINATOR`, so
whichever of the four platforms sets up first wins and the others attach to the
same instance, and an account with no bridge cameras never creates it at all.
Teardown is free: `__init__.py` pops the whole per-entry `hass.data` dict on
unload.

Its first refresh is a plain `async_refresh()`, never
`async_config_entry_first_refresh()`, for the same reason as the camera and
activity coordinators: the bridge is deployed and updated **separately** from the
integration, so it may be down, restarting, or simply an older build that does not
serve these routes yet. Any of those must leave these entities unavailable and
nothing else; aborting the config entry would take the alarm panel, the locks and
every sensor down with it.

That first refresh is also **not awaited**. It runs as a config-entry background
task (`async_create_background_task`), and `async_get_camera_settings_coordinator`
returns as soon as the instance is stashed. Swallowing the exception is only half
the protection: one poll cycle is two bridge requests per camera: the
`/settings` request bounded by the shared client's 10 s timeout and the
`/diagnostics` request by its own 45 s timeout
(`CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS`; the signal read alone can legitimately
take ~25 s on some models), so a bridge that is *slow* rather than *down*
costs up to ~55 s per camera and can outlast Home Assistant's 60 s
platform-setup budget with two cameras, which aborts whichever of
`button`/`number`/`switch`/`sensor` set up first, taking the Debug buttons, the
Skybell config entities and the integration diagnostics with it. Not waiting
turns that into "entities appear a moment later".

The cost of not waiting is that capability-gated entities are **late, not
missing**: with no data at setup time the siren Buttons cannot be created yet
(they exist only for a camera reporting the permission), so they arrive on the
first successful poll via the coordinator listener described below. Anything
referencing them must tolerate their absence for one poll interval after a
restart, the same tolerance a camera added to the account later already
requires.

### The entities

| Entity | Platform | Created when | Behaviour |
| --- | --- | --- | --- |
| **Speaker volume** | `number` | The camera reports `supports_call_volume` | Slider over the device's own `call_volume_min`–`call_volume_max` (0–10 on the doorbell), step 1, entity category *config*. Setting it POSTs `/settings/volume` and refreshes the coordinator from the response |
| **Privacy mode** | `switch` | One per bridge-reported camera | On/off via `/privacy`. Reports **unavailable** when the camera reports no privacy support |
| **Siren start**, **Siren stop** | `button` | Only when the bridge reports the camera has a siren **this account may manually trigger** | Each press POSTs `/siren` with `start` / `stop` |
| **Indoor chime** | `switch` | The camera reports `supports_indoor_chime` | On/off via `/settings/chime` (`indoor_on`), entity category *config*. Also reports **unavailable** while the selected wired chime type is one the bridge flags `disables_indoor_chime`; see below |
| **Outdoor tone** | `switch` | The camera reports `supports_outdoor_tone` | The tone the doorbell itself plays, on/off via `/settings/chime` (`outdoor_on`), entity category *config*. Its **volume** is a separate capability and has no entity: `supports_outdoor_tone_volume` is false on the reference doorbell |
| **Wired chime type** | `select` | The camera reports `supports_chime_type` | Which kind of physical chime the doorbell is wired to, *not* a ringtone. Options come from the bridge's `chime_type_choices` (e.g. None / Mechanical / Digital); selecting one POSTs `/settings/chime` with `chime_type` |
| **Status LED** | `switch` | The camera reports `led.supports_enabled` | The indicator light on the device, on/off via `/settings/led`, entity category *config*. **Not** the infrared illuminator: that is `ir_led_enabled` on the image route |
| **Status LED brightness** | `number` | The camera reports `led.supports_brightness` | Both cameras on the reference account report this **false**, so neither gets one; the entity exists for models that can dim and has never run against real hardware |
| **TELUS app motion notifications**, **TELUS app button press notifications**, **TELUS app smart chime notifications** | `switch` | Each gated on its own `notifications.supports_*` | What TELUS pushes to its **own app**: nothing to do with Home Assistant's notifications. Via `/settings/notifications`, entity category *config*. A camera that is not a doorbell reports every flag false and gets none |
| **TELUS app notification format** | `select` | The camera reports `notifications.supports_format` | Whether TELUS's push carries just text or a thumbnail. Two options only: the upstream enum has four members and the other two are a null state and a legacy value alarm.com rewrites away |
| **Live video resolution** | `select` | The camera reports `video.supports_live_resolution` | The **live** stream's resolution, labelled the way TELUS labels it. Changing it changes what the bridge restreams, verified live, despite the bridge asking TELUS for the "highest resolution" source. Takes effect on the next stream renegotiation, not immediately |
| **Disarm alarm on arrival** | `switch` | One per paired phone reporting `supports_disarming_toggle` | Whether that phone disarms the panel when it comes within range. Each phone is its own device in the registry, named as TELUS names it, so it is clear *whose* phone opens the house. Polled every 15 minutes; nothing but a person changes it |
| **Disarms partition** | `select` | One per paired phone reporting `supports_enrolled_partition`, **and only on an account with two or more partitions** | Which partition that phone disarms. Options come from the record's `enrolled_partition_choices`; selecting one POSTs `/arming-devices/{id}/enrolled-partition` with the partition **number**. See below for why a single-partition account gets no entity |
| **Panel Bluetooth pairing** | `switch` | The panel reports `supports_pairing_enabled` | Whether the panel accepts Bluetooth pairing at all, i.e. whether a phone that is not already enrolled can be added. On the integration's **service device**, not a camera and not a phone. **Not a master switch** over the per-phone toggles; see below |
| **Bluetooth disarm window** | `select` | The panel reports `supports_timeout` | How long the panel stays disarmable after a paired phone is seen. Also on the service device. A select rather than a number: the vendor offers exactly five windows (1, 5, 10, 20 and 30 minutes) and the bridge reports both the unit and the set, so the options are labelled "5 minutes" rather than a bare `5` |
| **Solar panel** | `sensor` | The camera reports `solar.supports_connected` | Whether a solar panel is attached, as an enum sensor reading *Connected* / *Not connected*, entity category *diagnostic*. **Both cameras on the reference account report the feature unsupported, so neither gets one** |
| **Signal strength** | `sensor` | The camera reports a signal strength | Diagnostic, graphable, but **no device class and no unit**: the bridge reports alarm.com's normalized 0–100 score, which is not dBm, and the fallback keys other models use have an unverified unit |
| **Battery** | `sensor` | The camera reports a battery level | Diagnostic, device class *battery*, % |
| **Camera status** | `sensor` | The camera reports a device status | Diagnostic, the upstream status string |

### Where each capability flag is read from

The volume Number and the diagnostic Sensors gate on fields the settings and
diagnostics responses document directly (`supports_call_volume`; a non-null
`signal_strength` / `battery` / `device_status`).

### Two small behaviours worth knowing

**Signal strength is labelled per camera, not globally.** The bridge says which
upstream key a reading came from, and the sensor declares a percentage only for
`normalizedSignalStrength`, alarm.com's 0-100 score. Other key spellings (kept
for camera models this was never tested against, and possibly real dBm) stay
unlabelled. Home Assistant records long-term statistics against the declared
unit, so a wrong label is not something a later fix undoes.

**Changing the wired chime type to "none" raises a notification.** That setting
turns the indoor chime off at TELUS and choosing a real type again does not turn
it back on. The integration does not silently re-enable it (re-arming something
someone may have wanted off is the worse failure) so instead it says what
happened, naming the camera, once per camera rather than once per change.

### The notification switches are TELUS's notifications, not yours

The three notification switches control what **alarm.com pushes to its own mobile
app**. Turning one off does not affect any automation, event or notification on
the Home Assistant side, and turning one on does not create one. The reason to
want them is the opposite of subtle: a doorbell press that notifies you twice,
once from the TELUS app and once from your own automation, and only one of those
is under your control from here.

They are gated independently and by three different upstream rules, which the
bridge has already collapsed into three booleans; see
[control-api.md](control-api.md). The visible consequence is that a camera which
is not a doorbell gets none of these switches, which is correct rather than
missing.

The notification **format** (text-only versus with a thumbnail) is a select.
Its two options are the only two alarm.com offers: the upstream enum has four
members, but one is a null state and one is a legacy value the portal rewrites
away when it reads it. The numbers behind them were read out of the portal's own
JavaScript, which is a public static asset, rather than guessed; see
[control-api.md](control-api.md).

### The chime entities, and why the indoor switch can go unavailable

The three chime entities are gated by **three separate** capability flags, which
is easy to get wrong because they sound like one feature: `supports_indoor_chime`
(the wired chime's on/off), `supports_chime_type` (which kind of chime it is), and
`supports_outdoor_tone` (the speaker the doorbell has of its own). A fourth,
`supports_outdoor_tone_volume`, is false on the reference doorbell (it can mute
the outdoor tone but not level it) so no volume control is offered for it.

The **wired chime type** is not a ringtone. It tells the doorbell what kind of
physical chime is on the other end of its wires, and its option names come from
the mobile API rather than being hardcoded: the website API publishes only bare
integers. If that secondary read fails the select still works, showing the
numbers. See [control-api.md](control-api.md#chime_type_choices-names-for-the-chime-types).

Each option carries a `disables_indoor_chime` flag, and the **Indoor chime switch
reports unavailable** when the selected type sets it: picking "no wired chime"
leaves the toggle nothing to ring, and the alarm.com portal greys its own control
out in the same state. Nothing in the integration hardcodes which number that is;
the flag exists so clients do not have to know. When the labelled options are
missing, or the current type is not among them, the switch stays available rather
than guessing itself into unavailability.

One upstream behaviour to know about, verified live: **picking the "none" chime
type turns the indoor chime off at TELUS, and picking a real type again does not
turn it back on.** The Indoor chime switch will therefore read *off* (not merely
unavailable) after a round trip through "none", and has to be switched on again
by hand. The integration does not paper over this: silently re-enabling a chime
the user may have wanted off is a worse failure than leaving it where TELUS put
it.

The siren and privacy capabilities come from somewhere else entirely: the
**mobile** API's `GetAllSirenDevices` and `GetAllCameraPrivacyModeInfo`, which
the bridge reads anyway before it will act on a write. It normalizes each into a
fixed shape and hands them back as two optional sections of the settings
response; the
[siren/privacy contract](control-api.md#the-siren-and-privacy-sections-a-fixed-contract)
in control-api.md is the authority on those key names, and this integration
depends on them exactly:

| Section | Field | Used for |
| --- | --- | --- |
| `settings.siren` | `can_manually_trigger` (bool) | Whether the siren Buttons exist at all |
| `settings.siren` | `active` (bool), `timestamp_end` | Not consumed today: the Buttons hold no state, deliberately (see below) |
| `settings.privacy` | `supported` (bool) | Whether the privacy Switch is available |
| `settings.privacy` | `active` (bool) | The Switch's on/off state |

Either section can be **`null`**, which the integration treats as *not reported*
and never as *unsupported*: a `null` siren section means no siren Buttons this
cycle, and a `null` privacy section leaves the Switch unavailable. Both are the
right outcome for a capability nothing has reported, and both mean neither
control can be pressed into a `422`. The most common cause is a bridge running
with `TALKDOWN_ENABLED=false`, which has no mobile session to read either record
through; it is a bridge-configuration state, not a device one.

Note that the siren Buttons hold no state even though `active` / `timestamp_end`
are available: the siren's duration is chosen server-side, so there is no on/off
the integration could honestly represent, hence two Buttons rather than one
Switch.

**Nothing here writes on its own.** Setup, polling and reloads only ever `GET`;
the volume, the switch and the buttons issue their `POST` solely from a user (or
automation) acting on the entity. That matters most for the siren, which is
audible hardware in a home; see the caution in
[control-api.md](control-api.md#post-controlcamerascamera_idsiren) before pressing
it the first time on a new device.

### The Bluetooth disarming entities

Three settings, all the same class of thing (who may walk up to a house and have
the alarm turn itself off) spread across two coordinators, both polling every
**15 minutes** because these are configuration records that only move when a
person moves them, and every cycle is a live Alarm.com round trip on an account
whose rate limits are undocumented. Neither poll is the feedback path for a change
made here: every write adopts the record the bridge answers with.

- **Per phone** (`ArmingDevicesCoordinator`, `GET /control/arming-devices`): the
  "Disarm alarm on arrival" switch and the "Enrolled partition" select. Each phone
  is its own device in the registry, named as TELUS names it, so it is clear
  *whose* phone opens the house.
- **Per system** (`PanelBluetoothCoordinator`, `GET /control/bluetooth-disarming`):
  the "Panel Bluetooth pairing" switch and the "Bluetooth disarm window" select.
  Both sit on the integration's **service device**, alongside Auth Health and the
  talkdown sensors: they belong to no phone and no camera, and the alarm panel's
  own device comes from the `pyalarmdotcomajax` device model, which these
  bridge-served entities have no handle on and which is not guaranteed to exist.

Three things about them are load-bearing:

**The panel pairing switch is not a master switch.** Whatever it looks like
sitting beside the per-phone toggles, turning it off is not established to stop an
already-paired phone from disarming. The TELUS portal's per-device page never
consults that document, and what the panel *firmware* does with the flag is
unresolved: "the portal does not read it" is evidence about the portal. What it
is definitely for is taking the panel out of pairing mode, so a phone that is not
already enrolled cannot be added. A user who wants to be sure a particular phone
cannot disarm must turn that phone's own switch off.

**The disarm window is a select, not a number.** The panel does not take a free
range: the vendor offers exactly five windows (1, 5, 10, 20 and 30 minutes) and
the bridge reports both the unit and the set, because the upstream document
publishes neither and no client could derive them. The options are labelled from
the unit the bridge reports rather than from a hardcoded word, and read "5
minutes" rather than a bare "5". A panel reporting a window the vendor no longer
offers reads as unknown rather than being snapped to the nearest option, which
would misreport it and make the next write look like a no-op.

**The enrolled-partition select is only created on an account with two or more
partitions.** That is a judgement about what to put in front of a user, not a
technical limit: the bridge accepts the write on a single-partition account
either way. An entity offering exactly one option, already selected, cannot
express anything: the only interaction it permits is re-selecting what is already
true, which is a whole-record `PUT` to a live alarm panel that changes nothing and
carries every risk such a write carries. It would also appear on the majority of
installs, since most homes have one partition, as a permanently inert control. The
information is not lost (the disarming switch publishes `enrolled_partition_id`
as an attribute either way) and the select appears by itself if a second
partition is ever added, because the builder is re-offered on every poll until it
yields something. An empty choices list (the bridge failing to read the partition
list) lands on the same "no entity" answer, and is likewise retried next poll.

One caveat applies to both panel-level entities: **the bridge's write shape for
`/control/bluetooth-disarming` is inferred from the sibling per-device endpoint
rather than captured.** If one of these writes fails, suspect the reconstruction
at least as readily as the panel.

The pairing switch also publishes the paired phones as `enrolled_device_ids`:
opaque ids and not descriptions, because those are household members' names and an
entity attribute is recorded, shown in the UI, and included in diagnostics
downloads. The switch entities are already named after the phones for anyone who
needs to tell which is which.

### The solar-panel sensor is a sensor, not a binary sensor

The underlying field is a plain boolean, so `binary_sensor` is the reflexive
choice, and the wrong one. Home Assistant renders a binary sensor's two states
through its device class, and there is no device class for "is a solar accessory
attached"; without one it reads as a bare On/Off, which for a wiring fact says the
wrong thing, since *On* suggests something is powered up or generating when all
the camera has said is that an accessory is present. An enum sensor reading
*Connected* / *Not connected* says exactly and only what the flag means. The
device classes that come closest (`plug`, `power`, `connectivity`) all describe a
live electrical or network condition rather than an installation.

There is no state class and no statistics: this does not change unless someone
physically installs or removes a panel. And it produces **no entity at all on
hardware that reports no solar support**, which is the case on both cameras this
was built against: a camera with no solar hardware reporting a permanent "not
connected" is noise dressed up as diagnostics. The gate is the support flag, never
the value: a camera that cannot answer the question sends the same
`connected: false` as one that answered "no".

### Absent versus unavailable

The two are used deliberately and differently:

- **A capability the device does not have gets no entity at all.** A camera with
  no adjustable speaker gets no volume Number; a camera this account may not
  sound gets no siren Buttons; a mains-powered camera gets no Battery sensor.
  Creating them anyway would put permanently-broken controls on the dashboard,
  and every press or slide would be a `422 unsupported` from the bridge.
- **Privacy mode is the exception: the Switch is always created and goes
  unavailable** when support is missing. Privacy support is read per poll, so
  "unsupported" and "the bridge did not answer this cycle" arrive the same way,
  and an entity that disappears takes its history and any automation referencing
  it with it, which is a much worse outcome than one that reads unavailable for a
  poll or two.

A write the device refuses (`422`) surfaces as a `HomeAssistantError` on the
service call rather than being swallowed, so a control that has stopped being
supported is visible instead of appearing to work. A rate-limited write (`429`)
surfaces the same way, worded "Alarm.com is rate-limiting this account; the
change was not applied -- try again in {N}s"; `async_bridge_post` takes the
wait from the bridge's `Retry-After` header, falls back to the response body's
`retry_after`, and ends "try again later" when neither is readable. The
integration only reports the server's wait; it never retries the write, so a
retry cannot walk straight back into the limiter.

The bridge's image and video/clip settings routes have **no entities yet**: the
URL templates are in `const.py` so the control surface is described in one place,
but brightness, HDR, clip resolution and retention are still curl-only. That is a
gap to fill, not a deliberate omission.


---

## Diagnostics

`diagnostics.py` implements `async_get_config_entry_diagnostics`. It returns the
redacted entry data/options plus a small controller summary
(`provider_name`, `auth_health`, `auth_health_detail`, `last_auth_success`,
`coordinator_last_update_success`).

Redaction is security-critical: `config_entry.data` stores a **plaintext
username and password** and the bridge credential-sync payload carries the same
password. `async_redact_data` only redacts by dict key, so `TO_REDACT` covers
`username`, `password`, `2fa_cookie`, and `arm_code`. Values derived from the
controller object (e.g. `user_email`) are not dict keys and cannot be caught by
key-based redaction; `user_email` is therefore **omitted entirely** rather than
partially masked. A diagnostics dump attached to a bug report must never leak
these.

---

## Key constants reference

From `const.py` (timing and identity):

| Constant | Value | Meaning |
| --- | --- | --- |
| `CONF_DEFAULT_UPDATE_INTERVAL_SECONDS` | 300 | Default REST poll (fallback behind push) |
| `CONF_DEFAULT_WEBSOCKET_RECONNECT_TIMEOUT` | 30 | Default reconnect delay after a socket close |
| `KEEP_ALIVE_INTERVAL_SECONDS` | 60 | Session keepalive interval |
| `HEARTBEAT_SECONDS` (heartbeat patch) | 55 | Websocket ping interval |
| `WEBSOCKET_START_TIMEOUT_SECONDS` (controller) | 60 | Pre-`RUNNING` backstop |
| `CAMERA_DISCOVERY_POLL_INTERVAL_SECONDS` | 60 | Camera-list poll |
| `VIDEO_EVENTS_POLL_INTERVAL_SECONDS` | 30 | Video-clip / activity poll |
| `AUTOMATION_RULES_POLL_INTERVAL_SECONDS` | 300 | Automation-rule poll |
| `CAMERA_SETTINGS_POLL_INTERVAL_SECONDS` | 120 | Camera settings + diagnostics poll |
| `CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS` | 45 | Per-request timeout for the `/diagnostics` bridge read (the signal read alone can take ~25 s on some models); the `/settings` read keeps the shared 10 s default |
| `ARMING_DEVICES_POLL_INTERVAL_SECONDS` | 900 | Bluetooth arming devices **and** the panel-wide Bluetooth settings poll |
| `TALK_METRICS_POLL_INTERVAL_SECONDS` | 120 | Talkdown-counter poll (`/control/metrics`) |
| `STREAM_HEALTH_POLL_INTERVAL_SECONDS` | 30 | Stream-health poll (`/control/status`): loopback-only, answered from bridge memory, zero Alarm.com cost |
| `CLIP_LINK_TTL_SECONDS` | 90 days | Signed clip-link lifetime |

Bridge control endpoints all live on `http://127.0.0.1:8199/control/…` (both
containers run `--network host`, so loopback reaches the bridge). The shared
directory is HA's `/share/telus_cam_bridge` (mounted into the bridge at
`/shared`). `hass.data` keys: `DATA_CONTROLLER`, `DATA_CAMERA_COORDINATOR`,
`DATA_ACTIVITY_COORDINATOR`, `DATA_CAMERA_SETTINGS_COORDINATOR`,
`DATA_ARMING_DEVICES_COORDINATOR`, `DATA_PANEL_BLUETOOTH_COORDINATOR`,
`DATA_STREAM_HEALTH_COORDINATOR`.

The blacklisted sensor subtypes (`SENSOR_SUBTYPE_BLACKLIST`) (mobile phone,
panel image sensor, fixed panic) are excluded from entity creation everywhere.

## Talkdown diagnostics

Two diagnostic sensors surface the bridge's talkdown counters (see
[control-api.md](control-api.md#get-controlmetrics)), so a talk that starts
failing is visible in Home Assistant instead of only as an HTTP error to whoever
triggered it. Both sit under the synthetic "TELUS Smart Home Integration" device
alongside Auth Health, and are polled slowly: the counters only move when
someone triggers a talk.

| Entity | State | Useful attributes |
| --- | --- | --- |
| `sensor.…_talkdown_status` | `idle`, `ok`, `stopped`, `rejected`, `failed` | `last_failure_reason`, `last_played_seconds`, `last_camera_id`, `by_path` (talks per transport: `full_duplex` / `webrtc` / `srtp`; which audio path is actually being used is otherwise invisible from HA), `recent` (last 5) |
| `sensor.…_talkdown_failures` | failed count since the bridge started (`total_increasing`) | `attempts`, `succeeded`, `stopped`, `rejected_in_progress`, `failures_by_reason`, `by_endpoint`, `by_codec`, `renegotiations`, `audio_seconds_played` |

The state is deliberately coarse (`failed`, not `failed:media_error`) so it is
easy to automate on; the reason rides along as an attribute. The failure count
resets to 0 when the bridge restarts, which `total_increasing` treats as a
counter reset rather than a spike.

### Getting told when talkdown breaks

```yaml
automation:
  - alias: Doorbell talkdown failed
    trigger:
      - trigger: state
        entity_id: sensor.telus_smart_home_integration_talkdown_status
        to: failed
    action:
      - action: notify.mobile_app_phone
        data:
          title: Doorbell talkdown failed
          message: >-
            {{ state_attr('sensor.telus_smart_home_integration_talkdown_status',
                          'last_failure_reason') }}
            (failures since bridge start:
            {{ states('sensor.telus_smart_home_integration_talkdown_failures') }})
```

`no_upstream_audio` suggests the device stopped advertising a talk path;
`transport_error` / `network_error` point at the doorbell or the LAN;
`media_error` means the audio you supplied could not be fetched or decoded.

## Stream health and bridge connectivity

Four entities surface the bridge's own pipeline diagnostics (the
[`GET /control/status`](control-api.md#get-controlstatus) payload), so a stream
stuck reconnecting, a camera publishing silence, or a bridge container that is
down altogether is visible in Home Assistant instead of only in the bridge's
container logs.

| Entity | Platform | Device | State |
| --- | --- | --- | --- |
| **Stream state** | `sensor` (enum, diagnostic) | the camera device | `connecting`, `streaming`, `reconnecting`, or `restart_deferred` |
| **Dropped frames** | `sensor` (`total_increasing`, diagnostic) | the camera device | lifetime count of frames the bridge dropped for that camera |
| **Audio state** | `sensor` (enum, diagnostic) | the camera device | `not_consuming`, `no_audio_yet`, `flowing`, or `stalled` |
| **Camera bridge** | `binary_sensor` (connectivity, diagnostic) | the integration service device | **on** while the last `/control/status` poll got an answer out of the bridge |

All four ride one `StreamHealthCoordinator` polling `/control/status` every
`STREAM_HEALTH_POLL_INTERVAL_SECONDS` (30 s). That interval is short where the
TELUS-backed coordinators are deliberately slow, and safely so: the route
answers from an in-memory dict the bridge's camera loops maintain themselves,
entirely over loopback: zero Alarm.com requests per cycle. The coordinator is
shared between `sensor.py` and `binary_sensor.py` through the same lazy
`hass.data` getter pattern as the settings and arming-device coordinators
(`async_get_stream_health_coordinator`, stashed under
`DATA_STREAM_HEALTH_COORDINATOR`): whichever platform sets up first creates it,
and the whole per-entry dict is popped on unload. Per-camera sensors are added
from a coordinator listener (the same pattern as the camera platform), so a
camera the bridge starts reporting later (or a bridge that comes up after HA)
gets its sensors with no restart.

**Stream state** pins the four states the bridge's `run_camera_forever` loop
can report. `restart_deferred` is the most informative one: the camera hit the
bridge's escape hatch, but `restart_guard` is holding the process before it
exits. The vocabulary is owned by a separately-deployed bridge, so a state this
build does not know degrades to **unknown** rather than erroring on every
update, with the verbatim value still readable in the `raw_state` attribute.
The supporting numbers ride along as attributes rather than more entities:
`fps` and `hardware_encode` (encoder properties that only ever change on a
publisher rebuild: `fps` is the rate the publisher is currently *declared*
with, which the bridge keeps under review while the stream runs but applies
stepwise, not live measurements worth a recorder series),
`consecutive_failures`, `last_error`, `last_frame_at`, and `raw_state`.

**Dropped frames** counts frames discarded because the consumer fell behind the
decoder: a sustained climb is a quietly-degrading stream that otherwise only
shows as throttled bridge log lines, and `total_increasing` lets an automation
trigger on it. It resets to 0 when the bridge restarts, which the state class
treats as a counter reset rather than a spike. The bridge folds each ~165 s
session's counter into the total as the session **ends**, so the value can lag
the live session's drops by up to one rotation.

**Audio state** answers "is this camera's audio flowing right now, or is it
dead?": the one question the rest of `/control/status` cannot answer, because
the bridge's frame-silence watchdog is video-only: a camera whose audio decoder
has died still reports `state: streaming` with a fresh `last_frame_at` while it
publishes pure silence. Its state is **derived here**, not read from a field:
the integration remembers `decoded_audio_frames` between polls and reports

| State | Meaning |
| --- | --- |
| `not_consuming` | the bridge is not consuming this camera's audio at all (`audio_consumed: false`; both `JANUS_REQUEST_AUDIO` and `JANUS_CONSUME_AUDIO` must name the camera) |
| `no_audio_yet` | audio is being consumed and the counter is still at zero: not one frame has ever decoded |
| `flowing` | the counter moved since the previous poll |
| `stalled` | it did not: audio was flowing and has stopped |
| unknown | no comparison is possible yet: the first poll of a camera that already has frames on the clock, or a bridge too old to report the audio fields |

Why a derived enum rather than a plain `decoded_audio_frames` counter: Home
Assistant has no "this number stopped changing" trigger, so a counter makes the
failure something a human has to notice on a graph, and the failure being
caught is exactly a counter that quietly stops. Comparing across polls turns
*stopped* into a state, which an automation can trigger on. The raw numbers are
not lost; they ride along as attributes: `audio_consumed`,
`decoded_audio_frames`, `decoded_audio_frames_last_poll` (the delta),
`audio_format` (`null` until a frame has genuinely decoded; a value here is
proof audio decoded at least once), `dropped_audio_frames`, `last_audio_at`
(when this poller last saw the counter move; empty until it does, and reset by a
Home Assistant restart), and `audio_dropped` / `audio_dropped_reason`.

Those last two say **why** audio stopped. The bridge drops a wedged audio pipe
deliberately and keeps publishing video rather than restarting the encoder, so
losing audio never costs the picture, but from the counter's point of view that
is indistinguishable from audio simply going quiet, and both read `stalled`.
`audio_dropped_reason` carries the bridge's own explanation, so the cause does
not require a trip to the container log. Both are `null` on a bridge too old to
report them, which is not the same as `false` and is not rendered as `false`.

Deliberately attributes rather than a fifth enum state: `stalled` is what an
automation should act on either way, and splitting it would break any automation
already keyed on it.

**Every camera gets an Audio state entity, consuming or not.** A camera whose
audio the bridge is not configured to consume (the per-camera
`JANUS_CONSUME_AUDIO` knob, off by default) reads `not_consuming`: a real
answer ("audio is switched off for this camera"), which is what someone asking
"why is there no sound?" needs, and it becomes `flowing` with no Home Assistant
restart when the knobs are turned on for that camera. The
conditional creation the per-camera diagnostics and solar-panel sensors use does
not apply: entity creation is sticky, so an entity that appears and vanishes
with a bridge env var breaks any automation naming it, and a camera with no
audio entity at all reads as "fine", the very gap this closes.

Reading the state honestly: `stalled` reflects **one poll window** (30 s).
Session rotation and a reconnect can each span one, so trigger on the state
*persisting*, not on the first sample:

```yaml
automation:
  - alias: Doorbell audio went silent
    trigger:
      - platform: state
        entity_id: sensor.<camera>_audio_state
        to: stalled
        for: "00:05:00"
    action:
      - service: notify.mobile_app_<device>
        data:
          title: Doorbell audio is dead
          message: >-
            Audio stopped at
            {{ state_attr('sensor.<camera>_audio_state', 'last_audio_at') }}
            after
            {{ state_attr('sensor.<camera>_audio_state', 'decoded_audio_frames') }}
            frames (format
            {{ state_attr('sensor.<camera>_audio_state', 'audio_format') }}).
            Video is {{ states('sensor.<camera>_stream_state') }}.
```

Add `no_audio_yet` as a second `to:` (with a longer `for:`) to catch a camera
that comes up consuming audio and never decodes a single frame, the same
condition as `audio_consumed: true` with `audio_format: null` on the raw
endpoint.

A stall is deliberately **not** reported when the coordinator could not reach
the bridge: an unanswered poll leaves the coordinator holding the previous
payload, and comparing that identical data against itself would read as "the
counter did not move". The comparison also treats a counter that went *down* as
a bridge restart (every number in `/control/status` resets with the process),
not a stall. All four ways a false `stalled` could be produced (an unanswered
poll, a failed poll, a counter reset, and the first poll of the entity's life)
are pinned by `tests/test_stream_health_sensors.py`.

**Camera bridge** reports whether the local bridge container is answering its
control API at all: **off** means the container is down, mid-restart, or its
control secret is not on the shared mount yet (that last case reads as
disconnected too). It deliberately breaks the plain coordinator-availability
contract: a coordinator whose poll fails marks its entities unavailable, and
for a connectivity sensor "the bridge is down" **is** the reportable state: an
entity that goes unavailable exactly when it should say "off" can never say
"off" at all. So it stays available once any poll attempt has completed,
successful or not, and is unknown only before the first attempt finishes.
