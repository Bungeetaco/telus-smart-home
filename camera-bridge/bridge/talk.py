"""On-demand talkdown orchestration for POST /control/cameras/{id}/talk.

Wires the four already-written talkdown modules together behind a single
TalkManager:

  * mobile_auth.MobileSession / mint_if_stale  -- mints (and de-duplicates the
    minting of) the legacy MWS SessionToken the talk endpoints need.
  * mws_client.get_live_video_stream           -- resolves the doorbell's live
    <webrtc>/<srtp> upstream-audio endpoints.
  * talk_webrtc.ADCTalkdown                     -- preferred talk transport.
  * talk_srtp.SrtpTalkdown                      -- SRTP fallback transport.

Deliberately kept separate from lifecycle.py (which owns the always-warm video
pipelines): talkdown is single-shot and user-triggered. A failure here fails one
HTTP request and must never touch the video pipelines or the restart guard, so
nothing in this module calls os._exit / restart_guard, mirroring the "on-demand,
non-fatal" philosophy the control endpoint already follows.

Guardrails (see the individual comments):
  * ONE in-flight talk per camera -- a burst of triggers collapses to a single
    session; concurrent triggers are rejected (409), never queued.
  * All token minting is routed through mobile_auth.mint_if_stale using the SAME
    shared login_lock main.py hands telus_auth, so a website re-login and a
    mobile re-mint can never interleave and a burst mints exactly once.
  * The stream lookup and the whole talk session are each bounded by a timeout;
    the underlying ADCTalkdown/SrtpTalkdown clients tear themselves down on every
    exit path (including the cancellation a wait_for timeout raises), and a
    temp file spooled for the SRTP path is always removed.
  * Secrets (SessionToken, signaling/camera tokens, SRTP keys) are never logged
    here -- only non-secret routing facts.
"""
import asyncio
import io
import logging
import os
import tempfile

import aiohttp

from . import config as c
from . import talk_srtp
from . import talk_fullduplex
from . import talk_webrtc
from .mobile_auth import MobileSession, mint_if_stale
from .mws_client import (
    DOORBELL_DEVICE_ID,
    FOR_DEVICE_DOORBELL,
    TOKEN_STATUS_LOGGED_OUT,
    end_live_stream,
    get_doorbell_endpoints,
    get_live_video_stream,
    start_full_duplex,
)

log = logging.getLogger(__name__)


class TalkInProgress(Exception):
    """A talk is already in flight for this camera -- caller maps to 409."""


class NoUpstreamAudio(Exception):
    """GetLiveVideoStream returned no webrtc/srtp upstream-audio endpoint for
    this device -- caller maps to 422."""


class TalkMediaError(Exception):
    """The supplied audio could not be obtained (bad/empty url or path, fetch
    failure) -- caller maps to 400."""


class TalkMediaTooLarge(TalkMediaError):
    """Fetched/read audio exceeded TALK_MEDIA_MAX_BYTES -- caller maps to 413."""


