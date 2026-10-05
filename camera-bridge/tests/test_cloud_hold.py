"""Tests for the process-wide cloud hold (bridge/cloud_hold.py) and the
re-login-and-retry helper every TELUS website call goes through.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_cloud_hold.py

No network and no live account: the "website" is a scripted coroutine that
answers whatever the test tells it to, the session's login() just counts, and
the hold runs against a scripted clock.

The invisible failure this file exists to catch (backlog E104, observed live
2026-10-03 and 2026-10-05): the alarm.com edge refused the account for ~30 min
and did so with 403s that looked exactly like an expired session cookie, so
every poll re-logged-in and retried -- 168 re-login warnings in one hour, the
discovery poll failing every cycle, both camera streams down for the window --
with the container healthy, zero restarts, and the bridge connectivity sensor
on the whole time. Nothing raised, nothing restarted, and every 502 the routes
answered was a well-formed response that sent Home Assistant straight back for
another try. What is pinned here:

  1. A 401/403 that a fresh login FIXES costs what it always did: one login,
     one retry, no hold. The ordinary cookie expiry must stay cheap, or the
     hold would be a new outage mode of its own.
  2. A 401/403 that SURVIVES the fresh login starts a hold, and while it is
     active the website is not touched at all -- the scripted call must not be
     invoked, and callers get a RateLimited carrying the remaining wait.
  3. The hold doubles per consecutive strike up to the cap, and the first
     successful call after it expires resets the strikes.
  4. An explicit edge wait (a 429 with Retry-After, or a RateLimited raised by
     a module that parsed one) holds for the server's own number, margined and
     clamped the way the sleeps already are, and a longer hold in force is
     never shortened by a later, shorter one.
  5. The control routes answer a held call as 429 + Retry-After, not 502: a
     502 is what tells a poller "try again on your own schedule", which is the
     behaviour that kept the window open.
"""
import asyncio
import contextlib
import logging
import os
import pathlib
import sys
import tempfile

import aiohttp
from aiohttp.test_utils import TestClient, TestServer
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c  # noqa: E402

_TMP = tempfile.mkdtemp(prefix="telus-cloud-hold-test-")
c.SHARED_DIR = _TMP
c.CONTROL_SECRET_PATH = os.path.join(_TMP, "control_secret")

from bridge import cloud_hold  # noqa: E402
from bridge import control_server as cs  # noqa: E402
from bridge.cloud_hold import CloudHeld, CloudHold, call_with_relogin  # noqa: E402
from bridge.telus_auth import RateLimited  # noqa: E402

logging.getLogger("bridge.cloud_hold").setLevel(logging.CRITICAL)
logging.getLogger("bridge.control_server").setLevel(logging.CRITICAL)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def _http_error(status: int, retry_after: str | None = None) -> aiohttp.ClientResponseError:
    headers = CIMultiDict()
    if retry_after is not None:
        headers["Retry-After"] = retry_after
    info = aiohttp.RequestInfo(URL("https://example.invalid/web/api/x"), "GET",
                               CIMultiDictProxy(CIMultiDict()), URL("https://example.invalid/web/api/x"))
    return aiohttp.ClientResponseError(info, (), status=status, message="scripted",
                                       headers=CIMultiDictProxy(headers))


class FakeTelus:
    """login() counts and bumps the generation like the real TelusSession."""

    def __init__(self):
        self.logins = 0
        self.login_generation = 0

    async def login(self):
        self.logins += 1
        self.login_generation += 1


