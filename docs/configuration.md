# Configuration Reference

This is the single place to look up any configurable knob across both halves of
the project:

- The **camera bridge**: a Docker container that logs in, pulls WebRTC video,
  re-publishes it as RTSP, and exposes a local control API. Configured through
  environment variables (files `.env` and `secrets.env`), with the authoritative
  defaults living in `bridge/config.py`.
- The **Home Assistant integration** (`custom_components/telus_smarthome`): a
  HACS-installable custom component. Configured through the UI (config flow and
  options flow); a few build-time constants live in `hacs.json` and
  `manifest.json`.

Most settings have sensible defaults and never need touching. The only strictly
required configuration is the pair of account credentials in `secrets.env`.

For how these components talk to each other, see
[camera-bridge.md](camera-bridge.md) and the top-level project README.

---

## Camera bridge

The bridge reads all configuration from environment variables at process start.
Anything not listed as an environment variable below is a hard-coded module
attribute in `bridge/config.py`, documented here so you know the value, but
changing it means editing the source. Some of these module attributes are
reassigned **at runtime** (credentials, see the note below), but they are not
environment-tunable.

### `secrets.env`: credentials (required)

Copy `secrets.env.example` to `secrets.env` and fill in real values. This file is
gitignored; never commit it.

| Variable | Required | Meaning |
| --- | --- | --- |
| `TELUS_EMAIL` | yes | The account email used to log in. Read once at startup; the process exits immediately if it is unset. |
| `TELUS_PASSWORD` | yes | The account password. Same startup requirement. |

If either variable is missing, the bridge exits at startup with a message telling
you to check `secrets.env`.

**The `$$` escaping rule.** Docker Compose interpolates `$`-references in
`env_file` values. If your password contains a literal `$`, you must double it to
`$$` in `secrets.env`, or Compose silently corrupts the value (this was verified
during development, not a hypothetical). The bundled `install.sh` performs this
escaping for you automatically if you let it prompt for credentials instead of
hand-editing the file. This is also the reason credentials live in `secrets.env`
and not in `.env`: keeping the escaping quirk out of the plain-config file.

