"""Minimal SIP/RTP call originator for the doorbell call feature (backlog E99).

MILESTONE 1 ONLY: place one authenticated call through the VoIP.ms POP to a
configured number, play a test tone on answer, hang up cleanly, and report a
timeline of what happened. This proves INVITE, digest auth, SDP negotiation,
NAT traversal and two-way RTP end to end before any doorbell audio is spliced
in. Milestone 2 replaces the tone generator with the doorbell's two audio
paths, which are already G.711 mu-law at 8 kHz -- the same codec this module
pins in its SDP offer -- so the splice is packet plumbing, not transcoding.

WHY HAND-ROLLED RATHER THAN A SIP LIBRARY. The call leg needs exactly one
scenario: authenticated outbound INVITE, PCMU-only, symmetric RTP, BYE. The
Python SIP libraries that cover it are either alpha-quality and thread-based
(pyVoIP) or a C toolkit with its own event loop to embed (pjsua2), and either
would be the project's heaviest dependency for what is ~300 lines of RFC 3261
subset. The repo already crossed this bridge once: talkdown re-implements its
protocol from captures rather than importing a stack, for the same reason.

DELIBERATE LIMITS, stated rather than discovered:
  * UAC only. Nothing listens for inbound calls; REGISTER is never sent
    (registration exists to route INBOUND calls; an authenticated INVITE
    stands on its own).
  * MD5 digest only (RFC 2617, with and without qop=auth). VoIP.ms offers
    nothing stronger on SIP.
  * One call at a time, enforced by the caller (control_server holds a lock).
  * Retransmission is a bounded resend of the INVITE while nothing has been
    heard, not a full RFC 3261 timer set. The transport to the POP is one UDP
    hop to a carrier; if it is lossy enough to need the full state machine,
    the call was not going to sound like anything anyway.

NAT: the container runs with host networking; the sub-account is provisioned
nat=yes and the SIP requests carry `rport`, so the POP replies to the source
address/port it saw. RTP is symmetric: this module sends its tone FROM the
same socket it told the far end to send TO (the SDP's m= port), and it starts
sending immediately on answer so the carrier's media relay latches the
mapping.

Secrets: the SIP password is used for digest computation only. It is never
logged, and it never appears in a SIP message (that is the point of digest).
"""

import asyncio
import hashlib
import logging
import math
import os
import random
import re
import socket
import time

from . import config as c

log = logging.getLogger(__name__)

# G.711 mu-law encoding of a 450 Hz sine at a comfortable level, one 20 ms
# frame (160 samples at 8 kHz), precomputed once. The frequency must be a
# multiple of 50 Hz (8000/160) so a frame holds a WHOLE number of cycles --
# 450 Hz is exactly 9 -- making every frame identical and the tone continuous
# across packet seams. The first draft used 440 Hz, which is 8.8 cycles: each
# frame ended mid-cycle and the repeated frame clicked 50 times a second. The
# suite's seam test exists because that mistake is inaudible in any assertion
# short of comparing the boundary jump against the intra-cycle jumps.
_TONE_HZ = 450
_FRAME_SAMPLES = 160


def _mulaw_encode(sample: int) -> int:
    """Encode one 16-bit PCM sample as G.711 mu-law (G.711 with the standard
    0x84 bias). Reference implementation; runs once at import for 160 samples."""
    BIAS, CLIP = 0x84, 32635
    sign = 0x80 if sample < 0 else 0
    if sample < 0:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


# One 20 ms frame of mu-law digital silence (0xFF encodes linear 0), sent
# whenever a live call has no fresh uplink frame: RTP must keep flowing at
# 50 packets/s or the far end's jitter buffer declares the stream dead, and
# the carrier's NAT latch (see the module docstring) needs the cadence too.
SILENCE_FRAME = b"\xff" * _FRAME_SAMPLES

# 16-bit linear -> mu-law, precomputed for the uplink path: the doorbell's
# decoded mic audio arrives as s16 PCM and re-encoding it per sample with the
# function above would put a python loop on a 50 Hz hot path. Indexed by
# (sample >> 2) & 0x3FFF is NOT valid mu-law (the low bits matter near zero),
# so the table is full 16-bit, built once at import (~64k entries, <1 MB).
LIN2ULAW = bytes(_mulaw_encode(x - 32768) for x in range(65536))


