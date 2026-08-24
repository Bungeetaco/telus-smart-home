"""Tests for the talkdown metrics counters (bridge/metrics.py).

Pure in-memory accounting, so no server and no ffmpeg: these assert that each
outcome lands in the right bucket, that durations accumulate, that the recent
window stays bounded, and -- the point of the "reason" labels -- that a failure
label never carries the exception's message (which can contain a URL or other
detail that has no business in a counter).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_metrics.py
"""
import json
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge.metrics import TalkMetrics  # noqa: E402


def test_counts_and_durations():
    m = TalkMetrics()
    m.attempt()
    m.success("cam-1", wall_s=6.2, result={
        "played_seconds": 6.0, "seconds": 6.0, "renegotiations": 1,
        "codec": "mulaw", "chunks": 150})
    m.attempt()
    m.success("cam-1", wall_s=4.0, result={
        "played_seconds": 3.9, "seconds": 30.0, "stopped": True, "codec": "mulaw"})
    m.attempt()
    m.failure("cam-1", "no_upstream_audio", wall_s=1.5)
    m.rejected_in_progress("cam-1")
    m.endpoint_used("direct")

    snap = m.snapshot()
    assert snap["attempts"] == 3, snap
    assert snap["succeeded"] == 1, snap
    assert snap["stopped"] == 1, snap
    assert snap["failed"] == 1, snap
    assert snap["rejected_in_progress"] == 1, snap
    assert snap["failures_by_reason"] == {"no_upstream_audio": 1}, snap
    assert snap["renegotiations"] == 1, snap
    assert snap["by_codec"] == {"mulaw": 2}, snap
    assert snap["by_endpoint"] == {"direct": 1}, snap
    # 6.0 played + 3.9 played (NOT the 30.0 the stopped clip would have been)
    assert abs(snap["audio_seconds_played"] - 9.9) < 0.01, snap
    assert abs(snap["talk_seconds"] - 11.7) < 0.01, snap
    assert snap["last_failure_reason"] == "no_upstream_audio"
    assert snap["last_success_at"] and snap["last_failure_at"]
    json.dumps(snap)   # must be serialisable for the control API
    print("  ok  counts/durations: outcomes bucketed, stopped clip counted by "
          "audio PLAYED not clip length, snapshot is JSON-serialisable")


def test_by_path_counter():
    """by_path must record WHICH talk transport actually ran, per success.

    The talk response reports its transport ("full_duplex"/"webrtc"/"srtp"),
    but a caller only sees its own response -- without this counter a preferred
    path silently degrading to the fallback for every talk is invisible in
    /control/metrics."""
    m = TalkMetrics()
    for path, n in (("full_duplex", 2), ("webrtc", 1), ("srtp", 1)):
        for _ in range(n):
            m.attempt()
            m.success("cam-1", wall_s=1.0, result={"path": path})
    # A stopped talk still ran a transport, so it counts too (like by_codec).
    m.attempt()
    m.success("cam-1", wall_s=0.5, result={"path": "full_duplex", "stopped": True})
    # Failures and path-less results must not add a bucket.
    m.attempt()
    m.failure("cam-1", "no_upstream_audio", wall_s=1.0)
    m.attempt()
    m.success("cam-1", wall_s=1.0, result={})

    snap = m.snapshot()
    assert snap["by_path"] == {"full_duplex": 3, "webrtc": 1, "srtp": 1}, snap
    json.dumps(snap)   # must stay serialisable for the control API
    print("  ok  by_path: one count per talk that ran a transport, "
          "failures and path-less results excluded")


def test_recent_window_bounded():
    m = TalkMetrics()
    for i in range(50):
        m.attempt()
        m.success(f"cam-{i}", wall_s=1.0, result={"played_seconds": 1.0})
    snap = m.snapshot()
    assert snap["attempts"] == 50
    assert len(snap["recent"]) == 20, f"recent window unbounded: {len(snap['recent'])}"
    assert snap["recent"][-1]["camera_id"] == "cam-49", "recent should keep the newest"
    print(f"  ok  recent window: bounded at {len(snap['recent'])} entries, newest kept")


def test_reason_labels_carry_no_detail():
    """Failure labels must be stable identifiers, never exception text."""
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from bridge import talk

    secret_ish = "https://192.0.2.10:41925/api/PutAudioStream?camera_id=SECRETTOKEN"
    cases = [
        (talk.TalkMediaTooLarge(secret_ish), "media_too_large"),
        (talk.TalkMediaError(secret_ish), "media_error"),
        (talk.NoUpstreamAudio(secret_ish), "no_upstream_audio"),
        (TimeoutError(), "timeout"),
        (RuntimeError(secret_ish), "upstream_error"),
    ]
    m = TalkMetrics()
    for err, expected in cases:
        label = talk._failure_reason(err)
        assert label == expected, f"{type(err).__name__} -> {label!r}, expected {expected!r}"
        m.attempt()
        m.failure("cam-1", label)
    blob = json.dumps(m.snapshot())
    assert "SECRETTOKEN" not in blob and "192.0.2.10" not in blob, \
        "metrics leaked exception detail into a counter"
    print("  ok  reason labels: stable identifiers, no exception text in the snapshot")


def main():
    failures = 0
    for test in (test_counts_and_durations, test_by_path_counter,
                 test_recent_window_bounded, test_reason_labels_carry_no_detail):
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
