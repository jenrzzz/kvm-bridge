# kvm-bridge

*A screen, a keyboard, and a mouse on a real computer, over plain HTTP.*
The bridge holds a [JetKVM](https://jetkvm.com)'s WebRTC connection and
offers it a session at a time: open a session, get the screen back as a
PNG, send an action (click, type, key, scroll, drag, wait), get a fresh
frame after each one. hob's consoles ([KVM.md](../hob/KVM.md)) use it; it
knows nothing about hob.

The JetKVM has no HTTP API for screens or input; everything is WebRTC
(an H.264 video track, and JSON-RPC 2.0 over a data channel named `rpc`).
Ruby has no WebRTC stack worth using, so this is Python with
[aiortc](https://github.com/aiortc/aiortc).

## Running

Python 3.12+ and [uv](https://docs.astral.sh/uv/). Run it on a tailnet
host that is **not** the machine behind the KVM, so it survives that
machine rebooting.

```sh
uv sync
bin/bridge key add hob                           # prints kvb_..., once; keep it in hob's env as KVM_BRIDGE_KEY
export JETKVM_URL=http://jetkvm.tailnet.ts.net JETKVM_PASSWORD=...
bin/bridge check                                 # the device answers over HTTP and takes the password
bin/bridge shot /tmp/kvm.png                     # one frame over WebRTC; takes the KVM for a second
BRIDGE_BIND=127.0.0.1,100.x.y.z bin/bridge serve # :8379
```

Bind to loopback and the tailnet address, never every interface: the
tailnet is the transport security, and the keys are the authorization.

| env | default | |
|---|---|---|
| `JETKVM_URL` | | the device |
| `JETKVM_PASSWORD` | unset | the device's local password. Only the bridge holds it |
| `BRIDGE_BIND` | `127.0.0.1` | comma list of addresses to listen on |
| `BRIDGE_PORT` | `8379` | |
| `BRIDGE_HOME` | `~/.config/kvm-bridge` | `keys.json`: key digests only, mode 600 |
| `BRIDGE_AUDIT_LOG` | `$BRIDGE_HOME/audit.jsonl` | one line per session opened, action, and close. No frames; typed text only by length |
| `BRIDGE_SETTLE_MS` | `300` | how long after input the answering frame is taken |

## API

Every request needs `Authorization: Bearer kvb_...`.

```
GET    /v1/status[?probe=1]              → { bridge, key, device: { version?, video?, usb?, http }, session? }
POST   /v1/sessions { ttl?, scale? }     → 201 { id, expires_at, video, screen }
GET    /v1/sessions/:id?screen=1&scale=  → { id, expires_at, video, screen }
POST   /v1/sessions/:id/actions { action, ... } → { id, expires_at, video, screen }
DELETE /v1/sessions/:id                  → 204
```

`screen` is `{ data, mime_type, width, height }`: a PNG, base64, `scale`
pixels wide (default 1280, up to the device's own 1920). `video` is the
device's `getVideoState`: `{ ready, error?, width, height, fps }`, with
`error: "no_signal"` when the host's display is asleep.

`/v1/status` connects nothing unless asked: without a session it answers
from the device's HTTP side. `?probe=1` connects for a moment to read
video and USB state, which cuts off anyone using the KVM in a browser.

**Sessions.** One at a time: a second `POST /v1/sessions` is 409 while one
is open. The bridge connects to the device when a session opens and
disconnects when it ends, so a person's KVM tab is interrupted only while
a session is open. A session ends when closed, after `ttl` seconds without
an action (30–900, default 300), after 30 minutes regardless, or when
someone opens the KVM in a browser (the device takes the newer connection).
The next call on an ended session is 410, and its `error` says which:
`closed by the caller`, `session expired: ...`, or `another session
connected to the KVM`.

**Actions** match Claude's computer-use tool. Coordinates are pixels in
the last frame the session served, at that frame's size; the bridge maps
them to the device's 0–32767 absolute range.

| action | arguments |
|---|---|
| `screenshot` | |
| `mouse_move` | `x, y` |
| `left_click`, `right_click`, `middle_click`, `double_click`, `triple_click` | `x, y`, `modifiers?` (`shift`, `cmd+shift`, ...) |
| `left_click_drag` | `from: [x, y], to: [x, y]` |
| `scroll` | `x, y, direction (up\|down\|left\|right), amount? (1–30, default 3)` |
| `type` | `text` (1–2000 characters, US layout; caps lock is read and accounted for) |
| `key` | `key`: `Return`, `Escape`, `cmd+space`, `ctrl+alt+Delete`, `cmd++`, ... |
| `hold_key` | `key, seconds (≤10)` |
| `wait` | `seconds (≤30)` |

An unknown action or argument is refused (400), never ignored, and so is
text the layout can't type or a key it doesn't know (422). All of it is
checked before anything reaches the device.

| status | when |
|---|---|
| 400, 422 | a refused request |
| 401 | no key, or not one of the bridge's |
| 404 | no such session |
| 409 | another session is open |
| 410 | the session ended; `error` says why |
| 502 | the device failed, refused the password, or couldn't be reached |

**What the bridge never sends** is everything else the device's RPC
offers: power and ATX, virtual media, wake-on-LAN, network, TLS, SSH keys,
dev mode, cloud, updates, reboot, factory reset. The device's password
opens all of that, and there are no scoped keys on the device, so the
bridge is the scope: `Session.call` refuses any method that isn't an input
report (`absMouseReport`, `wheelReport`, `keyboardReport`) or a read
(`getVideoState`, `getKeyboardLedState`, `getUsbEmulationState`).

## Tests

```sh
uv run pytest
```

They run against `bridge.fake.FakeDevice`. `bin/bridge shot` is the check
against a real device.
