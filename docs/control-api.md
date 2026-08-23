# Control API Reference

The camera bridge runs a small loopback-only HTTP control server so that the
companion Home Assistant integration can poll live state, drive on-demand
actions (snapshots, talkdown, clip re-minting), and push refreshed TELUS
credentials into the already-running bridge process without a container
restart. This document is the complete reference for that API, derived from
`camera-bridge/bridge/control_server.py`.

## Binding and reachability

- **Host:** `127.0.0.1` (`config.CONTROL_HOST`) — loopback only. The server is
  never bound to a routable interface and never given a Docker port-publish
  mapping.
- **Port:** `8199` by default (`config.CONTROL_PORT`, overridable via the
  `CONTROL_PORT` environment variable).
- The Home Assistant side reaches it because both containers run with
  `--network host`, so `127.0.0.1:8199` on the host is the same socket for both.

All example URLs below use `http://127.0.0.1:8199` as the placeholder base URL.

## Authentication

Every endpoint except `/control/health` requires a bearer token:

```
Authorization: Bearer <control-secret>
```

The secret is a random 64-hex-character value generated on first start and
persisted to `config.CONTROL_SECRET_PATH` (`/shared/control_secret` inside the
bridge container; the HA side reads it from its own view of the shared mount,
`/share/telus_cam_bridge/control_secret`). Key properties:

- Generated with `secrets.token_hex(32)` and written atomically with mode
  `0600`.
