"""Tests for the direct-LAN RTSP-in-HTTPS listen client (bridge/rtsp_listen.py)
and the call audio shaper (bridge/call_shape.py), backlog E99.

THE INVISIBLE FAILURES THIS SUITE EXISTS TO CATCH: a listen path that "works"
but carries nothing usable. A SDP parsed wrong picks the wrong codec and the
caller hears static; an interleaved demux that miscounts a length desyncs and
drops the whole stream; an RTP header not stripped decodes the 12-byte header
as audio (a click every packet); a shaper that is not really passthrough at
unit gain quietly distorts. None raise in production. So the pure protocol
parsers and the DSP are exercised directly here -- no device, no network.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_rtsp_listen.py
"""

import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import rtsp_listen  # noqa: E402
from bridge.rtsp_listen import (  # noqa: E402
    parse_sdp_audio, iter_interleaved, rtp_payload, RtspListenError,
    content_base_uri, track_uri, _ULAW_DECODE,
)
from bridge.call_shape import Shaper  # noqa: E402


# --------------------------------------------------------------------------
# SDP parsing -- codec read from the stream, never assumed
# --------------------------------------------------------------------------

def test_sdp_audio_is_read_from_the_stream_not_assumed():
    sdp = (
        "v=0\r\no=- 0 0 IN IP4 0.0.0.0\r\ns=stream\r\n"
        "m=video 0 RTP/AVP 96\r\na=control:track1\r\n"
        "a=rtpmap:96 H264/90000\r\n"
        "m=audio 0 RTP/AVP 0\r\na=control:track2\r\n"
        "a=rtpmap:0 PCMU/8000\r\n"
    )
    a = parse_sdp_audio(sdp)
    assert a["codec"] == "PCMU" and a["rate"] == 8000 and a["pt"] == 0, a
    assert a["control"] == "track2", a  # the AUDIO track's control, not video's
    print("  ok  SDP audio track (codec, rate, control) read from the stream")


def test_sdp_static_payload_type_without_rtpmap():
    # A stream may omit rtpmap for a static PT (RFC 3551); PCMU=0 is still known.
    a = parse_sdp_audio("v=0\r\nm=audio 0 RTP/AVP 0\r\na=control:t\r\n")
    assert a["codec"] == "PCMU" and a["rate"] == 8000, a
    print("  ok  static payload type resolves without an rtpmap")


def test_sdp_with_no_audio_track_fails_loudly():
    for bad, why in [
        ("v=0\r\nm=video 0 RTP/AVP 96\r\na=rtpmap:96 H264/90000\r\n", "video only"),
        ("v=0\r\ns=x\r\n", "no media"),
    ]:
        try:
            parse_sdp_audio(bad)
        except RtspListenError:
            continue
        raise AssertionError(f"a {why} SDP was accepted as having audio")
    print("  ok  a stream with no audio track raises instead of guessing")


# --------------------------------------------------------------------------
# Interleaved demux -- RTSP replies and RTP frames share the channel
# --------------------------------------------------------------------------

def _frame(channel, payload):
    return bytes([0x24, channel, (len(payload) >> 8) & 0xFF, len(payload) & 0xFF]) + payload


def test_interleaved_demux_splits_rtp_and_rtsp():
    reply = b"RTSP/1.0 200 OK\r\nCSeq: 2\r\nContent-Length: 3\r\n\r\nabc"
    buf = bytearray(_frame(0, b"\x01\x02\x03") + reply + _frame(0, b"\xaa\xbb"))
    msgs = list(iter_interleaved(buf))
    assert msgs[0] == ("rtp", 0, b"\x01\x02\x03"), msgs
    assert msgs[1][0] == "rtsp" and "200 OK" in msgs[1][1] and msgs[1][1].endswith("abc"), msgs
    assert msgs[2] == ("rtp", 0, b"\xaa\xbb"), msgs
    assert not buf, "fully-consumed buffer should be empty"
    print("  ok  demux splits interleaved RTP frames from RTSP text replies")


