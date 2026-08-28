"""Decode the gateway's audio track as what ARRIVES, not as what the SDP claims,
and survive a packet no decoder can read.

Replaces `aiortc.codecs.opus.OpusDecoder.decode` at import time. It does two
jobs, and the second one only exists because of the first.

--- 1. The codec mislabel -------------------------------------------------

The Janus offer advertises `audio(port=9,opus)`. The bytes that arrive are
**G.711 mu-law at 8 kHz**. aiortc believes the advertised codec, builds an
`OpusDecoder`, and hands it mu-law: libopus rejects most of it, and the small
fraction that happens to satisfy a TOC byte decodes as noise. Measured live on
this account, with the drop-survival wrapper below in place and nothing else:
the doorbell decoded **181 audio frames in 60 s** and the living room **268**,
where a 20 ms frame rate is 50/s. Roughly 6 to 9 percent of the audio timeline
was real; the rest was the sink's silence fill, which is what made it sound
like fragments jumping around rather than like an outage.

The evidence that the payload is mu-law, not damaged Opus:

  * The dropped payloads themselves. The doorbell's head is
    `7effff7f7efe7ffffffffefefeff7efe` -- alternating 0x7e/0x7f/0xfe/0xff is
    mu-law encoding near-silence. The living room's is
    `332038a8a9bd2223989e1e289e9f3a1d`, ordinary varying audio as mu-law.
  * The official Android app instantiates **no audio codec at all** (only
    `video/avc` and `video/raw`), which is what G.711 decoded in software looks
    like. Its `AudioFlinger` sets `notificationFrames to 160`, i.e. 20 ms at
    8 kHz, and two `AudioTrack` sessions delivered 252,160 and 212,480 frames:
    31.5 s and 26.6 s at 8 kHz, matching two roughly-30-second viewings. At
    48 kHz those would be 5 s, i.e. the app would be as broken as this bridge
    was, and it is not.
  * This project already established that talkdown to the same doorbell works
    as mu-law at 8 kHz and that L16 is impossible on the device (backlog E4).
    The camera is a mu-law device in both directions.

So the decoder is chosen by SNIFFING the first `SNIFF_PACKETS` packets of each
session with both candidates and committing to one for the life of that
session. Deciding once, rather than per packet, is deliberate: per-packet
fallback pays for two decodes forever and can flap mid-stream, which on this
pipeline is not cosmetic -- a frame in the other codec's shape (48 kHz stereo
against 8 kHz mono) trips `ffmpeg_sink.write_audio`'s format guard and rebuilds
the publisher, kicking every RTSP reader. Nothing is emitted until the decision
is made, for the same reason: a single spurious Opus frame ahead of a mu-law
stream would be enough to launch ffmpeg with the wrong `-ar`/`-ac`. Frames from
the sniff window are BUFFERED, not dropped, and the winner's are handed over the
moment it is chosen, so no audio is lost by measuring.

The committed decoder is aiortc's own `aiortc.codecs.g711.PcmuDecoder` rather
than a hand-rolled mu-law table, so its frames carry the `pts`, `time_base`,
sample format and layout the rest of the pipeline already expects (s16, mono,
8000 Hz, interleaved). Its `pts` is the RTP timestamp read on a 1/8000 time
base; if the gateway is stamping at the advertised 48 kHz clock instead, that
`pts` is wrong by 6x. UNVERIFIED, and harmless either way: nothing downstream
reads it. `ffmpeg_sink` writes raw PCM into a pipe whose timeline is its own
sample count (see `write_silence_to`), and the shape it launches ffmpeg with
comes from `audio_format_of`, which reads the format, rate and channel count.

--- 2. Surviving a packet nothing can decode ------------------------------

Upstream the two decoders are not written the same way, and only one of them is
survivable. `aiortc/codecs/h264.py`'s `H264Decoder.decode` wraps its decode in
`try/except av.FFmpegError`, logs `H264Decoder() failed to decode, skipping
package` and returns `[]` -- one bad packet is dropped and the decoder carries
on (43 such warnings on this account's live doorbell, zero dead video threads).
`aiortc/codecs/opus.py`'s `OpusDecoder.decode` has NO handler at all, and
nothing downstream supplies one: `rtcrtpreceiver.decoder_worker`'s `while True`
does not catch either, and that function IS the thread target. So a single
undecodable packet does not cost a packet -- it raises straight out of the
thread and kills the session's `audio-decoder` thread permanently. It is started
exactly once, in `RTCRtpReceiver._start()`, with no restart path.

What that looked like in production, before any of this: the doorbell's
published AAC track was *pure silence* -- 9,572,352 of 9,572,352 samples at the
s16 noise floor over a 200 s measurement, `max_volume == mean_volume`, not one
nonzero sample. Every "audio" sample the bridge published came from the sink's
silence fill. 11 sessions produced 11 `Exception in thread audio-decoder` and 0
live audio-decoder threads. Two further failures hang off the dead thread and
are invisible on their own: `RTCRtpReceiver._handle_rtp_packet` gates on
`self.__decoder_thread`, the Thread OBJECT, which stays truthy after the thread
dies, so ~8,000 frames per session were queued to nobody; and this bridge's
`janus_client._pump_audio_track` waits forever on a `recv()` whose
end-of-stream `None` sentinel only the dead worker can produce (4 leaked
`Task was destroyed but it is pending` tasks confirmed).

That survival behaviour is UNCHANGED and is still the floor: if the committed
decoder rejects a packet, the packet is dropped, the thread lives, and video
publishing is untouched. A camera that really does send Opus commits to Opus
and behaves exactly as it did before this module learned to sniff.

--- Counters --------------------------------------------------------------

The drop log is once per session by necessity (a per-packet WARNING on a 50
packets/s stream is its own outage), and "once per session" is precisely why a
WHOLESALE codec mismatch could hide for two days behind a single line that read
like an occasional glitch. So the outcome of every packet is COUNTED as well as
logged: `decode_stats()` is served process-wide at `/control/status` under
`audio_decoder`, and `codec_for_audio_format()` lets the per-camera status name
the codec actually in use beside its `audio_format`. `decoded_opus`,
`decoded_pcmu` and `discarded` are total: every packet this module sees lands in
exactly one of them.

Neither aiortc 1.15.0 (the pin) nor upstream `main` fixes the missing handler:
their `OpusDecoder.decode` is byte-identical, and `decoder_worker` is unchanged.
The repo does not vendor aiortc, so this follows the same model as the
integration's three `telus_*_patch.py` modules -- an import-time monkeypatch
carrying an import-time compatibility guard, kept honest by the CI job that
imports every bridge module (see AGENTS.md and docs/development.md).
"""
from __future__ import annotations

