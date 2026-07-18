"""Feeds decoded av.VideoFrame objects to an ffmpeg subprocess, which encodes and
publishes (RTSP ANNOUNCE, not listen -- ffmpeg's RTSP muxer cannot act as a server)
into the bundled MediaMTX instance.

Load-bearing flags, found by empirical testing against this exact stack:
  -bf 0 -g 30       avoid B-frames / keep a short GOP so Home Assistant's PyAV-based
                    stream worker never has to reorder frames without DTS info.
  -pkt_size 1200    keep RTP payloads under MediaMTX's repacketization threshold --
                    letting MediaMTX repacketize strips DTS continuity and produces
                    "No dts in N consecutive packets" in HA's stream_worker.
Without both of these, the Generic Camera config flow's live "can we read the
stream" verification step times out with no clue in the REST response as to why.

Encoding is hardware-accelerated (VAAPI, via the host's Intel iGPU / QuickSync)
rather than libx264 -- the container needs /dev/dri passed through (see
docker-compose.yml) for this to work. `format=nv12,hwupload` converts the raw
software frames this class receives into the hardware surface h264_vaapi requires;
-g/-bf above are still respected by the hardware encoder and remain load-bearing
for the same reason.

If the VAAPI device is unavailable or broken (driver crash, device reset, a host
without the passthrough at all), ffmpeg exits almost immediately after launch --
`start()` detects that and falls back to software libx264 rather than leaving the
camera dark. This is deliberately re-attempted on every start() call (i.e. every
reconnect/rotation) rather than remembered as "permanently broken", so hardware
encoding resumes automatically the moment the device comes back.
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

    async def start(self, width: int, height: int, fps: int = 15):
        self._wh = (width, height)
        fps = max(1, min(int(round(fps)), 30))
        url = c.rtsp_publish_url(self.camera_id)
        common_in = [
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
            "-r", str(fps), "-use_wallclock_as_timestamps", "1",
            "-i", "pipe:0",
        ]
        common_out = [
            "-g", "30", "-bf", "0",
            "-f", "rtsp", "-rtsp_transport", "tcp", "-pkt_size", "1200", url,
        ]
        vaapi_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-vaapi_device", c.VAAPI_DEVICE, *common_in,
            "-vf", "format=nv12,hwupload", "-c:v", "h264_vaapi", *common_out,
        ]
        software_args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", *common_in,
            "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency", *common_out,
        ]

        if await self._try_start(vaapi_args):
            self.hardware_encode = True
        else:
            log.warning("%s: h264_vaapi failed to start (device unavailable?), "
                        "falling back to software libx264", self.camera_id)
            if not await self._try_start(software_args):
                raise RuntimeError("ffmpeg failed to start with both hardware and software encoders")
            self.hardware_encode = False

        log.info("%s: ffmpeg publishing %dx%d@%dfps to %s (%s)", self.camera_id, width, height, fps,
                  url, "hw/vaapi" if self.hardware_encode else "sw/libx264")

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
