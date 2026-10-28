"""Tests for the Activity-feed parse in bridge/telus_rest.py (list_history_events).

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_history_events.py

No network and no account: `list_history_events` is driven against a scripted
stand-in session that answers the two GETs it makes (historyEvents, then the
batched videoEvents enrichment) from captured, anonymized page shapes.

**The invisible failure this exists to catch: a doorbell press that silently
stops being a doorbell press.** Home Assistant's doorbell event entity
(`custom_components/telus_smarthome_alarmdotcom/event.py`) decides that a press happened by
matching `event_type_name == "DoorbellButtonPress"` and attributes it to a
camera by matching `device_id` against the camera id. Both keys are renamed here
from alarm.com's own spellings (`eventTypeName`, `globalDeviceId`). If either
rename is broken -- a typo, a refactor, an upstream field that moves -- the
route keeps answering 200 with a perfectly well-formed list of activity, the
integration keeps polling it happily, and the only symptom is that the doorbell
never rings anything again. Nothing raises and nothing logs.

The second property pinned here is WHY the entity matches the enum rather than
the text. This account's descriptions have been observed in both French and
English, so the same press arrives as "Button Push" or as "Bouton poussé"
depending on a server-side account setting nobody here controls. The English and
French presses below are byte-identical apart from that description, and both
must parse to the same `event_type_name` -- which is the assertion that says a
text match would have been a latent, locale-triggered failure.
"""
import asyncio
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge.telus_rest import list_history_events  # noqa: E402

# Anonymized: a real unit id and a real signed relay URL never enter a fixture.
UNIT = "111111111"
DOORBELL_ID = f"{UNIT}-2048"
LOCK_ID = f"{UNIT}-31"


def _history_item(event_id, event_type_name, description, device_id,
                  device_description, media_id=None):
    """One historyEvents item, shaped as alarm.com actually sends it.

    JSON:API: the readable values live under `attributes`, and a correlated clip
    is a `relationships.media` list rather than a field on the event.
    """
    return {
        "id": event_id,
        "type": "activity/historyEvent",
        "attributes": {
            "eventDate": "2026-08-24T20:16:47.043Z",
            "description": description,
            "globalDeviceId": device_id,
            "deviceDescription": device_description,
            "eventTypeName": event_type_name,
            # Always null on historyEvents -- confirmed live; the clip data comes
            # from the videoEvents enrichment call, never from here.
            "thumbnailUrl": None,
        },
        "relationships": {
            "media": {"data": ([{"id": media_id, "type": "video/videoEvent"}]
                               if media_id else [])},
        },
    }


# The captured press, plus the same press as it arrives on a French-locale
# account, plus a non-camera entry and a motion entry that must not be mistaken
# for presses.
HISTORY_PAGE = {
    "data": [
        _history_item("M-1723220865386", "DoorbellButtonPress", "Button Push",
                      DOORBELL_ID, "Doorbell"),
        _history_item("M-1723220865111", "DoorbellButtonPress", "Bouton poussé",
                      DOORBELL_ID, "Doorbell", media_id="9001"),
        _history_item("M-1723220864000", "AccessControlLocked",
                      "Locked (Manually locked)", LOCK_ID, "Front Door Lock"),
        _history_item("M-1723220863000", "VideoCameraTriggered",
                      "Déclenché par caméra (Une personne)", DOORBELL_ID,
                      "Doorbell", media_id="9002"),
    ]
}

VIDEO_PAGE = {
    "data": [
        {
            "id": "9001",
            "attributes": {
                "timestamp": "2026-08-24T20:16:47.043Z",
                "description": "Bouton poussé",
                "thumbnail": "https://relay.example.invalid/thumb/9001",
                "downloadUrl": "https://relay.example.invalid/clip/9001",
                "clipDurationMs": 30000,
            },
            "relationships": {"camera": {"data": {"id": DOORBELL_ID}}},
        },
        {
            "id": "9002",
            "attributes": {
                "timestamp": "2026-08-24T20:15:00Z",
                "description": "Déclenché par caméra (Une personne)",
                "thumbnail": "https://relay.example.invalid/thumb/9002",
                "downloadUrl": "https://relay.example.invalid/clip/9002",
                "clipDurationMs": 15000,
            },
            "relationships": {"camera": {"data": {"id": DOORBELL_ID}}},
        },
    ]
}


