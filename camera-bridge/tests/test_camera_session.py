"""Tests for the per-camera reconnect loop (bridge/camera_session.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_camera_session.py

No network, no Janus, no ffmpeg: `JanusCameraSession`, `FfmpegSink`,
`restart_guard` and `os._exit` are replaced with scriptable fakes, and the
timing constants in `config.py` are shrunk to fractions of a second so a
165-second rotation cycle can be exercised in well under one. The two session-
teardown tests in section 6 are the exception: they drive the REAL
`JanusCameraSession`'s close()/keepalive against fake pc/websocket objects,
still entirely offline. Set `TEST_VERBOSE=1` to see the loop's own logging (it
is silenced by default -- several tests deliberately drive it through
`log.exception` paths).

Why this file exists: **every failure mode in this loop looks like a camera
going quiet, not like an error.** Nothing raises out of `run_camera_forever` --
it is designed not to -- so a regression here surfaces days later as "the
doorbell feed froze again", with no stack trace and nothing to bisect against.
The comments in `camera_session.py` enumerate bugs that already shipped once
each; the tests below are those comments made executable, so the second time is
caught here instead of on the porch.

Pinned here:

  1. rotation          -- the load-bearing invariant (docs/camera-bridge.md,
                          "The ~120s / 165s proactive session rotation -- do not
                          regress"). Sessions rotate on SESSION_MAX_LIFETIME_S
                          while frames are still flowing, and the ffmpeg
                          publisher deliberately OUTLIVES the rotation: one sink
                          across many sessions, never stopped in between.
  2. deadline vs silence -- the rotation deadline is re-checked when a frame read
                          times out, so a session that stops producing frames
                          entirely still rotates instead of hanging forever; and
                          silence BEFORE the deadline is still a failure. These
                          two share one `wait_for` and must not be collapsed.
  3. frame hold        -- the reconnect gap is bridged by repeating the last real
                          frame into the still-running publisher (readers see a
                          freeze, not a torn-down path), and the hold is bounded
                          by time-since-the-last-REAL-frame, so an offline camera
                          stops masquerading as live after FRAME_HOLD_MAX_S.
  4. fps measurement   -- the median of consecutive deltas, not span/(n-1): a
                          buffered burst at either end of the batch must not
                          contaminate the estimate (this shipped, producing a
                          bogus-but-plausible ~30fps for a real 15fps camera).
                          The 15fps *fallback* is never promoted to the camera's
                          remembered fps, and a real reading is never overwritten
                          by a later implausible one.
  5. sink repair       -- a sink whose `start()` raised is never adopted, a sink
                          whose process died is replaced, a frame-size change
                          replaces it, and nothing else does.
  6. escape hatch      -- MAX_CONSECUTIVE_SESSION_FAILURES ends the process, one
                          successful frame batch resets the count, the restart
                          guard's hold happens BEFORE the exit with the dead
                          session already closed, and a Cloudflare rate-limit
                          neither counts toward the hatch nor takes the fixed
                          backoff.
  7. close is bounded  -- `session.close()` can hang (aiortc's pc.close() under
                          certain ICE states) or raise; neither may wedge or
                          silently kill this camera's loop, which is what an
                          unbounded/unguarded await did.
  8. backoff           -- grows and caps, and the dead session is closed BEFORE
                          the sleep rather than after it (an orphaned session
                          keeps decoding RTP nobody drains for the whole wait).
  9. session teardown  -- janus_client's close() closes the gateway websocket
                          even when aiortc's pc.close() raises or the close
                          timeout's cancellation lands mid-close (otherwise a
                          wedged camera leaks one socket per ~165s rotation),
                          and a keepalive that sends into a dead websocket ends
                          its task quietly instead of finishing with an
                          exception nothing ever retrieves.
 10. dropped frames    -- each session's drop counter is folded into a
                          per-camera lifetime total in the status dict when the
                          session ends (healthy rotation and failure path
                          alike), so /control/status keeps the count across
                          rotations instead of losing it with each session.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c              # noqa: E402
from bridge import camera_session as cs     # noqa: E402
from bridge.telus_auth import RateLimited   # noqa: E402

if not os.environ.get("TEST_VERBOSE"):
    logging.disable(logging.CRITICAL)

CAMERA = {"id": "1234567", "description": "Front Door"}


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------

class _ExitCalled(BaseException):
    """Stands in for os._exit(): unwinds the loop instead of killing pytest.

    BaseException on purpose -- the escape hatch raises it from inside an
    `except Exception` block, and nothing in the loop may swallow it.
    """

    def __init__(self, code):
        self.code = code
        super().__init__(f"os._exit({code})")


class FakeOs:
    def __init__(self):
        self.exits = []
        self.exit_times = []

    def _exit(self, code):
        self.exits.append(code)
        self.exit_times.append(time.monotonic())
        raise _ExitCalled(code)


class FakeGuard:
    """restart_guard.record_exit() without the /shared state file."""

    def __init__(self, result=(0.0, 1)):
        self.result = result
        self.calls = []

    def record_exit(self):
        self.calls.append(time.monotonic())
        return self.result


class FakeFrame:
    __slots__ = ("width", "height", "seq")

    def __init__(self, width, height, seq):
        self.width, self.height, self.seq = width, height, seq


class FakeProc:
    def __init__(self):
        self.returncode = None


class FakeSink:
    """FfmpegSink's surface as camera_session.py uses it: proc, fps,
    hardware_encode, start(), write_frame() -> bool, stop()."""

    def __init__(self, camera_id):
        self.camera_id = camera_id
        self.proc = None          # only set by a start() that survives
        self.fps = None
        self.wh = None
        self.hardware_encode = False
        self.start_calls = []
        self.writes = []          # (monotonic, frame.seq)
        self.stopped = 0
        self.write_result = True
        self.write_error = None
        CTX.sinks.append(self)

    async def start(self, width, height, fps=15):
        self.start_calls.append((width, height, fps))
        if CTX.sink_start_hook is not None:
            CTX.sink_start_hook(self, width, height, fps)
        self.proc = FakeProc()
        self.fps, self.wh = fps, (width, height)

    async def write_frame(self, frame):
        # A sink whose start() raised, or whose process has exited, reports a
        # broken pipe rather than accepting the frame -- exactly what the real
        # FfmpegSink does, and the reason a poisoned sink cannot quietly look
        # healthy to this loop.
        if self.proc is None or self.proc.returncode is not None:
            return False
        self.writes.append((time.monotonic(), frame.seq))
        if self.write_error is not None:
            raise self.write_error
        return self.write_result

    async def stop(self):
        self.stopped += 1
        if self.proc is not None:
            self.proc.returncode = 0


class Plan:
    """One scripted Janus session.

    frames: [(delay_before, width, height), ...] delivered in order across BOTH
            frames() calls the loop makes (the real session pulls from one queue,
            so the second call must NOT re-deliver the first batch).
    tail:   what happens once the list is exhausted -- "stall" (never another
            frame), "end" (StopAsyncIteration), or ("repeat", interval, w, h).
    dropped_frames: what this session's drop counter reads when it ends (the
            real counter lives in janus_client._pump_track and is per-session).
    """

    def __init__(self, frames=(), tail="stall", connect_error=None,
                 connect_delay=0.0, close_error=None, close_hang=False,
                 on_frame=None, dropped_frames=0):
        self.frames = list(frames)
        self.tail = tail
        self.connect_error = connect_error
        self.connect_delay = connect_delay
        self.close_error = close_error
        self.close_hang = close_hang
        self.on_frame = on_frame
        self.dropped_frames = dropped_frames


class FakeSession:
    def __init__(self, camera, video_source):
        self.camera, self.video_source = camera, video_source
        self.plan = CTX.next_plan()
        self.created_at = time.monotonic()
        self.closed_at = None
        self.close_calls = 0
        self.frame_index = 0
        self._pending = list(self.plan.frames)
        self.dropped_frames = self.plan.dropped_frames   # the real one is a property
        CTX.sessions.append(self)

    async def connect_and_watch(self):
        if self.plan.connect_delay:
            await asyncio.sleep(self.plan.connect_delay)
        if self.plan.connect_error is not None:
            raise self.plan.connect_error

    def frames(self):
        return self._gen()

    async def _gen(self):
        while True:
            if self._pending:
                delay, width, height = self._pending.pop(0)
            elif self.plan.tail == "stall":
                await asyncio.Event().wait()
                return
            elif self.plan.tail == "end":
                return
            else:
                _, delay, width, height = self.plan.tail
            if delay:
                await asyncio.sleep(delay)
            CTX.seq += 1
            frame = FakeFrame(width, height, CTX.seq)
            idx = self.frame_index
            self.frame_index += 1
            if self.plan.on_frame is not None:
                self.plan.on_frame(idx)
            yield frame

    async def close(self):
        self.close_calls += 1
        if self.plan.close_hang:
            await asyncio.Event().wait()
        if self.plan.close_error is not None:
            raise self.plan.close_error
        self.closed_at = time.monotonic()


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

_FAST = {
    "WEBRTCUP_TIMEOUT_S": 1,
    "FIRST_FRAME_TIMEOUT_S": 2.0,
    "SESSION_MAX_LIFETIME_S": 0.4,
    "FRAME_SILENCE_TIMEOUT_S": 0.3,
    "FRAME_HOLD_MAX_S": 1.0,
    "RECONNECT_BACKOFF_INITIAL_S": 0.05,
    "RECONNECT_BACKOFF_MAX_S": 0.2,
    "MAX_CONSECUTIVE_SESSION_FAILURES": 3,
    "SESSION_CLOSE_TIMEOUT_S": 0.3,
    "RATE_LIMIT_RETRY_MARGIN_S": 0.0,
}


class Ctx:
    def __init__(self, plans):
        self.plans = list(plans) or [Plan()]
        self.sessions = []
        self.sinks = []
        self.status = {}
        self.gvs_calls = []          # monotonic time of each get_video_source
        self.gvs_hook = None         # async (index) -> None, may raise
        self.sink_start_hook = None  # (sink, w, h, fps) -> None, may raise
        self.os = FakeOs()
        self.guard = FakeGuard()
        self.seq = 0
        self.task = None
        self.exit_exc = None

    def next_plan(self):
        idx = len(self.sessions)
        return self.plans[min(idx, len(self.plans) - 1)]

    async def get_video_source(self, telus, camera_id):
        idx = len(self.gvs_calls)
        self.gvs_calls.append(time.monotonic())
        if self.gvs_hook is not None:
            await self.gvs_hook(idx)
        return {"attempt": idx}

    @property
    def sink(self):
        return self.sinks[-1] if self.sinks else None

    @property
    def state(self):
        return self.status.get(CAMERA["id"], {})


CTX = None


@contextlib.contextmanager
def scenario(plans, **overrides):
    """Install the fakes and the shrunk timing constants for one test."""
    global CTX
    ctx = Ctx(plans)
    CTX = ctx
    saved_cfg = {}
    for key, value in {**_FAST, **overrides}.items():
        saved_cfg[key] = getattr(c, key)
        setattr(c, key, value)
    saved = (cs.JanusCameraSession, cs.FfmpegSink, cs.restart_guard, cs.os)
    cs.JanusCameraSession, cs.FfmpegSink = FakeSession, FakeSink
    cs.restart_guard, cs.os = ctx.guard, ctx.os
    try:
        yield ctx
    finally:
        cs.JanusCameraSession, cs.FfmpegSink, cs.restart_guard, cs.os = saved
        for key, value in saved_cfg.items():
            setattr(c, key, value)
        CTX = None


def start(ctx):
    ctx.task = asyncio.ensure_future(
        cs.run_camera_forever(None, CAMERA, ctx.get_video_source, ctx.status)
    )
    return ctx.task


async def run_until(ctx, pred, timeout=15.0, why=""):
    """Let the loop run until `pred(ctx)` or the loop exits. Never hangs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if ctx.task.done():
            return
        if pred(ctx):
            return
        await asyncio.sleep(0.01)
    # Cancel before reporting: a loop left running past its own test keeps its
    # fakes alive into the next one, and a failing test must not take the rest
    # of the file down with it.
    ctx.task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await ctx.task
    raise AssertionError(f"timed out after {timeout}s waiting for {why or pred}")


