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


class _DeadlineExceeded(TalkdownError):
    """Safety deadline hit. Never retried/renegotiated -- it means the whole talk
    has run too long, not that this session went bad."""


async def stream_talkdown(put_url: str, password: str, audio_source: str, *,
                          username: str = DIGEST_USER, codec: str = DEFAULT_CODEC,
                          audio_filter: str | None = None,
                          pace: bool = True, timeout_s: float | None = None,
                          renegotiate=None, session_deadline: float | None = None) -> dict:
    """Play `audio_source` through the doorbell speaker via chunked Digest PUT.

    put_url  -- the PutAudioStream endpoint (duae/luae for LAN, puae/uae for WAN)
    password -- the per-session stream password (Digest password for "vieweruser")
    codec    -- "l16" (default, 16-bit linear) or "mulaw" (G.711 fallback)
    renegotiate -- optional async callable returning a fresh session as
                   {"put_url", "password", "username"}. When the stream session
                   dies mid-clip (its per-session URL/port/password expire, or the
                   connection drops), the transport calls this and RESUMES FROM THE
                   EXACT BYTE OFFSET it reached, so no audio is lost or repeated.
                   Bounded by config.TALK_MAX_RENEGOTIATIONS.
    session_deadline -- optional time.monotonic() value at which the current
                   session is known to expire. When given, the transport swaps
                   PROACTIVELY (config.TALK_RENEGOTIATE_MARGIN_S ahead) instead of
                   waiting for the first failed PUT.

    Returns {"path": "full_duplex", "codec": ..., "chunks": n, "bytes": total,
    "seconds": s, "renegotiations": n}. Guarantees ffmpeg + the HTTP session are
    torn down on every path. Raises TalkdownError on a transport/codec failure (so
    the caller can fall back to another codec)."""
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
    # Per-socket timeouts: the deadline below is only checked BETWEEN chunks, so
    # without these a single wedged PUT would pin the per-camera talk lock for
    # aiohttp's 300s default. A 40ms audio chunk never needs seconds.
    sock_timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=5)
    headers_base = {
        "User-Agent": "GStreamer souphttpclientsink libsoup/3.6.0",
        "Content-Type": spec["content_type"],
    }
    total_chunks = (total + chunk_bytes - 1) // chunk_bytes
    sent = 0
    chunks = 0
    renegotiations = 0
    off = 0          # byte offset into the encoded audio -- survives renegotiation
    idx = 0          # chunks sent overall, drives the pacing schedule
    start = time.monotonic()
    next_log = start + PROGRESS_LOG_S

    def _session():
        """A fresh ClientSession for the CURRENT credentials.

        HTTP Digest is handled by aiohttp's DigestAuthMiddleware (3.12+): it
        answers the doorbell's 401 challenge, then -- preemptive by default --
        signs every subsequent request from the cached challenge with an
        incrementing nc, which is exactly what this chunk loop needs. A
        renegotiated session gets a NEW password, hence a new middleware and a new
        connector (the old socket belongs to a dead session).
        """
        conn = aiohttp.TCPConnector(ssl=False, limit=1, force_close=False)
        digest = aiohttp.DigestAuthMiddleware(login=username, password=password)
        return aiohttp.ClientSession(connector=conn, timeout=sock_timeout,
                                     middlewares=(digest,))

    while off < total:
        # One iteration per session generation: stream until the audio runs out,
        # or until this session dies / is about to expire.
        async with _session() as s:

            async def put_chunk(body: bytes) -> None:
                nonlocal chunks, sent
                async with s.put(put_url, data=body, headers=dict(headers_base)) as r:
                    if r.status == 401:
                        raise TalkdownError(
                            "PUT rejected with 401 after Digest authentication -- "
                            "the per-session stream password is no longer valid")
                    if r.status // 100 != 2:
                        txt = (await r.read())[:120]
                        raise TalkdownError(f"PUT -> HTTP {r.status}: {txt!r}")
                    await r.read()
                    chunks += 1
                    sent += len(body)

            try:
                while off < total:
                    if time.monotonic() > deadline:
                        raise _DeadlineExceeded(
                            f"talkdown exceeded safety deadline after {chunks} chunks")
                    # Proactive swap: get ahead of a known expiry rather than
                    # discovering it as a failed PUT mid-sentence.
                    if (session_deadline is not None and renegotiate is not None
                            and renegotiations < c.TALK_MAX_RENEGOTIATIONS
                            and time.monotonic() >= session_deadline - c.TALK_RENEGOTIATE_MARGIN_S):
                        log.info("talkdown: session expiry approaching, renegotiating early "
                                 "at %d/%d chunks", chunks, total_chunks)
                        break
                    await put_chunk(audio[off:off + chunk_bytes])
                    off += chunk_bytes
                    idx += 1
                    if pace:
                        # Schedule-based pacing: sleep until THIS chunk's real-time
                        # slot (start + idx*CHUNK_MS), absorbing per-PUT latency so
                        # wall-clock tracks audio time (a fixed sleep-after-each-PUT
                        # drifted ~10%). The PREBUFFER_MS lead keeps a constant
                        # jitter buffer at the doorbell so playback stays continuous
                        # instead of underrunning into gaps.
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
            except _DeadlineExceeded:
                raise
            except (TalkdownError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                # The session went bad mid-clip. Without a way to get a new one,
                # this is fatal exactly as before.
                if renegotiate is None or renegotiations >= c.TALK_MAX_RENEGOTIATIONS:
                    raise
                log.warning("talkdown: session failed at %d/%d chunks (%s); renegotiating",
                            chunks, total_chunks, err)

        if off >= total:
            break

        # ---- renegotiate: a fresh session, then resume at the SAME byte offset ----
        renegotiations += 1
        swap_started = time.monotonic()
        try:
            fresh = await renegotiate()
        except Exception as err:  # noqa: BLE001
            raise TalkdownError(f"talkdown renegotiation failed: {err}") from err
        if not fresh or not fresh.get("put_url") or not fresh.get("password"):
            raise TalkdownError("talkdown renegotiation returned no usable session")
        put_url = fresh["put_url"]
        password = fresh["password"]
        username = fresh.get("username") or username
        swap_s = time.monotonic() - swap_started
        # The swap is dead air, not audio time: push the safety deadline out by
        # what it cost, and re-baseline the pacing schedule so the next chunk goes
        # immediately and then paces normally (rather than bursting to "catch up",
        # which would overrun the doorbell's small buffer).
        deadline += swap_s
        start = time.monotonic() - idx * (CHUNK_MS / 1000.0) + PREBUFFER_MS / 1000.0
        log.info("talkdown: renegotiated in %.1fs (#%d), resuming at %d/%d chunks to %s",
                 swap_s, renegotiations, chunks, total_chunks, urlsplit(put_url).netloc)

    log.info("talkdown: streamed %d chunks / %d bytes %s (audio %.1fs, %d renegotiation(s)) to %s",
             chunks, sent, spec["label"], expected_s, renegotiations,
             urlsplit(put_url).netloc)
    return {"path": "full_duplex", "codec": codec, "chunks": chunks,
            "bytes": sent, "seconds": round(expected_s, 1),
            "renegotiations": renegotiations}
