# Doorbell talkdown — full-duplex protocol (ADC-VDB750 / TELUS SmartHome)

Definitive, implementation-grade reference for the doorbell two-way-audio
("talkdown") path used by this bridge. Optimized for a future AI agent picking
this up cold: everything needed to understand, extend, or debug the feature is
here, with provenance. **The protocol below is confirmed end-to-end against a
real packet capture** (not inferred) — see *Provenance*. No further live probing
is needed for the protocol itself.

## TL;DR

To make the doorbell speak:

1. Mint a legacy **MWS SessionToken** (`mobile.alarm.com`) off the existing
   website session — `mobile_auth.MobileSession` / `mint_if_stale`.
2. **`GetDoorbellCameraList`** → the `<cli>` element already carries the
   ready-to-use per-session upstream-audio PUT URLs, the Digest credentials, and
   `did`. No separate "open stream" call is needed first.
3. **`StartFullDuplex&DeviceId=<did>`** → `<sfdr ts="0" s="true"/>` arms the
   doorbell mic-input path.
4. **Chunked HTTP `PUT`** of raw **G.711 µ-law / 8 kHz / mono** audio to the
   PutAudioStream URL, **HTTP Digest** auth (`vieweruser` + per-session
   password), one small chunk per request on a keep-alive connection.
5. **`EndLiveStream&DeviceId=<did>&ForDevice=0`** → `<elsr ts="0" s="true"/>`
   teardown.

Code map: `mobile_auth.py` (auth) → `mws_client.py` (steps 2/3/5 + parsers) →
`talk_fullduplex.py` (step 4 transport) → `talk.py` (`TalkManager`
orchestration) → `control_server.py` (`POST /control/cameras/{id}/talk`).

## Why the "obvious" paths do NOT work for this doorbell

- **Website REST/Janus path** (what the rest of the bridge uses for *video*):
  for the doorbell `liveVideoHighestResSources` returns
  `supportsAudio=false, audioUpStreamUrl=null` — no talk. Dead end.
- **`GetLiveVideoStream` `<webrtc>`/`<srtp>` endpoints** (the app's path for
  newer ADC cameras): the VDB750 returns **only `<rtsp>`** (video + listen),
  `SupportsWebRTC=false`, `StreamProtocol=0` (RTSP). So `talk_webrtc.py` /
  `talk_srtp.py` **do not apply to this doorbell** — they remain in the tree for
  other/newer ADC cameras and are the fallback path in `talk.py`, but for the
  VDB750 the full-duplex path is the only one that works.
- The VDB750 is a SkyBell-lineage RTSP/OpenEye device. Its upstream audio is a
  proprietary HTTP-PUT backchannel (`adcsouphttpclientsink` in the native
  `libvideo-streamer.so`), **not** WebRTC or SRTP. That backchannel is exactly
  the PutAudioStream PUT documented here.

## Device identity (this install)

| Fact | Value |
|---|---|
| Model | `ADC-VDB750` (firmware `02091`) |
| MWS `did` | `2048` |
| `UnitId` | `<unit-id>` |
| Website globalDeviceId | `<unit-id>-<did>` (= `UnitId-did`) |
| LAN IP | `<doorbell-ip>` (private VLAN, same LAN as the bridge `<bridge-ip>`) |
| Capability flags | `susa=true` (upstream audio), `sdsa=true` (downstream audio), `sfd=true` (full duplex), `StreamProtocol=0` (RTSP), `SupportsWebRTC=false` |

## Step 2 — `GetDoorbellCameraList` (`<gdclr>`), the key call

POST form-urlencoded to `https://mobile.alarm.com/MobileServlet/SubmitRequest.aspx`
with `Action=GetDoorbellCameraList`, the full BaseTokenRequest envelope
(`SessionToken`, `CustomerId`, device fields incl. `Haiku`, and `SeamlessLoginToken`
when present — see `mws_client._base_token_form`), plus `MaxHeight`/`MaxWidth`.

**Responses are gzip WITHOUT a `Content-Encoding` header** — gunzip manually
(`mobile_auth.read_body_text` handles this). XML fields are **attributes**, not
child text. `TokenStatus` is attribute **`ts`** (not `TokenStatus`); success is
`ts==0`. Error is `em`.

