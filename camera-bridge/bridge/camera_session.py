import asyncio
import logging
import os
import time

from . import config as c
from . import ffmpeg_sink
from . import restart_guard
from .janus_client import AUDIO_TIMEOUT, JanusCameraSession
from .ffmpeg_sink import FfmpegSink
from .telus_auth import RateLimited

log = logging.getLogger(__name__)


async def _close_with_timeout(session: JanusCameraSession, camera_id: str) -> None:
    """session.close() calls aiortc's pc.close(), which lifecycle.py already
    documents as "a known-hangable call under certain ICE states" with no
    timeout of its own. Both call sites below used to await it unbounded --
    if it actually hangs (rather than just failing-and-retrying, which is what
    2026-07-20's MAX_CONSECUTIVE_SESSION_FAILURES escape hatch was built for),
    the loop can never get back around to accumulate toward that failure
    count, since it's stuck on this very await. Bounding it here means a hang
    surfaces as one more counted failure instead of an unrecoverable wedge."""
    try:
        await asyncio.wait_for(session.close(), timeout=c.SESSION_CLOSE_TIMEOUT_S)
    except asyncio.TimeoutError:
        log.error("%s: session.close() did not finish within %ss -- abandoning it, "
                  "may leak resources (counted as a failure so the consecutive-failure "
                  "escape hatch above can still fire)", camera_id, c.SESSION_CLOSE_TIMEOUT_S)
    except Exception:
        # Anything else raised by aiortc's pc.close() or the websocket close must be
        # swallowed here. Both call sites are inside the reconnect loop's own
        # `except`/`finally` blocks, so an exception raised from this cleanup escapes
        # `while True` entirely and returns from run_camera_forever -- permanently and
        # silently killing this one camera. Nothing would restart it:
        # discover_and_launch_cameras skips ids already in the registry, and
        # CameraLifecycleManager holds the finished task forever without inspecting
        # its result, so even asyncio's "exception was never retrieved" warning never
        # fires. Log and let the loop go around instead.
        log.exception("%s: session.close() raised; ignoring so the reconnect loop survives",
                      camera_id)


def _audio_format_str(fmt) -> str | None:
    """`(sample format, rate, channels)` as /control/status's `audio_format`
    string, or None for "nothing observed".

    Deliberately fed from OBSERVED frames only. It used to be rendered from what
    the running ffmpeg had been LAUNCHED with, which is
    `config.AUDIO_DEFAULT_FORMAT` -- literally ("s16", 48000, 2) -- whenever no
    frame had been decoded yet. That made the field report a confident
    "48000Hz/2ch/s16" for a camera whose audio-decoder thread was dead and whose
    published track was pure silence, and it is why eleven consecutive sessions
    of total silence went unnoticed. A field that reads the same whether or not
    audio exists cannot answer the only question it is ever asked."""
    if not fmt:
        return None
    return f"{fmt[1]}Hz/{fmt[2]}ch/{fmt[0]}"


# --- the frame rate is a DEVICE SETTING, not a constant discovered once -------
# Absolute sanity range for ANY fps measurement, first batch or settled. Outside
# it the reading is an artifact of how frames were delivered, not a rate: a
# rotation's buffered frames arriving back-to-back have produced 544, 215 and 62
# fps for cameras whose real rates are 15 and 30.
_RATE_PLAUSIBLE_MIN = 1
_RATE_PLAUSIBLE_MAX = 60


def _median(values):
    """Middle value of `values` (mean of the middle two when even), or None.

    One definition for both fps measurements -- the first batch's and the
    settled re-measurement's -- because they are the same statistic answering
    the same question, and a second copy is a second thing to get wrong.
    """
    ordered = sorted(values)
    if not ordered:
        return None
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


def _fps_from_deltas(deltas):
    """Frames per second implied by a batch of inter-frame deltas, or None if
    the batch does not imply a plausible rate.

    The MEDIAN delta, never the span divided by the count: the span form takes
    its worst-case hit from just the first and last timestamps, so one
    buffered/burst frame at either end contaminates the whole estimate even when
    every frame in between arrived at the camera's real pace. The median is only
    fooled if a MAJORITY of the batch is contaminated.
    """
    median_delta = _median(list(deltas))
    if not median_delta or median_delta <= 0:
        return None
    measured = round(1 / median_delta)
    if not _RATE_PLAUSIBLE_MIN <= measured <= _RATE_PLAUSIBLE_MAX:
        return None
    return measured


def _rate_moved(declared, wanted) -> bool:
    """Has the rate moved far enough from what ffmpeg was DECLARED with to be
    worth rebuilding the publisher for?

    The band is ASYMMETRIC, and which way round is settled by measurement rather
    than taste. Both errors are silent, but they are not equally bad:

      * Declaring HIGHER than the source delivers pads the output to CFR with
        duplicated frames -- 15 declared against 9 delivered encoded 361 written
        frames as 601 (see ffmpeg_sink's table), and live, a camera declared at
        29fps logged "More than 1000 frames duplicated" within 2.5 minutes.
        Wasted encode, wrong pacing, but no content is lost.
      * Declaring LOWER than the source delivers DROPS real frames -- 30fps fed
        into an effective 25 encoded 1201 written frames as 1002, losing 199 of
        them. That content is gone.

    So the downward correction (we are declaring too high) is the cheaper one to
    get wrong in the other direction, and the upward one destroys frames if it
    fires on noise. Measurement error points the same way: burst-contaminated
    readings are biased HIGH -- buffered frames arrive back-to-back, never
    slower than real time -- so "the camera is faster than we thought" is
    exactly the claim an artifact makes, and deserves more scepticism than "the
    camera is slower than we thought".

    Both bands sit above the jitter this project has actually observed. The same
    camera has read 6 and 7 across restarts with nothing changed (a 17% step,
    inside both bands), and a camera whose setting is 15 delivers ~13.4 (an 11%
    step) -- which must NOT be corrected to 13, because 15 is the real setting
    and declaring 13 would start dropping the frames that do arrive on time.
    Real setting changes on this platform are far larger: 15 -> 7 is -53%,
    15 -> 30 is +100%, and the live 29 -> ~22 implied by that duplication rate
    is -24%.
    """
    if not declared or declared <= 0 or wanted is None:
        return False
    if wanted < declared:
        return (declared - wanted) > c.RATE_CHANGE_TOLERANCE_DOWN * declared
    return (wanted - declared) > c.RATE_CHANGE_TOLERANCE_UP * declared


