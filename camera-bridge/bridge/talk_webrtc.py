"""End-to-end WebRTC talkdown client for TELUS's alarm.com-hosted (ADC) doorbell
two-way audio, using aiortc. This is the *preferred* talk path: it speaks the ADC
signaling protocol directly over the per-session signaling WebSocket and pushes a
local audio track upstream to the doorbell's speaker.

Protocol source of truth: decompiled `com.alarm.alarmmobile.android.telus`
`...videostreamer.webrtc.*` (SignalingServerClientImpl / MessageHandler /
BasePeerConnectionClientImpl). Message framing, key names and the direction of
the offer/answer are byte-for-byte from that decompilation -- see
specs/webrtc_talk.md. This module has NOT been exercised against the live account
yet; the transport/aiortc wiring below deliberately mirrors the already-live
janus_client.py so the shared parts are known-good.

Critical direction fact: the *camera* is the offerer and the client answers. We
add our local audio track *before* createAnswer(), which makes the answer's audio
m-line sendrecv = upstream audio into the doorbell. We never create an offer, and
we add no video track.

Teardown is guaranteed on every exit path (normal completion, timeout, signaling
error, the "two way audio may not be available" warning, or any exception):
run() always funnels through teardown() in a finally, which cancels the
background receive task, closes the PeerConnection, closes the WebSocket and
stops the audio track. This bridge has a history of socket/FD leaks, so nothing
here is left dangling.
"""
import asyncio
import contextlib
import fractions
import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor

import av
import websockets
from aiortc import (MediaStreamError, MediaStreamTrack, RTCConfiguration,
                    RTCIceServer, RTCPeerConnection, RTCSessionDescription)
from aiortc.sdp import candidate_from_sdp

log = logging.getLogger(__name__)

PROTOCOL_VERSION = "2.0.1"
# Exact server warning string (TWA_AUDIO_AVAILABILITY_WARNING) meaning the talk
# channel is held by another party -- treated as a hard failure, not retried.
TWA_UNAVAILABLE = "Two way audio may not be available"

# --- timeouts (seconds) --------------------------------------------------------
# Local to this module so the talk path can be tuned independently of the
# video-streaming timeouts; kept in the same ballpark as config.WEBRTCUP_TIMEOUT_S.
WS_CONNECT_TIMEOUT_S = 15       # opening the signaling WebSocket
SESSION_CONNECT_TIMEOUT_S = 25  # HELLO -> ICE "connected", the whole handshake
ICE_GATHERING_TIMEOUT_S = 5     # local ICE gathering after setLocalDescription
CLIP_TIMEOUT_MARGIN_S = 5       # slack on top of the clip's own real-time length
FLUSH_GUARD_S = 0.2             # let the last RTP packets leave before mute_audio
WS_CLOSE_TIMEOUT_S = 5          # bound the graceful WS close on teardown

# Audio frame geometry. 48 kHz mono, 20 ms frames -- opus-native, and what aiortc
# resamples from most cleanly. aiortc's sender resamples/re-encodes to whatever
# codec the camera's offer negotiates (opus, or G.711 PCMU/PCMA at 8 kHz), so we
# always produce this one canonical format and let aiortc adapt.
_SAMPLE_RATE = 48000
_FRAME_SAMPLES = 960            # 20 ms @ 48 kHz
_LAYOUT = "mono"

# Matches a `"credential":"..."` (TURN password) JSON value for redaction.
_CREDENTIAL_RE = re.compile(r'("credential"\s*:\s*")[^"]*(")')

# Audio decode (av.open/demux/decode) is blocking CPU/IO work. Dispatch it to a
# dedicated single-worker pool -- NOT the loop's default ThreadPoolExecutor that
# every bridge.run_in_executor(None, ...) camera-pipeline call shares -- so a
# wedged/huge/streaming source cannot starve unrelated camera work. A
# run_in_executor future cannot truly be cancelled (the worker thread runs to
# completion), so max_workers=1 also caps how many stuck decodes can pile up: a
# burst of talk triggers queues here instead of exhausting the shared pool.
_DECODE_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="talk-decode")
# Wall-clock ceiling on a single decode; frees the awaiting talk task even if the
# worker thread stays stuck (the thread is abandoned, but the pool self-limits).
DECODE_TIMEOUT_S = 15


