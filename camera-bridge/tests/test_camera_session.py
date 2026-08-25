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
  4b. ADAPTIVE rate    -- and the rate is never LATCHED, because it is a device
                          setting: the resolution select is a Home Assistant
                          entity, so it changes with no restart and no signal to
                          the bridge, and `-framerate` then re-rates the whole
                          published stream (live: a camera declared at 29fps
                          duplicated 1000+ frames in 2.5 minutes). It is
                          re-measured while the stream runs and the publisher is
                          rebuilt through the SAME path a frame-size change
                          takes. Pinned: the burst at a session start is not
                          measured at all (a *plausible* burst -- 40fps against
                          a declared 15 -- because an implausible one proves
                          nothing), one batch never acts alone, a flapping
                          source converges instead of oscillating, the
                          hysteresis band is asymmetric (declaring too low DROPS
                          frames, declaring too high only duplicates them, and
                          burst error is biased high), a rebuild happens at a
                          rotation rather than by failing the session, the next
                          session's time-sized queue follows the new rate, and
                          the frame hold keeps repeating at the rate the RUNNING
                          ffmpeg was declared with while a change is pending.
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
 10. audio experiment  -- JANUS_REQUEST_AUDIO (default OFF) leaves the
                          mountpoint-create body byte-identical to what has
                          always shipped; with it on, a REJECTED audio create
                          falls back to that same video-only body instead of
                          failing the session, so a diagnostic can never feed
                          the consecutive-failure escape hatch. The knob is
                          PER-CAMERA -- one camera on this account tolerated the
                          audio m-line and one did not -- so one setting must
                          produce different wire behaviour for different cameras
                          in the same run, and an unnamed camera's body stays
                          byte-identical. A track this bridge does not consume is
                          DRAINED, because aiortc decodes it regardless and
                          queues it unbounded.
 12. first-frame timing -- the first-frame wait reports how long it ACTUALLY
                          took: on success alongside the streaming line, and on
                          failure the elapsed wait, the timeout it was measured
                          against, and whether webrtcup had been seen. "Waited
                          the full timeout and got nothing", "aborted at once"
                          and "ICE never came up" all used to print the same
                          five words.
 13. audio consumption -- JANUS_CONSUME_AUDIO (default OFF) leaves the ffmpeg
                          command line free of every audio token (pinned byte for
                          byte), the session video-only and an offered track
                          drained. The video input itself is the same either way:
                          it declares the camera's rate with `-framerate <fps>`
                          and never with `-r`, which would override
                          -use_wallclock_as_timestamps and advance the timeline
                          per frame written rather than per second elapsed --
                          publishing less media than wall time, and stalling the
                          audio pipe outright when a second input shares that
                          timeline. With it on, the SAME
                          publisher grows a second input and the decoded audio is
                          written into it -- rebuilding the publisher to add
                          audio would black the RTSP path on every rotation. The
                          rotation gap is filled with silence whose sample count
                          comes from ELAPSED WALLCLOCK, because a sample stream's
                          duration is its length and counting loop iterations
                          turns sleep jitter into permanent A/V offset. A changed
                          raw PCM shape forces a rebuild exactly as a changed
                          frame size does (writing it would desynchronise the
                          pipe into noise rather than fail) -- at a ROTATION,
                          never by failing the session, and a bounded number of
                          times per camera per process. The publisher is built
                          VIDEO-ONLY until a real audio frame has been decoded,
                          because the only alternative is a guessed shape,
                          which the first real frame then has to correct: doing
                          that by failing the session restarts the decoder's
                          per-session codec sniff, so a camera whose audio is
                          too sparse to finish the sniff inside the sizing
                          window never converges and publishes nothing at all
                          (E81). A camera that never sends an audio packet must
                          publish video forever. Every encoder
                          variant gets the audio input and encoder, since a
                          hardware path with audio and a software fallback
                          without it is the bug class this suite keeps catching.
                          CONSUME without REQUEST is a misconfiguration, not a
                          smaller feature: it degrades to off and says so once.
 14. audio pacing      -- the audio input PACES THE WHOLE PUBLISHER, and the
                          only trace of getting it wrong is video frames going
                          missing somewhere else. ffmpeg's muxer can only emit a
                          video packet once it holds audio covering that
                          packet's timestamp, so the audio has to stay ahead of
                          the video; the pipe is primed with a 100ms lead and
                          every top-up cadence has to stay under it. Pinned:
                          the fill's sample count comes from elapsed wallclock
                          (exactly, against a scripted clock), no single write
                          hands ffmpeg more than that lead, a large deficit is
                          repaid across bounded calls rather than in one burst,
                          a quiet camera is topped up under the lead, and the
                          frame hold tops up on the AUDIO deadline rather than
                          once per repeated video frame (which is 167ms on the
                          6fps camera). Measured in the deployed container: the
                          0.5s cadence that shipped stalled 120 of 900 frames
                          per 60s run, 0.1s stalled 162, 0.05s stalled none.
 15. publisher liveness -- "publishing" and "publishing to nowhere" have to be
                          distinguishable. The queue between decoder and
                          publisher is sized in TIME (~500ms) rather than in a
                          flat frame count that meant 267ms on one camera and
                          667ms on another; an audio pipe ffmpeg has stopped
                          reading degrades the sink to VIDEO-ONLY instead of
                          rebuilding the publisher and kicking every RTSP
                          reader; and write_frame's drain is bounded, because
                          an ffmpeg that stops reading its input parks the
                          camera's loop forever with the status frozen on
                          "streaming" -- which is exactly what E62 looked like
                          from outside.
 11. dropped frames    -- each session's drop counter is folded into a
                          per-camera lifetime total in the status dict when the
                          session ends (healthy rotation and failure path
                          alike), so /control/status keeps the count across
                          rotations instead of losing it with each session.
 16. handover trace    -- the instrumentation for the rotation window (E76),
                          which is only worth anything if it is honest about
                          two opposite things. OFF (the default) it must build
                          nothing and log nothing: it measures a window whose
                          whole problem is that ~14 frames go missing inside
                          it, so a diagnostic running there by default would be
                          changing what it watches. ON, its record must say
                          what actually happened: the marks in the order the
                          loop really does things, the audio timeline's advance
                          taken from the timeline rather than from time
                          passing, a muxer lead derived from the sink's own
                          sample counter and allowed to go NEGATIVE (which is
                          the failure it exists to report), drops placed
                          against the rotation rather than counted from the
                          whole session, and a retired session snapshotted
                          rather than held alive.
"""
import asyncio
import contextlib
import gc
import json
import logging
import os
import pathlib
import re
import sys
import time
import types
import weakref

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c              # noqa: E402
from bridge import camera_session as cs     # noqa: E402
from bridge import ffmpeg_sink as fs        # noqa: E402
from bridge import handover_trace as ht     # noqa: E402
from bridge import janus_client as jc       # noqa: E402
from bridge.janus_client import AUDIO_TIMEOUT  # noqa: E402
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


class FakeAudioFormat:
    def __init__(self, name):
        self.name = name
        self.is_planar = False


class FakeAudioFrame:
    """av.AudioFrame's surface as the audio path reads it: format name, sample
    rate, channel layout and sample count. That tuple IS the raw pipe's wire
    format (see ffmpeg_sink.audio_format_of), which is why it is what the guard
    compares."""

    def __init__(self, fmt, samples):
        name, rate, channels = fmt
        self.format = FakeAudioFormat(name)
        self.sample_rate = rate
        self.layout = types.SimpleNamespace(channels=tuple(range(channels)))
        self.samples = samples


class FakeSink:
    """FfmpegSink's surface as camera_session.py uses it: proc, fps,
    hardware_encode, audio_format/audio_enabled, publisher_alive, start(),
    write_frame() -> bool, write_audio() -> bool, write_silence_to() -> bool,
    stop().

    write_silence_to is the REAL FfmpegSink method, bound to this object. That is
    deliberate: the invariant under test is the arithmetic that turns elapsed
    wallclock into a sample count, and a fake reimplementation of it would only
    ever test itself. Everything it needs is modelled below -- the accounting
    fields and a _write_audio_bytes that records instead of writing to a pipe.

    The audio-drop path is modelled the same way: `drop_audio_after` makes the
    fake pipe go bad after N writes, and the response mirrors the real
    _drop_audio -- audio off, `audio_dropped` set, VIDEO UNTOUCHED. A fake that
    failed the whole sink instead would test the opposite of the invariant.
    """

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
        # --- publisher liveness ---
        # The real sink latches this when ffmpeg stops reading the video pipe
        # (its drain times out); a test sets it directly, since there is no pipe
        # here to wedge.
        self.publisher_stalled = False
        self.last_video_accepted_at = None
        self.duplicated_frame_warnings = 0
        # --- audio ---
        self.audio_dropped = False
        self.audio_dropped_reason = None
        # After this many successful PCM writes, the fake audio pipe goes bad.
        # None = never.
        self.drop_audio_after = None
        self.audio_format = None
        self.audio_writes = []    # (monotonic, samples) -- REAL decoded audio only
        self.pcm_writes = []      # (monotonic, samples) -- everything, silence included
        self._audio_transport = None      # only its truthiness is consulted
        self._audio_bytes_per_sample = 0
        self._audio_samples_written = 0
        self._audio_started_at = None
        self._audio_silence_byte = 0
        CTX.sinks.append(self)

    write_silence_to = fs.FfmpegSink.write_silence_to
    _silence_bytes = fs.FfmpegSink._silence_bytes
    # The REAL properties, for the same reason write_silence_to is real: the
    # handover record's whole claim is that the muxer lead is DERIVED from the
    # sample counter the sink already keeps, and a fake reimplementation of that
    # arithmetic would only ever test itself.
    audio_timeline_s = fs.FfmpegSink.audio_timeline_s
    audio_lead_s = fs.FfmpegSink.audio_lead_s

    @property
    def audio_enabled(self):
        return self._audio_transport is not None

    @property
    def publisher_alive(self):
        return (self.proc is not None and self.proc.returncode is None
                and not self.publisher_stalled)

    def _drop_audio(self, reason):
        """The real sink's degradation, modelled exactly: the audio input goes
        away and the video half is not touched."""
        if self.audio_dropped:
            return
        self.audio_dropped = True
        self.audio_dropped_reason = reason
        self._audio_transport = None

    # The public entry the loop calls when IT decides to give up on a camera's
    # audio (a shape that keeps moving past config.AUDIO_FORMAT_REBUILD_LIMIT).
    # The real sink's is the same one-line delegation, so a fake that diverged
    # here would be testing a degradation nothing ships.
    drop_audio = _drop_audio

    async def start(self, width, height, fps=15, audio_format=None):
        self.start_calls.append((width, height, fps, audio_format))
        # Runs BEFORE any state is adopted, so a raising hook models a start()
        # that failed (both encoders dying in the grace period) and leaves the
        # sink poisoned exactly as the real one would be.
        if CTX.sink_start_hook is not None:
            CTX.sink_start_hook(self, width, height, fps)
        self.proc = FakeProc()
        # CLAMPED, exactly as the real start() does before putting the value on
        # the command line. `fps` is what the loop asked for; `self.fps` is what
        # ffmpeg was actually declared with, and the two are not the same for a
        # measurement above 30 -- which is the number every rate comparison in
        # the loop has to be made against.
        self.fps, self.wh = fs.clamp_fps(fps), (width, height)
        if audio_format is not None:
            self.audio_format = tuple(audio_format)
            self._audio_bytes_per_sample = 2 * self.audio_format[2]
            self._audio_transport = object()
            # The real sink primes the pipe with a little silence the instant it
            # opens it, and starts its wallclock accounting from that moment.
            self._audio_started_at = time.monotonic()
            primed = int(self.audio_format[1] * fs._AUDIO_PRIME_MS / 1000)
            self._write_audio_bytes(bytes(primed * self._audio_bytes_per_sample), primed)
        # AFTER the audio setup, so a hook can model a sink that came up with
        # its audio already dropped (the real start() does that when the audio
        # pipe will not open) without start() overwriting what the hook set.
        if CTX.sink_started_hook is not None:
            CTX.sink_started_hook(self, width, height, fps)

    def _write_audio_bytes(self, data, samples):
        if self.proc is None or self.proc.returncode is not None:
            return False
        if self._audio_transport is None:
            return False
        if self.drop_audio_after is not None \
                and len(self.pcm_writes) >= self.drop_audio_after:
            self._drop_audio("ffmpeg has stopped reading the audio pipe "
                             "(636928 bytes queued)")
            return False
        self.pcm_writes.append((time.monotonic(), samples))
        self._audio_samples_written += samples
        return True

    async def write_audio(self, frame):
        """Mirrors the real guard: a frame whose PCM shape is not the one ffmpeg
        was launched with is REFUSED, because writing it would silently
        reinterpret every later sample from the wrong offset rather than fail."""
        if self._audio_transport is None:
            return False
        if fs.audio_format_of(frame) != self.audio_format:
            return False
        self.audio_writes.append((time.monotonic(), frame.samples))
        return self._write_audio_bytes(b"\0" * (frame.samples * self._audio_bytes_per_sample),
                                       frame.samples)

    async def write_frame(self, frame):
        # A sink whose start() raised, or whose process has exited, reports a
        # broken pipe rather than accepting the frame -- exactly what the real
        # FfmpegSink does, and the reason a poisoned sink cannot quietly look
        # healthy to this loop. A publisher that has stopped READING is the third
        # shape of the same thing (the real sink latches it when its bounded
        # drain times out) and reports the same way.
        if self.proc is None or self.proc.returncode is not None \
                or self.publisher_stalled:
            return False
        self.last_video_accepted_at = time.monotonic()
        self.writes.append((time.monotonic(), frame.seq))
        if CTX.write_frame_hook is not None:
            await CTX.write_frame_hook(self)
        if self.write_error is not None:
            raise self.write_error
        return self.write_result

    async def stop(self):
        self.stopped += 1
        self._audio_transport = None
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
    audio_interval: seconds between decoded audio frames, or None for a session
            that is offered no audio at all (every next_audio_frame call then
            times out, which is what the silence top-up exists for).
    audio_fmt: the (sample format, rate, channels) the DELIVERED frames carry.
    audio_peek: what session.audio_format reports when camera_session reads it to
            size the ffmpeg audio input -- i.e. whether a frame had already been
            decoded by then. `None` models "no audio yet", which is the case a
            sparse camera is in on every session (the decoder's codec sniff needs
            20 packets and a quiet room does not send them inside the sizing
            window), and the publisher is then built VIDEO-ONLY.
    """

    _UNSET = object()

    def __init__(self, frames=(), tail="stall", connect_error=None,
                 connect_delay=0.0, close_error=None, close_hang=False,
                 on_frame=None, dropped_frames=0, webrtcup_seen=True,
                 audio_interval=None, audio_fmt=("s16", 48000, 2),
                 audio_peek=_UNSET, audio_samples=960, dropped_audio_frames=0):
        self.audio_interval = audio_interval
        self.audio_fmt = audio_fmt
        self.audio_samples = audio_samples
        self.dropped_audio_frames = dropped_audio_frames
        self.audio_peek = (
            (audio_fmt if audio_interval is not None else None)
            if audio_peek is Plan._UNSET else audio_peek)
        self.frames = list(frames)
        self.tail = tail
        self.connect_error = connect_error
        self.connect_delay = connect_delay
        self.close_error = close_error
        self.close_hang = close_hang
        self.on_frame = on_frame
        self.dropped_frames = dropped_frames
        # Defaults True because the REAL connect_and_watch does not return until
        # the gateway's webrtcup event has arrived -- so by the time the loop
        # waits for a first frame, ICE is normally already up. That is exactly
        # what makes the flag diagnostic: "webrtcup seen, no frames" is a camera
        # that negotiated and then sent nothing, a different fault from ICE never
        # coming up at all.
        self.webrtcup_seen = webrtcup_seen


class FakeSession:
    def __init__(self, camera, video_source, consume_audio=False, fps=None):
        self.camera, self.video_source = camera, video_source
        # What the loop believed this camera's rate to be when it built the
        # session. The real JanusCameraSession sizes its decoded-frame queue from
        # it (janus_client.video_queue_maxsize), so a loop that stopped passing
        # it down would silently put every camera back on one flat frame count.
        self.fps = fps
        self.queue_maxsize = jc.video_queue_maxsize(fps)
        # The read-only surface the handover record samples (all properties on
        # the real session). Depth stays 0 here because these fakes have no
        # queue -- what the loop-level tests pin is that the record carries the
        # fields at all; the real ones are driven in
        # test_dropped_frames_are_timestamped_only_when_tracing.
        self.video_queue_depth = 0
        self.video_queue_limit = jc.video_queue_maxsize(fps)
        self.drop_times = ()
        self.plan = CTX.next_plan()
        self.created_at = time.monotonic()
        self.closed_at = None
        self.close_calls = 0
        self.frame_index = 0
        self._pending = list(self.plan.frames)
        self.dropped_frames = self.plan.dropped_frames   # the real one is a property
        self.webrtcup_seen = self.plan.webrtcup_seen     # ...as is this one
        # Passed DOWN by camera_session rather than read from config here -- the
        # real JanusCameraSession takes it the same way, so that one decision
        # cannot fork into two.
        self.consume_audio = consume_audio
        self.dropped_audio_frames = self.plan.dropped_audio_frames
        self.audio_format = self.plan.audio_peek         # ...also a property, really
        self.audio_sent = 0
        self._next_audio_at = None
        CTX.sessions.append(self)

    async def next_audio_frame(self, timeout):
        """The real signature: a decoded frame, AUDIO_TIMEOUT if none arrived
        within `timeout`, or None once the session is dead."""
        if not self.consume_audio or self.plan.audio_interval is None:
            await asyncio.sleep(timeout)
            return AUDIO_TIMEOUT
        now = time.monotonic()
        if self._next_audio_at is None:
            self._next_audio_at = now + self.plan.audio_interval
        wait = self._next_audio_at - now
        if wait > timeout:
            await asyncio.sleep(timeout)
            return AUDIO_TIMEOUT
        if wait > 0:
            await asyncio.sleep(wait)
        self._next_audio_at = time.monotonic() + self.plan.audio_interval
        self.audio_sent += 1
        return FakeAudioFrame(self.plan.audio_fmt, self.plan.audio_samples)

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
        self.sink_started_hook = None  # (sink, w, h, fps) -> None, after audio setup
        self.write_frame_hook = None # async (sink) -> None; slows the frame hold
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


@contextlib.contextmanager
def capture_logs(logger_name="bridge.camera_session", level=logging.INFO):
    """Collect formatted log messages from one logger.

    This file silences logging globally (several tests deliberately drive
    `log.exception` paths), so a test that asserts on a LOG LINE has to lift
    that for its own duration and put it back afterwards -- including for a test
    that fails part-way through.
    """
    records = []

    class Collect(logging.Handler):
        def emit(self, record):
            records.append(record.getMessage())

    logger = logging.getLogger(logger_name)
    handler = Collect()
    was_disabled = logging.root.manager.disable
    saved_level = logger.level
    logging.disable(logging.NOTSET)
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(saved_level)
        logging.disable(was_disabled)


@contextlib.contextmanager
def rate_knobs(**overrides):
    """Shrink the adaptive-rate constants for one test.

    `scenario` installs the loop's fakes; these tests drive RateWatcher directly
    against a synthetic clock, so they need the knobs without the fakes.
    """
    saved = {key: getattr(c, key) for key in overrides}
    for key, value in overrides.items():
        setattr(c, key, value)
    try:
        yield
    finally:
        for key, value in saved.items():
            setattr(c, key, value)


def feed_batch(watcher, interval, now, declared, frames=None):
    """Deliver one full settled batch to `watcher` at `interval` seconds apart.

    Returns (confirmed_rate_or_None, now). One extra frame beyond
    RATE_RESAMPLE_FRAMES because N deltas need N+1 arrivals.
    """
    confirmed = None
    for _ in range((frames or c.RATE_RESAMPLE_FRAMES) + 1):
        now += interval
        got = watcher.note_frame(now, declared)
        if got is not None:
            confirmed = got
    return confirmed, now


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
# 1b. First-frame timing, on both the success and the failure side
# --------------------------------------------------------------------------

async def test_a_first_frame_timeout_reports_how_long_it_actually_waited():
    """"no frames received before timeout" on its own names a symptom, not a
    fault, and three different faults produce it.

    Live, one camera failed this way every ~23s while its sibling on the same
    account streamed fine. The bare message could not say whether the loop had
    waited out the full FIRST_FRAME_TIMEOUT_S with nothing arriving (a camera
    that negotiated and then sent no RTP -- slowness) or aborted almost
    immediately (the frame iterator ending because the session was already
    dead), and it could not say whether ICE had come up at all. Those are
    opposite diagnoses. The elapsed wait, the timeout it was measured against,
    and the webrtcup state all have to be in the message itself, because the
    surrounding log lines are the same in every case.
    """
    # Waited it out: connect succeeds, webrtcup is up, no frame ever arrives.
    with scenario([Plan(frames=(), tail="stall", webrtcup_seen=True)],
                  FIRST_FRAME_TIMEOUT_S=0.4,
                  MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.state.get("last_error"), timeout=8.0,
                        why="the first-frame timeout")
        waited_out = ctx.state.get("last_error") or ""
        await stop(ctx)

    assert "no frames received before timeout" in waited_out, waited_out
    match = re.search(r"waited ([0-9.]+)s of ([0-9.]+)s", waited_out)
    assert match, (
        f"the message does not carry the ACTUAL elapsed wait against the timeout "
        f"it was measured with -- 'waited the whole timeout' and 'aborted early' "
        f"are indistinguishable: {waited_out}")
    assert match.group(2) == "0.4", (
        f"the reported timeout is not the one actually in force: {waited_out}")
    assert 0.35 <= float(match.group(1)) <= 0.9, (
        f"a wait that ran the full 0.4s timeout is reported as {match.group(1)}s: "
        f"{waited_out}")
    assert "webrtcup seen" in waited_out, (
        f"the message cannot tell 'ICE never came up' from 'ICE came up and no "
        f"media flowed': {waited_out}")

    # Aborted early: the frame stream ends at once (StopAsyncIteration), so the
    # loop breaks out long before the timeout -- and ICE never came up either.
    # The timeout is an INT here (as it is in production, FIRST_FRAME_TIMEOUT_S=20)
    # so the reported form stays "5.0s" rather than "5s" -- one shape to grep for,
    # whatever the constant happens to be.
    with scenario([Plan(frames=(), tail="end", webrtcup_seen=False)],
                  FIRST_FRAME_TIMEOUT_S=5,
                  MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.state.get("last_error"), timeout=8.0,
                        why="the early first-frame abort")
        aborted = ctx.state.get("last_error") or ""
        await stop(ctx)

    match = re.search(r"waited ([0-9.]+)s of ([0-9.]+)s", aborted)
    assert match, aborted
    assert match.group(2) == "5.0", aborted
    assert float(match.group(1)) < 0.5, (
        f"an abort that took no time at all is reported as if it had waited out "
        f"the 5.0s timeout: {aborted}")
    assert "webrtcup NOT seen" in aborted, (
        f"webrtcup state is not reported, or is reported as seen when it was not: "
        f"{aborted}")


