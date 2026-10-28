"""Monkeypatch pyalarmdotcomajax's websocket client to send keepalive pings.

The library calls `ClientSession.ws_connect()` with no `heartbeat`, so the
connection carries zero traffic between pushed events. Alarm.com's server
drops idle websockets after ~5 minutes (observed live: RUNNING at :45:45,
DISCONNECTED at :50:47), after which the integration waits a further
`ws_reconnect_timeout` before reconnecting - leaving push dead half the time
and state changes stuck behind the slow REST poll.

aiohttp's `heartbeat=N` sends a WS ping every N seconds and fails fast if the
pong doesn't arrive, which both defeats the idle timeout and detects dead
connections promptly. 55s keeps us far inside the observed 5-minute window.

Same persistence model as telus_auth_patch: patches the library at import
time from custom_components, so it survives HA Core container updates.
"""
from __future__ import annotations

import logging

from pyalarmdotcomajax.websockets.client import WebSocketClient

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 55

# Compatibility guard -- this patch replaces a PRIVATE coroutine on the library's
# websocket client. Fail loudly (naming what moved) rather than dying with a bare
# AttributeError at HA startup if a pyalarmdotcomajax bump renames it. Exercised
# by the CI smoke test in .github/scripts/check_patch.py.
if not hasattr(WebSocketClient, "_connect"):
    raise RuntimeError(
        "Websocket heartbeat patch is incompatible with the installed "
        "pyalarmdotcomajax: WebSocketClient._connect is missing. Review the "
        "upstream changes before bumping (see AGENTS.md / docs/development.md)."
    )

_orig_connect = WebSocketClient._connect


class _HeartbeatSession:
    """Transparent proxy around the aiohttp ClientSession that injects
    ``heartbeat=`` into ws_connect().

    We deliberately do NOT set attributes on the ClientSession itself: aiohttp's
    ``ClientSession.__setattr__`` emits a DeprecationWarning for any name outside
    its ``ATTRS`` (so the previous ``session.ws_connect = ...`` +
    ``session._telus_hb_patched = True`` produced two warnings per session), and
    that mutation surface is slated to close in aiohttp 4.0. Proxying instead
    keeps the change entirely on the library object we already patch.

    Everything except ws_connect delegates to the real session. The library only
    ever calls methods on it (``self._websession.ws_connect(...)`` /
    ``.get(...)``) and uses the RESULT as the async context manager, so simple
    __getattr__ delegation is sufficient.
    """

    __slots__ = ("_session",)

    def __init__(self, session):
        object.__setattr__(self, "_session", session)

    def __getattr__(self, name):
        return getattr(self._session, name)

    def ws_connect(self, *args, **kwargs):
        kwargs.setdefault("heartbeat", HEARTBEAT_SECONDS)
        return self._session.ws_connect(*args, **kwargs)


async def _connect_with_heartbeat(self):
    if not isinstance(self._websession, _HeartbeatSession):
        self._websession = _HeartbeatSession(self._websession)
        log.debug("Websocket heartbeat patch applied (%ss)", HEARTBEAT_SECONDS)
    return await _orig_connect(self)


WebSocketClient._connect = _connect_with_heartbeat

# NOTE (2026-07-29): the live portal connects to
#   wss://webskt.alarm.com:8443/?f=1&auth=<token>&ver=A
# while the library uses only `?auth=<token>`. Mirroring the portal's form was
# tried and REJECTED by the server: the socket reached RUNNING and closed 1ms
# later, every time, looping on the reconnect timer. The portal's extra params
# evidently pair with something else in a browser session (token type, headers,
# or cookies), so do not "fix" the endpoint to match the capture.
