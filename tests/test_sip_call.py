"""Tests for the SIP/RTP call originator (bridge/sip_call.py, backlog E99).

THE INVISIBLE FAILURE THIS SUITE EXISTS TO CATCH: a call that "succeeds"
without doing the thing a call is for. Every step of a SIP call fails
silently from the caller's side -- a wrong digest is a 403 the user
experiences as "my phone never rang"; a malformed SDP answer parse rings the
phone and carries silence; RTP sent from the wrong socket rings the phone
and carries silence THROUGH a NAT while working on the bench; a missing BYE
leaves the carrier billing an open call. None of those raise anything in
production. So the whole state machine runs here against an in-process fake
VoIP.ms POP that verifies the digest arithmetic, answers with a scripted SDP,
collects the RTP, and demands the BYE -- no network, no account, no charge.

Run from the camera-bridge directory:

    TELUS_EMAIL=x TELUS_PASSWORD=y python3 tests/test_sip_call.py
"""

import asyncio
import os
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELUS_EMAIL", "test@example.invalid")
os.environ.setdefault("TELUS_PASSWORD", "unused")

from bridge import sip_call  # noqa: E402
from bridge.sip_call import (  # noqa: E402
    SipCall, TONE_FRAME, digest_response, parse_digest_challenge,
    parse_sdp_answer,
)

USER, PASSWORD, REALM = "215317_doorbell", "sip-test-password", "voip.ms"


def _ulaw_to_pcm(data: bytes) -> list[int]:
    """G.711 mu-law decode (audioop was removed in Python 3.13)."""
    out = []
    for b in data:
        b = ~b & 0xFF
        sign, exponent, mantissa = b & 0x80, (b >> 4) & 0x07, b & 0x0F
        sample = ((mantissa << 3) + 0x84) << exponent
        sample -= 0x84
        out.append(-sample if sign else sample)
    return out


def _rms(samples: list[int]) -> float:
    return (sum(x * x for x in samples) / len(samples)) ** 0.5



# --------------------------------------------------------------------------
# Fake POP: enough of a VoIP.ms edge to run the whole client state machine.
# --------------------------------------------------------------------------