import logging
import threading

import av
from aiortc.codecs import g711 as _aiortc_g711
from aiortc.codecs import opus as _aiortc_opus

log = logging.getLogger(__name__)

# Set on the replacement function so the guard below can recognise our own work,
# and set on each decoder instance to carry that session's sniff state.
_PATCH_MARKER = "_telus_opus_decode_guard"
_STATE_ATTR = "_telus_audio_codec_state"

# The two codecs this module can commit a session to. Named rather than spelled
# inline so /control/status, the log lines and the tests cannot drift apart.
OPUS = "opus"
PCMU = "pcmu"

# How many packets are sniffed before a session commits to a decoder.
#
# 20 packets is 400 ms at the stream's 20 ms framing, and the size is set by two
# opposing constraints:
#
#   * BIG ENOUGH that the decision cannot be luck. libopus accepts essentially
#     every real Opus packet (0 of 500 rejected in a calibration sweep), so a
#     genuine Opus stream sniffs 20 of 20. A mu-law stream produces a much
#     smaller fraction of accidental Opus successes -- 181 and 268 accepted
#     packets per 60 s were measured live, and even at the least favourable
#     reading of the packet rate that is under a fifth of the stream. Needing
#     OPUS_CONFIDENCE of the sample to commit to Opus therefore leaves several
#     standard deviations of margin in both directions.
#   * SMALL ENOUGH to be worth landing inside the first video batch.
#     camera_session collects ten video frames (~670 ms at 15fps) before it
#     peeks at `session.audio_format` to size the ffmpeg audio input, so a
#     decision made at 400 ms gets this camera's audio published from its very
#     first session. It is no longer a CORRECTNESS constraint, and it must not
#     be treated as one: a peek that sees nothing now yields a video-only
#     publisher that gains audio at a later rotation, so the cost of missing the
#     window is latency, not a mis-sized publisher (E81). Sparse audio can miss
#     it by a long way -- 20 packets is 1.28 s of CONTINUOUS audio at the 64 ms
#     packetisation this account's cameras appear to use, and a quiet room does
#     not send that.
#
# It is also bounded by the consumer: the buffered frames are handed over in one
# batch at the commit, and janus_client's decoded-audio queue holds
# config.AUDIO_QUEUE_MAXSIZE (50) of them.
SNIFF_PACKETS = 20

