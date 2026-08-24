"""Tests for the pure parsers and helpers behind the MWS/talkdown path.

These are the functions that turn Alarm.com's XML into the values the bridge
acts on. They were originally verified by hand against a live capture, which
means nothing re-checks them when someone edits a field name. Everything here is
pure: no network, no ffmpeg, no device.

The XML below is shaped exactly like a real response (attributes rather than
child elements, the abbreviated names the server actually uses) with every
account-specific value replaced by a placeholder.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_parsers.py
"""
import base64
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import config as c            # noqa: E402
from bridge import mws_client             # noqa: E402
from bridge.talk import TalkManager       # noqa: E402

# A GetDoorbellCameraList response, trimmed to the elements the parser reads.
GDCLR = '''<gdclr ts="0" dsl="false" s="true">
  <dcl>
    <cli model="ADC-VDB750" modelForDisplay="ADC-VDB750" cd="Doorbell"
         did="2048" UnitId="111111111" Firmware="02091"
         l="vieweruser" p="STREAMPW0123456"
         susa="true" sdsa="true" sfd="true" srt="true"
         StreamProtocol="0" SupportsWebRTC="false" VideoEnabled="false"
         sid="OPAQUESESSIONID" cstet="2026-08-23T03:36:55.8377899Z"
         re="rtsp://vieweruser:PW@relay.example.com:8090/api/GetStream?streamID=0"
         dre="rtsp://vieweruser:PW@198.51.100.7:41925/api/GetStream?streamID=0"
         lre="rtsp://vieweruser:PW@198.51.100.7:41925/api/GetStream?streamID=0"
         pre="rtsp://vieweruser:PW@203.0.113.9:41925/api/GetStream?streamID=0"
         uae="https://relay.example.com:8090/api/PutAudioStream"
         duae="https://198.51.100.7:41925/api/PutAudioStream"
         luae="https://198.51.100.7:41925/api/PutAudioStream"
         puae="https://203.0.113.9:41925/api/PutAudioStream" />
  </dcl>
</gdclr>'''

# Same call, but the account has no doorbell attached.
GDCLR_EMPTY = '<gdclr ts="0" s="true"><dcl /></gdclr>'
# Session token rejected: ts != 0 and an error message rather than data.
GDCLR_LOGGED_OUT = '<gdclr ts="1" s="false" em="Session expired" />'

# A GetLiveVideoStream response with one endpoint of each protocol, shaped like
# the anonymized capture in parse_live_video_stream's docstring. The srtp keys
# are the base64 of "testkey0123456789" / "videokey0123456789" so the tests can
# assert the DECODED bytes -- the camera consumes raw key+salt, not base64 text.
LVSR = '''<lvsr TokenStatus="0" Resolution="1440x1440" StreamId="1">
  <vsel>
    <vsei st="3">
      <webrtc SignalingServerUrl="https://sig.example/ws"
              SignalingServerToken="ST-abc"
              CameraAuthToken="CAM-xyz"
              HalfDuplexReenableAudioDelayInMs="500"
              MaxLoadTimeSeconds="30"/>
    </vsei>
    <vsei st="1">
      <srtp aip="203.0.113.9" ap="5006" ack="dGVzdGtleTAxMjM0NTY3ODk="
            acc="AES_CM_128" aca="HMAC_SHA1_80" ac="1"
            oassrc="3735928559" iassrc="3405692433" ivssrc="3405692433"
            vip="203.0.113.9" vp="5004" vck="dmlkZW9rZXkwMTIzNDU2Nzg5"
            sdp=""/>
    </vsei>
    <vsei st="0">
      <rtsp rtspurl="rtsp://cam.example/live" csda="false" bfmrs="200"/>
    </vsei>
  </vsel>
</lvsr>'''
# Session token rejected: no stream data, only the error envelope.
LVSR_LOGGED_OUT = '<lvsr TokenStatus="1" ErrorMessage="Session expired" />'


def test_doorbell_parser():
    out = mws_client.parse_doorbell_camera_list(GDCLR)
    assert out["ok"] is True and out["token_status"] == 0, out
    assert len(out["doorbells"]) == 1, out
    d = out["doorbells"][0]
    assert d["did"] == "2048" and d["unit_id"] == "111111111"
    assert d["global_device_id"] == "111111111-2048", d
    assert d["stream_user"] == "vieweruser"
    assert d["stream_password"] == "STREAMPW0123456"
    # The bridge shares the doorbell's LAN, so "direct" is what it should pick.
    assert d["put_urls"]["direct"] == "https://198.51.100.7:41925/api/PutAudioStream"
    assert d["put_urls"]["local"] == d["put_urls"]["direct"]
    assert d["put_urls"]["public"].startswith("https://203.0.113.9:")
    assert d["put_urls"]["cloud"].startswith("https://relay.example.com:")
    assert d["rtsp_urls"]["direct"].startswith("rtsp://")
    # Capability flags decide whether talkdown is even attempted.
    assert d["supports_upstream_audio"] is True
    assert d["supports_downstream_audio"] is True
    assert d["supports_full_duplex"] is True
    assert d["stream_protocol"] == 0
    assert d["expires"] == "2026-08-23T03:36:55.8377899Z"
    print("  ok  gdclr parser: ids, credentials, all four endpoint variants, flags")


