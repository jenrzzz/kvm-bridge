"""The actions a session takes, matching Claude's computer-use tool, checked
and then turned into the JetKVM's JSON-RPC input calls.

Coordinates are pixels in the frame the caller last saw, at that frame's
size; they map to the device's 0-32767 absolute range. An unknown action or
argument is refused, never ignored, and so is text the keyboard layout can't
type: all of it is checked before anything reaches the device.

What is never sent: power, virtual media, wake-on-LAN, network, TLS, SSH
keys, dev mode, cloud, updates, reboot, factory reset. Only the methods in
INPUT_METHODS (and the read-only state calls in READ_METHODS) ever leave
this module; `Session` refuses any other method name in code.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from . import keyboard

INPUT_METHODS = frozenset({"absMouseReport", "wheelReport", "keyboardReport"})
READ_METHODS = frozenset({"getVideoState", "getKeyboardLedState", "getUsbEmulationState"})

CLICKS = {"left_click": (1, 1), "right_click": (2, 1), "middle_click": (4, 1), "double_click": (1, 2), "triple_click": (1, 3)}
ACTIONS: dict[str, list[str]] = {
    "screenshot": [],
    "mouse_move": ["x", "y"],
    **{name: ["x", "y", "modifiers"] for name in CLICKS},
    "left_click_drag": ["from", "to"],
    "scroll": ["x", "y", "direction", "amount"],
    "type": ["text"],
    "key": ["key"],
    "hold_key": ["key", "seconds"],
    "wait": ["seconds"],
}
REQUIRED: dict[str, list[str]] = {
    "mouse_move": ["x", "y"], **{name: ["x", "y"] for name in CLICKS}, "left_click_drag": ["from", "to"],
    "scroll": ["x", "y", "direction"], "type": ["text"], "key": ["key"], "hold_key": ["key", "seconds"], "wait": ["seconds"],
}
DIRECTIONS = {"up": (1, 0), "down": (-1, 0), "left": (0, -1), "right": (0, 1)}  # (wheelY, wheelX); HID wheel up is positive
MAX_TEXT = 2000
MAX_SCROLL = 30
MAX_HOLD = 10
MAX_WAIT = 30
ABS_MAX = 32767

# Pauses between reports, so the host sees distinct presses.
PRESS = 0.02
CLICK_GAP = 0.06
DRAG_STEPS = 12
KEY_GAP = 0.008


class Refused(ValueError):
    """The caller's mistake: an unknown action, a bad argument, or text the layout can't type."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class Frame:
    width: int
    height: int


