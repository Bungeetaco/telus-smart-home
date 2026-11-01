"""Tests for the websocket connect-failure log filter (`ws_log_filter.py`).

The invisible failure: nothing in production breaks when this filter is wrong.
If it is too loose, a genuine library bug loses its traceback and the next
"why did the socket die" investigation has one warning line to go on. If it is
too tight (or not installed), every failed connect writes a ~90-line
traceback with the websocket token in its URL into the HA log, as it did
before. Both are silent; only this suite notices.

Run:
    python3 tests/test_ws_log_filter.py
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))

import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.telus_smarthome_alarmdotcom import ws_log_filter  # noqa: E402


class ConnectionTimeoutError(Exception):   # same class NAME as aiohttp's, no aiohttp import
    pass


class WeirdLibraryError(Exception):
    pass


def _record(exc: BaseException | None, msg: str = "Unexpected WebSocket error", level=logging.ERROR):
    rec = logging.LogRecord(ws_log_filter.LIBRARY_LOGGER, level, __file__, 1, msg, (), None)
    if exc is not None:
        try:
            raise exc
        except BaseException as caught:  # noqa: BLE001
            rec.exc_info = (type(caught), caught, caught.__traceback__)
    return rec


def test_connect_failure_becomes_one_warning_line_without_the_url():
    flt = ws_log_filter.ConnectFailureFilter()
    rec = _record(ConnectionTimeoutError("Connection timeout to host wss://example/?auth=SECRET"))
    assert flt.filter(rec) is True, "records are rewritten, never dropped"
    assert rec.exc_info is None and rec.exc_text is None, "the traceback is gone"
    text = rec.getMessage()
    assert "SECRET" not in text and "wss://" not in text, text
    assert "ConnectionTimeoutError" in text, "the cause stays visible"
    assert rec.levelno == logging.WARNING
    print("  ok  a connect timeout is one WARNING line naming the cause, with no traceback and no URL")


def test_other_errors_keep_their_traceback():
    flt = ws_log_filter.ConnectFailureFilter()
    rec = _record(WeirdLibraryError("boom"))
    assert flt.filter(rec) is True
    assert rec.exc_info is not None and rec.levelno == logging.ERROR
    assert rec.getMessage() == "Unexpected WebSocket error"
    print("  ok  an unexpected library error passes through untouched, traceback and all")


def test_records_without_an_exception_pass_through():
    flt = ws_log_filter.ConnectFailureFilter()
    rec = _record(None, "plain message", logging.INFO)
    assert flt.filter(rec) is True and rec.getMessage() == "plain message" and rec.levelno == logging.INFO
    print("  ok  a record with no exception is left alone")


def test_install_is_idempotent_and_targets_the_library_logger():
    name = "test.ws_log_filter.logger"
    a = ws_log_filter.install(name)
    b = ws_log_filter.install(name)
    assert a is b
    assert [f for f in logging.getLogger(name).filters if isinstance(f, ws_log_filter.ConnectFailureFilter)] == [a]
    assert ws_log_filter.LIBRARY_LOGGER == "pyalarmdotcomajax.websockets.client"
    print("  ok  install() attaches exactly one filter to the library logger")


def main() -> int:
    failures = 0
    for test in (test_connect_failure_becomes_one_warning_line_without_the_url,
                 test_other_errors_keep_their_traceback,
                 test_records_without_an_exception_pass_through,
                 test_install_is_idempotent_and_targets_the_library_logger):
        try:
            test()
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print(f"RESULT: {'PASS' if not failures else f'FAIL ({failures})'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
