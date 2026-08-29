"""One structured record per session handover, so the rotation gap can be read
from data instead of guessed at (backlog E76).

OFF unless config.HANDOVER_TRACE. When it is off `begin()` returns None, every
call site is guarded by an `is not None` test on that result, and nothing here
is constructed, called or allocated -- the entire cost on the frame path is one
identity comparison per frame.

WHAT IT IS FOR. With audio consuming, the doorbell loses about 14 frames per
rotation against 0.8 with audio off (same camera, same rate, same build), and
every drop falls between the `proactive session rotation` line and the new
session's first frame. The SUSPECTED mechanism -- unconfirmed, and this module
exists precisely so that it does not have to be assumed -- is that the audio
timeline stops advancing while the two writers to the audio pipe are swapped:
the frame hold stops the previous session's audio pump as its first act, and the
next session's pump is not started until the hold has finished. If nothing
advances the audio timeline inside that window, the muxer lead
(ffmpeg_sink._AUDIO_PRIME_MS, 100 ms) decays, ffmpeg stops reading the video
pipe, and the video queue's ~500 ms of tolerance overflows.

So one record answers three questions about one rotation:

  * did the audio timeline stop advancing, where, and for how long -- `worst_gap`,
    and the `dt`/`adv` pair on every mark it is derived from ("wall clock since
    the previous mark" against "audio media written since the previous mark");
  * was the muxer lead gone while it did -- `lead` on every mark, `lead_min_ms`
    and `lead_min_at`;
  * did frames drop, and exactly when relative to both -- `drops`.

DERIVED, NOT RE-COUNTED. The lead and the timeline come from
FfmpegSink.audio_lead_s / audio_timeline_s, which are read off the sample
counter write_silence_to already maintains; the queue depth is the real queue's
own qsize; the drop times are recorded where the drop happens. Nothing here
keeps a parallel tally that could disagree with the thing it describes. The
frame hold's per-tick counters are held to the same rule: what a top-up wrote is
read off that same sample counter, never inferred from the call having been made
or from it returning True (see HandoverTrace.__init__).

THE LEAD IS A FLOOR, NOT A CEILING, and a record showing seconds of it is
correct rather than broken. ffmpeg_sink._AUDIO_PRIME_MS (100 ms) is the lead
established when the pipe is OPENED; after that only two things move the audio
timeline, and neither caps it. write_silence_to tops up TO the wall clock, so it
can raise the lead to zero and no further. write_audio has no wallclock term at
all -- it writes whatever a decoded frame carries, whenever it arrives -- so a
queued backlog drained by the audio pump (the session's queue holds up to
config.AUDIO_QUEUE_MAXSIZE frames, ~1 s, and the pump drains it as fast as the
event loop allows) legitimately leaves the timeline SECONDS ahead of real time.
A `lead` of +1.9 s on the `rotation` mark is that, and it is also why the frame
hold's top-ups can be perfectly regular and still write nothing for the first
second or more of a gap.

WHAT IT CANNOT SEE, stated rather than papered over:

  * The lead is what the BRIDGE has handed ffmpeg, not what ffmpeg's muxer has
    consumed. It is the input side of the mechanism; the muxer's own state is
    not observable from this process (see backlog E68 for the same limit on the
    pacing measurements).
  * `dt` is the wall clock between two marks, so a stall is located between the
    two events that bracket it, never to finer resolution than that.
  * Marks are taken where the loop already is. A mark is not a probe: nothing
    here schedules a sample of its own, because a sampler that runs on the event
    loop during the very window being measured would perturb it.

WHY THE LOG AND NOT /control/status. A handover is an EVENT, and
/control/status is a snapshot contract that Home Assistant polls continuously
and derives entities from. A record served there would be reported an arbitrary
number of times or not at all depending on how the poll cadence lines up with
the 165 s rotation, and it would put a debug-only blob (null on almost every
poll) into a contract other code is written against. The log has neither problem:
one line per rotation, in order, next to the `proactive session rotation` and
`dropped N frame(s)` lines it has to be read beside.

THE FORMAT is the one this project already uses for structured output: a plain
JSON object, snake_case keys, units in the name (`_ms`), floats rounded at the
edge -- the same shape metrics.py hands the control API, because a second
convention is a second thing to learn and to get wrong. It is emitted as one
log line rather than served, for the reason above.

Reading one: `docker logs telus-cam-bridge | grep 'handover trace' | sed
's/.*handover trace //' | jq .` -- everything after the prefix is a single JSON
object. See docs/troubleshooting.md for the field-by-field walkthrough.
"""
import json
import logging
import time