async def test_the_streaming_line_reports_how_long_the_first_frame_took():
    """The healthy-side counterpart: how close this camera runs to the cliff.

    A first frame at 2s and a first frame at 18s against a 20s
    FIRST_FRAME_TIMEOUT_S are the same "streaming" line and the same green
    status, but the second is one blip from the reconnect loop above. It rides
    on the existing streaming line rather than a line of its own -- a second
    per-rotation INFO line saying half of what that one says is log volume for
    nothing.
    """
    with capture_logs() as lines:
        # The first frame is deliberately slow, so a timing measured from the
        # wrong instant (e.g. from the frame batch rather than from when the
        # wait began) cannot accidentally match.
        with scenario([Plan(frames=[(0.5, 1280, 720)] + steady(5, 0.02),
                            tail=("repeat", 0.02, 1280, 720))],
                      FIRST_FRAME_TIMEOUT_S=4.0, SESSION_MAX_LIFETIME_S=5.0) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("state") == "streaming",
                            timeout=8.0, why="a streaming session")
            await stop(ctx)

    streaming = [line for line in lines if ": streaming " in line]
    assert streaming, f"no streaming line was logged at all: {lines}"
    assert len(streaming) == 1, (
        f"the first-frame timing was added as an extra line instead of extending "
        f"the streaming line: {streaming}")
    line = streaming[0]
    assert "1280x720" in line, f"the streaming line lost its size/fps: {line!r}"
    match = re.search(r"first frame after ([0-9.]+)s", line)
    assert match, (
        f"the streaming line does not report the first-frame latency alongside the "
        f"size/fps it already reported: {line!r}")
    assert 0.4 <= float(match.group(1)) <= 1.2, (
        f"the first-frame latency reads {match.group(1)}s for a frame that took "
        f"0.5s -- it is not measured from when the wait began: {line!r}")


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


async def test_the_rate_hysteresis_band_is_asymmetric_and_clears_observed_jitter():
    """A measurement only counts as a CHANGE if it leaves the band, and the band
    is deliberately not symmetric.

    The two ways of being wrong are not equally bad, and both are measured, in
    ffmpeg_sink's own in-container table:

      * declared HIGHER than the source delivers -> ffmpeg pads the output to CFR
        with duplicates (361 written frames encoded as 601). Live, a camera
        declared at 29fps logged "More than 1000 frames duplicated" inside 2.5
        minutes. Wasteful; nothing is lost.
      * declared LOWER than the source delivers -> ffmpeg DROPS real frames (1201
        written encoded as 1002, 199 gone). That content does not come back.

    Measurement error is asymmetric the same way: buffered frames arrive
    back-to-back, so a contaminated batch reads FASTER than reality and never
    slower. "The camera sped up" is therefore the claim an artifact makes, and it
    gets the wider band; "the camera slowed down" is both likelier to be true and
    costlier to ignore, and gets the narrower one.

    The band must clear the jitter this account actually produces -- the same
    camera reading 6 then 7 across restarts, and a camera SET to 15 delivering
    ~13.4 -- without swallowing a real setting change. Correcting that 13.4 to 13
    would be a bug, not an improvement: 15 is the real setting, and declaring 13
    starts dropping the frames that do arrive on time.
    """
    # Jitter, both directions, from real observations. None of it is a change.
    assert not cs._rate_moved(6, 7), "6 -> 7 is the restart-to-restart jitter this camera shows"
    assert not cs._rate_moved(7, 6), "7 -> 6 is the same jitter seen the other way round"
    assert not cs._rate_moved(15, 13), (
        "a camera SET to 15 that delivers ~13.4 was corrected to 13 -- declaring "
        "below the setting drops the frames that DO arrive at 15")
    assert not cs._rate_moved(15, 17), "a mild over-read of a 15fps camera is not a change"
    assert not cs._rate_moved(30, 29), "29 vs 30 is a rounding difference, not a setting"

    # Real setting changes, from the rates this account has actually produced.
    assert cs._rate_moved(15, 7), "15 -> 7 (-53%) is a setting change and must rebuild"
    assert cs._rate_moved(15, 30), "15 -> 30 (+100%) is a setting change and must rebuild"
    assert cs._rate_moved(6, 15), "6 -> 15 (+150%) is a setting change and must rebuild"
    assert cs._rate_moved(29, 22), (
        "29 -> ~22, the drop implied by the live duplication rate, was treated as "
        "jitter -- that is the case this whole mechanism exists for")

    # The asymmetry itself, at a step size that lands between the two bands: the
    # same relative move is a change downward and jitter upward.
    down = round(20 * (1 - (c.RATE_CHANGE_TOLERANCE_DOWN + c.RATE_CHANGE_TOLERANCE_UP) / 2))
    up = round(20 * (1 + (c.RATE_CHANGE_TOLERANCE_DOWN + c.RATE_CHANGE_TOLERANCE_UP) / 2))
    assert cs._rate_moved(20, down) and not cs._rate_moved(20, up), (
        f"the band is symmetric ({down} and {up} against 20 were treated alike) -- "
        "a burst-inflated reading is exactly what the upward half must resist, and "
        "a real slowdown is what the downward half must catch")
    assert c.RATE_CHANGE_TOLERANCE_UP > c.RATE_CHANGE_TOLERANCE_DOWN, (
        c.RATE_CHANGE_TOLERANCE_UP, c.RATE_CHANGE_TOLERANCE_DOWN)

    # Nothing declared yet is not "a change": there is no command line to differ
    # from, and the first sink is built from the first batch either way.
    assert not cs._rate_moved(None, 30) and not cs._rate_moved(0, 30)
    assert not cs._rate_moved(15, None)


async def test_the_rate_constants_hold_their_meaningful_ranges():
    """The E64/E73 thresholds, pinned as VALUES and not only as logic (E72).

    The rate tests drive the confirmation, hysteresis, storm-bound and flap
    logic against shrunk knobs, which is the right shape for unit tests -- and
    it means a tuning pass could set a shipped constant to a value that
    defeats its own purpose with every suite still green. Each assertion below
    carries the reason for its bound, so a deliberate retuning knows what it
    is arguing with.
    """
    assert 5.0 <= c.RATE_RESAMPLE_SETTLE_S <= c.SESSION_MAX_LIFETIME_S / 3, (
        f"RATE_RESAMPLE_SETTLE_S={c.RATE_RESAMPLE_SETTLE_S}: under ~5s the "
        "session-start burst is measured again, which is exactly the 38-49fps "
        "artifact readings the settle window exists to exclude; above a third "
        f"of a {c.SESSION_MAX_LIFETIME_S}s session too little of every session "
        "remains measurable")
    assert 30 <= c.RATE_RESAMPLE_FRAMES <= \
        6 * (c.SESSION_MAX_LIFETIME_S - c.RATE_RESAMPLE_SETTLE_S) / 2, (
        f"RATE_RESAMPLE_FRAMES={c.RATE_RESAMPLE_FRAMES}: under ~30 deltas a "
        "bunched arrival can own the median; larger than half the measurable "
        "window at the slowest real camera on this account (6fps) and a "
        "session no longer fits two batches, so nothing can ever CONFIRM")
    assert c.SESSION_MAX_LIFETIME_S < c.RATE_REBUILD_MIN_INTERVAL_S \
        <= 6 * c.SESSION_MAX_LIFETIME_S, (
        f"RATE_REBUILD_MIN_INTERVAL_S={c.RATE_REBUILD_MIN_INTERVAL_S}: at or "
        "under one rotation the storm bound stops bounding anything (two "
        "rate rebuilds can land on consecutive rotations); far above it a "
        "genuine correction is held for many minutes of wrong-rate publishing")
    assert c.RATE_FLAP_WINDOW_S > c.RATE_REBUILD_MIN_INTERVAL_S, (
        f"RATE_FLAP_WINDOW_S={c.RATE_FLAP_WINDOW_S} vs "
        f"RATE_REBUILD_MIN_INTERVAL_S={c.RATE_REBUILD_MIN_INTERVAL_S}: a "
        "confirmed return that the storm bound delayed past the window would "
        "never be recognised as flapping -- live, the flap confirmation "
        "arrived AFTER the 300s floor had held it back")
    assert c.RATE_FLAP_HOLD_S >= c.RATE_FLAP_WINDOW_S, (
        f"RATE_FLAP_HOLD_S={c.RATE_FLAP_HOLD_S} vs "
        f"RATE_FLAP_WINDOW_S={c.RATE_FLAP_WINDOW_S}: a memory shorter than "
        "the detection window expires and re-detects the same flap as a new "
        "one, doubling the rebuilds the guard exists to remove")


async def test_a_settled_rate_is_measured_past_the_burst_and_needs_two_to_agree():
    """The measurement that is allowed to move a known rate has to be worth
    trusting, because acting on it costs every RTSP reader a reconnect.

    Four separate gates, each pinned here against a synthetic clock:

      1. the SETTLE window -- the burst of buffered frames at a session start is
         not measured at all. That burst has produced 544fps, 215fps and (live)
         62fps readings for cameras really running at 15 and 30, and the first
         batch's own +/-50% tolerance cannot protect against it on the FIRST
         session of a process, when there is nothing to compare against yet;
      2. CONFIRMATION -- one settled batch is never enough on its own;
      3. FAST FLAPPING CONVERGES -- a source alternating between two rates
         quicker than two settled batches never produces two agreeing batches,
         so it never rebuilds anything. (A source that DWELLS at each rate for
         minutes defeats this gate and belongs to the flap guard -- see
         test_a_confirmed_reversal_is_flapping_and_resolves_to_the_higher_rate);
      4. the MINIMUM INTERVAL -- even genuine, agreeing evidence cannot cost a
         second glitch inside the window, and evidence it delays stays
         confirmed rather than being re-derived from scratch.
    """
    with rate_knobs(RATE_RESAMPLE_SETTLE_S=10.0, RATE_RESAMPLE_FRAMES=10,
                    RATE_REBUILD_MIN_INTERVAL_S=300):
        # 1. The burst -- and it has to be a PLAUSIBLE one, because that is the
        #    dangerous kind. A batch that reads 1000fps is thrown out by the
        #    1-60 sanity range wherever it is measured, so it proves nothing
        #    about the settle window. What the log actually shows is buffered
        #    frames producing "38-49fps for a real 15fps camera": inside the
        #    sanity range, well outside the hysteresis band, and sustained for
        #    as long as the buffer takes to drain. Nothing in the arithmetic can
        #    tell that from a camera that really did change setting; only
        #    refusing to measure until the burst is over can.
        watcher = cs.RateWatcher("cam")
        now = 1000.0
        watcher.session_started(now)
        seen = []
        for _ in range(200):                    # 5s of 40fps, inside a 10s settle
            now += 0.025
            seen.append(watcher.note_frame(now, 15))
        assert set(seen) == {None}, (
            f"a session-start burst of buffered frames was measured as a rate and "
            f"confirmed as one: {sorted(s for s in set(seen) if s is not None)}")
        assert watcher.candidate is None, (
            f"the burst became a pending rate change ({watcher.candidate}fps against a "
            "declared 15) -- one more agreeing batch and the publisher would be "
            "rebuilt at the rate a drained buffer happened to arrive at")
        # ...and a burst so fast it is not a rate at all is rejected wherever it
        # is measured, settle window or not.
        assert cs._fps_from_deltas([0.001] * 10) is None
        assert cs._fps_from_deltas([]) is None and cs._fps_from_deltas([0.0] * 4) is None

        # 2. Past the settle window, a real and material change: 6fps against a
        #    publisher declared at 15. One batch only proposes it.
        now += c.RATE_RESAMPLE_SETTLE_S
        first, now = feed_batch(watcher, 1 / 6, now, declared=15)
        assert first is None, (
            f"a single settled batch rebuilt the publisher ({first}fps) -- one bad "
            "batch is exactly what the first-batch measurement already proved can "
            "happen")
        assert watcher.candidate == 6, (
            f"a settled 6fps batch against a publisher declared at 15 left the "
            f"candidate at {watcher.candidate!r} -- the measurement was taken and "
            "then thrown away, so no amount of agreeing evidence can ever act on it")
        assert watcher.last_measured == 6, (
            f"the settled measurement was not kept for observability "
            f"({watcher.last_measured!r}) -- /control/status's measured_fps "
            "serves it (E69)")
        second, now = feed_batch(watcher, 1 / 6, now, declared=15)
        assert second == 6, f"two agreeing settled batches did not confirm 6fps: {second}"
        assert watcher.candidate is None, "the confirmed candidate was not cleared"

        # ...and a batch back inside the band forgets a pending candidate rather
        # than leaving it to be confirmed by something much later.
        watcher = cs.RateWatcher("cam")
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        _, now = feed_batch(watcher, 1 / 6, now, declared=15)
        assert watcher.candidate == 6
        got, now = feed_batch(watcher, 1 / 15, now, declared=15)
        assert got is None and watcher.candidate is None, (
            "a measurement that agrees with the declared rate left the earlier "
            "candidate pending, so the next stray batch would confirm a rate that "
            "two measurements ago had already been contradicted")

        # 3. A flapping source: alternating 6fps and 25fps batches against a
        #    publisher declared at 15. Both are material; neither is ever
        #    confirmed, because no two consecutive batches agree.
        watcher = cs.RateWatcher("cam")
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        flapped = []
        for interval in (1 / 6, 1 / 25) * 4:
            got, now = feed_batch(watcher, interval, now, declared=15)
            flapped.append(got)
        assert set(flapped) == {None}, (
            f"a source alternating between 6fps and 25fps confirmed {flapped} -- the "
            "publisher would rebuild on every second batch, forever, and land on "
            "whichever rate the last batch happened to see")

        # 4. The storm bound. A rebuild has just happened; the same agreeing
        #    evidence must wait -- and waiting is routine pacing, not
        #    warning-level news, because during a sustained hold a
        #    warning per batch is enough volume to age real warnings out of
        #    the container's capped log window.
        watcher = cs.RateWatcher("cam")
        watcher.rebuilt(now)
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        held = []
        with capture_logs(level=logging.WARNING) as warned:
            for _ in range(3):
                got, now = feed_batch(watcher, 1 / 6, now, declared=15)
                held.append(got)
        assert set(held) == {None}, (
            f"a second rate rebuild landed {now - watcher.last_rebuild_at:.0f}s after "
            f"the first, against a {c.RATE_REBUILD_MIN_INTERVAL_S}s floor: {held}")
        assert not warned, (
            f"a paced hold produced warning-level log output: {warned}")

        # ...and once the window has passed, the same evidence is acted on --
        # by the FIRST agreeing batch, because the confirmed candidate was
        # kept through the hold rather than thrown away and re-derived. The
        # bound delays a correction; it never cancels one.
        now += c.RATE_REBUILD_MIN_INTERVAL_S
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        got, now = feed_batch(watcher, 1 / 6, now, declared=15)
        assert got == 6, (
            f"the confirmed evidence was thrown away by the hold ({got!r}) -- a "
            "change the storm bound merely delays must apply on the first "
            "agreeing batch after the floor, not need re-confirming from scratch")

        # A rate above what ffmpeg can be given is not a change once it is
        # clamped: 40 and 55 are both published at 30, so a publisher already
        # declared at 30 must not be rebuilt for either.
        watcher = cs.RateWatcher("cam")
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        for interval in (1 / 40, 1 / 55):
            got, now = feed_batch(watcher, interval, now, declared=30)
            assert got is None, f"{1 / interval:.0f}fps rebuilt a publisher already at 30"
        assert watcher.candidate is None, watcher.candidate


async def test_a_confirmed_reversal_is_flapping_and_resolves_to_the_higher_rate():
    """A DELIVERED rate that swings with no setting change must not become a
    rebuild loop -- and confirmation alone cannot stop it becoming one.

    The live case this pins: the 30fps camera on this account halves its
    delivered rate under marginal light and comes back, dwelling MINUTES at
    each end. Both dwells are long enough for two settled batches to agree, so
    every swing confirmed a "change" and the publisher was rebuilt each way at
    the minimum-interval floor -- a reader-kicking glitch every few minutes for
    as long as the light stayed marginal, plus a warning every ~12s.

    The flapping signature is a confirmed return to the very rate the last
    rate rebuild moved away from, soon after it. The resolution is settled by
    the same measurements as the hysteresis band's asymmetry: held HIGH the
    output pads with duplicates (nothing lost), held low it drops real frames.
    So the flap is resolved ONCE, to the higher rate, and then held there for
    as long as settled batches keep sighting that higher rate.
    """
    with rate_knobs(RATE_RESAMPLE_SETTLE_S=10.0, RATE_RESAMPLE_FRAMES=10,
                    RATE_REBUILD_MIN_INTERVAL_S=0,
                    RATE_FLAP_WINDOW_S=1800.0, RATE_FLAP_HOLD_S=3600.0):
        # The first swing of the night looks exactly like a setting change and
        # is followed: declared 29, confirmed 15, rebuilt down. Nothing can
        # know better yet.
        watcher = cs.RateWatcher("cam")
        now = 1000.0
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        _, now = feed_batch(watcher, 1 / 15, now, declared=29)
        got, now = feed_batch(watcher, 1 / 15, now, declared=29)
        assert got == 15, f"the first downward swing was not confirmed: {got}"
        watcher.rebuilt(now, 29)

        # Minutes later the delivered rate comes back up and confirms: the
        # signature. It must be recognised, resolved UP (the content-safe
        # direction), and said out loud once.
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S + 120
        with capture_logs(level=logging.WARNING) as warned:
            _, now = feed_batch(watcher, 1 / 28, now, declared=15)
            got, now = feed_batch(watcher, 1 / 28, now, declared=15)
        assert got == 28, (
            f"the confirmed return to ~29fps was not applied ({got!r}) -- the "
            "flap must be resolved to the HIGHER rate, which pads with "
            "duplicates where staying low drops real frames")
        assert any("flapping" in ln for ln in warned), (
            f"the flap was resolved without saying so: {warned}")
        watcher.rebuilt(now, 15)

        # From here the swings continue -- and cost NOTHING. Every confirmed
        # return to the low rate is refused while batches keep sighting the
        # high one, and none of it is warning-level noise.
        refused = []
        with capture_logs(level=logging.WARNING) as warned:
            for _ in range(4):
                _, now = feed_batch(watcher, 1 / 15, now, declared=28)   # low dwell
                got, now = feed_batch(watcher, 1 / 15, now, declared=28)
                refused.append(got)
                _, now = feed_batch(watcher, 1 / 28, now, declared=28)   # high dwell
        assert set(refused) == {None}, (
            f"a night of flapping still rebuilds the publisher: {refused} -- "
            "each entry is a reconnect every RTSP reader pays for")
        assert not warned, (
            f"refusing a known flap produced warning-level output on every "
            f"swing: {warned}")

        # A change to a rate that is NEITHER end of the known flap is a real
        # setting change and goes through the ordinary gates untouched.
        _, now = feed_batch(watcher, 1 / 7, now, declared=28)
        got, now = feed_batch(watcher, 1 / 7, now, declared=28)
        assert got == 7, (
            f"the flap hold swallowed a genuine setting change to 7fps: {got!r}")


async def test_flap_memory_expires_once_the_high_rate_stops_appearing():
    """The flap hold must delay a REAL change to the flap's low rate, never
    cancel it.

    What tells the two apart is the high rate's disappearance: a flapping
    source keeps coming back to it (each settled sighting renews the memory),
    while a source whose SETTING really changed to the low rate never visits
    the high rate again. Once the memory has gone RATE_FLAP_HOLD_S without a
    sighting, it expires, and the sustained low measurement is confirmed and
    applied by the ordinary mechanism -- an hour of duplicate padding, then
    the right command line, with no content lost in between.
    """
    with rate_knobs(RATE_RESAMPLE_SETTLE_S=10.0, RATE_RESAMPLE_FRAMES=10,
                    RATE_REBUILD_MIN_INTERVAL_S=0,
                    RATE_FLAP_WINDOW_S=1800.0, RATE_FLAP_HOLD_S=3600.0):
        # Reach the resolved-and-held state: flapping between 15 and 28,
        # publisher held at 28.
        watcher = cs.RateWatcher("cam")
        now = 1000.0
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S
        _, now = feed_batch(watcher, 1 / 15, now, declared=29)
        got, now = feed_batch(watcher, 1 / 15, now, declared=29)
        assert got == 15
        watcher.rebuilt(now, 29)
        watcher.session_started(now)
        now += c.RATE_RESAMPLE_SETTLE_S + 120
        _, now = feed_batch(watcher, 1 / 28, now, declared=15)
        got, now = feed_batch(watcher, 1 / 28, now, declared=15)
        assert got == 28
        watcher.rebuilt(now, 15)

        # The setting really changes to 15 now. The first confirmations land
        # inside the memory and are refused -- the deliberate delay.
        _, now = feed_batch(watcher, 1 / 15, now, declared=28)
        got, now = feed_batch(watcher, 1 / 15, now, declared=28)
        assert got is None, (
            f"a low confirmation inside the flap hold was applied: {got}")

        # Refusals alone must not renew the memory -- only sightings of the
        # HIGH rate do, and there are none any more. Past the hold, the same
        # sustained evidence is applied.
        now += c.RATE_FLAP_HOLD_S
        _, now = feed_batch(watcher, 1 / 15, now, declared=28)
        got, now = feed_batch(watcher, 1 / 15, now, declared=28)
        assert got == 15, (
            f"the flap hold cancelled a real setting change instead of "
            f"delaying it: {got!r} -- the high rate stopped appearing, so the "
            "memory should have expired")


