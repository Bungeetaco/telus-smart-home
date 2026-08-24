"""Tests for bridge/aiortc_opus_patch.py -- the Opus decoder's survival of a bad packet.

The invisible failure this suite exists to catch: **a camera whose published
audio track is pure silence while every health signal says the stream is fine.**

aiortc's `OpusDecoder.decode` has no error handling (its H.264 twin does), and
neither does `rtcrtpreceiver.decoder_worker`, which is a THREAD TARGET. So one
malformed Opus packet does not cost a packet -- it kills the session's
`audio-decoder` thread permanently. Nothing above notices: the receiver gates on
the Thread OBJECT, which stays truthy after the thread dies, so RTP keeps being
queued to nobody; the frame-silence watchdog is video-only; and the sink fills
the gap with silence, so the RTSP path keeps carrying a perfectly valid,
perfectly silent AAC track. Measured live before the patch: 9,572,352 of
9,572,352 samples at the s16 noise floor, 11 sessions, 11 dead threads.

Everything here runs against the REAL aiortc and the REAL libopus -- the point
is what that decoder does with these bytes, which a stand-in cannot tell us. No
network, no account, no hardware. The valid packet is produced by aiortc's own
OpusEncoder rather than captured: it is the same libopus round-tripping itself,
not an invented wire shape. The invalid packets are the two byte sequences the
investigation established as reproducers -- an Ogg `OpusHead`/`OpusTags`
identification header, and H.264 NAL bytes arriving on the Opus payload type.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_opus_decoder_patch.py
"""
import asyncio
import importlib.util
import inspect
import logging
import os
import pathlib
import queue
import sys
import threading
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

import av  # noqa: E402
import aiortc.codecs  # noqa: E402
from av import AudioFrame  # noqa: E402
from aiortc.codecs.opus import OpusDecoder, OpusEncoder  # noqa: E402
from aiortc.jitterbuffer import JitterFrame  # noqa: E402
from aiortc.rtcrtpparameters import RTCRtpCodecParameters  # noqa: E402
from aiortc.rtcrtpreceiver import decoder_worker  # noqa: E402

from bridge import aiortc_opus_patch  # noqa: E402  (applies the patch)

PATCH_PATH = str(pathlib.Path(aiortc_opus_patch.__file__).resolve())

# An Ogg Opus identification header: TOC byte 0x4f is frame-count code 3, which
# libopus rejects outright. This is the leading (still UNVERIFIED) hypothesis for
# what the doorbell actually sends -- a stream-start header on the audio payload
# type.
OPUS_HEAD = b"OpusHead" + bytes([1, 2, 0x80, 0xBB, 0, 0, 0, 0, 0])
# H.264 SPS/IDR NAL bytes -- the other reproducer, i.e. video bytes arriving on
# the Opus payload type.
H264_NAL = bytes([0x67, 0x42, 0xC0, 0x1E, 0xD9, 0x00, 0xF0, 0x11, 0x7E, 0xF0])

OPUS_CODEC = RTCRtpCodecParameters(
    mimeType="audio/opus", clockRate=48000, channels=2, payloadType=111)


def _valid_opus_packet() -> bytes:
    """One real 20 ms stereo Opus packet, encoded here by libopus itself."""
    encoder = OpusEncoder()
    frame = AudioFrame(format="s16", layout="stereo", samples=960)
    frame.sample_rate = 48000
    frame.pts = 0
    for plane in frame.planes:
        plane.update(bytes(plane.buffer_size))
    payloads, _ = encoder.encode(frame)
    assert payloads, "libopus produced no packet -- the fixture builder is broken"
    return payloads[0]


GOOD = _valid_opus_packet()