**Runtime credential updates.** `TELUS_EMAIL` and `TELUS_PASSWORD` are
intentionally mutable module attributes. Both the control server (a live push
from Home Assistant's reconfigure flow) and the fallback-file watcher can
reassign them while the process runs, and the auth layer reads them lazily on
every login. A credential change therefore takes effect on the next login with no
container restart. See [camera-bridge.md](camera-bridge.md) for the credential
sync path.

### `.env`: non-secret settings (all optional)

Copy `.env.example` to `.env`. Every setting here is optional; the defaults are
what the project was built and tested against. The example file ships with all of
these commented out.

The environment variables the bridge actually reads, grouped by concern:

#### General

| Variable | Default | Meaning |
| --- | --- | --- |
| `LOG_LEVEL` | `INFO` | Log verbosity (standard Python levels, e.g. `DEBUG`, `INFO`, `WARNING`). The bridge logs only to stdout/stderr; the Docker layer owns retention (`docker-compose.yml` caps the json-file log at 10 MB × 3 files), so `docker logs telus-cam-bridge` is the one place logs live and history is bounded at ~30 MB. |
| `CONTROL_ACCESS_LOG` | `false` | Per-request access logging (`aiohttp.access`, INFO) for the control endpoint. Off by default: every request is loopback Home Assistant polling, and the lines measure at roughly a quarter of all log output, enough to age real warnings out of the bounded log window within days. Handler-level warning/error logging is unaffected; set `true` to watch individual requests. With the default, `grep aiohttp.access` over the container log finds nothing. |
| `CONTROL_PORT` | `8199` | TCP port for the local control API. Bound to loopback (`127.0.0.1`) only; never reachable from off-host, since both this container and the HA container run `--network host`. |
| `HANDOVER_TRACE` | `false` | **Instrumentation for the session-rotation window**, for investigating frames lost at a handover. With it on, every proactive rotation emits **one structured line per camera**, `handover trace {...}`, everything after the prefix being a single JSON object: when the previous session's audio pump actually stopped, the frame hold's silence cadence through the gap kept separate from how much audio those calls actually wrote, the **muxer lead** (how far the audio timeline runs ahead of the wallclock-stamped video) at every step, when the new session's first frame arrived and when it was written to ffmpeg, when its audio pump started, the video queue depth at each of those, and the timestamp of every dropped frame relative to the rotation. It is off by default because it measures a window whose whole problem is that frames go missing inside it: on, one traced rotation costs about **70 µs of CPU and ~1.5 kB of log** (a few kB a day against the container's 10 MB × 3 window); off, the entire mechanism is a single `is not None` test per frame (measured at 6.4 ns, against a 66.7 ms frame interval at 15 fps). Nothing it does awaits, sleeps or writes, so it is safe to leave on for a multi-hour soak. How to read a record: [troubleshooting.md](troubleshooting.md#measuring-what-a-session-rotation-costs-the-audio-timeline). The records go to the **log only, deliberately, and not to `/control/status`**: a handover is an event, and a snapshot contract Home Assistant polls continuously would report each record an arbitrary number of times or not at all. |
| `MEMORY_TRACE` | `false` | **Instrumentation for the container's memory**, for finding out WHICH process a climb is in. With it on the bridge emits one structured line every `MEMORY_TRACE_INTERVAL_S`, `memory trace {...}`, everything after the prefix being a single JSON object: the RSS of every process in the container (MediaMTX, `python3` and one ffmpeg per camera all share the `mem_limit`, so the `docker stats` figure is a sum over them), glibc's `mallinfo2` (what the allocator has taken from the OS against what is actually in use, which is how a per-thread-arena plateau is told apart from a real leak), the object types that grew since the previous record, and the allocation sites that grew, ranked by growth rather than by size. **Unlike `HANDOVER_TRACE` it is not safe to leave on**: it enables `tracemalloc`, which roughly doubles per-allocation cost and stores a frame per live allocation, and each sample walks every gc-tracked object -- and it adds to the very number under investigation, so read `python.rss_mb` from the record and never `tracemalloc.traced_mb`. Turn it on to attribute a climb, then turn it off. How to read a record: [troubleshooting.md](troubleshooting.md#attributing-the-containers-memory). |
| `MEMORY_TRACE_INTERVAL_S` | `330` | Seconds between records. The default is two of the ~165 s session rotations, so consecutive records always bracket at least one whole rotation per camera and a per-rotation retention shows as a step between records rather than as noise inside one. Settable from the environment because the useful window depends on what is being chased: a container that reaches its limit in twenty minutes cannot wait for six records at the default cadence. |
| `MEMORY_TRACE_TOP` | `12` | How many allocation sites each record lists, largest growth first. |
| `MEMORY_TRACE_FRAMES` | `1` | Stack frames `tracemalloc` keeps per allocation. 1 is the allocating line alone, which is what names a leak; deeper stacks multiply `tracemalloc`'s own memory cost by about the same factor and are only worth it when the allocating line turns out to be a shared helper (numpy's, PyAV's) that says nothing about which caller is responsible. |
| `JANUS_REQUEST_AUDIO` | `false` | **An experiment, not a feature**, and **per-camera**. Adds `audio`/`audioport`/`audiopt`/`audiortpmap` (Opus, payload type 111) to the Janus mountpoint-create body for the cameras it names, so the offer-media diagnostic below reveals whether the upstream will serve a *downstream* audio track at all. Accepted values: `false`/`0`/`no`/`off` or unset: **off for every camera** (the default, and the create body stays byte-identical to the video-only one the bridge has always sent); `true`/`1`/`yes`/`on`/`all`: on for every camera; a **comma-separated list of camera ids** (`<camera-id>,<camera-id>`; exact id match, surrounding whitespace ignored): on for exactly those. The keywords are only recognised as the *whole* value, never as one entry of a list. Which mode applied is logged once per camera at session start. It is per-camera because asking is not free for every camera: with audio requested the gateway offers the audio m-line *first*, and cameras differ in whether they then deliver video (see [camera-bridge.md](camera-bridge.md)). Safe to leave on for a camera that tolerates it: a rejected create is logged and retried once with the unchanged video-only body, so it cannot fail a session or count toward the restart escape hatch. **On its own it does not make the RTSP feed carry sound**: it only asks for the track. Publishing what comes back is the separate `JANUS_CONSUME_AUDIO` knob below; with that off, an offered audio track is drained and discarded. Talkdown, the separate path that plays audio *out* of a doorbell, is unaffected either way. |
| `JANUS_CONSUME_AUDIO` | `false` | **Publish the downstream audio track on the camera's RTSP path**, alongside the video. Per-camera, with exactly the same three value forms and the same whole-value keyword rule as `JANUS_REQUEST_AUDIO` above: `false`/`0`/`no`/`off` or unset: off everywhere (the default); `true`/`1`/`yes`/`on`/`all`: on for every camera; a comma-separated list of camera ids: on for exactly those (exact id match, surrounding whitespace ignored). **It requires `JANUS_REQUEST_AUDIO` to name the same camera.** A camera named here but not there is never offered an audio track at all, so the combination is reported once in the log and treated as **off** rather than silently building a publisher whose audio input can only ever carry silence. For a camera it applies to, the ffmpeg publisher gains a second raw-PCM input and the output carries **mono AAC** (`-c:a aac -ac 1`) at the sample rate the decoded frames actually arrive at. AAC is not a free choice: Home Assistant's HLS stream worker accepts only aac/mp3 for audio, and Frigate's `-c copy` recording preset needs a codec that is legal in an mp4 container: AAC is the only codec both accept, so what the gateway sends is transcoded rather than passed through. What it sends is **G.711 mu-law at 8 kHz**, on a payload type the offer advertises as Opus; the bridge works that out per session and reports it as each camera's `audio_codec` in [`/control/status`](control-api.md#get-controlstatus) (see [camera-bridge.md](camera-bridge.md#the-audio-track-is-decoded-as-the-codec-that-arrives-not-the-one-advertised)). The *video* input is unaffected: it is the same on both command lines, and its rate is declared with `-framerate <fps>` (never `-r`, which would override `-use_wallclock_as_timestamps` and make the video timeline advance per frame written rather than per second elapsed); see [camera-bridge.md](camera-bridge.md) for the measurements and for the audio path itself. To check that it actually worked, read the camera's `decoded_audio_frames`, `audio_format` and `audio_codec` in [`/control/status`](control-api.md#get-controlstatus) (a healthy stream decodes ~50 frames a second, since the frames are 20 ms; a small fraction of that is a codec problem, not a quiet room), or, with the Home Assistant integration installed, that camera's **Audio state** entity, which compares that counter across polls for you and reports `not_consuming` / `no_audio_yet` / `flowing` / `stalled` (see [ha-integration.md](ha-integration.md#stream-health-and-bridge-connectivity)). This matters: a stream that decodes nothing still publishes a valid, entirely silent AAC track, because the publisher's audio input is kept alive with generated silence whether or not the camera's audio ever arrives. |

#### Video / encode

| Variable | Default | Meaning |
| --- | --- | --- |
| `MEDIAMTX_RTSP_PORT` | `8554` | Port the bundled MediaMTX listens on for RTSP, and the port the bridge publishes to and reports to Home Assistant. Host is fixed at `127.0.0.1`. |
| `ENCODE_BACKEND` | `vaapi` | Hardware encode path tried before falling back to software `libx264`. One of `vaapi` (Intel QuickSync or AMD via `/dev/dri`; the project's own deployment), `nvenc` (NVIDIA via nvidia-container-toolkit; see the commented-out NVIDIA section in `docker-compose.yml`, not verified against real NVIDIA hardware), or `software` (skip hardware entirely). Deliberately not `auto`: probing an unavailable backend still costs a real failed subprocess launch on every reconnect. Value is lower-cased. |
| `VAAPI_DEVICE` | `/dev/dri/renderD128` | The render node handed to ffmpeg's `-vaapi_device`. Requires the host's `/dev/dri` be passed into the container. Overridable because render-node numbering is not stable across hosts/reboots when more than one GPU is present. |
| `RTSP_PASSWORD` | `""` (empty) | Password for the bundled MediaMTX's `bridge` user. Normally generated by `entrypoint.sh` and shared via `/shared/rtsp_password`; you do not set this by hand. If empty, published/reported RTSP URLs omit credentials rather than emit a guaranteed-rejected form. |

The RTSP user is fixed at `bridge`. Stream paths are derived from the camera id
(`telus_<camera-id-with-dashes-as-underscores>`); the password is hex, so it
needs no URL-escaping when threaded into the RTSP URL.

**Encode/resolution behavior (module constants, not env vars):**

| Setting | Value | Meaning |
| --- | --- | --- |
| `MAX_ENCODE_DIMENSION` | `960` | Upper bound on the larger side of the published H.264 frame. Some cameras (the doorbell) publish natively at 1440x1440; decoding that continuously can exceed what a modest viewer's browser/iGPU can sustain alongside a second camera (confirmed live: RTP kept arriving while the picture froze, a decoder/paint stall on the viewer, not the network). Downscaling at encode cuts pixel throughput at the cost of some detail. |

**Container resource caps.** These are read by `docker-compose.yml`, not by the
bridge process itself, but they are documented in `.env.example` because they live
alongside the other overrides:

| Variable | Default | Meaning |
| --- | --- | --- |
| `BRIDGE_MEM_LIMIT` | `768m` | Container memory cap. Default leaves generous headroom over measured live usage (~326 MB RSS for 2 cameras). |
| `BRIDGE_CPU_LIMIT` | `2.0` | Container CPU cap (cores). Default is well above measured usage (~14–31% of one core for 2 cameras). |
| `HOST_SHARED_DIR` | `/var/lib/homeassistant/share/telus_cam_bridge` | Host-side path for the directory shared with the HA container. Default matches the Home Assistant Supervised layout; override for a different install type or non-default path. |
| `RENDER_GID` | `992` | GID of the host `render` group, granted to the container user (`group_add`) for `/dev/dri` access (hardware encode). Set it to the output of `getent group render` if the host's GID differs. |

#### Talkdown (two-way audio)

Talkdown is the on-demand, user-triggered two-way audio path to the doorbell
speaker (`POST /control/cameras/{id}/talk`). It is separate from the always-warm
video pipeline and is deliberately excluded from the restart guard: a failed talk
fails one request and never exits the process. See [camera-bridge.md](camera-bridge.md)
for the full talk mechanism.

| Setting | Env var? | Default | Meaning |
| --- | --- | --- | --- |
| `TALKDOWN_ENABLED` | **yes** (`TALKDOWN_ENABLED`) | `true` | Master switch for two-way audio. Set it to a false value and the talk endpoint returns `403`. Truthy values: `1`, `true`, `yes`, `on` (case-insensitive, whitespace-trimmed). It gates **only** talkdown: the camera settings, diagnostics, siren and privacy routes are unaffected by it. |
| `TALK_PREFER_WEBRTC` | no | `True` | Prefer the `<webrtc>` endpoint (aiortc, clean sendrecv Opus) over `<srtp>` (ffmpeg) for cameras that offer both. |
| `TALK_PREFER_FULL_DUPLEX` | no | `True` | Prefer the full-duplex HTTP-PUT path when the device advertises upstream audio (`susa=true`). This is the only working talk path for the ADC-VDB750 doorbell, which returns only an `<rtsp>` (video + listen) endpoint. |
| `TALK_PUT_ENDPOINT_ORDER` | no | `("direct", "local", "public", "cloud")` | Order in which the full-duplex `PutAudioStream` endpoint variants are tried. The bridge shares the doorbell LAN, so the direct/local LAN URL is fastest and relay-free; public then cloud relay are WAN fallbacks. Values are keys in the parsed doorbell camera list `put_urls`. |
| `TALK_CODEC` | no (per-request override via POST `codec`) | `mulaw` | Audio codec for the full-duplex PUT path. On the VDB750 (fw 02091) the PUT path is mu-law only: L16 was tested and came out as noise, because that device decodes the body as mu-law regardless of Content-Type. A per-model/firmware limit, not project-wide; the codec plumbing is kept so a working codec can be added per device later. |
| `TALK_CODEC_FALLBACK` | no | `mulaw` | Fallback codec if the primary fails. Currently also mu-law for the same reason. |
| `TALK_AUDIO_FILTER` | no | see below | ffmpeg audio-conditioning filter applied to **all** talkdown audio before encoding to the doorbell codec. Set to `""` to pass audio through untouched (e.g. for already-conditioned sources). Applied to every talk path (full-duplex folds it into its encode pass; the webrtc/srtp fallback applies it up front), and overridable per request via the `audio_filter` body field. |
| `TALK_CHUNK_MS` | no | `40` | Milliseconds of audio per PUT. Also the granularity at which a stop request takes effect. Larger = fewer round trips but coarser stop response; the value matches what the app was observed sending. |
| `TALK_PREBUFFER_MS` | no | `500` | How far ahead of real time the stream stays, so the doorbell always holds a jitter buffer. Delivering chunks exactly at playback time underruns into audible gaps. Raising it delays how quickly a stop is heard. |
| `TALK_DOORBELL_CACHE_TTL_S` | no | `60` | How long a `GetDoorbellCameraList` result may be reused across talks, saving that round trip (~0.3 s before audio starts) on back-to-back talks. The session it carries is not per-talk in practice, and a stale hit self-corrects via renegotiation. Dropped on any talk failure and on every renegotiation. `0` disables it. |
| `TALK_PUT_ENDPOINT` | **yes** (`TALK_PUT_ENDPOINT`) | *(empty, auto-detect)* | Pin the full-duplex PUT to one endpoint kind (`direct`, `local`, `public`, `cloud`) instead of letting reachability detection choose. Set it when detection is wrong: a firewall that drops the probe but would pass the real PUT, or an endpoint that accepts a connection but cannot carry audio. A value this doorbell does not advertise is ignored with a warning and auto-detection runs anyway: a mistyped knob must not silence the doorbell. |
| `TALK_ENDPOINT_PROBE_TIMEOUT_S` | **yes** (`TALK_ENDPOINT_PROBE_TIMEOUT_S`) | `1.0` | How long the reachability probe may take per candidate. `TALK_PUT_ENDPOINT_ORDER` above is a *preference*, not a reachability claim (off the doorbell's LAN its first two entries are RFC1918 addresses that simply do not answer) so the candidates are probed concurrently and consumed in preference order, and only unreachable ones are skipped. A probe is a bare TCP connect: no TLS, no HTTP, nothing written, so it cannot disturb the audio path. Raise it if the doorbell is across a link that legitimately takes longer than a second to accept a connection; `0` disables probing and restores a blind walk down the order. If nothing answers, the first preferred URL is used anyway so the PUT itself produces the real error. |
| `TALK_STREAMING_ENCODE` | **yes** (`TALK_STREAMING_ENCODE`) | `true` | Feed the PUT loop from ffmpeg's stdout as it decodes instead of encoding the whole clip into memory first. The doorbell starts speaking after one chunk is ready rather than after the entire file is transcoded, and encoded-audio memory is bounded by the lookahead below rather than by the clip. Set it false to restore buffer-then-send: the switch exists so a field regression in a live audio path is an `.env` change rather than a code change. Both modes run the same send/pacing loop. |
| `TALK_ENCODE_LOOKAHEAD_S` | no | `30` | How far ahead of the send schedule the streaming encoder may run. This is what makes streaming safe: ffmpeg decodes far faster than real time, so without a bound it would simply re-buffer the whole clip. It doubles as a jitter buffer for the *encoder*: a decode hiccup with tens of seconds already queued is inaudible. Approximate; the OS pipe buffer adds a little more. |
| `TALK_ENCODE_READ_TIMEOUT_S` | no | `15` | A read from the encoder that blocks this long means ffmpeg is wedged rather than merely slow (it normally runs hundreds of chunks ahead), and fails the talk instead of letting the doorbell run dry with the session held open. |
| `TALK_PREBUFFER_ADAPTIVE` | **yes** (`TALK_PREBUFFER_ADAPTIVE`) | `false` | Let the jitter-buffer lead grow to cover the worst observed PUT round trip. **Leave it off until someone has listened to a talk with it on**: `TALK_PREBUFFER_MS` was tuned by ear against the real speaker, and nothing available offline justifies moving it. The *measurement* is always on regardless (see the talk response's `put_rtt_*` / `late_chunks`), so a later tuning has data rather than a guess. The adaptation is one-directional by design: it may only raise the lead, never lower it below the tuned value. |
| `TALK_PREBUFFER_MAX_MS` | no | `2000` | Ceiling on the adaptive lead, and therefore also the ceiling on how much audio the doorbell may still be holding when a stop arrives, which is why it is not larger. |
| `TALK_MAX_RECONNECTS` | no | `2` | Consecutive in-place retries (same credentials, new socket, fresh Digest handshake) before escalating to a full renegotiation. Two failures do not need new credentials: a dropped keep-alive connection, and a `401` the Digest middleware could not clear by itself. Renegotiation is three round trips of dead air and is bounded, so spending one on a problem a new socket fixes can cost the talk later. Any successful chunk resets the counter, and the safety deadline is deliberately *not* extended for a reconnect, so a link that blips forever still terminates. `0` sends every failure straight to renegotiation. |
| `TALK_MAX_RENEGOTIATIONS` | no | `2` | How many times a talk may fetch a fresh doorbell session and resume from the exact byte offset it reached. The PUT URL, port and password are per-session, so a long clip or a network blip can outlive one; bounded so a persistently broken session fails the request instead of looping. |
| `TALK_RENEGOTIATE_MARGIN_S` | no | `20` | When a session expiry can be determined, swap this many seconds early rather than waiting for the first failed PUT: a proactive swap costs one silent gap of a few hundred ms instead of a failed chunk plus a teardown/setup round trip. |
| `TALK_DEVICE_CERT_SHA256` | **yes** (`TALK_DEVICE_CERT_SHA256`) | the fingerprint observed on one ADC-VDB750 (the literal is in `config.py`) | SHA-256 of the DER certificate the doorbell presents on its audio endpoint. Change it after a firmware update, a factory reset or a replacement unit: all three legitimately change the certificate, and a mismatch logs the presented fingerprint so it can be pasted here. Set it empty to run unpinned; a value that is not a 32-byte hex digest is rejected with a logged error when a talk first constructs the pin (not at startup), and the talk continues unpinned. |
| `TALK_TLS_PIN_MODE` | **yes** (`TALK_TLS_PIN_MODE`) | `warn` | `warn` verifies the fingerprint, logs loudly on a mismatch (with both values and the fix) and **connects anyway**; `enforce` refuses the connection on a mismatch; `off` disables the check. See below. |
| `TALK_TLS_PIN_ENDPOINT_KINDS` | no | `("direct", "local", "public")` | Which endpoint kinds the pin applies to. The fingerprint is the *device's* own, so it is only meaningful where TLS terminates at the device: the two LAN URLs and the site's WAN address. The `cloud` endpoint is the vendor relay (a different host with its own CA-signed certificate) and pinning the doorbell's fingerprint there would break WAN talkdown outright. |

The default `TALK_AUDIO_FILTER` is tuned by ear for the doorbell's small speaker,
which clips and sounds harsh on raw full-range music/TTS:

```text
highpass=f=250,lowpass=f=2800,acompressor=threshold=-18dB:ratio=3:attack=5:release=150:makeup=2,alimiter=level_out=0.5:limit=0.6
```

That chain high-passes out the bass the speaker can't reproduce, low-passes the
harsh top end (warmer), gently compresses the dynamics, and hard-limits below full
scale so nothing clips.

**Certificate pinning, and why the default only warns.** The doorbell serves its
audio endpoint over HTTPS with a **self-signed** certificate, so ordinary CA
verification cannot apply and the only identity the device has is the certificate
itself. `TALK_DEVICE_CERT_SHA256` + `TALK_TLS_PIN_MODE` turn that into a real
check instead of a connection with verification switched off.

The shipped fingerprint was observed on **one** ADC-VDB750, and it is not a
vendor-wide constant: a firmware update, a factory reset or a replacement unit all
legitimately present a different certificate. That is exactly why `warn` is the
default: it detects and reports, printing both fingerprints and the fix, and
still connects, so a routine firmware update cannot turn into a silent doorbell
with an obscure TLS error. Move to `enforce` once the pin is known good for *your*
device, and re-pin after any firmware update. With no pin configured at all,
`warn` and `enforce` both degrade to learn-and-report: the observed fingerprint is
logged once so it can be adopted, and the talk proceeds.

The check runs once per connection rather than per request, so a keep-alive chunk
stream pays for it once, and it never applies to the `cloud` endpoint; see
`TALK_TLS_PIN_ENDPOINT_KINDS` above.

**The jitter-buffer lead is tuned by ear.** `TALK_PREBUFFER_MS` is how far ahead
of real time the stream stays so the doorbell always holds a jitter buffer;
delivering chunks exactly at playback time underruns into audible gaps. It was set
by listening to the real speaker, which is why `TALK_PREBUFFER_ADAPTIVE` ships off
and why raising the lead is the only direction the adaptation is allowed to move
it. A larger lead cannot cause an underrun (it only delays how quickly a stop is
heard and leaves more audio buffered at the doorbell) whereas a smaller one can.
What is always on is the measurement: every talk reports the observed PUT
round-trip distribution and how many chunks missed their slot, in the talk
response (see [control-api.md](control-api.md#post-controlcamerascamera_idtalk)).

**Talkdown timeouts** (module constants):

| Setting | Default | Meaning |
| --- | --- | --- |
| `TALK_STREAM_LOOKUP_TIMEOUT_S` | `20` | Bound on the `GetLiveVideoStream` lookup so a wedged call fails the one request instead of pinning the per-camera talk lock. |
| `TALK_SESSION_TIMEOUT_S` | `60` | Hard ceiling on a whole talk session (endpoint handshake + clip playback + teardown). On overrun the session is cancelled and torn down. |
| `TALK_SRTP_TIMEOUT_S` | `45` | Inner ceiling handed to the SRTP (ffmpeg) client, kept under the session ceiling so ffmpeg's own teardown runs first. |
| `TALK_MEDIA_FETCH_TIMEOUT_S` | `10` | Timeout fetching a `media_url` (a loopback HA TTS / media-source URL), so a slow/hung source can't stall the talk. |
| `TALK_MEDIA_MAX_BYTES` | `64 * 1024 * 1024` (64 MiB) | Hard cap on fetched/read audio, enforced while streaming so an oversize body is never buffered whole. |

#### Reconnect, timeouts, and hygiene (module constants)

None of these are environment variables; they are hard-coded in `config.py`. They
govern reconnect backoff, rate-limit handling, session lifetime, and the
crash-restart guard.

| Setting | Default | Meaning |
| --- | --- | --- |
| `RECONNECT_BACKOFF_INITIAL_S` | `2` | Initial reconnect backoff. |
| `RECONNECT_BACKOFF_MAX_S` | `30` | Maximum reconnect backoff (sequence 2, 4, 8, 16, 30, …). |
| `RATE_LIMIT_RETRY_MAX_S` | `30 * 60` (30 min) | Absolute ceiling on a single `Retry-After`-driven sleep. A sanity clamp on a garbage header value, not a second-guess of a reasonable one: observed real values are ~101s. Used to be 300s applied as a plain `min()`, which had the bridge coming back sooner than the server asked and re-entering the rate-limit window. |
| `RATE_LIMIT_RETRY_MARGIN_S` | `2` | Margin added on top of the server's stated wait so clock/rounding skew can't land the retry just inside the window again. |
| `VOIPMS_SIP_SERVER` / `VOIPMS_SIP_ACCOUNT` / `VOIPMS_SIP_PASSWORD` | (empty) | The VoIP.ms POP hostname and SIP sub-account for the outbound call leg. All three empty by default, which disables `POST /control/call/test` entirely; the deployment opts in via its env file. The password is used for digest computation only and never appears in a SIP message or a log. |
| `VOIPMS_DID` | (empty) | Caller ID number presented on outbound calls. |
| `DOORBELL_CALL_TARGET` | (empty) | Default number the call leg dials when a request supplies no target. |
| `SIP_PORT` | `5060` | The POP's SIP port. |
| `CALL_ENABLED` | `true` | Master gate for the call feature. Home Assistant manages this live (options flow -> `call_settings.json` + `/control/call/settings`), so the env value is only the bootstrap. |
| `CALL_MAX_S` | `600` | Hard ceiling on one bridged call, so a forgotten call cannot hold the doorbell session and bill by the minute indefinitely. |
| `CALL_LIVE_PREBUFFER_MS` | `300` | Doorbell-playout jitter buffer (the muxer lead) for a live call. Small and fixed: a live source cannot be buffered ahead, so the clip lead (`TALK_PREBUFFER_MS`) and its adaptive growth would be latency the caller hears. High enough to cover normal per-chunk ack jitter so playout does not gap. Does not affect clip talkdown. |
| `CALL_DOWNLINK_BUFFER_MS` | `600` | Cap on the caller's-voice buffer waiting to be PUT to the doorbell speaker -- and therefore the call's downlink mouth-to-ear latency ceiling. Fill and drain both run at real time, so a backlog formed during a slow-ack stretch never drains on its own; the caller hears the buffer's depth as delay for the rest of the call, and this cap is the only bound on it. Past the cap the OLDEST audio is shed: in a live conversation, stale speech is better dropped than replayed late. Do not deepen it to absorb stalls. |
| `CALL_MAX_PUT_MS` | `200` | Largest PUT body a live talkdown sends when it has backlog. The drain is one synchronous HTTP round trip per body; batching backlog into larger bodies keeps the drain at real time when the device acks slowly (its ack cost is per request, not per byte — probed to ≥400 ms bodies). Bodies stay multiples of the 10 ms frame. |
| `CALL_LISTEN_ENDPOINT_ORDER` | `direct,local,public,cloud` | Order for choosing the call's direct-LAN RTSP listen endpoint; first present wins. Direct/LAN preferred, WAN/cloud as fallback for a bridge off the device's LAN. |
| `RTSP_LISTEN_READ_TIMEOUT_S` | `10` | No RTP on the listen tunnel for this long ends the leg. |
| `CALL_DOWNLINK_HIGHPASS_HZ` / `CALL_DOWNLINK_AGC_TARGET` / `CALL_DOWNLINK_AGC_MAX_GAIN` | `250` / `0.5` / `6` | Caller→speaker shaping: high-pass cutoff, AGC target level (0-1), max lift. Low-latency in-process DSP; highpass=0 + max_gain=1 is passthrough. |
| `CALL_UPLINK_HIGHPASS_HZ` / `CALL_UPLINK_AGC_TARGET` / `CALL_UPLINK_AGC_MAX_GAIN` | `250` / `0.7` / `16` | Doorbell-mic→phone shaping: same knobs, tuned hotter for phone intelligibility of an outdoor mic (harder rumble cut, louder voice). |
| `CALL_UPLINK_GATE_THRESHOLD` / `CALL_UPLINK_GATE_ATTEN` | `0.015` / `0.15` | Noise gate on the uplink shaper: while the fast-attack/slow-release envelope sits below the threshold (normalized magnitude), gain is capped at the attenuation — instead of the AGC riding the porch background up to the target during speech pauses. `0` disables the gate. |
| `CALL_UPLINK_BUFFER_MS` | `600` | Cap on the mic audio buffered between the RTSP listen and the SIP leg — the uplink's mouth-to-ear latency ceiling, mirroring `CALL_DOWNLINK_BUFFER_MS`. The listen starts before the phone answers and the SIP leg drains at exactly real time, so uncapped pre-answer backlog persists as delay for the whole call. Overflow sheds oldest. |
| `CALL_TONE_SECONDS` | `10` | How long the test call plays its tone after answer. |
| `CALL_TIMEOUT_S` | `75` | Bound on the whole call attempt including ring time; must outlive a mobile's ~30 s of ring plus the tone. |
| `AUDIO_NORMALIZE_TARGET` | `0.9` | Loudness normalization for every published audio stream, done in process (a zero-lookahead AGC applied to the decoded PCM in the sink): per-sample magnitude the gain steers toward, `0` disables. In process rather than an ffmpeg filter because a dynamics filter's lookahead delays the audio leg of the two-input mux, ffmpeg stops reading the video pipe, and the watchdog kills every session (see troubleshooting). Applies to s16 mono only; other shapes pass through unshaped. Bridge-written silence bypasses it and stays digital zero. |
| `AUDIO_NORMALIZE_MAX_GAIN` | `12` | Gain cap (~21.6 dB) for the AGC above: what stops a silent scene's noise floor being amplified into hiss. |
| `AUDIO_NORMALIZE_ATTACK_MS` / `AUDIO_NORMALIZE_RELEASE_MS` | `5` / `4000` | AGC gain smoothing: fast attack limits a transient without clipping, a multi-second release rides gain up slowly so speech does not pump. |
| `AUDIO_NORMALIZE_FILTER` | `anull` | An EXTRA ffmpeg audio filter appended after the `aresample` timestamp repair. Never set a filter with lookahead (`dynaudnorm`, `loudnorm`): its window stalls the two-input mux and every session dies in a publish→stall→rebuild loop. An empty value resolves to `anull`, never to a malformed chain. |
| `SESSION_WEDGE_TIMEOUT_S` | `1800` (30 min) | If one camera goes this long with no successful frame batch, the bridge hard-exits (`os._exit`) so Docker's `restart: unless-stopped` brings up a clean process. A duration rather than a failure count, because a count cannot tell a wedged local resource from a flaky upstream: both produce failures, and only whether the camera comes back separates them. The value is several times the longest transient failure run measured on live hardware; a genuine wedge never self-clears, so any finite threshold catches it. Calibration evidence in `config.py` and backlog E89. |
| `SESSION_WEDGE_PEER_FRESH_S` | `120` (2 min) | How recently ANOTHER camera in this process must have produced a frame for it to veto the restart above. A wedged resource is a property of the process and takes every camera down with it; a single camera failing while its sibling streams is a far-end problem a restart cannot reach, and such runs are observed to recover unaided. No peer (single-camera deployment) means no veto. Evidence in backlog E89 and E96. |
| `SESSION_CLOSE_TIMEOUT_S` | `5` | Bounds aiortc's `pc.close()`, which has no timeout of its own and can hang under certain ICE states. |
| `SESSION_MAX_LIFETIME_S` | `165` | Proactively rotate the Janus session before Alarm.com's server-side ~180s kill timer fires (15s margin). A mismatch here (was 900s) previously let the server timeout win and hang the bridge. |
| `FRAME_HOLD_MAX_S` | `60` | Upper bound on repeating the last real frame into the RTSP publish while a session rotates/reconnects, measured from the last real frame. Rotation gaps are ~2–8s, so this only expires when the camera is genuinely gone, at which point the feed is allowed to go dark rather than present a frozen image as live. |
| `FRAME_SILENCE_TIMEOUT_S` | `15` | If no frame arrives for this long, treat the session as dead and reconnect. Must fire well before `SESSION_MAX_LIFETIME_S` and be short enough that a live view doesn't appear frozen for long. |
| `WEBRTCUP_TIMEOUT_S` | `20` | Timeout waiting for Janus `webrtcup`. |
| `FIRST_FRAME_TIMEOUT_S` | `20` | Timeout waiting for the first decoded frame after connect. |
| `JANUS_KEEPALIVE_INTERVAL_S` | `25` | Janus keepalive interval. Kept well under Janus's ~60s server-side idle-session GC. |
| `CAMERA_DISCOVERY_INTERVAL_S` | `300` | How often to re-poll for the camera list so a newly added camera starts streaming without a restart. Only additions are acted on; existing camera tasks are never torn down by this poll, to avoid killing a working stream over a transient hiccup. |

One more timeout lives in `camera_settings.py` rather than `config.py`, beside
the diagnostics reads it bounds:

| Setting | Default | Meaning |
| --- | --- | --- |
| `DIAGNOSTICS_READ_TIMEOUT_S` | `35` | Per-request ceiling on each of the three upstream reads behind `/control/cameras/<id>/diagnostics`, overriding the shared upstream session's 15s default for those requests only. The signal read is answered by alarm.com querying the camera live and measures up to 25s on some models; the value sits above that and under the HA side's 45s route budget (`CAMERA_DIAGNOSTICS_TIMEOUT_SECONDS`). The settings getters keep the session default; their document reads are fast. |

**Restart-frequency guard** (`restart_guard.py`): throttles the hard-exit escape
hatch above so a sustained upstream outage doesn't become an unbounded
crash-restart loop (measured once at 89 restarts in 7 days). State lives on disk
because the hatch kills the process:

| Setting | Default | Meaning |
| --- | --- | --- |
| `RESTART_GUARD_WINDOW_S` | `60 * 60` (1 h) | Rolling window over which hatch exits are counted; older exits are dropped, so genuine one-off wedges weeks apart never accumulate. |
| `RESTART_GUARD_MAX_EXITS` | `3` | Exits allowed inside the window before throttling starts. The first few stay immediate, preserving fast recovery; only a repeating pattern is held back. |
| `RESTART_GUARD_BACKOFF_INITIAL_S` | `60` | First throttled sleep-before-exit (then 120s, 240s, … capped). The sleep runs in-process, so Docker cannot restart until it finishes: this is the hard floor on restart frequency. |
| `RESTART_GUARD_BACKOFF_MAX_S` | `15 * 60` (15 min) | Cap on that backoff. Well past the ~101s `Retry-After`, so a sustained failure stops re-hammering the limiter that caused it. |

#### Media pipeline: frame rate, video queue and audio pacing (module constants)

Not environment variables. These live in `config.py`, `janus_client.py` and
`ffmpeg_sink.py`, and they are the numbers that decide whether the doorbell keeps
its frames. The `RATE_*` group governs how the bridge notices that a camera's
**frame rate has changed** (it is a device setting, changeable at any time from
Home Assistant, the TELUS app or the portal) and how rarely it is willing to
rebuild the publisher for it. **`AUDIO_SILENCE_TOPUP_S` and `_AUDIO_SILENCE_MAX_BURST_S` are paced
against `_AUDIO_PRIME_MS`**: the audio input has to stay ahead of the video or
ffmpeg stops reading the video pipe. The prime is a **floor** on that lead and
not a ceiling, because the silence top-up can only ever raise the lead back to
zero while decoded audio is written as it arrives and can put the timeline
seconds ahead. See
[camera-bridge.md](camera-bridge.md#the-audio-path) for the mechanism and the
measured table.

| Setting | Default | Meaning |
| --- | --- | --- |
| `VIDEO_QUEUE_TARGET_MS` (`janus_client`) | `500` | Depth of the decoded-video queue, in **time**. The frame count is `round(fps × this / 1000)`, from the camera's own measured rate, so one number means one thing on every camera: a flat 4 frames was 267 ms at 15 fps and 667 ms at 6 fps. Defence in depth only: a deeper queue converts dropped frames into latency, it does not remove a stall. |
| `VIDEO_QUEUE_MIN_FRAMES` / `VIDEO_QUEUE_MAX_FRAMES` (`janus_client`) | `4` / `8` | Floor and ceiling on that count. The ceiling is memory: a decoded 1440×1440 yuv420p frame is 3.11 MB, so 8 frames is 24.9 MB per camera (container `mem_limit` 768 MiB, ~435 MiB in use) and 16 would be 49.8 MB. Above ~16 fps the ceiling binds before the time target does, so any rate change between 16 and 30 fps leaves the depth unchanged at 8. |
| `RATE_RESAMPLE_SETTLE_S` | `10.0` | How long a session must have been delivering frames before any of them are re-measured. The frame rate is a **device setting** that can change with no restart (see [camera-bridge.md](camera-bridge.md#the-frame-rate-is-adaptive-not-latched)), so it is re-measured while the stream runs, but never across the buffered burst at a session start, which has produced 544, 215 and 62 fps readings for cameras really running at 15 and 30. Leaves ~155 s of every 165 s session measurable. |
| `RATE_RESAMPLE_FRAMES` | `90` | Inter-frame deltas per settled measurement, taken as a median: 3 s at 30 fps, 15 s at 6 fps. Long enough that no single late or bunched frame moves the result, short enough for several independent measurements per session even on the slowest camera here. |
| `RATE_CHANGE_TOLERANCE_DOWN` / `RATE_CHANGE_TOLERANCE_UP` | `0.20` / `0.35` | Hysteresis band around the rate ffmpeg was **declared** with, below which a measurement is jitter rather than a change. **Asymmetric on purpose**: declaring below what the camera delivers makes ffmpeg *drop* real frames (1201 written → 1002 encoded, 199 lost), while declaring above it only pads with duplicates, and burst contamination biases a reading *high*, never low, so "the camera sped up" is the claim an artifact makes and gets the wider band. Both clear the observed jitter (6↔7 across restarts, 17%; a camera set to 15 delivering ~13.4, 11%) without swallowing a real change (−53%, +100%, −24%). |
| `RATE_REBUILD_MIN_INTERVAL_S` | `300` | Floor between two rate-driven publisher rebuilds, a little under two session rotations. A rebuild kicks every RTSP reader, so even a source that defeats the settle window, the median, the band and the two-measurements-must-agree confirmation cannot cost more than one glitch per window. It delays a correction; it never cancels one: the confirmed evidence is kept through the hold, and the first agreeing batch past the floor acts on it. |
| `RATE_FLAP_WINDOW_S` | `1800` | A confirmed change **back to the rate the last rate rebuild moved away from**, within this long of that rebuild, is the flapping signature: a delivered rate swinging with no setting change (observed live: the 30 fps camera halves its delivered rate under marginal light and comes back, dwelling minutes at each end, so both ends confirm and the two-batch gate cannot help). The flap is resolved once, to the **higher** rate (held high the output pads with duplicates; held low it drops real frames) and then held. See [camera-bridge.md](camera-bridge.md#the-frame-rate-is-adaptive-not-latched). |
| `RATE_FLAP_HOLD_S` | `3600` | How long the flap memory lives past the last settled sighting of the flap's high rate. While the source keeps visiting the high rate the memory keeps renewing and confirmed changes down to the flap's low rate are refused. A **real** setting change to the low rate stops the sightings, outlives the hold, and is applied late rather than never: an hour of duplicate padding, chosen over a night of reader-kicking rebuilds. |
| `AUDIO_FORMAT_REBUILD_LIMIT` | `1` | How many times one camera's publisher may be rebuilt, per process, because the raw audio shape it was launched with is not the shape the camera sends. There is no bootstrap shape to be wrong about: the audio input is built only from an OBSERVED decoded frame, and the publisher is video-only until there is one (see [camera-bridge.md](camera-bridge.md#the-audio-path)), so the one rebuild this allows is the expected video-only-to-observed-shape transition, at a rotation the frame hold already bridges. It is a bound rather than a preference: a rebuild kicks every RTSP reader, so a shape that keeps moving must not buy that glitch once per rotation forever. Past this many the audio input is given up on and the camera publishes video only for the rest of the process, reported in `audio_dropped` / `audio_dropped_reason`. Raising it trades reader interruptions for the chance that an unstable shape settles; no camera on this account has needed more than one. |
| `SNIFF_PACKETS` (`aiortc_opus_patch`) | `20` | How many audio packets each session decodes with **both** candidate decoders before committing to one for that session's life (see [camera-bridge.md](camera-bridge.md#the-audio-track-is-decoded-as-the-codec-that-arrives-not-the-one-advertised)). 20 packets is ~400 ms at 20 ms framing: large enough that the decision cannot be luck (libopus accepts essentially every real Opus packet, and a mu-law stream produces far fewer accidental successes), small enough that on a camera with continuous audio the decision lands before the ten-video-frame batch that sizes the ffmpeg audio input. That second point is about LATENCY, not correctness: a sniff still running when the publisher is sized costs one rotation of video-only publishing, never a mis-sized input. Frames decoded during the window are **held**, not dropped, and the winner's are delivered at the commit, so measuring costs no audio. |
| `OPUS_CONFIDENCE` (`aiortc_opus_patch`) | `0.75` | What fraction of those sniffed packets libopus must accept for the session to commit to Opus. Deliberately asymmetric towards leaving today's behaviour alone: Opus is the advertised codec, so it wins ties, wins when nothing decoded at all, and wins whenever libopus is reading most of the stream. Only a stream libopus largely **cannot** read is re-labelled as mu-law. |
| `AUDIO_QUEUE_MAXSIZE` | `50` | Decoded-audio queue depth (~1 s at 20 ms frames). Drop-oldest with its own counter, like the video queue. |
| `AUDIO_SILENCE_TOPUP_S` | `0.05` | How long the audio pump waits for a decoded frame before topping the publisher up with silence, and therefore the **cadence of that top-up**. It must stay comfortably under the 100 ms lead the pipe is primed with. It shipped at `0.5`, five times the lead, which stalled 120 of every 900 frames (13.3%) on the doorbell; `0.1` (exactly the lead) stalled 162. Not a failure timeout: nothing is declared dead. |
| `AUDIO_PUMP_MAX_BURST_S` | `0.1` | How much audio the pump may write in one uninterrupted pass before yielding to the event loop, so draining a backlog cannot monopolise the loop or the executor `write_frame`'s pixel copy shares. A **yield**, not a real-time pace: audio that lags the video is what stalls the video pipe, so a backlog must be delivered as fast as the loop allows. |
| `_AUDIO_PRIME_MS` (`ffmpeg_sink`) | `100` | Silence written into the audio pipe the instant it opens. Two jobs: it lets `avformat_find_stream_info` complete on a raw input, and it is the **lead** the audio timeline keeps over the wallclock-stamped video. Every cadence above is chosen against it. |
| `_AUDIO_SILENCE_MAX_BURST_S` (`ffmpeg_sink`) | `0.1` | Ceiling on a single **ordinary** silence top-up. What it clips is still owed: the fill is derived from elapsed wallclock, so the next call recomputes it and keeps repaying, and A/V sync is unaffected. |
| `_AUDIO_CATCHUP_DEFICIT_S` (`ffmpeg_sink`) | `0.5` | Where a late top-up stops being one and becomes a **hole**, closed in a single write instead of at the ceiling above. The damage is in the *duration* of a negative lead, not in the deficit itself: at the shipped cadence the ordinary ceiling repays at about twice real time, so a 3 s hole would mean 3 s of ffmpeg not reading the video pipe and every frame behind it lost. Pinned to `VIDEO_QUEUE_TARGET_MS`, the buffer a deficit under it can be absorbed by, and kept well clear of the ordinary ceiling so no routine top-up ever takes this path. |
| `_AUDIO_CATCHUP_MAX_BYTES` (`ffmpeg_sink`) | `_AUDIO_PIPE_MAX_BUFFER // 4` (128 kB) | Bound on that single catch-up write, in **bytes** rather than seconds, because "a long stall must not dump minutes of silence at once" is a quantity of bytes: three seconds is 48 kB at the 8 kHz mono these cameras send and 576 kB at 48 kHz stereo. A quarter of the depth at which the sink already declares ffmpeg to have stopped reading, so a catch-up can never be what trips that. Anything past the bound is still owed and still repaid across later calls. |
| `_AUDIO_PIPE_MAX_BUFFER` (`ffmpeg_sink`) | `512 * 1024` | Unwritten audio the pipe transport may hold (~2.7 s) before the sink gives up on **audio only** and keeps publishing video (`audio_dropped` in `/control/status`). |
| `_VIDEO_DRAIN_TIMEOUT_S` (`ffmpeg_sink`) | `5.0` | How long a frame may sit unread in the video pipe before the publisher is treated as gone and rebuilt. A healthy write is ~2 ms, so this is ~75 frames' worth of not being read. It is also what stops an ffmpeg that has stopped reading from parking the camera's loop forever. |
| `HANDOVER_TRACE_SETTLE_S` | `1.0` | How long past the new session's first batch of frames a `HANDOVER_TRACE` record stays open before it is emitted. A tail, not a delay for its own sake: the drops it exists to place land around those first writes, so a record closed at the batch would report the window without its own consequence. ~15 frames at 15 fps, and short enough that the line still sits beside the rotation it belongs to. |
| `_MAX_PROCS` (`memory_trace`) | `12` | How many processes one `MEMORY_TRACE` record lists. The container's pid namespace normally holds five (the entrypoint's bash, MediaMTX, `python3` and one ffmpeg per camera), so this is headroom rather than a limit -- but it is a hard cap, because `/proc` is read from a process whose namespace the module cannot verify and a record that grew with a host's process table would be the log flooding the flag exists to avoid. `container.rss_mb` sums every process regardless, and `container.procs_total` says how many that was. |
| `_DROP_TIME_MEMORY` (`janus_client`) | `256` | How many drop timestamps one session remembers while `HANDOVER_TRACE` is on. A traced rotation drops ~14; the bound is what stops a session in real trouble turning a diagnostic into a ledger. Past it the record's `in_window` count is a floor, which the `session_total` beside it makes visible. |

#### Paths and internal endpoints (module constants)

| Setting | Value | Meaning |
| --- | --- | --- |
| `CONTROL_HOST` | `127.0.0.1` | Bind host for the local control API. Loopback only. |
| `SHARED_DIR` | `/shared` | In-container mount of the directory shared with the HA container (HA side: `/share/telus_cam_bridge`). Holds the control secret, credential fallback, mobile-auth state, and restart-guard state. |
| `CONTROL_SECRET_PATH` | `/shared/control_secret` | The control endpoint's shared secret (bridge-generated on first start). |
| `CREDENTIALS_FALLBACK_PATH` | `/shared/credentials.json` | Fallback credentials file HA writes on every credential update, so the bridge can self-heal on its next poll/startup if a live push was missed. |
| `CREDENTIALS_FALLBACK_POLL_S` | `30` | How often the bridge re-reads the fallback file. |
| `MOBILE_AUTH_STATE_PATH` | `/shared/mobile_auth.json` | Persisted mobile-session state: a stable synthetic device uid (so restarts don't register a fresh device) and the rolling seamless-login token (so a restart does one silent login instead of the full SSO bootstrap). Never holds the short-lived session token itself. |
| `CONTROL_SECRET_ROTATE_INTERVAL_S` | `30 * 24 * 60 * 60` (30 days) | Control-secret rotation interval. HA reads the secret fresh off disk per request, so an in-place atomic rewrite is picked up on HA's next poll with zero downtime (no dual-secret grace period). |
| `CLIP_URL_CACHE_TTL_S` | `60` | How long `/control/clip` serves a re-minted download URL from its per-event cache before re-fetching. Kept well under Alarm.com's own signed-URL lifetime so a cached URL is always still valid, while collapsing a burst of taps on one notification into a single upstream request. |
| `RESTART_GUARD_STATE_PATH` | `/shared/restart_guard.json` | On-disk state for the restart guard (see above). |

Fixed upstream/auth constants (`TELUS_BASE`, `AUTH_GATEWAY`, `TELUS_CLIENT_ID`,
`TELUS_AUTH_PROVIDER`, `RTSP_USER`) are protocol constants and are not
configurable.

**Rotating the shared secrets.** The control secret rotates automatically every
`CONTROL_SECRET_ROTATE_INTERVAL_S` (30 days); to rotate it manually, delete
`/shared/control_secret` and restart the bridge container; it regenerates the
file, and HA reads the secret fresh off disk on every request, so no HA-side
action is needed. To rotate the RTSP password, delete `/shared/rtsp_password`,
restart the bridge (`entrypoint.sh` regenerates it), then restart Home
Assistant so its camera entities re-read the stream URLs from
`/control/cameras`.

---

## Home Assistant integration

The integration is installed via HACS and configured entirely through the Home
Assistant UI. There are no YAML settings.

### Build / install metadata

`hacs.json` (how HACS presents and installs the repo):

| Key | Value | Meaning |
| --- | --- | --- |
| `name` | `TELUS Smart Home` | Display name in HACS. |
| `render_readme` | `true` | Render the repo README as the HACS info page. |
| `homeassistant` | `2024.11.0` | Minimum Home Assistant version. |
| `zip_release` | `false` | Install from the repo tree, not a packaged zip release. |

`manifest.json` (how Home Assistant loads the integration):

| Key | Value | Meaning |
| --- | --- | --- |
| `domain` | `telus_smarthome` | Integration domain. |
| `name` | `TELUS Smart Home` | Display name. |
| `config_flow` | `true` | Configured through the UI, not YAML. |
| `integration_type` | `hub` | Presented as a hub integration. |
| `iot_class` | `cloud_polling` | Talks to a cloud service by polling (plus a websocket, see the options below). |
| `dependencies` | `[]` | No other HA integrations required. |
| `requirements` | `pyalarmdotcomajax==0.5.13` | Python packages installed on setup. |
| `version` | the current release | Integration version: the single source of truth, kept in lockstep with the git tag (see [development.md](development.md)). |

The config-entry schema is at **version 4** (`ADCFlowHandler.VERSION`); older
entries are migrated forward.

### Initial setup (config flow)

Started from Settings → Devices & Services → Add Integration. Steps:

| Step | Fields | Notes |
| --- | --- | --- |
| `user` | Username, Password | The account credentials. Both trimmed of leading/trailing whitespace to guard against a pasted trailing newline reading as a wrong password. |
| `otp_select_method` | OTP method | Only shown if two-factor is enabled and more than one method exists. If the app authenticator is the only enabled method it is used without prompting. Email/SMS methods trigger a code send. |
| `otp_submit` | One-time code | The 2FA code. On success a two-factor cookie is stored so future logins skip OTP. |

The config entry's unique id is the account email. This prevents onboarding the
same account twice and, on reauth/reconfigure, guards against a mistyped or
wrong-account credential swap silently repointing an existing entry at a different
account.

On first entry creation the integration also, best-effort, writes the
camera-bridge credential fallback file and pushes credentials to the bridge's
control endpoint; see [camera-bridge.md](camera-bridge.md). A bridge that is
unreachable never fails the config flow.

### Reauth and reconfigure

- **Reauth** (`reauth_confirm` → `user`) is triggered automatically when the API
  reports an auth failure; it walks the same credential/OTP steps.
- **Reconfigure** (Settings → Devices & Services → the integration card →
  three-dot menu → Reconfigure) lets you update username/password in place without
  deleting and recreating the entry, so entities and devices are preserved. The
  username field is pre-filled; the password is intentionally never pre-filled.

Both update the entry with `data_updates` (a merge), not a wholesale `data`
replace, so non-credential data such as the clip-link secret survives, critical
because that secret must never rotate or every clip link already delivered to a
phone would break.

### Options (options flow)

Reached from the integration card's gear icon. Two screens.

**Screen 1: arm code and intervals (`init`):**

| Option | Key | Default | Meaning |
| --- | --- | --- | --- |
| Arm code | `arm_code` | `""` (none) | Code required to arm/disarm the panel, entered as a password field. Enter the literal string `CLEAR!` to clear a previously set code back to empty. |
| Update interval | `update_interval` | `300` seconds (5 min) | How often the integration polls Alarm.com for state. Entered as a number in seconds. |
| Websocket reconnect timeout | `ws_reconnect_timeout` | `30` seconds | How long to wait before reconnecting the realtime websocket after it drops. Entered as a number in seconds. |

**Screen 2: arm-mode profiles (`modes`):**

Each arm mode is a multi-select of extra arming flags applied when that mode is
engaged. All three default to an empty selection.

| Option | Key | Default |
| --- | --- | --- |
| Arm Home options | `arm_home_options` | `[]` |
| Arm Away options | `arm_away_options` | `[]` |
| Arm Night options | `arm_night_options` | `[]` |

The selectable flags (`CONF_ARM_MODE_OPTIONS`) are the same for each mode:

| Flag | Key | Meaning |
| --- | --- | --- |
| Force Bypass | `force_bypass` | Arm even if some sensors are open, bypassing them. |
| Arm Silently | `silent_arming` | Suppress the arming countdown/exit tones. |
| No Entry Delay | `no_entry_delay` | Trigger immediately on entry rather than allowing the entry delay. |

### Internal control-plane constants

These are hard-coded in the integration's `const.py` and are not user-editable;
they define how the integration reaches the camera bridge on loopback:

| Constant | Value |
| --- | --- |
| `CAMERA_BRIDGE_SHARE_DIR` | `/share/telus_cam_bridge` |
| `CAMERA_BRIDGE_CONTROL_URL` | `http://127.0.0.1:8199/control/credentials` |
| `CAMERA_BRIDGE_CAMERAS_URL` | `http://127.0.0.1:8199/control/cameras` |
| `CAMERA_BRIDGE_SNAPSHOT_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/snapshot` |
| `CAMERA_BRIDGE_TALK_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/talk` |
| `CAMERA_BRIDGE_EVENTS_URL` | `http://127.0.0.1:8199/control/events` |
| `CAMERA_BRIDGE_HISTORY_URL` | `http://127.0.0.1:8199/control/history` |
| `CAMERA_BRIDGE_CLIP_URL_TEMPLATE` | `http://127.0.0.1:8199/control/clip/{video_event_id}` |
| `CAMERA_BRIDGE_AUTOMATIONS_URL` | `http://127.0.0.1:8199/control/automations` |
| `CAMERA_BRIDGE_CAMERA_SETTINGS_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings` |
| `CAMERA_BRIDGE_CAMERA_DIAGNOSTICS_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/diagnostics` |
| `CAMERA_BRIDGE_CAMERA_VOLUME_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings/volume` |
| `CAMERA_BRIDGE_CAMERA_CHIME_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings/chime` |
| `CAMERA_BRIDGE_CAMERA_LED_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings/led` |
| `CAMERA_BRIDGE_CAMERA_NOTIFICATIONS_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings/notifications` |
| `CAMERA_BRIDGE_CAMERA_IMAGE_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings/image` |
| `CAMERA_BRIDGE_CAMERA_VIDEO_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/settings/video` |
| `CAMERA_BRIDGE_CAMERA_SIREN_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/siren` |
| `CAMERA_BRIDGE_CAMERA_PRIVACY_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/privacy` |
| `CAMERA_BRIDGE_TALK_STOP_URL_TEMPLATE` | `http://127.0.0.1:8199/control/cameras/{camera_id}/talk/stop` |
| `CAMERA_BRIDGE_METRICS_URL` | `http://127.0.0.1:8199/control/metrics` |
| `CAMERA_BRIDGE_STATUS_URL` | `http://127.0.0.1:8199/control/status` |
| `CAMERA_BRIDGE_AUTOMATION_PAUSE_URL_TEMPLATE` | `http://127.0.0.1:8199/control/automations/{rule_id}/pause` |
| `CAMERA_BRIDGE_AUTOMATION_RESUME_URL_TEMPLATE` | `http://127.0.0.1:8199/control/automations/{rule_id}/resume` |
| `CAMERA_BRIDGE_ARMING_DEVICES_URL` | `http://127.0.0.1:8199/control/arming-devices` |
| `CAMERA_BRIDGE_ARMING_DEVICE_URL_TEMPLATE` | `http://127.0.0.1:8199/control/arming-devices/{device_id}` |
| `CAMERA_BRIDGE_ARMING_DEVICE_DISARMING_URL_TEMPLATE` | `http://127.0.0.1:8199/control/arming-devices/{device_id}/disarming` |
| `CAMERA_BRIDGE_ARMING_DEVICE_PARTITION_URL_TEMPLATE` | `http://127.0.0.1:8199/control/arming-devices/{device_id}/enrolled-partition` |
| `CAMERA_BRIDGE_BLUETOOTH_DISARMING_URL` | `http://127.0.0.1:8199/control/bluetooth-disarming` |
| `CAMERA_BRIDGE_PARTITIONS_URL` | `http://127.0.0.1:8199/control/partitions` |
| `CAMERA_BRIDGE_PARTITION_BYPASS_URL_TEMPLATE` | `http://127.0.0.1:8199/control/partitions/{partition_id}/bypass` |
| `CONF_CLIP_LINK_SECRET` | `clip_link_secret` (entry data key; see reauth note above) |

These share the control port (`8199`) with the bridge; if you override
`CONTROL_PORT` on the bridge, this URL host/port must match for the two halves to
talk (they are compiled into the integration, so keep the default unless you also
patch these).

### Camera settings and diagnostics polling

| Constant | Value | Meaning |
| --- | --- | --- |
| `CAMERA_SETTINGS_POLL_INTERVAL_SECONDS` | `120` | How often the camera-settings coordinator re-reads `/settings` and `/diagnostics` for every known camera, backing the speaker-volume number, the chime switches and wired-chime-type select, the status-LED and TELUS-notification switches, the privacy switch, the siren buttons and the camera diagnostic sensors. |
| `ARMING_DEVICES_POLL_INTERVAL_SECONDS` | `900` | How often the Bluetooth arming devices are re-read, backing the per-phone "Disarm alarm on arrival" switches. Deliberately far slower than the camera settings: nothing but a person can change these records, the poll is not the feedback path for a user's own toggle (the write adopts the record the response echoes), and each cycle is a live round trip competing for the same undocumented rate-limit budget. The cost is that a change made in the TELUS app, or a newly paired phone, takes up to 15 minutes to appear. |
| `CAMERA_DISCOVERY_POLL_INTERVAL_SECONDS` | see `const.py` | How often the camera list is re-read from the bridge. Cheap: a local read of an in-memory dict, not a TELUS round trip. |
| `VIDEO_EVENTS_POLL_INTERVAL_SECONDS` | see `const.py` | How often clip/event history is re-read, backing the recent-activity sensors and event entities. |
| `AUTOMATION_RULES_POLL_INTERVAL_SECONDS` | see `const.py` | How often the automation rules are re-read, backing the rule pause/resume switches. |
| `TALK_METRICS_POLL_INTERVAL_SECONDS` | see `const.py` | How often the bridge's talkdown counters are re-read, backing the talkdown status and failure sensors. |
| `STREAM_HEALTH_POLL_INTERVAL_SECONDS` | `30` | How often the bridge's `/control/status` per-camera pipeline diagnostics are re-read, backing the per-camera stream-health sensors and the camera-bridge connectivity entity. Deliberately short where the TELUS-backed polls are cautious, and safely so: the route answers from an in-memory dict the bridge's own camera loops keep current, entirely over loopback: zero Alarm.com requests per cycle. |

Deliberately slow, and the one knob here worth understanding before changing.
Unlike the camera-list poll (a local read of an in-memory dict on the bridge)
each cycle is a real round trip to TELUS per camera: two bridge requests, and
nine upstream reads behind them (four website settings documents, two mobile
reads for the siren and privacy records, and three diagnostics reads). These values only change when someone edits them in the app or
the portal, and Alarm.com's rate limits are undocumented and can lock an account
(see [troubleshooting.md](troubleshooting.md#alarmcom-rate-limiting-http-429)), so
shortening it buys responsiveness nobody needs at a real account-level risk. A
write through one of the `POST` routes asks for its own immediate refresh, so a
slider you just moved does not wait out the interval.

The siren, privacy, image and video routes have **no interval of their own**:
they are only ever called from an explicit user action. Nothing in either half of
the project polls a write route.

The bridge side needs no new configuration for any of this: the routes reuse the
website session and the MWS session it already maintains.
