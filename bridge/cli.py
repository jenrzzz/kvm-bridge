"""bridge: the command line.

    bridge serve                  the HTTP API (BRIDGE_BIND, BRIDGE_PORT)
    bridge key add NAME           a new bearer key, printed once
    bridge key list | key rm NAME
    bridge check                  the JetKVM over HTTP: up, and the password works. Connects nothing
    bridge shot OUT.png           connect over WebRTC, save one frame, print the video state, disconnect.
                                  Read-only, but it does take the KVM over for a second

Environment:
    JETKVM_URL         the device, e.g. http://jetkvm.tailnet.ts.net
    JETKVM_PASSWORD    the device's local password (unset for a device with none)
    BRIDGE_BIND        comma list of addresses to listen on (default 127.0.0.1)
    BRIDGE_PORT        default 8379
    BRIDGE_HOME        default ~/.config/kvm-bridge: keys.json lives here
    BRIDGE_AUDIT_LOG   default $BRIDGE_HOME/audit.jsonl
    BRIDGE_SETTLE_MS   how long after input the answering frame is taken (default 300)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from . import __version__


def home() -> Path:
    return Path(os.environ.get("BRIDGE_HOME") or Path.home() / ".config" / "kvm-bridge")


def keys():
    from .keys import Keys

    return Keys(home() / "keys.json")


def device_url() -> str:
    url = os.environ.get("JETKVM_URL")
    if not url:
        sys.exit("JETKVM_URL is not set: the JetKVM's address, e.g. http://jetkvm.tailnet.ts.net")
    return url


def jetkvm():
    from .device import JetKVM

    return JetKVM(device_url(), os.environ.get("JETKVM_PASSWORD"))


def serve() -> None:
    from aiohttp import web

    from .app import make_app
    from .audit import Audit
    from .session import Bridge

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    url, password = device_url(), os.environ.get("JETKVM_PASSWORD")
    if not keys().names():
        sys.exit("no keys yet: bridge key add hob")

    from .device import JetKVM

    audit = Audit(os.environ.get("BRIDGE_AUDIT_LOG") or home() / "audit.jsonl")
    settle = int(os.environ.get("BRIDGE_SETTLE_MS", "300")) / 1000
    bridge = Bridge(lambda: JetKVM(url, password), audit=audit, settle=settle)
    app = make_app(bridge, keys(), JetKVM(url, password).http_status)
    hosts = [h.strip() for h in os.environ.get("BRIDGE_BIND", "127.0.0.1").split(",") if h.strip()]
    if "0.0.0.0" in hosts or "::" in hosts:
        sys.exit("bind to loopback and the tailnet address, never every interface")
    port = int(os.environ.get("BRIDGE_PORT", "8379"))
    print(f"bridge {__version__} on {', '.join(hosts)}:{port}, for {url}", flush=True)
    web.run_app(app, host=hosts, port=port, print=None)


async def shot(out: str) -> None:
    from .session import encode

    device = jetkvm()
    started = time.monotonic()
    await device.connect()
    try:
        video = await device.call("getVideoState")
        image = await device.frame_after(started, 10)
        print(json.dumps({"version": device.version, "video": video, "connected_in": round(time.monotonic() - started, 2)}))
        if image is None:
            sys.exit("no frame arrived")
        image.save(out)
        print(f"{image.width}x{image.height} -> {out} ({len(encode(image, 1280)['data']) * 3 // 4 // 1024} KB as the bridge would send it at 1280)")
    finally:
        await device.close()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bridge", description="A JetKVM over plain HTTP, a session at a time.")
    parser.add_argument("--version", action="version", version=__version__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("serve")
    key = commands.add_parser("key").add_subparsers(dest="key_command", required=True)
    key.add_parser("add").add_argument("name")
    key.add_parser("list")
    key.add_parser("rm").add_argument("name")
    commands.add_parser("check")
    commands.add_parser("shot").add_argument("out")
    args = parser.parse_args(argv)

    if args.command == "serve":
        serve()
    elif args.command == "key":
        if args.key_command == "add":
            token = keys().add(args.name)
            print(f"{args.name}: {token}\nshown once; the bridge keeps only its digest ({keys().path})")
        elif args.key_command == "list":
            print("\n".join(keys().names()) or "no keys")
        elif args.key_command == "rm":
            print("removed" if keys().remove(args.name) else f"no key named {args.name}")
    elif args.command == "check":
        print(json.dumps(asyncio.run(jetkvm().http_status()), indent=2))
    elif args.command == "shot":
        asyncio.run(shot(args.out))


if __name__ == "__main__":
    main()