def test_interleaved_demux_holds_partial_frames():
    # A frame split across reads must not be mis-parsed; it stays buffered.
    full = _frame(0, b"\x01\x02\x03\x04\x05")
    buf = bytearray(full[:3])                       # header truncated
    assert list(iter_interleaved(buf)) == [], "a partial header yielded a message"
    assert len(buf) == 3, "partial data was consumed"
    buf.extend(full[3:6])                            # still short of the payload
    assert list(iter_interleaved(buf)) == [], "a partial payload yielded a message"
    buf.extend(full[6:])                             # now complete
    assert list(iter_interleaved(buf)) == [("rtp", 0, b"\x01\x02\x03\x04\x05")]
    print("  ok  a frame split across reads is held until complete")


def test_rtp_payload_strips_header_and_csrc_and_extension():
    # 12-byte header, no CSRC/ext: payload is the rest.
    pkt = bytes([0x80, 0x00]) + b"\x00" * 10 + b"AUDIO"
    assert rtp_payload(pkt) == b"AUDIO"
    # 2 CSRC (cc=2): +8 bytes before payload.
    pkt = bytes([0x82, 0x00]) + b"\x00" * 10 + b"\x00" * 8 + b"PAY"
    assert rtp_payload(pkt) == b"PAY", "CSRC list not skipped -- header decodes as audio"
    # extension bit set: +4 header + ext_words*4.
    pkt = bytes([0x90, 0x00]) + b"\x00" * 10 + b"\x00\x00\x00\x01" + b"\x00" * 4 + b"X"
    assert rtp_payload(pkt) == b"X", "extension not skipped"
    assert rtp_payload(b"\x00" * 8) is None, "a too-short packet must be rejected"
    print("  ok  RTP payload extraction skips header, CSRC and extension")


# --------------------------------------------------------------------------
# Aggregate control URL -- SETUP and PLAY go to the Content-Base, not the
# DESCRIBE request URL (the 454/404 that made the live call carry no audio)
# --------------------------------------------------------------------------

def test_play_targets_content_base_not_the_request_url():
    # The real VDB750 shape: DESCRIBE is answered on a "?query" URL, but the
    # media live under a "/path/" Content-Base. PLAY/SETUP on the request URL
    # 404s the media the server just described; only the base plays.
    describe = ("RTSP/1.0 200 OK\r\nCSeq: 2\r\n"
                "Content-Base: rtsp://h:41925/api/GetStream/\r\n"
                "Content-Type: application/sdp\r\n\r\nv=0\r\n")
    req = "rtsp://h:41925/api/GetStream?streamID=0"
    assert content_base_uri(req, describe) == "rtsp://h:41925/api/GetStream/", (
        "Content-Base was not preferred over the request URL -- PLAY would 404")
    # Header match is case-insensitive and anchored to a header line.
    assert content_base_uri(req, "rtsp/1.0 200\r\ncontent-base: rtsp://h/b/\r\n\r\n") \
        == "rtsp://h/b/"
    # No Content-Base -> fall back to the request URL (a compliant server).
    assert content_base_uri(req, "RTSP/1.0 200 OK\r\n\r\nv=0\r\n") == req
    print("  ok  PLAY/SETUP target the Content-Base, not the DESCRIBE request URL")


def test_track_uri_resolves_relative_and_absolute_control():
    base = "rtsp://h:41925/api/GetStream/"
    assert track_uri(base, "stream=1") == "rtsp://h:41925/api/GetStream/stream=1"
    # a base without a trailing slash still joins with exactly one
    assert track_uri("rtsp://h/api/GetStream", "stream=1") \
        == "rtsp://h/api/GetStream/stream=1"
    # an absolute control URL wins outright
    assert track_uri(base, "rtsp://h/api/GetStream/stream=1") \
        == "rtsp://h/api/GetStream/stream=1"
    print("  ok  track control resolves against the base (relative and absolute)")


# --------------------------------------------------------------------------
# The shaper -- clarity without latency or distortion
# --------------------------------------------------------------------------