# What fraction of the sniffed packets libopus has to accept for the session to
# commit to Opus. Anything less commits to mu-law, provided mu-law read more of
# the sample than libopus did.
#
# The test is deliberately ASYMMETRIC, and it is asymmetric towards leaving
# today's behaviour alone: Opus is the advertised codec and the status quo, so
# it wins ties, wins when nothing decoded at all, and wins whenever libopus is
# reading most of the stream. Only a stream libopus largely CANNOT read is
# re-labelled. 0.75 sits far above a mu-law stream's accidental-success rate and
# far below the 100% a real Opus stream produces, so there is no realistic
# stream in between; a real Opus feed would have to be losing a quarter of its
# packets to libopus before this could mistake it, and a feed that damaged has
# already lost its audio.
OPUS_CONFIDENCE = 0.75

# Throttle for the post-commit discard log. The commit line already says what
# the session decided, so this only ever fires for packets the CHOSEN decoder
# could not read: rare by construction, and if it is not rare, that is the fact
# worth shouting about. First of a session, then every this-many-th, which at
# the worst imaginable rate (every packet failing, 50/s) is a line every 20 s.
_DISCARD_LOG_EVERY = 1000


# --- Compatibility guard ---------------------------------------------------
# Fail LOUDLY at import, naming what moved, rather than patching something that
# is no longer the thing described above. All the cases below are real: aiortc
# could rename either decoder class, could fix the missing handler upstream
# (making this patch a silent second layer over a handler that already exists),
# or this module could be executed twice against one interpreter (which would
# stack wrappers and make the per-session accounting meaningless). Exercised by
# CI's bridge-import-smoke job and by tests/test_opus_decoder_patch.py.
_decoder_cls = getattr(_aiortc_opus, "OpusDecoder", None)
if _decoder_cls is None:
    raise RuntimeError(
        "Opus decode patch is incompatible with the installed aiortc: "
        "aiortc.codecs.opus.OpusDecoder is missing. Review the upstream "
        "changes before bumping (see AGENTS.md / docs/development.md)."
    )

_orig_decode = _decoder_cls.__dict__.get("decode")
if not callable(_orig_decode):
    raise RuntimeError(
        "Opus decode patch is incompatible with the installed aiortc: "
        "aiortc.codecs.opus.OpusDecoder.decode is missing or is no longer a "
        "plain method on the class. Review the upstream changes before "
        "bumping (see AGENTS.md / docs/development.md)."
    )

if getattr(_orig_decode, _PATCH_MARKER, False):
    raise RuntimeError(
        "Opus decode patch has already been applied to this interpreter's "
        "aiortc.codecs.opus.OpusDecoder.decode. Stacking wrappers would make "
        "the once-per-session commit and drop logs count wrappers instead of "
        "sessions -- import bridge.aiortc_opus_patch, do not exec this module "
        "twice."
    )

# The upstream function has NO exception handling whatsoever, which is half the
# defect; its code object therefore carries an EMPTY exception table (h264.py's
# equivalent, which does have the try/except, carries 30 bytes). A non-empty
# table means the body grew a try/except or a with-block -- most likely upstream
# fixing this -- and the patch must be re-reviewed rather than layered on top of
# a handler that already returns []. co_exceptiontable exists on CPython 3.11+;
# the Dockerfile and every CI job pin 3.14, and they are required to match.
_exception_table = getattr(_orig_decode.__code__, "co_exceptiontable", None)
if _exception_table is None:
    raise RuntimeError(
        "Opus decode patch cannot verify the upstream shape: this interpreter "
        "has no code.co_exceptiontable (CPython 3.11+ only), but the bridge "
        "image and CI both pin 3.14. Review the interpreter before running."
    )
if _exception_table:
    raise RuntimeError(
        "Opus decode patch is incompatible with the installed aiortc: "
        "aiortc.codecs.opus.OpusDecoder.decode now contains its own exception "
        "handling, so upstream has probably fixed the dead audio-decoder "
        "thread this patch exists for. Read the new implementation and DELETE "
        "this module if it now drops bad packets itself (see AGENTS.md)."
    )

