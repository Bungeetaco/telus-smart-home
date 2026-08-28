"""Bridge an answered phone call to the doorbell's two audio paths (E99, M2).

One live call is a triangle of three existing pieces, and this module is only
the wiring between them:

  * **Uplink (doorbell mic -> caller's ear):** camera_session already decodes
    the doorbell's mic continuously for the RTSP publisher; a tap
    (camera_session.subscribe_decoded_audio) hands this module the same PCM
    frames, which are re-encoded to mu-law (sip_call.pcm_s16le_to_mulaw, a
    table) and pulled by the SIP leg's frame_source at its own 20 ms cadence.
  * **Downlink (caller's voice -> doorbell speaker):** the SIP leg's received
    RTP payloads are already wire-format mu-law; they are fed verbatim into a
    talk_fullduplex.LiveMulawSource, which talkdown's existing transport
    drains through the doorbell's chunked Digest PUT.
  * **The call itself:** sip_call.SipCall in live mode, which runs until the
    operator stops it, the far end hangs up, or config.CALL_MAX_S.

No transcoding anywhere: every leg is G.711 mu-law at 8 kHz, which is why the
whole bridge is queues and byte-slicing.

LIFECYCLE, the only part with judgment in it. The talk session (StartFullDuplex
through the MWS API) is armed CONCURRENTLY with dialing: arming takes seconds
and doing it during ring time means the doorbell speaker is live the moment the
call is answered. Either half ending ends the other -- a hangup closes the
talkdown source (EOF -> normal teardown), a talkdown failure hangs up the call
-- and both teardowns already exist in their own modules; this module only
pulls the triggers. One call at a time across ALL cameras: the SIP sub-account
is one line, and the operator is one person.

Settings come from config (VOIPMS_*, DOORBELL_CALL_TARGET, CALL_ENABLED),
which Home Assistant manages live through /control/call/settings and the
call_settings.json fallback -- the same two-channel pattern as the TELUS
credentials. Nothing here reads the environment directly.
"""

import asyncio
import logging
import time

from . import camera_session, config as c, sip_call, talk_fullduplex

log = logging.getLogger(__name__)


# The HA-managed fields, in one place: the control route's live push, the
# shared-file fallback loader in main.py and the watcher all apply the SAME
# list, so a field cannot be settable on one channel and not another.
_SETTING_FIELDS = {
    "enabled": "CALL_ENABLED",
    "sip_server": "VOIPMS_SIP_SERVER",
    "sip_account": "VOIPMS_SIP_ACCOUNT",
    "sip_password": "VOIPMS_SIP_PASSWORD",
    "did": "VOIPMS_DID",
    "call_target": "DOORBELL_CALL_TARGET",
}


def apply_settings(data: dict) -> list:
    """Apply an HA-managed settings payload to the live config.

    A missing key leaves the current value alone (a toggle-only push cannot
    wipe the credentials); a present key is applied verbatim, including an
    empty string, which is how a field is cleared. Returns the names applied
    -- names only, never values, because one of them is a password."""
    applied = []
    for key, attr in _SETTING_FIELDS.items():
        if key not in data:
            continue
        value = data[key]
        if key == "enabled":
            value = bool(value)
        elif not isinstance(value, str):
            continue
        else:
            value = value.strip()
        setattr(c, attr, value)
        applied.append(key)
    return applied