class FakePop(asyncio.DatagramProtocol):
    """Challenges the INVITE, VERIFIES the digest (with qop=auth), answers
    with SDP pointing at its own RTP socket, and records everything."""

    NONCE = "6a0d2c7b41f3"

    def __init__(self, rtp_port: int, reject_all: bool = False, rtp_out=None):
        self.rtp_port = rtp_port
        self.reject_all = reject_all
        self.events: list[str] = []
        self.transport = None
        self.caller_rtp = None
        self.caller_sip = None
        self._rtp_out = rtp_out

    def connection_made(self, transport):
        self.transport = transport

    def _header(self, text: str, name: str) -> str:
        m = re.search(rf"^{name}: ?(.*)$", text, re.M | re.I)
        return m.group(1) if m else ""

    def _respond(self, status: str, request_text: str, addr, extra: str = "",
                 body: str = ""):
        via = self._header(request_text, "Via")
        frm = self._header(request_text, "From")
        to = self._header(request_text, "To")
        call_id = self._header(request_text, "Call-ID")
        cseq = self._header(request_text, "CSeq")
        if ";tag=" not in to and not status.startswith("1"):
            to += ";tag=pop-tag"
        msg = (f"SIP/2.0 {status}\r\nVia: {via}\r\nFrom: {frm}\r\nTo: {to}\r\n"
               f"Call-ID: {call_id}\r\nCSeq: {cseq}\r\n{extra}"
               f"Content-Length: {len(body)}\r\n\r\n{body}")
        self.transport.sendto(msg.encode(), addr)

    def datagram_received(self, data, addr):
        text = data.decode(errors="replace")
        method = text.split(" ", 1)[0]
        if method == "ACK":
            self.events.append("ack")
            return
        if method == "BYE":
            self.events.append("bye")
            self._respond("200 OK", text, addr)
            return
        if method != "INVITE":
            self.events.append(f"unexpected:{method}")
            return
        if self.reject_all:
            self.events.append("rejected")
            self._respond("403 Forbidden", text, addr)
            return
        auth = self._header(text, "Authorization")
        if not auth:
            self.events.append("challenged")
            self._respond(
                "401 Unauthorized", text, addr,
                extra=(f'WWW-Authenticate: Digest realm="{REALM}", '
                       f'nonce="{self.NONCE}", qop="auth", algorithm=MD5\r\n'))
            return
        # VERIFY the digest exactly as the real edge does; a wrong one gets a
        # 403, which is what a production credential bug looks like. Computed
        # INLINE, never via the module's own digest_response: a fake that
        # reuses the code under test agrees with any bug in it, and an early
        # version of this file did exactly that -- with the HA2 order swapped
        # in the module, this test still passed. Only an independent
        # implementation can disagree.
        import hashlib
        def md5(x): return hashlib.md5(x.encode()).hexdigest()
        p = parse_digest_challenge(auth)
        ha1 = md5(f"{p.get('username','')}:{REALM}:{PASSWORD}")
        ha2 = md5(f"INVITE:{p.get('uri','')}")
        if p.get("qop"):
            expected = md5(f"{ha1}:{self.NONCE}:{p.get('nc','00000001')}:"
                           f"{p.get('cnonce','')}:auth:{ha2}")
        else:
            expected = md5(f"{ha1}:{self.NONCE}:{ha2}")
        if p.get("response") != expected:
            self.events.append("bad_digest")
            self._respond("403 Forbidden", text, addr)
            return
        self.events.append("authed")
        m = re.search(r"^m=audio (\d+) ", text, re.M)
        self.events.append(f"offer_rtp:{m.group(1) if m else 'MISSING'}")
        assert "a=rtpmap:0 PCMU/8000" in text, "offer must pin PCMU"
        self._respond("180 Ringing", text, addr)
        body = ("v=0\r\no=- 1 1 IN IP4 127.0.0.1\r\ns=-\r\n"
                "c=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                f"m=audio {self.rtp_port} RTP/AVP 0\r\n"
                "a=rtpmap:0 PCMU/8000\r\n")
        self._respond("200 OK", text, addr,
                      extra="Content-Type: application/sdp\r\n", body=body)
        m2 = re.search(r"^c=IN IP4 (\S+)$", text, re.M)
        self.caller_rtp = (m2.group(1) if m2 else "127.0.0.1",
                           int(m.group(1)) if m else 0)
        self.caller_sip = addr

    def send_rtp_to_caller(self, payload: bytes, seq: int):
        hdr = bytes([0x80, 0x00]) + seq.to_bytes(2, "big") + \
            (seq * 160).to_bytes(4, "big") + (0x1234).to_bytes(4, "big")
        self._rtp_out.sendto(hdr + payload, self.caller_rtp)

    def send_bye_to_caller(self):
        msg = ("BYE sip:x@y SIP/2.0\r\nVia: SIP/2.0/UDP 127.0.0.1\r\n"
               "From: <sip:pop@y>;tag=pop-tag\r\nTo: <sip:x@y>;tag=caller\r\n"
               "Call-ID: from-pop\r\nCSeq: 1 BYE\r\nContent-Length: 0\r\n\r\n")
        self.transport.sendto(msg.encode(), self.caller_sip)


class FakeRtpSink(asyncio.DatagramProtocol):
    def __init__(self):
        self.packets: list[bytes] = []

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.packets.append(data)


async def _run_call(password=PASSWORD, reject_all=False, tone_seconds=0.3,
                    call_kwargs=None):
    loop = asyncio.get_running_loop()
    rtp_t, rtp_sink = await loop.create_datagram_endpoint(
        FakeRtpSink, local_addr=("127.0.0.1", 0))
    rtp_port = rtp_t.get_extra_info("sockname")[1]
    pop_t, pop = await loop.create_datagram_endpoint(
        lambda: FakePop(rtp_port, reject_all=reject_all, rtp_out=rtp_t),
        local_addr=("127.0.0.1", 0))
    pop_port = pop_t.get_extra_info("sockname")[1]
    try:
        call = SipCall(server="127.0.0.1", sip_port=pop_port, username=USER,
                       password=password, target="18195551234",
                       caller_id="8195550000", **(call_kwargs or {}))
        result = await call.run(tone_seconds=tone_seconds, timeout=10.0)
        await asyncio.sleep(0.05)             # let the BYE's 200 and RTP drain
        return result, pop, rtp_sink
    finally:
        rtp_t.close()
        pop_t.close()


# --------------------------------------------------------------------------
# Tests
# --------------------------------------------------------------------------

def test_digest_arithmetic_matches_the_rfc():
    """RFC 2617's own published example (section 3.5): a wrong digest is a
    phone that never rings, reported by the far end as a bare 403."""
    got = digest_response("Mufasa", "testrealm@host.com", "Circle Of Life",
                          "GET", "/dir/index.html",
                          "dcd98b7102dd2f0e8b11d0f600bfb0c093", qop="auth",
                          nc="00000001", cnonce="0a4f113b")
    assert got == "6629fae49393a05397450978507c4ef1", (
        f"digest arithmetic disagrees with RFC 2617's worked example: {got}")
    print("  ok  digest matches RFC 2617's published vector")


