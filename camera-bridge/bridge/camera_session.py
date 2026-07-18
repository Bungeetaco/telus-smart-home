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

    # The ffmpeg/RTSP publisher deliberately outlives individual Janus sessions.
    # Alarm.com kills every session server-side at ~180s, forcing a rotation
    # (see SESSION_MAX_LIFETIME_S); if the publisher is torn down with the
    # session, MediaMTX destroys the path the moment ffmpeg's RTSP connection
    # EOFs, kicking every reader AND firing runOnUnDemand -- which cancels this
    # very task via CameraLifecycleManager before it can reconnect, leaving the
    # path dark until a reader's own retry re-triggers runOnDemand (observed
    # live: up to ~107s of every 165s cycle). Downstream, each teardown also
    # puts a discontinuity + multi-second stall into HA's HLS output, which
    # ha-camera-stream in browsers does not survive (it drops to stills and
    # never retries). Keeping one publisher running and bridging session gaps
    # with a repeated last frame (see _hold_frames) removes the whole chain:
    # readers just see a brief freeze.
    sink: FfmpegSink | None = None
    sink_wh: tuple[int, int] | None = None
    last_frame = None
    last_live = 0.0  # monotonic time a real (non-repeated) frame last arrived
    hold_task: asyncio.Task | None = None

    async def _hold_frames():
        # Repeat the most recent real frame at the camera's fps while the Janus
        # session is being rotated/reconnected, so the RTSP publish never goes
        # silent (use_wallclock_as_timestamps keeps PTS advancing). Bounded by
        # time-since-last-real-frame, not per-invocation, so a genuinely
        # offline camera (e.g. the 2026-07-17 doorbell outage) stops
        # masquerading as live after FRAME_HOLD_MAX_S and fails honestly.
        interval = 1.0 / max(1, min(int(known_fps or 15), 30))
        while time.monotonic() - last_live < c.FRAME_HOLD_MAX_S:
            if not await sink.write_frame(last_frame):
                return  # ffmpeg died; next successful session start replaces it
            await asyncio.sleep(interval)
        log.warning("%s: no live frames for %ss, ending frame-hold -- letting "
                    "the stream go dark until the camera recovers",
                    camera["id"], c.FRAME_HOLD_MAX_S)

    def _start_hold():
        nonlocal hold_task
        if sink is None or last_frame is None:
            return
        if (hold_task is None or hold_task.done()) and \
                time.monotonic() - last_live < c.FRAME_HOLD_MAX_S:
            hold_task = asyncio.ensure_future(_hold_frames())

    async def _stop_hold():
        nonlocal hold_task
        if hold_task is not None:
            hold_task.cancel()
            try:
                await hold_task
            except asyncio.CancelledError:
                pass
            hold_task = None

    try:
        while True:
            session = None
            try:
                vs = await get_video_source(telus, camera["id"])
                session = JanusCameraSession(camera, vs)
                await asyncio.wait_for(session.connect_and_watch(), timeout=c.WEBRTCUP_TIMEOUT_S + 10)

                # Collect frames for up to ~1s (or 10 frames) to learn actual size/fps --
                # observed to vary per camera model (e.g. 1440x1440@15fps vs 1280x720@30fps).
                # Each frame fetch is individually timed out -- if the mountpoint negotiates
                # (webrtcup fires) but zero RTP ever actually arrives, we must not just hang
                # here; that's exactly the "camera registered but not streaming" case.
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
                # 215fps for real 15fps/30fps cameras -- and, less obviously, plausible-
                # looking but still-wrong values like 38-49fps for a real 15fps camera,
                # seen live 2026-07-18 during a rapid activate/deactivate crash loop).
                # A bare 1-60 range only catches the extreme cases; once known_fps is
                # set, require the new measurement to land within 50% of it too, since
                # a camera's real fps doesn't change between rotations -- a bad
                # burst-artifact reading would otherwise silently overwrite a correct
                # known_fps and mismatch ffmpeg's declared -r for every rotation after,
                # skewing its GOP-length-in-frames (-g 30) assumption relative to actual
                # wall-clock pacing.
                fps = known_fps or 15
                if len(first_frames) >= 2:
                    elapsed = first_frames[-1][0] - first_frames[0][0]
                    measured = round((len(first_frames) - 1) / elapsed) if elapsed > 0 else None
                    plausible = (
                        measured is not None and 1 <= measured <= 60
                        and (known_fps is None or 0.5 * known_fps <= measured <= 1.5 * known_fps)
                    )
                    if plausible:
                        fps = measured
                    elif known_fps is None:
                        log.warning("%s: implausible fps measurement %s, defaulting to %s",
                                    camera["id"], measured, fps)
                known_fps = fps

                # The hold must be stopped before touching the sink so nothing
                # else is writing while we inspect/replace it. The publisher is
                # only replaced if ffmpeg actually died or the camera's frame
                # size changed (never observed, but a mismatched raw feed would
                # corrupt the encode) -- otherwise the same RTSP session
                # carries straight on and readers never notice the rotation.
                await _stop_hold()
                if sink is not None and (
                        (sink.proc is not None and sink.proc.returncode is not None)
                        or sink_wh != (w, h)):
                    await sink.stop()
                    sink = None
                if sink is None:
                    sink = FfmpegSink(camera["id"])
                    await sink.start(w, h, fps)
                    sink_wh = (w, h)

                for _, frame in first_frames:
                    last_frame = frame
                    last_live = time.monotonic()
                    if not await sink.write_frame(frame):
                        raise RuntimeError("ffmpeg pipe broken, restarting sink")
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
                    last_frame = frame
                    last_live = time.monotonic()
                    if not await sink.write_frame(frame):
                        raise RuntimeError("ffmpeg pipe broken, restarting sink")

            except Exception:
                log.exception("%s: camera session failed, reconnecting in %ss",
                              camera["id"], backoff)
                _start_hold()
                # Close the dead/dying session BEFORE the backoff sleep, not after --
                # the outer `finally` below runs only once this except block finishes,
                # i.e. after the sleep. Left running, the old JanusCameraSession's
                # _pump_track keeps decoding and enqueueing real RTP frames that
                # nothing is draining anymore (the main loop that called frames() just
                # exited) for the *entire* backoff duration -- confirmed live
                # 2026-07-18: exactly this caused 300+ frames to be silently dropped
                # in a single ~15s backoff window once the new drop-counter logging
                # (janus_client.py) made it visible. Wasteful (decoding work nobody
                # wants) and, before that logging existed, indistinguishable from a
                # real streaming problem when it was actually just an orphaned session
                # nobody had told to stop yet.
                if session:
                    await session.close()
                    session = None
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, c.RECONNECT_BACKOFF_MAX_S)
            else:
                # Clean proactive rotation: bridge the reconnect gap.
                _start_hold()
            finally:
                if session:
                    await session.close()
    finally:
        await _stop_hold()
        if sink:
            await sink.stop()