def test_a_rejected_packet_is_dropped_not_fatal():
    """The patch itself: `decode` returns no frames instead of raising, and the
    SAME decoder object decodes the very next packet normally.

    "Usable for the next packet" is the whole claim. A wrapper that swallowed the
    error but left the codec context poisoned would look identical from the
    outside and still publish silence forever."""
    for name, bad in (("OpusHead", OPUS_HEAD), ("H264 NAL", H264_NAL)):
        decoder = OpusDecoder()
        # Confirm the crafted packet really is one libopus rejects; a fixture
        # that quietly became decodable would make the rest of this vacuous.
        raw = aiortc_opus_patch._orig_decode
        try:
            raw(decoder, JitterFrame(data=bad, timestamp=0))
            raise AssertionError(
                f"{name} no longer makes libopus raise -- this fixture no longer "
                "reproduces the failure the patch exists for")
        except av.FFmpegError:
            pass

        dropped = decoder.decode(JitterFrame(data=bad, timestamp=0))
        assert dropped == [], (
            f"{name}: the patched decode returned {dropped!r} instead of dropping "
            "the packet")
        frames = decoder.decode(JitterFrame(data=GOOD, timestamp=960))
        assert frames and frames[0].samples == 960, (
            f"{name}: the decoder was left unusable for the NEXT packet "
            f"({frames!r}) -- dropping the packet is not enough, the codec "
            "context has to survive it")
    print("  ok  bad packet dropped (OpusHead + H264 NAL), decoder still decodes "
          "the next real packet")


def test_the_drop_is_logged_once_per_session_with_the_bytes():
    """Once per DECODER, which is once per session -- and carrying the bytes.

    aiortc builds a fresh OpusDecoder per RTP receiver, so per-instance is
    per-session. The stream runs at ~50 packets/s: a per-packet WARNING would be
    its own outage, and a silent drop would leave the still-unidentified packet
    unidentified forever. The length and first 16 bytes are the whole point of
    the line."""
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("bridge.aiortc_opus_patch")
    handler = _Capture()
    log.addHandler(handler)
    try:
        first = OpusDecoder()
        for _ in range(3):
            first.decode(JitterFrame(data=OPUS_HEAD, timestamp=0))
        second = OpusDecoder()
        second.decode(JitterFrame(data=OPUS_HEAD, timestamp=0))
    finally:
        log.removeHandler(handler)

    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, (
        f"expected one warning per decoder (2), got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]} -- three drops on one session "
        "must not produce three lines")
    message = warnings[0].getMessage()
    assert f"len={len(OPUS_HEAD)}" in message, message
    assert OPUS_HEAD[:16].hex() in message, (
        f"the offending bytes are not in the log line, so the packet stays "
        f"unidentified: {message}")
    print(f"  ok  logged once per decoder, with the bytes: {message}")


async def test_the_audio_decoder_thread_survives_a_bad_packet():
    """The production failure, end to end, in aiortc's own thread.

    This runs the REAL `rtcrtpreceiver.decoder_worker` -- the function whose
    unhandled exception is what actually died -- on a real thread, feeds it the
    bad packet followed by a good one, and requires that the good frame comes
    out and the thread is still alive. Before the patch the thread raises out of
    its `while True` on the first packet and nothing ever arrives again; it is
    started exactly once per receiver, so there is no recovery for the life of
    the session."""
    loop = asyncio.get_running_loop()
    input_q: queue.Queue = queue.Queue()
    output_q: asyncio.Queue = asyncio.Queue()
    thread = threading.Thread(
        target=decoder_worker, args=(loop, input_q, output_q),
        name="audio-decoder-under-test", daemon=True)
    thread.start()
    try:
        input_q.put((OPUS_CODEC, JitterFrame(data=OPUS_HEAD, timestamp=0)))
        input_q.put((OPUS_CODEC, JitterFrame(data=GOOD, timestamp=960)))
        try:
            frame = await asyncio.wait_for(output_q.get(), timeout=5.0)
        except asyncio.TimeoutError:
            raise AssertionError(
                "no decoded frame arrived after a malformed packet -- the "
                f"audio-decoder thread died (alive={thread.is_alive()}), which "
                "is the whole outage: RTP keeps being queued to a dead thread "
                "and the published track is silence for the life of the session")
        assert frame is not None and getattr(frame, "samples", 0) == 960, (
            f"the worker emitted {frame!r} rather than the decoded frame")
        assert thread.is_alive(), (
            "the audio-decoder thread is no longer running after one bad packet")
    finally:
        input_q.put(None)
        thread.join(timeout=5.0)
    print("  ok  aiortc's real decoder_worker thread survives a bad packet and "
          "keeps delivering frames")


