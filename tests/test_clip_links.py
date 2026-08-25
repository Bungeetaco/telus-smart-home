"""Tests for the signed clip links in `clip_view.py` (backlog E66).

Run from the repository root:

    python3 tests/test_clip_links.py

No Home Assistant, no network, no bridge: `tests/ha_stubs.py` supplies the
`homeassistant.*` names, the bridge call is a scripted stand-in, and the REAL
`clip_view.py` code runs, minting and verifying real HMACs.

Why this file exists: **this endpoint is the one unauthenticated,
internet-reachable surface the integration adds**, and every property that
makes that acceptable is invisible when it silently stops holding. The links
ride inside already-delivered phone notifications, `requires_auth` is False by
design (a tap opens in a plain browser), and event ids are near-sequential --
so the HMAC, the expiry bound and the log-injection guard are the entire
difference between "a notification's tap re-mints one clip" and "anyone can
enumerate the clip archive of a security camera". A regression in any of them
produces no error anywhere: the view keeps answering, just to more people than
it should.

Pinned here:

  1. round trip      -- a link minted by make_clip_link redirects (302) to the
                        freshly re-minted URL, with cache/referrer hygiene
                        headers on EVERY response, success and failure alike.
  2. the signature   -- a valid signature for one id authorises nothing about
                        another id; a client cannot extend its own link's
                        expiry (the expiry is bound INTO the mac); a non-ASCII
                        signature is a clean 401, never a 500 (compare_digest
                        raises TypeError on such input); a second config
                        entry's secret still validates.
  3. the expiry      -- an expired link with a VALID signature is a 410, and
                        the 401-vs-410 split is only reachable WITH a valid
                        mac (expiry is checked after the signature), so the
                        status code leaks nothing to an enumerator.
  4. input hygiene   -- an event id outside the conservative shape is refused
                        before any HMAC work, and the rejection log line does
                        not echo the attacker-controlled bytes.
  5. bridge failures -- each failure shape maps to the documented status (404
                        gone, 504 slow re-login, 503 unreachable/starting/
                        shape-drift) rather than escaping as a 500 traceback
                        on an unauthenticated endpoint.
"""
import asyncio
import logging
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

import aiohttp  # noqa: E402

from custom_components.telus_smarthome import clip_view  # noqa: E402
from custom_components.telus_smarthome.clip_view import (  # noqa: E402
    TelusClipRedirectView,
    clip_link_sig,
    make_clip_link,
)
from custom_components.telus_smarthome.const import (  # noqa: E402
    CONF_CLIP_LINK_SECRET,
    DOMAIN,
)

SECRET = "test-secret-0123456789abcdef"
FUTURE = 4_000_000_000     # far future; the code compares against utcnow()
PAST = 1_000_000_000


def make_view(secrets=(SECRET,), bridge=None):
    """The REAL view over a fake hass carrying config entries with `secrets`.

    `bridge` replaces clip_view.bridge_get for the test: an async callable, or
    an exception instance to raise, or a plain dict/None to return.
    """
    entries = [types.SimpleNamespace(data={CONF_CLIP_LINK_SECRET: s}) for s in secrets]
    hass = types.SimpleNamespace(
        config_entries=types.SimpleNamespace(async_entries=lambda domain: entries))
    view = TelusClipRedirectView(hass)

    async def scripted_bridge(hass_, url, timeout=None):
        if isinstance(bridge, BaseException):
            raise bridge
        return bridge

    view._test_bridge = scripted_bridge
    return view


class _PatchedBridge:
    def __init__(self, view):
        self._view = view

    def __enter__(self):
        self._saved = clip_view.bridge_get
        clip_view.bridge_get = self._view._test_bridge

    def __exit__(self, *exc):
        clip_view.bridge_get = self._saved


def request_for(link: str):
    """A stand-in aiohttp request for a minted link (the view reads only
    `.query`), plus the path's event id, parsed the way aiohttp routing would."""
    path, _, qs = link.partition("?")
    query = dict(pair.split("=", 1) for pair in qs.split("&")) if qs else {}
    event_id = path.rsplit("/", 1)[-1]
    return types.SimpleNamespace(query=query), event_id


async def fetch(view, link=None, event_id=None, query=None):
    if link is not None:
        request, event_id = request_for(link)
    else:
        request = types.SimpleNamespace(query=query or {})
    with _PatchedBridge(view):
        return await view.get(request, event_id)


def assert_hygiene(response):
    assert response.headers.get("Cache-Control") == "no-store", (
        f"missing no-store on a {response.status}: a shared cache may retain "
        "the freshly-minted alarm.com URL")
    assert response.headers.get("Referrer-Policy") == "no-referrer", (
        f"missing no-referrer on a {response.status}: the signed link leaks "
        "onward as a referrer")


async def test_a_minted_link_round_trips_to_a_redirect():
    view = make_view(bridge={"download_url": "https://relay.example/clip?tok=fresh"})
    link = make_clip_link(SECRET, "12345", FUTURE)
    response = await fetch(view, link)
    assert response.status == 302, f"a valid, unexpired link answered {response.status}"
    assert response.headers["Location"] == "https://relay.example/clip?tok=fresh"
    assert_hygiene(response)


