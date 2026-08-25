"""Tests for the bridge-fed stream-health sensors in `sensor.py`.

Run from the repository root:

    python3 tests/test_stream_health_sensors.py

No Home Assistant, no network, no bridge: `tests/ha_stubs.py` supplies the
`homeassistant.*` names the module imports, and the coordinator's one HTTP call
is replaced with a scripted stand-in. The REAL `sensor.py` classes run.

Why this file exists: **every failure these sensors report is one that produces
no error anywhere else, and every mistake in deriving them is equally quiet.**

`CameraAudioStateSensor` is the sharp case. The bridge's Opus decoder thread can
die mid-session (see `camera-bridge/bridge/aiortc_opus_patch.py`) and leave
`state` at `streaming`, `last_frame_at` fresh, no error logged and the published
audio pure silence -- eleven consecutive silent doorbell sessions went unnoticed
exactly that way. The only signal is that `decoded_audio_frames` stops moving,
which Home Assistant cannot trigger on, so this sensor turns *stopped* into the
state `stalled`.

That makes a FALSE `stalled` the expensive bug: an automation that pages someone
when the doorbell goes deaf is worthless if it also pages them every time the
bridge restarts or a poll times out. There are four ways to fabricate one, and
all four are pinned below -- an unanswered poll, a failed poll, a counter reset,
and the first poll of the entity's life. Each of those leaves the coordinator
holding data that *looks* like a counter that did not move.

Pinned here:

  1. coordinator      -- an unanswered `/control/status` (bridge secret not on
                         the shared mount yet, normal for seconds after a bridge
                         restart) keeps the PREVIOUS payload and records
                         `last_poll_ok = False` rather than flapping every
                         entity to unavailable; a transport failure raises
                         UpdateFailed and also records it. Those two flags are
                         the entire basis of the audio derivation's caution.
  2. stream state     -- the payload mapping, and that a state this build does
                         not know degrades to None while staying readable
                         verbatim in `raw_state` (an ENUM sensor whose state is
                         outside its own options errors on EVERY update).
  3. dropped frames   -- the payload mapping, including a missing or null count
                         reading 0 rather than raising.
  4. audio state      -- the four states, and above all the four cases that must
                         never read `stalled`. Plus the per-poll delta, which is
                         suppressed rather than reported negative across a
                         counter reset.
  5. enum containment -- every state the derivation can produce is inside
                         `_attr_options`, checked after every single poll these
                         tests make.
"""
import asyncio
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.telus_smarthome import sensor  # noqa: E402
from custom_components.telus_smarthome.sensor import (  # noqa: E402
    AUDIO_FLOWING,
    AUDIO_NO_AUDIO_YET,
    AUDIO_NOT_CONSUMING,
    AUDIO_STALLED,
    AUDIO_STATES,
    STREAM_STATES,
    CameraAudioStateSensor,
    CameraDroppedFramesSensor,
    CameraStreamStateSensor,
    StreamHealthCoordinator,
)

CAMERA_ID = "111111111-2048"

# Held so the coordinator tests can put the real helper back after scripting it.
_REAL_BRIDGE_GET = sensor.bridge_get

# A /control/status camera entry, shaped like what the bridge publishes. Every
# account-specific value is a placeholder.
STATUS_STREAMING = {
    "state": "streaming",
    "fps": 15,
    "hardware_encode": True,
    "consecutive_failures": 0,
    "last_error": None,
    "last_frame_at": "2026-08-24T10:00:00Z",
    "dropped_frames": 12,
    "audio_consumed": True,
    "decoded_audio_frames": 4800,
    "audio_format": "48000Hz/2ch/s16",
    "dropped_audio_frames": 0,
    # Present on every v1.21.0+ bridge, so the healthy fixture carries them.
    # A bridge that predates them omits both; test_audio_says_why_it_stopped
    # pins that case separately.
    "audio_dropped": False,
    "audio_dropped_reason": None,
    # Present on every v1.22.4+ bridge (E69/E70): the delivered rate beside
    # the declared one, a confirmed change waiting for its rotation, and
    # ffmpeg's own duplication-warning count.
    "measured_fps": 14,
    "rate_pending": None,
    "duplicated_frame_warnings": 0,
}


def status(**overrides):
    """One camera's status entry with `overrides` applied."""
    entry = dict(STATUS_STREAMING)
    entry.update(overrides)
    return entry


class FakeConfigEntry:
    entry_id = "entry"
    data: dict = {}