def _s16(samples):
    b = bytearray(len(samples) * 2)
    for i, s in enumerate(samples):
        b[2 * i] = s & 0xFF
        b[2 * i + 1] = (s >> 8) & 0xFF
    return bytes(b)


def _unpack(pcm):
    return [int.from_bytes(pcm[i:i + 2], "little", signed=True)
            for i in range(0, len(pcm), 2)]


def test_shaper_unit_gain_no_highpass_is_exact_passthrough():
    sh = Shaper(highpass_hz=0, agc_max_gain=1.0, agc_min_gain=1.0)
    assert sh.passthrough, "unit-gain no-highpass shaper must declare passthrough"
    data = _s16([0, 1000, -2000, 32767, -32768, 500])
    assert sh.process(data) == data, "passthrough altered the samples"
    print("  ok  a unit-gain, no-highpass shaper is exact passthrough")


def test_shaper_agc_lifts_quiet_speech_toward_target():
    # A steady quiet tone should be brought UP (gain > 1) toward the target.
    sh = Shaper(highpass_hz=0, agc_target=0.5, agc_max_gain=8.0)
    quiet = _s16([1000, -1000] * 400)              # ~0.03 amplitude
    out = _unpack(sh.process(quiet))
    peak_in, peak_out = 1000, max(abs(x) for x in out[200:])  # after gain settles
    assert peak_out > peak_in * 2, (
        f"AGC did not lift quiet audio: {peak_in} -> {peak_out}")
    assert peak_out <= 32767, "AGC overshot full scale"
    print(f"  ok  AGC lifts quiet speech ({peak_in} -> {peak_out})")


def test_shaper_never_clips_a_loud_signal():
    sh = Shaper(highpass_hz=0, agc_target=0.5, agc_max_gain=8.0)
    loud = _s16([30000, -30000] * 400)
    out = _unpack(sh.process(loud))
    assert max(abs(x) for x in out) <= 32767, "output clipped"
    # and it should be pulled DOWN toward the target, not amplified
    assert max(abs(x) for x in out[200:]) < 30000, "loud audio was not limited"
    print("  ok  AGC limits a loud signal instead of clipping it")


def test_shaper_highpass_removes_dc():
    sh = Shaper(highpass_hz=150, agc_max_gain=1.0, agc_min_gain=1.0)
    dc = _s16([8000] * 800)
    out = _unpack(sh.process(dc))
    assert abs(out[-1]) < 800, f"high-pass did not remove the DC offset: {out[-1]}"
    print("  ok  high-pass removes a DC offset")


def test_ulaw_decode_is_correct_and_inverts_the_encoder():
    # Anchors: 0xFF is +0 (digital silence), 0x00 is the most-negative code,
    # 0x80 the most-positive. (0x7F is mu-law -0, i.e. ~0 -- a classic trap.)
    assert _ULAW_DECODE[0xFF] == 0, "mu-law silence did not decode to 0"
    assert _ULAW_DECODE[0x00] < -30000, "0x00 is the most-negative mu-law code"
    assert _ULAW_DECODE[0x80] > 30000, "0x80 is the most-positive mu-law code"
    # The load-bearing property: the listen decode and the uplink/downlink
    # encode must be inverse-consistent, or a round-tripped call distorts.
    from bridge.sip_call import pcm_s16le_to_mulaw, mulaw_to_pcm_s16le
    # Value-preserving round-trip for all 256 codes: decode -> encode -> decode
    # returns the same sample. (Checked at the VALUE level, not the byte level,
    # because mu-law has two zero codes -- 0xFF +0 and 0x7F -0 -- so the -0 code
    # canonicalizes to +0 on re-encode; both are silence, so audio is preserved.)
    for b in range(256):
        val = mulaw_to_pcm_s16le(bytes([b]))
        reencoded = pcm_s16le_to_mulaw(val)
        assert mulaw_to_pcm_s16le(reencoded) == val, (
            f"decode/encode/decode changed the value of {b:#04x} -- tables disagree")
    print("  ok  mu-law decode is correct and value-consistent with the encoder")



