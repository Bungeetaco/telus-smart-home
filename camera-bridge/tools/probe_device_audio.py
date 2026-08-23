"""Probe a doorbell/camera for inbound-audio (talkdown) capabilities.

Answers "what audio paths does this device actually offer, and can any of them
carry something better than G.711 mu-law?" without guessing from the app's
strings. Written while closing out the L16 question on an ADC-VDB750 (see
docs/talkdown-fullduplex-protocol.md, E4); kept because the same questions come
up for any other model someone points this project at.

What it does, all read-only:

  1. one MWS call (GetDoorbellCameraList -- the same one every talk makes) to
     learn the device's current session URL, credentials and capability flags;
  2. GETs a list of paths to map the device's local API surface, including the
     OSP backchannel names that appear in the vendor's native streaming library;
  3. PUTs 40ms of mu-law SILENCE to the upstream-audio endpoint with various
     codec selectors, to see whether it negotiates a format at all;
  4. speaks RTSP OPTIONS/DESCRIBE at the media port, since RTSP ANNOUNCE+RECORD
     would be a standard route for inbound RTP (and thus L16).

Nothing audible is played and nothing is written to the device.

Run inside the bridge container, which already has the session and dependencies:

    docker exec -i telus-cam-bridge python3 - < camera-bridge/tools/probe_device_audio.py

Interpreting it: a device that answers "Unknown request" for every path except
the two it really implements, ignores every codec selector, and speaks HTTP
rather than RTSP on its media port, has exactly one inbound audio path and one
codec. That is the VDB750 result -- see the E4 entry in the protocol doc.
"""
import asyncio
import hashlib
import re
import ssl
import sys
from urllib.parse import urlsplit

import aiohttp

sys.path.insert(0, "/app")
from bridge.main import _load_fallback_credentials_if_present  # noqa: E402
from bridge.mobile_auth import MobileSession, mint_if_stale    # noqa: E402
from bridge.mws_client import get_doorbell_endpoints           # noqa: E402
from bridge.telus_auth import TelusSession                     # noqa: E402

# Known-good endpoints first as controls, then the OSP/backchannel names from
# libvideo-streamer.so, then a few plausible capability endpoints.
PROBE_PATHS = [
    "/api/GetStream?streamID=0",
    "/api/PutAudioStream",
    "/ws/ospserver",
    "/osp",
    "/api/osp",
    "/api/StartBackchannel",
    "/api/GetAudioStream",
    "/api/PutAudioStreamL16",
    "/api/capabilities",
    "/api/GetDeviceInfo",
]


def _h(*parts):
    return hashlib.md5(":".join(parts).encode()).hexdigest()


def _digest_header(user, pw, method, uri, chal):
    ha1 = _h(user, chal.get("realm", ""), pw)
    ha2 = _h(method, uri)
    parts = [f'username="{user}"', f'realm="{chal.get("realm", "")}"',
             f'nonce="{chal.get("nonce", "")}"', f'uri="{uri}"',
             f'response="{_h(ha1, chal.get("nonce", ""), ha2)}"']
    return "Digest " + ", ".join(parts)


async def rtsp(host, port, url, user, pw, method="OPTIONS", extra="", use_tls=True):
    """One RTSP request over the media port, retried once after a 401.

    TLS by default: on this device family the media port is the same TLS port
    that serves HTTPS, so a plaintext request gets no reply at all."""
    ctx = None
    if use_tls:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    reader, writer = await asyncio.open_connection(host, port, ssl=ctx)
    try:
        chal, cseq = {}, 1
        for attempt in (1, 2):
            req = f"{method} {url} RTSP/1.0\r\nCSeq: {cseq}\r\nUser-Agent: probe\r\n"
            if chal:
                req += f"Authorization: {_digest_header(user, pw, method, url, chal)}\r\n"
            req += extra + "\r\n"
            writer.write(req.encode())
            await writer.drain()
            text = (await asyncio.wait_for(reader.read(4096), timeout=8)).decode("utf-8", "replace")
            if text.split("\r\n")[0].find("401") != -1 and attempt == 1:
                chal = {k: v for k, v in re.findall(r'(\w+)="([^"]*)"',
                                                    re.search(r'WWW-Authenticate:\s*Digest\s*(.*)',
                                                              text, re.I).group(1))}
                cseq += 1
                continue
            return text
    finally:
        writer.close()
    return ""


