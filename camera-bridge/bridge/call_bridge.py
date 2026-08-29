"""Bridge an answered phone call to the doorbell's two audio paths (E99).

THE ARCHITECTURE (why the uplink is NOT the Janus tap). The bridge's cloud
Janus/WebRTC video path routes even a same-LAN doorbell's media up to
alarm.com and back through a TURN relay, and for a high-bitrate device that
relay churns (webrtcup, no RTP, 20 s timeout, reconnect, on a loop). Feeding a
live call's uplink from that path made the caller's audio drop out whenever the
relay hiccuped. So the call takes the device's OWN stream directly on the LAN,
exactly as the official app does, and never touches Janus:

  * **Uplink (doorbell mic -> caller):** rtsp_listen.RtspTunnelListen pulls the
    device's audio over its direct RTSP-in-HTTPS tunnel, decoded to PCM, shaped
    for phone clarity (call_shape), re-encoded to mu-law, and drained by the
    SIP leg at its 20 ms cadence. No cloud, no relay, no churn.
  * **Downlink (caller -> doorbell speaker):** the SIP leg's received mu-law is
    shaped for the small speaker (call_shape) and fed to a
    talk_fullduplex.LiveMulawSource, which talkdown's chunked Digest PUT drains
    straight to the device on the LAN.
  * **The call itself:** sip_call.SipCall in live mode.

GENERIC BY CONSTRUCTION (see the generic-capability-driven memory / AGENTS):
the call is offered per device from the ADVERTISED capability flags, never a
model or id. Two-way needs both upstream and downstream audio; the RTSP listen
URL and codec are read from the account's camera list and the stream's own SDP.
A device that advertises no upstream audio is not offered talk; a device with a
different codec or endpoint just works.

Settings are HA-managed live (apply_settings) through /control/call/settings and
the call_settings.json fallback, the same two-channel pattern as the TELUS
credentials. Nothing here reads the environment directly.
"""

import asyncio
import logging
import re
import time

from . import config as c, rtsp_listen, sip_call, talk_fullduplex
from .call_shape import downlink_shaper, uplink_shaper

