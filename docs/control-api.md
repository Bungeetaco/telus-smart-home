# Control API Reference

The camera bridge runs a small loopback-only HTTP control server so that the
companion Home Assistant integration can poll live state, drive on-demand
actions (snapshots, talkdown, clip re-minting), and push refreshed TELUS
credentials into the already-running bridge process without a container
restart. This document is the complete reference for that API, derived from
`camera-bridge/bridge/control_server.py`.

## Binding and reachability

- **Host:** `127.0.0.1` (`config.CONTROL_HOST`): loopback only. The server is
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
- **A pre-existing secret file is re-tightened on adoption.** When the bridge
  starts against a secret file it did not create (e.g. restored from a backup,
  or written under a permissive umask) and the file's mode has any group/other
  bits set, it is best-effort `chmod`ed to `0600`, with a warning naming the
  previous mode. If the chmod fails (e.g. the file is owned by another uid on
  the shared mount) a warning is logged and the secret is served unchanged;
  the bridge never regenerates the secret over a permissions problem, so the
  HA client keeps authenticating.
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
| GET | `/control/partitions` | bearer | List alarm partitions and the flags that gate a sensor bypass |
| POST | `/control/partitions/{partition_id}/bypass` | bearer | Bypass / un-bypass named sensors; **a bypassed zone stops triggering the alarm** |
| GET | `/control/arming-devices` | bearer | List the Bluetooth arming devices (phones that may auto-disarm the panel) |
| POST | `/control/arming-devices/{device_id}/disarming` | bearer | Turn a phone's auto-disarm permission on or off |
| POST | `/control/arming-devices/{device_id}/enrolled-partition` | bearer | Move a paired phone to a different partition |
| DELETE | `/control/arming-devices/{device_id}` | bearer | Unpair one phone: **not undoable from this side** |
| GET | `/control/bluetooth-disarming` | bearer | The panel-wide Bluetooth pairing flag and disarm window |
| POST | `/control/bluetooth-disarming` | bearer | Change the panel-wide Bluetooth pairing flag and disarm window |
| POST | `/control/cameras/{camera_id}/settings/notifications` | bearer | Turn TELUS-side push notifications on/off |
| POST | `/control/cameras/{camera_id}/settings/led` | bearer | Turn the device's status LED on/off, or set its brightness |
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
| `429` | `{"error": "rate_limited", "retry_after": <seconds>}` | The alarm.com edge rate-limited us. Carries a `Retry-After` header with the **server's own** wait: honour it rather than retrying on your own schedule, or the next attempt walks straight back into the limiter. Emitted by the camera settings, diagnostics, siren, privacy, partition, bypass, arming-device and Bluetooth-disarming routes. The one exception: the settings read's two **MWS** sections (siren/privacy) absorb a rate limit internally (held for the server's wait and degraded to `null`) rather than failing the response |

---

## `GET /control/health`

Process liveness probe. **No authentication**: it exposes no credential
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
grepping container logs. The Home Assistant integration polls this route every
30 s to back its per-camera stream-health sensors and its camera-bridge
connectivity entity (see
[ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity));
the route answers from memory over loopback, so that poll (unlike the settings
poll) costs no upstream requests.

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
      "dropped_frames": 0,
      "last_error": null,
      "audio_consumed": true,
      "audio_format": "8000Hz/1ch/s16",
      "audio_codec": "pcmu",
      "publisher_audio_format": "8000Hz/1ch/s16",
      "audio_format_rebuilds": 1,
      "decoded_audio_frames": 4821,
      "dropped_audio_frames": 0,
      "audio_dropped": false,
      "audio_dropped_reason": null,
      "publisher_alive": true,
      "last_publish_at": 1723406400
    }
  },
  "talk": {
    "attempts": 3,
    "succeeded": 1,
    "stopped": 1,
    "failed": 1,
    "last_failure_reason": "media_error"
  },
  "audio_decoder": {
    "decoded_opus": 0,
    "decoded_pcmu": 49213,
    "discarded": 0,
    "sessions_opus": 0,
    "sessions_pcmu": 12,
    "sniff_packets": 20,
    "opus_confidence": 0.75
  }
}
```

The top-level `talk` key is a five-field summary of the talkdown counters
(`attempts`, `succeeded`, `stopped`, `failed`, `last_failure_reason`); the full
counters live at [`/control/metrics`](#get-controlmetrics) below.

| Field | Type | Notes |
| --- | --- | --- |
| `state` | string | One of `streaming`, `connecting`, `reconnecting`, `restart_deferred` |
| `consecutive_failures` | int | Reconnect attempts since the last good frame |
| `last_frame_at` | int \| null | Unix timestamp of the most recent frame, or `null` |
| `fps` | number \| null | The frame rate the running ffmpeg was **declared** with (`-framerate`, post-clamp to 1–30), not a live measurement of what the camera is delivering. `null` until the camera first reaches the `streaming` state. It can change between polls without a restart: the frame rate is a device setting, so the bridge keeps re-measuring it and rebuilds the publisher when the delivered rate moves materially away from the declared one (see [camera-bridge.md](camera-bridge.md#the-frame-rate-is-adaptive-not-latched)). The delivered rate rides beside it in `measured_fps` |
| `hardware_encode` | bool \| null | Whether the pipeline is using hardware encode; `null` until the camera first reaches the `streaming` state |
| `dropped_frames` | int | Lifetime total, since process start, of frames dropped because the consumer fell behind the decoder (the ingest queue's latency-over-completeness drop; see [camera-bridge.md](camera-bridge.md)). Present (`0`) from the camera's first status entry. The total is accumulated when each ~165 s video session **ends**, on healthy rotation and on the failure paths alike, not per frame, so it can lag the live session's drops by up to one rotation. Resets with the process, like the rest of this response |
| `last_error` | string \| null | Most recent error string, or `null` |
| `audio_consumed` | bool | Whether the downstream audio track is being decoded and published on this camera's RTSP path. True only when `JANUS_CONSUME_AUDIO` **and** `JANUS_REQUEST_AUDIO` both name the camera; naming it in only the first is a misconfiguration that reads `false` here and is logged once (see [configuration.md](configuration.md)) |
| `audio_format` | string \| null | The raw PCM shape of an audio frame this camera has actually **decoded**, as `<rate>Hz/<channels>ch/<sample format>`; `null` until one has been. It is an observation, never the shape the publisher was launched with, which has its own field (`publisher_audio_format`): reporting the launched shape here made this field read `"48000Hz/2ch/s16"` for a camera publishing pure silence. Once observed it persists across rotations, since a camera's audio shape is a property of the device rather than of a session, and every decoded frame re-checks it; use `decoded_audio_frames` to ask whether audio is flowing *now*. On this account's cameras the observed shape is `"8000Hz/1ch/s16"`: the track is advertised as Opus and carries G.711 mu-law (see `audio_codec`) |
| `audio_codec` | string \| null | Which decoder produced this camera's audio frames: `"pcmu"` (G.711 mu-law at 8 kHz, what this account's cameras actually send) or `"opus"` (what the Janus offer **advertises**). `null` until a frame has been decoded. It is derived from the frame shape in `audio_format`, which identifies the decoder exactly: aiortc pins its Opus decoder to 48 kHz stereo and its mu-law decoder to 8 kHz mono. Read it against the process-wide `audio_decoder` block: `"opus"` here with almost every packet in `discarded` is the codec mislabel that once left ~6% of the audio timeline real and the rest silence fill |
| `publisher_audio_format` | string \| null | The raw PCM shape the RUNNING publisher's audio input reads, in the same form as `audio_format`, or `null` when the publisher has no audio input at all. The publisher is deliberately **video-only** until a real frame has been decoded for the camera: nothing is ever sized from a guess, so a camera that never sends an audio packet keeps publishing video forever rather than looping on a repair. This field and `audio_format` differing means a publisher rebuild is owed at the next session rotation; `publisher_audio_format: null` with `audio_dropped: false` means the camera is still waiting to learn whether it has audio at all, and with `audio_dropped: true` means the publisher gave up on an input it had |
| `audio_format_rebuilds` | int | How many times, since process start, this camera's publisher has been rebuilt because the audio shape it was launched with was not the shape the camera sends. Present (`0`) from the camera's first status entry. Normally at most `1` (video-only, then the observed shape); the ceiling is `AUDIO_FORMAT_REBUILD_LIMIT` (see [configuration.md](configuration.md)), past which the audio input is given up on and `audio_dropped` says so. A rebuild kicks every RTSP reader, so this climbing is a reader-visible cost, not just an accounting one |
| `decoded_audio_frames` | int | Lifetime total, since process start, of decoded audio frames this camera's sessions have handed to the publisher. Present (`0`) from the camera's first status entry. Unlike the two drop counters this is updated **as each frame arrives**, not folded in when a session ends, so it does not lag a rotation; it is the field to poll twice and compare. `audio_consumed: true` with this stuck at `0` is a silent stream, not a quiet one, and it is the only field in this response that can tell them apart |
| `dropped_audio_frames` | int | Lifetime total, since process start, of decoded **audio** frames dropped because the consumer fell behind. Present (`0`) from the camera's first status entry, and accumulated across rotations exactly like `dropped_frames`. Deliberately a separate field: `fps` and `dropped_frames` are a contract about the video stream, and a number that quietly started counting two different things would be worse than no number at all. Note that it cannot detect audio that never decodes at all: a stream that produces nothing drops nothing |
| `audio_dropped` | bool | Whether the running publisher **gave up on its audio input** and is now publishing video only, because ffmpeg stopped reading the audio pipe, the pipe closed under it, the pipe could not be opened when the publisher started, a persistent run of decoded frames failed to match their own declared shape, or the decoded shape kept changing past `AUDIO_FORMAT_REBUILD_LIMIT`. Video is worth more than audio, so this degrades instead of rebuilding the publisher (which would kick every RTSP reader off the path), and the degradation lasts until the publisher is next rebuilt for some other reason. `audio_consumed` keeps saying what the camera is **configured** to do; this says whether the publisher is still doing it, and without it the difference is invisible: `decoded_audio_frames` keeps climbing either way, because frames are still being decoded, they just no longer reach ffmpeg |
| `audio_dropped_reason` | string \| null | What was observed when the audio input was dropped (for a wedged pipe, including the backlog in bytes); `null` while `audio_dropped` is false |
| `publisher_alive` | bool \| null | Whether this camera's ffmpeg publisher exists **and is still consuming what it is fed**: the process being alive is not sufficient on its own, which is how a camera once reported `state: "streaming"` with `consecutive_failures: 0` while MediaMTX had no path at all. `null` until the camera's first publisher starts. False drives a publisher rebuild, so it is normally seen only in the moment before one; a value that stays false means the rebuild is not succeeding either. It cannot see past ffmpeg: it proves ffmpeg is reading its input, not that MediaMTX still accepts what ffmpeg writes |
| `last_publish_at` | int \| null | Unix timestamp of the last frame ffmpeg actually **took**, against `last_frame_at`'s "a frame arrived from the camera". `null` until the first published frame. The two normally move together; the gap between them is the difference between "streaming" and "streaming to nowhere", and a `last_frame_at` that keeps advancing while this one does not is a publisher that has stopped reading |
| `measured_fps` | int \| null | The rate the camera is actually **delivering**: the bridge's last settled measurement (a median over `RATE_RESAMPLE_FRAMES` inter-frame deltas, taken clear of the session-start burst). Read it against `fps`, which is what ffmpeg was **declared** with: a material gap is padding (measured below declared) or dropping (measured above) before ffmpeg says so. `null` until the first settled batch; survives rotations; updated as frames flow, so it lags a reconnect gap |
| `rate_pending` | int \| null | A **confirmed** rate change waiting for the session rotation that applies it (rate rebuilds happen only at rotations, so readers pay one reconnect instead of a failed session). `null` otherwise, including after the rotation re-checked the current reading and found the change no longer real |
| `duplicated_frame_warnings` | int | Lifetime count, since process start, of ffmpeg's `frames duplicated` stderr warnings, accumulated across publisher rebuilds like `dropped_frames`. The **encoder's own** corroboration that the declared rate exceeds what the camera delivers. Observability only, and read it knowing its limits: it is one-directional (a too-LOW declaration, the direction that destroys frames, never fires it), not quantitative, and coarse (fixed 1000-frame thresholds). A rate correction that worked shows here as the count going quiet |

The top-level `audio_decoder` block carries the audio decode counters for the
whole process, not for one camera. That is a limit of what is knowable, not an
oversight: aiortc builds its audio decoder inside `decoder_worker`, a thread no
`RTCRtpReceiver` keeps a reference to, so a count cannot honestly be attributed
to a camera from there. The per-camera answer is each camera's `audio_codec`.

| Field | Type | Notes |
| --- | --- | --- |
| `decoded_opus` | int | Audio RTP packets, since process start, that were decoded as Opus and delivered |
| `decoded_pcmu` | int | The same for G.711 mu-law |
| `discarded` | int | Packets that reached nobody: neither candidate could read them while the session was choosing a codec, or the committed decoder rejected them afterwards. **`decoded_opus + decoded_pcmu + discarded` is every audio packet the process has seen**, which is what makes a wholesale codec mismatch impossible to miss: a stream being decoded as the wrong codec puts nearly all of its packets here. The decoder's own drop log is once per session by necessity (a per-packet warning on a ~50 packets/s stream is its own outage), and that is exactly how such a mismatch once read like an occasional glitch for two days |
| `sessions_opus` | int | Decoder sessions that committed to Opus. One decoder is built per Janus session per camera, so this climbs by one per camera per ~165 s rotation |
| `sessions_pcmu` | int | The same for mu-law |
| `sniff_packets` | int | How many packets each session samples before committing (a constant, echoed here so the response is self-describing) |
| `opus_confidence` | float | What fraction of that sample libopus must accept for the session to commit to Opus (a constant, echoed for the same reason) |

The `call` section reports the live call: `enabled` (feature armed and a
sub-account present), `active`, the `camera_id` and `since` of a call in
flight, and `last_result` (how the previous call ended, persisted so it
survives the call).

`state: "restart_deferred"` is the most informative value: the camera hit the
`SESSION_WEDGE_TIMEOUT_S` escape hatch, but it has fired too often
lately to be a local wedge, so `restart_guard.py` is holding the process before
it exits.

Building a health check on this: the frame-silence watchdog is **video-only**, so
a camera whose audio has stopped still reports `state: "streaming"` with a fresh
`last_frame_at`, and nothing in this response *reconnects* a camera on dead
audio. What it can tell you is whether audio is flowing: poll
`decoded_audio_frames` twice and compare, and treat `audio_consumed: true` with
`audio_format: null` as "this camera has never decoded a single audio frame" --
which is a state it may legitimately stay in forever, publishing video with
`publisher_audio_format: null`, if the camera really never sends audio.
A camera that decodes far fewer frames than the ~50/s a 20 ms audio stream
implies is decoding the wrong codec rather than hearing a quiet room: check
`audio_codec` and the `audio_decoder` block's `discarded`.
Two fields answer questions no counter can: `audio_dropped: true` is a publisher
that stopped carrying audio (rather than a camera that never sent any), and
`last_publish_at` falling behind `last_frame_at` is a publisher that has stopped
taking video, the case where every other field in this response still reads
healthy.

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
    "since": 1787500350.2,
    "uptime_seconds": 52.5,
    "attempts": 3,
    "succeeded": 1,
    "stopped": 1,
    "failed": 1,
    "rejected_in_progress": 1,
    "failures_by_reason": {"media_error": 1},
    "by_endpoint": {"direct": 2},
    "by_codec": {"mulaw": 2},
    "by_path": {"full_duplex": 2},
    "renegotiations": 0,
    "audio_seconds_played": 8.3,
    "talk_seconds": 7.9,
    "last_success_at": 1787500390.1,
    "last_failure_at": 1787500402.7,
    "last_failure_reason": "media_error",
    "recent": [{"at": 1787500402.7, "camera_id": "<unit-id>-<did>",
                "outcome": "ok", "wall_seconds": 4.89, "played_seconds": 5.0}]
  }
}
```