class CallBridge:
    """At most one live doorbell call; see the module docstring."""

    def __init__(self, talk_manager):
        self._talk = talk_manager
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._camera_id: str | None = None
        self._started_at: float | None = None
        self.last_result: dict | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
        """The /control/status "call" section: current state, always honest.

        `last_result` persists across calls so "why did the last call end"
        survives the call itself -- the same reasoning as the talk metrics.
        """
        return {
            "enabled": bool(c.CALL_ENABLED and c.VOIPMS_SIP_SERVER
                            and c.VOIPMS_SIP_ACCOUNT and c.VOIPMS_SIP_PASSWORD),
            "active": self.active,
            "camera_id": self._camera_id if self.active else None,
            "since": self._started_at if self.active else None,
            "last_result": self.last_result,
        }

    async def start(self, camera_id: str, target: str) -> dict:
        if self._talk is None:
            return {"error": "talkdown_disabled"}
        if self.active:
            return {"error": "call_in_progress"}
        self._camera_id = camera_id
        self._started_at = time.time()
        self._stop_event = asyncio.Event()
        self._task = asyncio.ensure_future(self._run(camera_id, target,
                                                     self._stop_event))
        return {"status": "started", "camera_id": camera_id}

    def stop(self) -> bool:
        """Ask the live call to end. Both halves observe it: the SIP leg
        hangs up at its next frame slot, and the talkdown stops through the
        talk manager's own stop path."""
        if not self.active:
            return False
        self._stop_event.set()
        if self._camera_id:
            self._talk.stop(self._camera_id)
        return True

    async def _run(self, camera_id: str, target: str,
                   stop_event: asyncio.Event) -> None:
        tap = camera_session.subscribe_decoded_audio(
            camera_id, maxsize=c.CALL_UPLINK_TAP_FRAMES)
        live_source = talk_fullduplex.LiveMulawSource()
        ulaw_buf = bytearray()

        def frame_source() -> bytes | None:
            # Pull whatever decoded mic audio has arrived, encode, and hand
            # out exactly one 20 ms frame. Returning None sends silence, which
            # is correct both while the buffer is short and while the camera's
            # own audio is sparse (E97): the tap simply has nothing yet.
            while len(ulaw_buf) < 160:
                try:
                    pcm = tap.get_nowait()
                except asyncio.QueueEmpty:
                    break
                ulaw_buf.extend(sip_call.pcm_s16le_to_mulaw(pcm))
            if len(ulaw_buf) >= 160:
                out = bytes(ulaw_buf[:160])
                del ulaw_buf[:160]
                return out
            return None

        call = sip_call.SipCall(
            server=c.VOIPMS_SIP_SERVER, username=c.VOIPMS_SIP_ACCOUNT,
            password=c.VOIPMS_SIP_PASSWORD, target=target,
            caller_id=c.VOIPMS_DID or "doorbell", sip_port=c.SIP_PORT,
            frame_source=frame_source, payload_sink=live_source.feed,
            stop_event=stop_event)

        async def _talk_leg() -> dict:
            try:
                return await self._talk.speak_live(camera_id, live_source)
            except Exception as err:  # noqa: BLE001
                # The speaker path died (auth, session, transport). The call
                # keeps no value without it: hang up. The error is the result.
                log.warning("call bridge: talkdown leg failed (%s); hanging up",
                            err)
                stop_event.set()
                return {"error": repr(err)}

        talk_task = asyncio.ensure_future(_talk_leg())
        try:
            result = await call.run(tone_seconds=0,
                                    timeout=c.CALL_TIMEOUT_S + c.CALL_MAX_S)
        except Exception as err:  # noqa: BLE001
            log.exception("call bridge: SIP leg failed")
            result = {"result": "error", "error": repr(err)}
        finally:
            # Whatever ended the call ends the talkdown: EOF the live source
            # (normal drain-out) and signal the manager's stop path too, so a
            # talkdown mid-renegotiation does not linger on a dead call.
            live_source.close()
            self._talk.stop(camera_id)
            camera_session.unsubscribe_decoded_audio(camera_id, tap)
        talk = await talk_task
        result["talk"] = {k: talk.get(k) for k in
                         ("chunks", "seconds", "renegotiations", "error")
                         if isinstance(talk, dict) and k in talk}
        result["downlink_dropped_bytes"] = live_source.dropped_bytes
        self.last_result = result
        log.info("call bridge: call ended: %s",
                 {k: result.get(k) for k in ("result", "reason", "seconds")})