- **Rotated every 30 days** (`config.CONTROL_SECRET_ROTATE_INTERVAL_S`). The
  new value is rewritten atomically to the same file. No pairing or grace
  period is needed because the HA client reads the secret fresh off disk on
  every request (see [bridge_client](#home-assistant-client)); the next poll
  after a rotation simply picks up the new value.
- Comparison is constant-time (`secrets.compare_digest`). A header containing a
  non-ASCII byte is treated as a failed auth (`401`), not a server error.

A request with a missing or wrong `Authorization` header on any authed route
returns:

```json
401 {"error": "bad_secret"}
```

The `<control-secret>` placeholder is used throughout the curl examples; export
it once for convenience:

```bash
SECRET=$(cat /share/telus_cam_bridge/control_secret)
BASE=http://127.0.0.1:8199
```

## Endpoint summary

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| GET | `/control/health` | none | Process liveness probe |
| GET | `/control/status` | bearer | Live per-camera pipeline diagnostics |
| GET | `/control/cameras` | bearer | In-memory camera registry (metadata) |
| GET | `/control/cameras/{camera_id}/snapshot` | bearer | Fresh on-demand JPEG still |
| GET | `/control/metrics` | bearer | Talkdown counters: outcomes, failure reasons, durations |
| POST | `/control/cameras/{camera_id}/talk` | bearer | Play audio through the camera speaker (talkdown) |
| POST | `/control/cameras/{camera_id}/talk/stop` | bearer | Stop the talk currently playing on that camera |
| GET | `/control/cameras/{camera_id}/settings` | bearer | All audio / image / video settings for one camera |
| GET | `/control/cameras/{camera_id}/diagnostics` | bearer | Camera signal strength, battery, device status |
| POST | `/control/cameras/{camera_id}/settings/volume` | bearer | Set the camera speaker (call) volume |
| POST | `/control/cameras/{camera_id}/settings/chime` | bearer | Set the indoor chime, wired chime type, outdoor tone and its volume |
| POST | `/control/cameras/{camera_id}/settings/image` | bearer | Set picture settings (brightness, HDR, IR, night vision, …) |
| POST | `/control/cameras/{camera_id}/settings/video` | bearer | Set clip settings (resolution, quality, retention, pre-trigger) |
| POST | `/control/cameras/{camera_id}/siren` | bearer | Start or stop the camera siren |
| POST | `/control/cameras/{camera_id}/privacy` | bearer | Turn camera privacy mode on or off |
| GET | `/control/events` | bearer | Recent camera video-clip events |
| GET | `/control/history` | bearer | System-wide activity feed |
| GET | `/control/clip/{video_event_id}` | bearer | Re-mint a fresh signed clip download URL |
| GET | `/control/automations` | bearer | List video-related automation rules |
| POST | `/control/automations/{rule_id}/pause` | bearer | Pause a rule |
| POST | `/control/automations/{rule_id}/resume` | bearer | Resume a rule |
| POST | `/control/cameras/by-path/{path_name}/activate` | bearer | Manually (re)start one camera pipeline |
| POST | `/control/cameras/by-path/{path_name}/deactivate` | bearer | Manually stop one camera pipeline |
| POST | `/control/credentials` | bearer | Push updated TELUS credentials into the process |

Shared error responses across the authed routes:

| Status | Body | Meaning |
| --- | --- | --- |
| `401` | `{"error": "bad_secret"}` | Missing/wrong `Authorization` header |
| `502` | `{"error": "telus_error"}` | The upstream TELUS/alarm.com call failed |

---

## `GET /control/health`

Process liveness probe. **No authentication** — it exposes no credential
material, only that the process is up and serving.

**Response**

```json
200 {"status": "ok"}
```

**Example**

```bash
curl -s $BASE/control/health
```

---

## `GET /control/status`

Live per-camera diagnostics maintained by the camera session loop
(`camera_session.py`'s `run_camera_forever`, threaded a shared status dict by
`main.py`). Useful for spotting reconnect churn or a stalled pipeline without
grepping container logs.

**Response**

```json
200 {
  "cameras": {
    "<camera_id>": {
      "state": "streaming",
      "consecutive_failures": 0,
      "last_frame_at": 1723406400,
      "fps": 15.0,
      "hardware_encode": true,
      "last_error": null
    }
  }
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `state` | string | One of `streaming`, `connecting`, `reconnecting`, `restart_deferred` |
| `consecutive_failures` | int | Reconnect attempts since the last good frame |
| `last_frame_at` | int \| null | Unix timestamp of the most recent frame, or `null` |
| `fps` | number | Current frame rate |
| `hardware_encode` | bool | Whether the pipeline is using hardware encode |
| `last_error` | string \| null | Most recent error string, or `null` |

`state: "restart_deferred"` is the most informative value: the camera hit the
`MAX_CONSECUTIVE_SESSION_FAILURES` escape hatch, but it has fired too often
lately to be a local wedge, so `restart_guard.py` is holding the process before
it exits.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" $BASE/control/status
```

---

## `GET /control/metrics`

Counters for the talkdown path. A failed talk otherwise leaves no trace beyond
one HTTP error to whoever asked, which makes "has this been failing all week?" a
log-grep question; these answer it directly.

```bash
curl -s -H "Authorization: Bearer $SECRET" $BASE/control/metrics
```

```json
{
  "talk": {
    "uptime_seconds": 52.5,
    "attempts": 3,
    "succeeded": 1,
    "stopped": 1,
    "failed": 1,
    "rejected_in_progress": 1,
    "failures_by_reason": {"media_error": 1},
    "by_endpoint": {"direct": 2},
    "by_codec": {"mulaw": 2},
    "renegotiations": 0,
    "audio_seconds_played": 8.3,
    "talk_seconds": 7.9,
    "last_failure_reason": "media_error",
    "recent": [{"at": 1787500402.7, "camera_id": "<unit-id>-<did>",
                "outcome": "ok", "wall_seconds": 4.89, "played_seconds": 5.0}]
  }
}
```

Notes:

- **In-memory only.** They reset when the bridge restarts, which is what keeps
  them free; the log remains the durable record.
- `attempts` counts talks that actually started. A request refused because one was
  already running is counted separately as `rejected_in_progress`, not as an
  attempt.
- `audio_seconds_played` is audio actually pushed to a speaker, so a clip stopped
  early contributes what it played, not its full length.
- `failures_by_reason` uses stable labels (`media_error`, `media_too_large`,
  `no_upstream_audio`, `timeout`, `transport_error`, `network_error`,
  `upstream_error`, `cancelled`) — never exception text, which could carry a URL
  or token.
- `recent` is the last 20 talks, oldest first.
- `GET /control/status` carries a short summary of the same data alongside the
  camera states.


## `GET /control/cameras`

Returns the bridge's in-memory camera registry, populated at startup and kept
current by the periodic discovery poll (`discover_and_launch_cameras()`, every
`config.CAMERA_DISCOVERY_INTERVAL_S` — 300 s by default). A camera added to the
TELUS account appears here within one poll interval, no restart needed.

**Response**

```json
200 {
  "cameras": [
    {
      "id": "<camera_id>",
      "name": "Front Door",
      "model": "VDB750",
      "firmware": "<firmware>",
      "mac_address": "<mac>",
      "rtsp_url": "rtsp://<host>:<port>/<path>"
    }
  ]
}
```

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" $BASE/control/cameras
```

---

## `GET /control/cameras/{camera_id}/snapshot`

Fetches a fresh on-demand JPEG still for one camera. Not cached: TELUS snapshot
URLs are signed and short-lived, so every call does a real round trip to TELUS
(unlike `/control/cameras`, which is served from memory). Includes the
one-shot re-login-and-retry behavior on an expired session cookie.

**Response**

- `200`, `Content-Type: image/jpeg`, raw JPEG bytes on success.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Errors**

| Status | Body | Meaning |
| --- | --- | --- |
| `404` | `{"error": "unknown_camera"}` | `camera_id` not in the registry |
| `502` | `{"error": "telus_error"}` | The TELUS-side fetch failed |

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/snapshot -o snapshot.jpg
```

---

## `POST /control/cameras/{camera_id}/talk`

Plays a short audio clip (typically an HA-rendered TTS phrase) through the
camera's speaker via two-way audio (talkdown). **Off by default:** the route
is enabled by default and returns `403` only when `TALKDOWN_ENABLED` is set false,
so the wiring ships inert.

Only one talk may be in flight per camera at a time. A concurrent trigger for
the same camera is rejected with `409` — never queued. See `talk.py` /
`talk_fullduplex.py` for the transport details.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `audio_url` | string | one of `audio_url`/`audio_path` | An `http://` or `https://` URL the bridge fetches. Wins if both are given. |
| `audio_path` | string | one of `audio_url`/`audio_path` | A filesystem path readable by the bridge. |
| `codec` | string | no | Transport codec, case-insensitive. Only `mulaw` (mu-law) is currently defined; omit to use the default. An unknown value returns `400 {"error": "bad_codec"}`. |
| `conditioning` | bool | no | `true`/absent = apply the default audio filter chain; `false` = disable conditioning entirely. Must be a boolean if present. |
| `audio_filter` | string | no | Explicit ffmpeg `-af` filter string that overrides both the default chain and `conditioning`. An empty string `""` disables conditioning. Must be a string if present. |

**Conditioning resolution.** Audio conditioning is on by default
(`config.TALK_AUDIO_FILTER`, a highpass/lowpass + compressor + limiter chain
tuned for speech). The three inputs resolve as follows:

1. If `audio_filter` is a string, it is used verbatim (an empty string disables filtering).
2. Otherwise, if `conditioning` is `false`, filtering is disabled.
3. Otherwise the configured default chain is applied.

**Success response**

```json
200 {"status": "spoken", "path": "webrtc"}
```

`path` reports which upstream-audio transport carried the clip — `"webrtc"` or
`"srtp"` (additional keys from the talk manager's result are merged into the
response).

**Errors**

| Status | Body | Meaning |
| --- | --- | --- |
| `400` | `{"error": "malformed"}` | Bad JSON, neither audio field, a non-`http(s)` `audio_url`, a bad-type `conditioning`/`audio_filter`, or unreadable/invalid audio |
| `400` | `{"error": "bad_codec"}` | `codec` not in the supported set |
| `401` | `{"error": "bad_secret"}` | Auth failure |
| `403` | `{"error": "talkdown_disabled"}` | `TALKDOWN_ENABLED` was set false (or the talk manager was not constructed) |
| `404` | `{"error": "unknown_camera"}` | `camera_id` not in the registry |
| `409` | `{"error": "talk_in_progress"}` | A talk is already in flight for this camera |
| `413` | `{"error": "media_too_large"}` | Fetched/read audio exceeded the size cap |
| `422` | `{"error": "no_upstream_audio"}` | Camera exposes no webrtc/srtp upstream-audio endpoint |
| `502` | `{"error": "telus_error"}` | MWS auth / stream lookup / signaling failed |
| `504` | `{"error": "talk_timeout"}` | Session exceeded `config.TALK_SESSION_TIMEOUT_S` (60 s) |

**Examples**

Play a TTS clip fetched from a URL, with default conditioning:

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"audio_url": "http://192.0.2.10:8123/local/tts/hello.wav"}' \
  $BASE/control/cameras/<camera_id>/talk
```

Play a local file with conditioning disabled and an explicit codec:

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"audio_path": "/shared/clips/chime.wav", "codec": "mulaw", "conditioning": false}' \
  $BASE/control/cameras/<camera_id>/talk
```

Override the filter chain:

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"audio_url": "http://192.0.2.10/clip.wav", "audio_filter": "highpass=f=200"}' \
  $BASE/control/cameras/<camera_id>/talk