def make_coordinator(data=None):
    """A real StreamHealthCoordinator with no polling attached."""
    coordinator = StreamHealthCoordinator(object(), FakeConfigEntry())
    coordinator.data = data
    coordinator.last_update_success = True
    coordinator.last_poll_ok = True if data is not None else None
    return coordinator


def poll(entity, coordinator, cameras, *, poll_ok=True, update_success=True,
         same_object=False):
    """Deliver one coordinator update to `entity`, the way a real poll would.

    `cameras` is rebuilt into a NEW dict each time unless `same_object` is set,
    because the entity treats an identical payload object as "not a second
    observation" -- distinguishing that guard from the `poll_ok` one needs both
    to be drivable independently.
    """
    if not same_object:
        coordinator.data = {cid: dict(entry) for cid, entry in cameras.items()}
    coordinator.last_poll_ok = poll_ok
    coordinator.last_update_success = update_success
    entity._handle_coordinator_update()
    # Section 5: an ENUM sensor whose state falls outside its own options errors
    # on every update, so this holds after every poll any test in this file makes.
    value = entity.native_value
    if isinstance(entity, CameraAudioStateSensor):
        assert value is None or value in AUDIO_STATES, f"state {value!r} outside options"
    return value


# ---------------------------------------------------------------------------
# 1. The coordinator's two failure flags
# ---------------------------------------------------------------------------

def test_coordinator_records_a_good_poll():
    coordinator = make_coordinator()

    async def fake_get(hass, url):
        assert url == sensor.CAMERA_BRIDGE_STATUS_URL
        return {"cameras": {CAMERA_ID: status()}}

    sensor.bridge_get = fake_get
    try:
        data = asyncio.run(coordinator._async_update_data())
    finally:
        sensor.bridge_get = _REAL_BRIDGE_GET
    assert data == {CAMERA_ID: status()}
    assert coordinator.last_poll_ok is True
    print("  ok  coordinator: a good poll returns the cameras dict, last_poll_ok True")


def test_unanswered_poll_keeps_the_previous_payload():
    """`bridge_get` returning None is the bridge's control secret not being on
    the shared mount yet -- normal for a few seconds after a bridge restart.

    Flapping every stream-health entity to unavailable for that would be noise;
    what the entities get instead is the previous payload plus a flag saying it
    is not a fresh observation."""
    previous = {CAMERA_ID: status()}
    coordinator = make_coordinator(previous)

    async def fake_get(hass, url):
        return None

    sensor.bridge_get = fake_get
    try:
        data = asyncio.run(coordinator._async_update_data())
    finally:
        sensor.bridge_get = _REAL_BRIDGE_GET
    assert data is previous, "an unanswered poll must keep the previous payload"
    assert coordinator.last_poll_ok is False
    print("  ok  coordinator: an unanswered poll keeps the payload, last_poll_ok False")


def test_failed_poll_raises_update_failed():
    coordinator = make_coordinator({CAMERA_ID: status()})

    async def fake_get(hass, url):
        raise OSError("connection refused")

    sensor.bridge_get = fake_get
    try:
        raised = False
        try:
            asyncio.run(coordinator._async_update_data())
        except sensor.UpdateFailed:
            raised = True
    finally:
        sensor.bridge_get = _REAL_BRIDGE_GET
    assert raised, "a transport failure must surface as UpdateFailed"
    assert coordinator.last_poll_ok is False
    print("  ok  coordinator: a transport failure raises UpdateFailed, last_poll_ok False")


# ---------------------------------------------------------------------------
# 2. CameraStreamStateSensor
# ---------------------------------------------------------------------------

def test_stream_state_maps_the_payload():
    coordinator = make_coordinator()
    entity = CameraStreamStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(state="reconnecting",
                                                 consecutive_failures=2,
                                                 last_error="ice failed")})
    assert entity.native_value == "reconnecting"
    attrs = entity.extra_state_attributes
    assert attrs["fps"] == 15
    assert attrs["measured_fps"] == 14, (
        "the DELIVERED rate must ride beside the declared one -- the gap "
        "between them is the padding/dropping diagnostic (E69)")
    assert attrs["rate_pending"] is None
    assert attrs["duplicated_frame_warnings"] == 0
    assert attrs["hardware_encode"] is True
    assert attrs["consecutive_failures"] == 2
    assert attrs["last_error"] == "ice failed"
    assert attrs["last_frame_at"] == "2026-08-24T10:00:00Z"
    assert attrs["raw_state"] == "reconnecting"
    assert entity.available is True
    print("  ok  stream state: the /control/status entry maps to state + attributes")


