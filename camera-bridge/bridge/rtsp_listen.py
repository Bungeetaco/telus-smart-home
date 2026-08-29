"""Direct-LAN listen: the doorbell's own audio over RTSP-tunnelled-in-HTTPS.

WHY THIS EXISTS (backlog E96/E99). The bridge's normal video+listen path is
alarm.com's cloud Janus/WebRTC. For a camera on the SAME LAN as the bridge that
path still routes media up to the cloud and back through a TURN relay, and for a
high-bitrate device that relay churns (webrtcup, then no RTP, then a 20 s
timeout and reconnect, on a loop). The official app never sees this because it
consumes the device's OWN stream directly on the LAN. This module does the same:
it pulls audio straight from the device, bypassing the cloud entirely, so a live
call is not at the mercy of the relay's churn.

GENERIC BY CONSTRUCTION (see the generic-capability-driven memory / AGENTS):
nothing here is specific to one device. The endpoint URL, credentials and the
audio codec are all read at run time -- the URL and Digest password come from
the account's camera list (mws_client.get_doorbell_endpoints), and the codec is
read from the stream's own SDP, never assumed. A device that serves a different
codec, port or path just works; a device that offers no audio track fails
cleanly and the caller falls back or degrades.

THE TRANSPORT, recovered from the device and confirmed live 2026-08-29:

  * The device runs a GStreamer RTSP server behind lighttpd, reachable only as
    RTSP-tunnelled-over-HTTP (Content-Type application/x-rtsp-tunnelled) on its
    HTTPS port -- it answers native RTSP with an HTTP 400. Tunnelling uses the
    Apple scheme: a long-lived GET carries server->client data (RTSP replies +
    interleaved RTP), and a paired POST (same x-sessioncookie) carries
    base64-encoded RTSP requests client->server.
  * TWO Digest layers, both using the camera list's per-session vieweruser/
    password: HTTP Digest on the GET/POST themselves, and RTSP Digest inside the
    tunnel (realm "Alarm.com"; the first RTSP request draws a 401 with a fresh
    nonce, answered on the retry).
  * Media is interleaved over the GET channel (no UDP through the tunnel):
    `$`, one channel byte, a 16-bit length, then an RTP packet. RTSP text
    replies and interleaved frames share the channel and are demuxed by the
    leading byte.

The device's TLS is self-signed, so the connection is unverified (same posture
as the talkdown PUT to the same host; see talk_fullduplex's cert pinning note).
Secrets (the stream password) are used for Digest only and never logged.
"""

import asyncio
import base64
import logging
import re
import ssl

from . import config as c
from .sip_call import digest_response, parse_digest_challenge

log = logging.getLogger(__name__)

_INTERLEAVE_MAGIC = 0x24  # '$' -- start of an interleaved binary frame

# G.711 decode tables, built once. Upstream mu-law lives in sip_call (encode);
# the listen path needs DECODE, and a-law too, so both are here rather than
# reaching across modules for half of a pair.
def _build_ulaw_decode() -> list:
    out = []
    for b in range(256):
        u = ~b & 0xFF
        sign, exp, mant = u & 0x80, (u >> 4) & 0x07, u & 0x0F
        sample = ((mant << 3) + 0x84) << exp
        sample -= 0x84
        out.append(-sample if sign else sample)
    return out


def _build_alaw_decode() -> list:
    out = []
    for b in range(256):
        a = b ^ 0x55
        mant = a & 0x0F
        exp = (a >> 4) & 0x07
        if exp:
            sample = ((mant << 4) + 0x108) << (exp - 1)
        else:
            sample = (mant << 4) + 0x08
        out.append(-sample if (a & 0x80) else sample)
    return out


_ULAW_DECODE = _build_ulaw_decode()
_ALAW_DECODE = _build_alaw_decode()


def _pcm_bytes(samples) -> bytes:
    out = bytearray(len(samples) * 2)
    for i, s in enumerate(samples):
        s = -32768 if s < -32768 else (32767 if s > 32767 else s)
        out[2 * i] = s & 0xFF
        out[2 * i + 1] = (s >> 8) & 0xFF
    return bytes(out)