```

---

## `POST /control/cameras/{camera_id}/talk/stop`

Stops the clip currently playing through that camera's speaker. Empty body.

The stream ends at the next chunk boundary — at most one chunk of audio (40 ms) —
and the still-open `POST .../talk` call returns normally with partial stats and
`"stopped": true`, rather than erroring. Stopping is a signal, not a task
cancellation, so the session teardown (`EndLiveStream`) still runs on its usual
path.

```bash
curl -s -X POST \
  -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/talk/stop
```

**Response**

| Status | Body | Meaning |
| --- | --- | --- |
| `200` | `{"status": "stopping"}` | A talk was in flight and has been asked to stop |
| `403` | `{"error": "talkdown_disabled"}` | `TALKDOWN_ENABLED` was set false |
| `404` | `{"error": "unknown_camera"}` | No such camera |
| `404` | `{"error": "no_talk_in_progress"}` | Nothing is playing on that camera |

A `404 no_talk_in_progress` is informational, not a failure: callers may fire this
unconditionally (e.g. on a door-open event) without first checking whether a clip
is playing. The Home Assistant service `telus_smarthome.stop_speaking` wraps this
and treats that case as a no-op.


---

## Camera settings, diagnostics, siren and privacy

Eight routes cover the per-camera **settings** surface (speaker volume, the
doorbell's own chime, image, video/clip), read-only **diagnostics** (signal,
battery, device status), the camera **siren**, and **privacy mode**.

They are independent of talkdown: `TALKDOWN_ENABLED` does not gate them and they
never open an audio session. Settings and diagnostics come from the **website
API** (the cookie + `ajaxrequestuniquekey` session the bridge already holds);
siren and privacy come from the **mobile MWS API** (form POST in, JSON envelope
back — same `isSuccess` / `ErrorMessage` fields as the XML actions). See
[alarm-com-api.md](alarm-com-api.md) for the two backends.

> **These write to real hardware.** A `GET` here never changes anything, but a
> `POST` takes effect on a camera in someone's home immediately — and the siren
> is audible. Call the write routes only when a user asked for that write; never
> as a side effect of a read or a poll.

Two properties of the upstream API shape every route in this group:

- **Read-modify-write, always.** A website settings `PUT` carries the **whole**
  settings object, not a patch, so a partial body risks resetting fields the
  caller never mentioned. Each `POST` below therefore re-reads the current
  object, changes only the named field(s), and sends it all back. That is two
  upstream round trips per write, which is one reason these routes are slow-poll
  only and are never called from the video path.
- **Feature-support enums.** Every settings field is paired with a
  `<name>FeatureSupport` enum (`0` = unsupported, `2` = supported/editable), and
  the *same payload shape* comes back for a device that cannot do the thing — so
  the presence of a field proves nothing. Support is reported upward as a
  `supports_*` boolean on the read routes, and writing an unsupported field is a
  **`422 unsupported`**, not a silently-dropped write.

Errors shared by all eight (individual routes note anything extra):

| Status | Body | Meaning |
| --- | --- | --- |
| `400` | `{"error": "malformed"}` | Body is not valid JSON, carries an unknown key, a value of the wrong type, a number outside the range the device publishes, or a value that is not on the option list the device offers |
| `401` | `{"error": "bad_secret"}` | Missing/wrong `Authorization` header |
| `404` | `{"error": "unknown_camera"}` | `camera_id` not in the camera registry |
| `422` | `{"error": "unsupported"}` | The device reports no support for that field or capability |
| `502` | `{"error": "telus_error"}` | The upstream TELUS/alarm.com call failed |
| `503` | `{"error": "mws_unavailable"}` | Siren and privacy only — no mobile MWS session exists to act through |

**Out-of-range values are refused, never clamped.** Every numeric field on every
write route is checked against the `*_min` / `*_max` the device itself published
on the read, and every string field against the `*_options` list it published;
anything outside is a **`400 malformed`** and nothing is sent to the camera.

This is a deliberate choice, not an accident of validation. Clamping would mean
writing a *different* value to a camera in someone's home than the caller asked
for and still answering `200` — the caller believes the volume it named is the
volume that landed, and nothing tells it otherwise. Refusing puts the
disagreement in front of whoever caused it. A device that publishes no usable
bounds or no option list is not second-guessed: the value is passed through and
TELUS is the judge.

Note which status this is. **Out of range is a `400`, not a `422`** — the caller
sent a wrong value, which is a caller mistake and worth fixing at the call site.
`422 unsupported` means something else entirely: the device reports it cannot do
that field *at all* (its `*FeatureSupport` enum is not `2`), which no retry with
a different value will fix. Both leave the camera untouched.

---

## `GET /control/cameras/{camera_id}/settings`

Reads one camera's audio, image and video/clip settings, plus its siren and
privacy records — five keys. Uncached: a settings value a UI renders as a slider
must not be stale. Each call is **three website GETs and two mobile MWS POSTs**,
which makes it the heaviest read here; the Home Assistant side polls it every
120 s, and you should not poll it tighter.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Response**

```json
200 {
  "audio": {
    "supports_call_volume": true,
    "call_volume": 10,
    "call_volume_min": 0,
    "call_volume_max": 10,
    "mic_and_speaker_on": true,
    "supports_indoor_chime": true,
    "indoor_chime_on": true,
    "supports_chime_type": true,
    "chime_type": 0,
    "chime_type_options": [
      -1,
      0,
      1
    ],
    "chime_type_choices": [
      {"value": -1, "label": "None", "disables_indoor_chime": true},
      {"value": 0, "label": "Mechanical", "disables_indoor_chime": false},
      {"value": 1, "label": "Digital", "disables_indoor_chime": false}
    ],
    "supports_outdoor_tone": true,
    "outdoor_tone_on": true,
    "supports_outdoor_tone_volume": false,
    "outdoor_tone_volume": 0,
    "outdoor_tone_volume_min": 0,
    "outdoor_tone_volume_max": 3,
    "raw": {}
  },
  "image": {
    "supports_brightness": true,
    "brightness": 2,
    "brightness_min": -5,
    "brightness_max": 5,
    "supports_contrast": true,
    "contrast": 0,
    "contrast_min": -5,
    "contrast_max": 5,
    "supports_saturation": true,
    "saturation": 2,
    "saturation_min": -5,
    "saturation_max": 5,
    "supports_sharpness": false,
    "sharpness": 0,
    "sharpness_min": -3,
    "sharpness_max": 3,
    "supports_is_flipped": true,
    "is_flipped": false,
    "supports_hdr_enabled": true,
    "hdr_enabled": true,
    "supports_max_exposure": false,
    "max_exposure": 1,
    "max_exposure_options": [
      1,
      8,
      11,
      16,
      32,
      63,
      125,
      250,
      500,
      1000,
      2000,
      4000,
      8000
    ],
    "supports_ir_led_enabled": true,
    "ir_led_enabled": true,
    "supports_ir_sensitivity": true,
    "ir_sensitivity": 1,
    "supports_night_vision_enabled": false,
    "night_vision_enabled": false,
    "raw": {}
  },
  "video": {
    "supports_clip_resolution": true,
    "clip_resolution": "1440x1440",
    "clip_resolution_options": [
      "1",
      "4",
      "7",
      "480x480",
      "960x960",
      "1440x1440"
    ],
    "supports_clip_quality": false,
    "clip_quality": "3",
    "clip_quality_options": [
      "5",
      "3",
      "2",
      "1"
    ],
    "supports_keep_clips_days": true,
    "keep_clips_days": -1,
    "keep_clips_days_options": [
      3,
      5,
      7,
      10,
      15,
      30,
      60,
      120,
      180,
      -1
    ],
    "supports_alarm_clip_duration": true,
    "alarm_clip_duration": 2,
    "alarm_clip_duration_options": [
      1,
      2,
      5,
      10,
      15
    ],
    "supports_camera_triggered_clip_duration": true,
    "camera_triggered_clip_duration": 30,
    "camera_triggered_clip_duration_options": [
      "30",
      "30 seconds"
    ],
    "supports_pretrigger_enabled": true,
    "pretrigger_enabled": true,
    "raw": {}
  },
  "siren": {
    "can_manually_trigger": true,
    "active": false,
    "timestamp_end": null
  },
  "privacy": {
    "supported": false,
    "active": false
  }
}
```

**The `audio` object** (the shape `camera_settings.get_audio_settings()` returns,
and what the volume and chime writes return too):

| Field | Type | Notes |
| --- | --- | --- |
| `supports_call_volume` | bool | Derived from `callVolumeFeatureSupport` (`2` → true) |
| `call_volume` | int | Current speaker/call volume — **this is the volume talkdown plays at** |
| `call_volume_min` / `call_volume_max` | int | Device-reported range; `0`–`10` on the doorbell this was built against |
| `mic_and_speaker_on` | bool | The device's mic + speaker master toggle |
| `supports_indoor_chime` | bool | Whether the indoor chime can be turned on/off |
| `indoor_chime_on` | bool | Indoor chime state |
| `supports_chime_type` | bool | Whether the **wired chime type** is selectable — its own upstream enum (`indoorChimeTypeFeatureSupport`), not the indoor chime's on/off flag |
| `chime_type` | int \| null | Selected wired chime type. **Not a ringtone** — it is the kind of physical chime the doorbell is wired to |
| `chime_type_options` | list | The bare integer values this device offers, e.g. `[-1, 0, 1]` |
| `chime_type_choices` | list | The same options **with names**: `{"value": int, "label": str, "disables_indoor_chime": bool}`. May be absent or `[]` — see below |
| `supports_outdoor_tone` | bool | Whether the doorbell's own outdoor tone can be turned on/off |
| `outdoor_tone_on` | bool | Outdoor tone state |
| `supports_outdoor_tone_volume` | bool | Whether that tone's **volume** is settable — a **separate** flag, and `false` on the reference doorbell even though the tone itself toggles fine |
| `outdoor_tone_volume` | int | `outdoor_tone_volume_min`–`outdoor_tone_volume_max` (0–3 on the doorbell tested) |
| `outdoor_tone_volume_min` / `outdoor_tone_volume_max` | int | Device-reported range |
| `raw` | object | The untouched upstream settings object, so a caller can read a field the bridge does not model yet |

Two traps in this object. The upstream enum names are
`<thing>EnableDisableFeatureSupport`, not the `is<Thing>OnFeatureSupport` spelling
you would guess — reading the raw document with the guessed name silently finds
nothing and looks like "unsupported". And the outdoor tone's on/off support and
its **volume** support are genuinely different flags: on the reference doorbell
the tone can be switched but its volume cannot, so a `chime` write carrying
`outdoor_volume` is a `422 unsupported` there while `outdoor_on` succeeds.

The talk/call volume and the outdoor tone volume are also different things.
**Only `call_volume` affects talkdown.**

### `chime_type_choices` — names for the chime types

`chime_type_options` is bare integers, because that is all the website API
publishes; the alarm.com web portal names them with a client-side enum. The
bridge instead asks the **mobile** API (`GetCurrentDoorbellSettings`), which
returns the names and, more usefully, a per-option `disables_indoor_chime` flag —
so no client has to know which number means "no wired chime" on a given model.
On the reference doorbell:

| `value` | `label` | `disables_indoor_chime` |
| --- | --- | --- |
| `-1` | None | `true` |
| `0` | Mechanical | `false` |
| `1` | Digital | `false` |

Two things a caller must handle:

- **It is best-effort.** It is a second upstream call, over a different API, and
  is absent or `[]` when there is no mobile session or the read fails. Fall back
  to `chime_type_options` and show the numbers — losing the labels must not lose
  the control.
- **Honour `disables_indoor_chime`.** When the selected type carries it, the
  indoor chime toggle is meaningless (there is no wired chime to ring) and the
  alarm.com portal disables it. Do not hardcode `-1` for this; the flag exists so
  clients do not have to.

> **Selecting the "none" type turns the indoor chime OFF upstream, and switching
> back does not turn it on again.** Observed live: setting `chime_type` to `-1`
> left `indoor_chime_on` false, and a later `chime_type: 0` restored the type but
> not the toggle. TELUS does this, not the bridge — the write carries only the
> field asked for. It follows from the same coupling `disables_indoor_chime`
> describes, but it is a lasting state change rather than a display rule, so a
> caller that offers the "none" option should expect to send `indoor_on: true`
> afterwards if it wants the chime back.

The option list is a property of the hardware, so the bridge caches it for an
hour rather than re-reading it every poll. The current *selection* is not cached —
it comes from the website settings in the same response.

`image` and `video` follow the same convention: one key per field the write
routes accept, each paired with a `supports_<field>` boolean off the matching
`*FeatureSupport` enum, plus `*_min`/`*_max`/`*_options` where the device reports
a range or a list, plus `raw`. **Which fields appear is device-dependent** — the
example above is a doorbell; a camera without IR will still return the IR keys,
with `supports_*` false. Always branch on `supports_*`, never on key presence.

### The `siren` and `privacy` sections — a fixed contract

`audio`, `image` and `video` come from the website API and vary by device model.
`siren` and `privacy` do not: they come from the **mobile MWS** actions
`GetAllSirenDevices` and `GetAllCameraPrivacyModeInfo`, whose raw records use
inconsistent, capture-derived key spellings (`canManuallyTriggerSiren` vs
`can_manually_trigger_siren`, `SupportsPrivacyMode` vs `supports_privacy_mode`).
The bridge **normalizes** each record into a fixed, small shape before returning
it, and that shape is a contract other code depends on — the Home Assistant
entities read exactly these key names and nothing else.

**`settings["siren"]`** — an object, or `null`:

| Field | Type | Meaning |
| --- | --- | --- |
| `can_manually_trigger` | bool | Whether **this account** may sound this camera's siren. A per-account permission, not a hardware capability — a camera with a siren this account may not fire reports `false`. Gate the control on this |
| `active` | bool | Whether the siren is sounding right now |
| `timestamp_end` | value \| `null` | The server-chosen end of the current sounding, as the upstream reports it. `null` when nothing is running (or when upstream sent none). The duration is not the caller's to choose |

**`settings["privacy"]`** — an object, or `null`:

| Field | Type | Meaning |
| --- | --- | --- |
| `supported` | bool | Whether the camera reports privacy-mode support. `false` for every camera on the account this was built against |
| `active` | bool | Whether privacy mode is on (camera blinded) |

Every field is a real JSON `bool`, always present, whatever the upstream sent —
the MWS side spells booleans as the strings `"true"`/`"false"` and privacy state
as `0`/`1`, and normalizing that here is the point of the contract (a truthiness
test on the string `"false"` is `True`).

The two capability flags default in **opposite** directions, on purpose:

- `can_manually_trigger` fails **closed** — absent, null or unrecognised becomes
  `false`. It is the permission that sounds a real siren in someone's home, so a
  record the bridge cannot read is never taken as consent.
- `supported` fails **open** — only an explicit `false` reports unsupported. That
  action returns a privacy *state* object rather than an enumeration of capable
  devices, so a missing flag is a gap in a shape recovered from the decompiled
  app, not the device saying no; failing closed would leave the privacy control
  permanently unavailable on every camera.

`active` fails closed on both: an unreadable value reports "not sounding" /
"not blinded", because claiming a camera is blinded while it is still recording
is the dangerous direction to be wrong in.

**`null` is not "unsupported".** Either section is `null` when the mobile session
was unavailable — no talk manager (`TALKDOWN_ENABLED=false`), a failed MWS read,
a token that would not mint — or when the MWS response carried no record
matching this camera. All of those mean *nothing was reported*, and none of them
means the camera cannot do the thing.

Both sections are best effort by design: an MWS outage degrades them to `null`
and still returns a `200` with the website settings intact, rather than failing
the whole read with a `502`. A caller must therefore distinguish three states —
`null` (unknown), `{"can_manually_trigger": false}` / `{"supported": false}`
(reported unsupported), and a positive flag — and must not collapse the first
two. Treating `null` as `false` turns a transient MWS blip into "this camera has
no siren"; treating it as `true` offers a control that will fail.

Because these are two extra MWS POSTs on top of three website GETs, this route
is the most expensive read in the API. Poll it slowly.

**Errors:** `401 bad_secret`, `404 unknown_camera`, `502 telus_error`. Note there
is **no `503` here** — unlike the siren and privacy *write* routes, a missing
mobile session degrades this read to `null` sections instead of failing it.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/settings
```