class TalkManager:
    """Serializes and runs one talkdown session per camera.

    Constructed once in main.py with the shared TelusSession, the MobileSession
    minted off it, and the SAME login_lock telus_auth uses (threaded through, not
    re-created), then handed to the control server exactly as `lifecycle` is.
    """

    def __init__(self, telus, mobile: MobileSession, login_lock: asyncio.Lock):
        self._telus = telus              # bridge.telus_auth.TelusSession
        self._mobile = mobile            # bridge.mobile_auth.MobileSession
        self._login_lock = login_lock    # the shared telus_auth login lock
        # One lock per camera id, created lazily. A burst of triggers for the
        # same camera collapses onto this: the first holds it for the whole
        # session, every concurrent trigger is rejected (TalkInProgress) rather
        # than queued, so we never build a backlog of stale talk sessions.
        self._camera_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, camera_id: str) -> asyncio.Lock:
        return self._camera_locks.setdefault(camera_id, asyncio.Lock())

    async def speak(self, camera_id: str, *, audio_url: str | None = None,
                    audio_path: str | None = None,
                    codec: str | None = None,
                    audio_filter: str | None = None) -> dict:
        """Play one audio clip through the camera's speaker.

        Exactly one of audio_url / audio_path is used (url wins if both given).
        Returns {"path": "webrtc"|"srtp"} on success. Raises TalkInProgress,
        NoUpstreamAudio, TalkMediaError/TalkMediaTooLarge, asyncio.TimeoutError,
        or a transport/auth RuntimeError (which the caller maps to 502).
        """
        cam_lock = self._lock_for(camera_id)
        # Non-blocking rejection: acquiring an uncontended asyncio.Lock does not
        # suspend, so this check-then-acquire cannot interleave two callers -- a
        # second concurrent trigger sees it locked and is rejected immediately.
        if cam_lock.locked():
            raise TalkInProgress(camera_id)

        async with cam_lock:
            audio_bytes = await self._resolve_audio(audio_url, audio_path)
            # Full-duplex (chunked Digest mu-law PUT to the doorbell's own
            # /api/PutAudioStream) is the ONLY working talk path for the
            # ADC-VDB750, which exposes no <webrtc>/<srtp> upstream endpoint.
            # Try it first when the device advertises upstream audio; fall
            # back to the webrtc/srtp endpoint path for other ADC cameras.
            if c.TALK_PREFER_FULL_DUPLEX:
                doorbell = await self._resolve_doorbell(camera_id)
                if doorbell is not None:
                    # No fixed session cap for the full-duplex path: the
                    # transport bounds itself by the AUDIO length (see
                    # talk_fullduplex), so a 10-min clip plays in full. The MWS
                    # auth/start/end steps are each bounded inside
                    # _run_full_duplex; the doorbell imposes no session limit
                    # we've observed.
                    return await self._run_full_duplex(doorbell, audio_bytes, codec=codec, audio_filter=audio_filter)
            endpoint = await self._resolve_endpoint(camera_id)
            return await asyncio.wait_for(
                self._run_talk(endpoint, audio_bytes),
                timeout=c.TALK_SESSION_TIMEOUT_S,
            )

    # ---- audio ingest -------------------------------------------------------
    async def _resolve_audio(self, audio_url: str | None,
                             audio_path: str | None) -> bytes:
        """Return the clip as bytes, bounded by TALK_MEDIA_MAX_BYTES.

        A url is fetched over the bridge's own aiohttp session (reusing its
        connector rather than spinning a fresh one per call), bounded by a
        connect/read timeout AND a hard size cap enforced while streaming so a
        runaway body can't be buffered whole. A path is read off disk under the
        same cap. Neither the url nor the path is a secret, so both may be
        logged."""
        if audio_url:
            return await self._fetch_url(audio_url)
        if audio_path:
            return await self._read_path(audio_path)
        raise TalkMediaError("neither audio_url nor audio_path supplied")

    async def _fetch_url(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=c.TALK_MEDIA_FETCH_TIMEOUT_S)
        try:
            async with self._telus.session.get(url, timeout=timeout) as resp:
                resp.raise_for_status()
                buf = io.BytesIO()
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    buf.write(chunk)
                    if buf.tell() > c.TALK_MEDIA_MAX_BYTES:
                        raise TalkMediaTooLarge(url)
        except TalkMediaError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            raise TalkMediaError(f"could not fetch audio_url: {e}") from e
        data = buf.getvalue()
        if not data:
            raise TalkMediaError("audio_url returned an empty body")
        log.info("talkdown: fetched %d bytes of audio from %s", len(data), url)
        return data

    async def _read_path(self, path: str) -> bytes:
        loop = asyncio.get_running_loop()
        try:
            data = await loop.run_in_executor(None, self._read_path_blocking, path)
        except TalkMediaError:
            raise
        except OSError as e:
            raise TalkMediaError(f"could not read audio_path: {e}") from e
        if not data:
            raise TalkMediaError("audio_path is empty")
        log.info("talkdown: read %d bytes of audio from %s", len(data), path)
        return data

    @staticmethod
    def _read_path_blocking(path: str) -> bytes:
        # Cap the read so an unexpectedly huge file can't be slurped whole; read
        # one byte past the cap to detect an oversize file deterministically.
        with open(path, "rb") as f:
            data = f.read(c.TALK_MEDIA_MAX_BYTES + 1)
        if len(data) > c.TALK_MEDIA_MAX_BYTES:
            raise TalkMediaTooLarge(path)
        return data

    # ---- endpoint resolution ------------------------------------------------
    async def _resolve_endpoint(self, camera_id: str) -> dict:
        """Mint (or reuse) the MWS SessionToken and resolve a talk-capable
        endpoint for camera_id, re-minting exactly once if the token has expired.

        All minting goes through mint_if_stale under the shared login_lock, so a
        burst of talk requests (or a talk racing a website re-login) collapses to
        a single real mint."""
        if self._mobile.session_token is None:
            await mint_if_stale(self._mobile, self._login_lock)

        result = await self._get_stream(camera_id)
        if not result.get("ok") and result.get("token_status") == TOKEN_STATUS_LOGGED_OUT:
            # Token expired server-side. Invalidate and re-mint ONCE (de-duplicated
            # across concurrent callers by mint_if_stale's login_generation check),
            # then retry the lookup a single time.
            log.info("talkdown: MWS token rejected (logged out), re-minting once")
            self._mobile.session_token = None
            await mint_if_stale(self._mobile, self._login_lock)
            result = await self._get_stream(camera_id)

        if not result.get("ok"):
            raise RuntimeError(
                f"GetLiveVideoStream failed: token_status={result.get('token_status')} "
                f"error={result.get('error')!r}")

        endpoint = _select_endpoint(result.get("endpoints") or [])
        if endpoint is None:
            raise NoUpstreamAudio(camera_id)
        log.info("talkdown: selected %s endpoint for %s", endpoint["type"], camera_id)
        return endpoint

    async def _get_stream(self, camera_id: str) -> dict:
        # Bounded so a wedged MWS call fails the one request instead of pinning
        # the per-camera lock until the outer session timeout. Uses the bridge's
        # aiohttp session and threads the MobileSession's device envelope through
        # so the request carries the same fields the app attaches to every token
        # request (see mws_client.get_live_video_stream).
        return await asyncio.wait_for(
            get_live_video_stream(
                self._telus.session,
                self._mobile.session_token,
                camera_id,
                for_device=FOR_DEVICE_DOORBELL,
                customer_id=self._mobile.customer_id,
                mobile_device_uid=self._mobile.device_uid,
                seamless_login_token=self._mobile.seamless_login_token,
            ),
            timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
        )

    # ---- full-duplex talkdown (ADC doorbell) --------------------------------
    async def _resolve_doorbell(self, camera_id: str) -> dict | None:
        """Return the parsed doorbell <cli> for camera_id if it supports
        upstream audio (the full-duplex talk path), else None so the caller
        falls back to the webrtc/srtp endpoint path.

        Mints/reuses the MWS SessionToken through the shared login_lock exactly
        like _resolve_endpoint, re-minting once on a server-side logout."""
        if self._mobile.session_token is None:
            await mint_if_stale(self._mobile, self._login_lock)

        result = await self._get_doorbells()
        if not result.get("ok") and result.get("token_status") == TOKEN_STATUS_LOGGED_OUT:
            log.info("talkdown: MWS token rejected (logged out), re-minting once")
            self._mobile.session_token = None
            await mint_if_stale(self._mobile, self._login_lock)
            result = await self._get_doorbells()

        if not result.get("ok"):
            log.info("talkdown: GetDoorbellCameraList not ok (token_status=%s); "
                     "falling back to live-stream endpoint path",
                     result.get("token_status"))
            return None

        want = str(camera_id)
        want_did = want.split("-")[-1]
        for d in result.get("doorbells") or []:
            if want in (d.get("global_device_id"), d.get("did")) or \
                    str(d.get("did")) == want_did:
                if not d.get("supports_upstream_audio"):
                    log.info("talkdown: doorbell %s does not advertise upstream "
                             "audio; falling back to live-stream path", camera_id)
                    return None
                return d
        return None

    async def _get_doorbells(self) -> dict:
        # Bounded like _get_stream so a wedged MWS call fails the one request
        # instead of pinning the per-camera lock.
        return await asyncio.wait_for(
            get_doorbell_endpoints(
                self._telus.session,
                self._mobile.session_token,
                customer_id=self._mobile.customer_id,
                mobile_device_uid=self._mobile.device_uid,
                seamless_login_token=self._mobile.seamless_login_token,
            ),
            timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
        )

    def _select_put_url(self, doorbell: dict) -> tuple[str | None, str | None]:
        """Pick a PutAudioStream URL by TALK_PUT_ENDPOINT_ORDER (the bridge is
        on-LAN with the doorbell, so direct/local win). Returns (kind, url)."""
        put_urls = doorbell.get("put_urls") or {}
        for kind in c.TALK_PUT_ENDPOINT_ORDER:
            url = put_urls.get(kind)
            if url:
                return kind, url
        return None, None

    async def _run_full_duplex(self, doorbell: dict, audio_bytes: bytes, *,
                               codec: str | None = None,
                               audio_filter: str | None = None) -> dict:
        """StartFullDuplex -> chunked Digest mu-law PUT -> EndLiveStream.

        Modular: audio_bytes is any transcodable clip (TTS, music, ...);
        talk_fullduplex re-encodes it to the doorbell's mu-law/8k/mono. Every
        exit path tears the session down -- EndLiveStream runs best-effort in a
        finally and the spooled temp clip is always removed. Secrets (stream
        password, SessionToken) are never logged."""
        did = doorbell.get("did") or DOORBELL_DEVICE_ID
        password = doorbell.get("stream_password")
        username = doorbell.get("stream_user") or "vieweruser"
        kind, put_url = self._select_put_url(doorbell)
        if not put_url or not password:
            raise NoUpstreamAudio(
                f"doorbell {doorbell.get('global_device_id')} has no usable "
                f"PutAudioStream URL / stream password")

        ack = await asyncio.wait_for(
            start_full_duplex(
                self._telus.session, self._mobile.session_token, did,
                customer_id=self._mobile.customer_id,
                mobile_device_uid=self._mobile.device_uid,
                seamless_login_token=self._mobile.seamless_login_token,
            ),
            timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
        )
        if not ack.get("ok"):
            raise RuntimeError(
                f"StartFullDuplex failed: token_status={ack.get('token_status')} "
                f"success={ack.get('success')} error={ack.get('error')!r}")

        log.info("talkdown: full-duplex armed for %s via %s endpoint",
                 doorbell.get("global_device_id"), kind)
        # Codec: prefer the configured/ requested one (L16 by default) and fall
        # back to mu-law on a transport failure, so an L16-incompatible session
        # still gets audio. NOTE: fallback triggers on a TalkdownError (e.g. an
        # HTTP error), not on merely bad-sounding audio -- a 2xx L16 stream is
        # taken as success. The default lives in config.TALK_CODEC.
        primary = codec or c.TALK_CODEC
        attempts = [primary]
        if c.TALK_CODEC_FALLBACK and c.TALK_CODEC_FALLBACK != primary:
            attempts.append(c.TALK_CODEC_FALLBACK)
        tmp_path = await self._spool_temp(audio_bytes)
        try:
            last_err: Exception | None = None
            for i, cc in enumerate(attempts):
                try:
                    return await talk_fullduplex.stream_talkdown(
                        put_url, password, tmp_path, username=username, codec=cc,
                        audio_filter=audio_filter)
                except talk_fullduplex.TalkdownError as e:
                    last_err = e
                    more = i + 1 < len(attempts)
                    log.warning("talkdown: codec=%s failed: %s%s", cc, e,
                                (" -- falling back to %s" % attempts[i + 1]) if more else "")
            raise last_err if last_err else RuntimeError("talkdown: no codec attempted")
        finally:
            try:
                await asyncio.wait_for(
                    end_live_stream(
                        self._telus.session, self._mobile.session_token, did,
                        customer_id=self._mobile.customer_id,
                        mobile_device_uid=self._mobile.device_uid,
                        seamless_login_token=self._mobile.seamless_login_token,
                    ),
                    timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
                )
            except Exception:
                log.warning("talkdown: EndLiveStream teardown failed (non-fatal)",
                            exc_info=True)
            finally:
                _unlink_quiet(tmp_path)

    # ---- transport ----------------------------------------------------------
    async def _run_talk(self, endpoint: dict, audio_bytes: bytes) -> dict:
        """Dispatch to the WebRTC or SRTP client. Both tear themselves down on
        every exit path (normal, error, and the CancelledError a wait_for timeout
        raises); the SRTP temp file is removed in a finally regardless."""
        if endpoint["type"] == "webrtc":
            talk = talk_webrtc.ADCTalkdown(
                endpoint["signaling_server_url"],
                endpoint["signaling_server_token"],
                endpoint["camera_auth_token"],
            )
            # ADCTalkdown.run accepts raw bytes directly (it decodes them in a
            # bounded executor) and guarantees teardown in its own finally.
            await talk.run(audio_bytes)
            return {"path": "webrtc"}

        if endpoint["type"] == "srtp":
            # SrtpTalkdown shells out to ffmpeg, which reads a file/URL, not raw
            # bytes -- spool the clip to a temp file and hand ffmpeg the path.
            tmp_path = await self._spool_temp(audio_bytes)
            try:
                talk = talk_srtp.SrtpTalkdown(
                    endpoint["aip"],
                    endpoint.get("ap"),
                    endpoint["ack"],
                    endpoint.get("oassrc"),
                    endpoint.get("ac"),
                )
                # Bound ffmpeg well under the outer session timeout so its own
                # teardown runs before wait_for would cancel us.
                ok = await talk.run(tmp_path, timeout=c.TALK_SRTP_TIMEOUT_S)
                if not ok:
                    raise RuntimeError("SRTP talkdown did not complete cleanly")
                return {"path": "srtp"}
            finally:
                await asyncio.get_running_loop().run_in_executor(
                    None, _unlink_quiet, tmp_path)

        raise NoUpstreamAudio(f"unsupported endpoint type {endpoint['type']!r}")

    @staticmethod
    async def _spool_temp(audio_bytes: bytes) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, _spool_temp_blocking, audio_bytes)


def _spool_temp_blocking(audio_bytes: bytes) -> str:
    fd, path = tempfile.mkstemp(prefix="talkdown-", suffix=".audio")
    with os.fdopen(fd, "wb") as f:
        f.write(audio_bytes)
    return path


def _unlink_quiet(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        log.debug("talkdown: could not remove temp audio %s", path, exc_info=True)


def _select_endpoint(endpoints: list[dict]) -> dict | None:
    """Pick the talk-capable endpoint: the <webrtc> one if present (preferred --
    clean sendrecv Opus via aiortc), else the <srtp> one. <rtsp> is video-only
    and never a talk path. A <webrtc> entry must carry a signaling URL to be
    usable; an <srtp> entry must carry a destination host and key."""
    webrtc = [e for e in endpoints
              if e.get("type") == "webrtc" and e.get("signaling_server_url")]
    if c.TALK_PREFER_WEBRTC and webrtc:
        return webrtc[0]
    srtp = [e for e in endpoints
            if e.get("type") == "srtp" and e.get("aip") and e.get("ack")]
    if webrtc:
        return webrtc[0]
    if srtp:
        return srtp[0]
    return None