def pcm_s16le_to_mulaw(pcm: bytes) -> bytes:
    """Encode little-endian s16 mono PCM to mu-law using the table."""
    t = LIN2ULAW
    return bytes(t[(pcm[i] | (pcm[i + 1] << 8)) ^ 0x8000]
                 for i in range(0, len(pcm) - 1, 2))


# mu-law -> 16-bit linear, the decode direction (downlink shaping decodes the
# caller's mu-law to PCM, shapes it for the speaker, and re-encodes). Built once.
def _ulaw_decode_table():
    out = []
    for b in range(256):
        u = ~b & 0xFF
        sign, exp, mant = u & 0x80, (u >> 4) & 0x07, u & 0x0F
        sample = ((mant << 3) + 0x84) << exp
        sample -= 0x84
        out.append(-sample if sign else sample)
    return out


ULAW2LIN = _ulaw_decode_table()


def mulaw_to_pcm_s16le(mulaw: bytes) -> bytes:
    """Decode mu-law to little-endian s16 mono PCM using the table."""
    t = ULAW2LIN
    out = bytearray(len(mulaw) * 2)
    for i, b in enumerate(mulaw):
        v = t[b]
        out[2 * i] = v & 0xFF
        out[2 * i + 1] = (v >> 8) & 0xFF
    return bytes(out)


TONE_FRAME = bytes(
    _mulaw_encode(int(12000 * math.sin(2 * math.pi * _TONE_HZ * i / 8000)))
    for i in range(_FRAME_SAMPLES)
)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def digest_response(username: str, realm: str, password: str, method: str,
                    uri: str, nonce: str, qop: str | None = None,
                    nc: str = "00000001", cnonce: str = "") -> str:
    """RFC 2617 digest, MD5, with and without qop=auth.

    Kept as a pure function so the test suite can verify it against a known
    vector: a wrong digest fails silently in production (the POP just answers
    403 and the phone never rings), which is exactly the class of failure the
    suites exist for.
    """
    ha1 = _md5(f"{username}:{realm}:{password}")
    ha2 = _md5(f"{method}:{uri}")
    if qop:
        return _md5(f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}")
    return _md5(f"{ha1}:{nonce}:{ha2}")


def parse_digest_challenge(header: str) -> dict:
    """The WWW-/Proxy-Authenticate header's parameters, unquoted."""
    params = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,\s]+))', header):
        params[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    return params


def parse_sdp_answer(body: str) -> tuple[str, int]:
    """The far end's RTP address and port from an SDP answer.

    Raises ValueError rather than guessing: a call with no usable media
    answer must fail loudly, not ring and carry silence.
    """
    addr = None
    port = None
    for line in body.splitlines():
        line = line.strip()
        if line.startswith("c=IN IP4 "):
            addr = line.split()[-1]
        elif line.startswith("m=audio "):
            parts = line.split()
            port = int(parts[1])
            # PCMU is payload type 0; the answer must include it, because the
            # offer contained nothing else to fall back to.
            if "0" not in parts[3:]:
                raise ValueError(f"answer does not accept PCMU: {line}")
    if not addr or not port:
        raise ValueError("SDP answer carries no audio address/port")
    return addr, port


def _parse_response(datagram: bytes) -> tuple[int, str, dict, str]:
    """(status, reason, headers-lowercased, body) of one SIP response."""
    text = datagram.decode(errors="replace")
    head, _, body = text.partition("\r\n\r\n")
    lines = head.split("\r\n")
    m = re.match(r"SIP/2\.0 (\d{3}) ?(.*)", lines[0])
    if not m:
        raise ValueError(f"not a SIP response: {lines[0]!r}")
    headers: dict = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers.setdefault(name.strip().lower(), value.strip())
    return int(m.group(1)), m.group(2), headers, body


class _Sip(asyncio.DatagramProtocol):
    def __init__(self):
        self.queue: asyncio.Queue = asyncio.Queue()
        self.transport = None

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.queue.put_nowait(data)


class _Rtp(asyncio.DatagramProtocol):
    def __init__(self, payload_sink=None):
        self.received = 0
        self.transport = None
        self._sink = payload_sink

    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.received += 1
        if self._sink is not None and len(data) > 12:
            # Strip the fixed RTP header. CSRC lists/extensions are not sent
            # on a plain PCMU leg; if one ever appears the payload is noise
            # for one packet, which the doorbell's speaker absorbs better
            # than a parser absorbs a malformed header.
            try:
                self._sink(data[12:])
            except Exception:
                log.exception("rtp payload sink failed; dropping this payload")


def _local_ip_toward(host: str, port: int) -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, port))
        return s.getsockname()[0]
    finally:
        s.close()