---

## `GET /control/cameras/{camera_id}/diagnostics`

Read-only health for one camera: signal strength, battery, and the device status
the settings screen shows. A camera degrading to poor Wi-Fi is otherwise a silent
failure — the picture just gets worse — so this exists to make it a number
something can alert on.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Response**

```json
200 {
  "signal_strength": 78,
  "battery": null,
  "device_status": "Online",
  "raw": {}
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `signal_strength` | number \| null | alarm.com's `normalizedSignalStrength` — a **0–100 score, not dBm** (see below). `null` when the device reports none, typically a wired camera |
| `battery` | number \| null | Battery percentage, `null` on a camera with no battery |
| `device_status` | string \| null | Upstream status string |
| `raw` | object | The three upstream responses as received, under `signal_strength` / `battery` / `device_status` keys, for anything not modelled above |

**`signal_strength` is not dBm.** The signal document reports
`normalizedSignalStrength` (0–100, the bars the portal draws) and a coarse
`signalStrengthEnum`; it does **not** report `signalStrength` or `rssi`. Anything
consuming this must not label it dBm — a real Wi-Fi dBm figure is negative, so a
`58` presented as dBm is nonsense, and in Home Assistant a wrong unit is recorded
into long-term statistics rather than just shown. The bridge does fall back to
`signalStrength` / `signalStrengthValue` for camera models that report those
instead, and their unit is **unverified** — which is why the Home Assistant
sensor asserts no unit at all.

**`battery` is `null` for "no battery", not `0`.** `batteryLevel` is present and
`0` on mains-powered devices, so the value alone cannot tell a missing battery
from a flat one. The bridge honours alarm.com's own `shouldDisplayBatteryLevel`
flag and reports `null` when it is false — build no entity for a `null`, or you
get a permanent 0%.

**`raw` carries the camera's Wi-Fi SSID and encryption type.** That is diagnostic
detail about someone's home network. Do not log it or surface it as an entity
attribute without deciding to do so on purpose.

Every field is nullable on purpose: three separate upstream reads back this
response, and a camera that reports none of them is normal, not an error. A
caller must not create a sensor for a `null` field — an "unknown" battery sensor
on a wired camera is noise, not diagnostics.

**Errors:** `401 bad_secret`, `404 unknown_camera`, `502 telus_error`.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/diagnostics
```

