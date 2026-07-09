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
import time

log = logging.getLogger(__name__)

# Backstop for deactivate() below: bounds how long we wait for a cancelled
# pipeline's own cleanup (sink.stop()'s bounded ffmpeg terminate, then
# session.close()'s aiortc pc.close(), which has no timeout of its own and is
# a known-hangable call under certain ICE states) before giving up on it. Set
# comfortably above sink.stop()'s own 5s ffmpeg-terminate timeout.
DEACTIVATE_TIMEOUT_S = 10

# Guards against a real, observed race: when a cold start doesn't produce a
# publisher within mediamtx.yml's runOnDemandStartTimeout, MediaMTX tears that
# attempt down (firing runOnUnDemand -> deactivate()) *and*, if a reader is
# still waiting, immediately retries (firing a fresh runOnDemand -> activate()
# again). Those two calls race, and since _tasks is keyed only by camera_id
# with no per-cycle identity, whichever HTTP request reaches this server
# second wins -- if that's the stale deactivate, it cancels the brand-new
# task moments after activate() created it, before it ever gets to try
# connecting. Confirmed live: repeated runOnDemandStartTimeout failures, 3+
# minutes straight, entirely caused by this and not by anything actually
# being slow.
#
# A *legitimate* idle-timeout deactivate can only be fired by MediaMTX after
# a camera's current cycle has had zero readers for runOnDemandCloseAfter
# (45s in mediamtx.yml) -- so it can never legitimately target a cycle that
# started less than that long ago. Any deactivate arriving sooner must be
# stale/misdirected from a previous cycle; ignoring it is therefore safe by
# construction, not just a heuristic. Set comfortably below both
# runOnDemandCloseAfter (45s) and runOnDemandStartTimeout (60s) so a genuine
# retry-cascade's own later, legitimate deactivate still gets through.
MIN_TASK_AGE_BEFORE_DEACTIVATE_S = 20


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
        self._started_at: dict[str, float] = {}

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
            self._started_at[camera_id] = time.monotonic()

    async def deactivate(self, camera_id: str) -> None:
        async with self._lock_for(camera_id):
            task = self._tasks.get(camera_id)
            if task is None or task.done():
                return  # already stopped -- idempotent no-op

            age = time.monotonic() - self._started_at.get(camera_id, 0.0)
            if age < MIN_TASK_AGE_BEFORE_DEACTIVATE_S:
                log.warning("%s: ignoring deactivate for a pipeline only %.1fs old -- "
                            "almost certainly a stale call racing a fresh activate, "
                            "see MIN_TASK_AGE_BEFORE_DEACTIVATE_S", camera_id, age)
                return

            del self._tasks[camera_id]
            log.info("%s: deactivating camera pipeline (idle timeout)", camera_id)
            task.cancel()
            try:
                await asyncio.wait_for(task, timeout=DEACTIVATE_TIMEOUT_S)
            except asyncio.CancelledError:
                pass
            except asyncio.TimeoutError:
                # task is still cancelled again by wait_for on timeout, but its cleanup
                # (most likely aiortc's pc.close()) is wedged and won't be waited on any
                # further -- log loudly and move on rather than holding this camera's
                # lock (and therefore every future activate/deactivate for it) forever.
                log.error("%s: pipeline did not finish shutting down within %ss after "
                          "cancel -- abandoning it, may leak resources", camera_id,
                          DEACTIVATE_TIMEOUT_S)
            except Exception:
                # run_camera_forever has its own internal retry loop and normally never
                # raises on its own -- this is a backstop, same reasoning as main.py's
                # top-level gather() comment.
                log.exception("%s: pipeline raised while shutting down", camera_id)
