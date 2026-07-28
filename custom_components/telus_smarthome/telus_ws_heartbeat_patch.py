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

_orig_connect = WebSocketClient._connect


async def _connect_with_heartbeat(self):
    session = self._websession
    if not getattr(session, "_telus_hb_patched", False):
        orig_ws_connect = session.ws_connect

        def ws_connect_with_heartbeat(*args, **kwargs):
            kwargs.setdefault("heartbeat", HEARTBEAT_SECONDS)
            return orig_ws_connect(*args, **kwargs)

        session.ws_connect = ws_connect_with_heartbeat
        session._telus_hb_patched = True
        log.debug("Websocket heartbeat patch applied (%ss)", HEARTBEAT_SECONDS)
    return await _orig_connect(self)


WebSocketClient._connect = _connect_with_heartbeat