from . import config as c

log = logging.getLogger(__name__)

# How many drop timestamps one record prints before it stops listing them. A
# traced rotation drops ~14; printing thousands from a session in real trouble
# would be exactly the log flooding this flag exists to avoid. The COUNT is
# always exact -- only the list is cut.
_MAX_DROP_TIMES = 40


def begin(camera_id, sink=None, session=None):
    """Start a record for the handover about to happen, or None when tracing is
    off.

    The knob is read off the module attribute at each call rather than captured
    at import, like every other knob in config.py, so the setting in force is
    the one the process currently holds.
    """
    if not c.HANDOVER_TRACE:
        return None
    return HandoverTrace(camera_id, sink, session)


def _ms(seconds):
    """Seconds to milliseconds at 0.1 ms resolution, passing None through."""
    if seconds is None:
        return None
    return round(float(seconds) * 1000.0, 1)


def _read(obj, name):
    """One attribute of a possibly-absent object, or None.

    getattr rather than a direct read because every source here is optional at
    some point in a handover: there is no new session yet when the record opens,
    no sink at all on a camera's first cycle, and a sink can be rebuilt
    mid-window. A diagnostic that raised in any of those cases would fail a
    healthy session from inside the loop's own error handling.
    """
    if obj is None:
        return None
    try:
        return getattr(obj, name, None)
    except Exception:
        return None


