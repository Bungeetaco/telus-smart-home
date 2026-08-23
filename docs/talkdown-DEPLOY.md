# Talkdown deployment & validation (run LATER, by a human, with explicit approval)

These are manual steps for a person to run when ready to enable doorbell
two-way audio. **Nothing here has been run.** Every step touches the live
system (rebuilds/restarts containers, opens a real MWS stream, plays audio at
the door), so read the whole file first and run the steps in order.

All wiring already lives on branch `feature/doorbell-talkdown` in
`~/claude/telus-smart-home`. The bridge builds its image from that working tree;
Home Assistant runs a **separate copy** at
`~/homeassistant/config/custom_components/telus_smarthome`, so the HA side needs
an explicit sync. The talk endpoint is **off by default** (`TALKDOWN_ENABLED`),
so deploying the code does not arm anything until step 4.

Container names (live): `telus-cam-bridge`, `homeassistant`, `frigate`.

---

## 0. One-time live probe -- confirm the endpoint type BEFORE arming

The one unverified fact is the MWS auth chain and whether the doorbell returns a
`<webrtc>` endpoint (aiortc path) or only `<srtp>` (ffmpeg path). The probe does
exactly ONE website login, stops on any 429, and redacts all tokens (prints
presence/lengths only). Run it against the already-running bridge container:

    ssh hass 'sudo docker exec -i telus-cam-bridge python3 -' < ~/ClaudeSession/telus-talkdown-build/mws_probe.py

- If it prints a `webrtc` endpoint with a SignalingServerUrl/CameraAuthToken:
  the primary path (talk_webrtc.ADCTalkdown) is what will run -- proceed.
- If it prints only an `srtp` endpoint: the fallback path (talk_srtp.SrtpTalkdown,
  ffmpeg) will run instead. That path needs ffmpeg in the image (already present)
  -- proceed, but validate audio carefully in step 5 (L16 vs PCMU codec).
- If it 429s or fails the SSO handoff: do NOT retry in a loop (mobile.alarm.com
  is behind the same Cloudflare edge as the auth gateway). Wait out the
  Retry-After and re-run once. mint_if_stale enforces a 300s cooldown after a
  failed mint for exactly this reason.

---

## 1. Deploy the bridge (build from the branch)

The bridge image builds from `~/claude/telus-smart-home/camera-bridge`. Make sure
the branch is checked out (or merged to your deploy branch) first:

    ssh hass 'cd ~/claude/telus-smart-home && git status --short && git rev-parse --abbrev-ref HEAD'

Then rebuild + recreate the container (requirements.txt gained `pylibsrtp`, so a
rebuild is required, not just a restart):

    ssh hass 'cd ~/claude/telus-smart-home/camera-bridge && docker compose up -d --build'

Confirm it came up clean:

    ssh hass 'sudo docker logs --tail 50 telus-cam-bridge'
    ssh hass 'curl -s http://127.0.0.1:8199/control/health'   # -> {"status": "ok"}

At this point the talk route exists but returns 403 (talkdown_disabled) -- that
is expected until step 4.

## 2. Deploy the HA integration (sync the separate copy)

    ssh hass 'rsync -a --delete \
      ~/claude/telus-smart-home/custom_components/telus_smarthome/ \
      ~/homeassistant/config/custom_components/telus_smarthome/'

Restart Home Assistant so the new `services.yaml` and `telus_smarthome.speak`
service register (manifest bumped to 1.5.1):

    ssh hass 'sudo docker restart homeassistant'

Verify the service exists: Developer Tools > Actions > search `telus_smarthome.speak`
(or `Developer Tools > Services`).

## 3. (If the probe showed WebRTC/zone-based dwell) add the Frigate 10s zone

The deterrent automation triggers on a 10-second dwell zone. The live doorbell
camera has only `front_door` (loitering_time 5). Add a co-located
`front_door_talk` zone (same polygon, loitering_time 10) under
`cameras.doorbell.zones` in `~/frigate/config/config.yml`:

    front_door_talk:
      coordinates: 0,1,0,0.86,0.178,0.842,0.303,0.814,0.77,0.937,0.7,1
      loitering_time: 10
      objects: person
      inertia: 3

Then restart Frigate:

    ssh hass 'sudo docker restart frigate'

Add the automation from `docs/talkdown-automation.yaml` to HA
(`~/homeassistant/config/automations.yaml`, or via the UI) and reload
automations. Leave it disabled until after the manual test in step 5.

## 4. Enable talkdown on the bridge

Set the flag (non-secret; put it in `.env`, not `secrets.env`) and recreate:

    ssh hass 'cd ~/claude/telus-smart-home/camera-bridge && \
      grep -q "^TALKDOWN_ENABLED=" .env && \
      sed -i "s/^TALKDOWN_ENABLED=.*/TALKDOWN_ENABLED=true/" .env || \
      echo "TALKDOWN_ENABLED=true" >> .env'
    ssh hass 'cd ~/claude/telus-smart-home/camera-bridge && docker compose up -d'

## 5. Manual end-to-end test (someone should be able to hear the doorbell)

Fire the service once from HA Developer Tools > Actions:

    action: telus_smarthome.speak
    data:
      message: "This is a talkdown test."
    # target the doorbell explicitly if the default doorbell match is ambiguous:
    # target:
    #   entity_id: camera.<your_doorbell_entity>

Expected: audio plays at the doorbell within a few seconds; the bridge logs a
`talkdown: phrase played; tearing down` line and the service returns without
error. To test the raw endpoint directly (bypassing HA), on the host:

    SECRET=$(sudo cat /var/lib/homeassistant/share/telus_cam_bridge/control_secret)
    curl -s -X POST http://127.0.0.1:8199/control/cameras/111937238-2048/talk \
      -H "Authorization: Bearer $SECRET" -H "Content-Type: application/json" \
      -d '{"audio_url":"http://127.0.0.1:8123/local/test.wav"}'
    # -> {"status":"spoken","path":"webrtc"|"srtp"}

Troubleshooting quick map (see control_server.py docstring for the full contract):
- 403 talkdown_disabled  -> step 4 not done / bridge not recreated.
- 409 talk_in_progress   -> a talk is already running for that camera; wait.
- 422 no_upstream_audio  -> GetLiveVideoStream returned no talk endpoint (re-run
                            the probe; the doorbell may need a fresh stream).
- 502 telus_error        -> MWS auth / stream lookup / signaling failed; check
                            bridge logs (tokens are redacted there).
- 504 talk_timeout       -> handshake/playback exceeded TALK_SESSION_TIMEOUT_S.

## 6. Arm the automation

Once the manual test plays cleanly and never triggers for the household (verify
`binary_sensor.recent_entry` goes `on` when you enter), enable the
`Doorbell - Talkdown deterrent` automation.

---

## Rollback

- Disable without redeploying: set `TALKDOWN_ENABLED=false` in
  `camera-bridge/.env` and `docker compose up -d` -- the route goes inert (403).
- Full revert: check out the previous branch/commit, rebuild the bridge, re-sync
  and restart HA, remove the `front_door_talk` zone + automation.
