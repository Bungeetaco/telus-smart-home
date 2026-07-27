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
"""
import asyncio
import logging

from . import config as c

log = logging.getLogger(__name__)

# How long to give a freshly-launched ffmpeg to prove it's actually alive before
# trusting it -- a doomed VAAPI attempt (missing/broken device) exits almost
# immediately; a working one is still running past this, quietly waiting on stdin.
_STARTUP_GRACE_S = 0.75


class FfmpegSink:
    """Feeds decoded av.VideoFrame objects to ffmpeg, which encodes and publishes to MediaMTX."""

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self.proc: asyncio.subprocess.Process | None = None
        self._wh: tuple[int, int] | None = None
        self._stderr_task: asyncio.Task | None = None
        self.hardware_encode: bool = False
        self.fps: int | None = None  # actual (clamped) fps in use -- see start()

    async def start(self, width: int, height: int, fps: int = 15):
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
        common_in = [
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
            "-r", str(fps), "-use_wallclock_as_timestamps", "1",
            "-i", "pipe:0",
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

        if c.ENCODE_BACKEND == "software":
            if not await self._try_start(software_args):
                raise RuntimeError("ffmpeg failed to start with the software encoder")
            self.hardware_encode = False
        else:
            hw_args = nvenc_args if c.ENCODE_BACKEND == "nvenc" else vaapi_args
            if await self._try_start(hw_args):
                self.hardware_encode = True
            else:
                log.warning("%s: %s failed to start (device unavailable?), "
                            "falling back to software libx264", self.camera_id, c.ENCODE_BACKEND)
                if not await self._try_start(software_args):
                    raise RuntimeError("ffmpeg failed to start with both hardware and software encoders")
                self.hardware_encode = False

        log.info("%s: ffmpeg publishing %dx%d@%dfps (source %dx%d) to %s (%s)", self.camera_id,
                  enc_width, enc_height, fps, width, height,
                  url, f"hw/{c.ENCODE_BACKEND}" if self.hardware_encode else "sw/libx264")

    async def _try_start(self, args: list[str]) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
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

    async def stop(self):
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                self.proc.kill()
        if self._stderr_task:
            self._stderr_task.cancel()