async def test_a_settled_rate_change_rebuilds_the_publisher_without_failing_the_session():
    """The whole point of E64, end to end: the rate is a DEVICE SETTING, so when
    it changes the publisher is rebuilt for it -- through the same path a
    frame-size change takes, because on this platform it is the same event (both
    come from the live-video resolution setting).

    Latched, it is wrong by construction. `-framerate` is the OUTPUT's constant
    frame rate and the basis for `-g`, so a stale declaration duplicates or drops
    frames for as long as the process lives, and the same cameras have come up at
    6, 7, 15 and 29fps across restarts with nothing changed.

    Three things are pinned besides the rebuild itself:

      * it happens at a ROTATION BOUNDARY, not by failing the live session. The
        frame hold already bridges that gap, so the change costs the one glitch a
        rebuild costs and not a failed session, a backoff and a dark stream on
        top of it -- "a rate change must never take the camera off the air";
      * the next session's decoded-frame queue is sized from the NEW rate, since
        `video_queue_maxsize` is in milliseconds and a stale rate silently makes
        it mean something else;
      * the log says what happened, so a rebuild that a reader sees as one
        reconnect is not mistaken for a fault.

    The frames are driven by real asyncio.sleep, so the measured rate is a
    real-time measurement: nominally 25fps, in practice 25 minus whatever the
    scheduler's sleep overshoot cost (observed as low as 24 on a loaded
    machine). What is pinned is therefore the MECHANISM -- one confirmed rate,
    far from the old 10, applied consistently to the publisher, the status,
    the next session's queue and the log -- not the exact integer the clock
    happened to produce.
    """
    # First batch at 10fps, everything after it at 25fps: a +150% move, well past
    # the upward band, sustained rather than a burst.
    def preset_dup_warnings(sink, width, height, fps):
        # Models ffmpeg having emitted "frames duplicated" on each publisher:
        # the loop must fold a replaced sink's count into the lifetime total
        # /control/status serves, exactly as it does for dropped_frames.
        sink.duplicated_frame_warnings = 3 if len(CTX.sinks) == 1 else 2

    with rate_knobs(RATE_RESAMPLE_SETTLE_S=0.05, RATE_RESAMPLE_FRAMES=4,
                    RATE_REBUILD_MIN_INTERVAL_S=0):
        with scenario([Plan(frames=steady(10, 0.1), tail=("repeat", 0.04, 1280, 720))],
                      FIRST_FRAME_TIMEOUT_S=2.0, SESSION_MAX_LIFETIME_S=1.2,
                      FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
            ctx.sink_started_hook = preset_dup_warnings
            with capture_logs(level=logging.WARNING) as lines:
                start(ctx)
                # ...and wait until a frame has flowed through the REBUILT
                # publisher, so the status has folded the replaced sink's
                # duplication-warning count and added the live one on top --
                # the per-frame status write is what carries it.
                await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].fps
                                and x.state.get("duplicated_frame_warnings") == 5,
                                timeout=15.0, why="the rebuilt publisher's first "
                                "status write (dup warnings folded to 5)")
                sinks = list(ctx.sinks)
                sessions = list(ctx.sessions)
                state = dict(ctx.state)
                await stop(ctx)

    assert sinks[0].fps == 10, (
        f"the first publisher was not declared at the measured 10fps: {sinks[0].fps}")
    new_rate = sinks[1].fps
    assert new_rate is not None and 20 <= new_rate <= 25, (
        f"the rebuilt publisher was declared at {new_rate}fps for a camera now "
        "delivering ~25 -- the rate was re-measured but not applied (sleep "
        "overshoot only ever biases the measurement LOW, never below 20)")
    assert len(sinks) == 2, (
        f"{len(sinks)} publishers for one rate change -- every rebuild past the "
        "first is a reconnect every RTSP reader pays for")
    assert sinks[0].stopped == 1, "the stale-rate publisher was left running"

    # Applied at a rotation, not by failing the session.
    assert state.get("state") == "streaming", f"the rate change failed the session: {state}"
    assert state.get("consecutive_failures") == 0, (
        f"a rate change counted toward the container-restart escape hatch: {state}")
    assert state.get("fps") == new_rate, (
        f"/control/status does not report the rebuilt rate ({new_rate}): {state}")
    assert len(sessions) == 2, (
        f"{len(sessions)} sessions -- the rate change should have been applied at the "
        "first rotation, not by failing and reconnecting")

    # The queue follows the rate, because it is sized in milliseconds.
    assert sessions[0].fps is None, sessions[0].fps
    assert sessions[1].fps == new_rate, (
        f"the next session was built with a stale rate ({sessions[1].fps} against "
        f"the confirmed {new_rate}) -- its decoded-frame queue is sized in TIME, "
        "so it now means something else")
    assert sessions[1].queue_maxsize == jc.video_queue_maxsize(new_rate), \
        sessions[1].queue_maxsize

    said = [ln for ln in lines if "frame rate changed" in ln]
    assert said, (
        f"the publisher was rebuilt for a rate change and the log does not say so, "
        f"so the reconnect every reader sees looks like a fault: {lines}")
    assert "10" in said[0] and str(new_rate) in said[0], said[0]

    # The observability fields (E69/E70): the DELIVERED rate rides alongside
    # the declared one, a confirmed-but-unapplied change is visible while it
    # waits, and ffmpeg's duplication warnings accumulate across the rebuild
    # exactly like dropped_frames.
    assert state.get("measured_fps") == new_rate, (
        f"/control/status does not carry the settled measurement: "
        f"{state.get('measured_fps')!r} against the confirmed {new_rate}")
    assert "rate_pending" in state and state["rate_pending"] is None, (
        f"rate_pending should read null once the rotation has applied the "
        f"change: {state.get('rate_pending')!r}")
    assert state.get("duplicated_frame_warnings") == 5, (
        f"duplicated_frame_warnings must be a lifetime total across publisher "
        f"rebuilds (3 folded from the replaced sink + 2 live): "
        f"{state.get('duplicated_frame_warnings')!r}")


async def test_the_frame_hold_repeats_at_the_rate_ffmpeg_was_declared_with():
    """While a confirmed rate change waits for the rotation that applies it, the
    camera's measured rate and the running publisher's declared rate are
    deliberately different -- and the frame hold has to follow the DECLARED one.

    A repeated frame means "one frame" to an ffmpeg reading a raw pipe at
    `-framerate N`. Repeating at a rate the running publisher was not told about
    misdescribes the stream for the length of the reconnect: writing faster than
    declared makes the encoder drop the extras, writing slower makes it pad. The
    rate that is correct here is the one on the live command line, which is
    exactly the one that has NOT been updated yet.

    The audio half of the same tick is pinned separately by
    test_the_rotation_gap_is_filled_with_wallclock_derived_silence: because the
    hold ticks at min(1/fps, AUDIO_SILENCE_TOPUP_S) and clamp_fps bounds the
    video interval at 1/30, no measured rate whatsoever can push the audio top-up
    past the muxer lead the pacing fix depends on.
    """
    gap = {}

    async def slow_second_attempt(idx):
        if idx == 1:
            gap["start"] = time.monotonic()
            await asyncio.sleep(0.6)
            gap["end"] = time.monotonic()

    with rate_knobs(RATE_RESAMPLE_SETTLE_S=0.05, RATE_RESAMPLE_FRAMES=4,
                    RATE_REBUILD_MIN_INTERVAL_S=0):
        with scenario([Plan(frames=steady(10, 0.1), tail=("repeat", 0.04, 1280, 720))],
                      FIRST_FRAME_TIMEOUT_S=2.0, SESSION_MAX_LIFETIME_S=1.2,
                      FRAME_SILENCE_TIMEOUT_S=5.0, FRAME_HOLD_MAX_S=5.0) as ctx:
            ctx.gvs_hook = slow_second_attempt
            start(ctx)
            await run_until(ctx, lambda x: "end" in gap, timeout=15.0,
                            why="the reconnect gap after the rate change")
            declared = ctx.sinks[0].fps
            writes = list(ctx.sinks[0].writes)
            await stop(ctx)

    assert declared == 10, f"the scenario did not declare 10fps: {declared}"
    held = [t for t, _ in writes if gap["start"] <= t <= gap["end"]]
    spanned = gap["end"] - gap["start"]
    assert len(held) >= 3, (
        f"only {len(held)} held frames across a {spanned:.2f}s gap -- the publisher "
        "went silent during the rotation")
    observed = len(held) / spanned
    assert observed <= 1.6 * declared, (
        f"the hold repeated at ~{observed:.0f}fps into a publisher declared at "
        f"{declared}fps -- it is following the newly measured rate instead of the "
        "one on the live ffmpeg command line, so every extra frame is one the "
        "encoder has to throw away")


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
# 6b. The JANUS_REQUEST_AUDIO experiment, and the track nothing consumes
# --------------------------------------------------------------------------

class ScriptedGateway:
    """A Janus streaming-plugin gateway, scripted well enough to drive the REAL
    connect_and_watch end to end. Answers create/attach/message by inspecting
    the request rather than by call order, so an extra create (the audio
    fallback) does not desynchronise the script."""

    OFFER = ("v=0\r\n"
             "m=video 9 UDP/TLS/RTP/SAVPF 126\r\n"
             "a=rtpmap:126 H264/90000\r\n"
             "a=ice-ufrag:NOTAREALUFRAG\r\n")

    def __init__(self, reject_audio_with=None):
        import asyncio as _asyncio
        self.sent = []
        self.creates = []          # every mountpoint-create body, in order
        self.reject_audio_with = reject_audio_with
        self.closed = 0
        self._out = _asyncio.Queue()

    async def _emit(self, msg, txn=None):
        msg = dict(msg)
        if txn is not None:
            msg.setdefault("transaction", txn)
        await self._out.put(json.dumps(msg))

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        txn = msg.get("transaction")
        kind = msg.get("janus")
        if kind == "create":
            await self._emit({"janus": "success", "data": {"id": 11}}, txn)
        elif kind == "attach":
            await self._emit({"janus": "success", "data": {"id": 22}}, txn)
        elif kind == "message":
            body = msg.get("body") or {}
            request = body.get("request")
            if request == "create":
                self.creates.append(body)
                if body.get("audio") and self.reject_audio_with is not None:
                    await self._emit(self.reject_audio_with, txn)
                else:
                    await self._emit({"janus": "success", "plugindata":
                                      {"data": {"stream": {"id": 77}}}}, txn)
            elif request == "watch":
                await self._emit({"janus": "ack"}, txn)
                await self._emit({"janus": "event",
                                  "jsep": {"type": "offer", "sdp": self.OFFER}}, txn)
            elif request == "start":
                await self._emit({"janus": "success"}, txn)
                await self._emit({"janus": "webrtcup"})

    async def close(self):
        self.closed += 1
        await self._out.put(None)

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self._out.get()
        if item is None:
            raise StopAsyncIteration
        return item


class FakePc:
    """RTCPeerConnection's surface as janus_client uses it. Keeps the handlers
    it is given so a test can fire the `track` event by hand."""

    def __init__(self, *args, **kwargs):
        import types
        self.handlers = {}
        self.iceConnectionState = "new"
        self.connectionState = "new"
        self.remote_description = None
        self.localDescription = types.SimpleNamespace(type="answer", sdp="v=0\r\n")
        self.closed = 0

    def on(self, event):
        def register(fn):
            self.handlers[event] = fn
            return fn
        return register

    async def setRemoteDescription(self, desc):
        self.remote_description = desc

    async def createAnswer(self):
        return "answer"

    async def setLocalDescription(self, answer):
        pass

    async def addIceCandidate(self, candidate):
        pass

    async def close(self):
        self.closed += 1


class FakeRemoteTrack:
    """aiortc's RemoteStreamTrack surface, including the part that matters: an
    UNBOUNDED queue that the receiver's decoder thread fills and that only
    recv() ever empties. stop() marks the track ended -- which is precisely why
    it is not a release: a stopped track's queue still grows, it just can no
    longer be drained."""

    def __init__(self, kind):
        self.kind = kind
        self.queue = asyncio.Queue()
        self.readyState = "live"
        self.stops = 0

    async def recv(self):
        if self.readyState != "live":
            raise RuntimeError("MediaStreamError")
        frame = await self.queue.get()
        if frame is None:
            self.stop()
            raise RuntimeError("MediaStreamError")
        return frame

    def stop(self):
        self.stops += 1
        self.readyState = "ended"


@contextlib.contextmanager
def janus_rig(gateway):
    """Point janus_client's RTCPeerConnection and websockets.connect at fakes."""
    from bridge import janus_client as jc

    class WsShim:
        """Everything the real websockets module has, except connect()."""

        def __init__(self, real, connect):
            self._real = real
            self.connect = connect

        def __getattr__(self, name):
            return getattr(self._real, name)

    async def fake_connect(url, **kwargs):
        return gateway

    saved_pc, saved_ws = jc.RTCPeerConnection, jc.websockets
    jc.RTCPeerConnection = FakePc
    jc.websockets = WsShim(saved_ws, fake_connect)
    try:
        yield jc
    finally:
        jc.RTCPeerConnection, jc.websockets = saved_pc, saved_ws


async def _watch_once(gateway, camera_id="cam"):
    """Run the REAL connect_and_watch against `gateway`; returns the session.

    `camera_id` is a parameter because JANUS_REQUEST_AUDIO is per-camera: the
    interesting case is two DIFFERENT ids in one run against one setting.
    """
    with janus_rig(gateway) as jc:
        session = jc.JanusCameraSession(
            {"id": camera_id, "mac_address": "AA:BB:CC:DD:EE:FF", "model": "ADC-V",
             "firmware": "1"},
            {"proxy_url": "rtsp://relay.invalid/stream", "janus_gateway_url": "wss://gw.invalid",
             "janus_token": "TOKEN", "ice_servers": []})
        await asyncio.wait_for(session.connect_and_watch(), timeout=10)
        return session


VIDEO_ONLY_KEYS = {
    "request", "is_private", "type", "media_uri", "add_sps_pps", "is_virtual",
    "streaming_type", "timeout_seconds", "max_timeout_seconds", "video",
    "videoport", "videopt", "videortpmap", "videofmtp", "name", "model", "firmware",
}


VIDEO_ONLY_BODY = {
    "request": "create", "is_private": True, "type": "rtp",
    "media_uri": "rtsp://relay.invalid/stream",
    "add_sps_pps": False, "is_virtual": False, "streaming_type": "Live",
    "timeout_seconds": 180, "max_timeout_seconds": 900,
    "video": True, "videoport": 0, "videopt": 126,
    "videortpmap": "H264/90000",
    "videofmtp": "profile-level-id=42e01f;packetization-mode=1",
    "name": "AA:BB:CC:DD:EE:FF_Live", "model": "ADC-V", "firmware": "1",
}


@contextlib.contextmanager
def audio_knob(value):
    """Set JANUS_REQUEST_AUDIO to a raw setting STRING, as the env var carries it.

    A string, not a bool, because the knob is no longer a bool: it names the
    cameras it applies to. Assigning a parsed value here would test a shape the
    process never actually sees.
    """
    saved = c.JANUS_REQUEST_AUDIO
    c.JANUS_REQUEST_AUDIO = value
    try:
        yield
    finally:
        c.JANUS_REQUEST_AUDIO = saved


async def test_the_audio_experiment_is_off_by_default_and_changes_nothing():
    """With JANUS_REQUEST_AUDIO at its default the mountpoint-create body must be
    exactly what this bridge has always sent -- same keys, same order,
    byte-identical JSON. This is the whole basis for calling the experiment
    reversible, and it has to survive the knob growing per-camera value forms:
    every off spelling, and an unset value, still produce that one body.
    """
    for off in ("false", "0", "no", "off", "", "  ", "FALSE"):
        with audio_knob(off):
            gateway = ScriptedGateway()
            session = await _watch_once(gateway)
            await session.close()

        assert len(gateway.creates) == 1, (off, gateway.creates)
        body = gateway.creates[0]
        assert not any(k.startswith("audio") for k in body), (off, body)
        # Byte-identical, key order included -- this is the wire, not a dict compare.
        assert json.dumps(body) == json.dumps(VIDEO_ONLY_BODY), (
            f"JANUS_REQUEST_AUDIO={off!r} changed the video-only create body on the "
            f"wire: {json.dumps(body)}")
        assert set(body) == VIDEO_ONLY_KEYS, sorted(set(body) ^ VIDEO_ONLY_KEYS)


async def test_the_audio_experiment_asks_for_opus_alongside_the_video():
    """With the knob on for every camera, the audio keys mirror the video ones
    and nothing else moves: same single create, video keys untouched, a dynamic
    payload type that cannot collide with the video 126. Every "on" spelling
    means the same thing, `all` included."""
    for on in ("true", "1", "yes", "on", "all", "  true  "):
        with audio_knob(on):
            gateway = ScriptedGateway()
            session = await _watch_once(gateway)
            await session.close()
        assert len(gateway.creates) == 1, (on, "an accepted audio create must not be retried")
        assert gateway.creates[0].get("audio") is True, (
            f"JANUS_REQUEST_AUDIO={on!r} did not request audio")

    assert len(gateway.creates) == 1, "an accepted audio create must not be retried"
    body = gateway.creates[0]
    assert body["audio"] is True and body["audioport"] == 0
    assert body["audiortpmap"] == "opus/48000/2", body
    assert body["audiopt"] != body["videopt"], (
        f"the audio payload type collides with the video one ({body['audiopt']})")
    for key in VIDEO_ONLY_KEYS:
        assert key in body, f"the audio body dropped {key}"


async def test_a_rejected_audio_create_falls_back_instead_of_failing_the_session():
    """The fail-safe, in both rejection shapes.

    A rejected experiment must cost NOTHING. If it raised, camera_session.py
    would count a consecutive failure for every rotation of every camera -- five
    in a row and MAX_CONSECUTIVE_SESSION_FAILURES hard-exits the process, which
    with `restart: unless-stopped` behind it is a container restart loop caused
    purely by a diagnostic. So the create is retried once with the byte-identical
    video-only body and the session proceeds normally.

    Both shapes matter: the streaming plugin reports a refused mountpoint at the
    PLUGIN level ({"janus": "success", ... "error_code": ...}), which the
    original code could only ever have seen as a KeyError on ["stream"]["id"].
    """
    rejections = [
        {"janus": "success", "plugindata":
         {"data": {"error_code": 456,
                   "error": "Can't add 'rtp' stream from rtsp://relay.invalid/stream"}}},
        {"janus": "error", "error": {"code": 458, "reason": "No such handle"}},
    ]
    with audio_knob("all"):
        for rejection in rejections:
            gateway = ScriptedGateway(reject_audio_with=rejection)
            # No raise: connect_and_watch completing is the assertion.
            session = await _watch_once(gateway)
            await session.close()

            assert len(gateway.creates) == 2, (
                f"expected the audio create plus one video-only retry, got "
                f"{len(gateway.creates)}: {gateway.creates}")
            first, second = gateway.creates
            assert first.get("audio") is True, first
            assert set(second) == VIDEO_ONLY_KEYS, (
                "the fallback body is not the video-only body this bridge has always "
                f"sent: {sorted(set(second) ^ VIDEO_ONLY_KEYS)}")
            assert json.dumps(second) == json.dumps(
                {k: v for k, v in first.items() if not k.startswith("audio")}), (
                "the fallback body is not byte-identical to the video-only body")
            # And the session really did carry on: it watched and started the
            # mountpoint the RETRY created, not the rejected one.
            requests = [m["body"]["request"] for m in gateway.sent
                        if m.get("janus") == "message"]
            assert requests == ["create", "create", "watch", "start"], requests


async def test_the_audio_knob_names_the_cameras_it_applies_to():
    """The knob is PER-CAMERA, and one setting must produce different wire
    behaviour for different cameras in the same process.

    This exists because a single global flag has no safe setting here. Asked for
    audio, the gateway offers an audio m-line FIRST to both cameras on this
    account; the doorbell keeps streaming through it and the slower indoor camera
    stops producing a first frame and reconnects in a loop. A global on/off means
    choosing between abandoning the experiment and breaking a working camera, so
    the knob names ids and everything not named behaves exactly as if the knob
    were off -- byte-identical body included.
    """
    with audio_knob(" doorbell-2048 , indoor-2049x "):
        named_gateway = ScriptedGateway()
        session = await _watch_once(named_gateway, camera_id="doorbell-2048")
        await session.close()

        # The SAME setting, in the same run, for a camera it does not name.
        other_gateway = ScriptedGateway()
        session = await _watch_once(other_gateway, camera_id="indoor-2049")
        await session.close()

        # Read while the setting is in force -- the helper reads the live config
        # attribute, exactly as janus_client does.
        # Whitespace around ids is tolerated; matching is exact, so an id that
        # merely PREFIXES a listed one is not a match ("indoor-2049" vs
        # "indoor-2049x").
        modes = (c.janus_request_audio_mode("doorbell-2048"),
                 c.janus_request_audio_mode("indoor-2049"))
        decisions = (c.janus_request_audio_for("doorbell-2048"),
                     c.janus_request_audio_for("indoor-2049"))

    assert modes == ("listed", "unlisted"), modes
    assert decisions == (True, False), decisions

    named_body = named_gateway.creates[0]
    assert named_body.get("audio") is True, (
        f"a camera the knob names did not get audio: {named_body}")
    assert named_body["audiortpmap"] == "opus/48000/2", named_body

    assert len(other_gateway.creates) == 1, (
        "a camera the knob does not name took the audio fallback path -- an extra "
        f"create round trip per session for an experiment it is not running: "
        f"{other_gateway.creates}")
    other_body = other_gateway.creates[0]
    assert not any(k.startswith("audio") for k in other_body), (
        f"a camera the knob does not name was sent audio keys: {other_body}")
    # Not merely audio-free: byte-identical to the always-shipped body, so an
    # unnamed camera is bit-for-bit unaffected by another camera's experiment.
    assert json.dumps(other_body) == json.dumps(VIDEO_ONLY_BODY), (
        f"an unnamed camera's create body changed on the wire: {json.dumps(other_body)}")
    # "all" may only ever be the whole value -- never one entry of a list, or a
    # camera literally called "all" would silently turn the knob on globally.
    with audio_knob("all,doorbell-2048"):
        assert c.janus_request_audio_for("some-other-camera") is False