---

## `POST /control/cameras/{camera_id}/settings/volume`

Sets the camera's speaker/call volume — the level talkdown is heard at. The
mobile API's `<duav>` element claims `SupportsVolumeAdjustment="false"` for this
doorbell; the website API exposes it as supported with a live 0–10 sweep
observed, so this route trusts the website's `callVolumeFeatureSupport` and
ignores the mobile claim.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `volume` | int | yes | Must be within the device's own `call_volume_min`–`call_volume_max` (0–10 on the doorbell tested). Anything outside that range is **refused with a `400 malformed`** — never clamped, so the camera is never quietly set to a volume the caller did not ask for. A non-integer (including `true`) is also a `400` |

The range comes from the device on every write: the route re-reads the audio
settings before applying and bounds the request against the `call_volume_min` /
`call_volume_max` it just read, not against anything hardcoded. A device that
reports no usable bounds is passed through unbounded.

**Success response:** the refreshed `audio` object, identical in shape to the
`audio` key of `GET .../settings` above, read back after the write rather than
echoed from the request. Since an out-of-range value never gets written, the
returned `call_volume` is the value that was asked for; the read-back is there
for the *other* fields in the section.

**Errors:** the shared table. `422 unsupported` means
`supports_call_volume` is false for this camera.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"volume": 8}' \
  $BASE/control/cameras/<camera_id>/settings/volume
