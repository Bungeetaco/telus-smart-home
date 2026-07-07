import asyncio
import logging
import time

from . import config as c
from .janus_client import JanusCameraSession
from .ffmpeg_sink import FfmpegSink

log = logging.getLogger(__name__)


async def run_camera_forever(telus, camera: dict, get_video_source):
    backoff = c.RECONNECT_BACKOFF_INITIAL_S
    known_fps = None  # learned once, reused across rotations for this camera
    while True:
        session = None
        sink = None
        try:
            vs = await get_video_source(telus, camera["id"])
            session = JanusCameraSession(camera, vs)
            await asyncio.wait_for(session.connect_and_watch(), timeout=c.WEBRTCUP_TIMEOUT_S + 10)

            # Collect frames for up to ~1s (or 10 frames) to learn actual size/fps --
            # observed to vary per camera model (e.g. 1440x1440@15fps vs 1280x720@30fps).
            # Each frame fetch is individually timed out -- if the mountpoint negotiates
            # (webrtcup fires) but zero RTP ever actually arrives, we must not just hang
            # here; that's exactly the "camera registered but not streaming" case.
            sink = FfmpegSink(camera["id"])
            first_frames = []
            frame_iter = session.frames()
            deadline = time.monotonic() + c.FIRST_FRAME_TIMEOUT_S
            while len(first_frames) < 10 and time.monotonic() < deadline:
                remaining = max(0.1, deadline - time.monotonic())
                try:
                    frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=remaining)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    break
                first_frames.append((time.monotonic(), frame))
            if not first_frames:
                raise RuntimeError("no frames received before timeout")

            w, h = first_frames[0][1].width, first_frames[0][1].height
            # On a rotation, buffered frames can arrive in a near-instant burst,
            # making elapsed-time-based fps math produce nonsense (observed: 544fps,
            # 215fps for real 15fps/30fps cameras). Only trust a fresh measurement
            # that falls in a plausible camera fps range; otherwise reuse whatever we
            # already learned for this camera, since its real fps doesn't change
            # between rotations.
            fps = known_fps or 15
            if len(first_frames) >= 2:
                elapsed = first_frames[-1][0] - first_frames[0][0]
                measured = round((len(first_frames) - 1) / elapsed) if elapsed > 0 else None
                if measured is not None and 1 <= measured <= 60:
                    fps = measured
                elif known_fps is None:
                    log.warning("%s: implausible fps measurement %s, defaulting to %s",
                                camera["id"], measured, fps)
            known_fps = fps
            await sink.start(w, h, fps)
            for _, frame in first_frames:
                await sink.write_frame(frame)
            backoff = c.RECONNECT_BACKOFF_INITIAL_S  # reset after any successful frame batch
            log.info("%s: streaming %dx%d@%dfps", camera["id"], w, h, fps)

            # Alarm.com's own mountpoint timeout (see janus_client.py's "timeout_seconds":
            # 180 in the create body) actually kills the stream server-side at ~180s --
            # confirmed live. SESSION_MAX_LIFETIME_S must stay comfortably under that so
            # we always rotate first. Just as important: the deadline check below must
            # run even when NO frame ever arrives (camera stall, silent RTP death) --
            # checking it only after a frame lands is exactly what let the bridge hang
            # forever once frames stopped coming entirely.
            rotate_deadline = time.monotonic() + c.SESSION_MAX_LIFETIME_S
            frame_iter = session.frames()
            while True:
                remaining_to_rotate = rotate_deadline - time.monotonic()
                if remaining_to_rotate <= 0:
                    log.info("%s: proactive session rotation", camera["id"])
                    break
                timeout = min(remaining_to_rotate, c.FRAME_SILENCE_TIMEOUT_S)
                try:
                    frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=timeout)
                except asyncio.TimeoutError:
                    if time.monotonic() >= rotate_deadline:
                        log.info("%s: proactive session rotation", camera["id"])
                        break
                    raise RuntimeError(
                        f"no frame for {c.FRAME_SILENCE_TIMEOUT_S}s, treating session as dead"
                    )
                except StopAsyncIteration:
                    raise RuntimeError("frame stream ended unexpectedly")
                if not await sink.write_frame(frame):
                    raise RuntimeError("ffmpeg pipe broken, restarting sink")

        except Exception:
            log.exception("%s: camera session failed, reconnecting in %ss",
                           camera["id"], backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, c.RECONNECT_BACKOFF_MAX_S)
        finally:
            if sink:
                await sink.stop()
            if session:
                await session.close()
