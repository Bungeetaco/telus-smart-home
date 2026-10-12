# TELUS Smart Home for Home Assistant

**Brings a TELUS Smart Home system (which is really a white-labeled Alarm.com account) into Home Assistant: the alarm panel, locks, sensors, live camera video, and two-way audio through the doorbell.**

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.8.0%2B-41BDF5?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Docker](https://img.shields.io/badge/camera%20bridge-Docker-2496ED?logo=docker&logoColor=white)](camera-bridge)
![Status](https://img.shields.io/badge/status-unofficial%20%26%20reverse--engineered-yellow)

TELUS doesn't publish an API, and their portal logs in through TELUS's own SSO/OAuth gateway instead of Alarm.com's normal login form. That's enough to break the standard `alarmdotcom` integration and every off-the-shelf Alarm.com camera tool against a TELUS account. This repo exists to close that gap. The login flow and the camera signaling were both worked out by reading the network traffic of a real browser session; there was no spec to build from.

> **Heads up:** this is a personal, unofficial project, not affiliated with TELUS or Alarm.com. It can break any time either of them changes their portal. See [Status and disclaimer](#status-and-disclaimer) before you lean on it.

## What you get

- **The whole security panel as HA entities**: arm and disarm, lock and unlock, and read every door, motion, and glass-break sensor natively.
- **Live camera video**: cameras show up as ordinary HA camera entities. Behind the scenes a bridge translates Alarm.com's proprietary WebRTC into plain RTSP that HA (and its bundled go2rtc) can play. There's nothing to wire up by hand; the integration reads the stream URLs, password included, from the bridge. Video only by default; the camera's own **listen** audio can be published on the same stream per camera, opt-in; see `JANUS_REQUEST_AUDIO`/`JANUS_CONSUME_AUDIO` in [docs/configuration.md](docs/configuration.md).
- **Two-way audio ("talkdown")**: speak through the doorbell's speaker from HA: a text-to-speech phrase, or any audio clip you point it at, via a `telus_smarthome.speak` service, and stop it early with `telus_smarthome.stop_speaking`. See [Two-way audio](#two-way-audio-talkdown) below.
- **Ring-to-phone with the doorbell bridged in (opt-in)**: the camera bridge can place a phone call through a VoIP provider (VoIP.ms) and connect it to the doorbell's own microphone and speaker, so a ring you do not answer in the app can reach you as an actual call you can talk through. It is off until you configure it, entirely from Home Assistant's options (the enable toggle, the SIP sub-account and the number to call); nothing is hand-edited in the bridge. See [docs/configuration.md](docs/configuration.md) and the `POST /control/call/*` routes in [docs/control-api.md](docs/control-api.md).
- **The doorbell press, as a real trigger**: pressing the button fires a proper Home Assistant event entity with the `doorbell` device class, so a ring automation triggers on a press instead of scraping the wording out of the activity feed. That distinction matters more than it looks: TELUS renders the activity feed's text in the account's own language, and this project's reference account has produced both French and English descriptions for the same kinds of event, so a text match is an automation that stops working one day with no error anywhere. The entity matches TELUS's own untranslated event code instead. It's created for cameras that report having a chime, attaches to the camera it belongs to, and fires exactly once per press: a restart won't re-ring you for the last person who called. See [Ringing the doorbell as a trigger](#ringing-the-doorbell-as-a-trigger) below.
- **The alarm side, where it was worth having**: a switch per paired phone for whether it may auto-disarm the panel on approach, and, on a system with more than one partition, a picker for which partition that phone opens. The panel's own Bluetooth settings are there too: whether it accepts pairing at all, so a phone that isn't already enrolled can't be added, and how long it stays disarmable once a paired phone is seen: 1, 5, 10, 20 or 30 minutes, which are the only windows TELUS offers. Removing a phone for good is a service (`unpair_arming_device`) rather than a button, because it can't be undone from Home Assistant: re-pairing needs physical access to both the phone and the panel. Alongside them a `bypass_sensors` service for skipping specific zones, also deliberately a service and not a per-sensor toggle, since a bypassed zone stops triggering the alarm until you put it back, and that is not something a stray tap should do.
- **The camera's own settings, as entities**: how loud the doorbell speaks (the same volume talkdown is heard at), privacy mode, and the siren, from Home Assistant instead of the TELUS app. Alongside them, per-camera signal strength, battery and status, so a camera drifting onto bad Wi-Fi becomes a number you can alert on rather than a picture that quietly gets worse, plus, on a camera with the hardware to answer the question, whether a solar panel is attached. Controls only show up for cameras that report supporting them, so a capability your camera lacks leaves no broken button behind. The doorbell's chime is there too: switches for the indoor chime and the outdoor tone, and a picker for the kind of chime the doorbell is wired to (mechanical, digital, or none) with the names coming from TELUS rather than guessed at. There's a switch for the device's status LED as well (the little light on the front) for anyone who'd rather it weren't lit, and switches for TELUS's own push notifications, which are handy if you'd rather your doorbell told you about visitors once rather than twice. There's also a picker for the live video resolution, which is the honest lever on how much bandwidth a camera uses; it changes what the bridge itself receives, not just what the TELUS app shows. Picture and clip settings are reachable through the bridge's control API but do not have entities yet; see [docs/control-api.md](docs/control-api.md).
- **Credentials that stay in sync**: change your TELUS password once, in HA's Reconfigure flow, and both the integration and the separate bridge process pick it up. No more silently-stale bridge login after a password change.
- **Cameras that heal themselves**: Alarm.com force-closes a video session about every three minutes. The bridge rotates ahead of that so viewers see at most a brief freeze, notices dead sessions on its own, and restarts cleanly if one truly wedges, with a guard that keeps an *upstream* outage from turning into a restart loop.
- **Diagnostics as entities**: an Auth Health sensor for login/session state, plus talkdown status and failure-count
  sensors, so a doorbell that quietly stops speaking can trigger a notification instead of going unnoticed. Each camera also gets a stream-health trio: a Stream state sensor for where its video pipeline is (connecting, streaming, reconnecting, or held back by the restart guard), a Dropped frames counter for a stream that is quietly degrading, and an Audio state sensor that says whether sound is actually flowing right now, is switched off, or has stopped dead. That last one exists because a camera whose audio decoder dies keeps every other signal green while publishing pure silence, and "a number stopped going up" is not something Home Assistant can alert on: a state that says *stalled* is. A connectivity sensor says whether the bridge container itself is answering at all, so "the camera feed died", "the sound died" and "the bridge is down" are entities you can alert on rather than log greps.

## Two moving parts

- **[`custom_components/telus_smarthome/`](custom_components/telus_smarthome)**: the Home Assistant integration (a fork of [pyalarmdotcom/alarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom) with the TELUS SSO login patched in). It provides the panel, lock, sensor, camera, and event entities, the `speak` service, and a GUI Reconfigure flow.
- **[`camera-bridge/`](camera-bridge)**: a standalone Docker service that logs in to TELUS, pulls the WebRTC video, and re-serves it as RTSP. It's a separate process because Alarm.com's video platform has no RTSP or ONVIF of its own.

They talk over a loopback-only HTTP endpoint and a shared directory (for credential and secret handoff). The [architecture doc](docs/architecture.md) has the full picture.

## Installing

**Before anything else:** both the Home Assistant container **and** the bridge container must run with **`--network host`**. This isn't a preference: Janus/WebRTC's RTP port range doesn't survive Docker's bridge networking, and the credential-sync endpoint is loopback-only. If your HA runs on the default bridge networking, the camera bridge won't work as-is. `install.sh` checks this first and stops with a clear message if it's not the case.

**1. The integration**: in HACS, add this repo as a custom repository (category *Integration*), install "TELUS Smart Home", then add it from Settings → Devices & Services.

**2. The camera bridge**: on the same host as HA:

```bash
git clone https://github.com/Bungeetaco/telus-smart-home.git
cd telus-smart-home
./install.sh
```

The installer handles the network pre-flight, your TELUS credentials, the shared directory, and building the container. If you'd rather do it by hand, or want every knob explained, the [setup guide](docs/setup.md) and [configuration reference](docs/configuration.md) cover it.

## Ringing the doorbell as a trigger

Each doorbell gets an event entity (`event.<doorbell>_button_press`) that fires
when someone presses the button. Trigger on its state change:

```yaml
automation:
  - alias: Someone is at the door
    triggers:
      - trigger: state
        entity_id: event.doorbell_button_press
        # Not to: an event entity's state is the timestamp of the last
        # event, so every press is a new state. `not_to` filters out the
        # startup transition from unavailable/unknown, which is not a press.
        not_to:
          - unavailable
          - unknown
    actions:
      - action: notify.mobile_app_phone
        data:
          message: Someone is at the front door
```

Check the entity's actual id under **Settings → Devices & services → Entities**;
it is named from the camera's own name, so a doorbell called "Front Door"
produces `event.front_door_button_press`.

The event's attributes carry `event_id`, `timestamp`, `camera_id`,
`device_description` and the untranslated `event_type_name`, plus the clip
fields (`clip_link`, `video_event_id`, `thumbnail_url`, …) when TELUS has already
attached a recording. It often hasn't yet: the clip is linked a poll or two
after the press itself lands, and this entity deliberately does not fire a second
time when that happens, because a doorbell that rings your automation twice for
one visitor is worse than a first notification without a thumbnail. If you want
the clip in the notification, trigger on the account-wide `Activity` event
entity, which does re-fire exactly once when a clip arrives.

A complete worked example lives in [`ha-config/`](ha-config): an installable
package that turns the press event, Frigate person detection and the
`telus_smarthome.speak` service into an actionable-notification intercom
(snapshot notifications with tap-to-speak quick replies), plus a matching
doorbell dashboard view. It is written against one specific deployment's
entity ids; its README says what to re-check before using it on another.

## Two-way audio (talkdown)

Home Assistant can play audio out the doorbell speaker: a spoken warning, a greeting, whatever you like. Call the `telus_smarthome.speak` service with either a `message` (rendered by HA's text-to-speech) or a `media_content_id` (a media-source URI or a direct URL to an audio file):

```yaml
action: telus_smarthome.speak
data:
  message: "Hello, someone will be right there."
  # or target a specific camera; defaults to the doorbell:
  # entity_id: camera.front_door
```

To cut a clip short (a long piece of music, say), call `telus_smarthome.stop_speaking`: it stops within a few tens of milliseconds, and doing it when nothing is playing is harmless.

It's **on by default**: nothing ever speaks on its own, it only plays what you ask it to; set `TALKDOWN_ENABLED=false` on the bridge to disable the endpoint entirely. Long clips are safe: the doorbell's audio session is short-lived and per-session, so if one expires mid-clip the bridge transparently opens a fresh one and picks up exactly where it left off. The audio is band-limited and level-managed for a small doorbell speaker, so music and speech come through cleanly rather than clipping, and that conditioning is yours to control per call: pass `conditioning: false` to play a clip exactly as decoded, or `audio_filter` with an ffmpeg `-af` chain of your own. The doorbell's audio endpoint is HTTPS with a self-signed certificate, so the bridge pins the certificate's fingerprint instead, and by default a mismatch is logged loudly rather than blocked, so a firmware update can't silently leave you with a doorbell that won't speak. The mechanism was reverse-engineered against an ADC-VDB750 doorbell; the full protocol and how to turn it on are in [docs/talkdown-fullduplex-protocol.md](docs/talkdown-fullduplex-protocol.md) and [docs/talkdown-DEPLOY.md](docs/talkdown-DEPLOY.md).

## Documentation

The [`docs/`](docs) folder is the deep end; start with its [index](docs/README.md). Highlights:

| Doc | What's in it |
| --- | --- |
| [architecture.md](docs/architecture.md) | How the two components fit together and the two Alarm.com backends they use. Start here. |
| [ha-integration.md](docs/ha-integration.md) | The integration in detail: entities, services, the update model, and the SSO/websocket patches. |
| [camera-bridge.md](docs/camera-bridge.md) | The bridge: the video pipeline, session rotation, hardware encoding, and RTSP auth. |
| [control-api.md](docs/control-api.md) | The bridge's HTTP control API, endpoint by endpoint. |
| [alarm-com-api.md](docs/alarm-com-api.md) | The reverse-engineered Alarm.com website + mobile APIs and the TELUS login handshake. |
| [configuration.md](docs/configuration.md) | Every setting across both components, in one place. |
| [setup.md](docs/setup.md) | Full installation and deployment walkthrough. |
| [troubleshooting.md](docs/troubleshooting.md) | Common problems as symptom → cause → fix. |
| [development.md](docs/development.md) | Repo layout, versioning, and the release process. |

## For AI tools

If you're an AI coding agent working with this repo, read [`AGENTS.md`](AGENTS.md) first: it's the short version of the layout, the build/run steps, the conventions to follow (commit style, keeping the docs anonymized, versioning), and the things that will bite you (rate limits against a live account, the load-bearing session-rotation logic).

## Status and disclaimer

This is a personal, unofficial integration built by reverse-engineering one TELUS account's browser traffic. It is **not affiliated with or endorsed by TELUS or Alarm.com**, and it may break if either changes their portal, login flow, or camera platform. It installs through HACS as a custom repository but isn't in the default store; treat it as a solid starting point rather than a supported package. No license has been set for this repo yet.

**Known limitations:**

- **No MFA in the camera bridge.** Its TELUS login is username/password only. If MFA gets enabled on the account, the bridge's login breaks (the HA integration itself does handle 2FA/OTP). The bridge's [README](camera-bridge/README.md) explains why this one needs a real MFA-challenge capture to implement.
- **Talkdown is device-specific.** It's confirmed on the ADC-VDB750 doorbell; other camera models may need a different audio path (details in the talkdown docs).
- **Downstream (listen) audio is opt-in and unproven per camera.** Asking for the audio track is known to work (the gateway offers it) but one camera on the account this was built against stopped delivering *video* while an audio m-line was in the offer, which is exactly why both audio knobs name the cameras they apply to instead of being global. Turn it on for one camera at a time and watch that camera's video before turning it on for another. A published audio track is also not by itself proof of sound: once a camera has an audio input the bridge keeps it alive with generated silence, so a camera that stops decoding still produces a valid, entirely silent track. Check `decoded_audio_frames` for that camera in the bridge's status endpoint, and check the *rate* it climbs at: audio arrives in 20 ms frames, so a whole stream decodes about 50 a second. A camera that has decoded *nothing yet* is published video-only until it does, so audio on a quiet camera can take a couple of minutes to appear (and the one publisher rebuild that adds it disconnects readers once). (Two causes of "no usable sound" are known and both are fixed. A defect in the WebRTC library let one malformed audio packet kill a session's decoder thread outright; that is patched and confirmed live. Underneath it, the gateway advertises the audio track as one codec and sends another, so most of the stream was being discarded and the audio arrived in fragments; the bridge now works out which codec is really arriving, once per session, and reports what it chose as `audio_codec` beside a process-wide count of what each decoder read and discarded. That second fix is in the bridge and awaiting a live soak, along with the follow-on it exposed: a camera whose audio is sparse could not finish the codec decision before its publisher was built, so the bridge now builds one without audio and adds it once, later, rather than restarting the camera to try again.) Turning audio on used to cost that camera most of its *video* frames, because the silence the bridge feeds its publisher was delivered in half-second bursts and ffmpeg stops reading the video pipe while it waits for audio; that pacing is fixed and measured in the container, and a live validation soak is still running; when you turn audio on, watch that camera's `dropped_frames` and its new `audio_dropped` and `last_publish_at` fields.
- **The panel-wide Bluetooth settings have never been written by a real browser.** Reading them is captured traffic; the *write* was reconstructed from the per-phone endpoint next to it, because no save to that one was ever observed. The switch and the disarm-window picker work as far as anything here can tell, but treat the first change as an experiment, and if one fails, suspect the reconstruction before the panel. Relatedly, whether turning panel-wide pairing off also stops an already-paired phone from disarming is **not known**: treat the panel switch and the per-phone switches as independent controls.
- **The siren and privacy mode have never been fired for real.** Both were recovered from the mobile app rather than watched working: the account this was built against exposes neither in its own UI, and reports privacy mode as unsupported. The buttons appear only if your camera says it supports them, but treat the first press as an experiment.
