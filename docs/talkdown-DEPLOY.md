# Talkdown deployment & validation (run LATER, by a human, with explicit approval)

Manual steps to enable doorbell two-way audio. **Nothing here has been run
against the live doorbell.** Every step touches the live system (rebuilds/
restarts containers, opens a real MWS stream, plays audio at the door), so read
the whole file first and run the steps in order.

The protocol is confirmed and the transport is built + unit-tested — see
`docs/talkdown-fullduplex-protocol.md` for the full mechanism. The doorbell
(ADC-VDB750) uses the **full-duplex HTTP-PUT** talk path; the webrtc/srtp modules
do **not** apply to it (they stay as a fallback for other ADC cameras).

All wiring lives on branch `feature/doorbell-talkdown` in
`~/telus-smart-home`. The bridge builds its image from that working tree;
Home Assistant runs a **separate copy** at
`~/homeassistant/config/custom_components/telus_smarthome`, so the HA side needs
an explicit sync. The talk endpoint is **off by default** (`TALKDOWN_ENABLED`),
so deploying the code arms nothing until step 3.

Credentials: none are entered here. The bridge mints its own MWS SessionToken
from the TELUS credentials that already live in Home Assistant (see repo
README) — the same website session the bridge already uses for video.

Container names (live): `telus-cam-bridge`, `homeassistant`, `frigate`.

---

## 1. Deploy the bridge (build from the branch)

The bridge image builds from `~/telus-smart-home/camera-bridge`. Ensure
the branch is checked out (or merged to your deploy branch) first:

    ssh <ha-host> 'cd ~/telus-smart-home && git status --short && git rev-parse --abbrev-ref HEAD'

Rebuild + recreate the container:

    ssh <ha-host> 'cd ~/telus-smart-home/camera-bridge && docker compose up -d --build'

Confirm it came up clean:

    ssh <ha-host> 'sudo docker logs --tail 50 telus-cam-bridge'
    ssh <ha-host> 'curl -s http://127.0.0.1:8199/control/health'   # -> {"status": "ok"}

At this point the talk route exists but returns 403 (`talkdown_disabled`) — that
is expected until step 3.

## 2. Deploy the HA integration (sync the separate copy)

    ssh <ha-host> 'rsync -a --delete \
      ~/telus-smart-home/custom_components/telus_smarthome/ \
      ~/homeassistant/config/custom_components/telus_smarthome/'

Restart Home Assistant so `services.yaml` and `telus_smarthome.speak` register:

    ssh <ha-host> 'sudo docker restart homeassistant'

Verify: Developer Tools > Actions > search `telus_smarthome.speak`.

## 3. Enable talkdown on the bridge

Set the flag (non-secret; put it in `.env`, not `secrets.env`) and recreate:

    ssh <ha-host> 'cd ~/telus-smart-home/camera-bridge && \
      grep -q "^TALKDOWN_ENABLED=" .env && \
      sed -i "s/^TALKDOWN_ENABLED=.*/TALKDOWN_ENABLED=true/" .env || \
      echo "TALKDOWN_ENABLED=true" >> .env'
    ssh <ha-host> 'cd ~/telus-smart-home/camera-bridge && docker compose up -d'

## 4. Manual end-to-end test (someone should be able to hear the doorbell)

The endpoint is **modular** — it plays whatever audio you point it at (TTS clip,
WAV, or an MP3 music file). ffmpeg transcodes any input to the doorbell's
µ-law/8k/mono. Test the raw endpoint directly on the host:

    SECRET=$(sudo cat <control_secret_path>)   # see config.CONTROL_SECRET_PATH
    # A short WAV/TTS clip:
    curl -s -X POST http://127.0.0.1:8199/control/cameras/<unit-id>-<did>/talk \
      -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
      -d '{"audio_url":"http://127.0.0.1:8123/local/test.wav"}'
    # A music file (expect lo-fi 8 kHz mono — telephone grade, not hi-fi):
    curl -s -X POST http://127.0.0.1:8199/control/cameras/<unit-id>-<did>/talk \
      -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
      -d '{"audio_url":"http://127.0.0.1:8123/local/music.mp3"}'
    # -> {"path":"full_duplex","chunks":N,"bytes":M}

Or via HA Developer Tools > Actions (`telus_smarthome.speak`, with a TTS
`message:` or a media URL). Expected: audio plays at the doorbell within a few
seconds; the bridge logs `talkdown: full-duplex armed ...` then
`talkdown: streamed N chunks / M bytes mu-law to <host>`.

Troubleshooting quick map (full contract in control_server.py docstring):
- 403 talkdown_disabled  -> step 3 not done / bridge not recreated.
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
greets known occupants). It is only a sample — do not treat its phrase/trigger as
part of the feature. If you use it, add a co-located `front_door_talk` zone
(same polygon as your existing door zone, `loitering_time: 10`) under
`cameras.<doorbell>.zones` in your Frigate `config.yml`, restart Frigate, add
the automation, and enable it only after the manual test in step 4 plays cleanly.

---

## Rollback

- Disable without redeploying: set `TALKDOWN_ENABLED=false` in
  `camera-bridge/.env` and `docker compose up -d` — the route goes inert (403).
- Full revert: check out the previous branch/commit, rebuild the bridge, re-sync
  and restart HA, remove any `front_door_talk` zone + automation you added.
