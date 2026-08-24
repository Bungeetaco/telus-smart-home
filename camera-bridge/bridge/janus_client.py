"""Janus WebSocket JSON-RPC client + aiortc wiring for TELUS's alarm.com-hosted
streaming-plugin gateway. Message sequence and field names verified byte-for-byte
against camerafeed.har's captured _webSocketMessages, and confirmed live end-to-end
against both real cameras on this account (webrtcup + decoded frames flowing).
"""
import asyncio
import contextlib
import itertools
import json
import logging
import re

import websockets
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from . import config as c

log = logging.getLogger(__name__)

# Tokens allowed out of _sdp_media_summary. An SDP is mostly identity and secret
# material, so nothing is emitted that does not look like a media kind, a port
# or an IANA codec name ("video", "9", "H264", "opus", "telephone-event"). A
# token that does not match is reported as "?" rather than printed.
_SDP_SAFE_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._\-]{0,23}\Z")


def _sdp_media_summary(sdp_text) -> str:
    """Compact, redaction-safe one-line summary of an SDP's media lines.

    Reads ONLY `m=` lines (media kind + port) and `a=rtpmap:` lines (IANA codec
    names) and returns e.g. `video(port=9,H264) audio(port=9,opus)`. Every
    other line in an SDP is identity or secret material -- `a=ice-ufrag`,
    `a=ice-pwd`, `a=fingerprint`, `a=candidate` (which carries host addresses)
    -- and is never read, let alone emitted, so the result is safe to log.

    TOTAL BY CONSTRUCTION. The only caller sits on the session-rotation path,
    where an exception raised by a diagnostic would fail an otherwise-healthy
    session and count toward camera_session.py's consecutive-failure escape
    hatch. Anything that goes wrong returns "<unparsable>"; an SDP that simply
    carries no media lines returns "<no media>" (a different fact, worth
    telling apart when reading a log).
    """
    try:
        def safe(token: str) -> str:
            return token if _SDP_SAFE_TOKEN.match(token) else "?"

        # [kind, port, [payload type, ...], {payload type: codec name}]
        sections: list[list] = []
        for raw_line in str(sdp_text).splitlines():
            line = raw_line.strip()
            if line.startswith("m="):
                # m=<media> <port> <proto> <fmt> ...
                fields = line[2:].split()
                sections.append([
                    safe(fields[0]) if fields else "?",
                    safe(fields[1]) if len(fields) > 1 else "?",
                    fields[3:],
                    {},
                ])
            elif line.startswith("a=rtpmap:") and sections:
                # a=rtpmap:<payload type> <encoding name>/<clock rate>[/<params>]
                value = line[len("a=rtpmap:"):].split()
                if len(value) >= 2:
                    sections[-1][3][value[0]] = safe(value[1].split("/")[0])
        if not sections:
            return "<no media>"
        parts = []
        for kind, port, payload_types, rtpmap in sections:
            codecs = []
            for payload_type in payload_types:
                name = rtpmap.get(payload_type)
                if name and name not in codecs:
                    codecs.append(name)
            parts.append(f"{kind}(port={port}" + "".join("," + n for n in codecs) + ")")
        return " ".join(parts)
    except Exception:
        return "<unparsable>"


def _create_reply_error(reply: dict) -> str | None:
    """Redacted description of a failed mountpoint-create reply, or None if it
    succeeded.

    A create can be rejected in two different shapes and only one of them was
    ever recognised here:
      * transport level -- {"janus": "error", "error": {"code": .., "reason": ..}}
      * PLUGIN level    -- {"janus": "success", "plugindata": {"data":
                            {"error_code": .., "error": ".."}}}, which is what
                            the streaming plugin returns when it accepts the
                            request but refuses to build the mountpoint. The
                            original code only saw that as a KeyError on
                            ["stream"]["id"] further down.
    Used ONLY by the JANUS_REQUEST_AUDIO fallback below, so the video-only path
    behaves exactly as it always has.

    The reason text comes from the server, and Janus error strings can quote
    request fields, so any token carrying a URL scheme is replaced before it is
    handed to the log -- media_uri is the relay proxyUrl and must never appear.
    """
    try:
        if reply.get("janus") == "error":
            err = reply.get("error") or {}
            code, reason = err.get("code"), err.get("reason")
        else:
            data = (reply.get("plugindata") or {}).get("data") or {}
            code, reason = data.get("error_code"), data.get("error")
            if code is None and reason is None:
                return None
        scrubbed = " ".join(
            "<url>" if "://" in tok else tok for tok in str(reason).split()
        )[:200]
        return f"error_code={code} reason={scrubbed!r}"
    except Exception:
        return "unrecognised error reply"


