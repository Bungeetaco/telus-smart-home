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
| POST | `/control/cameras/{camera_id}/talk` | bearer | Play audio through the camera speaker (talkdown) |
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
returns `403` until `config.TALKDOWN_ENABLED` is set (env `TALKDOWN_ENABLED=true`),
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
| `403` | `{"error": "talkdown_disabled"}` | `config.TALKDOWN_ENABLED` is false (or the talk manager was not constructed) |
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
