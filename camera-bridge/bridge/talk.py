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
import time
from urllib.parse import urlsplit

import aiohttp

from . import config as c
from . import talk_srtp
from . import metrics
from . import talk_fullduplex
from . import talk_webrtc
from .mobile_auth import MobileSession, mint_if_stale
from .mws_client import (
    DOORBELL_DEVICE_ID,
    end_full_duplex,
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



def _redact_media_url(url: str) -> str:
    """audio_url with userinfo and query string stripped, safe for logs/errors.

    The URL is caller-supplied and routinely a Home Assistant media URL, which
    can carry a signed access token in its query string (e.g. authSig on a
    media-source URL) -- and nothing stops a caller embedding user:pass@
    userinfo either. Scheme, host and path identify the source; the rest is the
    same class of live capability token the snapshot handler already refuses to
    log."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    redacted = f"{parts.scheme}://{host}{parts.path}"
    if parts.query:
        redacted += "?<query redacted>"
    return redacted


def _failure_reason(err: BaseException) -> str:
    """Short, stable label for a failed talk -- the value that gets counted.

    Deliberately coarse: these are for spotting a pattern ("everything is failing
    with no_upstream_audio since Tuesday"), not for reconstructing a stack trace,
    which is what the log is for. Never includes the exception message, which can
    carry a URL or other detail that does not belong in a counter."""
    if isinstance(err, TalkMediaTooLarge):
        return "media_too_large"
    if isinstance(err, TalkMediaError):
        return "media_error"
    if isinstance(err, NoUpstreamAudio):
        return "no_upstream_audio"
    if isinstance(err, asyncio.TimeoutError):
        return "timeout"
    if isinstance(err, asyncio.CancelledError):
        return "cancelled"
    if isinstance(err, talk_fullduplex.TalkdownError):
        return "transport_error"
    if isinstance(err, aiohttp.ClientError):
        return "network_error"
    if isinstance(err, RuntimeError):
        return "upstream_error"
    return type(err).__name__


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
        # Stop signal for the talk currently in flight on a camera, if any.
        # Present only while a talk is running (see speak/stop), so membership
        # doubles as "is a talk in flight for this camera".
        self._stop_events: dict[str, asyncio.Event] = {}
        # camera_id -> (expires_at_monotonic, doorbell dict). See
        # config.TALK_DOORBELL_CACHE_TTL_S for why reusing a session across talks
        # is safe here and how a stale entry self-corrects.
        self._doorbell_cache: dict[str, tuple[float, dict]] = {}

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
            metrics.talk.rejected_in_progress(camera_id)
            raise TalkInProgress(camera_id)

        async with cam_lock:
            stop_event = asyncio.Event()
            self._stop_events[camera_id] = stop_event
            # Outcome accounting for /control/metrics. A talkdown failure is
            # otherwise invisible after the fact: it surfaces as one HTTP error to
            # whoever asked and nothing keeps score, so "has this been failing all
            # week?" needs a log grep. Recorded here, at the single entry point,
            # so every transport and every failure mode is counted the same way.
            metrics.talk.attempt()
            started = time.monotonic()
            try:
                result = await self._speak_locked(
                    camera_id, stop_event, audio_url=audio_url,
                    audio_path=audio_path, codec=codec, audio_filter=audio_filter)
            except BaseException as err:
                # Whatever went wrong, do not hand the next talk a session that
                # may have been part of the problem.
                self._doorbell_cache.pop(camera_id, None)
                metrics.talk.failure(camera_id, _failure_reason(err),
                                     wall_s=time.monotonic() - started)
                raise
            else:
                metrics.talk.success(camera_id, wall_s=time.monotonic() - started,
                                     result=result if isinstance(result, dict) else None)
                return result
            finally:
                self._stop_events.pop(camera_id, None)

    async def speak_live(self, camera_id: str, source) -> dict:
        """Stream a LIVE mu-law source (talk_fullduplex.LiveMulawSource)
        through the camera's speaker -- the call bridge's downlink. Same lock,
        metrics and teardown as speak(); no clip resolution and no
        conditioning (the source is already wire-format). Ends when the
        source closes, stop() is called, or the transport fails."""
        cam_lock = self._lock_for(camera_id)
        if cam_lock.locked():
            metrics.talk.rejected_in_progress(camera_id)
            raise TalkInProgress(camera_id)
        async with cam_lock:
            stop_event = asyncio.Event()
            self._stop_events[camera_id] = stop_event
            metrics.talk.attempt()
            started = time.monotonic()
            try:
                doorbell = await self._resolve_doorbell(camera_id)
                if doorbell is None:
                    raise NoUpstreamAudio(
                        f"{camera_id} does not expose the full-duplex PUT path")
                result = await self._run_full_duplex(
                    camera_id, doorbell, b"", codec="mulaw", audio_filter="",
                    stop_event=stop_event, source_obj=source)
            except BaseException as err:
                self._doorbell_cache.pop(camera_id, None)
                metrics.talk.failure(camera_id, _failure_reason(err),
                                     wall_s=time.monotonic() - started)
                raise
            else:
                metrics.talk.success(camera_id, wall_s=time.monotonic() - started,
                                     result=result if isinstance(result, dict) else None)
                return result
            finally:
                self._stop_events.pop(camera_id, None)

    def stop(self, camera_id: str) -> bool:
        """Ask the talk in flight on this camera to stop.

        Returns True if one was running (the stream ends at the next chunk
        boundary, <=40ms, and its POST returns partial stats), False if there was
        nothing to stop. Signalling rather than cancelling keeps the MWS teardown
        (EndLiveStream) on its normal path."""
        event = self._stop_events.get(camera_id)
        if event is None:
            return False
        log.info("talkdown: stop requested for %s", camera_id)
        event.set()
        return True

    async def _speak_locked(self, camera_id: str, stop_event: asyncio.Event, *,
                            audio_url: str | None, audio_path: str | None,
                            codec: str | None, audio_filter: str | None) -> dict:
        """The body of speak(), with the per-camera lock already held."""
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
                return await self._run_full_duplex(
                    camera_id, doorbell, audio_bytes, codec=codec,
                    audio_filter=audio_filter, stop_event=stop_event)
        endpoint = await self._resolve_endpoint(camera_id)
        return await asyncio.wait_for(
            self._run_talk(endpoint, audio_bytes, audio_filter=audio_filter),
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
        same cap. The path is not a secret and may be logged as-is; the URL may
        carry a signed access token in its query string (an HA media-source
        authSig, for one), so it only ever reaches a log line or an exception
        message through _redact_media_url."""
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
                        raise TalkMediaTooLarge(_redact_media_url(url))
        except TalkMediaError:
            raise
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            # str(e) for a ClientResponseError embeds the full request URL,
            # which for an HA media-source audio_url includes its signed access
            # token -- the same leak the snapshot handler had. Describe the
            # failure without the raw URL, and break the exception chain (`from
            # None`): a chained cause is re-formatted, raw URL and all, when the
            # control server logs this TalkMediaError with exc_info.
            status = getattr(e, "status", None)
            detail = f"HTTP {status}" if status is not None else type(e).__name__
            raise TalkMediaError(
                f"could not fetch audio_url {_redact_media_url(url)}: {detail}"
            ) from None
        data = buf.getvalue()
        if not data:
            raise TalkMediaError("audio_url returned an empty body")
        log.info("talkdown: fetched %d bytes of audio from %s",
                 len(data), _redact_media_url(url))
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
    async def resolve_doorbell(self, camera_id: str) -> dict | None:
        """Public accessor: the parsed doorbell <cli> (endpoints, per-session
        creds, capability flags) for camera_id, or None if it advertises no
        upstream audio. call_bridge uses it to pick the direct-LAN RTSP listen
        URL and to gate the call on advertised capabilities."""
        return await self._resolve_doorbell(camera_id)

    async def _resolve_doorbell(self, camera_id: str) -> dict | None:
        """Return the parsed doorbell <cli> for camera_id if it supports
        upstream audio (the full-duplex talk path), else None so the caller
        falls back to the webrtc/srtp endpoint path.

        Mints/reuses the MWS SessionToken through the shared login_lock exactly
        like _resolve_endpoint, re-minting once on a server-side logout."""
        cached = self._doorbell_cache.get(camera_id)
        if cached and time.monotonic() < cached[0]:
            log.debug("talkdown: reusing cached doorbell session for %s", camera_id)
            return cached[1]

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
                if c.TALK_DOORBELL_CACHE_TTL_S > 0:
                    self._doorbell_cache[camera_id] = (
                        time.monotonic() + c.TALK_DOORBELL_CACHE_TTL_S, d)
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

    async def _select_put_url(self, doorbell: dict) -> tuple[str | None, str | None]:
        """Pick a PutAudioStream URL: TALK_PUT_ENDPOINT_ORDER as the PREFERENCE,
        filtered by what actually answers right now (E10). Returns (kind, url).

        The four URLs on the <cli> are four routes to (mostly) the same device:
        `direct` and `local` are its LAN address, `public` is the site's own WAN
        address, and only `cloud` is a different host (the vendor relay). On-LAN
        the old blind walk down the order already picked the right one -- the
        value here is in NOT STALLING: off-LAN, `direct`/`local` are RFC1918
        addresses that do not answer, and the first thing that discovers that is
        otherwise a TCP connect inside the audio session, holding the per-camera
        talk lock for however long the OS takes to give up.

        So the candidates are probed CONCURRENTLY and consumed in preference
        order, which costs nothing when the preferred one answers (a LAN connect
        resolves in about a millisecond) and bounds the unreachable case at
        TALK_ENDPOINT_PROBE_TIMEOUT_S.

        A probe is a bare TCP connect -- no TLS, no HTTP, nothing written -- so
        it cannot disturb the audio path or the device's stream session.

        Detection is never allowed to be the reason a talk fails: if nothing
        answers, the first preferred URL is returned anyway and the PUT gets to
        produce the real error.
        """
        put_urls = doorbell.get("put_urls") or {}
        candidates = [(k, put_urls[k]) for k in c.TALK_PUT_ENDPOINT_ORDER if put_urls.get(k)]
        if not candidates:
            return None, None

        # Static override for when detection is wrong. An override naming an
        # endpoint this doorbell does not advertise is a misconfiguration, and
        # the right response to a misconfigured knob is a warning plus the normal
        # behaviour -- not a silent doorbell.
        if c.TALK_PUT_ENDPOINT:
            for kind, url in candidates:
                if kind == c.TALK_PUT_ENDPOINT:
                    log.debug("talkdown: using the configured %s endpoint (probing skipped)", kind)
                    return kind, url
            log.warning("talkdown: TALK_PUT_ENDPOINT=%r is not one of the endpoints this "
                        "doorbell advertises (%s); auto-detecting instead",
                        c.TALK_PUT_ENDPOINT, ",".join(k for k, _ in candidates))

        if c.TALK_ENDPOINT_PROBE_TIMEOUT_S <= 0 or len(candidates) == 1:
            return candidates[0]

        probes = {kind: asyncio.ensure_future(
                      _probe_endpoint(url, c.TALK_ENDPOINT_PROBE_TIMEOUT_S))
                  for kind, url in candidates}
        try:
            for kind, url in candidates:
                try:
                    reachable = await probes[kind]
                except Exception:  # noqa: BLE001
                    continue
                if reachable:
                    return kind, url
                log.debug("talkdown: %s endpoint did not answer a probe", kind)
            log.warning("talkdown: none of the PutAudioStream endpoints (%s) answered a "
                        "connect probe; trying %s anyway",
                        ",".join(k for k, _ in candidates), candidates[0][0])
            return candidates[0]
        finally:
            for task in probes.values():
                if not task.done():
                    task.cancel()


    @staticmethod
    def _session_deadline(expires_iso: str | None) -> float | None:
        """Monotonic time at which the stream session expires, or None.

        The doorbell's <cli> carries `cstet`, which looks like a session-expiry
        timestamp -- but its exact semantics are NOT confirmed (in one capture it
        sat within seconds of the fetch, which would make it a creation time). So
        this only returns a deadline when the value is far enough in the future to
        be meaningful as one; anything else yields None and the transport simply
        falls back to reacting to a failed PUT. Data-driven rather than a guess.
        """
        if not expires_iso:
            return None
        try:
            from datetime import datetime, timezone
            when = datetime.fromisoformat(expires_iso.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=timezone.utc)
            remaining = (when - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError):
            return None
        if remaining < 2 * c.TALK_RENEGOTIATE_MARGIN_S:
            log.debug("talkdown: ignoring implausible session expiry %r (%.0fs away)",
                      expires_iso, remaining)
            return None
        return time.monotonic() + remaining

    async def _run_full_duplex(self, camera_id: str, doorbell: dict,
                               audio_bytes: bytes, *, codec: str | None = None,
                               audio_filter: str | None = None,
                               stop_event: "asyncio.Event | None" = None,
                               source_obj=None) -> dict:
        """StartFullDuplex -> chunked Digest mu-law PUT -> EndLiveStream.

        Modular: audio_bytes is any transcodable clip (TTS, music, ...);
        talk_fullduplex re-encodes it to the doorbell's mu-law/8k/mono. Every
        exit path tears the session down -- EndLiveStream runs best-effort in a
        finally and the spooled temp clip is always removed. Secrets (stream
        password, SessionToken) are never logged."""
        did = doorbell.get("did") or DOORBELL_DEVICE_ID
        password = doorbell.get("stream_password")
        username = doorbell.get("stream_user") or "vieweruser"
        kind, put_url = await self._select_put_url(doorbell)
        if not put_url or not password:
            raise NoUpstreamAudio(
                f"doorbell {doorbell.get('global_device_id')} has no usable "
                f"PutAudioStream URL / stream password")

        if stop_event is not None and stop_event.is_set():
            # Stopped during discovery -- don't open a session just to close it.
            log.info("talkdown: %s: stopped before the session was armed", camera_id)
            return {"path": "full_duplex", "codec": codec or c.TALK_CODEC,
                    "chunks": 0, "bytes": 0, "seconds": 0.0, "played_seconds": 0.0,
                    "renegotiations": 0, "stopped": True}

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

        metrics.talk.endpoint_used(kind)
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
        # Session renegotiation (E1). The PutAudioStream URL/port/password are
        # per-session, so a long clip -- or a network blip -- can outlive the one
        # we opened above. Rather than abort with the audio half-played, hand the
        # transport a way to get a FRESH session; it resumes from the exact byte
        # offset it reached. Everything here (teardown, re-discovery, re-arm) is
        # MWS work that belongs on this side of the boundary, so the transport
        # stays transport-only.
        state = {"did": did}

        async def _renegotiate() -> dict:
            # 1. best-effort close the dying session; a failure here is expected
            #    (it may already be gone) and must not block the swap.
            try:
                await asyncio.wait_for(
                    end_live_stream(
                        self._telus.session, self._mobile.session_token, state["did"],
                        customer_id=self._mobile.customer_id,
                        mobile_device_uid=self._mobile.device_uid,
                        seamless_login_token=self._mobile.seamless_login_token,
                    ),
                    timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001
                log.debug("talkdown: teardown of the dying session failed (ignored)",
                          exc_info=True)

            # 2. fresh GetDoorbellCameraList -> new port, password and PUT URL.
            # Drop the cache first: the session we are replacing is the one that
            # just died, so a cache hit here would hand back the dead one.
            self._doorbell_cache.pop(camera_id, None)
            fresh = await self._resolve_doorbell(camera_id)
            if fresh is None:
                raise RuntimeError("doorbell no longer reports upstream audio")
            new_kind, new_url = await self._select_put_url(fresh)
            new_password = fresh.get("stream_password")
            if not new_url or not new_password:
                raise RuntimeError("renegotiated session has no usable PUT URL / password")

            # 3. re-arm the mic path on the new session.
            state["did"] = fresh.get("did") or state["did"]
            new_ack = await asyncio.wait_for(
                start_full_duplex(
                    self._telus.session, self._mobile.session_token, state["did"],
                    customer_id=self._mobile.customer_id,
                    mobile_device_uid=self._mobile.device_uid,
                    seamless_login_token=self._mobile.seamless_login_token,
                ),
                timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
            )
            if not new_ack.get("ok"):
                raise RuntimeError(
                    f"StartFullDuplex failed on renegotiation: "
                    f"token_status={new_ack.get('token_status')} error={new_ack.get('error')!r}")
            log.info("talkdown: renegotiated session for %s via %s endpoint",
                     fresh.get("global_device_id"), new_kind)
            return {"put_url": new_url, "password": new_password,
                    "username": fresh.get("stream_user") or username,
                    # The pin travels with the session: a renegotiation can land
                    # on a different endpoint kind, and the cloud relay is a
                    # different host with its own CA-signed certificate.
                    "cert_pin": talk_fullduplex.make_cert_pin(new_kind)}

        session_deadline = self._session_deadline(doorbell.get("expires"))
        if source_obj is not None:
            # A live source is handed straight to the transport; there is no
            # clip to spool and no codec fallback to try (the source IS
            # wire-format mu-law -- re-encoding a live stream as anything
            # else has nothing to fall back from).
            try:
                return await talk_fullduplex.stream_talkdown(
                    put_url, password, source_obj, username=username,
                    codec="mulaw", audio_filter="", renegotiate=_renegotiate,
                    session_deadline=session_deadline, stop_event=stop_event,
                    cert_pin=talk_fullduplex.make_cert_pin(kind), live=True)
            finally:
                try:
                    await asyncio.wait_for(
                        end_live_stream(
                            self._telus.session, self._mobile.session_token,
                            state["did"],
                            customer_id=self._mobile.customer_id,
                            mobile_device_uid=self._mobile.device_uid,
                            seamless_login_token=self._mobile.seamless_login_token,
                        ),
                        timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
                    )
                except Exception:
                    log.warning("talkdown: live-session EndLiveStream teardown "
                                "failed (non-fatal)", exc_info=True)
        tmp_path = await self._spool_temp(audio_bytes)
        try:
            last_err: Exception | None = None
            for i, cc in enumerate(attempts):
                try:
                    return await talk_fullduplex.stream_talkdown(
                        put_url, password, tmp_path, username=username, codec=cc,
                        audio_filter=audio_filter, renegotiate=_renegotiate,
                        session_deadline=session_deadline, stop_event=stop_event,
                        # E12: identify the doorbell by its self-signed cert.
                        # Built per attempt because it accumulates per-connection
                        # observations, and only for endpoints that terminate at
                        # the device (see make_cert_pin).
                        cert_pin=talk_fullduplex.make_cert_pin(kind))
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
                        self._telus.session, self._mobile.session_token, state["did"],
                        customer_id=self._mobile.customer_id,
                        mobile_device_uid=self._mobile.device_uid,
                        seamless_login_token=self._mobile.seamless_login_token,
                    ),
                    timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
                )
            except Exception:
                # Leaving the mic-input path armed is the one teardown failure
                # with a lasting effect, so try the other route before giving up.
                log.warning("talkdown: EndLiveStream teardown failed; trying "
                            "EndFullDuplex", exc_info=True)
                self._doorbell_cache.pop(camera_id, None)
                try:
                    await asyncio.wait_for(
                        end_full_duplex(
                            self._telus.session, self._mobile.session_token,
                            state["did"],
                            customer_id=self._mobile.customer_id,
                            mobile_device_uid=self._mobile.device_uid,
                            seamless_login_token=self._mobile.seamless_login_token,
                        ),
                        timeout=c.TALK_STREAM_LOOKUP_TIMEOUT_S,
                    )
                except Exception:
                    log.warning("talkdown: EndFullDuplex fallback also failed "
                                "(non-fatal)", exc_info=True)
            finally:
                _unlink_quiet(tmp_path)

    # ---- transport ----------------------------------------------------------
    async def _run_talk(self, endpoint: dict, audio_bytes: bytes, *,
                        audio_filter: str | None = None) -> dict:
        """Dispatch to the WebRTC or SRTP client. Both tear themselves down on
        every exit path (normal, error, and the CancelledError a wait_for timeout
        raises); the SRTP temp file is removed in a finally regardless.

        The clip is conditioned here so EVERY talk path gets the same treatment:
        the full-duplex transport folds the filter into its own encode step, but
        these two decode the clip themselves, so without this they would push raw
        full-range audio at a small speaker and clip exactly as the doorbell did
        before conditioning existed."""
        audio_bytes = await talk_fullduplex.condition_audio(audio_bytes, audio_filter)
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


async def _probe_endpoint(url: str, timeout_s: float) -> bool:
    """Is there something listening on this URL's host:port right now?

    A bare TCP connect, immediately closed: no TLS handshake, no HTTP request,
    no bytes written. That matters -- this runs against a doorbell whose audio
    endpoint is the thing being probed, and the probe must be incapable of
    disturbing it.

    Any failure is False, never an exception: reachability detection exists to
    pick a better route, so it must never become a new way for a talk to fail.
    """
    parts = urlsplit(url)
    host, port = parts.hostname, parts.port
    if not host:
        return False
    if port is None:
        port = 443 if parts.scheme == "https" else 80
    writer = None
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port),
                                           timeout=timeout_s)
        return True
    except (OSError, asyncio.TimeoutError):
        return False
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001
        return False
    finally:
        if writer is not None:
            writer.close()


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
