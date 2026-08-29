"""Tests for the call bridge (bridge/call_bridge.py, backlog E99).

THE INVISIBLE FAILURE THIS SUITE EXISTS TO CATCH: a bridged call where one half
is quietly missing or a call offered on hardware that cannot do it. Every wiring
mistake still "works" -- the phone rings, status says active -- while the caller
hears silence (uplink dropped), the visitor hears nothing (downlink dropped), a
device that never advertised two-way audio gets dialled anyway, a hangup leaves
the talkdown streaming, or a mu-law table wrong near zero buzzes forever. None
raise in production, so the capability gate, the wiring and the lifecycle are
exercised here against fakes -- no device, no network, no charge.

GENERICNESS is a first-class subject here (see the generic-capability-driven
memory): a device advertising no/partial audio must degrade correctly, and the
listen endpoint must be chosen from the account's own flags, never a model/id.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_call_bridge.py
"""

import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import call_bridge, config as c, rtsp_listen, sip_call  # noqa: E402
from bridge.call_bridge import select_listen_url  # noqa: E402
from bridge.sip_call import _mulaw_encode, pcm_s16le_to_mulaw  # noqa: E402
from bridge.talk_fullduplex import LiveMulawSource  # noqa: E402


# --------------------------------------------------------------------------
# mu-law table (uplink encode) + LiveMulawSource (downlink buffer)
# --------------------------------------------------------------------------

def test_the_lin2ulaw_table_agrees_with_the_reference_encoder():
    for sample in range(-32768, 32768, 7):
        pcm = (sample & 0xFFFF).to_bytes(2, "little")
        assert pcm_s16le_to_mulaw(pcm)[0] == _mulaw_encode(sample), sample
    print("  ok  lin2ulaw table matches the reference across the full range")


async def test_live_source_fills_silence_and_ends_on_close():
    src = LiveMulawSource()
    src.feed(b"\x01" * 100)
    chunk = await src.next_chunk(320)
    assert chunk[:100] == b"\x01" * 100 and chunk[100:] == b"\xff" * 220, chunk[:4]
    assert await src.next_chunk(320) == b"\xff" * 320  # dry -> silence, not EOF
    src.feed(b"\x02" * 10); src.close()
    assert (await src.next_chunk(320))[:10] == b"\x02" * 10
    assert await src.next_chunk(320) == b"", "close did not become EOF"
    print("  ok  live source pads silence, drains residue, then EOFs")


async def test_live_source_absorbs_a_slow_ack_stretch_without_dropping():
    SR = 8000
    src = LiveMulawSource()
    two_seconds = SR * 2
    assert two_seconds > SR * 400 // 1000, "must feed past the retired 400ms cap"
    assert two_seconds <= src._max, "elastic cap must hold 2s"
    src.feed(b"\x07" * two_seconds)
    assert src.dropped_bytes == 0, f"absorbing a 2s stall dropped {src.dropped_bytes}"
    print(f"  ok  absorbed 2s backlog with no drop (cap {src._max // SR}s)")


# --------------------------------------------------------------------------
# select_listen_url + capability gating -- GENERIC (task 6)
# --------------------------------------------------------------------------

def test_select_listen_url_prefers_direct_lan():
    kind, url = select_listen_url({"rtsp_urls": {
        "cloud": "rtsp://cloud/x", "direct": "rtsp://lan/x",
        "local": "rtsp://lan2/x", "public": "rtsp://wan/x"}})
    assert kind == "direct" and url == "rtsp://lan/x", (kind, url)
    # Bridge off the device LAN: only cloud present -> cloud is chosen, not None.
    kind, url = select_listen_url({"rtsp_urls": {"cloud": "rtsp://cloud/x"}})
    assert kind == "cloud", (kind, url)
    assert select_listen_url({"rtsp_urls": {}}) == (None, None)
    print("  ok  listen URL chosen by endpoint order (direct-LAN preferred)")


