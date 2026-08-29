"""One structured record per interval saying WHERE the container's memory is,
so an audio-correlated climb can be attributed instead of guessed at (E83).

OFF unless config.MEMORY_TRACE. When it is off nothing here runs: `start()`
returns None, `tracemalloc` is never enabled, and no sampler task exists. This
is not a hot-path instrument -- it samples on its own timer and touches nothing
in the streaming loop.

WHAT IT IS FOR. With audio consuming, the container climbs about 12 MB per
minute and does not plateau; with audio off it climbs about 3.7 MB per minute
toward the plateau E74 measured and stops. Every diagnosis of that gap so far
has had to guess between four suspects that no existing signal separates,
because the number being watched -- `docker stats`, i.e. the cgroup -- is the
sum of FOUR processes:

    entrypoint.sh -> mediamtx  (Go)      + python3 -m bridge.main
                                          + one ffmpeg child PER CAMERA

so "the bridge is leaking" has never actually been measured. It is equally
consistent with MediaMTX buffering a second (AAC) track per stream for its
readers, with ffmpeg's two-input mux holding packets to interleave them, with
the Python heap retaining decoded frames, and with glibc holding freed memory in
per-thread arenas rather than returning it (which is what E74's plateau was).
Those four have four different fixes and three of them are not in this
repository, so the FIRST thing any record here has to answer is which process
the megabytes are in.

So one record answers four questions, outermost first:

  * WHICH PROCESS -- `container.procs`, every pid in the container's namespace
    with its RSS, and `container.rss_mb` as their sum. If the growth is not in
    `python3`, nothing else in this module matters and nothing in this
    repository is the fix.
  * IS IT THE PYTHON HEAP OR THE ALLOCATOR -- `glibc` (mallinfo2: how much the
    process has taken from the OS against how much is actually in use). Freed
    memory sitting in arenas reads as a large `fordblks` against a flat
    `uordblks`; a real Python-object leak moves both together. This is the
    reading that separates E74's arena plateau from an actual leak, and
    tracemalloc cannot make it because tracemalloc only sees the Python
    allocator.
  * WHAT KIND OF OBJECT -- `python.gc_growth`, the biggest movers in
    `gc.get_objects()` by type since the previous record. A retained
    `av.AudioFrame` per decoded frame shows here and nowhere else.
  * WHICH LINE ALLOCATED IT -- `tracemalloc.top`, the largest allocation sites
    with their DELTA since the previous record. The delta is the useful column:
    a big steady site is a buffer, a big growing one is the leak.

DIFFED ACROSS TWO ROTATIONS, deliberately. The default interval is
config.MEMORY_TRACE_INTERVAL_S (330 s, two of the ~165 s session rotations), so
consecutive records bracket at least one whole rotation per camera and a
per-rotation retention appears as a per-record step rather than as noise inside
one. Reading a single record tells you the shape; reading two consecutive ones
tells you the rate.

WHAT IT COSTS, stated rather than discovered later. tracemalloc roughly doubles
per-allocation cost and adds a frame's worth of memory per live allocation, so
it is part of the flag rather than always on, and `MEMORY_TRACE_FRAMES` is 1 by
default (the allocating line, no call stack) because deeper stacks multiply that
cost. `gc.get_objects()` walks every tracked object and takes tens of
milliseconds on this heap. Neither runs at all with the flag off, and with it on
they run once per interval, on the event loop, between rotations rather than
inside one. Turning it on CHANGES the number being measured (traced memory is
counted too) -- which is fine for attribution, and wrong for deciding whether
the leak is still there. Read `python.rss_mb` for that, never `traced_mb`.

WHY THE LOG AND NOT /control/status, for the same reason handover_trace.py
gives: this is a sample in a series, and a snapshot contract Home Assistant
polls continuously would report it an arbitrary number of times or not at all
depending on how the cadence lines up.

THE FORMAT is the one this project already uses for structured output: a plain
JSON object, snake_case keys, units in the name (`_mb`, `_s`), floats rounded at
the edge -- the same shape metrics.py hands the control API and handover_trace.py
writes to the log.

Reading one: `docker logs telus-cam-bridge | grep 'memory trace' | sed
's/.*memory trace //' | jq .` -- everything after the prefix is a single JSON
object. See docs/troubleshooting.md for the field-by-field walkthrough.
"""
from __future__ import annotations

import asyncio
import collections
import ctypes
import gc
import json
import logging
import os
import time
import tracemalloc

from . import config as c

log = logging.getLogger(__name__)

_PAGE_SIZE = os.sysconf("SC_PAGE_SIZE")

# Object types that are never interesting as movers: the counters themselves and
# the machinery of taking a sample. Excluded so the interesting rows are not
# pushed off the end of the list by this module's own work.
_BORING_TYPES = frozenset({"Counter", "Traceback", "Statistic", "StatisticDiff",
                           "Frame", "Snapshot"})