def test_stream_state_degrades_an_unknown_vocabulary():
    """The state vocabulary belongs to a separately-deployed bridge.

    A newer bridge inventing a fifth state must not turn into an ENUM sensor
    erroring on every update -- it degrades to unknown, and the raw word stays
    readable so nobody has to read container logs to find out what it was."""
    coordinator = make_coordinator()
    entity = CameraStreamStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(state="rekeying")})
    assert "rekeying" not in STREAM_STATES
    assert entity.native_value is None, "a state outside the options must not be reported"
    assert entity.extra_state_attributes["raw_state"] == "rekeying"
    assert entity._attr_options == STREAM_STATES
    print("  ok  stream state: an unknown state degrades to None, verbatim in raw_state")


def test_stream_state_unavailable_when_the_camera_is_dropped():
    coordinator = make_coordinator()
    entity = CameraStreamStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status()})
    assert entity.available is True
    poll(entity, coordinator, {})
    assert entity.available is False, "a camera the bridge stopped reporting is unavailable"
    assert entity.native_value is None
    print("  ok  stream state: a camera dropped from the payload goes unavailable")


# ---------------------------------------------------------------------------
# 3. CameraDroppedFramesSensor
# ---------------------------------------------------------------------------

def test_dropped_frames_maps_the_payload():
    coordinator = make_coordinator()
    entity = CameraDroppedFramesSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(dropped_frames=37)})
    assert entity.native_value == 37
    assert entity._attr_state_class == sensor.SensorStateClass.TOTAL_INCREASING, \
        "a bridge restart resets the counter; only TOTAL_INCREASING reads that as a reset"
    print("  ok  dropped frames: counts map through, state class is total_increasing")


def test_dropped_frames_tolerates_missing_and_null():
    """A bridge that has not counted yet reports null, and one too old to report
    the field at all reports nothing. Neither may raise into the state machine."""
    coordinator = make_coordinator()
    entity = CameraDroppedFramesSensor(coordinator, CAMERA_ID)

    without = status()
    del without["dropped_frames"]
    poll(entity, coordinator, {CAMERA_ID: without})
    assert entity.native_value == 0
    poll(entity, coordinator, {CAMERA_ID: status(dropped_frames=None)})
    assert entity.native_value == 0
    print("  ok  dropped frames: a missing or null count reads 0, never raises")


# ---------------------------------------------------------------------------
# 4. CameraAudioStateSensor -- the derivation
# ---------------------------------------------------------------------------

def test_audio_not_consuming():
    """Audio consumption is currently off on the live deployment, so this is
    what every camera reports today. It is an answer, not a broken entity."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    value = poll(entity, coordinator, {CAMERA_ID: status(audio_consumed=False,
                                                        decoded_audio_frames=0)})
    assert value == AUDIO_NOT_CONSUMING
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] is None
    print("  ok  audio: a camera whose audio is not consumed reports not_consuming")


def test_audio_old_bridge_is_unknown_not_fabricated():
    """A bridge predating these fields cannot answer the question. Reporting
    `not_consuming` for it would be a guess that reads exactly like a fact."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    old = status()
    del old["audio_consumed"]
    del old["decoded_audio_frames"]
    value = poll(entity, coordinator, {CAMERA_ID: old})
    assert value is None, "a bridge that cannot report audio_consumed gets unknown"
    print("  ok  audio: a bridge too old to report audio_consumed reports unknown")


def test_audio_first_poll_is_never_stalled():
    """CASE 4 of the four false-`stalled` traps.

    The first comparable poll has nothing to compare against. A camera already
    at zero is unambiguous (`no_audio_yet`); one with frames already on the
    clock is genuinely unknown for one poll interval and says so."""
    coordinator = make_coordinator()
    at_zero = CameraAudioStateSensor(coordinator, CAMERA_ID)
    value = poll(at_zero, coordinator, {CAMERA_ID: status(decoded_audio_frames=0)})
    assert value == AUDIO_NO_AUDIO_YET
    assert at_zero.extra_state_attributes["decoded_audio_frames_last_poll"] is None

    coordinator2 = make_coordinator()
    mid_stream = CameraAudioStateSensor(coordinator2, CAMERA_ID)
    value = poll(mid_stream, coordinator2, {CAMERA_ID: status(decoded_audio_frames=4800)})
    assert value != AUDIO_STALLED, "the first poll can never be a stall"
    assert value is None, "one poll interval of honest unknown, not a guess"
    print("  ok  audio: the first poll is no_audio_yet at zero, unknown mid-stream, never stalled")


