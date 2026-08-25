"""Local-only control endpoint so Home Assistant's TELUS Smart Home integration
(its config-flow reconfigure/reauth step) can push updated TELUS credentials
into this already-running bridge process, without restarting the container.

Bound to 127.0.0.1 only. Reachable from the HA container because both
containers run with --network host (confirmed via `docker inspect`) -- never
exposed off the host, never given a Docker port-publish mapping.

Authentication is a random per-install shared secret, generated on first start
and persisted to the shared bind-mounted directory (config.CONTROL_SECRET_PATH)
so the HA side can read it directly off disk (it shares the same mount from
the HA-container side at /share/telus_cam_bridge) rather than needing any kind
of interactive pairing step.

Contract (as implemented -- this is what callers must match):

    POST http://127.0.0.1:<CONTROL_PORT>/control/credentials
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>
    Content-Type: application/json
    Body: {"username": "<telus email>", "password": "<telus password>"}

    Responses:
      200 {"status": "applied"}          -- credentials swapped in, re-login succeeded
      400 {"error": "malformed"}         -- body wasn't valid JSON / missing or
                                             empty username or password
      401 {"error": "bad_secret"}        -- Authorization header missing/wrong
      500 {"error": "internal"}          -- re-login with the new credentials
                                             itself failed (e.g. genuinely wrong
                                             password, or TELUS/network error);
                                             the new credentials are still left
                                             in place in config.py so the
                                             process's normal retry paths keep
                                             trying them rather than silently
                                             reverting to the old (also
                                             possibly-stale) ones

    GET http://127.0.0.1:<CONTROL_PORT>/control/health   (no auth required --
      no secret, this leaks no credential material, just process liveness)
    Response: 200 {"status": "ok"}

    GET http://127.0.0.1:<CONTROL_PORT>/control/status
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"cameras": {"<camera_id>": {"state": "streaming"|"connecting"|
                                                 "reconnecting"|"restart_deferred",
                                                 "consecutive_failures": ...,
                                                 "last_frame_at": <unix ts or null>,
                                                 "fps": ..., "hardware_encode": ...,
                                                 "last_error": <str or null>,
                                                 "dropped_frames": <int>,
                                                 "audio_consumed": <bool>,
                                                 "audio_format": <str or null>,
                                                 "decoded_audio_frames": <int>,
                                                 "dropped_audio_frames": <int>,
                                                 "audio_dropped": <bool>,
                                                 "audio_dropped_reason": <str or null>,
                                                 "publisher_alive": <bool or null>,
                                                 "last_publish_at": <unix ts or null>},
                                                ...}}
      Live per-camera diagnostics maintained by camera_session.py's own
      run_camera_forever loop (main.py threads a shared dict into it) --
      reconnect counts, last-frame age, and current encode mode, without
      needing to grep container logs. "restart_deferred" is the rarest and
      most informative one: this camera hit the MAX_CONSECUTIVE_SESSION_FAILURES
      escape hatch, but it has fired too often lately to be a local wedge, so
      restart_guard.py is holding the process before it exits (see there).
      "dropped_frames" is the camera's LIFETIME total (since process start) of
      frames dropped because the consumer fell behind (janus_client._pump_track's
      latency-over-completeness drop). Each Janus session's own counter dies
      with it at the ~165s rotation, so the loop folds it into this running
      total as each session ends -- which also means the field updates on
      session end, not per frame, and lags the live session by up to one
      rotation.
      The four audio fields are ADDITIVE and deliberately separate from "fps"
      and "dropped_frames", which are a contract about the VIDEO stream that
      callers already read: a number that quietly started counting two different
      things would be worse than no number at all. "audio_consumed" is whether
      JANUS_CONSUME_AUDIO resolved to on for this camera (which requires
      JANUS_REQUEST_AUDIO to name it too -- if it does not, this reads false and
      the bridge logged why once). "audio_format" is the raw PCM shape of a
      frame this camera has actually DECODED, as
      "<rate>Hz/<channels>ch/<sample format>", and null until one has been --
      it is an OBSERVATION, never the shape ffmpeg was launched with (that is
      config.AUDIO_DEFAULT_FORMAT until a frame arrives, and reporting it made
      this field read "48000Hz/2ch/s16" for eleven consecutive sessions that
      published pure silence). "decoded_audio_frames" is the lifetime total of
      decoded audio frames handed to the publisher, updated as each frame
      arrives rather than folded in at session end, so consumption on with this
      stuck at 0 is a silent stream. "dropped_audio_frames" is the lifetime
      total of decoded AUDIO frames dropped because the consumer fell behind,
      accumulated across rotations exactly like "dropped_frames" -- it cannot
      see audio that never decodes at all, since a stream producing nothing
      drops nothing.
      "audio_dropped"/"audio_dropped_reason" say the publisher GAVE UP on its
      audio input and is publishing video only -- a wedged pipe, a pipe that
      never opened, or a run of malformed frames -- with the reason verbatim;
      without them the degradation is invisible, since "audio_consumed" keeps
      saying what the camera is CONFIGURED to do and "decoded_audio_frames"
      keeps climbing (frames still decode, they just no longer reach ffmpeg).
      "publisher_alive" is whether the ffmpeg publisher exists AND is still
      consuming what it is fed -- the process being alive is not sufficient,
      which is how a camera once reported "streaming" while MediaMTX had no
      path at all -- and "last_publish_at" is when ffmpeg last TOOK a frame,
      against "last_frame_at"'s "a frame arrived from the camera"; the gap
      between those two is the difference between "streaming" and "streaming
      to nowhere". Full field semantics: docs/control-api.md.
      NOTE for callers building health checks on this: the frame-silence
      watchdog is video-only, so a camera whose audio has stopped still reports
      state "streaming" with a fresh "last_frame_at". Nothing here RECONNECTS a
      camera on dead audio; comparing two polls of "decoded_audio_frames" is
      how a caller detects it.

    GET http://127.0.0.1:<CONTROL_PORT>/control/cameras
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"cameras": [{"id": ..., "name": ..., "model": ..., "firmware": ...,
                                 "mac_address": ..., "rtsp_url": ...}, ...]}
      Reflects main.py's in-memory camera_registry, which is populated on
      startup and kept current by discover_and_launch_cameras()'s periodic
      poll -- so a camera added to the TELUS account shows up here within
      config.CAMERA_DISCOVERY_INTERVAL_S of being added, no restart needed on
      either side.

    GET http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/snapshot
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200, Content-Type: image/jpeg, raw JPEG bytes -- a fresh
      on-demand still image fetched from TELUS for this one request (not
      cached; TELUS's snapshot URLs are signed and short-lived, so every
      call here does a real round trip to TELUS, unlike /control/cameras).
      404 {"error": "unknown_camera"} if camera_id isn't in camera_registry.
      502 {"error": "telus_error"} if the TELUS-side fetch itself fails.

    GET http://127.0.0.1:<CONTROL_PORT>/control/metrics
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Talkdown counters: attempts, succeeded/stopped/failed, failures by reason,
    endpoint and codec usage, renegotiations, seconds of audio played, and the
    last 20 talks. In-memory, reset on restart. /control/status carries a short
    summary of the same data.

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/talk
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>
    Content-Type: application/json
    Body: {"audio_url": "<http(s) url the bridge fetches>"}   and/or
          {"audio_path": "<path readable by the bridge>"}
      Optional body fields: "codec" (string, case-insensitive -- only "mulaw"
      is currently defined; omit for the default; an unknown value is the 400
      bad_codec below), "conditioning" (bool -- false disables the default
      audio filter chain, config.TALK_AUDIO_FILTER), and "audio_filter"
      (string -- an explicit ffmpeg -af chain that overrides both the default
      chain and "conditioning"; the empty string "" disables conditioning).
      Play a short audio clip (typically HA-rendered TTS) through the camera's
      speaker via two-way audio (talkdown). audio_url wins if both are given.
      Enabled by default (config.TALKDOWN_ENABLED); returns 403 when it has been
      turned off. Nothing plays unless a caller asks for it. Single in-flight talk per camera -- a concurrent
      trigger for the same camera is rejected (409), never queued. See talk.py.

    Responses:
      200 {"status": "spoken", "path": "webrtc"|"srtp"}
      400 {"error": "malformed"}          -- bad JSON / neither audio field / bad audio
      400 {"error": "bad_codec"}          -- "codec" not in the supported set
      401 {"error": "bad_secret"}         -- Authorization missing/wrong
      403 {"error": "talkdown_disabled"}  -- TALKDOWN_ENABLED was set false
      404 {"error": "unknown_camera"}     -- camera_id not in camera_registry
      409 {"error": "talk_in_progress"}   -- a talk is already in flight for THIS camera
      413 {"error": "media_too_large"}    -- fetched/read audio exceeded the cap
      422 {"error": "no_upstream_audio"}  -- no webrtc/srtp upstream-audio endpoint
      502 {"error": "telus_error"}        -- MWS auth / stream lookup / signaling failed
      504 {"error": "talk_timeout"}       -- session exceeded TALK_SESSION_TIMEOUT_S

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/talk/stop

    Stops the talk currently playing on that camera. The stream ends at the next
    chunk boundary (<=40ms) and the in-flight POST above returns its partial
    stats with "stopped": true. Response: 200 {"status": "stopping"}, or
    404 {"error": "no_talk_in_progress"} when nothing is playing.

    GET http://127.0.0.1:<CONTROL_PORT>/control/events?limit=<N>
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"events": [{"id": ..., "camera_id": ..., "timestamp": ...,
                                "description": ..., "thumbnail_url": ...,
                                "download_url": ..., "clip_duration_ms": ...}, ...]}
      Most recent video clip events across all cameras, newest first.
      thumbnail_url/download_url are themselves signed TELUS relay-host URLs,
      passed through as-is -- fetchable directly (confirmed live, no cookie
      needed), not proxied through this endpoint.

    GET http://127.0.0.1:<CONTROL_PORT>/control/automations
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"rules": [{"id": ..., "name": ..., "is_paused": ...,
                               "can_be_paused": ..., "action": ...,
                               "trigger": ..., "timeframe": ...}, ...]}
      Video-related automation rules (recording schedules etc).

    GET http://127.0.0.1:<CONTROL_PORT>/control/history?limit=<N>
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"events": [{"id": ..., "timestamp": ..., "description": ...,
                                "device_id": ..., "device_description": ...,
                                "event_type_name": ..., "thumbnail_url": ...,
                                "download_url": ..., "clip_duration_ms": ...,
                                "video_event_id": ...}, ...]}
      TELUS's system-wide Activity feed -- arm/disarm, door/lock, camera
      triggers, doorbell presses, etc, newest first. Broader than
      /control/events (which is camera clips only): entries the bridge could
      correlate to a saved clip carry the same thumbnail_url/download_url/
      clip_duration_ms shape as /control/events, so callers can offer the
      same "click to see the clip" behavior here too; entries with no
      correlated clip have those fields as null. video_event_id is the id of
      the correlated saved clip (null when there is none) -- pass it to
      /control/clip to re-mint a fresh download_url, since the one embedded
      here expires within minutes of this fetch (the clip itself does not).

    GET http://127.0.0.1:<CONTROL_PORT>/control/clip/<video_event_id>
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"download_url": ..., "thumbnail_url": ..., "clip_duration_ms": ...}
      Re-mints a FRESH signed download URL for one saved clip, on demand.
      The signed URLs embedded in /control/events and /control/history
      responses expire within minutes of being issued (alarm.com stamps
      each mint into the token; the clip itself never expires --
      ageLimitDays is -1 on every event observed live 2026-08-08), so a
      URL captured at poll time and opened later (e.g. from a phone
      notification) lands on alarm.com's "You don't have permission to
      view this clip" page. Every videoEvents fetch returns freshly-signed
      URLs -- this endpoint is a by-id fetch of exactly one event
      (get_video_events_by_ids, the same call list_history_events already
      uses for enrichment), so callers hit it at click time and redirect
      to the result.
      404 {"error": "unknown_event"} if TELUS returns no event for that id
      (deleted clip, or a bogus id -- TELUS omits it from the response
      rather than erroring).
      502 {"error": "telus_error"} if the TELUS-side fetch itself fails.

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/by-path/<path_name>/activate
    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/by-path/<path_name>/deactivate
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Not meant for Home Assistant, and not called by anything automatically as of
    2026-07-18 -- every registered camera's pipeline is started eagerly by main.py
    and left running indefinitely (see main.py's "always warm" comment), so there's
    no more on-demand start/stop driven by MediaMTX reader counts. These endpoints
    remain as a manual lever (e.g. force one camera's pipeline to restart without
    touching the others, or without a full container restart). <path_name> is the
    RTSP path name (config.rtsp_path_for(camera_id)), kept rather than switched to
    camera_id since MediaMTX's $MTX_PATH is still what identifies a path elsewhere
    in this codebase's conventions.

    Response: 200 {"status": "activating"} / {"status": "deactivating"}
      404 {"error": "unknown_path"} if no known camera maps to that path name.

    POST http://127.0.0.1:<CONTROL_PORT>/control/automations/<rule_id>/pause
    POST http://127.0.0.1:<CONTROL_PORT>/control/automations/<rule_id>/resume
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"status": "applied"}
      502 {"error": "telus_error"} if the TELUS-side call itself fails.
      Doesn't validate rule_id against a known-rules list first (unlike the
      snapshot endpoint's unknown_camera check) -- TELUS itself 404s/errors
      on a bad id and that's surfaced as telus_error, so a second local
      check would just duplicate what TELUS already enforces.

    ---- camera settings, diagnostics, siren and privacy --------------------

    These are INDEPENDENT of TALKDOWN_ENABLED: the settings/diagnostics routes
    speak the website API through bridge.camera_settings and keep working with
    talkdown switched off. Only the siren/privacy routes need the mobile MWS
    SessionToken, which only exists because the talk manager mints one.

    Shared error bodies across all eight: 400 {"error": "malformed"} for a bad
    body, 404 {"error": "unknown_camera"}, 422 {"error": "unsupported"} when the
    device itself reports it cannot do the thing, 502 {"error": "telus_error"}
    on an upstream failure, and 429 {"error": "rate_limited", "retry_after":
    <int>} with a Retry-After header when the alarm.com edge rate-limits a
    website call. The settings bundle's MWS halves are the exception to that
    last one: a rate-limited siren/privacy read is absorbed and held for the
    server's wait, degrading those sections to null instead of failing the
    response.

    GET http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/settings
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"audio": {...}, "image": {...}, "video": {...},
                    "led": {...}, "notifications": {...},
                    "solar": {"supports_connected": <bool>,
                              "connected": <bool>, "raw": {...}},
                    "siren": {"can_manually_trigger": <bool>, "active": <bool>,
                              "timestamp_end": <value or null>} | null,
                    "privacy": {"supported": <bool>, "active": <bool>} | null}
      The camera's website settings objects, each carrying its own
      supports_* flags. The last three come from ONE generalSettings read:
      the status LED, the TELUS-app notification toggles, and the read-only
      solar-panel status (backlog E39 -- there is no write route for it, and
      supports_connected is false on every camera that is not a battery+solar
      model, which is the expected answer rather than a gap).
      Those flags are not decoration: every website settings
      field is paired with a "<name>FeatureSupport" enum (0 = unsupported,
      2 = editable) and the SAME payload shape comes back for a device that
      CANNOT do the thing, so a caller must read the flag before offering a
      control rather than reading a field's presence as permission.
      "siren"/"privacy" are this camera's records from the mobile-side
      GetAllSirenDevices / GetAllCameraPrivacyModeInfo, and are the only read a
      caller gets for "may this account trigger the siren" and "is privacy mode
      on" -- there is no separate route for either. Their keys above are this
      API's contract, NOT the upstream spelling: _normalize_siren /
      _normalize_privacy translate the record, because the upstream key names
      come from the decompiled app rather than a live capture and its booleans
      arrive as the STRINGS "true"/"false" -- passing the raw record through
      made every caller re-implement the same candidate-key list and the same
      string-bool trap, and get it wrong. Both sections are best-effort and
      degrade to null (never a 502) when the talk manager was not constructed or
      the MWS read fails, so an MWS outage still returns the website settings.
      Four website GETs per call plus AT MOST two MWS POSTs: those two actions
      are account-wide, so they are read once per _MWS_ACCOUNT_READ_TTL_S and
      shared by every camera rather than re-issued per camera. Poll this SLOWLY
      regardless -- alarm.com's rate limits are undocumented and can lock the
      account.

    GET http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/diagnostics
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"signal_strength": ..., "battery": ..., "device_status": ...,
                    "raw": {...}}
      A camera degrading to poor Wi-Fi is otherwise a silent failure mode --
      nothing else in this API surfaces it.

    POST http://127.0.0.1:<CONTROL_PORT>/control/cameras/<camera_id>/settings/volume
    Body: {"volume": <int>}

      The talk/call volume -- i.e. the volume talkdown itself plays at (E9 in
      docs/backlog.md). Bounded by the device's own
      call_volume_min..call_volume_max as reported by the settings read; a value
      outside that range is 400, never silently clamped, so a caller is told its
      value was not applied instead of quietly getting a different one.
      Response: 200 <the same object GET .../settings returns under "audio">

    POST .../settings/chime
    Body: at least one of {"indoor_on": <bool>, "chime_type": <int>,
                            "outdoor_on": <bool>, "outdoor_volume": <int>}
      The doorbell's own chime. chime_type is the WIRED CHIME TYPE and must be
      one of the values the device offers; outdoor_volume is bounded by the
      device's outdoor_tone_volume_max the same way.
      Response: the audio settings object.

    POST .../settings/led
    Body: at least one of {"enabled": <bool>, "brightness": <int>}
      The device's STATUS indicator light -- not the infrared illuminator, which
      is ir_led_enabled on the image route. The reference doorbell can toggle it
      but not dim it, so brightness is a 422 there.
      Response: 200 <the same object GET .../settings returns under "led">

    POST .../settings/notifications
    Body: at least one of {"motion": <bool>, "button_press": <bool>,
                            "smart_chime": <bool>, "format": <int>}
      What TELUS pushes to its OWN app for this camera -- unrelated to anything
      Home Assistant notifies about. Motion and button-press share one support
      enum and the smart chime has another, so a camera can offer some and not
      others. "format" chooses between a text-only push and one carrying a
      thumbnail, and must be a value listed in the response's format_choices.
      Response: 200 <the same object GET .../settings returns under
      "notifications">

    POST .../settings/image
    Body: at least one of brightness / contrast / saturation / sharpness /
      ir_sensitivity (ints), is_flipped / hdr_enabled / ir_led_enabled /
      night_vision_enabled (bools). Response: the image settings object.

    POST .../settings/video
    Body: at least one of clip_resolution (str, e.g. "960x960"), clip_quality
      (int or str), pretrigger_enabled (bool), keep_clips_days (int),
      alarm_clip_duration (int), live_resolution (str). The last one is the LIVE
      stream's resolution rather than a clip setting -- see the route docs for
      what it does and does not change about this bridge's own restream.
      Response: the video config object.

      All six writes read the current settings first, purely to honour the
      FeatureSupport gate above (422 rather than a write TELUS would refuse);
      the read-modify-write a website settings PUT requires -- it sends the
      WHOLE object back, and a partial body risks resetting unrelated fields --
      is camera_settings' job, not this module's.

    POST .../siren      Body: {"action": "start"|"stop"}
    POST .../privacy    Body: {"active": <bool>}

    Response: 200 {"status": "applied"}
      503 {"error": "mws_unavailable"} when no talk manager was constructed
          (TALKDOWN_ENABLED=false), since it owns the MWS SessionToken these
          two actions need.
      A siren "start" is gated on this camera's canManuallyTriggerSiren, which
      is a per-account PERMISSION and not merely "this device has a siren" --
      it makes a real siren sound in someone's home, so it is never fired blind
      (docs/mobile-api-findings.md). "stop" is deliberately NOT gated: silencing
      a running siren has to stay possible even when the capability read fails.
      The siren is also time-bounded server-side (timestampEnd), so it stops on
      its own eventually; "stop" is how a caller ends it early.

    ---- bluetooth arming devices -------------------------------------------

    The phones paired to the panel for BLUETOOTH DISARMING (backlog E24). These
    speak the website API through bridge.arming_devices and, like the settings
    routes, are independent of TALKDOWN_ENABLED. Same error bodies as above --
    400 {"error": "malformed"}, 422 {"error": "unsupported"},
    502 {"error": "telus_error"} -- plus 404 {"error": "unknown_device"} and,
    since these hit an account whose rate limits are undocumented,
    429 {"error": "rate_limited", "retry_after": <int>} with a Retry-After header.

    GET http://127.0.0.1:<CONTROL_PORT>/control/partitions
      The alarm partitions, each with the two flags that decide whether a bypass
      is allowed right now: has_permission_to_change_state, and
      can_bypass_when_armed alongside is_disarmed.

    POST .../control/partitions/<partition_id>/bypass
    Body: {"bypass": [<sensor id>, ...], "unbypass": [...]}  (>=1 id total)
      A BYPASSED SENSOR DOES NOT TRIGGER THE ALARM until it is un-bypassed. The
      partition is re-read before the write and the operation is refused (422)
      when it says no -- notably when the partition is armed and reports
      canBypassSensorWhenArmed false. This is NOT the integration's force_bypass
      arming option and must never become an automatic second one; see
      partitions.py. Response: 200 {"status": "applied", "partition": {...}}

    GET http://127.0.0.1:<CONTROL_PORT>/control/arming-devices
    Authorization: Bearer <contents of CONTROL_SECRET_PATH, stripped>

    Response: 200 {"devices": [{"id": "<unit>-<did>", "description": <str>,
                                "disarming_enabled": <bool>,
                                "enrolled_partition_id": <str>,
                                "supports_disarming_toggle": <bool>,
                                "supports_enrolled_partition": <bool>,
                                "enrolled_partition_choices": [
                                    {"value": "<partition number>",
                                     "label": <str>}, ...],
                                "raw": {...}}, ...]}
      One upstream request for the whole list -- it comes from the manage-devices
      catalog, which inlines each device's full record. An account with no paired
      phones returns an empty list, not a 404.
      "supports_disarming_toggle" is not decoration: a record that cannot be
      written reports the same "disarming_enabled" a writable one does, so a
      caller reading the value as permission would offer a control that silently
      does nothing. See arming_devices._supports_toggle for what it is built from.
      "enrolled_partition_choices" is the account's partitions, folded into every
      record (the same value on each) rather than offered as a sibling key: a
      caller building a picker for enrolled_partition_id needs the record and its
      choices together, and it is what the write route below validates against.
      It costs ONE extra upstream request per call, and only when the account has
      at least one paired phone. Best effort -- an empty list means the partition
      read failed, not that the account has no partitions, and a caller must then
      offer no picker rather than a picker with nothing in it.

    POST .../control/arming-devices/<device_id>/enrolled-partition
    Body: {"partition_id": "<partition number>"}

      Moves ONE paired phone to a different partition -- i.e. changes WHICH
      partition that phone disarms (backlog E43). Same class of setting as the
      disarming toggle and refused in the same four places: the body must carry a
      non-empty JSON string (the partition NUMBER as the record reports it, e.g.
      "1" -- not the "<unit>-127" device id the alarm panel entity uses),
      <device_id> must be in the list above (404), the record's own
      supports_enrolled_partition must be true (422), and the partition must be
      one this account actually has (400). That last check re-reads the partition
      list and FAILS CLOSED: if it cannot be read, the write does not happen
      (502), because "which partition disarms" is not a field to write against an
      unverified id.
      Response: 200 <one element of the array GET /control/arming-devices returns>

    POST http://127.0.0.1:<CONTROL_PORT>/control/arming-devices/<device_id>/disarming
    Body: {"enabled": <bool>}

      Turns bluetooth disarming on or off for ONE paired phone -- i.e. whether
      that phone walking up to the house disarms the alarm. Treated with the same
      caution as the siren route, and refusing in three places before it writes:
      the body must carry a real JSON boolean (a truthy string is a 400, never
      coerced), <device_id> must be in the list GET /control/arming-devices
      returns (a 404 rather than a blind PUT to an arbitrary device id on a live
      panel), and the device's own supports_disarming_toggle must be true (422).
      alarm.com is the CONFIGURATION AUTHORITY for this setting -- the panel's
      Control4 endpoint is local-access-only and independent -- so there is no
      second place a mistake here can be corrected from.
      The read-modify-write a device PUT requires (it replaces the WHOLE record)
      is arming_devices' job, not this module's.
      Response: 200 <one element of the array GET /control/arming-devices returns>

    GET  http://127.0.0.1:<CONTROL_PORT>/control/bluetooth-disarming
    POST http://127.0.0.1:<CONTROL_PORT>/control/bluetooth-disarming
    Body: {"pairing_enabled": <bool>?, "timeout": <int>?}   (any non-empty subset)

    Response: 200 {"id": "<system id>", "pairing_enabled": <bool>,
                   "supports_pairing_enabled": <bool>, "timeout": <int|null>,
                   "supports_timeout": <bool>, "timeout_unit": null,
                   "enrolled_device_ids": ["<unit>-<did>", ...], "raw": {...}}

      The PANEL-level Bluetooth disarming settings (backlog E41) -- system-wide,
      not per phone. Two things a caller must not misread:

      * This is NOT the capability gate for the per-device toggle above. The
        portal's per-device page never consults it, and whether the panel
        firmware treats a system-wide false as a veto over a per-device true is
        UNRESOLVED. Do not tell a user that one disables the other.
      * "timeout" has NO ESTABLISHED UNIT. The document publishes no unit, no
        bounds and no options; the backlog's "minutes" was never verified, which
        is why "timeout_unit" is null rather than a string. A caller that needs a
        range has to choose one and own the choice.

      THE WRITE SHAPE IS INFERRED, NOT CAPTURED -- no PUT to this endpoint was
      ever observed. See arming_devices.set_bluetooth_disarming_settings for
      exactly which parts are deductions and which are guesses. Unlike the
      per-device write there is no membership pre-read here: the document is the
      session's own system, so there is nothing to check it against, and the
      setter's own read-modify-write carries the capability gate.
"""
import asyncio
import logging
import os
import secrets as secrets_mod
import time

