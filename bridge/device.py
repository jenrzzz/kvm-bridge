"""A JetKVM, over the same WebRTC the device's own web UI uses.

HTTP on the device is almost nothing: `POST /auth/login-local` sets an
`authToken` cookie, and everything else is WebRTC. A websocket to
`/webrtc/signaling/client` carries the offer and answer; the peer connection
has a recvonly H.264 video track and a data channel named `rpc` that carries
JSON-RPC 2.0, calls from us and notifications from the device.

One connection at a time: a new one takes the device over, and the old one
hears `otherSessionConnected`. That is how a person walking up to the KVM in
a browser ends an agent's session, and `lost` says so.

`Device` is the interface the session layer uses; `JetKVM` is the real one,
and tests use `bridge.fake.FakeDevice`.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
from typing import Any, Protocol

import aiohttp
from PIL import Image

log = logging.getLogger("bridge.device")


class DeviceError(Exception):
    """The device could not be reached, refused us, or failed a call."""


class Device(Protocol):
    lost: str | None
    version: str | None

    async def connect(self) -> None: ...
    async def close(self) -> None: ...
    async def call(self, method: str, params: dict | None = None) -> Any: ...
    async def frame_after(self, t: float, timeout: float) -> Image.Image | None: ...


class JetKVM:
    """One WebRTC connection to a JetKVM, held for the length of a session."""

    RPC_TIMEOUT = 5.0
    CONNECT_TIMEOUT = 15.0

    def __init__(self, url: str, password: str | None):
        self.url = url.rstrip("/")
        self.password = password or None
        self.lost: str | None = None
        self.version: str | None = None
        self._http: aiohttp.ClientSession | None = None
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._pc = None
        self._rpc = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task] = []
        self._frame = None
        self._frame_at = 0.0
        self._new_frame = asyncio.Condition()

    # --- HTTP, without a connection -------------------------------------

    async def http_status(self) -> dict:
        """What the device says over HTTP: up, and whether our password works. No WebRTC, so nobody is cut off."""
        async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=8)) as http:
            try:
                async with http.get(f"{self.url}/device/status") as r:
                    status = await r.json(content_type=None)
                await self._login(http)
                async with http.get(f"{self.url}/device") as r:
                    if r.status == 200:
                        info = await r.json(content_type=None)
                    elif r.status == 401 and not self.password:
                        info = {"error": "the device wants a password: set JETKVM_PASSWORD"}
                    else:
                        info = {"error": f"HTTP {r.status}"}
            except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
                raise DeviceError(f"the JetKVM is unreachable at {self.url}: {e.__class__.__name__}: {e}") from e
        return {"status": status, "info": info}

    async def _login(self, http: aiohttp.ClientSession) -> None:
        if not self.password:
            return
        async with http.post(f"{self.url}/auth/login-local", json={"password": self.password}) as r:
            if r.status in (401, 403):
                raise DeviceError("the JetKVM refused the password (JETKVM_PASSWORD)")
            if r.status >= 300:
                raise DeviceError(f"the JetKVM's login answered HTTP {r.status}")

    # --- the connection ---------------------------------------------------

    async def connect(self) -> None:
        from aiortc import RTCPeerConnection

        self._http = aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True), timeout=aiohttp.ClientTimeout(total=None, connect=8))
        try:
            await self._login(self._http)
            ws_url = self.url.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
            self._ws = await self._http.ws_connect(f"{ws_url}/webrtc/signaling/client", heartbeat=10)
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            await self.close()
            raise DeviceError(f"the JetKVM is unreachable at {self.url}: {e.__class__.__name__}: {e}") from e

        pc = self._pc = RTCPeerConnection()
        pc.addTransceiver("video", direction="recvonly")
        pc.addTransceiver("audio", direction="recvonly")
        rpc = self._rpc = pc.createDataChannel("rpc")
        rpc.on("message", self._on_message)

        @pc.on("track")
        def on_track(track):
            if track.kind == "video":
                self._tasks.append(asyncio.ensure_future(self._read_frames(track)))

        @pc.on("connectionstatechange")
        async def on_state():
            if pc.connectionState in ("failed", "closed") and not self.lost:
                self.lost = f"the WebRTC connection to the KVM {pc.connectionState}"

        await pc.setLocalDescription(await pc.createOffer())
        local = pc.localDescription
        sd = base64.b64encode(json.dumps({"type": local.type, "sdp": local.sdp}).encode()).decode()
        await self._ws.send_str(json.dumps({"type": "offer", "data": {"sd": sd}}))
        self._tasks.append(asyncio.ensure_future(self._signaling()))

        deadline = time.monotonic() + self.CONNECT_TIMEOUT
        while rpc.readyState != "open":
            if time.monotonic() > deadline or self.lost:
                reason = self.lost or "the rpc channel did not open"
                await self.close()
                raise DeviceError(f"could not connect to the JetKVM: {reason}")
            await asyncio.sleep(0.05)

    async def _signaling(self) -> None:
        from aiortc import RTCSessionDescription
        from aiortc.sdp import candidate_from_sdp

        try:
            async for msg in self._ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                data = json.loads(msg.data)
                kind = data.get("type")
                if kind == "answer":
                    answer = json.loads(base64.b64decode(data["data"]))
                    await self._pc.setRemoteDescription(RTCSessionDescription(answer["sdp"], answer["type"]))
                elif kind == "new-ice-candidate":
                    c = data.get("data") or {}
                    if c.get("candidate"):
                        candidate = candidate_from_sdp(c["candidate"].split(":", 1)[1])
                        candidate.sdpMid, candidate.sdpMLineIndex = c.get("sdpMid"), c.get("sdpMLineIndex")
                        await self._pc.addIceCandidate(candidate)
                elif kind == "device-metadata":
                    self.version = (data.get("data") or {}).get("deviceVersion")
        except asyncio.CancelledError:
            raise
        except Exception as e:  # signaling is done once connected; note it and move on
            log.info("signaling ended: %s", e)

    async def _read_frames(self, track) -> None:
        try:
            while True:
                frame = await track.recv()
                if frame.width:
                    async with self._new_frame:
                        self._frame, self._frame_at = frame, time.monotonic()
                        self._new_frame.notify_all()
        except asyncio.CancelledError:
            raise
        except Exception as e:  # MediaStreamError when the track ends
            log.info("video ended: %s", e)

    def _on_message(self, message) -> None:
        try:
            data = json.loads(message)
        except (TypeError, ValueError):
            return
        if "id" in data and data["id"] in self._pending:
            future = self._pending.pop(data["id"])
            if not future.done():
                future.set_result(data)
        elif data.get("method") == "otherSessionConnected":
            self.lost = "another session connected to the KVM"

    async def call(self, method: str, params: dict | None = None) -> Any:
        if self.lost:
            raise DeviceError(self.lost)
        if not self._rpc or self._rpc.readyState != "open":
            raise DeviceError("not connected to the JetKVM")
        self._next_id += 1
        call_id = self._next_id
        future = asyncio.get_running_loop().create_future()
        self._pending[call_id] = future
        self._rpc.send(json.dumps({"jsonrpc": "2.0", "method": method, "params": params or {}, "id": call_id}))
        try:
            answer = await asyncio.wait_for(future, self.RPC_TIMEOUT)
        except asyncio.TimeoutError as e:
            self._pending.pop(call_id, None)
            raise DeviceError(f"the JetKVM did not answer {method}") from e
        if "error" in answer and answer["error"]:
            error = answer["error"]
            raise DeviceError(f"the JetKVM refused {method}: {error.get('message', error) if isinstance(error, dict) else error}")
        return answer.get("result")

    async def frame_after(self, t: float, timeout: float) -> Image.Image | None:
        """The first frame decoded after monotonic time `t`, or the newest one at the timeout; None if there's none."""
        deadline = time.monotonic() + timeout
        async with self._new_frame:
            while self._frame_at <= t:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(self._new_frame.wait(), remaining)
                except asyncio.TimeoutError:
                    break
            frame = self._frame
        return frame.to_image() if frame is not None else None

    async def close(self) -> None:
        for task in self._tasks:
            task.cancel()
        self._tasks = []
        for future in self._pending.values():
            if not future.done():
                future.cancel()
        self._pending = {}
        if self._pc is not None:
            await self._pc.close()
            self._pc = None
        if self._ws is not None:
            await self._ws.close()
            self._ws = None
        if self._http is not None:
            await self._http.close()
            self._http = None
