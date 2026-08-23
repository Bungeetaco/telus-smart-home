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
(`mobile_auth._read_body_text` handles this). XML fields are **attributes**, not
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
(the bridge does: `_resolve_doorbell` calls it at the start of every `speak()`).

Parser: `mws_client.parse_doorbell_camera_list()` → per-doorbell dict with
`put_urls` `{cloud,direct,local,public}` and `rtsp_urls` likewise, `stream_user`,
`stream_password`, `did`, `global_device_id`, `supports_upstream_audio`, etc. It
is unit-tested against the real capture (see *Provenance*).

## Step 3 — `StartFullDuplex`

POST `Action=StartFullDuplex` + envelope + `DeviceId=<did>` (no `ForDevice`).
Response: `<sfdr ts="0" dsl="false" s="true" crs="0"/>`. **Success = `ts==0` AND
`s=="true"`.** This only arms the mic path; it returns no URL (the URLs came from
step 2). `mws_client.start_full_duplex()`.

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
  multiples of 80 bytes (10 ms). `talk_fullduplex._encode_mulaw()` shells to
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
what the app sent). `mws_client.end_live_stream()`; `talk._run_full_duplex` runs
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

Feature is **inert until `config.TALKDOWN_ENABLED=true`** — the control route
returns 403 otherwise.

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
- Live-verified 2026-08-23 (see "Live verification" section below): plays at the
  door, sustains >=10 min continuous, no renegotiation needed. Deploy/enable
  steps in `docs/talkdown-DEPLOY.md`.
- If a future firmware flips the VDB750 to WebRTC/SRTP, the `talk.py` fallback
  path already covers it; `susa`/`SupportsWebRTC` on `<cli>` tell you which.


## Live verification 2026-08-23 (fired against the real doorbell)

Deployed inert-then-enabled and tested end to end against the live ADC-VDB750.
Findings:
- **It works.** A tone streamed to `duae` (direct/LAN `<doorbell-ip>:41925`) plays
  at the doorbell. Response `{"status":"spoken","path":"full_duplex","seconds":N}`.
- **Login `lr` is not the success signal.** The live `<lalr>`/`<slnr>` returns
  `lr=0 cvs=0` with a fully valid session (st/slt/dcid present). `mobile_auth`
  now keys success off the **presence of a SessionToken**, not `lr==2` (the
  decompiled `il0/h.java R()` predicate was wrong for this account). An
  AccountSetupUrl (`asu`) still hard-fails.
- **Session length so far: >=10 min with no server-side limit hit.** A
  continuous 10-minute talk ran with zero server-side cutoff, 401 re-challenge,
  or connection drop (`cstet` on `<cli>` did not bite on an active PUT stream),
  so the current transport already covers typical announcements and music
  tracks. Longer/robust streaming is a **planned enhancement** -- see
  **Planned enhancements & known gaps** below (E1).
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
  sounded harsh on the small speaker, so a conditioning filter (E-COND) is
  applied by default (band-limit + compress + hard-limit), tuned by ear and
  toggleable per request -- music is clean, just lo-fi. L16 was tried for
  higher fidelity but THIS doorbell's PUT path is mu-law only (E4). The
  session bound is derived from the audio length (clip + margin), not a
  fixed/env timeout.

## Planned enhancements & known gaps

Standing practice for this feature: **every known gap or potential improvement,
however minor, is logged here** rather than dropped -- so a future agent (or the
maintainer) can pick it up. "Not implemented" means "not yet," never "unwanted."
Add to this list whenever something is deferred, simplified, or noticed in
passing. Rough priority order; IDs are stable references.

### High value
_Implemented since live testing:_ **E-COND audio conditioning (with per-request
toggle).** `config.TALK_AUDIO_FILTER` runs an ffmpeg chain on every clip before
encoding -- high-pass 250 Hz (drop bass the small speaker overloads on),
low-pass 2.8 kHz (warmth), gentle compression, hard limiter at 0.5/0.6 (no
clipping). Tuned by ear on the live VDB750; fixed the clipping/harshness raw
full-range music produced. Applied to EVERY clip on the doorbell (full-duplex)
path regardless of source/format/URL (runs inside the encode step). Per-request
control on `POST /control/cameras/{id}/talk`: `"conditioning": false` disables
it, or `"audio_filter": "<ffmpeg -af chain>"` overrides it; omitted = the
config default. Remaining (minor): **E19** the webrtc/srtp fallback path (other
ADC cameras) does NOT yet apply the filter -- apply it there too so ALL talk
audio is conditioned regardless of path; **E20** the tuned chain is
VDB750-speaker-specific -- other speakers may want different cutoffs/levels.

