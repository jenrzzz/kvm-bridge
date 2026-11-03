"""Sessions: one at a time per device, each holding the WebRTC connection for
its length. The bridge connects when a session opens and drops the
connection when it closes, so a person's own KVM tab is interrupted only
while an agent is actually working.

A session ends when it's closed, after `ttl` seconds without a step, after
MAX_LIFE regardless, or when someone else connects to the KVM (`lost`). The
next call on an ended session answers 410 with the reason.
"""

from __future__ import annotations

import asyncio
import base64
import io
import secrets
import time
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from typing import Callable

from PIL import Image

from . import actions
from .audit import Audit
from .device import Device, DeviceError

MIN_TTL, MAX_TTL, DEFAULT_TTL = 30, 900, 300
MAX_LIFE = 30 * 60
DEFAULT_SCALE, MIN_SCALE, MAX_SCALE = 1280, 320, 3840
FRAME_WAIT = 3.0  # how long to wait for a fresh frame before answering with the newest one
ENDED_KEPT = 200  # ended sessions remembered, to answer 410 rather than 404


class Busy(Exception):
    """A session is already open on the device."""


class Ended(Exception):
    """The session is over: closed, expired, or lost."""


class Unknown(Exception):
    """No such session."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(t: datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def check_ttl(value) -> int:
    if value is None:
        return DEFAULT_TTL
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise actions.Refused("ttl is a number of seconds")
    return int(min(max(value, MIN_TTL), MAX_TTL))


def check_scale(value) -> int | None:
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise actions.Refused(f"scale is a width in pixels, {MIN_SCALE} to {MAX_SCALE}") from None
    if isinstance(value, bool) or not MIN_SCALE <= number <= MAX_SCALE:
        raise actions.Refused(f"scale is a width in pixels, {MIN_SCALE} to {MAX_SCALE}")
    return number


class Session:
    def __init__(self, device: Device, ttl: int, scale: int, key: str, settle: float):
        self.id = secrets.token_urlsafe(9)
        self.device = device
        self.ttl = ttl
        self.scale = scale
        self.key = key
        self.settle = settle
        self.opened = _now()
        self.last_step = self.opened
        self.steps = 0
        self.frame = actions.Frame(1, 1)  # the last frame served, which coordinates refer to
        self.lock = asyncio.Lock()
        self.ended: str | None = None

    @property
    def expires_at(self) -> datetime:
        return min(self.last_step + timedelta(seconds=self.ttl), self.opened + timedelta(seconds=MAX_LIFE))

    def overdue(self, now: datetime | None = None) -> str | None:
        now = now or _now()
        if now >= self.opened + timedelta(seconds=MAX_LIFE):
            return f"session expired: reached the {MAX_LIFE // 60}-minute limit"
        if now >= self.expires_at:
            return f"session expired: no step for {self.ttl} seconds"
        return None

    async def call(self, method: str, params: dict | None = None):
        # The allowlist, in code: nothing but input and read-only state ever reaches the device.
        if method not in actions.INPUT_METHODS and method not in actions.READ_METHODS:
            raise PermissionError(f"{method} is not something the bridge sends")
        return await self.device.call(method, params or {})

    async def video(self) -> dict | None:
        try:
            return await self.call("getVideoState")
        except DeviceError:
            return None

    async def state(self, after: float | None = None, scale: int | None = None) -> dict:
        """The session's state with a fresh frame: one decoded after `after` (monotonic), at `scale` wide."""
        if scale:
            self.scale = scale
        image = await self.device.frame_after(after if after is not None else time.monotonic(), FRAME_WAIT)
        screen = None
        if image is not None:
            screen = encode(image, self.scale)
            self.frame = actions.Frame(screen["width"], screen["height"])
        return {"id": self.id, "expires_at": _iso(self.expires_at), "video": await self.video(), "screen": screen}


