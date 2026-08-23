"""Full-duplex "talkdown" for TELUS/alarm.com ADC doorbells (e.g. ADC-VDB750).

Reverse-engineered end-to-end (see the research memory): the doorbell's own
HTTP endpoint accepts upstream audio via chunked HTTP PUT, authenticated with
HTTP Digest (user "vieweruser", realm "Alarm.com", MD5, qop=auth; handled by
aiohttp's DigestAuthMiddleware). On-LAN the
target is the doorbell directly (the `duae`/`luae` URL); off-LAN it is the public
IP (`puae`) or the cloud relay (`uae`, which carries a ?camera_id= token).

Codec: on THIS doorbell (ADC-VDB750, fw 02091) the PUT path is **G.711 mu-law
(PCMU), 8 kHz mono**, raw chunked body (no RTP header). L16 (16-bit linear, the
`skyL16pay`/pt97 path) was TESTED over this PUT endpoint in BOTH endiannesses and
came out as noise -- this doorbell decodes the body as mu-law regardless of
Content-Type. This is a per-model/firmware limitation, NOT an alarm.com-wide one:
other alarm.com camera/doorbell models may support L16 (newer ADC cameras carry
L16 via the webrtc/srtp path -- see talk_srtp.py). The ``CODECS`` table +
per-request codec plumbing are kept so a working codec can be added per device
without reworking the transport. Small-speaker audio quality on the VDB750 is
handled by config.TALK_AUDIO_FILTER conditioning applied at encode time.

This module is transport only: it is handed a ready PUT URL + credentials + an
audio source + a codec and streams it. Activation (StartFullDuplex), codec
selection/fallback, and URL/credential discovery live in mws_client / talk.py.

Nothing here logs secrets (Digest password, tokens) -- only non-sensitive shape.
"""
import asyncio
import logging
import time
from urllib.parse import urlsplit

import aiohttp

from . import config as c

log = logging.getLogger(__name__)

# All doorbell talk audio is 8 kHz mono. CHUNK_MS of audio is streamed per PUT.
SAMPLE_RATE = 8000
CHUNK_MS = 40
DIGEST_USER = "vieweruser"
# How often to log progress during a long talk (seconds of wall clock).
PROGRESS_LOG_S = 15
# Stay this far ahead of real time so the doorbell always has a jitter buffer.
# Delivering chunks exactly at playback time underruns into choppy gaps; a small
# constant lead keeps playback continuous.
PREBUFFER_MS = 500

# Per-codec transport spec. bytes_per_s = SAMPLE_RATE * bytes-per-sample; ``fmt``
# is the ffmpeg raw output format (L16/RTP is big-endian -> s16be).
CODECS = {
    "mulaw": {
        "ffmpeg_fmt": "mulaw",
        "bytes_per_s": SAMPLE_RATE * 1,
        "content_type": "application/octet-stream",
        "label": "mu-law",
    },
}
DEFAULT_CODEC = "mulaw"


class TalkdownError(RuntimeError):
    pass


async def _encode(audio_source: str, ffmpeg_fmt: str, audio_filter: str) -> bytes:
    """ffmpeg-decode any audio (file path or URL) to raw 8 kHz mono bytes in the
    requested raw format (``mulaw`` or ``s16be`` for L16)."""
    # Conditioning filter (config.TALK_AUDIO_FILTER) runs before the final
    # downmix/resample so a small-speaker-friendly, non-clipping signal is
    # what gets encoded. Empty string -> no filter (pass-through).
    af = (audio_filter or "").strip()
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", audio_source,
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE)]
    if af:
        args += ["-af", af]
    args += ["-f", ffmpeg_fmt, "pipe:1"]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=c.TALK_MEDIA_FETCH_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise TalkdownError("ffmpeg encode timed out")
    if proc.returncode != 0:
        raise TalkdownError(f"ffmpeg failed: {(err or b'')[:200]!r}")
    if not out:
        raise TalkdownError("ffmpeg produced no audio")
    return out


