# Talkdown deployment & validation

How to deploy doorbell two-way audio and verify it end to end. Every step here
touches the live system (it rebuilds and restarts containers, opens a real
stream to the doorbell, and plays audio at someone's front door) so read the
whole file before running any of it.

The doorbell (ADC-VDB750) uses the **full-duplex HTTP-PUT** talk path; the
webrtc/srtp modules do not apply to it and remain a fallback for other ADC
cameras. The mechanism is documented in
[talkdown-fullduplex-protocol.md](talkdown-fullduplex-protocol.md).

Two deployments have to move together. The bridge builds its image from the
repository working tree, while Home Assistant runs a **separate copy** of the
integration at `~/homeassistant/config/custom_components/telus_smarthome_alarmdotcom`, so
the HA side needs an explicit sync: a bridge rebuild alone leaves the two
halves mismatched.

The talk endpoint is **enabled by default** (`TALKDOWN_ENABLED`). It plays only
what a caller asks it to, so deploying the code does not make anything speak on
its own; set `TALKDOWN_ENABLED=false` to disable the endpoint entirely.

## 1. Deploy the bridge

The bridge image builds from the repository working tree, so confirm it is on the
commit you mean to deploy and is clean:

    ssh <ha-host> 'cd <repo> && git status --short && git rev-parse --abbrev-ref HEAD'

Rebuild + recreate the container:

    ssh <ha-host> 'cd <repo>/camera-bridge && docker compose up -d --build'

Confirm it came up clean:

    ssh <ha-host> 'sudo docker logs --tail 50 telus-cam-bridge'
    ssh <ha-host> 'curl -s http://127.0.0.1:8199/control/health'   # -> {"status": "ok"}

At this point the talk route is live: talkdown is enabled by default. A
`403 talkdown_disabled` here means `TALKDOWN_ENABLED` was set `false`; see
step 3.

## 2. Deploy the HA integration (sync the separate copy)

    ssh <ha-host> 'rsync -a --delete \
      <repo>/custom_components/telus_smarthome_alarmdotcom/ \
      ~/homeassistant/config/custom_components/telus_smarthome_alarmdotcom/'

Restart Home Assistant so `services.yaml` and `telus_smarthome_alarmdotcom.speak` register:

    ssh <ha-host> 'sudo docker restart homeassistant'

Verify: Developer Tools > Actions > search `telus_smarthome_alarmdotcom.speak`.

## 3. (Optional) turn talkdown off

Talkdown is enabled by default, so there is nothing to do here to use it; skip
to step 4. If you would rather the endpoint refuse all talk requests, set the
flag false (non-secret; it belongs in `.env`, not `secrets.env`) and recreate:

    ssh <ha-host> 'cd <repo>/camera-bridge && \
      grep -q "^TALKDOWN_ENABLED=" .env && \
      sed -i "s/^TALKDOWN_ENABLED=.*/TALKDOWN_ENABLED=false/" .env || \
      echo "TALKDOWN_ENABLED=false" >> .env'
    ssh <ha-host> 'cd <repo>/camera-bridge && docker compose up -d'

The route then answers `403 talkdown_disabled`.

## 4. Manual end-to-end test (someone should be able to hear the doorbell)

The endpoint is **modular**: it plays whatever audio you point it at (TTS clip,
WAV, or an MP3 music file). ffmpeg transcodes any input to the doorbell's
µ-law/8k/mono. Test the raw endpoint directly on the host:

    SECRET=$(sudo cat <control_secret_path>)   # see config.CONTROL_SECRET_PATH
    # A short WAV/TTS clip:
    curl -s -X POST http://127.0.0.1:8199/control/cameras/<unit-id>-<did>/talk \
      -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
      -d '{"audio_url":"http://127.0.0.1:8123/local/test.wav"}'
    # A music file (expect lo-fi 8 kHz mono, telephone grade, not hi-fi):
    curl -s -X POST http://127.0.0.1:8199/control/cameras/<unit-id>-<did>/talk \
      -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
      -d '{"audio_url":"http://127.0.0.1:8123/local/music.mp3"}'
    # -> {"path":"full_duplex","chunks":N,"bytes":M}

Or via HA Developer Tools > Actions (`telus_smarthome_alarmdotcom.speak`, with a TTS
`message:` or a media URL). Expected: audio plays at the doorbell within a few
seconds; the bridge logs `talkdown: full-duplex armed ...` then
`talkdown: streamed N chunks / M bytes mu-law to <host>`.

Troubleshooting quick map (full contract in control_server.py docstring):
- 403 talkdown_disabled  -> TALKDOWN_ENABLED was set false (step 3).
- 409 talk_in_progress   -> a talk is already running for that camera; wait.
- 422 no_upstream_audio  -> the doorbell returned no usable PutAudioStream URL /
                            password (susa false, or a stale stream); retry.
- 502 telus_error        -> MWS auth / GetDoorbellCameraList / StartFullDuplex /
                            PUT failed; check bridge logs (secrets redacted).
- 504 talk_timeout       -> exceeded TALK_SESSION_TIMEOUT_S.

## 5. (Optional) arm an automation

The talk endpoint is a generic "play this audio at the door" primitive; wire it
to whatever you like. `docs/talkdown-automation.yaml` is **one example** (a 10 s
Frigate-zone dwell deterrent gated on a `binary_sensor` so it never
greets known occupants). It is only a sample: do not treat its phrase/trigger as
part of the feature. If you use it, add a co-located `front_door_talk` zone
(same polygon as your existing door zone, `loitering_time: 10`) under
`cameras.<doorbell>.zones` in your Frigate `config.yml`, restart Frigate, add
the automation, and enable it only after the manual test in step 4 plays cleanly.

---

## Rollback

- Disable without redeploying: set `TALKDOWN_ENABLED=false` in
  `camera-bridge/.env` and `docker compose up -d`; the route goes inert (403).
- Full revert: check out the previous branch/commit, rebuild the bridge, re-sync
  and restart HA, remove any `front_door_talk` zone + automation you added.
