"""Tests for the Activity-feed event entities in `event.py`.

Run from the repository root:

    python3 tests/test_event_entities.py

No Home Assistant, no network, no bridge: `tests/ha_stubs.py` supplies the
`homeassistant.*` names the module imports, and the coordinators are the real
classes with their polling never started. The REAL entity classes run.

Why this file exists: **every regression here is an automation that quietly
stops firing, or one that fires when nothing happened.** Neither raises, neither
logs, and both are discovered by a person eventually noticing their doorbell
notification never arrived.

Two specific traps are pinned above everything else:

**The locale trap.** This account's Activity descriptions have been observed in
BOTH French ("Déclenché par caméra (Une personne)", "Mouvement détecté") and
English ("Button Push", "Locked (Manually locked)") for the same event kinds.
The locale is a server-side account property that can change with nothing on
this side being touched, so `TelusDoorbellEvent` matches TELUS's untranslated
`event_type_name` enum and never the description. A French-described press must
still fire, and an entry whose description says "Button Push" while its enum
says something else must NOT -- because the day someone "simplifies" the matcher
into a text match, the tests that only ever feed it English text stay green.

**The dedup trap.** The dedup is a per-page id SET, deliberately not a
high-water mark. Activity ids are opaque ("M-1723220865386") so they cannot be
compared for recency, and a timestamp mark is worse: one press reliably produces
several feed entries inside the same second, so a strict ">" would suppress the
siblings forever. The baseline is re-derived at entity construction, which is
what stops a Home Assistant restart re-firing the newest press in the feed --
and an EMPTY page is a failed poll, not a baseline, or the first real page would
fire wholesale.

Pinned here:

  1. `_classify`   -- French and English both classify, everything else lands on
                      `other`, and every output is inside `EVENT_TYPES` (the
                      event entity refuses a type it never declared).
  2. `_event_time` -- unparseable entries sort rather than raise, and the two
                      timestamp formats TELUS emits order correctly, which a
                      string sort does not.
  3. `_is_doorbell`-- any one of the three chime capabilities is enough.
  4. doorbell      -- the enum matcher (locale-proof, description-proof), the
                      device correlation, restart safety, sibling timestamps,
                      oldest-first firing with one state write per press, an
                      empty page never clearing the baseline, and that the
                      entity declares and fires only Home Assistant's standard
                      `ring` type (a doorbell-class entity without it is
                      refused from 2027.4).
  5. activity      -- the baseline, and the single extra fire an entry gets when
                      TELUS attaches its clip a poll or two after the entry.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import ha_stubs  # noqa: E402

ha_stubs.install()

from custom_components.telus_smarthome import event as event_module  # noqa: E402
from custom_components.telus_smarthome.const import CONF_CLIP_LINK_SECRET  # noqa: E402
from custom_components.telus_smarthome.event import (  # noqa: E402
    DOORBELL_EVENT_TYPES,
    DOORBELL_PRESS_EVENT_TYPE_NAME,
    EVENT_TYPES,
    ActivityHistoryCoordinator,
    TelusActivityEvent,
    TelusDoorbellEvent,
    _classify,
    _event_time,
    _is_doorbell,
)

CAMERA_ID = "111111111-2048"
OTHER_CAMERA_ID = "111111111-2049"

# A /control/history entry for a button press, shaped like the live capture with
# every account-specific value replaced by a placeholder. The description is the
# captured one, verbatim -- English, on an account whose OTHER descriptions come
# back in French. That mixture is the whole reason the matcher reads the enum.
PRESS = {
    "id": "M-1723220865386",
    "timestamp": "2026-08-24T10:00:00Z",
    "description": "Button Push",
    "device_id": CAMERA_ID,
    "device_description": "Doorbell",
    "event_type_name": DOORBELL_PRESS_EVENT_TYPE_NAME,
    "thumbnail_url": None,
    "download_url": None,
    "clip_duration_ms": None,
    "video_event_id": None,
}

# Limit of the evidence: a press has NOT been captured with a French
# description. What has been captured is that this account renders other event
# kinds in French ("Déclenché par caméra (Une personne)", "Mouvement détecté"),
# that the locale is a server-side account property, and that `event_type_name`
# is an untranslated enum. This string is therefore CONSTRUCTED to stand for the
# account flipping locale -- it is the scenario the entity is designed for, not
# a recording of one.
PRESS_DESCRIPTION_FR = "Bouton de sonnette actionné"


def press(**overrides):
    entry = dict(PRESS)
    entry.update(overrides)
    return entry


class FakeConfigEntry:
    entry_id = "entry"

    def __init__(self, data=None):
        self.data = data or {}


def make_coordinator(data=None, config_entry=None):
    """A real ActivityHistoryCoordinator with no polling attached."""
    coordinator = ActivityHistoryCoordinator(object(), config_entry or FakeConfigEntry())
    coordinator.data = data
    coordinator.last_update_success = True
    return coordinator


def poll(entity, coordinator, events):
    """Deliver one page to `entity`, rebuilt into fresh dicts as a poll would."""
    coordinator.data = [dict(e) for e in events]
    entity._handle_coordinator_update()


def fired_ids(entity):
    return [attrs.get("event_id") for _type, attrs in entity.triggered_events]


# ---------------------------------------------------------------------------
# 1. _classify
# ---------------------------------------------------------------------------

def test_classify_handles_both_locales():
    """The account's live descriptions are French; English is what it would send
    if the locale ever flipped. Both must land on the same type.

    Which half of each pair is load-bearing differs, and mutation testing is how
    that was established: **"personne" contains "person"**, so the French person
    text is matched by the English needle and the French one is belt-and-braces;
    "animal" is spelled the same in both. Only the VEHICLE needles are
    genuinely per-locale -- "véhicule" shares no substring with "vehicle" -- so
    the vehicle assertions below are the ones that fail if either language's
    match is dropped, and this test is only as strong as they are."""
    assert _classify("Déclenché par caméra (Une personne)") == "person"
    assert _classify("Person detected by camera") == "person"
    assert _classify("Déclenché par caméra (Un animal)") == "animal"
    assert _classify("Animal detected") == "animal"
    assert _classify("Déclenché par caméra (Un véhicule)") == "vehicle"
    assert _classify("Vehicle detected") == "vehicle"
    # Accents are not guaranteed to survive every transport this text crosses.
    assert _classify("Declenche par camera (Un vehicule)") == "vehicle"
    print("  ok  classify: French and English descriptions map to the same types")


def test_classify_falls_back_to_other():
    assert _classify("Button Push") == "other"
    assert _classify("Mouvement détecté") == "other"
    assert _classify("") == "other"
    print("  ok  classify: an unrecognised description is 'other', never an error")


def test_classify_only_produces_declared_event_types():
    """`TelusActivityEvent` passes this straight into `_trigger_event`, which
    raises on a type outside `event_types`. A fifth return value would take out
    every activity event, not just its own."""
    corpus = [
        "Déclenché par caméra (Une personne)", "Person", "Un animal", "Animal",
        "Un véhicule", "Vehicle", "Button Push", "Locked (Manually locked)",
        "Mouvement détecté", "", "PERSONNE", "unknown gibberish",
    ]
    for description in corpus:
        assert _classify(description) in EVENT_TYPES, description
    print("  ok  classify: every output is inside EVENT_TYPES")


# ---------------------------------------------------------------------------
# 2. _event_time
# ---------------------------------------------------------------------------

def test_event_time_never_raises():
    """One malformed entry must not take out the sort for the whole page."""
    epoch = _event_time({})
    assert _event_time({"timestamp": None}) == epoch
    assert _event_time({"timestamp": "not a timestamp"}) == epoch
    assert _event_time(press()) > epoch
    print("  ok  event time: missing and unparseable timestamps sort at epoch")


def test_event_time_orders_across_the_two_timestamp_formats():
    """TELUS emits timestamps with AND without fractional seconds, and
    "...T10:00:00Z" sorts BELOW "...T10:00:00.500000Z" as a string -- so a
    string sort reorders events across that boundary. Parsing does not."""
    earlier = press(id="A", timestamp="2026-08-24T10:00:00Z")
    later = press(id="B", timestamp="2026-08-24T10:00:00.500000Z")

    assert _event_time(earlier) < _event_time(later)
    assert later["timestamp"] < earlier["timestamp"], \
        "the string comparison really is the wrong way round -- this is the trap"
    assert [e["id"] for e in sorted([later, earlier], key=_event_time)] == ["A", "B"]
    print("  ok  event time: fractional and whole-second stamps order correctly")


# ---------------------------------------------------------------------------
# 3. _is_doorbell
# ---------------------------------------------------------------------------

def test_is_doorbell_accepts_any_single_chime_capability():
    """The three flags are independent upstream, so a unit with an outdoor tone
    but no wired-chime support is still a doorbell."""
    for flag in ("supports_indoor_chime", "supports_outdoor_tone", "supports_chime_type"):
        assert _is_doorbell({"settings": {"audio": {flag: True}}}), flag
    assert not _is_doorbell({"settings": {"audio": {
        "supports_indoor_chime": False,
        "supports_outdoor_tone": False,
        "supports_chime_type": False,
    }}})
    assert not _is_doorbell({"settings": {"audio": {}}})
    assert not _is_doorbell({}), "a camera with no settings block is not a doorbell"
    print("  ok  is_doorbell: any one chime capability is enough, none means no")


# ---------------------------------------------------------------------------
# 4. TelusDoorbellEvent
# ---------------------------------------------------------------------------

def test_doorbell_fires_on_a_french_described_press():
    """The locale trap, from the firing side: the description is French and the
    entity must still fire, because it never reads the description."""
    coordinator = make_coordinator([press(id="OLD", event_type_name="AccessControlUnlocked")])
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)

    poll(entity, coordinator, [press(id="P1", description=PRESS_DESCRIPTION_FR),
                               press(id="OLD", event_type_name="AccessControlUnlocked")])
    assert fired_ids(entity) == ["P1"]
    kind, attrs = entity.triggered_events[0]
    assert kind == ha_stubs.DoorbellEventType.RING, (
        f"fired {kind!r}: a doorbell-class entity fires the standard `ring` "
        "type, the one generic doorbell automations and Home Assistant's own "
        "doorbell handling key on")
    assert attrs["camera_id"] == CAMERA_ID
    assert attrs["description"] == PRESS_DESCRIPTION_FR
    assert attrs["event_type_name"] == DOORBELL_PRESS_EVENT_TYPE_NAME
    assert attrs["timestamp"] == "2026-08-24T10:00:00Z"
    assert attrs["clip_link"] is None, "no clip-link secret configured means no link"
    print("  ok  doorbell: a French-described press fires, with its attributes")


def test_doorbell_ignores_a_matching_description_with_the_wrong_enum():
    """The locale trap, from the refusing side.

    Every entry below would match a description-based matcher. None of them is a
    press, so none may fire. This is what fails if the matcher is ever
    "simplified" back onto the free text."""
    coordinator = make_coordinator([press(id="BASE")])
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)

    decoys = [
        # An unrelated event kind whose description happens to say the words.
        press(id="D1", description="Button Push", event_type_name="AccessControlUnlocked"),
        # An entry from a bridge/feed that dropped the enum entirely.
        press(id="D2", description="Button Push", event_type_name=None),
        {"id": "D3", "timestamp": "2026-08-24T10:05:00Z", "description": "Button Push",
         "device_id": CAMERA_ID, "device_description": "Doorbell"},
        # The right enum, spelled the way a description would be.
        press(id="D4", event_type_name="Button Push"),
    ]
    poll(entity, coordinator, decoys)
    assert entity.triggered_events == [], \
        "only the untranslated event_type_name enum may fire this entity"
    print("  ok  doorbell: 'Button Push' text without the enum never fires")


def test_doorbell_ignores_another_cameras_press():
    coordinator = make_coordinator([press(id="BASE")])
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)

    poll(entity, coordinator, [press(id="P-OTHER", device_id=OTHER_CAMERA_ID)])
    assert entity.triggered_events == [], "a press on another camera is not this doorbell's"

    poll(entity, coordinator, [press(id="P-MINE"), press(id="P-OTHER",
                                                         device_id=OTHER_CAMERA_ID)])
    assert fired_ids(entity) == ["P-MINE"]
    print("  ok  doorbell: presses are correlated by device_id, not by label")


def test_doorbell_restart_does_not_replay_the_last_press():
    """Nothing here is persisted, so the entity is rebuilt on every Home
    Assistant start. If the press already sitting in the feed were treated as
    news, every restart would ring the house."""
    page = [press(id="P1"), press(id="P0", timestamp="2026-08-24T09:00:00Z")]
    coordinator = make_coordinator(page)
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)

    poll(entity, coordinator, page)
    assert entity.triggered_events == [], "a press already in the feed is history, not news"
    assert entity.state_writes >= 1, "the entity still writes state for the poll"

    poll(entity, coordinator, [press(id="P2", timestamp="2026-08-24T10:30:00Z")] + page)
    assert fired_ids(entity) == ["P2"]
    print("  ok  doorbell: a restart baselines on the current page and replays nothing")


def test_doorbell_dedup_is_an_id_set_not_a_timestamp_mark():
    """One press produces several feed entries inside the same second, so a
    high-water timestamp would discard the siblings forever. Two presses sharing
    a timestamp, one already seen: the other still fires."""
    coordinator = make_coordinator([press(id="P1")])
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)

    # Same timestamp as the baselined P1, brand new id.
    poll(entity, coordinator, [press(id="P1-SIBLING"), press(id="P1")])
    assert fired_ids(entity) == ["P1-SIBLING"], \
        "an id sharing the newest timestamp must still fire"

    # And the same page again is not a second press.
    poll(entity, coordinator, [press(id="P1-SIBLING"), press(id="P1")])
    assert fired_ids(entity) == ["P1-SIBLING"], "re-polling the same page must not re-fire"
    print("  ok  doorbell: dedup is a per-page id set, so timestamp siblings both fire")


def test_doorbell_fires_two_presses_oldest_first_with_one_write_each():
    """`_trigger_event` only stores the attributes; the state write is what a
    listener sees. One write at the end of the loop would collapse a two-press
    poll into a single state_changed and lose the earlier ring entirely."""
    coordinator = make_coordinator([press(id="BASE", timestamp="2026-08-24T08:00:00Z")])
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)
    before = entity.state_writes

    # Delivered newest-first, the way the feed returns them.
    poll(entity, coordinator, [
        press(id="P2", timestamp="2026-08-24T10:00:02Z"),
        press(id="P1", timestamp="2026-08-24T10:00:01Z"),
        press(id="BASE", timestamp="2026-08-24T08:00:00Z"),
    ])
    assert fired_ids(entity) == ["P1", "P2"], "presses reach a listener oldest-first"
    assert entity.state_writes - before >= 2, "one state write per press, not one per poll"
    print("  ok  doorbell: two presses in one poll fire oldest-first, one write each")


def test_doorbell_empty_page_is_a_failed_poll_not_a_baseline():
    """An empty first page means the bridge is not up yet. Adopting it as a
    baseline would fire every press on the first real page; clearing an existing
    baseline with it would re-fire the whole next page."""
    coordinator = make_coordinator([])
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)
    assert entity._baseline_pending is True

    poll(entity, coordinator, [press(id="P1")])
    assert entity.triggered_events == [], "the first non-empty page is the baseline"

    poll(entity, coordinator, [])
    poll(entity, coordinator, [press(id="P1")])
    assert entity.triggered_events == [], "an empty page must not clear the baseline"

    poll(entity, coordinator, [press(id="P2", timestamp="2026-08-24T10:30:00Z"),
                               press(id="P1")])
    assert fired_ids(entity) == ["P2"]
    print("  ok  doorbell: an empty page neither becomes nor clears the baseline")


def test_doorbell_declares_the_only_type_it_fires():
    """`EventDeviceClass.DOORBELL` carries a vocabulary: the entity must declare
    `DoorbellEventType.RING` or Home Assistant refuses the entity outright
    from 2027.4 (a deprecation warning until then). And RING is the ONLY type:
    TELUS reports one press event with no long/double-press distinction, and a
    second type fired for the same press would be a second state write and a
    second automation run per ring."""
    assert DOORBELL_EVENT_TYPES == [ha_stubs.DoorbellEventType.RING], (
        f"declares {DOORBELL_EVENT_TYPES!r}")
    assert DOORBELL_EVENT_TYPES[0] == "ring", "the standard type's wire value"
    assert TelusDoorbellEvent._attr_event_types == DOORBELL_EVENT_TYPES
    assert TelusDoorbellEvent._attr_device_class == ha_stubs.EventDeviceClass.DOORBELL
    print("  ok  doorbell: declares device class doorbell and only the standard 'ring' type")


def test_doorbell_clip_link_is_signed_when_a_secret_exists():
    """The clip link outlives the download_url in an already-delivered phone
    notification, so it is minted here rather than at click time."""
    config_entry = FakeConfigEntry({CONF_CLIP_LINK_SECRET: "test-secret-not-a-real-one"})
    coordinator = make_coordinator([press(id="BASE")], config_entry)
    entity = TelusDoorbellEvent(coordinator, CAMERA_ID)

    poll(entity, coordinator, [press(id="P1", video_event_id="9876")])
    _kind, attrs = entity.triggered_events[0]
    assert attrs["clip_link"] is not None
    assert "9876" in attrs["clip_link"] and "sig=" in attrs["clip_link"] \
        and "exp=" in attrs["clip_link"]
    print("  ok  doorbell: a correlated clip gets a signed, expiring link")


# ---------------------------------------------------------------------------
# 5. TelusActivityEvent
# ---------------------------------------------------------------------------

def test_activity_baselines_and_fires_new_entries():
    config_entry = FakeConfigEntry()
    existing = [press(id="OLD", description="Mouvement détecté",
                      event_type_name="MotionDetected")]
    coordinator = make_coordinator(existing, config_entry)
    entity = TelusActivityEvent(coordinator, config_entry)

    poll(entity, coordinator, existing)
    assert entity.triggered_events == [], "a restart must not replay the current page"

    poll(entity, coordinator, [press(id="NEW", timestamp="2026-08-24T10:30:00Z",
                                     description="Déclenché par caméra (Une personne)")]
         + existing)
    assert [k for k, _ in entity.triggered_events] == ["person"], \
        "a new entry fires under its classified type"
    print("  ok  activity: the current page is the baseline; a new entry fires classified")


def test_activity_refires_once_when_the_clip_finally_attaches():
    """TELUS attaches the media relationship a poll or two AFTER the history
    entry appears. Without the one extra fire, the clip notification is lost
    forever; without the bound, it would fire on every subsequent poll."""
    config_entry = FakeConfigEntry()
    coordinator = make_coordinator([press(id="BASE")], config_entry)
    entity = TelusActivityEvent(coordinator, config_entry)

    poll(entity, coordinator, [press(id="E1", timestamp="2026-08-24T10:30:00Z")])
    assert len(entity.triggered_events) == 1
    assert entity.triggered_events[0][1]["download_url"] is None

    with_clip = press(id="E1", timestamp="2026-08-24T10:30:00Z",
                      download_url="https://clips.example.invalid/E1.mp4",
                      video_event_id="9876")
    poll(entity, coordinator, [with_clip])
    assert len(entity.triggered_events) == 2, "the clip arriving late must fire once more"
    assert entity.triggered_events[1][1]["download_url"].endswith("E1.mp4")

    poll(entity, coordinator, [with_clip])
    poll(entity, coordinator, [with_clip])
    assert len(entity.triggered_events) == 2, "and exactly once -- not on every later poll"
    print("  ok  activity: a late-arriving clip re-fires its entry exactly once")


def main():
    failures = 0
    for test in (
        test_classify_handles_both_locales,
        test_classify_falls_back_to_other,
        test_classify_only_produces_declared_event_types,
        test_event_time_never_raises,
        test_event_time_orders_across_the_two_timestamp_formats,
        test_is_doorbell_accepts_any_single_chime_capability,
        test_doorbell_fires_on_a_french_described_press,
        test_doorbell_ignores_a_matching_description_with_the_wrong_enum,
        test_doorbell_ignores_another_cameras_press,
        test_doorbell_restart_does_not_replay_the_last_press,
        test_doorbell_dedup_is_an_id_set_not_a_timestamp_mark,
        test_doorbell_fires_two_presses_oldest_first_with_one_write_each,
        test_doorbell_empty_page_is_a_failed_poll_not_a_baseline,
        test_doorbell_declares_the_only_type_it_fires,
        test_doorbell_clip_link_is_signed_when_a_secret_exists,
        test_activity_baselines_and_fires_new_entries,
        test_activity_refires_once_when_the_clip_finally_attaches,
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