async def stop(ctx):
    """Cancel the loop and surface an escape-hatch exit if one happened."""
    if ctx.task.done():
        exc = ctx.task.exception()
        if isinstance(exc, _ExitCalled):
            ctx.exit_exc = exc
        elif exc is not None:
            raise exc
        return
    ctx.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await ctx.task


def steady(count, interval, width=1280, height=720):
    return [(interval, width, height)] * count


def burst(count, width=1280, height=720):
    """A rotation's worth of buffered frames arriving back to back."""
    return [(0.0, width, height)] * count


# --------------------------------------------------------------------------
# 1. Rotation, and the publisher that outlives it
# --------------------------------------------------------------------------

async def test_rotation_keeps_one_publisher():
    """Sessions rotate on schedule while frames are still flowing, and the
    ffmpeg publisher is NOT torn down with them.

    Both halves are load-bearing. Without the rotation, alarm.com's own ~180s
    server-side kill wins the race and the feed goes dark; without a publisher
    that survives it, MediaMTX destroys the RTSP path on every cycle, kicking
    every reader and putting an HLS discontinuity in front of every browser.
    """
    with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720))],
                  SESSION_MAX_LIFETIME_S=0.4) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sessions) >= 3, why="3 sessions")
        sink = ctx.sink
        stops_during_run = sink.stopped
        sinks_during_run = len(ctx.sinks)
        writes_during_run = len(sink.writes)
        creations = [s.created_at for s in ctx.sessions[:3]]
        closed = [s.close_calls for s in ctx.sessions[:2]]
        state = dict(ctx.state)
        await stop(ctx)

    assert sinks_during_run == 1, f"publisher was recreated: {sinks_during_run} sinks"
    assert stops_during_run == 0, "publisher was stopped across a rotation"
    assert writes_during_run > 10, "frames stopped flowing to the one publisher"
    assert all(n >= 1 for n in closed), f"rotated sessions not closed: {closed}"
    for a, b in zip(creations, creations[1:]):
        cycle = b - a
        # 0.4s lifetime + the ~0.2s first-frame batch, generously bounded. The
        # regression this catches is a cycle that never comes at all.
        assert 0.3 <= cycle <= 1.5, f"rotation cycle was {cycle:.2f}s"
    assert state.get("state") == "streaming", state
    assert state.get("consecutive_failures") == 0, state