class Scripted:
    """A website call that answers from a script, one entry per invocation."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, _telus, *args):
        self.calls += 1
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    __name__ = "scripted"


async def _run(coro):
    # The suite's main() already runs inside asyncio.run(); the tests below are
    # coroutines themselves and await through this so a sync `asyncio.run`
    # never nests inside the running loop.
    return await coro


# ---------------------------------------------------------------------------
# The hold on its own
# ---------------------------------------------------------------------------

def test_hold_is_inactive_until_something_happens():
    clock = Clock()
    hold = CloudHold(clock)
    assert not hold.active and hold.remaining() == 0.0 and hold.reason is None
    hold.check()  # must not raise
    print("  ok  a fresh hold refuses nothing")


def test_forbidden_after_relogin_doubles_to_the_cap_and_a_success_resets():
    clock = Clock()
    hold = CloudHold(clock)
    expected = []
    length = c.CLOUD_HOLD_INITIAL_S
    for _ in range(12):
        expected.append(min(length, c.CLOUD_HOLD_MAX_S))
        length *= 2
    seen = []
    for i, want in enumerate(expected, start=1):
        got = hold.note_forbidden_after_relogin("list_video_events")
        seen.append(got)
        assert got == want, f"strike {i}: hold {got}s, expected {want}s"
        assert hold.strikes == i
        assert abs(hold.remaining() - want) < 1e-6, (
            f"strike {i}: remaining {hold.remaining()} != {want}")
        with contextlib.suppress(CloudHeld):
            hold.check()
            raise AssertionError("an active hold must refuse the call")
        clock.now += want + 1  # let it expire before the next strike
    assert seen[-1] == c.CLOUD_HOLD_MAX_S, "the doubling must stop at CLOUD_HOLD_MAX_S"
    hold.note_success()
    assert hold.strikes == 0 and not hold.active, "a success must reset the strikes"
    assert hold.note_forbidden_after_relogin("x") == c.CLOUD_HOLD_INITIAL_S, (
        "after a reset the next strike starts over at the initial hold")
    print(f"  ok  strikes double {expected[0]:.0f}s -> {c.CLOUD_HOLD_MAX_S:.0f}s cap, "
          "and a success resets them")


def test_rate_limited_holds_for_the_servers_wait_and_never_shortens():
    clock = Clock()
    hold = CloudHold(clock)
    hold.note_rate_limited(1825, "list_history_events")
    assert abs(hold.remaining() - c.rate_limit_sleep_s(1825)) < 1e-6, (
        f"must hold for rate_limit_sleep_s(1825)={c.rate_limit_sleep_s(1825)}, "
        f"got {hold.remaining()}")
    assert "1825" in (hold.reason or "")
    before = hold.remaining()
    hold.note_rate_limited(10, "get_audio_settings")
    assert hold.remaining() == before, "a shorter wait must not shorten a longer hold in force"
    hold.note_forbidden_after_relogin("x")
    assert hold.remaining() == before, "a strike must not shorten a longer hold either"
    try:
        hold.check()
    except CloudHeld as exc:
        assert isinstance(exc, RateLimited), "CloudHeld must be a RateLimited"
        assert abs(exc.retry_after_s - before) < 1e-6
        assert "held locally" in str(exc) and "1825" in str(exc), str(exc)
    else:
        raise AssertionError("held call must raise CloudHeld")
    clock.now += before + 0.01
    hold.check()  # expired: silent
    print("  ok  an edge Retry-After holds for the server's margined wait; never shortened")


# ---------------------------------------------------------------------------
# call_with_relogin against a scripted website
# ---------------------------------------------------------------------------

async def test_expired_cookie_costs_one_login_and_one_retry_and_no_hold():
    clock = Clock()
    hold = CloudHold(clock)
    telus = FakeTelus()
    site = Scripted(_http_error(403), {"ok": 1})
    result = await _run(call_with_relogin(telus, asyncio.Lock(), site, hold=hold))
    assert result == {"ok": 1}
    assert telus.logins == 1 and site.calls == 2
    assert not hold.active and hold.strikes == 0, "a fixed cookie expiry must not hold"
    print("  ok  expired cookie: one login, one retry, no hold")


async def test_forbidden_after_login_holds_and_the_website_is_not_touched_meanwhile():
    clock = Clock()
    hold = CloudHold(clock)
    telus = FakeTelus()
    site = Scripted(_http_error(403), _http_error(403))
    try:
        await _run(call_with_relogin(telus, asyncio.Lock(), site, hold=hold))
    except CloudHeld as exc:
        assert exc.__cause__ is not None and exc.__cause__.status == 403
        assert abs(exc.retry_after_s - c.CLOUD_HOLD_INITIAL_S) < 1e-6
    else:
        raise AssertionError("a 403 after a fresh login must raise CloudHeld")
    assert telus.logins == 1 and site.calls == 2
    assert hold.active and hold.strikes == 1

    # Held: the scripted site has NO answers left, so touching it would raise
    # IndexError rather than CloudHeld -- that is the assertion.
    untouched = Scripted()
    for _ in range(3):
        try:
            await _run(call_with_relogin(telus, asyncio.Lock(), untouched, hold=hold))
        except CloudHeld:
            pass
        else:
            raise AssertionError("held call must not reach the website")
    assert untouched.calls == 0 and telus.logins == 1, (
        "neither the website nor the login may be touched while held")

    # Expired: the next call is the probe. It 403s again -> strike 2, doubled.
    clock.now += c.CLOUD_HOLD_INITIAL_S + 1
    probe = Scripted(_http_error(403), _http_error(401))
    try:
        await _run(call_with_relogin(telus, asyncio.Lock(), probe, hold=hold))
    except CloudHeld as exc:
        assert abs(exc.retry_after_s - 2 * c.CLOUD_HOLD_INITIAL_S) < 1e-6, exc.retry_after_s
    else:
        raise AssertionError("probe 403 must re-hold")
    assert hold.strikes == 2 and telus.logins == 2

    # Expired again: a probe that succeeds clears everything.
    clock.now += 2 * c.CLOUD_HOLD_INITIAL_S + 1
    good = Scripted({"events": []})
    assert await _run(call_with_relogin(telus, asyncio.Lock(), good, hold=hold)) == {"events": []}
    assert not hold.active and hold.strikes == 0
    print("  ok  403-after-login holds; nothing touches the site meanwhile; probe "
          "doubles or clears")


async def test_explicit_rate_limits_hold_whichever_shape_they_arrive_in():
    clock = Clock()
    hold = CloudHold(clock)
    telus = FakeTelus()

    # A raw 429 with Retry-After from a telus_rest-style call (raise_for_status).
    site = Scripted(_http_error(429, retry_after="120"))
    try:
        await _run(call_with_relogin(telus, asyncio.Lock(), site, hold=hold))
    except RateLimited as exc:
        assert not isinstance(exc, CloudHeld)
        assert exc.retry_after_s == 120.0, exc.retry_after_s
    else:
        raise AssertionError("a 429 must surface as RateLimited")
    assert abs(hold.remaining() - c.rate_limit_sleep_s(120)) < 1e-6
    assert telus.logins == 0, "a 429 is not a cookie problem; no login"
    hold.note_success()

    # A RateLimited already parsed by a module such as camera_settings.
    site = Scripted(RateLimited(90))
    try:
        await _run(call_with_relogin(telus, asyncio.Lock(), site, hold=hold))
    except RateLimited as exc:
        assert exc.retry_after_s == 90
    else:
        raise AssertionError("a parsed RateLimited must propagate")
    assert abs(hold.remaining() - c.rate_limit_sleep_s(90)) < 1e-6
    hold.note_success()

    # A 403, then the LOGIN itself is rate limited.
    class LimitedLogin(FakeTelus):
        async def login(self):
            raise RateLimited(101)

    site = Scripted(_http_error(403))
    try:
        await _run(call_with_relogin(LimitedLogin(), asyncio.Lock(), site, hold=hold))
    except RateLimited as exc:
        assert exc.retry_after_s == 101
    else:
        raise AssertionError("a rate-limited login must propagate")
    assert abs(hold.remaining() - c.rate_limit_sleep_s(101)) < 1e-6
    assert "login" in (hold.reason or "")
    hold.note_success()

    # A 429 on the RETRY after a successful login.
    site = Scripted(_http_error(403), _http_error(429, retry_after="33"))
    try:
        await _run(call_with_relogin(FakeTelus(), asyncio.Lock(), site, hold=hold))
    except RateLimited as exc:
        assert exc.retry_after_s == 33.0
    else:
        raise AssertionError("a 429 on the retry must surface as RateLimited")
    assert abs(hold.remaining() - c.rate_limit_sleep_s(33)) < 1e-6
    print("  ok  429 / parsed RateLimited / rate-limited login / 429-on-retry all hold "
          "for the server's wait")


async def test_other_errors_pass_through_untouched():
    clock = Clock()
    hold = CloudHold(clock)
    telus = FakeTelus()
    site = Scripted(_http_error(500))
    try:
        await _run(call_with_relogin(telus, asyncio.Lock(), site, hold=hold))
    except aiohttp.ClientResponseError as exc:
        assert exc.status == 500
    else:
        raise AssertionError("a 500 must propagate as-is")
    assert not hold.active and telus.logins == 0
    site = Scripted(_http_error(403), _http_error(500))
    try:
        await _run(call_with_relogin(telus, asyncio.Lock(), site, hold=hold))
    except aiohttp.ClientResponseError as exc:
        assert exc.status == 500
    else:
        raise AssertionError("a 500 on the retry must propagate as-is")
    assert not hold.active and telus.logins == 1
    print("  ok  non-auth, non-rate-limit errors pass through and never hold")


# ---------------------------------------------------------------------------
# The control routes answer a hold as 429, not 502
# ---------------------------------------------------------------------------

class RouteTelus:
    session = object()
    login_generation = 0

    async def login(self):
        raise AssertionError("no route under test may re-login")


@contextlib.asynccontextmanager
async def _client(events_fn):
    saved = cs.list_video_events
    cs.list_video_events = events_fn
    app = cs.create_app(RouteTelus(), asyncio.Lock(), {}, {}, None, None)
    headers = {"Authorization": f"Bearer {app[cs.APP_CURRENT_SECRET]['value']}"}
    try:
        async with TestClient(TestServer(app)) as client:
            yield client, headers
    finally:
        cs.list_video_events = saved


async def test_events_route_answers_429_with_the_holds_remaining_wait():
    hold = cloud_hold.HOLD
    hold.note_success()  # start clean whatever an earlier test left behind
    calls = Scripted({"never": "reached"})

    async def events(telus, limit):
        return await calls(telus, limit)

    async with _client(events) as (client, headers):
        hold.note_forbidden_after_relogin("list_video_events")
        try:
            resp = await client.get("/control/events", headers=headers)
            assert resp.status == 429, f"held call must be 429, got {resp.status}"
            body = await resp.json()
            assert body["error"] == "rate_limited"
            wait = int(resp.headers["Retry-After"])
            assert body["retry_after"] == wait
            assert 1 <= wait <= int(c.CLOUD_HOLD_INITIAL_S), wait
            assert calls.calls == 0, "the route must not reach the website while held"
        finally:
            hold.note_success()
        resp = await client.get("/control/events", headers=headers)
        assert resp.status == 200 and calls.calls == 1, (
            "once the hold is cleared the route serves the website again")
    print("  ok  /control/events answers 429 + Retry-After while held, 200 after")


async def test_a_403_that_survives_login_is_answered_429_not_502():
    hold = cloud_hold.HOLD
    hold.note_success()
    site = Scripted(_http_error(403), _http_error(403))

    class LoginTelus(RouteTelus):
        logins = 0

        async def login(self):
            type(self).logins += 1
            self.login_generation += 1

    saved = cs.list_history_events

    async def history(telus, limit):
        return await site(telus, limit)

    cs.list_history_events = history
    app = cs.create_app(LoginTelus(), asyncio.Lock(), {}, {}, None, None)
    headers = {"Authorization": f"Bearer {app[cs.APP_CURRENT_SECRET]['value']}"}
    try:
        async with TestClient(TestServer(app)) as client:
            resp = await client.get("/control/history", headers=headers)
            assert resp.status == 429, (
                f"a 403 after a fresh login used to be a 502 that sent HA straight "
                f"back; must be 429 now, got {resp.status}")
            assert "Retry-After" in resp.headers
            assert LoginTelus.logins == 1 and site.calls == 2
            assert hold.active and hold.strikes == 1
    finally:
        cs.list_history_events = saved
        hold.note_success()
    print("  ok  /control/history: 403 after a fresh login -> 429 + hold, not 502")


async def main() -> int:
    tests = [
        test_hold_is_inactive_until_something_happens,
        test_forbidden_after_relogin_doubles_to_the_cap_and_a_success_resets,
        test_rate_limited_holds_for_the_servers_wait_and_never_shortens,
        test_expired_cookie_costs_one_login_and_one_retry_and_no_hold,
        test_forbidden_after_login_holds_and_the_website_is_not_touched_meanwhile,
        test_explicit_rate_limits_hold_whichever_shape_they_arrive_in,
        test_other_errors_pass_through_untouched,
        test_events_route_answers_429_with_the_holds_remaining_wait,
        test_a_403_that_survives_login_is_answered_429_not_502,
    ]
    failures = 0
    for test in tests:
        try:
            result = test()
            if asyncio.iscoroutine(result):
                await result
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