import aiohttp
from aiohttp import web

# Typed application keys. aiohttp warns (NotAppKeyWarning) on plain-str
# Application keys and web.AppKey has been the supported form since 3.9.
APP_CURRENT_SECRET = web.AppKey("current_secret", dict)
APP_SECRET_ROTATION_TASK = web.AppKey("secret_rotation_task", asyncio.Task)

from . import arming_devices
from . import camera_settings
from . import partitions as partitions_mod
from . import config as c
from . import metrics
from . import talk as talk_mod
from . import talk_fullduplex
from .mobile_auth import mint_if_stale
# _jget/_json_bool are module-private in mws_client and imported anyway, on
# purpose: they are the canonical way this codebase reads an MWS JSON scalar, and
# the trap they exist for -- the API sends booleans as the STRINGS "true"/"false",
# so bool("false") is True -- is exactly what a second, local implementation gets
# wrong. Sharing them keeps one definition of "what does this camera actually say"
# rather than two that can drift apart.
from .mws_client import (
    TOKEN_STATUS_LOGGED_OUT,
    _jget as _mws_jget,
    _json_bool as _mws_json_bool,
    get_privacy_mode_info,
    get_siren_devices,
    set_privacy_mode,
    start_siren,
    stop_siren,
    get_doorbell_settings,
    parse_chime_type_options,
)
from .telus_auth import RateLimited, relogin_if_stale
from .telus_rest import (
    get_snapshot_url,
    get_video_events_by_ids,
    list_automation_rules,
    list_history_events,
    list_video_events,
    pause_rule,
    resume_rule,
)

