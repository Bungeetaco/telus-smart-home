"""Turn the library's websocket connect-failure tracebacks into one warning line.

`pyalarmdotcomajax.websockets.client` logs every failed connect with
`log.exception(...)`: a ~90-line aiohttp traceback whose message embeds the
websocket URL, and that URL carries the freshly fetched `?auth=` token. With
connects failing in streaks (backlog E106) that is dozens of tracebacks a night
and as many token copies in a world-readable log, for a condition the
controller already reports in one line ("Websocket attempt N did not reach
RUNNING"). This filter rewrites such records in place: no traceback, no URL,
the exception class name kept so the cause stays visible. Anything that is not
a connect-phase transport failure passes through untouched -- an unexpected
library error must still show its traceback.

Pure module (no aiohttp or library import) so the test harness can pin it.
"""
from __future__ import annotations

import logging

# Connect-phase transport failures: the socket never came up, nothing to debug
# in a traceback. Matched by class name so this module imports nothing.
CONNECT_FAILURES = frozenset({
    "ConnectionTimeoutError", "ClientConnectorError", "ClientConnectorDNSError",
    "ServerDisconnectedError", "ClientOSError", "TimeoutError", "ConnectionResetError",
})
LIBRARY_LOGGER = "pyalarmdotcomajax.websockets.client"
NOTE = "websocket connect failed (%s); the controller retries -- see its next line for the delay"


def _cause_name(record: logging.LogRecord) -> str | None:
    info = record.exc_info
    if not info or not isinstance(info, tuple) or len(info) < 2 or info[1] is None:
        return None
    return type(info[1]).__name__


class ConnectFailureFilter(logging.Filter):
    """Rewrite (never drop) connect-failure records: one line, no traceback, no URL."""

    def filter(self, record: logging.LogRecord) -> bool:
        cause = _cause_name(record)
        if cause in CONNECT_FAILURES:
            record.exc_info = None
            record.exc_text = None
            record.msg = NOTE
            record.args = (cause,)
            record.levelno = logging.WARNING
            record.levelname = "WARNING"
        return True


def install(logger_name: str = LIBRARY_LOGGER) -> ConnectFailureFilter:
    """Attach the filter to the library logger once (idempotent)."""
    logger = logging.getLogger(logger_name)
    for existing in logger.filters:
        if isinstance(existing, ConnectFailureFilter):
            return existing
    flt = ConnectFailureFilter()
    logger.addFilter(flt)
    return flt
