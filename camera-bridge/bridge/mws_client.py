"""GetLiveVideoStream MWS client + XML response parser for the TELUS/alarm.com
ADC-VDB750 doorbell.

Recovered from the decompiled com.alarm.alarmmobile.android.telus 5.10.7 app
(see specs/mws_getlivevideostream.md). Unlike the rest of this bridge -- which
talks to the smarthome-security.telus.com *website* API with the AJAX/cookie
session telus_auth.py holds -- this hits the legacy mobile web service (MWS) at
mobile.alarm.com/MobileServlet/SubmitRequest.aspx, which speaks
x-www-form-urlencoded POSTs and replies in XML.

The MWS endpoint is the one that returns the WebRTC/SRTP endpoints carrying the
upstream (talkdown) audio path; the website /videoSources endpoint the bridge
already uses only gives the downstream (video) RTSP/Janus feed.

IMPORTANT -- this needs a legacy MWS ``SessionToken``, which is NOT the website
session telus_auth.py holds today. Acquiring that token is a separate, still
unbuilt concern; until it exists this call returns TokenStatus==1 (logged out).
Pass the token in; this module does not mint it.
"""
import base64
import logging
from xml.etree import ElementTree as ET

import aiohttp

# GetLiveVideoStream is a BaseTokenRequest, so it carries the same device/token
# envelope every MWS action does. Reuse mobile_auth's canonical assembly of that
# envelope (HAIKU/APP_BUILD/_device_fields) and its Cloudflare-429 type so the
# stream path funnels into the identical Retry-After backoff as the auth path.
from .mobile_auth import HAIKU, APP_BUILD, _device_fields, _read_body_text
from .telus_auth import RateLimited

log = logging.getLogger(__name__)

MWS_URL = "https://mobile.alarm.com/MobileServlet/SubmitRequest.aspx"

# k90.j LiveVideoDeviceEnum ordinal-value. The ADC-VDB750 doorbell is 0.
FOR_DEVICE_DOORBELL = 0

# k90.n StreamProtocolEnum. Value absent/unknown defaults to RTSP in the app.
STREAM_TYPES = {-1: "unknown", 0: "rtsp", 1: "srtp", 3: "webrtc",
                4: "mjpeg", 5: "oesp", 6: "rtsps"}

# TokenStatus semantics (BaseMwsResponse; confirmed against the sibling
# GetOpenEyeStreamProperties handler lk0.c / jy0.c). Only 0 yields usable data.
TOKEN_STATUS_OK = 0             # success -> stream data present
TOKEN_STATUS_LOGGED_OUT = 1    # session token invalid/expired -> re-auth
TOKEN_STATUS_NO_PERMISSION = 2  # account lacks the entitlement -> don't retry
TOKEN_STATUS_SERVER_ERROR = 3  # transient server error -> back off and retry

# Fixed in the app code for every SRTP endpoint.
SRTP_CRYPTO_SUITE = "AES_CM_128_HMAC_SHA1_80"


def _local(tag: str) -> str:
    """Local (namespace-stripped) name of an ElementTree tag.

    ElementTree stores a namespaced element as ``{uri}name``; the alarm.com
    response has been observed unprefixed, but we stay tolerant either way so a
    server-side namespace change can't silently break every lookup."""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _find(el, name):
    """First child of ``el`` whose local tag name matches, namespace-tolerant."""
    if el is None:
        return None
    for child in el:
        if _local(child.tag) == name:
            return child
    return None


def _attr(el, name, default=None):
    """MWS scalar fields are element attributes. Matched by local name so a
    namespaced attribute (unusual, but possible) still resolves."""
    if el is None:
        return default
    v = el.get(name)
    if v is None:
        for k, val in el.attrib.items():
            if _local(k) == name:
                return val
        return default
    return v


def _int(el, name, default):
    try:
        return int(_attr(el, name, str(default)))
    except (TypeError, ValueError):
        return default


def decode_srtp_key(b64: str | None) -> bytes | None:
    """Base64-decode an SRTP crypto key (vck/ack). The concatenated
    AES_CM_128 key+salt the camera expects is these raw bytes, not the base64
    text. Returns None on empty/invalid input rather than raising, so a
    malformed key surfaces as a missing key downstream instead of a parse
    crash."""
    if not b64:
        return None
    try:
        return base64.b64decode(b64)
    except (ValueError, TypeError):
        return None