async def test_rotation_deadline_survives_total_frame_silence():
    """A session that stops producing frames entirely still rotates.

    The deadline check and the silence watchdog share one `wait_for`. Checking
    the deadline only *after* a frame landed is exactly what let the bridge hang
    forever once frames stopped coming -- so a timed-out read must re-check the
    deadline before it decides anything. Here the silence timeout is longer than
    the lifetime, so only the re-check can produce a rotation.
    """
    with scenario([Plan(frames=steady(6, 0.02), tail="stall")],
                  SESSION_MAX_LIFETIME_S=0.4, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sessions) >= 2, timeout=5.0,
                        why="a rotation despite total silence")
        state = dict(ctx.state)
        sessions = len(ctx.sessions)
        await stop(ctx)

    assert sessions >= 2, "a silent session never rotated"
    # A rotation is not a failure: nothing may count toward the escape hatch and
    # the status must not flap to "reconnecting".
    assert state.get("consecutive_failures") == 0, state
    assert state.get("last_error") is None, state


async def test_silence_before_the_deadline_is_a_failure():
    """The same silence, with the watchdog shorter than the lifetime, IS a
    failure -- the session is declared dead immediately rather than waiting out
    a full rotation lifetime on a camera that has already stopped."""
    with scenario([Plan(frames=steady(6, 0.02), tail="stall")],
                  SESSION_MAX_LIFETIME_S=10.0, FRAME_SILENCE_TIMEOUT_S=0.2,
                  MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.state.get("state") == "reconnecting",
                        timeout=5.0, why="the silence watchdog to fire")
        state = dict(ctx.state)
        await stop(ctx)

    assert state.get("state") == "reconnecting", state
    assert "no frame for" in (state.get("last_error") or ""), state
    assert state.get("consecutive_failures", 0) >= 1, state