log = logging.getLogger(__name__)


async def _fetch_with_relogin(telus_session, login_lock, coro_fn, *args):
    """Call coro_fn(telus_session, *args); on a 401/403 (expired session
    cookie), re-login once (serialized against every other login() caller
    via login_lock, same as main.py's get_video_source_with_relogin) and
    retry exactly once before giving up.
    """
    try:
        return await coro_fn(telus_session, *args)
    except aiohttp.ClientResponseError as err:
        if err.status in (401, 403):
            log.warning("Control endpoint: %s got %s, re-logging in to TELUS",
                        coro_fn.__name__, err.status)
            await relogin_if_stale(telus_session, login_lock)
            return await coro_fn(telus_session, *args)
        raise


# Accepted body fields for the image/video settings writes and the type each
# must have. Enforced here rather than left to camera_settings so a typo'd key
# is a local 400 instead of a round trip against a rate-limited account.
_IMAGE_FIELDS: dict[str, type | tuple[type, ...]] = {
    "brightness": int,
    "contrast": int,
    "saturation": int,
    "sharpness": int,
    "ir_sensitivity": int,
    # Unsupported on both cameras here (canSetMaxExposure false), so it has never
    # been exercised against hardware -- routed because camera_settings already
    # parses it and another model may offer it.
    "max_exposure": int,
    "is_flipped": bool,
    "hdr_enabled": bool,
    "ir_led_enabled": bool,
    "night_vision_enabled": bool,
}
_VIDEO_FIELDS: dict[str, type | tuple[type, ...]] = {
    "clip_resolution": str,
    # A STRING, despite looking like a number: the capture shows
    # selectedClipQuality as "3", a KEY of the clipQualityOptions object, not the
    # integer value it maps to (docs/web-portal-api-findings.md). This used to
    # accept (int, str) from before the capture settled it, which disagreed with
    # camera_settings' own "str" typing -- an int passed here only to be refused a
    # layer down.
    "clip_quality": str,
    "pretrigger_enabled": bool,
    "keep_clips_days": int,
    "alarm_clip_duration": int,
    "camera_triggered_clip_duration": int,
    # The LIVE stream's resolution, not a clip setting. Must be one of the values
    # in live_resolution_options; camera_settings checks it against the device's
    # own webOptions before writing.
    "live_resolution": str,
}
# generalSettings -> ledLightSetting: the device's STATUS indicator light, not the
# infrared illuminator (that is `ir_led_enabled` on the image route).
_LED_FIELDS: dict[str, type | tuple[type, ...]] = {
    "enabled": bool,
    "brightness": int,
}
# generalSettings -> doorbellNotificationSetting: what TELUS pushes to its OWN
# app, which is a different thing from anything Home Assistant sends.
_NOTIFICATION_FIELDS: dict[str, type | tuple[type, ...]] = {
    "motion": bool,
    "button_press": bool,
    "smart_chime": bool,
    # An enum, not a bool: the allowed values are the ones the settings response
    # lists in format_choices. camera_settings enforces that set, because the
    # device publishes no options of its own for this field.
    "format": int,
}
# The PANEL-level bluetooth disarming document (backlog E41). System-wide, not
# per phone, and nothing to do with a camera despite sharing this validator.
#
# `timeout` is an int with NO declared unit and no bounds, here or anywhere else
# in this codebase: the document publishes none and the backlog's "minutes" was
# never verified. It is listed as a plain int on purpose -- _validate_fields'
# int branch also rejects a bool, which matters because bool is an int subclass
# and {"timeout": true} would otherwise reach a live panel as 1.
_BLUETOOTH_DISARMING_FIELDS: dict[str, type | tuple[type, ...]] = {
    "pairing_enabled": bool,
    "timeout": int,
}
_CHIME_FIELDS: dict[str, type | tuple[type, ...]] = {
    "indoor_on": bool,
    "chime_type": int,
    "outdoor_on": bool,
    "outdoor_volume": int,
}
# One support flag per chime field -- and the outdoor tone's on/off is NOT the
# same feature as its volume. The reference doorbell reports
# outdoorToneEnableDisableFeatureSupport 2 alongside outdoorToneVolumeFeatureSupport
# 0: it can mute the outdoor tone but not change its level. Mapping both to the
# on/off flag let a volume write past this pre-read gate and be refused a layer
# down by camera_settings instead -- the same 422 in the end, but by accident, and
# only after a round trip against a rate-limited account.
_CHIME_SUPPORT: dict[str, str] = {
    "indoor_on": "supports_indoor_chime",
    # The chime TYPE is a third, separate feature enum
    # (indoorChimeTypeFeatureSupport) -- not the indoor chime's on/off flag.
    "chime_type": "supports_chime_type",
    "outdoor_on": "supports_outdoor_tone",
    "outdoor_volume": "supports_outdoor_tone_volume",
}


def _validate_fields(body: object, spec: dict) -> dict | None:
    """Return the requested fields from `body`, or None if the body is unusable.

    None means "respond 400": not a JSON object, empty, an unknown key, or a
    value of the wrong type. Trap: bool is a subclass of int in Python, so an
    int-typed field must reject True/False explicitly -- otherwise
    {"brightness": true} would be written to a live camera as brightness=1.
    """
    if not isinstance(body, dict) or not body:
        return None
    fields: dict = {}
    for key, value in body.items():
        want = spec.get(key)
        if want is None:
            return None
        if want is bool:
            if not isinstance(value, bool):
                return None
        elif isinstance(value, bool) or not isinstance(value, want):
            return None
        fields[key] = value
    return fields


def _unsupported_flags(current: dict, flags: list[str]) -> list[str]:
    """Return the entries of `flags` the device explicitly reports it cannot do.

    Only a flag that is PRESENT and exactly False blocks a write. Every website
    settings field is paired with a "<name>FeatureSupport" enum and the same
    payload shape comes back for a device that cannot do the thing, so a flag
    camera_settings did not surface means "unknown", not "unsupported" --
    refusing on a missing flag would make a perfectly capable camera permanently
    un-settable through this API.
    """
    return [flag for flag in flags if current.get(flag) is False]


def _settings_error(err, camera_id: str, what: str) -> web.Response:
    """Map a camera_settings.CameraSettingsError onto this API's status codes.

    `unsupported` is the device reporting it cannot do the thing at all (its
    *FeatureSupport enum is not 2) -- the documented 422, and the same answer
    _unsupported_flags gives from the pre-read gate. Everything else the exception
    covers is a CALLER mistake (an unknown field, a wrong value type, a number
    outside the range the device published) and is a 400, matching what
    _out_of_range already returns for the value it can check locally: an
    out-of-range write is a 400 whichever of the two notices it first.

    Deliberately branches on the flag and never on the message text, as that
    exception's own docstring requires. Logged at info, not exception: neither
    outcome is a bug in this process, and a stack trace per refused write would
    bury the ones that are."""
    status, body = (422, "unsupported") if err.unsupported else (400, "malformed")
    log.info("Control endpoint: %s refused for %s (field=%s, unsupported=%s): %s",
             what, camera_id, err.field, err.unsupported, err)
    return web.json_response({"error": body}, status=status)


def _bound(setter, **fields):
    """Bind keyword `fields` to a camera_settings setter so it can be passed to
    _fetch_with_relogin, which forwards positional args only and logs
    coro_fn.__name__ (functools.partial has no __name__).
    """
    async def call(telus, camera_id):
        return await setter(telus, camera_id, **fields)

    call.__name__ = setter.__name__
    return call


# The two account-wide MWS reads, named once so the cache key, the log line and
# the post-write invalidation below cannot drift apart.
_MWS_SIREN_READ = "GetAllSirenDevices"
_MWS_PRIVACY_READ = "GetAllCameraPrivacyModeInfo"

# How long one account-wide MWS read is reused across cameras. Both actions are
# ACCOUNT-WIDE -- tests/test_mws_json_actions.py asserts neither may even carry a
# DeviceId -- but /settings is per-camera, so re-issuing them there cost 2 identical
# upstream POSTs per camera per poll against an API whose rate limits are
# undocumented and can lock the account.
#
# The window only has to outlive ONE of Home Assistant's coordinator sweeps, not
# its 120s poll interval: that sweep walks the cameras SEQUENTIALLY (deliberately
# -- see camera_settings_coordinator.py) at roughly six upstream round trips each,
# so a handful of cameras is tens of seconds. 30s covers that while staying a
# quarter of the poll interval, i.e. it can add at most 25% to a staleness the
# entities reading these records already accept by design. It is not load-bearing
# for correctness after a write: both write routes invalidate their own action's
# entry, so the read-back the privacy switch performs is never served from here.
# Lives here rather than in config.py because it is an implementation detail of
# this route, not a knob an operator should be turning against a live account.
_MWS_ACCOUNT_READ_TTL_S = 30

# The chime-type option LIST is hardware, not state: a doorbell offers the same
# handful of wired-chime types for its whole life. Re-reading it on the 30s
# account-wide TTL would be one upstream round trip per camera per poll to be told
# the same three values, so it is held for an hour. The current SELECTION is not
# cached here -- it comes from the website settings read in the same response.
_MWS_CHIME_OPTIONS_TTL_S = 3600

# How many container levels below an MWS action result _mws_entry_for_device
# descends before giving up. The known shapes sit one level below the ack's
# "data" ({"CameraPrivacyModeInfo": {...}}) or two ({"AllCameraSirenInfo":
# [{...}]}), so 3 clears both with one level of slack for a wrapper alarm.com
# adds -- these shapes come from the decompiled app, not a live capture. Bounded
# rather than unlimited so the cost of this search stays something you can read
# off the page: an unexpected payload turns into a miss (which every caller
# already handles), never into a walk of an arbitrarily deep tree on every poll.
_MWS_SEARCH_DEPTH = 3


def _mws_ack_data(result: dict) -> dict:
    """Return the action's own body from an mws_client ack envelope.

    _parse_json_ack hands back ``{"ok", "error", "data"}`` with the per-device
    records inside ``data`` -- so code that iterated the ack itself was iterating
    [bool, str|None, dict] and matched nothing. Recognised by the presence of
    "ok"/"error" ALONGSIDE a dict "data" rather than by "data" alone, so a raw
    action body that happens to carry a "data" member is not unwrapped by
    accident. Anything that is not an ack is returned untouched, which keeps this
    usable on a bare action body in a test.
    """
    if isinstance(result, dict) and isinstance(result.get("data"), dict) and (
            "ok" in result or "error" in result):
        return result["data"]
    return result


def _mws_entry_for_device(result: dict, camera_id: str) -> dict | None:
    """Return the per-device record for `camera_id` in an MWS JSON action result.

    Takes the ack _parse_json_ack returns (or a bare action body) and searches
    every list- and dict-valued member of it down to _MWS_SEARCH_DEPTH instead of
    one fixed key: these two actions' shapes were recovered from the decompiled
    app rather than a live capture (`AllCameraSirenInfo` is a list,
    `CameraPrivacyModeInfo` an object -- docs/mobile-api-findings.md), so the key
    they arrive under is not something to hard-code against. Matches the website
    globalDeviceId "<unit-id>-<did>" against either half as well as whole, because
    MWS speaks the bare numeric id (mws_client.get_live_video_stream splits it the
    same way). Returns None when nothing matches -- callers decide what that
    means, and the siren and privacy routes deliberately decide differently.
    """
    want = str(camera_id)
    candidates = {want, want.split("-", 1)[0], want.rsplit("-", 1)[-1]}

    def _matches(item: object) -> bool:
        if not isinstance(item, dict):
            return False
        return any(key in item and str(item[key]) in candidates
                   for key in ("device_id", "DeviceId", "did", "id"))

    def _search(node: object, depth: int) -> dict | None:
        if depth < 0:
            return None
        if isinstance(node, dict):
            if _matches(node):
                return node
            children: object = node.values()
        elif isinstance(node, list):
            children = node
        else:
            return None
        for child in children:
            found = _search(child, depth - 1)
            if found is not None:
                return found
        return None

    return _search(_mws_ack_data(result), _MWS_SEARCH_DEPTH)


def _mws_flag(entry: dict, *names: str):
    """Return the first of `names` present in `entry`, else None.

    Accepts both the wire spelling from the decompiled app
    ("canManuallyTriggerSiren", "SupportsPrivacyMode") and a snake_case
    normalisation, since no live capture exists to settle which one mws_client
    emits. None means "the device said nothing about it", which is NOT the same
    as False -- each call site documents which way it errs.
    """
    for name in names:
        if name in entry:
            return entry[name]
    return None