def _raise_if_rate_limited(resp: aiohttp.ClientResponse) -> None:
    """mobile.alarm.com sits behind the same Cloudflare edge as
    auth-gateway.telus.com, so GetLiveVideoStream can 429 exactly like the auth
    POSTs. Raise the shared telus_auth.RateLimited (carrying the server's
    Retry-After) BEFORE resp.raise_for_status(), so a caller sleeps the
    server-requested wait rather than tight-retrying a generic ClientResponseError
    against the same edge. Mirrors mobile_auth.MobileSession._raise_if_rate_limited.
    """
    if resp.status == 429:
        try:
            retry_after_s = float(resp.headers.get("Retry-After"))
        except (TypeError, ValueError):
            retry_after_s = 60.0
        raise RateLimited(retry_after_s)


async def get_live_video_stream(session: aiohttp.ClientSession,
                                session_token: str,
                                device_id: str,
                                for_device: int = FOR_DEVICE_DOORBELL,
                                customer_id: str | None = None,
                                mobile_device_uid: str | None = None,
                                seamless_login_token: str | None = None,
                                bmx_call_guid: str | None = None) -> dict:
    """POST GetLiveVideoStream to the MWS servlet and return the parsed dict.

    ``session_token`` is the legacy MWS SessionToken (see module docstring --
    NOT the website session). ``device_id`` may be the website's globalDeviceId
    form ("111937238-2048"); the numeric part before the first '-' is sent as
    the MWS DeviceId. ``for_device`` is the LiveVideoDeviceEnum value (0 for the
    doorbell). ``customer_id`` is the CustomerId (dcid from the login response);
    GetLiveVideoStream is a BaseTokenRequest and the app DOES send CustomerId on
    it (account/device scoping), so it is included when provided.
    ``mobile_device_uid``/``seamless_login_token`` come from the MobileSession
    that minted ``session_token`` and, when present, let this request carry the
    same device envelope the app attaches to every token request.
    ``bmx_call_guid`` is a Butterfly-MX intercom GUID, irrelevant to the
    doorbell; omit it.

    Never call this against the live account without an explicit decision to do
    so -- it opens a real stream server-side.
    """
    numeric_id = str(device_id).split("-", 1)[0]

    # Build the full BaseTokenRequest device/token envelope the app attaches to
    # EVERY token request (decompiled jl0/a.java: CustomerId, TwoFactorId,
    # UseNewSessionManager, MobileDeviceUid, ApplicationBuildNumber, Haiku, the
    # device-identity fields, and -- when an slt exists -- SeamlessLoginToken).
    # mws_client previously sent only Action/SessionToken/DeviceId/ForDevice,
    # which omitted CustomerId (scoping) and Haiku (app-integrity gate) and made
    # server rejection likely. We send the full app set first; a reviewer should
    # trim it empirically against ONE live capture (see specs/mws_getlivevideostream.md).
    if mobile_device_uid:
        form = _device_fields(mobile_device_uid)
    else:
        # No device uid threaded through: fall back to the minimal envelope plus
        # the app-integrity Haiku, which gates the alarm.com endpoints.
        form = {"Haiku": HAIKU, "ApplicationBuildNumber": APP_BUILD,
                "UseNewSessionManager": "true"}
    form.update({
        "Action": "GetLiveVideoStream",
        "SessionToken": session_token,
        "DeviceId": numeric_id,
        "ForDevice": str(for_device),
        "ForceServerError": "false",
    })
    if customer_id is not None:
        form["CustomerId"] = str(customer_id)
    if seamless_login_token:
        form["SeamlessLoginToken"] = seamless_login_token
    if bmx_call_guid:
        form["BmxCallGUID"] = bmx_call_guid

    # SessionToken / SeamlessLoginToken / Haiku must never be logged -- log only
    # the non-secret routing fields.
    log.debug("GetLiveVideoStream DeviceId=%s ForDevice=%s customer_id=%s "
              "(SessionToken/slt/Haiku redacted)", numeric_id, for_device, customer_id)

    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with session.post(MWS_URL, data=form, headers=headers) as resp:
        _raise_if_rate_limited(resp)
        resp.raise_for_status()
        body = _read_body_text(await resp.read())
    return parse_live_video_stream(body)