async def test_an_offered_track_we_do_not_consume_is_drained_not_ignored():
    """aiortc auto-creates a recvonly transceiver for every offered m-line and
    starts its receiver whether or not anyone reads the track: the decoder
    thread pushes each decoded frame into an UNBOUNDED RemoteStreamTrack queue.
    Silently dropping a non-video track therefore leaks for the whole ~165s life
    of every session -- a per-rotation memory sawtooth, once per camera, forever.

    Draining is the fix, and stop() is NOT: aiortc's MediaStreamTrack.stop()
    only sets an ended flag and emits an event nothing in aiortc listens for, so
    a stopped track's queue keeps growing while recv() refuses to empty it. The
    assertion below is exactly that difference -- the queue must reach zero.
    Ending the track must also NOT mark the session dead: only the video track
    decides whether this session is alive.
    """
    gateway = ScriptedGateway()
    session = await _watch_once(gateway)
    on_track = session.pc.handlers["track"]

    audio = FakeRemoteTrack("audio")
    on_track(audio)
    for i in range(200):
        audio.queue.put_nowait(f"frame{i}")
    for _ in range(400):
        if audio.queue.qsize() == 0:
            break
        await asyncio.sleep(0.005)
    drained = audio.queue.qsize()

    # The video track still reaches the frame queue -- the leak fix must not
    # have swallowed the one track this bridge exists to publish.
    video = FakeRemoteTrack("video")
    on_track(video)
    video.queue.put_nowait("video-frame")
    for _ in range(400):
        if not session._track_queue.empty():
            break
        await asyncio.sleep(0.005)
    got_video = session._track_queue.qsize()

    audio.queue.put_nowait(None)          # the decoder stopping, as at pc.close()
    await asyncio.sleep(0.05)
    dead_after_audio_ended = session._dead.is_set()
    await session.close()

    assert drained == 0, (
        f"{drained} of 200 frames were left queued on a track nothing consumes -- "
        "this is the unbounded RemoteStreamTrack._queue growing for the life of "
        "every session")
    assert got_video == 1, "the video track stopped reaching the frame queue"
    assert not dead_after_audio_ended, (
        "an audio track ending marked the whole session dead -- it would force a "
        "reconnect on every session that is offered audio")


# --------------------------------------------------------------------------
# 6c. JANUS_CONSUME_AUDIO: publishing the downstream audio track
# --------------------------------------------------------------------------

@contextlib.contextmanager
def consume_knob(consume, request="all"):
    """Set both audio knobs to raw setting STRINGS, as the env vars carry them,
    and reset the one-time CONSUME-without-REQUEST warning bookkeeping so a test
    that asserts on that warning is not silenced by an earlier test."""
    saved = (c.JANUS_CONSUME_AUDIO, c.JANUS_REQUEST_AUDIO,
             set(c._CONSUME_WITHOUT_REQUEST_WARNED))
    c.JANUS_CONSUME_AUDIO, c.JANUS_REQUEST_AUDIO = consume, request
    c._CONSUME_WITHOUT_REQUEST_WARNED.clear()
    try:
        yield
    finally:
        c.JANUS_CONSUME_AUDIO, c.JANUS_REQUEST_AUDIO = saved[0], saved[1]
        c._CONSUME_WITHOUT_REQUEST_WARNED.clear()
        c._CONSUME_WITHOUT_REQUEST_WARNED.update(saved[2])


def _subsequence_at(haystack, needle):
    """Index where `needle` appears as a CONTIGUOUS run inside `haystack`, or -1.

    Contiguous, not merely "all present": ffmpeg's option grammar is positional
    -- an input option that drifts past its `-i` silently becomes an option for
    the next input, or for the output.
    """
    for i in range(len(haystack) - len(needle) + 1):
        if haystack[i:i + len(needle)] == needle:
            return i
    return -1


async def _captured_ffmpeg_args(backend, audio_format):
    """Every argv FfmpegSink.start would launch, without launching anything."""
    captured = []

    async def fake_try_start(self, args, pass_fds=()):
        captured.append((list(args), tuple(pass_fds)))
        return True

    saved_backend, saved_try = c.ENCODE_BACKEND, fs.FfmpegSink._try_start
    c.ENCODE_BACKEND = backend
    fs.FfmpegSink._try_start = fake_try_start
    try:
        sink = fs.FfmpegSink("cam")
        await sink.start(1280, 720, 15, audio_format=audio_format)
        await sink.stop()
    finally:
        c.ENCODE_BACKEND, fs.FfmpegSink._try_start = saved_backend, saved_try
    return captured


async def test_audio_consumption_is_off_by_default_and_changes_no_wire_behaviour():
    """The default must be indistinguishable from the bridge that had no audio
    path at all -- on the ffmpeg command line, in the session it builds, and in
    what happens to an offered track.

    This is the whole basis for shipping the feature: a camera nobody opted in
    for keeps publishing a video-only stream, from an argv that carries not one
    audio token. An audio input that appears "just in case" would be a second
    stream the muxer has to interleave, which changes video timing for everyone.

    The byte-for-byte argv below is pinned for the same reason it always was --
    "no audio tokens" would also be true of a command line that had quietly
    changed some other way -- but it is no longer the pre-audio argv verbatim:
    the video input declares its rate with `-framerate <fps>` where it used to
    use `-r <fps>`, which is E58's fix and applies to BOTH shapes (see
    test_the_video_input_declares_its_rate_with_framerate_never_r).
    """
    assert c.janus_consume_audio_mode("any-camera") == "off", (
        f"JANUS_CONSUME_AUDIO defaults to {c.JANUS_CONSUME_AUDIO!r}, which is not off")
    assert c.janus_consume_audio_for("any-camera") is False

    # The argv, for every encoder variant: not one audio token anywhere, and no
    # fd is handed to the child.
    for backend in ("vaapi", "nvenc", "software"):
        for args, pass_fds in await _captured_ffmpeg_args(backend, None):
            assert pass_fds == (), (backend, pass_fds)
            for token in ("-map", "-c:a", "-af", "-b:a", "-analyzeduration"):
                assert token not in args, (backend, token, args)
            assert [a for a in args if a.startswith("pipe:")] == ["pipe:0"], args
            # The video input is the SAME either way now (see
            # test_the_video_input_declares_its_rate_with_framerate_never_r):
            # `-framerate`, never `-r`. Audio adds a second input and the
            # audio encoder, and changes nothing about input 0.
            video_in = args[:args.index("-i")]
            assert "-r" not in video_in, (backend, args)
            assert _subsequence_at(video_in, ["-framerate", "15"]) > 0, (backend, args)
    # ...and the software variant in full, byte for byte, because "no audio
    # tokens" would also be true of an argv that had quietly changed some other way.
    (software_args, _), = await _captured_ffmpeg_args("software", None)
    assert software_args == [
        "ffmpeg", "-hide_banner", "-loglevel", "warning",
        "-f", "rawvideo", "-pix_fmt", "yuv420p", "-s", "1280x720",
        "-framerate", "15", "-use_wallclock_as_timestamps", "1", "-i", "pipe:0",
        "-vf", "scale=960:540",
        "-c:v", "libx264", "-preset", "ultrafast", "-tune", "zerolatency",
        "-g", "15", "-bf", "0",
        "-f", "rtsp", "-rtsp_transport", "tcp", "-pkt_size", "1200",
        c.rtsp_publish_url("cam"),
    ], software_args

    # And in the loop: a video-only sink, a session told not to consume, an
    # offered track still drained, and the status saying so.
    with consume_knob("false", request="all"):
        with scenario([Plan(frames=steady(8, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02)],
                      SESSION_MAX_LIFETIME_S=5.0) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("state") == "streaming",
                            timeout=8.0, why="a streaming session")
            await asyncio.sleep(0.3)
            sink, session = ctx.sink, ctx.sessions[0]
            state = dict(ctx.state)
            start_call, audio_writes = sink.start_calls[0], list(sink.audio_writes)
            pcm_writes = list(sink.pcm_writes)
            consumed = session.consume_audio
            await stop(ctx)

    assert start_call[3] is None, (
        f"the publisher was built with an audio input for a camera that never "
        f"opted in: {start_call}")
    assert consumed is False, "the Janus session was told to consume audio"
    assert not audio_writes and not pcm_writes, (
        f"audio reached the publisher with consumption off: {audio_writes}")
    assert state.get("audio_consumed") is False, state
    assert state.get("audio_format") is None, state
    assert state.get("dropped_audio_frames") == 0, state


async def test_a_consume_enabled_camera_publishes_its_audio():
    """The feature itself: a camera the knob names gets a publisher with a second
    (audio) input, and its decoded audio frames are written into it alongside the
    video -- on the SAME sink, which is the point. Anything that published audio
    by rebuilding or replacing the publisher would black the RTSP path on every
    rotation, which is the bug camera_session.py's outlives-sessions comment
    exists to prevent."""
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02, audio_fmt=("s16", 48000, 2),
                            audio_samples=960)],
                      SESSION_MAX_LIFETIME_S=0.4) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: len(x.sessions) >= 3, timeout=10.0,
                            why="3 sessions")
            sinks = len(ctx.sinks)
            sink = ctx.sink
            start_calls = list(sink.start_calls)
            audio_writes = list(sink.audio_writes)
            stops = sink.stopped
            consumed = [s.consume_audio for s in ctx.sessions[:3]]
            state = dict(ctx.state)
            await stop(ctx)

    assert consumed == [True, True, True], consumed
    assert sinks == 1 and stops == 0, (
        f"the publisher was rebuilt to carry audio ({sinks} sinks, {stops} stops) -- "
        "readers would be kicked off the RTSP path on every rotation")
    assert len(start_calls) == 1, start_calls
    assert start_calls[0][3] == ("s16", 48000, 2), (
        f"the audio input was not sized from the decoded frames: {start_calls[0]}")
    assert len(audio_writes) > 10, (
        f"only {len(audio_writes)} decoded audio frames reached the publisher across "
        "three sessions -- audio is not being published")
    assert state.get("audio_consumed") is True, state
    # OBSERVED, not the shape ffmpeg was launched with -- see
    # test_audio_status_reports_only_what_was_actually_decoded.
    assert state.get("audio_format") == "48000Hz/2ch/s16", state
    assert state.get("decoded_audio_frames", 0) > 10, (
        f"decoded_audio_frames did not follow the frames that reached the "
        f"publisher: {state.get('decoded_audio_frames')!r} against "
        f"{len(audio_writes)} writes")


async def test_audio_status_reports_only_what_was_actually_decoded():
    """/control/status must not claim audio that never existed.

    This is the field that hid a real outage. `audio_format` used to be rendered
    from what the running ffmpeg had been LAUNCHED with, and when no frame had
    been decoded that was a bootstrap guess at the shape the SDP implies --
    literally ("s16", 48000, 2). So a camera whose aiortc audio-decoder thread
    had died on its first malformed Opus packet, whose published track was pure
    silence for eleven consecutive sessions, reported a confident
    `"audio_format": "48000Hz/2ch/s16"` the entire time, alongside
    `state: "streaming"` and a fresh `last_frame_at` (the frame-silence watchdog
    is video-only). Nothing in the status could distinguish "audio is flowing"
    from "audio has never once decoded".

    So: `audio_format` is null until a real frame is observed, and
    `decoded_audio_frames` -- which no field previously covered -- is the count
    that makes flow checkable at a glance. `dropped_audio_frames` cannot do that
    job: a stream that simply never decodes drops nothing. What the publisher
    was launched with is a separate field, `publisher_audio_format`, and the
    two are never allowed to become one.
    """
    # 1. Consumption on and NOT ONE audio frame ever decoded -- the silent-stream
    #    failure exactly. The publisher now comes up VIDEO-ONLY in that state
    #    rather than on a guessed 48 kHz stereo input (see
    #    test_a_camera_that_never_sends_audio_publishes_video_forever), which is
    #    what makes the status question sharper rather than softer: there is no
    #    launched shape to mistake for an observation, and the field that says so
    #    is publisher_audio_format.
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=None)],
                      SESSION_MAX_LIFETIME_S=5.0, AUDIO_SILENCE_TOPUP_S=0.05) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("state") == "streaming",
                            timeout=8.0, why="a streaming session")
            await asyncio.sleep(0.3)
            silent = dict(ctx.state)
            launched_with = ctx.sink.start_calls[0][3]
            pcm_writes = len(ctx.sink.pcm_writes)
            video_writes = len(ctx.sink.writes)
            await stop(ctx)

    assert silent.get("state") == "streaming", silent
    assert launched_with is None, (
        f"the publisher was given an audio input for a camera that has decoded "
        f"nothing: {launched_with} -- a guessed shape is what the first real "
        "frame then had to correct, at the cost of a failed session")
    assert pcm_writes == 0 and video_writes > 0, (
        f"{pcm_writes} PCM writes into a publisher with no audio input, and "
        f"{video_writes} video frames -- the video half is what must keep going")
    assert silent.get("audio_format") is None, (
        f"audio_format reported {silent.get('audio_format')!r} for a camera that "
        "has never decoded an audio frame, which is how eleven sessions of pure "
        "silence went unnoticed")
    assert silent.get("publisher_audio_format") is None, (
        f"publisher_audio_format reported {silent.get('publisher_audio_format')!r} "
        "for a video-only publisher")
    assert silent.get("audio_dropped") is False, (
        "a publisher that never had an audio input has not DROPPED one -- that "
        "reads as a degradation an operator would go looking for")
    assert silent.get("decoded_audio_frames") == 0, (
        f"decoded_audio_frames must be 0 when nothing decoded, got "
        f"{silent.get('decoded_audio_frames')!r}")
    assert silent.get("audio_consumed") is True, silent

    # 2. The same camera once frames really do decode: both fields move, and the
    #    format is the one the FRAMES carry, not the one ffmpeg was started with.
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02, audio_fmt=("s16", 16000, 1),
                            audio_peek=None, audio_samples=320)],
                      SESSION_MAX_LIFETIME_S=10.0, FRAME_SILENCE_TIMEOUT_S=5.0,
                      MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("decoded_audio_frames", 0) >= 3,
                            timeout=10.0, why="decoded audio frames")
            flowing = dict(ctx.state)
            await stop(ctx)

    assert flowing.get("audio_format") == "16000Hz/1ch/s16", (
        f"audio_format is not the shape the decoded frames carry: "
        f"{flowing.get('audio_format')!r}")
    assert flowing.get("decoded_audio_frames") >= 3, flowing


def _silence_rig(rate=48000, channels=2, prime_ms=None):
    """The REAL FfmpegSink.write_silence_to, wired to a recording transport.

    Everything below the transport is real: the deficit arithmetic, the per-call
    ceiling, the sample accounting. `now` is passed in by the caller, so the
    scripted clock is the only fake in it.
    """
    sink = fs.FfmpegSink("cam")
    writes = []
    sink._audio_transport = types.SimpleNamespace(
        is_closing=lambda: False, get_write_buffer_size=lambda: 0,
        write=lambda data: writes.append(len(data)))
    sink._audio_protocol = types.SimpleNamespace(lost=False)
    sink.audio_format = ("s16", rate, channels)
    sink._audio_bytes_per_sample = 2 * channels
    sink._audio_started_at = 1000.0
    primed = int(rate * (fs._AUDIO_PRIME_MS if prime_ms is None else prime_ms) / 1000)
    sink._audio_samples_written = primed
    return sink, writes, primed


async def test_the_silence_fill_tracks_the_wallclock_and_never_bursts():
    """The two halves of the silence top-up, on the REAL sink with a scripted clock.

    HALF ONE -- the count comes from ELAPSED REAL TIME, never from counting calls.
    A repeated video frame is self-describing: whenever it lands it is one frame,
    so the caller's sleep jitter is invisible. A sample stream's duration IS its
    length, so filling a gap by writing "one interval's worth of samples per
    call" makes every bit of that jitter a permanent, accumulating A/V offset --
    and on a single-output RTSP mux there is no separate audio timeline to absorb
    it. The calls below are deliberately spaced irregularly and the total written
    is asserted to equal the wall clock EXACTLY; no fixed-per-call implementation
    can satisfy that.

    HALF TWO -- no single write may hand ffmpeg more than
    _AUDIO_SILENCE_MAX_BURST_S of audio, and that is the invariant this whole
    change exists for. It is invisible in production: a 0.5s burst publishes
    perfectly good audio and perfectly good video, and merely makes ffmpeg stop
    reading the VIDEO pipe for 165-330ms each time, which surfaces a layer away
    as "dropped N frame(s) -- consumer falling behind" (measured in the deployed
    container: 120 of 900 frames per 60s run, against 0 when the same audio is
    delivered under the pipe's lead). Nothing in the audio path complains at all.

    The ceiling is a CEILING, not a budget: what it clips is still owed, and
    successive calls keep repaying it against the wall clock until the stream is
    whole again.
    """
    rate = 48000
    sink, writes, primed = _silence_rig(rate=rate)
    started = sink._audio_started_at
    cap_samples = int(rate * fs._AUDIO_SILENCE_MAX_BURST_S)

    # Inside the primed lead there is nothing owed yet -- the pipe already covers
    # further than the wall clock has reached.
    assert await sink.write_silence_to(now=started + 0.05) is True
    assert writes == [], (
        f"silence was written while the audio still led the wallclock: {writes}")

    # Irregular spacing, and the TOTAL must land exactly on the wall clock every
    # time -- not on a multiple of anything this loop counted.
    for elapsed in (0.15, 0.19, 0.27, 0.33, 0.36, 0.44):
        now = started + elapsed
        assert await sink.write_silence_to(now=now) is True
        # `now - started` is not exactly `elapsed` in binary floating point, so
        # the expected count is derived the same way the code derives it and
        # compared exactly. This is not slack: a per-call implementation is out
        # by hundreds of samples here, not by one.
        assert sink._audio_samples_written == int((now - started) * rate), (
            f"after {elapsed:.2f}s the stream holds {sink._audio_samples_written} "
            f"samples, not the {int((now - started) * rate)} elapsed real time "
            "calls for -- the fill is counting its own calls, not the wall clock")
    assert max(writes) <= cap_samples * sink._audio_bytes_per_sample, (
        f"a single top-up handed over {max(writes)} bytes, past the "
        f"{fs._AUDIO_SILENCE_MAX_BURST_S}s ceiling -- this is the burst that "
        "stops ffmpeg reading the video pipe")

    # A long stall: 3s owed at once. It is repaid over successive calls, each
    # bounded, and never in one burst -- and nothing is lost in the process.
    writes.clear()
    calls = 0
    while sink._audio_samples_written < int(3.0 * rate) and calls < 200:
        assert await sink.write_silence_to(now=started + 3.0) is True
        calls += 1
    assert calls > 1, (
        "a 3s deficit was repaid in one write -- the per-call ceiling is not "
        "being applied")
    assert max(writes) == cap_samples * sink._audio_bytes_per_sample, (
        f"the largest catch-up write was {max(writes)} bytes, not the "
        f"{fs._AUDIO_SILENCE_MAX_BURST_S}s ceiling")
    assert sink._audio_samples_written == int(3.0 * rate), (
        f"the clipped deficit was lost: the stream holds "
        f"{sink._audio_samples_written} samples where 3.0s is {int(3.0 * rate)}")
    # ...and once whole, it stops: no overrun past the wall clock.
    writes.clear()
    assert await sink.write_silence_to(now=started + 3.0) is True
    assert writes == [], f"the fill overran the wall clock: {writes}"

    # The cadence every caller of this method is paced at has to stay under the
    # lead the pipe is primed with, or the audio stops covering the next video
    # packet and ffmpeg stops reading the video pipe (0.1s cadence measured: 162
    # of 900 frames stalled; 0.05s: 0 of 900).
    assert c.AUDIO_SILENCE_TOPUP_S < fs._AUDIO_PRIME_MS / 1000.0, (
        f"the silence top-up runs every {c.AUDIO_SILENCE_TOPUP_S}s against a "
        f"{fs._AUDIO_PRIME_MS}ms lead -- the audio input falls behind the video "
        "between top-ups, which is what makes ffmpeg stop reading the video pipe")
    assert fs._AUDIO_SILENCE_MAX_BURST_S <= fs._AUDIO_PRIME_MS / 1000.0, (
        "a single top-up may hand over more audio than the pipe's whole lead")
    # The pump's yield window, bounded relative to the queue it drains. Audio
    # arrives in 20ms frames, so the queue holds ~AUDIO_QUEUE_MAXSIZE * 0.02s;
    # a burst window past that means a full backlog is drained in one unbroken
    # pass -- an executor burst in front of the video path at exactly the
    # moment it is already behind, which is the fault the yield exists for.
    assert 0 < c.AUDIO_PUMP_MAX_BURST_S <= c.AUDIO_QUEUE_MAXSIZE * 0.020 / 2, (
        f"AUDIO_PUMP_MAX_BURST_S={c.AUDIO_PUMP_MAX_BURST_S}s against a "
        f"~{c.AUDIO_QUEUE_MAXSIZE * 0.020:.1f}s queue -- a full backlog would "
        "be drained without yielding even once")