async def stream_talkdown(put_url: str, password: str, audio_source: str, *,
                          username: str = DIGEST_USER, codec: str = DEFAULT_CODEC,
                          audio_filter: str | None = None,
                          pace: bool = True, timeout_s: float | None = None) -> dict:
    """Play `audio_source` through the doorbell speaker via chunked Digest PUT.

    put_url  -- the PutAudioStream endpoint (duae/luae for LAN, puae/uae for WAN)
    password -- the per-session stream password (Digest password for "vieweruser")
    codec    -- "l16" (default, 16-bit linear) or "mulaw" (G.711 fallback)
    Returns {"path": "full_duplex", "codec": ..., "chunks": n, "bytes": total,
    "seconds": s}. Guarantees ffmpeg + the HTTP session are torn down on every
    path. Raises TalkdownError on a transport/codec failure (so the caller can
    fall back to another codec)."""
    spec = CODECS.get(codec)
    if spec is None:
        raise TalkdownError(f"unknown codec {codec!r}")
    bytes_per_s = spec["bytes_per_s"]
    chunk_bytes = bytes_per_s * CHUNK_MS // 1000

    # None -> the config default filter; "" -> conditioning disabled;
    # any string -> that ffmpeg filter chain (per-request override).
    af = c.TALK_AUDIO_FILTER if audio_filter is None else audio_filter
    audio = await _encode(audio_source, spec["ffmpeg_fmt"], af)
    total = len(audio)
    # Natural end is "all chunks sent"; wall-clock length == audio length. The
    # deadline is only a backstop against a wedged connection, DERIVED FROM THE
    # AUDIO (clip length + generous PUT-overhead room), not a fixed/config cap.
    expected_s = total / bytes_per_s
    deadline = time.monotonic() + (timeout_s or (expected_s + max(30.0, 0.5 * expected_s)))
    # Doorbell serves a self-signed cert -> no verification. Keep-alive one host.
    conn = aiohttp.TCPConnector(ssl=False, limit=1, force_close=False)
    # Per-socket timeouts: the deadline below is only checked BETWEEN chunks,
    # so without these a single wedged PUT would pin the per-camera talk lock
    # for aiohttp's 300s default. A 40ms audio chunk never needs seconds.
    sock_timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=5)
    sent = 0
    chunks = 0
    headers_base = {
        "User-Agent": "GStreamer souphttpclientsink libsoup/3.6.0",
        "Content-Type": spec["content_type"],
    }
    # HTTP Digest is handled by aiohttp's DigestAuthMiddleware (added in 3.12):
    # it answers the doorbell's 401 challenge, then -- with preemptive=True, the
    # default -- signs every subsequent request from the cached challenge with an
    # incrementing nc, which is exactly what this chunk loop needs. This replaced
    # a hand-rolled RFC 2617 implementation; aiohttp 3.14 additionally fixed the
    # middleware to sign the ENCODED request-target, which matters for the cloud
    # relay endpoint whose URL carries a ?camera_id= token.
    digest = aiohttp.DigestAuthMiddleware(login=username, password=password)
    async with aiohttp.ClientSession(connector=conn, timeout=sock_timeout,
                                     middlewares=(digest,)) as s:

        async def put_chunk(body: bytes) -> None:
            nonlocal chunks, sent
            async with s.put(put_url, data=body, headers=dict(headers_base)) as r:
                if r.status == 401:
                    raise TalkdownError(
                        "PUT rejected with 401 after Digest authentication -- "
                        "check the per-session stream password")
                if r.status // 100 != 2:
                    txt = (await r.read())[:120]
                    raise TalkdownError(f"PUT -> HTTP {r.status}: {txt!r}")
                await r.read()
                chunks += 1
                sent += len(body)

        total_chunks = (total + chunk_bytes - 1) // chunk_bytes
        start = time.monotonic()
        next_log = start + PROGRESS_LOG_S
        idx = 0
        for off in range(0, total, chunk_bytes):
            if time.monotonic() > deadline:
                raise TalkdownError(
                    f"talkdown exceeded safety deadline after {chunks} chunks")
            await put_chunk(audio[off:off + chunk_bytes])
            idx += 1
            if pace:
                # Schedule-based pacing: sleep until THIS chunk's real-time slot
                # (start + idx*CHUNK_MS), absorbing per-PUT latency so wall-clock
                # tracks audio time (a fixed sleep-after-each-PUT drifted ~10%).
                # The PREBUFFER_MS lead keeps a constant jitter buffer at the
                # doorbell (first ~PREBUFFER worth of chunks burst out) so
                # playback stays continuous instead of underrunning into gaps.
                slack = (start + idx * (CHUNK_MS / 1000.0)
                         - PREBUFFER_MS / 1000.0) - time.monotonic()
                if slack > 0:
                    await asyncio.sleep(slack)
            now = time.monotonic()
            if now >= next_log:
                log.info("talkdown: %s %d/%d chunks, %.0f/%.0fs elapsed to %s",
                         spec["label"], chunks, total_chunks, now - start,
                         expected_s, urlsplit(put_url).netloc)
                next_log = now + PROGRESS_LOG_S
    log.info("talkdown: streamed %d chunks / %d bytes %s in %.1fs (audio %.1fs) to %s",
             chunks, sent, spec["label"], time.monotonic() - start, expected_s,
             urlsplit(put_url).netloc)
    return {"path": "full_duplex", "codec": codec, "chunks": chunks,
            "bytes": sent, "seconds": round(expected_s, 1)}
