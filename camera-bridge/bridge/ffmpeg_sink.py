"""Feeds decoded av.VideoFrame objects to an ffmpeg subprocess, which encodes and
publishes (RTSP ANNOUNCE, not listen -- ffmpeg's RTSP muxer cannot act as a server)
into the bundled MediaMTX instance.

Load-bearing flags, found by empirical testing against this exact stack:
  -bf 0 -g <fps>    avoid B-frames / keep a short (~1s) GOP so Home Assistant's
                    PyAV-based stream worker never has to reorder frames without DTS
                    info. GOP is tied to the actual measured fps (not a fixed frame
                    count) so the keyframe interval is a consistent ~1s regardless of
                    a given camera's frame rate.
  -pkt_size 1200    keep RTP payloads under MediaMTX's repacketization threshold --
                    letting MediaMTX repacketize strips DTS continuity and produces
                    "No dts in N consecutive packets" in HA's stream_worker.
  -framerate <fps>  declare the camera's rate to the RAWVIDEO DEMUXER, never as
                    `-r <fps>` (which overrides -use_wallclock_as_timestamps) and
                    never by omission (the demuxer then defaults to 25fps and
                    re-rates every stream). See the comment on common_in for the
                    measurements -- this one flag has three distinct behaviours.
Without both of these, the Generic Camera config flow's live "can we read the
stream" verification step times out with no clue in the REST response as to why.

Encoding is hardware-accelerated by default -- config.ENCODE_BACKEND selects
which path:

  "vaapi" (default, this project's own deployment): the host's Intel iGPU
    (QuickSync) or an AMD GPU via Mesa, through /dev/dri -- the container
    needs /dev/dri passed through (see docker-compose.yml). `format=nv12,
    hwupload` converts the raw software frames this class receives into the
    hardware surface h264_vaapi requires; -g/-bf above are still respected by
    the hardware encoder and remain load-bearing for the same reason.
    -async_depth 1 (VAAPI-only, not a libx264/NVENC option) forces strictly
    sequential encoding rather than the encoder's default 2-frame pipeline --
    that parallelism trades latency for throughput across many concurrent
    streams sharing an encode session, which doesn't apply to this
    one-process-per-camera design.

  "nvenc": an NVIDIA GPU, via nvidia-container-toolkit device injection (see
    docker-compose.yml's commented-out NVIDIA section) -- no /dev/dri
    involved. Unlike VAAPI, ffmpeg's h264_nvenc accepts the raw yuv420p pipe
    input directly (no explicit hwupload step needed). NOT verified against
    real NVIDIA hardware by this project (Intel-only host) -- ffmpeg's own
    encoder support was confirmed present (`ffmpeg -encoders`), but the full
    pipeline hasn't been exercised live the way the VAAPI path has.

  "software": skip the hardware attempt entirely and always use libx264.

If the selected hardware backend is unavailable or broken (driver crash,
device reset, no passthrough at all), ffmpeg exits almost immediately after
launch -- `start()` detects that and falls back to software libx264 rather
than leaving the camera dark. This is deliberately re-attempted on every
start() call (i.e. every reconnect/rotation) rather than remembered as
"permanently broken", so hardware encoding resumes automatically the moment
the device comes back.

AUDIO (optional, off unless start() is given an audio format -- see
config.JANUS_CONSUME_AUDIO):

  A second raw input on its OWN fd, muxed into the SAME RTSP output. The fd is
  an anonymous os.pipe() handed to the child through subprocess `pass_fds` and
  addressed as `-i pipe:<n>`, NOT a FIFO. A FIFO would need a writable path
  inside the container, cleanup on every restart, and -- the deciding problem --
  opening one for write BLOCKS until a reader opens the other end, while ffmpeg
  opens its inputs strictly in order and only reaches input 1 after input 0 has
  produced a frame. That is a genuine deadlock, not a theoretical one. An
  anonymous pipe has neither end of that problem, leaves nothing on disk, and is
  reclaimed by the OS when either side goes away.

  With a second input the two streams must share ONE clock, and only the
  wallclock can be it: the audio stream's timeline is its own sample count (see
  write_silence_to, which sizes the fill from elapsed real time), so the video
  timeline has to advance at real time too or the muxer's interleaver holds one
  of them back forever. That is what `-r` on the video input silently prevented
  -- it is absent from every command line this file builds, and the camera's rate
  is declared with the demuxer's `-framerate` instead (see the comment on
  common_in). `-af aresample=async=1` (fill-and-trim only, no stretching) absorbs
  what is left.

  Sharing one clock also means the audio input must stay AHEAD of the video,
  continuously, or ffmpeg stops reading the VIDEO pipe and the camera drops
  frames -- the audio input paces the whole publisher. _AUDIO_PRIME_MS
  establishes that lead when the pipe is opened; both carry the measurements.

  The lead is a FLOOR, not a fixed quantity, and the two writers move it in
  opposite ways. write_silence_to tops the timeline up TO the wall clock, so it
  can only ever raise the lead to zero -- it stops the lead going negative, it
  does not restore the prime. write_audio has no wallclock term at all, so a
  decoded backlog drained by the pump legitimately puts the timeline seconds
  AHEAD. That is why config.AUDIO_SILENCE_TOPUP_S is chosen against the prime
  (the cadence is what has to stay under it) and why a lead far above the prime
  in a handover record is a correct reading rather than a broken one.

  An audio input that goes bad mid-session is DROPPED, not repaired: video is
  worth more than audio, so the sink degrades to video-only (see _drop_audio)
  rather than failing and taking every RTSP reader down with it.

  The encoder is AAC, and that is not a free choice: Home Assistant's HLS stream
  worker accepts only aac/mp3 for audio, and Frigate's `-c copy` recording preset
  needs a codec that is legal in an mp4 container. AAC is the only codec that
  satisfies both consumers, so what the gateway sends (G.711 mu-law on a payload
  type advertised as Opus -- see aiortc_opus_patch) is transcoded rather than
  passed through. The published audio is mono; the sample rate is whatever the
  decoded frames actually carry, never assumed, which on this account's cameras
  is 8 kHz rather than the 48 kHz the SDP implies. Nothing in the pacing below
  is written in terms of a particular rate: the prime, the top-up ceiling and
  the silence fill are all sample counts derived from `audio_format[1]`.

  Every encoder variant gets the audio input and the audio encoder by
  CONSTRUCTION -- they are spliced into the `common_in`/`common_out` fragments all
  three variants are built from, so a hardware path that publishes audio while
  the software fallback silently does not is not expressible here.
"""
import asyncio
import contextlib
import logging
import os
import re
import time

from . import config as c

log = logging.getLogger(__name__)

