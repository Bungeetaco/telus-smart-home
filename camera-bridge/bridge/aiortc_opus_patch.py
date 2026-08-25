"""Make aiortc's Opus decoder survive a malformed packet, the way its H.264 one does.

Replaces `aiortc.codecs.opus.OpusDecoder.decode` at import time.

Upstream the two decoders are not written the same way, and only one of them is
survivable. `aiortc/codecs/h264.py`'s `H264Decoder.decode` wraps its decode in
`try/except av.FFmpegError`, logs `H264Decoder() failed to decode, skipping
package` and returns `[]` -- one bad packet is dropped and the decoder carries
on (43 such warnings on this account's live doorbell, zero dead video threads).
`aiortc/codecs/opus.py`'s `OpusDecoder.decode` has NO handler at all, and
nothing downstream supplies one: `rtcrtpreceiver.decoder_worker`'s `while True`
does not catch either, and that function IS the thread target. So a single
malformed Opus packet does not cost a packet -- it raises straight out of the
thread and kills the session's `audio-decoder` thread permanently. It is started
exactly once, in `RTCRtpReceiver._start()`, with no restart path.

What that looked like in production, before this patch: the doorbell's published
AAC track was *pure silence* -- 9,572,352 of 9,572,352 samples at the s16 noise
floor over a 200 s measurement, `max_volume == mean_volume`, not one nonzero
sample. Every "audio" sample the bridge published came from the sink's silence
fill. 11 sessions produced 11 `Exception in thread audio-decoder` and 0 live
audio-decoder threads. Two further failures hang off the dead thread and are
invisible on their own: `RTCRtpReceiver._handle_rtp_packet` gates on
`self.__decoder_thread`, the Thread OBJECT, which stays truthy after the thread
dies, so ~8,000 frames per session were queued to nobody; and this bridge's
`janus_client._pump_audio_track` waits forever on a `recv()` whose
end-of-stream `None` sentinel only the dead worker can produce (4 leaked
`Task was destroyed but it is pending` tasks confirmed).

The failing bytes are NOT ordinary Opus -- libopus accepts every TOC code-0
packet (the shape of every normal 20 ms WebRTC Opus packet; 0 of 500 rejected
in a calibration sweep). Identified from this wrapper's own log line, once per
session, consistently: **G.711 mu-law audio arriving on the Opus payload
type** -- 512 bytes clustered on 0x7c-0x7f/0xfc-0xff, which is mu-law near
silence. Not either of the shapes originally suspected (Ogg
`OpusHead`/`OpusTags` identification headers, or H.264 NAL bytes); those
merely reproduce the same error class. WHY the gateway emits it is still
unverified -- the doorbell's own upstream audio is mu-law, so the most
plausible reading is a device-native frame or two forwarded before the
relay's Opus transcode is running -- which is why the wrapper keeps logging
`len` and the first 16 bytes. One line per session; a per-packet line on a
50 packets/s stream would be its own outage. The full identification is in
docs/camera-bridge.md.

Neither aiortc 1.15.0 (the pin) nor upstream `main` fixes this: their
`OpusDecoder.decode` is byte-identical, and `decoder_worker` is unchanged. The
repo does not vendor aiortc, so this follows the same model as the integration's
three `telus_*_patch.py` modules -- an import-time monkeypatch carrying an
import-time compatibility guard, kept honest by the CI job that imports every
bridge module (see AGENTS.md and docs/development.md).
"""
from __future__ import annotations

import logging

import av
from aiortc.codecs import opus as _aiortc_opus

log = logging.getLogger(__name__)

# Set on the replacement function so the guard below can recognise our own work,
# and set on each decoder instance the first time it drops a packet.
_PATCH_MARKER = "_telus_opus_decode_guard"
_LOGGED_ATTR = "_telus_opus_drop_logged"


# --- Compatibility guard ---------------------------------------------------
# Fail LOUDLY at import, naming what moved, rather than patching something that
# is no longer the thing described above. All three cases below are real: aiortc
# could rename the class, could fix the defect upstream (making this patch a
# silent second layer over a handler that already exists), or this module could
# be executed twice against one interpreter (which would stack wrappers and make
# the "once per session" log accounting meaningless). Exercised by CI's
# bridge-import-smoke job and by tests/test_opus_decoder_patch.py.
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
        "the once-per-session drop log count wrappers instead of sessions -- "
        "import bridge.aiortc_opus_patch, do not exec this module twice."
    )

# The upstream function has NO exception handling whatsoever, which is the whole
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


def _decode_dropping_undecodable_packets(self, encoded_frame):
    """`OpusDecoder.decode`, but a packet libopus rejects is dropped, not fatal.

    Mirrors `H264Decoder.decode`: catch `av.FFmpegError` (the base class of the
    `av.error.InvalidDataError` libopus raises through
    `avcodec_send_packet()`), log, and return no frames. The decoder object
    itself is unharmed by the failed send and decodes the very next packet
    normally -- verified against the real libopus, and pinned by
    tests/test_opus_decoder_patch.py.

    Logged at most ONCE per decoder instance, which is once per session: aiortc
    builds a fresh OpusDecoder per RTP receiver, and this stream carries ~50
    packets a second. The length and the first 16 bytes stay in the line: they
    are what identified the packet as G.711 mu-law (see the module docstring),
    and they are what would show a DIFFERENT undecodable shape turning up.
    """
    try:
        return _orig_decode(self, encoded_frame)
    except av.FFmpegError as exc:
        if not getattr(self, _LOGGED_ATTR, False):
            setattr(self, _LOGGED_ATTR, True)
            data = bytes(getattr(encoded_frame, "data", b"") or b"")
            log.warning(
                "OpusDecoder: dropped an undecodable packet and kept the "
                "audio decoder alive -- len=%d head=%s: %s (further drops in "
                "this session are not logged)",
                len(data), data[:16].hex(), exc)
        return []


setattr(_decode_dropping_undecodable_packets, _PATCH_MARKER, True)
_decoder_cls.decode = _decode_dropping_undecodable_packets