def parse_live_video_stream(xml: str) -> dict:
    """Parse a GetLiveVideoStream XML response into a plain dict.

    Pure function -- no I/O, safe to unit-test with a literal string. Returns::

        {
            "token_status": int,   # 0 == success (TOKEN_STATUS_OK)
            "ok": bool,            # token_status == 0
            "error": str | None,
            "resolution": str | None,
            "stream_id": int,
            "endpoints": [ {"type": "webrtc"|"srtp"|"rtsp"|..., ...fields}, ... ],
        }

    Each endpoint dict carries a ``type`` plus the fields for that protocol:

    * webrtc: signaling_server_url, signaling_server_token, camera_auth_token,
      half_duplex_reenable_ms, max_load_s  (primary talkdown path)
    * srtp:   aip, ap, ack (base64 str), ack_key (decoded bytes), acc, aca, ac,
      oassrc, iassrc, ivssrc, plus the video side (vip/vp/vck/vck_key/vcc/vca),
      sdp and crypto_suite  (secondary talkdown path; oassrc is the app->camera
      audio SSRC)
    * rtsp:   rtspurl, csda, bfmrs  (video only -- NOT a talkdown path)

    Namespace-tolerant. Success is TokenStatus == 0.

    Sample (minimal) response::

        <lvsr TokenStatus="0" Resolution="1440x1440" StreamId="1">
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
        </lvsr>
    """
    root = ET.fromstring(xml)
    lvsr = root if _local(root.tag) == "lvsr" else None
    if lvsr is None:
        for el in root.iter():
            if _local(el.tag) == "lvsr":
                lvsr = el
                break
    if lvsr is None:
        raise ValueError("no <lvsr> element in MWS GetLiveVideoStream response")

    token_status = _int(lvsr, "ts", _int(lvsr, "TokenStatus", 3))
    # ErrorMessage is the BaseMwsResponse field; 'e' is the video-response
    # error string. Prefer the former, fall back to the latter.
    error = (_attr(lvsr, "em") or _attr(lvsr, "e") or _attr(lvsr, "ErrorMessage") or "") or None

    out = {
        "token_status": token_status,
        "ok": token_status == TOKEN_STATUS_OK,
        "error": error,
        "resolution": _attr(lvsr, "Resolution", "") or None,
        "stream_id": _int(lvsr, "StreamId", 1),
        "endpoints": [],
    }

    vsel = _find(lvsr, "vsel")
    if vsel is None:
        return out

    for vsei in vsel:
        if _local(vsei.tag) != "vsei":
            continue
        st = _int(vsei, "st", -1)

        webrtc = _find(vsei, "webrtc")
        srtp = _find(vsei, "srtp")
        rtsp = _find(vsei, "rtsp")

        if webrtc is not None:
            out["endpoints"].append({
                "type": "webrtc",
                "st": st,
                "signaling_server_url": _attr(webrtc, "SignalingServerUrl", "") or None,
                "signaling_server_token": _attr(webrtc, "SignalingServerToken", ""),
                "camera_auth_token": _attr(webrtc, "CameraAuthToken", ""),
                "half_duplex_reenable_ms": _int(webrtc, "HalfDuplexReenableAudioDelayInMs", 0),
                "max_load_s": _int(webrtc, "MaxLoadTimeSeconds", -1),
            })
        elif srtp is not None:
            ack = _attr(srtp, "ack", "")
            vck = _attr(srtp, "vck", "")
            out["endpoints"].append({
                "type": "srtp",
                "st": st,
                "sdp": _attr(srtp, "sdp", ""),
                # upstream (app->camera) audio -- the talkdown-relevant side
                "aip": _attr(srtp, "aip", ""),
                "ap": _int(srtp, "ap", 5006),
                "ack": ack,                        # base64 text as sent
                "ack_key": decode_srtp_key(ack),   # raw key+salt bytes
                "acc": _attr(srtp, "acc", ""),
                "aca": _attr(srtp, "aca", ""),
                "ac": _int(srtp, "ac", 2),
                "oassrc": _int(srtp, "oassrc", 0xDEADBEEF),  # outgoing (talkdown) audio SSRC
                "iassrc": _int(srtp, "iassrc", 0xCAFED00D),  # incoming audio SSRC
                "ivssrc": _int(srtp, "ivssrc", 0xCAFED00D),  # incoming video SSRC
                # downstream video side (kept for completeness)
                "vip": _attr(srtp, "vip", "-1"),
                "vp": _int(srtp, "vp", 5004),
                "vck": vck,
                "vck_key": decode_srtp_key(vck),
                "vcc": _attr(srtp, "vcc", ""),
                "vca": _attr(srtp, "vca", ""),
                "crypto_suite": SRTP_CRYPTO_SUITE,
            })
        elif rtsp is not None:
            out["endpoints"].append({
                "type": "rtsp",
                "st": st,
                "rtspurl": _attr(rtsp, "rtspurl", ""),
                "csda": _attr(rtsp, "csda", "false") == "true",
                "bfmrs": _int(rtsp, "bfmrs", 200),
            })
        else:
            # vsei with no recognised child; record its declared type so a
            # caller can see the endpoint existed but wasn't understood.
            out["endpoints"].append({
                "type": STREAM_TYPES.get(st, "unknown"),
                "st": st,
            })

    return out