class RateWatcher:
    """Keeps one camera's frame rate under review for as long as it streams, and
    says when the publisher has to be rebuilt for a change.

    The rate is a DEVICE SETTING -- the live-video resolution select ships as a
    Home Assistant entity, so the rate can change from HA, from the TELUS app or
    from the portal, with no restart and no signal to this bridge. Measuring it
    once and latching it for the life of the process is therefore wrong by
    construction rather than merely imprecise: `-framerate` is the OUTPUT's
    constant frame rate and the basis for `-g`, so a stale value duplicates or
    drops frames for as long as the process lives.

    Every rebuild costs a real glitch -- the publisher is torn down, MediaMTX
    destroys the path, every RTSP reader is kicked and every HLS viewer gets a
    discontinuity -- so four separate things stand between a measurement and a
    rebuild, and each of them is load-bearing:

      * A SETTLE WINDOW (config.RATE_RESAMPLE_SETTLE_S). The first measurement
        of a session is taken from the first ten frames and is demonstrably
        unreliable: a rotation's buffered frames arrive in a near-instant burst,
        which has produced 544, 215 and (live, on the doorbell) 62fps readings
        for cameras running at 15 and 30. Nothing is measured until the session
        has been delivering frames for this long, so the burst is behind us.
      * A BATCH (config.RATE_RESAMPLE_FRAMES) with the median taken over it, so
        a single late or bunched frame moves nothing.
      * A HYSTERESIS BAND (_rate_moved), so ordinary measurement jitter never
        starts a rebuild loop.
      * CONFIRMATION: two consecutive settled batches must agree with each other
        before anything is acted on. This converges a FAST-flapping source --
        one that alternates quicker than two settled batches (~12s) never
        produces two agreeing batches in a row, so it is never confirmed. It
        does NOT converge a slow one, and a slow one exists: the 30fps camera
        on this account halves its delivered rate under marginal light and
        comes back, dwelling minutes at each rate, so both ends of the swing
        confirm. That case belongs to the flap guard below.
      * A FLAP GUARD: a confirmed change straight back to the rate the last
        rate rebuild moved away from, within config.RATE_FLAP_WINDOW_S of it,
        is the flapping signature. It is resolved ONCE, to the HIGHER of the
        two rates -- declaring high pads the output with duplicates, declaring
        low drops real frames (ffmpeg_sink's measured table) -- and then held:
        confirmed changes down to the flap's low rate are refused for as long
        as settled batches keep sighting the high one, and for
        config.RATE_FLAP_HOLD_S past the last such sighting. A real setting
        change to the low rate stops the sightings, outlives the hold, and is
        applied late rather than never.
      * A MINIMUM INTERVAL (config.RATE_REBUILD_MIN_INTERVAL_S) between
        rate-driven rebuilds, so even a source that defeats all of the above
        cannot cost more than one glitch per window. A change it delays stays
        confirmed (the candidate is kept), so the first agreeing batch after
        the floor acts on it.

    It never decides anything by itself: it returns a confirmed rate and the
    caller applies it through the same sink-rebuild path a frame-size change
    already takes, at the next session rotation. Nothing here can fail a live
    session or take a camera off the air.
    """

    def __init__(self, camera_id: str):
        self.camera_id = camera_id
        self._deltas: list[float] = []
        self._prev_at: float | None = None
        self._settle_until: float | None = None
        # The last settled batch that asked for a change, waiting for a second
        # batch to agree with it. Deliberately survives a rotation: it is a fact
        # about the camera, not about one session.
        self.candidate: int | None = None
        self.last_rebuild_at: float | None = None
        # The most recent settled batch's measurement (raw, pre-clamp), kept
        # purely for observability: /control/status serves it as measured_fps
        # so an operator can see the declared/delivered gap before ffmpeg
        # complains about it. Survives rotations like everything else here.
        self.last_measured: int | None = None
        # The rate the last rate-driven rebuild moved AWAY from. A confirmed
        # change straight back to it is the flapping signature (see _observe),
        # not a second setting change.
        self.prev_declared: int | None = None
        # Flap memory: the two rates a flapping source swings between, and how
        # long the memory lives. All camera facts, so all survive rotations.
        self.flap_low: int | None = None
        self.flap_high: int | None = None
        self.flap_until: float | None = None
        self._holdoff_logged = False
        self._flap_refused_logged = False

    def session_started(self, now: float):
        """A new Janus session is streaming. Restart the settle window and throw
        away any partial batch: the frames on either side of a rotation are the
        burst this whole mechanism exists to not measure."""
        self._deltas = []
        self._prev_at = None
        self._settle_until = now + c.RATE_RESAMPLE_SETTLE_S

    def rebuilt(self, now: float, from_fps=None):
        """The caller rebuilt the publisher for a rate change; start the
        minimum-interval clock, and remember the rate the change moved away
        from -- a confirmed return TO it is how flapping is recognised."""
        self.last_rebuild_at = now
        if from_fps:
            self.prev_declared = int(from_fps)

    def note_frame(self, now: float, declared):
        """Record one live frame's arrival. Returns a newly CONFIRMED rate for
        this camera, or None -- which is almost always the answer."""
        if self._settle_until is None or now < self._settle_until:
            return None
        if self._prev_at is not None:
            self._deltas.append(now - self._prev_at)
        self._prev_at = now
        if len(self._deltas) < c.RATE_RESAMPLE_FRAMES:
            return None
        deltas, self._deltas = self._deltas, []
        return self._observe(_fps_from_deltas(deltas), declared, now)

    def _observe(self, measured, declared, now):
        if measured is not None:
            self.last_measured = int(measured)
        if measured is None or not declared:
            # An unmeasurable batch is not evidence of anything, and it must not
            # be able to CONFIRM the batch before it either.
            self.candidate = None
            return None
        # Compare against what ffmpeg would actually be given, not the raw
        # reading: 40 and 55 are both published at 30, and "the rate moved" has
        # to mean "the command line would change".
        wanted = ffmpeg_sink.clamp_fps(measured)
        # Flap-memory upkeep, before anything else: while the memory lives, any
        # settled batch back at the flap's high rate is evidence the source
        # still visits it, and pushes the expiry out. A source that has
        # genuinely settled at the low rate stops producing these, the memory
        # expires RATE_FLAP_HOLD_S later, and the ordinary mechanism takes the
        # change from there -- late, but taken.
        flap_active = self.flap_until is not None and now < self.flap_until
        if flap_active and self.flap_high is not None \
                and not _rate_moved(self.flap_high, wanted):
            self.flap_until = now + c.RATE_FLAP_HOLD_S
        if not _rate_moved(declared, wanted):
            self.candidate = None
            return None
        if self.candidate is None or _rate_moved(self.candidate, wanted):
            # First sighting, or a sighting that disagrees with the pending one.
            # Either way this batch becomes the thing the NEXT batch has to
            # agree with -- a FAST-flapping source lands here forever.
            self.candidate = wanted
            self._holdoff_logged = False
            log.info("%s: measured %dfps against the declared %dfps -- waiting for a "
                     "second measurement to agree before rebuilding the publisher",
                     self.camera_id, wanted, declared)
            return None
        # Two consecutive settled batches agree: the change is CONFIRMED. From
        # here down the question is whether acting on it is wise, not whether
        # the evidence is real.
        if flap_active and self.flap_low is not None and wanted < declared \
                and not _rate_moved(self.flap_low, wanted):
            # The low half of a known flap. Refusing it is the point of the
            # memory: held at the high rate the output pads with duplicates
            # (the cheap error), where following the source down is a
            # reader-kicking rebuild now and another when it comes back up.
            self.candidate = None
            log_fn = log.debug if self._flap_refused_logged else log.info
            self._flap_refused_logged = True
            log_fn("%s: still flapping (between ~%dfps and ~%dfps) -- refusing the "
                   "confirmed change to %dfps and keeping the publisher at %dfps",
                   self.camera_id, self.flap_low, self.flap_high, wanted, declared)
            return None
        if not flap_active and self.prev_declared is not None \
                and self.last_rebuild_at is not None \
                and now - self.last_rebuild_at < c.RATE_FLAP_WINDOW_S \
                and not _rate_moved(self.prev_declared, wanted):
            # A confirmed return to the very rate the last rebuild moved away
            # from, this soon after it: the source's DELIVERED rate is flapping
            # with no setting change (live: the 30fps camera halves its rate
            # under marginal light and comes back, dwelling minutes at each
            # end -- long enough that both ends confirm). Resolve it once, to
            # the HIGHER rate, and hold there.
            low, high = sorted((int(self.prev_declared), int(declared)))
            self.flap_low, self.flap_high = low, high
            self.flap_until = now + c.RATE_FLAP_HOLD_S
            self._flap_refused_logged = False
            if wanted <= declared:
                self.candidate = None
                log.warning(
                    "%s: the delivered rate is flapping between ~%dfps and ~%dfps "
                    "(a confirmed return %.0fs after the rebuild that left it) -- "
                    "keeping the publisher at %dfps, which pads with duplicates "
                    "where the lower rate would drop real frames",
                    self.camera_id, low, high, now - self.last_rebuild_at, declared)
                return None
            log.warning(
                "%s: the delivered rate is flapping between ~%dfps and ~%dfps "
                "(a confirmed return %.0fs after the rebuild that left it) -- "
                "resolving to the higher %dfps and holding there, which pads "
                "with duplicates where the lower rate would drop real frames",
                self.camera_id, low, high, now - self.last_rebuild_at, wanted)
            # Falls through: the upward resolution is still one rebuild, and
            # still subject to the minimum interval like any other.
        if self.last_rebuild_at is not None and \
                now - self.last_rebuild_at < c.RATE_REBUILD_MIN_INTERVAL_S:
            # The evidence stays confirmed -- the candidate is KEPT, so the
            # first agreeing batch after the floor acts on it. Only the clock
            # is short, and a paced delay is routine, not warning-level news;
            # said once per candidate, not once per batch.
            if not self._holdoff_logged:
                self._holdoff_logged = True
                log.info("%s: the confirmed change to %dfps waits for the rebuild "
                         "floor (%.0fs of %ds since the last rate rebuild)",
                         self.camera_id, wanted, now - self.last_rebuild_at,
                         c.RATE_REBUILD_MIN_INTERVAL_S)
            return None
        self.candidate = None
        return wanted