def _normalize_siren(entry: dict) -> dict:
    """Map one GetAllSirenDevices record to the keys this API publishes:

        {"can_manually_trigger": bool, "active": bool, "timestamp_end": <value|None>}

    The translation lives on this side of the wire on purpose. The upstream
    spellings were recovered from the decompiled app rather than a live capture,
    and this API sends booleans as the STRINGS "true"/"false" -- so every consumer
    that read the record raw would have to carry the same candidate-key list and
    the same trap, and a plain truthiness test on "false" silently means True.

    ``can_manually_trigger`` therefore fails CLOSED: absent, null or unrecognised
    is False. It is the per-account PERMISSION that gates sounding a real siren in
    someone's home (the app has a dedicated checker for it), not a hint about the
    hardware. ``timestamp_end`` is passed through untouched -- the server picks
    both the value and its format, and an active siren with no timestamp_end is
    the app's own error case, i.e. "state unknown", not "runs forever".
    """
    return {
        "can_manually_trigger": _mws_json_bool(
            _mws_flag(entry, "can_manually_trigger_siren", "canManuallyTriggerSiren"),
            default=False),
        "active": _mws_json_bool(
            _mws_flag(entry, "is_active", "isActive", "IsActive"), default=False),
        "timestamp_end": _mws_flag(entry, "timestamp_end", "timestampEnd",
                                   "TimestampEnd"),
    }


def _normalize_privacy(entry: dict) -> dict:
    """Map one GetAllCameraPrivacyModeInfo record to the keys this API publishes:

        {"supported": bool, "active": bool}

    ``supported`` defaults the OPPOSITE way from _normalize_siren's permission
    flag, deliberately. This action returns a privacy-STATE object rather than an
    enumeration of capable devices, so a missing SupportsPrivacyMode is a gap in a
    shape recovered from the decompiled app, not the device saying no; only an
    explicit false reports unsupported. Erring the other way would leave the
    privacy switch permanently unavailable on every camera. (SupportsPrivacyMode
    IS false on the account this was built against, so the gate is expected to
    fire -- docs/mobile-api-findings.md.) _json_bool cannot express that
    distinction on its own -- it collapses "absent" and "false" into False -- so
    the None _mws_flag returns for an absent key is handled here instead.

    ``active`` comes from ``State``, which the one recovered shape carries as 0/1;
    _json_bool reads that, a real bool, or the "true"/"false" strings the request
    side of this API uses. Anything else is reported as off rather than guessed
    at: claiming a camera is blinded when it is still recording is the dangerous
    direction to be wrong in.
    """
    supports = _mws_flag(entry, "supports_privacy_mode", "SupportsPrivacyMode")
    return {
        "supported": True if supports is None else _mws_json_bool(supports, default=True),
        "active": _mws_json_bool(
            _mws_flag(entry, "state", "State", "is_active", "IsActive"), default=False),
    }


def _mws_token_status(result: dict) -> int | None:
    """The MWS TokenStatus carried by an action result, or None if it carried none.

    Both places an mws_client parser can put it are read, because the two put it
    in different places and this module must not care which one it is holding:

    * TOP LEVEL -- _parse_json_ack surfaces ``token_status`` on the ack itself
      (and strips ``tokenstatus`` out of ``data`` so it cannot be read twice).
      This is the JSON actions' source, and it is what makes the logged-out
      re-mint in _mws_call reachable rather than the dead branch it used to be,
      when this read ``result["token_status"]`` against an ack that had no such
      key and so was always None.
    * INSIDE ``data`` -- _parse_validate_session emits ``data["token_status"]``
      instead, since it models a status-only response.

    Checking both is what keeps this decoupled from either parser's layout: an
    envelope key that moves (as ``TokenStatus`` just did, from surviving inside
    ``data`` to being hoisted onto the ack) is absorbed here rather than needing
    a matching edit at every call site. ``data`` is checked first so a future
    action that carries a status in its own body still wins over the envelope's.

    Returns None, never a guess, when the field is absent or unparsable: None
    means "the server said nothing about the token", and callers must not treat
    that as a status value -- least of all as TOKEN_STATUS_OK, which would make a
    logged-out session that simply does not report itself look healthy (see
    _mws_failure_note, which omits the field entirely rather than logging None).
    """
    sources = (result.get("data") if isinstance(result, dict) else None, result)
    for source in sources:
        if not isinstance(source, dict):
            continue
        for name in ("TokenStatus", "token_status"):
            raw = _mws_jget(source, name)
            if raw is None:
                continue
            try:
                return int(raw)
            except (TypeError, ValueError):
                log.debug("Control endpoint: unparsable MWS %s=%r", name, raw)
                return None
    return None


def _mws_failure_note(result: dict) -> str:
    """A short reason string for logging a not-ok MWS result.

    Always reports the server's ErrorMessage, and reports the token status ONLY
    when the response actually carried one. The lines here used to log
    "token_status=%s" unconditionally against a key _parse_json_ack never emits,
    so every one of them printed None -- which reads as "the server answered
    'unknown'" when it means "we never looked in the right place".
    """
    parts = [f"error={result.get('error')!r}"]
    status = _mws_token_status(result)
    if status is not None:
        parts.append(f"token_status={status}")
    return ", ".join(parts)


def _tighten_secret_mode() -> None:
    """Re-tighten an ADOPTED control_secret file to 0o600 when it is looser.

    The 0o600 in _write_secret and the generate path below only applies to files
    THIS process creates; a secret restored from a backup or written under a
    permissive umask arrives with whatever mode it was given and would sit
    group/world-readable on the shared bind mount until the first scheduled
    rotation happens to replace it. Best-effort, like the HA side's handling of
    its credentials.json sibling: a chmod that fails (e.g. the file is owned by
    another uid on the shared mount) is logged and the secret is still served --
    refusing to start over a permissions wart would turn it into an outage.
    Its own try block on purpose: an OSError here must NOT fall into
    _load_or_create_secret's read handler, which would regenerate a perfectly
    good secret. Logs the mode only, never the secret itself.
    """
    try:
        mode = os.stat(c.CONTROL_SECRET_PATH).st_mode & 0o777
        if mode & 0o077:
            os.chmod(c.CONTROL_SECRET_PATH, 0o600)
            log.warning("Re-tightened %s from mode %04o to 0600",
                        c.CONTROL_SECRET_PATH, mode)
    except OSError:
        log.warning("Could not re-tighten permissions on %s; it may remain "
                    "group/world-readable", c.CONTROL_SECRET_PATH, exc_info=True)


def _load_or_create_secret() -> str:
    try:
        with open(c.CONTROL_SECRET_PATH) as f:
            secret = f.read().strip()
            if secret:
                _tighten_secret_mode()
                return secret
            log.warning("%s exists but is empty; regenerating", c.CONTROL_SECRET_PATH)
    except FileNotFoundError:
        pass
    except OSError:
        log.exception("Could not read %s; regenerating in place", c.CONTROL_SECRET_PATH)

    os.makedirs(c.SHARED_DIR, exist_ok=True)
    secret = secrets_mod.token_hex(32)
    tmp_path = f"{c.CONTROL_SECRET_PATH}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.replace(tmp_path, c.CONTROL_SECRET_PATH)
    log.info("Generated new control-endpoint shared secret at %s", c.CONTROL_SECRET_PATH)
    return secret


def _write_secret(secret: str) -> None:
    os.makedirs(c.SHARED_DIR, exist_ok=True)
    tmp_path = f"{c.CONTROL_SECRET_PATH}.tmp"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(secret)
    os.replace(tmp_path, c.CONTROL_SECRET_PATH)


async def _rotate_secret_periodically(current_secret: dict) -> None:
    """Regenerate the shared secret every CONTROL_SECRET_ROTATE_INTERVAL_S and
    atomically rewrite it to disk. No coordination or grace period with the HA
    side is needed: HA's bridge_client reads the secret fresh off disk on every
    single request rather than caching it, so the very next poll after a rotation
    picks up the new value. Not strictly zero-downtime -- an HA request that read
    the old secret microseconds before os.replace lands can 401 once -- but that
    is at most one request per 30-day rotation and every caller already retries."""
    while True:
        await asyncio.sleep(c.CONTROL_SECRET_ROTATE_INTERVAL_S)
        new_secret = secrets_mod.token_hex(32)
        try:
            _write_secret(new_secret)
        except OSError:
            # Keep serving the CURRENT secret and try again next interval. Updating
            # the in-memory value after a failed write would be far worse than not
            # rotating: HA reads the secret from this very file, so every subsequent
            # request would 401 until someone noticed. An unhandled raise here was
            # also silently fatal -- it killed the rotation task for the life of the
            # process with no retry and no log, since nothing awaits this task.
            log.exception("Control endpoint: failed to write rotated secret to %s; keeping "
                          "the current secret and retrying in %ss",
                          c.CONTROL_SECRET_PATH, c.CONTROL_SECRET_ROTATE_INTERVAL_S)
            continue
        current_secret["value"] = new_secret
        log.info("Control endpoint: rotated shared secret (scheduled %ss rotation)",
                  c.CONTROL_SECRET_ROTATE_INTERVAL_S)


