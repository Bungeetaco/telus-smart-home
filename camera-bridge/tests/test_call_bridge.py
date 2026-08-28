"""Tests for the call bridge's wiring (bridge/call_bridge.py, backlog E99 M2).

THE INVISIBLE FAILURE THIS SUITE EXISTS TO CATCH: a bridged call where one
half is quietly missing. Every wiring mistake here still "works" -- the phone
rings, someone answers, the status says active -- while the caller hears
silence instead of the doorbell mic (uplink dropped), the visitor hears
nothing (downlink dropped), a hangup leaves the talkdown streaming at a dead
call, or a finished call leaves its tap subscribed and the pump paying for it
forever. The encoder table gets the same treatment: a mu-law table that is
wrong only near zero decodes as a permanent background buzz, which no
integration signal ever reports.

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

from bridge import call_bridge, camera_session, config as c, sip_call  # noqa: E402
from bridge.sip_call import _mulaw_encode, pcm_s16le_to_mulaw  # noqa: E402
from bridge.talk_fullduplex import LiveMulawSource  # noqa: E402


# --------------------------------------------------------------------------
# The mu-law table
# --------------------------------------------------------------------------

def test_the_lin2ulaw_table_agrees_with_the_reference_encoder():
    """The table is what the hot path uses; the per-sample function is the
    reference. If they disagree anywhere, the uplink audio is subtly wrong in
    a way nothing downstream can detect -- checked across the FULL 16-bit
    range, because mu-law's segments make 'spot checks pass, segment 5 is
    wrong' a real failure shape."""
    for sample in range(-32768, 32768, 7):     # every segment, both signs
        pcm = (sample & 0xFFFF).to_bytes(2, "little")
        got = pcm_s16le_to_mulaw(pcm)[0]
        want = _mulaw_encode(sample)
        assert got == want, (
            f"table disagrees with the encoder at sample {sample}: "
            f"{got:#04x} != {want:#04x}")
    # And frame-level: one known frame through both paths.
    frame_pcm = b"".join((x & 0xFFFF).to_bytes(2, "little")
                         for x in range(-800, 800, 10))
    assert pcm_s16le_to_mulaw(frame_pcm) == bytes(
        _mulaw_encode(x) for x in range(-800, 800, 10))
    print("  ok  lin2ulaw table matches the reference across the full range")


# --------------------------------------------------------------------------
# LiveMulawSource
# --------------------------------------------------------------------------

async def test_live_source_fills_with_silence_and_ends_on_close():
    """The transport paces itself assuming a full chunk per slot; a live
    source must pad a quiet stretch with mu-law silence and reserve b"" for
    the actual end of the call, or the talkdown ends the first time the
    caller stops talking."""
    src = LiveMulawSource()
    src.feed(b"\x01" * 100)
    chunk = await src.next_chunk(320)
    assert chunk[:100] == b"\x01" * 100 and chunk[100:] == b"\xff" * 220, (
        "a short read was not padded with mu-law silence")
    assert len(chunk) == 320
    assert src.produced == 320
    # Quiet stretch: pure fill, still not EOF.
    assert await src.next_chunk(320) == b"\xff" * 320
    # Close with residue: drain, THEN EOF.
    src.feed(b"\x02" * 10)
    src.close()
    assert (await src.next_chunk(320))[:10] == b"\x02" * 10
    assert await src.next_chunk(320) == b"", "close did not become EOF"
    print("  ok  live source pads silence, drains residue, then EOFs")


async def test_live_source_sheds_oldest_audio_not_latency():
    """A stalled PUT must cost audio, not delay: a live conversation cannot
    pay latency back. The buffer keeps the FRESHEST bytes."""
    src = LiveMulawSource()
    limit = src._max
    src.feed(b"\x01" * limit)
    src.feed(b"\x02" * 100)                    # overflow by 100
    assert src.dropped_bytes == 100
    # Read exactly what the buffer holds: asking for more would be padded
    # with silence fill, which is next_chunk's job, not this test's subject.
    first = await src.next_chunk(limit)
    assert first[-100:] == b"\x02" * 100, "the freshest audio was dropped"
    assert first[:limit - 100] == b"\x01" * (limit - 100), (
        "the oldest audio was not what got shed")
    print("  ok  overflow drops oldest audio and counts it")


# --------------------------------------------------------------------------
# The uplink frame assembly + tap lifecycle (through a real tap registry)
# --------------------------------------------------------------------------

async def test_the_tap_registry_is_bounded_and_cleans_up():
    q = camera_session.subscribe_decoded_audio("cam-t", maxsize=3)
    try:
        for i in range(5):
            camera_session._offer_to_taps("cam-t", bytes([i]) * 4)
        assert q.qsize() == 3
        got = [q.get_nowait()[0] for _ in range(3)]
        assert got == [2, 3, 4], (
            f"a full tap did not drop the OLDEST frames: kept {got}")
    finally:
        camera_session.unsubscribe_decoded_audio("cam-t", q)
    assert "cam-t" not in camera_session._audio_taps, (
        "an unsubscribed camera left its registry entry behind, so the pump "
        "would pay the tap cost forever")
    print("  ok  tap drops oldest at its bound and unsubscribes cleanly")


# --------------------------------------------------------------------------
# CallBridge lifecycle, with the SIP leg and talk manager faked
# --------------------------------------------------------------------------

class FakeTalk:
    """speak_live drains the source until EOF, like the real transport."""

    def __init__(self):
        self.fed = bytearray()
        self.stopped = []
        self.raise_on_speak = None

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


