"""Tests for the container memory instrumentation (bridge/memory_trace.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_memory_trace.py

Why this file exists: **an instrument that lies about memory is worse than no
instrument, and both of its failure modes are silent.**

It can lie by RUNNING when it is supposed to be off. tracemalloc roughly doubles
per-allocation cost and stores a frame per live allocation, and each sample
walks every gc-tracked object; left on by accident it would change the very
number E83 is trying to attribute, and the only symptom would be a slightly
worse memory curve -- indistinguishable from the leak being measured.

It can also lie by producing a plausible record that names the wrong thing. A
record is read once, under pressure, to decide which of four processes and which
of four mechanisms to chase; one that emits well-formed JSON while pointing at
the sampler's own allocations would send a session at the wrong target, which is
the exact failure the measurement discipline in AGENTS.md exists to prevent.

Pinned here:

  * OFF by default: start() returns None, nothing is scheduled, and tracemalloc
    is not started.
  * The record NAMES a deliberate allocation -- the allocating line as the
    top-growth row, and the object type among the gc movers.
  * The record separates the processes sharing the container's mem_limit
    (mediamtx + python3 + one ffmpeg per camera share it -- see entrypoint.sh),
    which is the first question any reading of E83 has to answer.
  * The sampler keeps exactly ONE previous snapshot, so watching for a leak is
    not itself one.
  * Every method is total, because it is called from a task inside main()'s
    gather, and the record is JSON-serialisable, because it is emitted as one
    log line.
"""
import asyncio
import gc
import json
import os
import pathlib
import sys
import tracemalloc

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c            # noqa: E402
from bridge import memory_trace as mt     # noqa: E402


class ScriptedProcfs:
    """`os` with /proc listing a scripted set of pids, and nothing else changed.

    The same shim shape test_camera_session.py uses for `websockets`, and for
    the same reason: the process table is the one input this module reads from
    the outside world, and a test that read the REAL one could only assert
    properties the host happens to satisfy. It did exactly that once -- the
    largest-first check passed against a build with the sort deleted, because
    this host's first twelve /proc entries are this process followed by kernel
    threads with no RSS at all, which is already descending.
    """

    def __init__(self, real, pids):
        self._real = real
        self._pids = [str(pid) for pid in pids]

    def listdir(self, path):
        return list(self._pids) if path == "/proc" else self._real.listdir(path)

    def __getattr__(self, name):
        return getattr(self._real, name)


class Ballast:
    """A type that appears nowhere else, so a gc census can be checked for it by
    name rather than by counting anonymous bytearrays."""

    def __init__(self, size):
        self.payload = bytearray(size)


def _record_pair(allocate):
    """Two records with `allocate()` run between them, tracing enabled for both.

    tracemalloc has to be started BEFORE the first record, because a diff can
    only see allocations made while tracing was on -- which is also why start()
    enables it rather than the first sample doing so.
    """
    already = tracemalloc.is_tracing()
    if not already:
        tracemalloc.start(1)
    try:
        tracer = mt.MemoryTrace()
        first = tracer.record("first")
        held = allocate()
        second = tracer.record("second")
        return tracer, first, second, held
    finally:
        if not already:
            tracemalloc.stop()


def test_off_by_default_builds_nothing():
    """The flag is the whole safety property: with it off nothing is scheduled
    and tracemalloc is never started.

    A sampler that ran anyway would add allocation cost and traced memory to the
    process whose memory is the thing under investigation -- and would do it
    invisibly, because the only evidence is the curve it is there to explain.
    """
    assert c.MEMORY_TRACE is False, (
        "MEMORY_TRACE ships ON. It costs allocation time and memory and changes "
        "the number E83 is measuring; it is a flag you turn on to attribute a "
        "climb and turn off again")

    async def check():
        saved = c.MEMORY_TRACE
        c.MEMORY_TRACE = False
        was_tracing = tracemalloc.is_tracing()
        try:
            task = mt.start()
            assert task is None, "start() scheduled a sampler with the flag off"
            assert tracemalloc.is_tracing() == was_tracing, (
                "start() enabled tracemalloc with the flag off")
        finally:
            c.MEMORY_TRACE = saved

    asyncio.run(check())
    print("  ok  off by default: start() returns None, tracemalloc untouched")


