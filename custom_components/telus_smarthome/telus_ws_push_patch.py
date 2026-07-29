"""Keep the Alarm.com websocket alive and make its pushes reach HA entities.

Two defects sat between a door opening and Home Assistant showing it:

1. A library crash killed the connection. pyalarmdotcomajax's
   `process_raw_message()` handles an unrecognised device by logging inside
   `except UnkonwnDevice:` and then falling through to
   `EventMessage(message, device)` - where `device` was never assigned. The
   resulting UnboundLocalError propagates out of `_async_handle_message`, out
   of the `async for msg in websocket` loop, and drops the socket. Observed
   live 2026-07-29: an event from the bridge-served doorbell camera
   (111937238-2048, absent from the library's registry) took the websocket
   down 18s after it connected, and it stayed down until the reconnect timer.
   Because the doorbell emits events regularly, real-time updates were dead
   much of the time and sensor states only refreshed on the REST poll.

2. Nothing refreshed HA after a push. The library applies each message to its
   in-memory device objects, and this integration's entities read those live
   (BaseDevice keeps the reference; binary_sensor reads `_device.state`), but
   no listener was ever notified, so even a successfully-processed push was
   invisible until the next poll.

Wrapping `_async_handle_message` fixes both: swallow per-message errors so one
unprocessable message cannot end the connection, then notify each registered
coordinator's listeners. Notifying re-reads objects already in memory, so this
issues no additional API requests and cannot contribute to rate limiting.

Same import-time patching model as telus_auth_patch / telus_ws_heartbeat_patch,
so it survives HA Core updates.
"""
from __future__ import annotations

import logging
import weakref

from pyalarmdotcomajax.websockets.client import WebSocketClient

log = logging.getLogger(__name__)

# Weak refs so a reloaded/removed config entry's coordinator can be collected.
_coordinators: weakref.WeakSet = weakref.WeakSet()


def register_coordinator(coordinator) -> None:
    """Register a DataUpdateCoordinator to refresh on each websocket push."""
    _coordinators.add(coordinator)


_orig_handle_message = WebSocketClient._async_handle_message


async def _handle_message_and_notify(self, message):
    result = None
    try:
        result = await _orig_handle_message(self, message)
    except Exception:  # noqa: BLE001
        # Never let one bad message tear down the socket - that is the library
        # bug described above, and losing the connection costs every subsequent
        # real-time update until the reconnect timer fires.
        log.debug("Ignoring unprocessable websocket message: %s", str(message)[:300])

    for coordinator in list(_coordinators):
        try:
            # Listeners re-read the library device objects updated above.
            coordinator.async_update_listeners()
        except Exception:  # noqa: BLE001
            log.exception("Failed to notify listeners after a websocket push")
    return result


WebSocketClient._async_handle_message = _handle_message_and_notify

log.info("Websocket resilience + HA push-refresh patch applied")
