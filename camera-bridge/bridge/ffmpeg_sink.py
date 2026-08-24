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

  The video input keeps `-use_wallclock_as_timestamps 1`; the AUDIO input must
  not have it (see the comment at the flag). The audio stream's timeline is
  therefore its own sample count, which is exactly why the silence fill sizes
  itself from elapsed real time -- that count is what keeps the audio aligned
  with the wallclock-stamped video. `-af aresample=async=1` (fill-and-trim only,
  no stretching) absorbs what is left.

  The encoder is AAC, and that is not a free choice: Home Assistant's HLS stream
  worker accepts only aac/mp3 for audio, and Frigate's `-c copy` recording preset
  needs a codec that is legal in an mp4 container. AAC is the only codec that
  satisfies both consumers, so Opus (what the gateway actually sends) is
  transcoded rather than passed through. The published audio is mono; the sample
  rate is whatever the decoded frames actually carry, never assumed.

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

# How much unwritten audio the pipe transport may hold before the sink calls
# itself broken. ffmpeg drains this pipe continuously; a backlog this large means
# it has stopped reading input 1 entirely, and buffering audio in this process
# forever would hide that instead of rebuilding the publisher.
_AUDIO_PIPE_MAX_BUFFER = 512 * 1024
# Silence written into the pipe the instant it is opened. ffmpeg's
# avformat_find_stream_info runs on input 1 before any camera frame exists, and a
# raw input with nothing in it is a blocking read; this hands it something to
# find. It is also why the audio input carries -analyzeduration 0 -probesize 32:
# raw PCM's parameters are fully described by -f/-ar/-ac, so there is nothing to
# probe for and no reason to let ffmpeg wait for more.
_AUDIO_PRIME_MS = 100
# Ceiling on a single silence top-up, so a long stall cannot dump minutes of
# silence into the pipe in one write. The deficit is not lost -- the next call
# recomputes it against the wallclock and keeps catching up.
_AUDIO_SILENCE_MAX_BURST_S = 2.0


def _redact_url(url: str) -> str:
    """Strip credentials out of a URL before it reaches a log line."""
    return _URL_CREDENTIALS_RE.sub("://***:***@", url)


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

    @property
    def audio_enabled(self) -> bool:
        return self._audio_transport is not None

    async def start(self, width: int, height: int, fps: int = 15,
                    audio_format: tuple | None = None):
        self._wh = (width, height)
        fps = max(1, min(int(round(fps)), 30))
        # Exposed so callers can log/reason about the fps actually given to ffmpeg
        # (for -r/-g) rather than whatever raw value they originally passed in --
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
        # off, which keeps the video-only command line byte-identical to what has
        # always shipped.
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
                # NO -use_wallclock_as_timestamps here, unlike the video input.
                # Measured against ffmpeg n9: with it set on BOTH inputs, ffmpeg
                # stops reading input 1 within a couple of seconds and never
                # resumes -- the audio pipe then backs up at real-time rate
                # forever (1.4 MB in 8s), which this sink can only read as a
                # dead publisher. Audio-only, the same input keeps up perfectly;
                # it is specifically the two-input combination that breaks. With
                # the flag off, the audio stream's PTS come from the sample count
                # and the pipe stays drained (backlog 0 over the same run).
                #
                # That makes write_silence_to's wallclock-derived sample count
                # MORE load-bearing, not less: the sample count is now the only
                # thing tying this stream to real time, and therefore to the
                # wallclock-stamped video beside it.
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

        common_in = [
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
            "-r", str(fps), "-use_wallclock_as_timestamps", "1",
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
            try:
                await self._open_audio_writer(audio_write_fd)
            except Exception:
                # ffmpeg is already up and publishing video at this point, and
                # video is worth far more than audio: degrade to video-only
                # rather than raising and taking a working publisher down with
                # it. ffmpeg simply never sees anything on input 1.
                log.exception("%s: could not open the audio pipe; publishing "
                              "video only from this encoder", self.camera_id)
                self.audio_format = None
                self._audio_transport = None
                with contextlib.suppress(OSError):
                    os.close(audio_write_fd)
            else:
                # Prime before anything else can write: see _AUDIO_PRIME_MS.
                self._audio_started_at = time.monotonic()
                primed = int(audio_rate * _AUDIO_PRIME_MS / 1000)
                self._write_audio_bytes(bytes(primed * self._audio_bytes_per_sample),
                                        primed)

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
                log.warning("%s: ffmpeg: %s", self.camera_id, line.decode(errors="replace").rstrip())
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
            await self.proc.stdin.drain()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _write_audio_bytes(self, data: bytes, samples: int) -> bool:
        """Push raw PCM at the pipe. False means the pipe is unusable."""
        transport = self._audio_transport
        if transport is None or self._audio_protocol.lost or transport.is_closing():
            return False
        if transport.get_write_buffer_size() > _AUDIO_PIPE_MAX_BUFFER:
            log.warning("%s: ffmpeg has stopped reading the audio pipe (%d bytes "
                        "queued); restarting the encoder", self.camera_id,
                        transport.get_write_buffer_size())
            return False
        transport.write(data)
        self._audio_samples_written += samples
        return True

    async def write_audio(self, frame) -> bool:
        """Feed one decoded av.AudioFrame to ffmpeg's audio input.

        Returns False if the caller should rebuild the sink -- a broken pipe, or a
        frame whose PCM shape no longer matches what ffmpeg was launched with.

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
                        "restarting the encoder rather than corrupting the stream",
                        self.camera_id, self.audio_format, fmt)
            return False
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, _audio_pcm_bytes, frame)
        expected = int(frame.samples) * self._audio_bytes_per_sample
        if len(data) != expected:
            # Same hazard from the other direction: the format matched but the
            # frame did not carry the number of bytes that format implies (a
            # planar frame that did not interleave, a partially-decoded frame).
            log.warning("%s: decoded audio frame is %d bytes, expected %d for "
                        "%d samples of %s; restarting the encoder",
                        self.camera_id, len(data), expected, frame.samples,
                        self.audio_format)
            return False
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

        Returns False on a broken pipe, like write_audio.
        """
        if self._audio_transport is None or self._audio_started_at is None:
            return False
        rate = self.audio_format[1]
        now = time.monotonic() if now is None else now
        deficit = int((now - self._audio_started_at) * rate) - self._audio_samples_written
        if deficit <= 0:
            return True     # real audio is already at or ahead of the wallclock
        deficit = min(deficit, int(rate * _AUDIO_SILENCE_MAX_BURST_S))
        return self._write_audio_bytes(bytes(deficit * self._audio_bytes_per_sample),
                                       deficit)

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