# Codecs decodable in-process, keyed by SDP encoding name (upper-cased). Value is
# (decode_fn(payload)->pcm_s16le_bytes, sample_rate). Anything else is reported
# so the caller can decide (PyAV could be added, but the devices seen serve
# G.711); read from the SDP, never assumed.
def _decode_pcmu(payload: bytes) -> bytes:
    t = _ULAW_DECODE
    return _pcm_bytes([t[b] for b in payload])


def _decode_pcma(payload: bytes) -> bytes:
    t = _ALAW_DECODE
    return _pcm_bytes([t[b] for b in payload])


_INPROC_CODECS = {"PCMU": _decode_pcmu, "PCMA": _decode_pcma}
# Static payload types (RFC 3551), used when a stream omits the rtpmap.
_STATIC_PT = {0: ("PCMU", 8000), 8: ("PCMA", 8000)}


class RtspListenError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Pure parsers (no I/O) -- unit-tested directly.
# ---------------------------------------------------------------------------

def parse_sdp_audio(sdp: str) -> dict:
    """The audio track's payload type, codec, rate, channels and control URL.

    Reads it from the SDP rather than assuming, so any device's codec works.
    Raises RtspListenError if the SDP carries no audio track -- a device with
    no listen audio must fail loudly, not silently carry silence.
    """
    lines = [ln.strip() for ln in sdp.splitlines()]
    in_audio = False
    pt = None
    control = None
    codec = None
    rate = None
    channels = 1
    session_control = None
    for ln in lines:
        if ln.startswith("a=control:") and not in_audio:
            session_control = ln.split(":", 1)[1]
        if ln.startswith("m="):
            if ln.startswith("m=audio "):
                in_audio = True
                parts = ln.split()
                # first payload type listed is the default
                pt = int(parts[3]) if len(parts) > 3 and parts[3].isdigit() else None
            else:
                in_audio = False
        elif in_audio and ln.startswith("a=control:"):
            control = ln.split(":", 1)[1]
        elif in_audio and ln.startswith("a=rtpmap:"):
            m = re.match(r"a=rtpmap:(\d+)\s+([^/]+)/(\d+)(?:/(\d+))?", ln)
            if m and (pt is None or int(m.group(1)) == pt):
                pt = int(m.group(1))
                codec = m.group(2).upper()
                rate = int(m.group(3))
                if m.group(4):
                    channels = int(m.group(4))
    if pt is None:
        raise RtspListenError("SDP carries no audio media line")
    if codec is None and pt in _STATIC_PT:
        codec, rate = _STATIC_PT[pt]
    if codec is None:
        raise RtspListenError(f"audio payload type {pt} has no rtpmap and is not static")
    return {"pt": pt, "codec": codec, "rate": rate or 8000, "channels": channels,
            "control": control or session_control or ""}


def iter_interleaved(buf: bytearray):
    """Yield ('rtp', channel, payload) or ('rtsp', text) messages from a tunnel
    read buffer, consuming what it parses. Leaves a partial trailing message in
    `buf`. The GET channel carries both interleaved RTP frames (lead byte '$')
    and RTSP text replies (lead 'R' of "RTSP/1.0"), so they are demuxed here by
    that first byte -- the one place the two share a stream.
    """
    while buf:
        if buf[0] == _INTERLEAVE_MAGIC:
            if len(buf) < 4:
                return
            channel = buf[1]
            length = (buf[2] << 8) | buf[3]
            if len(buf) < 4 + length:
                return
            packet = bytes(buf[4:4 + length])
            del buf[:4 + length]
            yield ("rtp", channel, packet)
        else:
            # An RTSP text reply: header block then optional body.
            head_end = buf.find(b"\r\n\r\n")
            if head_end < 0:
                return
            head = bytes(buf[:head_end]).decode(errors="replace")
            m = re.search(r"[Cc]ontent-[Ll]ength:\s*(\d+)", head)
            body_len = int(m.group(1)) if m else 0
            total = head_end + 4 + body_len
            if len(buf) < total:
                return
            text = bytes(buf[:total]).decode(errors="replace")
            del buf[:total]
            yield ("rtsp", text)


