"""SRTP talkdown fallback -- upstream (mic -> doorbell) audio for the *non-WebRTC*
branch of GetLiveVideoStream.

Where this fits
---------------
The mobile MWS path returns an <lvsr>/<vsel>/<vsei> document. When it carries a
<webrtc> endpoint we do talkdown over that (aiortc, elsewhere). When it carries
only an <srtp> endpoint -- as the ADC-VDB750 doorbell does -- there is no
signaling handshake at all: the upstream leg is a plain SRTP-over-UDP RTP audio
sender. We parse a host/port/key/SSRC out of the XML and blast SRTP packets at
it. No ICE, no DTLS, no SDP negotiation. That is what makes it usable as a
fallback and what this module implements.

The crypto suite is NOT taken from the XML: the decompiled app hardcodes both
legs to AES_CM_128 / HMAC_SHA1_80 (SRTP default suite, RFC 3711) and never reads
the acc/aca attributes. Master key (16) + master salt (14) = a 30-byte blob, so
base64decode(ack) MUST be exactly 30 bytes -- any other length means the suite
assumption is wrong and we bail rather than transmit garbage.

The upstream codec is *device-implied*, not in the XML (the <srtp> element has no
payload-type field). libvideostreamer picks it by device model. The VDB750 is a
SkyBell OEM doorbell whose SkyBell override uses L16: payload 97, encoding-name
L16, 8000 Hz, 16-bit big-endian PCM, mono. So we send L16/97/8000/mono first
(CODEC_L16); the PCMU/0/8000/mono legacy path (CODEC_PCMU) is the
fallback-within-fallback if the doorbell plays nothing. See the module summary /
docstring notes for the residual uncertainty on this.

Approach (spec Option A): shell out to ffmpeg's `srtp://` output muxer. ffmpeg
7.1 is already in the container and handles resample -> L16 -> RTP -> SRTP
encryption, ROC, sequence/timestamp and realtime pacing in one process, and it
takes the `ack` base64 key *verbatim* as srtp_out_params (no hex conversion --
unlike the GStreamer srtpenc buffer form). Option B (pylibsrtp + hand-rolled
RTP) is documented at the bottom of this file as a comment only; it exists for
the case where ffmpeg's srtp muxer can't force PT=97/SSRC on some build, or where
RTCP SR has to be emitted from the same code (see the RTCP caveat below).

Cleanup is guaranteed: the ffmpeg child is terminated (then killed) on normal
completion, on the caller's timeout, and on cancellation/exception.
"""
import asyncio
import base64
import binascii
import logging

log = logging.getLogger(__name__)

# Hardcoded by the app's SrtpStreamEndpoint.A() regardless of the acc/aca XML
# attrs -- AES-CM-128 with an 80-bit HMAC-SHA1 auth tag. This is ffmpeg's SRTP
# suite name for that combination.
SRTP_SUITE = "AES_CM_128_HMAC_SHA1_80"

# 16-byte master key + 14-byte master salt. base64decode(ack) must equal this.
SRTP_KEY_LEN = 30

# Defaults from the <srtp> parser (ch0/q.java) for fields that may be absent.
DEFAULT_AUDIO_PORT = 5006
DEFAULT_OUT_AUDIO_SSRC = 0xDEADBEEF  # 3735928559

# Upstream codec selector. L16 is the SkyBell/VDB750 path and the primary target;
# PCMU is the legacy two-way-camera fallback. Both are 8 kHz mono for talkdown.
CODEC_L16 = "l16"
CODEC_PCMU = "pcmu"

# UDP payload cap handed to the srtp muxer (well under a 1500-byte MTU once the
# IP/UDP/SRTP-tag overhead is added).
_PKT_SIZE = 1400

# How long to wait for ffmpeg to exit after we ask it to stop before hard-killing.
_TERM_GRACE_S = 5

# Hard ceiling on a single talkdown, used as the DEFAULT timeout for run()/
# send_talkdown. A talkdown phrase is a few seconds; ffmpeg with `-re -i <source>`
# exits at EOF on its own, but any stall (a source that never EOFs, ffmpeg wedged
# on the srtp:// output, a hung DNS/connect) would otherwise pin proc.wait()
# forever and the finally: await self.stop() teardown would never run -- the
# socket/FD-leak class that has blanked these cameras before. Defaulting to a
# finite ceiling guarantees teardown even if the orchestrator forgets to pass one.
DEFAULT_TALKDOWN_TIMEOUT_S = 60


def _decode_ack(ack: str) -> bytes:
    """Validate the base64 SRTP key material and return the 30 raw bytes.

    Raises ValueError loudly if it is not valid base64 or not exactly 30 bytes --
    a wrong length means the AES_CM_128_HMAC_SHA1_80 assumption does not hold and
    transmitting would only send unusable ciphertext at the doorbell.
    """
    try:
        raw = base64.b64decode(ack, validate=True)
    except (binascii.Error, ValueError) as e:
        # Never log `ack` itself -- it is secret key material.
        raise ValueError(f"srtp talkdown: 'ack' is not valid base64 ({e})") from None
    if len(raw) != SRTP_KEY_LEN:
        raise ValueError(
            f"srtp talkdown: decoded 'ack' is {len(raw)} bytes, expected {SRTP_KEY_LEN} "
            f"(16-byte key + 14-byte salt for {SRTP_SUITE}); refusing to transmit -- "
            f"the endpoint is using a crypto suite this fallback does not implement"
        )
    return raw