def test_doorbell_parser_edge_cases():
    empty = mws_client.parse_doorbell_camera_list(GDCLR_EMPTY)
    assert empty["ok"] is True and empty["doorbells"] == [], empty

    out = mws_client.parse_doorbell_camera_list(GDCLR_LOGGED_OUT)
    assert out["ok"] is False, out
    assert out["token_status"] == mws_client.TOKEN_STATUS_LOGGED_OUT, out
    assert out["error"] == "Session expired", out

    try:
        mws_client.parse_doorbell_camera_list("<somethingelse/>")
        raise AssertionError("a response without <gdclr> should raise")
    except ValueError:
        pass
    print("  ok  gdclr edge cases: no doorbell, logged-out token status, wrong root")


def test_live_video_stream_parser():
    out = mws_client.parse_live_video_stream(LVSR)
    assert out["ok"] is True and out["token_status"] == mws_client.TOKEN_STATUS_OK, out
    assert out["error"] is None, out
    assert out["resolution"] == "1440x1440" and out["stream_id"] == 1, out
    assert [e["type"] for e in out["endpoints"]] == ["webrtc", "srtp", "rtsp"], out

    webrtc, srtp, rtsp = out["endpoints"]
    # webrtc is the primary talkdown path; every field here is what the bridge
    # would hand to the signaling client.
    assert webrtc["signaling_server_url"] == "https://sig.example/ws"
    assert webrtc["signaling_server_token"] == "ST-abc"
    assert webrtc["camera_auth_token"] == "CAM-xyz"
    assert webrtc["half_duplex_reenable_ms"] == 500
    assert webrtc["max_load_s"] == 30

    # ack_key/vck_key must be the DECODED bytes of ack/vck -- the camera
    # consumes the raw key+salt, and feeding it the base64 text instead would
    # produce garbled (not absent) audio.
    assert srtp["ack"] == "dGVzdGtleTAxMjM0NTY3ODk="
    assert srtp["ack_key"] == b"testkey0123456789", srtp
    assert srtp["vck"] == "dmlkZW9rZXkwMTIzNDU2Nzg5"
    assert srtp["vck_key"] == b"videokey0123456789", srtp
    assert srtp["aip"] == "203.0.113.9" and srtp["ap"] == 5006
    assert srtp["acc"] == "AES_CM_128" and srtp["aca"] == "HMAC_SHA1_80"
    assert srtp["ac"] == 1
    assert srtp["oassrc"] == 3735928559
    assert srtp["iassrc"] == 3405692433 and srtp["ivssrc"] == 3405692433
    assert srtp["vip"] == "203.0.113.9" and srtp["vp"] == 5004
    assert srtp["crypto_suite"] == mws_client.SRTP_CRYPTO_SUITE

    assert rtsp["rtspurl"] == "rtsp://cam.example/live"
    assert rtsp["csda"] is False
    assert rtsp["bfmrs"] == 200
    print("  ok  lvsr parser: envelope, webrtc/srtp/rtsp fields, decoded srtp keys")


def test_live_video_stream_edge_cases():
    # Namespace-tolerant, like the gdclr parser: a server-side xmlns must not
    # silently break every element lookup.
    namespaced = LVSR.replace("<lvsr ", '<lvsr xmlns="urn:example" ', 1)
    out = mws_client.parse_live_video_stream(namespaced)
    assert out["ok"] is True, out
    assert [e["type"] for e in out["endpoints"]] == ["webrtc", "srtp", "rtsp"], out
    assert out["endpoints"][0]["signaling_server_token"] == "ST-abc"
    assert out["endpoints"][1]["ack_key"] == b"testkey0123456789"

    # Rejected token: callers must see the failure, not an empty success.
    out = mws_client.parse_live_video_stream(LVSR_LOGGED_OUT)
    assert out["ok"] is False, out
    assert out["token_status"] == mws_client.TOKEN_STATUS_LOGGED_OUT, out
    assert out["error"] == "Session expired", out
    assert out["endpoints"] == [], out

    try:
        mws_client.parse_live_video_stream("<somethingelse/>")
        raise AssertionError("a response without <lvsr> should raise")
    except ValueError:
        pass
    print("  ok  lvsr edge cases: xmlns tolerated, logged-out token status, wrong root")