def test_audio_says_why_it_stopped_when_the_bridge_dropped_the_pipe():
    """`stalled` alone cannot distinguish "the audio pipe wedged and the bridge
    deliberately dropped it to keep video publishing" from "audio just went
    quiet". The bridge knows which; the entity has to pass it on.

    Deliberately attributes and not a fifth enum state: `stalled` is what an
    automation acts on either way, and splitting it would break any automation
    already keyed on it.
    """
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=4800)})
    attrs = entity.extra_state_attributes
    assert attrs["audio_dropped"] is False
    assert attrs["audio_dropped_reason"] is None

    value = poll(entity, coordinator, {CAMERA_ID: status(
        decoded_audio_frames=4800,
        audio_dropped=True,
        audio_dropped_reason="ffmpeg stopped reading the audio pipe",
    )})
    assert value == AUDIO_STALLED, "a dropped pipe does stop the counter"
    attrs = entity.extra_state_attributes
    assert attrs["audio_dropped"] is True
    assert attrs["audio_dropped_reason"] == "ffmpeg stopped reading the audio pipe", (
        "the operator must not have to read the container log for the cause"
    )
    # A bridge predating these fields omits them entirely. Absent must read as
    # absent, never as a fabricated False.
    older = dict(status(decoded_audio_frames=4800))
    older.pop("audio_dropped")
    older.pop("audio_dropped_reason")
    coordinator2 = make_coordinator()
    entity2 = CameraAudioStateSensor(coordinator2, CAMERA_ID)
    poll(entity2, coordinator2, {CAMERA_ID: older})
    attrs = entity2.extra_state_attributes
    assert attrs["audio_dropped"] is None, "an older bridge is unknown, not False"
    assert attrs["audio_dropped_reason"] is None

    print("  ok  audio: a deliberately dropped pipe reports why, not just stalled")


def test_audio_flowing_then_stalled():
    """The state this entity exists for: a counter that stops moving while every
    other signal stays green."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=4800)})
    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)})
    assert value == AUDIO_FLOWING
    attrs = entity.extra_state_attributes
    assert attrs["decoded_audio_frames_last_poll"] == 1500
    assert attrs["decoded_audio_frames"] == 6300
    assert attrs["audio_format"] == "48000Hz/2ch/s16"
    assert attrs["last_audio_at"], "a moving counter stamps last_audio_at"

    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)})
    assert value == AUDIO_STALLED, "an unmoved counter on a fresh poll IS the failure"
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] == 0
    print("  ok  audio: a moving counter is flowing with a delta; an unmoved one is stalled")


def test_audio_zero_stays_no_audio_yet():
    """A consuming camera sitting at zero across polls has never decoded a
    frame. That is `no_audio_yet`, not a stall of something that was flowing."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=0)})
    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=0)})
    assert value == AUDIO_NO_AUDIO_YET, "zero across polls is not a stall"
    print("  ok  audio: a counter parked at zero stays no_audio_yet, never stalls")


def test_audio_unanswered_poll_is_never_stalled():
    """CASE 1 of the four false-`stalled` traps.

    The bridge did not answer, so the coordinator is holding the PREVIOUS
    payload -- the same object, with the same counter in it. Folding that in
    would compare the counter against itself and report a dead decoder on a
    perfectly healthy camera. Both guards are exercised: the flag on its own
    (a fresh object that is not a fresh observation) and the identical object."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=4800)})
    assert poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)}) == AUDIO_FLOWING

    # The literal shape of an unanswered poll: `_async_update_data` returns the
    # object it already had, so the entity is handed the very same dict again.
    value = poll(entity, coordinator, None, same_object=True)
    assert value == AUDIO_FLOWING, "the identical payload object is not a second observation"
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] == 1500

    # The flag on its own, with a distinct-but-equal payload -- so the guard is
    # pinned independently of the object-identity one.
    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)},
                 poll_ok=False)
    assert value == AUDIO_FLOWING, "an unanswered poll must not fabricate a stall"
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] == 1500, \
        "the delta must not be recomputed from an observation that never happened"
    print("  ok  audio: an unanswered poll (identical payload, and flag) never stalls")


def test_audio_failed_poll_is_never_stalled():
    """CASE 2 of the four false-`stalled` traps.

    `UpdateFailed` leaves `last_update_success` false with the previous data
    still in place. The entity is already unavailable through
    `CoordinatorEntity.available`; what must not happen is the stale counter
    being folded in and surviving as a stall once the next poll succeeds."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=4800)})
    assert poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)}) == AUDIO_FLOWING

    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)},
                 update_success=False)
    assert value == AUDIO_FLOWING, "a failed poll must not fabricate a stall"
    assert entity.available is False

    # ...and the recovery poll compares against the last REAL observation.
    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=7800)})
    assert value == AUDIO_FLOWING
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] == 1500
    print("  ok  audio: a failed poll never stalls, and recovery compares to the last real sample")