# --------------------------------------------------------------------------
# The compatibility guard
# --------------------------------------------------------------------------

def _exec_patch_against(fake_opus):
    """Execute a fresh copy of the patch module against a stand-in
    `aiortc.codecs.opus`, leaving the real one alone.

    `from aiortc.codecs import opus` resolves through the package attribute, so
    swapping that attribute is enough -- and it means these guard cases never
    touch the decoder the rest of this suite (and the running bridge) uses."""
    real = aiortc.codecs.opus
    aiortc.codecs.opus = fake_opus
    try:
        spec = importlib.util.spec_from_file_location("_opus_patch_probe", PATCH_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        aiortc.codecs.opus = real


def _fake_opus(decode=None, with_class=True):
    module = types.ModuleType("aiortc.codecs.opus")
    if with_class:
        namespace = {} if decode is None else {"decode": decode}
        module.OpusDecoder = type("OpusDecoder", (), namespace)
    return module


def test_the_guard_fires_when_the_upstream_shape_changes():
    """A monkeypatch that silently stops applying is worse than no monkeypatch.

    Three ways this patch can become wrong, all of which must raise at IMPORT --
    where CI's bridge-import-smoke job (it imports every bridge module) sees
    them -- rather than at the first doorbell press:

    1. aiortc renames/removes `OpusDecoder`, or moves `decode` off the class:
       the patch would apply to nothing.
    2. The module is executed twice: wrappers stack, and "once per session"
       silently becomes "once per wrapper".
    3. Upstream grows its own exception handling -- most likely because they
       FIXED this -- in which case this module must be read and deleted, not
       layered over a handler that already returns [].

    The fourth case is the control: an aiortc shaped exactly like 1.15.0 must
    still be patched, and the replacement must actually swallow an FFmpegError.
    A guard that raised unconditionally would pass 1-3 and prove nothing."""
    def _expect(fake, fragment, why):
        try:
            _exec_patch_against(fake)
        except RuntimeError as exc:
            assert fragment in str(exc), (
                f"{why}: the guard fired but does not name what moved: {exc}")
            return str(exc)
        raise AssertionError(
            f"{why}: the patch applied silently instead of failing loudly")

    def _pristine_decode(self, encoded_frame):
        return [encoded_frame]

    def _upstream_fixed_decode(self, encoded_frame):
        try:
            return [encoded_frame]
        except av.FFmpegError:
            return []

    marked = _pristine_decode
    already = types.FunctionType(marked.__code__, marked.__globals__, "decode")
    setattr(already, aiortc_opus_patch._PATCH_MARKER, True)

    _expect(_fake_opus(with_class=False), "OpusDecoder is missing",
            "the decoder class disappeared")
    _expect(_fake_opus(decode=None), "decode is missing",
            "decode moved off the class")
    _expect(_fake_opus(decode=already), "already been applied",
            "the module was executed twice")
    _expect(_fake_opus(decode=_upstream_fixed_decode), "own exception handling",
            "upstream added its own handler")

    # The control: an unmodified aiortc still gets patched, and the wrapper does
    # the job.
    def _always_raises(self, encoded_frame):
        raise av.error.InvalidDataError(1094995529, "Invalid data", "packet")

    fake = _fake_opus(decode=_always_raises)
    _exec_patch_against(fake)
    patched = fake.OpusDecoder.decode
    assert getattr(patched, aiortc_opus_patch._PATCH_MARKER, False), (
        "an aiortc shaped like the pinned one was not patched -- the guard is "
        "refusing everything, which would pass the cases above for free")
    assert patched(fake.OpusDecoder(), JitterFrame(data=b"\x00", timestamp=0)) == [], (
        "the replacement did not swallow an FFmpegError")
    print("  ok  guard fires on all three incompatible shapes and still patches "
          "an unmodified aiortc")


def main():
    failures = 0
    tests = (test_a_rejected_packet_is_dropped_not_fatal,
             test_the_drop_is_logged_once_per_session_with_the_bytes,
             test_the_audio_decoder_thread_survives_a_bad_packet,
             test_the_guard_fires_when_the_upstream_shape_changes)
    for test in tests:
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