class HandoverTrace:
    """The record for one rotation, accumulated in place and emitted once.

    Every public method is TOTAL: it is called from the reconnect loop, the
    frame hold and the audio pump, and an exception raised by a diagnostic in
    any of those places would fail an otherwise-healthy session and count toward
    camera_session.py's consecutive-failure escape hatch. The same rule
    janus_client._sdp_media_summary already follows for the same reason.
    """

    def __init__(self, camera_id, sink=None, session=None):
        self.camera_id = str(camera_id)
        self.started = time.monotonic()
        # Wallclock too, and only here: the relative timings below are monotonic
        # (immune to a clock step), but the record still has to be findable
        # against the timestamped lines around it in the container log.
        self.started_wall = time.time()
        # What the retiring session dropped, SNAPSHOTTED when it is closed (see
        # session_retired) rather than held as a reference. A JanusCameraSession
        # owns its decoded-frame queue -- up to 8 frames of 3.11 MB on the
        # doorbell -- and a diagnostic that keeps a retired one alive for the
        # rest of the window would be spending 25 MB per camera to watch memory
        # behaviour it is itself changing.
        self._retired_drops: list[float] = []
        self._retired_total = None
        self._retired_id = None
        self._retired_seen = False
        self.sink_rebuilt = False
        self.marks: list[dict] = []
        self._mark_times: list[float] = []
        self._first_timeline = None
        self._last_timeline = None
        self._lead_min = None
        self._lead_min_at = None
        self._settle_at = None
        self._closed = False
        # Frame-hold aggregates. The hold ticks up to every 50 ms for the whole
        # gap, which is hundreds of events: one mark each would bury the record,
        # so the hold contributes its FIRST write as a mark and the rest as this
        # summary.
        #
        # A CALL AND A WRITE ARE DIFFERENT EVENTS and the record keeps them
        # apart, because conflating them is what made this instrument lie
        # (E88). write_silence_to tops the audio timeline up TO the wall clock,
        # so a top-up made while the timeline is still ahead of it writes
        # nothing at all and returns True. That is the ordinary state for the
        # first second or more of a hold -- a decoded backlog drained by the
        # previous session's pump can leave the timeline seconds ahead (see
        # `lead` on the `rotation` mark) -- so a record read "38 silence
        # writes" beside "adv: 0.0" for the same interval and looked like two
        # halves of one instrument disagreeing. They were not: `adv` was right
        # and the count was wrong.
        self._hold_silence_calls = 0    # top-ups that were accepted
        self._hold_silence = 0          # ...of those, the ones that wrote samples
        self._hold_silence_media = 0.0  # seconds of audio the hold really added
        self._hold_timeline = None      # the sink's timeline at the previous call
        self._hold_video = 0            # repeated frames ffmpeg ACCEPTED
        self._hold_video_issued = 0     # ...of those, how many were started
        self._hold_first_at = None
        self._hold_last_at = None
        self._hold_max_gap = 0.0
        # When the new session's first video frame ARRIVED, as opposed to when
        # the loop noticed. Carried as a bare timestamp rather than a mark
        # because it is read after the fact -- there is no honest lead or queue
        # sample to attach to an instant that has already passed, and inventing
        # one by sampling now and labelling it then is exactly how a diagnostic
        # starts lying.
        self.first_frame_at = None
        self.mark("rotation", sink, session)

    # ---- accumulation ---------------------------------------------------
    def mark(self, label, sink=None, session=None):
        """Record one named point in the window, with the lead, the audio
        timeline's advance since the previous mark, and the video queue depth as
        they are at this instant.

        `dt` (wall clock since the previous mark) against `adv` (audio media
        written since the previous mark) is the whole measurement: equal means
        the audio kept up, `adv` of zero across a long `dt` is the timeline
        having stopped.
        """
        if self._closed:
            return
        try:
            now = time.monotonic()
            timeline = _read(sink, "audio_timeline_s")
            lead = _read(sink, "audio_lead_s")
            previous_at = self._mark_times[-1] if self._mark_times else self.started
            advance = (timeline - self._last_timeline
                       if timeline is not None and self._last_timeline is not None
                       else None)
            self.marks.append({
                "m": str(label),
                "t": _ms(now - self.started),
                "dt": _ms(now - previous_at),
                "adv": _ms(advance),
                "lead": _ms(lead),
                "q": _read(session, "video_queue_depth"),
            })
            self._mark_times.append(now)
            if timeline is not None:
                if self._first_timeline is None:
                    self._first_timeline = timeline
                self._last_timeline = timeline
            self._note_lead(lead, label)
        except Exception:
            log.debug("%s: handover mark %r failed", self.camera_id, label,
                      exc_info=True)

    def hold_silence(self, sink=None, wrote=True):
        """One pass of the frame hold's audio top-up.

        The hold is the only writer to the audio pipe for the length of the gap,
        so its cadence IS the audio timeline's cadence there -- that is what
        `max_gap_ms` in the emitted record is, and why the first of these calls
        is promoted to a mark of its own.

        What the call PUT ON THE PIPE is a separate question from whether it was
        made, and it is answered the way everything here is answered: read off
        the sink's own sample counter, never inferred from the call's return
        value. See __init__ for what inferring it cost.
        """
        if self._closed:
            return
        try:
            now = time.monotonic()
            timeline = _read(sink, "audio_timeline_s")
            # Before the first of these the last reading is whatever the
            # previous mark took -- `pump_stopped`, an instant earlier -- so the
            # hold's very first write is measured like every other one.
            previous = self._hold_timeline
            if previous is None:
                previous = self._last_timeline
            if timeline is not None and previous is not None and timeline > previous:
                self._hold_silence += 1
                self._hold_silence_media += timeline - previous
            if timeline is not None:
                self._hold_timeline = timeline
            if wrote:
                self._hold_silence_calls += 1
            if self._hold_first_at is None:
                self._hold_first_at = now
                self.mark("hold_first_silence", sink)
            elif self._hold_last_at is not None:
                gap = now - self._hold_last_at
                if gap > self._hold_max_gap:
                    self._hold_max_gap = gap
            self._hold_last_at = now
            self._note_lead(_read(sink, "audio_lead_s"), "hold")
        except Exception:
            log.debug("%s: handover hold sample failed", self.camera_id,
                      exc_info=True)

    def hold_video_issued(self):
        """One repeated video frame handed to the publisher by the frame hold.

        Counted where the write STARTS, against hold_video's count of the ones
        that finished. The two differing is a video pipe ffmpeg has stopped
        reading, seen from inside the gap -- the shape of E88 -- and a record
        that carried only the second could not distinguish it from a hold that
        was never scheduled at all.
        """
        if not self._closed:
            self._hold_video_issued += 1

    def hold_video(self):
        """One repeated video frame the publisher ACCEPTED."""
        if not self._closed:
            self._hold_video += 1

    def session_retired(self, session=None, sink=None):
        """The rotated-away session has been closed.

        Takes the only two things that die with it -- when it dropped frames,
        and how many it dropped in total -- and keeps NO reference to the
        session itself. Only the first call counts: a later failure retires a
        different session, and the record is about this handover's.
        """
        if self._closed or self._retired_seen:
            return
        try:
            self._retired_seen = True
            self._retired_id = id(session) if session is not None else None
            self._retired_drops = [t for t in (_read(session, "drop_times") or ())
                                   if t >= self.started]
            self._retired_total = _read(session, "dropped_frames")
            self.mark("session_retired", sink)
        except Exception:
            log.debug("%s: handover retire sample failed", self.camera_id,
                      exc_info=True)

    def note_first_frame(self, at):
        """The monotonic time the new session's first video frame arrived."""
        if self._closed or self.first_frame_at is not None or at is None:
            return
        self.first_frame_at = at

    def arm(self, settle_s=None):
        """The new session is publishing; close the record `settle_s` later.

        A tail, not a delay: the drops being measured land around the first
        writes of the new session, so a record closed at the batch itself would
        report the window without its own consequence.
        """
        if self._closed:
            return
        if settle_s is None:
            settle_s = c.HANDOVER_TRACE_SETTLE_S
        self._settle_at = time.monotonic() + float(settle_s)

    def due(self, now=None) -> bool:
        """Has the settle tail elapsed? False until arm() has been called."""
        if self._closed or self._settle_at is None:
            return False
        return (time.monotonic() if now is None else now) >= self._settle_at

    def _note_lead(self, lead, label):
        if lead is None:
            return
        if self._lead_min is None or lead < self._lead_min:
            self._lead_min = lead
            self._lead_min_at = str(label)

    # ---- emission -------------------------------------------------------
    def emit(self, sink=None, session=None, reason="settled"):
        """Close the record and write it to the log. Idempotent."""
        if self._closed:
            return
        self.mark(reason, sink, session)
        self._closed = True
        try:
            log.info("%s: handover trace %s", self.camera_id,
                     json.dumps(self.record(sink, session, reason),
                                separators=(",", ":")))
        except Exception:
            log.debug("%s: handover record could not be emitted",
                      self.camera_id, exc_info=True)

    def record(self, sink=None, session=None, reason="settled") -> dict:
        """The record as a plain dict -- what emit() serialises, and what a test
        reads instead of re-parsing a log line."""
        span = (self._mark_times[-1] - self.started) if self._mark_times else 0.0
        audio_ms = (_ms(self._last_timeline - self._first_timeline)
                    if self._first_timeline is not None else None)
        return {
            "camera": self.camera_id,
            "at": round(self.started_wall, 3),
            "reason": str(reason),
            "span_ms": _ms(span),
            "fps": _read(sink, "fps"),
            "audio": bool(_read(sink, "audio_enabled")),
            "audio_dropped": bool(_read(sink, "audio_dropped")),
            # How much media the audio timeline gained over the whole window,
            # against span_ms. Two numbers that should match, and the headline
            # of the whole record when they do not.
            "audio_ms": audio_ms,
            "first_frame_ms": (_ms(self.first_frame_at - self.started)
                               if self.first_frame_at is not None else None),
            "lead_min_ms": _ms(self._lead_min),
            "lead_min_at": self._lead_min_at,
            "worst_gap": self._worst_gap(),
            "sink_rebuilt": bool(self.sink_rebuilt),
            "queue_limit": _read(session, "video_queue_limit"),
            "hold": {
                # The cadence (calls) and the content (writes, and what they
                # carried) are separate numbers because they answer separate
                # questions and are routinely different: silence_calls high with
                # silence_ms at 0 is a hold running normally into a timeline
                # that is already ahead of the wall clock, which is healthy;
                # silence_calls collapsing to 2 is the hold itself stopping.
                "silence_calls": self._hold_silence_calls,
                "silence_writes": self._hold_silence,
                "silence_ms": _ms(self._hold_silence_media),
                "video_issued": self._hold_video_issued,
                "video_writes": self._hold_video,
                "first_ms": (_ms(self._hold_first_at - self.started)
                             if self._hold_first_at is not None else None),
                "last_ms": (_ms(self._hold_last_at - self.started)
                            if self._hold_last_at is not None else None),
                # The cadence's own worst case, so it is gated on CALLS: a hold
                # ticking perfectly into an audio timeline that needs nothing
                # has no writes to space out and still has a cadence to report.
                "max_gap_ms": (_ms(self._hold_max_gap)
                               if self._hold_silence_calls else None),
            },
            "drops": self._drops(session),
            "marks": self.marks,
        }

    def _worst_gap(self):
        """The interval where the audio timeline fell furthest behind the wall
        clock, as {from, to, ms, dt, adv}.

        `ms` is dt minus adv: milliseconds of real time that interval did NOT
        cover with audio. On a healthy window it is a few ms of scheduling
        jitter; if the suspected mechanism is real it is most of the
        serialization window, sitting between the two marks that name it.
        """
        worst = None
        for previous, entry in zip(self.marks, self.marks[1:]):
            if entry["adv"] is None or entry["dt"] is None:
                continue
            deficit = round(entry["dt"] - entry["adv"], 1)
            if worst is None or deficit > worst["ms"]:
                worst = {"from": previous["m"], "to": entry["m"], "ms": deficit,
                         "dt": entry["dt"], "adv": entry["adv"]}
        return worst

    def _drops(self, session=None):
        """When frames were dropped in this window, in ms from the rotation.

        Both sides of the handover, kept apart: the retiring session's decoder
        keeps filling a queue nobody is draining until it is closed, and the new
        session's fills one the loop has not started reading yet. Those are
        different faults with the same symptom, and the ordinary log cannot tell
        them apart at all.
        """
        out = {}
        total = 0
        sources = [("retiring", self._retired_drops, self._retired_total)]
        if session is not None and id(session) != self._retired_id:
            sources.append((
                "new",
                [t for t in (_read(session, "drop_times") or ()) if t >= self.started],
                _read(session, "dropped_frames"),
            ))
        for name, within, session_total in sources:
            total += len(within)
            out[name] = {
                # Drops whose timestamps fall inside this window. The times are
                # kept in a bounded deque, so on a session dropping thousands
                # this is a floor, not the count -- which session_total beside
                # it makes obvious rather than hiding.
                "in_window": len(within),
                "session_total": session_total,
                "at_ms": [_ms(t - self.started) for t in within[:_MAX_DROP_TIMES]],
            }
        out["total"] = total
        return out