async def test_a_signature_authorises_exactly_one_id():
    view = make_view(bridge={"download_url": "https://x/"})
    sig_for_123 = clip_link_sig(SECRET, "123", FUTURE)
    response = await fetch(
        view, event_id="124",
        query={"exp": str(FUTURE), "sig": sig_for_123})
    assert response.status == 401, (
        f"a signature minted for one event id was accepted for its neighbour "
        f"({response.status}) -- ids are near-sequential, so this is archive "
        "enumeration")
    assert_hygiene(response)


async def test_a_client_cannot_extend_its_own_links_life():
    view = make_view(bridge={"download_url": "https://x/"})
    link = make_clip_link(SECRET, "123", PAST)      # honestly expired
    request, event_id = request_for(link)
    request.query["exp"] = str(FUTURE)              # ...stretched by the client
    response = await fetch(view, event_id=event_id, query=request.query)
    assert response.status == 401, (
        f"an expiry edited after minting was honoured ({response.status}) -- "
        "the expiry must be bound INTO the mac, or a leaked link is a bearer "
        "token forever")


async def test_expired_with_valid_mac_is_410_and_without_is_401():
    view = make_view(bridge={"download_url": "https://x/"})
    honest = make_clip_link(SECRET, "123", PAST)
    response = await fetch(view, honest)
    assert response.status == 410, (
        f"an expired but honestly-signed link answered {response.status}, "
        "not the tap-an-old-notification 410")
    assert_hygiene(response)

    response = await fetch(view, event_id="123",
                           query={"exp": str(PAST), "sig": "0" * 64})
    assert response.status == 401, (
        f"expiry was evaluated before the signature ({response.status}) -- "
        "the 401-vs-410 split must not be reachable without a valid mac")


async def test_garbage_signature_bytes_are_a_401_not_a_500():
    view = make_view(bridge={"download_url": "https://x/"})
    response = await fetch(view, event_id="123",
                           query={"exp": str(FUTURE), "sig": "café"})
    assert response.status == 401, (
        f"a non-ASCII signature answered {response.status} -- "
        "hmac.compare_digest raises TypeError on such input, and unguarded "
        "that is a scriptable 500 + traceback on an unauthenticated endpoint")


async def test_any_configured_entrys_secret_validates():
    view = make_view(secrets=("other-install-secret", SECRET),
                     bridge={"download_url": "https://x/"})
    link = make_clip_link(SECRET, "123", FUTURE)
    response = await fetch(view, link)
    assert response.status == 302, (
        f"a link signed with the second entry's secret answered "
        f"{response.status} -- an entry reload must not orphan delivered links")


async def test_a_malformed_event_id_is_refused_without_echoing_it():
    view = make_view(bridge={"download_url": "https://x/"})
    hostile = "999\r\n2026-01-01 ERROR forged line \x1b[31m"
    records = []
    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())
    clip_view.LOGGER.addHandler(handler)
    try:
        response = await fetch(view, event_id=hostile,
                               query={"exp": str(FUTURE),
                                      "sig": clip_link_sig(SECRET, hostile, FUTURE)})
    finally:
        clip_view.LOGGER.removeHandler(handler)
    assert response.status == 401, (
        f"an event id outside the conservative shape answered {response.status}")
    assert records, "the rejection was silent -- an attack surface with no trace"
    assert all("forged line" not in message and "\r" not in message
               and "\x1b" not in message for message in records), (
        f"the rejection log echoed attacker-controlled bytes: {records!r} -- "
        "that is the unauthenticated log injection the id shape guard exists "
        "to prevent")


async def test_bridge_failures_map_to_their_documented_statuses():
    link = make_clip_link(SECRET, "123", FUTURE)
    cases = [
        (aiohttp.ClientResponseError(request_info=None, history=(), status=404),
         404, "a clip alarm.com no longer has"),
        (asyncio.TimeoutError(), 504, "a slow re-login"),
        (aiohttp.ClientConnectionError("refused"), 503, "an unreachable bridge"),
        (None, 503, "a bridge still writing its secret"),
        ({"unexpected": "shape"}, 503, "a response with no download_url"),
    ]
    for bridge, expected, why in cases:
        view = make_view(bridge=bridge)
        response = await fetch(view, link)
        assert response.status == expected, (
            f"{why} answered {response.status}, expected {expected} -- an "
            "unauthenticated endpoint must map failures, not traceback them")
        assert_hygiene(response)


async def main():
    failures = 0
    for test in (
        test_a_minted_link_round_trips_to_a_redirect,
        test_a_signature_authorises_exactly_one_id,
        test_a_client_cannot_extend_its_own_links_life,
        test_expired_with_valid_mac_is_410_and_without_is_401,
        test_garbage_signature_bytes_are_a_401_not_a_500,
        test_any_configured_entrys_secret_validates,
        test_a_malformed_event_id_is_refused_without_echoing_it,
        test_bridge_failures_map_to_their_documented_statuses,
    ):
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
