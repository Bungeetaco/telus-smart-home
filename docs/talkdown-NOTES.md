# Talkdown branch — review fixes applied (feature/doorbell-talkdown)

Status: **library groundwork only**. The four transport/auth modules
(`mobile_auth.py`, `mws_client.py`, `talk_webrtc.py`, `talk_srtp.py`) still have
**zero callers** — the feature is dormant and cannot make the doorbell speak
until the orchestration + HA layer is built (see *Deferred* below). This pass
applied the confirmed/likely correctness and safety fixes that can be made
in-place **without any live network call, deploy, or commit**. No code was run
against the real account.

## Fixed (this pass)

1. **429 not honored on the stream path** — `mws_client.get_live_video_stream`
   (MEDIUM, confirmed). Added `_raise_if_rate_limited(resp)` (raises the shared
   `telus_auth.RateLimited` with the server's `Retry-After`) **before**
   `resp.raise_for_status()`, mirroring `mobile_auth`. A Cloudflare 429 now
   funnels into the same Retry-After backoff as the auth path instead of
   surfacing as a generic `ClientResponseError` that a caller might tight-retry.

2. **GetLiveVideoStream missing the BaseTokenRequest envelope** —
   `mws_client.get_live_video_stream` (HIGH, likely). The POST previously sent
   only `Action/SessionToken/DeviceId/ForDevice`, omitting `CustomerId`
   (account/device scoping) and `Haiku` (app-integrity gate) that the app
   attaches to every token request — server rejection was likely. Now builds the
   full app envelope by reusing `mobile_auth._device_fields()` / `HAIKU` /
   `APP_BUILD`, adds `CustomerId` (already received but previously discarded),
   `ForceServerError=false`, and, when threaded through, `MobileDeviceUid` /
   `SeamlessLoginToken`. Two new optional params (`mobile_device_uid`,
   `seamless_login_token`) let the eventual caller pass `MobileSession`
   identity through. **We send the full app set first; it must be trimmed
   empirically against ONE live capture** (see below and
   `specs/mws_getlivevideostream.md`).

3. **No backoff after a FAILED mint** — `mobile_auth` (MEDIUM, likely). Added
   `LOGIN_FAILURE_COOLDOWN_S = 300` and a per-`MobileSession`
   `_login_cooldown_until`. `mint_if_stale` now, under the shared `login_lock`,
   refuses another mint within the cooldown after a `MobileLoginError` and starts
   that cooldown when `login()` raises `MobileLoginError`. This guards the
   *sequential* retry case that `login_generation` (concurrent-only dedup) does
   not — so a caller re-triggering on each doorbell press cannot re-walk the
   unverified up-to-10-hop SSO bootstrap against the Cloudflare edge.
   `RateLimited` is deliberately **not** converted into this cooldown — it keeps
   propagating with its own `Retry-After`. (The "single bootstrap attempt per
   mint" part was already satisfied: `login()` does not loop.)

4. **SRTP path could wait forever** — `talk_srtp.py` (MEDIUM, confirmed). Added
   `DEFAULT_TALKDOWN_TIMEOUT_S = 60` and made it the default for
   `SrtpTalkdown.run()` and `send_talkdown()` (was `None`). With `timeout=None`,
   `asyncio.wait_for(proc.wait(), timeout=None)` blocks forever and the
   `finally: await self.stop()` teardown never runs — the FD/process-leak class
   that has blanked these cameras. Docstrings now warn that `timeout=None`
   disables teardown.

5. **Unbounded audio decode on the shared executor** — `talk_webrtc.py`
   (MEDIUM, likely). `FileTalkTrack.play()` decoded via
   `run_in_executor(None, ...)` (the shared default pool) with no timeout, so a
   malformed/huge/streaming source could stall — and, across repeated presses,
   exhaust — the same pool every camera-pipeline call uses. Now uses a dedicated
   `ThreadPoolExecutor(max_workers=1)` owned by the talk subsystem and wraps the
   decode in `asyncio.wait_for(..., DECODE_TIMEOUT_S=15)`. (A `run_in_executor`
   future still cannot truly cancel the worker thread — noted in the finding —
   but the dedicated single-worker pool contains the blast radius and the
   timeout frees the awaiting task.)

6. **SRTP master key on the ffmpeg argv** — `talk_srtp.py` (MEDIUM, confirmed) —
   **documented, rewrite deferred.** ffmpeg's srtp muxer accepts the key only via
   the `srtp://…?srtp_out_params=` URL, which is in argv and thus readable via
   `/proc/<pid>/cmdline` in the same PID namespace. Added an explicit
   SECURITY / ACCEPTED-RESIDUAL-RISK comment in `_build_args` stating the
   assumption it relies on (container does not share its PID namespace; no
   untrusted local process alongside) and pointing at the in-process pylibsrtp
   Option B as the fix if that assumption ever fails. The Option B rewrite is
   deferred because it is substantial and cannot be validated without a live
   SRTP session.

## Deferred (deliberately not done this pass — with reasons)

- **Build the orchestrator / `talk.py` + `talk_manager.py`** (HIGH + MEDIUM
  integration/lifecycle findings). This is a whole new subsystem, not an
  in-place fix: per-camera single-flight `asyncio.Lock`, a global
  `Semaphore(TALK_MAX_CONCURRENT)`, one top-level `asyncio.wait_for` wall-clock
  ceiling over mint→GetLiveVideoStream→ADCTalkdown/SrtpTalkdown, and driving
  `mint_if_stale(mobile, login_lock)` with **the exact `login_lock` instance
  `main.py` already builds for `telus_auth`**. It requires design decisions and
  can only be validated end-to-end with live calls. **This is the single most
  important thing still missing.**

- **`token_status==1` re-mint guard in the caller** (MEDIUM, likely). The fix
  lives "in the caller": on logged-out, re-mint at most once, retry the stream at
  most once, then cool down — never loop mint↔stream. There is no caller yet, so
  it cannot be written in-place. Must land with the orchestrator (the
  `mint_if_stale` failure-cooldown above already backstops the mint half).

- **HA safety/control layer** (HIGH, confirmed): the `/control/cameras/{id}/talk`
  endpoint reusing `_check_auth`, `talk_manager.py` guardrails
  (409/503/504/media-size cap), a disabled-by-default `TALK_ENABLED` +
  `TALK_MAX_CONCURRENT` / `TALK_SESSION_TIMEOUT_S` / `TALK_MEDIA_MAX_BYTES`
  config, the `telus_smarthome.speak` service + `services.yaml`, and the
  dwell/`recent_entry` automation. Deferred as a feature build requiring design +
  live validation. Config constants were **not** stubbed in, to avoid shipping
  dead settings whose names/semantics are the spec's design.

- **WebRTC trickle-ICE fallback** (MEDIUM, likely) — `talk_webrtc.py`. The code
  is non-trickle (candidates embedded in the answer SDP), which the spec
  sanctions as the default. Whether the camera firmware *requires* per-candidate
  trickled `{"to":…,"ice":{…}}` frames is unknown without a live probe, so adding
  the fallback would be speculative and unverifiable here. The live talk-test
  will reveal if it is needed; leaving as default until then.

- **Low-severity findings** (out of this pass's HIGH/MEDIUM scope): e.g.
  `_parse_login_xml` accepting `lr==2` with missing `st`; `Retry-After`
  HTTP-date parsing; `defusedxml` in `mws_client.parse_live_video_stream`; the
  non-trickle JSON whitespace vs Moshi compact output; `pc.close()` not bounded
  by `wait_for` in `teardown()`; SSO-walk per-hop timeout; wait_for-returns-a-
  socket race on `websockets.connect`. Not addressed here.

## Remaining human steps — each REQUIRES EXPLICIT APPROVAL (touches the live account/doorbell)

These were intentionally NOT performed and must be run by a human who has
decided to touch the live TELUS/alarm.com account and the physical doorbell:

1. **One-time mobile SSO/token probe (live).** Confirm the still-**unverified**
   `SSORedirectHandler.ashx?AppType=66` redirect chain actually yields a
   login-as token with the website cookie jar, and capture ONE real
   `GetLiveVideoStream` response to (a) confirm it returns a `webrtc` vs `srtp`
   endpoint and (b) **trim the request envelope** added in fix #2 down to what
   the server actually requires. This is a real login against the Cloudflare
   edge — do it once, honor any `Retry-After`, do not loop.

2. **Build + wire the orchestrator and HA safety layer** (the Deferred items
   above), then **deploy** — rsync into the live HA config dir / rebuild the
   bridge container. Not done here (no deploy was performed).

3. **Live talk-test.** With the guardrails in place and `TALK_ENABLED` turned
   on deliberately, send one short phrase and confirm the doorbell emits audio
   and the session tears down cleanly (no leaked ffmpeg/RTCPeerConnection/WS).
   This makes the doorbell physically speak — explicit approval required.

## What a reviewer should verify first

The envelope in fix #2 is **"likely", not confirmed** and is the highest-risk
change: it alters the on-wire request. It is grounded in the decompiled sources
and `specs/mobile_auth.md`, and sending the full app set is the documented
correct default — but the exact required field set can only be pinned by the
live capture in human-step #1. Verify that capture before relying on the stream
call, and trim the envelope to match.
