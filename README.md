# TELUS Smart Home for Home Assistant

Two components that together bring a TELUS Smart Home (white-labeled Alarm.com) security
system — panel, lock, door/motion/glass-break sensors, and live camera video — into Home
Assistant, including credentials that update from the HA GUI.

TELUS's consumer apps don't expose an official API, and their web portal authenticates
through TELUS's own SSO/OAuth gateway rather than Alarm.com's native login form. Both
pieces here exist because the standard `alarmdotcom` Home Assistant integration and the
standard Alarm.com camera tooling don't work against a TELUS-branded account. The TELUS
login flow and the Janus/WebRTC camera signaling were both reverse-engineered from HAR
captures of a real browser session (see comments in `telus_auth_patch.py` and
`camera-bridge/bridge/janus_client.py` for the exact request/response shapes this was
built against).

## Network requirement -- read this first

Both Home Assistant's own container and the camera bridge container **must run
with `--network host`**. This is a hard requirement, not a suggestion --
Janus/WebRTC's ephemeral RTP port range doesn't work through Docker's default
bridge networking, and the credential-sync endpoint between the two
components is loopback-only, reachable only because both share the host's
network namespace. `install.sh` (below) checks this up front and stops with a
clear message if it isn't true, rather than letting you find out halfway
through setup. If your Home Assistant install doesn't use host networking
(common on default HA Container/Docker setups, which typically use bridge
networking with port mapping instead), the camera bridge component won't work
as-is against that install.

## Installing

**Home Assistant integration** -- via HACS: open **HACS → ⋮ (top-right) →
Custom repositories**, add this repo's URL with category **Integration**,
then install "TELUS Smart Home" from HACS like any other integration.
(Manual alternative: copy `custom_components/telus_smarthome/` into your HA
config's `custom_components/` directory yourself.)

**Camera bridge** -- clone this repo onto the same box as Home Assistant and run:

```bash
cd telus-smart-home
./install.sh
```

This walks through the network pre-flight check, TELUS credentials, the
shared directory Home Assistant and the bridge use to sync credentials, and
building/starting the container. See [camera-bridge/README.md](camera-bridge/README.md)
for what it's doing under the hood, or to run the steps by hand.

## Components

### `custom_components/telus_smarthome/`

A Home Assistant custom integration — a fork of [pyalarmdotcom/alarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom)
with TELUS's SSO login patched in (`telus_auth_patch.py`, applied as a monkeypatch to the
`pyalarmdotcomajax` library at import time). Provides the alarm panel, lock, and sensor
entities, a GUI "Reconfigure" flow for updating credentials without losing entities/history,
and a diagnostic Auth Health sensor. See its own [README](custom_components/telus_smarthome/README.md)
for setup and architecture detail.

### `camera-bridge/`

A standalone Docker service that authenticates to TELUS, speaks the Janus Gateway
`streaming` plugin's WebRTC signaling protocol to pull live camera video, and re-serves it
as RTSP for Home Assistant's Generic Camera integration (and its bundled go2rtc) to consume.
Necessary because Alarm.com's video platform has no RTSP/ONVIF interface of its own — only
this proprietary WebRTC signaling path. See its own [README](camera-bridge/README.md) for
architecture, the credential-sync contract with the HA integration, and known limitations
(notably: Alarm.com force-closes video sessions after ~180s server-side, so the bridge
proactively rotates sessions every 165s — this is expected, self-healing behavior, not a bug).

## How the two talk to each other

Home Assistant's config entry is the single source of truth for TELUS credentials. When you
update them via the integration's Reconfigure flow, HA pushes the new credentials to the
camera bridge over a local-only authenticated HTTP endpoint, and also writes them to a
shared fallback file the bridge polls independently — so the bridge stays in sync even if it
was down at the moment of the update. See `camera-bridge/bridge/control_server.py` for the
exact contract.

## Status / disclaimer

This is a personal, unofficial integration built by reverse-engineering a specific TELUS
account's browser traffic. It is **not affiliated with or endorsed by TELUS or Alarm.com**,
and may break if either changes their web portal, SSO flow, or camera platform. It's
installable via HACS as a custom repository (see "Installing" above), but isn't in HACS's
default store and has no update-notification/release process beyond git tags on this repo.
Treat it as a reference/starting point, not a maintained public package with support
guarantees.