async def test_the_rotation_gap_is_filled_with_wallclock_derived_silence():
    """The audio counterpart of the frame hold, in the loop, on a SLOW camera.

    The hold repeats the last video frame at the camera's own frame interval,
    because that is the rate ffmpeg was told to expect. The audio top-up has a
    completely different deadline: it must keep the audio input ahead of the
    video or ffmpeg stops reading the video pipe (see
    ffmpeg_sink._AUDIO_PRIME_MS). Those two coincide well enough at 15fps (67ms)
    and are quietly wrong at 5-6fps -- one camera on this account runs at 6 --
    where one top-up per repeated frame is 167ms apart against a 100ms lead. So
    the hold ticks at whichever of the two is shorter, and this drives a 5fps
    camera to pin it.

    The wallclock derivation itself is pinned directly, against a scripted clock
    and exact sample counts, in
    test_the_silence_fill_tracks_the_wallclock_and_never_bursts -- it used to be
    inferred here from a hold artificially slowed to four times its nominal
    interval, which cannot separate the two implementations any more now that a
    single write is bounded (a slowed hold legitimately under-fills, and the
    catch-up is spread over later calls rather than landing in one).
    """
    gap = {}

    async def very_slow_second_attempt(idx):
        if idx == 1:
            gap["start"] = time.monotonic()
            await asyncio.sleep(1.2)
            gap["end"] = time.monotonic()

    with consume_knob("all", request="all"):
        # 0.2s between frames: a 5fps camera, whose frame interval is twice the
        # lead the audio pipe runs on.
        with scenario([Plan(frames=steady(8, 0.2), tail=("repeat", 0.2, 1280, 720),
                            audio_interval=0.02)],
                      SESSION_MAX_LIFETIME_S=1.6, FRAME_SILENCE_TIMEOUT_S=5.0,
                      FRAME_HOLD_MAX_S=5.0) as ctx:
            ctx.gvs_hook = very_slow_second_attempt
            start(ctx)
            await run_until(ctx, lambda x: "end" in gap, timeout=15.0,
                            why="the reconnect gap")
            sink = ctx.sink
            rate = sink.audio_format[1]
            fps = sink.fps
            # The window starts a beat after the gap is recorded. The instant the
            # gap begins is the same event-loop tick on which the previous
            # session's audio pump is being torn down, so a single in-flight write
            # can land exactly on the boundary. A pump that was genuinely still
            # running alongside the hold would write every 20ms for the whole
            # gap, so nothing interesting is excluded.
            began = gap["start"] + 0.2
            in_gap = [(t, n) for t, n in sink.pcm_writes if began <= t <= gap["end"]]
            audio_in_gap = [t for t, _ in sink.audio_writes if began <= t <= gap["end"]]
            await stop(ctx)

    assert 4 <= fps <= 6, f"the scenario did not produce a slow camera: {fps}fps"
    assert len(in_gap) >= 3, (
        f"only {len(in_gap)} audio writes during a {gap['end'] - gap['start']:.2f}s "
        "rotation gap -- the audio input went silent while video kept advancing, "
        "which on one mux is a permanently growing A/V offset, not 'a silence'")
    assert not audio_in_gap, (
        "real decoded audio was written during the gap -- the previous session's "
        "pump was still running alongside the hold, so two writers shared the pipe")

    lead_s = fs._AUDIO_PRIME_MS / 1000.0
    spacings = [b - a for (a, _), (b, _) in zip(in_gap, in_gap[1:])]
    assert max(spacings) < lead_s, (
        f"the hold left {max(spacings):.3f}s between audio top-ups on a {fps}fps "
        f"camera, against the {lead_s:.3f}s lead the pipe runs on -- the audio "
        "stops covering the video and ffmpeg stops reading the video pipe. This "
        "is what ticking the hold at 1/fps does to a slow camera")
    biggest = max(n for _, n in in_gap)
    assert biggest <= fs._AUDIO_SILENCE_MAX_BURST_S * rate * 1.05, (
        f"a single top-up wrote {biggest} samples ({biggest / rate:.2f}s) past the "
        f"{fs._AUDIO_SILENCE_MAX_BURST_S}s ceiling")

    covered = sum(n for _, n in in_gap)
    spanned = in_gap[-1][0] - in_gap[0][0]
    expected = spanned * rate
    assert covered >= 0.8 * expected, (
        f"{covered} samples cover {spanned:.2f}s of gap at {rate}Hz, where elapsed "
        f"real time calls for ~{expected:.0f} -- the fill is falling behind the "
        "wall clock, which is exactly when ffmpeg stops reading the video pipe")
    assert covered <= 1.2 * expected, (
        f"{covered} samples for {spanned:.2f}s at {rate}Hz is far more than elapsed "
        f"real time (~{expected:.0f}) -- the fill overruns the wall clock")


async def test_a_quiet_camera_is_topped_up_under_the_lead_and_never_in_bursts():
    """The defect that took the doorbell off the air, pinned end to end.

    A doorbell is quiet nearly all the time, so nearly every pass of the audio
    pump takes the TIMEOUT branch and fills with silence -- and that fill was
    paced at AUDIO_SILENCE_TOPUP_S=0.5 against a pipe primed with a 100ms lead.
    For 400ms of every cycle the audio input covered nothing past the video, so
    the muxer had nothing to interleave the next video packet against and ffmpeg
    stopped reading the video pipe. Measured in the deployed container, 900
    frames per 60s run at 15fps: 120 frames (13%) stalled past a whole frame
    interval, against 0 for a video-only publisher. That surfaced a layer away as
    janus_client's "dropped N frame(s) -- consumer falling behind" and, once the
    starved video timeline had fallen far enough behind the audio, as the audio
    pipe backing up until the sink declared itself broken.

    Nothing in the audio path can see it: the audio published is correct, the
    video published is correct, and the only trace is frames going missing
    somewhere else. So the invariant is asserted directly, on the writes
    themselves -- cadence UNDER the lead, and no single write larger than the
    lead either.

    The camera modelled here has already had a frame decoded (`audio_peek`) and
    then goes quiet, which is what a doorbell does all day and is the only shape
    of "quiet" this invariant can be about: a camera that has decoded NOTHING
    gets a video-only publisher, which has no audio input to run dry.
    """
    lead_s = fs._AUDIO_PRIME_MS / 1000.0
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(10, 0.066), tail=("repeat", 0.066, 1440, 1440),
                            audio_interval=None,     # offered audio, none arriving
                            audio_peek=("s16", 8000, 1))],
                      SESSION_MAX_LIFETIME_S=5.0, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("state") == "streaming",
                            timeout=8.0, why="a streaming session")
            await run_until(ctx, lambda x: x.sink is not None
                            and len(x.sink.pcm_writes) >= 12,
                            timeout=8.0, why="a run of silence top-ups")
            sink = ctx.sink
            rate = sink.audio_format[1]
            writes = list(sink.pcm_writes)
            await stop(ctx)

    # Skip the prime and the first top-up: the pump's first pass starts whenever
    # the session does, so only the steady cadence is under test.
    steady_writes = writes[2:]
    spacings = [b - a for (a, _), (b, _) in zip(steady_writes, steady_writes[1:])]
    biggest = max(n for _, n in steady_writes)

    assert biggest <= lead_s * rate * 1.05, (
        f"a single audio write handed ffmpeg {biggest / rate:.2f}s of PCM, past "
        f"the {lead_s:.2f}s the pipe leads the video by -- one video stall of "
        f"~0.33-0.66x that follows every such write")
    assert max(spacings) < lead_s, (
        f"the longest gap between audio writes was {max(spacings):.3f}s against a "
        f"{lead_s:.3f}s lead -- the audio input runs dry before the next write "
        "arrives, and ffmpeg stops reading the VIDEO pipe until it does")
    # ...and the stream really is being kept whole, not merely written to often:
    # what was written covers the wall clock it spans.
    covered = sum(n for _, n in steady_writes)
    spanned = steady_writes[-1][0] - steady_writes[0][0]
    assert covered >= 0.8 * spanned * rate, (
        f"{covered} samples for {spanned:.2f}s of quiet at {rate}Hz -- the audio "
        "timeline is falling behind the video's")


async def test_the_frame_queue_is_sized_in_time_not_in_frames():
    """The decoded-video queue holds a fixed number of MILLISECONDS, not a fixed
    number of frames.

    It was a flat maxsize=4, which is 267ms of tolerance on the 15fps doorbell
    and 667ms on the 6fps camera beside it -- one number meaning two different
    things, and neither of them chosen. Sized in time it means the same thing on
    every camera, and the camera's own measured rate (remembered across
    rotations) is what drives it.

    This is DEFENCE IN DEPTH and is deliberately not sized to hide a stall: a
    deeper queue does not remove one, it converts dropped frames into latency,
    since every frame held is a frame the viewer sees late. The ceiling is
    memory: a decoded 1440x1440 yuv420p frame is 3.11MB, so 8 frames is 24.9MB
    per camera against the container's 768MiB limit (~435MiB in use), and 16
    would be 49.8MB for a queue that is not supposed to fill.
    """
    depths = {fps: jc.video_queue_maxsize(fps) for fps in (5, 6, 10, 15, 20, 30)}
    for fps, depth in depths.items():
        held_ms = 1000.0 * depth / fps
        wanted = round(fps * jc.VIDEO_QUEUE_TARGET_MS / 1000.0)
        if wanted > jc.VIDEO_QUEUE_MAX_FRAMES:
            # Above ~16fps the memory ceiling binds before the time target does,
            # and that is a deliberate choice rather than an oversight: 8 frames
            # is 24.9MB at 1440x1440 and the target would ask for 15 at 30fps.
            # Even there it is twice the flat 4 this replaces.
            assert depth == jc.VIDEO_QUEUE_MAX_FRAMES, (fps, depths)
            assert held_ms >= 250, (fps, held_ms)
        else:
            assert 400 <= held_ms <= 900, (
                f"{fps}fps gets {depth} frames = {held_ms:.0f}ms of tolerance, "
                f"which is not the ~{jc.VIDEO_QUEUE_TARGET_MS}ms this is supposed "
                f"to mean on every camera: {depths}")
    assert depths[15] == 8 and depths[6] == 4, depths
    assert min(depths.values()) >= 4, (
        f"some camera got a shallower queue than the flat 4 that shipped: {depths}")
    assert jc.video_queue_maxsize(None) == jc.video_queue_maxsize(15), (
        "a camera whose rate has not been measured yet must get the same 15fps "
        "assumption the fps fallback uses")
    # Memory, in the units that matter: the doorbell's own frame size.
    frame_mb = 1440 * 1440 * 1.5 / (1024 * 1024)
    assert max(depths.values()) * frame_mb <= 25.0, (
        f"{max(depths.values())} frames of 1440x1440 is "
        f"{max(depths.values()) * frame_mb:.1f}MB per camera")

    # ...and the loop actually hands the rate down. The first session of a camera
    # has nothing to hand down; the ones after it carry the measured rate, which
    # is the entire point of remembering it across rotations.
    with scenario([Plan(frames=steady(10, 0.2), tail=("repeat", 0.2, 1280, 720))],
                  SESSION_MAX_LIFETIME_S=0.5, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: len(x.sessions) >= 2, timeout=12.0,
                        why="a second session")
        first, second = ctx.sessions[0], ctx.sessions[1]
        await stop(ctx)

    assert first.fps is None, (
        f"the first session was given an fps nothing had measured yet: {first.fps}")
    assert 4 <= (second.fps or 0) <= 6, (
        f"the measured rate was not handed to the next session: {second.fps}")
    assert second.queue_maxsize == jc.video_queue_maxsize(second.fps), (
        f"the session's queue was not sized from the rate it was given: "
        f"{second.queue_maxsize}")


async def test_a_wedged_audio_pipe_degrades_to_video_only():
    """An audio input that goes bad mid-session must cost the audio, not the camera.

    ffmpeg stopping reading the audio pipe used to mean "restart the encoder":
    the sink refused the write, the pump raised the rebuild flag, the session
    failed, and the publisher was torn down and relaunched -- which destroys the
    MediaMTX path, kicks every RTSP reader off it and puts a discontinuity in
    front of every HLS viewer. That trades a WORKING VIDEO FEED for an audio
    input that has already gone. During E62 it happened repeatedly and the camera
    ended with no publisher at all.

    Video is worth more than audio -- the same judgement start() already makes
    for an unsupported sample format. So the audio is dropped, the same publisher
    keeps going, and /control/status says so out loud, because a bridge quietly
    publishing silence is the failure mode this whole status block exists to
    prevent.
    """
    # 1. The REAL sink: a transport ffmpeg has stopped draining.
    real = fs.FfmpegSink("cam")
    closed = []
    real.proc = types.SimpleNamespace(returncode=None, stdin=object())
    real._audio_transport = types.SimpleNamespace(
        is_closing=lambda: False,
        get_write_buffer_size=lambda: fs._AUDIO_PIPE_MAX_BUFFER + 1,
        write=lambda data: None, close=lambda: closed.append(True))
    real._audio_protocol = types.SimpleNamespace(lost=False)
    real.audio_format = ("s16", 48000, 2)
    real._audio_bytes_per_sample = 4
    real._audio_started_at = time.monotonic() - 1.0
    real._audio_samples_written = 4800

    assert await real.write_silence_to() is False
    assert real.audio_dropped is True, (
        "a wedged audio pipe did not mark the sink audio-dead, so the caller has "
        "no way to tell it from a frame that simply needs a rebuild")
    assert real.audio_enabled is False, "the sink kept offering a dead audio input"
    assert closed, "the wedged audio pipe was left open"
    assert str(fs._AUDIO_PIPE_MAX_BUFFER + 1) in (real.audio_dropped_reason or ""), (
        f"the reason does not say what was observed: {real.audio_dropped_reason!r}")
    assert real.publisher_alive is True, (
        "losing the audio input took the VIDEO publisher down with it -- that is "
        "the trade this test exists to prevent")

    # 2. In the loop: the same publisher keeps publishing video, is never
    #    rebuilt, and the status carries the degradation.
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02)],
                      SESSION_MAX_LIFETIME_S=5.0, FRAME_SILENCE_TIMEOUT_S=5.0,
                      MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
            def wedge_the_audio_pipe(sink, width, height, fps):
                sink.drop_audio_after = 5
            ctx.sink_start_hook = wedge_the_audio_pipe
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("audio_dropped") is True,
                            timeout=10.0, why="the audio input to be dropped")
            writes_at_drop = len(ctx.sink.writes)
            state_at_drop = dict(ctx.state)
            await run_until(ctx, lambda x: len(x.sink.writes) > writes_at_drop + 10,
                            timeout=10.0, why="video to keep publishing")
            sinks, sink = len(ctx.sinks), ctx.sink
            stops, audio_writes = sink.stopped, len(sink.audio_writes)
            state = dict(ctx.state)
            await stop(ctx)

    assert sinks == 1 and stops == 0, (
        f"the publisher was rebuilt over a dead audio input ({sinks} sinks, "
        f"{stops} stops) -- every RTSP reader on this path was just kicked off it")
    assert state.get("state") == "streaming", (
        f"losing audio failed the session: {state}")
    assert state.get("consecutive_failures") == 0, (
        f"a dropped audio input counted toward the container-restart escape "
        f"hatch: {state}")
    assert state_at_drop.get("audio_dropped_reason"), (
        f"the status says audio was dropped but not why: {state_at_drop}")
    assert state.get("audio_consumed") is True, (
        "audio_consumed must keep saying what this camera is CONFIGURED to do -- "
        "audio_dropped is what says the publisher stopped doing it")
    # Nothing more was written into the dead pipe.
    assert len(sink.audio_writes) == audio_writes, sink.audio_writes


async def test_a_publisher_that_stopped_reading_is_detected_and_rebuilt():
    """"Publishing" and "publishing to nowhere" have to be distinguishable.

    Throughout the E62 outage /control/status reported `state: streaming`,
    `consecutive_failures: 0` and a fresh `last_frame_at` while MediaMTX had no
    path at all and every reader was refused. Nothing in the bridge asked whether
    the publisher still existed: the frame-silence watchdog watches frames
    ARRIVING from Janus, and the sink-repair check only asked whether the ffmpeg
    process had exited -- which it had not.

    The evidence available here is whether ffmpeg is still CONSUMING what it is
    fed. stdin.drain() resolves only once the transport has handed a whole raw
    frame (~3MB against a 64KB pipe buffer) to the process, so it completing is
    direct evidence and it failing to complete is direct evidence of the
    opposite. Unbounded -- which is what it was -- that same await simply parks
    the camera's loop forever, with no timeout, no failure and no reconnect, and
    the status frozen on its last good values.

    MediaMTX's own API would answer the question more directly and is
    deliberately not used: mediamtx.yml disables every listener except RTSP (the
    file says so, listener by listener), and this container runs with
    --network host, so turning the API on to health-check ourselves would open a
    real host port. This costs nothing per frame and needs no listener.
    """
    # 1. The REAL sink: a drain that never completes must be bounded, must latch,
    #    and must report -- not park the caller.
    class NeverReads:
        def __init__(self):
            self.written = 0

        def write(self, data):
            self.written += len(data)

        async def drain(self):
            await asyncio.Event().wait()

    class TinyFrame:
        width = height = 2

        def to_ndarray(self, format=None):
            return types.SimpleNamespace(tobytes=lambda: b"\0" * 6)

    saved = fs._VIDEO_DRAIN_TIMEOUT_S
    fs._VIDEO_DRAIN_TIMEOUT_S = 0.2
    try:
        real = fs.FfmpegSink("cam")
        real._wh = (2, 2)
        real.proc = types.SimpleNamespace(returncode=None, stdin=NeverReads())
        assert real.publisher_alive is True, "a fresh publisher reads as dead"
        began = time.monotonic()
        # The bound is the point: without it this await never returns, so the
        # outer wait_for must produce a FAILURE rather than hanging the suite.
        try:
            accepted = await asyncio.wait_for(real.write_frame(TinyFrame()), timeout=5.0)
        except asyncio.TimeoutError:
            raise AssertionError(
                "write_frame parked forever on an ffmpeg that stopped reading its "
                "video pipe -- this is the camera loop wedging with the status "
                "frozen on 'streaming'")
        elapsed = time.monotonic() - began
        assert accepted is False, "a frame ffmpeg never read was reported as published"
        assert elapsed < 2.0, f"the drain bound was not honoured ({elapsed:.2f}s)"
        assert real.publisher_stalled is True and real.publisher_alive is False, (
            "the publisher was not marked gone, so the repair check would leave "
            "this ffmpeg in place forever")
    finally:
        fs._VIDEO_DRAIN_TIMEOUT_S = saved

    # 2. In the loop: a publisher that has stopped consuming is REPLACED, and the
    #    status stops claiming it is publishing.
    def wedge_the_publisher(idx):
        if idx == 12 and CTX.sinks:
            CTX.sinks[0].publisher_stalled = True

    with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                        on_frame=wedge_the_publisher)],
                  SESSION_MAX_LIFETIME_S=5.0, FRAME_SILENCE_TIMEOUT_S=5.0,
                  MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
        start(ctx)
        await run_until(ctx, lambda x: x.state.get("publisher_alive") is False,
                        timeout=10.0, why="the publisher to be reported gone")
        while_stalled = dict(ctx.state)
        await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].writes,
                        timeout=10.0, why="a replacement publisher")
        first_stopped = ctx.sinks[0].stopped
        recovered = dict(ctx.state)
        await stop(ctx)

    assert while_stalled.get("last_frame_at") is not None
    assert (while_stalled.get("last_publish_at") or 0) < while_stalled["last_frame_at"], (
        f"last_publish_at kept up with last_frame_at while nothing was reaching "
        f"the publisher: {while_stalled} -- these two being the same number is "
        "exactly how 'streaming' and 'streaming to nowhere' became "
        "indistinguishable")
    assert first_stopped == 1, (
        "the wedged publisher was left running -- the process was alive, so the "
        "repair check that only asks about the process saw nothing wrong")
    assert recovered.get("publisher_alive") is True, recovered
    assert recovered.get("last_publish_at") >= while_stalled["last_frame_at"], (
        f"the replacement publisher is not being credited with the frames it "
        f"takes: {recovered}")


async def test_an_audio_format_change_rebuilds_at_a_rotation_not_by_failing():
    """The raw-PCM twin of the frame-size guard, and WHERE the repair happens.

    ffmpeg is told the audio pipe's sample format, rate and channel count once,
    on the command line, and then reads the pipe as a fixed-size sample stream.
    A frame in a different shape does not fail -- it permanently reinterprets
    every later sample from the wrong offset, so the audio becomes noise and (on
    a changed rate) drifts against the video forever. Exactly like a changed
    frame size, the only clean recovery is to relaunch ffmpeg with the real
    shape.

    **The invariant this pins moved, deliberately.** The repair used to be
    reached by FAILING the session: the pump set a flag, the streaming loop
    raised `audio input needs rebuilding`, and the sink-repair block rebuilt on
    the way back round. That works only if the next session can re-learn the
    shape, and it cannot: aiortc's codec sniff is per session, so every restart
    threw away the 20 packets of evidence the repair was waiting on. On a camera
    whose audio is sparse, the next sizing window therefore saw nothing decoded
    either, and the camera went round that cycle forever with no publisher at
    all (E81).

    So the shape is now learned by observation while the session runs, and the
    publisher is rebuilt at the next ROTATION, where the frame hold already
    bridges the gap -- the identical path a confirmed frame-RATE change takes,
    and for the identical reason: audio may not take the camera off the air.
    Pinned here: the mismatched frame is still never written, the session does
    not fail, and the rebuilt publisher carries the shape the frames really have.
    """
    # The guard itself, on the REAL sink -- the fake mirrors it, but the thing
    # that has to refuse the write in production is this.
    real = fs.FfmpegSink("cam")
    real._audio_transport = types.SimpleNamespace(
        is_closing=lambda: False, get_write_buffer_size=lambda: 0,
        write=lambda data: None)
    real._audio_protocol = types.SimpleNamespace(lost=False)
    real.audio_format = ("s16", 48000, 2)
    real._audio_bytes_per_sample = 4
    assert await real.write_audio(FakeAudioFrame(("s16", 16000, 1), 320)) is False, (
        "the real sink accepted a frame in a different PCM shape than ffmpeg was "
        "launched with -- the raw pipe is now permanently desynchronised")

    # ...and the loop's response: learn the shape from the frames, keep the
    # session, rebuild at the rotation.
    #
    # Three scripted sessions, because a running publisher can only be mismatched
    # by a shape that moves UNDER it: the first streams 48 kHz stereo and sizes
    # the publisher from it, the second decodes nothing by the time the publisher
    # is inspected (its codec sniff is still running) and then delivers 16 kHz
    # mono, and the third settles there. It is session two that has to survive
    # without writing a mismatched sample and without failing.
    steady_frames = dict(frames=steady(8, 0.02), tail=("repeat", 0.02, 1280, 720),
                         audio_interval=0.02)
    with consume_knob("all", request="all"):
        with scenario([Plan(**steady_frames, audio_fmt=("s16", 48000, 2),
                            audio_samples=960),
                       Plan(**steady_frames, audio_fmt=("s16", 16000, 1),
                            audio_peek=None, audio_samples=320),
                       Plan(**steady_frames, audio_fmt=("s16", 16000, 1),
                            audio_samples=320)],
                      SESSION_MAX_LIFETIME_S=0.4, FRAME_SILENCE_TIMEOUT_S=5.0,
                      MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].audio_writes,
                            timeout=10.0, why="a rebuilt publisher carrying audio")
            first, second = ctx.sinks[0], ctx.sinks[1]
            first_fmt = first.start_calls[0][3]
            second_fmt = second.start_calls[0][3]
            first_stopped = first.stopped
            # Every write the first publisher took, and their shapes: the 48 kHz
            # frames of session one belong there, the 16 kHz frames of session
            # two do not.
            first_audio = [n for _, n in first.audio_writes]
            # The mismatched session must have been allowed to FINISH: it wrote
            # video throughout and ended by rotating, not by raising.
            first_video, failures = len(first.writes), ctx.state.get("consecutive_failures")
            last_error = ctx.state.get("last_error")
            state = dict(ctx.state)
            await stop(ctx)

    assert first_fmt == ("s16", 48000, 2), (
        f"the first publisher was not sized from the peeked frame: {first_fmt}")
    assert first_audio and all(n == 960 for n in first_audio), (
        f"the first publisher took a frame that was not the 960-sample 48 kHz "
        f"shape it was launched for: {first_audio[:8]} -- a mismatched frame "
        "written into a pipe ffmpeg reads at a different sample size is silent "
        "corruption, not a failure")
    assert first_stopped == 1, "the mis-sized publisher was left running"
    assert first_video > 16, (
        f"the mismatched session published only {first_video} video frames -- it "
        "was cut short rather than left to rotate, which is the failure loop "
        "this design exists to remove")
    assert failures == 0 and last_error is None, (
        f"the audio-format change failed the session (failures={failures}, "
        f"last_error={last_error!r}) -- that restarts the codec sniff that has "
        "to finish for the repair to be possible at all")
    assert second_fmt == ("s16", 16000, 1), (
        f"the rebuilt publisher did not adopt the shape the real frames carry: "
        f"{second_fmt}")
    assert state.get("audio_format") == "16000Hz/1ch/s16", state
    assert state.get("publisher_audio_format") == "16000Hz/1ch/s16", (
        f"publisher_audio_format does not describe the running publisher: {state}")
    assert state.get("audio_format_rebuilds") == 1, (
        f"the rebuild was not counted against this camera's budget: {state}")