def test_the_record_names_what_actually_grew():
    """The measurement itself: something allocated between two records has to be
    the thing the second record blames.

    Both halves matter and answer different questions. `tracemalloc.top` is
    sorted by GROWTH rather than by size, so the row that got bigger outranks a
    large steady buffer doing its job -- that is the "which line" answer.
    `gc_growth` is the "what kind of object" answer, and it is the only one of
    the two that can see a retained av.AudioFrame for what it is.
    """
    _tracer, first, second, held = _record_pair(
        lambda: [Ballast(200_000) for _ in range(40)])
    assert held                       # keep it alive across the second record
    assert first["tracemalloc"]["top"][0]["d_mb"] is None, (
        "the first record reports deltas, but it has nothing to diff against; "
        "a number there would be invented")

    top = second["tracemalloc"]["top"][0]
    assert "test_memory_trace.py" in top["site"], (
        f"the top-growth row is {top['site']!r}, not the line that allocated "
        "8 MB between the two records -- a record that blames the sampler's own "
        "allocations sends the reader at the wrong target")
    assert top["d_mb"] >= 5.0, (
        f"the top row grew by {top['d_mb']} MB against ~8 MB allocated; the "
        "delta is the column the whole diff exists for")

    movers = dict((name, delta) for delta, name in (second["python"]["gc_growth"] or []))
    assert movers.get("Ballast", 0) >= 40, (
        f"gc_growth does not report the 40 objects that were retained: {movers}. "
        "This is the only field that can name a retained decoded frame by type")
    print(f"  ok  names the grower: top site {top['site'].rsplit('/', 1)[-1]} "
          f"at {top['d_mb']}MB, and Ballast x{movers.get('Ballast')} among the "
          "gc movers")


def test_the_record_separates_the_processes_sharing_the_limit():
    """`container.procs` is the first thing a reader needs, because the number
    that raised E83 is the cgroup's and the cgroup holds four processes.

    entrypoint.sh runs mediamtx and python3 side by side, and camera_session
    launches one ffmpeg per camera; all four count against the same
    docker-compose `mem_limit`. Without this field "the bridge leaks" is a claim
    about a sum that nothing has ever decomposed, and three of the four
    candidate mechanisms are not fixable from this repository at all.
    """
    # A scripted table an order of magnitude bigger than the container's real
    # five, with the biggest process buried in the middle so "largest first"
    # cannot be satisfied by listing them in the order /proc happened to yield.
    sizes = {100 + i: (i * 1_000_000) for i in range(20)}
    sizes[105] = 400_000_000        # the mediamtx-shaped one
    sizes[117] = 250_000_000
    saved_os, saved_rss, saved_comm = mt.os, mt._rss_bytes, mt._comm
    try:
        mt.os = ScriptedProcfs(saved_os, sorted(sizes))
        # "self" is what record() reads for the python block; leave that one
        # real so the two halves of the record are not both scripted.
        mt._rss_bytes = lambda pid: (saved_rss(pid) if pid == "self"
                                     else sizes.get(int(pid)))
        mt._comm = lambda pid: f"proc{pid}"
        container = mt.MemoryTrace().record()["container"]
    finally:
        mt.os, mt._rss_bytes, mt._comm = saved_os, saved_rss, saved_comm

    assert container["procs_total"] == len(sizes), (
        f"procs_total is {container['procs_total']} against {len(sizes)} "
        "processes -- it must count every one, not just the listed rows, or "
        "rss_mb is a sum over rows nobody can see")
    assert container["rss_mb"] == round(sum(sizes.values()) / 1e6, 2), (
        f"the container total is {container['rss_mb']}MB against a scripted "
        f"{sum(sizes.values()) / 1e6}MB; this is the number E83 is stated in")
    assert len(container["procs"]) == mt._MAX_PROCS, (
        f"{len(container['procs'])} rows listed against a cap of "
        f"{mt._MAX_PROCS}; a record that grows with the process table is the "
        "log flooding this flag exists to avoid")
    listed = [row["rss_mb"] for row in container["procs"]]
    assert listed == sorted(listed, reverse=True), (
        f"processes are not largest-first: {listed}. The answer to 'which "
        "process is it' has to be row one")
    assert container["procs"][0]["pid"] == 105, (
        f"the largest process is {container['procs'][0]['pid']}, not the 400MB "
        "one; the list is being truncated before it is ranked")
    assert {row["pid"] for row in container["procs"]} == \
        set(sorted(sizes, key=sizes.get, reverse=True)[:mt._MAX_PROCS]), (
        "the listed rows are not the largest processes, so the cap is dropping "
        "the very rows the record exists to show")

    # ...and against the REAL table, the one cross-check a scripted one cannot
    # make: the python block and the container list must be two readings of the
    # same number for this process.
    record = mt.MemoryTrace().record()
    mine = [p for p in record["container"]["procs"] if p["pid"] == os.getpid()]
    if mine:
        assert abs(mine[0]["rss_mb"] - record["python"]["rss_mb"]) < 1.0, (
            f"this process reads {mine[0]['rss_mb']}MB in the container list "
            f"and {record['python']['rss_mb']}MB in the python block; two "
            "readings of one number that disagree are worse than one")
    glibc = record["glibc"]
    assert glibc is None or {"arena_mb", "in_use_mb", "free_not_returned_mb"} \
        <= set(glibc), (
        f"the glibc block is missing the fields that separate 'the heap is "
        f"full' from 'the allocator is holding freed memory': {glibc}")
    print(f"  ok  container breakdown: {len(sizes)} scripted process(es) "
          f"summed and ranked, capped at {mt._MAX_PROCS}, glibc arenas read")