# How many processes one record lists, largest first. The container's pid
# namespace normally holds five (the entrypoint's bash, mediamtx, python3 and
# one ffmpeg per camera), so this is headroom rather than a limit -- but it is a
# hard cap because /proc is read from a process whose namespace this module
# cannot verify, and a record that grew with the host's process table would be
# the log flooding this flag exists to avoid. `container.rss_mb` is the sum over
# ALL of them regardless of how many are listed, and `container.procs_total`
# says how many that was.
_MAX_PROCS = 12

# Shortest window a megabytes-per-minute figure is reported for. Below it the
# division amplifies ordinary sampling noise into a number that reads like a
# catastrophe; the shipped interval is 330 s, so this only ever suppresses the
# rate on a hand-driven sample.
_MIN_RATE_WINDOW_S = 5.0


class _MallInfo2(ctypes.Structure):
    """glibc's `struct mallinfo2`.

    mallinfo2 rather than mallinfo: the fields are `size_t` here and `int` in
    the older call, and this process routinely holds more than 2 GB of virtual
    address space, where the `int` form silently wraps. Absent on musl, which is
    why every read of it is optional (see _glibc).
    """

    _fields_ = [(name, ctypes.c_size_t) for name in (
        "arena", "ordblks", "smblks", "hblks", "hblkhd", "usmblks", "fsmblks",
        "uordblks", "fordblks", "keepcost")]


def _mb(value) -> float | None:
    """Bytes to megabytes at 0.01 MB resolution, passing None through."""
    if value is None:
        return None
    return round(float(value) / 1e6, 2)


def _rss_bytes(pid) -> int | None:
    """Resident set size of one pid, or None if it is gone.

    /proc/<pid>/statm's second field, which is the same number `docker stats`
    sums -- read here rather than shelled out to `ps`, because this runs inside
    the container where there is no guarantee procps is installed.
    """
    try:
        with open(f"/proc/{pid}/statm") as handle:
            return int(handle.read().split()[1]) * _PAGE_SIZE
    except (OSError, ValueError, IndexError):
        return None


def _comm(pid) -> str:
    try:
        with open(f"/proc/{pid}/comm") as handle:
            return handle.read().strip()[:32]
    except OSError:
        return "?"


def _container_processes() -> dict:
    """Every process in this container's pid namespace, with its RSS.

    THE load-bearing field. The bridge shares its `mem_limit` with MediaMTX and
    with one ffmpeg per camera (see entrypoint.sh), so the container figure that
    raised E83 is a sum over processes this repository does not all own. Docker
    gives the container its own pid namespace, so /proc here lists exactly those
    processes and nothing of the host's.
    """
    procs = []
    total = 0
    try:
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            rss = _rss_bytes(name)
            if rss is None:
                continue
            total += rss
            procs.append({"pid": int(name), "comm": _comm(name), "rss_mb": _mb(rss)})
    except OSError:
        return {"rss_mb": None, "procs_total": None, "procs": []}
    # Largest first: the answer to "which process is it" should be row one.
    procs.sort(key=lambda entry: entry["rss_mb"] or 0.0, reverse=True)
    return {"rss_mb": _mb(total), "procs_total": len(procs),
            "procs": procs[:_MAX_PROCS]}


def _glibc() -> dict | None:
    """mallinfo2, or None where the C library does not provide it.

    `arena` + `hblkhd` is what the allocator has taken from the OS; `uordblks`
    is what is actually in use and `fordblks` what has been freed but not
    returned. A large and growing `fordblks` against a flat `uordblks` is the
    per-thread-arena behaviour E74 identified as this container's plateau, and
    it is invisible to tracemalloc, which only ever sees the Python allocator.
    """
    try:
        libc = ctypes.CDLL(None)
        mallinfo2 = libc.mallinfo2
    except (OSError, AttributeError):
        return None
    mallinfo2.restype = _MallInfo2
    mallinfo2.argtypes = []
    try:
        info = mallinfo2()
    except Exception:
        return None
    return {
        "arena_mb": _mb(info.arena),
        "mmapped_mb": _mb(info.hblkhd),
        "in_use_mb": _mb(info.uordblks),
        "free_not_returned_mb": _mb(info.fordblks),
        "releasable_mb": _mb(info.keepcost),
    }


def _gc_counts() -> collections.Counter:
    counts: collections.Counter = collections.Counter()
    for obj in gc.get_objects():
        counts[type(obj).__name__] += 1
    return counts