async def test_dropped_frames_accumulate_across_sessions_into_the_status():
    """janus_client's drop counter is per-session, so it dies with every ~165s
    rotation -- before the running total, the throttled warning lines were the
    only trace drops ever left and /control/status said nothing at all. The
    loop folds each ending session's count into a per-camera lifetime total in
    the status dict, on the healthy rotation path AND the failure path alike.
    """
    plans = [
        Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720),
             dropped_frames=7),                      # retired by a healthy rotation
        Plan(connect_error=RuntimeError("no frames received before timeout"),
             dropped_frames=5),                      # retired by the failure path
        Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720)),
    ]
    with scenario(plans, SESSION_MAX_LIFETIME_S=0.3,
                  MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        start(ctx)
        # Well inside the first session's 0.3s lifetime: nothing has retired yet,
        # so the field must already exist -- the status shape is a contract and a
        # key that appears only after the first rotation is a different shape.
        await asyncio.sleep(0.05)
        early = ctx.state.get("dropped_frames", "absent")
        await run_until(ctx, lambda x: x.state.get("dropped_frames", 0) >= 12,
                        timeout=8.0, why="both retired sessions' drops in the status")
        state = dict(ctx.state)
        await stop(ctx)

    assert early == 0, (
        f"dropped_frames must be published as 0 from the first status entry, got "
        f"{early!r}")
    assert state.get("dropped_frames") == 12, (
        f"7 (rotated) + 5 (failed) should accumulate to 12 in the status, got "
        f"{state.get('dropped_frames')!r} -- the per-session counter is being lost "
        "with each session instead of folded into the lifetime total")


# --------------------------------------------------------------------------
# 2. The frame hold that bridges reconnect gaps
# --------------------------------------------------------------------------

async def test_frame_hold_bridges_the_reconnect_gap():
    """During a rotation the last real frame keeps being written to the
    publisher, so the RTSP publish never goes silent and readers see a freeze
    instead of a dropped path."""
    gap = {}

    async def slow_second_attempt(idx):
        if idx == 1:
            gap["start"] = time.monotonic()
            await asyncio.sleep(0.5)
            gap["end"] = time.monotonic()

    with scenario([Plan(frames=steady(10, 0.05), tail="stall")],
                  SESSION_MAX_LIFETIME_S=0.3, FRAME_SILENCE_TIMEOUT_S=5.0,
                  FRAME_HOLD_MAX_S=5.0) as ctx:
        ctx.gvs_hook = slow_second_attempt
        start(ctx)
        await run_until(ctx, lambda x: "end" in gap, timeout=8.0, why="the reconnect gap")
        writes = list(ctx.sink.writes)
        await stop(ctx)

    live_seqs = {seq for _, seq in writes}
    last_live_seq = max(live_seqs)
    held = [t for t, seq in writes
            if gap["start"] <= t <= gap["end"] and seq == last_live_seq]
    assert len(held) >= 3, (
        f"only {len(held)} held frames during a {gap['end'] - gap['start']:.2f}s gap -- "
        "the publisher went silent across the rotation")


async def test_frame_hold_is_bounded_by_time_since_a_real_frame():
    """The hold ends FRAME_HOLD_MAX_S after the last REAL frame, not after each
    invocation. An offline camera must stop masquerading as live and let the
    feed go dark honestly."""
    gap = {}

    async def very_slow_second_attempt(idx):
        if idx == 1:
            gap["start"] = time.monotonic()
            await asyncio.sleep(1.5)
            gap["end"] = time.monotonic()

    # Frames flow right up to the rotation, so the hold does start -- the bound
    # under test is how long it then survives with no live frame behind it.
    with scenario([Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720))],
                  SESSION_MAX_LIFETIME_S=0.3, FRAME_HOLD_MAX_S=0.5) as ctx:
        ctx.gvs_hook = very_slow_second_attempt
        start(ctx)
        await run_until(ctx, lambda x: "end" in gap, timeout=8.0, why="the reconnect gap")
        writes = list(ctx.sink.writes)
        await stop(ctx)

    in_gap = [t for t, _ in writes if gap["start"] <= t <= gap["end"]]
    assert len(in_gap) >= 3, f"the hold barely ran at all ({len(in_gap)} frames)"
    # Bounded: with a 0.5s bound, nothing may still be written in the last third
    # of a 1.5s gap.
    latest = max(in_gap) - gap["start"]
    assert latest < 1.0, (
        f"still holding {latest:.2f}s into the gap with FRAME_HOLD_MAX_S=0.5 -- "
        "an offline camera would look live forever")


async def test_a_failed_hold_task_does_not_fail_the_next_session():
    """The hold belongs to the PREVIOUS cycle. If it died, awaiting it from
    inside the next one must not fail a fresh, otherwise-healthy session -- and,
    being awaited before the sink-repair block, would do so on every cycle after
    it too."""
    async def break_writes_during_the_gap(idx):
        if idx == 1:
            ctx.sink.write_error = RuntimeError("ffmpeg pipe exploded during the hold")
            await asyncio.sleep(0.3)
            ctx.sink.write_error = None

    with scenario([Plan(frames=steady(8, 0.05), tail="stall")],
                  SESSION_MAX_LIFETIME_S=0.3, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
        ctx.gvs_hook = break_writes_during_the_gap
        start(ctx)
        await run_until(ctx, lambda x: len(x.sessions) >= 2 and x.sessions[1].frame_index > 2,
                        timeout=8.0, why="the second session to stream")
        state = dict(ctx.state)
        failures = state.get("consecutive_failures")
        await stop(ctx)

    assert state.get("state") == "streaming", f"the fresh session inherited the hold's failure: {state}"
    assert failures == 0, state


# --------------------------------------------------------------------------
# 3. fps measurement
# --------------------------------------------------------------------------

async def test_fps_is_the_median_delta_not_the_span():
    """A buffered burst at one end of the batch must not contaminate the
    estimate.

    The batch here is five back-to-back frames (what a rotation's buffered
    frames look like) followed by five at a real 10fps. span/(n-1) gives ~18fps
    -- wrong, but inside the 1-60 plausibility range, so it gets locked in as
    this camera's fps and skews ffmpeg's declared -r and -g for the life of the
    process. The median of consecutive deltas gives 10.
    """
    with scenario([Plan(frames=burst(5) + steady(5, 0.1),
                        tail=("repeat", 0.1, 1280, 720))],
                  FIRST_FRAME_TIMEOUT_S=4.0, SESSION_MAX_LIFETIME_S=5.0) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.sinks and x.sinks[0].fps is not None,
                        timeout=6.0, why="the sink to start")
        fps = ctx.sinks[0].fps
        await stop(ctx)

    assert 7 <= fps <= 12, (
        f"measured {fps}fps for a 10fps camera -- span/(n-1) math would give ~18")