async def test_sparse_audio_converges_instead_of_looping_on_a_rebuild():
    """E81, which is the live failure this whole design is shaped by, and which
    is a LIVELOCK rather than a slow start.

    The camera: audio too sparse to have decoded anything by the time the
    publisher is sized. The decoder's codec sniff needs 20 packets, which at the
    64 ms packetisation these cameras appear to use is 1.28 s of CONTINUOUS
    audio; a quiet room does not send that inside the ten-video-frame window
    (~670 ms at 15fps) that the sizing peek happens at the end of. So the peek
    sees nothing, on this session and on every session after it.

    What that used to cost: the publisher was built from a guessed 48 kHz stereo
    shape, the first real 8 kHz frame tripped the pipe's format guard, the guard
    failed the session, the restart destroyed the per-session sniff, and the next
    peek was blind for exactly the same reason. Round and round, one publisher
    teardown per lap, and the camera ended up with no publisher at all -- live,
    it had to be switched to video-only by hand to get anything published.

    Each lap of that loop threw away precisely the progress that would have ended
    it, which is why "make the race less likely" is not a fix and why the sniff
    is not shortened. What converges it is that the publisher is never sized from
    a guess and an audio-format change never fails a session: the shape is
    learned by OBSERVING the frames while the session runs, and applied by ONE
    rebuild at a rotation the frame hold already bridges.

    Pinned: exactly two publishers across many sessions, the first video-only and
    the second carrying the shape the frames really have; not one failed session;
    and video published throughout, including before the rebuild.
    """
    fmt = ("s16", 8000, 1)
    with consume_knob("all", request="all"):
        # audio_peek=None on EVERY session: this camera never has a decoded
        # frame to show by sizing time, which is the whole scenario.
        with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.05, audio_fmt=fmt, audio_peek=None,
                            audio_samples=160)],
                      SESSION_MAX_LIFETIME_S=0.4, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
            # The failure counters in the status are not enough on their own:
            # they are RESET by the next successful frame batch, so a loop of
            # fail-reconnect-succeed-fail reads as clean whenever it is sampled.
            # The log line each failure emits does not get reset.
            with capture_logs(level=logging.WARNING) as records:
                start(ctx)
                await run_until(ctx, lambda x: len(x.sessions) >= 5, timeout=15.0,
                                why="five sessions of a sparse-audio camera")
                sinks = list(ctx.sinks)
                sessions = len(ctx.sessions)
                first_fmt = sinks[0].start_calls[0][3]
                first_video = len(sinks[0].writes)
                second_fmt = sinks[1].start_calls[0][3] if len(sinks) > 1 else "no rebuild"
                second_audio = [n for _, n in sinks[1].audio_writes] if len(sinks) > 1 else []
                second_video = len(sinks[1].writes) if len(sinks) > 1 else 0
                second_stopped = sinks[1].stopped if len(sinks) > 1 else 0
                state = dict(ctx.state)
                failed = [r for r in records if "camera session failed" in r]
                await stop(ctx)

    assert len(sinks) >= 2, (
        f"one publisher across {sessions} sessions: this camera's audio shape "
        "was never learned at all, so its video-only publisher never gained the "
        "audio input it is waiting for")
    assert len(sinks) == 2, (
        f"{len(sinks)} publishers were built across {sessions} sessions -- one "
        "per session is the E81 loop, and one rebuild is all a learned audio "
        "shape may ever cost")
    assert first_fmt is None, (
        f"the first publisher was sized from something other than a decoded "
        f"frame: {first_fmt} -- a guess here is what the loop was made of")
    assert first_video > 0 and second_video > 0, (
        f"video stopped being published across the rebuild ({first_video} then "
        f"{second_video} frames) -- the camera is never allowed to go dark for "
        "an audio repair")
    assert second_fmt == fmt, (
        f"the rebuilt publisher did not adopt the shape the sparse frames "
        f"carry: {second_fmt}")
    assert second_audio and all(n == 160 for n in second_audio), (
        f"the sparse camera's audio never reached the rebuilt publisher: "
        f"{second_audio[:5]}")
    assert second_stopped == 0, (
        "the rebuilt publisher was torn down again -- convergence means the "
        "camera settles on one publisher, not that it loops more slowly")
    assert not failed and state.get("last_error") is None, (
        f"{len(failed)} session(s) failed on the way to convergence: "
        f"{failed[:1]} -- and a failed session restarts the codec sniff, which "
        "is what made the original failure a loop rather than a stumble")
    assert state.get("state") == "streaming", state
    assert state.get("audio_format") == "8000Hz/1ch/s16", state
    assert state.get("publisher_audio_format") == "8000Hz/1ch/s16", state
    assert state.get("audio_format_rebuilds") == 1, (
        f"the audio shape cost more than the one rebuild it is allowed: {state}")
    print(f"  ok  sparse audio: {sessions} sessions, 2 publishers, "
          f"{len(second_audio)} audio frames published, 0 failed sessions")


async def test_a_camera_that_never_sends_audio_publishes_video_forever():
    """The acceptance case, and the one that was broken: a camera configured to
    consume audio that never sends a single packet must publish video forever.

    Nothing about it is hypothetical. The living room camera decodes nothing for
    minutes at a time, and while the publisher was sized from a guessed audio
    shape that camera could not keep a publisher at all: every session ended in
    `audio input needs rebuilding` and the next one started the same lap.

    With the input sized from observations only there is nothing to be wrong
    about. No frame decoded means no audio input, which means no format guard to
    trip, no rebuild to ask for and no session to fail -- just video, across
    rotation after rotation, on ONE publisher that outlives them all.

    This is deliberately NOT expressed as "audio_dropped is false": a publisher
    that never had an audio input has not dropped one, and reporting a
    degradation that did not happen sends an operator looking for a fault.
    """
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=None, audio_peek=None)],
                      SESSION_MAX_LIFETIME_S=0.4, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: len(x.sessions) >= 6, timeout=15.0,
                            why="six sessions of a silent camera")
            sinks = list(ctx.sinks)
            sessions = len(ctx.sessions)
            sink = sinks[0]
            launched_with = sink.start_calls[0][3]
            video_writes, pcm_writes = len(sink.writes), len(sink.pcm_writes)
            stops, consumed = sink.stopped, ctx.sessions[0].consume_audio
            state = dict(ctx.state)
            await stop(ctx)

    assert consumed is True, (
        "the camera was not even asked to consume audio, so this proves nothing")
    assert len(sinks) == 1 and stops == 0, (
        f"{len(sinks)} publishers and {stops} teardowns across {sessions} "
        "sessions for a camera with no audio at all -- the publisher must "
        "outlive every rotation")
    assert launched_with is None and pcm_writes == 0, (
        f"the publisher was given an audio input ({launched_with}) nothing could "
        f"ever write to, and {pcm_writes} PCM writes went into it")
    assert video_writes > 20, (
        f"only {video_writes} video frames across {sessions} sessions")
    assert state.get("state") == "streaming", state
    assert state.get("consecutive_failures") == 0 and state.get("last_error") is None, (
        f"a session failed for a camera that simply has no audio: "
        f"{state.get('last_error')!r}")
    assert state.get("audio_consumed") is True, state
    assert state.get("audio_format") is None and state.get("audio_codec") is None, state
    assert state.get("publisher_audio_format") is None, state
    assert state.get("audio_format_rebuilds") == 0, state
    assert state.get("audio_dropped") is False, (
        "a publisher that never had an audio input reported having dropped one")
    print(f"  ok  no audio at all: {sessions} sessions, 1 publisher, "
          f"{video_writes} video frames, 0 failed sessions")


async def test_audio_format_rebuilds_are_bounded_and_degrade_to_video_only():
    """A shape that keeps moving costs ONE rebuild, then the audio is given up on.

    A rebuild is not free: the publisher is torn down, MediaMTX destroys the path
    and every RTSP reader is kicked. Learning a camera's audio shape once is
    worth that; following a shape that changes every session is not, and an
    unbounded repair is how a fix for a loop becomes a slower loop. So
    config.AUDIO_FORMAT_REBUILD_LIMIT bounds it, and past the bound the sink's
    audio input is dropped in place -- video-only, visibly, on the SAME publisher
    -- which is the trade `_drop_audio` already makes for a wedged audio pipe.
    Video is worth more than audio; a camera is never taken off the air for it.

    **This scenario is also the closest reproduction of the live E81 loop, and
    that is why it is scripted this way.** The camera decodes nothing by sizing
    time on any session (sparse audio, the codec sniff unfinished) AND the shape
    it eventually delivers alternates between sessions -- which is what a decoder
    committing to a different codec on different sessions looks like from here,
    and the only mechanism that explains a camera failing EVERY session rather
    than one. Under the design that shipped, each lap sized the publisher from
    the shape the previous lap learned, met the other one, failed the session and
    started over: never converging, one publisher teardown per lap, and live it
    ended with no publisher at all. Here it must settle on two publishers, no
    failed sessions, and video throughout.
    """
    steady_frames = dict(frames=steady(10, 0.02), tail=("repeat", 0.02, 1280, 720),
                         audio_interval=0.02, audio_peek=None)
    quiet = Plan(**steady_frames, audio_fmt=("s16", 8000, 1), audio_samples=160)
    loud = Plan(**steady_frames, audio_fmt=("s16", 48000, 2), audio_samples=960)
    with consume_knob("all", request="all"):
        with scenario([quiet, loud] * 6,
                      SESSION_MAX_LIFETIME_S=0.4, FRAME_SILENCE_TIMEOUT_S=5.0,
                      AUDIO_FORMAT_REBUILD_LIMIT=1) as ctx:
            with capture_logs(level=logging.WARNING) as records:
                start(ctx)
                await run_until(ctx, lambda x: x.state.get("audio_dropped") is True,
                                timeout=15.0, why="the audio input to be given up on")
                dropped_at_sessions = len(ctx.sessions)
                # ...and then keep running, because the interesting claim is what
                # happens AFTER the budget is spent: nothing.
                await run_until(ctx, lambda x: len(x.sessions) >= dropped_at_sessions + 4,
                                timeout=15.0, why="four more sessions after the give-up")
                sinks = list(ctx.sinks)
                sessions = len(ctx.sessions)
                live = sinks[-1]
                video_before = len(live.writes)
                await asyncio.sleep(0.2)
                video_after = len(live.writes)
                audio_writes = len(live.audio_writes)
                state = dict(ctx.state)
                failed = [r for r in records if "camera session failed" in r]
                await stop(ctx)

    assert not failed, (
        f"{len(failed)} session(s) failed while the audio shape swung about: "
        f"{failed[:1]} -- that is the E81 loop, and each failure destroys the "
        "codec sniff that would have settled it")

    assert len(sinks) == 2, (
        f"{len(sinks)} publishers across {sessions} sessions -- past the limit "
        "the audio input is dropped in place, NOT rebuilt again")
    assert state.get("audio_format_rebuilds") == 1, state
    assert live.audio_dropped is True, (
        "the publisher kept an audio input it can only ever write silence into, "
        "which is the pure-silence failure E59 exists to make impossible")
    assert state.get("audio_dropped") is True, state
    assert "spent" in (state.get("audio_dropped_reason") or ""), (
        f"the reason does not say why audio stopped: "
        f"{state.get('audio_dropped_reason')!r}")
    assert state.get("publisher_audio_format") is None, state
    assert video_after > video_before, (
        f"video stopped being published after the audio was given up on "
        f"({video_before} -> {video_after} frames)")
    assert state.get("state") == "streaming", state
    print(f"  ok  bounded repair: 1 rebuild, audio dropped after "
          f"{dropped_at_sessions} sessions, video still publishing "
          f"{sessions} sessions in ({audio_writes} audio frames published)")


async def _started_sink(audio_format, backend="vaapi"):
    """A REAL FfmpegSink that has run the whole of start() except the exec.

    Everything the audio input depends on actually happens: the pipe is opened,
    the per-sample width is computed, and the prime is written. Only the child
    process is faked, so the sample accounting under test is the shipped one.
    """
    async def fake_try_start(self, args, pass_fds=()):
        return True

    saved_backend, saved_try = c.ENCODE_BACKEND, fs.FfmpegSink._try_start
    c.ENCODE_BACKEND = backend
    fs.FfmpegSink._try_start = fake_try_start
    try:
        sink = fs.FfmpegSink("cam")
        await sink.start(1280, 720, 15, audio_format=audio_format)
        return sink
    finally:
        c.ENCODE_BACKEND, fs.FfmpegSink._try_start = saved_backend, saved_try


async def test_the_cameras_real_8khz_mono_audio_flows_through_unchanged():
    """The publisher, its pacing and the status at the rate the cameras REALLY
    send.

    The audio track is advertised as Opus and carries G.711 mu-law, so the
    decoded frames are 8 kHz MONO, not the 48 kHz stereo every audio constant in
    this codebase was written beside (see bridge/aiortc_opus_patch.py). Nothing
    in the pacing may be rate-specific: the prime, the per-call ceiling and the
    silence fill are all sample counts derived from the sink's own
    `audio_format[1]`, and a single hardcoded 48000 anywhere in that chain would
    be silent -- it would publish audio at the wrong speed, or leave the muxer
    lead six times shorter than the constant it is chosen against, which is the
    E60 video stall arriving through a different door.

    The command line is pinned on every encoder variant for the same reason
    test_every_encoder_variant_gets_the_audio_input_and_encoder pins the 48 kHz
    one: the software fallback is not exotic, it fires on every start() where
    /dev/dri is missing.
    """
    fmt = ("s16", 8000, 1)
    rate = fmt[1]

    # 1. The command line, on all three variants. The rate travels to BOTH the
    #    raw input (how ffmpeg reads the pipe) and the AAC encoder (what it
    #    publishes); either one left at 48000 resamples the whole stream.
    for backend in ("vaapi", "nvenc", "software"):
        captured = await _captured_ffmpeg_args(backend, fmt)
        assert captured, backend
        for args, pass_fds in captured:
            fd = pass_fds[0]
            audio_in = ["-f", "s16le", "-ar", "8000", "-ac", "1",
                        "-analyzeduration", "0", "-probesize", "32",
                        "-i", f"pipe:{fd}"]
            audio_out = ["-map", "0:v:0", "-map", "1:a:0",
                         "-c:a", "aac", "-ac", "1", "-ar", "8000", "-b:a", "64k",
                         "-af", "aresample=async=1"]
            assert _subsequence_at(args, audio_in) > 0, (
                f"{backend}: the 8 kHz mono raw input is missing or not "
                f"contiguous: {args}")
            assert _subsequence_at(args, audio_out) > _subsequence_at(args, audio_in), (
                f"{backend}: the AAC encoder is not at the frames' own rate: {args}")
            assert "48000" not in args, (
                f"{backend}: a 48000 survived onto the command line of an 8 kHz "
                f"publisher: {args}")
            # The video half is untouched by the audio rate.
            video_in = args[:args.index("-i")]
            assert "-r" not in video_in and _subsequence_at(
                video_in, ["-framerate", "15"]) > 0, args

    # 2. The prime, which IS the muxer lead the video pipe rides on. It has to
    #    be _AUDIO_PRIME_MS of TIME at whatever rate the frames carry, not a
    #    fixed sample count.
    sink = await _started_sink(fmt)
    try:
        primed = sink._audio_samples_written
        assert sink._audio_bytes_per_sample == 2, (
            f"8 kHz mono s16 is 2 bytes per sample across all channels, not "
            f"{sink._audio_bytes_per_sample}")
        assert primed == int(rate * fs._AUDIO_PRIME_MS / 1000), (
            f"the pipe was primed with {primed} samples, which at {rate}Hz is "
            f"{primed / rate * 1000:.0f}ms rather than the "
            f"{fs._AUDIO_PRIME_MS}ms lead every top-up cadence is chosen "
            "against -- a lead computed at a hardcoded 48000 would be six times "
            "too long here, and one computed in samples six times too short")
        assert abs(sink.audio_timeline_s - fs._AUDIO_PRIME_MS / 1000.0) < 1e-9, (
            f"the timeline reads {sink.audio_timeline_s}s for a "
            f"{fs._AUDIO_PRIME_MS}ms prime at {rate}Hz")
    finally:
        await sink.stop()

    # 3. The silence fill, on the REAL method with a scripted clock. Same
    #    invariants as the 48 kHz case: exact wallclock tracking, and a per-call
    #    ceiling expressed in TIME.
    quiet, writes, primed = _silence_rig(rate=rate, channels=1)
    started = quiet._audio_started_at
    cap_samples = int(rate * fs._AUDIO_SILENCE_MAX_BURST_S)
    assert await quiet.write_silence_to(now=started + 0.05) is True
    assert writes == [], (
        f"silence was written while the audio still led the wallclock: {writes}")
    for elapsed in (0.15, 0.19, 0.27, 0.33, 0.41):
        now = started + elapsed
        assert await quiet.write_silence_to(now=now) is True
        assert quiet._audio_samples_written == int((now - started) * rate), (
            f"after {elapsed:.2f}s the 8 kHz stream holds "
            f"{quiet._audio_samples_written} samples, not the "
            f"{int((now - started) * rate)} elapsed real time calls for")
    assert max(writes) <= cap_samples * quiet._audio_bytes_per_sample, (
        f"a single 8 kHz top-up handed over {max(writes)} bytes, past the "
        f"{fs._AUDIO_SILENCE_MAX_BURST_S}s ceiling")
    # The cadence is a TIME, so it is rate-independent by construction -- but
    # only while the two quantities it is compared against are also times.
    assert c.AUDIO_SILENCE_TOPUP_S < fs._AUDIO_PRIME_MS / 1000.0 <= 1.0, (
        "the top-up cadence no longer sits under the muxer lead")
    assert cap_samples / rate == fs._AUDIO_SILENCE_MAX_BURST_S, (
        f"the per-call ceiling is {cap_samples / rate}s at {rate}Hz rather than "
        f"the {fs._AUDIO_SILENCE_MAX_BURST_S}s it is defined as")

    # 4. End to end through the loop, from the state that actually occurs on a
    #    cold start: nothing has decoded when the publisher is sized (the codec
    #    sniff is still running), so it comes up VIDEO-ONLY and gains its 8 kHz
    #    audio input at the next rotation, once real frames have said what shape
    #    they are.
    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(8, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02, audio_fmt=fmt,
                            audio_peek=None, audio_samples=160)],
                      SESSION_MAX_LIFETIME_S=0.4, FRAME_SILENCE_TIMEOUT_S=5.0,
                      MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: len(x.sinks) >= 2 and x.sinks[1].audio_writes,
                            timeout=10.0, why="a rebuilt 8 kHz publisher carrying audio")
            first_fmt = ctx.sinks[0].start_calls[0][3]
            second = ctx.sinks[1]
            second_fmt = second.start_calls[0][3]
            audio_writes = list(second.audio_writes)
            state = dict(ctx.state)
            await stop(ctx)

    assert first_fmt is None, (
        f"the first publisher was given an audio input before anything had "
        f"decoded: {first_fmt}")
    assert second_fmt == fmt, (
        f"the rebuilt publisher did not adopt the 8 kHz mono shape the frames "
        f"carry: {second_fmt}")
    assert audio_writes and all(n == 160 for _, n in audio_writes), (
        f"the 8 kHz frames were not written whole: {audio_writes[:5]}")
    assert state.get("audio_format") == "8000Hz/1ch/s16", state
    # ...and the field that says WHICH codec, not just which shape. This is the
    # question nothing in the status could answer while the bridge spent two
    # days decoding mu-law as Opus.
    assert state.get("audio_codec") == "pcmu", (
        f"audio_codec reads {state.get('audio_codec')!r} for a stream of 8 kHz "
        "mono frames, which only the G.711 mu-law decoder produces")
    print(f"  ok  8kHz mono audio: argv, {fs._AUDIO_PRIME_MS}ms prime, wallclock "
          f"fill and status all follow the frames' own rate")