```

---

## `POST /control/cameras/{camera_id}/settings/chime`

Sets the doorbell's chime: the indoor chime, the wired chime type, the tone the
doorbell itself plays, and that tone's volume. Every field is optional and
independent — send only what you want changed.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `indoor_on` | bool | no | Indoor chime on/off |
| `chime_type` | int | no | The **wired chime type** — must be one of `chime_type_options`. Off-list is a **`400 malformed`** (the field is supported, the value just is not offered), while a device that cannot select a type at all is a `422`. Gated on `supports_chime_type`, a *different* flag from `supports_indoor_chime` |
| `outdoor_on` | bool | no | The doorbell's own outdoor tone, on/off |
| `outdoor_volume` | int | no | Must be within `outdoor_tone_volume_min`–`outdoor_tone_volume_max` (0–3 on the doorbell tested). Outside that range is a **`400 malformed`**, not a clamp — same rule as `volume` above. Gated on `supports_outdoor_tone_volume`, which is a *different* flag from `supports_outdoor_tone` and is `false` on the reference doorbell: `outdoor_on` works there, `outdoor_volume` is a `422` |

An empty body — no recognised field at all — is a `400 malformed` rather than a
no-op write, so a caller with a typo'd key hears about it instead of believing a
setting was applied.

**Success response:** the refreshed `audio` object (same shape as `GET
.../settings`'s `audio`).

**Errors:** the shared table. `422 unsupported` names the requested field the
device does not support (`supports_indoor_chime` / `supports_chime_type` /
`supports_outdoor_tone` / `supports_outdoor_tone_volume` — four separate flags). All
requested fields are support-checked **before anything is written**, so a
partly-unsupported request leaves the camera untouched rather than half-applied.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"indoor_on": true, "outdoor_volume": 1}' \
  $BASE/control/cameras/<camera_id>/settings/chime
```

---

## `POST /control/cameras/{camera_id}/settings/image`

Sets picture settings. Every field is optional; only the ones sent are changed
(the rest are carried over from the read half of the read-modify-write).

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)** — accepted keys:

| Field | Type | Notes |
| --- | --- | --- |
| `brightness` | int | Must be within the device-reported min/max, else `400` |
| `contrast` | int | Must be within the device-reported min/max, else `400` |
| `saturation` | int | Must be within the device-reported min/max, else `400` |
| `sharpness` | int | Must be within the device-reported min/max, else `400` |
| `is_flipped` | bool | Image flip (a ceiling-mounted camera) |
| `hdr_enabled` | bool | HDR |
| `ir_led_enabled` | bool | Infrared illuminator |
| `ir_sensitivity` | int | IR sensitivity; must be within the device-reported range, else `400` |
| `night_vision_enabled` | bool | Night vision |

Any other key is a `400 malformed`, as is a value of the wrong type or a number
outside the range the device published. A recognised key the device reports no
support for is a `422 unsupported`. Nothing is written unless every requested
field passes, so a partly-invalid request leaves the camera untouched.

**Success response:** the refreshed `image` object (same shape as `GET
.../settings`'s `image`).

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"brightness": 60, "hdr_enabled": true}' \
  $BASE/control/cameras/<camera_id>/settings/image
```

---

## `POST /control/cameras/{camera_id}/settings/video`

Sets recording/clip settings. Same optional-field, read-modify-write behaviour as
the image route.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)** — accepted keys:

| Field | Type | Notes |
| --- | --- | --- |
| `clip_resolution` | string | Must be one of the device's `clip_resolution_options` (`480x480` / `960x960` / `1440x1440` on the cameras seen). This is the **recorded clip** resolution and has no effect on the live RTSP stream, which the bridge encodes itself |
| `clip_quality` | string | Encode quality; must be one of the device's `clip_quality_options` |
| `pretrigger_enabled` | bool | Pre-trigger recording (the seconds before the event) |
| `keep_clips_days` | int | Cloud retention in days, checked against whichever the device publishes — its `keep_clips_days_options` list and/or a min/max. **`-1` is a legitimate option meaning "keep forever"**, so do not filter negatives out on the assumption that a retention must be positive |
| `alarm_clip_duration` | int | Alarm-clip length in seconds; must be within the device-reported range, else `400` |

Any other key, or a value of the wrong type, is a `400 malformed`. A recognised
key the device reports no support for is a `422 unsupported` — that gate runs on
the pre-read, before anything is sent.

A value **off the option list** the device published is a `400 malformed`, not a
`422`: the field itself is supported, the value is simply not one this camera
offers. Read `clip_resolution_options` / `clip_quality_options` from
`GET .../settings` and pick from them rather than guessing.

**Success response:** the refreshed `video` object (same shape as `GET
.../settings`'s `video`).

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"clip_resolution": "960x960", "keep_clips_days": 30}' \
  $BASE/control/cameras/<camera_id>/settings/video
```