# The mu-law half. Missing or reshaped, this module cannot decode what the
# gateway actually sends, and a silent degradation to Opus-only is exactly the
# 6-percent-real-audio failure it exists to end -- so it is as loud as the Opus
# guards above. The rates are read from aiortc rather than written here because
# codec_for_audio_format() maps a decoded frame's shape back to the decoder
# that produced it, and that mapping is only true while these are the rates the
# two decoders are configured with.
_pcmu_decoder_cls = getattr(_aiortc_g711, "PcmuDecoder", None)
if _pcmu_decoder_cls is None or not callable(getattr(_pcmu_decoder_cls, "decode", None)):
    raise RuntimeError(
        "Opus decode patch is incompatible with the installed aiortc: "
        "aiortc.codecs.g711.PcmuDecoder (or its decode method) is missing, so "
        "the G.711 mu-law the gateway actually sends on the Opus payload type "
        "cannot be decoded. Review the upstream changes before bumping (see "
        "AGENTS.md / docs/development.md)."
    )
PCMU_SAMPLE_RATE = getattr(_aiortc_g711, "SAMPLE_RATE", None)
OPUS_SAMPLE_RATE = getattr(_aiortc_opus, "SAMPLE_RATE", None)
if PCMU_SAMPLE_RATE != 8000 or OPUS_SAMPLE_RATE != 48000:
    raise RuntimeError(
        "Opus decode patch is incompatible with the installed aiortc: it "
        f"declares g711 at {PCMU_SAMPLE_RATE!r}Hz and opus at "
        f"{OPUS_SAMPLE_RATE!r}Hz, not the 8000/48000 that let a decoded "
        "frame's shape identify the decoder that produced it. Review the "
        "upstream changes before bumping (see AGENTS.md)."
    )


# --- Process-wide counters -------------------------------------------------
# Written from aiortc's per-receiver decoder THREADS, so the increments are
# taken under a lock: two cameras decode concurrently, and a counter whose whole
# job is to make a silent failure countable must not be the thing that loses
# counts. One acquisition per packet at ~50 packets/s per camera.
_stats_lock = threading.Lock()
_STATS = {
    # Packets each decoder actually turned into frames that were delivered.
    "decoded_opus": 0,
    "decoded_pcmu": 0,
    # Packets that reached nobody: neither candidate could read them during the
    # sniff, or the committed decoder rejected them afterwards.
    "discarded": 0,
    # Sessions (one decoder instance each) that committed to each codec.
    "sessions_opus": 0,
    "sessions_pcmu": 0,
}


def _bump(**deltas: int) -> None:
    with _stats_lock:
        for key, delta in deltas.items():
            _STATS[key] += delta


def decode_stats() -> dict:
    """Snapshot of the process-wide audio decode counters.

    Served at /control/status under `audio_decoder`. PROCESS-WIDE, not per
    camera: aiortc creates its decoder inside `decoder_worker`, a thread-local
    that no `RTCRtpReceiver` keeps a reference to, so there is no honest way to
    attribute a count to a camera from here. The per-camera answer to "which
    codec is this stream on" is served beside each camera's `audio_format`
    instead, derived from the decoded frames themselves by
    codec_for_audio_format().

    `decoded_opus + decoded_pcmu + discarded` is every packet this process has
    decoded audio from, which is what makes a wholesale mismatch impossible to
    miss: a stream on the wrong codec shows almost all of its packets in
    `discarded`.
    """
    with _stats_lock:
        snapshot = dict(_STATS)
    snapshot["sniff_packets"] = SNIFF_PACKETS
    snapshot["opus_confidence"] = OPUS_CONFIDENCE
    return snapshot


def codec_for_audio_format(fmt) -> str | None:
    """Which decoder produced a decoded audio frame of shape `fmt`, or None.

    `fmt` is `ffmpeg_sink.audio_format_of`'s `(sample format, rate, channels)`.
    This is a derivation, not a guess: both decoders are configured with a fixed
    output shape by aiortc itself (Opus at 48 kHz stereo, mu-law at 8 kHz mono,
    both s16), the import guard above refuses to run if either rate moves, and
    no other decoder feeds this pipeline. It is how the per-camera status can
    name the codec in use without the camera loop having to reach into a decoder
    thread it has no handle on.
    """
    if not fmt:
        return None
    try:
        rate, channels = int(fmt[1]), int(fmt[2])
    except (IndexError, TypeError, ValueError):
        return None
    if rate == PCMU_SAMPLE_RATE and channels == 1:
        return PCMU
    if rate == OPUS_SAMPLE_RATE:
        return OPUS
    return None


