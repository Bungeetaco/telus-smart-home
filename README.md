# TELUS Smart Home for Home Assistant

**Brings a TELUS Smart Home system (which is really a white-labeled Alarm.com account) into Home Assistant — the alarm panel, locks, sensors, live camera video, and two-way audio through the doorbell.**

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2024.11.0%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Docker](https://img.shields.io/badge/camera%20bridge-Docker-2496ED?logo=docker&logoColor=white)](camera-bridge)
![Status](https://img.shields.io/badge/status-unofficial%20%26%20reverse--engineered-yellow)

TELUS doesn't publish an API, and their portal logs in through TELUS's own SSO/OAuth gateway instead of Alarm.com's normal login form. That's enough to break the standard `alarmdotcom` integration and every off-the-shelf Alarm.com camera tool against a TELUS account. This repo exists to close that gap. The login flow and the camera signaling were both worked out by reading the network traffic of a real browser session — there was no spec to build from.

> **Heads up:** this is a personal, unofficial project, not affiliated with TELUS or Alarm.com. It can break any time either of them changes their portal. See [Status and disclaimer](#status-and-disclaimer) before you lean on it.

## What you get

- **The whole security panel as HA entities** — arm and disarm, lock and unlock, and read every door, motion, and glass-break sensor natively.
- **Live camera video** — cameras show up as ordinary HA camera entities. Behind the scenes a bridge translates Alarm.com's proprietary WebRTC into plain RTSP that HA (and its bundled go2rtc) can play. There's nothing to wire up by hand; the integration reads the stream URLs, password included, from the bridge.
- **Two-way audio ("talkdown")** — speak through the doorbell's speaker from HA: a text-to-speech phrase, or any audio clip you point it at, via a `telus_smarthome.speak` service, and stop it early with `telus_smarthome.stop_speaking`. See [Two-way audio](#two-way-audio-talkdown) below.
- **The alarm side, where it was worth having** — a switch per paired phone for whether it may auto-disarm the panel on approach, and a `bypass_sensors` service for skipping specific zones. Bypass is deliberately a service and not a per-sensor toggle: a bypassed zone stops triggering the alarm until you put it back, and that is not something a stray tap should do.
- **The camera's own settings, as entities** — how loud the doorbell speaks (the same volume talkdown is heard at), privacy mode, and the siren, from Home Assistant instead of the TELUS app. Alongside them, per-camera signal strength, battery and status, so a camera drifting onto bad Wi-Fi becomes a number you can alert on rather than a picture that quietly gets worse. Controls only show up for cameras that report supporting them, so a capability your camera lacks leaves no broken button behind. The doorbell's chime is there too: switches for the indoor chime and the outdoor tone, and a picker for the kind of chime the doorbell is wired to — mechanical, digital, or none — with the names coming from TELUS rather than guessed at. There's a switch for the device's status LED as well — the little light on the front — for anyone who'd rather it weren't lit, and switches for TELUS's own push notifications, which are handy if you'd rather your doorbell told you about visitors once rather than twice. There's also a picker for the live video resolution, which is the honest lever on how much bandwidth a camera uses — it changes what the bridge itself receives, not just what the TELUS app shows. Picture and clip settings are reachable through the bridge's control API but do not have entities yet — see [docs/control-api.md](docs/control-api.md).
- **Credentials that stay in sync** — change your TELUS password once, in HA's Reconfigure flow, and both the integration and the separate bridge process pick it up. No more silently-stale bridge login after a password change.
- **Cameras that heal themselves** — Alarm.com force-closes a video session about every three minutes. The bridge rotates ahead of that so viewers see at most a brief freeze, notices dead sessions on its own, and restarts cleanly if one truly wedges — with a guard that keeps an *upstream* outage from turning into a restart loop.
- **Diagnostics as entities** — an Auth Health sensor for login/session state, plus talkdown status and failure-count
  sensors, so a doorbell that quietly stops speaking can trigger a notification instead of going unnoticed.

## Two moving parts

- **[`custom_components/telus_smarthome/`](custom_components/telus_smarthome)** — the Home Assistant integration (a fork of [pyalarmdotcom/alarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom) with the TELUS SSO login patched in). It provides the panel, lock, sensor, camera, and event entities, the `speak` service, and a GUI Reconfigure flow.
- **[`camera-bridge/`](camera-bridge)** — a standalone Docker service that logs in to TELUS, pulls the WebRTC video, and re-serves it as RTSP. It's a separate process because Alarm.com's video platform has no RTSP or ONVIF of its own.

They talk over a loopback-only HTTP endpoint and a shared directory (for credential and secret handoff). The [architecture doc](docs/architecture.md) has the full picture.

## Installing

**Before anything else:** both the Home Assistant container **and** the bridge container must run with **`--network host`**. This isn't a preference — Janus/WebRTC's RTP port range doesn't survive Docker's bridge networking, and the credential-sync endpoint is loopback-only. If your HA runs on the default bridge networking, the camera bridge won't work as-is. `install.sh` checks this first and stops with a clear message if it's not the case.

**1. The integration** — in HACS, add this repo as a custom repository (category *Integration*), install "TELUS Smart Home", then add it from Settings → Devices & Services.

**2. The camera bridge** — on the same host as HA:

```bash
git clone https://github.com/Bungeetaco/telus-smart-home.git
cd telus-smart-home
./install.sh
```

The installer handles the network pre-flight, your TELUS credentials, the shared directory, and building the container. If you'd rather do it by hand, or want every knob explained, the [setup guide](docs/setup.md) and [configuration reference](docs/configuration.md) cover it.

## Two-way audio (talkdown)

Home Assistant can play audio out the doorbell speaker — a spoken warning, a greeting, whatever you like. Call the `telus_smarthome.speak` service with either a `message` (rendered by HA's text-to-speech) or a `media_content_id` (a media-source URI or a direct URL to an audio file):

```yaml
action: telus_smarthome.speak
data:
  message: "Hello, someone will be right there."
  # or target a specific camera; defaults to the doorbell:
  # entity_id: camera.front_door
```

To cut a clip short (a long piece of music, say), call `telus_smarthome.stop_speaking` — it stops within a few tens of milliseconds, and doing it when nothing is playing is harmless.

It's **on by default** — nothing ever speaks on its own, it only plays what you ask it to; set `TALKDOWN_ENABLED=false` on the bridge to disable the endpoint entirely. Long clips are safe: the doorbell's audio session is short-lived and per-session, so if one expires mid-clip the bridge transparently opens a fresh one and picks up exactly where it left off. The audio is band-limited and level-managed for a small doorbell speaker, so music and speech come through cleanly rather than clipping. The mechanism was reverse-engineered against an ADC-VDB750 doorbell; the full protocol and how to turn it on are in [docs/talkdown-fullduplex-protocol.md](docs/talkdown-fullduplex-protocol.md) and [docs/talkdown-DEPLOY.md](docs/talkdown-DEPLOY.md).

## Documentation

The [`docs/`](docs) folder is the deep end — start with its [index](docs/README.md). Highlights:

| Doc | What's in it |
| --- | --- |
| [architecture.md](docs/architecture.md) | How the two components fit together and the two Alarm.com backends they use. Start here. |
| [ha-integration.md](docs/ha-integration.md) | The integration in detail — entities, services, the update model, and the SSO/websocket patches. |
| [camera-bridge.md](docs/camera-bridge.md) | The bridge — the video pipeline, session rotation, hardware encoding, and RTSP auth. |
| [control-api.md](docs/control-api.md) | The bridge's HTTP control API, endpoint by endpoint. |
| [alarm-com-api.md](docs/alarm-com-api.md) | The reverse-engineered Alarm.com website + mobile APIs and the TELUS login handshake. |
| [configuration.md](docs/configuration.md) | Every setting across both components, in one place. |
| [setup.md](docs/setup.md) | Full installation and deployment walkthrough. |
| [troubleshooting.md](docs/troubleshooting.md) | Common problems as symptom → cause → fix. |
| [development.md](docs/development.md) | Repo layout, versioning, and the release process. |

## For AI tools

If you're an AI coding agent working with this repo, read [`AGENTS.md`](AGENTS.md) first — it's the short version of the layout, the build/run steps, the conventions to follow (commit style, keeping the docs anonymized, versioning), and the things that will bite you (rate limits against a live account, the load-bearing session-rotation logic).

## Status and disclaimer

This is a personal, unofficial integration built by reverse-engineering one TELUS account's browser traffic. It is **not affiliated with or endorsed by TELUS or Alarm.com**, and it may break if either changes their portal, login flow, or camera platform. It installs through HACS as a custom repository but isn't in the default store; treat it as a solid starting point rather than a supported package. No license has been set for this repo yet.

**Known limitations:**

- **No MFA in the camera bridge.** Its TELUS login is username/password only. If MFA gets enabled on the account, the bridge's login breaks (the HA integration itself does handle 2FA/OTP). The bridge's [README](camera-bridge/README.md) explains why this one needs a real MFA-challenge capture to implement.
- **Talkdown is device-specific.** It's confirmed on the ADC-VDB750 doorbell; other camera models may need a different audio path (details in the talkdown docs).
- **The siren and privacy mode have never been fired for real.** Both were recovered from the mobile app rather than watched working — the account this was built against exposes neither in its own UI, and reports privacy mode as unsupported. The buttons appear only if your camera says it supports them, but treat the first press as an experiment.