---

## `POST /control/cameras/{camera_id}/siren`

Starts or stops the camera's built-in siren, via the mobile MWS actions
`ManualSirenStart` / `ManualSirenStop`.

> **This is loud, on purpose, in someone's home.** Nothing in the bridge fires it
> on its own — it happens only when a caller explicitly posts `start`. Treat the
> first invocation on a new device as an experiment and pick your moment: no live
> siren command was ever captured from the app on the account this was built
> against (the UI is absent there), so the request shape comes from the decompiled
> app rather than from an observed round trip.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | `"start"` or `"stop"`. Any other value is a `400 malformed` |

**Success response**

```json
200 {"status": "applied"}
```

That is the **whole** body — exactly two keys' worth, and nothing from the
upstream MWS envelope is merged into it. The route checks the envelope's `ok`
flag (a not-ok envelope becomes a `502 telus_error`) and then discards it, so a
`200` means "the upstream accepted the command", not "here is the siren's new
state". There is no read-back of siren state on this route; the only read is the
`siren` section of `GET .../settings`.

**Two things a caller must honour:**

- **Triggering is a per-account permission, not just a device capability.**
  `GetAllSirenDevices` reports `canManuallyTriggerSiren` per camera, and a device
  advertising a siren is not the same as this account being allowed to sound it.
  The bridge reads that flag (from `GetAllSirenDevices`) before firing and
  returns `422 unsupported` when it is false. A caller that mirrors the bridge's
  reported capabilities should decline to offer the control at all rather than
  discover the refusal on a failed press.
- **The siren is time-bounded by the server, not by you.** The upstream carries a
  `timestampEnd` chosen server-side. Do not assume a duration and do not assume
  it stops on its own on any particular schedule — post `{"action": "stop"}` to
  end it early.

**Errors:** the shared table, plus `422 unsupported` for
`canManuallyTriggerSiren` false, and:

| Status | Body | Meaning |
| --- | --- | --- |
| `503` | `{"error": "mws_unavailable"}` | There is no mobile MWS session to act through |

The `503` is the one that surprises people. Siren and privacy are the only two
routes in this group that speak the **mobile** API, and the bridge does not own
an MWS session of its own — it borrows the one the talk manager mints. With
`TALKDOWN_ENABLED=false` no talk manager is constructed, so these two routes
return `503 mws_unavailable` even though the rest of the settings surface works
normally. It is a bridge-configuration state, not a device or account state, and
it does not clear on retry.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"action": "start"}' \
  $BASE/control/cameras/<camera_id>/siren

curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"action": "stop"}' \
  $BASE/control/cameras/<camera_id>/siren
```

---

## `POST /control/cameras/{camera_id}/privacy`

Turns camera privacy mode on or off (MWS `SetCameraPrivacyModeState`). Privacy
mode blinds the camera at the device, so **the live RTSP stream and snapshots go
dark while it is active** — this is the one route here that changes what the
video half of the bridge can deliver.

**Path parameter:** `camera_id` — must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `active` | bool | yes | `true` = privacy on (camera blinded), `false` = off. A JSON boolean; the string form the wire protocol wants is built by the bridge |

**Success response**

```json
200 {"status": "applied"}
```

The body does **not** echo `active` back, and carries nothing from the upstream
MWS envelope. As with the siren, a `200` means the upstream accepted the write;
to read the resulting state, poll the `privacy` section of `GET .../settings`.

**Support caveat.** `SupportsPrivacyMode` is **false** for the cameras on the
account this was built against, so expect `422 unsupported` there even though the
action exists upstream. Like the siren, the capability is read upstream
(`GetAllCameraPrivacyModeInfo`) rather than assumed from the device model. Note
the asymmetry with the siren: privacy refuses only on an **explicit** `false`,
because this action returns a state object rather than an enumeration of capable
devices, so failing to find a matching record is a response-shape mismatch and
not evidence the camera lacks the feature.

**Errors:** the shared table, plus `422 unsupported` when the camera reports no
privacy support, and `503 mws_unavailable` on the same terms as the siren route
above (no talk manager, i.e. `TALKDOWN_ENABLED=false`).

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"active": true}' \
  $BASE/control/cameras/<camera_id>/privacy
```

---

## `GET /control/events`

Most recent camera video-clip events across all cameras, newest first.

**Query parameters**

| Param | Default | Notes |
| --- | --- | --- |
| `limit` | `20` | Max number of events. A non-integer value falls back to `20`. |

**Response**

```json
200 {
  "events": [
    {
      "id": "<event_id>",
      "camera_id": "<camera_id>",
      "timestamp": 1723406400,
      "description": "Motion detected",
      "thumbnail_url": "https://videostreamnaNN.devicetask.com/...",
      "download_url": "https://videostreamnaNN.devicetask.com/...",
      "clip_duration_ms": 12000
    }
  ]
}
```