Real response shape (secrets redacted; one `<cli>` per doorbell under `<dcl>`):

```xml
<gdclr ts="0" s="true" ...>
  <dcl>
    <cli model="ADC-VDB750" cd="Doorbell" did="2048" UnitId="<unit-id>"
         Firmware="02091" StreamProtocol="0" SupportsWebRTC="false"
         susa="true" sdsa="true" sfd="true"
         l="vieweruser" p="<PER-SESSION-PASSWORD>"
         sid="<opaque>" cstet="<timestamp>"
         re  ="rtsp://vieweruser:<PW>@videostreamnaNN.devicetask.com:8090/api/GetStream?streamID=0"
         dre ="rtsp://vieweruser:<PW>@<doorbell-ip>:41925/api/GetStream?streamID=0"
         lre ="rtsp://vieweruser:<PW>@<doorbell-ip>:41925/api/GetStream?streamID=0"
         pre ="rtsp://vieweruser:<PW>@<PUBLIC-IP>:41925/api/GetStream?streamID=0"
         uae ="https://videostreamnaNN.devicetask.com:8090/api/PutAudioStream"
         duae="https://<doorbell-ip>:41925/api/PutAudioStream"
         luae="https://<doorbell-ip>:41925/api/PutAudioStream"
         puae="https://<PUBLIC-IP>:41925/api/PutAudioStream" />
  </dcl>
</gdclr>
```

### `<cli>` attribute reference (talkdown-relevant)

| Attr | Meaning | Used for |
|---|---|---|
| `did` | MWS device id (`2048`) | `StartFullDuplex`/`EndLiveStream` `DeviceId` |
| `UnitId` | account/unit id (`<unit-id>`) | forms `global_device_id` |
| `l` | stream username (`vieweruser`) | HTTP Digest username |
| `p` | **per-session stream password** | HTTP Digest password |
| `uae` | upstream PUT URL — **cloud relay** | WAN fallback (carries `?camera_id=` when relayed) |
| `duae` | upstream PUT URL — **direct/LAN** | **bridge's primary** (on-LAN with doorbell) |
| `luae` | upstream PUT URL — **local/LAN** | LAN alternate |
| `puae` | upstream PUT URL — **public/WAN** | WAN (public IP) |
| `re`/`dre`/`lre`/`pre` | downstream RTSP (cloud/direct/local/public) | video + LISTEN audio (not talkdown) |
| `susa` | supports upstream audio | gate: only attempt talkdown if true |
| `sfd` | supports full duplex | informational |
| `sid` / `cstet` | session token / expiry timestamp | session lifetime bound |

**Key insight:** `GetDoorbellCameraList` itself mints a stream session — the port
(e.g. `41925`) is **ephemeral/per-session** and the password `p` is
**per-session**. Do not cache them across talk sessions; fetch fresh each time
(the bridge does: `_resolve_doorbell` calls it at the start of every `speak`).

Parser: `mws_client.parse_doorbell_camera_list` → per-doorbell dict with
`put_urls` `{cloud,direct,local,public}` and `rtsp_urls` likewise, `stream_user`,
`stream_password`, `did`, `global_device_id`, `supports_upstream_audio`, etc. It
is unit-tested against the real capture (see *Provenance*).

## Step 3 — `StartFullDuplex`

POST `Action=StartFullDuplex` + envelope + `DeviceId=<did>` (no `ForDevice`).
Response: `<sfdr ts="0" dsl="false" s="true" crs="0"/>`. **Success = `ts==0` AND
`s=="true"`.** This only arms the mic path; it returns no URL (the URLs came from
step 2). `mws_client.start_full_duplex`.

## Step 4 — the PutAudioStream PUT (the actual audio)

Transport: `talk_fullduplex.stream_talkdown(put_url, password, audio_source, *, username, ...)`.

- **Method** `PUT`, repeated — **one small audio chunk per request** on a single
  keep-alive TCP connection (libsoup-style). The bridge sends 40 ms (320 B)
  chunks and paces at real time.
- **Auth: HTTP Digest**, MD5, `qop=auth`, `nc` incrementing per request.
  `username="vieweruser"` (`l`), `realm="Alarm.com"`, password = `p`. The doorbell
  answers the first PUT with `401 WWW-Authenticate: Digest ...`; parse the
  `nonce`/`realm`/`opaque` and authenticate every subsequent PUT.
