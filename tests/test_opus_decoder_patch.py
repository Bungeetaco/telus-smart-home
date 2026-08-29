"""Tests for bridge/aiortc_opus_patch.py -- decoding the codec that ARRIVES, and
surviving a packet nothing can read.

The invisible failure this suite exists to catch: **a camera whose published
audio track carries almost no real audio while every health signal says the
stream is fine.** It has now happened twice, in two different shapes, and both
are pinned here.

SHAPE ONE -- a dead decoder thread. aiortc's `OpusDecoder.decode` has no error
handling (its H.264 twin does), and neither does `rtcrtpreceiver.decoder_worker`,
which is a THREAD TARGET. So one undecodable packet does not cost a packet -- it
kills the session's `audio-decoder` thread permanently. Nothing above notices:
the receiver gates on the Thread OBJECT, which stays truthy after the thread
dies, so RTP keeps being queued to nobody; the frame-silence watchdog is
video-only; and the sink fills the gap with silence, so the RTSP path keeps
carrying a perfectly valid, perfectly silent AAC track. Measured live: 9,572,352
of 9,572,352 samples at the s16 noise floor, 11 sessions, 11 dead threads.

SHAPE TWO -- the codec mislabel, which the survival wrapper for shape one then
HID. The Janus offer advertises `audio(port=9,opus)` and the bytes are G.711
mu-law at 8 kHz, so libopus rejected most of them and the wrapper dropped them.
Its log line fires once per session, which reads like an occasional glitch, so
for two days the doorbell decoded 181 audio frames per 60 s and the living room
268 -- roughly 6 to 9 percent of a timeline that should be 50 frames a second --
and the rest of the published audio was the sink's silence fill.

Everything here runs against the REAL aiortc and the REAL libopus and the REAL
libavcodec mu-law decoder -- the point is what those decoders do with these
bytes, which a stand-in cannot tell us. No network, no account, no hardware. The
valid Opus packet is produced by aiortc's own OpusEncoder rather than captured:
it is the same libopus round-tripping itself, not an invented wire shape. The
mu-law payloads are the head bytes actually captured from each camera's dropped
packets, tiled to a packet length (the capture kept the first 16 bytes, which is
what the drop log carries).

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
from bridge.ffmpeg_sink import audio_format_of  # noqa: E402

PATCH_PATH = str(pathlib.Path(aiortc_opus_patch.__file__).resolve())
SNIFF = aiortc_opus_patch.SNIFF_PACKETS

# The doorbell's captured payload head. Alternating 0x7e/0x7f/0xfe/0xff is
# G.711 mu-law encoding near-silence, which is what a doorbell's microphone
# produces nearly all the time.
DOORBELL_MULAW = bytes.fromhex("7effff7f7efe7ffffffffefefeff7efe") * 10
# The living room camera's, from the same account on the same day: ordinary
# varying audio as mu-law rather than near-silence.
LIVING_ROOM_MULAW = bytes.fromhex("332038a8a9bd2223989e1e289e9f3a1d") * 10
# G.711 mu-law bytes that libopus ACCEPTS. Both cameras send payloads like this
# and the acceptance is not hypothetical: on 2026-08-28 the live doorbell sniffed
# 15 and 16 of 20 packets as Opus across six sessions, while mu-law read 20 of
# 20 in every one of them. A mu-law payload is any byte string, so nothing stops
# its first byte being a valid Opus TOC (0x20 is config 4, mono, code 0), and
# once libopus accepts the TOC it will accept the frame. Decoded as what it is,
# this payload is 160 samples of 8 kHz mono -- the same shape as every other
# payload here.
MULAW_LIBOPUS_ACCEPTS = bytes.fromhex("20fe387e7fa97f20bd7ea9ff7e7f3838") * 10

# An Ogg Opus identification header: TOC byte 0x4f is frame-count code 3, which
# libopus rejects outright. Not what the cameras send (that is the mu-law above)
# but a second, differently-shaped reproducer of the same rejection, and unlike
# mu-law the mu-law decoder has no special claim on it either.
OPUS_HEAD = b"OpusHead" + bytes([1, 2, 0x80, 0xBB, 0, 0, 0, 0, 0])

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


def _feed(decoder, packets):
    """Every frame `decoder` emitted for `packets`, in order, with the index of
    the packet that produced them."""
    out = []
    for index, data in enumerate(packets):
        for frame in decoder.decode(JitterFrame(data=data, timestamp=160 * index)):
            out.append((index, frame))
    return out


def _committed(decoder):
    return getattr(decoder, aiortc_opus_patch._STATE_ATTR).committed


def test_a_mulaw_stream_is_decoded_as_mulaw_not_discarded():
    """The whole point: bytes that ARE mu-law reach the publisher as audio.

    This is the two-day outage, in one assertion. Fed the doorbell's own
    payloads, the shipped decoder produced a frame for 6 percent of them and
    dropped the rest; every dropped packet's span was published as silence fill,
    which is why it sounded like fragments jumping around rather than like a
    camera with no microphone. Both cameras' captured payloads are used, because
    they are different signals (near-silence and ordinary speech-level audio)
    and only one of them was ever seen in a log line.

    The counterfactual is asserted first: libopus really does reject these
    bytes, so a decoder that believed the SDP really would drop them. A fixture
    that quietly became Opus-decodable would make the rest of this vacuous.
    """
    for name, payload in (("doorbell", DOORBELL_MULAW),
                          ("living room", LIVING_ROOM_MULAW)):
        probe = OpusDecoder()
        try:
            aiortc_opus_patch._orig_decode(probe, JitterFrame(data=payload, timestamp=0))
            raise AssertionError(
                f"{name}: libopus now accepts this payload, so it no longer "
                "reproduces the mislabel the sniff exists for")
        except av.FFmpegError:
            pass

        decoder = OpusDecoder()
        packets = [payload] * (SNIFF + 30)
        frames = _feed(decoder, packets)
        assert _committed(decoder) == aiortc_opus_patch.PCMU, (
            f"{name}: the session committed to {_committed(decoder)!r} instead of "
            "mu-law, so it is still decoding the advertised codec rather than "
            "the arriving one")
        assert len(frames) == len(packets), (
            f"{name}: {len(frames)} frames out of {len(packets)} packets -- the "
            "audio timeline is still mostly silence fill. Before the sniff this "
            "ratio was ~6%")
        shapes = {audio_format_of(f) for _, f in frames}
        assert shapes == {("s16", 8000, 1)}, (
            f"{name}: decoded frames are not 8 kHz mono s16, which is the shape "
            f"ffmpeg's audio input is built from: {shapes}")
        assert all(f.samples == len(payload) for _, f in frames), (
            f"{name}: a mu-law frame did not carry one sample per payload byte")
    print(f"  ok  both cameras' captured mu-law payloads decode 100% of packets "
          f"as 8kHz mono s16 (was ~6%)")


def test_a_real_opus_stream_still_commits_to_opus_and_loses_nothing():
    """The other half of "never make things worse".

    A camera that really does send Opus must be unaffected: it commits to Opus,
    every packet is decoded, and the frames are the 48 kHz stereo shape the rest
    of the pipeline was already built around. The sniff window may not cost it a
    single frame either -- the held frames are handed over at the commit, not
    dropped, which is why the decision is allowed to take 20 packets at all.
    """
    decoder = OpusDecoder()
    packets = [GOOD] * (SNIFF + 10)
    frames = _feed(decoder, packets)
    assert _committed(decoder) == aiortc_opus_patch.OPUS, (
        f"a real Opus stream was re-labelled as {_committed(decoder)!r}")
    assert len(frames) == len(packets), (
        f"{len(frames)} frames out of {len(packets)} Opus packets -- the sniff "
        "window is dropping audio it should be holding")
    shapes = {audio_format_of(f) for _, f in frames}
    assert shapes == {("s16", 48000, 2)}, shapes
    # ...and the held frames really were held, not simply decoded late: nothing
    # is emitted before the decision, and the whole window arrives with the
    # packet that makes it.
    held = [index for index, _ in frames if index < SNIFF - 1]
    assert not held, (
        f"frames escaped before the codec was committed (packets {sorted(set(held))}) "
        "-- a frame in the losing codec's shape is enough to launch ffmpeg with "
        "the wrong -ar/-ac")
    at_commit = [index for index, _ in frames].count(SNIFF - 1)
    assert at_commit == SNIFF, (
        f"the commit released {at_commit} frames, not the {SNIFF} the sniff held")
    print(f"  ok  a real Opus stream commits to Opus and every one of "
          f"{len(packets)} packets is delivered, {SNIFF} of them at the commit")


def test_no_frame_escapes_in_the_losing_codecs_shape():
    """Why the sniff HOLDS its frames instead of emitting as it goes.

    On a mu-law stream a small fraction of packets satisfy libopus by accident
    and decode as noise -- that fraction is exactly what the broken bridge was
    publishing. Emitting them would be worse than dropping them: a 48 kHz stereo
    frame ahead of an 8 kHz mono stream is what camera_session peeks at to size
    the ffmpeg audio input, and once the publisher is running it trips
    ffmpeg_sink.write_audio's format guard, which rebuilds the publisher and
    kicks every RTSP reader. Once per rotation, forever.

    So: a mu-law stream carrying one genuinely-Opus-decodable packet inside the
    sniff window must still commit to mu-law, and must never emit a 48 kHz
    frame -- not the accidental one, and not the real one that follows the
    commit either. The decision is made ONCE and held.
    """
    packets = [DOORBELL_MULAW] * SNIFF
    packets[3] = GOOD                      # the accidental Opus success
    packets += [DOORBELL_MULAW] * 5
    packets.append(GOOD)                   # ...and one after the commit
    decoder = OpusDecoder()
    frames = _feed(decoder, packets)

    assert _committed(decoder) == aiortc_opus_patch.PCMU, (
        f"one Opus-decodable packet in {SNIFF} flipped the decision to "
        f"{_committed(decoder)!r} -- the confidence threshold is not doing its job")
    shapes = {audio_format_of(f) for _, f in frames}
    assert shapes == {("s16", 8000, 1)}, (
        f"a frame escaped in the Opus decoder's 48 kHz stereo shape: {shapes}. "
        "That frame is what rebuilds the publisher on every rotation")
    assert len(frames) == len(packets), (
        f"{len(frames)} frames from {len(packets)} packets -- committing to "
        "mu-law must not cost the packets that happened to satisfy libopus too")
    print("  ok  an accidental Opus success inside a mu-law stream neither flips "
          "the decision nor escapes as a 48kHz frame")


def test_the_counters_account_for_every_packet():
    """The fact that could not be seen, made countable.

    The drop log is once per session by necessity (a per-packet WARNING on a
    ~50 packets/s stream is its own outage), and "once per session" is precisely
    how a WHOLESALE mismatch hid behind a line that reads like an occasional
    glitch. So every packet lands in exactly one of decoded_opus, decoded_pcmu
    and discarded, and /control/status serves the totals: a stream on the wrong
    codec puts nearly all of its packets in `discarded`, which no amount of log
    throttling can hide.
    """
    before = aiortc_opus_patch.decode_stats()

    opus_packets = [GOOD] * (SNIFF + 5)
    _feed(OpusDecoder(), opus_packets)
    mulaw_packets = [DOORBELL_MULAW] * (SNIFF + 7)
    _feed(OpusDecoder(), mulaw_packets)
    # A committed-Opus session that then meets packets libopus cannot read: the
    # only way to reach `discarded` once a session has committed.
    undecodable = [OPUS_HEAD] * 4
    _feed(OpusDecoder(), [GOOD] * SNIFF + undecodable)

    after = aiortc_opus_patch.decode_stats()
    counters = ("decoded_opus", "decoded_pcmu", "discarded",
                "sessions_opus", "sessions_pcmu")
    delta = {k: after[k] - before[k] for k in counters}
    total_packets = len(opus_packets) + len(mulaw_packets) + SNIFF + len(undecodable)

    assert delta["decoded_opus"] == len(opus_packets) + SNIFF, delta
    assert delta["decoded_pcmu"] == len(mulaw_packets), delta
    assert delta["discarded"] == len(undecodable), delta
    assert (delta["decoded_opus"] + delta["decoded_pcmu"] + delta["discarded"]
            == total_packets), (
        f"{total_packets} packets in, "
        f"{delta['decoded_opus'] + delta['decoded_pcmu'] + delta['discarded']} "
        f"accounted for: {delta}. A packet that lands in no bucket is a packet "
        "the counters cannot report missing")
    assert delta["sessions_opus"] == 2 and delta["sessions_pcmu"] == 1, delta
    assert after["sniff_packets"] == SNIFF, after
    assert after["opus_confidence"] == aiortc_opus_patch.OPUS_CONFIDENCE, after
    print(f"  ok  every packet is counted exactly once: {delta}")


def test_the_codec_is_derivable_from_a_decoded_frames_shape():
    """How /control/status names the codec per CAMERA.

    aiortc builds its decoder inside `decoder_worker`, a thread no
    `RTCRtpReceiver` keeps a reference to, so camera_session cannot ask the
    decoder what it committed to. It can ask the FRAMES, and that is exact
    rather than a guess: aiortc pins its Opus decoder to 48 kHz stereo and its
    mu-law decoder to 8 kHz mono, and the patch's import guard refuses to run if
    either rate moves. This pins the mapping against the real decoders' real
    output rather than against the constants.
    """
    pcmu = _feed(OpusDecoder(), [DOORBELL_MULAW] * SNIFF)
    opus = _feed(OpusDecoder(), [GOOD] * SNIFF)
    pcmu_shape = audio_format_of(pcmu[0][1])
    opus_shape = audio_format_of(opus[0][1])
    assert aiortc_opus_patch.codec_for_audio_format(pcmu_shape) == \
        aiortc_opus_patch.PCMU, (
        f"a frame the mu-law decoder produced ({pcmu_shape}) is named "
        f"{aiortc_opus_patch.codec_for_audio_format(pcmu_shape)!r} -- a status "
        "field that names the wrong codec is worse than no field")
    assert aiortc_opus_patch.codec_for_audio_format(opus_shape) == \
        aiortc_opus_patch.OPUS, (
        f"a frame the Opus decoder produced ({opus_shape}) is named "
        f"{aiortc_opus_patch.codec_for_audio_format(opus_shape)!r}")
    assert aiortc_opus_patch.codec_for_audio_format(None) is None, (
        "no frame observed yet must read as unknown, not as a codec")
    assert aiortc_opus_patch.codec_for_audio_format(("s16", 16000, 1)) is None, (
        "a shape neither decoder produces must read as unknown, not be forced "
        "into one of the two")
    print("  ok  a decoded frame's shape names the decoder that produced it")


def test_a_rejected_packet_is_dropped_not_fatal():
    """The original patch, still the floor underneath the new decision.

    `decode` returns no frames instead of raising, and the SAME decoder object
    decodes the very next packet normally. "Usable for the next packet" is the
    whole claim: a wrapper that swallowed the error but left the codec context
    poisoned would look identical from the outside and still publish silence
    forever. Asserted after the session has committed to Opus, which is the
    state a genuine Opus camera spends its life in.
    """
    for name, bad in (("OpusHead", OPUS_HEAD), ("doorbell mu-law", DOORBELL_MULAW)):
        decoder = OpusDecoder()
        _feed(decoder, [GOOD] * SNIFF)
        assert _committed(decoder) == aiortc_opus_patch.OPUS, name

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
    print("  ok  a committed Opus session drops a bad packet and still decodes "
          "the next real one")


def test_the_decision_and_the_drops_are_logged_once_per_session():
    """One commit line per session, and a drop line that cannot flood.

    aiortc builds a fresh OpusDecoder per RTP receiver, so per-instance is
    per-session. The stream runs at ~50 packets/s: a per-packet WARNING would be
    its own outage. The commit line has to name the codec and the evidence, and
    the drop line has to carry the length and first bytes -- that is what
    identified the payload as mu-law in the first place, and what would show a
    THIRD shape turning up.
    """
    records = []

    class _Capture(logging.Handler):
        def emit(self, record):
            records.append(record)

    log = logging.getLogger("bridge.aiortc_opus_patch")
    handler = _Capture()
    log.addHandler(handler)
    # The commit line is INFO, and the bridge runs at INFO in the container; a
    # test that left the root logger's default WARNING in place would silently
    # assert nothing about it.
    previous_level = log.level
    log.setLevel(logging.INFO)
    try:
        mulaw = OpusDecoder()
        _feed(mulaw, [DOORBELL_MULAW] * (SNIFF + 10))
        first = OpusDecoder()
        _feed(first, [GOOD] * SNIFF + [OPUS_HEAD] * 3)
        second = OpusDecoder()
        _feed(second, [GOOD] * SNIFF + [OPUS_HEAD])
    finally:
        log.removeHandler(handler)
        log.setLevel(previous_level)

    commits = [r.getMessage() for r in records if r.levelno == logging.INFO]
    assert len(commits) == 3, (
        f"expected one commit line per session (3), got {len(commits)}: {commits}")
    assert "mu-law" in commits[0] and str(SNIFF) in commits[0], commits[0]
    assert "Opus" in commits[1], commits[1]

    warnings = [r.getMessage() for r in records if r.levelno == logging.WARNING]
    assert len(warnings) == 2, (
        f"expected one drop warning per session (2), got {len(warnings)}: "
        f"{warnings} -- three drops on one session must not produce three lines")
    assert f"len={len(OPUS_HEAD)}" in warnings[0], warnings[0]
    assert OPUS_HEAD[:16].hex() in warnings[0], (
        f"the offending bytes are not in the log line, so the packet stays "
        f"unidentified: {warnings[0]}")
    assert "discarded" in warnings[0], (
        "the drop line does not point at the counter that shows the running "
        f"total, which is the whole reason one line per session was misread: "
        f"{warnings[0]}")
    print(f"  ok  one commit line and one drop line per session, with the bytes")


async def test_the_audio_decoder_thread_survives_a_bad_packet():
    """The production failure, end to end, in aiortc's own thread.

    This runs the REAL `rtcrtpreceiver.decoder_worker` -- the function whose
    unhandled exception is what actually died -- on a real thread, feeds it the
    bad packet followed by real ones, and requires that frames come out and the
    thread is still alive. Before the patch the thread raises out of its
    `while True` on the first packet and nothing ever arrives again; it is
    started exactly once per receiver, so there is no recovery for the life of
    the session.

    It also proves the whole path works through aiortc rather than only through
    a decoder built by hand here: the packets are fed as `(codec, frame)` tasks
    exactly as `RTCRtpReceiver._handle_rtp_packet` feeds them.
    """
    loop = asyncio.get_running_loop()
    input_q: queue.Queue = queue.Queue()
    output_q: asyncio.Queue = asyncio.Queue()
    thread = threading.Thread(
        target=decoder_worker, args=(loop, input_q, output_q),
        name="audio-decoder-under-test", daemon=True)
    thread.start()
    try:
        input_q.put((OPUS_CODEC, JitterFrame(data=OPUS_HEAD, timestamp=0)))
        for index in range(SNIFF):
            input_q.put((OPUS_CODEC, JitterFrame(data=GOOD, timestamp=960 * index)))
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

        # ...and the same worker, given the doorbell's real payloads, delivers
        # mu-law frames rather than dropping them.
        for index in range(SNIFF):
            input_q.put((OPUS_CODEC,
                         JitterFrame(data=DOORBELL_MULAW, timestamp=160 * index)))
        got = []
        try:
            while len(got) < SNIFF:
                got.append(await asyncio.wait_for(output_q.get(), timeout=5.0))
        except asyncio.TimeoutError:
            pass
        # The Opus commit above is this decoder instance's decision for the life
        # of the session, so the mu-law that follows is dropped, not decoded --
        # which is the invariant, not a defect: one codec per session.
        assert not any(audio_format_of(f) == ("s16", 8000, 1) for f in got), (
            "a committed session changed codec mid-stream")
        assert thread.is_alive(), (
            "the audio-decoder thread died on the mu-law that followed")
    finally:
        input_q.put(None)
        thread.join(timeout=5.0)
    print("  ok  aiortc's real decoder_worker thread survives a bad packet, keeps "
          "delivering frames, and holds one codec for the session")


# --------------------------------------------------------------------------
# The compatibility guard
# --------------------------------------------------------------------------

def _exec_patch_against(fake_opus, fake_g711=None):
    """Execute a fresh copy of the patch module against stand-in
    `aiortc.codecs.opus` / `aiortc.codecs.g711` modules, leaving the real ones
    alone.

    `from aiortc.codecs import opus` resolves through the package attribute, so
    swapping that attribute is enough -- and it means these guard cases never
    touch the decoders the rest of this suite (and the running bridge) uses."""
    real_opus, real_g711 = aiortc.codecs.opus, aiortc.codecs.g711
    aiortc.codecs.opus = fake_opus
    if fake_g711 is not None:
        aiortc.codecs.g711 = fake_g711
    try:
        spec = importlib.util.spec_from_file_location("_opus_patch_probe", PATCH_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        aiortc.codecs.opus = real_opus
        aiortc.codecs.g711 = real_g711


def _fake_opus(decode=None, with_class=True, sample_rate=48000):
    module = types.ModuleType("aiortc.codecs.opus")
    module.SAMPLE_RATE = sample_rate
    if with_class:
        namespace = {} if decode is None else {"decode": decode}
        module.OpusDecoder = type("OpusDecoder", (), namespace)
    return module


def _fake_g711(with_class=True, sample_rate=8000):
    module = types.ModuleType("aiortc.codecs.g711")
    module.SAMPLE_RATE = sample_rate
    if with_class:
        module.PcmuDecoder = type(
            "PcmuDecoder", (), {"decode": lambda self, frame: []})
    return module


def test_the_guard_fires_when_the_upstream_shape_changes():
    """A monkeypatch that silently stops applying is worse than no monkeypatch.

    Five ways this patch can become wrong, all of which must raise at IMPORT --
    where CI's bridge-import-smoke job (it imports every bridge module) sees
    them -- rather than at the first doorbell press:

    1. aiortc renames/removes `OpusDecoder`, or moves `decode` off the class:
       the patch would apply to nothing.
    2. The module is executed twice: wrappers stack, and "once per session"
       silently becomes "once per wrapper".
    3. Upstream grows its own exception handling -- most likely because they
       FIXED the dead-thread half -- in which case this module must be read and
       re-reviewed, not layered over a handler that already returns [].
    4. `g711.PcmuDecoder` moves. Without it the bridge cannot decode what the
       gateway actually sends, and quietly falling back to Opus-only is the
       6-percent-real-audio outage all over again -- so it is as loud as the
       Opus cases.
    5. Either decoder's declared sample rate moves. `codec_for_audio_format`
       maps a decoded frame's shape back to the decoder that produced it, and
       that mapping is only true while the rates are 48000 and 8000; a status
       field that silently starts naming the wrong codec is worse than no field.

    The sixth case is the control: an aiortc shaped exactly like 1.15.0 must
    still be patched, and the replacement must actually swallow an FFmpegError.
    A guard that raised unconditionally would pass 1-5 and prove nothing."""
    def _expect(fake, fragment, why, g711=None):
        try:
            _exec_patch_against(fake, g711)
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
    _expect(_fake_opus(decode=_pristine_decode), "PcmuDecoder",
            "the mu-law decoder disappeared", g711=_fake_g711(with_class=False))
    _expect(_fake_opus(decode=_pristine_decode), "8000/48000",
            "the mu-law decoder's sample rate moved", g711=_fake_g711(sample_rate=16000))
    _expect(_fake_opus(decode=_pristine_decode, sample_rate=24000), "8000/48000",
            "the Opus decoder's sample rate moved", g711=_fake_g711())

    # The control: an unmodified aiortc still gets patched, and the wrapper does
    # the job -- a packet libopus rejects yields no frames rather than raising.
    def _always_raises(self, encoded_frame):
        raise av.error.InvalidDataError(1094995529, "Invalid data", "packet")

    fake = _fake_opus(decode=_always_raises)
    probe = _exec_patch_against(fake, _fake_g711())
    patched = fake.OpusDecoder.decode
    assert getattr(patched, probe._PATCH_MARKER, False), (
        "an aiortc shaped like the pinned one was not patched -- the guard is "
        "refusing everything, which would pass the cases above for free")
    assert patched(fake.OpusDecoder(), JitterFrame(data=b"\x00", timestamp=0)) == [], (
        "the replacement did not swallow an FFmpegError")
    print("  ok  guard fires on all five incompatible shapes and still patches "
          "an unmodified aiortc")


def test_the_accidental_opus_rate_the_live_cameras_actually_produce():
    """The 2026-08-28 audio outages, in one assertion.

    OPUS_CONFIDENCE was 0.75 and the test is `>=`, so a mu-law stream whose
    packets libopus accepts 15 times in 20 -- 0.75 exactly -- committed to Opus
    while mu-law was reading 20 of 20. The comment above the constant asserted
    that 0.75 sat "far above a mu-law stream's accidental-success rate"; the
    live rate is 0.75 to 0.80, so the threshold sat ON the collision rather than
    above it. Six sessions in 24 hours committed wrongly.

    What that costs is not a bad-sounding session. The mis-committed session
    emits 48 kHz stereo frames into a publisher launched for 8 kHz mono, which
    trips the format guard, spends the camera's audio-format rebuild budget and
    drops the camera to VIDEO ONLY for the rest of the process. The living room
    camera lost audio for 54 minutes this way and got it back only because an
    unrelated escape hatch restarted the container.

    Both live ratios are asserted, not just the boundary, because 16/20 is the
    one that proves this is a band rather than an off-by-one at 0.75.
    """
    for opus_hits in (15, 16):
        packets = ([MULAW_LIBOPUS_ACCEPTS] * opus_hits
                   + [DOORBELL_MULAW] * (SNIFF - opus_hits))
        assert len(packets) == SNIFF
        decoder = OpusDecoder()
        frames = _feed(decoder, packets)

        assert _committed(decoder) == aiortc_opus_patch.PCMU, (
            f"a mu-law stream libopus accepted {opus_hits} of {SNIFF} times -- "
            f"while mu-law read all {SNIFF} -- committed to "
            f"{_committed(decoder)!r}. This is the live doorbell's own rate, so "
            f"OPUS_CONFIDENCE ({aiortc_opus_patch.OPUS_CONFIDENCE}) is at or "
            f"below the accidental-success rate rather than above it.")

        shapes = {audio_format_of(f) for _, f in frames}
        assert shapes == {("s16", 8000, 1)}, (
            f"at {opus_hits}/{SNIFF} a frame escaped in a shape other than the "
            f"stream's own 8 kHz mono: {shapes}. A 48 kHz stereo frame is what "
            f"camera_session sizes the ffmpeg audio input from, and it is what "
            f"takes the camera to video-only for the rest of the process.")
    print(f"  ok  a mu-law stream libopus accepts 15 and 16 of {SNIFF} times "
          f"still commits to mu-law")


TESTS = (
    test_a_mulaw_stream_is_decoded_as_mulaw_not_discarded,
    test_a_real_opus_stream_still_commits_to_opus_and_loses_nothing,
    test_no_frame_escapes_in_the_losing_codecs_shape,
    test_the_accidental_opus_rate_the_live_cameras_actually_produce,
    test_the_counters_account_for_every_packet,
    test_the_codec_is_derivable_from_a_decoded_frames_shape,
    test_a_rejected_packet_is_dropped_not_fatal,
    test_the_decision_and_the_drops_are_logged_once_per_session,
    test_the_audio_decoder_thread_survives_a_bad_packet,
    test_the_guard_fires_when_the_upstream_shape_changes,
)


def main():
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
