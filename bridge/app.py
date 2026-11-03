"""The bridge's HTTP API. Plain JSON, bearer keys, and nothing about hob.

    GET    /v1/status                        → { bridge, device: { version, video, usb, http }, session? }
    POST   /v1/sessions { ttl?, scale? }     → 201 { id, expires_at, video, screen }
    GET    /v1/sessions/:id?screen=1&scale=  → { id, expires_at, video, screen }
    POST   /v1/sessions/:id/actions { action, ... } → { id, expires_at, video, screen }
    DELETE /v1/sessions/:id                  → 204

Errors are { error }: 400 or 422 for a refused request, 401 for a missing
or wrong key, 404 for no such session, 409 while another session is open,
410 for a session that ended (the reason says expired, closed, or lost),
502 when the device fails or can't be reached.
"""

from __future__ import annotations

import asyncio
import json
import logging

from aiohttp import web

from . import __version__
from .actions import Refused
from .device import DeviceError
from .keys import Keys
from .session import Bridge, Busy, Ended, Unknown, _iso

log = logging.getLogger("bridge.app")

BRIDGE = web.AppKey("bridge", Bridge)
KEYS = web.AppKey("keys", Keys)
STATUS = web.AppKey("status", object)  # async () -> dict: the device over HTTP, without connecting
KEY_NAME = web.RequestKey("key", str)  # which client key the request came with


def error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def auth_and_errors(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else None
    name = request.app[KEYS].lookup(token)
    if name is None:
        return error(401, "a bridge key is required: Authorization: Bearer kvb_...")
    request[KEY_NAME] = name
    try:
        return await handler(request)
    except Refused as e:
        return error(e.status, str(e))
    except Busy as e:
        return error(409, str(e))
    except Ended as e:
        return error(410, str(e))
    except Unknown as e:
        return error(404, str(e))
    except DeviceError as e:
        return error(502, str(e))
    except web.HTTPException:
        raise
    except Exception:
        log.exception("bridge failed on %s %s", request.method, request.path)
        return error(500, "the bridge failed; see its log")


async def body(request: web.Request) -> dict:
    if not request.can_read_body:
        return {}
    try:
        data = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise Refused("the body is not JSON") from None
    if not isinstance(data, dict):
        raise Refused("the body is a JSON object")
    return data


async def status(request: web.Request) -> web.Response:
    bridge = request.app[BRIDGE]
    await bridge.reap()
    device: dict = {}
    session = bridge.current
    if session is not None:
        device["version"] = session.device.version
        device["video"] = await session.video()
        try:
            device["usb"] = await session.call("getUsbEmulationState")
        except DeviceError:
            device["usb"] = None
    else:
        # No session: answer from the device's HTTP side, so checking the
        # bridge never cuts a person off their own KVM. Video is known only
        # while connected; ?probe=1 connects briefly to find out.
        if request.query.get("probe") == "1":
            opened = await bridge.open(request[KEY_NAME], ttl=30, scale=320)
            try:
                live = bridge.get(opened["id"])
                device["version"] = live.device.version
                device["video"] = opened["video"]
                device["usb"] = await live.call("getUsbEmulationState")
            finally:
                await bridge.close(opened["id"])
    try:
        device["http"] = await request.app[STATUS]()
    except DeviceError as e:
        device["http"] = {"error": str(e)}
    answer = {"bridge": __version__, "device": device, "key": request[KEY_NAME]}
    if bridge.status():
        answer["session"] = bridge.status()
    return web.json_response(answer)


async def open_session(request: web.Request) -> web.Response:
    data = await body(request)
    unknown = sorted(set(data) - {"ttl", "scale"})
    if unknown:
        raise Refused(f"a session takes ttl and scale, not {', '.join(unknown)}")
    state = await request.app[BRIDGE].open(request[KEY_NAME], ttl=data.get("ttl"), scale=data.get("scale"))
    return web.json_response(state, status=201)


async def show_session(request: web.Request) -> web.Response:
    bridge = request.app[BRIDGE]
    unknown = sorted(set(request.query) - {"screen", "scale"})
    if unknown:
        raise Refused(f"takes screen and scale, not {', '.join(unknown)}")
    if request.query.get("screen", "1") == "1":
        return web.json_response(await bridge.screen(request.match_info["id"], scale=request.query.get("scale")))
    session = await bridge._live(request.match_info["id"])
    return web.json_response({"id": session.id, "expires_at": _iso(session.expires_at), "video": await session.video()})


async def act(request: web.Request) -> web.Response:
    return web.json_response(await request.app[BRIDGE].act(request.match_info["id"], await body(request)))


async def close_session(request: web.Request) -> web.Response:
    await request.app[BRIDGE].close(request.match_info["id"])
    return web.Response(status=204)


async def reaper(app: web.Application):
    async def loop():
        while True:
            await asyncio.sleep(5)
            try:
                await app[BRIDGE].reap()
            except Exception:
                log.exception("reaping failed")

    task = asyncio.create_task(loop())
    yield
    task.cancel()
    session = app[BRIDGE].current
    if session is not None:
        await app[BRIDGE].end(session, "the bridge stopped")


def make_app(bridge: Bridge, keys: Keys, device_status) -> web.Application:
    app = web.Application(middlewares=[auth_and_errors], client_max_size=64 * 1024)
    app[BRIDGE] = bridge
    app[KEYS] = keys
    app[STATUS] = device_status
    app.router.add_get("/v1/status", status)
    app.router.add_post("/v1/sessions", open_session)
    app.router.add_get("/v1/sessions/{id}", show_session)
    app.router.add_post("/v1/sessions/{id}/actions", act)
    app.router.add_delete("/v1/sessions/{id}", close_session)
    app.cleanup_ctx.append(reaper)
    return app