def _tracemalloc_top(previous, limit: int) -> tuple[dict | None, object]:
    """The largest allocation sites and their delta since `previous`.

    Returns (record, snapshot). The snapshot is handed back so the caller can
    keep exactly one -- a diff needs the previous one and nothing older, and a
    list of them would be this module leaking while it watched for leaks.
    """
    if not tracemalloc.is_tracing():
        return None, previous
    snapshot = tracemalloc.take_snapshot()
    traced, peak = tracemalloc.get_traced_memory()
    if previous is None:
        rows = [{"site": str(stat.traceback), "mb": _mb(stat.size),
                 "count": stat.count, "d_mb": None}
                for stat in snapshot.statistics("lineno")[:limit]]
    else:
        # Sorted by GROWTH, not by size: a big steady site is a buffer doing its
        # job, and the row worth reading is the one that got bigger between two
        # rotations.
        rows = [{"site": str(stat.traceback), "mb": _mb(stat.size),
                 "count": stat.count, "d_mb": _mb(stat.size_diff)}
                for stat in snapshot.compare_to(previous, "lineno")[:limit]]
    return ({"traced_mb": _mb(traced), "peak_mb": _mb(peak), "top": rows},
            snapshot)


class MemoryTrace:
    """The sampler's state: what the previous record saw, and nothing else.

    Exactly one previous gc census and one previous tracemalloc snapshot are
    kept, because a diff needs the one before and a series of them would make
    this module the growth it is watching.
    """

    def __init__(self):
        self.started = time.monotonic()
        self._prev_counts: collections.Counter | None = None
        self._prev_snapshot = None
        self._prev_rss: int | None = None
        self._prev_at: float | None = None

    def record(self, label: str = "periodic") -> dict:
        """One sample, as a plain dict -- what sample() serialises and what a
        test reads instead of re-parsing a log line."""
        now = time.monotonic()
        rss = _rss_bytes("self")
        counts = _gc_counts()
        growth = None
        if self._prev_counts is not None:
            movers = sorted(
                ((count - self._prev_counts.get(name, 0), name)
                 for name, count in counts.items() if name not in _BORING_TYPES),
                reverse=True)
            growth = [[delta, name] for delta, name in movers[:8] if delta]
        trace, snapshot = _tracemalloc_top(self._prev_snapshot,
                                           int(c.MEMORY_TRACE_TOP))
        # The rate, computed here rather than left to whoever reads the log: the
        # whole question E83 asks is "megabytes per minute", and two records
        # subtracted by hand is how a reading gets attributed to the wrong
        # window.
        rate = None
        if self._prev_rss is not None and rss is not None \
                and self._prev_at is not None \
                and now - self._prev_at >= _MIN_RATE_WINDOW_S:
            rate = round((rss - self._prev_rss) / 1e6 / ((now - self._prev_at) / 60.0), 2)
        out = {
            "at": round(time.time(), 3),
            "label": str(label),
            "uptime_s": round(now - self.started, 1),
            "python": {
                "rss_mb": _mb(rss),
                # Megabytes of RSS per minute since the PREVIOUS record. Python
                # only: the container's own rate is container.rss_mb across two
                # records, and the two differing is the finding.
                "rss_mb_per_min": rate,
                "threads": _thread_count(),
                "gc_objects": sum(counts.values()),
                "gc_growth": growth,
            },
            "glibc": _glibc(),
            "container": _container_processes(),
            "tracemalloc": trace,
        }
        self._prev_counts = counts
        self._prev_snapshot = snapshot
        self._prev_rss = rss
        self._prev_at = now
        return out

    def sample(self, label: str = "periodic") -> dict | None:
        """Take one record and write it to the log.

        TOTAL, like every method in handover_trace.py and for the same reason:
        a diagnostic that raised would take down whatever called it, and this
        one is called from a task inside main()'s gather.
        """
        try:
            record = self.record(label)
        except Exception:
            log.debug("memory trace sample failed", exc_info=True)
            return None
        try:
            log.info("memory trace %s", json.dumps(record, separators=(",", ":")))
        except Exception:
            log.debug("memory trace record could not be emitted", exc_info=True)
        return record


def _thread_count() -> int | None:
    try:
        import threading
        return len(threading.enumerate())
    except Exception:
        return None


async def _watch(tracer: MemoryTrace, interval: float):
    # The first record is taken one interval in, not at t=0: a sample of a
    # process that has not finished starting has nothing to be compared against
    # and only makes the second record look like a leak.
    while True:
        await asyncio.sleep(interval)
        tracer.sample()


def start():
    """Begin sampling, or return None when config.MEMORY_TRACE is off.

    The knob is read off the module attribute at call time rather than captured
    at import, like every other knob in config.py, so the setting in force is
    the one the process currently holds.
    """
    if not c.MEMORY_TRACE:
        return None
    frames = max(1, int(c.MEMORY_TRACE_FRAMES))
    if not tracemalloc.is_tracing():
        tracemalloc.start(frames)
    interval = float(c.MEMORY_TRACE_INTERVAL_S)
    log.warning(
        "MEMORY_TRACE is ON: sampling every %.0fs with tracemalloc tracing %d "
        "frame(s). This measures where memory is, and it also COSTS memory and "
        "allocation time -- turn it off once the question is answered (E83)",
        interval, frames)
    return asyncio.ensure_future(_watch(MemoryTrace(), interval))