- **E1 (DONE 2026-08-23) Session renegotiation -- long-duration + resilient streaming.**
  The PutAudioStream URL, port and password are per-session, so a long clip or a
  network blip can outlive the session the talk opened, and the failure lands
  mid-clip with the audio half-played. Implemented as follows:
    * `talk_fullduplex.stream_talkdown()` takes an optional async `renegotiate`
      callback and owns the audio byte offset. When a session dies mid-stream
      (5xx, a 401 the Digest middleware can't recover, a connection reset or a
      socket-read timeout) it calls the callback for fresh credentials, rebuilds
      the HTTP session, and **resumes from the exact byte offset** -- nothing lost,
      nothing repeated.
    * `talk.py` supplies that callback, keeping all MWS work on its side of the
      boundary: best-effort `EndLiveStream` on the dying session ->
      `GetDoorbellCameraList` for a fresh port/password/URL -> `StartFullDuplex`
      to re-arm. The final teardown follows whichever session ended up live.
    * Bounded by `config.TALK_MAX_RENEGOTIATIONS` (default 2), so a persistently
      broken session fails the request instead of looping.
    * The swap is dead air, not audio time: the safety deadline is extended by
      what the swap cost, and the pacing schedule is re-baselined so the stream
      continues at real time rather than bursting to "catch up" (which would
      overrun the doorbell's small buffer).
    * **Proactive variant** (`session_deadline`): when a session expiry is known,
      the transport swaps `config.TALK_RENEGOTIATE_MARGIN_S` early instead of
      waiting for a failed PUT. `talk.py` derives this from the `<cli>` `cstet`
      timestamp, but ONLY when that value is far enough in the future to be
      meaningful as an expiry -- its semantics are unconfirmed (in one capture it
      sat within seconds of the fetch, which would make it a creation time). If it
      isn't plausible the value is ignored and the reactive path stands. So today
      this is expected to be dormant; it activates by itself if `cstet` turns out
      to be a real expiry, with no code change.
    * Renegotiation is **opt-in**: without a callback, a mid-stream failure stays
      fatal exactly as before.
    * Covered by `camera-bridge/tests/test_talkdown_renegotiation.py` (run in CI):
      a local fake doorbell enforcing real Digest auth dies mid-clip, and the test
      asserts the bytes both sessions received, concatenated, are byte-identical
      to a clean single-pass encode -- plus the bound and the opt-in behaviour.
- **E2 Cancel / stop an in-progress talk.** There is no way to stop a playing
  clip early -- a long (e.g. 10-min music) talk only ends when the audio finishes
  or the safety deadline fires. Add a cancel path (control endpoint + a
  per-camera handle that cancels the transport task and tears the session down).
- **E3 HA integration + speak service -- DEPLOYED & VERIFIED 2026-08-23.**
  `custom_components/telus_smarthome` v1.5.1 is live (rsynced to
  `~/homeassistant/config/...`, HA restarted); `telus_smarthome.speak` is
  registered and plays through the doorbell end to end via BOTH paths:
  `media_content_id` = a direct URL to a self-hosted TTS engine on the host
  (e.g. `http://127.0.0.1:<tts-port>/speak?text=...`), which bypasses HA TTS
  entirely, and `message` = HA cloud/Google TTS. Two things the
  live test required: (1) a one-char fix -- the handler re-validated `call.data`
  (a ReadOnlyDict) with voluptuous which coerces in place -> crash; fixed by
  validating `dict(call.data)`; (2) the `message` path needs HA's `internal_url`
  set (was None) so the bridge can fetch the absolute `/api/tts_proxy` URL -- set
  to `http://<ha-host-ip>:8123` via WS `config/core/update`. All speak audio gets
  the default conditioning.
  Still TODO: wire the actual door-greeting AUTOMATION (the example
  `docs/talkdown-automation.yaml` is a placeholder; if you already run
  door-greeting logic elsewhere, integrate talkdown there rather than as a
  standalone rule).

### Audio quality / transport
- **E4 L16 codec -- TESTED, unsupported on THIS doorbell's PUT path (device-
  specific).** L16 (16-bit linear, `skyL16pay`/pt97) was implemented and fired at
  the live ADC-VDB750 (fw 02091) over `/api/PutAudioStream` in BOTH endiannesses
  (`s16be` and `s16le`, Content-Type `audio/L16;rate=8000;channels=1`): both came
  out as **noise** -- this doorbell decodes the PUT body as mu-law regardless of
  Content-Type. So mu-law is the only working codec on this device's PUT path.
  **This is a per-model/firmware limit, NOT alarm.com-wide** -- other alarm.com
  camera/doorbell models may support L16 (newer ADC cameras carry L16 via the
  webrtc/srtp path, see `talk_srtp.py`). Remaining avenues for L16 on the VDB750:
  the OSP/RTSP backchannel (`skyL16pay` lives there, a bigger RE effort), or an
  undiscovered codec selector (query param / StartFullDuplex field). The `CODECS`
  table + per-request `codec` plumbing + primary/fallback are kept in place so a
  working codec can be dropped in per device. Quality on the VDB750 is instead
  handled by E-COND below.
- **E5 Streaming encode** -- pipe `ffmpeg` stdout straight into the PUT loop
  instead of buffering the whole mu-law clip in RAM and spooling a temp file.
  Lowers memory + latency-to-first-audio for long/large files and removes the
  `TALK_MEDIA_MAX_BYTES` ceiling concern.