class FileTalkTrack(MediaStreamTrack):
    """An outbound audio track that emits 20 ms of silence per frame until
    play() is called, then streams a decoded audio file to completion, then
    returns to silence. This mirrors the app's setEnabled(false)->play->
    setEnabled(false) gating: the track exists and produces (silent) RTP as soon
    as it is negotiated, so DTLS/SRTP is warm before we actually speak.

    recv() is paced to wall-clock so we push audio at real time rather than as
    fast as aiortc will pull; without pacing the whole clip would be blasted into
    the jitter buffer in a few milliseconds.
    """
    kind = "audio"

    def __init__(self):
        super().__init__()
        self._pts = 0
        self._t0 = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._playing = False
        self._clip_done = asyncio.Event()
        self._clip_done.set()  # nothing playing yet == "done"

    def _silence_frame(self) -> "av.AudioFrame":
        frame = av.AudioFrame(format="s16", layout=_LAYOUT, samples=_FRAME_SAMPLES)
        for plane in frame.planes:
            plane.update(b"\x00" * plane.buffer_size)
        frame.sample_rate = _SAMPLE_RATE
        return frame

    async def play(self, source) -> float:
        """Decode `source` (a path, or raw bytes) into 20 ms s16/mono/48k frames
        and enqueue them, followed by an end-of-clip sentinel. Returns the clip's
        real-time duration in seconds (for the caller's play-out timeout).

        Decoding is done in a dedicated thread executor (_DECODE_EXECUTOR, not the
        shared default pool) because av.open()/demux()/decode() is blocking CPU/IO
        work that would otherwise stall the event loop (and with it the receive
        loop, keepalives, and every other camera session). It is additionally
        bounded by DECODE_TIMEOUT_S so a wedged decode frees the talk task instead
        of hanging it forever (this is the one otherwise-unbounded await inside the
        talk handshake). raises asyncio.TimeoutError on a decode that overruns.
        """
        loop = asyncio.get_running_loop()
        frames = await asyncio.wait_for(
            loop.run_in_executor(_DECODE_EXECUTOR, _decode_to_frames, source),
            timeout=DECODE_TIMEOUT_S,
        )
        self._clip_done.clear()
        self._playing = True
        for frame in frames:
            self._queue.put_nowait(frame)
        self._queue.put_nowait(None)  # end-of-clip sentinel
        duration = len(frames) * _FRAME_SAMPLES / _SAMPLE_RATE
        log.info("talkdown: queued %d frames (%.2fs) for playback", len(frames), duration)
        return duration

    async def recv(self) -> "av.AudioFrame":
        if self.readyState != "live":
            # aiortc normally stops pulling once a track is ended, but guard anyway
            # so a stray recv() after stop() raises the documented error rather
            # than producing a frame from a torn-down track.
            raise MediaStreamError

        if self._t0 is None:
            self._t0 = time.time()

        frame = None
        if self._playing:
            try:
                frame = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                frame = self._silence_frame()
            else:
                if frame is None:            # clip finished
                    self._playing = False
                    self._clip_done.set()
                    frame = self._silence_frame()
        if frame is None:
            frame = self._silence_frame()

        frame.pts = self._pts
        frame.sample_rate = _SAMPLE_RATE
        frame.time_base = fractions.Fraction(1, _SAMPLE_RATE)
        self._pts += frame.samples

        # Pace to wall clock: sleep until this frame's scheduled play-out time.
        target = self._t0 + (self._pts / _SAMPLE_RATE)
        delay = target - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        return frame

    async def wait_clip_done(self, timeout: float):
        await asyncio.wait_for(self._clip_done.wait(), timeout=timeout)


def _decode_to_frames(source) -> list:
    """Blocking: decode a file path or raw bytes into a list of 20 ms
    s16/mono/48k av.AudioFrames. Runs in an executor (see FileTalkTrack.play).
    The container is always closed, even on decode error, so no libav FD leaks.
    """
    if isinstance(source, (bytes, bytearray)):
        import io
        handle = io.BytesIO(bytes(source))
    else:
        handle = source
    container = av.open(handle)
    try:
        resampler = av.AudioResampler(
            format="s16", layout=_LAYOUT, rate=_SAMPLE_RATE, frame_size=_FRAME_SAMPLES)
        frames = []
        for frame in container.decode(audio=0):
            for chunk in resampler.resample(frame):
                frames.append(chunk)
        # Flush the resampler's tail (partial final frame padded to frame_size).
        for chunk in resampler.resample(None):
            frames.append(chunk)
        return frames
    finally:
        with contextlib.suppress(Exception):
            container.close()