async def main() -> int:
    _load_fallback_credentials_if_present()
    async with TelusSession() as telus:
        await telus.login()
        mobile = MobileSession(telus)
        await mint_if_stale(mobile, asyncio.Lock())
        res = await get_doorbell_endpoints(
            telus.session, mobile.session_token,
            customer_id=mobile.customer_id,
            mobile_device_uid=mobile.device_uid,
            seamless_login_token=mobile.seamless_login_token)
        if not res.get("ok") or not res.get("doorbells"):
            print("could not read device endpoints:",
                  res.get("token_status"), res.get("error"))
            return 1

        d = res["doorbells"][0]
        put = d["put_urls"].get("direct") or d["put_urls"].get("local")
        if not put:
            print("device reports no direct/local upstream-audio endpoint")
            return 1
        base = put.rsplit("/api/", 1)[0]
        user, pw = d.get("stream_user"), d.get("stream_password")
        print(f"model={d.get('model')} firmware-flags: susa={d['supports_upstream_audio']} "
              f"sdsa={d['supports_downstream_audio']} sfd={d['supports_full_duplex']} "
              f"stream_protocol={d['stream_protocol']}")

        auth = aiohttp.DigestAuthMiddleware(login=user, password=pw)
        conn = aiohttp.TCPConnector(ssl=False, limit=2)
        async with aiohttp.ClientSession(connector=conn,
                                         timeout=aiohttp.ClientTimeout(total=8),
                                         middlewares=(auth,)) as s:
            print("\n--- local API surface (404 / 'Unknown request' = not implemented) ---")
            for path in PROBE_PATHS:
                try:
                    async with s.get(base + path) as r:
                        print(f"  {r.status:>3}  GET {path:<32} {(await r.read())[:60]!r}")
                except Exception as err:  # noqa: BLE001
                    print(f"  ---  GET {path:<32} {type(err).__name__}: {err}")

            print("\n--- does the upstream-audio endpoint negotiate a codec? ---")
            silence = b"\xff" * 320          # 40ms of mu-law silence
            for label, url, extra in [
                ("baseline (no hint)", base + "/api/PutAudioStream", {}),
                ("?codec=L16", base + "/api/PutAudioStream?codec=L16", {}),
                ("?format=L16", base + "/api/PutAudioStream?format=L16", {}),
                ("?encoding=L16", base + "/api/PutAudioStream?encoding=L16", {}),
                ("?payloadType=97", base + "/api/PutAudioStream?payloadType=97", {}),
                ("X-Audio-Codec: L16", base + "/api/PutAudioStream", {"X-Audio-Codec": "L16"}),
                ("Content-Type audio/L16", base + "/api/PutAudioStream",
                 {"Content-Type": "audio/L16;rate=8000;channels=1"}),
            ]:
                headers = {"User-Agent": "GStreamer souphttpclientsink libsoup/3.6.0",
                           "Content-Type": "application/octet-stream"}
                headers.update(extra)
                try:
                    async with s.put(url, data=silence, headers=headers) as r:
                        print(f"  {r.status:>3}  {label:<26} {(await r.read())[:60]!r}")
                except Exception as err:  # noqa: BLE001
                    print(f"  ---  {label:<26} {type(err).__name__}: {err}")

        rtsp_url = d["rtsp_urls"].get("direct") or d["rtsp_urls"].get("local")
        if rtsp_url:
            sp = urlsplit(rtsp_url)
            host, port = sp.hostname, sp.port or 554
            clean = f"rtsp://{host}:{port}{sp.path}" + (f"?{sp.query}" if sp.query else "")
            print(f"\n--- media port: is it really RTSP? (ANNOUNCE+RECORD would carry RTP) ---")
            opts = await rtsp(host, port, clean, user, pw, "OPTIONS")
            if not opts.strip():
                opts = await rtsp(host, port, clean, user, pw, "OPTIONS", use_tls=False)
            first = next((l for l in opts.splitlines() if l.strip()), "(no reply)")
            server = re.search(r"Server:\s*(.*)", opts, re.I)
            print(f"  status line: {first}")
            if server:
                print(f"  server:      {server.group(1).strip()}")
            methods = (re.search(r"Public:\s*(.*)", opts, re.I) or [None, ""])[1]
            for want in ("ANNOUNCE", "RECORD", "SET_PARAMETER"):
                print(f"  {want:<14} {'ADVERTISED' if want in methods else 'not advertised'}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