`thumbnail_url` and `download_url` are signed TELUS relay-host URLs, passed
through as-is (fetchable directly, no cookie needed) — they are **not** proxied
through this endpoint. Note that these signed URLs expire within minutes of the
fetch; use [`/control/clip/{video_event_id}`](#get-controlclipvideo_event_id)
to re-mint a fresh one at click time.

**Errors:** `401 bad_secret`, `502 telus_error`.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" "$BASE/control/events?limit=10"
```

---

## `GET /control/history`

TELUS's system-wide activity feed — arm/disarm, door/lock, camera triggers,
doorbell presses, etc., newest first. Broader than `/control/events` (which is
camera clips only).

**Query parameters**

| Param | Default | Notes |
| --- | --- | --- |
| `limit` | `20` | Max number of entries. A non-integer value falls back to `20`. |

**Response**

```json
200 {
  "events": [
    {
      "id": "<event_id>",
      "timestamp": 1723406400,
      "description": "Front Door unlocked",
      "device_id": "<device_id>",
      "device_description": "Front Door Lock",
      "event_type_name": "AccessControlUnlocked",
      "thumbnail_url": null,
      "download_url": null,
      "clip_duration_ms": null,
      "video_event_id": null
    }
  ]
}
```

Entries the bridge could correlate to a saved clip carry the same
`thumbnail_url` / `download_url` / `clip_duration_ms` shape as `/control/events`,
so callers can offer the same "click to see the clip" behavior; entries with no
correlated clip have those fields as `null`.

`video_event_id` is the id of the correlated saved clip (`null` when there is
none). Because the embedded `download_url` expires within minutes, pass
`video_event_id` to `/control/clip` to re-mint a fresh URL at the moment the
user clicks (e.g. from a phone notification).

**Errors:** `401 bad_secret`, `502 telus_error`.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" "$BASE/control/history?limit=25"
```

---

## `GET /control/clip/{video_event_id}`

Re-mints a **fresh** signed download URL for one saved clip on demand. The
signed URLs embedded in `/control/events` and `/control/history` responses
expire within minutes of being issued (alarm.com stamps each mint into the
token), even though the clip itself never expires. A URL captured at poll time
and opened later lands on alarm.com's "you don't have permission" page — so
callers hit this endpoint at click time and redirect to the result.

Responses are cached in-process for `config.CLIP_URL_CACHE_TTL_S` (60 s), keyed
by event id, so a burst of taps on one notification collapses into a single
TELUS round trip. Expired cache entries are evicted opportunistically.

**Path parameter:** `video_event_id` — the id from a `/control/history` entry's
`video_event_id` (or an `/control/events` entry `id`).

**Response**

```json
200 {
  "download_url": "https://videostreamnaNN.devicetask.com/...",
  "thumbnail_url": "https://videostreamnaNN.devicetask.com/...",
  "clip_duration_ms": 12000
}
```

**Errors**

| Status | Body | Meaning |
| --- | --- | --- |
| `404` | `{"error": "unknown_event"}` | TELUS returned no event (deleted clip or bogus id) |
| `502` | `{"error": "telus_error"}` | The TELUS-side fetch failed |

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" $BASE/control/clip/<video_event_id>
```

---

## `GET /control/automations`

Lists video-related automation rules (recording schedules, etc.).

**Response**

```json
200 {
  "rules": [
    {
      "id": "<rule_id>",
      "name": "Record on motion",
      "is_paused": false,
      "can_be_paused": true,
      "action": "<action>",
      "trigger": "<trigger>",
      "timeframe": "<timeframe>"
    }
  ]
}
```

**Errors:** `401 bad_secret`, `502 telus_error`.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" $BASE/control/automations
```

---

## `POST /control/automations/{rule_id}/pause`
## `POST /control/automations/{rule_id}/resume`

Pause or resume one automation rule. No request body.

**Path parameter:** `rule_id`. This is **not** validated against a known-rules
list first — TELUS itself 404s/errors on a bad id, and that is surfaced as
`telus_error`, so a local pre-check would only duplicate what TELUS enforces.

**Response**

```json
200 {"status": "applied"}
```

**Errors:** `401 bad_secret`, `502 telus_error` (includes the bad-`rule_id`
case).

**Examples**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  $BASE/control/automations/<rule_id>/pause

curl -s -X POST -H "Authorization: Bearer $SECRET" \
  $BASE/control/automations/<rule_id>/resume
```

---

## `POST /control/cameras/by-path/{path_name}/activate`
## `POST /control/cameras/by-path/{path_name}/deactivate`

Manual levers to (re)start or stop a single camera pipeline without touching
the others and without a full container restart. **Not used by Home
Assistant** and not called automatically — every registered camera's pipeline
is started eagerly by `main.py` and left running ("always warm"), so there is
no reader-count-driven on-demand start/stop.

**Path parameter:** `path_name` — the RTSP path name
(`config.rtsp_path_for(camera_id)`), kept rather than `camera_id` because
MediaMTX's `$MTX_PATH` is what identifies a path elsewhere in the codebase. The
handler maps it back to a camera via the registry.

**Responses**

```json
200 {"status": "activating"}
```

```json
200 {"status": "deactivating"}
```

**Errors**

| Status | Body | Meaning |
| --- | --- | --- |
| `401` | `{"error": "bad_secret"}` | Auth failure |
| `404` | `{"error": "unknown_path"}` | No known camera maps to that path name |

**Examples**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/by-path/<path_name>/activate

curl -s -X POST -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/by-path/<path_name>/deactivate
```

---

## `POST /control/credentials`

Pushes updated TELUS credentials into the already-running bridge process. This
is the route the HA integration's config-flow reconfigure/reauth step calls so
a password change takes effect without restarting the container.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `username` | string | yes | TELUS account email. Stripped; must be non-empty. |
| `password` | string | yes | TELUS account password. Stripped; must be non-empty. |

On receipt the bridge swaps the values into `config.TELUS_EMAIL` /
`config.TELUS_PASSWORD` and attempts a re-login (serialized against all other
login callers via the shared login lock).

**Success response**

```json
200 {"status": "applied"}
```

**Errors**

| Status | Body | Meaning |
| --- | --- | --- |
| `400` | `{"error": "malformed"}` | Body not valid JSON, or missing/empty `username`/`password` |
| `401` | `{"error": "bad_secret"}` | Auth failure |
| `500` | `{"error": "internal"}` | Re-login with the new credentials failed (e.g. genuinely wrong password, or a TELUS/network error) |

**Note on the `500` case:** even when re-login fails, the new credentials are
**left in place** in `config.py`. The process's normal reconnect/retry loops
keep trying them, rather than silently reverting to the old (also
possibly-stale) values.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"username": "user@example.com", "password": "<telus-password>"}' \
  $BASE/control/credentials
```

---

## Home Assistant client

The HA integration talks to this API through
`custom_components/telus_smarthome/bridge_client.py`, used by `camera.py`,
`event.py`, and `binary_sensor.py`. Two helpers matter:

- **`read_control_secret()`** reads the shared secret off the mounted share
  (`/share/telus_cam_bridge/control_secret`), stripped. It is blocking file I/O
  and must run in an executor. It returns `None` when the bridge has not yet
  generated its secret (e.g. right after either container restarts) — callers
  treat that as "nothing to report this cycle", not an error.
- **`bridge_get(hass, url, *, params=None, timeout=10)`** reads the secret
  fresh **on every call** (this is why secret rotation needs no coordination),
  issues an authenticated `GET` with `Authorization: Bearer <secret>`, raises
  for non-2xx, and returns the parsed JSON. It returns `None` when the secret
  is not yet available; any other failure (bridge unreachable, non-2xx, etc.)
  raises `aiohttp.ClientError` / `asyncio.TimeoutError` for the caller to
  handle.

Because the secret is re-read per request, an HA request that read the old
secret microseconds before a rotation lands can `401` exactly once; every
caller already retries, so this is at most one request per 30-day rotation.
