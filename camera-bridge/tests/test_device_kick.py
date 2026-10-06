"""Tests for the device-side escalation (bridge/device_kick.py).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_device_kick.py

No network and no live account: `camera_settings.get_video_config` and
`set_video_config` are replaced with scripted stand-ins that record every
write they are asked for.

The invisible failure this file exists to catch: the kick WRITES a setting on
a device in someone's home, and the two ways that goes wrong are both silent
in production. A kick that fires on a camera with nothing to cycle would write
the current value back (a no-op that reads as "kicked"), and a restore that
fails would leave the camera at the alternate resolution with nothing in the
status but a kick count. So what is pinned is exactly what is written:

  1. The cycle is two writes, alternate then original, in that order, and the
     original is whatever the device reported -- never a guess.
  2. A camera that offers fewer than two options, or says the option is not
     settable, gets ZERO writes and a `DeviceKickUnsupported`.
  3. A failed first write changes nothing (one write attempted, the error
     propagates as-is). A failed RESTORE raises `DeviceKickRestoreFailed`
     naming both values, after exactly two writes.
  4. The alternate choice is deterministic: the first offered option that is
     not the current one, so a repeated kick does the same thing.
"""
import asyncio
import logging
import os
import pathlib
import sys
import types

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import device_kick  # noqa: E402
from bridge.device_kick import (  # noqa: E402
    DeviceKickRestoreFailed,
    DeviceKickUnsupported,
    choose_alternate,
    cycle_live_resolution,
)

logging.getLogger("bridge.device_kick").setLevel(logging.CRITICAL)

CAM = "12345-2048"


class FakeSettings:
    """A scripted camera_settings: one read answer, writes recorded, and an
    optional error for the Nth write (1-based)."""

    def __init__(self, config, fail_write=None, error=None):
        self.config = config
        self.writes = []
        self.fail_write = fail_write
        self.error = error or RuntimeError("TELUS returned HTTP 500")

    async def get_video_config(self, _telus, camera_id):
        assert camera_id == CAM
        return dict(self.config)

    async def set_video_config(self, _telus, camera_id, **fields):
        assert camera_id == CAM
        self.writes.append(dict(fields))
        if self.fail_write == len(self.writes):
            raise self.error
        return {"live_resolution": fields.get("live_resolution")}


def _install(fake):
    saved = device_kick.camera_settings
    device_kick.camera_settings = types.SimpleNamespace(
        get_video_config=fake.get_video_config, set_video_config=fake.set_video_config)
    return lambda: setattr(device_kick, "camera_settings", saved)


OFFERED = {"supports_live_resolution": True, "live_resolution": "1440x1440",
           "live_resolution_options": ["480x480", "960x960", "1440x1440"]}


def test_alternate_is_the_first_other_option_and_none_when_there_is_none():
    assert choose_alternate("1440x1440", ["480x480", "960x960", "1440x1440"]) == "480x480"
    assert choose_alternate("480x480", ["480x480", "960x960"]) == "960x960"
    assert choose_alternate("960x960", ["960x960"]) is None, "one option is nothing to cycle"
    assert choose_alternate("960x960", []) is None
    assert choose_alternate(None, ["480x480"]) is None, "no current value: nothing to restore to"
    assert choose_alternate("960x960", ["960x960", "960x960"]) is None, (
        "a list that only repeats the current value is nothing to cycle")
    print("  ok  alternate: first other option in device order; None when nothing differs")


async def test_cycle_writes_alternate_then_original():
    fake = FakeSettings(OFFERED)
    restore = _install(fake)
    try:
        result = await cycle_live_resolution(object(), CAM)
    finally:
        restore()
    assert fake.writes == [{"live_resolution": "480x480"}, {"live_resolution": "1440x1440"}], (
        f"the cycle must be exactly alternate-then-original: {fake.writes}")
    assert result == {"restored": "1440x1440", "via": "480x480"}
    print("  ok  cycle: writes the alternate, then restores the device's own value")


async def test_unsupported_devices_are_never_written():
    cases = [
        ("not settable", {**OFFERED, "supports_live_resolution": False}),
        ("one option", {**OFFERED, "live_resolution_options": ["1440x1440"]}),
        ("no options", {**OFFERED, "live_resolution_options": None}),
        ("no current value", {**OFFERED, "live_resolution": None}),
    ]
    for label, config in cases:
        fake = FakeSettings(config)
        restore = _install(fake)
        try:
            try:
                await cycle_live_resolution(object(), CAM)
            except DeviceKickUnsupported:
                pass
            else:
                raise AssertionError(f"{label}: must raise DeviceKickUnsupported")
        finally:
            restore()
        assert fake.writes == [], f"{label}: an unsupported device must get ZERO writes"
    print("  ok  unsupported: not settable / one option / no options / no value -> no write")


async def test_a_failed_first_write_changes_nothing():
    fake = FakeSettings(OFFERED, fail_write=1)
    restore = _install(fake)
    try:
        try:
            await cycle_live_resolution(object(), CAM)
        except RuntimeError as exc:
            assert not isinstance(exc, DeviceKickRestoreFailed)
            assert "500" in str(exc)
        else:
            raise AssertionError("the first write's error must propagate")
    finally:
        restore()
    assert len(fake.writes) == 1, "no restore is attempted when nothing was changed"
    print("  ok  failed first write: one attempt, error propagates, nothing to restore")


async def test_a_failed_restore_names_what_the_camera_was_left_at():
    fake = FakeSettings(OFFERED, fail_write=2)
    restore = _install(fake)
    try:
        try:
            await cycle_live_resolution(object(), CAM)
        except DeviceKickRestoreFailed as exc:
            assert exc.camera_id == CAM
            assert exc.wanted == "1440x1440" and exc.left_at == "480x480"
            assert "LEFT at '480x480'" in str(exc), str(exc)
            assert exc.__cause__ is fake.error
        else:
            raise AssertionError("a failed restore must raise DeviceKickRestoreFailed")
    finally:
        restore()
    assert len(fake.writes) == 2
    print("  ok  failed restore: raises with wanted/left_at, after exactly two writes")


TESTS = [
    test_alternate_is_the_first_other_option_and_none_when_there_is_none,
    test_cycle_writes_alternate_then_original,
    test_unsupported_devices_are_never_written,
    test_a_failed_first_write_changes_nothing,
    test_a_failed_restore_names_what_the_camera_was_left_at,
]


async def main() -> int:
    failures = 0
    for test in TESTS:
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