async def test_a_malformed_audio_frame_is_skipped_and_a_run_of_them_drops_audio():
    """A frame whose bytes disagree with its own declared shape must not fail
    the session -- a rebuild cannot fix it, so forcing one is a loop.

    The frame's FORMAT matches what ffmpeg was launched with, so the relaunched
    command line would be byte-identical and the same refusal would recur --
    session after session, each one delivering its first video batch (which
    resets the consecutive-failure count), so the loop never even reaches the
    container-restart escape hatch. Instead: the frame is dropped and its span
    filled with silence from the wallclock, which keeps the audio's lead over
    the video intact (the lead is what keeps ffmpeg reading the VIDEO pipe).
    A RUN of them is a broken decoder, not a glitch, and latches the same
    visible video-only degradation a wedged pipe takes.
    """
    written = []
    real = fs.FfmpegSink("cam")
    real.proc = types.SimpleNamespace(returncode=None, stdin=object())
    real._audio_transport = types.SimpleNamespace(
        is_closing=lambda: False, get_write_buffer_size=lambda: 0,
        write=lambda data: written.append(data), close=lambda: None)
    real._audio_protocol = types.SimpleNamespace(lost=False)
    real.audio_format = ("s16", 16000, 1)
    real._audio_bytes_per_sample = 2
    real._audio_started_at = time.monotonic()
    real._audio_samples_written = 0

    def frame(nbytes, samples=320):
        f = FakeAudioFrame(("s16", 16000, 1), samples)
        f.to_ndarray = lambda: types.SimpleNamespace(
            tobytes=lambda: b"\x55" * nbytes)
        return f

    # A well-formed frame reaches the pipe.
    assert await real.write_audio(frame(640)) is True
    assert any(b"\x55" in w for w in written), "a good frame never reached the pipe"

    # A malformed one does NOT fail the sink, does not reach the pipe, and the
    # video half is untouched.
    written.clear()
    assert await real.write_audio(frame(100)) is True, (
        "one malformed frame failed the audio input -- the session fails, the "
        "publisher rebuilds with an identical command line, and the same frame "
        "shape fails it again: an unbounded rotation loop")
    assert not any(b"\x55" in w for w in written), (
        "the malformed bytes were written into the pipe -- every later sample "
        "is now decoded from the wrong offset")
    assert real.audio_dropped is False

    # A good frame in between resets the run: 49 bad + good + 49 bad is a
    # glitchy decoder, not a dead one. (One malformed frame is already on the
    # run from above.)
    for _ in range(fs._AUDIO_MALFORMED_LIMIT - 2):
        assert await real.write_audio(frame(100)) is True
    assert real.audio_dropped is False, (
        f"{fs._AUDIO_MALFORMED_LIMIT - 1} consecutive malformed frames already "
        "dropped the audio -- the limit is off by one")
    assert await real.write_audio(frame(640)) is True
    for _ in range(fs._AUDIO_MALFORMED_LIMIT - 1):
        assert await real.write_audio(frame(100)) is True
    assert real.audio_dropped is False, (
        "a good frame did not reset the malformed run -- an occasionally-glitchy "
        "decoder would accumulate to video-only over hours")

    # A full consecutive run IS a broken decoder: latch video-only, visibly.
    assert await real.write_audio(frame(640)) is True
    for _ in range(fs._AUDIO_MALFORMED_LIMIT - 1):
        await real.write_audio(frame(100))
    assert await real.write_audio(frame(100)) is False
    assert real.audio_dropped is True and "shape" in (real.audio_dropped_reason or ""), (
        f"a persistent run of malformed frames did not drop the audio input: "
        f"dropped={real.audio_dropped} reason={real.audio_dropped_reason!r}")
    assert real.publisher_alive is True, (
        "malformed AUDIO frames took the VIDEO publisher down")


async def test_silence_is_the_formats_zero_not_zero_bytes():
    """For the unsigned 8-bit sample formats, digital silence is the 0x80
    midpoint; all-zero bytes there are a full-scale negative DC offset that
    would publish as a loud click at every fill boundary. Every other format
    this sink accepts is signed or float, where zero bytes are genuinely zero.

    Pinned through the real start() (with the launch and the pipe stubbed out)
    because the byte is chosen there, from the launched format, and both the
    prime and every later fill must use it.
    """
    for fmt, silence in ((("u8", 8000, 1), 0x80), (("s16", 16000, 1), 0x00)):
        written = []

        async def fake_try_start(self, args, pass_fds=()):
            return True

        async def fake_open(self, write_fd):
            os.close(write_fd)
            self._audio_transport = types.SimpleNamespace(
                is_closing=lambda: False, get_write_buffer_size=lambda: 0,
                write=lambda data: written.append(data), close=lambda: None)
            self._audio_protocol = types.SimpleNamespace(lost=False)

        saved = (fs.FfmpegSink._try_start, fs.FfmpegSink._open_audio_writer)
        fs.FfmpegSink._try_start = fake_try_start
        fs.FfmpegSink._open_audio_writer = fake_open
        try:
            sink = fs.FfmpegSink("cam")
            await sink.start(64, 64, 15, audio_format=fmt)
            assert written, f"{fmt}: the pipe was never primed"
            # The prime, and a later wallclock fill, both in the format's zero.
            sink._audio_started_at -= 0.05
            assert await sink.write_silence_to() is True
            for chunk in written:
                assert set(chunk) == {silence}, (
                    f"{fmt}: silence was written as "
                    f"{sorted(set(chunk))} rather than {silence:#x} -- for u8 "
                    "that is a full-scale DC offset, not silence")
        finally:
            fs.FfmpegSink._try_start, fs.FfmpegSink._open_audio_writer = saved


async def test_a_publisher_born_without_its_audio_pipe_says_so():
    """start() degrading to video-only because the audio pipe would not open
    must be as visible as a mid-session drop.

    The sink half: the same audio_dropped latch _drop_audio sets, so
    `audio_enabled` is false and nothing will write. The loop half: the status
    is read off the fresh sink, so /control/status carries the reason -- before
    this, the degradation left audio_consumed true, decoded_audio_frames
    climbing, and nothing anywhere saying why the published stream had no
    sound.
    """
    async def fake_try_start(self, args, pass_fds=()):
        # The real _try_start adopts the process on success; publisher_alive
        # reads it, so the stub has to as well.
        self.proc = types.SimpleNamespace(returncode=None, stdin=object())
        return True

    async def fake_open_raises(self, write_fd):
        raise OSError("no pipe for you")

    saved = (fs.FfmpegSink._try_start, fs.FfmpegSink._open_audio_writer)
    fs.FfmpegSink._try_start = fake_try_start
    fs.FfmpegSink._open_audio_writer = fake_open_raises
    try:
        sink = fs.FfmpegSink("cam")
        await sink.start(64, 64, 15, audio_format=("s16", 48000, 2))
    finally:
        fs.FfmpegSink._try_start, fs.FfmpegSink._open_audio_writer = saved
    assert sink.audio_enabled is False and sink.audio_format is None
    assert sink.audio_dropped is True and sink.audio_dropped_reason, (
        "a publisher born video-only over a failed pipe is indistinguishable "
        "from one that is video-only by configuration")
    assert sink.publisher_alive is True, (
        "a failed AUDIO pipe failed the whole publisher")

    # The loop half, against the scenario fakes: a sink that comes up with
    # audio already dropped lands in /control/status at the build, without
    # waiting for a writer to notice (no writer ever runs -- audio_enabled is
    # false, so the pump is never started and the hold skips the fill).
    def born_without_audio(sink_, width, height, fps):
        sink_._audio_transport = None
        sink_.audio_dropped = True
        sink_.audio_dropped_reason = "the audio pipe could not be opened"

    with consume_knob("all", request="all"):
        with scenario([Plan(frames=steady(8, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02)],
                      SESSION_MAX_LIFETIME_S=5.0, FRAME_SILENCE_TIMEOUT_S=5.0) as ctx:
            ctx.sink_started_hook = born_without_audio
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("audio_dropped") is True,
                            timeout=10.0, why="the at-start degradation to be reported")
            state = dict(ctx.state)
            await stop(ctx)
    assert state.get("audio_dropped_reason") == "the audio pipe could not be opened", state
    assert state.get("state") == "streaming", (
        f"a failed audio pipe took the camera off the air: {state}")


async def test_ffmpeg_duplicated_frame_warnings_are_counted():
    """Every "frames duplicated" line ffmpeg writes to stderr is counted (E70).

    The count is the encoder's OWN corroboration of a declared/delivered rate
    gap, from the other side of the pipe than RateWatcher's arithmetic. It is
    observability only: one-directional (fires only when the declared rate is
    too high), not quantitative, and coarse -- so it feeds no control decision,
    it just makes "did the correction remove the padding" answerable from
    /control/status instead of from container-log archaeology.
    """
    class FakeStderr:
        def __init__(self, lines):
            self._lines = list(lines)

        async def readline(self):
            return self._lines.pop(0) if self._lines else b""

    real = fs.FfmpegSink("cam")
    real.proc = types.SimpleNamespace(returncode=None, stderr=FakeStderr([
        b"[vf#0:0 @ 0x5c1] More than 1000 frames duplicated\n",
        b"Past duration 0.999992 too large\n",
        b"[vf#0:0 @ 0x5c1] More than 10000 frames duplicated\n",
        b"[rtsp @ 0x5c2] Nonmatching transport in server reply\n",
    ]))
    await real._log_stderr()
    assert real.duplicated_frame_warnings == 2, (
        f"{real.duplicated_frame_warnings} warnings counted from a stderr "
        "stream carrying exactly two -- the count either misses the 10000-frame "
        "escalation or fires on unrelated encoder noise")


async def test_every_encoder_variant_gets_the_audio_input_and_encoder():
    """A hardware path that publishes audio while the software fallback silently
    does not is the exact class of bug this codebase keeps finding -- and the
    fallback is not exotic here, it fires on every start() where /dev/dri is
    missing or the driver has reset. So all three variants are built from the
    same input/output fragments, and this pins that they still are.

    It also pins ffmpeg's positional option grammar: the audio input options must
    sit contiguously in front of their own `-i`, and the maps/encoder after the
    last input. An option that drifts past its `-i` silently belongs to something
    else. The video input's own rate declaration -- the one thing that makes a
    two-input publish survive at all -- is pinned in
    test_the_video_input_declares_its_rate_with_framerate_never_r, which covers
    both shapes.
    """
    audio_format = ("s16", 48000, 2)
    seen = {}
    for backend in ("vaapi", "nvenc", "software"):
        captured = await _captured_ffmpeg_args(backend, audio_format)
        assert captured, backend
        for args, pass_fds in captured:
            assert len(pass_fds) == 1, (
                f"{backend}: the audio pipe's read end was not handed to ffmpeg: "
                f"{pass_fds}")
            fd = pass_fds[0]
            # No -use_wallclock_as_timestamps on this input: the audio timeline
            # is its sample count, not the moment a write happened to be
            # flushed. Pinned because the flag reads like an obvious symmetry
            # with the video input above it.
            audio_in = ["-f", "s16le", "-ar", "48000", "-ac", "2",
                        "-analyzeduration", "0", "-probesize", "32",
                        "-i", f"pipe:{fd}"]
            assert args.count("-use_wallclock_as_timestamps") == 1, (
                f"{backend}: the audio input was given -use_wallclock_as_timestamps "
                f"as well as the video input: {args}")
            # THE invariant this whole feature stands on -- restated in full,
            # for both shapes, in
            # test_the_video_input_declares_its_rate_with_framerate_never_r.
            assert "-r" not in args[:args.index("-i")], (
                f"{backend}: `-r` is back on the video input of an audio-enabled "
                f"argv -- the video timeline then ignores the wallclock and "
                f"ffmpeg stops reading the audio pipe within seconds: {args}")
            audio_out = ["-map", "0:v:0", "-map", "1:a:0",
                         "-c:a", "aac", "-ac", "1", "-ar", "48000", "-b:a", "64k",
                         "-af", "aresample=async=1"]
            at_in = _subsequence_at(args, audio_in)
            at_out = _subsequence_at(args, audio_out)
            assert at_in > 0, (
                f"{backend}: the audio input is missing or not contiguous: {args}")
            assert at_out > at_in, (
                f"{backend}: the audio maps/encoder are missing or precede the "
                f"input they map: {args}")
            # The video input still comes first (input 0), the audio maps/encoder
            # sit in the OUTPUT section, and the publish URL is still last.
            assert args.index("pipe:0") < at_in, args
            assert at_out < _subsequence_at(args, ["-f", "rtsp"]), args
            assert args[-7:] == ["-f", "rtsp", "-rtsp_transport", "tcp",
                                 "-pkt_size", "1200", c.rtsp_publish_url("cam")], args
        seen[backend] = [a for a, _ in captured]

    # vaapi and nvenc each launch once (their attempt "succeeds" here); the point
    # is that the software argv -- the fallback every hardware failure lands on --
    # carries the identical audio fragments, not a subset of them.
    for backend, arglists in seen.items():
        for args in arglists:
            assert args.count("-c:a") == 1 and args.count("-map") == 2, (backend, args)


async def test_the_video_input_declares_its_rate_with_framerate_never_r():
    """`-framerate <fps>` on the raw video input, on EVERY argv this sink builds,
    and `-r` on none of them. Both halves fail silently in production, in
    opposite directions.

    `-r <fps>` before `-i` forces the input stream's frame rate and in doing so
    overrides `-use_wallclock_as_timestamps`: the timeline advances 1/fps per
    frame WRITTEN instead of per second ELAPSED. A camera delivering fewer frames
    than its measured fps then publishes less media than wall time -- nothing
    errors, the picture looks fine, and an HLS consumer simply drifts further
    behind live for as long as the stream runs. With a second input it is worse
    than silent: the audio timeline really is wallclock, audio outruns video, and
    ffmpeg stops draining the audio pipe within seconds. Measured in the deployed
    container (ffmpeg 7.1.5, h264_vaapi, live RTSP publish), input declared 15fps
    with frames written at 9fps: 40s of wall time published as 24.07s of media,
    and with audio attached the pipe backlog crossed the sink's 512 kB limit at
    9.6s.

    Dropping the declaration entirely is NOT the fix, which is the half that is
    easy to get wrong: the rawvideo demuxer's own `framerate` defaults to 25, so
    an input with no rate at all is re-rated to a constant 25fps -- 361 written
    frames encoded as 1001 for a 15fps camera, and 1201 written encoded as 1002
    (199 DROPPED) for a 30fps one -- while `-g <fps>` then spans 0.6s or 1.2s
    instead of the ~1s it exists to produce. `-framerate <fps>` declares the rate
    to the demuxer without touching the timestamps: same run, 40.07s of media per
    40s of wall time, 15fps out, 1.0s GOP, and the audio pipe backlog flat at 0
    bytes across a 100s publish.
    """
    for backend in ("vaapi", "nvenc", "software"):
        for audio_format in (None, ("s16", 48000, 2)):
            captured = await _captured_ffmpeg_args(backend, audio_format)
            assert captured, (backend, audio_format)
            for args, _pass_fds in captured:
                video_in = args[:args.index("-i")]
                assert "-r" not in video_in, (
                    f"{backend} (audio={audio_format is not None}): `-r` is on the "
                    f"video input -- the timeline then advances per frame written, "
                    f"not per second elapsed: {args}")
                assert _subsequence_at(video_in, ["-framerate", "15"]) > 0, (
                    f"{backend} (audio={audio_format is not None}): the video input "
                    f"does not declare `-framerate 15`, so ffmpeg falls back to the "
                    f"rawvideo demuxer's default 25fps and re-rates the output: "
                    f"{args}")
                # ...and the declaration belongs to input 0, not to whatever
                # follows it: `-framerate` is a demuxer option, so a copy that
                # drifted past `-i pipe:0` would silently apply to the audio
                # input or to the output instead.
                assert args.count("-framerate") == 1, (backend, args)
                # -g is still the same fps, unscaled -- the ~1s keyframe interval
                # is only ~1s because the output rate is the declared rate.
                assert _subsequence_at(args, ["-g", "15"]) > args.index("-i"), (
                    f"{backend}: -g no longer follows the input as a plain frame "
                    f"count at the declared fps: {args}")


async def test_consume_without_request_degrades_to_off_and_says_so_once():
    """Consuming audio that was never asked for is not a smaller version of the
    feature, it is a different bug: no audio track is ever offered, so the
    publisher would come up with a second input that only ever receives the
    silence fill -- a stream that exists, carries nothing, and makes the muxer
    interleave against it forever. There is no way for an operator to see that
    from the outside, so the misconfiguration is reported rather than absorbed.

    Once per camera, not once per session: the resolution sits on a path that
    runs at camera start-up today, and a warning that repeated every ~165s would
    be noise rather than a signal.
    """
    with consume_knob("cam-a,cam-b", request="false"):
        with capture_logs("bridge.config", level=logging.WARNING) as lines:
            first = c.janus_consume_audio_for("cam-a")
            again = c.janus_consume_audio_for("cam-a")
            other = c.janus_consume_audio_for("cam-b")
            unnamed = c.janus_consume_audio_for("cam-c")
        warnings_for_a = [ln for ln in lines if "cam-a" in ln]
        warnings_for_b = [ln for ln in lines if "cam-b" in ln]
        warnings_for_c = [ln for ln in lines if "cam-c" in ln]

    assert first is False and again is False and other is False, (first, again, other)
    assert unnamed is False, "a camera the knob does not name consumed audio"
    assert len(warnings_for_a) == 1, (
        f"the CONSUME-without-REQUEST misconfiguration was reported "
        f"{len(warnings_for_a)} times for one camera: {warnings_for_a}")
    assert "JANUS_REQUEST_AUDIO" in warnings_for_a[0], warnings_for_a[0]
    assert len(warnings_for_b) == 1, warnings_for_b
    assert not warnings_for_c, (
        f"a camera the knob does not name produced a misconfiguration warning: "
        f"{warnings_for_c}")

    # Requesting it too is what turns it on -- nothing else changed.
    with consume_knob("cam-a", request="cam-a"):
        assert c.janus_consume_audio_for("cam-a") is True
        assert c.janus_consume_audio_for("cam-b") is False

    # ...and the loop honours the degradation end to end: no audio input, no pump.
    with consume_knob("all", request="false"):
        with scenario([Plan(frames=steady(8, 0.02), tail=("repeat", 0.02, 1280, 720),
                            audio_interval=0.02)],
                      SESSION_MAX_LIFETIME_S=5.0) as ctx:
            start(ctx)
            await run_until(ctx, lambda x: x.state.get("state") == "streaming",
                            timeout=8.0, why="a streaming session")
            await asyncio.sleep(0.2)
            start_call = ctx.sink.start_calls[0]
            pcm = list(ctx.sink.pcm_writes)
            state = dict(ctx.state)
            consumed = ctx.sessions[0].consume_audio
            await stop(ctx)

    assert start_call[3] is None, (
        f"a publisher was given an audio input for audio that is never offered: "
        f"{start_call}")
    assert consumed is False and not pcm, (state, pcm)
    assert state.get("audio_consumed") is False, state


async def test_the_consume_knob_takes_the_same_three_value_forms():
    """One value grammar across both audio knobs, including the rule that keeps
    it safe: `all` is only ever recognised as the WHOLE setting, never as one
    entry of a list, or a camera literally called "all" would silently turn
    consumption on for every camera on the account."""
    for off in ("false", "0", "no", "off", "", "  ", "FALSE"):
        with consume_knob(off, request="all"):
            assert c.janus_consume_audio_mode("cam-a") == "off", off
            assert c.janus_consume_audio_for("cam-a") is False, off
    for on in ("true", "1", "yes", "on", "all", "  true  "):
        with consume_knob(on, request="all"):
            assert c.janus_consume_audio_mode("cam-a") == "all", on
            assert c.janus_consume_audio_for("cam-a") is True, on
    with consume_knob(" doorbell-2048 , indoor-2049x ", request="all"):
        assert c.janus_consume_audio_mode("doorbell-2048") == "listed"
        # Exact match: an id that merely PREFIXES a listed one is not a match.
        assert c.janus_consume_audio_mode("indoor-2049") == "unlisted"
        assert c.janus_consume_audio_for("doorbell-2048") is True
        assert c.janus_consume_audio_for("indoor-2049") is False
    with consume_knob("all,doorbell-2048", request="all"):
        assert c.janus_consume_audio_for("some-other-camera") is False, (
            "'all' was honoured as one entry of a list -- a camera literally called "
            "'all' would then turn consumption on for every camera on the account")


async def test_the_offered_audio_track_is_pumped_only_when_the_session_consumes():
    """The REAL JanusCameraSession's on_track, both ways round.

    With consumption off the track must still be DRAINED -- aiortc decodes it
    into an unbounded queue regardless, so ignoring it leaks for the whole ~165s
    life of every session. With consumption on the same queue must still reach
    zero (pumping empties it exactly as draining does), the frames must actually
    be kept, and an audio track ENDING must still not mark the session dead:
    only the video track decides that.
    """
    gateway = ScriptedGateway()
    session = await _watch_once(gateway)
    assert session.consume_audio is False, "sessions consume audio by default"
    on_track = session.pc.handlers["track"]
    ignored = FakeRemoteTrack("audio")
    on_track(ignored)
    for i in range(50):
        ignored.queue.put_nowait(f"frame{i}")
    for _ in range(400):
        if ignored.queue.qsize() == 0:
            break
        await asyncio.sleep(0.005)
    assert ignored.queue.qsize() == 0, "an unconsumed audio track was not drained"
    assert session._audio_queue.qsize() == 0, (
        "an unconsumed audio track was queued for publishing anyway")
    await session.close()

    from bridge import janus_client as jc
    gateway = ScriptedGateway()
    with janus_rig(gateway):
        session = jc.JanusCameraSession(
            {"id": "cam", "mac_address": "AA:BB:CC:DD:EE:FF", "model": "ADC-V",
             "firmware": "1"},
            {"proxy_url": "rtsp://relay.invalid/stream",
             "janus_gateway_url": "wss://gw.invalid",
             "janus_token": "TOKEN", "ice_servers": []},
            consume_audio=True)
        await asyncio.wait_for(session.connect_and_watch(), timeout=10)
    on_track = session.pc.handlers["track"]
    audio = FakeRemoteTrack("audio")
    on_track(audio)
    frame = FakeAudioFrame(("s16", 48000, 2), 960)
    audio.queue.put_nowait(frame)
    got = await asyncio.wait_for(session.next_audio_frame(2.0), timeout=5.0)
    assert got is frame, f"a consumed audio track did not deliver its frame: {got!r}"
    assert session.audio_format == ("s16", 48000, 2), (
        f"the decoded audio shape was not recorded for the publisher to be sized "
        f"from: {session.audio_format}")
    # A quiet stretch is a timeout, not an end -- the publisher fills it with
    # silence rather than tearing anything down.
    quiet = await asyncio.wait_for(session.next_audio_frame(0.05), timeout=5.0)
    assert quiet is AUDIO_TIMEOUT, quiet
    # ...and the reader survives it. An implementation that wrapped an async
    # generator in wait_for would have killed the generator on that timeout, so
    # this second frame would never arrive.
    audio.queue.put_nowait(frame)
    again = await asyncio.wait_for(session.next_audio_frame(2.0), timeout=5.0)
    assert again is frame, (
        "the audio stream ended at the first quiet interval -- a bounded wait must "
        "not tear down the reader it is waiting on")

    audio.queue.put_nowait(None)          # the decoder stopping, as at pc.close()
    await asyncio.sleep(0.05)
    assert not session._dead.is_set(), (
        "a consumed audio track ending marked the whole session dead -- it would "
        "force a video reconnect every time the audio stopped")
    await session.close()


