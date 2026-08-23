"""Full-duplex "talkdown" for TELUS/alarm.com ADC doorbells (e.g. ADC-VDB750).

Reverse-engineered end-to-end (see the research memory): the doorbell's own
HTTP endpoint accepts upstream audio via chunked HTTP PUT, authenticated with
HTTP Digest (user "vieweruser", realm "Alarm.com", MD5, qop=auth). The audio
body is raw G.711 mu-law (PCMU), 8 kHz, mono. On-LAN the target is the doorbell
directly (the `duae`/`luae` URL); off-LAN it is the public IP (`puae`) or the
cloud relay (`uae`, which carries a ?camera_id= token). This module is transport
only: it is handed a ready PUT URL + credentials + an audio source and streams
it. Activation (StartFullDuplex) and URL/credential discovery live in mws_client
and are orchestrated by talk.py.

Nothing here logs secrets (Digest password, tokens) -- only non-sensitive shape.
"""
import asyncio
import hashlib
import logging
import os
import re
import time
import uuid

import aiohttp

from . import config as c

log = logging.getLogger(__name__)

# G.711 mu-law @ 8 kHz mono: 8000 samples/s * 1 byte = 8000 B/s = 160 B / 20 ms.
MULAW_RATE = 8000
CHUNK_MS = 40
CHUNK_BYTES = MULAW_RATE * CHUNK_MS // 1000  # 320 bytes / 40 ms
DIGEST_USER = "vieweruser"


class TalkdownError(RuntimeError):
    pass


def _h(*parts: str) -> str:
    return hashlib.md5(":".join(parts).encode("utf-8")).hexdigest()


def _parse_challenge(header: str) -> dict:
    """Parse a WWW-Authenticate: Digest ... header into a dict of params."""
    if not header:
        return {}
    scheme, _, rest = header.partition(" ")
    if scheme.lower() != "digest":
        return {}
    out = {}
    for m in re.finditer(r'(\w+)=(?:"([^"]*)"|([^,]+))', rest):
        out[m.group(1)] = m.group(2) if m.group(2) is not None else m.group(3).strip()
    return out


class _Digest:
    """Minimal RFC 2617 Digest (MD5, qop=auth) client state for one nonce."""

    def __init__(self, username: str, password: str, method: str, uri: str, ch: dict):
        self.username = username
        self.password = password
        self.method = method
        self.uri = uri
        self.realm = ch.get("realm", "")
        self.nonce = ch.get("nonce", "")
        self.qop = ch.get("qop", "auth")
        self.opaque = ch.get("opaque")
        self.algorithm = ch.get("algorithm", "MD5")
        self.cnonce = uuid.uuid4().hex[:16]
        self.nc = 0

    def header(self) -> str:
        self.nc += 1
        nc = f"{self.nc:08x}"
        ha1 = _h(self.username, self.realm, self.password)
        ha2 = _h(self.method, self.uri)
        resp = _h(ha1, self.nonce, nc, self.cnonce, self.qop, ha2)
        parts = [
            f'username="{self.username}"', f'realm="{self.realm}"',
            f'nonce="{self.nonce}"', f'uri="{self.uri}"',
            f'algorithm={self.algorithm}', f'response="{resp}"',
            f'qop={self.qop}', f'nc={nc}', f'cnonce="{self.cnonce}"',
        ]
        if self.opaque:
            parts.append(f'opaque="{self.opaque}"')
        return "Digest " + ", ".join(parts)


async def _encode_mulaw(audio_source: str) -> bytes:
    """ffmpeg-decode any audio (file path or URL) to raw mu-law/8k/mono bytes."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", audio_source,
        "-vn", "-ac", "1", "-ar", str(MULAW_RATE), "-f", "mulaw", "pipe:1",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=c.TALK_MEDIA_FETCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise TalkdownError("ffmpeg mu-law encode timed out")
    if proc.returncode != 0:
        raise TalkdownError(f"ffmpeg failed: {(err or b'')[:200]!r}")
    if not out:
        raise TalkdownError("ffmpeg produced no audio")
    return out


async def stream_talkdown(put_url: str, password: str, audio_source: str, *,
                          username: str = DIGEST_USER, pace: bool = True,
                          timeout_s: float | None = None) -> dict:
    """Play `audio_source` through the doorbell speaker via chunked Digest PUT.

    put_url  -- the PutAudioStream endpoint (duae/luae for LAN, puae/uae for WAN)
    password -- the per-session stream password (Digest password for "vieweruser")
    Returns {"chunks": n, "bytes": total, "path": "full_duplex"}.
    Guarantees the ffmpeg process and HTTP session are torn down on every path.
    """
    timeout_s = timeout_s or c.TALK_SESSION_TIMEOUT_S
    deadline = time.monotonic() + timeout_s
    audio = await _encode_mulaw(audio_source)
    total = len(audio)
    # Doorbell serves a self-signed cert -> no verification. Keep-alive one host.
    conn = aiohttp.TCPConnector(ssl=False, limit=1, force_close=False)
    sent = 0
    chunks = 0
    dig: _Digest | None = None
    ua = {"User-Agent": "GStreamer souphttpclientsink libsoup/3.6.0"}
    async with aiohttp.ClientSession(connector=conn) as s:
        # path+query for the Digest uri= field
        from urllib.parse import urlsplit
        sp = urlsplit(put_url)
        digest_uri = sp.path + (("?" + sp.query) if sp.query else "")

        async def put_chunk(body: bytes) -> None:
            nonlocal dig, chunks, sent
            for attempt in (1, 2):  # 2nd attempt after a (re)challenge
                headers = dict(ua)
                headers["Content-Type"] = "application/octet-stream"
                if dig is not None:
                    headers["Authorization"] = dig.header()
                async with s.put(put_url, data=body, headers=headers) as r:
                    if r.status == 401:
                        ch = _parse_challenge(r.headers.get("WWW-Authenticate", ""))
                        if not ch.get("nonce"):
                            raise TalkdownError("401 without a Digest challenge")
                        dig = _Digest(username, password, "PUT", digest_uri, ch)
                        await r.read()
                        continue  # retry this chunk with auth
                    if r.status // 100 != 2:
                        txt = (await r.read())[:120]
                        raise TalkdownError(f"PUT -> HTTP {r.status}: {txt!r}")
                    await r.read()
                    chunks += 1
                    sent += len(body)
                    return
            raise TalkdownError("PUT still unauthorized after challenge")

        for off in range(0, total, CHUNK_BYTES):
            if time.monotonic() > deadline:
                raise TalkdownError("talkdown exceeded session timeout")
            await put_chunk(audio[off:off + CHUNK_BYTES])
            if pace:
                await asyncio.sleep(CHUNK_MS / 1000.0)
    log.info("talkdown: streamed %d chunks / %d bytes mu-law to %s",
             chunks, sent, urlsplit(put_url).netloc)
    return {"path": "full_duplex", "chunks": chunks, "bytes": sent}
