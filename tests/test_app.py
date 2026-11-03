import base64
import io
from datetime import timedelta

import pytest
from PIL import Image

from bridge import session as sessions
from bridge.app import make_app
from bridge.audit import Audit
from bridge.device import DeviceError
from bridge.fake import FakeDevice
from bridge.keys import Keys


@pytest.fixture
def world(tmp_path):
    keys = Keys(tmp_path / "keys.json")
    token = keys.add("hob")
    devices = []

    def factory():
        devices.append(FakeDevice())
        return devices[-1]

    async def device_status():
        return {"status": {"isSetup": True}}

    bridge = sessions.Bridge(factory, audit=Audit(tmp_path / "audit.jsonl"), settle=0)
    return {"app": make_app(bridge, keys, device_status), "bridge": bridge, "devices": devices,
            "headers": {"Authorization": f"Bearer {token}"}, "audit": tmp_path / "audit.jsonl"}


@pytest.fixture
async def client(world, aiohttp_client):
    return await aiohttp_client(world["app"])


async def post(client, world, path, json=None):
    return await client.post(path, json=json, headers=world["headers"])


async def test_a_key_is_required(client):
    r = await client.get("/v1/status")
    assert r.status == 401
    r = await client.get("/v1/status", headers={"Authorization": "Bearer kvb_wrong"})
    assert r.status == 401


async def test_open_act_screen_close(client, world):
    r = await post(client, world, "/v1/sessions", {"ttl": 60})
    assert r.status == 201
    state = await r.json()
    assert state["screen"]["width"] == 1280 and state["screen"]["height"] == 720
    assert state["screen"]["mime_type"] == "image/png"
    assert Image.open(io.BytesIO(base64.b64decode(state["screen"]["data"]))).size == (1280, 720)
    assert state["video"]["ready"] is True
    sid = state["id"]

    r = await post(client, world, f"/v1/sessions/{sid}/actions", {"action": "left_click", "x": 640, "y": 360})
    assert r.status == 200, await r.text()
    assert (await r.json())["screen"]["width"] == 1280
    device = world["devices"][0]
    assert ("absMouseReport", {"x": 16396, "y": 16406, "buttons": 1}) in device.inputs()

    r = await client.get(f"/v1/sessions/{sid}", params={"screen": "1", "scale": "1920"}, headers=world["headers"])
    assert (await r.json())["screen"]["width"] == 1920
    r = await post(client, world, f"/v1/sessions/{sid}/actions", {"action": "mouse_move", "x": 1919, "y": 1079})
    assert r.status == 200, "coordinates follow the newest frame's size"

    r = await client.delete(f"/v1/sessions/{sid}", headers=world["headers"])
    assert r.status == 204
    assert device.closed
    r = await post(client, world, f"/v1/sessions/{sid}/actions", {"action": "screenshot"})
    assert r.status == 410
    assert "closed by the caller" in (await r.json())["error"]
    r = await client.delete(f"/v1/sessions/{sid}", headers=world["headers"])
    assert r.status == 204, "closing twice is fine"
    r = await client.get("/v1/sessions/nope", headers=world["headers"])
    assert r.status == 404

    log = world["audit"].read_text()
    assert '"event": "open"' in log and '"event": "act"' in log and '"event": "close"' in log
    assert "data" not in log


async def test_one_session_at_a_time(client, world):
    first = await (await post(client, world, "/v1/sessions")).json()
    r = await post(client, world, "/v1/sessions")
    assert r.status == 409
    await client.delete(f"/v1/sessions/{first['id']}", headers=world["headers"])
    assert (await post(client, world, "/v1/sessions")).status == 201


async def test_refusals_never_reach_the_device(client, world):
    sid = (await (await post(client, world, "/v1/sessions")).json())["id"]
    for body, status in [({"action": "reboot"}, 400), ({"action": "type", "text": "é"}, 422),
                         ({"action": "left_click", "x": 1280, "y": 0}, 400), ({"action": "key", "key": "nope"}, 422)]:
        r = await post(client, world, f"/v1/sessions/{sid}/actions", body)
        assert r.status == status, body
    assert world["devices"][0].inputs() == []
    assert (await post(client, world, "/v1/sessions", {"password": "x"})).status == 400


async def test_device_methods_outside_the_allowlist_are_refused_in_code(world):
    session = sessions.Session(FakeDevice(), 60, 1280, "hob", 0)
    for method in ["factoryReset", "reboot", "setSSHKeyState", "setATXPowerAction", "mountWithHTTP", "wakeHost"]:
        with pytest.raises(PermissionError):
            await session.call(method)


async def test_takeover_ends_the_session_as_lost_and_frees_the_kvm(client, world):
    sid = (await (await post(client, world, "/v1/sessions")).json())["id"]
    world["devices"][0].take_over()
    r = await post(client, world, f"/v1/sessions/{sid}/actions", {"action": "screenshot"})
    assert r.status == 410
    assert "another session connected" in (await r.json())["error"]
    assert (await post(client, world, "/v1/sessions")).status == 201


async def test_idle_and_lifetime_expiry(client, world, monkeypatch):
    sid = (await (await post(client, world, "/v1/sessions", {"ttl": 30})).json())["id"]
    session = world["bridge"].current
    session.last_step -= timedelta(seconds=31)
    r = await post(client, world, f"/v1/sessions/{sid}/actions", {"action": "screenshot"})
    assert r.status == 410
    assert "session expired: no step for 30 seconds" in (await r.json())["error"]
    assert world["devices"][0].closed

    sid = (await (await post(client, world, "/v1/sessions", {"ttl": 900})).json())["id"]
    session = world["bridge"].current
    session.opened -= timedelta(minutes=31)
    await world["bridge"].reap()
    r = await client.get(f"/v1/sessions/{sid}", headers=world["headers"])
    assert r.status == 410
    assert "30-minute limit" in (await r.json())["error"]


async def test_ttl_and_scale_are_checked(client, world):
    assert (await post(client, world, "/v1/sessions", {"scale": 10})).status == 400
    assert (await post(client, world, "/v1/sessions", {"ttl": "soon"})).status == 400
    state = await (await post(client, world, "/v1/sessions", {"ttl": 5, "scale": 640})).json()
    assert state["screen"]["width"] == 640
    assert world["bridge"].current.ttl == 30


async def test_a_device_that_wont_connect_is_502_and_leaves_nothing_open(world, aiohttp_client):
    world["bridge"].device_factory = lambda: FakeDevice(fail_connect="the JetKVM refused the password")
    client = await aiohttp_client(world["app"])
    r = await post(client, world, "/v1/sessions")
    assert r.status == 502
    assert world["bridge"].current is None


async def test_status_without_a_session_connects_nothing(client, world):
    body = await (await client.get("/v1/status", headers=world["headers"])).json()
    assert body["bridge"] == "0.1.0"
    assert body["device"] == {"http": {"status": {"isSetup": True}}}
    assert world["devices"] == []

    body = await (await client.get("/v1/status", params={"probe": "1"}, headers=world["headers"])).json()
    assert body["device"]["video"]["ready"] is True and body["device"]["version"] == "0.5.10"
    assert world["devices"][0].closed and world["bridge"].current is None

    sid = (await (await post(client, world, "/v1/sessions")).json())["id"]
    body = await (await client.get("/v1/status", headers=world["headers"])).json()
    assert body["session"]["id"] == sid
    assert body["device"]["usb"] is True