def create_app(telus_session, login_lock, camera_registry: dict, camera_status: dict, lifecycle, talk_manager=None) -> web.Application:
    # Held in a dict (not a bare local) so _rotate_secret_periodically can
    # update it in place and every closure below sees the new value -- a bare
    # `secret = ...` reassignment elsewhere wouldn't be visible to these
    # already-created closures.
    current_secret = {"value": _load_or_create_secret()}

    # video_event_id -> (monotonic_ts, response_payload). See handle_clip.
    _clip_cache: dict[str, tuple[float, dict]] = {}

    # MWS action name -> (monotonic deadline, action result or None). Holds the
    # two account-wide reads for _MWS_ACCOUNT_READ_TTL_S so one coordinator sweep
    # costs one POST each instead of one per camera. Only ever two keys, so unlike
    # _clip_cache it needs no eviction pass. Failures are cached too (as None) --
    # see _mws_account_read. The per-action locks stop a cold cache stampeding
    # into N simultaneous upstream calls when several camera requests are in
    # flight at once, which is the exact burst this cache exists to prevent.
    _mws_account_cache: dict[str, tuple[float, dict | None]] = {}
    _mws_account_locks: dict[str, asyncio.Lock] = {}

    def _camera_for_path(path_name: str) -> dict | None:
        return next(
            (cam for cam in camera_registry.values() if c.rtsp_path_for(cam["id"]) == path_name),
            None,
        )

    def _check_auth(request: web.Request) -> web.Response | None:
        """Returns a 401 response if the request's Bearer token doesn't match,
        else None. Shared by every authed endpoint below."""
        provided = request.headers.get("Authorization", "")
        try:
            matches = secrets_mod.compare_digest(provided, f"Bearer {current_secret['value']}")
        except TypeError:
            # compare_digest refuses non-ASCII str operands outright. A header with a
            # stray non-ASCII byte is simply a failed authentication, but unguarded it
            # escaped as a 500 -- contradicting the documented contract (401) and
            # turning a trivially-triggerable bad header into an error-level traceback.
            matches = False
        if not matches:
            log.warning("Control endpoint: rejected %s request with missing/bad secret", request.path)
            return web.json_response({"error": "bad_secret"}, status=401)
        return None

    async def handle_health(request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def handle_cameras(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        return web.json_response({"cameras": list(camera_registry.values())})

    async def handle_status(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        # "talk" is a small summary; the full counters live at /control/metrics.
        return web.json_response({"cameras": camera_status,
                                  "talk": metrics.talk.summary()})

    async def handle_metrics(request: web.Request) -> web.Response:
        """Counters for the talkdown path: attempts, outcomes, failure reasons,
        durations, and a rolling window of recent talks. In-memory only -- they
        reset with the bridge, which is what makes them cheap and what makes the
        log the durable record."""
        if (err := _check_auth(request)) is not None:
            return err
        return web.json_response({"talk": metrics.talk.snapshot()})

    async def handle_snapshot(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        camera_id = request.match_info["camera_id"]
        if camera_id not in camera_registry:
            return web.json_response({"error": "unknown_camera"}, status=404)

        try:
            snapshot_url = await _fetch_with_relogin(telus_session, login_lock, get_snapshot_url, camera_id)
            async with telus_session.session.get(snapshot_url) as resp:
                resp.raise_for_status()
                jpeg_bytes = await resp.read()
        except aiohttp.ClientResponseError as err:
            # str(ClientResponseError) embeds the request URL, and for the fetch
            # above that is the SIGNED snapshot URL -- so the blanket handler
            # below would print a live capability token into the log. Status and
            # camera id only, and no exc_info, since the traceback formats the
            # same exception text.
            log.warning("Control endpoint: on-demand snapshot fetch failed for %s "
                        "(HTTP %s)", camera_id, err.status)
            return web.json_response({"error": "telus_error"}, status=502)
        except Exception:
            log.exception("Control endpoint: on-demand snapshot fetch failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.Response(body=jpeg_bytes, content_type="image/jpeg")

    async def handle_talk(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        # Enabled by default: the route plays only what a caller asks it to, so
        # a deployed talk path never speaks on its own. TALKDOWN_ENABLED=false
        # turns the endpoint off entirely for a deployment that does not want it.
        if not c.TALKDOWN_ENABLED:
            return web.json_response({"error": "talkdown_disabled"}, status=403)
        if talk_manager is None:
            # Enabled but not constructed (shouldn't happen -- main.py builds it
            # whenever the flag is on); fail safe rather than 500.
            return web.json_response({"error": "talkdown_disabled"}, status=403)

        camera_id = request.match_info["camera_id"]
        if camera_id not in camera_registry:
            return web.json_response({"error": "unknown_camera"}, status=404)

        try:
            body = await request.json()
            audio_url = (body.get("audio_url") or "").strip()
            audio_path = (body.get("audio_path") or "").strip()
            codec = (body.get("codec") or "").strip().lower() or None
            conditioning = body.get("conditioning")
            audio_filter_req = body.get("audio_filter")
        except Exception:
            log.warning("Control endpoint: malformed talk request for %s", camera_id,
                        exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)

        if not audio_url and not audio_path:
            return web.json_response({"error": "malformed"}, status=400)
        if audio_url and not audio_url.startswith(("http://", "https://")):
            return web.json_response({"error": "malformed"}, status=400)
        if codec is not None and codec not in talk_fullduplex.CODECS:
            return web.json_response({"error": "bad_codec"}, status=400)
        # Audio conditioning is on by default (config.TALK_AUDIO_FILTER). A request
        # may disable it ("conditioning": false) or override the chain
        # ("audio_filter": "<ffmpeg -af string>"). audio_filter=None -> config default.
        if conditioning is not None and not isinstance(conditioning, bool):
            return web.json_response({"error": "malformed"}, status=400)
        if audio_filter_req is not None and not isinstance(audio_filter_req, str):
            return web.json_response({"error": "malformed"}, status=400)
        if isinstance(audio_filter_req, str):
            audio_filter = audio_filter_req            # explicit override (may be "")
        elif conditioning is False:
            audio_filter = ""                          # explicit disable
        else:
            audio_filter = None                        # config default

        try:
            result = await talk_manager.speak(
                camera_id,
                audio_url=audio_url or None,
                audio_path=audio_path or None,
                codec=codec,
                audio_filter=audio_filter,
            )
        except talk_mod.TalkInProgress:
            return web.json_response({"error": "talk_in_progress"}, status=409)
        except talk_mod.TalkMediaTooLarge:
            return web.json_response({"error": "media_too_large"}, status=413)
        except talk_mod.TalkMediaError:
            log.warning("Control endpoint: bad talk audio for %s", camera_id, exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        except talk_mod.NoUpstreamAudio:
            return web.json_response({"error": "no_upstream_audio"}, status=422)
        except asyncio.TimeoutError:
            log.warning("Control endpoint: talk timed out for %s", camera_id)
            return web.json_response({"error": "talk_timeout"}, status=504)
        except Exception:
            log.exception("Control endpoint: talk failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"status": "spoken", **result})

    async def handle_events(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        # Clamped, not refused, unlike the settings writes: this is a page size
        # passed verbatim to the live account (telus_rest sends it as pageSize),
        # not a value written to hardware, so bounding it changes nothing a
        # caller was promised -- while an absurd number would ask alarm.com for
        # an arbitrarily large response on an account whose rate limits are
        # undocumented.
        limit = max(1, min(100, limit))

        try:
            events = await _fetch_with_relogin(telus_session, login_lock, list_video_events, limit)
        except Exception:
            log.exception("Control endpoint: video events fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"events": events})

    async def handle_clip(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        video_event_id = request.match_info["video_event_id"]

        # Short TTL cache of re-minted responses, keyed by event id. The signed
        # URL alarm.com returns is valid for minutes, so serving the same one for
        # CLIP_URL_CACHE_TTL_S turns a burst of taps on one notification (or a
        # browser that re-requests on tab-restore) into a single TELUS round trip
        # instead of one per tap -- the endpoint is otherwise a 1:1 unauthenticated
        # proxy into TELUS's videoEvents API for any holder of a valid link.
        now = time.monotonic()
        cached = _clip_cache.get(video_event_id)
        if cached is not None and now - cached[0] < c.CLIP_URL_CACHE_TTL_S:
            return web.json_response(cached[1])

        try:
            videos = await _fetch_with_relogin(
                telus_session, login_lock, get_video_events_by_ids, [video_event_id])
        except Exception:
            log.exception("Control endpoint: fresh clip URL fetch failed for %s", video_event_id)
            return web.json_response({"error": "telus_error"}, status=502)

        video = videos.get(video_event_id)
        if video is None or not video.get("download_url"):
            return web.json_response({"error": "unknown_event"}, status=404)

        payload = {
            "download_url": video["download_url"],
            "thumbnail_url": video["thumbnail_url"],
            "clip_duration_ms": video["clip_duration_ms"],
        }
        # Evict expired entries opportunistically so the cache can't grow without
        # bound across many distinct clips over the process's month-plus lifetime.
        for vid in [k for k, (ts, _) in _clip_cache.items() if now - ts >= c.CLIP_URL_CACHE_TTL_S]:
            del _clip_cache[vid]
        _clip_cache[video_event_id] = (now, payload)
        return web.json_response(payload)

    async def handle_automations(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            rules = await _fetch_with_relogin(telus_session, login_lock, list_automation_rules)
        except Exception:
            log.exception("Control endpoint: automation rules fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"rules": rules})

    async def handle_history(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            limit = int(request.query.get("limit", "20"))
        except ValueError:
            limit = 20
        # Same clamp as handle_events, same reasoning: numberOfEvents goes to
        # the live account as-is.
        limit = max(1, min(100, limit))

        try:
            events = await _fetch_with_relogin(telus_session, login_lock, list_history_events, limit)
        except Exception:
            log.exception("Control endpoint: activity history fetch failed")
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"events": events})

    async def _handle_rule_action(request: web.Request, action_fn) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        rule_id = request.match_info["rule_id"]
        try:
            await _fetch_with_relogin(telus_session, login_lock, action_fn, rule_id)
        except Exception:
            log.exception("Control endpoint: %s failed for rule %s", action_fn.__name__, rule_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response({"status": "applied"})

    async def handle_pause_rule(request: web.Request) -> web.Response:
        return await _handle_rule_action(request, pause_rule)

    async def handle_resume_rule(request: web.Request) -> web.Response:
        return await _handle_rule_action(request, resume_rule)

    async def handle_activate_by_path(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        camera = _camera_for_path(request.match_info["path_name"])
        if camera is None:
            return web.json_response({"error": "unknown_path"}, status=404)
        await lifecycle.activate(camera)
        return web.json_response({"status": "activating"})

    async def handle_deactivate_by_path(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err
        camera = _camera_for_path(request.match_info["path_name"])
        if camera is None:
            return web.json_response({"error": "unknown_path"}, status=404)
        await lifecycle.deactivate(camera["id"])
        return web.json_response({"status": "deactivating"})

    async def handle_credentials(request: web.Request) -> web.Response:
        if (err := _check_auth(request)) is not None:
            return err

        try:
            body = await request.json()
            username = body["username"].strip()
            password = body["password"].strip()
            if not username or not password:
                raise ValueError("empty username or password")
        except Exception:
            log.warning("Control endpoint: malformed credential-push request", exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)

        c.TELUS_EMAIL, c.TELUS_PASSWORD = username, password
        try:
            async with login_lock:
                await telus_session.login()
        except Exception:
            log.exception("Control endpoint: re-login with pushed credentials failed "
                           "(credentials have still been updated in-process; existing "
                           "reconnect/retry loops will keep retrying them)")
            return web.json_response({"error": "internal"}, status=500)

        log.info("Control endpoint: credentials updated and re-login succeeded")
        return web.json_response({"status": "applied"})

    # ---- camera settings / diagnostics (website API) ------------------------
    # Unrelated to TALKDOWN_ENABLED: these speak the same website session the
    # snapshot and events routes already use, so they must keep working with
    # talkdown switched off.

    def _camera_guard(request: web.Request) -> tuple[str | None, web.Response | None]:
        """Auth + registry check shared by the eight camera settings routes.
        Returns (camera_id, None) when the request may proceed, else
        (None, <the response to send>)."""
        if (err := _check_auth(request)) is not None:
            return None, err
        camera_id = request.match_info["camera_id"]
        if camera_id not in camera_registry:
            return None, web.json_response({"error": "unknown_camera"}, status=404)
        return camera_id, None

    async def _mws_post(action_fn, mobile, *args) -> dict:
        # Bounded like talk.py's MWS lookups so a wedged servlet call fails this
        # one request rather than holding a control-server connection open for
        # aiohttp's default.
        return await asyncio.wait_for(
            action_fn(
                telus_session.session,
                mobile.session_token,
                *args,
                customer_id=mobile.customer_id,
                mobile_device_uid=mobile.device_uid,
                seamless_login_token=mobile.seamless_login_token,
            ),
            timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
        )

    def _mobile_session():
        """The MobileSession the talk manager owns, or None when there isn't one.

        Deliberately reuses the talk manager's instance instead of constructing a
        second MobileSession: a new one registers another mobile device uid and
        re-walks the SSO bootstrap against the same Cloudflare edge that
        rate-limits the website login -- exactly the duplication
        mobile_auth.mint_if_stale exists to collapse. getattr rather than a bare
        attribute so a rename in talk.py degrades these two routes to a logged
        503 instead of 500-ing every request."""
        if talk_manager is None:
            return None
        mobile = getattr(talk_manager, "_mobile", None)
        if mobile is None:
            log.warning("Control endpoint: talk manager exposes no MobileSession; "
                        "siren/privacy routes unavailable")
        return mobile

    def _rate_limited(err: RateLimited, what: str, camera_id: str) -> web.Response:
        """Turn an edge rate-limit into a 429 that carries the server's own wait.

        These routes used to answer 502 for this, which tells a caller "upstream
        broke" when the truth is "come back in N seconds" -- so Home Assistant
        retried on its own schedule and walked straight back into the limiter.
        The header is the server's number, not ours; a caller that honours it
        backs off exactly as far as TELUS asked and no further.

        Retry-After is emitted as an integer number of seconds, which is the form
        every HTTP client understands (RFC 9110 also allows a date; nothing here
        needs one)."""
        wait = max(1, int(err.retry_after_s or 1))
        log.warning("Control endpoint: %s for %s rate limited by the edge; "
                    "answering 429 Retry-After=%ss", what, camera_id, wait)
        return web.json_response({"error": "rate_limited", "retry_after": wait},
                                 status=429, headers={"Retry-After": str(wait)})

    async def _mws_call(action_fn, *args) -> dict:
        """Run one mws_client action with a live MWS SessionToken, re-minting
        exactly once if the server reports the token logged out.

        Mirrors talk.TalkManager._resolve_endpoint: all minting goes through
        mint_if_stale under the SAME shared login_lock, so a control request
        racing a website re-login (or another talk) collapses into a single mint
        rather than each walking the SSO bootstrap. Returns the action's parsed
        result; the caller decides what a not-ok result means, since that differs
        per route. Only call this after _mobile_session() returned non-None."""
        mobile = _mobile_session()
        if mobile.session_token is None:
            await mint_if_stale(mobile, login_lock)

        result = await _mws_post(action_fn, mobile, *args)
        # Read through _mws_token_status, not result["token_status"]: the JSON
        # actions go through _parse_json_ack, which returns ok/error/data and no
        # token status at all, so the original lookup was always None and this
        # re-mint could never fire. The envelope's own TokenStatus survives inside
        # data (the ack strips only isSuccess/ErrorMessage/DidSeamlesslyLogin), and
        # that is where the helper finds it. A response that genuinely carries no
        # status yields None, which is not TOKEN_STATUS_LOGGED_OUT and so leaves
        # the not-ok result for the caller to interpret rather than blindly
        # re-minting a token the server never complained about.
        if not result.get("ok") and _mws_token_status(result) == TOKEN_STATUS_LOGGED_OUT:
            log.info("Control endpoint: MWS token rejected (logged out), re-minting once")
            mobile.session_token = None
            await mint_if_stale(mobile, login_lock)
            result = await _mws_post(action_fn, mobile, *args)
        return result

    async def _mws_account_fetch(action_fn, what: str, args: tuple = (),
                                 ttl: float = _MWS_ACCOUNT_READ_TTL_S
                                 ) -> tuple[dict | None, float]:
        """Run one cached MWS read; return (result or None, seconds to cache).

        Never raises -- the caller is a best-effort section of the settings
        bundle. The TTL comes back with the result so a rate-limited edge can be
        held longer than an ordinary miss."""
        try:
            result = await _mws_call(action_fn, *args)
        except RateLimited as err:
            # Logged apart from every other failure, and held for the server's own
            # Retry-After: this is Cloudflare's edge limiter (telus_auth.RateLimited),
            # and coming straight back at it is precisely what turns one rate-limit
            # window into a multi-hour outage. Swallowing the Retry-After -- which
            # this route used to do -- meant the next poll re-hammered it 120s later
            # having learned nothing.
            log.warning("Control endpoint: %s rate limited by the edge, "
                        "Retry-After=%ss; holding a negative result that long",
                        what, err.retry_after_s)
            return None, max(ttl, float(err.retry_after_s))
        except Exception:
            log.warning("Control endpoint: %s read failed", what, exc_info=True)
            return None, ttl
        if not result.get("ok"):
            log.info("Control endpoint: %s not ok (%s)", what, _mws_failure_note(result))
            return None, ttl
        return result, ttl

    async def _mws_account_read(action_fn, what: str, args: tuple = (),
                                ttl: float = _MWS_ACCOUNT_READ_TTL_S) -> dict | None:
        """The cached result of one MWS read, or None.

        `what` is the cache key as well as the log label, so a PER-DEVICE read
        passes a key that includes the device id (see _chime_type_choices) while
        the account-wide reads pass the bare action name and share one entry
        across every camera in a sweep.

        A FAILURE is cached exactly like a success. The only caller is a poll, so
        the alternative is every camera in the sweep independently retrying an
        upstream that just failed -- against an account whose rate limits are
        undocumented -- and getting the same answer N times.

        The cache is re-checked after taking the lock so only the first waiter of
        a sweep pays for the round trip; the rest see the value it stored."""
        now = time.monotonic()
        cached = _mws_account_cache.get(what)
        if cached is not None and now < cached[0]:
            return cached[1]

        lock = _mws_account_locks.setdefault(what, asyncio.Lock())
        async with lock:
            cached = _mws_account_cache.get(what)
            if cached is not None and time.monotonic() < cached[0]:
                return cached[1]
            result, hold_s = await _mws_account_fetch(action_fn, what, args, ttl)
            _mws_account_cache[what] = (time.monotonic() + hold_s, result)
            return result

    def _mws_invalidate(what: str) -> None:
        """Drop a cached account-wide read after a write that may have changed it.

        Without this, a caller that writes and then reads its own state back --
        which the privacy switch does on purpose, because an accepted MWS command
        is not proof the camera moved -- could be answered from a record minted
        before the write."""
        _mws_account_cache.pop(what, None)

    async def _mws_camera_record(action_fn, camera_id: str, what: str,
                                 normalize) -> dict | None:
        """Best-effort NORMALISED per-camera record for the settings bundle.

        Never raises. Returns None when talkdown was never constructed, the
        account-wide read failed, or nothing in it matches this camera -- null for
        the whole section, so a caller can tell "no data" from "the camera says
        no". The website settings are the point of that route; an MWS outage must
        not take them down with it.

        `normalize` is _normalize_siren/_normalize_privacy, the same functions the
        siren and privacy gates run, so the shape a caller reads and the shape
        this bridge enforces come from ONE place."""
        if _mobile_session() is None:
            return None
        result = await _mws_account_read(action_fn, what)
        if result is None:
            return None
        entry = _mws_entry_for_device(result, camera_id)
        if entry is None:
            log.debug("Control endpoint: %s carries no record for %s", what, camera_id)
            return None
        return normalize(entry)

    async def _chime_type_choices(camera_id: str) -> list:
        """Labelled chime-type options for one camera, or [] if unavailable.

        The website API publishes `chimeTypeOptions` as bare integers
        (`[-1, 0, 1]`) and leaves naming them to a client-side enum in the portal's
        JavaScript. The mobile API hands over the names instead -- and, more
        usefully, a per-option `disables_indoor_chime` flag saying which value
        means "no wired chime" on THIS model. Asking the server beats hardcoding a
        mapping this project would then have to keep true for every camera model.

        Best-effort: [] when there is no mobile session or the read fails, and the
        caller falls back to the bare integers. Losing the labels must not lose
        the control.

        Cached for _MWS_CHIME_OPTIONS_TTL_S rather than the account-wide 30s: the
        set of chime types a doorbell offers is a property of the hardware, not
        state, so re-reading it every poll would be one upstream round trip per
        camera per cycle to be told the same three options. The CURRENT selection
        is not taken from here -- that comes from the website settings in the same
        response, which the chime write refreshes."""
        if _mobile_session() is None:
            return []
        result = await _mws_account_read(
            get_doorbell_settings, f"GetCurrentDoorbellSettings:{camera_id}",
            args=(camera_id,), ttl=_MWS_CHIME_OPTIONS_TTL_S)
        if result is None:
            return []
        return parse_chime_type_options(result).get("options") or []

    async def _augment_audio(camera_id: str, audio) -> object:
        """Add the labelled chime-type options to an audio section, in place.

        Called on EVERY response that carries an audio object -- the settings
        bundle and both audio write routes -- because the section is a single
        contract and a caller must not get a different shape depending on how it
        asked. Skipping it on the write path is not a cosmetic difference: a
        client that renders a picker from the labels, then writes, then adopts the
        echoed section, would lose the labels and fall back to bare integers,
        invalidating the very options it had just shown. That was a real bug.
        """
        if isinstance(audio, dict) and audio.get("supports_chime_type"):
            audio["chime_type_choices"] = await _chime_type_choices(camera_id)
        return audio

    async def handle_camera_settings(request: web.Request) -> web.Response:
        """Audio, image and video settings for one camera, plus its siren and
        privacy records. Every object carries supports_* flags the caller must
        check before offering a control -- see the module docstring."""
        camera_id, err = _camera_guard(request)
        if err is not None:
            return err

        try:
            # Sequential, not gathered: alarm.com's rate limits are undocumented,
            # so this route keeps its TELUS load to one call at a time rather than
            # firing a three-wide burst per poll. Latency is irrelevant here --
            # callers poll it slowly by design.
            audio = await _fetch_with_relogin(
                telus_session, login_lock, camera_settings.get_audio_settings, camera_id)
            image = await _fetch_with_relogin(
                telus_session, login_lock, camera_settings.get_image_settings, camera_id)
            video = await _fetch_with_relogin(
                telus_session, login_lock, camera_settings.get_video_config, camera_id)
            # One request, two sections: the LED and the notification toggles are
            # the same upstream document, and this runs per camera per poll.
            general = await _fetch_with_relogin(
                telus_session, login_lock, camera_settings.get_general_settings,
                camera_id)
        except RateLimited as err:
            # The same mapping the siren/privacy routes settled on: a 502 here
            # tells a caller "upstream broke, retry", which walks it straight
            # back into the limiter. The MWS halves of this bundle keep their
            # own absorb-and-hold behavior (_mws_account_fetch) -- this covers
            # the four website GETs above, which used to fall into the blanket
            # handler below.
            return _rate_limited(err, "camera settings read", camera_id)
        except Exception:
            log.exception("Control endpoint: camera settings fetch failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        # Folded into `audio` rather than offered as a sibling section: it is part
        # of the chime control's own contract, and a caller that has the options
        # but has to look elsewhere for their names is the situation this exists
        # to remove. Absent/[] means "names unavailable, use chime_type_options".
        await _augment_audio(camera_id, audio)

        return web.json_response({
            "audio": audio,
            "image": image,
            "video": video,
            "led": general.get("led"),
            "notifications": general.get("notifications"),
            # Third section off the SAME generalSettings read, at no extra
            # upstream cost (backlog E39). Read-only -- there is no write route
            # for it, because whether a solar panel is bolted to a camera is not
            # a preference. Expect supports_connected FALSE on every camera that
            # is not a battery+solar model, which is both cameras on the
            # reference account; a caller must create nothing in that case.
            "solar": general.get("solar"),
            "siren": await _mws_camera_record(get_siren_devices, camera_id,
                                              _MWS_SIREN_READ, _normalize_siren),
            "privacy": await _mws_camera_record(get_privacy_mode_info, camera_id,
                                                _MWS_PRIVACY_READ, _normalize_privacy),
        })

    async def handle_diagnostics(request: web.Request) -> web.Response:
        camera_id, err = _camera_guard(request)
        if err is not None:
            return err

        try:
            diagnostics = await _fetch_with_relogin(
                telus_session, login_lock, camera_settings.get_diagnostics, camera_id)
        except RateLimited as err:
            return _rate_limited(err, "diagnostics read", camera_id)
        except Exception:
            log.exception("Control endpoint: diagnostics fetch failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response(diagnostics)

    def _out_of_range(fields: dict, current: dict, camera_id: str) -> web.Response | None:
        """Return a 400 response if a volume field is outside the range the device
        itself reports, else None.

        Refusing beats clamping: silently writing a different value than the caller
        asked for to a live camera hides the mismatch from them. A device that
        publishes no bounds is not second-guessed -- TELUS is then the judge."""
        for name, min_key, max_key in (("volume", "call_volume_min", "call_volume_max"),
                                       ("outdoor_volume", None, "outdoor_tone_volume_max")):
            if name not in fields:
                continue
            low = current.get(min_key) if min_key else 0
            high = current.get(max_key)
            if not isinstance(low, int) or not isinstance(high, int):
                continue
            if not low <= fields[name] <= high:
                log.warning("Control endpoint: %s reports %s range %s..%s; refusing %s",
                            camera_id, name, low, high, fields[name])
                return web.json_response({"error": "malformed"}, status=400)
        return None

    async def _settings_write(request: web.Request, getter, setter, spec: dict,
                              support_flags, augment=None) -> web.Response:
        """Shared body for the four settings writes: validate, refuse anything
        the device reports unsupported, then apply.

        support_flags maps a body field to the supports_* key that gates it.
        The pre-read exists only for that gate -- the read-modify-write a website
        settings PUT needs is camera_settings' job. It costs one extra GET per
        write, which is acceptable because a write is an explicit user action and
        never a background poll.

        camera_settings.CameraSettingsError is caught ahead of the generic handler
        on BOTH upstream calls because it is not an upstream failure at all: its
        docstring says to branch on `.unsupported`, and folding it into the blanket
        `except Exception` reported a device that clearly said "I cannot do this"
        as a 502 telus_error -- the one status that tells a caller to retry. The
        generic handler stays behind it for genuine upstream failures."""
        camera_id, err = _camera_guard(request)
        if err is not None:
            return err

        try:
            body = await request.json()
        except Exception:
            log.warning("Control endpoint: malformed %s body for %s",
                        request.path, camera_id, exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)

        fields = _validate_fields(body, spec)
        if fields is None:
            return web.json_response({"error": "malformed"}, status=400)

        try:
            current = await _fetch_with_relogin(telus_session, login_lock, getter, camera_id)
        except RateLimited as err:
            # Caught ahead of the generic handler on BOTH upstream calls for the
            # same reason CameraSettingsError is: a rate limit is not an upstream
            # failure, and 502 is the one status that tells a caller to retry.
            return _rate_limited(err, "settings pre-read", camera_id)
        except camera_settings.CameraSettingsError as err:
            return _settings_error(err, camera_id, "settings pre-read")
        except Exception:
            log.exception("Control endpoint: settings pre-read failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        unsupported = _unsupported_flags(
            current, sorted({support_flags(name) for name in fields}))
        if unsupported:
            log.info("Control endpoint: %s reports no support for %s",
                     camera_id, ", ".join(unsupported))
            return web.json_response({"error": "unsupported"}, status=422)

        if (err := _out_of_range(fields, current, camera_id)) is not None:
            return err

        try:
            result = await _fetch_with_relogin(
                telus_session, login_lock, _bound(setter, **fields), camera_id)
        except RateLimited as err:
            return _rate_limited(err, setter.__name__, camera_id)
        except camera_settings.CameraSettingsError as err:
            return _settings_error(err, camera_id, setter.__name__)
        except Exception:
            log.exception("Control endpoint: %s failed for %s", setter.__name__, camera_id)
            return web.json_response({"error": "telus_error"}, status=502)

        if augment is not None:
            result = await augment(camera_id, result)
        return web.json_response(result)

    async def handle_set_volume(request: web.Request) -> web.Response:
        """Set the camera's talk/call volume -- the volume talkdown plays at."""
        return await _settings_write(
            request, camera_settings.get_audio_settings, camera_settings.set_call_volume,
            {"volume": int}, lambda _name: "supports_call_volume",
            augment=_augment_audio)

    async def handle_set_chime(request: web.Request) -> web.Response:
        """Set the doorbell's own indoor chime / outdoor tone."""
        return await _settings_write(
            request, camera_settings.get_audio_settings, camera_settings.set_chime,
            _CHIME_FIELDS, _CHIME_SUPPORT.__getitem__, augment=_augment_audio)

    async def handle_set_led(request: web.Request) -> web.Response:
        """Turn the camera's status LED on/off, or set its brightness."""
        return await _settings_write(
            request, camera_settings.get_led_settings, camera_settings.set_led_settings,
            _LED_FIELDS, lambda name: f"supports_{name}")

    async def handle_set_notifications(request: web.Request) -> web.Response:
        """Turn TELUS-side push notifications on/off for one camera."""
        return await _settings_write(
            request, camera_settings.get_notification_settings,
            camera_settings.set_notification_settings,
            _NOTIFICATION_FIELDS, lambda name: f"supports_{name}")

    async def handle_set_image(request: web.Request) -> web.Response:
        """Set picture/IR fields on one camera (see _IMAGE_FIELDS for the set)."""
        return await _settings_write(
            request, camera_settings.get_image_settings, camera_settings.set_image_settings,
            _IMAGE_FIELDS, lambda name: f"supports_{name}")

    async def handle_set_video(request: web.Request) -> web.Response:
        """Set clip recording fields on one camera (see _VIDEO_FIELDS)."""
        return await _settings_write(
            request, camera_settings.get_video_config, camera_settings.set_video_config,
            _VIDEO_FIELDS, lambda name: f"supports_{name}")

    # ---- siren / privacy (mobile MWS API) -----------------------------------

    async def handle_siren(request: web.Request) -> web.Response:
        """Start or stop this camera's siren.

        A start is gated on the account's canManuallyTriggerSiren permission and
        never fired blind -- it makes a real siren sound in someone's home. A
        stop is not gated at all, deliberately."""
        camera_id, err = _camera_guard(request)
        if err is not None:
            return err

        try:
            action = (await request.json())["action"]
        except Exception:
            log.warning("Control endpoint: malformed siren request for %s", camera_id,
                        exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        if action not in ("start", "stop"):
            return web.json_response({"error": "malformed"}, status=400)

        if _mobile_session() is None:
            return web.json_response({"error": "mws_unavailable"}, status=503)

        if action == "start":
            # canManuallyTriggerSiren is a per-camera PERMISSION (the app carries
            # a dedicated checker for it), not "this device has a siren", so a
            # device advertising one is not this account being allowed to fire it
            # -- docs/mobile-api-findings.md. No matching entry means the device
            # is absent from the list of siren-capable devices, i.e. no siren.
            # Uncached, unlike the settings bundle's copy of this same read: a
            # permission check standing between a caller and a siren in someone's
            # home is worth one guaranteed-fresh round trip, and a start is an
            # explicit user action, never a poll, so it adds no steady-state load.
            try:
                info = await _mws_call(get_siren_devices)
            except RateLimited as err:
                return _rate_limited(err, "siren capability read", camera_id)
            except Exception:
                log.exception("Control endpoint: siren capability read failed for %s", camera_id)
                return web.json_response({"error": "telus_error"}, status=502)
            if not info.get("ok"):
                log.warning("Control endpoint: %s not ok for %s (%s)",
                            _MWS_SIREN_READ, camera_id, _mws_failure_note(info))
                return web.json_response({"error": "telus_error"}, status=502)
            entry = _mws_entry_for_device(info, camera_id)
            # Through the same _normalize_siren the settings bundle publishes, so
            # what Home Assistant reads as can_manually_trigger and what this gate
            # enforces cannot disagree. It coerces the flag rather than testing it
            # for truthiness: the value arrives as the STRING "false", and
            # `not "false"` is False -- so the bare truthiness test this replaces
            # would have FIRED the siren on a camera that explicitly refused it.
            siren = _normalize_siren(entry) if entry is not None else None
            if siren is None or not siren["can_manually_trigger"]:
                log.info("Control endpoint: %s may not be manually sirened", camera_id)
                return web.json_response({"error": "unsupported"}, status=422)

        try:
            result = await _mws_call(start_siren if action == "start" else stop_siren,
                                     camera_id)
        except RateLimited as err:
            return _rate_limited(err, f"siren {action}", camera_id)
        except Exception:
            log.exception("Control endpoint: siren %s failed for %s", action, camera_id)
            return web.json_response({"error": "telus_error"}, status=502)
        if not result.get("ok"):
            log.warning("Control endpoint: siren %s rejected for %s (%s)",
                        action, camera_id, _mws_failure_note(result))
            return web.json_response({"error": "telus_error"}, status=502)

        # The cached siren record now describes a state this command changed.
        _mws_invalidate(_MWS_SIREN_READ)
        log.info("Control endpoint: siren %s applied to %s", action, camera_id)
        return web.json_response({"status": "applied"})

    async def handle_privacy(request: web.Request) -> web.Response:
        """Turn this camera's privacy mode on or off."""
        camera_id, err = _camera_guard(request)
        if err is not None:
            return err

        try:
            active = (await request.json())["active"]
        except Exception:
            log.warning("Control endpoint: malformed privacy request for %s", camera_id,
                        exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        if not isinstance(active, bool):
            return web.json_response({"error": "malformed"}, status=400)

        if _mobile_session() is None:
            return web.json_response({"error": "mws_unavailable"}, status=503)

        # Uncached for the same reason the siren gate is: this decides whether a
        # write reaches a camera, and it runs only when a caller asked for one.
        try:
            info = await _mws_call(get_privacy_mode_info)
        except RateLimited as err:
            return _rate_limited(err, "privacy support read", camera_id)
        except Exception:
            log.exception("Control endpoint: privacy support read failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)
        if not info.get("ok"):
            log.warning("Control endpoint: %s not ok for %s (%s)",
                        _MWS_PRIVACY_READ, camera_id, _mws_failure_note(info))
            return web.json_response({"error": "telus_error"}, status=502)

        entry = _mws_entry_for_device(info, camera_id)
        # Only an explicit false refuses. Unlike the siren list, this action
        # returns a privacy-state object rather than an enumeration of capable
        # devices, so failing to match an entry is a response-shape mismatch and
        # not evidence that the camera lacks the feature. SupportsPrivacyMode was
        # false for every camera on the account surveyed, so this gate is the one
        # that will actually fire (docs/mobile-api-findings.md).
        #
        # _normalize_privacy carries that asymmetry (an absent flag reports
        # supported True; only an explicit false reports False), so the gate is a
        # plain read of the same "supported" key the settings bundle publishes and
        # the switch reads -- one definition of "may this camera do it", not two.
        # The entry-is-None case stays here rather than inside the normaliser
        # because there is no record to normalise: no match means allow through.
        privacy = _normalize_privacy(entry) if entry is not None else None
        if privacy is not None and not privacy["supported"]:
            log.info("Control endpoint: %s reports no privacy-mode support", camera_id)
            return web.json_response({"error": "unsupported"}, status=422)

        try:
            result = await _mws_call(set_privacy_mode, camera_id, active)
        except RateLimited as err:
            return _rate_limited(err, "privacy mode write", camera_id)
        except Exception:
            log.exception("Control endpoint: privacy mode write failed for %s", camera_id)
            return web.json_response({"error": "telus_error"}, status=502)
        if not result.get("ok"):
            log.warning("Control endpoint: privacy mode rejected for %s (%s)",
                        camera_id, _mws_failure_note(result))
            return web.json_response({"error": "telus_error"}, status=502)

        # The cached privacy record is now stale by definition, and the caller is
        # expected to read this state straight back (an accepted MWS command is not
        # proof the camera moved), so it must not be answered from before the write.
        _mws_invalidate(_MWS_PRIVACY_READ)
        log.info("Control endpoint: privacy mode %s for %s",
                 "on" if active else "off", camera_id)
        return web.json_response({"status": "applied"})

    # ---- bluetooth arming devices (website API) -----------------------------

    async def _partition_choices() -> list:
        """The account's partitions as [{"value", "label"}], or [] if unreadable.

        `value` is the partition NUMBER as a string, because that is what an
        arming device's enrolledPartitionId holds ("1") -- NOT the "<unit>-127"
        alarm.com device id the same partition record also carries and the alarm
        panel entity uses as its unique id. Mixing the two would build a picker
        whose every option is refused by the write route.

        `label` is the partition's own description, passed through as text: it is
        whatever the account holder called it and may contain a name or a curly
        apostrophe, so it is never parsed, sliced or used to build an identifier.

        Best-effort, like _chime_type_choices: [] on any failure, logged at
        warning. That is the right degradation for the READ (a caller shows no
        picker rather than an empty one), and deliberately NOT what the write
        route does with the same failure -- see handle_set_enrolled_partition.
        """
        try:
            found = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, _unused: partitions_mod.get_partitions(telus), "")
        except Exception:
            log.warning("Control endpoint: partition list unavailable; arming "
                        "devices will carry no partition choices", exc_info=True)
            return []
        choices = []
        for partition in found:
            number = partition.get("partition_id")
            if number is None:
                continue
            choices.append({"value": str(number),
                            "label": partition.get("description") or str(number)})
        return choices

    async def _with_partition_choices(records: list) -> list:
        """Stamp the account's partition choices onto every arming device record.

        Called on EVERY response carrying an arming device, the list route and
        both write routes alike, because the record is a single contract and a
        caller must not get a different shape depending on how it asked. That is
        not cosmetic: the HA side folds a write's echoed record straight back
        into what its entities read, so a reply missing the choices would empty
        the picker it had just been used from -- the same bug _augment_audio
        exists to prevent on the camera settings sections.

        One upstream read for the whole batch, and none at all when there are no
        records to stamp.
        """
        if not records:
            return records
        choices = await _partition_choices()
        for record in records:
            record["enrolled_partition_choices"] = choices
        return records

    async def handle_arming_devices(request: web.Request) -> web.Response:
        """List the phones paired to the panel for bluetooth disarming.

        No camera is involved, so there is no _camera_guard here and the
        _rate_limited/_settings_error "camera_id" argument is filled with the
        route name -- those two helpers only ever put it in a log line and this
        keeps one implementation of each rather than a second copy that drifts.

        An ArmingDeviceError is deliberately NOT routed through _settings_error
        here: that helper maps a non-`unsupported` error to 400 "the caller asked
        for something wrong", and this route takes no caller input at all, so
        anything that fails is upstream and 502 is the truthful answer.
        """
        if (err := _check_auth(request)) is not None:
            return err
        try:
            devices = await _fetch_with_relogin(
                telus_session, login_lock, arming_devices.get_arming_devices)
        except RateLimited as err:
            return _rate_limited(err, "arming device list", "arming-devices")
        except Exception:
            log.exception("Control endpoint: arming device list failed")
            return web.json_response({"error": "telus_error"}, status=502)
        return web.json_response(
            {"devices": await _with_partition_choices(devices)})

    async def handle_set_disarming(request: web.Request) -> web.Response:
        """Turn bluetooth disarming on or off for one paired phone.

        This decides whether that phone walking up to the house disarms the
        alarm, so it refuses in three places before it writes, and the order
        matters: the cheapest, most certain refusals run first so a bad request
        never costs a round trip against an account that can be rate-limited.

        The pre-read is NOT just the capability gate the settings writes use --
        it is also the membership check. Without it a typo'd id would be PUT
        straight at an arbitrary device on a live panel, and this endpoint's PUT
        replaces the whole record.
        """
        if (err := _check_auth(request)) is not None:
            return err
        device_id = request.match_info["device_id"]

        try:
            enabled = (await request.json())["enabled"]
        except Exception:
            log.warning("Control endpoint: malformed disarming request for %s",
                        device_id, exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        # A real JSON boolean, never a coercion: bool("false") is True, and this
        # is the last field in this API to be generous with.
        if not isinstance(enabled, bool):
            return web.json_response({"error": "malformed"}, status=400)

        try:
            devices = await _fetch_with_relogin(
                telus_session, login_lock, arming_devices.get_arming_devices)
        except RateLimited as err:
            return _rate_limited(err, "arming device pre-read", device_id)
        except Exception:
            log.exception("Control endpoint: arming device pre-read failed for %s",
                          device_id)
            return web.json_response({"error": "telus_error"}, status=502)

        current = next((dev for dev in devices if dev["id"] == device_id), None)
        if current is None:
            log.warning("Control endpoint: no bluetooth arming device %s on this system",
                        device_id)
            return web.json_response({"error": "unknown_device"}, status=404)
        if not current["supports_disarming_toggle"]:
            log.info("Control endpoint: %s reports no bluetooth disarming toggle",
                     device_id)
            return web.json_response({"error": "unsupported"}, status=422)

        try:
            result = await _fetch_with_relogin(
                telus_session, login_lock,
                _bound(arming_devices.set_disarming_enabled, enabled=enabled),
                device_id)
        except RateLimited as err:
            return _rate_limited(err, "bluetooth disarming write", device_id)
        except arming_devices.ArmingDeviceError as err:
            # Same contract as CameraSettingsError, so the same mapping: 422 when
            # the record itself says it cannot, 400 when the caller asked wrong.
            # Folding it into the generic handler would report an explicit "I
            # cannot do this" as a 502, the one status that says "retry".
            return _settings_error(err, device_id, "set_disarming_enabled")
        except Exception:
            log.exception("Control endpoint: bluetooth disarming write failed for %s",
                          device_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response((await _with_partition_choices([result]))[0])

    async def handle_set_enrolled_partition(request: web.Request) -> web.Response:
        """Move one paired phone to a different partition (backlog E43).

        Which partition a phone disarms is the same class of setting as whether
        it may disarm at all, so this follows handle_set_disarming exactly --
        cheapest and most certain refusals first, so a bad request never costs a
        round trip against an account that can be rate-limited -- with one extra
        gate that the disarming toggle has no equivalent of.

        That gate is the partition itself, and it FAILS CLOSED. The requested
        partition must appear in the account's own partition list, and if that
        list cannot be read the write does not happen. Refusing is the whole
        point: a PUT here replaces the record, `enrolledPartitionId` is a bare
        panel-side number ("1", "2") with no way to tell a valid one from a
        typo by looking at it, and writing an id nothing confirmed exists to the
        field that decides which partition a phone opens is exactly the mistake
        this endpoint should be incapable of making.
        """
        if (err := _check_auth(request)) is not None:
            return err
        device_id = request.match_info["device_id"]

        try:
            partition_id = (await request.json())["partition_id"]
        except Exception:
            log.warning("Control endpoint: malformed enrolled-partition request "
                        "for %s", device_id, exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        # A non-empty JSON string, never a coercion: this is an identifier that
        # goes back onto a whole-record PUT exactly as it arrived, and str(1)
        # would change its type on the wire without anyone noticing.
        if not isinstance(partition_id, str) or not partition_id.strip():
            return web.json_response({"error": "malformed"}, status=400)

        try:
            devices = await _fetch_with_relogin(
                telus_session, login_lock, arming_devices.get_arming_devices)
        except RateLimited as err:
            return _rate_limited(err, "arming device pre-read", device_id)
        except Exception:
            log.exception("Control endpoint: arming device pre-read failed for %s",
                          device_id)
            return web.json_response({"error": "telus_error"}, status=502)

        current = next((dev for dev in devices if dev["id"] == device_id), None)
        if current is None:
            log.warning("Control endpoint: no bluetooth arming device %s on this system",
                        device_id)
            return web.json_response({"error": "unknown_device"}, status=404)
        if not current.get("supports_enrolled_partition"):
            log.info("Control endpoint: %s reports no writable enrolled partition",
                     device_id)
            return web.json_response({"error": "unsupported"}, status=422)

        # Not _partition_choices(): that one degrades an unreadable partition
        # list to [], which is right for a read and wrong here. An empty list
        # from a failed read and an empty list from an account with no partitions
        # are indistinguishable, and both must stop the write rather than let it
        # through unvalidated.
        try:
            offered = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, _unused: partitions_mod.get_partitions(telus), "")
        except RateLimited as err:
            return _rate_limited(err, "partition list", device_id)
        except Exception:
            log.exception("Control endpoint: partition list failed; refusing to "
                          "enroll %s on an unverified partition", device_id)
            return web.json_response({"error": "telus_error"}, status=502)

        numbers = {str(p.get("partition_id")) for p in offered
                   if p.get("partition_id") is not None}
        if partition_id not in numbers:
            log.warning("Control endpoint: partition %r is not one of this "
                        "account's (%s); refusing to enroll %s on it",
                        partition_id, ", ".join(sorted(numbers)) or "none", device_id)
            return web.json_response({"error": "malformed"}, status=400)

        try:
            result = await _fetch_with_relogin(
                telus_session, login_lock,
                _bound(arming_devices.set_enrolled_partition,
                       partition_id=partition_id),
                device_id)
        except RateLimited as err:
            return _rate_limited(err, "enrolled partition write", device_id)
        except arming_devices.ArmingDeviceError as err:
            return _settings_error(err, device_id, "set_enrolled_partition")
        except Exception:
            log.exception("Control endpoint: enrolled partition write failed for %s",
                          device_id)
            return web.json_response({"error": "telus_error"}, status=502)

        return web.json_response((await _with_partition_choices([result]))[0])

    # ---- panel-level bluetooth disarming (website API) ----------------------

    async def handle_bluetooth_disarming(request: web.Request) -> web.Response:
        """The system-wide Bluetooth disarming settings (backlog E41).

        A pure read. Like handle_arming_devices it takes no caller input, so an
        ArmingDeviceError is NOT routed through _settings_error -- that helper's
        non-`unsupported` branch is 400 "you asked for something wrong", and
        there is nothing here for a caller to have asked wrong.
        """
        if (err := _check_auth(request)) is not None:
            return err
        try:
            settings = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, _unused: arming_devices
                .get_bluetooth_disarming_settings(telus), "")
        except RateLimited as err:
            return _rate_limited(err, "bluetooth disarming read", "bluetooth-disarming")
        except Exception:
            log.exception("Control endpoint: bluetooth disarming read failed")
            return web.json_response({"error": "telus_error"}, status=502)
        return web.json_response(settings)

    async def handle_set_bluetooth_disarming(request: web.Request) -> web.Response:
        """Change the system-wide Bluetooth disarming settings (backlog E41).

        **The write shape this reaches is INFERRED, not captured** -- see
        arming_devices.set_bluetooth_disarming_settings. That is a reason to be
        stricter here, not looser, so the body is validated to the letter:
        _validate_fields refuses an unknown key, a wrong type, and (its whole
        reason for existing) a bool where an int is wanted, since bool is an int
        subclass in Python and {"timeout": true} would otherwise be written to a
        real panel as 1.

        No pre-read gate, unlike handle_set_disarming. That route pre-reads for
        MEMBERSHIP -- to keep a typo'd device id from reaching a PUT at an
        arbitrary device -- and there is no membership question here: the
        document is the session's own system, addressed by its Context-Id. The
        setter's own read-modify-write carries the capability gate, and adding a
        second read would double this route's upstream cost to check nothing.
        """
        if (err := _check_auth(request)) is not None:
            return err
        try:
            body = await request.json()
        except Exception:
            log.warning("Control endpoint: malformed bluetooth disarming body",
                        exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)

        fields = _validate_fields(body, _BLUETOOTH_DISARMING_FIELDS)
        if fields is None:
            return web.json_response({"error": "malformed"}, status=400)

        try:
            settings = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, _unused: arming_devices
                .set_bluetooth_disarming_settings(telus, **fields), "")
        except RateLimited as err:
            return _rate_limited(err, "bluetooth disarming write", "bluetooth-disarming")
        except arming_devices.ArmingDeviceError as err:
            # Same contract as CameraSettingsError, so the same mapping: 422 when
            # the panel reports no such setting, 400 when the caller asked wrong.
            return _settings_error(err, "bluetooth-disarming",
                                   "set_bluetooth_disarming_settings")
        except Exception:
            log.exception("Control endpoint: bluetooth disarming write failed")
            return web.json_response({"error": "telus_error"}, status=502)

        log.info("Control endpoint: panel bluetooth disarming settings applied (%s)",
                 ", ".join(sorted(fields)))
        return web.json_response(settings)

    async def handle_partitions(request: web.Request) -> web.Response:
        """List the alarm partitions and the two flags that gate a bypass."""
        if (err := _check_auth(request)) is not None:
            return err
        try:
            found = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, _unused: partitions_mod.get_partitions(telus), "")
        except RateLimited as err:
            return _rate_limited(err, "partition list", "partitions")
        except Exception:
            log.exception("Control endpoint: partition list failed")
            return web.json_response({"error": "telus_error"}, status=502)
        return web.json_response({"partitions": found})

    async def handle_bypass(request: web.Request) -> web.Response:
        """Bypass and/or un-bypass named sensors on one partition.

        A bypassed sensor stops triggering the alarm until it is un-bypassed, so
        this refuses everything it is not sure about: the body must be two lists
        of strings, at least one non-empty, and the partition itself must report
        that it permits the operation right now (partitions.bypass_sensors
        re-reads it before writing)."""
        if (err := _check_auth(request)) is not None:
            return err
        partition_id = request.match_info["partition_id"]

        try:
            body = await request.json()
        except Exception:
            log.warning("Control endpoint: malformed bypass body for %s",
                        partition_id, exc_info=True)
            return web.json_response({"error": "malformed"}, status=400)
        if not isinstance(body, dict):
            return web.json_response({"error": "malformed"}, status=400)
        wanted = {}
        for key in ("bypass", "unbypass"):
            value = body.get(key, [])
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                return web.json_response({"error": "malformed"}, status=400)
            wanted[key] = value
        if not wanted["bypass"] and not wanted["unbypass"]:
            return web.json_response({"error": "malformed"}, status=400)

        try:
            partition = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, pid: partitions_mod.bypass_sensors(
                    telus, pid, bypass=wanted["bypass"], unbypass=wanted["unbypass"]),
                partition_id)
        except partitions_mod.PartitionError as err:
            return _settings_error(err, partition_id, "bypass")
        except RateLimited as err:
            return _rate_limited(err, "bypass", partition_id)
        except Exception:
            log.exception("Control endpoint: bypass failed for %s", partition_id)
            return web.json_response({"error": "telus_error"}, status=502)

        log.info("Control endpoint: bypass applied on %s (bypass=%s unbypass=%s)",
                 partition_id, wanted["bypass"], wanted["unbypass"])
        return web.json_response({"status": "applied", "partition": partition})

    async def handle_unpair_arming_device(request: web.Request) -> web.Response:
        """Unpair one Bluetooth arming device. NOT undoable from this side.

        DELETE rather than POST, because that is what it is. Re-pairing needs
        physical access to the phone and the panel, so an unknown device id is a
        404 rather than a delete aimed at whatever that id addresses."""
        if (err := _check_auth(request)) is not None:
            return err
        device_id = request.match_info["device_id"]
        try:
            result = await _fetch_with_relogin(
                telus_session, login_lock,
                lambda telus, did: arming_devices.unpair_arming_device(telus, did),
                device_id)
        except arming_devices.ArmingDeviceError as err:
            # An id the catalog does not know is a 404, not the 400 the shared
            # mapper would give: the caller named a device, and the answer is
            # that it does not exist.
            if err.field == "device_id":
                log.info("Control endpoint: unpair refused, %s unknown", device_id)
                return web.json_response({"error": "unknown_device"}, status=404)
            return _settings_error(err, device_id, "unpair")
        except RateLimited as err:
            return _rate_limited(err, "unpair", device_id)
        except Exception:
            log.exception("Control endpoint: unpair failed for %s", device_id)
            return web.json_response({"error": "telus_error"}, status=502)

        log.info("Control endpoint: unpaired arming device %s", device_id)
        return web.json_response({"status": "unpaired", **result})

    app = web.Application()
    app[APP_CURRENT_SECRET] = current_secret

    async def handle_talk_stop(request: web.Request) -> web.Response:
        """Stop the talk currently playing on this camera.

        The stream ends at the next chunk boundary (<=40ms) and the original
        POST returns its partial stats with "stopped": true. Idempotent-ish: with
        nothing in flight this is a 404 rather than an error condition.
        """
        if (err := _check_auth(request)) is not None:
            return err
        if not c.TALKDOWN_ENABLED or talk_manager is None:
            return web.json_response({"error": "talkdown_disabled"}, status=403)

        camera_id = request.match_info["camera_id"]
        if camera_id not in camera_registry:
            return web.json_response({"error": "unknown_camera"}, status=404)
        if not talk_manager.stop(camera_id):
            return web.json_response({"error": "no_talk_in_progress"}, status=404)
        return web.json_response({"status": "stopping"})

    app.router.add_post("/control/credentials", handle_credentials)
    app.router.add_get("/control/health", handle_health)
    app.router.add_get("/control/cameras", handle_cameras)
    app.router.add_get("/control/status", handle_status)
    app.router.add_get("/control/metrics", handle_metrics)
    app.router.add_get("/control/cameras/{camera_id}/snapshot", handle_snapshot)
    app.router.add_post("/control/cameras/{camera_id}/talk", handle_talk)
    app.router.add_post("/control/cameras/{camera_id}/talk/stop", handle_talk_stop)
    app.router.add_get("/control/events", handle_events)
    app.router.add_get("/control/clip/{video_event_id}", handle_clip)
    app.router.add_get("/control/automations", handle_automations)
    app.router.add_get("/control/history", handle_history)
    app.router.add_post("/control/automations/{rule_id}/pause", handle_pause_rule)
    app.router.add_post("/control/automations/{rule_id}/resume", handle_resume_rule)
    app.router.add_post("/control/cameras/by-path/{path_name}/activate", handle_activate_by_path)
    app.router.add_post("/control/cameras/by-path/{path_name}/deactivate", handle_deactivate_by_path)
    app.router.add_get("/control/cameras/{camera_id}/settings", handle_camera_settings)
    app.router.add_get("/control/cameras/{camera_id}/diagnostics", handle_diagnostics)
    app.router.add_post("/control/cameras/{camera_id}/settings/volume", handle_set_volume)
    app.router.add_post("/control/cameras/{camera_id}/settings/chime", handle_set_chime)
    app.router.add_post("/control/cameras/{camera_id}/settings/led", handle_set_led)
    app.router.add_post("/control/cameras/{camera_id}/settings/notifications",
                        handle_set_notifications)
    app.router.add_post("/control/cameras/{camera_id}/settings/image", handle_set_image)
    app.router.add_post("/control/cameras/{camera_id}/settings/video", handle_set_video)
    app.router.add_post("/control/cameras/{camera_id}/siren", handle_siren)
    app.router.add_post("/control/cameras/{camera_id}/privacy", handle_privacy)
    app.router.add_get("/control/partitions", handle_partitions)
    app.router.add_post("/control/partitions/{partition_id}/bypass", handle_bypass)
    app.router.add_delete("/control/arming-devices/{device_id}",
                          handle_unpair_arming_device)
    app.router.add_get("/control/arming-devices", handle_arming_devices)
    app.router.add_post("/control/arming-devices/{device_id}/disarming",
                        handle_set_disarming)
    app.router.add_post("/control/arming-devices/{device_id}/enrolled-partition",
                        handle_set_enrolled_partition)
    app.router.add_get("/control/bluetooth-disarming", handle_bluetooth_disarming)
    app.router.add_post("/control/bluetooth-disarming", handle_set_bluetooth_disarming)
    return app


async def start_control_server(
    telus_session, login_lock, camera_registry: dict, camera_status: dict, lifecycle,
    talk_manager=None,
) -> web.AppRunner:
    app = create_app(telus_session, login_lock, camera_registry, camera_status, lifecycle,
                     talk_manager)
    # Access logging is opt-in (config.CONTROL_ACCESS_LOG). Every request here
    # is loopback polling from Home Assistant, so the one-INFO-line-per-request
    # "aiohttp.access" output is routine noise that ages real warnings out of
    # the container's capped log-rotation window; failures still log through
    # the route handlers themselves. access_log=None is aiohttp's supported way
    # to disable it -- the kwarg is forwarded to the request handler factory.
    runner = (web.AppRunner(app) if c.CONTROL_ACCESS_LOG
              else web.AppRunner(app, access_log=None))
    await runner.setup()
    site = web.TCPSite(runner, c.CONTROL_HOST, c.CONTROL_PORT)
    await site.start()
    log.info("Control endpoint listening on %s:%d", c.CONTROL_HOST, c.CONTROL_PORT)
    # Stashed in the app's own dict-style storage (confirmed live: web.AppRunner
    # itself rejects arbitrary attribute assignment -- AttributeError, no
    # __slots__ escape hatch) rather than a bare fire-and-forget ensure_future,
    # so main.py can cancel it in its shutdown path and asyncio has a live
    # reference to it for its full (month-plus) lifetime.
    app[APP_SECRET_ROTATION_TASK] = asyncio.ensure_future(
        _rotate_secret_periodically(app[APP_CURRENT_SECRET]))
    return runner