def _whole(name: str, value: Any, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or (isinstance(value, float) and not value.is_integer()):
        raise Refused(f"{name} must be a whole number from {low} to {high}")
    value = int(value)
    if not low <= value <= high:
        raise Refused(f"{name} must be a whole number from {low} to {high}")
    return value


def _seconds(action: str, value: Any, limit: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= limit:
        raise Refused(f"seconds for {action} is a number from 0 to {limit}")
    return float(value)


def check(body: Any, frame: Frame) -> dict:
    """The action, every argument checked against the frame the caller last saw. Raises Refused."""
    if not isinstance(body, dict):
        raise Refused("the body is a JSON object: { action, ...its arguments }")
    action = body.get("action")
    if action not in ACTIONS:
        raise Refused(f"action is one of {', '.join(ACTIONS)}")
    unknown = sorted(set(body) - {"action"} - set(ACTIONS[action]))
    if unknown:
        raise Refused(f"{action} does not take {', '.join(unknown)} (it takes {', '.join(ACTIONS[action]) or 'nothing else'})")
    missing = [name for name in REQUIRED.get(action, []) if body.get(name) is None]
    if missing:
        raise Refused(f"{action} needs {' and '.join(missing)}")

    out: dict[str, Any] = {"action": action}
    if "x" in body:
        out["x"] = _whole("x", body["x"], 0, frame.width - 1)
        out["y"] = _whole("y", body["y"], 0, frame.height - 1)
    for name in ("from", "to"):
        if name in body:
            point = body[name]
            if not isinstance(point, list) or len(point) != 2:
                raise Refused(f"{name} is [x, y]")
            out[name] = [_whole(f"{name}[0]", point[0], 0, frame.width - 1), _whole(f"{name}[1]", point[1], 0, frame.height - 1)]
    if body.get("modifiers") is not None:
        try:
            parsed = keyboard.chord(body["modifiers"])
        except keyboard.KeyError_ as e:
            raise Refused(str(e)) from e
        if parsed.key is not None:
            raise Refused("modifiers is modifier keys only, like shift or cmd+shift")
        out["modifiers"] = parsed.modifier
    if "direction" in body:
        if body["direction"] not in DIRECTIONS:
            raise Refused(f"direction is one of {', '.join(DIRECTIONS)}")
        out["direction"] = body["direction"]
        out["amount"] = _whole("amount", body["amount"], 1, MAX_SCROLL) if body.get("amount") is not None else 3
    if "text" in body:
        if not isinstance(body["text"], str) or not 1 <= len(body["text"]) <= MAX_TEXT:
            raise Refused(f"text must be a string of 1 to {MAX_TEXT} characters")
        try:
            keyboard.text(body["text"])
        except keyboard.KeyError_ as e:
            raise Refused(str(e), status=422) from e
        out["text"] = body["text"]
    if "key" in body:
        try:
            out["chord"] = keyboard.chord(body["key"])
        except keyboard.KeyError_ as e:
            raise Refused(str(e), status=422) from e
        if action == "key" and out["chord"].key is None:
            raise Refused("key needs a key, not only modifiers; use hold_key to hold a modifier")
    if "seconds" in body:
        out["seconds"] = _seconds(action, body["seconds"], MAX_HOLD if action == "hold_key" else MAX_WAIT)
    return out


def absolute(x: int, y: int, frame: Frame) -> tuple[int, int]:
    """A pixel in the caller's frame, as the device's 0-32767 absolute coordinates (the pixel's centre)."""
    ax = round((x + 0.5) / frame.width * ABS_MAX)
    ay = round((y + 0.5) / frame.height * ABS_MAX)
    return min(max(ax, 0), ABS_MAX), min(max(ay, 0), ABS_MAX)


class Driver:
    """Sends a checked action to the device as input reports."""

    def __init__(self, call):
        self.call = call  # async (method, params) -> result; only INPUT_METHODS and READ_METHODS are sent

    async def mouse(self, x: int, y: int, buttons: int = 0) -> None:
        await self.call("absMouseReport", {"x": x, "y": y, "buttons": buttons})

    async def keys(self, modifier: int, usages: list[int]) -> None:
        await self.call("keyboardReport", {"modifier": modifier, "keys": usages})

    async def caps_lock(self) -> bool:
        try:
            state = await self.call("getKeyboardLedState", {}) or {}
        except Exception:
            return False
        return bool(state.get("caps_lock") or state.get("capsLock"))

    async def run(self, action: dict, frame: Frame) -> None:
        kind = action["action"]
        if kind in ("screenshot",):
            return
        if kind == "wait":
            await asyncio.sleep(action["seconds"])
            return
        if kind == "mouse_move":
            await self.mouse(*absolute(action["x"], action["y"], frame))
            return
        if kind in CLICKS:
            button, count = CLICKS[kind]
            x, y = absolute(action["x"], action["y"], frame)
            modifier = action.get("modifiers", 0)
            await self.mouse(x, y)
            if modifier:
                await self.keys(modifier, [])
            try:
                for n in range(count):
                    if n:
                        await asyncio.sleep(CLICK_GAP)
                    await self.mouse(x, y, button)
                    await asyncio.sleep(PRESS)
                    await self.mouse(x, y, 0)
            finally:
                if modifier:
                    await self.keys(0, [])
            return
        if kind == "left_click_drag":
            (fx, fy), (tx, ty) = absolute(*action["from"], frame), absolute(*action["to"], frame)
            await self.mouse(fx, fy)
            await self.mouse(fx, fy, 1)
            try:
                for step in range(1, DRAG_STEPS + 1):
                    await asyncio.sleep(PRESS)
                    await self.mouse(fx + (tx - fx) * step // DRAG_STEPS, fy + (ty - fy) * step // DRAG_STEPS, 1)
            finally:
                await self.mouse(tx, ty, 0)
            return
        if kind == "scroll":
            await self.mouse(*absolute(action["x"], action["y"], frame))
            wheel_y, wheel_x = DIRECTIONS[action["direction"]]
            for _ in range(action["amount"]):
                params = {"wheelY": wheel_y} if wheel_x == 0 else {"wheelY": 0, "wheelX": wheel_x}
                await self.call("wheelReport", params)
                await asyncio.sleep(PRESS)
            return
        if kind == "type":
            for modifier, usage in keyboard.text(action["text"], caps_lock=await self.caps_lock()):
                await self.keys(modifier, [usage])
                await asyncio.sleep(KEY_GAP)
                await self.keys(0, [])
                await asyncio.sleep(KEY_GAP)
            return
        if kind in ("key", "hold_key"):
            chord: keyboard.Chord = action["chord"]
            pressed = [chord.key] if chord.key is not None else []
            if chord.modifier and pressed:
                await self.keys(chord.modifier, [])  # modifiers down first, as a person presses them
            await self.keys(chord.modifier, pressed)
            try:
                await asyncio.sleep(action["seconds"] if kind == "hold_key" else PRESS * 2)
            finally:
                await self.keys(0, [])
            return
        raise Refused(f"unknown action {kind}")  # unreachable: check() refused it
