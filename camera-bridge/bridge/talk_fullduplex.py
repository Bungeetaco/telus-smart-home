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

TWO DESIGNS HERE ARE LOAD-BEARING AND WERE PAID FOR WITH LIVE TESTING -- do not
"simplify" either of them:

  1. **Pacing is schedule-based, not sleep-after-send.** Each chunk waits until
     its own real-time slot (``start + idx*CHUNK_MS``), which ABSORBS the PUT
     round trip. The naive `send; sleep(CHUNK_MS)` form adds the round trip on
     top of every sleep and drifted ~10% (600s of audio took ~660s). Note that
     this schedule needs only the chunk INDEX, never the clip's total length --
     which is what lets the encoder stream (below) without breaking it.
  2. **The conditioning chain and the PREBUFFER_MS jitter-buffer lead were tuned
     BY EAR against the real speaker.** Delivering chunks exactly at playback
     time underruns into audible gaps; the constant lead is what makes playback
     continuous.
"""
import asyncio
import hashlib
import logging
import os
import statistics
import time
from urllib.parse import urlsplit

import aiohttp

from . import config as c

log = logging.getLogger(__name__)

# All doorbell talk audio is 8 kHz mono. CHUNK_MS of audio is streamed per PUT.
SAMPLE_RATE = 8000
# Wire tuning lives in config so it is documented alongside the other knobs
# (see config.TALK_CHUNK_MS / TALK_PREBUFFER_MS); referenced here under the
# short names the transport logic reads.
CHUNK_MS = c.TALK_CHUNK_MS
DIGEST_USER = "vieweruser"
# How often to log progress during a long talk (seconds of wall clock).
PROGRESS_LOG_S = 15
# Stay this far ahead of real time so the doorbell always has a jitter buffer.
# Delivering chunks exactly at playback time underruns into choppy gaps; a small
# constant lead keeps playback continuous.
PREBUFFER_MS = c.TALK_PREBUFFER_MS
# E6: how often the opt-in adaptive lead re-evaluates, in chunks (250 * 40ms =
# 10s of audio), and by how much the worst observed round trip must be covered.
# A single stalled PUT of duration D eats D of the doorbell's buffer, so the
# lead has to exceed the worst round trip -- with margin -- or that stall is
# heard as a gap. Factor and window are implementation detail of a feature that
# is off by default, so they are constants rather than more env knobs.
_ADAPT_WINDOW_CHUNKS = 250
_PREBUFFER_JITTER_FACTOR = 2.0
# Cap on how many per-PUT latency samples are kept for the E6 summary. 50k
# chunks is ~33 minutes of audio; past that the distribution is long settled and
# the samples are just memory.
_MAX_RTT_SAMPLES = 50_000

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


class _DeadlineExceeded(TalkdownError):
    """Safety deadline hit. Never retried/renegotiated -- it means the whole talk
    has run too long, not that this session went bad."""


class _EncodeFailed(TalkdownError):
    """The encoder died or wedged. Never retried/renegotiated: a fresh doorbell
    session cannot produce audio that ffmpeg is no longer making."""


class _CertPinMismatch(TalkdownError):
    """The device presented a certificate other than the pinned one, in enforce
    mode. Never retried: every reconnect would meet the same certificate, so
    retrying would only turn one clear error into a slow confusing one."""


class _AuthStale(TalkdownError):
    """A 401 the Digest middleware could not recover from. Fixable in place by a
    fresh HTTP session (new handshake, `nc` restarting at 1) on the SAME
    credentials -- see config.TALK_MAX_RECONNECTS."""


def _ffmpeg_args(audio_source: str, ffmpeg_fmt: str, audio_filter: str) -> list:
    """ffmpeg argv decoding any audio (file path or URL) to raw 8 kHz mono bytes
    in the requested raw format (``mulaw`` or ``s16be`` for L16)."""
    # Conditioning filter (config.TALK_AUDIO_FILTER) runs before the final
    # downmix/resample so a small-speaker-friendly, non-clipping signal is
    # what gets encoded. Empty string -> no filter (pass-through).
    af = (audio_filter or "").strip()
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", audio_source,
            "-vn", "-ac", "1", "-ar", str(SAMPLE_RATE)]
    if af:
        args += ["-af", af]
    args += ["-f", ffmpeg_fmt, "pipe:1"]
    return args


async def _encode(audio_source: str, ffmpeg_fmt: str, audio_filter: str) -> bytes:
    """Encode the whole clip to bytes in one pass (the buffered source, and the
    reference the tests compare a streamed run against)."""
    proc = await asyncio.create_subprocess_exec(
        *_ffmpeg_args(audio_source, ffmpeg_fmt, audio_filter),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
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


# ---------------------------------------------------------------------------
# Chunk sources (E5)
#
# The send loop pulls fixed-size chunks from one of these and never learns the
# clip's total length from anywhere else. That is the whole trick: schedule-based
# pacing is driven by the chunk INDEX, so it does not need a total, and the two
# things that did -- the safety deadline and the progress log -- are re-derived
# from "audio produced so far" instead. A source that is `complete` has produced
# the whole clip, at which point "produced so far" IS the total and every derived
# value is identical to the pre-streaming behaviour.
# ---------------------------------------------------------------------------
class _ChunkSource:
    complete = True     # has the encoder reached EOF?
    produced = 0        # bytes of encoded audio handed out so far

    async def next_chunk(self, n: int) -> bytes:
        """Up to n bytes; b"" means end of audio."""
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class _BufferedSource(_ChunkSource):
    """The whole clip, encoded up front. What ran before E5, kept as the
    fallback behind config.TALK_STREAMING_ENCODE so a field regression in the
    streaming path is an .env change rather than a code change."""

    def __init__(self, data: bytes):
        self._data = data
        self._off = 0
        self.produced = len(data)
        self.complete = True

    async def next_chunk(self, n: int) -> bytes:
        out = self._data[self._off:self._off + n]
        self._off += len(out)
        return out


class _StreamingSource(_ChunkSource):
    """ffmpeg's stdout, decoded ahead into a BOUNDED queue and drained at
    playback rate.

    Three properties make this safe to put in front of a live audio path:

    * **The encoder is throttled, not raced.** ffmpeg decodes ~100x faster than
      real time, so drained freely it would simply re-buffer the whole clip in
      memory and E5 would buy nothing. The queue's maxsize is
      config.TALK_ENCODE_LOOKAHEAD_S of audio: the reader fills it, blocks on
      `put`, ffmpeg then blocks on the pipe, and both are released as the send
      loop consumes. Memory is bounded by the lookahead, not by the clip.
    * **That lookahead is also a jitter buffer for the ENCODER.** A chunk the
      send loop had to WAIT for would miss its slot and be heard as a gap, so
      the loop must essentially never wait. With tens of seconds already decoded
      and queued, a decode hiccup is invisible. A read that genuinely blocks past
      TALK_ENCODE_READ_TIMEOUT_S means ffmpeg is wedged, and that fails the talk
      rather than letting the doorbell run dry with the session held open.
    * **`produced` counts DECODED audio, not delivered audio.** That is what
      keeps the two values derived from "the whole clip" honest under streaming:
      any clip at or under the lookahead is fully decoded before playback ends,
      so its true length is known and reported exactly as in buffered mode, and
      the safety deadline stops advancing the moment decoding stalls.

    An encoder failure is surfaced when the queue drains rather than the instant
    it happens: audio already decoded is real audio and is played out first. The
    talk still fails afterwards, so nothing is silently swallowed.

    stderr is drained concurrently: it is tiny at -loglevel error, but an
    undrained stderr pipe is a classic deadlock once it fills, and this process
    now outlives the encode instead of ending with communicate().
    """

    def __init__(self, args: list, bytes_per_s: int, chunk_bytes: int):
        self._args = args
        self._chunk_bytes = chunk_bytes
        lookahead_chunks = max(4, (c.TALK_ENCODE_LOOKAHEAD_S * bytes_per_s) // chunk_bytes)
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=lookahead_chunks)
        self._proc = None
        self._reader_task = None
        self._stderr_task = None
        self._stderr = b""
        self._error: BaseException | None = None
        self.complete = False
        self.produced = 0

    async def start(self) -> None:
        self._proc = await asyncio.create_subprocess_exec(
            *self._args, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        self._stderr_task = asyncio.ensure_future(self._drain_stderr())
        self._reader_task = asyncio.ensure_future(self._read_loop())

    async def _drain_stderr(self) -> None:
        try:
            self._stderr = (await self._proc.stderr.read())[-2000:]
        except Exception:  # noqa: BLE001
            pass

    async def _read_loop(self) -> None:
        """Decode-ahead pump. Ends by queueing the EOF sentinel exactly once, on
        every path, so a consumer can never block forever waiting for it."""
        try:
            while True:
                try:
                    # readexactly, NOT read: a plain read returns whatever has
                    # arrived, which would put odd-sized chunks on the wire. The
                    # doorbell has only ever been fed whole CHUNK_MS frames
                    # (sizes are multiples of 80 bytes = 10ms), and this is not
                    # the place to find out whether it minds otherwise.
                    data = await self._proc.stdout.readexactly(self._chunk_bytes)
                except asyncio.IncompleteReadError as e:
                    if e.partial:
                        self.produced += len(e.partial)
                        await self._queue.put(e.partial)
                    break
                self.produced += len(data)
                await self._queue.put(data)
            await self._reap()
        except asyncio.CancelledError:
            raise
        except Exception as err:  # noqa: BLE001
            self._error = err
        finally:
            self.complete = True
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                # Full queue: the consumer is behind, and it re-checks `complete`
                # once it has drained, so the sentinel is not needed to end it.
                pass

    async def _reap(self) -> None:
        """EOF on stdout: reap ffmpeg and turn a non-zero exit into a failure."""
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            self._proc.kill()
        if self._stderr_task is not None:
            try:
                await asyncio.wait_for(self._stderr_task, timeout=2)
            except Exception:  # noqa: BLE001
                pass
        if self._proc.returncode not in (0, None):
            self._error = _EncodeFailed(
                f"ffmpeg exited {self._proc.returncode} after producing "
                f"{self.produced} bytes: {self._stderr[:200]!r}")
        elif not self.produced:
            self._error = _EncodeFailed("ffmpeg produced no audio")

    async def next_chunk(self, n: int) -> bytes:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                if self.complete:
                    # Drained. Now -- and only now -- is an encoder failure fatal.
                    if self._error is not None:
                        raise (self._error if isinstance(self._error, _EncodeFailed)
                               else _EncodeFailed(f"audio encoder failed: {self._error}"))
                    return b""
                try:
                    item = await asyncio.wait_for(self._queue.get(),
                                                  timeout=c.TALK_ENCODE_READ_TIMEOUT_S)
                except asyncio.TimeoutError:
                    raise _EncodeFailed(
                        f"the audio encoder produced nothing for "
                        f"{c.TALK_ENCODE_READ_TIMEOUT_S}s -- treating it as wedged")
            if item is None:          # EOF sentinel; loop re-checks _error
                continue
            return item

    async def aclose(self) -> None:
        proc, self._proc = self._proc, None
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._reader_task = self._stderr_task = None
        if proc is None:
            return
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except Exception:  # noqa: BLE001
            pass


class LiveMulawSource(_ChunkSource):
    """A live call's downlink audio as a talkdown source (backlog E99).

    Fed mu-law bytes by the caller (the SIP leg's received RTP payloads) and
    drained by stream_talkdown at its own 40 ms pace. Three rules keep a LIVE
    feed compatible with a transport built for clips:

    * **Silence fill, never starvation.** next_chunk pads a short read with
      mu-law silence (0xFF) up to the requested size: the transport's pacing
      schedule assumes a full chunk per slot, and the far end going quiet
      (or a lost RTP packet) must read as silence, not as EOF.
    * **Bounded buffer, freshest audio wins.** The buffer holds
      ~MAX_BUFFER_MS of audio; past that the OLDEST bytes are dropped. A live
      conversation cannot pay latency back, so a stall in the PUT path must
      shed audio rather than drift the conversation out of sync.
    * **`produced` advances with the fill.** The transport's safety deadline
      derives from produced audio; a live source that stopped advancing it
      during silence would trip that deadline mid-conversation.

    close() ends the talk: the next next_chunk returns b"" (EOF) once the
    buffer is drained, and the transport finishes its normal teardown.
    """

    MAX_BUFFER_MS = 400

    def __init__(self):
        self._buf = bytearray()
        self._closed = False
        self._max = SAMPLE_RATE * self.MAX_BUFFER_MS // 1000
        self.complete = False
        self.produced = 0
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> None:
        if self._closed or not data:
            return
        self._buf.extend(data)
        if len(self._buf) > self._max:
            drop = len(self._buf) - self._max
            del self._buf[:drop]
            self.dropped_bytes += drop

    def close(self) -> None:
        self._closed = True
        self.complete = True

    async def next_chunk(self, n: int) -> bytes:
        if self._closed and not self._buf:
            return b""
        out = bytes(self._buf[:n])
        del self._buf[:len(out)]
        if len(out) < n and not self._closed:
            out += b"\xff" * (n - len(out))
        self.produced += len(out)
        return out


async def _open_source(audio_source, spec: dict, audio_filter: str,
                       chunk_bytes: int) -> _ChunkSource:
    if isinstance(audio_source, _ChunkSource):
        # A live source (the call bridge's downlink). Already mu-law at the
        # wire rate: no ffmpeg, and the conditioning chain -- tuned for
        # pre-recorded clips -- does not apply.
        return audio_source
    args = _ffmpeg_args(audio_source, spec["ffmpeg_fmt"], audio_filter)
    if not c.TALK_STREAMING_ENCODE:
        return _BufferedSource(await _encode(audio_source, spec["ffmpeg_fmt"], audio_filter))
    src = _StreamingSource(args, spec["bytes_per_s"], chunk_bytes)
    await src.start()
    return src


# ---------------------------------------------------------------------------
# TLS certificate pinning (E12)
# ---------------------------------------------------------------------------
class _CertPin(aiohttp.Fingerprint):
    """SHA-256 pin for the doorbell's own SELF-SIGNED certificate.

    aiohttp's Fingerprint does exactly the right two things when handed to a
    connector as ``ssl=``: it selects the UNVERIFIED SSL context (verified
    against the pinned aiohttp 3.14.3 -- `connector._get_ssl_context` returns
    `_SSL_CONTEXT_UNVERIFIED` for any `ssl` value that is neither True nor an
    SSLContext), so a self-signed certificate still completes the handshake, and
    it then checks the peer certificate's SHA-256. Without a CA in the picture
    that fingerprint is the ONLY identity the device has, so this replaces
    `ssl=False` with a real check rather than a cosmetic one.

    `check()` runs once per new CONNECTION, not per request, so a keep-alive
    chunk stream pays for it once.

    It is SUBCLASSED rather than used directly so a mismatch can be a loud
    warning instead of a hard failure. That is the default, and the reason is
    the failure mode this has to be safe against: the pinned value came from ONE
    device, and a firmware update, factory reset or replacement unit all change
    the certificate legitimately. Defaulting to enforce would turn a routine
    firmware update into a silent doorbell with an obscure TLS error. So the
    default detects and reports, "enforce" is available once the pin is known
    good, and either way the message names the fix.
    """

    _PLACEHOLDER = b"\x00" * 32

    def __init__(self, expected_hex: str, *, enforce: bool):
        # Fingerprint validates the digest length, so an unset pin gets a
        # placeholder it is never compared against (see check()).
        expected_hex = (expected_hex or "").strip().lower()
        raw = None
        if expected_hex:
            try:
                raw = bytes.fromhex(expected_hex)
            except ValueError:
                raw = None
            if raw is not None and len(raw) != 32:
                raw = None
            if raw is None:
                log.error("talkdown: TALK_DEVICE_CERT_SHA256=%r is not a 32-byte hex "
                          "SHA-256; ignoring the pin (talkdown continues unpinned)",
                          expected_hex)
                expected_hex = ""
        super().__init__(raw if raw is not None else self._PLACEHOLDER)
        self._expected_hex = expected_hex
        self._enforce = bool(enforce)
        self.observed: str | None = None
        self.mismatch = False

    def check(self, transport) -> None:
        if not transport.get_extra_info("sslcontext"):
            return
        sslobj = transport.get_extra_info("ssl_object")
        cert = sslobj.getpeercert(binary_form=True) if sslobj is not None else None
        if not cert:
            return
        got = hashlib.sha256(cert).hexdigest()
        self.observed = got
        peer = transport.get_extra_info("peername")
        host = peer[0] if peer else "?"
        if not self._expected_hex:
            # Learn-and-report: no pin configured, so there is nothing to verify
            # against -- print the value to adopt instead of failing.
            log.warning("talkdown: no certificate pin configured; the device at %s "
                        "presented sha256=%s -- set TALK_DEVICE_CERT_SHA256 to this "
                        "value to pin it", host, got)
            return
        if got == self._expected_hex:
            log.debug("talkdown: device certificate at %s matches the pin", host)
            return
        self.mismatch = True
        log.warning(
            "talkdown: the device at %s presented certificate sha256=%s but "
            "TALK_DEVICE_CERT_SHA256 pins %s. If this device was replaced, reset, "
            "or had a firmware update, that is expected -- update "
            "TALK_DEVICE_CERT_SHA256 to the presented value. Otherwise the "
            "connection is not the device it claims to be. (mode=%s)",
            host, got, self._expected_hex,
            "enforce -- refusing" if self._enforce else "warn -- continuing anyway")
        if self._enforce:
            raise aiohttp.ServerFingerprintMismatch(
                self.fingerprint, bytes.fromhex(got), host,
                peer[1] if peer and len(peer) > 1 else 0)


def _ssl_param(pin: "_CertPin | None"):
    """What to hand TCPConnector(ssl=...). Without a pin this is the historical
    `False` -- no verification, which is what a self-signed device cert requires
    when there is nothing to compare it to."""
    return pin if pin is not None else False


def make_cert_pin(endpoint_kind: str | None) -> "_CertPin | None":
    """The pin to use for an endpoint of this kind, or None for no pinning.

    Only endpoints that terminate AT the device are pinned. The "cloud" endpoint
    is the vendor relay -- a different host with its own CA-signed certificate --
    so pinning the doorbell's fingerprint there would break WAN talkdown outright.
    """
    if c.TALK_TLS_PIN_MODE not in ("warn", "enforce"):
        return None
    if endpoint_kind is not None and endpoint_kind not in c.TALK_TLS_PIN_ENDPOINT_KINDS:
        return None
    return _CertPin(c.TALK_DEVICE_CERT_SHA256, enforce=c.TALK_TLS_PIN_MODE == "enforce")


async def condition_audio(data: bytes, audio_filter: str | None = None) -> bytes:
    """Apply the small-speaker conditioning chain to an audio clip, as bytes.

    The full-duplex path conditions inside its own encode step, where the filter
    runs as part of the single ffmpeg pass that also produces mu-law. The
    webrtc/srtp transports decode the clip themselves, so they need the same
    treatment applied up front -- that is what this is for (see talk._run_talk).
    Keeping it here means one definition of "conditioned audio" for every path.

    audio_filter: None -> config.TALK_AUDIO_FILTER; "" -> no conditioning (the
    bytes are returned untouched, without spawning ffmpeg); any string -> that
    chain. Output is WAV; the transports re-decode it anyway.

    NOTE: the default chain is tuned for the ADC-VDB750's small speaker (see the
    E20 backlog item) -- another device may want different cutoffs, which is what
    the per-request override is for.
    """
    af = c.TALK_AUDIO_FILTER if audio_filter is None else audio_filter
    af = (af or "").strip()
    if not af or not data:
        return data

    import tempfile
    with tempfile.TemporaryDirectory(prefix="talkcond-") as tmp:
        src = os.path.join(tmp, "in")
        dst = os.path.join(tmp, "out.wav")
        # Temp files rather than pipes: a WAV written to a non-seekable pipe gets
        # placeholder sizes in its header, which some decoders handle poorly.
        with open(src, "wb") as f:
            f.write(data)
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
            "-vn", "-af", af, "-f", "wav", dst,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(proc.communicate(),
                                            timeout=c.TALK_MEDIA_FETCH_TIMEOUT_S)
        except asyncio.TimeoutError:
            proc.kill()
            raise TalkdownError("ffmpeg audio conditioning timed out")
        if proc.returncode != 0:
            raise TalkdownError(f"ffmpeg audio conditioning failed: {(err or b'')[:200]!r}")
        with open(dst, "rb") as f:
            out = f.read()
    if not out:
        raise TalkdownError("ffmpeg audio conditioning produced no audio")
    return out


def _reconnectable(err: BaseException) -> bool:
    """Is this a failure a NEW SOCKET on the SAME credentials can fix?

    Only two things qualify, and -- this is the whole reason to distinguish them
    -- both are cheap to retry in place:

      * the connection itself died: a reset or an EOF, which surfaces INSTANTLY,
        so retrying it costs microseconds. (E11. Note aiohttp already absorbs the
        first such drop by itself: it retries an idempotent request once when a
        persistent connection is closed under it, and PUT is idempotent. What
        reaches here is a drop that outlived that retry.)
      * a 401 the Digest middleware could not clear (E7 -- a rotated nonce its
        own single retry did not settle, or a 401 with no WWW-Authenticate to
        re-challenge from). Also one round trip. A fresh session means a fresh
        handshake with `nc` restarting at 1.

    A TIMEOUT is deliberately NOT in that set, even though aiohttp models
    ServerTimeoutError as a subclass of ClientConnectionError -- hence the
    explicit check first. A timeout means the peer took the connection and then
    did not answer, which is the session being dead, not the socket; and unlike
    the two cases above, each retry costs a full sock_read timeout. Two of those
    is ten seconds of dead air in the middle of an announcement, spent before
    starting the renegotiation that was needed all along.

    Everything else -- an HTTP 5xx, an unparseable response -- is likewise the
    SESSION saying it is finished, and reconnecting to it would only waste round
    trips before the renegotiation that was actually needed.
    """
    if isinstance(err, asyncio.TimeoutError):
        return False
    return isinstance(err, (_AuthStale, aiohttp.ClientConnectionError))


def _percentile(values: list, pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    idx = min(len(ordered) - 1, max(0, int(round((pct / 100.0) * (len(ordered) - 1)))))
    return ordered[idx]


async def stream_talkdown(put_url: str, password: str, audio_source: str, *,
                          username: str = DIGEST_USER, codec: str = DEFAULT_CODEC,
                          audio_filter: str | None = None,
                          pace: bool = True, timeout_s: float | None = None,
                          renegotiate=None, session_deadline: float | None = None,
                          stop_event: "asyncio.Event | None" = None,
                          cert_pin: "_CertPin | None" = None) -> dict:
    """Play `audio_source` through the doorbell speaker via chunked Digest PUT.

    put_url  -- the PutAudioStream endpoint (duae/luae for LAN, puae/uae for WAN)
    password -- the per-session stream password (Digest password for "vieweruser")
    codec    -- "mulaw" (the only codec this doorbell accepts; see CODECS)
    renegotiate -- optional async callable returning a fresh session as
                   {"put_url", "password", "username"[, "cert_pin"]}. When the
                   stream session dies mid-clip (its per-session URL/port/password
                   expire) it is called for fresh credentials and the transport
                   RESUMES FROM THE EXACT BYTE OFFSET it reached, so no audio is
                   lost or repeated. Bounded by config.TALK_MAX_RENEGOTIATIONS.
                   A dropped socket or an unrecoverable 401 is retried in place
                   first (config.TALK_MAX_RECONNECTS) -- see _reconnectable().
    stop_event  -- optional asyncio.Event; when set, the stream stops cleanly at
                   the next chunk boundary (<=CHUNK_MS) and returns partial stats
                   with "stopped": True. An event rather than task cancellation,
                   so the MWS teardown in the caller's finally still runs normally.
    session_deadline -- optional time.monotonic() value at which the current
                   session is known to expire. When given, the transport swaps
                   PROACTIVELY (config.TALK_RENEGOTIATE_MARGIN_S ahead) instead of
                   waiting for the first failed PUT.
    cert_pin -- optional _CertPin (see make_cert_pin) checked against the device's
                   self-signed certificate. None keeps the historical
                   no-verification behaviour.

    Returns {"path": "full_duplex", "codec": ..., "chunks": n, "bytes": total,
    "seconds": s, "renegotiations": n, ...} plus the E6 latency measurements.
    Guarantees ffmpeg + the HTTP session are torn down on every path. Raises
    TalkdownError on a transport/codec failure (so the caller can fall back)."""
    spec = CODECS.get(codec)
    if spec is None:
        raise TalkdownError(f"unknown codec {codec!r}")
    bytes_per_s = spec["bytes_per_s"]
    chunk_bytes = bytes_per_s * CHUNK_MS // 1000

    # None -> the config default filter; "" -> conditioning disabled;
    # any string -> that ffmpeg filter chain (per-request override).
    af = c.TALK_AUDIO_FILTER if audio_filter is None else audio_filter
    src = await _open_source(audio_source, spec, af, chunk_bytes)

    headers_base = {
        "User-Agent": "GStreamer souphttpclientsink libsoup/3.6.0",
        "Content-Type": spec["content_type"],
    }
    # Per-socket timeouts: the deadline below is only checked BETWEEN chunks, so
    # without these a single wedged PUT would pin the per-camera talk lock for
    # aiohttp's 300s default. A 40ms audio chunk never needs seconds.
    sock_timeout = aiohttp.ClientTimeout(total=None, sock_connect=5, sock_read=5)

    sent = 0
    chunks = 0
    renegotiations = 0
    reconnects = 0            # CONSECUTIVE; any successful chunk resets it
    reconnects_total = 0
    stopped = False
    finished = False
    pending = b""             # the chunk being delivered; retained across a
                              # reconnect/renegotiation so the resume offset is
                              # exact even though a stream cannot be rewound
    idx = 0                   # chunks sent overall, drives the pacing schedule
    lead_ms = float(PREBUFFER_MS)
    rtts_ms: list = []
    late_chunks = 0
    max_late_ms = 0.0
    t0 = time.monotonic()     # never moves; the safety deadline is anchored here
    deadline_extra = 0.0      # time renegotiation swaps cost (dead air, not audio)
    start = None              # pacing anchor; set when the first chunk goes out
    next_log = t0 + PROGRESS_LOG_S

    def _deadline() -> float:
        """Backstop against a wedged connection -- NOT a cap on the talk.

        Derived from the audio, exactly as before E5, except that with a
        streaming encoder "the audio" is what has been decoded so far rather than
        the whole clip up front. That preserves the property that matters: the
        deadline advances only while audio is actually being produced, so a
        stalled stream stops extending it and it fires. Once the encoder is
        complete, "produced" IS the clip length and this is bit-for-bit the old
        expression.
        """
        if timeout_s is not None:
            return t0 + timeout_s + deadline_extra
        produced_s = src.produced / bytes_per_s
        return t0 + produced_s + max(30.0, 0.5 * produced_s) + deadline_extra

    def _rebaseline() -> None:
        """After dead air (a reconnect or a session swap), re-anchor the pacing
        schedule so the next chunk goes immediately and then paces normally --
        rather than bursting to "catch up", which would overrun the doorbell's
        small buffer."""
        nonlocal start
        if start is not None:
            start = time.monotonic() - idx * (CHUNK_MS / 1000.0) + lead_ms / 1000.0

    def _maybe_adapt_lead() -> None:
        """E6, opt-in: raise the jitter-buffer lead to cover the worst observed
        PUT round trip. One-directional by design -- see config."""
        nonlocal lead_ms
        if not c.TALK_PREBUFFER_ADAPTIVE or not rtts_ms:
            return
        window = rtts_ms[-_ADAPT_WINDOW_CHUNKS:]
        want = min(float(c.TALK_PREBUFFER_MAX_MS),
                   max(float(PREBUFFER_MS), max(window) * _PREBUFFER_JITTER_FACTOR))
        if want > lead_ms + 1.0:
            log.info("talkdown: raising prebuffer lead %.0f -> %.0f ms "
                     "(worst PUT round trip in the last %d chunks: %.0f ms)",
                     lead_ms, want, len(window), max(window))
            lead_ms = want

    def _session():
        """A fresh ClientSession for the CURRENT credentials.

        HTTP Digest is handled by aiohttp's DigestAuthMiddleware (3.12+): it
        answers the doorbell's 401 challenge, then -- preemptive by default --
        signs every subsequent request from the cached challenge with an
        incrementing nc, which is exactly what this chunk loop needs.

        E7 notes, from reading the installed aiohttp 3.14.3 source rather than
        assuming:
          * A `stale=true` challenge is already handled. The middleware does not
            inspect `stale` at all: ANY 401 carrying a Digest challenge replaces
            its cached challenge and retries the request once, and because the
            nonce changed its `nc` restarts at 1 -- which is precisely the
            correct response to `stale=true`. Nothing to add here.
          * `nc` cannot roll over on a real stream. It is formatted "%08x" and
            increments once per request, so it would need 2**32 chunks; at
            CHUNK_MS=40 that is over five YEARS of continuous audio on one nonce.
            The limit is unreachable, so guarding it would be dead code.
          * What the middleware canNOT recover is a SECOND consecutive 401 (its
            retry loop is `range(2)`), or a 401 with no WWW-Authenticate to
            re-challenge from. That is the real gap, and it is handled a level up:
            such a 401 raises _AuthStale, and the loop rebuilds this session --
            new middleware, new handshake, `nc` from 1 -- on the same credentials
            before spending a renegotiation on it.

        A renegotiated session gets a NEW password, hence a new middleware and a
        new connector (the old socket belongs to a dead session).
        """
        conn = aiohttp.TCPConnector(ssl=_ssl_param(cert_pin), limit=1, force_close=False)
        digest = aiohttp.DigestAuthMiddleware(login=username, password=password)
        return aiohttp.ClientSession(connector=conn, timeout=sock_timeout,
                                     middlewares=(digest,))

    try:
        while not finished:
            recover = None
            # One iteration per session generation: stream until the audio runs
            # out, or until this session dies / is about to expire.
            async with _session() as s:

                async def put_chunk(body: bytes) -> None:
                    nonlocal chunks, sent
                    t_put = time.monotonic()
                    try:
                        async with s.put(put_url, data=body, headers=dict(headers_base)) as r:
                            if r.status == 401:
                                raise _AuthStale(
                                    "PUT rejected with 401 after the Digest handshake "
                                    "(the middleware's own re-challenge did not clear it)")
                            if r.status // 100 != 2:
                                txt = (await r.read())[:120]
                                raise TalkdownError(f"PUT -> HTTP {r.status}: {txt!r}")
                            await r.read()
                    except aiohttp.ServerFingerprintMismatch as e:
                        # E12 enforce mode. Never retried -- every reconnect would
                        # meet the same certificate. _CertPin.check() has already
                        # logged the actionable detail (both fingerprints + the fix).
                        raise _CertPinMismatch(
                            "the device's TLS certificate does not match "
                            "TALK_DEVICE_CERT_SHA256 and TALK_TLS_PIN_MODE=enforce; "
                            "re-pin it (or set TALK_TLS_PIN_MODE=warn) if the device "
                            "was replaced, reset or updated") from e
                    if len(rtts_ms) < _MAX_RTT_SAMPLES:
                        rtts_ms.append((time.monotonic() - t_put) * 1000.0)
                    chunks += 1
                    sent += len(body)

                try:
                    while True:
                        if stop_event is not None and stop_event.is_set():
                            stopped = True
                            finished = True
                            break
                        if time.monotonic() > _deadline():
                            raise _DeadlineExceeded(
                                f"talkdown exceeded safety deadline after {chunks} chunks")
                        # Proactive swap: get ahead of a known expiry rather than
                        # discovering it as a failed PUT mid-sentence.
                        if (session_deadline is not None and renegotiate is not None
                                and renegotiations < c.TALK_MAX_RENEGOTIATIONS
                                and time.monotonic() >= session_deadline - c.TALK_RENEGOTIATE_MARGIN_S):
                            log.info("talkdown: session expiry approaching, renegotiating "
                                     "early at %d chunks", chunks)
                            recover = "renegotiate"
                            break
                        if not pending:
                            pending = await src.next_chunk(chunk_bytes)
                            if not pending:
                                finished = True
                                break
                        if start is None:
                            # Anchor the schedule on the first byte actually sent,
                            # not on encoder startup -- which is where the buffered
                            # path anchored it too (encoding finished first).
                            start = time.monotonic()
                            next_log = start + PROGRESS_LOG_S
                        await put_chunk(pending)
                        pending = b""
                        reconnects = 0        # this session is demonstrably alive
                        idx += 1
                        if pace:
                            # Schedule-based pacing: sleep until THIS chunk's
                            # real-time slot (start + idx*CHUNK_MS), absorbing
                            # per-PUT latency so wall-clock tracks audio time (a
                            # fixed sleep-after-each-PUT drifted ~10%). The
                            # lead keeps a constant jitter buffer at the doorbell
                            # so playback stays continuous instead of
                            # underrunning into gaps.
                            slack = (start + idx * (CHUNK_MS / 1000.0)
                                     - lead_ms / 1000.0) - time.monotonic()
                            if slack > 0:
                                await asyncio.sleep(slack)
                            elif idx * CHUNK_MS > lead_ms:
                                # E6 measurement: the slot had already passed, so
                                # the lead is being eaten by round-trip time.
                                # Chunks inside the opening lead are excluded --
                                # those go out back to back BY DESIGN (that burst
                                # is what fills the doorbell's jitter buffer) and
                                # counting them would report every healthy talk as
                                # a dozen chunks late.
                                late_chunks += 1
                                max_late_ms = max(max_late_ms, -slack * 1000.0)
                        if idx % _ADAPT_WINDOW_CHUNKS == 0:
                            _maybe_adapt_lead()
                        now = time.monotonic()
                        if now >= next_log:
                            produced_s = src.produced / bytes_per_s
                            log.info("talkdown: %s %d chunks, %.0fs elapsed of %.0fs%s to %s",
                                     spec["label"], chunks, now - start, produced_s,
                                     "" if src.complete else "+ decoded so far",
                                     urlsplit(put_url).netloc)
                            next_log = now + PROGRESS_LOG_S
                except (_DeadlineExceeded, _EncodeFailed, _CertPinMismatch):
                    # None of these is a bad SESSION, so none of them is fixed by
                    # getting another one.
                    raise
                except (TalkdownError, aiohttp.ClientError, asyncio.TimeoutError) as err:
                    if _reconnectable(err) and reconnects < c.TALK_MAX_RECONNECTS:
                        reconnects += 1
                        reconnects_total += 1
                        recover = "reconnect"
                        log.warning("talkdown: connection/auth lost at %d chunks (%s); "
                                    "reconnecting (%d/%d) on the same session",
                                    chunks, err, reconnects, c.TALK_MAX_RECONNECTS)
                    elif renegotiate is not None and renegotiations < c.TALK_MAX_RENEGOTIATIONS:
                        recover = "renegotiate"
                        log.warning("talkdown: session failed at %d chunks (%s); "
                                    "renegotiating", chunks, err)
                    else:
                        # Without a way to get a new session, this is fatal
                        # exactly as it was before renegotiation existed.
                        raise

            if finished:
                break

            if recover == "reconnect":
                # E11/E7: same credentials, new socket + new Digest handshake.
                # The pending chunk is retried, so nothing is lost or repeated.
                # The safety deadline is deliberately NOT extended -- a reconnect
                # is milliseconds, and not extending it is what stops a link that
                # blips forever from streaming forever.
                _rebaseline()
                continue

            # ---- renegotiate: a fresh session, resume at the SAME byte offset --
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
            if "cert_pin" in fresh:
                # A renegotiation may land on a different endpoint kind (LAN ->
                # relay), and the relay is not the device -- so the pin travels
                # with the session rather than being fixed for the whole talk.
                cert_pin = fresh["cert_pin"]
            swap_s = time.monotonic() - swap_started
            # The swap is dead air, not audio time: push the safety deadline out
            # by what it cost, and re-baseline the pacing schedule.
            deadline_extra += swap_s
            _rebaseline()
            log.info("talkdown: renegotiated in %.1fs (#%d), resuming at %d chunks to %s",
                     swap_s, renegotiations, chunks, urlsplit(put_url).netloc)
    finally:
        await src.aclose()

    produced_s = src.produced / bytes_per_s
    played_s = sent / bytes_per_s
    log.info("talkdown: %s %d chunks / %d bytes %s (played %.1fs of %.1fs, "
             "%d reconnect(s), %d renegotiation(s)) to %s",
             "STOPPED after" if stopped else "streamed", chunks, sent, spec["label"],
             played_s, produced_s, reconnects_total, renegotiations,
             urlsplit(put_url).netloc)
    result = {"path": "full_duplex", "codec": codec, "chunks": chunks,
              "bytes": sent, "seconds": round(produced_s, 1),
              "played_seconds": round(played_s, 1),
              "renegotiations": renegotiations, "reconnects": reconnects_total,
              "stopped": stopped, "encode": "streaming" if c.TALK_STREAMING_ENCODE else "buffered"}
    # E6 measurement -- always collected, so a future prebuffer/chunk-size tuning
    # has real numbers instead of a guess. late_chunks is the one that matters:
    # it counts chunks whose scheduled slot had already passed when they were
    # sent, i.e. round-trip time eating into the jitter-buffer lead.
    if rtts_ms:
        result.update({
            "put_rtt_p50_ms": round(_percentile(rtts_ms, 50), 1),
            "put_rtt_p95_ms": round(_percentile(rtts_ms, 95), 1),
            "put_rtt_max_ms": round(max(rtts_ms), 1),
            "put_rtt_stdev_ms": round(statistics.pstdev(rtts_ms), 1) if len(rtts_ms) > 1 else 0.0,
            "late_chunks": late_chunks,
            "max_late_ms": round(max_late_ms, 1),
            "prebuffer_ms": round(lead_ms),
        })
    if cert_pin is not None and cert_pin.observed:
        result["cert_pinned"] = not cert_pin.mismatch
    return result