log = logging.getLogger(__name__)


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
    wipe the credentials); a present key is applied verbatim, including an empty
    string, which is how a field is cleared. Returns the names applied -- names
    only, never values, because one of them is a password."""
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


class EchoDucker:
    """Downlink-priority ducking (half-duplex bias) -- the echo control.

    No AEC exists in this path, and with the uplink latency cap in place the
    caller's voice returns speaker -> mic -> phone as a ~0.6 s echo. So while
    the downlink carries active speech, the uplink is attenuated hard: the
    caller never hears the room they are talking into. note_downlink() is fed
    every decoded downlink frame; factor() is applied to the uplink PCM.
    Pure and clock-injected for testing; atten 1.0 disables."""

    def __init__(self, *, atten: float | None = None,
                 hangover_ms: float | None = None,
                 activity: float | None = None):
        self.atten = c.CALL_ECHO_DUCK_ATTEN if atten is None else atten
        self.hangover_s = (c.CALL_ECHO_DUCK_HANGOVER_MS if hangover_ms is None
                           else hangover_ms) / 1000.0
        self.activity = int(32768 * (c.CALL_ECHO_DUCK_ACTIVITY if activity is None
                                     else activity))
        self._active_until = 0.0
        self.ducked_ms = 0.0

    def note_downlink(self, pcm: bytes, now: float) -> None:
        if self.atten >= 1.0:
            return
        for i in range(0, len(pcm) - 1, 2):
            v = int.from_bytes(pcm[i:i + 2], "little", signed=True)
            if v >= self.activity or -v >= self.activity:
                self._active_until = now + self.hangover_s
                return

    def factor(self, now: float) -> float:
        return self.atten if (self.atten < 1.0
                              and now < self._active_until) else 1.0

    def apply(self, pcm: bytes, now: float) -> bytes:
        f = self.factor(now)
        if f >= 1.0:
            return pcm
        self.ducked_ms += len(pcm) / 2 / 8.0
        out = bytearray(len(pcm))
        for i in range(0, len(pcm) - 1, 2):
            v = int(int.from_bytes(pcm[i:i + 2], "little", signed=True) * f)
            out[i] = v & 0xFF
            out[i + 1] = (v >> 8) & 0xFF
        return bytes(out)


class UplinkMulawBuffer:
    """Mu-law between the RTSP listen and the SIP leg -- capped, shed-oldest.

    The exact mirror of the downlink's LiveMulawSource cap: the listen starts
    several seconds before the phone answers and the SIP leg drains at exactly
    real time, so any pre-answer backlog would persist as uplink delay for the
    whole call (measured live: 4-5 s). The cap (config.CALL_UPLINK_BUFFER_MS)
    is the uplink's mouth-to-ear latency ceiling; past it the OLDEST audio is
    shed, keeping what the caller hears current."""

    def __init__(self):
        self._buf = bytearray()
        self._max = 8000 * c.CALL_UPLINK_BUFFER_MS // 1000
        self.dropped_bytes = 0

    def feed(self, ulaw: bytes) -> None:
        self._buf.extend(ulaw)
        if len(self._buf) > self._max:
            drop = len(self._buf) - self._max
            del self._buf[:drop]
            self.dropped_bytes += drop

    def take(self, n: int) -> bytes | None:
        """One n-byte frame, or None when short (the SIP leg sends silence)."""
        if len(self._buf) < n:
            return None
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out


def select_listen_url(doorbell: dict) -> tuple[str | None, str | None]:
    """Pick the direct-LAN RTSP listen URL by CALL_LISTEN_ENDPOINT_ORDER, first
    present wins. Returns (kind, url). Direct/LAN is preferred; a bridge not on
    the device's LAN falls through to WAN/cloud. Generic -- keyed on the account's
    own endpoint set, not on any address."""
    urls = doorbell.get("rtsp_urls") or {}
    for kind in c.CALL_LISTEN_ENDPOINT_ORDER:
        if urls.get(kind):
            return kind, urls[kind]
    return None, None


class CallBridge:
    """At most one live doorbell call; see the module docstring."""

    def __init__(self, talk_manager, lifecycle=None):
        self._talk = talk_manager
        self._lifecycle = lifecycle          # pause/resume video during a call
        self._task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._camera_id: str | None = None
        self._started_at: float | None = None
        self.last_result: dict | None = None

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict:
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
        # Gate on ADVERTISED capabilities before opening anything: a device that
        # cannot do two-way audio must not be dialled. Generic -- reads the flags,
        # never the model.
        try:
            doorbell = await self._talk.resolve_doorbell(camera_id)
        except Exception as err:  # noqa: BLE001
            log.warning("call bridge: doorbell resolve failed for %s: %s", camera_id, err)
            return {"error": "resolve_failed"}
        if doorbell is None or not doorbell.get("supports_upstream_audio"):
            return {"error": "talk_not_supported"}
        if not doorbell.get("supports_downstream_audio"):
            # Talk-only device: a phone call with no return audio is not a call.
            return {"error": "listen_not_supported"}
        kind, listen_url = select_listen_url(doorbell)
        if not listen_url or not doorbell.get("stream_password"):
            return {"error": "no_listen_endpoint"}
        self._camera_id = camera_id
        self._started_at = time.time()
        self._stop_event = asyncio.Event()
        self._task = asyncio.ensure_future(
            self._run(camera_id, target, doorbell, kind, listen_url, self._stop_event))
        return {"status": "started", "camera_id": camera_id}

    def stop(self) -> bool:
        if not self.active:
            return False
        self._stop_event.set()
        if self._camera_id:
            self._talk.stop(self._camera_id)
        return True

    async def _run(self, camera_id, target, doorbell, listen_kind, listen_url,
                   stop_event) -> None:
        up_shaper = uplink_shaper(c)
        down_shaper = downlink_shaper(c)
        live_source = talk_fullduplex.LiveMulawSource()
        uplink_buf = UplinkMulawBuffer()
        ducker = EchoDucker()

        # --- uplink: direct-LAN RTSP mic -> shape -> duck -> mu-law -> SIP ----
        def on_mic_audio(pcm: bytes, rate: int) -> None:
            _cap("uplink_raw.s16le", pcm)
            shaped = ducker.apply(up_shaper.process(pcm), time.monotonic())
            ulaw = sip_call.pcm_s16le_to_mulaw(shaped)
            _cap("uplink_sent.ulaw", ulaw)
            uplink_buf.feed(ulaw)

        listen = rtsp_listen.RtspTunnelListen(
            listen_url, doorbell["stream_user"], doorbell["stream_password"],
            on_mic_audio)

        def frame_source() -> bytes | None:
            # One 20 ms mu-law frame, or None (silence) when the mic buffer is
            # short -- correct while audio is sparse or the listen is warming up.
            return uplink_buf.take(160)

        # --- downlink: SIP mu-law -> shape for the speaker -> talkdown PUT -----
        def on_caller_audio(mulaw: bytes) -> None:
            pcm = sip_call.mulaw_to_pcm_s16le(mulaw)
            ducker.note_downlink(pcm, time.monotonic())
            if down_shaper.passthrough:
                _cap("downlink_sent.ulaw", mulaw)
                live_source.feed(mulaw)
            else:
                out = sip_call.pcm_s16le_to_mulaw(down_shaper.process(pcm))
                _cap("downlink_sent.ulaw", out)
                live_source.feed(out)

        call = sip_call.SipCall(
            server=c.VOIPMS_SIP_SERVER, username=c.VOIPMS_SIP_ACCOUNT,
            password=c.VOIPMS_SIP_PASSWORD, target=target,
            caller_id=c.VOIPMS_DID or "doorbell", sip_port=c.SIP_PORT,
            frame_source=frame_source, payload_sink=on_caller_audio,
            stop_event=stop_event)

        async def _talk_leg() -> dict:
            try:
                return await self._talk.speak_live(camera_id, live_source)
            except Exception as err:  # noqa: BLE001
                log.warning("call bridge: talkdown leg failed (%s); hanging up", err)
                stop_event.set()
                return {"error": repr(err)}

        # Debug capture (config.CALL_DEBUG_CAPTURE_DIR): never allowed to break
        # the call -- every write is best-effort.
        caps = {}
        if c.CALL_DEBUG_CAPTURE_DIR:
            try:
                import os as _os
                _os.makedirs(c.CALL_DEBUG_CAPTURE_DIR, exist_ok=True)
                stamp = f"{int(time.time())}-{camera_id}"
                for name in ("uplink_raw.s16le", "uplink_sent.ulaw",
                             "downlink_sent.ulaw"):
                    caps[name] = open(
                        f"{c.CALL_DEBUG_CAPTURE_DIR}/{stamp}-{name}", "wb")
                log.info("call bridge: debug capture -> %s/%s-*",
                         c.CALL_DEBUG_CAPTURE_DIR, stamp)
            except Exception:
                log.warning("call bridge: debug capture unavailable", exc_info=True)
                caps = {}

        def _cap(name: str, data: bytes) -> None:
            f = caps.get(name)
            if f is not None:
                try:
                    f.write(data)
                except Exception:
                    caps.pop(name, None)

        # Pause this camera's always-on video for the call (see
        # config.CALL_PAUSE_VIDEO_DURING_CALL): the device's encoder must serve
        # the call, not the cloud session. Reactivated in the finally below.
        paused = False
        if c.CALL_PAUSE_VIDEO_DURING_CALL and self._lifecycle is not None:
            try:
                await self._lifecycle.deactivate(camera_id)
                paused = True
                log.info("call bridge: paused %s video for the call", camera_id)
            except Exception:
                log.warning("call bridge: could not pause %s video; calling "
                            "with it running", camera_id, exc_info=True)

        listen_started = False
        talk_task = asyncio.ensure_future(_talk_leg())
        try:
            audio = await listen.start()
            listen_started = True
            log.info("call bridge: uplink via %s RTSP (%s); dialing %s",
                     listen_kind, audio.get("codec"), re.sub(r"\d", "x", target))
            result = await call.run(tone_seconds=0,
                                    timeout=c.CALL_TIMEOUT_S + c.CALL_MAX_S)
        except Exception as err:  # noqa: BLE001
            log.exception("call bridge: call failed")
            result = {"result": "error", "error": repr(err)}
        finally:
            stop_event.set()
            if listen_started:
                try:
                    await listen.stop()
                except Exception:
                    log.debug("call bridge: listen stop failed", exc_info=True)
            live_source.close()
            self._talk.stop(camera_id)
            for f in caps.values():
                try:
                    f.close()
                except Exception:
                    pass
            if paused:
                try:
                    await self._lifecycle.reactivate(camera_id)
                    log.info("call bridge: resumed %s video after the call", camera_id)
                except Exception:
                    log.exception("call bridge: FAILED to resume %s video -- "
                                  "reactivate it via the control API", camera_id)
        talk = await talk_task
        result["talk"] = {k: talk.get(k) for k in
                          ("chunks", "seconds", "played_seconds",
                           "renegotiations", "reconnects", "put_rtt_p50_ms",
                           "put_rtt_p95_ms", "put_rtt_max_ms", "late_chunks",
                           "error")
                          if isinstance(talk, dict) and k in talk}
        result["uplink_frames"] = listen.frames
        result["uplink_dropped_bytes"] = uplink_buf.dropped_bytes
        result["uplink_ducked_ms"] = round(ducker.ducked_ms)
        result["downlink_dropped_bytes"] = live_source.dropped_bytes
        self.last_result = result
        log.info("call bridge: call ended: %s",
                 {k: result.get(k) for k in ("result", "reason", "seconds",
                                             "uplink_frames")})