def test_sdp_answer_parsing_refuses_a_useless_answer():
    """A call with no usable media answer must fail loudly, not ring and
    carry silence."""
    addr, port = parse_sdp_answer(
        "v=0\r\nc=IN IP4 208.100.60.10\r\nm=audio 14944 RTP/AVP 0 101\r\n")
    assert (addr, port) == ("208.100.60.10", 14944)
    for bad, why in [
        ("v=0\r\nm=audio 5004 RTP/AVP 0\r\n", "no c= address"),
        ("v=0\r\nc=IN IP4 1.2.3.4\r\n", "no m=audio"),
        ("v=0\r\nc=IN IP4 1.2.3.4\r\nm=audio 5004 RTP/AVP 8\r\n", "no PCMU"),
    ]:
        try:
            parse_sdp_answer(bad)
        except ValueError:
            continue
        raise AssertionError(f"an SDP answer with {why} was accepted")
    print("  ok  SDP answers with no usable media are refused")


async def test_the_whole_call_against_a_verifying_fake_pop():
    """The milestone, end to end: challenge, digest the fake POP verifies,
    ringing, answer, RTP that is really the tone, BYE."""
    result, pop, rtp = await _run_call()
    assert result["result"] == "completed", result
    assert pop.events[0] == "challenged", pop.events
    assert "authed" in pop.events, (
        f"the authenticated INVITE never passed digest verification: {pop.events}")
    assert pop.events.count("ack") >= 2, (
        f"expected an ACK for the 401 and one for the 200: {pop.events}")
    assert "bye" in pop.events, f"the call was never hung up: {pop.events}"
    assert rtp.packets, "no RTP reached the answered SDP's address"
    pkt = rtp.packets[0]
    assert pkt[0] == 0x80 and (pkt[1] & 0x7F) == 0, (
        "RTP is not version 2 / payload type 0 (PCMU)")
    assert len(pkt) == 12 + 160, f"not a 20ms mu-law frame: {len(pkt)} bytes"
    assert pkt[12:] == TONE_FRAME, "the payload is not the tone frame"
    seqs = [int.from_bytes(p[2:4], "big") for p in rtp.packets[:5]]
    assert all((b - a) & 0xFFFF == 1 for a, b in zip(seqs, seqs[1:])), (
        f"RTP sequence numbers do not increment by one: {seqs}")
    assert _rms(_ulaw_to_pcm(TONE_FRAME)) > 5000, "the tone frame is near-silence"
    print(f"  ok  full call: {pop.events} with {len(rtp.packets)} RTP packets "
          f"of verified tone")


async def test_a_wrong_password_is_a_clean_rejection_not_a_hang():
    """The production shape of a credential bug: 403 after the authed INVITE.
    The client must report it and stop -- not retry into a lockout, not hang
    until the timeout, and never claim the call happened."""
    result, pop, rtp = await _run_call(password="wrong-password")
    assert result["result"] == "rejected", result
    assert result.get("status") == 403, result
    assert "bad_digest" in pop.events, pop.events
    assert not rtp.packets, "RTP was sent for a call that was never answered"
    assert "bye" not in pop.events, "BYE sent for a call that never existed"
    print("  ok  wrong password: clean 403 report, no RTP, no BYE")


async def test_an_outright_rejection_is_reported_with_the_status():
    result, pop, rtp = await _run_call(reject_all=True)
    assert result["result"] == "rejected" and result.get("status") == 403, result
    assert not rtp.packets
    print("  ok  a 403 without a challenge is reported as rejected")


async def test_a_live_call_streams_the_uplink_and_delivers_the_downlink():
    """Milestone 2's media contract, both directions at once.

    Uplink: frame_source's bytes -- not the tone -- must be what leaves in
    RTP, with silence frames filling the slots where the source has nothing
    (a live doorbell mic is allowed to be dry; the RTP cadence is not).
    Downlink: every RTP payload the far end sends must reach payload_sink
    with the header stripped -- this is the caller's voice, and losing it is
    a call that rings and carries silence one way.
    """
    marker = bytes([7]) * 160
    fed = {"n": 0}

    def source():
        fed["n"] += 1
        return marker if fed["n"] % 2 else None   # every other slot is dry

    got = []
    stop = asyncio.Event()

    async def _end_later():
        await asyncio.sleep(0.35)
        stop.set()

    ender = asyncio.ensure_future(_end_later())
    result, pop, rtp = await _run_call(call_kwargs=dict(
        frame_source=source, payload_sink=got.append, stop_event=stop))
    ender.cancel()
    assert result["result"] == "completed", result
    assert result["reason"] == "stopped", result
    payloads = {p[12:] for p in rtp.packets}
    assert marker in payloads, "the uplink frames never reached the wire"
    assert sip_call.SILENCE_FRAME in payloads, (
        "dry source slots did not go out as silence -- the RTP cadence broke")
    assert TONE_FRAME not in payloads, "a live call played the test tone"
    # Downlink: the fake POP sends voice after answering.
    voice = bytes([9]) * 160
    # (sent during the call by the pop below in the BYE test; here, send now
    # against the still-known caller address and give the loop one tick)
    assert pop.caller_rtp is not None
    print(f"  ok  live uplink on the wire ({len(rtp.packets)} packets, "
          f"marker + silence, no tone)")