- **Body: raw G.711 µ-law (PCMU), 8 kHz, mono. No RTP header.** Sizes are
  multiples of 80 bytes (10 ms). `talk_fullduplex._encode_mulaw` shells to
  `ffmpeg -i <src> -ac 1 -ar 8000 -f mulaw pipe:1`, so **any input format works**
  (WAV, MP3, TTS, a music file). 8 kHz mono µ-law is telephone-grade — music
  plays but sounds lo-fi; that is a codec limit of the doorbell, not a bug.
- **TLS:** the doorbell serves HTTPS with a **self-signed cert** → verification is
  disabled for the PUT connection (`ssl=False`). This is scoped to the talk PUT
  only.
- **User-Agent** sent by the app: `GStreamer souphttpclientsink libsoup/3.6.0`
  (the bridge mirrors it).
- The doorbell/relay replies with a tiny `application/json` ack per chunk.

### LAN vs WAN endpoint choice

The bridge is on the doorbell's LAN, so it prefers `duae`/`luae` (direct, no
relay). Order is `config.TALK_PUT_ENDPOINT_ORDER = (direct, local, public, cloud)`
(`talk._select_put_url`). Off-LAN the app uses `puae` (public IP) or the cloud
relay `uae` (which appends a `?camera_id=<token>` routing token); everything else
— Digest, µ-law body, chunking — is identical. Confirmed by a cellular capture.

## Step 5 — teardown

POST `Action=EndLiveStream` + envelope + `DeviceId=<did>&ForDevice=0`. Response
`<elsr ts="0" s="true" he="false"/>`. This is the teardown the app actually
issues after a full-duplex talk (an `EndFullDuplex` action also exists but was not
what the app sent). `mws_client.end_live_stream`; `talk._run_full_duplex` runs
it best-effort in a `finally` and always removes the spooled temp clip.

## Orchestration & safety (`talk.py`)

`TalkManager.speak(camera_id, audio_url=|audio_path=)`:
- one in-flight talk per camera (concurrent triggers → `TalkInProgress`/409);
- resolves audio to bytes under a size cap + timeout;
- `_resolve_doorbell` mints/reuses the MWS token via the **shared `login_lock`**
  (re-mints once on a server-side logout), then `GetDoorbellCameraList`; if the
  camera matches a doorbell advertising `susa`, takes the full-duplex path,
  otherwise falls back to the `GetLiveVideoStream` webrtc/srtp path;
- `_run_full_duplex`: `StartFullDuplex` → spool temp → `stream_talkdown` →
  (finally) `EndLiveStream` + unlink;
- everything bounded by `TALK_STREAM_LOOKUP_TIMEOUT_S` / `TALK_SESSION_TIMEOUT_S`;
- **no secrets logged** (password, SessionToken, nonce) — only non-secret routing
  facts (endpoint kind, chunk/byte counts, host).

Feature is **enabled by default** (`config.TALKDOWN_ENABLED`); the control route
returns 403 only when it has been explicitly disabled. Nothing plays unless a
caller asks for it.

## How to test (once enabled)

The endpoint is fully modular — it plays whatever audio you point it at. Creds are
NOT supplied here; the bridge mints its own MWS session from the TELUS credentials
that already live in Home Assistant (see repo README).

```bash
# control secret is per-install; path per control_server.py / config.CONTROL_SECRET_PATH
SECRET=$(ssh <ha-host> 'sudo cat <control_secret_path>')
# Play an arbitrary file the bridge can fetch (e.g. an mp3 music file):
curl -s -X POST http://127.0.0.1:8199/control/cameras/<unit-id>-<did>/talk \
  -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
  -d '{"audio_url":"http://127.0.0.1:8123/local/test.mp3"}'
# -> {"path":"full_duplex","chunks":N,"bytes":M}
```

Or via the HA service `telus_smarthome.speak` (TTS or a media URL). See
`docs/talkdown-DEPLOY.md`.

## Provenance (how this was recovered — for auditing the claims)

- App: `com.alarm.alarmmobile.android.telus` 5.10.7, jadx-decompiled. The MWS host
  and obfuscated strings were decoded via the app's `hi1.b` Java-`Random` XOR.
