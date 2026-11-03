"""An in-memory JetKVM for tests: it answers the read calls, records every
input report, makes frames on demand, and can be taken over."""

from __future__ import annotations

import time

from PIL import Image

from .device import DeviceError


class FakeDevice:
    def __init__(self, width: int = 1920, height: int = 1080, caps_lock: bool = False, fail_connect: str | None = None):
        self.width, self.height = width, height
        self.caps_lock = caps_lock
        self.fail_connect = fail_connect
        self.calls: list[tuple[str, dict]] = []
        self.lost: str | None = None
        self.version = "0.5.10"
        self.connected = False
        self.closed = False
        self.video = {"ready": True, "width": width, "height": height, "fps": 60}
        self.no_frames = False

    async def connect(self) -> None:
        if self.fail_connect:
            raise DeviceError(self.fail_connect)
        self.connected = True

    async def close(self) -> None:
        self.closed = True
        self.connected = False

    def take_over(self) -> None:
        self.lost = "another session connected to the KVM"

    async def call(self, method: str, params: dict | None = None):
        if self.lost:
            raise DeviceError(self.lost)
        self.calls.append((method, params or {}))
        if method == "getVideoState":
            return self.video
        if method == "getKeyboardLedState":
            return {"caps_lock": self.caps_lock, "num_lock": False, "scroll_lock": False}
        if method == "getUsbEmulationState":
            return True
        return None

    def inputs(self) -> list[tuple[str, dict]]:
        return [c for c in self.calls if not c[0].startswith("get")]

    async def frame_after(self, t: float, timeout: float):
        if self.no_frames:
            return None
        return Image.new("RGB", (self.width, self.height), (int(time.monotonic() * 1000) % 255, 40, 90))
