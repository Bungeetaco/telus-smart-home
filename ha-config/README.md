# ha-config: doorbell intercom package and dashboard view

Installable Home Assistant YAML for the TELUS doorbell, built against one
specific live deployment. Unlike `docs/`, files here name real **entity ids**,
because they are meant to be dropped into that deployment as-is; they contain
no secrets, tokens, IPs or account ids. On any other deployment, every entity
id needs re-checking first.

| File | What it is |
| --- | --- |
| `telus_doorbell.yaml` | A Home Assistant **package**: the actionable-notification doorbell intercom (press + no-press visitor + still-waiting follow-up, three tap-to-speak quick replies), its mute toggle, and the Frigate-fed "person at door" sensor it needs |
| `doorbell-dashboard.yaml` | A Lovelace **view** modelled on the TELUS Android app's doorbell panel: live view, tap-to-speak buttons, chime/audio settings, activity, stream-health diagnostics |
| `camera_no_media.yaml` | A Home Assistant **package**: the camera "no media" watch. Three automations push a camera's entry into the bridge's `no_media` stream state, each device-kick outcome, and the recovery to the phone under one notification tag per camera (see [Installing the no-media watch](#installing-the-no-media-watch)) |

## The one rule everything here honours

**Nothing speaks, deters, or escalates automatically. Sound only ever comes
out of the doorbell because a human tapped a button.** This is an operator
constraint, settled deliberately (talkdown plays real audio into a real home).
Do not add automatic talkdown, deterrent phrases, or auto-escalation to this
package.

## Installing the package

The target deployment does **not** have packages configured (verified: its
`configuration.yaml` has a `homeassistant:` block with no `packages:` key;
automations load via `automation: !include automations.yaml`). Wire it up
once:

1. Create the directory `/config/packages/`.
2. Add one line under the existing `homeassistant:` key in
   `configuration.yaml`:

   ```yaml
   homeassistant:
     packages: !include_dir_named packages
   ```

   Keep every key already under `homeassistant:` (`allowlist_external_dirs`,
   `auth_providers`) exactly as it is; `packages:` sits beside them.
3. Copy `telus_doorbell.yaml` into `/config/packages/`.
4. Run the config check, then restart Home Assistant (a package adds
   `input_boolean` and `template` entries, which a plain automation reload
   does not pick up).

The package's `automation:`, `input_boolean:` and `template:` lists merge
with the existing ones; nothing in the live configuration is overridden
(every id here is namespaced `telus_doorbell_`).

## Installing the no-media watch

`camera_no_media.yaml` needs the packages wiring above and the integration's
Stream state sensors (`sensor.doorbell_stream_state`,
`sensor.living_room_stream_state`) with the device-kick attributes
(`device_kicks`, `last_device_kick`, integration v1.30.1 or later). It
contains only `automation:` entries, so after copying it into
`/config/packages/` the config check plus an **automation reload** is enough;
no restart. The notify target is the same phone service as the doorbell
package.

What it pushes, all under the tag `camera-no-media-<entity id>` so the phone
shows one card per camera that updates rather than stacking: the camera
entering `no_media` (after a minute there, on top of the bridge's own
five-minute threshold); every change of `last_device_kick` (the kick's
outcome, with the stream state at that moment; the attribute reads use
`.get()`, so an integration reload, which empties the sensor's attributes on
its way through `unavailable`, is silent and is not a kick); and the camera **leaving**
`no_media` for `reconnecting` or `streaming`. The recovery trigger lists both
because the sensor passes through `reconnecting` on its way back to
`streaming`, so a `no_media` to `streaming` trigger never fires; `unavailable`
and `unknown` are deliberately not recoveries. The recovery message says which
of the two it was and how many kicks the bridge has sent this run, which is
the evidence backlog E103 collects per occurrence.

## Installing the dashboard view

`doorbell-dashboard.yaml` is one **view**, not a whole dashboard. Add it to
any existing dashboard: open the dashboard, pencil menu, **Raw configuration
editor**, and paste the file's content (from `title:` down) as a new entry in
the `views:` list. The raw editor stores JSON, so the file's comments are for
the reader and will not survive the paste; the file in this directory stays
the commented source of truth.

Stock cards only, with one deliberate exception noted in the file: the
`webrtc-camera` custom card (AlexxIT, v3.6.1) is verified installed on the
target deployment (`/webrtc/webrtc-camera.js` in its Lovelace resources), so
the file carries a commented swap for sub-second live video. The default
stays `picture-glance` so the view also works on a stock install.

## What was verified live, and how

Every entity id below was read out of the running system on 2026-08-25
(`/config/.storage/core.entity_registry` inside the HA container, plus the
live `configuration.yaml`, `automations.yaml`, Frigate's `config.yml` and the
Lovelace resource list). None are guesses.

- `camera.doorbell`, `event.doorbell_button_press` (event type `ring`),
  `event.doorbell_recent_activity`
- `notify.mobile_app_cph2655` (the service the deployment's existing phone
  notifications already use), `tts.home_assistant_cloud`,
  `tts.google_translate_en_com`
- `switch.doorbell_indoor_chime`, `switch.doorbell_outdoor_tone`,
  `select.doorbell_wired_chime_type`, `number.doorbell_speaker_volume`,
  `switch.doorbell_privacy_mode`, `switch.doorbell_status_led`
- `sensor.doorbell_audio_state`, `sensor.doorbell_stream_state`,
  `sensor.doorbell_dropped_frames`, `sensor.doorbell_signal_strength`,
  `sensor.doorbell_camera_status`,
  `binary_sensor.telus_smart_home_integration_camera_bridge`
- Frigate publishes on MQTT topic `frigate/events`; the doorbell camera's
  `front_door` zone has `loitering_time: 5`, so zone credit already means
  five continuous seconds standing at the door
- The `telus_smarthome.speak` and `telus_smarthome.stop_speaking` services
  (see `custom_components/telus_smarthome/services.yaml`)

Limit of the evidence: the `current_zones` key the package's
"person at door" sensor reads on `frigate/events` is standard in Frigate's
event payload but was not captured live during this work (nobody stood at
the door while it was being built); the `entered_zones` test beside it is
the exact test the deployment's existing template sensors use and IS
verified live. `current_zones` is read with a default, so if it were ever
absent the sensor turns off (the follow-up is skipped) rather than erroring.

## Design decisions

**Latency, stated plainly.** `event.doorbell_button_press` is fed by the
integration's polled Activity feed (30 s interval, on top of TELUS's own
feed latency): the press notification lands roughly 30 to 60 s after the
physical press. The no-press visitor path (Frigate zone entry) is
sub-second. That asymmetry drives several choices below.

**Notification snapshots are live `/api/camera_proxy/camera.doorbell`
grabs**, as a relative URL the companion app resolves and authenticates
itself, so no hostname appears in the YAML. Frigate's best-frame snapshot
was considered and rejected: the Frigate HA integration is **not** installed
on this deployment (Frigate talks MQTT only), so the
`/api/frigate/notifications/...` snapshot endpoint does not exist, and
Frigate finalises its best frame only at event end, too late for an instant
notification.

**One shared notification `tag`** (`telus_doorbell`) across the press, the
visitor and the follow-up notifications: the phone shows at most one current
doorbell card, and since the slow press event usually lands after the fast
Frigate one for the same visit, the press replaces the visitor notification
instead of double-buzzing. All three carry the same three quick actions, so
a replacement never costs the tap targets.

**Dedup** in the visitor automation: skip if a press event fired within the
preceding 20 s, exactly as specified. The shared tag covers the (much more
common) reverse ordering.

**"Still there" is derived from `frigate/events` filtered to the doorbell
camera, not from the `frigate/front_door/person` zone-occupancy topic**: the
living-room camera also defines a zone named `front_door` (the interior side
of the same door), and Frigate merges same-named zones across cameras into
one occupancy count, so the topic would read "someone inside the house near
the door" as "someone at the doorstep".

**The follow-up is a notification, not a call.** It is the placeholder for a
future call-my-phone feature and says so in the YAML; when that feature
lands, the follow-up's action is what it replaces.

**Mute toggle** (`input_boolean.telus_doorbell_notifications_muted`):
silences the three phone notifications in this package and nothing else.
Speaking on tap is never gated on it: a tap is an explicit human action.
The toggle deliberately has no `initial:` (it would silently unmute on every
restart). The deployment's schedule-based `binary_sensor.quiet_hours`
(22:30 to 07:00) was left out of these automations on purpose: the operator
asked for a toggle they control, not a schedule, and a doorbell press is
exactly the notification many people still want overnight; flip the toggle
at bedtime if not.

**TTS engine** is pinned to `tts.home_assistant_cloud` (active on this
deployment, better voice) rather than left to the speak service's
first-available default, so the voice at the door does not change if another
TTS integration is ever added. Swap to `tts.google_translate_en_com` if
cloud TTS is dropped.

**`mode:` choices.** Press and visitor notifications are `mode: single` with
`max_exceeded: silent` (one visit, one card; re-triggers during a run are the
same visitor). The follow-up is `single`/`silent` so exactly one follow-up
per visit can ever fire. The speak-on-tap automation is `mode: queued`
(`max: 3`): two deliberate taps are two utterances and both must play, in
order.

## What already exists on the live system, and how this coexists

Read before assuming this package is the only doorbell logic:

- **`telus_activity_clip_notification`** (in the live `automations.yaml`)
  sends a phone notification with the recorded clip when TELUS's Activity
  feed delivers one, Frigate-gated, 32 to 64 s after the event. It is
  complementary, not duplicate: this package's notifications are instant
  (visitor) or press-authoritative, carry live snapshots and quick actions;
  the clip notification carries the recorded evidence. It uses no `tag`, so
  it stacks alongside rather than replacing. If two cards per visit is one
  too many, that automation is the one to reconsider; this package does not
  touch it.
- **The kiosk voice-announcement stack** (template sensors
  `frigate_doorbell_person`, `announce_doorbell_ok`, and friends in the live
  `configuration.yaml`) speaks in the house, not at the door, and is
  untouched. This package's "person at door" sensor is a new, differently
  named entity; it does not replace `frigate_doorbell_person` (30 s
  zone-entry latch) because "entered recently" and "still standing there"
  answer different questions.
- **The kiosk snapshot-strip automations** keep writing
  `/config/www/doorbell_snaps`; this package deliberately writes no files.

## Validation

Both YAML files parse with Python's `yaml.safe_load` (the package as a
document, the view as a document). `hass --script check_config` was **not**
run: it would have to run inside the live container against `/config`, which
means copying these files onto the live system first, and this work was
staged without touching the deployment. Run it as part of the install
(step 4 above) before restarting.
