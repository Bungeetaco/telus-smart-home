"""Tests for the credentials.json fallback reader and watcher (bridge/main.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_credentials_fallback.py

No network and no live account. CREDENTIALS_FALLBACK_PATH is redirected into a
temp directory at import time (same pattern as test_control_server_routes.py's
control-secret redirect), so this never reads or rewrites a real install's
credentials file.

The invisible production failure this suite exists to catch: credentials.json
lives on a bind mount shared with the Home Assistant container, so a torn write
(invalid UTF-8 mid-file) or a wrong-shape file (valid JSON that is not an
object) is a real possibility -- and neither is a JSONDecodeError.
json.load on a text-mode file with invalid bytes raises UnicodeDecodeError,
which is a ValueError but NOT a JSONDecodeError; a valid-JSON `[]` sails past
json.load and blows up on the very next attribute access. Uncaught, either one
kills the process (at startup, before any login; or mid-run, by killing the
watcher task and with it the bare asyncio.gather main() blocks on) -- and the
bad file is still there on restart, so Docker's `restart: unless-stopped`
re-runs a fresh SSO login against the Cloudflare-fronted auth gateway on every
lap. That is the documented multi-hour-outage shape from _login_with_retry's
own comments, triggered by nothing more than a half-written file.
"""
import asyncio
import json
import logging
import os
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c  # noqa: E402

# Redirected BEFORE bridge.main is ever exercised: these tests write corrupt
# files to the fallback path on purpose, and clobbering a real install's
# credentials.json would take its self-heal path down with it.
_TMP = tempfile.mkdtemp(prefix="telus-creds-test-")
c.SHARED_DIR = _TMP
c.CREDENTIALS_FALLBACK_PATH = os.path.join(_TMP, "credentials.json")

from bridge import main as m  # noqa: E402

# Every test here provokes the unreadable/corrupt warning path on purpose, and
# each one logs with exc_info=True. Left alone, a PASSING run prints tracebacks
# that read like failures. Nothing asserts on log output, so the logger is
# muted rather than the assertions weakened.
logging.getLogger("bridge.main").setLevel(logging.CRITICAL)


def _write_fallback(payload: bytes) -> None:
    with open(c.CREDENTIALS_FALLBACK_PATH, "wb") as f:
        f.write(payload)


def _remove_fallback() -> None:
    try:
        os.remove(c.CREDENTIALS_FALLBACK_PATH)
    except FileNotFoundError:
        pass


class FakeTelusSession:
    """The watcher only reaches login() after adopting NEW credentials, which
    no scenario here provides -- so any login attempt is itself a failure."""

    async def login(self):
        raise AssertionError("the watcher must not re-login in these scenarios")


def test_startup_reader_survives_a_torn_write():
    """Invalid UTF-8 (a torn write) must not raise out of the startup reader.

    UnicodeDecodeError is a ValueError but NOT a JSONDecodeError, so a handler
    catching only json.JSONDecodeError lets it kill the process before login.
    """
    saved = (c.TELUS_EMAIL, c.TELUS_PASSWORD)
    try:
        _write_fallback(b"\xff\xfe{")
        marker = m._load_fallback_credentials_if_present()
        assert marker is None, (
            f"a corrupt file must read as 'no fallback', got marker {marker!r}")
        assert (c.TELUS_EMAIL, c.TELUS_PASSWORD) == saved, (
            "the .env credentials must be left untouched by a corrupt file")
    finally:
        c.TELUS_EMAIL, c.TELUS_PASSWORD = saved
        _remove_fallback()
    print("  ok  startup reader: invalid UTF-8 returns None and leaves the "
          ".env credentials alone")


def test_startup_reader_survives_a_non_object_file():
    """Valid JSON of the wrong shape (`[]`) is as likely as a truncated file."""
    saved = (c.TELUS_EMAIL, c.TELUS_PASSWORD)
    try:
        _write_fallback(b"[]")
        marker = m._load_fallback_credentials_if_present()
        assert marker is None, (
            f"a non-object file must read as 'no fallback', got marker {marker!r}")
        assert (c.TELUS_EMAIL, c.TELUS_PASSWORD) == saved, (
            "the .env credentials must be left untouched by a wrong-shape file")
    finally:
        c.TELUS_EMAIL, c.TELUS_PASSWORD = saved
        _remove_fallback()
    print("  ok  startup reader: a valid-JSON non-object returns None and "
          "leaves the .env credentials alone")


def test_startup_reader_still_adopts_a_good_file():
    """The happy path, pinned so the broadened handler can't over-catch it."""
    saved = (c.TELUS_EMAIL, c.TELUS_PASSWORD)
    try:
        _write_fallback(json.dumps({"username": " fallback@example.invalid ",
                                    "password": " hunter-two ",
                                    "updated_at": "2026-08-23T00:00:00Z"}).encode())
        marker = m._load_fallback_credentials_if_present()
        assert marker == "2026-08-23T00:00:00Z", (
            f"a valid file must return its updated_at marker, got {marker!r}")
        assert (c.TELUS_EMAIL, c.TELUS_PASSWORD) == (
            "fallback@example.invalid", "hunter-two"), (
            "a valid file must override the .env credentials (stripped): "
            f"{(c.TELUS_EMAIL, c.TELUS_PASSWORD)}")
    finally:
        c.TELUS_EMAIL, c.TELUS_PASSWORD = saved
        _remove_fallback()
    print("  ok  startup reader: a valid file is applied and its updated_at "
          "marker returned")


async def test_watcher_survives_corrupt_and_wrong_shape_files():
    """The watcher task must outlive both corruption shapes.

    main() blocks on a bare asyncio.gather over this task, so the watcher dying
    IS the process dying -- into the restart loop the docstring describes.
    Alive vs dead is the assertion precisely because it is a far-apart margin:
    the unfixed watcher dies on its first poll cycle, the fixed one just keeps
    logging and polling.
    """
    saved_poll = c.CREDENTIALS_FALLBACK_POLL_S
    saved = (c.TELUS_EMAIL, c.TELUS_PASSWORD)
    c.CREDENTIALS_FALLBACK_POLL_S = 0.01
    task = asyncio.ensure_future(
        m.watch_credentials_fallback(FakeTelusSession(), asyncio.Lock(), None))
    try:
        _write_fallback(b"\xff\xfe{")
        await asyncio.sleep(0.2)        # ~20 poll cycles over the corrupt file
        assert not task.done(), (
            "the watcher died on an invalid-UTF-8 file: "
            f"{task.exception()!r} -- with it dies the gather and the process")

        _write_fallback(b"[]")
        await asyncio.sleep(0.2)        # ~20 more cycles over the non-object file
        assert not task.done(), (
            "the watcher died on a valid-JSON non-object file: "
            f"{task.exception()!r} -- data.get() ran before any shape check")
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:  # noqa: BLE001
            # A task that already died re-raises its own error here; the
            # assertion above has reported it, so don't let it mask that.
            pass
        c.CREDENTIALS_FALLBACK_POLL_S = saved_poll
        c.TELUS_EMAIL, c.TELUS_PASSWORD = saved
        _remove_fallback()
    print("  ok  watcher: stays alive across invalid UTF-8 and non-object "
          "files instead of taking the process down")


async def main():
    failures = 0
    tests = (test_startup_reader_survives_a_torn_write,
             test_startup_reader_survives_a_non_object_file,
             test_startup_reader_still_adopts_a_good_file,
             test_watcher_survives_corrupt_and_wrong_shape_files)
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