async def test_the_fps_fallback_is_never_locked_in():
    """An implausible measurement uses the 15fps fallback for that session only.

    Promoting the fallback into the camera's remembered fps permanently locks
    the tolerance check to 7-22fps, so a camera whose real rate falls outside
    that band -- and whose very first batch happened to arrive as a burst --
    could never correct itself for the rest of the process's lifetime: every
    later good reading is rejected against the bad baseline. The second session
    here runs at 5fps precisely because 5 is outside that band, so a sink that
    comes up at 15 again means the real reading was thrown away.
    """
    def poison_after_the_batch(idx):
        if idx == 12 and ctx.sinks:
            ctx.sinks[0].proc.returncode = 1   # forces a fresh sink next cycle

    plans = [
        Plan(frames=burst(10), tail=("repeat", 0.05, 1280, 720),
             on_frame=poison_after_the_batch),
        Plan(frames=steady(10, 0.2), tail="stall"),
    ]
    with scenario(plans, FIRST_FRAME_TIMEOUT_S=4.0, SESSION_MAX_LIFETIME_S=0.5,
                  FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].fps is not None,
                        timeout=8.0, why="the second sink to start")
        first, second = ctx.sinks[0].fps, ctx.sinks[1].fps
        await stop(ctx)

    assert first == 15, f"an unmeasurable burst should fall back to 15fps, got {first}"
    assert 4 <= second <= 6, (
        f"the 5fps camera measured {second}fps -- the 15fps fallback was locked in as "
        "known_fps and its 7-22fps tolerance rejected the real reading")


async def test_a_real_fps_is_not_overwritten_by_a_burst():
    """Once measured, a camera's fps is remembered across rotations and a
    later burst-contaminated batch cannot overwrite it -- a camera's real fps
    does not change between rotations.

    The remembered value is 5fps here precisely because it is nowhere near the
    15fps fallback: a second sink that came up at 15 would mean the measurement
    was forgotten, not reused."""
    def poison_after_the_batch(idx):
        if idx == 11 and ctx.sinks:
            ctx.sinks[0].proc.returncode = 1

    plans = [
        Plan(frames=steady(10, 0.2), tail=("repeat", 0.05, 1280, 720),
             on_frame=poison_after_the_batch),
        Plan(frames=burst(10), tail="stall"),
    ]
    with scenario(plans, FIRST_FRAME_TIMEOUT_S=4.0, SESSION_MAX_LIFETIME_S=0.4,
                  FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].fps is not None,
                        timeout=10.0, why="the second sink to start")
        first, second = ctx.sinks[0].fps, ctx.sinks[1].fps
        await stop(ctx)

    assert 4 <= first <= 6, f"first measurement was {first}fps for a 5fps camera"
    assert second == first, (
        f"a burst batch overwrote the remembered {first}fps with {second}fps")


# --------------------------------------------------------------------------
# 4. Sink repair
# --------------------------------------------------------------------------

async def test_a_sink_whose_start_raised_is_never_adopted():
    """Adopting the sink before start() succeeded is what left a `proc=None`
    publisher behind: every later write_frame raised AttributeError on
    None.stdin, no path in the loop could repair it, and each otherwise-perfect
    session counted as a failure until the escape hatch restarted the bridge --
    long after the original blip (an RTSP ANNOUNCE to a momentarily-down
    MediaMTX) had cleared."""
    def fail_the_first_start(sink, width, height, fps):
        if len(CTX.sinks) == 1:
            raise RuntimeError("both encoders died in the startup grace period")

    with scenario([Plan(frames=steady(8, 0.02), tail=("repeat", 0.05, 1280, 720))],
                  SESSION_MAX_LIFETIME_S=5.0, MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        ctx.sink_start_hook = fail_the_first_start
        start(ctx)
        await run_until(ctx, lambda x: len(x.sinks) >= 2 and len(x.sinks[1].writes) > 2,
                        timeout=8.0, why="a healthy replacement sink")
        first, second = ctx.sinks[0], ctx.sinks[1]
        state = dict(ctx.state)
        await stop(ctx)

    assert first.proc is None, "the poisoned sink should never have a process"
    assert not first.writes, "frames were written to a sink whose start() raised"
    assert second.writes, "the replacement sink was never used"
    assert state.get("state") == "streaming", state
    # It was never adopted, so nothing ever had to tear it down. A sink that
    # gets stopped is one this loop had accepted as its publisher -- the
    # difference between "never adopted" and "adopted, then repaired next cycle
    # by the redundant dead-sink check", which is the guard being pinned here.
    assert first.stopped == 0, (
        "a sink whose start() raised was adopted as the publisher and then had to be "
        "torn down -- it should never have been assigned in the first place")


async def test_a_dead_publisher_is_replaced_at_the_next_rotation():
    """`proc is None` or a process that exited both mean dead. Treating either
    as healthy is unrecoverable inside this loop."""
    def kill_the_sink(idx):
        if idx == 12 and CTX.sinks:
            CTX.sinks[0].proc.returncode = 137

    with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                        on_frame=kill_the_sink)],
                  SESSION_MAX_LIFETIME_S=0.4) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].writes,
                        timeout=8.0, why="the replacement publisher")
        first_stopped = ctx.sinks[0].stopped
        started = list(ctx.sinks[1].start_calls)
        await stop(ctx)

    assert first_stopped == 1, "the dead publisher was never stopped"
    assert len(started) == 1, started


async def test_a_frame_size_change_replaces_the_publisher():
    """A mismatched raw feed would corrupt the encode, so a size change is the
    one non-fatal reason to rebuild an otherwise-healthy publisher."""
    plans = [
        Plan(frames=steady(10, 0.02, 1440, 1440), tail="stall"),
        Plan(frames=steady(10, 0.02, 1280, 720), tail="stall"),
    ]
    with scenario(plans, SESSION_MAX_LIFETIME_S=0.3, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].wh is not None,
                        timeout=8.0, why="the resized publisher")
        first, second = ctx.sinks[0], ctx.sinks[1]
        first_stopped, first_wh, second_wh = first.stopped, first.wh, second.wh
        await stop(ctx)

    assert first_wh == (1440, 1440) and second_wh == (1280, 720)
    assert first_stopped == 1, "the old-size publisher was left running"


# --------------------------------------------------------------------------
# 5. The consecutive-failure escape hatch
# --------------------------------------------------------------------------