class SrtpTalkdown:
    """Sends one upstream audio clip to an <srtp> talkdown endpoint via ffmpeg.

    Construct with the parsed endpoint fields, then `await run(source)`. The
    instance is single-shot -- one ffmpeg process per talkdown phrase, matching
    the app's "new session each talkdown, ROC starts at 0" behaviour.
    """

    def __init__(self, aip: str, ap: int | None, ack: str,
                 oassrc: int | None, ac: int | None, *, codec: str = CODEC_L16):
        if not aip:
            raise ValueError("srtp talkdown: 'aip' (audio destination host) is empty")
        if codec not in (CODEC_L16, CODEC_PCMU):
            raise ValueError(f"srtp talkdown: unknown codec {codec!r}")

        self.aip = aip
        self.ap = int(ap) if ap else DEFAULT_AUDIO_PORT
        # Mask to 32 bits -- oassrc default 0xDEADBEEF is already 32-bit, but a
        # caller could hand us a Python int wider than the RTP SSRC field.
        self.oassrc = (int(oassrc) if oassrc is not None else DEFAULT_OUT_AUDIO_SSRC) & 0xFFFFFFFF
        # Parsed but we force mono for talkdown (see below). Retained for reference
        # / a possible honor-ac retry.
        self.ac = int(ac) if ac else 2
        self.codec = codec

        # Validate up front so a bad key fails before we ever launch a subprocess.
        self._raw_key = _decode_ack(ack)
        # Kept as the verbatim base64 string ffmpeg wants; treated as a secret and
        # never logged.
        self._ack_b64 = ack

        self.proc: asyncio.subprocess.Process | None = None
        self._stderr_task: asyncio.Task | None = None

    def _build_args(self, source: str) -> list[str]:
        # SECURITY / ACCEPTED RESIDUAL RISK: the live SRTP master key (the base64
        # `ack`) is embedded in srtp_out_params inside the srtp:// URL, which is
        # the LAST element of argv. ffmpeg's srtp muxer accepts the key ONLY via
        # these URL params, so there is no argv-free channel for the Option A
        # (ffmpeg) approach. That means any process able to read
        # /proc/<pid>/cmdline in the same PID namespace can read the key while
        # ffmpeg runs. We slice the URL off our own log lines (see run()) and
        # never log `ack`, but that redaction does NOT hide the key from the OS
        # process table. This is accepted only under: the container does NOT share
        # its PID namespace and no untrusted local process runs alongside it. If
        # that assumption ever fails, switch to Option B (pylibsrtp in-process,
        # sketch at the bottom of this file) so the key never appears in argv.
        #
        # srtp_out_params takes the base64 key AS-IS (spec Option A / cheat sheet).
        # Do NOT hex-encode it here -- that hex form is only for the GStreamer
        # srtpenc buffer path, which we are not using.
        srtp_url = (
            f"srtp://{self.aip}:{self.ap}"
            f"?srtp_out_suite={SRTP_SUITE}"
            f"&srtp_out_params={self._ack_b64}"
            f"&pkt_size={_PKT_SIZE}"
        )

        # -re paces the input at realtime so the RTP timestamps advance correctly
        # (20 ms/packet at 8 kHz). aresample=async=1 keeps the timeline continuous
        # if the source has gaps.
        args = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning",
            "-re", "-i", source, "-vn",
            "-af", "aresample=async=1",
        ]
        if self.codec == CODEC_L16:
            # L16 = 16-bit big-endian PCM (RFC 3551 network byte order); matches
            # the SkyBell skyL16pay width=16/depth=16 caps. PT 97 is dynamic and
            # device-specific, so force it explicitly.
            args += ["-c:a", "pcm_s16be", "-ar", "8000", "-ac", "1",
                     "-payload_type", "97"]
        else:  # CODEC_PCMU
            # PCMU is statically payload type 0; forcing it is harmless and explicit.
            args += ["-c:a", "pcm_mulaw", "-ar", "8000", "-ac", "1",
                     "-payload_type", "0"]

        # Force the on-wire SSRC to oassrc -- some receivers drop packets whose
        # SSRC does not match the negotiated outgoing SSRC. ffmpeg 7.1's rtp/srtp
        # muxer honours -ssrc.
        args += ["-ssrc", str(self.oassrc), "-f", "rtp", srtp_url]
        return args

    async def run(self, source: str, *,
                  timeout: float | None = DEFAULT_TALKDOWN_TIMEOUT_S) -> bool:
        """Stream `source` (any file ffmpeg can read) to the endpoint as SRTP.

        Blocks until ffmpeg finishes sending the clip (or `timeout` seconds
        elapse). Returns True if ffmpeg exited cleanly, False otherwise. The
        subprocess is always cleaned up before returning, including on timeout,
        cancellation, or exception.

        `timeout` defaults to DEFAULT_TALKDOWN_TIMEOUT_S. WARNING: passing
        timeout=None disables the ceiling -- asyncio.wait_for(proc.wait(),
        timeout=None) then waits forever, so a source that never EOFs or a wedged
        ffmpeg leaks the child process (and its stderr FD) because the finally:
        stop() teardown is never reached. Only pass None if the caller enforces
        its own outer wall-clock bound.
        """
        args = self._build_args(source)
        # Safe to log: the srtp:// URL (which embeds the key) is the LAST element,
        # so slice it off. Host/port/SSRC/codec are not secret.
        log.info("srtp talkdown: sending %s to %s:%d ssrc=0x%08x codec=%s",
                 source, self.aip, self.ap, self.oassrc, self.codec)

        self.proc = await asyncio.create_subprocess_exec(
            *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
        )
        self._stderr_task = asyncio.ensure_future(self._log_stderr())
        try:
            await asyncio.wait_for(self.proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            log.warning("srtp talkdown: ffmpeg still running after %.1fs, stopping it", timeout)
            return False
        finally:
            # Runs on clean exit, timeout, cancellation and exception alike.
            await self.stop()

        ok = self.proc.returncode == 0
        if not ok:
            log.warning("srtp talkdown: ffmpeg exited with code %s", self.proc.returncode)
        return ok

    async def stop(self):
        """Terminate ffmpeg if it is still running, escalating to kill, and stop
        the stderr reader. Idempotent."""
        if self.proc and self.proc.returncode is None:
            self.proc.terminate()
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=_TERM_GRACE_S)
            except asyncio.TimeoutError:
                self.proc.kill()
                try:
                    await self.proc.wait()
                except Exception:
                    pass
        if self._stderr_task:
            self._stderr_task.cancel()
            self._stderr_task = None

    async def _log_stderr(self):
        try:
            while self.proc and self.proc.stderr:
                line = await self.proc.stderr.readline()
                if not line:
                    break
                log.warning("srtp talkdown: ffmpeg: %s", line.decode(errors="replace").rstrip())
        except Exception:
            pass