def rtp_payload(packet: bytes) -> bytes | None:
    """The payload of one RTP packet, header (and any CSRC/extension) stripped.
    None if the packet is too short to be RTP."""
    if len(packet) < 12:
        return None
    cc = packet[0] & 0x0F
    has_ext = packet[0] & 0x10
    offset = 12 + 4 * cc
    if has_ext:
        if len(packet) < offset + 4:
            return None
        ext_words = (packet[offset + 2] << 8) | packet[offset + 3]
        offset += 4 + 4 * ext_words
    if len(packet) < offset:
        return None
    return packet[offset:]


# ---------------------------------------------------------------------------
# The tunnel client.
# ---------------------------------------------------------------------------

def content_base_uri(request_uri: str, describe_reply: str) -> str:
    """The aggregate/control URL for SETUP and PLAY: the server's Content-Base
    header when present, else the DESCRIBE request URL. A device may answer
    DESCRIBE on one URL but host its media under a different base -- the VDB750
    describes ".../api/GetStream?streamID=0" yet its media live under Content-Base
    ".../api/GetStream/" (track control "stream=1"). PLAY/SETUP on the request URL
    404s the very media just described; they must target the base."""
    m = re.search(r"(?im)^Content-Base:\s*(\S+)", describe_reply)
    return m.group(1).strip() if m else request_uri


def track_uri(control_uri: str, track_control: str) -> str:
    """A media track's control URL resolved against the aggregate control URL.
    An absolute "rtsp://" control wins; a relative one is joined to the base."""
    if track_control.startswith("rtsp://"):
        return track_control
    return control_uri.rstrip("/") + "/" + track_control.lstrip("/")


