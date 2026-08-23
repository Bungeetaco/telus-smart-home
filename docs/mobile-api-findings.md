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

Added since this survey: `GetAllSirenDevices`, `ManualSirenStart`,
`ManualSirenStop`, `GetAllCameraPrivacyModeInfo`, `SetCameraPrivacyModeState`,
`ValidateSession` — all in `camera-bridge/bridge/mws_client.py`, exposed as the
bridge's siren and privacy routes (see
[control-api.md](control-api.md#camera-settings-diagnostics-siren-and-privacy)).
They are marked **[implemented]** below; everything else in this document is
still unused.

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
| `ValidateSession` **[implemented]** | Cheap liveness check; response `<vsr … siv="true">`. The app calls it constantly. Better than inferring auth health indirectly. Note it answers **XML** even though its JSON-era siblings below do not. |
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

## Camera siren and privacy mode — resolved from the app binary **[implemented]**

The app calls `GetAllSirenDevices` and `GetAllCameraPrivacyModeInfo` on every
camera screen, but the account tested exposes no UI for either, so no command
could be captured live. Reading the decompiled APK settles it: **both commands
exist**, and they are ordinary MWS actions.

They differ from the XML actions in one respect: **the response is JSON**, parsed
with Moshi, though the envelope carries the same fields as the XML one
(`isSuccess`, `ErrorMessage`, `DidSeamlesslyLogin`). Requests are the usual
form-urlencoded POST with the standard token envelope.

### Commands

| Action | Extra parameters | Response |
| --- | --- | --- |
| `ManualSirenStart` **[implemented]** | `DeviceId` | `StartSirenResponse` (JSON) |
| `ManualSirenStop` **[implemented]** | `DeviceId` | `StopSirenResponse` (JSON) |
| `SetCameraPrivacyModeState` **[implemented]** | `DeviceId`, `IsActive` (`true`/`false` as a string) | `{"CameraPrivacyModeInfo": {"DeviceId": …, "State": …}}` |

Constructors in the app are `(customerId, deviceId)` and
`(customerId, deviceId, isActive)`; `customerId` rides in the standard envelope
(`CustomerId`) and `deviceId` is emitted by the shared base class as `DeviceId`.

### Reads

| Action | Response shape (as the app models it) |
| --- | --- |
| `GetAllSirenDevices` **[implemented]** | `{"AllCameraSirenInfo": [ {"DeviceId": …, "isActive": …, "timestampEnd": …, "canManuallyTriggerSiren": …}, … ]}` |
| `GetAllCameraPrivacyModeInfo` **[implemented]** | `{"CameraPrivacyModeInfo": {"DeviceId": …, "State": …}}` |

> **These two reads answer in XML, not JSON**, despite the app modelling them as
> JSON. What the account returns is:
>
> ```xml
> <GetAllSirenDevicesResponse ts="0" dsl="false" s="true">
>   <AllCameraSirenInfo />
> </GetAllSirenDevicesResponse>
> ```
>
> i.e. the ordinary MWS XML envelope (`s` success, `ts` TokenStatus, `dsl`
> DidSeamlesslyLogin, `em` error message) wrapping the same member names. Note
> `s="true"` with an **empty** `<AllCameraSirenInfo />`: the call succeeded and
> the answer is "this account has no siren-capable devices" — not a failure, and
> not something to retry. `GetAllCameraPrivacyModeInfo` answers the same way with
> an empty `<AllCameraPrivacyModeInfo />`.
>
> `mws_client._parse_mws_ack` now dispatches on the body's first byte rather than
> on Content-Type (this endpoint already serves gzip without announcing it) and
> normalizes both encodings to one ack shape. Only the empty-container case has
> been seen live; a populated one is still modelled from the app, so treat
> non-empty results as unverified. The four WRITE actions have never been
> invoked, so their encoding is unknown — both paths handle either.

### Caveats — all still true, and all honoured by the implementation

- **`canManuallyTriggerSiren` is a per-camera permission**, and the app has a
  dedicated `ManualCameraSirenPermissionChecker`. A device advertising a siren is
  not the same as this account being allowed to trigger it — read
  `GetAllSirenDevices` first and honour that flag rather than firing blind. The
  bridge reads it before every start and answers `422 unsupported` when it is
  false; Home Assistant creates no siren buttons for such a camera at all.
- The siren is **time-bounded**: `CameraSirenInfoDTO` carries `timestampEnd`, and
  the app has an error path for "isActive:true but received null or empty
  timestampEnd". Expect a server-chosen duration, and use `ManualSirenStop` to end
  it early rather than assuming it stops on its own.
- The doorbell's `<cli>` advertises `srn="true"` and `GetDashboard` carries
  siren-shaped attributes, but **no live command was ever observed on this
  account** — the UI is absent. This is still true: the implemented siren path has
  never been fired against a real device. Treat a first invocation as an
  experiment, on a device where an unexpected siren is acceptable.
- Privacy mode is also gated by `SupportsPrivacyMode`, which is **false** for the
  cameras on the account tested — so `SetCameraPrivacyModeState` would likely be
  refused there even though the action exists. The privacy switch therefore ships
  reporting *unavailable* on those cameras rather than pretending to work.

## `GetCurrentDoorbellSettings` — the doorbell's own settings document **[implemented, partially]**

One action returning a large settings document for a single doorbell. Request shape is
the standard token envelope plus `DeviceId` (the bare numeric MWS id), from the
decompiled `GetCurrentDoorbellSettingsRequest`. It answers in **XML** (root
`<gcdsr>`), like the other reads — see the correction above.

**Why this project calls it: the chime-type NAMES.** The website API publishes
`chimeTypeOptions` as bare integers (`[-1, 0, 1]`) and leaves naming them to a
client-side enum in the portal's JavaScript. This action sends the names, and
better, a per-option flag saying which value means "no wired chime" on this
model — so no mapping has to be hardcoded and kept true across camera models.

Element shapes, from `SupportedChimeTypeOptionsInfoParser`:

| Element | Meaning |
| --- | --- |
| `<scto>` → `<sctoi>` × N | the offered chime types |
| `<ct>` | the current selection, same shape |
| `n` | display name |
| `v` | value (int) |
| `sdict` | `shouldDisableIndoorChimeToggle` — true for the "none" option |

Live from the reference doorbell: `-1` = **None** (`sdict="true"`), `0` =
**Mechanical** (current), `1` = **Digital**. There is a fourth attribute `sdcds`
whose meaning was not resolved; it is not read.

**It also independently corroborates E9.** The same root element carries
`cancontrolspeakervolume="true" speakervolume="10" speakervolumemin="0"
speakervolumemax="10" speakervolumestep="1"`, matching the website's
`callVolumeValue` exactly. That is the *third* mobile-API opinion on this and it
agrees with the website against `<duav>`'s `SupportsVolumeAdjustment="false"` —
so treat that one element as the outlier, not the website.

**Root attributes worth knowing, none of them read yet** (candidates for future
work, all read-only here):

| Attribute | Meaning |
| --- | --- |
| `csic` / `ic` | can-set / current indoor chime — the mobile twin of `isIndoorChimeOn` |
| `supportsoutdoorchime`, `csocl`, `ocl`, `<ocll>` | outdoor chime: supported, can-set-level, current level, and the offered levels |
| `canturnonoffdeviceled`, `ledi`, `ledo`, `ledmn`/`ledmx` | the doorbell's status LED — brightness and on/off. The website carries the same control in `generalSettings` → `ledLightSetting`, which is what the implementation uses |
| `cancontrolirleds`, `irledenabled` | IR LEDs (the website's `infraredSetting` covers this) |
| `cswt`, `wt`, `wsn`, `<wtol>` | mounting wedge type, e.g. "Downward Wedge Installed" |
| `CanSetResolutionAndQuality`, `ResolutionAndQuality`, `<ResolutionAndQualityOptionList>` | live-video resolution/quality |
| `IsBatteryPowered`, `CanGetBatteryLevel`, `BatteryLevel` | `-1` on this mains-powered doorbell — a cleaner "no battery" signal than the website's present-and-0 `batteryLevel` |
| `CanSetPIRSensitivity`, `PIRSensitivity`, `Max`/`MinPIRSensitivity` | PIR motion sensitivity (all 0/false here) |

One discrepancy left open: the mobile document reports `ocl="3"` for the outdoor
chime level while the website reports `outdoorToneVolumeValue: 0` for what should
be the same thing. Both agree it is **not settable** on this device
(`csocl="false"`, `outdoorToneVolumeFeatureSupport: 0`), so nothing depends on
resolving it — but do not assume the two are interchangeable reads.

The corresponding WRITE action (fields `ChimeType`, `IndoorChime`,
`OutdoorChimeEnabled`, `OutdoorChimeLevel`) exists in the app and is **not** used:
the chime writes go over the website API, which is the path this project has
actually verified end to end.

## Not resolved by this survey

Sections backed by the webview (Users/access codes, account, support) were not
captured: they are the alarm.com web portal, whose API the integration already
speaks from the website side.