def test_watching_for_a_leak_is_not_itself_one():
    """The sampler keeps exactly one previous census and one previous snapshot.

    A diff needs the record before it and nothing older. Keeping a series is the
    obvious way to write this and would make the instrument grow without bound
    for as long as it ran -- inside the process whose growth it is measuring,
    which is how a diagnostic starts producing its own findings.
    """
    tracer, _first, _second, held = _record_pair(
        lambda: [Ballast(1_000) for _ in range(5)])
    assert held
    for _ in range(20):
        tracer.record()
    gc.collect()
    snapshots = [obj for obj in gc.get_objects()
                 if type(obj).__name__ == "Snapshot"]
    assert len(snapshots) <= 1, (
        f"{len(snapshots)} tracemalloc snapshots are alive after 22 records; "
        "the sampler is accumulating the thing it takes to look for accumulation")
    assert isinstance(tracer._prev_counts, dict), tracer._prev_counts
    print(f"  ok  bounded state: {len(snapshots)} snapshot(s) after 22 records")


def test_a_record_is_total_and_serialisable():
    """Every path here is called from a task inside main()'s gather, so nothing
    may raise -- and the record is emitted as ONE log line, so it has to survive
    json.dumps with no custom encoder.

    Driven by breaking the two readings that touch the outside world (/proc and
    libc), because those are the ones that differ between this host, the Debian
    image and a musl one.
    """
    saved_rss, saved_glibc = mt._rss_bytes, mt._glibc
    try:
        mt._rss_bytes = lambda pid: (_ for _ in ()).throw(RuntimeError("boom"))
        mt._glibc = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        tracer = mt.MemoryTrace()
        assert tracer.sample("broken") is None, (
            "a sample that could not be taken must report that, not a record "
            "full of nulls that reads like a measurement")
    finally:
        mt._rss_bytes, mt._glibc = saved_rss, saved_glibc

    # ...and with libc absent but /proc readable, which is the musl case: the
    # record is still produced, with the glibc block honestly null.
    saved_glibc = mt._glibc
    try:
        mt._glibc = lambda: None
        record = mt.MemoryTrace().record()
        assert record["glibc"] is None, record["glibc"]
        blob = json.dumps(record)
    finally:
        mt._glibc = saved_glibc
    assert '"rss_mb"' in blob and '"tracemalloc"' in blob, blob[:200]
    print("  ok  total and serialisable: a failed read yields no record, a "
          "missing mallinfo2 yields an honest null, and the record is JSON")


TESTS = [
    test_off_by_default_builds_nothing,
    test_the_record_names_what_actually_grew,
    test_the_record_separates_the_processes_sharing_the_limit,
    test_watching_for_a_leak_is_not_itself_one,
    test_a_record_is_total_and_serialisable,
]


def main():
    failures = 0
    for test in TESTS:
        try:
            test()
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