class RtspTunnelListen:
    """Pulls one device's audio over the RTSP-in-HTTPS tunnel and hands each
    decoded PCM frame to `on_audio(pcm_s16le, sample_rate)`. See module docstring.

    Lifecycle: start() opens the tunnel and begins the read loop as a task;
    stop() tears it down (RTSP TEARDOWN best-effort, then closes the sockets).
    on_audio must not block or raise -- it is called from the read loop.
    """

    def __init__(self, rtsp_url: str, username: str, password: str,
                 on_audio, *, ssl_ctx: ssl.SSLContext | None = None):
        m = re.match(r"rtsp://(?:[^@/]+@)?([^/:]+):(\d+)(/\S*)", rtsp_url)
        if not m:
            raise RtspListenError(f"unparseable rtsp url: {rtsp_url!r}")
        self.host, self.port, self.path = m.group(1), int(m.group(2)), m.group(3)
        self.rtsp_uri = f"rtsp://{self.host}:{self.port}{self.path}"
        self.username = username
        self._password = password
        self.on_audio = on_audio
        self._ssl = ssl_ctx or self._default_ssl()
        self._cookie = f"{__import__('random').getrandbits(64):016x}"
        self._get = None            # (reader, writer) data channel
        self._post = None           # (reader, writer) command channel
        self._cseq = 0
        self._rtsp_nonce = None
        self._rtsp_realm = "Alarm.com"
        self._rtsp_qop = None
        self._control_uri = None  # aggregate URL (Content-Base) for SETUP/PLAY
        self._audio = None          # parsed SDP audio dict
        self._audio_channel = 0     # interleaved channel the audio SETUP got
        self._task = None
        self.frames = 0
        self.started = False

    @staticmethod
    def _default_ssl() -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # device cert is self-signed
        return ctx

    def _digest_header(self, method: str, uri: str, chal: dict) -> str:
        """A complete RFC 2617 Authorization header. When the challenge carries
        qop, the response is computed WITH nc/cnonce AND the header must echo
        qop/nc/cnonce -- omitting them (a subtle trap) makes the server recompute
        without qop and reject with another 401."""
        realm = chal.get("realm", "")
        nonce = chal.get("nonce", "")
        qop = "auth" if "auth" in (chal.get("qop") or "") else None
        nc = "00000001"
        cnonce = f"{__import__('random').getrandbits(32):08x}"
        resp = digest_response(self.username, realm, self._password, method, uri,
                               nonce, qop=qop, nc=nc, cnonce=cnonce)
        h = (f'Authorization: Digest username="{self.username}", realm="{realm}", '
             f'nonce="{nonce}", uri="{uri}", response="{resp}"')
        if qop:
            h += f', qop=auth, nc={nc}, cnonce="{cnonce}"'
        return h + "\r\n"

    def _http_auth(self, method: str, chal: str | None) -> str:
        if not chal:
            return ""
        return self._digest_header(method, self.path, parse_digest_challenge(chal))

    def _rtsp_auth(self, method: str, uri: str | None = None) -> str:
        if not self._rtsp_nonce:
            return ""
        return self._digest_header(method, uri or self.rtsp_uri,
                                   {"realm": self._rtsp_realm, "nonce": self._rtsp_nonce,
                                    "qop": self._rtsp_qop})

    async def _open_channels(self):
        # GET data channel, with an HTTP-Digest retry.
        r, w = await asyncio.open_connection(self.host, self.port, ssl=self._ssl)
        req = (f"GET {self.path} HTTP/1.0\r\nHost: {self.host}\r\n"
               f"x-sessioncookie: {self._cookie}\r\n"
               f"Accept: application/x-rtsp-tunnelled\r\n"
               f"Cache-Control: no-cache\r\n")
        w.write((req + "\r\n").encode()); await w.drain()
        head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 8)
        # The HTTP-Digest challenge from the 401 is kept: it authenticates the
        # GET retry AND the POST command channel (the 200 that follows the retry
        # carries no challenge, so reading it there would leave the POST
        # unauthenticated and the server would drop the tunnel).
        chal = None
        if b" 401 " in head:
            m = re.search(rb"WWW-Authenticate: (.+)", head)
            chal = m.group(1).decode() if m else None
            w.close()
            r, w = await asyncio.open_connection(self.host, self.port, ssl=self._ssl)
            w.write((req + self._http_auth("GET", chal) + "\r\n").encode())
            await w.drain()
            head = await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), 8)
        if b" 200 " not in head:
            raise RtspListenError(f"GET tunnel refused: {head[:80]!r}")
        self._get = (r, w)
        # POST command channel (same cookie), authenticated with the SAME
        # challenge. Its body is a long stream of base64 RTSP requests.
        pr, pw = await asyncio.open_connection(self.host, self.port, ssl=self._ssl)
        post = (f"POST {self.path} HTTP/1.0\r\nHost: {self.host}\r\n"
                f"x-sessioncookie: {self._cookie}\r\n"
                f"Content-Type: application/x-rtsp-tunnelled\r\n"
                f"Content-Length: 100000000\r\n")
        pw.write((post + (self._http_auth("POST", chal) if chal else "")
                  + "\r\n").encode())
        await pw.drain()
        self._post = (pr, pw)

    async def _send_rtsp(self, method: str, extra: str = "",
                         uri: str | None = None) -> None:
        uri = uri or self.rtsp_uri
        self._cseq += 1
        req = (f"{method} {uri} RTSP/1.0\r\nCSeq: {self._cseq}\r\n"
               f"{self._rtsp_auth(method, uri)}{extra}\r\n")
        _, pw = self._post
        pw.write(base64.b64encode(req.encode())); await pw.drain()

    async def _read_message(self, timeout: float):
        """Next demuxed message from the GET channel, growing a buffer."""
        r, _ = self._get
        if not hasattr(self, "_buf"):
            self._buf = bytearray()
        while True:
            for kind in iter_interleaved(self._buf):
                return kind
            chunk = await asyncio.wait_for(r.read(4096), timeout)
            if not chunk:
                return None
            self._buf.extend(chunk)

    async def _rtsp_exchange(self, method: str, extra: str = "",
                             uri: str | None = None) -> str:
        """Send an RTSP request, return its reply text, transparently answering
        one 401 with the fresh nonce (the GStreamer server's stale-nonce flow)."""
        await self._send_rtsp(method, extra, uri)
        while True:
            msg = await self._read_message(10)
            if msg is None:
                raise RtspListenError(f"{method}: tunnel closed awaiting reply")
            if msg[0] != "rtsp":
                continue  # a stray interleaved frame before PLAY; ignore
            text = msg[1]
            if " 401 " in text.split("\r\n", 1)[0]:
                ch = re.search(r"WWW-Authenticate: (.+)", text)
                if not ch or self._rtsp_nonce:
                    raise RtspListenError(f"{method}: 401 with no fresh challenge")
                p = parse_digest_challenge(ch.group(1))
                self._rtsp_nonce = p.get("nonce")
                self._rtsp_realm = p.get("realm", self._rtsp_realm)
                self._rtsp_qop = p.get("qop")
                await self._send_rtsp(method, extra, uri)
                continue
            if not re.match(r"RTSP/1\.0 200", text):
                raise RtspListenError(f"{method} failed: {text.splitlines()[0]}")
            return text

    async def start(self) -> dict:
        await self._open_channels()
        await self._rtsp_exchange("OPTIONS")
        describe = await self._rtsp_exchange("DESCRIBE", "Accept: application/sdp\r\n")
        # SETUP and PLAY target the server's Content-Base, not the DESCRIBE
        # request URL (see content_base_uri): this device describes a "?query"
        # URL but plays only under its "/path/" base.
        self._control_uri = content_base_uri(self.rtsp_uri, describe)
        sdp = describe.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in describe else ""
        self._audio = parse_sdp_audio(sdp)
        log.info("rtsp listen: SDP audio=%s control=%s", self._audio, self._control_uri)
        # SETUP the audio track over interleaved TCP (channels 0-1) -- there is
        # no UDP through the tunnel. Track control resolves against the base.
        track = self._audio["control"]
        setup_uri = track_uri(self._control_uri, track)
        self._cseq += 1
        setup = (f"SETUP {setup_uri} RTSP/1.0\r\nCSeq: {self._cseq}\r\n"
                 f"{self._rtsp_auth('SETUP', setup_uri)}"
                 f"Transport: RTP/AVP/TCP;unicast;interleaved=0-1\r\n\r\n")
        _, pw = self._post
        pw.write(base64.b64encode(setup.encode())); await pw.drain()
        reply = None
        while reply is None:
            msg = await self._read_message(10)
            if msg is None:
                raise RtspListenError("SETUP: tunnel closed")
            if msg[0] == "rtsp":
                reply = msg[1]
        status_line = reply.splitlines()[0] if reply else ""
        sess = re.search(r"Session:\s*([^\r\n;]+)", reply)
        self._session = sess.group(1).strip() if sess else None
        ilm = re.search(r"interleaved=(\d+)", reply)
        self._audio_channel = int(ilm.group(1)) if ilm else 0
        log.info("rtsp listen: SETUP uri=%s -> %s session=%s interleaved=%d",
                 setup_uri, status_line, self._session, self._audio_channel)
        if not re.match(r"RTSP/1\.0 200", status_line) or not self._session:
            raise RtspListenError(
                f"SETUP did not create a session: {status_line!r} "
                f"(track control={track!r})")
        await self._rtsp_exchange(
            "PLAY", (f"Session: {self._session}\r\n" if self._session else ""),
            uri=self._control_uri)
        self.started = True
        self._task = asyncio.ensure_future(self._read_loop())
        log.info("rtsp listen: playing %s audio from %s (interleaved ch %d)",
                 self._audio["codec"], self.host, self._audio_channel)
        return dict(self._audio)

    async def _read_loop(self):
        codec = self._audio["codec"]
        rate = self._audio["rate"]
        decode = _INPROC_CODECS.get(codec)
        if decode is None:
            log.error("rtsp listen: no in-process decoder for %s; audio unavailable",
                      codec)
            return
        try:
            while True:
                msg = await self._read_message(c.RTSP_LISTEN_READ_TIMEOUT_S)
                if msg is None:
                    log.warning("rtsp listen: stream from %s ended", self.host)
                    return
                if msg[0] != "rtp" or msg[1] != self._audio_channel:
                    continue
                payload = rtp_payload(msg[2])
                if not payload:
                    continue
                pcm = decode(payload)
                self.frames += 1
                try:
                    self.on_audio(pcm, rate)
                except Exception:
                    log.exception("rtsp listen: on_audio sink failed")
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("rtsp listen: read loop failed for %s", self.host)

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            if self._post and self.started:
                await self._send_rtsp(
                    "TEARDOWN",
                    f"Session: {getattr(self, '_session', '') or ''}\r\n",
                    uri=self._control_uri or self.rtsp_uri)
        except Exception:
            pass
        for pair in (self._get, self._post):
            if pair:
                try:
                    pair[1].close()
                except Exception:
                    pass
