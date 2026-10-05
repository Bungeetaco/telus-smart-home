"""A process-wide hold on TELUS/alarm.com website calls while the edge is
refusing this account, plus the one re-login-and-retry helper every website
call goes through.

Why this exists (observed live 2026-10-03 10:44-11:14 and 2026-10-05 16:25-16:59,
same shape both times -- backlog E104). Alarm.com's edge blocked the account
for its stated ``Retry-After`` of 1825 s. The block did not look like a 429 on
most routes: the SSO login kept SUCCEEDING, and ordinary website GETs came back
``403 Forbidden`` instead. Each of those 403s was read as an expired session
cookie, so every poll re-logged-in and retried, which is the right response to
a stale cookie and exactly the wrong one to an edge block: in one hour the
control server logged 168 re-login warnings and the discovery poll failed every
cycle, all of it knocking on a limiter whose window restarts on every knock.
Both camera streams were down for the whole window because each session
rotation needs one website call (the video-source fetch) and that call was
walking into the same block on the 2/4/8/30 s reconnect schedule.

The hold is the missing piece of state: one answer to "is the edge refusing us
right now" that every website call consults BEFORE it is made.

* An explicit edge rate limit (a 429, or a ``RateLimited`` raised by a module
  that already parses one) holds every website call for the server's own
  ``Retry-After``, clamped and margined the same way the sleeps already are
  (``config.rate_limit_sleep_s``).
* A 401/403 that SURVIVES a fresh login is treated as the edge block it is:
  the hold starts at ``CLOUD_HOLD_INITIAL_S`` and doubles on each consecutive
  occurrence up to ``CLOUD_HOLD_MAX_S``. A plain expired cookie never reaches
  this path -- a fresh login fixes it and the retry succeeds -- so an ordinary
  session expiry costs what it always did: one login, one retry.
* The first call after a hold expires is the probe. If it succeeds the strike
  count resets; if it 403s again the hold doubles. Nothing else in the process
  touches the website while a hold is active, which is the whole point.

Callers that would have hit the website see ``CloudHeld`` (a ``RateLimited``
subclass carrying the remaining wait), so the control routes answer the same
honest ``429`` + ``Retry-After`` they already answer for an edge 429, and the
camera loop sleeps the hold the way it already sleeps a ``Retry-After``.
Fail-open by design: the hold only ever delays calls, never the login itself,
and an expired hold costs nothing.

Not covered, deliberately: the mobile (MWS) session's own calls, which already
absorb a rate limit per read (``control_server._mws_account_fetch``), and the
login POST, which has its own ``RateLimited`` handling in ``main.py``.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

import aiohttp

from . import config as c
from .telus_auth import RateLimited, relogin_if_stale

log = logging.getLogger(__name__)


class CloudHeld(RateLimited):
    """Raised INSTEAD of making a website call while a hold is active.

    A ``RateLimited`` so every existing handler of an edge rate limit (the 429
    routes, the camera loop's Retry-After sleep) treats it identically; the
    message says it was the local hold rather than a fresh edge answer, because
    the two look the same from outside and only the log can tell them apart."""

    def __init__(self, remaining_s: float, reason: str | None):
        super().__init__(remaining_s)
        self.reason = reason
        self.args = (f"TELUS website calls held locally for {remaining_s:.0f}s"
                     + (f": {reason}" if reason else ""),)

    def __str__(self) -> str:
        return self.args[0]


class CloudHold:
    """The hold's state. One instance per process (``HOLD`` below); the class
    exists so the tests can run one against a scripted clock."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._until = 0.0
        self._reason: str | None = None
        # Consecutive forbidden-after-relogin holds. Drives the doubling; reset by
        # the first website call that succeeds.
        self._strikes = 0

    @property
    def reason(self) -> str | None:
        return self._reason if self.active else None

    @property
    def strikes(self) -> int:
        return self._strikes

    def remaining(self) -> float:
        return max(0.0, self._until - self._clock())

    @property
    def active(self) -> bool:
        return self.remaining() > 0

    def check(self) -> None:
        """Raise ``CloudHeld`` if a hold is active. Every website call starts here."""
        remaining = self.remaining()
        if remaining > 0:
            raise CloudHeld(remaining, self._reason)

    def note_rate_limited(self, retry_after_s: float, what: str) -> None:
        """The edge answered with its own wait: hold for exactly that."""
        self._extend(c.rate_limit_sleep_s(retry_after_s),
                     f"edge Retry-After {float(retry_after_s):.0f}s on {what}")

    def note_forbidden_after_relogin(self, what: str) -> float:
        """A 401/403 right after a successful fresh login. Returns the hold length."""
        self._strikes += 1
        hold_s = min(c.CLOUD_HOLD_INITIAL_S * (2 ** (self._strikes - 1)), c.CLOUD_HOLD_MAX_S)
        self._extend(hold_s, f"403 after a fresh login on {what} (strike {self._strikes})")
        return hold_s

    def note_success(self) -> None:
        """A website call went through: the edge is serving us again."""
        if self._strikes or self._until:
            log.info("TELUS website calls succeeding again; cloud hold cleared "
                     "(was: %s)", self._reason)
        self._strikes = 0
        self._until = 0.0
        self._reason = None

    def _extend(self, hold_s: float, reason: str) -> None:
        until = self._clock() + hold_s
        if until <= self._until:
            # A longer hold is already in force; never shorten one.
            return
        self._until = until
        self._reason = reason
        log.warning("Holding every TELUS website call for %.0fs: %s", hold_s, reason)


HOLD = CloudHold()


def _retry_after_of(err: aiohttp.ClientResponseError) -> float:
    headers = err.headers or {}
    return c.parse_retry_after(headers.get("Retry-After"))


async def call_with_relogin(telus, login_lock, coro_fn: Callable[..., Awaitable],
                            *args, hold: CloudHold | None = None, what: str | None = None):
    """Call ``coro_fn(telus, *args)`` against the website, through the hold.

    On a 401/403 (an expired session cookie, usually) re-login once -- serialized
    and de-duplicated against every other login() caller via ``login_lock`` and
    ``relogin_if_stale`` -- and retry exactly once. A 401/403 on THAT retry is
    not a cookie problem and starts (or doubles) the hold; so does any explicit
    edge rate limit, whether it arrives as a 429 or as a ``RateLimited`` from a
    module that parsed one itself. A call made while the hold is active raises
    ``CloudHeld`` without touching the network.

    ``what`` names the call in the log; it defaults to the coroutine's name,
    which is wrong for a lambda -- pass it explicitly there.
    """
    hold = HOLD if hold is None else hold
    what = what or getattr(coro_fn, "__name__", "website call")
    hold.check()
    try:
        result = await coro_fn(telus, *args)
    except RateLimited as err:
        hold.note_rate_limited(err.retry_after_s, what)
        raise
    except aiohttp.ClientResponseError as err:
        if err.status == 429:
            retry_after = _retry_after_of(err)
            hold.note_rate_limited(retry_after, what)
            raise RateLimited(retry_after) from err
        if err.status not in (401, 403):
            raise
        log.warning("%s got %s, re-logging in to TELUS", what, err.status)
        try:
            await relogin_if_stale(telus, login_lock)
        except RateLimited as login_err:
            # The login POST itself was rate limited: the edge is refusing the
            # account outright, so hold for its stated wait.
            hold.note_rate_limited(login_err.retry_after_s, "login")
            raise
        try:
            result = await coro_fn(telus, *args)
        except aiohttp.ClientResponseError as retry_err:
            if retry_err.status in (401, 403):
                hold.note_forbidden_after_relogin(what)
                raise CloudHeld(hold.remaining(), hold.reason) from retry_err
            if retry_err.status == 429:
                retry_after = _retry_after_of(retry_err)
                hold.note_rate_limited(retry_after, what)
                raise RateLimited(retry_after) from retry_err
            raise
    hold.note_success()
    return result