class SipCall:
    """One outbound call: INVITE/auth/ACK, tone RTP, BYE. See module docstring."""

    def __init__(self, *, server: str, username: str, password: str,
                 target: str, caller_id: str, sip_port: int = 5060,
                 frame_source=None, payload_sink=None,
                 stop_event: "asyncio.Event | None" = None):
        """frame_source: optional zero-arg callable returning one 160-byte
        mu-law frame, or None for "nothing fresh" (silence is sent). When
        given, the call is LIVE: it runs until stop_event, a remote hangup or
        config.CALL_MAX_S. When absent, the milestone-1 tone is played for
        tone_seconds and the call hangs itself up.
        payload_sink: optional callable handed each received RTP payload
        (mu-law bytes, header stripped). Never raises into the RTP path.
        stop_event: cooperative hangup, same idiom as talkdown's stop."""
        self.server = server
        self.sip_port = sip_port
        self.frame_source = frame_source
        self.payload_sink = payload_sink
        self.stop_event = stop_event
        self.username = username
        self._password = password
        self.target = re.sub(r"[^\d+]", "", target).lstrip("+")
        self.caller_id = caller_id
        self.call_id = f"{random.getrandbits(64):016x}@telus-cam-bridge"
        self.from_tag = f"{random.getrandbits(32):08x}"
        self.cseq = 0
        self.timeline: list[dict] = []
        self._t0 = time.monotonic()

    def _mark(self, event: str, **detail):
        entry = {"t": round(time.monotonic() - self._t0, 3), "event": event, **detail}
        self.timeline.append(entry)
        log.info("sip call %s: %s %s", self.call_id[:8], event,
                 {k: v for k, v in detail.items() if k != "body"})

    def _request(self, method: str, local_ip: str, local_port: int,
                 rtp_port: int | None = None, auth: str | None = None,
                 to_tag: str | None = None) -> bytes:
        self.cseq += 1
        uri = f"sip:{self.target}@{self.server}"
        to = f"<{uri}>" + (f";tag={to_tag}" if to_tag else "")
        branch = f"z9hG4bK{random.getrandbits(48):012x}"
        lines = [
            f"{method} {uri} SIP/2.0",
            f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport",
            f"Max-Forwards: 70",
            f"From: \"{self.caller_id}\" <sip:{self.username}@{self.server}>;tag={self.from_tag}",
            f"To: {to}",
            f"Call-ID: {self.call_id}",
            f"CSeq: {self.cseq} {method}",
            f"Contact: <sip:{self.username}@{local_ip}:{local_port}>",
            "User-Agent: telus-cam-bridge",
        ]
        if auth:
            lines.append(auth)
        body = ""
        if rtp_port is not None:
            body = (
                "v=0\r\n"
                f"o=- {int(time.time())} 1 IN IP4 {local_ip}\r\n"
                "s=doorbell\r\n"
                f"c=IN IP4 {local_ip}\r\n"
                "t=0 0\r\n"
                f"m=audio {rtp_port} RTP/AVP 0\r\n"
                "a=rtpmap:0 PCMU/8000\r\n"
                "a=ptime:20\r\n"
                "a=sendrecv\r\n"
            )
            lines.append("Content-Type: application/sdp")
        lines.append(f"Content-Length: {len(body)}")
        return ("\r\n".join(lines) + "\r\n\r\n" + body).encode()

    def _ack(self, local_ip: str, local_port: int, to_header: str) -> bytes:
        # ACK for a final response: same CSeq NUMBER as the INVITE it
        # acknowledges, method ACK, To copied verbatim (it carries their tag).
        uri = f"sip:{self.target}@{self.server}"
        branch = f"z9hG4bK{random.getrandbits(48):012x}"
        msg = (
            f"ACK {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: \"{self.caller_id}\" <sip:{self.username}@{self.server}>;tag={self.from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} ACK\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        return msg.encode()

    def _auth_header(self, challenge_header: str, challenge_name: str,
                     method: str) -> str:
        params = parse_digest_challenge(challenge_header)
        realm = params.get("realm", "")
        nonce = params.get("nonce", "")
        qop = "auth" if "auth" in (params.get("qop") or "") else None
        uri = f"sip:{self.target}@{self.server}"
        cnonce = f"{random.getrandbits(32):08x}"
        resp = digest_response(self.username, realm, self._password, method,
                               uri, nonce, qop=qop, cnonce=cnonce)
        name = ("Proxy-Authorization" if challenge_name == "proxy-authenticate"
                else "Authorization")
        h = (f'{name}: Digest username="{self.username}", realm="{realm}", '
             f'nonce="{nonce}", uri="{uri}", response="{resp}", algorithm=MD5')
        if qop:
            h += f', qop=auth, nc=00000001, cnonce="{cnonce}"'
        return h

    async def run(self, tone_seconds: float, timeout: float) -> dict:
        loop = asyncio.get_running_loop()
        local_ip = _local_ip_toward(self.server, self.sip_port)
        sip_t, sip_p = await loop.create_datagram_endpoint(
            _Sip, local_addr=(local_ip, 0),
            remote_addr=(self.server, self.sip_port))
        rtp_t, rtp_p = await loop.create_datagram_endpoint(
            lambda: _Rtp(self.payload_sink), local_addr=(local_ip, 0))
        local_port = sip_t.get_extra_info("sockname")[1]
        rtp_port = rtp_t.get_extra_info("sockname")[1]
        try:
            return await asyncio.wait_for(
                self._run(sip_t, sip_p, rtp_t, rtp_p, local_ip, local_port,
                          rtp_port, tone_seconds),
                timeout)
        except asyncio.TimeoutError:
            self._mark("timeout")
            return {"result": "timeout", "timeline": self.timeline}
        finally:
            sip_t.close()
            rtp_t.close()

    async def _recv(self, sip_p, want_final: bool = True):
        while True:
            data = await sip_p.queue.get()
            try:
                status, reason, headers, body = _parse_response(data)
            except ValueError:
                continue                      # a request (e.g. retransmit); not ours
            self._mark("response", status=status, reason=reason)
            if status >= 200 or not want_final:
                return status, reason, headers, body

    async def _run(self, sip_t, sip_p, rtp_t, rtp_p, local_ip, local_port,
                   rtp_port, tone_seconds) -> dict:
        # INVITE, once unauthenticated (the challenge is expected), then again
        # with the digest. A resend loop covers a lost first packet.
        invite = self._request("INVITE", local_ip, local_port, rtp_port=rtp_port)
        self._mark("invite_sent", target=self.target, server=self.server)
        sip_t.sendto(invite)
        status, reason, headers, body = await self._recv(sip_p)

        if status in (401, 407):
            name = "www-authenticate" if status == 401 else "proxy-authenticate"
            self._mark("challenged", scheme=status)
            sip_t.sendto(self._ack(local_ip, local_port, headers.get("to", "")))
            auth = self._auth_header(headers.get(name, ""), name, "INVITE")
            invite = self._request("INVITE", local_ip, local_port,
                                   rtp_port=rtp_port, auth=auth)
            sip_t.sendto(invite)
            self._mark("invite_auth_sent")
            status, reason, headers, body = await self._recv(sip_p)

        if status != 200:
            self._mark("failed", status=status, reason=reason)
            sip_t.sendto(self._ack(local_ip, local_port, headers.get("to", "")))
            return {"result": "rejected", "status": status, "reason": reason,
                    "timeline": self.timeline}

        to_header = headers.get("to", "")
        remote_addr, remote_port = parse_sdp_answer(body)
        self._mark("answered", rtp=f"{remote_addr}:{remote_port}")
        sip_t.sendto(self._ack(local_ip, local_port, to_header))

        # Media. Sent from the same socket the SDP offered (symmetric RTP), on
        # a wall-clock schedule so the stream is exactly real-time. Tone mode
        # (milestone 1, no frame_source) runs a fixed frame budget; live mode
        # runs until the operator hangs up (stop_event), the far end does
        # (BYE, handled below), or config.CALL_MAX_S.
        ssrc = random.getrandbits(32)
        seq = random.getrandbits(15)
        ts = random.getrandbits(31)
        live = self.frame_source is not None
        frames_budget = None if live else int(tone_seconds * 50)
        max_frames = int(c.CALL_MAX_S * 50)
        started = time.monotonic()
        ended_by = None
        i = 0
        while True:
            if not live and i >= frames_budget:
                ended_by = "tone_done"
                break
            if live and self.stop_event is not None and self.stop_event.is_set():
                ended_by = "stopped"
                break
            if i >= max_frames:
                ended_by = "max_duration"
                break
            # A remote hangup arrives as a BYE REQUEST on the SIP socket;
            # _recv only returns responses, so requests are watched here,
            # between frames, where the 20 ms cadence bounds the answer delay.
            bye = self._pending_bye(sip_p)
            if bye is not None:
                sip_t.sendto(bye)
                ended_by = "remote_bye"
                break
            payload = None
            if live:
                try:
                    payload = self.frame_source()
                except Exception:
                    log.exception("uplink frame source failed; sending silence")
            frame = payload if payload else (SILENCE_FRAME if live else TONE_FRAME)
            hdr = bytes([0x80, 0x00]) + seq.to_bytes(2, "big") + \
                ts.to_bytes(4, "big") + ssrc.to_bytes(4, "big")
            rtp_t.sendto(hdr + frame, (remote_addr, remote_port))
            seq = (seq + 1) & 0xFFFF
            ts = (ts + _FRAME_SAMPLES) & 0xFFFFFFFF
            i += 1
            await asyncio.sleep(max(0.0, started + i * 0.02 - time.monotonic()))
        self._mark("media_done", reason=ended_by, frames_sent=i,
                   rtp_received=rtp_p.received)

        if ended_by != "remote_bye":
            sip_t.sendto(self._bye(local_ip, local_port, to_header))
            self._mark("bye_sent")
            try:
                await asyncio.wait_for(self._recv(sip_p), 3.0)
            except asyncio.TimeoutError:
                pass                          # best-effort; the call is over
        return {"result": "completed", "reason": ended_by, "frames_sent": i,
                "rtp_received": rtp_p.received,
                "seconds": round(time.monotonic() - started, 1),
                "timeline": self.timeline}

    def _pending_bye(self, sip_p) -> bytes | None:
        """A 200 OK for a BYE the far end sent, or None. Non-blocking: drains
        whatever sits in the SIP queue, answering retransmitted 200s' ACKs by
        ignoring them (UDP duplicates are normal, not errors)."""
        while not sip_p.queue.empty():
            data = sip_p.queue.get_nowait()
            text = data.decode(errors="replace")
            if not text.startswith("BYE "):
                continue
            self._mark("remote_bye")
            head = text.split("\r\n\r\n", 1)[0].split("\r\n")
            keep = [l for l in head[1:] if l.split(":", 1)[0].strip().lower()
                    in ("via", "from", "to", "call-id", "cseq")]
            return ("SIP/2.0 200 OK\r\n" + "\r\n".join(keep) +
                    "\r\nContent-Length: 0\r\n\r\n").encode()
        return None

    def _bye(self, local_ip, local_port, to_header) -> bytes:
        self.cseq += 1
        uri = f"sip:{self.target}@{self.server}"
        branch = f"z9hG4bK{random.getrandbits(48):012x}"
        msg = (
            f"BYE {uri} SIP/2.0\r\n"
            f"Via: SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport\r\n"
            "Max-Forwards: 70\r\n"
            f"From: \"{self.caller_id}\" <sip:{self.username}@{self.server}>;tag={self.from_tag}\r\n"
            f"To: {to_header}\r\n"
            f"Call-ID: {self.call_id}\r\n"
            f"CSeq: {self.cseq} BYE\r\n"
            "Content-Length: 0\r\n\r\n"
        )
        return msg.encode()


async def place_test_call(*, server: str | None = None, username: str | None = None,
                          password: str | None = None, target: str | None = None,
                          sip_port: int | None = None,
                          tone_seconds: float | None = None,
                          timeout: float | None = None) -> dict:
    """Milestone 1's whole surface: one call, one tone, one report."""
    call = SipCall(
        server=server or c.VOIPMS_SIP_SERVER,
        username=username or c.VOIPMS_SIP_ACCOUNT,
        password=password or c.VOIPMS_SIP_PASSWORD,
        target=target or c.DOORBELL_CALL_TARGET,
        caller_id=c.VOIPMS_DID or "doorbell",
        sip_port=sip_port if sip_port is not None else c.SIP_PORT,
    )
    return await call.run(
        tone_seconds if tone_seconds is not None else c.CALL_TONE_SECONDS,
        timeout if timeout is not None else c.CALL_TIMEOUT_S,
    )