class _Response:
    def __init__(self, body):
        self._body = body

    def raise_for_status(self):
        return None

    async def json(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeSession:
    """Answers the two GETs list_history_events makes, and records both."""

    def __init__(self):
        self.urls = []

    def get(self, url, params=None, headers=None):
        self.urls.append(url)
        if "historyEvents" in url:
            return _Response(HISTORY_PAGE)
        if "videoEvents" in url:
            return _Response(VIDEO_PAGE)
        raise AssertionError(f"unexpected URL requested: {url}")


class _FakeTelus:
    def __init__(self):
        self.session = _FakeSession()
        self.api_headers = {"Accept": "application/vnd.api+json"}


async def test_the_keys_home_assistant_matches_a_press_on_survive_the_parse():
    """`eventTypeName`/`globalDeviceId` reach the caller as the renamed keys.

    These two are the entire basis on which a press becomes a Home Assistant
    doorbell event: one says it IS a press, the other says WHICH camera was
    pressed. A break in either is silent -- see the module docstring.
    """
    telus = _FakeTelus()
    events = await list_history_events(telus, limit=20)

    assert len(events) == 4, events
    press = events[0]
    assert press["event_type_name"] == "DoorbellButtonPress", press
    assert press["device_id"] == DOORBELL_ID, press
    assert press["id"] == "M-1723220865386", press
    assert press["timestamp"] == "2026-08-24T20:16:47.043Z", press
    assert press["device_description"] == "Doorbell", press
    assert press["description"] == "Button Push", press

    # Every key the doorbell entity reads must be PRESENT on every entry, even
    # one with no clip -- an absent key and a null one are the same to a .get(),
    # but only until someone indexes it.
    for event in events:
        for key in ("id", "timestamp", "description", "device_id",
                    "device_description", "event_type_name", "thumbnail_url",
                    "download_url", "clip_duration_ms", "video_event_id"):
            assert key in event, (key, event)

    print("  ok  history parse: event_type_name and device_id survive the "
          "rename from eventTypeName/globalDeviceId, on every entry")


async def test_the_same_press_in_two_languages_parses_identically():
    """A press is a press in French and in English.

    The two press entries differ ONLY in their description. If they parse to the
    same event_type_name, then matching the enum is locale-proof and matching
    the text is not -- which is the whole reason the integration matches the
    enum. This assertion is what would fail if someone "simplified" the entity
    back to a description match and updated this fixture to suit.
    """
    telus = _FakeTelus()
    events = await list_history_events(telus, limit=20)

    presses = [e for e in events
               if e["event_type_name"] == "DoorbellButtonPress"]
    assert len(presses) == 2, presses
    assert {e["description"] for e in presses} == {"Button Push", "Bouton poussé"}, presses
    assert {e["device_id"] for e in presses} == {DOORBELL_ID}, presses

    # ...and the inverse: a camera trigger whose French description mentions
    # neither a button nor a bell must not be reachable as a press, and a lock
    # entry on the same page must not be attributed to the doorbell.
    non_presses = [e for e in events
                   if e["event_type_name"] != "DoorbellButtonPress"]
    assert {e["event_type_name"] for e in non_presses} == {
        "AccessControlLocked", "VideoCameraTriggered"}, non_presses
    lock = [e for e in events if e["event_type_name"] == "AccessControlLocked"][0]
    assert lock["device_id"] == LOCK_ID != DOORBELL_ID, lock

    print("  ok  locale: the English and French presses carry one identical "
          "event_type_name; description is the only thing that differs")


async def test_a_press_with_a_clip_is_enriched_and_one_without_stays_null():
    """Clip enrichment is per-entry, and its absence is null, not a missing key.

    A press's media relationship is attached a poll or two after the entry
    itself appears, so "this press has no clip yet" is a normal state and must
    round-trip as explicit nulls rather than as an error or an absent key.
    """
    telus = _FakeTelus()
    events = await list_history_events(telus, limit=20)

    with_clip = [e for e in events if e["id"] == "M-1723220865111"][0]
    assert with_clip["video_event_id"] == "9001", with_clip
    assert with_clip["download_url"] == "https://relay.example.invalid/clip/9001", with_clip
    assert with_clip["clip_duration_ms"] == 30000, with_clip

    without_clip = [e for e in events if e["id"] == "M-1723220865386"][0]
    assert without_clip["video_event_id"] is None, without_clip
    assert without_clip["download_url"] is None, without_clip
    assert without_clip["thumbnail_url"] is None, without_clip

    # One batched enrichment call for the whole page, not one per entry: this
    # runs on every poll against an account whose rate limits are undocumented.
    video_calls = [u for u in telus.session.urls if "videoEvents" in u]
    assert len(video_calls) == 1, telus.session.urls

    print("  ok  enrichment: a correlated clip fills video_event_id/download_url, "
          "an uncorrelated one is null, and the page costs one batched call")


async def main():
    failures = 0
    tests = (test_the_keys_home_assistant_matches_a_press_on_survive_the_parse,
             test_the_same_press_in_two_languages_parses_identically,
             test_a_press_with_a_clip_is_enriched_and_one_without_stays_null)
    for test in tests:
        try:
            await test()
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