Notes:

- **In-memory only.** They reset when the bridge restarts, which is what keeps
  them free; the log remains the durable record.
- `since` is the Unix timestamp the counters started at (process start);
  `last_success_at` and `last_failure_at` are Unix timestamps or `null` when no
  talk has yet succeeded/failed.
- `attempts` counts talks that actually started. A request refused because one was
  already running is counted separately as `rejected_in_progress`, not as an
  attempt.
- `audio_seconds_played` is audio actually pushed to a speaker, so a clip stopped
  early contributes what it played, not its full length.
- `failures_by_reason` uses stable labels (`media_error`, `media_too_large`,
  `no_upstream_audio`, `timeout`, `transport_error`, `network_error`,
  `upstream_error`, `cancelled`); never exception text, which could carry a URL
  or token.
- `by_path` counts, per transport, the talks that successfully ran it. Keys are
  the same values the talk response's `path` field reports (`full_duplex`,
  `webrtc`, `srtp`), and a key appears only once that path has run. A talk ended
  early by an explicit stop still counts (the same convention as `by_codec`)
  while failed and rejected talks never do. This is what makes a silent
  degradation visible: e.g. the preferred full-duplex path failing device
  discovery and every talk quietly running the `webrtc`/`srtp` fallback.
- `recent` is the last 20 talks, oldest first. Each entry carries the outcome plus
  `played_seconds`, `seconds`, `renegotiations`, `codec` and `chunks` when the
  talk reported them.