async def send_talkdown(aip: str, ap: int | None, ack: str, oassrc: int | None,
                        ac: int | None, source: str, *, codec: str = CODEC_L16,
                        timeout: float | None = DEFAULT_TALKDOWN_TIMEOUT_S) -> bool:
    """Convenience one-shot wrapper around SrtpTalkdown.

    codec defaults to CODEC_L16 (the VDB750 path); pass CODEC_PCMU as the
    fallback-within-fallback if L16 produces no audio at the doorbell. `timeout`
    defaults to DEFAULT_TALKDOWN_TIMEOUT_S; see SrtpTalkdown.run for the
    timeout=None teardown caveat.
    """
    return await SrtpTalkdown(aip, ap, ack, oassrc, ac, codec=codec).run(source, timeout=timeout)


# ---------------------------------------------------------------------------
# Option B (DOCUMENTED FALLBACK ONLY -- not implemented, not exercised).
#
# If ffmpeg's srtp:// muxer ever proves unable to force PT=97 / the oassrc SSRC
# on a given build, or if the doorbell tears the session down for lack of RTCP
# (see caveat below) and we need to emit SRTCP SR from the same process, replace
# the ffmpeg subprocess with pylibsrtp (the same lib aiortc uses) + a hand-rolled
# RTP sender. Sketch:
#
#   import base64, struct
#   from pylibsrtp import Policy, Session
#
#   raw = base64.b64decode(ack)                    # 30 bytes = 16 key + 14 salt
#   assert len(raw) == SRTP_KEY_LEN
#   pol = Policy(key=raw, ssrc_type=Policy.SSRC_ANY_OUTBOUND)
#   pol.srtp_profile = Policy.SRTP_PROFILE_AES128_CM_SHA1_80   # == AES_CM_128_HMAC_SHA1_80
#   tx = Session(policy=pol)
#
#   def rtp(seq, ts, ssrc, payload, pt=97):
#       b = bytearray(12)
#       b[0] = 0x80; b[1] = pt & 0x7F
#       struct.pack_into("!HII", b, 2, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc & 0xFFFFFFFF)
#       return bytes(b) + payload
#
# Decode the source to 8 kHz mono S16BE yourself (e.g. `ffmpeg -f s16be`), split
# into 20 ms frames (160 samples = 320 bytes for L16; 160 bytes for PCMU),
# timestamp += 160 per frame, seq incrementing from a random start, SSRC = oassrc,
# ROC starts at 0. For each frame: enc = tx.protect(rtp(...)); udp.sendto(enc,
# (aip, ap)); then sleep ~20 ms to pace. RTCP SR (if needed) goes to port ap+1
# via tx.protect_rtcp(). This keeps encryption/ROC/pacing in-process at the cost
# of reimplementing what ffmpeg already does for us in Option A.
# ---------------------------------------------------------------------------