async def test_the_escape_hatch_ends_the_process():
    """MAX_CONSECUTIVE_SESSION_FAILURES with no successful frame in between
    means something below this loop isn't being released -- a leaked/wedged
    aiortc resource that only a fresh process can clear (observed live: 26+
    consecutive failures across both cameras, needing a manual restart)."""
    with scenario([Plan(connect_error=RuntimeError("no frames received before timeout"))],
                  MAX_CONSECUTIVE_SESSION_FAILURES=3) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.task.done(), timeout=8.0, why="the escape hatch")
        await stop(ctx)
        sessions, guard_calls, exits = len(ctx.sessions), len(ctx.guard.calls), list(ctx.os.exits)

    assert exits == [1], f"the process was not ended: exits={exits}"
    assert sessions == 3, f"exited after {sessions} failures, expected 3"
    assert guard_calls == 1, "the restart guard was not consulted before exiting"


async def test_a_successful_batch_resets_the_failure_count():
    """"Consecutive" means consecutive. One good frame batch in between must
    clear the count, or a camera that fails intermittently but recovers each
    time would eventually restart the whole container for no reason."""
    good = Plan(frames=steady(3, 0.01), tail="end")
    bad = Plan(connect_error=RuntimeError("no frames received before timeout"))
    with scenario([bad, bad, good, bad, bad, bad],
                  MAX_CONSECUTIVE_SESSION_FAILURES=3) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.task.done(), timeout=10.0, why="the escape hatch")
        await stop(ctx)
        sessions, exits = len(ctx.sessions), list(ctx.os.exits)

    assert exits == [1], f"the process was never ended: exits={exits}"
    # Session 3 resets the count to 0 and then fails itself (its frame stream
    # ends), leaving 1 -- so the hatch lands on session 5. Without the reset the
    # first three failures alone would have ended the process at session 3.
    assert sessions == 5, (
        f"exited after {sessions} sessions -- the successful batch at #3 did not reset "
        "the count")


async def test_the_restart_guard_holds_the_process_before_exiting():
    """When the hatch keeps firing, the failures are coming from OUTSIDE this
    process and restarting only re-hammers the upstream (89 restarts in 7 days,
    one 14-minute tight loop). The guard holds this process first -- the
    container physically cannot restart while we sleep -- and the dead session
    is closed BEFORE that hold, because it is minutes long and a live session
    would keep decoding RTP nobody drains for all of it. The hold never cancels
    the exit."""
    with scenario([Plan(connect_error=RuntimeError("no frames received before timeout"))],
                  MAX_CONSECUTIVE_SESSION_FAILURES=2) as ctx:
        ctx.guard.result = (0.4, 7)
        start(ctx)
        await run_until(ctx, lambda x: x.task.done(), timeout=10.0, why="the guarded exit")
        await stop(ctx)
        exits = list(ctx.os.exits)
        guard_at, exit_at = ctx.guard.calls[0], ctx.os.exit_times[0]
        last = ctx.sessions[-1]
        closed_at, close_calls = last.closed_at, last.close_calls
        state = dict(ctx.state)

    assert exits == [1], f"the guard cancelled the exit: exits={exits}"
    # The hold really happened: the process stayed alive for it, so Docker could
    # not restart the container into the same upstream failure at loop speed.
    assert exit_at - guard_at >= 0.3, (
        f"exited {exit_at - guard_at:.2f}s after the guard asked for a 0.4s hold -- "
        "the hold was skipped, which is the restart loop it exists to prevent")
    assert state.get("state") == "restart_deferred", state
    assert close_calls == 1 and closed_at is not None, "the dead session was not closed"
    # ...and it was closed BEFORE the hold, not swept up by the outer `finally`
    # once the exit was already unwinding. The hold is minutes long in
    # production; a session left open decodes RTP nobody drains for all of it.
    assert closed_at < exit_at and closed_at - guard_at < 0.2, (
        f"the session was closed {closed_at - guard_at:.2f}s after the guard decided to "
        f"hold (exit at {exit_at - guard_at:.2f}s) -- it must be closed before the hold")


async def test_a_rate_limit_neither_counts_nor_takes_the_fixed_backoff():
    """Cloudflare's edge rate-limiter is an explicitly transient server-side
    condition, not the local wedge the escape hatch exists for -- and restarting
    is actively counterproductive there, since every restart re-runs a login
    into the same window. It also gets the server's own Retry-After, because the
    fixed 2/4/8/16/30s schedule just keeps re-entering that window instead of
    ever clearing it."""
    with scenario([Plan()],
                  MAX_CONSECUTIVE_SESSION_FAILURES=2,
                  RECONNECT_BACKOFF_INITIAL_S=1.5,
                  RECONNECT_BACKOFF_MAX_S=1.5) as ctx:
        async def rate_limited(idx):
            raise RateLimited(0.15)

        ctx.gvs_hook = rate_limited
        start(ctx)
        await run_until(ctx, lambda x: len(x.gvs_calls) >= 5, timeout=8.0,
                        why="5 rate-limited attempts")
        gaps = [b - a for a, b in zip(ctx.gvs_calls, ctx.gvs_calls[1:])]
        state = dict(ctx.state)
        exits = list(ctx.os.exits)
        await stop(ctx)

    assert not exits, "a Cloudflare rate-limit triggered the container-restart hatch"
    assert state.get("consecutive_failures") == 0, (
        f"a rate-limit counted toward the escape hatch: {state}")
    # rate_limit_sleep_s(0.15) with the margin zeroed for the test, vs a 1.5s
    # fixed backoff -- the two are unmistakable.
    assert all(0.1 <= g <= 0.9 for g in gaps), (
        f"retry gaps {[round(g, 2) for g in gaps]} follow the fixed backoff, not the "
        "server's Retry-After")


# --------------------------------------------------------------------------
# 6. Closing a session can hang or raise; neither may kill the camera
# --------------------------------------------------------------------------