class FakeTalk:
    def __init__(self, doorbell):
        self._doorbell = doorbell
        self.fed = bytearray()
        self.stopped = []
        self.raise_on_speak = None

    async def resolve_doorbell(self, camera_id):
        return self._doorbell

    async def speak_live(self, camera_id, source):
        if self.raise_on_speak:
            raise self.raise_on_speak
        while True:
            chunk = await source.next_chunk(320)
            if chunk == b"":
                return {"chunks": len(self.fed) // 320, "seconds": 1.0,
                        "renegotiations": 0}
            self.fed.extend(chunk)
            await asyncio.sleep(0)

    def stop(self, camera_id):
        self.stopped.append(camera_id)
        return True


def _doorbell(up=True, down=True, rtsp=True):
    return {
        "did": "9", "global_device_id": "acct-9", "model": "GENERIC-CAM",
        "stream_user": "u", "stream_password": "p",
        "supports_upstream_audio": up, "supports_downstream_audio": down,
        "rtsp_urls": {"direct": "rtsp://u:p@10.0.0.9:41925/api/GetStream?streamID=0"}
                     if rtsp else {},
    }


async def _gate(doorbell):
    bridge = call_bridge.CallBridge(FakeTalk(doorbell))
    return await bridge.start("cam", "18195551234")


async def test_capability_gate_degrades_for_partial_or_absent_audio():
    """The heart of genericness: the call is offered per ADVERTISED capability,
    never by model. A cloner's device that reports no/partial audio must be
    turned away cleanly, not dialled into silence."""
    assert (await _gate(None)).get("error") == "talk_not_supported"
    assert (await _gate(_doorbell(up=False))).get("error") == "talk_not_supported"
    assert (await _gate(_doorbell(down=False))).get("error") == "listen_not_supported"
    assert (await _gate(_doorbell(rtsp=False))).get("error") == "no_listen_endpoint"
    ok = await _gate(_doorbell())
    assert ok.get("status") == "started", ok
    print("  ok  capability gate: no-talk / listen-only / no-endpoint all degrade; "
          "full two-way starts")


# --------------------------------------------------------------------------
# Lifecycle with the RTSP listen + SIP faked
# --------------------------------------------------------------------------

class FakeListen:
    instances = []

    def __init__(self, url, user, pw, on_audio, **kw):
        self.on_audio = on_audio
        self.frames = 0
        self.stopped = False
        FakeListen.instances.append(self)

    async def start(self):
        # deliver a little mic audio, as the real read loop would
        for _ in range(3):
            self.on_audio(b"\x10\x00" * 160, 8000)  # 160 samples s16
            self.frames += 1
        return {"codec": "PCMU", "rate": 8000}

    async def stop(self):
        self.stopped = True


class FakeSip:
    instances = []
    fail = False

    def __init__(self, **kw):
        self.kw = kw
        self.pulled = []
        FakeSip.instances.append(self)

    async def run(self, tone_seconds, timeout):
        if FakeSip.fail:
            raise RuntimeError("dial failed")
        sink = self.kw["payload_sink"]
        source = self.kw["frame_source"]
        stop = self.kw["stop_event"]
        for _ in range(20):
            if stop.is_set():
                return {"result": "completed", "reason": "stopped"}
            sink(b"\xff" * 160)          # caller's voice (mu-law)
            self.pulled.append(source())  # doorbell mic frame or None
            await asyncio.sleep(0)
        return {"result": "completed", "reason": "remote_bye"}


async def _run_bridge(doorbell=None, fail_dial=False, talk_error=None):
    saved_listen, saved_sip = rtsp_listen.RtspTunnelListen, sip_call.SipCall
    rtsp_listen.RtspTunnelListen = FakeListen
    sip_call.SipCall = FakeSip
    FakeListen.instances = []; FakeSip.instances = []; FakeSip.fail = fail_dial
    talk = FakeTalk(doorbell or _doorbell())
    if talk_error:
        talk.raise_on_speak = talk_error
    bridge = call_bridge.CallBridge(talk)
    try:
        started = await bridge.start("cam", "18195551234")
        assert started.get("status") == "started", started
        while bridge.active:
            await asyncio.sleep(0.01)
        return bridge, talk
    finally:
        rtsp_listen.RtspTunnelListen = saved_listen
        sip_call.SipCall = saved_sip