- **The per-talk timing measurements are not aggregated here.** `put_rtt_*`,
  `late_chunks`, `max_late_ms` and `prebuffer_ms` are returned on the talk
  response itself (see
  [`POST …/talk`](#post-controlcamerascamera_idtalk)); these counters stay coarse
  on purpose, for "has this been failing all week?" rather than for tuning.
- `GET /control/status` carries a short summary of the same data alongside the
  camera states.


## `GET /control/cameras`

Returns the bridge's in-memory camera registry, populated at startup and kept
current by the periodic discovery poll (`discover_and_launch_cameras()`, every
`config.CAMERA_DISCOVERY_INTERVAL_S`, 300 s by default). A camera added to the
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

**Path parameter:** `camera_id`. Must exist in the camera registry.

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
camera's speaker via two-way audio (talkdown). The route is **enabled by
default** and returns `403` only when `TALKDOWN_ENABLED` is set false. Nothing
ever speaks on its own: it plays what a caller explicitly asks it to, and nothing
else.

Only one talk may be in flight per camera at a time. A concurrent trigger for
the same camera is rejected with `409`, never queued. See `talk.py` /
`talk_fullduplex.py` for the transport details.

**Path parameter:** `camera_id`. Must exist in the camera registry.

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
200 {
  "status": "spoken",
  "path": "full_duplex",
  "codec": "mulaw",
  "chunks": 251,
  "bytes": 80320,
  "seconds": 10.0,
  "played_seconds": 10.0,
  "renegotiations": 0,
  "reconnects": 0,
  "stopped": false,
  "encode": "streaming",
  "put_rtt_p50_ms": 6.2,
  "put_rtt_p95_ms": 11.8,
  "put_rtt_max_ms": 41.0,
  "put_rtt_stdev_ms": 3.1,
  "late_chunks": 0,
  "max_late_ms": 0.0,
  "prebuffer_ms": 500,
  "cert_pinned": true
}
```

The whole result the talk manager produced is merged into the response, so the
exact key set depends on which transport ran.

| Field | Notes |
| --- | --- |
| `path` | Which upstream-audio transport carried the clip: `full_duplex` (the chunked HTTP PUT the ADC-VDB750 uses), `webrtc`, or `srtp` |
| `codec` | The codec that actually carried it: `mulaw` on the doorbell this was built against |
| `chunks` / `bytes` | Chunks and bytes delivered to the device |
| `seconds` | Length of the audio that was produced by the encoder |
| `played_seconds` | Audio actually pushed to the speaker, lower than `seconds` for a clip that was stopped early |
| `stopped` | `true` when a `talk/stop` ended it; the call still returns `200` with partial stats |
| `renegotiations` / `reconnects` | How many times a fresh device session, or a fresh socket on the same session, was needed mid-clip. Non-zero is not a failure (the stream resumes at the exact byte offset it reached) but a persistently non-zero count says the session or the link is unstable |
| `encode` | `streaming` or `buffered`, i.e. which side of `TALK_STREAMING_ENCODE` the clip ran on |
| `cert_pinned` | Present only when a certificate pin applied and a certificate was seen: `true` when the device's certificate matched the pin, `false` when it did not (in `warn` mode, where the talk proceeds anyway) |

**Timing measurements** (full-duplex path; present whenever at least one chunk
was delivered). They exist so a later tuning of the chunk size or the
jitter-buffer lead has real numbers instead of a guess: the current values were
tuned by ear:

| Field | Notes |
| --- | --- |
| `put_rtt_p50_ms`, `put_rtt_p95_ms`, `put_rtt_max_ms`, `put_rtt_stdev_ms` | Distribution of the per-chunk PUT round trip. Sampling is capped at the first 50 000 chunks (~33 minutes of audio), past which the distribution is long settled |
| `late_chunks` | Chunks whose scheduled slot had already passed when they went out, i.e. round-trip time eating into the jitter-buffer lead. **This is the one that matters**, and it **deliberately excludes the opening prebuffer fill**: those chunks go out back to back *by design*, since that burst is what fills the doorbell's buffer, and counting them would report every healthy talk as a dozen chunks late |
| `max_late_ms` | The worst such overshoot |
| `prebuffer_ms` | The lead actually used. Equal to `TALK_PREBUFFER_MS` unless `TALK_PREBUFFER_ADAPTIVE` is on and raised it |

A healthy talk on the LAN reports single-digit `put_rtt_p50_ms` and
`late_chunks: 0`. Persistent late chunks with a `put_rtt_p95_ms` approaching
`prebuffer_ms` is the shape of audio that will be heard as gaps.

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

The stream ends at the next chunk boundary, at most one chunk of audio (40 ms),
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

Ten routes cover the per-camera **settings** surface (speaker volume, the
doorbell's own chime, TELUS's own push notifications, the status LED, image,
video/clip), read-only **diagnostics** (signal, battery, device status), the
camera **siren**, and **privacy mode**. One settings section (`solar`) is
read-only and has no write route of its own.

They are independent of talkdown: `TALKDOWN_ENABLED` does not gate them and they
never open an audio session. Settings and diagnostics come from the **website
API** (the cookie + `ajaxrequestuniquekey` session the bridge already holds);
siren and privacy come from the **mobile MWS API** (form POST in, JSON envelope
back: same `isSuccess` / `ErrorMessage` fields as the XML actions). See
[alarm-com-api.md](alarm-com-api.md) for the two backends.

> **These write to real hardware.** A `GET` here never changes anything, but a
> `POST` takes effect on a camera in someone's home immediately, and the siren
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
  the *same payload shape* comes back for a device that cannot do the thing, so
  the presence of a field proves nothing. Support is reported upward as a
  `supports_*` boolean on the read routes, and writing an unsupported field is a
  **`422 unsupported`**, not a silently-dropped write.

Errors shared by all ten (individual routes note anything extra):

| Status | Body | Meaning |
| --- | --- | --- |
| `400` | `{"error": "malformed"}` | Body is not valid JSON, carries an unknown key, a value of the wrong type, a number outside the range the device publishes, or a value that is not on the option list the device offers |
| `401` | `{"error": "bad_secret"}` | Missing/wrong `Authorization` header |
| `404` | `{"error": "unknown_camera"}` | `camera_id` not in the camera registry |
| `422` | `{"error": "unsupported"}` | The device reports no support for that field or capability |
| `429` | `{"error": "rate_limited", "retry_after": <seconds>}` | The alarm.com edge rate-limited the upstream call. Carries a `Retry-After` header with the **server's own** wait: honour it (see the shared table above) |
| `502` | `{"error": "telus_error"}` | The upstream TELUS/alarm.com call failed |
| `503` | `{"error": "mws_unavailable"}` | Siren and privacy only: no mobile MWS session exists to act through |

**Out-of-range values are refused, never clamped.** Every numeric field on every
write route is checked against the `*_min` / `*_max` the device itself published
on the read, and every string field against the `*_options` list it published;
anything outside is a **`400 malformed`** and nothing is sent to the camera.

This is a deliberate choice, not an accident of validation. Clamping would mean
writing a *different* value to a camera in someone's home than the caller asked
for and still answering `200`; the caller believes the volume it named is the
volume that landed, and nothing tells it otherwise. Refusing puts the
disagreement in front of whoever caused it. A device that publishes no usable
bounds or no option list is not second-guessed: the value is passed through and
TELUS is the judge.

Note which status this is. **Out of range is a `400`, not a `422`**: the caller
sent a wrong value, which is a caller mistake and worth fixing at the call site.
`422 unsupported` means something else entirely: the device reports it cannot do
that field *at all* (its `*FeatureSupport` enum is not `2`), which no retry with
a different value will fix. Both leave the camera untouched.

---

## `GET /control/cameras/{camera_id}/settings`

Reads one camera's audio, image and video/clip settings, its status LED,
notification and solar-panel sections, and its siren and privacy records: eight
keys. The website settings are uncached: a settings value a UI renders as a
slider must not be stale. Each call is **four website GETs plus at most two
mobile MWS reads** (plus one more MWS read, cached for much longer, for the
chime-type names), which makes it the heaviest read here. The two MWS actions
are account-wide, so their results, failures included, are cached for 30 s
and shared across every camera: a sequential sweep over all cameras pays them
once, not per camera. The Home Assistant side polls this route every 120 s,
and you should not poll it tighter.

`led`, `notifications` and `solar` all come out of the **same** upstream
`generalSettings` document and cost one request between them.

**Path parameter:** `camera_id`. Must exist in the camera registry.

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
  "led": {
    "supports_enabled": true,
    "enabled": true,
    "supports_brightness": false,
    "brightness": 0,
    "raw": {}
  },
  "notifications": {
    "supports_motion": true,
    "motion": true,
    "supports_button_press": true,
    "button_press": true,
    "supports_smart_chime": false,
    "smart_chime": false,
    "supports_format": true,
    "format": 0,
    "format_choices": [
      {"value": 0, "label": "Text Only"},
      {"value": 2, "label": "Attach a Thumbnail"}
    ],
    "raw": {}
  },
  "solar": {
    "supports_connected": false,
    "connected": false,
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
| `call_volume` | int | Current speaker/call volume; **this is the volume talkdown plays at** |
| `call_volume_min` / `call_volume_max` | int | Device-reported range; `0`–`10` on the doorbell this was built against |
| `mic_and_speaker_on` | bool | The device's mic + speaker master toggle |
| `supports_indoor_chime` | bool | Whether the indoor chime can be turned on/off |
| `indoor_chime_on` | bool | Indoor chime state |
| `supports_chime_type` | bool | Whether the **wired chime type** is selectable: its own upstream enum (`indoorChimeTypeFeatureSupport`), not the indoor chime's on/off flag |
| `chime_type` | int \| null | Selected wired chime type. **Not a ringtone**: it is the kind of physical chime the doorbell is wired to |
| `chime_type_options` | list | The bare integer values this device offers, e.g. `[-1, 0, 1]` |
| `chime_type_choices` | list | The same options **with names**: `{"value": int, "label": str, "disables_indoor_chime": bool}`. May be absent or `[]`; see below |
| `supports_outdoor_tone` | bool | Whether the doorbell's own outdoor tone can be turned on/off |
| `outdoor_tone_on` | bool | Outdoor tone state |
| `supports_outdoor_tone_volume` | bool | Whether that tone's **volume** is settable: a **separate** flag, and `false` on the reference doorbell even though the tone itself toggles fine |
| `outdoor_tone_volume` | int \| null | **`null` when `supports_outdoor_tone_volume` is false.** The website reports `0` there for a tone that is audibly on, while the mobile API reports level 3 for the same setting on the same device; it is a placeholder for a field the website does not manage, not a reading. The min/max still come through for a model that can set it |
| `outdoor_tone_volume_min` / `outdoor_tone_volume_max` | int | Device-reported range |
| `raw` | object | The untouched upstream settings object, so a caller can read a field the bridge does not model yet |

Two traps in this object. The upstream enum names are
`<thing>EnableDisableFeatureSupport`, not the `is<Thing>OnFeatureSupport` spelling
you would guess: reading the raw document with the guessed name silently finds
nothing and looks like "unsupported". And the outdoor tone's on/off support and
its **volume** support are genuinely different flags: on the reference doorbell
the tone can be switched but its volume cannot, so a `chime` write carrying
`outdoor_volume` is a `422 unsupported` there while `outdoor_on` succeeds.

The talk/call volume and the outdoor tone volume are also different things.
**Only `call_volume` affects talkdown.**

### `chime_type_choices`: names for the chime types

`chime_type_options` is bare integers, because that is all the website API
publishes; the alarm.com web portal names them with a client-side enum. The
bridge instead asks the **mobile** API (`GetCurrentDoorbellSettings`), which
returns the names and, more usefully, a per-option `disables_indoor_chime` flag,
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
  to `chime_type_options` and show the numbers; losing the labels must not lose
  the control.
- **Honour `disables_indoor_chime`.** When the selected type carries it, the
  indoor chime toggle is meaningless (there is no wired chime to ring) and the
  alarm.com portal disables it. Do not hardcode `-1` for this; the flag exists so
  clients do not have to.

> **Selecting the "none" type turns the indoor chime OFF upstream, and switching
> back does not turn it on again.** Observed live: setting `chime_type` to `-1`
> left `indoor_chime_on` false, and a later `chime_type: 0` restored the type but
> not the toggle. TELUS does this, not the bridge: the write carries only the
> field asked for. It follows from the same coupling `disables_indoor_chime`
> describes, but it is a lasting state change rather than a display rule, so a
> caller that offers the "none" option should expect to send `indoor_on: true`
> afterwards if it wants the chime back.

The option list is a property of the hardware, so the bridge caches it for an
hour rather than re-reading it every poll. The current *selection* is not cached;
it comes from the website settings in the same response.

`image` and `video` follow the same convention: one key per field the write
routes accept, each paired with a `supports_<field>` boolean off the matching
`*FeatureSupport` enum, plus `*_min`/`*_max`/`*_options` where the device reports
a range or a list, plus `raw`. **Which fields appear is device-dependent**: the
example above is a doorbell; a camera without IR will still return the IR keys,
with `supports_*` false. Always branch on `supports_*`, never on key presence.

### `live_resolution`: the one video field that is not about clips

The `video` object's fields are clip/recording settings with one exception:
`live_resolution` is the **live stream's** resolution, read from a different
relationship of the same upstream document.

| Field | Type | Notes |
| --- | --- | --- |
| `supports_live_resolution` | bool | True when the camera offers a choice; see below |
| `live_resolution` | str | e.g. `"1440x1440"` |
| `live_resolution_options` | list | The settable values |
| `live_resolution_choices` | list | `{"value": str, "label": str}` using the vendor's own labels, e.g. `"Full High Definition (1440x1440)"` |

**This changes what the bridge itself restreams.** That is not obvious: the
bridge fetches its stream from an endpoint named `liveVideoHighestResSources`,
which reads as though it would ignore the setting. It does not. Verified live:
selecting `480x480` and renegotiating took the bridge's own stream from
`1440x1440@15fps` to `480x480@15fps`. "Highest available" means highest *under
the current selection*.

So it is the control for a camera's bandwidth, and equally a way to degrade every
view of that camera by accident. The change reaches an already-running stream
only when that stream renegotiates.

Support is derived from the option object itself: an **empty** `webOptions` is
how this API says "no choice offered" (it is exactly what `mobileOptions` is on
the reference doorbell), and `isWebOptionsDisabled` is a separate server-side
veto. Only the **keys** of `webOptions` are settable; the labels are not values,
unlike the ambiguous `<field>Options` objects elsewhere in this API.

### The `led` object: the status indicator light

Read from `generalSettings`, the same document that carries the `notifications`
and `solar` objects below; a write to any of them resends the other two
untouched.

| Field | Type | Notes |
| --- | --- | --- |
| `supports_enabled` | bool | From `canTurnOnOffLedLight` |
| `enabled` | bool | Whether the light is on |
| `supports_brightness` | bool | From `canChangeLedBrightness`: a **separate** flag, and `false` on the reference doorbell, which can switch its LED but not dim it |
| `brightness` | int | Current brightness. `brightness_min` / `brightness_max` appear only if the device publishes them; the reference doorbell publishes neither |
| `raw` | object | The untouched `ledLightSetting` document |

**This is the status indicator light, not the infrared illuminator.** The IR LEDs
are `ir_led_enabled` in the `image` object, a different field on a different
endpoint. alarm.com's own interface calls this one "Camera Status LED".

### The `notifications` object: TELUS's own push notifications

**These are the notifications alarm.com sends to its own mobile app, not
anything Home Assistant does.** Turning one off stops the TELUS app notifying;
it has no effect on automations, events or notifications on this side. The usual
reason to want them is to stop being told twice about the same doorbell press.

Read from the same `generalSettings` document as the `led` object; the settings
bundle fetches it once and splits it into the two sections.

| Field | Type | Notes |
| --- | --- | --- |
| `supports_motion` | bool | Whether a motion-notification toggle should be offered |
| `motion` | bool | Notify on motion |
| `supports_button_press` | bool | Whether a button-press toggle should be offered |
| `button_press` | bool | Notify when the doorbell button is pressed |
| `supports_smart_chime` | bool | `false` on the reference doorbell |
| `smart_chime` | bool | Notify an alarm.com Smart Chime |
| `supports_format` | bool | Whether the notification format may be set. Shares the panel-notification capability with motion and button-press |
| `format` | int \| null | The current notification format. `null` when the camera reports none |
| `format_choices` | list | `{"value": int, "label": str}` for each format that may be written; always present, see below |
| `raw` | object | The untouched `doorbellNotificationSetting` document |

**The three `supports_*` booleans are not one upstream flag each**, which is the
trap if you go to the raw document. Motion and button-press share
`panelNotificationEnableDisableFeatureSupport` (the portal calls them "panel
notification toggles" and shows both off that one enum), the smart chime has its
own `smartChimeNotificationEnableDisableFeatureSupport`, and motion carries a
*second* condition: the portal renders its row only when
`isMotionNotificationEnabled` is true, so that is a per-device availability flag
rather than a toggle. The bridge collapses all of it into the three booleans:
use them and don't re-derive the rules.

A camera that is not a doorbell reports every flag false and gets no controls at
all. That is the expected answer, not a failure.

#### The notification format, and why only two of its four values are offered

`notificationFormat` selects how much TELUS puts in a push notification. The
upstream enum is:

| Value | Member | Offered? |
| --- | --- | --- |
| `-1` | `Unknown` | No: a null state, reported by a camera with no doorbell notifications |
| `0` | `TextOnly` | **Yes**: "Text Only" |
| `1` | `AttachVideoClip` | No: legacy, see below |
| `2` | `AttachThumbnail` | **Yes**: "Attach a Thumbnail" |

Those numbers are not inferred. They were read out of the alarm.com web portal's
own JavaScript (webpack module `288086`), which is a public static asset; no
account, no session and no capture of a logged-in page was involved. The labels
are the portal's own English strings, so what a caller displays matches what the
TELUS website shows.

**`AttachVideoClip` is deliberately withheld.** The portal builds its own
dropdown from `TextOnly` and `AttachThumbnail` alone, and elsewhere in the same
bundle it rewrites `AttachVideoClip` to `AttachThumbnail` whenever it reads one,
so it is a value alarm.com itself treats as stale. `Unknown` is a state, not a
choice.

The device publishes no option list for this field, so **the allowed set is
enforced by this bridge or by nothing**: `camera_settings` refuses anything
outside `format_choices` before the read-modify-write touches the document.
`format_choices` is a constant rather than an upstream read, so unlike
`chime_type_choices` it is always present when the feature is supported.

### The `solar` object: is a solar panel attached

Read from the same `generalSettings` document as the LED and the notification
toggles, at no extra upstream cost. **Read-only: there is no write route**, and
that is deliberate: whether a solar panel is bolted to a camera is a fact about
the installation, and the two things that could change it are someone fitting a
panel and someone removing one.

| Field | Type | Notes |
| --- | --- | --- |
| `supports_connected` | bool | From `connectedToSolarPanelFeatureSupport`: whether this device has anything to say about a solar panel at all |
| `connected` | bool | From `isConnectedToSolarPanel`. Only meaningful when `supports_connected` is true |
| `raw` | object | The untouched `solarPanelSetting` document |

**Expect `supports_connected: false` on any camera that is not a battery+solar
model**; it is false on both cameras this was built against. That is the correct
outcome rather than a gap: `connected` is false there too, and a camera with no
solar hardware reporting a permanent "no solar panel" is noise dressed up as
diagnostics. A caller should create nothing for a camera that reports the feature
unsupported.

One nuance worth knowing if you read `raw`: elsewhere in this API a
`*FeatureSupport` of `2` means "supported **and** editable", and nothing
establishes that a camera reporting `2` here would accept a write; the field
reads like a statement about hardware rather than a preference. Since nothing
writes it, the bridge uses the enum only as "does this device answer the
question", which is the weaker claim and the one that fails closed.

### The `siren` and `privacy` sections: a fixed contract

`audio`, `image` and `video` come from the website API and vary by device model.
`siren` and `privacy` do not: they come from the **mobile MWS** actions
`GetAllSirenDevices` and `GetAllCameraPrivacyModeInfo`, whose raw records use
inconsistent, capture-derived key spellings (`canManuallyTriggerSiren` vs
`can_manually_trigger_siren`, `SupportsPrivacyMode` vs `supports_privacy_mode`).
The bridge **normalizes** each record into a fixed, small shape before returning
it, and that shape is a contract other code depends on: the Home Assistant
entities read exactly these key names and nothing else.

**`settings["siren"]`**: an object, or `null`:

| Field | Type | Meaning |
| --- | --- | --- |
| `can_manually_trigger` | bool | Whether **this account** may sound this camera's siren. A per-account permission, not a hardware capability: a camera with a siren this account may not fire reports `false`. Gate the control on this |
| `active` | bool | Whether the siren is sounding right now |
| `timestamp_end` | value \| `null` | The server-chosen end of the current sounding, as the upstream reports it. `null` when nothing is running (or when upstream sent none). The duration is not the caller's to choose |

**`settings["privacy"]`**: an object, or `null`:

| Field | Type | Meaning |
| --- | --- | --- |
| `supported` | bool | Whether the camera reports privacy-mode support. `false` for every camera on the account this was built against |
| `active` | bool | Whether privacy mode is on (camera blinded) |

Every field is a real JSON `bool`, always present, whatever the upstream sent:
the MWS side spells booleans as the strings `"true"`/`"false"` and privacy state
as `0`/`1`, and normalizing that here is the point of the contract (a truthiness
test on the string `"false"` is `True`).

The two capability flags default in **opposite** directions, on purpose:

- `can_manually_trigger` fails **closed**: absent, null or unrecognised becomes
  `false`. It is the permission that sounds a real siren in someone's home, so a
  record the bridge cannot read is never taken as consent.
- `supported` fails **open**: only an explicit `false` reports unsupported. That
  action returns a privacy *state* object rather than an enumeration of capable
  devices, so a missing flag is a gap in a shape recovered from the decompiled
  app, not the device saying no; failing closed would leave the privacy control
  permanently unavailable on every camera.

`active` fails closed on both: an unreadable value reports "not sounding" /
"not blinded", because claiming a camera is blinded while it is still recording
is the dangerous direction to be wrong in.

**`null` is not "unsupported".** Either section is `null` when the mobile session
was unavailable (no talk manager (`TALKDOWN_ENABLED=false`), a failed MWS read,
a token that would not mint) or when the MWS response carried no record
matching this camera. All of those mean *nothing was reported*, and none of them
means the camera cannot do the thing.

Both sections are best effort by design: an MWS outage degrades them to `null`
and still returns a `200` with the website settings intact, rather than failing
the whole read with a `502`. A caller must therefore distinguish three states:
`null` (unknown), `{"can_manually_trigger": false}` / `{"supported": false}`
(reported unsupported), and a positive flag, and must not collapse the first
two. Treating `null` as `false` turns a transient MWS blip into "this camera has
no siren"; treating it as `true` offers a control that will fail.

Because these are up to two extra MWS POSTs on top of four website GETs, this
route is the most expensive read in the API. Poll it slowly. The MWS halves are
account-wide and cached for 30 s across all cameras (failures included), so a
sweep over several cameras pays them once.

**Errors:** `401 bad_secret`, `404 unknown_camera`, `429 rate_limited`,
`502 telus_error`. Note there is **no `503` here**, unlike the siren and
privacy *write* routes, a missing mobile session degrades this read to `null`
sections instead of failing it. The `429` comes from the **website** half: a
rate-limited website GET fails the read with the server's `Retry-After`, while a
rate-limited MWS read is absorbed like any other MWS failure: the negative
result is held for the server's wait and the siren/privacy sections degrade to
`null`.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/settings
```

---

## `GET /control/cameras/{camera_id}/diagnostics`

Read-only health for one camera: signal strength, battery, and the device status
the settings screen shows. A camera degrading to poor Wi-Fi is otherwise a silent
failure (the picture just gets worse) so this exists to make it a number
something can alert on.

**Path parameter:** `camera_id`. Must exist in the camera registry.

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
| `signal_strength_key` | str \| null | WHICH upstream key the reading came from. `normalizedSignalStrength` is a 0–100 score; the other spellings, kept for camera models this was never tested against, may be real dBm. A caller must label the first and only the first; Home Assistant records long-term statistics against the declared unit, so a wrong one poisons history rather than just the display |
| `signal_strength` | number \| null | alarm.com's `normalizedSignalStrength`: a **0–100 score, not dBm** (see below). `null` when the device reports none, typically a wired camera |
| `battery` | number \| null | Battery percentage, `null` on a camera with no battery |
| `device_status` | string \| null | Upstream status string |
| `raw` | object | The three upstream responses as received, under `signal_strength` / `battery` / `device_status` keys, for anything not modelled above |

**`signal_strength` is not dBm.** The signal document reports
`normalizedSignalStrength` (0–100, the bars the portal draws) and a coarse
`signalStrengthEnum`; it does **not** report `signalStrength` or `rssi`. Anything
consuming this must not label it dBm: a real Wi-Fi dBm figure is negative, so a
`58` presented as dBm is nonsense, and in Home Assistant a wrong unit is recorded
into long-term statistics rather than just shown. The bridge does fall back to
`signalStrength` / `signalStrengthValue` for camera models that report those
instead, and their unit is **unverified**, which is why the Home Assistant
sensor asserts no unit at all.

**`battery` is `null` for "no battery", not `0`.** `batteryLevel` is present and
`0` on mains-powered devices, so the value alone cannot tell a missing battery
from a flat one. The bridge honours alarm.com's own `shouldDisplayBatteryLevel`
flag and reports `null` when it is false: build no entity for a `null`, or you
get a permanent 0%.

**`raw` carries the camera's Wi-Fi SSID and encryption type.** That is diagnostic
detail about someone's home network. Do not log it or surface it as an entity
attribute without deciding to do so on purpose.

Every field is nullable on purpose: three separate upstream reads back this
response, and a camera that reports none of them is normal, not an error. A
caller must not create a sensor for a `null` field: an "unknown" battery sensor
on a wired camera is noise, not diagnostics.

**A healthy response can take tens of seconds.** The three upstream reads run
sequentially, and alarm.com appears to answer the signal read by querying the
camera live, measured on one account at 1.6s for a doorbell and **25s** for an
indoor camera. The bridge allows each read up to 35s
(`DIAGNOSTICS_READ_TIMEOUT_S`, see [configuration.md](configuration.md)) instead
of its usual 15s upstream timeout, so a caller's own client timeout must sit
well above that: the Home Assistant integration budgets 45s for this route. A
short client timeout abandons responses the bridge would have delivered.

**Errors:** `401 bad_secret`, `404 unknown_camera`, `429 rate_limited`,
`502 telus_error`.

**Example**

```bash
curl -s -H "Authorization: Bearer $SECRET" \
  $BASE/control/cameras/<camera_id>/diagnostics
```

---

## `POST /control/call/test`

Places one outbound test call through the configured VoIP.ms sub-account:
rings the target, plays a ~10 s tone on answer, hangs up, and returns a
timeline of the SIP/RTP milestones. Exists to prove the call leg (INVITE,
digest auth, SDP, NAT, two-way RTP) independently of any doorbell event; the
doorbell splice builds on it (backlog E99).

    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

Body (optional): `{"target": "<number>"}` -- overrides the configured
`DOORBELL_CALL_TARGET` for this one call.

Responses:

      200 {"result": "completed", "frames_sent": n, "rtp_received": n,
           "timeline": [...]}          -- the call was answered and torn down
      200 {"result": "rejected", "status": n, "reason": "...", ...}
                                       -- the POP refused it (bad auth, bad number)
      200 {"result": "timeout", ...}   -- nothing answered inside CALL_TIMEOUT_S
      400 {"error": "no_target"}       -- no target configured or supplied
      403 {"error": "call_disabled"}   -- no VoIP.ms sub-account in the env
      409 {"error": "call_in_progress"} -- one call at a time
      502 {"error": "call_failed"}     -- unexpected local failure (logged)

The route holds the request open for the life of the call (ring time plus the
tone), so give the HTTP client a generous timeout.

## `POST /control/call/doorbell`

Starts a LIVE bridged call: the bridge dials the configured number and, on
answer, connects the doorbell's microphone to the caller and the caller's
voice to the doorbell speaker. While the call runs, the called camera's
always-on video pipeline is paused and its NVR feed gaps (see
`CALL_PAUSE_VIDEO_DURING_CALL`); it resumes automatically when the call ends. Returns immediately; the call runs in the
background and its progress is the `call` section of `/control/status`.

    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

Body: `{"camera_id": "<id>", "target": "<number>"}`. `target` is optional and
defaults to the configured `DOORBELL_CALL_TARGET`.

Responses: `200 {"status": "started", "camera_id": ...}`;
`403 {"error": "call_disabled"}` (feature off or no sub-account);
`404 {"error": "unknown_camera"}`; `400 {"error": "no_target"}`;
`409 {"error": "call_in_progress"}`.

When the call ends, the `call.last_result` object in `/control/status` carries
how it went: `result`/`reason`/`seconds` and the SIP leg's `frames_sent` /
`rtp_received` (20 ms frames each way); `uplink_frames` (RTSP audio frames
received from the doorbell mic across the call window);
`uplink_dropped_bytes` and `downlink_dropped_bytes` (mu-law shed at each
direction's latency cap — see `CALL_UPLINK_BUFFER_MS` /
`CALL_DOWNLINK_BUFFER_MS`); `uplink_ducked_ms` (how long the echo ducker
held the uplink down while the caller spoke); and `talk`, the talkdown leg's own stats
(`chunks`, `seconds`, `played_seconds`, `renegotiations`, `reconnects`,
`put_rtt_p50_ms`, `put_rtt_p95_ms`, `put_rtt_max_ms`, `late_chunks`). A
healthy call shows dropped bytes near zero in both directions and
`played_seconds` ≈ the call's `seconds`; a rising `put_rtt_p95_ms` with
`downlink_dropped_bytes` growing means the device was acking slowly and the
batched drain (`CALL_MAX_PUT_MS`) was riding through it.

## `POST /control/call/stop`

Ends the live call in progress (the doorbell hangs up at its next frame slot
and the talkdown stops). `200 {"stopped": true|false}` -- false when there was
no call to stop.

## `POST /control/call/settings`

Live-applies the Home-Assistant-managed call settings, mirroring
`/control/credentials`: HA writes `call_settings.json` to the shared dir first
(the restart-surviving copy), then pushes the same payload here (the no-restart
apply). A missing key leaves the current value alone; a present key applies
verbatim, including an empty string to clear it.

    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

Body (all keys optional): `enabled` (bool), `sip_server`, `sip_account`,
`sip_password`, `did`, `call_target` (strings). Returns
`200 {"status": "applied", "fields": [names applied]}` -- names only, never
values, since one is a password.

## `POST /control/cameras/{camera_id}/settings/volume`

Sets the camera's speaker/call volume: the level talkdown is heard at. The
mobile API's `<duav>` element claims `SupportsVolumeAdjustment="false"` for this
doorbell; the website API exposes it as supported with a live 0–10 sweep
observed, so this route trusts the website's `callVolumeFeatureSupport` and
ignores the mobile claim.

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `volume` | int | yes | Must be within the device's own `call_volume_min`–`call_volume_max` (0–10 on the doorbell tested). Anything outside that range is **refused with a `400 malformed`**, never clamped, so the camera is never quietly set to a volume the caller did not ask for. A non-integer (including `true`) is also a `400` |

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
independent: send only what you want changed.

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `indoor_on` | bool | no | Indoor chime on/off |
| `chime_type` | int | no | The **wired chime type**. Must be one of `chime_type_options`. Off-list is a **`400 malformed`** (the field is supported, the value just is not offered), while a device that cannot select a type at all is a `422`. Gated on `supports_chime_type`, a *different* flag from `supports_indoor_chime` |
| `outdoor_on` | bool | no | The doorbell's own outdoor tone, on/off |
| `outdoor_volume` | int | no | Must be within `outdoor_tone_volume_min`–`outdoor_tone_volume_max` (0–3 on the doorbell tested). Outside that range is a **`400 malformed`**, not a clamp; same rule as `volume` above. Gated on `supports_outdoor_tone_volume`, which is a *different* flag from `supports_outdoor_tone` and is `false` on the reference doorbell: `outdoor_on` works there, `outdoor_volume` is a `422` |

An empty body (no recognised field at all) is a `400 malformed` rather than a
no-op write, so a caller with a typo'd key hears about it instead of believing a
setting was applied.

**Success response:** the refreshed `audio` object (same shape as `GET
.../settings`'s `audio`).

**Errors:** the shared table. `422 unsupported` names the requested field the
device does not support (`supports_indoor_chime` / `supports_chime_type` /
`supports_outdoor_tone` / `supports_outdoor_tone_volume`: four separate flags). All
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

## `POST /control/cameras/{camera_id}/settings/notifications`

Turns TELUS's own push notifications on or off for one camera. All fields are
optional and independent: send only what you want changed.

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `motion` | bool | no | Gated on `supports_motion` |
| `button_press` | bool | no | Gated on `supports_button_press` |
| `smart_chime` | bool | no | Gated on `supports_smart_chime`, `false` on the reference doorbell; a `422` there |
| `format` | int | no | Must be one of the values in `format_choices` (`0` or `2`). Anything else (including the upstream enum's other two members, `-1` and `1`) is a **`400 malformed`**: the field is supported, the value just is not offered. Gated on `supports_format` |

An empty body is a `400 malformed`.

**Success response:** the refreshed `notifications` object.

**Errors:** the shared table. Every requested field is support-checked **before
anything is written**.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"button_press": false}' \
  $BASE/control/cameras/<camera_id>/settings/notifications
```

---

## `GET /control/partitions`

Lists the alarm partitions with the two flags that decide whether a bypass is
allowed right now.

```json
{"partitions": [
  {"id": "<unit>-127", "partition_id": 1, "description": "Panel",
   "state": 1, "is_disarmed": true,
   "has_permission_to_change_state": true,
   "can_bypass_when_armed": false,
   "has_open_bypassable_sensors": false,
   "raw": {}}
]}
```

Both gate flags default to the **refusing** value when the partition does not
report them: an unknown permission is not permission, and an unknown "may I
bypass while armed" is not a yes.

---

## `POST /control/partitions/{partition_id}/bypass`

**A bypassed sensor does not trigger the alarm until it is un-bypassed.**

`partition_id` is the partition's alarm.com device id (`<unit>-127`), not the
`partition_id: 1` ordinal.

**Request body:** `{"bypass": [<sensor id>, ...], "unbypass": [...]}`, both
lists optional, at least one non-empty, else `400 malformed`. A sensor id
containing the `|` separator is refused: the wire format is pipe-separated, so
one would split server-side and bypass a sensor nobody named.

The partition is **re-read before every write** and the operation refused with
`422 unsupported` when it says no: no permission, or armed on a panel whose
`canBypassSensorWhenArmed` is false. Discovering that a panel forbade the write
by having already attempted it is not an acceptable order of operations.

**Success response:** `{"status": "applied", "partition": { … }}`.

> **The write takes about ten seconds to reach the sensor.** Verified live:
> `isBypassed` was still false 5s after a 200 and true at 10s. The response says
> nothing about which sensors ended up bypassed (this endpoint never does) so a
> caller that re-reads immediately will see the old state and conclude nothing
> happened. Wait past that window before reading back.

This is **not** the integration's `force_bypass` arming option and must never
become an automatic second one; see `partitions.py`.

---

## `GET /control/arming-devices`

Lists the **Bluetooth arming devices** on the account: paired phones that may
disarm the panel when they come within range.

**Response**

```json
{"devices": [
  {"id": "<unit>-<did>",
   "description": "Someone's iPhone",
   "disarming_enabled": true,
   "enrolled_partition_id": "1",
   "enrolled_partition_choices": [
     {"value": "1", "label": "Home"}
   ],
   "supports_disarming_toggle": true,
   "supports_enrolled_partition": true,
   "raw": {}}
]}
```

| Field | Type | Notes |
| --- | --- | --- |
| `id` | string | The alarm.com device id of the paired phone |
| `description` | string | The phone's name as its owner typed it, routinely a real person's name, so treat it as text: never parsed, sliced, or used to build an identifier |
| `disarming_enabled` | bool | Whether this phone disarms the panel on approach |
| `enrolled_partition_id` | string | The partition it disarms, as the panel's own partition **number** (`"1"`); see the warning below |
| `enrolled_partition_choices` | list | `{"value": "<partition number>", "label": "<partition description>"}` for every partition on the account, so a client can build a picker without a second read. `[]` when the partition list could not be read |
| `supports_disarming_toggle` | bool | Whether the disarming toggle may be written |
| `supports_enrolled_partition` | bool | Whether the enrolled partition may be written |
| `raw` | object | The untouched upstream record |

**`enrolled_partition_id` is the partition NUMBER, not a device id.** The same
partition also has an alarm.com device id of the form `<unit>-127`, which is
what `GET /control/partitions` reports as `id` and what the alarm panel entity
uses as its unique id, and posting *that* to the write route below is a `400`.
Match against `partition_id` from `/control/partitions`, as strings.

`enrolled_partition_choices` is stamped onto **every** response that carries an
arming device, the list route and both write routes alike, because the record is
a single contract. A client that renders a picker from the choices, writes, and
then adopts the echoed record must not have the choices vanish underneath it.

Both `supports_*` flags are assembled rather than read from a single flag: this
endpoint publishes no `*FeatureSupport` enum and no `canSetX`, and the portal
renders its controls unconditionally. Each combines `canBeSaved` (which does
discriminate: true on these devices, false on plain mobile devices in the same
catalog), `canAccessWebSettings` (the bridge is a website client), and *its own*
field being present with a usable type. They are separate flags because a record
can be missing one field and not the other, and gating the partition control on
the disarming field would offer it on the strength of something unrelated.

`supports_enrolled_partition` is deliberately **not** gated on the account having
more than one partition. That is a fact about the account, not about this
record's writability, but it is a real consideration for whoever builds a
control, and the Home Assistant side acts on it (see
[ha-integration.md](ha-integration.md#the-entities)).

**Errors:** the shared table, minus `400`/`422`: this route takes no caller
input, so an upstream problem is a `502` rather than "you asked wrong".

---

## `POST /control/arming-devices/{device_id}/disarming`

**This decides whether a phone walking up to the house disarms the alarm.**
Treat it with the caution the siren route gets: it is never called except by an
explicit user action.

**Request body:** `{"enabled": <bool>}`; a non-bool is a `400`.

Three refusals, cheapest first: a bad body is a `400` before any upstream call; a
device id absent from the catalog is a `404`, so a typo can never become a blind
`PUT` at an arbitrary device; and a device that does not report the capability is
a `422`.

**Success response:** the same object `GET /control/arming-devices` returns for
that device.

**Errors:** the shared table.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}' \
  $BASE/control/arming-devices/<device_id>/disarming
```

---

## `POST /control/arming-devices/{device_id}/enrolled-partition`

Moves one paired phone to a different partition: **which** partition it disarms,
the same class of setting as whether it may disarm at all.

**Request body:** `{"partition_id": "<partition number>"}`; a non-empty JSON
**string** carrying the panel's own partition number (`"1"`, `"2"`), exactly as
the arming-device record reports it. It is never coerced: it goes back onto a
whole-record `PUT` as it arrived, and `str(1)` would change its type on the wire
without anyone noticing.

Four refusals, cheapest first:

| Situation | Status |
| --- | --- |
| Body is not a non-empty JSON string | `400 malformed` |
| Device id is not in the catalog | `404 unknown_device` |
| The record's own `supports_enrolled_partition` is false | `422 unsupported` |
| The partition is not one this account has | `400 malformed` |

That last gate **fails closed**: the partition must appear in the account's own
partition list, and if that list cannot be read the write does **not** happen
(`502`) rather than proceeding unverified. A `PUT` here replaces the whole
record, and `enrolledPartitionId` is a bare panel-side number with no way to tell
a valid one from a typo by looking at it, so writing an id nothing confirmed
exists is the mistake this route is built to be incapable of making.

**Success response:** the same object `GET /control/arming-devices` returns for
that device, `enrolled_partition_choices` included.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"partition_id": "2"}' \
  $BASE/control/arming-devices/<device_id>/enrolled-partition
```

---

## `DELETE /control/arming-devices/{device_id}`

Unpairs one phone from the panel's Bluetooth disarming. **This cannot be undone
from this side**: re-pairing needs physical access to both the phone and the
panel. There is no cloud-side undo and nothing the bridge can call to reverse it.

One device per call. The vendor portal also offers a delete-all for the whole
account; it is deliberately not implemented, because one mis-aimed call would
unpair every phone in the household at once.

Membership is checked against the device catalog first, so an id the catalog does
not know is a `404` rather than a `DELETE` aimed at whatever that id happens to
address.

**Success response**

```json
200 {"status": "unpaired", "unpaired": "<unit>-<did>", "description": "Someone's iPhone"}
```

The description is read **before** the delete, so the reply can name the phone
that was removed; afterwards it is no longer knowable.

**Errors:** the shared table, plus `404 {"error": "unknown_device"}`.

```bash
curl -s -X DELETE -H "Authorization: Bearer $SECRET" \
  $BASE/control/arming-devices/<device_id>
```

---

## `GET /control/bluetooth-disarming`

The **panel-wide** Bluetooth disarming settings: whether the panel accepts
Bluetooth pairing at all, and the disarm window. System-wide, one document per
account, addressed by the session's own system id.

**Response**

```json
200 {
  "id": "<system-id>",
  "pairing_enabled": true,
  "supports_pairing_enabled": true,
  "timeout": 5,
  "supports_timeout": true,
  "timeout_unit": "minutes",
  "timeout_options": [1, 5, 10, 20, 30],
  "enrolled_device_ids": ["<unit>-<did>"],
  "raw": {}
}
```

| Field | Type | Notes |
| --- | --- | --- |
| `pairing_enabled` | bool | Whether the panel accepts Bluetooth pairing, i.e. whether a phone that is not already enrolled can be added |
| `timeout` | int \| null | The disarm window, in the unit below |
| `timeout_unit` | string | `"minutes"` |
| `timeout_options` | list | The five windows the vendor offers: `[1, 5, 10, 20, 30]` |
| `supports_pairing_enabled` / `supports_timeout` | bool | See the caveat below: a weaker claim than the per-device flags |
| `enrolled_device_ids` | list | The phones the panel says are paired, as ids. The same set `GET /control/arming-devices` discovers, reported here so a caller can tell "pairing is configured" from "the document exists but nothing is paired" without a second read |
| `raw` | object | The untouched flat document |

**The unit and the option set do not come from the payload.** The upstream
document publishes neither: it carries a bare `timeout` with no unit, no bounds
and no options. Both come from the vendor portal's own picker, which builds its
five entries by rendering `[1, 5, 10, 20, 30]` through a "minutes" string, which
is exactly why the bridge reports them, since no client could derive them from
the document.

**The `supports_*` flags here are weaker than the per-device ones.** This
document publishes no capability flag of any kind: its whole attribute set is
`deviceId`, `isBluetoothPairingEnabledForSystem` and `timeout`, so rather than
invent a gate, the bridge reports the only signal there is: the field is present
and holds a value of the expected type. It fails closed for a panel with no
Bluetooth support and for a payload shape that has changed, but it does **not**
claim the panel will accept a write.

**This is not the capability gate for the per-device disarming toggle**, and must
not be used as one. The portal's per-device settings page never consults this
document, and **whether the panel firmware treats system-wide `pairing_enabled:
false` as a veto over a phone whose own flag is `true` is not known.** "The
portal does not read it" is evidence about the portal, not about the firmware.
Treat the two as independent, and do not tell a user that turning one off
disables the other.

**Errors:** the shared table, minus `400`/`422`: this route takes no caller
input, so an upstream problem is a `502`.

---

## `POST /control/bluetooth-disarming`

Changes the panel-wide settings above.

> **The write shape this reaches is INFERRED, not captured.** No `PUT` to this
> upstream endpoint was ever observed. What the bridge sends is the convention
> this API uses everywhere else (the flat whole-document body proven against the
> per-device endpoint) applied to the one captured `GET`. Two parts of it in
> particular are reconstructions rather than observations: how the to-many
> `enrolledDevices` relationship is resent, and the pluralized resource type. If
> a write here fails, suspect the reconstruction at least as readily as the panel.

**Request body:** any non-empty subset of

| Field | Type | Notes |
| --- | --- | --- |
| `pairing_enabled` | bool | A real JSON boolean |
| `timeout` | int | Must be one of `timeout_options`: `1`, `5`, `10`, `20` or `30` minutes |

An unknown key, a wrong type, or a boolean where an int is wanted is a `400`. The
bool-for-int check is not pedantry: `bool` is an `int` subclass in Python, so
`{"timeout": true}` would otherwise be written to a real panel as `1`.

`timeout` is range-checked against the offered set rather than against invented
bounds, because unlike a range the set is *known*: the panel is never asked to
reject a value the vendor never offers.

Both fields are checked against a freshly-read document before anything is
mutated, so a partly-unsupported request leaves the panel untouched rather than
half-applied. A field the panel does not report is a `422 unsupported`.

Unlike the per-device write there is **no membership pre-read**: the document is
the session's own system, addressed by its `Context-Id`, so there is no id for a
caller to have got wrong.

**Success response:** the same shape `GET /control/bluetooth-disarming` returns,
refreshed after the write, so a client can adopt it instead of polling.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"timeout": 10}' \
  $BASE/control/bluetooth-disarming
```

---

## `POST /control/cameras/{camera_id}/settings/led`

Turns the device's status indicator light on or off, or sets its brightness.
Both fields are optional and independent: send only what you want changed.

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `enabled` | bool | no | The light on/off. Gated on `supports_enabled` |
| `brightness` | int | no | Gated on `supports_brightness`, which is a *different* flag and is `false` on the reference doorbell: `enabled` works there, `brightness` is a `422`. Bounded by `brightness_min`/`brightness_max` when the device publishes them; out of range is a `400`, never a clamp |

An empty body (no recognised field) is a `400 malformed` rather than a no-op.

**Success response:** the refreshed `led` object (same shape as `GET
.../settings`'s `led`).

**Errors:** the shared table. Both fields are support-checked **before anything
is written**, so a partly-unsupported request leaves the device untouched.

**Example**

```bash
curl -s -X POST -H "Authorization: Bearer $SECRET" \
  -H "Content-Type: application/json" \
  -d '{"enabled": false}' \
  $BASE/control/cameras/<camera_id>/settings/led
```

---

## `POST /control/cameras/{camera_id}/settings/image`

Sets picture settings. Every field is optional; only the ones sent are changed
(the rest are carried over from the read half of the read-modify-write).

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**: accepted keys:

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
| `max_exposure` | int | Must be one of the device's `max_exposure_options`. Unsupported on both reference cameras (`canSetMaxExposure` false), so expect `422 unsupported` there; routed because another model may offer it, and never exercised against hardware |

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

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**: accepted keys:

| Field | Type | Notes |
| --- | --- | --- |
| `clip_resolution` | string | Must be one of the device's `clip_resolution_options` (`480x480` / `960x960` / `1440x1440` on the cameras seen). This is the **recorded clip** resolution and has no effect on the live stream; for that, see `live_resolution` below |
| `clip_quality` | string | Encode quality; must be one of the device's `clip_quality_options` |
| `pretrigger_enabled` | bool | Pre-trigger recording (the seconds before the event) |
| `keep_clips_days` | int | Cloud retention in days, checked against whichever the device publishes: its `keep_clips_days_options` list and/or a min/max. **`-1` is a legitimate option meaning "keep forever"**, so do not filter negatives out on the assumption that a retention must be positive |
| `alarm_clip_duration` | int | Alarm-clip length in seconds; must be within the device-reported range, else `400` |
| `camera_triggered_clip_duration` | int | Camera-triggered clip length in seconds; checked against the device's published options/range like the other numeric fields |
| `live_resolution` | string | The **live stream's** resolution: the exception in this route, and the one field here that is not about clips. Must be one of `live_resolution_options`. Unlike `clip_resolution` it **does** change what this bridge restreams (verified live), though only from the next stream renegotiation |

Any other key, or a value of the wrong type, is a `400 malformed`. A recognised
key the device reports no support for is a `422 unsupported`; that gate runs on
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
> on its own; it happens only when a caller explicitly posts `start`. Treat the
> first invocation on a new device as an experiment and pick your moment: no live
> siren command was ever captured from the app on the account this was built
> against (the UI is absent there), so the request shape comes from the decompiled
> app rather than from an observed round trip.

**Path parameter:** `camera_id`. Must exist in the camera registry.

**Request body (JSON)**

| Field | Type | Required | Notes |
| --- | --- | --- | --- |
| `action` | string | yes | `"start"` or `"stop"`. Any other value is a `400 malformed` |

**Success response**

```json
200 {"status": "applied"}
```

That is the **whole** body: exactly two keys' worth, and nothing from the
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
  it stops on its own on any particular schedule; post `{"action": "stop"}` to
  end it early.

**Errors:** the shared table, plus `422 unsupported` for
`canManuallyTriggerSiren` false, and:

| Status | Body | Meaning |
| --- | --- | --- |
| `503` | `{"error": "mws_unavailable"}` | There is no mobile MWS session to act through |

The `503` is the one that surprises people. Siren and privacy are the only two
routes in this group that speak the **mobile** API, and the bridge does not own
an MWS session of its own; it borrows the one the talk manager mints. With
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
dark while it is active**: this is the one route here that changes what the
video half of the bridge can deliver.

**Path parameter:** `camera_id`. Must exist in the camera registry.

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
| `limit` | `20` | Max number of events. Clamped to `1`–`100`; it is passed to the upstream account as a page size, so it is bounded rather than forwarded verbatim. A non-integer value falls back to `20`. |

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
through as-is (fetchable directly, no cookie needed); they are **not** proxied
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

TELUS's system-wide activity feed: arm/disarm, door/lock, camera triggers,
doorbell presses, etc., newest first. Broader than `/control/events` (which is
camera clips only).

**Query parameters**

| Param | Default | Notes |
| --- | --- | --- |
| `limit` | `20` | Max number of entries. Clamped to `1`–`100`; it is passed to the upstream account as a page size, so it is bounded rather than forwarded verbatim. A non-integer value falls back to `20`. |

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

`event_type_name` is the upstream server-side enum naming what the entry *is*,
and is **the field to classify an entry on**. Values captured live include
`DoorbellButtonPress` (a doorbell button press), `VideoCameraTriggered`,
`AccessControlLocked` and `AccessControlUnlocked`; the full set is not published
by the upstream API, so treat an unrecognised value as "some other activity"
rather than assuming the list above is exhaustive.

`description` is **not** a substitute for it. That field is free text rendered in
the account's own language, and this project's reference account has produced
both French (`Déclenché par caméra (Une personne)`, `Mouvement détecté`) and
English (`Button Push`, `Locked (Manually locked)`) descriptions; the language
is a server-side account property, not a request parameter. A consumer that
matches description text therefore breaks silently the day the account's locale
changes: the route keeps answering 200 with well-formed activity, and the only
symptom is a match that stops matching. `event_type_name` does not translate.

`device_id` is the alarm.com device id (`<unit>-<did>`), the same id
`/control/cameras` reports for a camera, so a camera-related entry can be
attributed by comparing the two directly. `device_description` is a
user-editable label and is for display only.

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
and opened later lands on alarm.com's "you don't have permission" page, so
callers hit this endpoint at click time and redirect to the result.

Responses are cached in-process for `config.CLIP_URL_CACHE_TTL_S` (60 s), keyed
by event id, so a burst of taps on one notification collapses into a single
TELUS round trip. Expired cache entries are evicted opportunistically.

**Path parameter:** `video_event_id`: the id from a `/control/history` entry's
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
list first; TELUS itself 404s/errors on a bad id, and that is surfaced as
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
Assistant** and not called automatically; every registered camera's pipeline
is started eagerly by `main.py` and left running ("always warm"), so there is
no reader-count-driven on-demand start/stop.

**Path parameter:** `path_name`: the RTSP path name
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
`custom_components/telus_smarthome/bridge_client.py`, used throughout the
integration: `camera.py`, `event.py`, `switch.py`, `sensor.py`, the
settings and arming-device coordinators, and the speak/clip/bypass/unpair
services. Two helpers matter:

- **`read_control_secret()`** reads the shared secret off the mounted share
  (`/share/telus_cam_bridge/control_secret`), stripped. It is blocking file I/O
  and must run in an executor. It returns `None` when the bridge has not yet
  generated its secret (e.g. right after either container restarts); callers
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