async def run_camera_forever(telus, camera: dict, get_video_source, status: dict):
    backoff = c.RECONNECT_BACKOFF_INITIAL_S
    consecutive_failures = 0  # reset on any successful frame batch; see the
    # os._exit() escape hatch in the except block below for why this exists.
    # This camera's current best answer for its own frame rate: bootstrapped from
    # the first batch of a session and then kept under review by the RateWatcher
    # below, because it is a DEVICE SETTING that can change at any time. It is
    # NOT "learned once and reused forever" -- that was E64.
    known_fps = None
    # A confirmed rate change waiting to be applied. It is applied at the next
    # session rotation, through the same sink-rebuild path a frame-size change
    # takes, rather than by failing the live session: the rotation gap is already
    # bridged by the frame hold, so the rebuild costs the glitch it costs and not
    # a moment of dark stream on top of it.
    rate_rebuild_needed = False

    camera_id = camera["id"]
    # Resolved ONCE for this camera, here, and handed down to every component
    # that needs it (the Janus session, which decides whether to pump or drain
    # the offered track, and the sink, which is built with or without a second
    # input). Reading the knob independently in each of those places is how the
    # two could end up disagreeing -- a session pumping audio into a publisher
    # that has nowhere to put it, or a publisher with an audio input nothing ever
    # writes to. config.janus_consume_audio_for also folds in the
    # CONSUME-without-REQUEST degradation (warned once, then treated as off).
    consume_audio = c.janus_consume_audio_for(camera_id)
    # Live diagnostics surfaced read-only via control_server.py's
    # /control/status -- deferred item from the original design ("no /status
    # endpoint yet"), now backed by this loop updating its own entry directly
    # rather than the control server having to infer state from the outside.
    status[camera_id] = {
        "state": "connecting", "consecutive_failures": 0, "last_frame_at": None,
        "fps": None, "hardware_encode": None, "last_error": None,
        "dropped_frames": 0,
        # Audio fields are deliberately SEPARATE from fps/dropped_frames rather
        # than folded into them: those two are a documented /control/status
        # contract about the video stream, and a caller that has always read
        # dropped_frames as "video frames lost" must not silently start getting a
        # mixed count.
        # audio_format stays null until a frame is actually DECODED (see
        # _audio_format_str), and decoded_audio_frames is what makes "audio is
        # flowing" checkable without measuring the published track: a camera
        # with audio_consumed true and decoded_audio_frames stuck at 0 is
        # publishing silence, which is exactly the failure aiortc_opus_patch.py
        # exists for and which nothing here could see before.
        "audio_consumed": consume_audio, "audio_format": None,
        "decoded_audio_frames": 0, "dropped_audio_frames": 0,
        # "audio was dropped mid-session" -- the publisher gave up on its audio
        # input and is publishing video only (ffmpeg_sink._drop_audio). Without
        # this the degradation is invisible: audio_consumed still reads true (the
        # camera IS configured to consume), decoded_audio_frames still climbs
        # (frames are still being decoded), and nothing else in the status
        # changes at all.
        "audio_dropped": False, "audio_dropped_reason": None,
        # The rate under review (E69/E70): fps above is what ffmpeg was
        # DECLARED with; measured_fps is what the camera is actually delivering
        # (the last settled batch), rate_pending is a CONFIRMED change waiting
        # for the rotation that applies it, and duplicated_frame_warnings is
        # ffmpeg's own stderr corroboration that the declared rate exceeds
        # delivery. All three update as frames flow, so they can lag by up to
        # one frame interval (and duplicated_frame_warnings by a reconnect gap).
        "measured_fps": None, "rate_pending": None,
        "duplicated_frame_warnings": 0,
        # ...and the video counterpart of the same question. last_frame_at says a
        # frame arrived FROM the camera; publisher_alive and last_publish_at say
        # ffmpeg took it. Throughout the E62 outage the first was fresh, the
        # state read "streaming", and there was no publisher at all.
        "publisher_alive": None, "last_publish_at": None,
    }

    def _update_status(**fields):
        status[camera_id].update(fields)

    # Lifetime total of frames dropped because the consumer fell behind (see
    # janus_client._pump_track). Kept here because each session's own counter
    # dies with it at every ~165s rotation, so before this the throttled log
    # lines were the only trace -- now /control/status carries the running
    # total per camera.
    dropped_frames_total = 0
    dropped_audio_frames_total = 0
    # Re-measures the rate while the stream runs; see RateWatcher for why the
    # first batch alone cannot be trusted with it and what stands between a
    # measurement and a rebuild.
    rate = RateWatcher(camera_id)
    # "frames duplicated" stderr warnings from PUBLISHERS THAT HAVE BEEN
    # REPLACED; the live sink's own counter is added on top at each status
    # write, so /control/status carries a lifetime total like dropped_frames.
    dup_warnings_folded = 0
    # Lifetime total of decoded audio frames this camera's sessions have handed
    # to the publisher. Unlike the two drop counters it is NOT folded in at
    # session end -- it is incremented by the pump as each frame arrives, so it
    # answers "is audio flowing right now" rather than "did audio flow during
    # the session that just retired".
    decoded_audio_frames_total = 0

    async def _retire_session(sess):
        # The single exit for a finished/failed session: fold its drop counter
        # into the lifetime total BEFORE the close, which is bounded but can
        # time out and abandon the session object entirely. Close ordering,
        # bounding and error handling are unchanged (_close_with_timeout).
        nonlocal dropped_frames_total, dropped_audio_frames_total
        dropped_frames_total += sess.dropped_frames
        dropped_audio_frames_total += getattr(sess, "dropped_audio_frames", 0)
        _update_status(dropped_frames=dropped_frames_total,
                       dropped_audio_frames=dropped_audio_frames_total)
        await _close_with_timeout(sess, camera_id)

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
    # The audio format the RUNNING ffmpeg was launched with, and the one learned
    # from real decoded frames -- the audio counterparts of sink_wh and the (w, h)
    # read off each session's first frames. known_audio_fmt persists across
    # rotations for the same reason known_fps does: a camera's audio shape does
    # not change between sessions, and re-guessing it every rotation would risk
    # rebuilding the publisher for nothing.
    sink_audio_fmt: tuple | None = None
    known_audio_fmt: tuple | None = None
    audio_rebuild_needed = False
    last_frame = None
    last_live = 0.0  # monotonic time a real (non-repeated) frame last arrived
    hold_task: asyncio.Task | None = None
    audio_task: asyncio.Task | None = None

    def _note_audio_dropped(sink_):
        # The publisher gave up on its audio input mid-session and kept
        # publishing video. Recorded once, from whichever writer noticed --
        # the pump and the frame hold both call the sink, and only one of
        # them is running at a time.
        if sink_ is not None and sink_.audio_dropped \
                and not status[camera_id].get("audio_dropped"):
            _update_status(audio_dropped=True,
                           audio_dropped_reason=sink_.audio_dropped_reason)

    async def _pump_audio(sess):
        # Per-session: drains this session's decoded audio into the publisher's
        # second input. It may not raise into its ensure_future (asyncio would log
        # a never-retrieved exception at GC time) and it may not fail the session
        # directly -- a sink that refuses a frame sets the rebuild flag and the
        # VIDEO loop acts on it, so the repair goes through exactly the same path
        # a frame-size change does. A sink that has DROPPED its audio input is
        # not a refusal to repair: it is already publishing video only, and this
        # pump simply stops.
        nonlocal known_audio_fmt, audio_rebuild_needed, decoded_audio_frames_total
        # Audio written since this loop last yielded. The queue holds up to
        # AUDIO_QUEUE_MAXSIZE frames, so a backlog can be tens of frames deep and
        # each one costs an executor round trip on the same executor
        # write_frame's pixel copy uses -- see config.AUDIO_PUMP_MAX_BURST_S.
        unyielded_s = 0.0
        try:
            while True:
                frame = await sess.next_audio_frame(c.AUDIO_SILENCE_TOPUP_S)
                if frame is None:
                    return                      # session over; the hold takes it from here
                if frame is AUDIO_TIMEOUT:
                    # No audio this interval. Keep the input alive with silence
                    # rather than letting the mux hold the VIDEO back waiting on
                    # it -- and it really does hold it back: the audio has to stay
                    # AHEAD of the video or ffmpeg stops reading the video pipe
                    # entirely (see config.AUDIO_SILENCE_TOPUP_S for the
                    # measurements). The sample count is derived from the
                    # wallclock inside the sink, so a top-up cannot over-fill,
                    # and what its per-call ceiling clips is still owed and is
                    # repaid by the next call -- which is why this loop's own
                    # cadence has to stay under the ceiling as well as under the
                    # lead.
                    unyielded_s = 0.0           # this branch already waited
                    if sink is not None and sink.audio_enabled \
                            and not await sink.write_silence_to():
                        _note_audio_dropped(sink)
                        if sink.audio_dropped:
                            return              # video-only from here; nothing to repair
                        audio_rebuild_needed = True
                        return
                    continue
                # Counted BEFORE the write: this is "a frame was decoded and
                # reached the publisher's pump", which is the fact
                # /control/status could not report before -- a sink that then
                # refuses the frame is a separate, already-visible failure
                # (the rebuild below).
                decoded_audio_frames_total += 1
                observed = ffmpeg_sink.audio_format_of(frame)
                if observed != known_audio_fmt:
                    # Purely an observation, exactly like reading (w, h) off a
                    # video frame -- the GUARD that stops a mismatched frame
                    # corrupting the pipe lives in sink.write_audio.
                    known_audio_fmt = observed
                    _update_status(audio_format=_audio_format_str(observed))
                _update_status(decoded_audio_frames=decoded_audio_frames_total)
                if sink is None or not await sink.write_audio(frame):
                    _note_audio_dropped(sink)
                    if sink is not None and sink.audio_dropped:
                        return                  # video-only from here; nothing to repair
                    audio_rebuild_needed = True
                    return
                # Yield, do NOT pace. A backlog has to reach ffmpeg as fast as
                # this loop can move it -- audio that lags the video is exactly
                # what stalls the video pipe -- but it must not hold the loop (or
                # the shared executor) for the whole drain while it does.
                sample_rate = (sink.audio_format or (None, 48000))[1] or 48000
                unyielded_s += float(frame.samples) / float(sample_rate)
                if unyielded_s >= c.AUDIO_PUMP_MAX_BURST_S:
                    unyielded_s = 0.0
                    await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("%s: audio pump failed; publishing video only until the "
                          "next session", camera["id"])

    async def _stop_audio_pump():
        # asyncio.wait rather than `await task`: this is itself called from inside
        # the hold task, so a plain await would make the pump's CancelledError
        # indistinguishable from the hold task's OWN cancellation and the hold
        # would swallow its cancellation and keep running. wait() never re-raises
        # the inner task's outcome; task.exception() is read purely so nothing is
        # left unretrieved for asyncio to complain about at GC time.
        nonlocal audio_task
        task, audio_task = audio_task, None
        if task is None:
            return
        task.cancel()
        await asyncio.wait({task})
        if not task.cancelled() and task.exception() is not None:
            log.warning("%s: audio pump ended with %r", camera["id"], task.exception())

    async def _hold_frames():
        # Repeat the most recent real frame at the camera's fps while the Janus
        # session is being rotated/reconnected, so the RTSP publish never goes
        # silent -- the repeated frames keep the encoder's output moving, and
        # they keep its timeline moving at real time, since
        # -use_wallclock_as_timestamps is in effect on every publisher this
        # bridge launches (see ffmpeg_sink.py's comment on the video input's
        # absent `-r`). Bounded by
        # time-since-last-real-frame, not per-invocation, so a genuinely
        # offline camera (e.g. the 2026-07-17 doorbell outage) stops
        # masquerading as live after FRAME_HOLD_MAX_S and fails honestly.
        #
        # The previous session's audio pump is stopped HERE, as this task's very
        # first act, rather than by the (synchronous) _start_hold: it makes the
        # two writers to the audio pipe mutually exclusive by construction --
        # nothing can be mid-write when the silence below starts, and the next
        # session's pump is not started until _stop_hold has finished.
        await _stop_audio_pump()
        # The rate the RUNNING ffmpeg was DECLARED with, not the most recent
        # measurement. Those two are deliberately different for a while whenever
        # a rate change has been confirmed and is waiting for the next rotation
        # to be applied (see RateWatcher), and during that window the repeated
        # frames still have to arrive at the pace the current publisher expects
        # -- it is `-framerate` on the live command line that defines what one
        # repeated frame means, and nothing else. known_fps is only the
        # bootstrap for the first hold of a camera, before any sink exists.
        interval = 1.0 / ffmpeg_sink.clamp_fps(sink.fps or known_fps or 15)
        # The two writers tick at DIFFERENT rates, and they have to. Video is
        # repeated at the camera's own frame interval, because that is the rate
        # ffmpeg was told to expect (-framerate) and writing faster would
        # misdescribe the stream. The audio top-up has its own deadline that has
        # nothing to do with the camera's rate: the audio must stay ahead of the
        # video or ffmpeg stops reading the video pipe (see
        # config.AUDIO_SILENCE_TOPUP_S). Ticking both at 1/fps is fine at 15fps
        # (67 ms) and quietly wrong at 6fps (167 ms, well past the lead) -- so the
        # loop ticks at whichever is shorter and writes video on its own schedule.
        #
        # That min() is what makes the audio cadence SAFE AT ANY RATE, which is
        # what an adaptive rate needs from it: clamp_fps bounds the interval at
        # 1/30 = 33 ms and AUDIO_SILENCE_TOPUP_S is 50 ms, so the tick can never
        # exceed 50 ms -- half of ffmpeg_sink._AUDIO_PRIME_MS (100 ms), the
        # muxer lead the whole pacing fix depends on -- no matter what rate is
        # measured. A rate change moves the VIDEO half of this loop and cannot
        # move the audio half past that ceiling.
        tick = min(interval, c.AUDIO_SILENCE_TOPUP_S) if sink.audio_enabled else interval
        next_video_at = time.monotonic()
        while time.monotonic() - last_live < c.FRAME_HOLD_MAX_S:
            now = time.monotonic()
            if now >= next_video_at:
                if not await sink.write_frame(last_frame):
                    # ffmpeg died, or stopped reading its input; either way the
                    # next successful session start replaces it, and the repair
                    # check up there reads publisher_alive to know it must.
                    _update_status(publisher_alive=sink.publisher_alive)
                    return
                next_video_at = max(next_video_at + interval, now)
                _update_status(last_publish_at=time.time())
            # The audio analogue of the repeated frame, and it cannot be built the
            # same way. A repeated video frame is self-describing: whenever it
            # lands, it is one frame, so asyncio.sleep jitter is invisible. A
            # sample stream's duration IS its length, so filling a gap by writing
            # "one interval's worth of samples per loop" accumulates every bit of
            # that jitter as a permanent, growing A/V offset -- and on a single-
            # output RTSP mux there is no separate audio timeline to absorb it.
            # write_silence_to derives the count from elapsed real time instead,
            # so the gap is filled with exactly as many samples as the wall clock
            # says passed, however irregularly this loop actually ran.
            if sink.audio_enabled and not await sink.write_silence_to():
                _note_audio_dropped(sink)
                if not sink.audio_dropped:
                    return
                # Audio is gone, video is not: keep holding the frame. Ending the
                # hold here would black the RTSP path for the rest of the
                # reconnect over an input that has already been given up on.
                tick = interval
            await asyncio.sleep(tick)
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
            except Exception:
                # The hold task belongs to the PREVIOUS session cycle, but this await
                # happens inside the next one -- re-raising here would fail a fresh,
                # otherwise-healthy session for something that went wrong before it
                # started, and (being raised before the sink-repair block below) would
                # do so on every subsequent cycle too.
                log.exception("%s: frame-hold task failed; continuing with this session",
                              camera["id"])
            hold_task = None
        # Also here, not only inside the hold task: _start_hold declines to start
        # at all when there is no sink or no frame yet, so this is the one place
        # guaranteed to run before the next session's pump is created. Idempotent.
        await _stop_audio_pump()

    try:
        while True:
            session = None
            try:
                vs = await get_video_source(telus, camera["id"])
                # known_fps is passed so the session's decoded-frame queue can be
                # sized in TIME rather than in a flat frame count -- 4 frames is
                # 267 ms at 15fps and 667 ms at 6fps, and one number has to mean
                # one thing on every camera (janus_client.video_queue_maxsize).
                # None on the very first session of a camera, which is exactly
                # when the 15fps fallback below applies too.
                session = JanusCameraSession(camera, vs, consume_audio=consume_audio,
                                             fps=known_fps)
                await asyncio.wait_for(session.connect_and_watch(), timeout=c.WEBRTCUP_TIMEOUT_S + 10)

                # Collect frames for up to ~1s (or 10 frames) to learn actual size/fps --
                # observed to vary per camera model (e.g. 1440x1440@15fps vs 1280x720@30fps).
                # Each frame fetch is individually timed out -- if the mountpoint negotiates
                # (webrtcup fires) but zero RTP ever actually arrives, we must not just hang
                # here; that's exactly the "camera registered but not streaming" case.
                first_frames = []
                frame_iter = session.frames()
                first_frame_wait_began = time.monotonic()
                deadline = first_frame_wait_began + c.FIRST_FRAME_TIMEOUT_S
                # Sampled BEFORE the wait, not after: this is the state the wait
                # started from. See the failure message below for why it matters.
                webrtcup_before_wait = getattr(session, "webrtcup_seen", None)
                while len(first_frames) < 10 and time.monotonic() < deadline:
                    remaining = max(0.1, deadline - time.monotonic())
                    try:
                        frame = await asyncio.wait_for(frame_iter.__anext__(), timeout=remaining)
                    except (asyncio.TimeoutError, StopAsyncIteration):
                        break
                    first_frames.append((time.monotonic(), frame))
                if not first_frames:
                    # Purely descriptive -- the control flow, the timeout and the
                    # raise are unchanged. The bare "no frames received before
                    # timeout" this replaced could not distinguish the two
                    # failures that actually look like this in the log:
                    #   * the full FIRST_FRAME_TIMEOUT_S elapsed with nothing
                    #     arriving (a camera that negotiated and then sent no
                    #     RTP -- what a genuinely slow camera looks like), versus
                    #     a much shorter abort (the frame iterator raising
                    #     StopAsyncIteration because the session was already
                    #     dead, which exits this loop immediately);
                    #   * ICE never coming up at all versus ICE coming up and no
                    #     media flowing -- opposite diagnoses that shared one
                    #     message. webrtcup_seen answers that directly.
                    waited = time.monotonic() - first_frame_wait_began
                    webrtcup = ("unknown" if webrtcup_before_wait is None
                                else "seen" if webrtcup_before_wait else "NOT seen")
                    raise RuntimeError(
                        f"no frames received before timeout "
                        f"(waited {waited:.1f}s of {float(c.FIRST_FRAME_TIMEOUT_S):.1f}s; "
                        f"webrtcup {webrtcup} before the wait)")

                w, h = first_frames[0][1].width, first_frames[0][1].height
                # On a rotation, buffered frames can arrive in a near-instant burst,
                # making elapsed-time-based fps math produce nonsense (observed: 544fps,
                # 215fps for real 15fps/30fps cameras -- and, less obviously, plausible-
                # looking but still-wrong values like 38-49fps for a real 15fps camera,
                # seen live 2026-07-18 during a rapid activate/deactivate crash loop).
                # A bare 1-60 range only catches the extreme cases; once known_fps is
                # set, require the new measurement to land within 50% of it too -- a bad
                # burst-artifact reading would otherwise silently overwrite a correct
                # known_fps and mismatch what ffmpeg was declared with for every rotation
                # after. ffmpeg_sink.py puts that same value on `-framerate` (the OUTPUT's
                # constant frame rate) and ties `-g` to it for a ~1s GOP, so a wrong
                # reading both re-rates the published stream and skews the keyframe
                # interval.
                #
                # That +/-50% tolerance is also why THIS batch cannot be the thing that
                # notices a real setting change. The rate is a device setting and does
                # change (E64): 15 -> 7 is a 53% drop and would be rejected here, and
                # loosening the tolerance would put burst artifacts straight back into
                # known_fps. So the two measurements have different jobs and different
                # rules. This one BOOTSTRAPS a rate for a camera that has none, and
                # refuses anything far from what is already known. The settled
                # re-measurement in the streaming loop below (RateWatcher) is the one
                # allowed to move a known rate a long way, and it earns that by measuring
                # far more frames, well clear of the burst, and by requiring two
                # independent batches to agree before anything is rebuilt.
                fps = known_fps or 15
                if len(first_frames) >= 2:
                    # Median inter-frame delta, not total-span/(count-1) -- the total-span
                    # form takes its single worst-case hit from just the first and last
                    # timestamps in the batch, so if EITHER happens to be a buffered/burst
                    # frame (arriving back-to-back with near-zero gap) the whole estimate is
                    # contaminated even though most of the other 8+ frames arrived at the
                    # camera's real pace. Confirmed live 2026-07-18: this was still producing
                    # a bogus-but-1-60-plausible ~30-39fps reading for the doorbell's real
                    # ~15fps on ordinary (non-crash-loop) cold starts, which then got locked in
                    # by the known_fps tolerance check below for the rest of that process's
                    # lifetime -- there was no history yet to compare the bad *first* reading
                    # against. The median of all consecutive deltas only gets fooled if a
                    # MAJORITY of the batch is burst-contaminated, not just one endpoint.
                    median_delta = _median([
                        first_frames[i][0] - first_frames[i - 1][0]
                        for i in range(1, len(first_frames))
                    ])
                    measured = round(1 / median_delta) if median_delta > 0 else None
                    plausible = (
                        measured is not None
                        and _RATE_PLAUSIBLE_MIN <= measured <= _RATE_PLAUSIBLE_MAX
                        and (known_fps is None or 0.5 * known_fps <= measured <= 1.5 * known_fps)
                    )
                    if plausible:
                        # Only a measurement we actually trust becomes this camera's
                        # remembered fps. Promoting the 15fps *fallback* into known_fps
                        # (what the unconditional assignment used to do) permanently
                        # locked the tolerance check below to 7-22fps, so a real 30fps
                        # camera whose very first measurement happened to be burst-
                        # contaminated could never correct itself for the life of the
                        # process -- every later good reading was rejected as
                        # implausible against the bad baseline.
                        fps = known_fps = measured
                    elif known_fps is None:
                        log.warning("%s: implausible fps measurement %s, using %s for this "
                                    "session without locking it in as this camera's fps",
                                    camera["id"], measured, fps)

                # The hold must be stopped before touching the sink so nothing
                # else is writing while we inspect/replace it. The publisher is
                # only replaced if ffmpeg actually died or the camera's frame
                # size changed (never observed, but a mismatched raw feed would
                # corrupt the encode) -- otherwise the same RTSP session
                # carries straight on and readers never notice the rotation.
                await _stop_hold()
                # Size the audio input from a REAL decoded frame wherever possible.
                # The audio m-line comes first in the offer and its RTP starts with
                # the video's, so by the time ten video frames have been collected
                # a decoded audio frame has almost always already been queued --
                # this reads its shape without consuming it and without making the
                # video path wait on audio. AUDIO_DEFAULT_FORMAT is only the
                # bootstrap for the case where none has arrived yet; if it turns
                # out to be wrong, write_audio's guard forces a rebuild.
                if consume_audio:
                    observed_audio = getattr(session, "audio_format", None)
                    if observed_audio is not None and observed_audio != known_audio_fmt:
                        known_audio_fmt = observed_audio
                        # The peek is a real decoded frame, so it is the earliest
                        # honest answer /control/status can give -- and on a
                        # healthy camera it lands a rotation before the pump's
                        # first frame does.
                        _update_status(audio_format=_audio_format_str(observed_audio))
                desired_audio_fmt = (
                    (known_audio_fmt or c.AUDIO_DEFAULT_FORMAT) if consume_audio else None)
                audio_rebuild_needed = False
                # A confirmed frame-rate change is applied HERE, at a rotation
                # boundary, and nowhere else -- the frame hold is already bridging
                # this gap, so the rebuild costs one glitch rather than a glitch
                # plus a failed session plus a backoff. That is also why it is not
                # raised from the streaming loop the way an audio-format change is:
                # a rate change must never take the camera off the air.
                #
                # Re-checked against the CURRENT reading rather than trusted from
                # when it was confirmed: this session's own first batch may have
                # already moved known_fps back inside the band (a rate that
                # wandered and came home), and rebuilding for a rate that no longer
                # differs would be a glitch for nothing. Checking here is what makes
                # the mechanism converge.
                rate_changed = (
                    rate_rebuild_needed and sink is not None
                    and _rate_moved(sink.fps, ffmpeg_sink.clamp_fps(fps)))
                rate_rebuild_from = None
                if rate_rebuild_needed and not rate_changed and sink is not None:
                    # Only worth saying when there IS a publisher to have kept. With
                    # no sink at all one is about to be built at the new rate anyway,
                    # which is not "the change was dropped".
                    log.info("%s: the confirmed rate change is no longer a change "
                             "(declared %sfps, now measuring %sfps) -- keeping the "
                             "publisher", camera["id"], sink.fps,
                             ffmpeg_sink.clamp_fps(fps))
                rate_rebuild_needed = False
                _update_status(rate_pending=None)
                # `sink.proc is None` means a previous start() raised partway through
                # (both encoders failed to survive the startup grace period -- what an
                # RTSP ANNOUNCE to a momentarily-down MediaMTX looks like). Such a sink
                # must count as dead: treating it as healthy left every subsequent
                # write_frame raising AttributeError on None.stdin, which no path in
                # this loop could repair, so each otherwise-perfect session counted as
                # a failure until the os._exit escape hatch below restarted the whole
                # bridge -- long after the original blip had cleared.
                if sink is not None and (
                        sink.proc is None
                        or sink.proc.returncode is not None
                        # ...and the case neither of those covers: a process that
                        # is alive and has stopped publishing. `publisher_alive`
                        # folds in whether ffmpeg is still CONSUMING the video
                        # pipe, which is the only evidence available here that
                        # the publisher still exists (see FfmpegSink.write_frame).
                        # Without it the E62 outage is unrepairable from this
                        # loop: every session fails, every rebuild check says the
                        # sink is fine, and the status reports "streaming" into a
                        # path MediaMTX no longer has.
                        or not sink.publisher_alive
                        or sink_wh != (w, h)
                        # A changed raw AUDIO format is a rebuild for exactly the
                        # same reason a changed frame size is: ffmpeg was told the
                        # pipe's shape once, on the command line, and cannot be
                        # told again.
                        or sink_audio_fmt != desired_audio_fmt
                        # ...and a changed FRAME RATE, for the third time the same
                        # reason: `-framerate` and `-g` were set once, on the command
                        # line. The rate is a device setting that can change while
                        # this process runs, so this is the same class of event as a
                        # frame-size change -- on this platform it literally is one,
                        # since both come from the live-video resolution setting.
                        or rate_changed):
                    if rate_changed:
                        log.warning(
                            "%s: frame rate changed (declared %sfps, now measuring "
                            "%sfps) -- rebuilding the publisher at this rotation so "
                            "-framerate and -g describe the stream again. Readers will "
                            "see one reconnect; this is not a fault.",
                            camera["id"], sink.fps, ffmpeg_sink.clamp_fps(fps))
                        # Remembered so RateWatcher can recognise a confirmed
                        # return to this rate as flapping, not a second change.
                        rate_rebuild_from = sink.fps
                    dup_warnings_folded += sink.duplicated_frame_warnings
                    await sink.stop()
                    sink = None
                if sink is None:
                    # Adopt the new sink only once start() has actually succeeded --
                    # assigning first is what left the poisoned proc=None sink behind.
                    new_sink = FfmpegSink(camera["id"])
                    await new_sink.start(w, h, fps, audio_format=desired_audio_fmt)
                    sink, sink_wh = new_sink, (w, h)
                    if rate_changed:
                        # Start the minimum-interval clock only once a rebuild has
                        # actually happened, so a rebuild that never took place
                        # cannot suppress a later, genuine one.
                        rate.rebuilt(time.monotonic(), rate_rebuild_from)
                    # What this ffmpeg was LAUNCHED with -- the command line is
                    # what the rebuild check above is really about, and start()
                    # can legitimately come up video-only anyway (an unsupported
                    # sample format, a pipe that would not open). Recording what
                    # the sink ended up with instead would make every rotation
                    # see a mismatch and rebuild the publisher forever, which is
                    # the one thing this loop exists to avoid.
                    sink_audio_fmt = desired_audio_fmt
                    # A fresh publisher: the status describes what IS, read off
                    # the sink itself -- normally alive and (if it was given an
                    # audio input at all) carrying audio, but start() can come
                    # up with the audio already dropped (a pipe that would not
                    # open), and that must be as visible as a mid-session drop.
                    _update_status(publisher_alive=True,
                                   audio_dropped=sink.audio_dropped,
                                   audio_dropped_reason=sink.audio_dropped_reason)
                    # NOTE: /control/status's audio_format is deliberately NOT
                    # written here. What the sink was launched with is a guess
                    # (AUDIO_DEFAULT_FORMAT) until a frame has actually been
                    # decoded, and publishing that guess is what let eleven
                    # sessions of pure silence report "48000Hz/2ch/s16". The
                    # field is written only where a real frame is observed --
                    # the peek above and the audio pump.
                if consume_audio and sink.audio_enabled:
                    audio_task = asyncio.ensure_future(_pump_audio(session))

                for _, frame in first_frames:
                    last_frame = frame
                    last_live = time.monotonic()
                    if not await sink.write_frame(frame):
                        _update_status(publisher_alive=sink.publisher_alive)
                        raise RuntimeError("ffmpeg pipe broken, restarting sink")
                backoff = c.RECONNECT_BACKOFF_INITIAL_S  # reset after any successful frame batch
                consecutive_failures = 0
                # sink.fps (not the local fps var) so this always reflects what's
                # actually driving the encoder's -r/-g, not a pre-clamp/burst-noisy
                # measurement -- see FfmpegSink.start()'s own comment.
                #
                # The first-frame latency rides on THIS line rather than a line of
                # its own: it is measured on every session start, and a second
                # per-rotation INFO line saying half of what this one says is log
                # volume for nothing. It is the healthy-side counterpart of the
                # failure message above -- a camera that takes 12s to produce a
                # first frame against a 20s FIRST_FRAME_TIMEOUT_S is one blip away
                # from the failure loop, and nothing said so before.
                log.info("%s: streaming %dx%d@%dfps (first frame after %.1fs)",
                         camera["id"], w, h, sink.fps,
                         first_frames[0][0] - first_frame_wait_began)
                _update_status(state="streaming", consecutive_failures=0, fps=sink.fps,
                               hardware_encode=sink.hardware_encode, last_error=None,
                               last_frame_at=time.time(), last_publish_at=time.time(),
                               publisher_alive=True)

                # Alarm.com's own mountpoint timeout (see janus_client.py's "timeout_seconds":
                # 180 in the create body) actually kills the stream server-side at ~180s --
                # confirmed live. SESSION_MAX_LIFETIME_S must stay comfortably under that so
                # we always rotate first. Just as important: the deadline check below must
                # run even when NO frame ever arrives (camera stall, silent RTP death) --
                # checking it only after a frame lands is exactly what let the bridge hang
                # forever once frames stopped coming entirely.
                rotate_deadline = time.monotonic() + c.SESSION_MAX_LIFETIME_S
                # Nothing measured until this session has been delivering frames
                # for RATE_RESAMPLE_SETTLE_S -- the batch just consumed above is
                # exactly the unreliable one (see RateWatcher).
                rate.session_started(time.monotonic())
                frame_iter = session.frames()
                while True:
                    if audio_rebuild_needed:
                        # Set by the audio pump when the sink refused a frame --
                        # a changed PCM format, or a pipe ffmpeg has stopped
                        # reading. Raised from HERE rather than from the pump so
                        # the repair runs through the identical path a frame-size
                        # change takes: fail the session, close it, then let the
                        # sink-repair block above rebuild the publisher with the
                        # format learned from the real frames.
                        raise RuntimeError("audio input needs rebuilding, restarting sink")
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
                    _update_status(
                        last_frame_at=time.time(),
                        measured_fps=rate.last_measured,
                        duplicated_frame_warnings=(
                            dup_warnings_folded + sink.duplicated_frame_warnings))
                    # The rate under review, measured from live frames only. A
                    # confirmed change updates this camera's remembered rate
                    # immediately -- so the NEXT session's decoded-frame queue is
                    # sized from it (janus_client.video_queue_maxsize) and a sink
                    # rebuilt for any other reason already uses it -- and asks for
                    # the publisher to be rebuilt at the next rotation.
                    confirmed = rate.note_frame(last_live, sink.fps)
                    if confirmed is not None:
                        known_fps = confirmed
                        rate_rebuild_needed = True
                        _update_status(rate_pending=confirmed)
                    if not await sink.write_frame(frame):
                        # last_frame_at (a frame arrived from the camera) has
                        # already moved; last_publish_at (ffmpeg took it) has
                        # not. The gap between those two IS the difference
                        # between "streaming" and "streaming to nowhere".
                        _update_status(publisher_alive=sink.publisher_alive)
                        raise RuntimeError("ffmpeg pipe broken, restarting sink")
                    _update_status(last_publish_at=time.time())

            except Exception as exc:
                rate_limited = isinstance(exc, RateLimited)
                if not rate_limited:
                    # A Cloudflare rate-limit is an explicitly-transient server-side
                    # condition, not the leaked/wedged local resource the os._exit
                    # escape hatch below exists for -- and restarting the container is
                    # actively counterproductive there, since every restart re-attempts
                    # a login and re-triggers the same window (see main.py's
                    # _login_with_retry). Don't let it count toward that threshold.
                    consecutive_failures += 1
                _update_status(state="reconnecting", consecutive_failures=consecutive_failures,
                               last_error=repr(exc))
                # For a RateLimited failure the actual sleep is the server's own
                # Retry-After (logged with the cause and delay just below), not
                # this loop's fixed backoff -- claiming "reconnecting in 2s" and
                # then sleeping 100+s put two contradictory delays in the log
                # for one event. Message text only; the handling is unchanged.
                log.exception("%s: camera session failed%s", camera["id"],
                              "" if rate_limited else f", reconnecting in {backoff}s")
                if consecutive_failures >= c.MAX_CONSECUTIVE_SESSION_FAILURES:
                    # This loop's own backoff already absorbs ordinary transient
                    # failures (auth blips, brief camera dropouts, etc). Reaching
                    # this many *consecutive* failures for one camera with no
                    # successful frame batch in between means something below
                    # this loop isn't being fully released between attempts --
                    # lifecycle.py's deactivate() already documents aiortc's
                    # pc.close() as "a known-hangable call under certain ICE
                    # states" that can leak resources when abandoned. Observed
                    # live 2026-07-20: 26+ consecutive "no frames received"
                    # failures across both cameras that never self-cleared,
                    # needing a manual `docker restart` -- retries kept cycling
                    # but never succeeding, consistent with a leaked/wedged
                    # resource rather than a transient/retryable one.
                    #
                    # os._exit() (not sys.exit/raise) is deliberate: a graceful
                    # shutdown would run through the very cleanup path already
                    # suspected of being wedged, which is exactly what we can't
                    # rely on working here. Hard-exiting the whole process lets
                    # the OS reclaim every fd/socket/child unconditionally, and
                    # Docker's `restart: unless-stopped` policy (see main.py's
                    # _login_with_retry comment for the same pattern) brings up
                    # a fully clean process.
                    log.critical(
                        "%s: %d consecutive session failures with no successful "
                        "frame in between -- likely a leaked/wedged resource that "
                        "only a fresh process can clear; forcing container restart",
                        camera["id"], consecutive_failures)
                    # ...unless we've already done exactly that several times in
                    # the last hour. A wedged resource is gone after the first
                    # restart; a hatch that keeps firing means the failures are
                    # coming from *outside* this process (alarm.com down,
                    # Cloudflare rate-limiting the login every restart re-runs),
                    # where restarting immediately only re-hammers the upstream --
                    # 89 restarts in 7 days, one 14-minute tight loop. The guard
                    # holds this process up first (see restart_guard.py); the
                    # container physically cannot restart while we're sleeping,
                    # and the other cameras plus the control endpoint keep serving
                    # meanwhile. It never *cancels* the exit -- a real wedge still
                    # gets its fresh process, just not at loop speed.
                    hold_s, exits_in_window = restart_guard.record_exit()
                    if hold_s:
                        log.critical(
                            "%s: that's %d hatch-triggered exits within the last %ds -- "
                            "treating this as a sustained upstream failure rather than a "
                            "local wedge, and holding the process for %.0fs before exiting "
                            "so the container can't restart-loop into it",
                            camera["id"], exits_in_window, c.RESTART_GUARD_WINDOW_S, hold_s)
                        _update_status(state="restart_deferred",
                                       consecutive_failures=consecutive_failures)
                        # Close before the hold for the same reason the backoff
                        # sleep below closes first: left running, the dead
                        # session's _pump_track keeps decoding RTP nobody drains
                        # for the entire sleep -- and this sleep is minutes, not
                        # seconds. Bounded by SESSION_CLOSE_TIMEOUT_S, so even a
                        # hung close only costs 5s of the wait.
                        if session:
                            await _retire_session(session)
                            session = None
                        await asyncio.sleep(hold_s)
                    os._exit(1)
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
                    await _retire_session(session)
                    session = None
                if rate_limited:
                    # Honor Cloudflare's own Retry-After instead of the fixed
                    # 2/4/8/16/30s schedule. main.py's startup path already learned
                    # this the hard way -- the short schedule just keeps re-entering
                    # the same rate-limit window rather than ever clearing it -- but
                    # that handling only ever covered the startup login, so a
                    # RateLimited raised by a mid-run re-login (the far more common
                    # case, since the session expires every few hours) landed here and
                    # got the exact backoff that compounds the problem.
                    # The full server-stated wait, not a locally truncated one:
                    # this used to min() against a 300s cap, so a longer window
                    # had us knocking again before Cloudflare said we could,
                    # which restarts the window instead of clearing it. Only
                    # RATE_LIMIT_RETRY_MAX_S (30min) clamps it now, as a guard
                    # against an absurd header value.
                    delay = c.rate_limit_sleep_s(exc.retry_after_s)
                    log.warning("%s: rate-limited by Cloudflare, sleeping %.0fs "
                                "(server-specified Retry-After=%.0fs, not the fixed backoff)",
                                camera["id"], delay, exc.retry_after_s)
                    await asyncio.sleep(delay)
                else:
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, c.RECONNECT_BACKOFF_MAX_S)
            else:
                # Clean proactive rotation: bridge the reconnect gap.
                _start_hold()
            finally:
                if session:
                    await _retire_session(session)
    finally:
        await _stop_hold()          # also stops the audio pump
        if sink:
            await sink.stop()