# How long to give a freshly-launched ffmpeg to prove it's actually alive before
# trusting it -- a doomed VAAPI attempt (missing/broken device) exits almost
# immediately; a working one is still running past this, quietly waiting on stdin.
_STARTUP_GRACE_S = 0.75

# Matches the "user:password@" userinfo section of a URL.
_URL_CREDENTIALS_RE = re.compile(r"://[^/@]+:[^/@]*@")

# PyAV sample-format name -> (ffmpeg raw demuxer name, bytes per sample). The
# planar variants map to the same INTERLEAVED demuxer because _audio_pcm_bytes
# interleaves them before they reach the pipe -- ffmpeg's raw PCM demuxers only
# read interleaved data, and there is no `-f s16lep`.
_PCM_PIPE_FORMAT = {
    "u8": ("u8", 1), "u8p": ("u8", 1),
    "s16": ("s16le", 2), "s16p": ("s16le", 2),
    "s32": ("s32le", 4), "s32p": ("s32le", 4),
    "flt": ("f32le", 4), "fltp": ("f32le", 4),
    "dbl": ("f64le", 8), "dblp": ("f64le", 8),
}

# How much unwritten audio the pipe transport may hold before the sink stops
# feeding audio. ffmpeg drains this pipe continuously; a backlog this large means
# it has stopped reading input 1 entirely, and buffering audio in this process
# forever would hide that. It is NOT a reason to rebuild the publisher -- see
# _drop_audio.
_AUDIO_PIPE_MAX_BUFFER = 512 * 1024
# Silence written into the pipe the instant it is opened. ffmpeg's
# avformat_find_stream_info runs on input 1 before any camera frame exists, and a
# raw input with nothing in it is a blocking read; this hands it something to
# find. It is also why the audio input carries -analyzeduration 0 -probesize 32:
# raw PCM's parameters are fully described by -f/-ar/-ac, so there is nothing to
# probe for and no reason to let ffmpeg wait for more.
#
# It is ALSO the audio stream's standing LEAD over the video, and that is the
# load-bearing half. The video input is stamped with the wallclock
# (-use_wallclock_as_timestamps), while the audio timeline is its own sample
# count topped up to the wallclock (write_silence_to) -- so from the moment the
# pipe is primed, the audio covers timestamps this far past what the video has
# reached. The muxer's interleaver can only emit a video packet once it holds
# audio covering that packet's timestamp; while the lead is positive it always
# does, and ffmpeg keeps reading the video pipe. When the lead reaches zero,
# ffmpeg stops reading the video pipe until the next audio write arrives -- the
# frame-dropping stall measured below. Every top-up cadence in this file is
# therefore chosen against THIS number: config.AUDIO_SILENCE_TOPUP_S must stay
# comfortably under it.
_AUDIO_PRIME_MS = 100
# Ceiling on a single silence top-up, so a long stall cannot dump minutes of
# silence into the pipe in one write. The deficit is not lost and A/V sync is
# unaffected: write_silence_to derives its sample count from ELAPSED WALLCLOCK
# rather than from how many times it has been called, so the next call simply
# recomputes what is still owed and keeps catching up -- at up to this much per
# call, and callers top up more often than this (config.AUDIO_SILENCE_TOPUP_S is
# half of it), so an ordinary top-up is never clipped at all.
#
# Measured in the deployed container (ffmpeg 7.1.5, h264_vaapi, 1440x1440 ->
# 960x960 at 15fps, synthetic feed, 900 frames per 60s run, timing write_frame;
# the audio writer runs write_silence_to's exact arithmetic). The runs output to
# `-f null -` rather than publishing into the live MediaMTX, so they measure the
# demux/interleave side and not whatever a real muxer target adds; the shipped
# 0.5s row reproduces the live defect's drop rate to the frame:
#
#   top-up cadence / ceiling      write_frame p50/p90/p99 (ms)   frames >66.7ms
#   video-only (control)                1.81 / 1.94 / 2.87        0 of 900
#   every 500ms, ceiling 2.0s           1.80 / 231.6 / 297.1    120 of 900 (13.3%)
#   every 100ms, ceiling 0.1s           1.90 / 202.8 / 297.1    162 of 900 (18%)
#   every  80ms, ceiling 0.1s           1.81 / 1.97 / 2.27        0 of 900
#   every  50ms, ceiling 0.1s           1.84 / 2.03 / 3.56        0 of 900
#   every  25ms, ceiling 0.1s           1.80 / 2.04 / 3.44        0 of 900
#
# The cliff sits exactly at _AUDIO_PRIME_MS, which is what identifies the lead
# (not the size of any one write) as the mechanism: a 100ms cadence spends the
# instant before each write with a lead of zero, and 18% of frames stall behind
# it. The old 500ms cadence is the same fault with 400ms of the cycle spent
# BEHIND the video -- one video stall per audio write, ~0.33-0.66x the audio
# owed. Handing over MORE than is owed is harmless by comparison (it just
# lengthens the lead): the same rig fed 2x real time showed no video stall at
# all, but 10.9MB of audio backed up in this process because ffmpeg stopped
# reading a pipe running a minute ahead -- which is what _AUDIO_PIPE_MAX_BUFFER
# and this ceiling together bound.
_AUDIO_SILENCE_MAX_BURST_S = 0.1
# ...and the point past which a deficit stops being a late top-up and becomes a
# HOLE, which is a different event and gets closed in one write instead.
#
# The ceiling above bounds a routine top-up, and at the 0.05 s cadence every
# caller uses it repays a backlog at about twice real time. That is the right
# answer for a deficit of tens of milliseconds and the wrong one for a deficit of
# seconds, because of WHERE the damage is: while the audio timeline is behind the
# wall clock the lead is negative, and a negative lead is exactly when ffmpeg
# stops reading the VIDEO pipe. Repaying 2.9 s at 2x therefore means 2.9 s of the
# video pipe not being read, and the frames behind it are lost at frame-interval
# cadence for the whole of it (measured live: 6 to 19 frames per handover, drop
# count tracking deficit size). Closing the hole in one write ends that window
# immediately.
#
# That is not a contradiction of the table above, it is its other half: what
# stalled video there was the instant of ZERO lead before each write, never the
# size of the write. Running ahead was measured harmless -- "the same rig fed 2x
# real time showed no video stall at all" -- and a catch-up cannot even run
# ahead, since write_silence_to only ever tops up TO the wall clock.
#
# The threshold is the video queue's own tolerance (janus_client's
# VIDEO_QUEUE_TARGET_MS, 500 ms): under it, a deficit repaid across calls costs
# at most buffer the queue already carries, so the ordinary ceiling keeps its
# job; past it frames are lost either way and the only question is for how long.
_AUDIO_CATCHUP_DEFICIT_S = 0.5
# The catch-up's own bound, and it is in BYTES rather than seconds on purpose.
# What must never happen is the thing _AUDIO_SILENCE_MAX_BURST_S was written
# for -- dumping minutes of silence into the pipe after a long stall -- and
# "minutes" is a quantity of bytes, not of seconds: the same 3 s means 48 kB at
# the 8 kHz mono the cameras on this account actually send and 576 kB at the
# 48 kHz stereo the SDP implies. Expressed against _AUDIO_PIPE_MAX_BUFFER, a
# catch-up can never be what pushes the transport past the depth that already
# means "ffmpeg has stopped reading" -- it is a quarter of it. At 8 kHz mono s16
# that is 8.2 s of silence, comfortably past the worst deficit ever measured
# here (2.9 s); at any shape, a deficit larger than this still repays across
# calls at the ordinary ceiling, exactly as before.
_AUDIO_CATCHUP_MAX_BYTES = _AUDIO_PIPE_MAX_BUFFER // 4
# How many CONSECUTIVE decoded audio frames may fail write_audio's byte-count
# check before the audio input is given up on. One mismatched frame is a
# glitch (a partially-decoded frame) and is skipped -- its span is filled with
# silence so the sample-counted timeline, and with it the muxer lead the video
# pipe depends on, keeps tracking the wall clock. A RUN of them is a broken
# decoder: every frame it will ever produce is unusable, so the input is
# dropped (video-only, visibly -- see _drop_audio) rather than skipped forever
# while /control/status counts "decoded" frames that never carry sound. At the
# stream's 20ms frames this is ~1s of decoder output. NOT a rebuild in either
# case: the frames' FORMAT matched what ffmpeg was launched with, so a rebuilt
# command line would be byte-identical and the failure would simply recur --
# and recur as a session-failure loop the consecutive-failure escape hatch
# never sees, because every such session still delivers its first video batch.
_AUDIO_MALFORMED_LIMIT = 50
# What one sample of silence looks like, per raw sample format. Digital
# silence is the format's ZERO: for the unsigned 8-bit formats that is the
# 0x80 midpoint -- all-zero bytes there are a full-scale negative DC offset,
# not silence. Every other format here is signed or float, where zero bytes
# are genuinely zero.
_PCM_SILENCE_BYTE = {"u8": 0x80, "u8p": 0x80}
# How long a single frame may sit unread in the video pipe before the publisher
# is treated as gone (see write_frame). stdin.drain() returns once the transport
# has handed the frame to ffmpeg, so it completing is direct evidence that ffmpeg
# is still consuming; it having NOT completed for this long is not slowness --
# a healthy write is ~2ms (measurements above) and a whole frame is ~3MB against
# a 64KB pipe buffer, so 5s is ~75 frames' worth of not being read.
_VIDEO_DRAIN_TIMEOUT_S = 5.0


