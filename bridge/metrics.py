"""In-process counters for the talkdown path.

Talkdown is user-triggered, comparatively rare, and fails in ways that are easy
to miss: a failure surfaces as one HTTP error to whoever asked, and nothing keeps
score. That makes "has the doorbell been refusing talks all week?" unanswerable
without grepping logs. These counters give the control API a cheap answer.

Deliberately small: plain in-memory counters with no persistence and no
dependency. They reset when the bridge restarts, which is fine for "is it working
now / what happened recently" -- the durable record is still the log. Everything
runs on the single asyncio loop, so no locking is needed.

Nothing here records secrets, audio, or the content of a request -- only outcome
labels, counts and timings.
"""
from __future__ import annotations

import time
from collections import Counter, deque

# Enough recent talks to see a pattern without unbounded growth.
_RECENT_MAX = 20


class TalkMetrics:
    """Counters + a rolling window of recent talk outcomes."""

    def __init__(self) -> None:
        self.started_at = time.time()
        self.attempts = 0
        self.succeeded = 0
        self.stopped = 0            # ended early by an explicit stop request
        self.rejected = 0           # refused because a talk was already running
        self.failed = 0
        self.failures_by_reason: Counter[str] = Counter()
        self.by_endpoint: Counter[str] = Counter()   # direct / local / public / cloud
        self.by_codec: Counter[str] = Counter()
        self.by_path: Counter[str] = Counter()       # full_duplex / webrtc / srtp
        self.renegotiations = 0
        self.audio_seconds_played = 0.0   # audio actually pushed to a speaker
        self.talk_seconds = 0.0           # wall time spent in talk requests
        self.last_success_at: float | None = None
        self.last_failure_at: float | None = None
        self.last_failure_reason: str | None = None
        self.recent: deque[dict] = deque(maxlen=_RECENT_MAX)

    # ---- recording ------------------------------------------------------
    def attempt(self) -> None:
        self.attempts += 1

    def rejected_in_progress(self, camera_id: str) -> None:
        self.rejected += 1
        self._remember(camera_id, "rejected", 0.0)

    def success(self, camera_id: str, *, wall_s: float, result: dict | None = None) -> None:
        result = result or {}
        stopped = bool(result.get("stopped"))
        if stopped:
            self.stopped += 1
        else:
            self.succeeded += 1
        self.talk_seconds += wall_s
        self.audio_seconds_played += float(
            result.get("played_seconds") or result.get("seconds") or 0.0)
        self.renegotiations += int(result.get("renegotiations") or 0)
        if result.get("codec"):
            self.by_codec[str(result["codec"])] += 1
        if result.get("endpoint"):
            self.by_endpoint[str(result["endpoint"])] += 1
        if result.get("path"):
            self.by_path[str(result["path"])] += 1
        self.last_success_at = time.time()
        self._remember(camera_id, "stopped" if stopped else "ok", wall_s, result)

    def failure(self, camera_id: str, reason: str, *, wall_s: float = 0.0) -> None:
        self.failed += 1
        self.failures_by_reason[reason] += 1
        self.talk_seconds += wall_s
        self.last_failure_at = time.time()
        self.last_failure_reason = reason
        self._remember(camera_id, f"failed:{reason}", wall_s)

    def endpoint_used(self, kind: str | None) -> None:
        """Recorded when a session is armed, so the counter still reflects which
        endpoint was in play even if the talk later fails."""
        if kind:
            self.by_endpoint[str(kind)] += 1

    def _remember(self, camera_id: str, outcome: str, wall_s: float,
                  result: dict | None = None) -> None:
        entry = {
            "at": round(time.time(), 3),
            "camera_id": camera_id,
            "outcome": outcome,
            "wall_seconds": round(wall_s, 2),
        }
        if result:
            for key in ("played_seconds", "seconds", "renegotiations", "codec", "chunks"):
                if result.get(key) is not None:
                    entry[key] = result[key]
        self.recent.append(entry)

    # ---- reporting ------------------------------------------------------
    def snapshot(self) -> dict:
        """JSON-serialisable view for the control API."""
        return {
            "since": round(self.started_at, 3),
            "uptime_seconds": round(time.time() - self.started_at, 1),
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "stopped": self.stopped,
            "failed": self.failed,
            "rejected_in_progress": self.rejected,
            "failures_by_reason": dict(self.failures_by_reason),
            "by_endpoint": dict(self.by_endpoint),
            "by_codec": dict(self.by_codec),
            "by_path": dict(self.by_path),
            "renegotiations": self.renegotiations,
            "audio_seconds_played": round(self.audio_seconds_played, 1),
            "talk_seconds": round(self.talk_seconds, 1),
            "last_success_at": (round(self.last_success_at, 3)
                                if self.last_success_at else None),
            "last_failure_at": (round(self.last_failure_at, 3)
                                if self.last_failure_at else None),
            "last_failure_reason": self.last_failure_reason,
            "recent": list(self.recent),
        }

    def summary(self) -> dict:
        """The few fields worth folding into /control/status."""
        return {
            "attempts": self.attempts,
            "succeeded": self.succeeded,
            "stopped": self.stopped,
            "failed": self.failed,
            "last_failure_reason": self.last_failure_reason,
        }


# One instance for the process; talk.py records into it and control_server reads it.
talk = TalkMetrics()