class ADCTalkdown:
    """One talkdown session over one ADC signaling WebSocket.

    Usage:
        talk = ADCTalkdown(url, token, camera_auth_token)
        await talk.run("/path/to/phrase.wav")   # or run(raw_wav_bytes)

    run() connects, performs the full offer/answer + ICE handshake, waits for the
    connection to come up, plays the clip once (unmute -> audio -> mute) and tears
    everything down. It raises RuntimeError on any signaling failure, the TWA
    warning, or a handshake timeout -- and tears down regardless.
    """

    def __init__(self, signaling_server_url, signaling_server_token,
                 camera_auth_token, ice_servers_fallback=None):
        # Token is a *path segment*, not a query param (SignalingServerClientImpl
        # .startSignaling: establishConnection(url + "/" + token)).
        self._signaling_url = signaling_server_url.rstrip("/")
        self._token = signaling_server_token
        self.url = f"{self._signaling_url}/{self._token}"
        self._auth = camera_auth_token
        self._fallback_ice = ice_servers_fallback or []

        self.ws: websockets.asyncio.client.ClientConnection | None = None
        self.pc: RTCPeerConnection | None = None
        self.track = FileTalkTrack()

        # publisher_peer_id is the camera side -- the "to" on every outbound
        # message. local_peer_id is our side, learned from the offer's "to"
        # field, and used only as the "from" on ADC_CMD messages.
        self.publisher_peer_id: str | None = None
        self.local_peer_id: str = ""
        self.session_id: str | None = None

        self._recv_task: asyncio.Task | None = None
        self._connected = asyncio.Event()   # ICE connected/completed
        self._aborted = asyncio.Event()     # error / warning / transport death
        self._abort_reason = ""

        # Remote (camera) trickled candidates that beat setRemoteDescription are
        # buffered, mirroring janus_client.py, so an early candidate is never
        # dropped or applied before the remote description exists.
        self._remote_desc_set = asyncio.Event()
        self._pending_candidates: list[dict] = []

        self._closed = False

    # ---- redaction -----------------------------------------------------------
    def _redact(self, text: str) -> str:
        """Strip secrets before anything reaches a log line: the signaling token,
        the camera auth token, and any TURN credential from an iceServers blob."""
        if self._token:
            text = text.replace(self._token, "***")
        if self._auth:
            text = text.replace(self._auth, "***")
        return _CREDENTIAL_RE.sub(r"\1***\2", text)

    # ---- wire helpers --------------------------------------------------------
    async def _send(self, text: str):
        log.debug("talkdown C->S %s", self._redact(text))
        await self.ws.send(text)

    async def _send_command(self, verb: str, payload: dict):
        # Commands are "<VERB> " + json (note the single space). SDP/ICE are raw
        # JSON with no prefix and go through _send_json instead.
        await self._send(f"{verb} {json.dumps(payload)}")

    async def _send_json(self, payload: dict):
        await self._send(json.dumps(payload))

    def _mark_aborted(self, reason: str):
        if not self._aborted.is_set():
            log.warning("talkdown: aborting: %s", reason)
            self._abort_reason = reason
            self._aborted.set()

    def _ice_config(self, ice_json_list) -> RTCConfiguration:
        servers = []
        for entry in (ice_json_list or []):
            kwargs = {"urls": entry["urls"]}
            # username/credential are TURN creds and may be absent on STUN-only
            # entries; only pass them through when present (matches janus_client).
            if entry.get("username"):
                kwargs["username"] = entry["username"]
            if entry.get("credential"):
                kwargs["credential"] = entry["credential"]
            servers.append(RTCIceServer(**kwargs))
        return RTCConfiguration(iceServers=servers)

    # ---- top level -----------------------------------------------------------
    async def run(self, audio_source):
        """Connect, handshake, play `audio_source` once, and tear down. Raises on
        any failure; tears down on every path."""
        try:
            await self._connect_and_talk(audio_source)
        finally:
            await self.teardown()

    async def _connect_and_talk(self, audio_source):
        # 1. open the signaling WebSocket (text frames; no subprotocol). Bounded
        #    so a dead endpoint fails fast instead of hanging the caller.
        self.ws = await asyncio.wait_for(
            websockets.connect(self.url, max_size=None),
            timeout=WS_CONNECT_TIMEOUT_S,
        )
        log.info("talkdown: signaling WebSocket connected")
        self._recv_task = asyncio.ensure_future(self._recv_loop())

        # 2. say HELLO immediately on open; START_SESSION is only sent in reaction
        #    to the *inbound* HELLO ack (handled in _on_message).
        await self._send(f"HELLO {PROTOCOL_VERSION}")

        # 3. wait for ICE to connect, or for an abort (error/warning/transport
        #    death), whichever comes first, bounded by the handshake timeout.
        await self._await_connected_or_aborted()
        if self._aborted.is_set():
            raise RuntimeError(f"talkdown failed: {self._abort_reason}")
        log.info("talkdown: connection up; starting talk")

        # 4. talk: unmute -> feed audio to completion -> mute. The ADC_CMD is sent
        #    in addition to keeping the track enabled (the app does both).
        await self._adc_cmd("unmute_audio")
        clip_seconds = await self.track.play(audio_source)
        try:
            await self.track.wait_clip_done(timeout=clip_seconds + CLIP_TIMEOUT_MARGIN_S)
        except asyncio.TimeoutError:
            # Don't leave the mic hot: fall through to mute + teardown anyway.
            log.warning("talkdown: clip did not finish within %.1fs; muting anyway",
                        clip_seconds + CLIP_TIMEOUT_MARGIN_S)
        # Give the last RTP packets a moment to leave before we mute, so the tail
        # of the phrase isn't clipped.
        await asyncio.sleep(FLUSH_GUARD_S)
        await self._adc_cmd("mute_audio")
        log.info("talkdown: phrase played; tearing down")

    async def _await_connected_or_aborted(self):
        connected = asyncio.ensure_future(self._connected.wait())
        aborted = asyncio.ensure_future(self._aborted.wait())
        try:
            done, pending = await asyncio.wait(
                {connected, aborted},
                timeout=SESSION_CONNECT_TIMEOUT_S,
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (connected, aborted):
                if not task.done():
                    task.cancel()
        if not done:
            self._mark_aborted(f"handshake timed out after {SESSION_CONNECT_TIMEOUT_S}s")

    # ---- receive loop --------------------------------------------------------
    async def _recv_loop(self):
        """Single reader for the WebSocket: dispatches every inbound frame. Runs
        for the whole session (kept alive during playback so the camera's trickled
        ICE keeps being applied) and is cancelled by teardown()."""
        try:
            async for raw in self.ws:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                log.debug("talkdown S->C %s", self._redact(raw))
                await self._on_message(raw)
        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed as e:
            self._mark_aborted(f"signaling websocket closed: {e}")
        except Exception as e:
            log.exception("talkdown: receive loop crashed")
            self._mark_aborted(f"receive loop crashed: {e!r}")
        else:
            # The async-for ended without an exception -- the iterator only stops
            # that way on a clean close. If we hadn't connected yet, that's a
            # failure; if we had, teardown is likely already underway.
            self._mark_aborted("signaling websocket iterator ended")

    async def _on_message(self, msg: str):
        # Classification order mirrors SignalingServerClientImpl.onMessage exactly.
        if msg.startswith("keepalive-ok"):
            return  # received-only; the client never responds to it
        if msg.startswith("HELLO"):
            # Inbound HELLO ack triggers START_SESSION. Payload is exactly
            # {"auth": <camera_auth_token>} with the "START_SESSION " prefix.
            await self._send_command("START_SESSION", {"auth": self._auth})
            return
        if msg.startswith("SESSION_STARTED"):
            # Framing is `SESSION_STARTED {json}`; split on the FIRST space only.
            payload = json.loads(msg.split(" ", 1)[1])
            self.session_id = payload.get("sessionId")
            self.publisher_peer_id = payload["publisher_peer_id"]  # snake_case
            ice = payload.get("iceServers") or self._fallback_ice
            await self._create_pc(ice)
            return

        # Everything else is raw JSON, dispatched by top-level key presence in
        # this priority order: sdp -> ice -> warning -> error -> info.
        data = json.loads(msg)
        if "sdp" in data:
            await self._on_offer(data)
        elif "ice" in data:
            await self._on_remote_ice(data)
        elif "warning" in data:
            warning = data["warning"]
            if warning == TWA_UNAVAILABLE:
                # Talk channel is busy/unavailable -- hard failure, abort.
                self._mark_aborted(TWA_UNAVAILABLE)
            else:
                log.warning("talkdown: server warning: %s", warning)
        elif "error" in data:
            self._mark_aborted(f"signaling error: {data['error']}")
        elif "info" in data:
            log.info("talkdown: server info: %s", data["info"])
        else:
            log.debug("talkdown: unknown message type: %s", self._redact(msg))

    # ---- peer connection -----------------------------------------------------
    async def _create_pc(self, ice_json_list):
        # aiortc defaults: Unified Plan + MAXBUNDLE + rtcp-mux, which match
        # BasePeerConnectionClientImpl.createRtcConfig() -- no extra config needed.
        self.pc = RTCPeerConnection(configuration=self._ice_config(ice_json_list))
        # Add the local audio track BEFORE the answer is created so the answered
        # audio m-line is sendrecv = upstream into the doorbell. No video track.
        self.pc.addTrack(self.track)

        @self.pc.on("iceconnectionstatechange")
        def _on_ice_state():
            state = self.pc.iceConnectionState
            log.info("talkdown: ICE connection state -> %s", state)
            if state in ("connected", "completed"):
                self._connected.set()
            elif state in ("failed", "closed"):
                self._mark_aborted(f"ice connection state: {state}")

        @self.pc.on("connectionstatechange")
        def _on_conn_state():
            state = self.pc.connectionState
            log.debug("talkdown: peer connection state -> %s", state)
            if state in ("failed", "closed"):
                self._mark_aborted(f"peer connection state: {state}")

    async def _on_offer(self, data: dict):
        # The camera offers; we answer. Our peer id is the offer's "to".
        self.local_peer_id = data["to"]
        offer = RTCSessionDescription(sdp=data["sdp"]["sdp"], type=data["sdp"]["type"])
        await self.pc.setRemoteDescription(offer)
        self._remote_desc_set.set()
        # Apply any camera candidates that arrived before the remote description.
        for cand in self._pending_candidates:
            await self._apply_remote_candidate(cand)
        self._pending_candidates.clear()

        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)
        # aiortc does not trickle outbound: setLocalDescription gathers our
        # candidates and embeds them in the SDP, so the answer we send is already
        # complete. Wait for gathering to finish before sending (belt-and-braces;
        # setLocalDescription usually returns with gathering already complete).
        await self._await_ice_gathering_complete()

        answer_desc = self.pc.localDescription
        # Raw JSON, NO verb prefix. "from" is omitted (Moshi null-omission); "to"
        # is always the publisher.
        await self._send_json({
            "to": self.publisher_peer_id,
            "sdp": {"type": answer_desc.type, "sdp": answer_desc.sdp},
        })

    async def _await_ice_gathering_complete(self):
        if self.pc.iceGatheringState == "complete":
            return
        done = asyncio.Event()

        @self.pc.on("icegatheringstatechange")
        def _on_gather_state():
            if self.pc.iceGatheringState == "complete":
                done.set()

        try:
            await asyncio.wait_for(done.wait(), timeout=ICE_GATHERING_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Send whatever candidates we have rather than failing the whole talk;
            # a host/srflx candidate is often enough for the LAN doorbell case.
            log.warning("talkdown: ICE gathering did not complete within %.1fs; "
                        "sending answer with candidates gathered so far",
                        ICE_GATHERING_TIMEOUT_S)

    async def _on_remote_ice(self, data: dict):
        cand = data["ice"]
        if self._remote_desc_set.is_set():
            await self._apply_remote_candidate(cand)
        else:
            # Camera trickled a candidate before its own offer was processed;
            # stash it and apply once the remote description exists.
            self._pending_candidates.append(cand)

    async def _apply_remote_candidate(self, cand: dict):
        raw = cand.get("candidate")
        if not raw:
            return  # end-of-candidates / empty marker; nothing to add
        # aiortc's candidate_from_sdp treats the first token as the ICE
        # foundation, so the "candidate:" prefix (present in W3C-style candidate
        # strings) must be stripped first, otherwise the foundation is malformed.
        sdp = raw.split(":", 1)[1] if raw.startswith("candidate:") else raw
        ice_cand = candidate_from_sdp(sdp)
        ice_cand.sdpMid = cand.get("sdpMid")
        ice_cand.sdpMLineIndex = cand.get("sdpMLineIndex")
        await self.pc.addIceCandidate(ice_cand)

    async def _adc_cmd(self, cmd: str):
        # ADC_CMD carries all three fields non-null: to (publisher), adc_cmd, and
        # from (our local peer id).
        await self._send_command("ADC_CMD", {
            "to": self.publisher_peer_id,
            "adc_cmd": cmd,
            "from": self.local_peer_id,
        })

    # ---- teardown ------------------------------------------------------------
    async def teardown(self):
        """Idempotent, exception-swallowing per step: cancel the receive task,
        close the PeerConnection, close the WebSocket, and stop the audio track.
        Safe to call multiple times and from any exit path."""
        if self._closed:
            return
        self._closed = True

        if self._recv_task and not self._recv_task.done():
            self._recv_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._recv_task

        if self.pc is not None:
            with contextlib.suppress(Exception):
                await self.pc.close()

        if self.ws is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.ws.close(), timeout=WS_CLOSE_TIMEOUT_S)

        with contextlib.suppress(Exception):
            self.track.stop()

        log.info("talkdown: torn down")