class FakeSipCall:
    """Stands in for sip_call.SipCall: pulls the frame source, feeds the
    payload sink, honours the stop event -- the same contract, no network."""

    instances: list = []
    fail = False

    def __init__(self, **kwargs):
        self.kw = kwargs
        FakeSipCall.instances.append(self)
        self.pulled: list = []

    async def run(self, tone_seconds, timeout):
        if FakeSipCall.fail:
            raise RuntimeError("dial failed")
        sink = self.kw["payload_sink"]
        source = self.kw["frame_source"]
        stop = self.kw["stop_event"]
        for i in range(30):
            if stop.is_set():
                return {"result": "completed", "reason": "stopped",
                        "frames_sent": i, "rtp_received": i, "seconds": i * 0.02}
            sink(bytes([9]) * 160)             # the caller's voice
            self.pulled.append(source())       # the doorbell's mic
            await asyncio.sleep(0)
        return {"result": "completed", "reason": "remote_bye",
                "frames_sent": 30, "rtp_received": 30, "seconds": 0.6}


async def _run_bridge(fail_dial=False, talk_error=None):
    saved = sip_call.SipCall
    sip_call.SipCall = FakeSipCall
    FakeSipCall.instances = []
    FakeSipCall.fail = fail_dial
    talk = FakeTalk()
    if talk_error:
        talk.raise_on_speak = talk_error
    bridge = call_bridge.CallBridge(talk)
    try:
        started = await bridge.start("cam-b", "18195551234")
        assert started.get("status") == "started", started
        # Uplink: a real tap feed while the call runs.
        for i in range(4):
            camera_session._offer_to_taps(
                "cam-b", (i & 0xFFFF).to_bytes(2, "little") * 160)
            await asyncio.sleep(0)
        while bridge.active:
            await asyncio.sleep(0.01)
        return bridge, talk
    finally:
        sip_call.SipCall = saved


async def test_a_bridged_call_wires_both_directions_and_cleans_up():
    bridge, talk = await _run_bridge()
    call = FakeSipCall.instances[0]
    assert bytes(talk.fed[:160]) == bytes([9]) * 160, (
        "the caller's voice never reached the talkdown source")
    real_frames = [f for f in call.pulled if f is not None]
    assert real_frames and all(len(f) == 160 for f in real_frames), (
        f"the doorbell mic never reached the call: {len(real_frames)} frames")
    assert "cam-b" not in camera_session._audio_taps, (
        "the finished call left its audio tap subscribed")
    assert talk.stopped == ["cam-b"], (
        "the finished call did not stop the talkdown leg")
    r = bridge.last_result
    assert r and r["reason"] == "remote_bye" and "talk" in r, r
    assert bridge.status()["active"] is False
    print(f"  ok  both directions wired; {len(real_frames)} uplink frames, "
          f"{len(talk.fed)} downlink bytes, tap and talk torn down")


async def test_a_talkdown_failure_hangs_the_call_up():
    """The speaker path dying must not leave a half-call: the caller would
    hear the doorbell but the visitor would hear nothing, with every status
    green. The bridge hangs up instead and reports the error."""
    bridge, talk = await _run_bridge(talk_error=RuntimeError("mws refused"))
    r = bridge.last_result
    assert r and r["reason"] == "stopped", (
        f"a dead talkdown did not hang up the call: {r}")
    assert "error" in (r.get("talk") or {}), r
    assert "cam-b" not in camera_session._audio_taps
    print("  ok  talkdown failure hangs up and reports the error")


async def test_a_failed_dial_still_tears_everything_down():
    bridge, talk = await _run_bridge(fail_dial=True)
    r = bridge.last_result
    assert r and r.get("result") == "error", r
    assert "cam-b" not in camera_session._audio_taps
    assert talk.stopped == ["cam-b"]
    assert bridge.status()["active"] is False
    print("  ok  a failed dial tears down the tap and the talk leg")


# --------------------------------------------------------------------------
# apply_settings: the HA-managed channel
# --------------------------------------------------------------------------

def test_apply_settings_is_partial_and_clearing_and_never_leaks():
    saved = {a: getattr(c, a) for a in call_bridge._SETTING_FIELDS.values()}
    try:
        applied = call_bridge.apply_settings({
            "enabled": False, "sip_server": " s.example ", "junk": "x",
            "sip_password": 42,                # wrong type: ignored
        })
        assert sorted(applied) == ["enabled", "sip_server"], applied
        assert c.CALL_ENABLED is False
        assert c.VOIPMS_SIP_SERVER == "s.example", "values are stripped"
        assert c.VOIPMS_SIP_PASSWORD == saved["VOIPMS_SIP_PASSWORD"], (
            "a wrong-typed field overwrote the stored password")
        # A toggle-only push must not touch credentials.
        call_bridge.apply_settings({"enabled": True})
        assert c.VOIPMS_SIP_SERVER == "s.example"
        # Clearing is explicit empty string.
        call_bridge.apply_settings({"sip_server": ""})
        assert c.VOIPMS_SIP_SERVER == ""
    finally:
        for attr, val in saved.items():
            setattr(c, attr, val)
    print("  ok  apply_settings: partial, clearing, type-guarded")


TESTS = (
    test_the_lin2ulaw_table_agrees_with_the_reference_encoder,
    test_live_source_fills_with_silence_and_ends_on_close,
    test_live_source_sheds_oldest_audio_not_latency,
    test_the_tap_registry_is_bounded_and_cleans_up,
    test_a_bridged_call_wires_both_directions_and_cleans_up,
    test_a_talkdown_failure_hangs_the_call_up,
    test_a_failed_dial_still_tears_everything_down,
    test_apply_settings_is_partial_and_clearing_and_never_leaks,
)


def main():
    import inspect
    failures = 0
    for test in TESTS:
        try:
            if inspect.iscoroutinefunction(test):
                asyncio.run(test())
            else:
                test()
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
