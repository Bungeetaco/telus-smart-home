# Web portal API survey (browser HAR)

A HAR capture of a full browsing session in the alarm.com web portal, inventoried
the same way as [mobile-api-findings.md](mobile-api-findings.md). This is the
**website API** — the one the integration already authenticates to via
`pyalarmdotcomajax` (cookie session + `ajaxrequestuniquekey` header) — so
everything here is reachable with the session the integration already holds.

~180 distinct endpoints were observed. Most are read-only page data; the
interesting part is the **write surface**, which is large and almost entirely
unused by this project.

## Headline: the doorbell's speaker volume is settable

`PUT /web/api/video/settings/audioSettings/{device-id}` carries a
`microphoneSpeakerSetting` object including:

| Field | Meaning |
| --- | --- |
| `callVolumeFeatureSupport` | `2` = supported on this device |
| `callVolumeValue` / `callVolumeMin` / `callVolumeMax` | **talk/call volume, range 0–10** |
| `areMicrophoneAndSpeakerOn` | mic + speaker master toggle |
| `isHalfDuplexAudio` | `false` here — confirms full duplex |
| `canToggleAudioRecording` / `isAudioRecordingOn` | audio recording toggle |
| `isNoiseFilteringOn`, `microphoneGain*` | present but `FeatureSupport: 0` on this device |

This is the volume the talkdown audio plays at. It **contradicts the mobile API**,
whose `<cli>` `<duav>` element reports `SupportsVolumeAdjustment="false"` — the
website exposes it as supported with a 0–10 range, and a live session was observed
sweeping it. When the two APIs disagree, prefer the one that demonstrably works.

The same call carries a `chimeToneSetting` object: `isIndoorChimeOn`,
`selectedChimeType` (+`chimeTypeOptions`), `isOutdoorToneOn`, and
`outdoorToneVolumeValue` with range 0–3 — i.e. the doorbell's own chime is
configurable too.

## Other camera settings, all writable

| Endpoint | Controls |
| --- | --- |
| `PUT /web/api/video/settings/imageSettings/{id}` | brightness, contrast, saturation, sharpness (each with min/max), HDR, image flip, max exposure, and a nested `infraredSetting` (IR LEDs, IR sensitivity, night vision) |
| `PUT /web/api/video/settings/videoConfigs/{id}` | clip resolution (`480x480` / `960x960` / `1440x1440`), clip quality, alarm-clip duration options, camera-triggered clip duration, pre-trigger recording, keep-clips-for-days |
| `PUT /web/api/video/settings/generalSettings/{id}` | general camera settings |
| `PUT /web/api/video/devices/cameras/{id}` | camera device properties |

Each `*FeatureSupport` field is an enum where `0` means unsupported and `2` means
supported/editable — check it before offering a control, because the same payload
shape is returned for devices that cannot do the thing.

## Diagnostics (read)

| Endpoint | Returns |
| --- | --- |
| `GET /web/api/video/signalStrengthInfos/{id}` | camera signal strength |
| `GET /web/api/video/batteryStatusInfos/{id}` | battery status |
| `GET /web/api/video/settings/deviceStatusInfos/{id}` | device status |
| `POST /web/WebServices/Video/DeviceCommands.asmx/GetVideoDeviceNetworkInfo` | camera network details |
| `POST …/DeviceCommands.asmx/GetExternalIpAddress`, `TestRemoteAccess`, `IsSecondRouterPresent` | connectivity diagnostics |

These are the website equivalents of the mobile `GetSettings*` actions, and the
same argument applies: a camera degrading to poor Wi-Fi is currently invisible.
Note the `.asmx` endpoints are the older ASP.NET web-service style, not JSON:API.

## Other write endpoints worth knowing

| Endpoint | What it does |
| --- | --- |
| `POST /web/api/devices/partitions/{id}/bypassSensors` | bypass sensors — the server-side counterpart of the open-zone arm problem |
| `PUT /web/api/devices/bluetoothArmingDevices/{id}` | Bluetooth arming device config (the phone-proximity auto-disarm path) |
| `POST /web/api/systems/systems/{id}/connectPanelToWifi`, `GET /web/api/settings/panelWifi/wifiSettings/{id}`, `…/wifiNetworks` | panel Wi-Fi |
| `POST …/DeviceCommands.asmx/SetStaticNetworkConfig`, `SetToFindIpAddressAutomatically` | camera IP configuration |
| `DELETE /web/api/devices/mobileDevices/{id}` | de-register a mobile device |
| `PUT …/monitoringStation/contacts/emergencyContactListCollections/{id}` | emergency contact list |
| `PUT /web/api/video/ovRules/{id}` | video/object rules |
| `POST /web/api/swa/wizard/automation/zwave-troubleshooting/{LqiTest,Rediscover,ResetPanel,ResetSettings}` | Z-Wave troubleshooting wizard |

## Reads the integration doesn't use

`GET /web/api/devices/sensors`, `/web/api/automation/rules/rules` (+
`ruleSuggestions`, `smartArmings`), `/web/api/systems/systems/{id}`,
`/web/api/websockets/token`, `/web/api/video/devices/{cameras,svrs,ovReadyCameras,
digitalFovCameras,thirdPartyAnalyticsCameras}`, `/web/api/video/videoUsages/{id}`,
`/web/api/video/permissions/videoSettingsPermissions/{id}`, and the two-factor
endpoints under `/web/api/engines/twoFactorAuthentication/`.

## Practical notes

- Requests carry the `ajaxrequestuniquekey` header plus the session cookie — the
  same pair `telus_auth.py` already assembles, so no new auth work is needed.
- Bodies are plain `application/json` (the settings object itself, with `id` and
  `type`), even though many *responses* are `application/vnd.api+json`.
- A settings `PUT` sends the **whole object back**, not a patch: read the current
  settings, change one field, send it all. Sending a partial object risks
  resetting fields.
- Ignore the noise: `routeTransition`, `recordRouteView`, `/metrics/`,
  `KeepAlive.aspx`, `dt_beacon`, and the Cloudflare challenge paths are telemetry
  and page plumbing, not API.
