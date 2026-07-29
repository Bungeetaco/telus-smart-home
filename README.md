# TELUS Smart Home for Home Assistant

**Bring a TELUS Smart Home (white-labeled Alarm.com) system into Home Assistant — panel, lock, sensors, and live camera video — with credentials that stay in sync from HA's own GUI.**

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.11.0%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Docker](https://img.shields.io/badge/camera%20bridge-Docker-2496ED?logo=docker&logoColor=white)](camera-bridge)
![Status](https://img.shields.io/badge/status-unofficial%20%26%20reverse--engineered-yellow)

TELUS's consumer apps don't expose an official API, and their web portal authenticates through
TELUS's own SSO/OAuth gateway rather than Alarm.com's native login form — so the standard
`alarmdotcom` Home Assistant integration and the standard Alarm.com camera tooling simply don't
work against a TELUS-branded account. Both pieces in this repo exist to close that gap. The TELUS
login flow and the Janus/WebRTC camera signaling were both reverse-engineered from HAR captures of
a real browser session — see the comments in `telus_auth_patch.py` and
`camera-bridge/bridge/janus_client.py` for the exact request/response shapes this was built
against.

> **Status:** personal project, unofficial and unaffiliated with TELUS or Alarm.com — see
> [Status and disclaimer](#status-and-disclaimer) before relying on it.

## Contents

- [What it brings into Home Assistant](#what-it-brings-into-home-assistant)
- [Network requirement](#network-requirement)
- [Installing](#installing)
- [Components](#components)
- [How the two talk to each other](#how-the-two-talk-to-each-other)
- [Camera stream authentication](#camera-stream-authentication)
- [For developers](#for-developers)
- [Status and disclaimer](#status-and-disclaimer)

## What it brings into Home Assistant

- 🔐 **Full panel control** — arm/disarm the alarm panel, lock/unlock, and read every
  door/motion/glass-break sensor as native HA entities.
- 📹 **Live camera video** — cameras appear as native HA camera entities, created automatically
  from whatever the bridge currently sees; under the hood the bridge translates Alarm.com's
  proprietary WebRTC signaling into ordinary RTSP that HA (and its bundled go2rtc) consumes
  directly. Nothing to configure by hand — the integration reads the stream URLs, credentials
  included, straight from the bridge.
- 🔒 **Authenticated camera streams** — the bridge's RTSP server requires a password (generated
  per install), so nothing else on the host can quietly read your camera feeds or publish over
  them.
- 🔄 **Credentials that actually stay in sync** — update your TELUS username/password once, from
  HA's own Reconfigure flow, and both the integration *and* the separate camera bridge process
  pick it up — no more silently-stale bridge credentials after a password change.
- 🩺 **Self-healing camera sessions** — Alarm.com force-closes video sessions server-side every
  ~180s; the bridge proactively rotates ahead of that, bridges the gap so viewers see at most a
  brief freeze, detects dead/silent sessions independently, and hard-restarts itself if a session
  gets truly wedged rather than retrying forever in a broken state.
- 🩹 **A diagnostic Auth Health sensor** — see login/session state from within HA instead of
  guessing from logs.
- 🧩 **No official API required** — both the SSO login and the camera signaling were
  reverse-engineered from real browser traffic; see [For developers](#for-developers) for where
  that lives in the code.

## Network requirement

**Read this before installing.** Both Home Assistant's own container **and** the camera bridge container must run with
**`--network host`**. This is a hard requirement, not a suggestion — Janus/WebRTC's ephemeral RTP
port range doesn't work through Docker's default bridge networking, and the credential-sync
endpoint between the two components is loopback-only, reachable only because both share the
host's network namespace.

`install.sh` checks this up front and stops with a clear message if it isn't true, rather than
letting you find out halfway through setup. If your Home Assistant install doesn't use host
networking (common on default HA Container/Docker setups, which typically use bridge networking
with port mapping instead), the camera bridge component won't work as-is against that install.

## Installing

**1. Home Assistant integration** — via HACS: open **HACS → ⋮ (top-right) → Custom
repositories**, add this repo's URL with category **Integration**, then install "TELUS Smart
Home" from HACS like any other integration.

**2. Camera bridge** — clone this repo onto the same box as Home Assistant and run the installer:

```bash
git clone https://github.com/Bungeetaco/telus-smart-home.git
cd telus-smart-home
./install.sh
```

This walks through the network pre-flight check, TELUS credentials, the shared directory Home
Assistant and the bridge use to sync credentials, and building/starting the container.

### Integration options (state-change latency)

Door/window/lock changes arrive over Alarm.com's push websocket in **well under a second**
(measured: sensor tripped at `17:16:40.729Z`, Home Assistant state written at `17:16:41.223`).
The REST poll is only a backstop for events pushed while the socket happens to be down.

The socket goes down predictably: **the server closes it roughly every 6 minutes**, regardless
of traffic. It's a token lifetime, not an idle timeout — keepalive pings don't prevent it.
Everything pushed between that drop and the reconnect is lost and only surfaces on the next
poll, so the reconnect delay is the single biggest factor in perceived latency.

| Option | Default | What it does |
| --- | --- | --- |
| Websocket reconnect timeout | `30` s | Delay before reconnecting after the server drops the socket — i.e. **how long the blind window lasts**. Lowering it does not increase request volume: the reconnect *count* is fixed by the server's ~6 min cycle, so only the gap shrinks. `5` s is strictly better; at `30` s roughly 8% of events miss the socket entirely and wait for the poll. |
| Update interval | `300` s | REST poll period — the backstop, and the worst-case latency for a push lost in a blind window. |

If a change ever seems slow, it almost certainly landed in a reconnect gap rather than meaning
push is broken. To tell which path delivered a state, check the log: a push updates one device
(`Got update for <name> ... from WebSocket message`), whereas a poll logs `Requesting update
from Alarm.com` and then updates *every* device in the same millisecond.

Alarm.com's rate limits are undocumented and, if tripped, can lock the account. Since push
carries the latency-sensitive path, there is little reason to poll aggressively — `300` s is a
sound default. `60` s has been run without a single HTTP `429`, but watch the log if you
shorten it.

**Do not "fix" the websocket endpoint to match a browser capture.** The live portal connects to
`wss://webskt.alarm.com:8443/?f=1&auth=<token>&ver=A` while the library uses only
`?auth=<token>`. Mirroring the portal's form was tested and rejected by the server: the socket
reached RUNNING and closed 1 ms later, every time, looping on the reconnect timer. Those params
evidently pair with something else in a browser session.

Existing installs keep whatever they have — these defaults only apply to new config entries,
so update the two values via Configure if you installed before they changed.

### Doorbell / camera event snapshots

The `event.*_recent_activity` entities carry two useful attributes when a clip is recorded:
`thumbnail_url` and `download_url`. For "who was at the door" snapshots, prefer these over
grabbing a frame from the live camera — two measured reasons:

- **The clip starts before the subject is in view.** Alarm.com's video-events API reports
  `timestampToStartClip` (12 s on a doorbell here), so a frame captured when the event fires
  shows an empty scene.
- **Camera stills can be stale.** `async_camera_image()` returns the stream's last cached
  keyframe rather than waiting for a fresh one, so a stalled RTSP publisher yields the same
  image indefinitely — observed as byte-identical files across events minutes apart, and one
  frame 42 minutes old.

`thumbnail_url` is Alarm.com's own detection frame: the moment the subject was classified,
with a labelled rectangle drawn around them. **That rectangle is rendered server-side and no
query parameter removes it** — `vvui` and `easi` make no difference to the returned bytes, and
`culture` only switches the label language (`2` = French "personne", `1` = English). If you
need an unannotated image, fetch `download_url` and seek into the clip instead:

```bash
ffmpeg -nostdin -ss 12 -i clip.mp4 -frames:v 1 -q:v 3 frame.jpg
```

The signed token in both URLs is **self-contained** — the image fetches with no session,
cookies or headers — but it expires within hours, so fetch at event time rather than storing
the URL for later. An expired token returns a small JPEG reading "You don't have permission to
view this clip" (or "Clip supprimé"), not an HTTP error, so check the payload rather than just
the status code.

<details>
<summary><strong>Prefer to do it by hand, or just want the manual-install alternative?</strong></summary>

**HA integration without HACS** — copy `custom_components/telus_smarthome/` directly into your HA
config's `custom_components/` directory, then restart Home Assistant and add the integration from
Settings → Devices & Services as usual.

**Camera bridge without the installer**:

```bash
cd telus-smart-home/camera-bridge
cp .env.example .env
cp secrets.env.example secrets.env   # fill in TELUS_EMAIL / TELUS_PASSWORD
chmod 600 secrets.env                # it holds your password in plaintext
sudo docker compose up -d --build
```

`chmod 600` is not optional housekeeping: `cp` creates the file using your umask,
which on most distributions leaves it world-readable (0644) — so every local user
could read your TELUS password. `install.sh` sets these permissions for you.

If your TELUS password contains a literal `$`, escape it as `$$` in `secrets.env` — Docker Compose
interpolates `$`-references in `env_file` values, and an unescaped `$` gets silently corrupted
(verified during development, not a hypothetical). `install.sh` does this escaping for you
automatically if you let it prompt for credentials instead of hand-editing the file.

</details>

<details>
<summary><strong>Camera bridge settings (environment variables)</strong></summary>

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `TELUS_EMAIL` | **yes** | — | TELUS account email. Lives in `secrets.env`, not `.env` — see the `$`-escaping note above for why the two files are kept separate. |
| `TELUS_PASSWORD` | **yes** | — | TELUS account password. Same file/reasoning as above. |
| `MEDIAMTX_RTSP_PORT` | no | `8554` | Port MediaMTX (bundled in the container) serves RTSP on. Requires authentication — see [Camera stream authentication](#camera-stream-authentication). |
| `LOG_LEVEL` | no | `INFO` | Python logging level for the bridge process. |
| `CONTROL_PORT` | no | `8199` | Loopback-only port for the credential-sync control endpoint HA's Reconfigure flow pushes to. |
| `HOST_SHARED_DIR` | no | `/var/lib/homeassistant/share/telus_cam_bridge` | Host-side path shared with the Home Assistant container — holds the control secret, the credential fallback file, and the generated RTSP password. Override if you're not on the HA Supervised default layout. |
| `VAAPI_DEVICE` | no | `/dev/dri/renderD128` | Render node passed to ffmpeg for hardware (QuickSync) H.264 encoding. Override if your host has more than one GPU and the numbering differs. |
| `RENDER_GID` | no | `992` | Host GID of the `render` group, granted to the container's non-root user for `/dev/dri` access. Must match `getent group render` on your actual host if it differs. |

</details>

## Components

### [`custom_components/telus_smarthome/`](custom_components/telus_smarthome)

A Home Assistant custom integration — a fork of
[pyalarmdotcom/alarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom) with TELUS's SSO login
patched in (`telus_auth_patch.py`, applied as a monkeypatch to the `pyalarmdotcomajax` library at
import time). Provides the alarm panel, lock, and sensor entities, a GUI "Reconfigure" flow for
updating credentials without losing entities/history, and a diagnostic Auth Health sensor. See its
own [README](custom_components/telus_smarthome/README.md) for setup and architecture detail.

### [`camera-bridge/`](camera-bridge)

A standalone Docker service that authenticates to TELUS, speaks the Janus Gateway `streaming`
plugin's WebRTC signaling protocol to pull live camera video, and re-serves it as RTSP for Home
Assistant's Generic Camera integration (and its bundled go2rtc) to consume. Necessary because
Alarm.com's video platform has no RTSP/ONVIF interface of its own — only this proprietary WebRTC
signaling path. See its own [README](camera-bridge/README.md) for architecture, the
credential-sync contract with the HA integration, and known limitations.

## How the two talk to each other

Home Assistant's config entry is the single source of truth for TELUS credentials. When you
update them via the integration's Reconfigure flow, HA pushes the new credentials to the camera
bridge over a local-only authenticated HTTP endpoint, and also writes them to a shared fallback
file the bridge polls independently — so the bridge stays in sync even if it was down at the
moment of the update. See `camera-bridge/bridge/control_server.py` for the exact contract.

The same shared directory carries the camera streams' RTSP password, which is how the integration
knows how to authenticate to them without you configuring anything.

## Camera stream authentication

**As of v1.4.0 the bridge's RTSP server requires a username and password.** Before that it ran
with MediaMTX's default anonymous access, which — because both containers use `--network host` —
meant any process on the host could read the camera feeds, or publish over them, using path names
that are trivially derived from the camera ID.

You don't have to set this up. On first start the bridge generates a random password into
`<shared dir>/rtsp_password` (mode `0600`) and builds it into the stream URLs it hands to Home
Assistant, so the integration's camera entities keep working with no configuration.

It only matters if you point something else at a stream directly:

```
rtsp://bridge:<contents of rtsp_password>@127.0.0.1:8554/telus_<camera_id_with_underscores>
```

> **Upgrading from v1.3.0 or earlier:** anything already holding a credential-less
> `rtsp://127.0.0.1:8554/...` URL will start getting `401 Unauthorized`. Home Assistant's own
> camera entities fix themselves once HA is restarted (they re-read the URLs from the bridge), but
> a hand-configured **Generic Camera** entity pointing at the old URL must be updated to the
> authenticated form above, or replaced by the integration's own camera entities. To rotate the
> password, delete `rtsp_password`, restart the bridge, then restart Home Assistant.

## For developers

Interested in how it works or want to hack on it? Both component READMEs
([integration](custom_components/telus_smarthome/README.md),
[camera bridge](camera-bridge/README.md)) go deep on their own architecture — the short version:

- **`telus_auth_patch.py`** (integration) and **`camera-bridge/bridge/telus_auth.py`** (bridge,
  a separate from-scratch implementation with no shared dependency) are where TELUS's SSO/OAuth
  dance is reimplemented. Both were built by reading HAR captures of a real browser session against
  a live TELUS account, not from any published spec.
- **`camera-bridge/bridge/janus_client.py`** implements just enough of the Janus Gateway
  `streaming` plugin's WebRTC signaling to pull frames — also reverse-engineered, comments there
  document the exact request/response shapes it was built against.
- **`camera-bridge/bridge/camera_session.py`** owns the per-camera reconnect/backoff loop,
  including the escalation to a full process restart if a session gets stuck failing — read it in
  full before touching it, the comments explain several previously-shipped bugs and why the
  current logic looks the way it does.

If TELUS or Alarm.com change their web portal, SSO flow, or camera platform, the place to start
debugging is a fresh HAR capture compared against what these files currently expect.

## Status and disclaimer

This is a personal, unofficial integration built by reverse-engineering a specific TELUS account's
browser traffic. It is **not affiliated with or endorsed by TELUS or Alarm.com**, and may break if
either changes their web portal, SSO flow, or camera platform.

It's installable via HACS as a custom repository (see [Installing](#installing) above), but isn't
in HACS's default store and has no update-notification/release process beyond git tags on this
repo. Treat it as a reference/starting point, not a maintained public package with support
guarantees. No license has been specified for this repository yet.

**Known limitations:**

- **No MFA/2FA support in the camera bridge.** Its TELUS login is username/password only — if
  TELUS ever enables MFA on this account, the bridge's login breaks independently of the HA
  integration (which does support 2FA/OTP). See `camera-bridge/README.md`'s own limitations
  section for why this one specifically needs a real MFA-challenge HAR capture rather than
  being implementable from the existing reverse-engineering notes alone.
- The camera bridge's control-endpoint secret now rotates automatically (see
  `camera-bridge/README.md`); manual rotation (delete `control_secret`, restart the container)
  still works too.