def test_decode_srtp_key():
    # Round-trip: the decode must give back exactly the original raw bytes
    # (an AES_CM_128 key+salt is 30 of them).
    known = bytes(range(30))
    assert mws_client.decode_srtp_key(base64.b64encode(known).decode()) == known
    # Malformed input yields None -- a missing key downstream, not a crash.
    assert mws_client.decode_srtp_key(None) is None
    assert mws_client.decode_srtp_key("") is None
    assert mws_client.decode_srtp_key("!!!not-base64!!!") is None
    print("  ok  decode_srtp_key: round-trip, None for None/empty/invalid input")


def test_action_acks():
    # Success is ts==0 AND s=="true" -- either alone is not enough.
    ok = mws_client._parse_action_ack('<sfdr ts="0" dsl="false" s="true" crs="0"/>', "sfdr")
    assert ok["ok"] is True and ok["success"] is True, ok

    refused = mws_client._parse_action_ack('<sfdr ts="0" s="false" em="Busy"/>', "sfdr")
    assert refused["ok"] is False and refused["error"] == "Busy", refused

    expired = mws_client._parse_action_ack('<sfdr ts="1" s="true"/>', "sfdr")
    assert expired["ok"] is False, "ts!=0 must not count as success"

    elsr = mws_client._parse_action_ack('<elsr ts="0" s="true" he="false"/>', "elsr")
    assert elsr["ok"] is True, elsr

    # A missing root is reported, not raised: teardown runs best-effort and must
    # not turn a parse surprise into an exception that masks the real error.
    missing = mws_client._parse_action_ack("<other/>", "elsr")
    assert missing["ok"] is False and "no <elsr>" in missing["error"], missing
    print("  ok  action acks: ts+s both required, teardown parse never raises")


def test_defusedxml_is_a_hard_import():
    """A build without defusedxml must fail at import, not fall back to stdlib
    ElementTree -- the fallback would drop entity-expansion protection with no
    indication, which is exactly the silent failure this suite exists to catch.
    Simulated by importing both MWS modules in a subprocess whose meta path
    blocks defusedxml."""
    import subprocess
    blocker = (
        "import sys\n"
        "class _Block:\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name == 'defusedxml' or name.startswith('defusedxml.'):\n"
        "            raise ImportError('defusedxml blocked for test')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _Block())\n"
        "for mod in ('bridge.mws_client', 'bridge.mobile_auth'):\n"
        "    try:\n"
        "        __import__(mod)\n"
        "    except ImportError:\n"
        "        pass\n"
        "    else:\n"
        "        print('IMPORTED', mod)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", blocker],
        cwd=str(pathlib.Path(__file__).resolve().parents[1]),
        capture_output=True, text=True, env=dict(os.environ))
    assert "IMPORTED" not in proc.stdout, (
        "an MWS module imported without defusedxml -- the silent stdlib "
        f"fallback is back: {proc.stdout!r} {proc.stderr!r}")
    print("  ok  defusedxml hard import: MWS modules refuse to load without it")


def test_retry_after():
    assert c.parse_retry_after("120") == 120.0
    soon = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=90))
    assert 80 < c.parse_retry_after(soon) < 95, "HTTP-date form must be honoured"
    past = format_datetime(datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert c.parse_retry_after(past) == 0.0, "a past date means retry now, not 60s"
    assert c.parse_retry_after(None) == 60.0
    assert c.parse_retry_after("soon-ish") == 60.0
    print("  ok  retry-after: delta-seconds, HTTP-date, past date, missing, garbage")


def test_session_deadline():
    """cstet is only trusted when it is plausibly a future expiry."""
    assert TalkManager._session_deadline(None) is None
    assert TalkManager._session_deadline("not a timestamp") is None
    # The value seen in a real capture sat essentially at fetch time -- exactly the
    # case that must NOT be treated as a session expiry.
    now_ish = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    assert TalkManager._session_deadline(now_ish) is None, \
        "a timestamp at ~now must not be trusted as an expiry"
    far = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
    deadline = TalkManager._session_deadline(far)
    assert deadline is not None and deadline > 0, "a plausible future expiry should be used"
    print("  ok  session deadline: implausible cstet ignored, future expiry accepted")


def main():
    failures = 0
    for test in (test_doorbell_parser, test_doorbell_parser_edge_cases,
                 test_live_video_stream_parser, test_live_video_stream_edge_cases,
                 test_decode_srtp_key,
                 test_action_acks, test_defusedxml_is_a_hard_import,
                 test_retry_after, test_session_deadline):
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