- **E6 Tune/auto-tune `CHUNK_MS`** (larger chunks = fewer PUT round-trips/less
  overhead) and **`PREBUFFER_MS`** (adaptive to observed jitter) instead of the
  fixed 40 ms / 500 ms.
- **E7 Explicit Digest `stale=true` handling / nonce refresh** without a full
  re-challenge, plus `nc` rollover safety, for very long streams.
- **E8 Confirm playback from the ack** -- parse the doorbell's per-chunk JSON ack
  body, not just the HTTP 2xx, so a silently-dropped-but-200 case is caught.
- **E9 Volume control** -- investigate `duav` (v/min/max, `SupportsVolumeAdjustment`
  observed false); confirm whether talkdown volume can be set at all.

### Robustness / networking
- **E10 Auto-detect LAN vs WAN reachability** to choose the PutAudioStream
  endpoint at runtime, instead of the static `TALK_PUT_ENDPOINT_ORDER`.
- **E11 Reconnect on a dropped keep-alive connection** (a subset of E1).
- **E12 Pin the doorbell's self-signed cert** (fingerprint) instead of disabling
  TLS verification entirely for the PUT.

### Fallback path (other ADC cameras)
- **E13 Validate the webrtc/srtp fallback.** `talk_webrtc.py` / `talk_srtp.py`
  were built on the pre-capture hypothesis and have NOT been verified against a
  real webrtc/srtp-capable ADC camera; validate when such a device exists.

### Testing / observability
- **E14 (PARTIAL) Repo unit tests** -- `camera-bridge/tests/` now exists and runs
  in CI, covering talkdown session renegotiation. Still wanted: for the transport (`_Digest` header correctness,
  schedule-based pacing, prebuffer) and for `parse_doorbell_camera_list` (the
  parser was verified ad hoc against a capture but no test is committed).
- **E15 Metrics** -- emit talk success/failure counts + durations for observability.

### Dependency-driven (from the 2026-08 aiortc 1.15 / aiohttp 3.14 upgrade)
- **E21 (DONE 2026-08-23) Replaced the hand-rolled HTTP Digest with aiohttp's `DigestAuthMiddleware`.**
  Implemented and verified against the live doorbell (150 chunks streamed clean);
  ~50 lines of bespoke RFC 2617 code (`_Digest`, `_parse_challenge`, `_h`) deleted.
  Original rationale:
  `talk_fullduplex.py` implements RFC 2617 Digest (MD5, `qop=auth`, `nc` counter)
  by hand because aiohttp had no client-side Digest support when it was written.
  aiohttp **3.12** added `DigestAuthMiddleware`, and **3.14** fixed it to sign the
  *encoded* request-target (which matters for the relay `uae` endpoint, whose URL
  carries a `?camera_id=` token). The bridge now runs aiohttp 3.14.x, so this is
  actionable: swapping to the middleware deletes bespoke auth code (`_Digest`,
  `_parse_challenge`, `_h`) and inherits upstream fixes. Verify the doorbell still
  accepts the middleware's header ordering/format before committing to it.
- **E22 (DONE 2026-08-23) Bumped PyAV to 17.1.0 for the per-frame memory-leak fix.**
  Note aiortc pins `av<18.0.0`, so 17.1.0 is the ceiling -- an automated 18.x bump
  would conflict. Verify RSS over time. Original rationale: aiortc 1.15.0
  relaxed its PyAV pin to allow 17.x specifically because it *"fixes a per-frame
  memory leak"*. This bridge is the worst case for that leak: every frame of every
  camera crosses into Python (`janus_client` `track.recv()` -> `ffmpeg_sink`
  `frame.to_ndarray()`), 24/7, in a container capped at 768 MB. `requirements.txt`
  still pins `av==16.1.0`, so the fix is NOT yet in effect. Bump and watch RSS over
  a few days (baseline after the 1.15/3.14 upgrade: ~316 MiB with 2 cameras).
- **E23 (DECIDED: keep, gated) `aiortc.sdp.candidate_from_sdp` is a private-API dependency.**
  There is no public equivalent (confirmed: it is not in aiortc's `__all__`), and
  vendoring an SDP candidate parser would trade a checked dependency for silently
  drifting duplicate logic. Kept, with aiortc marked review-required in Renovate and
  a CI job that imports every bridge module. Re-check on each aiortc bump. Detail:
  `janus_client.py` and `talk_webrtc.py` import it from `aiortc.sdp`, an internal
  module with no stability guarantee, and it sits in the live video path. It still
  exists unchanged in 1.15.0 (verified), but every aiortc bump must re-check it.
  Options: keep it and rely on the CI/dep-review gate, or vendor a small local SDP
  candidate parser so the video path stops depending on library internals.

### Minor
- **E16 Cache `GetDoorbellCameraList`** within the `cstet` window to save a round
  trip on rapid repeat talks.
- **E17 Also send `EndFullDuplex`** (we use `EndLiveStream`, which the app used);
  send both for symmetry if a session is ever seen lingering.
- **E18 Document `CHUNK_MS` / `PREBUFFER_MS`** as tunables in config comments.