def _redact_url(url: str) -> str:
    """Strip credentials out of a URL before it reaches a log line."""
    return _URL_CREDENTIALS_RE.sub("://***:***@", url)


def clamp_fps(fps) -> int:
    """The rate ffmpeg is actually declared with, for any measured value.

    start() has always clamped its argument to 1-30 before putting it on the
    command line, so `sink.fps` -- what `-framerate` and `-g` were really given
    -- is not always what the caller passed. camera_session.py has to compare a
    fresh measurement against THAT number rather than against the raw reading
    (see its RateWatcher): a camera measured at 40fps and one measured at 55fps
    are both published at 30, so treating either as "the rate moved" would
    rebuild the publisher for a command line that cannot change. One definition,
    used by both.
    """
    try:
        rate = float(fps)
    except (TypeError, ValueError):
        rate = 15.0
    return max(1, min(int(round(rate)), 30))


def audio_channel_count(frame) -> int:
    """Channels in a decoded av.AudioFrame, across PyAV layout spellings."""
    channels = getattr(getattr(frame, "layout", None), "channels", None)
    if channels is not None:
        return len(channels)
    return int(getattr(frame, "channels", 1) or 1)


def audio_format_of(frame) -> tuple:
    """(sample-format name, sample rate, channel count) of a decoded audio frame.

    The identity of the raw pipe's wire format, and therefore the thing
    camera_session.py compares against what the running ffmpeg was launched with
    -- the audio counterpart of a frame's (width, height).
    """
    return (str(frame.format.name), int(frame.sample_rate), audio_channel_count(frame))


def _audio_pcm_bytes(frame) -> bytes:
    """Interleaved raw PCM bytes for one decoded audio frame.

    Planar frames (`s16p`, `fltp`, ...) come out of to_ndarray() as
    (channels, samples) and MUST be interleaved before they reach the pipe;
    writing them plane-major would be accepted by ffmpeg and decode as two
    channels of garbage, which is precisely the silent-corruption failure the
    format guard below exists to prevent.
    """
    arr = frame.to_ndarray()
    if getattr(frame.format, "is_planar", False) and getattr(arr, "ndim", 1) == 2 \
            and arr.shape[0] > 1:
        arr = arr.T.reshape(-1)
    return arr.tobytes()


class _AudioPipeProtocol(asyncio.BaseProtocol):
    """Minimal write-pipe protocol: all this sink needs to know is whether the
    far end went away. Deliberately not asyncio.StreamWriter, whose drain()
    requires the private FlowControlMixin -- the transport's own buffer size is
    a public equivalent for the one thing being watched here (see
    _AUDIO_PIPE_MAX_BUFFER)."""

    def __init__(self):
        self.lost = False

    def connection_lost(self, exc):
        self.lost = True