class _SessionCodec:
    """One decoder instance's sniff state, decision and counters.

    Per instance is per session: aiortc builds a fresh `OpusDecoder` inside each
    receiver's decoder thread, and that thread lives and dies with the Janus
    session.
    """

    __slots__ = ("committed", "sampled", "opus_ok", "pcmu_ok", "opus_frames",
                 "pcmu_frames", "pcmu", "discarded", "first_reject", "tag")

    def __init__(self):
        # Short per-session marker so the commit line and this session's later
        # discard lines can be tied together in a log carrying several cameras
        # (aiortc names every audio decoder thread "audio-decoder", and the
        # decoder has no idea which camera it belongs to -- the per-camera
        # answer is /control/status's audio_codec).
        self.tag = f"{id(self) & 0xffff:04x}"
        self.committed: str | None = None
        self.sampled = 0
        self.opus_ok = 0
        self.pcmu_ok = 0
        # Candidate frames held while the decision is being made. Bounded by
        # SNIFF_PACKETS; the loser's are released at the commit.
        self.opus_frames: list = []
        self.pcmu_frames: list = []
        # Built lazily and only once, so a session that commits to Opus on its
        # first packets never pays for a decoder it will not use... and so an
        # aiortc whose PcmuDecoder cannot be constructed degrades to exactly the
        # Opus-only behaviour that shipped before, rather than failing a camera.
        self.pcmu = None
        self.discarded = 0
        # len + head of the first packet libopus rejected, for the commit line:
        # it is what identified the payload as mu-law in the first place, and it
        # is what would show a THIRD shape turning up.
        self.first_reject: str = ""


def _try_opus(decoder, encoded_frame):
    """`(frames, error)` from the upstream Opus decode. Never raises.

    An empty frame list is NOT a success: the packet reached nobody either way,
    and counting it as decoded is how a decoder that silently produces nothing
    would look healthy.
    """
    try:
        frames = _orig_decode(decoder, encoded_frame)
    except av.FFmpegError as exc:
        return [], exc
    return list(frames or ()), None


def _try_pcmu(state, encoded_frame):
    """`(frames, error)` from this session's mu-law decoder. Never raises."""
    if not state.pcmu:
        return [], None
    try:
        frames = state.pcmu.decode(encoded_frame)
    except av.FFmpegError as exc:
        return [], exc
    return list(frames or ()), None


def _packet_head(encoded_frame) -> str:
    data = bytes(getattr(encoded_frame, "data", b"") or b"")
    return f"len={len(data)} head={data[:16].hex()}"


def _commit(state: _SessionCodec):
    """Choose this session's decoder, log the choice once, and release the
    frames the winner decoded while the choice was being made."""
    opus_wins = (
        state.sampled <= 0
        or state.opus_ok >= OPUS_CONFIDENCE * state.sampled
        or state.opus_ok >= state.pcmu_ok
    )
    if opus_wins:
        state.committed = OPUS
        delivered, ok = state.opus_frames, state.opus_ok
        _bump(sessions_opus=1, decoded_opus=ok)
        log.info(
            "audio decoder %s: committing this session to Opus, the advertised "
            "codec (%d of %d sniffed packets decoded as Opus, %d as G.711 "
            "mu-law)", state.tag, state.opus_ok, state.sampled, state.pcmu_ok)
    else:
        state.committed = PCMU
        delivered, ok = state.pcmu_frames, state.pcmu_ok
        _bump(sessions_pcmu=1, decoded_pcmu=ok)
        log.info(
            "audio decoder %s: the track advertised as Opus is carrying G.711 "
            "mu-law -- committing this session to the mu-law decoder (%d of %d "
            "sniffed packets decoded as mu-law, only %d as Opus; first packet "
            "libopus rejected: %s)",
            state.tag, state.pcmu_ok, state.sampled, state.opus_ok,
            state.first_reject or "none")
    # Release the loser's held frames and, when Opus won, the mu-law decoder
    # that will never be used again.
    state.opus_frames = []
    state.pcmu_frames = []
    if state.committed == OPUS:
        state.pcmu = None
    discarded = state.sampled - ok
    state.discarded += discarded
    _bump(discarded=discarded)
    return delivered


