# alarm.com Backend API Reference

This project drives an Alarm.com security/camera account through a TELUS Smart Home
white-label deployment. TELUS Smart Home is a re-branded Alarm.com dealer portal:
the REST/websocket API is Alarm.com's own, only the login handshake is TELUS-specific.

There are **two entirely separate backends** in play, with different hosts, auth
tokens, wire formats, and capabilities. Everything below was reverse-engineered from
HAR captures of a real browser session and from a decompiled copy of the native
Alarm.com mobile app; it is a protocol reference, not an official spec.

| | Website API | Legacy mobile "MWS" API |
|---|---|---|
| Host | `smarthome-security.telus.com` (dealer portal) | `mobile.alarm.com/MobileServlet` |
| Auth gateway | `auth-gateway.telus.com` (PingFederate + Cloudflare) | same SSO edge, reused |
| Session credential | AJAX cookie session + `ajaxrequestuniquekey` header | MWS `SessionToken` (opaque) |
| Request encoding | JSON:API (`application/vnd.api+json`) | `application/x-www-form-urlencoded` |
| Response encoding | JSON:API | XML (fields as **attributes**) |
| Real-time | push websocket | none (request/response only) |
| What it unlocks | video (downstream), events, automation rules, snapshots | full-duplex talkdown / doorbell audio endpoints |

The website SPA never talks to `mobile.alarm.com`, and the mobile app never uses the
website AJAX session. The two are bridged only through the shared SSO identity: an
authenticated website session's cookie jar can be replayed against the mobile SSO
servlet to mint an MWS `SessionToken` without re-entering credentials.

Relevant modules:

- Website session/login: `camera-bridge/bridge/telus_auth.py`, and the Home Assistant
  integration's `custom_components/telus_smarthome/telus_auth_patch.py` (same flow,
  monkeypatched into `pyalarmdotcomajax`).
- Website video/events/rules calls: `camera-bridge/bridge/telus_rest.py`.
- Janus streaming (downstream video): `camera-bridge/bridge/janus_client.py`.
- MWS auth: `camera-bridge/bridge/mobile_auth.py`.
- MWS actions (stream/doorbell/talkdown): `camera-bridge/bridge/mws_client.py`.

---

## Part 1 — Website API

### 1.1 SSO login flow

Login is a three-request chain against the TELUS identity gateway, followed by an
OAuth2 authorization-code redirect that lands on the dealer portal and sets the
real session cookie. No password is ever sent to `alarm.com` itself.

Constants used to build the flow (TELUS white-label, not per-account):

```
TELUS_BASE          = https://smarthome-security.telus.com/
AUTH_GATEWAY        = https://auth-gateway.telus.com
TELUS_CLIENT_ID     = <telus-client-id>
TELUS_AUTH_PROVIDER = <telus-auth-provider-id>
```