def test_audio_counter_reset_is_never_stalled():
    """CASE 3 of the four false-`stalled` traps.

    A counter that went DOWN is the bridge process restarting -- every number in
    /control/status resets with it. Frames have demonstrably arrived since that
    restart, so this reads `flowing`, and the delta is suppressed rather than
    reported as a negative number."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=48000)})
    assert poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=49500)}) == AUDIO_FLOWING

    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=300)})
    assert value == AUDIO_FLOWING, "a counter reset is a bridge restart, not a stall"
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] is None, \
        "a negative delta is meaningless and must be suppressed"

    # The comparison restarts from the post-reset value rather than the old high.
    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=300)})
    assert value == AUDIO_STALLED
    print("  ok  audio: a counter reset reads flowing with no delta, never stalled")


def test_audio_disabling_consumption_restarts_the_comparison():
    """Re-enabling audio restarts the bridge, and with it the counter. The
    remembered count is dropped on the way through `not_consuming` so the first
    poll after the restart is a first poll, not a measurement across the gap."""
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=48000)})
    poll(entity, coordinator, {CAMERA_ID: status(audio_consumed=False,
                                                 decoded_audio_frames=48000)})
    assert entity._previous_frames is None

    value = poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=120)})
    assert value is None, "the poll after re-enabling has nothing to compare against"
    assert entity.extra_state_attributes["decoded_audio_frames_last_poll"] is None
    print("  ok  audio: not_consuming drops the remembered count, so re-enabling starts fresh")


def test_audio_dropped_camera_keeps_its_last_state():
    coordinator = make_coordinator()
    entity = CameraAudioStateSensor(coordinator, CAMERA_ID)

    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=4800)})
    poll(entity, coordinator, {CAMERA_ID: status(decoded_audio_frames=6300)})
    poll(entity, coordinator, {})
    assert entity.available is False, "a camera the bridge dropped is unavailable"
    assert entity._audio_state == AUDIO_FLOWING, \
        "an absent camera must not advance the comparison"
    print("  ok  audio: a camera dropped from the payload goes unavailable, state untouched")


def test_audio_options_cover_every_derivable_state():
    """`_attr_options` is the ENUM sensor's whole vocabulary; a state outside it
    errors on every update. Pinned as a set so adding a state without adding it
    to the options list fails here rather than in a production log."""
    assert set(AUDIO_STATES) == {AUDIO_NOT_CONSUMING, AUDIO_NO_AUDIO_YET,
                                 AUDIO_FLOWING, AUDIO_STALLED}
    assert CameraAudioStateSensor._attr_options == AUDIO_STATES
    assert CameraAudioStateSensor._attr_device_class == sensor.SensorDeviceClass.ENUM
    print("  ok  audio: the options list holds exactly the four derivable states")


def main():
    failures = 0
    for test in (
        test_coordinator_records_a_good_poll,
        test_unanswered_poll_keeps_the_previous_payload,
        test_failed_poll_raises_update_failed,
        test_stream_state_maps_the_payload,
        test_stream_state_degrades_an_unknown_vocabulary,
        test_stream_state_unavailable_when_the_camera_is_dropped,
        test_dropped_frames_maps_the_payload,
        test_dropped_frames_tolerates_missing_and_null,
        test_audio_not_consuming,
        test_audio_old_bridge_is_unknown_not_fabricated,
        test_audio_first_poll_is_never_stalled,
        test_audio_says_why_it_stopped_when_the_bridge_dropped_the_pipe,
        test_audio_flowing_then_stalled,
        test_audio_zero_stays_no_audio_yet,
        test_audio_unanswered_poll_is_never_stalled,
        test_audio_failed_poll_is_never_stalled,
        test_audio_counter_reset_is_never_stalled,
        test_audio_disabling_consumption_restarts_the_comparison,
        test_audio_dropped_camera_keeps_its_last_state,
        test_audio_options_cover_every_derivable_state,
    ):
        try:
            test()
        except AssertionError as exc:
            print(f"  FAIL {test.__name__}: {exc}")
            failures += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  ERROR {test.__name__}: {exc!r}")
            failures += 1
    print("\nRESULT:", "PASS" if not failures else f"FAIL ({failures})")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
