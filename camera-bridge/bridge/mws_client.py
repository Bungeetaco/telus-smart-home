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

Most MWS actions reply in XML, but the newer camera siren / privacy-mode
ones reply in JSON with an equivalent envelope; they get their own
_post_mws_json/_parse_json_ack pair at the bottom of this module.
"""
import base64
import json
import logging
from xml.etree import ElementTree as ET

import aiohttp

# GetLiveVideoStream is a BaseTokenRequest, so it carries the same device/token
# envelope every MWS action does. Reuse mobile_auth's canonical assembly of that
# envelope (HAIKU/APP_BUILD/_device_fields) and its Cloudflare-429 type so the
# stream path funnels into the identical Retry-After backoff as the auth path.
from .mobile_auth import HAIKU, APP_BUILD, _device_fields, _read_body_text
from . import config as c
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



def _parse_xml(body):
    """Parse an MWS XML response, preferring defusedxml so a hostile/oversized
    response can't drive an XML attack; falls back to stdlib ElementTree when
    defusedxml isn't installed. Mirrors mobile_auth._parse_login_xml's pattern
    (this module parses responses from the same server)."""
    try:
        import defusedxml.ElementTree as DET
        return DET.fromstring(body)
    except ImportError:
        return ET.fromstring(body)

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
        raise RateLimited(c.parse_retry_after(resp.headers.get("Retry-After")))


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
    root = _parse_xml(xml)
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


# ---------------------------------------------------------------------------
# Full-duplex talkdown (ADC-VDB750 doorbell)
# ---------------------------------------------------------------------------
# The VDB750 exposes NO <webrtc>/<srtp> upstream-audio endpoint (GetLiveVideoStream
# returns only <rtsp>, video + listen). Its talkdown path, recovered end-to-end
# from a real capture (specs/fullduplex_talkdown.md), is:
#
#   1. GetDoorbellCameraList -> <gdclr><dcl><cli ...>. The <cli> element already
#      carries, per doorbell, the ready-to-use per-session upstream-audio PUT URLs
#      (uae=cloud, duae/luae=direct/LAN, puae=public/WAN), the matching downstream
#      RTSP URLs (re/dre/lre/pre), and the Digest credentials for them: user attr
#      `l` ("vieweruser") + password attr `p`. Also did/UnitId, susa (supports
#      upstream audio), sdsa, sfd (supports full duplex), StreamProtocol, sid, and
#      cstet (session expiry). The port is ephemeral/per-session.
#   2. StartFullDuplex&DeviceId=<did> -> <sfdr ts=0 s=true .../> arms the mic-input
#      path. (ts==0 AND s=="true" == success.)
#   3. Chunked HTTP-Digest PUT of G.711 mu-law/8k/mono to the chosen PutAudioStream
#      URL (talk_fullduplex.stream_talkdown) -- NOT in this module.
#   4. EndLiveStream&DeviceId=<did>&ForDevice=0 -> <elsr ts=0 s=true .../> teardown.
#
# These are BaseTokenRequests carrying the same device/token envelope every MWS
# action uses (see get_live_video_stream). Confirmed attribute names/nesting from
# the capture -- no more live probing needed for the protocol itself.

# MWS DeviceId of the doorbell for the full-duplex actions. The authoritative did
# is read from the <cli> element; this is only a fallback default.
DOORBELL_DEVICE_ID = 2048


def _base_token_form(action, session_token, *, customer_id=None,
                     mobile_device_uid=None, seamless_login_token=None, **extra):
    """Assemble the BaseTokenRequest envelope shared by every MWS token action.

    Mirrors get_live_video_stream exactly: the full app device envelope
    (_device_fields, which includes Haiku/ApplicationBuildNumber) when a device
    uid is threaded through, else the minimal Haiku/build fallback; then Action +
    SessionToken + ForceServerError, optional CustomerId/SeamlessLoginToken, and
    any per-action ``extra`` params (None-valued extras are dropped)."""
    if mobile_device_uid:
        form = _device_fields(mobile_device_uid)
    else:
        form = {"Haiku": HAIKU, "ApplicationBuildNumber": APP_BUILD,
                "UseNewSessionManager": "true"}
    form.update({
        "Action": action,
        "SessionToken": session_token,
        "ForceServerError": "false",
    })
    if customer_id is not None:
        form["CustomerId"] = str(customer_id)
    if seamless_login_token:
        form["SeamlessLoginToken"] = seamless_login_token
    for k, v in extra.items():
        if v is not None:
            form[k] = v
    return form


async def _post_mws(session, form):
    """POST a form-urlencoded MWS action and return the (gunzipped) body text.
    Funnels 429s into telus_auth.RateLimited exactly like get_live_video_stream."""
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    async with session.post(MWS_URL, data=form, headers=headers) as resp:
        _raise_if_rate_limited(resp)
        resp.raise_for_status()
        return _read_body_text(await resp.read())


def parse_doorbell_camera_list(xml):
    """Parse a GetDoorbellCameraList <gdclr> response into a plain dict.

    Pure function -- no I/O. Returns::

        {"token_status": int, "ok": bool, "error": str|None,
         "doorbells": [ {...}, ... ]}

    Each doorbell dict (one per <cli>) carries::

        did, unit_id, global_device_id ("<unit>-<did>"), model, name (cd),
        stream_user (l), stream_password (p),      # Digest creds for PutAudioStream
        put_urls  {"cloud":uae, "direct":duae, "local":luae, "public":puae},
        rtsp_urls {"cloud":re,  "direct":dre,  "local":lre,  "public":pre},
        supports_upstream_audio (susa: bool), supports_downstream_audio (sdsa),
        supports_full_duplex (sfd: bool), stream_protocol (int),
        session_id (sid), expires (cstet)

    Blank ("") URL/credential attributes normalise to None. Namespace-tolerant.
    """
    root = _parse_xml(xml)
    gdclr = root if _local(root.tag) == "gdclr" else None
    if gdclr is None:
        for el in root.iter():
            if _local(el.tag) == "gdclr":
                gdclr = el
                break
    if gdclr is None:
        raise ValueError("no <gdclr> element in GetDoorbellCameraList response")

    ts = _int(gdclr, "ts", _int(gdclr, "TokenStatus", 3))
    out = {
        "token_status": ts,
        "ok": ts == TOKEN_STATUS_OK,
        "error": (_attr(gdclr, "em") or _attr(gdclr, "e") or None),
        "doorbells": [],
    }

    for cli in gdclr.iter():
        if _local(cli.tag) != "cli":
            continue

        def a(name):  # non-empty attribute or None
            v = _attr(cli, name)
            return v if v else None

        did = a("did")
        unit = a("UnitId")
        out["doorbells"].append({
            "did": did,
            "unit_id": unit,
            "global_device_id": (f"{unit}-{did}" if unit and did else did),
            "model": a("model"),
            "name": a("cd"),
            "stream_user": a("l"),
            "stream_password": a("p"),
            "put_urls": {
                "cloud": a("uae"),
                "direct": a("duae"),
                "local": a("luae"),
                "public": a("puae"),
            },
            "rtsp_urls": {
                "cloud": a("re"),
                "direct": a("dre"),
                "local": a("lre"),
                "public": a("pre"),
            },
            "supports_upstream_audio": _attr(cli, "susa", "false") == "true",
            "supports_downstream_audio": _attr(cli, "sdsa", "false") == "true",
            "supports_full_duplex": _attr(cli, "sfd", "false") == "true",
            "stream_protocol": _int(cli, "StreamProtocol", -1),
            "session_id": a("sid"),
            "expires": a("cstet"),
        })

    return out


def _parse_action_ack(xml, root_name):
    """Parse a StartFullDuplex/EndLiveStream/EndFullDuplex ack.

    Both <sfdr>/<elsr>/<efdr> carry ``ts`` (TokenStatus) and ``s`` (success
    "true"/"false"). Returns {"token_status", "ok", "success", "error"}; ok means
    ts==0 AND s==true. A missing root is reported (ok False) rather than raised so
    a best-effort teardown never masks the real error with a parse crash."""
    root = _parse_xml(xml)
    el = root if _local(root.tag) == root_name else None
    if el is None:
        for x in root.iter():
            if _local(x.tag) == root_name:
                el = x
                break
    if el is None:
        return {"token_status": None, "ok": False, "success": False,
                "error": f"no <{root_name}> in response"}
    ts = _int(el, "ts", _int(el, "TokenStatus", 3))
    success = _attr(el, "s", "false") == "true"
    return {
        "token_status": ts,
        "ok": ts == TOKEN_STATUS_OK and success,
        "success": success,
        "error": (_attr(el, "em") or _attr(el, "e") or None),
    }


async def get_doorbell_endpoints(session, session_token, *, customer_id=None,
                                 mobile_device_uid=None, seamless_login_token=None,
                                 max_height=1440, max_width=1440):
    """POST GetDoorbellCameraList and return parse_doorbell_camera_list's dict.

    The camera item it returns already carries the per-session PutAudioStream URLs
    and Digest stream password -- no separate stream-open call is needed before
    StartFullDuplex. ``session_token`` is the legacy MWS SessionToken."""
    form = _base_token_form(
        "GetDoorbellCameraList", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        MaxHeight=str(max_height), MaxWidth=str(max_width))
    log.debug("GetDoorbellCameraList (SessionToken/slt/Haiku redacted)")
    return parse_doorbell_camera_list(await _post_mws(session, form))


async def start_full_duplex(session, session_token, device_id=DOORBELL_DEVICE_ID, *,
                            customer_id=None, mobile_device_uid=None,
                            seamless_login_token=None):
    """POST StartFullDuplex&DeviceId=<device_id> to arm the doorbell mic-input
    path. Returns the ack dict (ok == ts 0 and s true).

    Opens a real full-duplex session server-side -- never call against the live
    account without an explicit decision."""
    form = _base_token_form(
        "StartFullDuplex", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        DeviceId=str(device_id))
    log.debug("StartFullDuplex DeviceId=%s (SessionToken/slt/Haiku redacted)", device_id)
    return _parse_action_ack(await _post_mws(session, form), "sfdr")


async def end_live_stream(session, session_token, device_id=DOORBELL_DEVICE_ID,
                          for_device=FOR_DEVICE_DOORBELL, *, customer_id=None,
                          mobile_device_uid=None, seamless_login_token=None):
    """POST EndLiveStream&DeviceId=<device_id>&ForDevice=<for_device> to tear the
    full-duplex session down. This is the teardown the app actually issues after a
    StartFullDuplex talk (an EndFullDuplex action also exists; EndLiveStream is
    what was captured). Best-effort: the caller runs it in a finally and tolerates
    a non-ok result."""
    form = _base_token_form(
        "EndLiveStream", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        DeviceId=str(device_id), ForDevice=str(for_device))
    log.debug("EndLiveStream DeviceId=%s (SessionToken/slt/Haiku redacted)", device_id)
    return _parse_action_ack(await _post_mws(session, form), "elsr")


async def end_full_duplex(session, session_token, device_id=DOORBELL_DEVICE_ID, *,
                          customer_id=None, mobile_device_uid=None,
                          seamless_login_token=None):
    """POST EndFullDuplex&DeviceId=<device_id>. Returns the same ack shape as
    end_live_stream.

    The app tears a talk down with EndLiveStream (that is what was captured), and
    that remains the primary path. This exists as a FALLBACK for the case where
    EndLiveStream fails: leaving the doorbell's mic-input path armed is the one
    teardown failure with a lasting effect, so it is worth a second attempt by a
    different route rather than only a log line. Not sent on the happy path --
    doubling every teardown would double MWS traffic against a rate-limited
    account for no observed benefit.
    """
    form = _base_token_form(
        "EndFullDuplex", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        DeviceId=str(device_id))
    log.debug("EndFullDuplex DeviceId=%s (SessionToken/slt/Haiku redacted)", device_id)
    return _parse_action_ack(await _post_mws(session, form), "efdr")


# ---------------------------------------------------------------------------
# Camera siren + privacy mode (JSON-envelope MWS actions)
# ---------------------------------------------------------------------------
# Same form-urlencoded BaseTokenRequest POST as everything above -- only the
# RESPONSE differs: these actions reply in JSON (the app parses them with Moshi)
# with an envelope that means what the XML ts/s pair means: isSuccess,
# ErrorMessage, DidSeamlesslyLogin. Hence the separate _post_mws_json/
# _parse_json_ack pair rather than bending the XML parser at them.
#
# Provenance and caveats: docs/mobile-api-findings.md. The app issues
# GetAllSirenDevices/GetAllCameraPrivacyModeInfo on every camera screen, but the
# account this project was built against exposes no UI for either feature, so the
# commands were recovered from the decompiled APK and NO live invocation has ever
# been observed. Treat every first call here as an experiment.
#
# ValidateSession is the odd one out in this group: the only capture of it is XML
# (<vsr ... siv="true">), so validate_session sniffs the body and parses either.

# Envelope fields (lowercased for a case-insensitive match) that belong to
# BaseMwsResponse rather than to the action, and so are stripped out of "data".
_JSON_ENVELOPE_KEYS = ("issuccess", "errormessage", "didseamlesslylogin",
                       "tokenstatus")


def _jget(payload: dict, name: str, default=None):
    """Case-insensitively read ``name`` from a decoded MWS JSON object.

    alarm.com mixes casing inside one object (``isSuccess`` next to
    ``ErrorMessage``), and the field names here come from decompiled Moshi
    adapters rather than from a live capture, so an exact-case lookup is a
    single-character away from silently reading None. Returns ``default`` when
    absent."""
    if not isinstance(payload, dict):
        return default
    if name in payload:
        return payload[name]
    lower = name.lower()
    for k, v in payload.items():
        if isinstance(k, str) and k.lower() == lower:
            return v
    return default


def _json_bool(value, default: bool = False) -> bool:
    """Coerce an MWS JSON scalar to a bool, accepting real booleans, 0/1 and the
    "true"/"false" strings.

    The request side of this API is all strings ("true"/"false"), and the
    response side has not been seen live, so a plain ``bool(value)`` would turn
    the string "false" -- truthy in Python -- into True. Unrecognised values
    yield ``default``."""
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes")
    return default


def _json_token_status(payload: dict) -> int | None:
    """The envelope's token status, or None when it does not report one.

    The XML actions carry this as the ``ts`` attribute (falling back to
    ``TokenStatus``); no live capture of a JSON action's envelope exists, so the
    JSON spelling is read the same two ways and None is returned when neither is
    present or neither is an integer. None means "the server said nothing about
    the token" -- callers must NOT read that as TOKEN_STATUS_OK, because a
    logged-out session that simply does not report it would then look healthy.

    ``ts`` is only accepted when it parses as an int: unlike the XML attribute
    namespace, an action's JSON body could plausibly carry a ``ts`` of its own.
    """
    for name in ("TokenStatus", "ts"):
        value = _jget(payload, name)
        if value is None:
            continue
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _parse_json_ack(payload: dict) -> dict:
    """Read the JSON MWS envelope and return
    ``{"ok", "error", "data", "token_status"}``.

    Pure function -- no I/O, safe to unit-test with a literal dict. ``ok`` is
    isSuccess; ``error`` is ErrorMessage or None; ``data`` is the payload with
    the envelope keys removed, i.e. the action's own body with the UPSTREAM key
    names untouched (``{"AllCameraSirenInfo": [...]}``,
    ``{"CameraPrivacyModeInfo": {...}}``) so a caller can match it directly
    against docs/mobile-api-findings.md.

    ``token_status`` is the envelope's token state (see _json_token_status) or
    None, which is what lets a caller re-mint an expired SessionToken once
    instead of surfacing a spurious failure.

    Traps: it fails CLOSED -- a payload with no isSuccess reports ok False, on
    the principle that an unrecognised shape is not a success. And for the write
    actions, ok False does NOT prove the command failed to reach the camera;
    re-firing ManualSirenStart on a False could sound a siren twice. Re-read
    get_siren_devices instead of retrying blind."""
    if not isinstance(payload, dict):
        return {"ok": False, "data": {}, "token_status": None,
                "error": f"unexpected MWS JSON payload type {type(payload).__name__}"}
    err = _jget(payload, "ErrorMessage")
    if _json_bool(_jget(payload, "DidSeamlesslyLogin")):
        # Not an error: the server transparently re-logged the session in, which
        # means the SeamlessLoginToken the caller holds has rolled.
        log.debug("MWS JSON action reports DidSeamlesslyLogin")
    return {
        "ok": _json_bool(_jget(payload, "isSuccess")),
        "error": (str(err) or None) if err else None,
        "token_status": _json_token_status(payload),
        "data": {k: v for k, v in payload.items()
                 if not (isinstance(k, str) and k.lower() in _JSON_ENVELOPE_KEYS)},
    }


async def _post_mws_json(session: aiohttp.ClientSession, form: dict) -> dict:
    """POST a form-urlencoded MWS action whose response is JSON and return the
    decoded object.

    Identical transport to _post_mws (the gzip-without-Content-Encoding fix and
    the 429 -> telus_auth.RateLimited funnel); only the decode differs.

    Raises ValueError when the body is not JSON, so an HTML error page or an XML
    fault from the Cloudflare edge cannot be mistaken for a well-formed "not ok"
    ack. The message quotes only a short prefix of the RESPONSE -- never the
    form, which carries SessionToken/slt/Haiku; the actions routed through here
    return no tokens of their own (the login actions that do are posted through
    mobile_auth, not this helper)."""
    body = await _post_mws(session, form)
    try:
        return json.loads(body)
    except ValueError as exc:
        raise ValueError(
            f"MWS action returned a non-JSON body ({exc}): {body[:120]!r}") from exc


def _mws_device_id(device_id) -> str:
    """Normalise a camera id to the numeric MWS DeviceId these actions expect.

    Accepts either the bare MWS id (``2048``) or the website/bridge
    globalDeviceId form ``"<unit>-<did>"``, and returns the part after the last
    '-', matching how parse_doorbell_camera_list BUILDS that id
    (``f"{unit}-{did}"``) and how talk.py matches one. Note this is the opposite
    end from the split inside get_live_video_stream, whose device-id handling has
    never been confirmed against a live capture -- do not "make them agree" by
    copying that one."""
    return str(device_id).rsplit("-", 1)[-1]


async def get_siren_devices(session: aiohttp.ClientSession, session_token: str, *,
                            customer_id: str | int | None = None,
                            mobile_device_uid: str | None = None,
                            seamless_login_token: str | None = None) -> dict:
    """POST GetAllSirenDevices and return the _parse_json_ack dict.

    Read-only. On success ``data["AllCameraSirenInfo"]`` is a list of
    ``{"DeviceId", "isActive", "timestampEnd", "canManuallyTriggerSiren"}``.

    Traps for the caller:

    * ``canManuallyTriggerSiren`` is a PER-CAMERA PERMISSION, not a capability
      flag -- the app has a dedicated ManualCameraSirenPermissionChecker for it.
      A camera advertising a siren is not the same as this account being allowed
      to fire it, so this read is the mandatory precondition for offering or
      calling start_siren, and a false value must be honoured rather than
      probed.
    * The siren is TIME-BOUNDED: ``timestampEnd`` is the server-chosen end. An
      ``isActive`` true with an empty ``timestampEnd`` is an error case in the
      app, so treat it as "state unknown", not as "runs forever".
    * No live invocation of the siren actions has ever been observed on this
      project's account (the feature has no UI there; the wire format comes from
      the decompiled app), so this read is also the cheapest way to sanity-check
      the shape before anything is fired."""
    form = _base_token_form(
        "GetAllSirenDevices", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token)
    log.debug("GetAllSirenDevices (SessionToken/slt/Haiku redacted)")
    return _parse_json_ack(await _post_mws_json(session, form))


async def start_siren(session: aiohttp.ClientSession, session_token: str,
                      device_id: str | int, *,
                      customer_id: str | int | None = None,
                      mobile_device_uid: str | None = None,
                      seamless_login_token: str | None = None) -> dict:
    """POST ManualSirenStart&DeviceId=<id>. Returns the _parse_json_ack dict.

    ``device_id`` may be the MWS numeric id or a ``"<unit>-<did>"``
    globalDeviceId (see _mws_device_id).

    THIS SOUNDS A REAL SIREN IN SOMEONE'S HOME. Only ever call it because a
    caller explicitly asked for it -- never as part of a read, a refresh or a
    retry loop.

    Traps:

    * Call get_siren_devices first and honour ``canManuallyTriggerSiren`` for
      this camera; it is a per-account permission the server enforces separately
      from the device having a siren at all.
    * The siren is time-bounded by the server (``timestampEnd`` in
      get_siren_devices); stop_siren is how you end it EARLY, and is the only
      way to -- there is no duration parameter to shorten.
    * No live invocation has ever been observed on this project's account, so a
      first call is an EXPERIMENT: run it on a device where an unexpected siren
      is acceptable, with someone present who can stop it.
    * ok False does not prove nothing happened (see _parse_json_ack); confirm
      with get_siren_devices rather than firing again."""
    did = _mws_device_id(device_id)
    form = _base_token_form(
        "ManualSirenStart", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        DeviceId=did)
    log.info("ManualSirenStart DeviceId=%s (SessionToken/slt/Haiku redacted)", did)
    return _parse_json_ack(await _post_mws_json(session, form))


async def stop_siren(session: aiohttp.ClientSession, session_token: str,
                     device_id: str | int, *,
                     customer_id: str | int | None = None,
                     mobile_device_uid: str | None = None,
                     seamless_login_token: str | None = None) -> dict:
    """POST ManualSirenStop&DeviceId=<id>. Returns the _parse_json_ack dict.

    This is the way to end a running siren before the server's ``timestampEnd``;
    the siren is time-bounded, so a stop that never lands means the siren keeps
    sounding until that timestamp rather than forever.

    Same experimental status as start_siren -- no live invocation has ever been
    observed on this project's account -- but this is the SAFE half of the pair:
    if a start is ever attempted, have the stop call ready first. Stopping a
    siren that is not sounding is expected to be a no-op, so a caller may retry
    it (unlike start_siren) when the result is not ok."""
    did = _mws_device_id(device_id)
    form = _base_token_form(
        "ManualSirenStop", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        DeviceId=did)
    log.info("ManualSirenStop DeviceId=%s (SessionToken/slt/Haiku redacted)", did)
    return _parse_json_ack(await _post_mws_json(session, form))


async def get_privacy_mode_info(session: aiohttp.ClientSession, session_token: str, *,
                                customer_id: str | int | None = None,
                                mobile_device_uid: str | None = None,
                                seamless_login_token: str | None = None) -> dict:
    """POST GetAllCameraPrivacyModeInfo and return the _parse_json_ack dict.

    Read-only. On success ``data["CameraPrivacyModeInfo"]`` carries
    ``{"DeviceId", "State"}``.

    Traps: despite the "All" in the action name, the recovered shape is a SINGLE
    object, not a list -- code defensively for both rather than assuming either.
    And privacy mode is separately gated by ``SupportsPrivacyMode``, which is
    false for the cameras on the account tested, so a device appearing here is
    not proof set_privacy_mode will be accepted for it."""
    form = _base_token_form(
        "GetAllCameraPrivacyModeInfo", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token)
    log.debug("GetAllCameraPrivacyModeInfo (SessionToken/slt/Haiku redacted)")
    return _parse_json_ack(await _post_mws_json(session, form))


async def set_privacy_mode(session: aiohttp.ClientSession, session_token: str,
                           device_id: str | int, active: bool, *,
                           customer_id: str | int | None = None,
                           mobile_device_uid: str | None = None,
                           seamless_login_token: str | None = None) -> dict:
    """POST SetCameraPrivacyModeState&DeviceId=<id>&IsActive=<true|false> and
    return the _parse_json_ack dict, whose ``data["CameraPrivacyModeInfo"]``
    echoes ``{"DeviceId", "State"}``.

    A WRITE with a lasting effect: privacy mode on stops the camera recording
    and it STAYS on until something turns it back off -- so it is only ever
    called because a caller asked for that exact state, never as a side effect.

    Traps: ``IsActive`` goes on the wire as the string "true"/"false" (the whole
    request is form-urlencoded; a Python bool would be sent as "True" and read as
    neither). Cameras on the account tested report ``SupportsPrivacyMode`` false,
    so expect a refusal even though the action exists -- check support before
    offering the control, and report an unsupported device upward instead of
    retrying. Like the siren actions, this has never been invoked live."""
    did = _mws_device_id(device_id)
    form = _base_token_form(
        "SetCameraPrivacyModeState", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token,
        DeviceId=did, IsActive=("true" if active else "false"))
    log.info("SetCameraPrivacyModeState DeviceId=%s IsActive=%s "
             "(SessionToken/slt/Haiku redacted)", did, bool(active))
    return _parse_json_ack(await _post_mws_json(session, form))


def _parse_validate_session(body: str) -> dict:
    """Parse a ValidateSession response into the _parse_json_ack shape.

    Pure function -- no I/O. Returns ``{"ok", "error", "data"}`` with
    ``data == {"session_is_valid": bool, "token_status": int|None}``.

    ValidateSession sits with the JSON actions in the app's newer code but the
    one captured response is XML (``<vsr ts="0" siv="true"/>``), so the body is
    sniffed: a JSON object goes through _parse_json_ack (with ``siv`` surfaced as
    session_is_valid if present), anything else is parsed as XML. A missing
    <vsr> is reported as not-ok rather than raised -- a liveness probe that
    throws is worse than one that answers "no"."""
    stripped = body.lstrip()
    if stripped.startswith("{"):
        ack = _parse_json_ack(json.loads(stripped))
        siv = _jget(ack["data"], "siv", _jget(ack["data"], "SessionIsValid"))
        ack["data"]["session_is_valid"] = _json_bool(siv, ack["ok"])
        return ack

    root = _parse_xml(body)
    el = root if _local(root.tag) == "vsr" else None
    if el is None:
        for x in root.iter():
            if _local(x.tag) == "vsr":
                el = x
                break
    if el is None:
        return {"ok": False, "error": "no <vsr> in ValidateSession response",
                "data": {"session_is_valid": False, "token_status": None}}
    ts = _int(el, "ts", _int(el, "TokenStatus", 3))
    siv = _attr(el, "siv", "false") == "true"
    return {
        "ok": ts == TOKEN_STATUS_OK and siv,
        "error": (_attr(el, "em") or _attr(el, "e") or None),
        "data": {"session_is_valid": siv, "token_status": ts},
    }


async def validate_session(session: aiohttp.ClientSession, session_token: str, *,
                           customer_id: str | int | None = None,
                           mobile_device_uid: str | None = None,
                           seamless_login_token: str | None = None) -> dict:
    """POST ValidateSession and return ``{"ok", "error", "data"}`` where
    ``data`` is ``{"session_is_valid": bool, "token_status": int|None}``.

    A read-only liveness check for an MWS SessionToken -- cheaper and more
    direct than inferring auth health from a failed action.

    Traps: the app calls this constantly and this bridge must not. The account's
    rate limits are undocumented and a stampede can lock it, so call it on
    demand (before a talk, after a suspected expiry), never on a poll. It also
    does not refresh anything: an ok False means re-mint via mobile_auth, and the
    same body-sniffing caveat as _parse_validate_session applies (the captured
    response is XML even though its siblings here are JSON)."""
    form = _base_token_form(
        "ValidateSession", session_token, customer_id=customer_id,
        mobile_device_uid=mobile_device_uid, seamless_login_token=seamless_login_token)
    log.debug("ValidateSession (SessionToken/slt/Haiku redacted)")
    return _parse_validate_session(await _post_mws(session, form))
