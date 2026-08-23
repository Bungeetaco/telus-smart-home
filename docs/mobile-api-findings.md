# Mobile app API survey (MWS actions beyond talkdown)

A walkthrough of the Android app under TLS-decrypting instrumentation, section by
section, to inventory what the mobile API exposes that this project does not use.
Complements [alarm-com-api.md](alarm-com-api.md), which documents the two APIs the
bridge and integration already speak.

Everything below is the mobile **MWS** API (`mobile.alarm.com/MobileServlet/SubmitRequest.aspx`,
form-urlencoded POST + XML response, same `ts`/`s` success convention documented
in alarm-com-api.md) unless stated otherwise.

## Method

The app was **spawned** under instrumentation rather than attached to. This matters:
attaching to a running app only hooks connections opened *after* the hooks land,
and this app keeps a long-lived HTTP connection pool, so an attach-based capture
sees background polling and almost none of the interactive traffic.

WebView traffic additionally required disabling QUIC (a Chromium
`--disable-quic` flag file plus an `iptables` REJECT on UDP/443), because the
HTTP/3 hook only captures inbound bodies — otherwise webview traffic appears as
opaque UDP.

## Actions already used by this project

`GetDoorbellCameraList`, `GetLiveVideoStream`, `StartFullDuplex`, `EndLiveStream`,
`SeamlessLoginNew`, `GetStreamPreloadProperties`, `GetDoorbellAssociatedItems`.

## Actions NOT used, grouped by what they would enable

### Camera / doorbell diagnostics — highest practical value
| Action | Returns |
| --- | --- |
| `GetSettingsSignalStrength` | Signal strength for a camera |
| `GetSettingsCameraNetworkInfo` | Camera network/Wi-Fi details |
| `GetSettingsCameraStatus` | Camera status as the settings screen shows it |
| `GetSettingsCameraList` | Camera list in the settings shape |

A camera degrading to poor Wi-Fi is a real, silent failure mode, and nothing in
the integration exposes it. These map naturally onto diagnostic sensors next to
the existing talkdown ones.

### Bulk state and command confirmation
| Action | Notes |
| --- | --- |
| `UberPoll` | One batch poll returning **deltas** for every device class. The response carries `Completed*CommandsList` / `TimedOut*CommandsList` elements — this is how the app learns a command actually took effect. |
| `UberSeamlessLoginNew` | Combined seamless login + initial bulk fetch (we do the two separately) |
| `ValidateSession` | Cheap liveness check; response `<vsr … siv="true">`. The app calls it constantly. Better than inferring auth health indirectly. |
| `GetDashboard` | Dashboard summary; response `<gdr>` carries flags including some that look siren-related (`hsr`, `srtsmac`) |

The `AddToPendingCommands=true` parameter on commands plus those UberPoll lists
form a **pending-command pattern**: submit, then confirm via polling, rather than
assuming success.

### Events, clips and history
| Action / endpoint | Notes |
| --- | --- |
| `GetEventHistoryV2` | A V2 history API |
| `GetCategorizedActivityFilters` | Server-side categories for filtering the activity feed |
| `GetSavedClipsHistory` / `GetSavedClipsHistoryFilters` | Clip browser with server-side filtering |
| `GetDailySummary` | Returns `dsjson` containing a **playlist of the day's events** — a daily recap feature |
| `GET rlyviduse.devicetask.com/GetSavedClip.ashx?…&vf=<n>&en=0&t=<start>,<end>&watermark=` | Clip fetch with a **server-side time trim** and a watermark toggle — more control than fetching a whole clip |
| `GET …/GetSavedClipThumbnail.ashx?…` | Clip thumbnails |
| `GET /MobileServlet/ImageSensor/ImageSensorFrame.ashx?FrameNumber=&EventId=&Thumbnail=` | Image-sensor stills |

### Geofences / presence
| Action | Notes |
| --- | --- |
| `GetAllFencesForCustomer` | Existing geofences |
| `GetGeoFenceRadiusSizeSettings` | Allowed radius sizes |
| `AddEditFence` | **Write.** Params: `FenceName`, `FenceId`, `CenterLatitude`, `CenterLongitude`, `InsideRadius` (metres) |

### Session / auth plumbing
| Action | Notes |
| --- | --- |
| `GetWebsocketAuthToken` | Mints the token for the push websocket |
| `GetWebViewUrl` | Returns `https://www.alarm.com/web/default.aspx?loginAsKey=<key>` — an SSO handoff that opens the **full web portal** in a webview. Sections like Users/access codes are the website, not a separate mobile API. Also a second, independent route into a website session. |

Note the mobile push socket connects as `wss://webskt.alarm.com:8443/?auth=<token>&**ver=A**`,
which differs from the plain `?auth=` form the library uses. (The README documents
that mirroring the *portal's* `?f=1&…&ver=A` form was rejected by the server; this
is the **mobile** form, which is a different thing and untested by us.)

### Other
`GetCSContacts`, `GetCSLocationPhoneNumber` (central-station contact details),
`GetSmartArmingRules`, `GetNestProperties` (returns `s="false"` — not enabled on
the account tested), `POST adc-vid-rstats.devicetask.com/api/v1/RecordMobileStreamStatsApi`
(stream QoS telemetry the app reports), and device artwork at
`www.alarm.com/web/webimages/camera_images/<MODEL>_{64,300}px.{png,jpg}`.

## Commands (write actions) captured

| Action | Parameters | Response |
| --- | --- | --- |
| `LockUnlockLocks` | `LocksList=<device id>`, `DesiredStatus=2` (lock) / `3` (unlock), `AddToPendingCommands=true` | `<lulr ts="0" s="true" em="" vlid="<device id>"/>` |
| `AddEditFence` | `FenceName`, `FenceId`, `CenterLatitude`, `CenterLongitude`, `InsideRadius` | `<aefr ts="0" s="true"/>` |
| `EndLiveStream` | `DeviceId`, `ForDevice` | `<elsr …/>` (already used) |

`LockUnlockLocks` is a **second, independent control path** for locks: the
integration drives locks through the website API today, whereas this works off the
mobile session the bridge already mints for talkdown. Worth knowing as a fallback.

## Not resolved by this survey

`GetAllSirenDevices` and `GetAllCameraPrivacyModeInfo` are called by the app on
every camera screen, but the account tested has **no UI for either**, so no
command action for them was captured — only the reads. The `<cli>` element for the
doorbell does advertise `srn="true"`, and `GetDashboard`'s response carries
siren-shaped attributes, so the capability may exist without a UI surface.
Determining the command names/parameters needs a static read of the decompiled
app rather than another capture.

Sections backed by the webview (Users/access codes, account, support) were not
captured: they are the alarm.com web portal, whose API the integration already
speaks from the website side.