async def test_close_with_timeout_is_bounded_and_swallows_errors():
    """Direct unit test of the two shapes aiortc's pc.close() actually takes.

    A hang left the loop unable to get back around to accumulate toward the
    failure count -- it was stuck on that very await -- so a wedge became
    unrecoverable rather than one more counted failure. A raise escaped `while
    True` entirely and returned from run_camera_forever, permanently and
    SILENTLY killing that one camera: discover_and_launch_cameras skips ids
    already in the registry, and CameraLifecycleManager holds the finished task
    forever without inspecting its result, so even asyncio's "exception was
    never retrieved" warning never fires.
    """
    saved = c.SESSION_CLOSE_TIMEOUT_S
    c.SESSION_CLOSE_TIMEOUT_S = 0.2
    try:
        class Hangs:
            async def close(self):
                await asyncio.Event().wait()

        class Raises:
            async def close(self):
                raise RuntimeError("pc.close() blew up")

        began = time.monotonic()
        # The outer wait_for is the point: if the bound under test is ever
        # removed, this must FAIL, not hang. An unbounded await here would wedge
        # the whole suite -- and in CI, the whole job -- with no verdict at all.
        try:
            await asyncio.wait_for(cs._close_with_timeout(Hangs(), "cam"), timeout=5.0)
        except asyncio.TimeoutError:
            raise AssertionError("a hanging close() was not bounded at all")
        elapsed = time.monotonic() - began
        assert elapsed < 1.0, f"a hanging close was not bounded ({elapsed:.2f}s)"
        assert elapsed >= 0.15, f"the timeout was not honoured ({elapsed:.2f}s)"

        await asyncio.wait_for(cs._close_with_timeout(Raises(), "cam"), timeout=5.0)
    finally:
        c.SESSION_CLOSE_TIMEOUT_S = saved


async def test_a_raising_close_does_not_kill_the_camera():
    """The same thing from inside the loop: both call sites sit in the reconnect
    loop's own except/finally, so an exception from cleanup would escape `while
    True` and end this camera forever."""
    with scenario([Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720),
                        close_error=RuntimeError("pc.close() blew up"))],
                  SESSION_MAX_LIFETIME_S=0.3) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sessions) >= 3, timeout=8.0,
                        why="3 rotations despite a raising close")
        alive = not ctx.task.done()
        sessions = len(ctx.sessions)
        await stop(ctx)

    assert alive, "the loop returned -- this camera would be dark until a restart"
    assert sessions >= 3, sessions


async def test_a_hanging_close_does_not_wedge_the_loop():
    with scenario([Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720),
                        close_hang=True)],
                  SESSION_MAX_LIFETIME_S=0.3, SESSION_CLOSE_TIMEOUT_S=0.2) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sessions) >= 3, timeout=10.0,
                        why="3 rotations despite a hanging close")
        sessions = len(ctx.sessions)
        alive = not ctx.task.done()
        await stop(ctx)

    assert alive and sessions >= 3, f"the loop wedged on close(): {sessions} sessions"


async def test_janus_close_never_abandons_the_websocket():
    """The REAL JanusCameraSession.close(), against fake pc/websocket objects.

    close() runs pc.close() first (ordering is deliberate and unchanged), and
    both camera_session call sites bound it with
    wait_for(SESSION_CLOSE_TIMEOUT_S). aiortc's pc.close() is the known-hangable
    call the bound exists for -- so it can raise, or the wait_for's cancellation
    can land mid-close. A plain sequential close abandoned the websocket to
    alarm.com's Janus gateway open in both cases, and the rotation loop opens a
    fresh session every ~165s: a wedged camera leaked one socket per rotation,
    invisibly, forever.
    """
    from bridge.janus_client import JanusCameraSession

    class RaisingPc:
        async def close(self):
            raise RuntimeError("pc.close() blew up")

    class HangingPc:
        async def close(self):
            await asyncio.Event().wait()

    class FakeWs:
        def __init__(self, error=None):
            self.closed = 0
            self.error = error

        async def close(self):
            self.closed += 1
            if self.error is not None:
                raise self.error

    def make_session(pc, ws):
        session = JanusCameraSession({"id": "cam"}, {"janus_token": "tok"})
        session.pc = pc
        session._ws = ws
        return session

    # pc.close() raising must still close the ws -- and the exception must still
    # propagate, since swallowing it is _close_with_timeout's job, not close()'s.
    ws = FakeWs()
    try:
        await make_session(RaisingPc(), ws).close()
        raise AssertionError("pc.close()'s exception vanished inside close()")
    except RuntimeError:
        pass
    assert ws.closed == 1, "pc.close() raised and the websocket was abandoned open"

    # The guarded ws close must not mask pc.close()'s exception with its own:
    # the pc's RuntimeError is what the caller logs, not the ws's OSError.
    ws = FakeWs(error=OSError("ws close failed too"))
    try:
        await make_session(RaisingPc(), ws).close()
        raise AssertionError("no exception propagated at all")
    except RuntimeError:
        pass
    assert ws.closed == 1, "the ws close was skipped when it was going to raise"

    # wait_for's cancellation landing mid-pc.close() -- what a hung close looks
    # like from _close_with_timeout -- must close the ws on its way out, and the
    # timeout must still surface (the cleanup may not swallow the cancellation).
    ws = FakeWs()
    try:
        await asyncio.wait_for(make_session(HangingPc(), ws).close(), timeout=0.1)
        raise AssertionError("a hanging pc.close() did not time out")
    except asyncio.TimeoutError:
        pass
    assert ws.closed == 1, (
        "the close timeout's cancellation abandoned the websocket -- one leaked "
        "socket per ~165s rotation on a wedged camera")