async def test_a_bridged_call_wires_both_directions_and_tears_down():
    bridge, talk = await _run_bridge()
    # downlink: the caller's mu-law reached the talkdown source
    assert bytes(talk.fed[:160]) == b"\xff" * 160, "caller voice never reached talkdown"
    # uplink: the RTSP mic frames were encoded and pulled by SIP
    sip = FakeSip.instances[0]
    real = [f for f in sip.pulled if f]
    assert real and all(len(f) == 160 for f in real), f"mic never reached SIP: {len(real)}"
    # teardown: listen stopped, talk stopped
    assert FakeListen.instances[0].stopped, "RTSP listen not stopped"
    assert talk.stopped == ["cam"], "talkdown not stopped"
    r = bridge.last_result
    assert r and r["reason"] == "remote_bye" and r["uplink_frames"] == 3, r
    print(f"  ok  both directions wired ({len(real)} mic frames, "
          f"{len(talk.fed)} downlink bytes); listen + talk torn down")


async def test_a_talkdown_failure_hangs_up_and_tears_down():
    bridge, talk = await _run_bridge(talk_error=RuntimeError("mws refused"))
    r = bridge.last_result
    assert r and r["reason"] == "stopped", f"dead talkdown did not hang up: {r}"
    assert FakeListen.instances[0].stopped, "listen not stopped on talkdown failure"
    print("  ok  a talkdown failure hangs up the call and stops the listen")


async def test_a_failed_dial_tears_everything_down():
    bridge, talk = await _run_bridge(fail_dial=True)
    r = bridge.last_result
    assert r and r.get("result") == "error", r
    assert FakeListen.instances[0].stopped and talk.stopped == ["cam"]
    print("  ok  a failed dial tears down the listen and the talk leg")


# --------------------------------------------------------------------------
# apply_settings
# --------------------------------------------------------------------------

def test_apply_settings_is_partial_and_clearing_and_never_leaks():
    saved = {a: getattr(c, a) for a in call_bridge._SETTING_FIELDS.values()}
    try:
        applied = call_bridge.apply_settings(
            {"enabled": False, "sip_server": " s.example ", "junk": "x",
             "sip_password": 42})
        assert sorted(applied) == ["enabled", "sip_server"], applied
        assert c.CALL_ENABLED is False and c.VOIPMS_SIP_SERVER == "s.example"
        assert c.VOIPMS_SIP_PASSWORD == saved["VOIPMS_SIP_PASSWORD"], "wrong-type overwrote pw"
        call_bridge.apply_settings({"enabled": True})
        assert c.VOIPMS_SIP_SERVER == "s.example", "toggle-only push wiped a field"
        call_bridge.apply_settings({"sip_server": ""})
        assert c.VOIPMS_SIP_SERVER == "", "empty string did not clear"
    finally:
        for a, v in saved.items():
            setattr(c, a, v)
    print("  ok  apply_settings: partial, clearing, type-guarded")


TESTS = (
    test_the_lin2ulaw_table_agrees_with_the_reference_encoder,
    test_live_source_fills_silence_and_ends_on_close,
    test_live_source_absorbs_a_slow_ack_stretch_without_dropping,
    test_select_listen_url_prefers_direct_lan,
    test_capability_gate_degrades_for_partial_or_absent_audio,
    test_a_bridged_call_wires_both_directions_and_tears_down,
    test_a_talkdown_failure_hangs_up_and_tears_down,
    test_a_failed_dial_tears_everything_down,
    test_apply_settings_is_partial_and_clearing_and_never_leaks,
)


def main():
    import inspect
    failures = 0
    for t in TESTS:
        try:
            asyncio.run(t()) if inspect.iscoroutinefunction(t) else t()
        except AssertionError as exc:
            print(f"  FAIL {t.__name__}: {exc}"); failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {exc!r}"); failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
