"""Starts/stops each camera's Janus WebRTC + decode/encode pipeline
(camera_session.run_camera_forever) on demand, driven by MediaMTX's own
runOnDemand/runOnUnDemand hooks (see mediamtx.yml, activate_camera.sh,
deactivate_camera.sh) via the control server's /control/cameras/by-path/* endpoints.

This class knows nothing about Janus/TELUS/MediaMTX itself -- it just needs to be
idempotent and safe to call activate()/deactivate() on repeatedly and concurrently for
the same camera (MediaMTX may fire runOnDemand again for a second nearly-simultaneous
reader before the first has finished starting, and a slow-to-stop pipeline could still
be tearing down when a new viewer triggers another activate()).
"""
import asyncio
import logging

log = logging.getLogger(__name__)


class CameraLifecycleManager:
    def __init__(self, start_camera_fn):
        """start_camera_fn(camera_dict) must be an async function equivalent to
        camera_session.run_camera_forever for that one camera -- an infinite loop that
        only returns via cancellation. This class doesn't call it directly except via
        asyncio.ensure_future, and only ever has at most one outstanding task per
        camera_id at a time."""
        self._start_camera_fn = start_camera_fn
        self._tasks: dict[str, asyncio.Task] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, camera_id: str) -> asyncio.Lock:
        return self._locks.setdefault(camera_id, asyncio.Lock())

    async def activate(self, camera: dict) -> None:
        camera_id = camera["id"]
        async with self._lock_for(camera_id):
            task = self._tasks.get(camera_id)
            if task is not None and not task.done():
                return  # already running -- idempotent no-op
            log.info("%s: activating camera pipeline (on-demand)", camera_id)
            self._tasks[camera_id] = asyncio.ensure_future(self._start_camera_fn(camera))

    async def deactivate(self, camera_id: str) -> None:
        async with self._lock_for(camera_id):
            task = self._tasks.pop(camera_id, None)
            if task is None or task.done():
                return  # already stopped -- idempotent no-op
            log.info("%s: deactivating camera pipeline (idle timeout)", camera_id)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                # run_camera_forever has its own internal retry loop and normally never
                # raises on its own -- this is a backstop, same reasoning as main.py's
                # top-level gather() comment.
                log.exception("%s: pipeline raised while shutting down", camera_id)
