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

import websockets
from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, RTCSessionDescription
from aiortc.sdp import candidate_from_sdp

from . import config as c

log = logging.getLogger(__name__)


class JanusCameraSession:
    """One Janus streaming-plugin session for one camera. Yields decoded video frames."""

    def __init__(self, camera: dict, video_source: dict):
        self.camera = camera
        self.vs = video_source
        self._txn = itertools.count()
        self._ws: websockets.WebSocketClientProtocol | None = None
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
        txn = self._next_txn()
        await self._send({
            "session_id": self._session_id, "janus": "message", "transaction": txn,
            "handle_id": self._handle_id,
            "body": {
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
            },
        })
        created = await self._wait_for(txn)
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
            await self._send({"janus": "keepalive", "session_id": self._session_id,
                               "transaction": txn})
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
        if self.pc:
            await self.pc.close()
        if self._ws:
            await self._ws.close()
