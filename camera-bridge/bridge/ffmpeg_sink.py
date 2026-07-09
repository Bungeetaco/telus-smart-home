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
"""
import asyncio
import logging

from . import config as c

log = logging.getLogger(__name__)


class FfmpegSink:
    """Feeds decoded av.VideoFrame objects to ffmpeg, which encodes and publishes to MediaMTX."""

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self.proc: asyncio.subprocess.Process | None = None
        self._wh: tuple[int, int] | None = None
        self._stderr_task: asyncio.Task | None = None

    async def start(self, width: int, height: int, fps: int = 15):
        self._wh = (width, height)
        fps = max(1, min(int(round(fps)), 30))
        url = c.rtsp_publish_url(self.camera_id)
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-vaapi_device", c.VAAPI_DEVICE,
            "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", f"{width}x{height}",
            "-r", str(fps), "-use_wallclock_as_timestamps", "1",
            "-i", "pipe:0",
            "-vf", "format=nv12,hwupload",
            "-c:v", "h264_vaapi",
            "-g", "30", "-bf", "0",
            "-f", "rtsp", "-rtsp_transport", "tcp", "-pkt_size", "1200", url,
        ]
        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.ensure_future(self._log_stderr())
        log.info("%s: ffmpeg publishing %dx%d@%dfps to %s", self.camera_id, width, height, fps, url)

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
        data = frame.to_ndarray(format="yuv420p").tobytes()
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