def _sniff(decoder, state: _SessionCodec, encoded_frame):
    """Try both candidates on one packet, hold what they produced, and commit
    once SNIFF_PACKETS have been sampled.

    Returns [] until the commit, then the whole of the winner's held output at
    once. Nothing is emitted before the decision because a single frame in the
    losing codec's shape is enough to launch ffmpeg with the wrong `-ar`/`-ac`
    (or to trip the running publisher's format guard and rebuild it), and
    nothing is DISCARDED before the decision because measuring should not cost
    audio.
    """
    opus_frames, opus_error = _try_opus(decoder, encoded_frame)
    if state.pcmu is None:
        try:
            state.pcmu = _pcmu_decoder_cls()
        except Exception:
            # Opus-only from here, which is exactly the behaviour that shipped
            # before this module could sniff. Logged once (the state is per
            # session) and never retried, so a broken libavcodec cannot cost a
            # decoder construction per packet.
            log.exception("audio decoder %s: could not build the G.711 mu-law "
                          "decoder; this session can only try Opus", state.tag)
            state.pcmu = False
    pcmu_frames, _pcmu_error = _try_pcmu(state, encoded_frame)

    state.sampled += 1
    if opus_frames:
        state.opus_ok += 1
        state.opus_frames.extend(opus_frames)
    elif not state.first_reject and opus_error is not None:
        state.first_reject = f"{_packet_head(encoded_frame)}: {opus_error}"
    if pcmu_frames:
        state.pcmu_ok += 1
        state.pcmu_frames.extend(pcmu_frames)

    if state.sampled < SNIFF_PACKETS:
        return []
    return _commit(state)


def _decode_committed(decoder, state: _SessionCodec, encoded_frame):
    """`OpusDecoder.decode`, but through this session's chosen decoder and with
    a packet it rejects dropped rather than fatal.

    Mirrors `H264Decoder.decode`: catch `av.FFmpegError` (the base class of the
    `av.error.InvalidDataError` libopus raises through
    `avcodec_send_packet()`), count it, log it sparingly, and return no frames.
    The decoder object itself is unharmed by the failed send and decodes the
    very next packet normally -- verified against the real libopus, and pinned
    by tests/test_opus_decoder_patch.py.
    """
    if state.committed == PCMU:
        frames, error = _try_pcmu(state, encoded_frame)
    else:
        frames, error = _try_opus(decoder, encoded_frame)
    if frames:
        _bump(**{f"decoded_{state.committed}": 1})
        return frames
    state.discarded += 1
    _bump(discarded=1)
    # A per-packet WARNING on a ~50 packets/s stream would be its own outage, so
    # the line is throttled -- but it is no longer the only trace, which is the
    # point: decode_stats() counts every one of these and /control/status serves
    # the total, so a run of them cannot read like an occasional glitch again.
    if state.discarded == 1 or state.discarded % _DISCARD_LOG_EVERY == 0:
        log.warning(
            "audio decoder %s (%s): dropped an undecodable packet and kept the "
            "audio decoder alive -- %s: %s (%d dropped in this session; only "
            "every %dth is logged, the running total is /control/status's "
            "audio_decoder.discarded)",
            state.tag, state.committed, _packet_head(encoded_frame), error,
            state.discarded, _DISCARD_LOG_EVERY)
    return []


def _decode_the_codec_that_actually_arrived(self, encoded_frame):
    """Replacement for `aiortc.codecs.opus.OpusDecoder.decode`.

    Sniffs the first SNIFF_PACKETS packets of the session, commits to Opus or
    G.711 mu-law, and from then on decodes with the committed codec, dropping
    (and counting) anything it cannot read.
    """
    state = getattr(self, _STATE_ATTR, None)
    if state is None:
        state = _SessionCodec()
        setattr(self, _STATE_ATTR, state)
    if state.committed is None:
        return _sniff(self, state, encoded_frame)
    return _decode_committed(self, state, encoded_frame)


setattr(_decode_the_codec_that_actually_arrived, _PATCH_MARKER, True)
_decoder_cls.decode = _decode_the_codec_that_actually_arrived
