"""Text and chords to USB HID boot-keyboard reports.

`keyboardReport` takes HID usage codes, which only become characters through
the host's keyboard layout. This is the US layout: each character is a usage
code plus whether shift is held. Characters the layout can't produce are
refused, never dropped. Caps lock (read from the device) flips shift for
letters only.

Chords are written as the computer-use tool writes them: `Return`,
`cmd+space`, `ctrl+alt+Delete`, `shift+Tab`, `cmd++`.
"""

from __future__ import annotations

from dataclasses import dataclass


class KeyError_(ValueError):
    """A key, chord, or character the bridge can't send. Refused, not dropped."""


# Modifier bits in a boot-keyboard report's first byte.
LCTRL, LSHIFT, LALT, LMETA, RCTRL, RSHIFT, RALT, RMETA = (1 << i for i in range(8))

MODIFIERS = {
    "shift": LSHIFT, "lshift": LSHIFT, "shift_l": LSHIFT, "rshift": RSHIFT, "shift_r": RSHIFT,
    "ctrl": LCTRL, "control": LCTRL, "lctrl": LCTRL, "control_l": LCTRL, "ctrl_l": LCTRL,
    "rctrl": RCTRL, "control_r": RCTRL, "ctrl_r": RCTRL,
    "alt": LALT, "option": LALT, "opt": LALT, "lalt": LALT, "alt_l": LALT, "ralt": RALT, "alt_r": RALT,
    "cmd": LMETA, "command": LMETA, "super": LMETA, "meta": LMETA, "win": LMETA, "windows": LMETA,
    "super_l": LMETA, "meta_l": LMETA, "lmeta": LMETA, "super_r": RMETA, "meta_r": RMETA, "rmeta": RMETA,
}

# US layout: character -> (usage, shift).
CHARS: dict[str, tuple[int, bool]] = {}
for i, letter in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CHARS[letter] = (0x04 + i, False)
    CHARS[letter.upper()] = (0x04 + i, True)
for i, (plain, shifted) in enumerate(zip("1234567890", "!@#$%^&*()")):
    CHARS[plain] = (0x1E + i, False)
    CHARS[shifted] = (0x1E + i, True)
for usage, plain, shifted in [
    (0x2D, "-", "_"), (0x2E, "=", "+"), (0x2F, "[", "{"), (0x30, "]", "}"), (0x31, "\\", "|"),
    (0x33, ";", ":"), (0x34, "'", '"'), (0x35, "`", "~"), (0x36, ",", "<"), (0x37, ".", ">"), (0x38, "/", "?"),
]:
    CHARS[plain] = (usage, False)
    CHARS[shifted] = (usage, True)
CHARS[" "] = (0x2C, False)
CHARS["\n"] = (0x28, False)
CHARS["\t"] = (0x2B, False)

# Named keys, lowercased; the xdotool names the computer-use tool uses, and
# the obvious others.
NAMED = {
    "return": 0x28, "enter": 0x28, "kp_enter": 0x58, "escape": 0x29, "esc": 0x29,
    "backspace": 0x2A, "tab": 0x2B, "space": 0x2C, "delete": 0x4C, "del": 0x4C, "insert": 0x49,
    "home": 0x4A, "end": 0x4D, "page_up": 0x4B, "pageup": 0x4B, "prior": 0x4B,
    "page_down": 0x4E, "pagedown": 0x4E, "next": 0x4E,
    "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
    "caps_lock": 0x39, "capslock": 0x39, "num_lock": 0x53, "scroll_lock": 0x47,
    "print": 0x46, "printscreen": 0x46, "pause": 0x48, "menu": 0x65,
    "minus": 0x2D, "equal": 0x2E, "bracketleft": 0x2F, "bracketright": 0x30, "backslash": 0x31,
    "semicolon": 0x33, "apostrophe": 0x34, "grave": 0x35, "comma": 0x36, "period": 0x37, "slash": 0x38,
    "audiomute": 0x7F, "audioraisevolume": 0x80, "audiolowervolume": 0x81,
    "xf86audiomute": 0x7F, "xf86audioraisevolume": 0x80, "xf86audiolowervolume": 0x81,
}
NAMED.update({f"f{n}": 0x3A + n - 1 for n in range(1, 13)})
NAMED.update({f"f{n}": 0x68 + n - 13 for n in range(13, 25)})


@dataclass(frozen=True)
class Chord:
    """Modifiers held, and at most one key pressed with them (none for a bare modifier)."""

    modifier: int
    key: int | None


def char(c: str, caps_lock: bool = False) -> tuple[int, int]:
    """(modifier, usage) for one character, or raise KeyError_."""
    if c not in CHARS:
        raise KeyError_(f"{c!r} can't be typed on the US keyboard layout")
    usage, shift = CHARS[c]
    if caps_lock and c.isalpha():
        shift = not shift
    return (LSHIFT if shift else 0), usage


def text(s: str, caps_lock: bool = False) -> list[tuple[int, int]]:
    """Every character's (modifier, usage), checked before any is sent."""
    bad = sorted({c for c in s if c not in CHARS})
    if bad:
        raise KeyError_(f"can't be typed on the US keyboard layout: {', '.join(repr(c) for c in bad)}")
    return [char(c, caps_lock) for c in s]


def _parts(spec: str) -> list[str]:
    """Split on '+', keeping a literal '+' key at the end: 'cmd++' is cmd and '+'."""
    if spec == "+":
        return ["+"]
    if spec.endswith("++"):
        parts = spec[:-2].split("+") + ["+"]
    else:
        parts = spec.split("+")
    if any(p == "" for p in parts):
        raise KeyError_(f"not a key or chord: {spec!r}")
    return parts


def chord(spec: str, caps_lock: bool = False) -> Chord:
    """A chord like 'cmd+shift+4', 'Return', or 'shift' (a bare modifier, for hold_key)."""
    if not isinstance(spec, str) or not spec or len(spec) > 64 or any(ch.isspace() for ch in spec):
        raise KeyError_(f"not a key or chord: {spec!r}")
    parts = _parts(spec)
    modifier, key = 0, None
    for index, part in enumerate(parts):
        lower = part.lower()
        last = index == len(parts) - 1
        if lower in MODIFIERS and not (last and len(part) == 1):
            modifier |= MODIFIERS[lower]
            continue
        if not last:
            raise KeyError_(f"{part!r} in {spec!r} is not a modifier; only the last key in a chord may be one")
        if lower in NAMED:
            key = NAMED[lower]
        elif len(part) == 1 and part in CHARS:
            shift, key = char(part)
            # A letter is the key itself, as computer-use writes cmd+a; shift
            # rides along only for symbols that need it (cmd++ is cmd+shift+=).
            if not part.isalpha():
                modifier |= shift
        else:
            raise KeyError_(f"unknown key {part!r} in {spec!r}")
    return Chord(modifier, key)