async def test_audio_drops_accumulate_separately_from_the_video_count():
    """`fps` and `dropped_frames` are a documented /control/status contract about
    the VIDEO stream. A caller that has always read dropped_frames as "video
    frames lost" must not silently start getting a mixed count, so audio drops
    get a field of their own -- accumulated across rotations the same way, on the
    healthy path and the failure path alike."""
    plans = [
        Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720),
             audio_interval=0.02, dropped_frames=7, dropped_audio_frames=9),
        Plan(connect_error=RuntimeError("no frames received before timeout"),
             dropped_frames=5, dropped_audio_frames=4),
        Plan(frames=steady(6, 0.02), tail=("repeat", 0.02, 1280, 720),
             audio_interval=0.02),
    ]
    with consume_knob("all", request="all"):
        with scenario(plans, SESSION_MAX_LIFETIME_S=0.3,
                      MAX_CONSECUTIVE_SESSION_FAILURES=99) as ctx:
            start(ctx)
            await asyncio.sleep(0.05)
            early = ctx.state.get("dropped_audio_frames", "absent")
            await run_until(ctx, lambda x: x.state.get("dropped_audio_frames", 0) >= 13,
                            timeout=10.0, why="both retired sessions' audio drops")
            state = dict(ctx.state)
            await stop(ctx)

    assert early == 0, (
        f"dropped_audio_frames must exist as 0 from the first status entry, got "
        f"{early!r} -- the status shape is a contract and a key that appears later "
        "is a different shape")
    assert state.get("dropped_audio_frames") == 13, (
        f"9 (rotated) + 4 (failed) should accumulate to 13, got "
        f"{state.get('dropped_audio_frames')!r}")
    assert state.get("dropped_frames") == 12, (
        f"the VIDEO counter changed meaning: expected 7 + 5 = 12, got "
        f"{state.get('dropped_frames')!r}")


# --------------------------------------------------------------------------
# 6d. The session-handover trace (E76)
# --------------------------------------------------------------------------

@contextlib.contextmanager
def handover_knob(enabled, settle=None):
    """Set config.HANDOVER_TRACE (and optionally the settle tail) for one test.

    The knob is read live off the module attribute wherever it is consulted --
    handover_trace.begin() and JanusCameraSession's construction -- so assigning
    it here is the same thing the env var does at process start.
    """
    saved = (c.HANDOVER_TRACE, c.HANDOVER_TRACE_SETTLE_S)
    c.HANDOVER_TRACE = bool(enabled)
    if settle is not None:
        c.HANDOVER_TRACE_SETTLE_S = settle
    try:
        yield
    finally:
        c.HANDOVER_TRACE, c.HANDOVER_TRACE_SETTLE_S = saved


class StubTraceSink:
    """A publisher's audio bookkeeping as the record reads it, driven by hand.

    The loop-level tests below run the real arithmetic against the real fakes;
    this one exists so the record's own derivations -- where the audio timeline
    stopped, and for how long -- can be asserted against numbers chosen rather
    than raced for.
    """

    def __init__(self, timeline=0.1, lead=0.1):
        self.fps = 15
        self.audio_enabled = True
        self.audio_dropped = False
        self.timeline = timeline
        self.lead = lead

    @property
    def audio_timeline_s(self):
        return self.timeline

    @property
    def audio_lead_s(self):
        return self.lead

    def advanced(self, seconds, lead=None):
        """The pipe was written to: the timeline moved with the wall clock."""
        self.timeline += seconds
        if lead is not None:
            self.lead = lead

    def stalled(self, seconds):
        """Nothing wrote: the timeline stands still and the lead decays."""
        self.lead -= seconds


class StubTraceSession:
    """A session's read-only diagnostic surface (all properties on the real one)."""

    def __init__(self, drop_times=(), depth=0, limit=8, dropped=0):
        self.drop_times = tuple(drop_times)
        self.video_queue_depth = depth
        self.video_queue_limit = limit
        self.dropped_frames = dropped


def _records(lines):
    """Every handover record in a captured log, parsed."""
    out = []
    for line in lines:
        if "handover trace " in line:
            out.append(json.loads(line.split("handover trace ", 1)[1]))
    return out


def _mark_index(record, label):
    for index, mark in enumerate(record["marks"]):
        if mark["m"] == label:
            return index
    return -1


async def test_the_handover_trace_is_off_by_default_and_emits_nothing():
    """The default deployment must be indistinguishable from one with no
    instrumentation in it at all.

    That is not a preference: this measures a window whose whole problem is that
    ~14 frames go missing inside it, so anything that runs there by default is a
    change to the thing being measured. Off, `begin()` returns None, every call
    site is an `is not None` test on that None, and no session remembers a drop
    time.
    """
    assert c.HANDOVER_TRACE is False, (
        "HANDOVER_TRACE must default to off -- an instrumentation flag that "
        "ships on is instrumentation nobody chose to run")
    with handover_knob(False):
        assert ht.begin("cam", None, None) is None, (
            "begin() built a record with tracing off: the guard at every call "
            "site is `is not None` on this value, so a non-None here puts the "
            "whole mechanism on the frame path of every deployment")
        with consume_knob("all", request="all"):
            with scenario([Plan(frames=steady(6, 0.02),
                                tail=("repeat", 0.02, 1280, 720),
                                audio_interval=0.02)],
                          SESSION_MAX_LIFETIME_S=0.3) as ctx:
                with capture_logs("bridge.handover_trace") as lines:
                    start(ctx)
                    await run_until(ctx, lambda x: len(x.sessions) >= 3,
                                    why="3 sessions (2 rotations)")
                    await stop(ctx)
                emitted = list(lines)

    assert not emitted, (
        f"tracing is off and {len(emitted)} handover line(s) were logged: {emitted[:1]}")


async def test_a_traced_rotation_emits_one_record_covering_the_whole_window():
    """One rotation, one record, and the record spans the window end to end.

    The window is the thing E76 is about: from `proactive session rotation` to
    the new session settling. Every question it has to answer needs two events
    that bracket it, so a record missing any of these marks cannot answer
    anything -- and the pairs asserted below are the ones whose ORDER is
    structural rather than raced: the hold stops the previous pump before it
    writes its first silence, and the loop readies the publisher, starts the new
    pump and writes the first batch strictly in that order.
    """
    with handover_knob(True, settle=0.05):
        with consume_knob("all", request="all"):
            with scenario([Plan(frames=steady(6, 0.02),
                                tail=("repeat", 0.02, 1280, 720),
                                audio_interval=0.02)],
                          SESSION_MAX_LIFETIME_S=0.4) as ctx:
                with capture_logs("bridge.handover_trace") as lines:
                    start(ctx)
                    await run_until(ctx, lambda x: len(_records(lines)) >= 2,
                                    timeout=10.0, why="two handover records")
                    await stop(ctx)
                records = _records(lines)
                sessions = len(ctx.sessions)

    assert len(records) <= sessions - 1, (
        f"{len(records)} records for {sessions} sessions: a record is one "
        "ROTATION, and more of them than rotations means the window is being "
        "opened somewhere it does not belong")
    record = records[0]
    assert record["reason"] == "settled", (
        f"the first record closed as {record['reason']!r}, not on its settle "
        "tail -- the drops being measured land just after the first batch, so a "
        "record that closes earlier reports the window without its consequence")
    assert record["audio"] is True and record["fps"], record
    assert record["span_ms"] > 0 and record["audio_ms"] is not None, record
    assert record["first_frame_ms"] is not None, (
        "the record does not say when the new session's first frame arrived, "
        "which is one half of the interval every drop in E76 falls inside")

    for label in ("rotation", "pump_stopped", "hold_first_silence",
                  "session_retired", "batch_collected", "hold_stopped",
                  "publisher_ready", "pump_created", "first_write",
                  "batch_written", "settled"):
        assert _mark_index(record, label) >= 0, (
            f"the record has no {label!r} mark: {[m['m'] for m in record['marks']]}")

    order = [
        ("rotation", "pump_stopped"),          # the hold's first act
        ("pump_stopped", "hold_first_silence"),  # ...then, and only then, silence
        ("hold_first_silence", "hold_stopped"),
        ("session_retired", "batch_collected"),
        ("batch_collected", "hold_stopped"),
        ("hold_stopped", "publisher_ready"),
        ("publisher_ready", "pump_created"),
        ("pump_created", "first_write"),
        ("first_write", "batch_written"),
        ("batch_written", "settled"),
    ]
    for earlier, later in order:
        assert _mark_index(record, earlier) < _mark_index(record, later), (
            f"{earlier!r} is recorded after {later!r}; the record's ordering is "
            "the measurement, so a mark in the wrong place is a wrong reading, "
            f"not a cosmetic one: {[m['m'] for m in record['marks']]}")

    times = [mark["t"] for mark in record["marks"]]
    assert times == sorted(times), f"mark timestamps are not monotonic: {times}"
    assert record["marks"][0]["m"] == "rotation" and record["marks"][0]["t"] == 0.0, (
        "the record's clock does not start at the rotation, so every offset in "
        "it is measured from somewhere else")
    assert record["hold"]["silence_writes"] >= 1, (
        f"the record says the frame hold wrote no silence during the gap: "
        f"{record['hold']}")


async def test_the_record_shows_where_the_audio_timeline_stopped():
    """The measurement itself: an interval where the wall clock advanced and the
    audio timeline did not.

    This is the quantity E76's suspected mechanism is stated in. Each mark
    carries `dt` (wall clock since the previous mark) beside `adv` (audio media
    written since the previous mark); `worst_gap` is the largest `dt - adv` and
    names the two marks that bracket it. A stall in the serialization window
    therefore reads as `worst_gap.from = hold_stopped, to = pump_first_write,
    adv = 0`, and the lead reaching zero or below is the same event seen from
    the other side.

    Driven against a hand-driven sink rather than a live one so the numbers are
    chosen: a rig that raced for them could not tell a real stall from a slow
    test machine.
    """
    with handover_knob(True):
        sink = StubTraceSink()
        before = time.monotonic() - 0.5      # a drop from BEFORE this rotation
        retiring = StubTraceSession(drop_times=(before,), dropped=1)
        trace = ht.begin("cam", sink, retiring)
        assert trace is not None
        # A healthy stretch: the hold is writing, so the timeline tracks the
        # wall clock and the lead holds.
        await asyncio.sleep(0.05)
        sink.advanced(0.05)
        trace.session_retired(retiring, sink)
        # ...and the record must not be what keeps a retired session alive: it
        # owns a decoded-frame queue of up to 8 x 3.11 MB on the doorbell, and a
        # diagnostic holding that for the rest of the window would be changing
        # the memory behaviour it is there to watch.
        retiring_ref = weakref.ref(retiring)
        del retiring
        gc.collect()
        assert retiring_ref() is None, (
            "the record still references the retired session, so its "
            "decoded-frame queue cannot be freed until the record is emitted")
        trace.mark("hold_stopped", sink)
        # The serialization window: nothing writes to the audio pipe at all.
        drop_at = time.monotonic()
        await asyncio.sleep(0.12)
        sink.stalled(0.12)
        session = StubTraceSession(drop_times=(drop_at,), depth=8, limit=8,
                                   dropped=14)
        trace.mark("pump_first_write", sink, session)
        await asyncio.sleep(0.03)
        sink.advanced(0.03, lead=0.1)
        trace.emit(sink, session)
        record = trace.record(sink, session)

    gap = record["worst_gap"]
    assert gap["from"] == "hold_stopped" and gap["to"] == "pump_first_write", (
        f"the record blames the wrong interval: {gap}. The stall was scripted "
        "between those two marks and nowhere else")
    assert gap["adv"] == 0.0, (
        f"the stalled interval is recorded as having advanced the audio "
        f"timeline by {gap['adv']}ms; it advanced by nothing")
    assert 110 <= gap["ms"] <= 200, (
        f"the stall is reported as {gap['ms']}ms of uncovered wall clock, "
        "against a scripted 120ms")
    assert record["lead_min_at"] == "pump_first_write", (
        f"the minimum lead is attributed to {record['lead_min_at']!r}: the "
        "lead is at its worst at the END of a stall, which is the mark that "
        "closes it")
    assert -45 <= record["lead_min_ms"] <= -5, (
        f"lead_min_ms is {record['lead_min_ms']}, against a scripted 100ms lead "
        "decayed by a 120ms stall, i.e. about -20ms. The SIGN is the whole "
        "point: negative means the audio no longer covers the video, which is "
        "when ffmpeg stops reading the video pipe")
    assert record["audio_ms"] == 80.0, (
        f"the window wrote 0.05s + 0.03s of audio, so audio_ms must be 80.0, "
        f"got {record['audio_ms']}")

    drops = record["drops"]
    assert drops["retiring"]["in_window"] == 0, (
        "a drop from before the rotation was counted inside the window -- the "
        "record would then blame the handover for the whole session's drops")
    assert drops["retiring"]["session_total"] == 1, drops
    assert drops["new"]["in_window"] == 1 and drops["total"] == 1, drops
    assert 0 <= drops["new"]["at_ms"][0] <= record["span_ms"], (
        f"the drop is placed at {drops['new']['at_ms']}ms of a "
        f"{record['span_ms']}ms window -- outside it, which means it is being "
        "measured from something other than the rotation")
    assert record["marks"][-1]["q"] == 8, (
        "the record does not carry the video queue depth, so 'the queue was "
        "full when the frames were lost' is not checkable from it")


async def test_the_muxer_lead_is_read_off_the_sinks_own_sample_counter():
    """`audio_lead_s` is a DERIVATION, not a second tally.

    The lead is the quantity the whole pacing design turns on (see
    ffmpeg_sink._AUDIO_PRIME_MS), and it is already implicit in what
    write_silence_to maintains: the audio covers `_audio_started_at +
    samples/rate`, the video is stamped with the wall clock. A separately
    counted "lead" could disagree with the pipe's actual contents, which is the
    one thing a measurement of it must never do.

    Pinned here: after the prime, the lead IS the prime; a stretch with nothing
    written decays it by exactly the elapsed time and takes it NEGATIVE, which
    is the shape of a stalled window.
    """
    # `scenario` only for its bookkeeping: FakeSink registers itself with it.
    # Nothing is started, and no loop runs.
    with scenario([Plan()]):
        sink = FakeSink("cam")
        assert sink.audio_lead_s is None and sink.audio_timeline_s is None, (
            "a publisher with no audio input reported a lead; there is no audio "
            "timeline to lead with")
        await sink.start(1280, 720, 15, audio_format=("s16", 48000, 2))

    primed_lead = sink.audio_lead_s
    assert sink.audio_timeline_s == fs._AUDIO_PRIME_MS / 1000.0, (
        f"the timeline reads {sink.audio_timeline_s}s after a "
        f"{fs._AUDIO_PRIME_MS}ms prime")
    assert 0.05 < primed_lead <= 0.1 + 1e-6, (
        f"the lead reads {primed_lead}s immediately after the pipe was primed "
        f"with {fs._AUDIO_PRIME_MS}ms of silence")

    # Nothing written for 400ms: exactly the serialization window's shape.
    sink._audio_started_at -= 0.4
    stalled_lead = sink.audio_lead_s
    assert -0.25 > stalled_lead > -0.35, (
        f"400ms with nothing written left the lead at {stalled_lead}s, where "
        "the 100ms prime decayed by 400ms is -0.3s. A lead that cannot go "
        "negative cannot report the failure it exists to report")
    assert sink.audio_timeline_s == fs._AUDIO_PRIME_MS / 1000.0, (
        "the timeline moved without anything being written to the pipe")


async def test_dropped_frames_are_timestamped_only_when_tracing():
    """A dropped frame gets a TIME, and only while tracing is on.

    The drop log is throttled to the first of a session and then every 150th, so
    a session that drops fourteen leaves exactly one line: the log understates
    the rate by design, and it cannot place a single drop against a rotation.
    That is why the times are kept at the drop site instead. Off, the deque
    stays empty, so the drop path is what it always was.

    Driven through the REAL JanusCameraSession, filling the real queue past its
    real depth, because the queue's own maxsize is what decides when a drop
    happens at all.
    """
    gateway = ScriptedGateway()
    with handover_knob(True):
        traced = await _watch_once(gateway)
    video = FakeRemoteTrack("video")
    traced.pc.handlers["track"](video)
    limit = traced.video_queue_limit
    for i in range(limit + 5):
        video.queue.put_nowait(f"frame{i}")
    for _ in range(400):
        if traced.dropped_frames >= 5:
            break
        await asyncio.sleep(0.005)
    traced_times = traced.drop_times
    traced_dropped = traced.dropped_frames
    depth = traced.video_queue_depth
    await traced.close()

    gateway = ScriptedGateway()
    with handover_knob(False):
        untraced = await _watch_once(gateway)
    video = FakeRemoteTrack("video")
    untraced.pc.handlers["track"](video)
    for i in range(untraced.video_queue_limit + 5):
        video.queue.put_nowait(f"frame{i}")
    for _ in range(400):
        if untraced.dropped_frames >= 5:
            break
        await asyncio.sleep(0.005)
    untraced_times = untraced.drop_times
    untraced_dropped = untraced.dropped_frames
    await untraced.close()

    assert traced_dropped >= 5, (
        f"the queue (maxsize {limit}) did not overflow: {traced_dropped} drops")
    assert len(traced_times) == traced_dropped, (
        f"{traced_dropped} frames were dropped and {len(traced_times)} times "
        "were recorded -- a record that misses drops cannot say a rotation "
        "carried all of them")
    assert list(traced_times) == sorted(traced_times), (
        "the drop times are not in order, so nothing can be placed against them")
    assert depth == limit, (
        f"video_queue_depth reads {depth} against a full queue of {limit}: the "
        "depth sample is what says how much of the queue's tolerance was left")
    assert untraced_dropped >= 5 and not untraced_times, (
        f"tracing is off and {len(untraced_times)} drop times were kept anyway; "
        "the drop path must be exactly what it was")


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
    test_a_first_frame_timeout_reports_how_long_it_actually_waited,
    test_the_streaming_line_reports_how_long_the_first_frame_took,
    test_frame_hold_bridges_the_reconnect_gap,
    test_frame_hold_is_bounded_by_time_since_a_real_frame,
    test_a_failed_hold_task_does_not_fail_the_next_session,
    test_fps_is_the_median_delta_not_the_span,
    test_the_fps_fallback_is_never_locked_in,
    test_a_real_fps_is_not_overwritten_by_a_burst,
    test_the_rate_hysteresis_band_is_asymmetric_and_clears_observed_jitter,
    test_the_rate_constants_hold_their_meaningful_ranges,
    test_a_settled_rate_is_measured_past_the_burst_and_needs_two_to_agree,
    test_a_confirmed_reversal_is_flapping_and_resolves_to_the_higher_rate,
    test_flap_memory_expires_once_the_high_rate_stops_appearing,
    test_a_settled_rate_change_rebuilds_the_publisher_without_failing_the_session,
    test_the_frame_hold_repeats_at_the_rate_ffmpeg_was_declared_with,
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
    test_the_audio_experiment_is_off_by_default_and_changes_nothing,
    test_the_audio_experiment_asks_for_opus_alongside_the_video,
    test_a_rejected_audio_create_falls_back_instead_of_failing_the_session,
    test_the_audio_knob_names_the_cameras_it_applies_to,
    test_an_offered_track_we_do_not_consume_is_drained_not_ignored,
    test_audio_consumption_is_off_by_default_and_changes_no_wire_behaviour,
    test_a_consume_enabled_camera_publishes_its_audio,
    test_audio_status_reports_only_what_was_actually_decoded,
    test_the_silence_fill_tracks_the_wallclock_and_never_bursts,
    test_the_rotation_gap_is_filled_with_wallclock_derived_silence,
    test_a_quiet_camera_is_topped_up_under_the_lead_and_never_in_bursts,
    test_the_frame_queue_is_sized_in_time_not_in_frames,
    test_a_wedged_audio_pipe_degrades_to_video_only,
    test_a_publisher_that_stopped_reading_is_detected_and_rebuilt,
    test_an_audio_format_change_rebuilds_at_a_rotation_not_by_failing,
    test_sparse_audio_converges_instead_of_looping_on_a_rebuild,
    test_a_camera_that_never_sends_audio_publishes_video_forever,
    test_audio_format_rebuilds_are_bounded_and_degrade_to_video_only,
    test_the_cameras_real_8khz_mono_audio_flows_through_unchanged,
    test_ffmpeg_duplicated_frame_warnings_are_counted,
    test_a_malformed_audio_frame_is_skipped_and_a_run_of_them_drops_audio,
    test_silence_is_the_formats_zero_not_zero_bytes,
    test_a_publisher_born_without_its_audio_pipe_says_so,
    test_every_encoder_variant_gets_the_audio_input_and_encoder,
    test_the_video_input_declares_its_rate_with_framerate_never_r,
    test_consume_without_request_degrades_to_off_and_says_so_once,
    test_the_consume_knob_takes_the_same_three_value_forms,
    test_the_offered_audio_track_is_pumped_only_when_the_session_consumes,
    test_audio_drops_accumulate_separately_from_the_video_count,
    test_the_handover_trace_is_off_by_default_and_emits_nothing,
    test_a_traced_rotation_emits_one_record_covering_the_whole_window,
    test_the_record_shows_where_the_audio_timeline_stopped,
    test_the_muxer_lead_is_read_off_the_sinks_own_sample_counter,
    test_dropped_frames_are_timestamped_only_when_tracing,
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