def test_gate_stops_the_agc_amplifying_background_noise():
    # An ungated AGC rides quiet background UP to the target during speech
    # pauses -- measured on a live call as porch noise louder than the visitor.
    # With the gate: sub-threshold audio comes out ATTENUATED, and speech-level
    # audio still gets lifted.
    def peaks(sh, amp, n=8000):
        data = _s16([amp, -amp] * (n // 2))
        out = _unpack(sh.process(data))
        return max(abs(x) for x in out[n // 2:])
    gated = Shaper(highpass_hz=0, agc_target=0.7, agc_max_gain=16.0,
                   gate_threshold=0.015, gate_atten=0.15)
    noise_amp = 160                                  # ~0.005, below the gate
    noise_peak = peaks(gated, noise_amp)
    assert noise_peak < noise_amp, (
        f"background noise was amplified through the gate ({noise_amp} -> "
        f"{noise_peak}) -- pauses will roar on the phone")
    speech_peak = peaks(gated, 3300)                 # ~0.1, well above the gate
    assert speech_peak > 3300 * 2, (
        f"speech was not lifted ({speech_peak}) -- the gate is eating the voice")
    # threshold 0 keeps the gate fully out of the path
    ungated = Shaper(highpass_hz=0, agc_target=0.7, agc_max_gain=16.0,
                     gate_threshold=0.0)
    assert peaks(ungated, noise_amp) > noise_amp, "gate off should mean AGC-only"
    print("  ok  gate attenuates sub-threshold noise, passes and lifts speech")



def test_fixed_gain_profile_has_no_dynamics():
    # min_gain == max_gain == G is a FIXED gain: after four live rounds the
    # dynamics chain (hot AGC + gate) was heard doing harm on the real mic, so
    # the default uplink profile is a plain fixed gain -- and the Shaper must
    # honour min_gain above 1 for that to exist at all (it used to clamp it).
    sh = Shaper(highpass_hz=0, agc_target=0.7, agc_max_gain=4.0, agc_min_gain=4.0)
    # Gain starts at 1 and slews to the fixed value over the release time, so
    # settle on 2 s of audio and judge the tail.
    data = _s16([2000, -2000] * 8000)                # ~0.06: ordinary speech
    out = _unpack(sh.process(data))
    tail = out[len(out) // 2:]
    peak = max(abs(x) for x in tail)
    assert 7900 <= peak <= 8000, (
        f"fixed gain 4 not applied (settled peak {peak}, expected ~8000) -- "
        f"dynamics crept back into the fixed profile")
    # Louder input where an AGC would ride gain DOWN: fixed gain must not.
    loud = _s16([6000, -6000] * 8000)
    peak = max(abs(x) for x in _unpack(sh.process(loud))[8000:])
    assert 23700 <= peak <= 24000, (
        f"gain moved on loud input ({peak}, expected ~24000) -- that is an AGC")
    print("  ok  fixed-gain profile applies G verbatim, no dynamics")


TESTS = (
    test_sdp_audio_is_read_from_the_stream_not_assumed,
    test_sdp_static_payload_type_without_rtpmap,
    test_sdp_with_no_audio_track_fails_loudly,
    test_interleaved_demux_splits_rtp_and_rtsp,
    test_interleaved_demux_holds_partial_frames,
    test_rtp_payload_strips_header_and_csrc_and_extension,
    test_play_targets_content_base_not_the_request_url,
    test_track_uri_resolves_relative_and_absolute_control,
    test_shaper_unit_gain_no_highpass_is_exact_passthrough,
    test_shaper_agc_lifts_quiet_speech_toward_target,
    test_shaper_never_clips_a_loud_signal,
    test_shaper_highpass_removes_dc,
    test_ulaw_decode_is_correct_and_inverts_the_encoder,
    test_gate_stops_the_agc_amplifying_background_noise,
    test_fixed_gain_profile_has_no_dynamics,
)


def main():
    failures = 0
    for t in TESTS:
        try:
            t()
        except AssertionError as exc:
            print(f"  FAIL {t.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {t.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
