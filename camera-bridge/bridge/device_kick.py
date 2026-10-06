"""Device-side escalation for a camera whose cloud publish leg has wedged.

The bridge can prove when a fault is at the device rather than in this process
or the cloud path: a session completes ICE and gets ``webrtcup``, then not one
media frame arrives, while ANOTHER camera keeps streaming through the same
process (``camera_session._peer_still_delivering``). Every recovery lever the
bridge has -- reconnect, rotation, the restart hatch -- reaches only this side
of that path, and a camera in this state has been observed to stay there for
22+ hours while answering every other cloud call normally (backlog E103).

The one lever that reaches the device through the cloud is its own settings
API. ``cycle_live_resolution`` writes the live stream resolution to a DIFFERENT
offered option and then back to the current one: an encoder reconfigure that
may restart the device's publisher. Whether it does is NOT VERIFIED on any
device -- the wedge has not recurred since this was written -- which is why
``config.DEVICE_KICK_ENABLED`` is off by default and the loop applies it at
most once per ``DEVICE_KICK_COOLDOWN_S``. What is verified is the write itself
(the same read-modify-write the settings control route uses) and that the
original value is restored.

Failure handling, because this writes to a device in someone's home:

* A camera that offers fewer than two resolution options, or whose document
  says the option is not settable, raises ``DeviceKickUnsupported`` before any
  write, so an unsupported device is never touched.
* If the first write fails nothing has changed and the error propagates.
* If the RESTORE write fails the camera has been left at the alternate
  resolution. That is logged at CRITICAL with both values, the error carries
  them too, and the caller's counters record it: the one thing worse than a
  wedged camera is a silently re-configured one.

Called through ``cloud_hold.call_with_relogin`` like every website call, so a
held edge refuses the kick rather than the kick walking into the block.
"""
from __future__ import annotations

import logging

from . import camera_settings

log = logging.getLogger(__name__)


class DeviceKickUnsupported(RuntimeError):
    """The camera offers nothing the kick can cycle; nothing was written."""


class DeviceKickRestoreFailed(RuntimeError):
    """The alternate value was written and the restore failed: the camera is
    LEFT at ``left_at``, not at ``wanted``."""

    def __init__(self, camera_id: str, wanted: str, left_at: str, cause: BaseException):
        self.camera_id = camera_id
        self.wanted = wanted
        self.left_at = left_at
        super().__init__(f"{camera_id}: live resolution restore to {wanted!r} failed; "
                         f"camera LEFT at {left_at!r}: {cause!r}")


def choose_alternate(current, options) -> str | None:
    """The option to cycle through, or None when there is nothing to cycle.

    The first offered option that is not the current one, in the device's own
    order. Deterministic so a repeated kick does the same thing, and None (not
    the current value) when the list has one entry or does not contain a
    different value -- writing the current value back is not a cycle.
    """
    if current is None or not options:
        return None
    for option in options:
        if option != current:
            return str(option)
    return None


async def cycle_live_resolution(telus, camera_id: str) -> dict:
    """Write the live resolution to another offered option, then restore it.

    Returns ``{"restored": <original>, "via": <alternate>}``. Raises
    ``DeviceKickUnsupported`` (nothing written), the first write's own error
    (nothing changed), or ``DeviceKickRestoreFailed`` (camera left at the
    alternate value).
    """
    config = await camera_settings.get_video_config(telus, camera_id)
    current = config.get("live_resolution")
    options = config.get("live_resolution_options") or []
    if not config.get("supports_live_resolution"):
        raise DeviceKickUnsupported(f"{camera_id}: live resolution is not settable on this camera")
    alternate = choose_alternate(current, options)
    if alternate is None:
        raise DeviceKickUnsupported(
            f"{camera_id}: no alternate live resolution to cycle through "
            f"(current={current!r}, options={options!r})")

    log.warning("%s: device kick: cycling live resolution %r -> %r -> %r",
                camera_id, current, alternate, current)
    await camera_settings.set_video_config(telus, camera_id, live_resolution=alternate)
    try:
        await camera_settings.set_video_config(telus, camera_id, live_resolution=current)
    except Exception as exc:  # noqa: BLE001 -- every failure here must be named
        log.critical("%s: device kick wrote live resolution %r and could NOT restore %r; "
                     "the camera is LEFT at %r. Restore it from the settings route or "
                     "the vendor app.", camera_id, alternate, current, alternate)
        raise DeviceKickRestoreFailed(camera_id, str(current), alternate, exc) from exc
    log.warning("%s: device kick: live resolution restored to %r", camera_id, current)
    return {"restored": current, "via": alternate}