def encode(image: Image.Image, scale: int) -> dict:
    """A frame as base64 PNG, `scale` pixels wide (never wider than the source)."""
    width = min(scale, image.width)
    if width != image.width:
        image = image.resize((width, round(image.height * width / image.width)), Image.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="PNG", compress_level=6)
    return {"data": base64.b64encode(buffer.getvalue()).decode(), "mime_type": "image/png", "width": image.width, "height": image.height}


class Bridge:
    """The device's sessions: at most one open, and a memory of the ended ones."""

    def __init__(self, device_factory: Callable[[], Device], audit: Audit | None = None, settle: float = 0.3):
        self.device_factory = device_factory
        self.audit = audit or Audit(None)
        self.settle = settle
        self.current: Session | None = None
        self.ended: OrderedDict[str, str] = OrderedDict()
        self._opening = asyncio.Lock()

    async def open(self, key: str, ttl=None, scale=None) -> dict:
        ttl, scale = check_ttl(ttl), check_scale(scale) or DEFAULT_SCALE
        async with self._opening:
            await self.reap()
            if self.current is not None:
                raise Busy("the KVM is in use by another session")
            device = self.device_factory()
            await device.connect()  # raises DeviceError
            session = Session(device, ttl, scale, key, self.settle)
            self.current = session
        self.audit.write("open", session=session.id, key=key, ttl=ttl, scale=scale)
        try:
            return await session.state()
        except Exception:
            await self.end(session, "the first frame failed")
            raise

    def get(self, session_id: str) -> Session:
        session = self.current
        if session is not None and session.id == session_id:
            reason = session.device.lost or session.overdue()
            if reason:
                raise Ended(reason)
            return session
        if session_id in self.ended:
            raise Ended(self.ended[session_id])
        raise Unknown("no such session")

    async def act(self, session_id: str, body) -> dict:
        session = await self._live(session_id)
        async with session.lock:
            action = actions.check(body, session.frame)
            started = time.monotonic()
            try:
                await actions.Driver(session.call).run(action, session.frame)
            except DeviceError:
                if session.device.lost:
                    await self.end(session, session.device.lost)
                    raise Ended(session.device.lost) from None
                raise
            session.steps += 1
            session.last_step = _now()
            self.audit.write("act", session=session.id, key=session.key, action=_logged(body))
            await asyncio.sleep(session.settle)
            return await session.state(after=started + session.settle)

    async def screen(self, session_id: str, scale=None) -> dict:
        session = await self._live(session_id)
        async with session.lock:
            return await session.state(scale=check_scale(scale))

    async def close(self, session_id: str) -> None:
        session = self.current
        if session is None or session.id != session_id:
            if session_id in self.ended:
                return
            raise Unknown("no such session")
        await self.end(session, "closed by the caller")

    async def _live(self, session_id: str) -> Session:
        try:
            return self.get(session_id)
        except Ended as e:
            if self.current is not None and self.current.id == session_id:
                await self.end(self.current, str(e))
            raise

    async def end(self, session: Session, reason: str) -> None:
        if session.ended:
            return
        session.ended = reason
        if self.current is session:
            self.current = None
        self.ended[session.id] = reason
        while len(self.ended) > ENDED_KEPT:
            self.ended.popitem(last=False)
        self.audit.write("close", session=session.id, key=session.key, steps=session.steps, reason=reason)
        try:
            await session.device.close()
        except Exception:
            pass

    async def reap(self) -> None:
        """End the current session if it's overdue or lost. Runs every few seconds, and before every open."""
        session = self.current
        if session is None:
            return
        reason = session.device.lost or session.overdue()
        if reason:
            await self.end(session, reason)

    def status(self) -> dict | None:
        session = self.current
        if session is None:
            return None
        return {"id": session.id, "key": session.key, "steps": session.steps, "expires_at": _iso(session.expires_at)}


def _logged(body) -> dict:
    """An action as the audit log keeps it: typed text by length, never its characters."""
    if not isinstance(body, dict):
        return {}
    kept = {k: v for k, v in body.items() if k != "text"}
    if "text" in body:
        kept["text_length"] = len(body["text"]) if isinstance(body["text"], str) else None
    return kept