**Step 0 — clear cookies.** The gateway rejects a fresh login outright ("Logout or
clear cookies first") if stale cookies from a prior login are still present, so the
client wipes its cookie jar before *every* login attempt, not just the first. The
aiohttp session must be dedicated to this integration for that to be safe.

**Step 1 — submit credentials** (XHR/fetch shape, JSON body):

```
POST {AUTH_GATEWAY}/v1/flows/smarthome_login_v1
Content-Type: application/json
Origin: https://www.telus.com
{"authdomain":"telus","loginid":"<email>","password":"<password>"}
```

Expect `200`. A `4xx` other than `429` means bad credentials. A `429` or `5xx` is
**transient**, not a credentials failure (see [rate limiting](#15-rate-limiting-cloudflare-429)).

**Step 2 — complete the login flow** (issues the gateway session):

```
POST {AUTH_GATEWAY}/v1/flows/login?acr=ci-loa2-smarthome
Content-Type: application/json
{}
```

The empty JSON body is required. A truly bodiless POST (no `Content-Type` at all)
gets a `415` from the gateway. Expect `200`.

**Step 3 — OAuth2 authorization code grant**, following the full redirect chain:

```
GET {AUTH_GATEWAY}/as/authorization.oauth2
    ?response_type=code
    &scope=adc%20openid
    &redirect_uri={TELUS_BASE}web/api/TokenSso.ashx?AuthProvider={TELUS_AUTH_PROVIDER}&sso_version=2
    &state={TELUS_BASE}web/DetermineLandingPage.aspx
    &client_id={TELUS_CLIENT_ID}
    &acr_values=ci-loa2-smarthome
    &service_type=adc
    &from=login
Accept: text/html,application/xhtml+xml
upgrade-insecure-requests: 1
```

Follow redirects. The chain is `auth-gateway` → `TokenSso.ashx` →
`DetermineLandingPage.aspx`; `TokenSso.ashx` is what actually sets the dealer-portal
session cookie on `smarthome-security.telus.com`. **Success predicate:** the final
URL contains `DetermineLandingPage` *or* `web/system`. Landing anywhere else means
the SSO handoff failed — most likely an MFA/OTP challenge, which this flow does not
handle (none was observed in the captured login; a `2FA` gate at the TELUS gateway
would break here).

Send browser-shaped headers throughout (`Origin: https://www.telus.com`,
`Referer: https://www.telus.com/`, a real desktop `User-Agent`). Steps 1–2 use an
XHR `Accept: */*`; step 3 uses a navigation `Accept: text/html…`.

### 1.2 AJAX / cookie session

After login the session is cookie-based. Two things are extracted for subsequent
API calls:

- **`ajaxrequestuniquekey` header** — the value of the `afg` anti-forgery cookie set
  during the SSO handoff. Every authenticated `web/api/*` call must echo it as this
  header. (It may be absent for some vendors; treat as best-effort.)
- **`Context-Id` header** — identifies which Alarm.com *system* (site) to operate
  against. It is **not** the account or user id; it is the `id` of whichever system
  has `isSelected: true` in the available-systems list. A real browser sends it on
  every `web/api/video/*` call, and also on automation-rule and video-event calls.

Resolve the Context-Id right after login:

```
GET {TELUS_BASE}web/api/systems/availableSystemItems?searchString=
Accept: application/vnd.api+json
ajaxrequestuniquekey: <afg cookie value>
```

Walk `data[]`; take the `id` of the entry whose `attributes.isSelected` is true, else
fall back to the first (usually only) entry.

The standard header set for any authenticated JSON:API call is therefore:

```
Accept: application/vnd.api+json
ajaxrequestuniquekey: <afg>
Context-Id: <selected system id>
```

Home Assistant note: the same login flow is monkeypatched into `pyalarmdotcomajax`
by `telus_auth_patch.py`, which additionally rewrites `URL_BASE` to the dealer host
and repoints a camera-settings extension whose `alarm.com` endpoint was baked in at
import time. It also treats the library's native `2FA`-requirements check as
best-effort (the white-label deployment doesn't expose Alarm.com's native 2FA engine
for that endpoint, since auth already happened at the TELUS gateway), and maps
transient login statuses to a retry-with-backoff state instead of a hard
credentials failure.

### 1.3 JSON:API conventions

Responses follow the JSON:API shape: a `data` array (or object) of resources, each
with `id`, `type`, `attributes`, and `relationships`. Cross-resource links appear
under `relationships.<name>.data.{id,type}`, and (for history/media correlation) the
related resource may be included by id. Callers filter client-side where the API
offers no server-side filter.

### 1.4 Video and account endpoints

All paths are relative to `TELUS_BASE` and use the header set from §1.2.
(See `telus_rest.py` for the exact field mapping.)

| Endpoint | Method | Purpose / notes |
|---|---|---|
| `web/api/systems/availableSystemItems?searchString=` | GET | list systems; source of the `Context-Id` |
| `web/api/video/devices/cameras?filterByLiveViewDisplayable=true` | GET | enumerate cameras (`id`, `description`, `macAddress`, `deviceModel`, `firmwareVersion`) |
| `web/api/video/videoSources/liveVideoHighestResSources/{camera_id}` | GET | **downstream live-video source** (see §1.4.1) |
| `web/api/video/snapshots/{camera_id}` | GET | one-shot signed JPEG still URL (see below) |
| `web/api/video/videoEvents` | GET | saved camera clips; `?page&pageSize&search&startTime&endTime`, or `?ids[]=A&ids[]=B` to batch by id |
| `web/api/activity/historyEvents?includeCommands=false&numberOfEvents=N` | GET | system-wide activity feed (arm/disarm, locks, doorbell presses, camera triggers) |
| `web/api/automation/rules/rules?filter[deviceType]=15` | GET | video automation rules; **the filter is mandatory** — omitting it returns `423`, not an unfiltered list |
| `web/api/automation/rules/rules/{id}/pauseRule` | POST | pause a rule; **empty body** (rule identified by the URL) |
| `web/api/automation/rules/rules/{id}/resumeRule` | POST | resume a rule; empty body |

Signed-URL behaviour worth knowing:

- **Snapshot / clip URLs point at a separate relay host** (not the dealer portal) and
  need no auth of their own — a bare GET with no headers succeeds. They are
  **short-lived** (minutes) and effectively single-use. Fetch promptly; never cache.
- `historyEvents` entries never carry thumbnail/download URLs themselves
  (`thumbnailUrl` is always null there). To attach clip media to an activity entry,
  read its `relationships.media.data[0].id`, then batch-fetch those ids via
  `videoEvents?ids[]=…`. Because the signed download URL expires within minutes but
  the clip itself does not, store the **videoEvent id**, not the URL, if you need to
  re-mint a link later.

#### 1.4.1 Live-video source (downstream)

`liveVideoHighestResSources/{camera_id}` returns, under `data.attributes`:

- `proxyUrl` — the RTP relay URI the Janus mountpoint pulls from.
- `janusGatewayUrl` — the Janus WebSocket gateway to connect to.
- `janusToken` — bearer token attached to every Janus message.
- `iceServers` — a **JSON string** (parse it) of STUN/TURN servers.

This path yields **video only** (downstream). For this account's doorbell it reports
`supportsAudio=false` / `audioUpStreamUrl=null` — there is no talkdown here. Upstream
audio comes from the mobile MWS API instead (Part 2).

The Janus side (`janus_client.py`) speaks the streaming plugin over a
`janus-protocol` WebSocket. Sequence: `create` session → `attach`
`janus.plugin.streaming` → `message{request:create, type:rtp, media_uri:proxyUrl, …}`
to build a mountpoint → `watch` it. Two protocol details that matter:

- A `watch` yields an **`ack` first, then a separate `event` carrying the `jsep`
  offer on the *same* transaction id**. A single-future waiter drops one of the two;
  keep draining that transaction's messages until `jsep` appears (and bail early on a
  `janus:"error"` reply, which carries no `jsep` and is followed by nothing).
- ICE candidates **trickle in asynchronously** as `janus:"trickle"` messages as soon
  as the offer lands; buffer any that arrive before `setRemoteDescription` and apply
  them after. The local answer embeds its own gathered candidates (no outbound
  trickle needed). A periodic `keepalive` on the session id keeps it alive; the
  server signals readiness with a `webrtcup` message.

### 1.5 Push websocket

The Alarm.com library maintains a real-time push websocket for device-state updates.
Two lifecycle facts a reimplementer must plan around:

- **The server drops the socket roughly every 6 minutes**, on every connection,
  regardless of keepalive pings — it is a token-lifetime cutoff, not an idle
  timeout. Keep a short reconnect timeout (≈30 s) and treat the websocket as
  best-effort: back it with a REST poll (≈300 s) so state still converges when the
  socket is down. Polling much faster than that risks Alarm.com rate-limiting.
- **A malformed/unknown-device push can kill the whole connection.** In the upstream
  library, an unrecognised device raised an unhandled error inside the read loop that
  propagated out and dropped the socket — so any event from a device not in the
  library's registry (e.g. a bridge-served camera) silently took down *all* real-time
  updates until the next poll. A robust client swallows per-message parse errors
  rather than letting one bad frame end the loop.

Diagnostic tip: a bulk update where *every* device logs an update in the same
millisecond is a REST poll, not a push; a genuine push touches a single device. Use
that to tell them apart in logs.

### 1.6 Rate limiting (Cloudflare 429)

Every `auth-gateway.telus.com` request — and, in Part 2, every `mobile.alarm.com`
request — sits behind the same Cloudflare edge. Under load it returns **HTTP 429 with
Cloudflare error 1015** ("Access denied … used Cloudflare to restrict access"). This
is the **edge** rate limiter, *not* an Alarm.com application-level account lockout.

Discipline that matters (learned the hard way — a fixed short retry schedule turned
one bad night into a multi-hour outage):

- **Honor `Retry-After` exactly.** The 429 response carries its own `Retry-After`
  (observed ≈101 s); sleep for precisely that. A fixed short retry just re-triggers
  the same window instead of clearing it. Cap any backoff generously (≈30 min), and
  never `min()` it down below what the server asked for.
- **Single-flight logins.** Serialize logins under one lock so concurrent callers
  can't interleave and mix two attempts' cookies.
- **De-duplicate the burst.** On a session expiry, every in-flight request 401s at
  roughly the same instant and each one queues to re-login. Because each login clears
  the whole cookie jar, they invalidate one another — a self-sustaining stampede
  against the Cloudflare-fronted gateway. Collapse the burst by capturing a
  monotonically-increasing "login generation" counter *before* taking the lock and
  re-checking it *after*: if it moved, another caller already refreshed the session,
  so skip. Bump the counter only on a *fully successful* login (last statement of the
  routine), so a login that raised partway through never looks refreshed to waiters.
- Restart-loop guard: because some failure paths exit the process, put a floor behind
  any auto-restart supervisor (persisted exit timestamps + escalating pre-exit sleep)
  so a sustained outage can't become an unbounded crash-restart loop, each restart
  re-running a full login into the same limiter.

---

## Part 2 — Legacy mobile "MWS" XML API

The Mobile Web Service (MWS) is the legacy backend the native Alarm.com app uses. It
lives at `https://mobile.alarm.com/MobileServlet/SubmitRequest.aspx`, speaks
`x-www-form-urlencoded` POSTs, and replies in XML. It is the only backend that
returns the doorbell/full-duplex **talkdown** endpoints; the website API's
`videoSources` gives downstream only.

MWS wire format was recovered from a decompiled copy of the native app and confirmed
end-to-end against a live capture. Everything below is the protocol; account-specific
values are shown as placeholders.

### 2.1 Auth chain → SessionToken

MWS calls authenticate with an opaque **`SessionToken`**, distinct from the website
cookie session. There is no website REST endpoint that mints it; the app drives the
mobile SSO servlet against the same PingFederate IdP session the website already
holds, catches a "login-as" token off the redirect, and exchanges it. Two paths
mirror the app (`mobile_auth.py`):

**Path A — first-time bootstrap (SSO → LoginAsLogin).** Used when no rolling token is
held, or when the rolling token is rejected. It hits the Cloudflare edge, so it gets
the same care as the website login (single-flight, honor `Retry-After`).

1. Set a cookie on `mobile.alarm.com` before the SSO GET:
   `twoFactorAuthenticationId=<device-uid>:` (the app uses `<uid>:<2faId>`; a fresh
   device has an empty 2faId).
2. `GET https://mobile.alarm.com/MobileServlet/SSO/SSORedirectHandler.ashx?AppType=66`
   with the website SSO cookie jar. Follow the redirect chain **by hand** with
   redirects disabled — it terminates in a custom-scheme hop
   (`alarmtelus://loginaslogin?…`) that an HTTP client cannot auto-follow. Scan every
   hop's **query *and* fragment** for the login-as token. The chain observed:
   `SSORedirectHandler → auth-gateway.telus.com OAuth code → TokenSso.ashx →
   alarmtelus://loginaslogin?token=…`. The token param on the terminal hop is named
   **`token`** (length ≈50); older analysis expected `loginAsKey`/`loginAsLoginToken`,
   so a robust reader checks those names case-insensitively and falls back to the
   longest opaque value on the custom-scheme hop.
3. Exchange it:

   ```
   POST {MWS_SUBMIT}
   Content-Type: application/x-www-form-urlencoded
   Action=LoginAsLogin&LoginAsLoginToken=<token>&RememberMe=true&HashCode=0
   &IncludeDealerBranding=true&IncludePushSettings=false&IncludeDashboard=false
   &ForceLoadSafetyButton=false
   <...device/token envelope, see §2.3...>
   ```

   Response root element `<lalr …/>` (attributes, see §2.4). Absorb `st`
   (SessionToken), `slt` (rolling SeamlessLoginToken), `dcid` (CustomerId).

**Path B — silent re-login (SeamlessLoginNew).** The steady-state / restart path.
Once you hold a rolling `slt`, a single form POST re-mints the SessionToken with **no
SSO / Cloudflare hop** — this is the rate-limit-safe path, preferred whenever an
`slt` is held:

```
POST {MWS_SUBMIT}
Action=SeamlessLoginNew&SeamlessLoginToken=<slt>
<...device/token envelope...>
```

Response root `<slnr …/>`. On a genuine rejection (not a 429) fall back to Path A.

State to persist across restarts: the stable **device uid** and the rolling **`slt`**
(atomic write, fail-open). Do **not** persist the SessionToken — it is short-lived
and always re-minted in memory. A failed mint (as opposed to a 429) should start a
cool-down (≈5 min) so a caller that re-triggers on every event can't re-walk the SSO
bootstrap against the edge; a 429 is not converted into that cool-down, since it
carries its own `Retry-After`.

### 2.2 Request/response format

- **Request:** `POST …/SubmitRequest.aspx`, `Content-Type:
  application/x-www-form-urlencoded`. The body is `Action=<name>` plus the device/token
  envelope plus per-action params. Send a mobile-app-ish `User-Agent`.
- **Response:** XML. The fields you want are **attributes on a per-action root
  element**, not child text nodes. Each action has its own root tag (`<lalr>`,
  `<slnr>`, `<lvsr>`, `<gdclr>`, `<sfdr>`, `<elsr>`, …). Parse namespace-tolerantly
  (match on the local, namespace-stripped tag name) and read scalars from
  `element.attrib`. Use a hardened XML parser (e.g. `defusedxml`) — the body is
  attacker-reachable.

### 2.3 The device/token envelope (BaseTokenRequest)

Every authenticated MWS action ("BaseTokenRequest") carries the same device-identity
envelope the app attaches to all token requests. Sending only
`Action`/`SessionToken`/`DeviceId` makes server rejection likely — `CustomerId`
(scoping) and the app-integrity constant were the missing pieces. The canonical set:

| Field | Value / meaning |
|---|---|
| `UseNewSessionManager` | `true` |
| `MobileDeviceUid` | stable per-install uid (persisted) |
| `MobileDeviceOsVersion` | e.g. `34` |
| `MobileDeviceModel` / `MobileManufacturer` | e.g. `Pixel 7` / `Google` |
| `MobileDeviceType` | `2` (Android phone; `7` = Fire TV) |
| `ApplicationBuildNumber` / `BuildString` | app build / version (e.g. `2260` / `5.10.7`) |
| `Culture` | e.g. `en-US` |
| `Haiku` | static app-integrity secret baked into the app build — treat as a secret constant (`<app-haiku>`); gates the endpoints, and must never be logged |
| `TwoFactorId` | `<device-uid>:<2faId>` (fresh device: `<device-uid>:`) |
| `CustomerId` | `dcid` from the login response; sent on scoped actions |
| `SeamlessLoginToken` | the rolling `slt`, when held |
| `SessionToken` | the minted token, on every action after login |

Never log `SessionToken`, `SeamlessLoginToken`, `LoginAsLoginToken`, `Haiku`, or
`TwoFactorId` — redact them out of any form dump.

### 2.4 Quirks that cost debugging time

These are the non-obvious behaviours that break a naive reimplementation. Each one
was a real debugging session.

1. **Responses are gzip *without* a `Content-Encoding` header.** `SubmitRequest.aspx`
   gzips the body but omits the header, so HTTP clients don't auto-decompress and a
   `.text()` call chokes on the `1f 8b` gzip magic. Read the raw bytes and gunzip
   manually when they start with `\x1f\x8b`, else decode as UTF-8.

2. **XML fields are attributes, not elements.** Read every scalar from the root
   element's `attrib`, matched by local (namespace-stripped) name. Do not look for
   child text nodes.

3. **`TokenStatus` is the attribute `ts`, not `TokenStatus`.** The video/doorbell
   responses expose it as `ts`; fall back to a literal `TokenStatus` attribute only
   defensively. `TokenStatus` semantics:

   | value | meaning | action |
   |---|---|---|
   | `0` | success — stream/data present | proceed |
   | `1` | session token invalid/expired | re-auth |
   | `2` | account lacks the entitlement | do **not** retry |
   | `3` | transient server error | back off and retry |

4. **Login success = a `SessionToken` (`st`) is present, not `lr == 2`.** The
   decompiled app checks `LoginResult == 2`, but the live account returns **`lr=0`
   with a full, working session** (`st`/`slt`/`dcid` all present). Gating on `lr==2`
   rejected valid logins ("login failed lr=0 cvs=0" on a session that was fine).
   Trust the returned token; treat `lr` (LoginResult) and `cvs` (ClientVersionStatus)
   as diagnostics only. One hard-fail still applies: an `asu` (AccountSetupUrl)
   attribute means the account isn't provisioned for mobile.

5. **Cloudflare 429 + `Retry-After` discipline** — identical to §1.6, and *sharper*
   here because the bootstrap path walks up to ~10 GETs plus a POST against the same
   edge. Single-flight the mint, dedup the burst via a login-generation counter, and
   honor the server's `Retry-After` rather than any fixed schedule. Distinguish a
   rate-limit error type (must propagate to backoff, never retry the same edge) from a
   genuine login-rejection error type (may fall through to the bootstrap path).

6. **Error message attribute** is `em` (BaseMwsResponse) or `e` (video-response
   string); prefer `em`, fall back to `e`.

### 2.5 Action reference

All actions are BaseTokenRequests (§2.3). Success predicates as noted.

#### `GetLiveVideoStream`

```
Action=GetLiveVideoStream&SessionToken=<tok>&DeviceId=<numeric>&ForDevice=0
&ForceServerError=false  [+CustomerId, +envelope]
```

- `DeviceId` is the **numeric part before the first `-`** of the website
  `globalDeviceId` (`<unit-id>-<device-id>` → send `<unit-id>`).
- `ForDevice` is the `LiveVideoDeviceEnum` value (`0` = doorbell).
- Opens a real stream server-side — never call casually against a live account.

Response `<lvsr ts=… Resolution=… StreamId=…>` → `<vsel>` list of `<vsei st=…>`
entries; each entry can carry a `<webrtc>`, `<srtp>`, and/or `<rtsp>` child:

| endpoint | key attributes | talkdown? |
|---|---|---|
| `<webrtc>` | `SignalingServerUrl`, `SignalingServerToken`, `CameraAuthToken`, `HalfDuplexReenableAudioDelayInMs`, `MaxLoadTimeSeconds` | yes (custom text-WS signaling) |
| `<srtp>` | audio side `aip`/`ap`/`ack`(base64 key)/`acc`/`aca`/`ac`/`oassrc` (app→camera SSRC), video side `vip`/`vp`/`vck`/…, `sdp`; crypto suite fixed at `AES_CM_128_HMAC_SHA1_80` | yes (SRTP upstream) |
| `<rtsp>` | `rtspurl`, `csda` (downstream/listen audio flag), `bfmrs` | no — video (+ listen) only |

`st` on each `<vsei>` is the `StreamProtocolEnum` ordinal
(`0`=rtsp, `1`=srtp, `3`=webrtc, `4`=mjpeg, `5`=oesp, `6`=rtsps). SRTP crypto keys
(`ack`/`vck`) are base64 of the concatenated AES_CM_128 key+salt bytes — decode before
use.

> Device-specific caveat: the doorbell in this deployment (an ADC-VDB750, older
> firmware) returns **only an `<rtsp>` endpoint** — no `<webrtc>`, no `<srtp>` — so its
> talkdown is *not* on this response at all (see §2.6). The webrtc/srtp fields above
> are still the right target for newer Alarm.com cameras.

#### `GetDoorbellCameraList`

```
Action=GetDoorbellCameraList&SessionToken=<tok>&MaxHeight=1440&MaxWidth=1440
&ForceServerError=false  [+CustomerId, +envelope]
```

Response `<gdclr ts=…>` → `<dcl>` → one `<cli>` per doorbell. The `<cli>` element is
the payload for full-duplex talkdown — it carries, **per session**, everything needed
in one call:

| attribute | meaning |
|---|---|
| `did` / `UnitId` | MWS device id / unit id (website `globalDeviceId` = `UnitId-did`) |
| `model` / `cd` | model / name |
| `l` / `p` | Digest stream **user** (`vieweruser`) / **password** (per-session `<redacted>`) |
| `uae` / `duae` / `luae` / `puae` | upstream-audio PUT URLs: cloud / direct / LAN / public(WAN) |
| `re` / `dre` / `lre` / `pre` | downstream RTSP URLs: cloud / direct / LAN / public |
| `susa` / `sdsa` / `sfd` | supports upstream audio / downstream audio / full duplex (bools) |
| `StreamProtocol` | stream protocol ordinal (`0` = RTSP) |
| `sid` / `cstet` | session id / session expiry |

The port embedded in these URLs and the password `p` are **ephemeral / per-session** —
always fetch fresh, never cache. Blank attributes normalise to none.

#### `StartFullDuplex` / `EndLiveStream` / `EndFullDuplex`

```
Action=StartFullDuplex&SessionToken=<tok>&DeviceId=<did>  [+envelope]
Action=EndLiveStream&SessionToken=<tok>&DeviceId=<did>&ForDevice=0  [+envelope]
```

`StartFullDuplex` arms the doorbell mic-input path; its ack `<sfdr>` carries **no URL**
(the URLs already came from `<cli>`). Teardown is `EndLiveStream` (an `EndFullDuplex`
action also exists; `EndLiveStream` is what the app was observed to send). Both acks
(`<sfdr>` / `<elsr>` / `<efdr>`) carry `ts` (TokenStatus) and `s` ("true"/"false");
**success = `ts==0` AND `s=="true"`**. Run teardown best-effort in a `finally` and
tolerate a non-ok result. `StartFullDuplex` opens a real session server-side.

### 2.6 Talkdown transport (device-specific)

For the older RTSP-only doorbell, the actual **upstream audio path is not carried in
the MWS XML** at all — it is a chunked HTTP PUT directly to the doorbell:

1. Mint a `SessionToken` (§2.1).
2. `GetDoorbellCameraList` → read the `<cli>` PUT URL and Digest credentials.
3. `StartFullDuplex&DeviceId=<did>` → `<sfdr ts=0 s=true>`.
4. **Chunked HTTP PUT** to the chosen `PutAudioStream` URL (`duae`/`luae` on-LAN,
   `puae`/`uae` off-LAN — the cloud/relay variant adds a `camera_id` routing token to
   the query). One small chunk per request on a keep-alive connection:
   - `Authorization: Digest username="vieweruser", realm="Alarm.com", algorithm=MD5,
     qop=auth`, per-request incrementing `nc`, password = the per-session `p`. The
     server nonce comes from the doorbell's `401` challenge.
   - Body = **raw G.711 µ-law (PCMU), 8 kHz mono**, ~20 ms/chunk (~160 bytes), no RTP
     header. The doorbell serves HTTPS with a self-signed cert (disable TLS verify).
5. `EndLiveStream` (and/or `EndFullDuplex`) to tear down.

This transport (chunked Digest µ-law PUT) is specific to this doorbell model. The
`<webrtc>`/`<srtp>` endpoints in §2.5 remain the talkdown path for cameras that
advertise them. This section documents the doorbell mechanism for completeness; the
transport itself lives outside `mws_client.py`.