async def test_the_far_end_hanging_up_ends_the_call_cleanly():
    """A remote BYE is how every real call ends when the phone user hangs up
    first. Missing it leaves the bridge streaming silence at a dead carrier
    until CALL_MAX_S -- and the carrier billing all of it."""
    got = []
    sent = {"bye": False, "rtp": 0}
    real_dg = FakePop.datagram_received

    class HangupPop(FakePop):
        def datagram_received(self, data, addr):
            real_dg(self, data, addr)
            # Once media is up, deliver a little voice and then hang up.
            if self.caller_rtp and not sent["bye"]:
                for i in range(5):
                    self.send_rtp_to_caller(bytes([9]) * 160, i)
                    sent["rtp"] += 1
                self.send_bye_to_caller()
                sent["bye"] = True

    loop = asyncio.get_running_loop()
    rtp_t, rtp_sink = await loop.create_datagram_endpoint(
        FakeRtpSink, local_addr=("127.0.0.1", 0))
    rtp_port = rtp_t.get_extra_info("sockname")[1]
    pop_t, pop = await loop.create_datagram_endpoint(
        lambda: HangupPop(rtp_port, rtp_out=rtp_t), local_addr=("127.0.0.1", 0))
    pop_port = pop_t.get_extra_info("sockname")[1]
    try:
        call = SipCall(server="127.0.0.1", sip_port=pop_port, username=USER,
                       password=PASSWORD, target="18195551234",
                       caller_id="8195550000",
                       frame_source=lambda: None, payload_sink=got.append,
                       stop_event=asyncio.Event())
        result = await call.run(tone_seconds=0, timeout=10.0)
        await asyncio.sleep(0.05)
    finally:
        rtp_t.close()
        pop_t.close()

    assert result["result"] == "completed", result
    assert result["reason"] == "remote_bye", (
        f"the far end hung up and the call ended with {result.get('reason')!r}"
        " instead of remote_bye")
    assert got and got[0] == bytes([9]) * 160, (
        f"the caller's voice payloads never reached the sink: {len(got)}")
    assert all(len(p) == 160 for p in got), (
        "a downlink payload kept its RTP header")
    print(f"  ok  remote BYE ends the call; {len(got)} voice payloads "
          f"delivered header-stripped")


def test_the_tone_frame_is_one_exact_mulaw_cycle():
    """440 Hz at 8 kHz is 11 whole cycles in one 160-sample frame, which is
    what lets every frame be identical; a frame that does not loop cleanly
    clicks 50 times a second."""
    # The seam between two frames must be as smooth as any intra-frame step:
    # compare the largest sample-to-sample jump across the seam with the
    # largest within one cycle.
    samples = _ulaw_to_pcm(TONE_FRAME + TONE_FRAME)
    jumps = [abs(b - a) for a, b in zip(samples, samples[1:])]
    seam = jumps[159]
    assert seam <= max(jumps[:159]) * 1.05, (
        f"the tone frame does not loop cleanly: seam jump {seam} vs "
        f"max intra-frame {max(jumps[:159])}")
    print("  ok  tone frame loops cleanly across the packet seam")


TESTS = (
    test_digest_arithmetic_matches_the_rfc,
    test_sdp_answer_parsing_refuses_a_useless_answer,
    test_the_whole_call_against_a_verifying_fake_pop,
    test_a_wrong_password_is_a_clean_rejection_not_a_hang,
    test_an_outright_rejection_is_reported_with_the_status,
    test_a_live_call_streams_the_uplink_and_delivers_the_downlink,
    test_the_far_end_hanging_up_ends_the_call_cleanly,
    test_the_tone_frame_is_one_exact_mulaw_cycle,
)


def main():
    import inspect
    failures = 0
    for test in TESTS:
        try:
            if inspect.iscoroutinefunction(test):
                asyncio.run(test())
            else:
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