class FfmpegSink:
    """Feeds decoded av.VideoFrame objects to ffmpeg, which encodes and publishes to MediaMTX."""

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self.proc: asyncio.subprocess.Process | None = None
        self._wh: tuple[int, int] | None = None
        self._stderr_task: asyncio.Task | None = None
        self.hardware_encode: bool = False
        self.fps: int | None = None  # actual (clamped) fps in use -- see start()
        # --- audio, only populated when start() was given an audio format ---
        # The write end of the audio pipe is opened ONCE, here, with the ffmpeg
        # process, and is never closed or reopened while this sink lives. That
        # is not tidiness, it is the only thing that can work: ffmpeg opens each
        # input exactly once at startup and has no way to reopen one, so closing
        # this fd at the end of a Janus session would EOF input 1 permanently and
        # there would be no way to attach the next session's audio to the running
        # publisher. The publisher deliberately outlives sessions (see
        # camera_session.py's comment on why tearing it down blacks the RTSP
        # path), so the audio fd has to outlive them too -- and the gap between
        # sessions is covered by writing silence into this same fd rather than by
        # closing anything.
        self.audio_format: tuple | None = None
        self._audio_transport = None
        self._audio_protocol: _AudioPipeProtocol | None = None
        self._audio_pipe_file = None
        self._audio_bytes_per_sample: int = 0   # across ALL channels
        self._audio_samples_written: int = 0
        self._audio_started_at: float | None = None
        self._audio_silence_byte: int = 0       # per-format; see _PCM_SILENCE_BYTE
        self._audio_malformed_run: int = 0      # see _AUDIO_MALFORMED_LIMIT
        # Set when an audio input this sink was supposed to have is gone -- a
        # live one abandoned mid-session (_drop_audio), or one whose pipe never
        # opened at start(). Distinct from "this sink never had audio": the
        # caller has to be able to tell "video-only by configuration" from
        # "video-only because the audio input failed", and /control/status has
        # to say so.
        self.audio_dropped: bool = False
        self.audio_dropped_reason: str | None = None
        # --- publisher liveness ---
        # ffmpeg's process being alive is necessary but NOT sufficient evidence
        # that anything is being published: throughout the E62 outage the process
        # was up, /control/status said "streaming", and MediaMTX had no path at
        # all. What is actually checkable from here is whether ffmpeg is still
        # CONSUMING what it is fed -- see write_frame.
        self.publisher_stalled: bool = False
        self.last_video_accepted_at: float | None = None   # monotonic
        # ffmpeg's own corroboration of a declared-rate/delivered-rate gap:
        # every "frames duplicated" line on stderr, counted (E70). Strictly
        # observability -- one-directional (fires only when the declared rate
        # is too HIGH), not quantitative, and coarse (fixed thresholds), so it
        # is deliberately NOT an input to any control decision; the settled
        # re-measurement (camera_session.RateWatcher) owns those.
        self.duplicated_frame_warnings: int = 0

    @property
    def audio_enabled(self) -> bool:
        """Whether audio can still be written to this publisher.

        False both for a sink built without audio and for one whose audio input
        was dropped mid-session (audio_dropped) -- callers must stop writing in
        either case, and the video half is unaffected by the difference.
        """
        return self._audio_transport is not None

    @property
    def publisher_alive(self) -> bool:
        """Whether this sink still has a publisher that is demonstrably taking
        frames.

        Three things have to be true, and the first two alone are what made the
        E62 outage invisible: the process exists, it has not exited, and it has
        not stopped reading the video pipe (write_frame's bounded drain). A
        caller that sees False must rebuild the sink -- there is no way back from
        here, since ffmpeg cannot be told to reopen an input or an output.
        """
        return (self.proc is not None and self.proc.returncode is None
                and not self.publisher_stalled)

    @property
    def audio_timeline_s(self) -> float | None:
        """Seconds of media this publisher's audio input has been handed.

        The audio input carries no -use_wallclock_as_timestamps (see start()),
        so its PTS come from its own sample count: this IS the audio stream's
        timeline, and it is read off the counter _write_audio_bytes already
        keeps rather than from a second tally that could disagree with it.
        None for a sink that was never given an audio input.
        """
        if self.audio_format is None or not self._audio_bytes_per_sample:
            return None
        rate = self.audio_format[1]
        if not rate:
            return None
        return self._audio_samples_written / float(rate)

    @property
    def audio_lead_s(self) -> float | None:
        """How far that audio timeline is AHEAD of the wall clock, in seconds.

        The quantity every pacing constant in this file is chosen against, made
        readable: the audio covers up to `_audio_started_at + timeline`, the
        video is stamped with the wall clock (-use_wallclock_as_timestamps), and
        the muxer can emit a video packet only while the first is past the
        second. Positive is the lead _AUDIO_PRIME_MS establishes, zero is the
        cliff the measured table above sits on either side of, and NEGATIVE
        means the audio timeline stopped advancing while real time did not --
        the shape of a video pipe ffmpeg has stopped reading.

        Derived, not measured separately, and only ever read by an observer
        (handover_trace.py): nothing in the publishing path consults it, so it
        cannot change what it is watching. None when there is no audio input, or
        before the pipe has been primed.
        """
        timeline = self.audio_timeline_s
        if timeline is None or self._audio_started_at is None:
            return None
        return (self._audio_started_at + timeline) - time.monotonic()

    async def start(self, width: int, height: int, fps: int = 15,
                    audio_format: tuple | None = None):
        self._wh = (width, height)
        fps = clamp_fps(fps)
        # Exposed so callers can log/reason about the fps actually given to ffmpeg
        # (for -framerate/-g) rather than whatever raw value they originally passed in --
        # confirmed live 2026-07-18 that logging the pre-clamp value elsewhere
        # (camera_session.py) made the doorbell's real 30fps look like "39fps" in
        # logs, since a burst-artifact measurement can exceed this clamp.
        self.fps = fps

        # Downscale the larger side to config.MAX_ENCODE_DIMENSION if the camera's
        # native resolution exceeds it -- see that constant for why. h264/yuv420p
        # needs even dimensions, so round down to the nearest even number rather
        # than risk ffmpeg rejecting an odd one.
        scale = min(1.0, c.MAX_ENCODE_DIMENSION / max(width, height))
        enc_width = max(2, (int(width * scale) // 2) * 2)
        enc_height = max(2, (int(height * scale) // 2) * 2)
        scale_filter = f"scale={enc_width}:{enc_height}" if scale < 1.0 else None

        url = c.rtsp_publish_url(self.camera_id)

        # Audio input/output fragments, spliced into common_in/common_out below so
        # every encoder variant picks them up by construction. Empty when audio is
        # off, and they are the ONLY difference between the two command lines --
        # the video input itself is now identical either way (see common_in).
        audio_in: list[str] = []
        audio_out: list[str] = []
        audio_read_fd = audio_write_fd = None
        pipe_fmt = sample_bytes = None
        if audio_format is not None:
            pipe_fmt, sample_bytes = _PCM_PIPE_FORMAT.get(str(audio_format[0]),
                                                          (None, None))
            if pipe_fmt is None:
                # Publishing video is worth far more than publishing audio, so an
                # unrecognised sample format degrades to the video-only pipeline
                # rather than failing the sink (and with it the camera).
                log.warning("%s: decoded audio is in an unsupported sample format "
                            "%r; publishing video only", self.camera_id, audio_format[0])
                audio_format = None
        if audio_format is not None:
            _fmt_name, audio_rate, audio_channels = audio_format
            audio_read_fd, audio_write_fd = os.pipe()
            audio_in = [
                "-f", pipe_fmt, "-ar", str(audio_rate), "-ac", str(audio_channels),
                # Raw PCM's parameters are fully described by the three options
                # above, so there is nothing to probe for; without this ffmpeg
                # waits for up to 5s of stream time on input 1 before it will
                # start the output, which is 5s of dark video.
                "-analyzeduration", "0", "-probesize", "32",
                # NO -use_wallclock_as_timestamps here, unlike the video input,
                # and this is a design choice rather than a workaround. With it
                # on, every read off this pipe is stamped with the moment it
                # happened to be flushed, so the audio timeline would follow
                # writer jitter -- a burst of frames after a stall would compress
                # into no time at all, and the samples in it would have nowhere
                # to go. Off, the PTS come from the sample count, which is what a
                # sample stream's duration actually is, and write_silence_to's
                # wallclock-derived fill is then the single thing tying that
                # count to real time and therefore to the video beside it.
                #
                # This flag is NOT what makes the two-input pipeline stall; that
                # was `-r` on the video input (see the long comment there).
                # Measured on the deployed 7.1.5 without `-r`, the pipeline
                # stays drained with the flag on BOTH inputs as well -- so the
                # asymmetry is deliberate, not load-bearing. It has not been run
                # live in the both-on form and there is no reason to.
                "-i", f"pipe:{audio_read_fd}",
            ]
            audio_out = [
                # Explicit maps: with two inputs, ffmpeg's default stream
                # selection would still pick these two, but it would do so
                # silently and differently if either input ever grew a stream.
                "-map", "0:v:0", "-map", "1:a:0",
                # AAC because it is the only codec BOTH live consumers accept --
                # HA's HLS worker takes aac/mp3 only, and Frigate's `-c copy`
                # recording preset needs an mp4-legal codec. Mono, at the rate the
                # frames actually arrived at.
                "-c:a", "aac", "-ac", "1", "-ar", str(audio_rate), "-b:a", "64k",
                # Fill-and-trim only (async=1 does not stretch), against the
                # wallclock timestamps both inputs carry: it absorbs the small
                # drift between "silence written on a sleep schedule" and real time.
                "-af", "aresample=async=1",
            ]

        # The camera's frame rate is declared with the rawvideo demuxer's own
        # `-framerate`, NEVER with `-r` before `-i`. The two look interchangeable
        # and are not, and both halves of that matter -- on BOTH command lines,
        # audio-enabled or not.
        #
        # `-r <fps>` before `-i` forces the INPUT STREAM's frame rate, which
        # silently overrides -use_wallclock_as_timestamps: the video timeline
        # then advances by exactly 1/fps per frame WRITTEN, not by elapsed real
        # time. A camera delivering fewer frames than the fps it was measured at
        # therefore publishes less media than wall time -- so an HLS consumer
        # falls progressively further behind live -- and with a second input it
        # is outright fatal, because the audio timeline IS real time
        # (write_silence_to sizes the fill from the wallclock). Audio then
        # outruns video permanently, the muxer's interleaver stops accepting
        # audio, and the pipe backs up until _write_audio_bytes calls the sink
        # dead -- "ffmpeg has stopped reading the audio pipe", restarting the
        # encoder (and kicking every RTSP reader) every few seconds.
        #
        # Declaring NOTHING is not the fix either, and this is the part that is
        # easy to miss: the rawvideo demuxer's `framerate` option defaults to 25
        # (`ffmpeg -h demuxer=rawvideo`), so with no declaration at all every
        # stream is re-rated to a constant 25fps regardless of the camera --
        # duplicating frames for a slower one and DROPPING them for a faster one
        # -- and `-g <fps>` then spans a different number of SECONDS than the ~1s
        # it is chosen for. `-framerate <fps>` sets the demuxer's rate (so the
        # output stays CFR at the camera's own rate, and the GOP stays ~1s) while
        # leaving the packet timestamps to -use_wallclock_as_timestamps, which is
        # exactly the combination this pipeline needs.
        #
        # Measured INSIDE the deployed container (ffmpeg 7.1.5-0+deb13u1, Debian
        # 13, h264_vaapi, 1280x720 -> 960x540, publishing live RTSP into
        # MediaMTX), 40s of wallclock per run:
        #                        media/wall   frames written -> encoded   GOP
        #   declared 15, fed  9fps
        #     -r 15            24.07s/40s     361 -> 361  (15fps out)     1.0s
        #     nothing          40.04s/40s     361 -> 1001 (25fps out)     0.6s
        #     -framerate 15    40.07s/40s     361 -> 601  (15fps out)     1.0s
        #   declared 30, fed 30fps
        #     -r 30            40.03s/40s    1201 -> 1201 (30fps out)     1.0s
        #     nothing          40.08s/40s    1201 -> 1002 (25fps out,     1.2s
        #                                    199 frames DROPPED)
        #     -framerate 30    40.03s/40s    1201 -> 1201 (30fps out)     1.0s
        #   declared  6, fed  6fps  (a rate one camera on this account runs at)
        #     -r 6             40.17s/40s     241 -> 241  (6fps out)      1.0s
        #     nothing          40.08s/40s     241 -> 1002 (25fps out)     0.24s
        #     -framerate 6     40.17s/40s     241 -> 241  (6fps out)      1.0s
        # The `-r` rows are the E58 defect: 40s of wall time published as 24s of
        # media, the exact 9/15 ratio of written-to-declared fps. The "nothing"
        # rows are why removing the flag is not enough on its own. The libx264
        # path was measured alongside the hardware one and matches row for row.
        #
        # With the audio input attached, same build, declared 15 while frames
        # were written at 9fps -- the shortfall that stalls with `-r`:
        #   * -r 15:         audio backlog crossed the sink's 512 kB limit at
        #                    9.6s, video timeline 24.07s per 40s of wall time.
        #   * nothing:       backlog stayed at 0 bytes for a 100s RTSP publish;
        #                    timeline 100.0s/100.0s. ffmpeg logs "More than 1000
        #                    frames duplicated" (the re-rate to 25fps).
        #   * -framerate 15: backlog stayed at 0 bytes for a 100s RTSP publish;
        #                    timeline 100.0s/100.0s, output CFR 15, no duplicate
        #                    warning.
        # Bisected against the alternatives on that same build, ALL of which
        # still stall at ~10s with `-r` present: -thread_queue_size (4096, either
        # or both inputs), dropping -analyzeduration/-probesize, -use_wallclock_
        # as_timestamps on both inputs or neither, -fps_mode passthrough/vfr,
        # -max_interleave_delta 0, dropping -af aresample, and swapping the input
        # ORDER. None of them is the fix; the input's rate declaration is.
        #
        # NOT a 7.x quirk: the same reproduction and the same behaviour were
        # confirmed on ffmpeg n9.0.1 (libx264). The earlier "works offline, fails
        # live" split was the test feed happening to run at exactly the declared
        # rate.
        common_in = [
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
            "-framerate", str(fps), "-use_wallclock_as_timestamps", "1",
            "-i", "pipe:0",
            *audio_in,
        ]
        common_out = [
            # GOP was a fixed 30-frame count, so keyframe interval (and therefore the
            # granularity HLS/WebRTC decoders can recover at) varied by camera --
            # exactly 1s at the living room's 30fps but 2s at the doorbell's ~15fps.
            # Tying it to the actual fps keeps a consistent ~1s keyframe interval
            # regardless of camera frame rate, which also keeps clean alignment with
            # HA's HLS segmenter (hardcoded 2.0s non-LL-HLS target, see
            # TARGET_SEGMENT_DURATION_NON_LL_HLS in HA core's stream/const.py --
            # not configurable without LL-HLS) instead of relying on the coincidence
            # that both cameras' previous fixed-frame-count GOPs happened to divide
            # evenly into 2.0s at their current measured fps.
            "-g", str(fps), "-bf", "0",
            *audio_out,
            "-f", "rtsp", "-rtsp_transport", "tcp", "-pkt_size", "1200", url,
        ]
        vaapi_vf = f"{scale_filter}," if scale_filter else ""
        vaapi_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-vaapi_device", c.VAAPI_DEVICE, *common_in,
            "-vf", f"{vaapi_vf}format=nv12,hwupload", "-c:v", "h264_vaapi",
            # Default 2: lets the encoder have up to 2 frames in flight for pipeline
            # parallelism, which mainly helps *throughput* across many concurrent
            # streams sharing one encode session -- not our case (one process, one
            # stream, real-time source rate well under this iGPU's capability even
            # sequentially). For a single live stream that extra frame in flight is
            # pure added latency with no throughput benefit, so force strictly
            # sequential encoding instead.
            "-async_depth", "1",
            *common_out,
        ]
        scale_vf = ["-vf", scale_filter] if scale_filter else []
        nvenc_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", *common_in, *scale_vf,
            # p1/ll: NVENC's fastest preset + low-latency tuning, mirroring the
            # same latency-over-throughput choice vaapi_args makes with
            # -async_depth 1 and software_args makes with ultrafast/zerolatency.
            "-c:v", "h264_nvenc", "-preset", "p1", "-tune", "ll", *common_out,
        ]
        software_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", *common_in, *scale_vf,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", *common_out,
        ]

        # Both attempts inherit the SAME read fd (nothing has been written to the
        # pipe yet, so a failed attempt consumes nothing), and the parent's copy is
        # closed once the choice is made -- keeping it open would mean a write into
        # a pipe ffmpeg is no longer reading never raises BrokenPipeError, so a dead
        # publisher would look healthy until the pipe filled and blocked forever.
        pass_fds = (audio_read_fd,) if audio_read_fd is not None else ()
        try:
            if c.ENCODE_BACKEND == "software":
                if not await self._try_start(software_args, pass_fds):
                    raise RuntimeError("ffmpeg failed to start with the software encoder")
                self.hardware_encode = False
            else:
                hw_args = nvenc_args if c.ENCODE_BACKEND == "nvenc" else vaapi_args
                if await self._try_start(hw_args, pass_fds):
                    self.hardware_encode = True
                else:
                    log.warning("%s: %s failed to start (device unavailable?), "
                                "falling back to software libx264", self.camera_id, c.ENCODE_BACKEND)
                    if not await self._try_start(software_args, pass_fds):
                        raise RuntimeError("ffmpeg failed to start with both hardware and software encoders")
                    self.hardware_encode = False
        except BaseException:
            if audio_write_fd is not None:
                os.close(audio_write_fd)
            raise
        finally:
            if audio_read_fd is not None:
                os.close(audio_read_fd)

        if audio_format is not None:
            _fmt_name, audio_rate, audio_channels = audio_format
            self.audio_format = tuple(audio_format)
            self._audio_bytes_per_sample = sample_bytes * audio_channels
            self._audio_silence_byte = _PCM_SILENCE_BYTE.get(str(_fmt_name), 0)
            try:
                await self._open_audio_writer(audio_write_fd)
            except Exception:
                # ffmpeg is already up and publishing video at this point, and
                # video is worth far more than audio: degrade to video-only
                # rather than raising and taking a working publisher down with
                # it. ffmpeg simply never sees anything on input 1. The same
                # latch a mid-session drop sets, so /control/status can say WHY
                # this publisher carries no audio.
                log.exception("%s: could not open the audio pipe; publishing "
                              "video only from this encoder", self.camera_id)
                self.audio_format = None
                self._audio_transport = None
                self.audio_dropped = True
                self.audio_dropped_reason = "the audio pipe could not be opened"
                with contextlib.suppress(OSError):
                    os.close(audio_write_fd)
            else:
                # Prime before anything else can write: see _AUDIO_PRIME_MS.
                self._audio_started_at = time.monotonic()
                primed = int(audio_rate * _AUDIO_PRIME_MS / 1000)
                self._write_audio_bytes(self._silence_bytes(primed), primed)

        # Mask the RTSP credentials: this URL now carries the MediaMTX password, and
        # container logs are persisted by Docker's json-file driver (and routinely
        # pasted into issues), so logging it verbatim would defeat the point of
        # requiring auth at all.
        log.info("%s: ffmpeg publishing %dx%d@%dfps (source %dx%d) to %s (%s, %s)",
                 self.camera_id, enc_width, enc_height, fps, width, height,
                 _redact_url(url),
                 f"hw/{c.ENCODE_BACKEND}" if self.hardware_encode else "sw/libx264",
                 f"audio aac mono from {self.audio_format[1]}Hz/"
                 f"{self.audio_format[2]}ch/{self.audio_format[0]}"
                 if self.audio_format else "video only")

    async def _open_audio_writer(self, write_fd: int):
        """Wrap the audio pipe's write end in a non-blocking transport.

        connect_write_pipe (public API) both sets the fd non-blocking and gives
        connection_lost when ffmpeg goes away; the alternative -- os.write from a
        thread -- would park an executor thread indefinitely on a wedged ffmpeg,
        and that executor is shared with write_frame's per-frame pixel copy.
        """
        loop = asyncio.get_running_loop()
        self._audio_pipe_file = os.fdopen(write_fd, "wb", 0)
        self._audio_transport, self._audio_protocol = await loop.connect_write_pipe(
            _AudioPipeProtocol, self._audio_pipe_file)

    async def _try_start(self, args: list[str], pass_fds: tuple = ()) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE, pass_fds=pass_fds,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=_STARTUP_GRACE_S)
        except asyncio.TimeoutError:
            pass  # still running past the grace period -- treat as a successful start
        else:
            stderr = (await proc.stderr.read()).decode(errors="replace")
            log.warning("%s: ffmpeg exited immediately (code %s): %s",
                        self.camera_id, proc.returncode, stderr.strip()[-500:])
            return False

        self.proc = proc
        self._stderr_task = asyncio.ensure_future(self._log_stderr())
        return True

    async def _log_stderr(self):
        try:
            while self.proc and self.proc.stderr:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                text = line.decode(errors="replace").rstrip()
                if "frames duplicated" in text:
                    self.duplicated_frame_warnings += 1
                log.warning("%s: ffmpeg: %s", self.camera_id, text)
        except Exception:
            pass

    async def write_frame(self, frame) -> bool:
        """Returns False if the pipe is broken (caller should restart the sink)."""
        # Never started (start() raised partway through), or already stopped. Report
        # this the same way as a broken pipe rather than raising AttributeError on
        # None.stdin below -- an exception here isn't caught by any caller and, from
        # inside the frame-hold task, propagates into an unrelated later session.
        if self.proc is None or self.proc.stdin is None:
            return False
        # Already known not to be reading (below). Answer from the latch rather
        # than spending another _VIDEO_DRAIN_TIMEOUT_S -- and another whole raw
        # frame of memory -- proving it again on every later call.
        if self.publisher_stalled:
            return False
        # ffmpeg was launched with a fixed `-s WxH` raw input size. A frame of any
        # other size writes the wrong number of bytes into the pipe, permanently
        # desynchronising the raw stream so every subsequent frame is decoded from
        # the wrong offset -- visible as persistent garbage rather than a clean
        # failure. Force a sink rebuild instead; the caller's own size check then
        # relaunches ffmpeg with the new dimensions.
        if self._wh is not None and (frame.width, frame.height) != self._wh:
            log.warning("%s: frame size changed mid-session (%dx%d -> %dx%d); "
                        "restarting the encoder rather than corrupting the stream",
                        self.camera_id, self._wh[0], self._wh[1], frame.width, frame.height)
            return False
        # to_ndarray()/tobytes() is a real synchronous CPU cost -- a full pixel-format
        # copy of the whole frame (e.g. ~3.1MB for this codebase's 1440x1440 doorbell
        # camera) done in pure Python/numpy, not I/O. Running it inline on the event
        # loop stalls every other task scheduled on it for that duration: the OTHER
        # camera's frame pump, Janus keepalives/WebRTC signaling, the control HTTP
        # server -- for both cameras running this every frame, continuously, since the
        # 2026-07-18 always-on change. run_in_executor moves the copy to a thread so
        # the loop stays responsive; only the actual pipe write/drain (already
        # non-blocking/async) still happens on the loop.
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, lambda: frame.to_ndarray(format="yuv420p").tobytes())
        try:
            self.proc.stdin.write(data)
            # BOUNDED, and that bound is the publisher-liveness check itself.
            # drain() resolves once the transport has handed this frame over, and
            # a whole raw frame is far larger than the pipe buffer, so it only
            # resolves because ffmpeg read it: completing is direct evidence the
            # publisher is still consuming. Unbounded (what this was), an ffmpeg
            # that stops reading -- blocked on its own RTSP output, or alive with
            # nowhere to publish -- parks this await forever, and the loop above
            # simply never comes back: no timeout, no failure, no reconnect, and
            # /control/status frozen on its last good values. That is the shape
            # of the E62 outage, where the process stayed up and the status kept
            # saying "streaming" while MediaMTX had no path at all.
            await asyncio.wait_for(self.proc.stdin.drain(),
                                   timeout=_VIDEO_DRAIN_TIMEOUT_S)
            self.last_video_accepted_at = time.monotonic()
            return True
        except asyncio.TimeoutError:
            # Latched: a publisher that stopped reading its input does not
            # recover, and ffmpeg has no way to be re-pointed at an output. The
            # caller rebuilds the sink (publisher_alive is now False).
            self.publisher_stalled = True
            log.error("%s: ffmpeg has not read a frame from the video pipe in "
                      "%.0fs -- the process is alive but is publishing nowhere; "
                      "rebuilding the encoder", self.camera_id, _VIDEO_DRAIN_TIMEOUT_S)
            return False
        except (BrokenPipeError, ConnectionResetError):
            return False

    def drop_audio(self, reason: str):
        """Give up on this sink's audio input from OUTSIDE the sink.

        The same one-way degradation the write paths below apply to a pipe that
        has gone bad, exposed for the one caller that can know something the
        sink cannot: camera_session gives up on a camera whose decoded audio
        shape keeps moving (see config.AUDIO_FORMAT_REBUILD_LIMIT), because the
        alternative is a publisher rebuild -- and every RTSP reader kicked --
        each time it moves. Idempotent, and it never touches the video half.
        """
        self._drop_audio(reason)

    def _drop_audio(self, reason: str):
        """Give up on this sink's audio input and keep publishing video.

        The same principle start() already applies to an unrecognised sample
        format, applied to a pipe that went bad mid-session: VIDEO IS WORTH MORE
        THAN AUDIO. This used to return "rebuild the sink" instead, which fails
        the session, tears the publisher down and kicks every RTSP reader off the
        path -- trading a working video feed for an audio input that was already
        gone. ffmpeg simply sees input 1 end (measured in the deployed container
        against `-f null -`, not a live publish: closing the write end mid-run,
        the video half kept being consumed at its usual ~1.9ms per frame for the
        remaining 435 frames, with no stall).

        Latched and one-way for this sink: ffmpeg cannot reopen an input, so the
        only way audio comes back is the next publisher rebuild, which happens on
        its own schedule (a frame-size change, a dead process, a format change).
        """
        if self.audio_dropped:
            return
        self.audio_dropped = True
        self.audio_dropped_reason = reason
        log.error("%s: %s -- dropping audio and publishing VIDEO ONLY from this "
                  "encoder (a rebuild would kick every RTSP reader)",
                  self.camera_id, reason)
        transport, self._audio_transport = self._audio_transport, None
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        self._audio_pipe_file = None

    def _silence_bytes(self, samples: int) -> bytes:
        """`samples` samples of digital silence in this sink's launched format."""
        return bytes([self._audio_silence_byte]) * (samples * self._audio_bytes_per_sample)

    def _write_audio_bytes(self, data: bytes, samples: int) -> bool:
        """Push raw PCM at the pipe.

        False means "nothing more will be written to this audio input". It is
        NOT a request to rebuild the publisher: every path here that fails calls
        _drop_audio first, so `audio_dropped` is set and the video half carries
        on. The one failure that DOES need a rebuild is a changed PCM shape,
        which write_audio detects before reaching this.
        """
        transport = self._audio_transport
        if transport is None:
            return False
        if self._audio_protocol.lost or transport.is_closing():
            self._drop_audio("the audio pipe was closed by the far end")
            return False
        if transport.get_write_buffer_size() > _AUDIO_PIPE_MAX_BUFFER:
            self._drop_audio(
                f"ffmpeg has stopped reading the audio pipe "
                f"({transport.get_write_buffer_size()} bytes queued)")
            return False
        transport.write(data)
        self._audio_samples_written += samples
        return True

    async def write_audio(self, frame) -> bool:
        """Feed one decoded av.AudioFrame to ffmpeg's audio input.

        Returns False when the audio input is beyond this sink's help. Two
        different situations produce that, and the caller tells them apart by
        reading `audio_dropped`: a frame whose PCM FORMAT no longer matches what
        ffmpeg was launched with needs a REBUILD (below), while an input that
        has been given up on -- a wedged pipe, or a run of malformed frames
        past _AUDIO_MALFORMED_LIMIT -- has already been dropped and leaves the
        video publish untouched. A one-off malformed frame is neither: it is
        skipped, its span filled with silence, and the call reports True.

        The format check is a BACKSTOP rather than the primary path: the caller
        compares each decoded frame's shape against `audio_format` before
        offering it here, so that it can log once per session rather than once
        per frame and can keep the audio timeline advancing with silence while
        the rebuild waits for a rotation (camera_session, E81). "Needs a
        rebuild" never means "fail the session"; the caller applies it at its
        next rotation, where the frame hold bridges the gap.

        The format check is the exact audio analogue of write_frame's frame-size
        check, and exists for the identical reason: ffmpeg was told the input's
        sample format, rate and channel count once, on the command line, and reads
        the pipe as a fixed-size sample stream. A frame in a different shape writes
        the wrong number of bytes per unit of time, which does not fail -- it
        permanently reinterprets every later sample from the wrong offset, so the
        audio becomes noise (and, with a changed rate, drifts against the video
        forever) instead of erroring. Forcing a rebuild is the only clean recovery.
        """
        if self._audio_transport is None:
            return False
        fmt = audio_format_of(frame)
        if fmt != self.audio_format:
            log.warning("%s: audio format changed mid-session (%s -> %s); "
                        "refusing the frame rather than corrupting the stream, "
                        "and leaving the rebuild to the caller's next rotation",
                        self.camera_id, self.audio_format, fmt)
            return False
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _audio_pcm_bytes, frame)
        expected = int(frame.samples) * self._audio_bytes_per_sample
        if len(data) != expected:
            # Same hazard from the other direction: the format matched but the
            # frame did not carry the number of bytes that format implies (a
            # planar frame that did not interleave, a partially-decoded frame).
            # NOT a rebuild -- the relaunched command line would be identical,
            # so a rebuild is a session-failure loop, not a repair. The frame
            # is dropped and its span filled from the wallclock, which keeps
            # the audio's lead over the video intact; a persistent run of
            # these is a broken decoder and latches video-only instead (see
            # _AUDIO_MALFORMED_LIMIT).
            self._audio_malformed_run += 1
            if self._audio_malformed_run >= _AUDIO_MALFORMED_LIMIT:
                self._drop_audio(
                    f"{self._audio_malformed_run} consecutive decoded audio "
                    f"frames did not match their declared shape (last: "
                    f"{len(data)} bytes for {frame.samples} samples of "
                    f"{self.audio_format})")
                return False
            if self._audio_malformed_run == 1:
                log.warning("%s: decoded audio frame is %d bytes, expected %d "
                            "for %d samples of %s; dropping it and filling with "
                            "silence (only the first such frame is logged)",
                            self.camera_id, len(data), expected, frame.samples,
                            self.audio_format)
            return await self.write_silence_to()
        self._audio_malformed_run = 0
        return self._write_audio_bytes(data, int(frame.samples))

    async def write_silence_to(self, now: float | None = None) -> bool:
        """Top the audio stream up to the current wallclock with silence.

        The sample count is derived from ELAPSED REAL TIME, never from counting
        how many times this was called: it writes exactly
        (elapsed_seconds * sample_rate) - samples_already_written samples, so the
        total written always tracks the wall clock no matter how much
        asyncio.sleep jitter, event-loop contention or scheduling delay sits
        between calls. Counting sleeps instead is invisible on repeated video
        frames (a frame is a frame whenever it lands) but is real, accumulating
        A/V drift on audio, because a sample stream's duration IS its length.

        CALL IT OFTEN. An ordinary call can only ever close the gap up to
        _AUDIO_SILENCE_MAX_BURST_S, and -- far more importantly -- the audio has
        to stay AHEAD of the video for ffmpeg to keep reading the video pipe at
        all (see _AUDIO_PRIME_MS for the mechanism and the measurements). Every
        caller here tops up at config.AUDIO_SILENCE_TOPUP_S, which is half the
        lead the prime establishes.

        TWO REGIMES, and which one a call is in is decided by what it finds, not
        by who called it. A deficit up to _AUDIO_CATCHUP_DEFICIT_S is a top-up
        that ran late and is clipped at the ordinary ceiling, repaying across
        successive calls; a deficit past it is a HOLE, and is closed in one write
        bounded by _AUDIO_CATCHUP_MAX_BYTES. Both constants carry the reasoning.
        Neither regime ever writes past the wall clock, so nothing here can
        overfill the pipe, and neither loses anything: the count is recomputed
        from elapsed real time on every call, so the total written always
        converges on exactly what the wall clock says.

        WHAT IS NOT DONE HERE, because it looks like the obvious shortcut and is
        wrong: moving `_audio_started_at` forward to make a deficit read zero --
        rebasing the audio origin, accepting the gap as dead air. The audio input
        carries no -use_wallclock_as_timestamps (see start()), so its PTS ARE its
        sample count. Skipping N seconds of samples does not skip N seconds of
        presentation time; it makes the audio stream N seconds shorter than the
        wallclock-stamped video beside it, so every later sample is presented N
        seconds early, for the life of the publisher. `-af aresample=async=1`
        cannot correct that either: it fills and trims against the audio's own
        timeline, not against the video's. A slow repayment is a bounded, one-off
        video stall; a rebase is a permanent A/V offset.

        Returns False if nothing was written because the audio input is gone,
        like write_audio -- and, like write_audio, that never means "rebuild".
        """
        if self._audio_transport is None or self._audio_started_at is None:
            return False
        rate = self.audio_format[1]
        now = time.monotonic() if now is None else now
        deficit = int((now - self._audio_started_at) * rate) - self._audio_samples_written
        if deficit <= 0:
            return True     # real audio is already at or ahead of the wallclock
        ceiling = int(rate * _AUDIO_SILENCE_MAX_BURST_S)
        if deficit > int(rate * _AUDIO_CATCHUP_DEFICIT_S):
            ceiling = max(ceiling,
                          _AUDIO_CATCHUP_MAX_BYTES // max(1, self._audio_bytes_per_sample))
        deficit = min(deficit, ceiling)
        return self._write_audio_bytes(self._silence_bytes(deficit), deficit)

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._stderr_task:
            self._stderr_task.cancel()
        # The audio fd is released HERE and nowhere else -- one open per sink, one
        # close per sink, and nothing in between (see __init__).
        if self._audio_transport is not None:
            self._audio_transport.close()
            self._audio_transport = None
        self._audio_pipe_file = None
