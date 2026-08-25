# Web portal API survey (browser HAR)

A HAR capture of a full browsing session in the alarm.com web portal, inventoried
the same way as [mobile-api-findings.md](mobile-api-findings.md). This is the
**website API**, the one the integration already authenticates to via
`pyalarmdotcomajax` (cookie session + `ajaxrequestuniquekey` header), so
everything here is reachable with the session the integration already holds.

~180 distinct endpoints were observed. Most are read-only page data; the
interesting part is the **write surface**, which is large and was, when this
survey was written, entirely unused by this project.

**Status legend.** Entries marked **[implemented]** are now spoken by
`camera-bridge/bridge/camera_settings.py` and exposed on the bridge's control API;
see [control-api.md](control-api.md#camera-settings-diagnostics-siren-and-privacy)
for the routes and [ha-integration.md](ha-integration.md#camera-settings-siren-and-diagnostics-entities)
for the entities. Everything else here is still unused and still a survey finding.

## Headline: the doorbell's speaker volume is settable **[implemented]**

`PUT /web/api/video/settings/audioSettings/{device-id}` carries a
`microphoneSpeakerSetting` object including:

| Field | Meaning |
| --- | --- |
| `callVolumeFeatureSupport` | `2` = supported on this device |
| `callVolumeValue` / `callVolumeMin` / `callVolumeMax` | **talk/call volume, range 0–10** |
| `areMicrophoneAndSpeakerOn` | mic + speaker master toggle |
| `isHalfDuplexAudio` | `false` here: confirms full duplex |
| `canToggleAudioRecording` / `isAudioRecordingOn` | audio recording toggle |
| `isNoiseFilteringOn`, `microphoneGain*` | present but `FeatureSupport: 0` on this device |

This is the volume the talkdown audio plays at, and it is now settable through
the bridge (`POST /control/cameras/{id}/settings/volume`, surfaced in Home
Assistant as a speaker-volume number entity). It **contradicts the mobile API**,
whose `<cli>` `<duav>` element reports `SupportsVolumeAdjustment="false"`; the
website exposes it as supported with a 0–10 range, and a live session was observed
sweeping it. When the two APIs disagree, prefer the one that demonstrably works.

The same call carries a `chimeToneSetting` object: `isIndoorChimeOn`,
`selectedChimeType` (+`chimeTypeOptions`), `isOutdoorToneOn`, and
`outdoorToneVolumeValue` with range 0–3, i.e. the doorbell's own chime is
configurable too. **[implemented]** for `isIndoorChimeOn` / `isOutdoorToneOn` /
`outdoorToneVolumeValue` via `POST …/settings/chime`, and `selectedChimeType`
too, settable as `chime_type` on the same route (validated against the
device's offered options), with the option labels merged from the mobile API
and an HA select entity; see backlog E27.

## Other camera settings, all writable

| Endpoint | Controls | Status |
| --- | --- | --- |
| `PUT /web/api/video/settings/imageSettings/{id}` | brightness, contrast, saturation, sharpness (each with min/max), HDR, image flip, max exposure, and a nested `infraredSetting` (IR LEDs, IR sensitivity, night vision) | **implemented**: `POST …/settings/image`; `max_exposure` is routed too but unsupported on both reference cameras (`canSetMaxExposure` false), so it is unexercised against hardware (E30). The image scalars have no HA entity |
| `PUT /web/api/video/settings/videoConfigs/{id}` | clip resolution (`480x480` / `960x960` / `1440x1440`), clip quality, alarm-clip duration, camera-triggered clip duration, pre-trigger recording, keep-clips-for-days, all of them in the **`savedVideoSetting`** relationship, not in the document's own attributes (see the wire-shape section) | **implemented**: `POST …/settings/video`; `camera_triggered_clip_duration` is routed too but refused by the support gate on the reference cameras (E30). The same endpoint also backs the **live video resolution** control (`live_resolution`), which has an HA select entity (E34); the clip scalars have no HA entity |
| `PUT /web/api/video/settings/generalSettings/{id}` | **the status LED** (`ledLightSetting`: `canTurnOnOffLedLight`, `canChangeLedBrightness`, `canChangeLedColor`, `isLedLightEnabled`, `ledBrightness`, `ledLightColor`), the doorbell's TELUS-side notification toggles (`doorbellNotificationSetting`), and solar-panel status. "General camera settings" undersold this one badly; see the note below | **implemented**: `POST …/settings/led` and `POST …/settings/notifications`, both with HA switches. `notificationFormat` is settable (`format` on `POST …/settings/notifications`, limited to the Text Only / Attach a Thumbnail values; E40) with an HA select; solar-panel status is read-only, surfaced as an enum sensor where supported (E39) |
| `PUT /web/api/video/devices/cameras/{id}` | camera device properties | unused |

> **`generalSettings` carries more than its name suggests**: the status LED, the
> doorbell's TELUS-side notification toggles and the solar-panel status all live
> there. A vague row in an endpoint inventory is an unread endpoint, not an empty
> one; open it before concluding a capability is missing.

Each `*FeatureSupport` field is an enum where `0` means unsupported and `2` means
supported/editable: check it before offering a control, because the same payload
shape is returned for devices that cannot do the thing. `camera_settings.py`
resolves these into plain `supports_*` booleans on every read and refuses an
unsupported write with a `422`, rather than sending a `PUT` the device will
ignore.

## Diagnostics (read)

| Endpoint | Returns | Status |
| --- | --- | --- |
| `GET /web/api/video/signalStrengthInfos/{id}` | `normalizedSignalStrength` (a **0–100 score, not dBm**), a coarse `signalStrengthEnum`, plus `isConnected`, SSID, encryption type, connection type and frequency band | **implemented**: `GET …/diagnostics` |
| `GET /web/api/video/batteryStatusInfos/{id}` | `batteryLevel`, but it is present and `0` on mains-powered cameras, so gate on `shouldDisplayBatteryLevel` or an entity built from it sits at a permanent 0% | **implemented**: `GET …/diagnostics` |
| `GET /web/api/video/settings/deviceStatusInfos/{id}` | device status | **implemented**: `GET …/diagnostics` |
| `POST /web/WebServices/Video/DeviceCommands.asmx/GetVideoDeviceNetworkInfo` | camera network details | unused |
| `POST …/DeviceCommands.asmx/GetExternalIpAddress`, `TestRemoteAccess`, `IsSecondRouterPresent` | connectivity diagnostics | unused |

`deviceStatusInfos` returns `connectionStatus` (`0` on a healthy camera here),
plus the SSID, connection type and frequency band again.

**`signalStrengthInfos` is not a cached read.** Measured live on one account, it
answers in 1.6s on a doorbell but **25s** on an indoor camera, latency that
suggests alarm.com queries the camera itself for the figure (inferred from the
timing, not confirmed anywhere). Budget for tens of seconds when calling it: the
bridge gives each of its three diagnostics reads a 35s per-request ceiling
(`DIAGNOSTICS_READ_TIMEOUT_S`, see [configuration.md](configuration.md)) precisely
because a 15s client timeout cancels a legitimately slow signal read. The slow
figure is attributed to the signal read; the battery and device-status reads have
not shown comparable latency, but were not timed in isolation.

These are the website equivalents of the mobile `GetSettings*` actions. The first
three are now diagnostic sensors in Home Assistant, so a camera degrading to poor
Wi-Fi is no longer invisible; the `.asmx` ones remain unused, and are the older
ASP.NET web-service style, not JSON:API.

> **Privacy note.** Two of these documents carry the home Wi-Fi **SSID** and its
> encryption type. `camera_settings.get_diagnostics` hands the untouched payloads
> back under `raw`, so anything that logs that structure, or copies it into an
> entity attribute, publishes the network name. Decide that on purpose; the
> shipped sensors expose only the three scalars.

## Other write endpoints worth knowing

| Endpoint | What it does |
| --- | --- |
| `POST /web/api/devices/partitions/{id}/bypassSensors` | **Selective** sensor bypass: a standalone partition action, NOT an arm-time option: the body carries `bypass` and `unbypass` as pipe-separated device-id strings, applied immediately. Gated by the partition's own `canBypassSensorWhenArmed` / `hasPermissionToChangeState`. **Implemented** as the manual `telus_smarthome.bypass_sensors` service; the integration's existing `CONF_FORCE_BYPASS` arm flag is a different mechanism and is untouched |
| `PUT /web/api/devices/bluetoothArmingDevices/{id}` | Bluetooth arming device config (the phone-proximity auto-disarm path): **genuinely unimplemented, see below** |
| `POST /web/api/systems/systems/{id}/connectPanelToWifi`, `GET /web/api/settings/panelWifi/wifiSettings/{id}`, `…/wifiNetworks` | panel Wi-Fi |
| `POST …/DeviceCommands.asmx/SetStaticNetworkConfig`, `SetToFindIpAddressAutomatically` | camera IP configuration |
| `DELETE /web/api/devices/mobileDevices/{id}` | de-register a mobile device |
| `PUT …/monitoringStation/contacts/emergencyContactListCollections/{id}` | emergency contact list |
| `PUT /web/api/video/ovRules/{id}` | video/object rules |
| `POST /web/api/swa/wizard/automation/zwave-troubleshooting/{LqiTest,Rediscover,ResetPanel,ResetSettings}` | Z-Wave troubleshooting wizard |

### Sensor bypass: two independent mechanisms

Bypass reaches this account two ways, and they are not alternatives.

**`force_bypass`, the arming option.** `CONF_FORCE_BYPASS` is passed as
`force_bypass` on all three arm calls in `alarm_control_panel.py`
(`async_arm_away`, `async_arm_stay`, `async_arm_night`), surfaced per arm mode in
the options flow and documented in [configuration.md](configuration.md). It means
"bypass whatever is open as you arm" and is automatic.

**`POST /web/api/devices/partitions/{id}/bypassSensors`, the manual action.** The
body carries `bypass` and `unbypass` as pipe-separated device-id strings and is
applied immediately, independent of any arm call. It is exposed as the
`telus_smarthome.bypass_sensors` service.

Only one of them is automatic, which is what keeps them from disagreeing about
which zones are skipped. **Do not add a second automatic path**: two of those
racing over one partition is a genuine hazard on a live alarm system.


## Reads the integration doesn't use

`GET /web/api/devices/sensors`, `/web/api/automation/rules/rules` (+
`ruleSuggestions`, `smartArmings`), `/web/api/systems/systems/{id}`,
`/web/api/websockets/token`, `/web/api/video/devices/{cameras,svrs,ovReadyCameras,
digitalFovCameras,thirdPartyAnalyticsCameras}`, `/web/api/video/videoUsages/{id}`,
`/web/api/video/permissions/videoSettingsPermissions/{id}`, and the two-factor
endpoints under `/web/api/engines/twoFactorAuthentication/`.

## The wire shape: read JSON:API, write flat

This is the part that costs the most time to rediscover, so it is written out in
full. All of it was established by diffing captured `GET` responses against the
`PUT` bodies the browser sent immediately afterwards, and it is pinned by
`camera-bridge/tests/test_camera_settings.py` against fixtures taken from those
same captures (`camera-bridge/tests/fixtures/`).

**Every one of these fails silently.** The endpoint does not complain; the
setting simply never takes, or reads back as absent. Nothing surfaces an error,
so only a test catches it.

1. **The read is JSON:API, the write is flat.** A `GET` returns
   `{"data": {"id", "type", "attributes", "relationships"}, "included": [...]}`.
   The `PUT` body is a **single flat object**: *not* wrapped in `data`, and not
   the `attributes` sub-object on its own.

2. **The settings are in `included`, not in `data.attributes`.** This is the
   trap. `data.attributes` for `videoConfigs` holds only `shouldDisplay*Link`
   booleans; the actual clip settings are a separate resource in `included`,
   referenced from `data.relationships.savedVideoSetting`. Read only the
   attributes and every setting reads as absent, i.e. the whole device looks
   unsupported.

   To build the write body: take `data.attributes` plus `id`/`type`, then inline
   each relationship under its **relationship name** (not its type), carrying its
   own `id`/`type`. A relationship whose resource is absent from `included`
   becomes an `{"id", "type"}` stub, which is what the browser sends for one it
   did not load. Dropping it instead would delete that relationship, because the
   `PUT` replaces the whole document.

3. **Resource types are pluralized on write.** The server sends
   `video/settings/audio-setting`; the browser sends back
   `video/settings/audio-settings`. Consistently, for the document and every
   inlined relationship (an Ember Data convention showing through). Whether the
   server validates it is untested: send what a working client sends.

4. **A settings `PUT` replaces the whole object**, never a patch: read, change
   one field, send it all back. A partial body risks resetting fields on a
   camera in someone's home.

### Capability flags come in two spellings

Which one applies is decided by the key's **name**, not its value:

| Spelling | Rule | Examples |
| --- | --- | --- |
| `<name>FeatureSupport` | an **enum**: only `2` means editable | `callVolumeFeatureSupport`, `clipResolutionFeatureSupport` |
| anything else | a plain truthy flag | `canSetBrightness`, `canFlipImage`, `canControlInfraredLeds`, `isKeepClipsForFeatureSupported` |

`0` in the enum form means "has the field, cannot set it", and `1` is *not*
supported however truthy it looks. In the flag form the value is not always a
bool: `canSetHdr` arrives as the integer `1`. **An absent flag means
unsupported**: a device that cannot do a thing returns the same payload shape as
one that can, so absence must fail closed.

### Bounds and option lists

Bounds also use two spellings: **suffix** on the audio/video objects
(`callVolumeMin`/`callVolumeMax`) and **prefix** on the image object
(`minBrightness`/`maxBrightness`). An absent bound must be treated as *unknown*,
never as `0`: reading it as 0 would reject every negative brightness on a camera
whose real range is `-5..5`.

Option lists are a JSON array *or* an object, and when it is an object, **which
half is the settable value differs per field**, in the same document, on the same
device:

| Field | Object | Settable half |
| --- | --- | --- |
| `selectedClipResolution` | `clipResolutionOptions` = `{"1": "480x480", "7": "1440x1440"}` | the **value** (`"1440x1440"`); the key is a resolution-class enum used to prefix a label |
| `selectedClipQuality` | `clipQualityOptions` = `{"5": 3, "3": 2, "2": 1}` | the **key** (`"3"`); the value is a quality enum used to look up a label |
| `selectedWebOption` | `webOptions` = `{"1440x1440": "Full High Definition (1440x1440)"}` | the **key** |
| `selectedCameraTriggeredClipsDuration` | `cameraTriggeredClipsDurationOptions` = `{"30": "30 seconds"}` | the **key** |

None of that is inferable from the payload: `selectedClipQuality` is `"3"`,
which is *both* a key of its object and a value of it. It was settled by reading
the portal's own dropdown builders out of its JavaScript (a public static asset;
see the note on reading enums that way). The resolution builder pushes
`{value: options[key]}`; the quality builder writes `props.id`, the key.

`camera_settings` declares the half per field, and warns rather than guessing for
any field that has not been resolved.

## Practical notes

- Requests carry the `ajaxrequestuniquekey` header plus the session cookie; the
  same pair `telus_auth.py` already assembles, so no new auth work is needed.
- Request bodies are plain `application/json` even though many *responses* are
  `application/vnd.api+json`.
- Ignore the noise: `routeTransition`, `recordRouteView`, `/metrics/`,
  `KeepAlive.aspx`, `dt_beacon`, and the Cloudflare challenge paths are telemetry
  and page plumbing, not API.