class JanusCameraSession:
    """One Janus streaming-plugin session for one camera. Yields decoded video frames."""

    def __init__(self, camera: dict, video_source: dict):
        self.camera = camera
        self.vs = video_source
        self._txn = itertools.count()
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        self._session_id = None
        self._handle_id = None
        self.pc: RTCPeerConnection | None = None
        self._remote_desc_set = asyncio.Event()
        self._pending_candidates: list[dict] = []
        self._webrtcup = asyncio.Event()
        self._track_queue: asyncio.Queue = asyncio.Queue(maxsize=4)
        self._dropped_frames = 0
        # Set on any sign this session has died (track ended, ICE/connection failed,
        # websocket closed) so frames() can stop blocking forever instead of hanging.
        self._dead = asyncio.Event()
        self._dead_reason = ""
        # One asyncio.Queue per outstanding transaction id. A "watch" request gets
        # an "ack" followed by a separate "event" (carrying jsep) on the SAME
        # transaction id -- a single-future waiter would drop one of them, so we
        # queue instead and let callers drain until they see what they need.
        self._queues: dict[str, asyncio.Queue] = {}
        self._recv_task: asyncio.Task | None = None
        self._keepalive_task: asyncio.Task | None = None
        self._closed = False

    def _next_txn(self) -> str:
        txn = str(next(self._txn))
        self._queues[txn] = asyncio.Queue()
        return txn

    async def _send(self, msg: dict):
        msg["token"] = self.vs["janus_token"]
        await self._ws.send(json.dumps(msg))

    async def _wait_for(self, txn: str) -> dict:
        return await asyncio.wait_for(self._queues[txn].get(), timeout=15)

    def _mark_dead(self, reason: str):
        if not self._dead.is_set():
            log.warning("%s: session marked dead: %s", self.camera["id"], reason)
            self._dead_reason = reason
            self._dead.set()

    async def connect_and_watch(self):
        ice_servers = []
        for s in self.vs["ice_servers"]:
            kwargs = {"urls": s["urls"]}
            if "username" in s:
                kwargs["username"] = s["username"]
            if "credential" in s:
                kwargs["credential"] = s["credential"]
            ice_servers.append(RTCIceServer(**kwargs))
        self.pc = RTCPeerConnection(configuration=RTCConfiguration(iceServers=ice_servers))

        @self.pc.on("track")
        def on_track(track):
            if track.kind == "video":
                asyncio.ensure_future(self._pump_track(track))
            else:
                # A track this bridge does not consume must still be DRAINED, not
                # merely ignored. aiortc 1.15 auto-creates a recvonly transceiver
                # for every m-line the remote offers and __connect() starts its
                # receiver unconditionally: a per-track decoder thread spawns and
                # decoder_worker pushes every decoded frame into
                # RemoteStreamTrack._queue, an UNBOUNDED asyncio.Queue. Silently
                # dropping the track (what this branch used to do implicitly) left
                # that queue growing for the entire ~165s life of every session --
                # a per-rotation memory sawtooth, once per camera, forever. Nothing
                # here consumes audio; the JANUS_REQUEST_AUDIO experiment only asks
                # whether the upstream WILL serve a downstream audio track.
                #
                # track.stop() is NOT the release, despite reading like it:
                # aiortc's MediaStreamTrack.stop() only sets an ended flag, emits
                # "ended" and drops its listeners, and NOTHING inside aiortc
                # subscribes to that event for a RemoteStreamTrack -- the receiver
                # keeps its decoder thread and keeps enqueueing, so the queue grows
                # exactly as before while recv() now refuses to drain it. Stopping
                # the receiver instead is no good either: this callback fires from
                # inside setRemoteDescription, BEFORE __connect() has started the
                # receiver, so RTCRtpReceiver.stop() short-circuits on
                # `if self.__started` and the receiver is started a moment later
                # anyway. Draining is what actually bounds the memory; the decoder
                # thread itself is released by pc.close() at the end of the session
                # (session.close() -> RTCPeerConnection.close() -> transceiver.stop()).
                log.info("%s: upstream offered a %r track this bridge does not "
                         "consume; draining it (audio consumption is not implemented)",
                         self.camera["id"], track.kind)
                asyncio.ensure_future(self._drain_track(track))

        @self.pc.on("iceconnectionstatechange")
        def on_ice_state_change():
            state = self.pc.iceConnectionState
            log.debug("%s: ICE connection state -> %s", self.camera["id"], state)
            if state in ("failed", "closed", "disconnected"):
                self._mark_dead(f"ice connection state: {state}")

        @self.pc.on("connectionstatechange")
        def on_connection_state_change():
            state = self.pc.connectionState
            log.debug("%s: peer connection state -> %s", self.camera["id"], state)
            if state in ("failed", "closed", "disconnected"):
                self._mark_dead(f"peer connection state: {state}")

        self._ws = await websockets.connect(self.vs["janus_gateway_url"], subprotocols=["janus-protocol"])
        self._recv_task = asyncio.ensure_future(self._recv_loop())

        # 1. create session
        txn = self._next_txn()
        await self._send({"janus": "create", "request": "create", "transaction": txn})
        created_session = await self._wait_for(txn)
        if created_session.get("janus") == "error":
            raise RuntimeError(f"{self.camera['id']}: session create failed: {created_session}")
        self._session_id = created_session["data"]["id"]

        # 2. attach streaming plugin
        txn = self._next_txn()
        await self._send({
            "session_id": self._session_id, "janus": "attach", "transaction": txn,
            "plugin": "janus.plugin.streaming", "force-bundle": True, "force-rtcp-mux": True,
            "opaque_id": f"webrtc-player_{self.camera['id']}",
        })
        attached = await self._wait_for(txn)
        if attached.get("janus") == "error":
            raise RuntimeError(f"{self.camera['id']}: attach failed: {attached}")
        self._handle_id = attached["data"]["id"]

        # 3. create rtp mountpoint from the relay's proxyUrl
        video_only_body = {
            "request": "create", "is_private": True, "type": "rtp",
            "media_uri": self.vs["proxy_url"],
            "add_sps_pps": False, "is_virtual": False, "streaming_type": "Live",
            "timeout_seconds": 180, "max_timeout_seconds": 900,
            "video": True, "videoport": 0, "videopt": 126,
            "videortpmap": "H264/90000",
            "videofmtp": "profile-level-id=42e01f;packetization-mode=1",
            "name": f"{self.camera['mac_address']}_Live",
            "model": self.camera["model"],
            "firmware": self.camera["firmware"],
        }
        if c.JANUS_REQUEST_AUDIO:
            # EXPERIMENT (off by default -- see config.JANUS_REQUEST_AUDIO). The
            # audio keys mirror the video ones exactly in shape: an enable flag, a
            # port of 0 (let Janus pick), a dynamic payload type and an rtpmap.
            #
            # Opus at payload type 111 is the choice most likely to be ACCEPTED:
            # 111 is the de-facto WebRTC dynamic PT for Opus (what Chrome and
            # Firefox both offer) and cannot collide with the video 126 above, and
            # Opus/48000/2 is the one audio codec every WebRTC endpoint is required
            # to implement -- alarm.com's own player is a browser, so it is what
            # this gateway is certain to have a decoder path for. Anything more
            # exotic would make a rejection ambiguous between "no audio upstream"
            # and "that codec".
            audio_body = {
                **video_only_body,
                "audio": True, "audioport": 0, "audiopt": 111,
                "audiortpmap": "opus/48000/2",
            }
        else:
            audio_body = video_only_body

        created = await self._create_mountpoint(audio_body)
        if c.JANUS_REQUEST_AUDIO:
            # FAIL-SAFE. A rejected experiment must cost nothing: it may not raise,
            # so it never counts a consecutive failure, never reaches
            # camera_session.py's MAX_CONSECUTIVE_SESSION_FAILURES escape hatch and
            # never restart-loops the container. On any error reply -- transport or
            # plugin level -- retry the create ONCE with the byte-identical
            # video-only body this bridge has always sent, and carry on as normal.
            # Strictly local to this step: nothing else in connect_and_watch knows
            # the experiment happened.
            audio_error = _create_reply_error(created)
            if audio_error is not None:
                log.warning("%s: mountpoint create WITH AUDIO was rejected (%s) -- "
                            "retrying video-only; the upstream will not serve a "
                            "downstream audio track for this camera",
                            self.camera["id"], audio_error)
                created = await self._create_mountpoint(video_only_body)
        if created.get("janus") == "error":
            raise RuntimeError(f"{self.camera['id']}: mountpoint create failed: {created}")
        mountpoint_id = created["plugindata"]["data"]["stream"]["id"]

        # 4. watch it -- an ack arrives first, then a separate "event" carrying the
        #    jsep offer; keep draining this transaction's queue until we see jsep.
        txn = self._next_txn()
        await self._send({
            "session_id": self._session_id, "janus": "message", "transaction": txn,
            "handle_id": self._handle_id, "body": {"request": "watch", "id": mountpoint_id},
        })
        watch_event = await self._wait_for(txn)
        while "jsep" not in watch_event:
            # An error reply carries no jsep and no further messages follow it, so
            # without this check the loop just blocks until _wait_for's 15s timeout --
            # turning an immediate, self-describing rejection into a slow generic
            # timeout that also delays the reconnect by those 15 seconds.
            if watch_event.get("janus") == "error":
                raise RuntimeError(f"{self.camera['id']}: watch failed: {watch_event}")
            watch_event = await self._wait_for(txn)
        offer_sdp = watch_event["jsep"]["sdp"]
        # DIAGNOSTIC: what the gateway actually offers us, media-wise. This is the
        # answer to "will Janus serve a downstream AUDIO track" -- an `audio(...)`
        # entry here means it does; video only means it does not, regardless of
        # what was asked for in the create above. Summary only: the raw SDP carries
        # a=ice-ufrag / a=ice-pwd / a=fingerprint / a=candidate and is never logged.
        # One line per session start (~1 per SESSION_MAX_LIFETIME_S per camera).
        log.info("%s: janus offer media: %s", self.camera["id"],
                 _sdp_media_summary(offer_sdp))

        # 5. trickled candidates start arriving asynchronously as soon as the offer
        #    lands (handled by _recv_loop) -- apply any that beat us here, then any
        #    further ones as they arrive (also handled by _recv_loop once
        #    _remote_desc_set is set).
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
        self._remote_desc_set.set()
        for cand in self._pending_candidates:
            await self._apply_remote_candidate(cand)
        self._pending_candidates.clear()

        # 6. build + send our answer. aiortc gathers its own ICE candidates and embeds
        #    them directly in the SDP by the time setLocalDescription resolves (it
        #    doesn't trickle by default), so Janus gets a fully-formed answer here --
        #    confirmed sufficient in live testing (no separate outbound trickle needed).
        answer = await self.pc.createAnswer()
        await self.pc.setLocalDescription(answer)

        txn = self._next_txn()
        await self._send({
            "session_id": self._session_id, "janus": "message", "transaction": txn,
            "handle_id": self._handle_id, "body": {"request": "start"},
            "jsep": {"type": self.pc.localDescription.type, "sdp": self.pc.localDescription.sdp},
        })
        start_result = await self._wait_for(txn)
        if start_result.get("janus") == "error":
            raise RuntimeError(f"{self.camera['id']}: start failed: {start_result}")

        try:
            await asyncio.wait_for(self._webrtcup.wait(), timeout=c.WEBRTCUP_TIMEOUT_S)
        except asyncio.TimeoutError:
            raise RuntimeError(f"{self.camera['id']}: webrtcup not received within "
                               f"{c.WEBRTCUP_TIMEOUT_S}s")
        log.info("%s: webrtcup", self.camera["id"])

        self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def _create_mountpoint(self, body: dict) -> dict:
        """One streaming-plugin `create` request. Extracted verbatim from step 3
        so the JANUS_REQUEST_AUDIO fallback can re-send a different body without
        restructuring connect_and_watch; the request shape and the 15s
        _wait_for are unchanged."""
        txn = self._next_txn()
        await self._send({
            "session_id": self._session_id, "janus": "message", "transaction": txn,
            "handle_id": self._handle_id, "body": body,
        })
        return await self._wait_for(txn)

    async def _recv_loop(self):
        try:
            async for raw in self._ws:
                data = json.loads(raw)
                txn = data.get("transaction")
                kind = data.get("janus")
                if kind == "trickle":
                    cand = data["candidate"]
                    if self._remote_desc_set.is_set():
                        await self._apply_remote_candidate(cand)
                    else:
                        self._pending_candidates.append(cand)
                elif kind == "webrtcup":
                    self._webrtcup.set()
                elif kind in ("success", "ack", "event", "error") and txn in self._queues:
                    self._queues[txn].put_nowait(data)
        except asyncio.CancelledError:
            pass
        except websockets.ConnectionClosed as e:
            self._mark_dead(f"websocket closed: {e}")
        except Exception:
            log.exception("%s: janus recv loop crashed", self.camera["id"])
            self._mark_dead("recv loop crashed")
        else:
            # The `async for` ended without an exception -- the websocket's own
            # iterator only stops that way when the connection closed cleanly.
            self._mark_dead("websocket iterator ended")

    async def _apply_remote_candidate(self, cand: dict):
        if "completed" in cand:
            return  # end-of-candidates marker; no explicit aiortc call needed
        ice_cand = candidate_from_sdp(cand["candidate"])
        ice_cand.sdpMid = cand.get("sdpMid")
        ice_cand.sdpMLineIndex = cand.get("sdpMLineIndex")
        await self.pc.addIceCandidate(ice_cand)

    async def _keepalive_loop(self):
        while True:
            await asyncio.sleep(c.JANUS_KEEPALIVE_INTERVAL_S)
            txn = self._next_txn()
            try:
                await self._send({"janus": "keepalive", "session_id": self._session_id,
                                   "transaction": txn})
            except asyncio.CancelledError:
                raise
            except websockets.ConnectionClosed:
                # Routine, not exceptional: the server force-closes every session
                # at ~180s and keepalives run every 25s, so a send into a
                # just-dead websocket happens on ordinary rotations. The recv
                # loop owns marking the session dead -- this loop just stops.
                # Raising out of here instead left the task finished-with-an-
                # exception; close() only cancel()s it (a no-op on a finished
                # task), so nothing ever retrieved the exception and asyncio
                # logged "Task exception was never retrieved" tracebacks at GC
                # time as recurring error-level noise.
                self._queues.pop(txn, None)
                return
            except Exception:
                log.warning("%s: keepalive send failed; stopping keepalives",
                            self.camera["id"], exc_info=True)
                self._queues.pop(txn, None)
                return
            # keepalive ack isn't otherwise consumed; drop its queue entry after a beat
            # to avoid leaking memory over a long-lived session.
            asyncio.ensure_future(self._drop_queue_later(txn))

    async def _drop_queue_later(self, txn: str, delay: float = 5.0):
        await asyncio.sleep(delay)
        self._queues.pop(txn, None)

    async def _pump_track(self, track):
        while True:
            try:
                frame = await track.recv()
            except Exception as e:
                self._mark_dead(f"track ended: {e!r}")
                return
            try:
                self._track_queue.put_nowait(frame)
            except asyncio.QueueFull:
                # Drop the oldest queued frame rather than blocking the decoder --
                # a live view cares about latency, not completeness. This previously
                # had zero observability -- if the consumer (camera_session.py's
                # sink.write_frame, one queue.get() per frame) ever falls behind
                # (e.g. ffmpeg/VAAPI encode contention), frames vanish here with no
                # trace, which is exactly what a "dropped frames" complaint would look
                # like from the outside. Logging (throttled, since a real backlog can
                # drop many frames a second) at least makes it visible.
                self._dropped_frames += 1
                if self._dropped_frames == 1 or self._dropped_frames % 150 == 0:
                    log.warning("%s: dropped %d frame(s) so far -- consumer falling "
                                "behind (queue maxsize=%d)", self.camera["id"],
                                self._dropped_frames, self._track_queue.maxsize)
                with contextlib.suppress(asyncio.QueueEmpty):
                    self._track_queue.get_nowait()
                self._track_queue.put_nowait(frame)

    async def _drain_track(self, track):
        """Discard frames from an offered track nothing consumes, so aiortc's
        unbounded RemoteStreamTrack._queue cannot accumulate them for the life of
        the session (see on_track for why stopping the track does not do this).

        Deliberately NOT _pump_track: it must not enqueue anything, and it must
        not _mark_dead() when it ends -- the video track alone decides whether
        this session is alive, and an audio track ending (or never carrying a
        packet at all) is not a session failure. It exits by itself when
        pc.close() stops the decoder, i.e. at every rotation. Nothing may escape
        into the ensure_future that started it, or asyncio logs a
        never-retrieved task exception at GC time.
        """
        kind = getattr(track, "kind", "?")
        drained = 0
        try:
            while True:
                await track.recv()
                drained += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.debug("%s: %s track drain ended after %d frame(s): %r",
                      self.camera["id"], kind, drained, e)

    @property
    def dropped_frames(self) -> int:
        """Frames _pump_track dropped because the consumer fell behind.

        Per-SESSION, so it dies with each ~165s rotation; camera_session's loop
        reads it as each session ends and folds it into the per-camera lifetime
        total that /control/status serves -- otherwise the throttled log lines
        above were the only trace the drops ever left."""
        return self._dropped_frames

    async def frames(self):
        """Yield decoded frames until the session dies (track end, ICE/connection
        failure, websocket close) or the caller stops iterating. Never blocks
        forever: races the next frame against the death signal every iteration.
        """
        get_task = None
        dead_task = None
        try:
            while True:
                if self._dead.is_set():
                    raise RuntimeError(f"{self.camera['id']}: session dead: {self._dead_reason}")
                get_task = asyncio.ensure_future(self._track_queue.get())
                dead_task = asyncio.ensure_future(self._dead.wait())
                done, pending = await asyncio.wait(
                    {get_task, dead_task}, return_when=asyncio.FIRST_COMPLETED
                )
                if get_task in done:
                    dead_task.cancel()
                    yield get_task.result()
                else:
                    get_task.cancel()
                    raise RuntimeError(f"{self.camera['id']}: session dead: {self._dead_reason}")
        finally:
            for t in (get_task, dead_task):
                if t is not None and not t.done():
                    t.cancel()

    async def close(self):
        self._closed = True
        if self._keepalive_task:
            self._keepalive_task.cancel()
        if self._recv_task:
            self._recv_task.cancel()
        try:
            if self.pc:
                await self.pc.close()
        finally:
            # Unconditional, same ordering as before (pc first, then ws). aiortc's
            # pc.close() is a known-hangable call under certain ICE states (see
            # lifecycle.py), and every camera_session call site bounds this whole
            # coroutine with wait_for(SESSION_CLOSE_TIMEOUT_S) -- so pc.close() can
            # raise, or the wait_for's CancelledError can land mid-close. A plain
            # sequential close abandoned the websocket to alarm.com's Janus gateway
            # open in both cases, and the rotation loop opens a fresh session every
            # ~165s, so a wedged camera accumulated one leaked socket per rotation.
            #
            # The ws close is guarded so its own failure cannot mask pc.close()'s
            # exception; a CancelledError (the enclosing wait_for's) is re-raised
            # after the close attempt, never swallowed -- for the pc.close() one,
            # by this finally finishing normally. websockets bounds close() with
            # its own close_timeout, so this cleanup cannot itself hang the caller.
            if self._ws:
                try:
                    await self._ws.close()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.debug("%s: websocket close failed during session close",
                              self.camera["id"], exc_info=True)