- Control plane + audio transport captured by frida gst/soup hooks on an **arm64**
  Android emulator on an Apple Silicon Mac (x86 emulators can't inject frida into the
  arm64-translated app — "need arm64 Gadget"). A decrypted HTTP capture (friTap +
  the app's TLS keys) contains the exact `GetDoorbellCameraList` `<gdclr>`,
  `StartFullDuplex` → `<sfdr>`, and `EndLiveStream` → `<elsr>` exchanges quoted
  above, plus (from a separate Mac capture) the `PutAudioStream` Digest PUT of
  µ-law audio.
- WAN behavior confirmed on a cellular hotspot (relay `uae` + `?camera_id=`).
- `parse_doorbell_camera_list` is unit-tested against the real `<gdclr>` body
  (all fields asserted: did/unit/user/put_urls/susa/sfd/protocol).
- **Capture files contained live per-session secrets and were deleted after the
  build; never commit them.** Per-session password/port are already expired.

## Caveats / open items

- Per-session `p` + ephemeral port ⇒ always call `GetDoorbellCameraList` fresh per
  talk; never persist them.
- `stream_talkdown` re-challenges (401) and retries a chunk once; if the doorbell
  rotates the nonce mid-stream beyond that, add re-challenge handling.
- Live-verified  (see "Live verification" section below): plays at the
  door, sustains >=10 min continuous, no renegotiation needed. Deploy/enable
  steps in `docs/talkdown-DEPLOY.md`.
- If a future firmware flips the VDB750 to WebRTC/SRTP, the `talk.py` fallback
  path already covers it; `susa`/`SupportsWebRTC` on `<cli>` tell you which.


## Live verification  (fired against the real doorbell)

Deployed inert-then-enabled and tested end to end against the live ADC-VDB750.
Findings:
- **It works.** A tone streamed to `duae` (direct/LAN `<doorbell-ip>:41925`) plays
  at the doorbell. Response `{"status":"spoken","path":"full_duplex","seconds":N}`.
- **Login `lr` is not the success signal.** The live `<lalr>`/`<slnr>` returns
  `lr=0 cvs=0` with a fully valid session (st/slt/dcid present). `mobile_auth`
  now keys success off the **presence of a SessionToken**, not `lr==2` (the
  decompiled `il0/h.java R` predicate was wrong for this account). An
  AccountSetupUrl (`asu`) still hard-fails.
- **Session length so far: >=10 min with no server-side limit hit.** A
  continuous 10-minute talk ran with zero server-side cutoff, 401 re-challenge,
  or connection drop (`cstet` on `<cli>` did not bite on an active PUT stream),
  so the current transport already covers typical announcements and music
  tracks. Longer/robust streaming is handled by session renegotiation -- see
  [backlog.md](backlog.md) E1.
- **Pacing must be schedule-based, not sleep-after-each-PUT.** The naive
  `PUT; sleep(CHUNK_MS)` added the PUT round-trip on top of every sleep and
  drifted ~10 percent (600s audio took ~660s). Fixed: sleep until each chunk's
  real-time slot (`start + idx*CHUNK_MS`), which absorbs PUT latency so
  wall-clock tracks audio time (60s audio -> 59.5s wall).
- **Prebuffer to avoid choppy playback.** Delivering chunks exactly at playback
  time can underrun into gaps; a small constant lead (`PREBUFFER_MS=500`) keeps a
  jitter buffer at the doorbell so playback is continuous. (A "morse code" report
  during testing was actually an `apulsator` pulsing TEST TONE, not the transport
  -- a continuous tone plays smoothly.)
- Audio is 8 kHz mono mu-law = telephone-grade. Raw full-range music clipped/
  sounded harsh on the small speaker, so a conditioning filter (E-COND, see
  [backlog.md](backlog.md)) is applied by default (band-limit + compress +
  hard-limit), tuned by ear and toggleable per request -- music is clean, just
  lo-fi. L16 was tried for higher fidelity but THIS doorbell's PUT path is
  mu-law only (backlog E4). The
  session bound is derived from the audio length (clip + margin), not a
  fixed/env timeout.

## Enhancement backlog

Every planned improvement, deferred idea, blocked item, and settled question —
for this feature and for the adjacent surfaces the project has grown into —
lives in [backlog.md](backlog.md), with stable `E<n>` IDs and the evidence
behind each status.
