<div align="center">

<img src="custom_components/telus_smarthome_alarmdotcom/brand/icon.png" alt="TELUS Smart Home for Home Assistant" width="160">

### Bring a TELUS Smart Home system — really a white-labeled Alarm.com account — into Home Assistant: the alarm panel, locks, sensors, live camera video, and two-way audio through the doorbell.

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg)](https://github.com/hacs/integration)
[![Home Assistant](https://img.shields.io/badge/Home%20Assistant-2026.8.0%2B-049cdb.svg?logo=home-assistant&logoColor=white)](https://www.home-assistant.io/)
[![Camera bridge](https://img.shields.io/badge/camera%20bridge-Docker-2496ED.svg?logo=docker&logoColor=white)](camera-bridge)
[![Status](https://img.shields.io/badge/status-unofficial%20%26%20reverse--engineered-yellow.svg)](#-status-and-limitations)

</div>

---

> ⚠ **Unofficial.** This is a personal project, not affiliated with, endorsed by, or supported by TELUS or Alarm.com. It can break any time either of them changes their portal, login flow, or camera platform. Use it with your own account, at your own risk. See [Status and limitations](#-status-and-limitations) before you lean on it.

TELUS doesn't publish an API, and their portal logs in through TELUS's own SSO/OAuth gateway instead of Alarm.com's normal login form. That's enough to break the standard `alarmdotcom` integration and every off-the-shelf Alarm.com camera tool against a TELUS account. This repo exists to close that gap. The login flow and the camera signaling were both worked out by reading the network traffic of a real browser session; there was no spec to build from.

## ✨ Features

- 🛡 **The whole security panel as HA entities** — arm and disarm, lock and unlock, and read every door, motion, and glass-break sensor natively.
- 📹 **Live camera video** — cameras appear as ordinary HA camera entities. A bridge translates Alarm.com's proprietary WebRTC into plain RTSP that HA (and its bundled go2rtc) can play; the integration reads the stream URLs, password included, from the bridge. Video by default; a camera's own **listen** audio can be published on the same stream, opt-in and per camera.
- 🗣 **Two-way audio ("talkdown")** — speak through the doorbell's speaker from HA, a text-to-speech phrase or any audio clip, via `telus_smarthome_alarmdotcom.speak`, and stop it early with `telus_smarthome_alarmdotcom.stop_speaking`. See [Two-way audio](#-two-way-audio-talkdown).
- 📞 **Ring-to-phone (opt-in)** — the bridge can place a call through a VoIP provider (VoIP.ms) and connect it to the doorbell's mic and speaker, so a ring you miss in the app reaches you as a real call you can talk through. Off until you configure it, entirely from HA options.
- 🔔 **The doorbell press as a real trigger** — pressing the button fires a proper event entity with the `doorbell` device class, matched to TELUS's untranslated event code rather than scraped activity-feed text (which TELUS renders in the account's own language, so a text match silently breaks). Fires exactly once per press. See [Ringing the doorbell as a trigger](#-ringing-the-doorbell-as-a-trigger).
- 🎛 **Camera settings as entities** — speaker volume, privacy mode, siren, status LED, chime type and indoor/outdoor chime, live-video resolution, and TELUS's own push notifications, all from HA. Controls only appear for cameras that report supporting them.
- 🔑 **The alarm side, where it's worth having** — a per-phone auto-disarm switch, a partition picker on multi-partition systems, the panel's Bluetooth pairing and disarm-window settings, a `bypass_sensors` service, and an `unpair_arming_device` service (a service, not a button, because it can't be undone from HA).
- ♻ **Cameras that heal themselves** — Alarm.com force-closes a video session about every three minutes; the bridge rotates ahead of that, spots dead sessions, and restarts cleanly if one truly wedges, with a guard that keeps an upstream outage from becoming a restart loop.
- 🔄 **Credentials that stay in sync** — change your TELUS password once in HA's Reconfigure flow and both the integration and the separate bridge pick it up. No more silently-stale bridge login.
- 🩺 **Diagnostics as entities** — an Auth Health sensor, talkdown status and failure counts, and a per-camera stream-health trio (stream state, dropped-frames counter, audio state), plus a bridge-connectivity sensor, so "the feed died", "the sound died" and "the bridge is down" are each something you can alert on.

## 🧭 How it works

The integration owns authentication, the panel, and all the entities. Live video needs a helper because Alarm.com's camera platform speaks proprietary WebRTC with no RTSP or ONVIF of its own — so a bridge converts it to RTSP once, and everything downstream just reads RTSP.

```
                     ┌──────────────────────────────┐
                     │   TELUS / Alarm.com Cloud     │
                     │  (SSO login · website + MWS   │
                     │   APIs · WebRTC video)        │
                     └───────────────┬──────────────┘
           auth, panel, sensors,     │   camera: proprietary WebRTC
           locks, events             │
                     ┌───────────────┴──────────────┐
                     ▼                               ▼
         ┌───────────────────────┐      ┌────────────────────────────┐
         │  HA Integration       │ loop │   camera-bridge (Docker)    │
         │  custom_components/   │ back │   WebRTC → RTSP (Janus +     │
         │  telus_smarthome_alarmdotcom      ├─────►│   ffmpeg + MediaMTX)         │
         │  (panel, locks,       │ :8199│   + talkdown + ring-to-call │
         │   sensors, camera,    │      └──────────────┬─────────────┘
         │   events, speak)      │                     │ RTSP
         └───────────┬───────────┘                     ▼
                     │                       ┌────────────────────┐
                     └──────────────────────►│  Home Assistant    │
                        entities + stream    │  (+ go2rtc)        │
                                             └────────────────────┘
```

There are **two pieces** to install:

| Piece | What it does | Required for |
| --- | --- | --- |
| [`custom_components/telus_smarthome_alarmdotcom/`](custom_components/telus_smarthome_alarmdotcom) | Panel, locks, sensors, camera, event entities, the `speak` service, and a GUI Reconfigure flow. A fork of [pyalarmdotcom/alarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom) with TELUS SSO login patched in. | Everything |
| [`camera-bridge/`](camera-bridge) | A standalone Docker service that logs in to TELUS, pulls the WebRTC video, and re-serves it as RTSP, plus a loopback control API for talkdown and ring-to-call. | Live video, audio, talkdown |

They talk over a loopback-only HTTP endpoint and a shared directory for credential and secret handoff. The [architecture doc](docs/architecture.md) has the full picture.

## 📦 Installation

> **Before anything else:** both the Home Assistant container **and** the bridge container must run with **`--network host`**. This isn't a preference — Janus/WebRTC's RTP port range doesn't survive Docker's bridge networking, and the credential-sync endpoint is loopback-only. `install.sh` checks this first and stops with a clear message if it's not the case.

### 1 — Integration (via HACS)

1. In Home Assistant: **HACS → ⋮ (top right) → Custom repositories**.
2. Add this repository URL, category **Integration**, and click **Add**.
3. Find **TELUS Smart Home** in HACS and click **Download**.
4. **Restart Home Assistant.**
5. **Settings → Devices & Services → Add Integration → TELUS Smart Home**, and sign in.

<details>
<summary>Or install manually</summary>

Copy `custom_components/telus_smarthome_alarmdotcom/` into your HA `config/custom_components/` folder and restart.
</details>

### 2 — Camera bridge (for video, audio, and talkdown)

On the same host as Home Assistant:

```bash
git clone https://github.com/Bungeetaco/telus-smart-home.git
cd telus-smart-home
./install.sh
```

The installer handles the network pre-flight, your TELUS credentials, the shared directory, and building the container. To do it by hand or see every knob explained, the [setup guide](docs/setup.md) and [configuration reference](docs/configuration.md) cover it.

## 🔔 Ringing the doorbell as a trigger

Each doorbell gets an event entity (`event.<doorbell>_button_press`) that fires when someone presses the button. Trigger on it with `event.received`:

```yaml
automation:
  - alias: Someone is at the door
    triggers:
      - trigger: event.received
        target:
          entity_id: event.doorbell_button_press
        options:
          event_type:
            - ring
    actions:
      - action: notify.mobile_app_phone
        data:
          message: Someone is at the front door
```

Not a `state` trigger on the entity: an event entity restores its last event when Home Assistant starts, so its state changes from `unavailable` to the previous press's timestamp, and a state trigger takes that for a new press (a `not_to` filter cannot tell them apart, because the new state is a timestamp). `event.received` fires only for a new event.

Check the entity's actual id under **Settings → Devices & services → Entities**; it is named from the camera's own name, so a doorbell called "Front Door" produces `event.front_door_button_press`.

The event's attributes carry `event_id`, `timestamp`, `camera_id`, `device_description` and the untranslated `event_type_name`, plus the clip fields (`clip_link`, `video_event_id`, `thumbnail_url`, …) when TELUS has already attached a recording. It often hasn't yet: the clip is linked a poll or two after the press itself lands, and this entity deliberately does not fire a second time when that happens, because a doorbell that rings your automation twice for one visitor is worse than a first notification without a thumbnail. If you want the clip in the notification, trigger on the account-wide `Activity` event entity, which does re-fire exactly once when a clip arrives.

A complete worked example lives in [`ha-config/`](ha-config): an installable package that turns the press event, Frigate person detection and the `telus_smarthome_alarmdotcom.speak` service into an actionable-notification intercom (snapshot notifications with tap-to-speak quick replies), plus a matching doorbell dashboard view. It is written against one specific deployment's entity ids; its README says what to re-check before using it on another.

## 🗣 Two-way audio (talkdown)

Home Assistant can play audio out the doorbell speaker: a spoken warning, a greeting, whatever you like. Call the `telus_smarthome_alarmdotcom.speak` service with either a `message` (rendered by HA's text-to-speech) or a `media_content_id` (a media-source URI or a direct URL to an audio file):

```yaml
action: telus_smarthome_alarmdotcom.speak
data:
  message: "Hello, someone will be right there."
  # or target a specific camera; defaults to the doorbell:
  # entity_id: camera.front_door
```

To cut a clip short (a long piece of music, say), call `telus_smarthome_alarmdotcom.stop_speaking`: it stops within a few tens of milliseconds, and doing it when nothing is playing is harmless.

It's **on by default**: nothing ever speaks on its own, it only plays what you ask it to; set `TALKDOWN_ENABLED=false` on the bridge to disable the endpoint entirely. Long clips are safe: the doorbell's audio session is short-lived and per-session, so if one expires mid-clip the bridge transparently opens a fresh one and picks up exactly where it left off. The audio is band-limited and level-managed for a small doorbell speaker, so music and speech come through cleanly rather than clipping, and that conditioning is yours to control per call: pass `conditioning: false` to play a clip exactly as decoded, or `audio_filter` with an ffmpeg `-af` chain of your own. The doorbell's audio endpoint is HTTPS with a self-signed certificate, so the bridge pins the certificate's fingerprint instead, and by default a mismatch is logged loudly rather than blocked, so a firmware update can't silently leave you with a doorbell that won't speak. The mechanism was reverse-engineered against an ADC-VDB750 doorbell; the full protocol and how to turn it on are in [docs/talkdown-fullduplex-protocol.md](docs/talkdown-fullduplex-protocol.md) and [docs/talkdown-DEPLOY.md](docs/talkdown-DEPLOY.md).

## 🗺 Roadmap

Every known gap, however minor, is tracked in [docs/backlog.md](docs/backlog.md) with stable `E#` ids and their evidence; this is the short, user-facing view.

- [x] **Two-way audio (talkdown)** through the doorbell speaker.
- [x] **The doorbell press as a native event entity**, matched on the untranslated event code.
- [x] **Self-healing camera sessions** across Alarm.com's ~3-minute forced rotation.
- [x] **Credential sync** between the integration and the bridge via the Reconfigure flow.
- [ ] **Downstream (listen) audio** finished and default-safe per camera — the codec and pacing fixes are in and awaiting a longer live soak.
- [ ] **Picture and clip settings as entities** — reachable through the bridge's control API today, no entities yet.
- [ ] **MFA in the camera bridge** — needs a real MFA-challenge capture; the integration already handles 2FA/OTP.

## 🩺 Troubleshooting

Full symptom → cause → fix coverage is in [docs/troubleshooting.md](docs/troubleshooting.md). The common ones:

- **Entities go *Unavailable*, or HA keeps retrying into a rate limit** — Alarm.com rate-limits a live account (HTTP 429); the coordinators hold off on their own. Don't shorten the poll interval. See *Alarm.com rate-limiting* in the doc.
- **A doorbell notification fires on every HA restart** — you're using a `state` trigger; switch to `event.received` as shown above.
- **Camera is black, or the stream freezes / goes dark** — the bridge isn't publishing. Check `docker compose logs -f` and `curl -s http://127.0.0.1:8199/control/health`.
- **Talkdown returns 403/409/422, or sounds bad on the speaker** — covered in the doc under the matching headings; a certificate-fingerprint mismatch is logged, not silently blocked.
- **The push websocket drops every few minutes** — expected; the integration reconnects with back-off. Only worry if it never comes back.

## 📚 Documentation

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

## 🤖 For AI tools

If you're an AI coding agent working with this repo, read [`AGENTS.md`](AGENTS.md) first: it's the short version of the layout, the build/run steps, the conventions to follow (commit style, keeping the docs anonymized, versioning), and the things that will bite you (rate limits against a live account, the load-bearing session-rotation logic).

## 🎨 Branding

The icon ships **with the integration**, in [`custom_components/telus_smarthome_alarmdotcom/brand/`](custom_components/telus_smarthome_alarmdotcom/brand/):

```
custom_components/telus_smarthome_alarmdotcom/brand/
├── icon.png      # 256×256
└── icon@2x.png   # 512×512
```

Since **Home Assistant 2026.3**, custom integrations serve their own brand images locally from this `brand/` folder — they override the brands CDN, so the icon shows on the device/integration card with no submission to home-assistant/brands and no extra config. Supported filenames also include `logo.png`, `dark_icon.png`, `dark_logo.png`, and the `@2x` variants.

## ⚠ Status and limitations

This is a personal, unofficial integration built by reverse-engineering one TELUS account's browser traffic. It is **not affiliated with or endorsed by TELUS or Alarm.com**, and it may break if either changes their portal, login flow, or camera platform. It installs through HACS as a custom repository but isn't in the default store; treat it as a solid starting point rather than a supported package.

**Known limitations:**

- **No MFA in the camera bridge.** Its TELUS login is username/password only. If MFA gets enabled on the account, the bridge's login breaks (the HA integration itself does handle 2FA/OTP). The bridge's [README](camera-bridge/README.md) explains why this one needs a real MFA-challenge capture to implement.
- **Talkdown is device-specific.** It's confirmed on the ADC-VDB750 doorbell; other camera models may need a different audio path (details in the talkdown docs).
- **Downstream (listen) audio is opt-in and unproven per camera.** Asking for the audio track is known to work (the gateway offers it) but one camera on the account this was built against stopped delivering *video* while an audio m-line was in the offer, which is exactly why both audio knobs name the cameras they apply to instead of being global. Turn it on for one camera at a time and watch that camera's video before turning it on for another. A published audio track is also not by itself proof of sound: once a camera has an audio input the bridge keeps it alive with generated silence, so a camera that stops decoding still produces a valid, entirely silent track. Check `decoded_audio_frames` for that camera in the bridge's status endpoint, and check the *rate* it climbs at: audio arrives in 20 ms frames, so a whole stream decodes about 50 a second. A camera that has decoded *nothing yet* is published video-only until it does, so audio on a quiet camera can take a couple of minutes to appear (and the one publisher rebuild that adds it disconnects readers once). (Two causes of "no usable sound" are known and both are fixed. A defect in the WebRTC library let one malformed audio packet kill a session's decoder thread outright; that is patched and confirmed live. Underneath it, the gateway advertises the audio track as one codec and sends another, so most of the stream was being discarded and the audio arrived in fragments; the bridge now works out which codec is really arriving, once per session, and reports what it chose as `audio_codec` beside a process-wide count of what each decoder read and discarded. That second fix is in the bridge and awaiting a live soak, along with the follow-on it exposed: a camera whose audio is sparse could not finish the codec decision before its publisher was built, so the bridge now builds one without audio and adds it once, later, rather than restarting the camera to try again.) Turning audio on used to cost that camera most of its *video* frames, because the silence the bridge feeds its publisher was delivered in half-second bursts and ffmpeg stops reading the video pipe while it waits for audio; that pacing is fixed and measured in the container, and a live validation soak is still running; when you turn audio on, watch that camera's `dropped_frames` and its new `audio_dropped` and `last_publish_at` fields.
- **The panel-wide Bluetooth settings have never been written by a real browser.** Reading them is captured traffic; the *write* was reconstructed from the per-phone endpoint next to it, because no save to that one was ever observed. The switch and the disarm-window picker work as far as anything here can tell, but treat the first change as an experiment, and if one fails, suspect the reconstruction before the panel. Relatedly, whether turning panel-wide pairing off also stops an already-paired phone from disarming is **not known**: treat the panel switch and the per-phone switches as independent controls.
- **The siren and privacy mode have never been fired for real.** Both were recovered from the mobile app rather than watched working: the account this was built against exposes neither in its own UI, and reports privacy mode as unsupported. The buttons appear only if your camera says it supports them, but treat the first press as an experiment.

## 🙏 Acknowledgements

Built on [pyalarmdotcom/alarmdotcom](https://github.com/pyalarmdotcom/alarmdotcom), with the TELUS SSO login and camera path worked out by reverse-engineering a real browser and app session. TELUS Smart Home is a white-label Alarm.com deployment; your mileage may vary by region, plan, and firmware.

## 📄 License

No license has been set for this repo yet. Until one is added, no general grant of rights is implied; treat it as source-available for personal reference.
