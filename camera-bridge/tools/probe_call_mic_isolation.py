"""The 2x2 isolation probe for backlog E99's open defect: the doorbell MIC
feed reads near-silent during a real call, but healthy in a plain listen.

Measured before shelving (2026-08-30): during a live bridged call the raw
mic capture's median frame peak was -63 dB with a physical TAP on the housing
reaching only -26 dB, while a passive doorstep listen minutes later carried
normal speech at 2100-5900/32768 through the same rtsp_listen path and the
speaker played a test clip fine. An UNARMED continuous silent PUT does not
suppress the mic (measured: ambient identical). The two conditions a call
adds that were never isolated with speech present are the ARMED full-duplex
session and the video-pipeline pause -- this probe walks all four cells:

  A 0-15s   listen only (baseline)
  B 15-35s  StartFullDuplex ARMED + continuous silent PUT
  C 35-55s  video pipeline paused (deactivated), no PUT
  D 55-75s  paused AND armed + silent PUT   (the live-call condition)

then reactivates the video and prints a per-2s mic-peak timeline. Whichever
phase collapses names the mechanism; if none does, the defect lives in the
call bridge's own wiring, not the device.

RUN RULES: the operator must be AT the doorbell talking continuously for the
whole ~80 s (a speechless run is flagged invalid) and must have consented --
the video feed gaps during phases C/D. Nothing audible plays. Run inside the
bridge container, which has the session and dependencies:

    docker exec -i telus-cam-bridge python3 - \
        < camera-bridge/tools/probe_call_mic_isolation.py

CAUTION: this logs in as a standalone session; do not run it in a tight loop
(see the probe-login note in AGENTS/operator memory -- stacked fresh logins
are suspected of evicting the account's other sessions)."""
import asyncio, ssl, sys, time
sys.path.insert(0, "/app")
import aiohttp
from bridge import config as c
from bridge.rtsp_listen import RtspTunnelListen
from bridge.main import _load_fallback_credentials_if_present
from bridge.mobile_auth import MobileSession, mint_if_stale
from bridge.mws_client import get_doorbell_endpoints, start_full_duplex, end_live_stream
from bridge.telus_auth import TelusSession


async def main():
    _load_fallback_credentials_if_present()
    secret = open(c.CONTROL_SECRET_PATH).read().strip()
    async with TelusSession() as telus:
        await telus.login()
        mobile = MobileSession(telus)
        await mint_if_stale(mobile, asyncio.Lock())
        res = await get_doorbell_endpoints(
            telus.session, mobile.session_token, customer_id=mobile.customer_id,
            mobile_device_uid=mobile.device_uid,
            seamless_login_token=mobile.seamless_login_token)
        d = res["doorbells"][0]
        urls = d.get("rtsp_urls") or {}
        url = urls.get("direct") or urls.get("local")
        put = d["put_urls"].get("direct") or d["put_urls"].get("local")
        user, pw = d.get("stream_user"), d.get("stream_password")
        did = d.get("did")
        cam_id = d.get("camera_id")
        path_name = c.rtsp_path_for(cam_id) if cam_id else None

        t0 = time.monotonic()
        peaks, marks = {}, {}
        phase = {"cur": "A"}

        def on_audio(pcm, rate):
            b = int((time.monotonic() - t0) // 2)
            p = max(abs(int.from_bytes(pcm[i:i + 2], "little", signed=True))
                    for i in range(0, len(pcm), 2))
            peaks[b] = max(peaks.get(b, 0), p)
            marks[b] = phase["cur"]

        async def silent_put(seconds):
            sslctx = ssl.create_default_context()
            sslctx.check_hostname = False
            sslctx.verify_mode = ssl.CERT_NONE
            conn = aiohttp.TCPConnector(ssl=sslctx, limit=1, force_close=False)
            digest = aiohttp.DigestAuthMiddleware(login=user, password=pw)
            body = b"\xff" * 320
            async with aiohttp.ClientSession(
                    connector=conn, timeout=aiohttp.ClientTimeout(total=10),
                    middlewares=(digest,)) as s:
                end = time.monotonic() + seconds
                while time.monotonic() < end:
                    try:
                        async with s.put(put, data=body,
                                         headers={"Content-Type": "audio/basic"}) as r:
                            await r.read()
                    except Exception as e:  # noqa: BLE001
                        print("PUT err:", type(e).__name__)
                        return
                    await asyncio.sleep(0.04)

        async def ctl(method, path_):
            async with aiohttp.ClientSession() as s:
                async with s.request(
                        method, f"http://127.0.0.1:8199{path_}",
                        headers={"Authorization": f"Bearer {secret}"}) as r:
                    return r.status

        async def arm():
            ack = await start_full_duplex(
                telus.session, mobile.session_token, did,
                customer_id=mobile.customer_id,
                mobile_device_uid=mobile.device_uid,
                seamless_login_token=mobile.seamless_login_token)
            return ack.get("ok")

        async def disarm():
            try:
                await end_live_stream(
                    telus.session, mobile.session_token, did,
                    customer_id=mobile.customer_id,
                    mobile_device_uid=mobile.device_uid,
                    seamless_login_token=mobile.seamless_login_token)
            except Exception as e:  # noqa: BLE001
                print("disarm err:", type(e).__name__)

        L = RtspTunnelListen(url, user, pw, on_audio)
        await L.start()
        print("A: baseline -- TALK")
        await asyncio.sleep(15)

        phase["cur"] = "B"
        print("B: armed+PUT (ok=%s) -- TALK" % await arm())
        await silent_put(20)
        await disarm()

        st = await ctl("POST", f"/control/cameras/by-path/{path_name}/deactivate")
        phase["cur"] = "C"
        print(f"C: video paused ({st}) -- TALK")
        await asyncio.sleep(20)

        phase["cur"] = "D"
        print("D: paused AND armed+PUT (ok=%s) -- TALK" % await arm())
        await silent_put(20)
        await disarm()
        st = await ctl("POST", f"/control/cameras/by-path/{path_name}/activate")
        print(f"video reactivated ({st})")
        await L.stop()

        print("\nper-2s mic peak (A=base, B=armed+PUT, C=video off, D=call condition):")
        for b in sorted(peaks):
            bar = "#" * min(60, peaks[b] // 100)
            print(f"  [{marks.get(b, '?')}] {b * 2:>3}-{b * 2 + 2:<3}s  peak {peaks[b]:>6}  {bar}")
        for ph in "ABCD":
            vals = sorted(peaks[b] for b in peaks if marks.get(b) == ph)
            if vals:
                print(f"phase {ph}: n={len(vals)} median {vals[len(vals) // 2]} max {vals[-1]}")
        if all(v < 800 for v in peaks.values()):
            print("!! NO SPEECH DETECTED IN ANY PHASE -- run invalid; the operator "
                  "must talk continuously at the doorbell")

asyncio.run(main())