async def test_janus_keepalive_dies_quietly_when_the_session_is_already_dead():
    """The REAL _keepalive_loop, against a websocket whose send raises.

    The server force-closes every session at ~180s and keepalives run every 25s,
    so a keepalive sent into a just-dead websocket is a routine part of every
    unclean rotation, not an edge case. The loop must end QUIETLY: close() only
    cancel()s the task, which is a no-op once it has already finished, so a loop
    that dies by raising leaves a finished-with-exception task nobody ever
    inspects -- asyncio then logs "Task exception was never retrieved"
    tracebacks at GC time, recurring error-level noise for normal operation.
    Marking the session dead stays the recv loop's job either way.
    """
    import websockets.exceptions
    from bridge.janus_client import JanusCameraSession

    class DeadWs:
        def __init__(self, exc):
            self.exc = exc
            self.sends = 0

        async def send(self, _msg):
            self.sends += 1
            raise self.exc

    def make_session(ws):
        session = JanusCameraSession({"id": "cam"}, {"janus_token": "tok"})
        session._ws = ws
        session._session_id = 1
        return session

    saved = c.JANUS_KEEPALIVE_INTERVAL_S
    c.JANUS_KEEPALIVE_INTERVAL_S = 0.01
    try:
        # ConnectionClosed (the routine case) and any other send failure both
        # end the loop with NO stored exception -- task.exception() is the very
        # thing nothing in production ever retrieves.
        for exc in (websockets.exceptions.ConnectionClosedError(None, None),
                    RuntimeError("send blew up some other way")):
            ws = DeadWs(exc)
            task = asyncio.ensure_future(make_session(ws)._keepalive_loop())
            await asyncio.wait({task}, timeout=5.0)
            assert task.done(), "the keepalive loop kept running after a dead send"
            assert not task.cancelled()
            assert task.exception() is None, (
                f"the keepalive task finished with an exception nothing retrieves "
                f"({task.exception()!r}) -- this is the recurring 'Task exception "
                "was never retrieved' log noise")
            assert ws.sends == 1, f"a dead session was re-sent to: {ws.sends} sends"

        # ...but cancellation must still cancel: close() relies on it to stop the
        # keepalive of a LIVE session, and a blanket handler that ate
        # CancelledError would turn every clean rotation into a leaked task.
        c.JANUS_KEEPALIVE_INTERVAL_S = 60
        task = asyncio.ensure_future(make_session(DeadWs(RuntimeError("unused")))
                                     ._keepalive_loop())
        await asyncio.sleep(0)
        task.cancel()
        try:
            await task
            raise AssertionError("the keepalive loop swallowed its cancellation")
        except asyncio.CancelledError:
            pass
    finally:
        c.JANUS_KEEPALIVE_INTERVAL_S = saved


# --------------------------------------------------------------------------
# 7. Backoff
# --------------------------------------------------------------------------

async def test_backoff_grows_caps_and_closes_the_session_first():
    """The dead session is closed BEFORE the backoff sleep, not after.

    The outer `finally` runs only once the except block finishes -- i.e. after
    the sleep. Left running, the old session's _pump_track keeps decoding and
    enqueueing real RTP that nothing is draining anymore, for the entire backoff
    window: 300+ frames silently dropped in a single ~15s window, confirmed
    live, and indistinguishable from a real streaming problem.
    """
    with scenario([Plan(connect_error=RuntimeError("no frames received before timeout"))],
                  RECONNECT_BACKOFF_INITIAL_S=0.1, RECONNECT_BACKOFF_MAX_S=0.25,
                  MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.gvs_calls) >= 5, timeout=8.0,
                        why="5 backed-off attempts")
        gaps = [b - a for a, b in zip(ctx.gvs_calls, ctx.gvs_calls[1:])]
        # How long after each session was closed did the next attempt begin?
        waited_after_close = [
            ctx.gvs_calls[i + 1] - ctx.sessions[i].closed_at
            for i in range(3) if ctx.sessions[i].closed_at is not None
        ]
        closed = [s.close_calls for s in ctx.sessions[:4]]
        await stop(ctx)

    assert all(n >= 1 for n in closed), f"failed sessions were not closed: {closed}"
    assert gaps[1] > gaps[0] * 1.4, f"backoff did not grow: {[round(g, 2) for g in gaps]}"
    assert all(g < 0.6 for g in gaps), f"backoff exceeded its cap: {[round(g, 2) for g in gaps]}"
    assert len(waited_after_close) >= 3
    assert all(w >= 0.08 for w in waited_after_close), (
        f"the session was still open during the backoff sleep: "
        f"{[round(w, 3) for w in waited_after_close]}")


async def test_shutdown_stops_the_publisher():
    """Cancellation (what CameraLifecycleManager.deactivate does) must take the
    publisher and the hold task down with it -- a leaked ffmpeg would keep the
    MediaMTX path claimed and reject the next publisher."""
    with scenario([Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720))]) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.sink is not None and x.sink.writes,
                        why="a streaming publisher")
        sink, session = ctx.sink, ctx.sessions[-1]
        await stop(ctx)
        stopped, closed = sink.stopped, session.close_calls

    assert stopped >= 1, "the ffmpeg publisher was leaked on shutdown"
    assert closed >= 1, "the Janus session was leaked on shutdown"


TESTS = [
    test_rotation_keeps_one_publisher,
    test_rotation_deadline_survives_total_frame_silence,
    test_silence_before_the_deadline_is_a_failure,
    test_dropped_frames_accumulate_across_sessions_into_the_status,
    test_frame_hold_bridges_the_reconnect_gap,
    test_frame_hold_is_bounded_by_time_since_a_real_frame,
    test_a_failed_hold_task_does_not_fail_the_next_session,
    test_fps_is_the_median_delta_not_the_span,
    test_the_fps_fallback_is_never_locked_in,
    test_a_real_fps_is_not_overwritten_by_a_burst,
    test_a_sink_whose_start_raised_is_never_adopted,
    test_a_dead_publisher_is_replaced_at_the_next_rotation,
    test_a_frame_size_change_replaces_the_publisher,
    test_the_escape_hatch_ends_the_process,
    test_a_successful_batch_resets_the_failure_count,
    test_the_restart_guard_holds_the_process_before_exiting,
    test_a_rate_limit_neither_counts_nor_takes_the_fixed_backoff,
    test_close_with_timeout_is_bounded_and_swallows_errors,
    test_a_raising_close_does_not_kill_the_camera,
    test_a_hanging_close_does_not_wedge_the_loop,
    test_janus_close_never_abandons_the_websocket,
    test_janus_keepalive_dies_quietly_when_the_session_is_already_dead,
    test_backoff_grows_caps_and_closes_the_session_first,
    test_shutdown_stops_the_publisher,
]


async def main():
    failures = 0
    for test in TESTS:
        try:
            await test()
            print(f"  ok   {test.__name__}")
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
