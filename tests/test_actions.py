import pytest

from bridge import actions
from bridge.actions import Frame, Refused, check
from bridge.fake import FakeDevice

FRAME = Frame(1280, 720)


async def run(body, device=None, frame=FRAME):
    device = device or FakeDevice()
    await actions.Driver(device.call).run(check(body, frame), frame)
    return device.inputs()


def test_coordinates_map_to_the_absolute_range_at_the_pixel_centre():
    assert actions.absolute(0, 0, FRAME) == (13, 23)
    assert actions.absolute(1279, 719, FRAME) == (32754, 32744)
    assert actions.absolute(640, 360, FRAME) == (16396, 16406)


@pytest.mark.parametrize("body, message", [
    ({"action": "reboot"}, "action is one of"),
    ({"action": "factoryReset"}, "action is one of"),
    ({"action": "screenshot", "x": 1}, "does not take x"),
    ({"action": "left_click", "x": 1}, "needs y"),
    ({"action": "left_click", "x": 1280, "y": 0}, "x must be"),
    ({"action": "left_click", "x": 1.5, "y": 0}, "x must be"),
    ({"action": "left_click", "x": True, "y": 0}, "x must be"),
    ({"action": "left_click", "x": 1, "y": 1, "modifiers": "cmd+a"}, "modifier keys only"),
    ({"action": "left_click_drag", "from": [1], "to": [1, 2]}, "from is"),
    ({"action": "scroll", "x": 1, "y": 1, "direction": "in"}, "direction"),
    ({"action": "scroll", "x": 1, "y": 1, "direction": "up", "amount": 99}, "amount"),
    ({"action": "type", "text": ""}, "text must be"),
    ({"action": "key", "key": "shift"}, "use hold_key"),
    ({"action": "hold_key", "key": "shift", "seconds": 11}, "seconds"),
    ({"action": "wait", "seconds": -1}, "seconds"),
    ("nope", "JSON object"),
])
def test_refusals(body, message):
    with pytest.raises(Refused, match=message):
        check(body, FRAME)


def test_untypeable_text_and_unknown_keys_are_422():
    with pytest.raises(Refused) as e:
        check({"action": "type", "text": "naïve"}, FRAME)
    assert e.value.status == 422
    with pytest.raises(Refused) as e:
        check({"action": "key", "key": "Hyper_Q"}, FRAME)
    assert e.value.status == 422


async def test_left_click_with_modifiers_holds_them_around_the_press():
    inputs = await run({"action": "left_click", "x": 640, "y": 360, "modifiers": "cmd"})
    assert inputs == [
        ("absMouseReport", {"x": 16396, "y": 16406, "buttons": 0}),
        ("keyboardReport", {"modifier": 8, "keys": []}),
        ("absMouseReport", {"x": 16396, "y": 16406, "buttons": 1}),
        ("absMouseReport", {"x": 16396, "y": 16406, "buttons": 0}),
        ("keyboardReport", {"modifier": 0, "keys": []}),
    ]


async def test_double_and_right_clicks():
    presses = [p["buttons"] for m, p in await run({"action": "double_click", "x": 1, "y": 1}) if m == "absMouseReport"]
    assert presses == [0, 1, 0, 1, 0]
    presses = [p["buttons"] for m, p in await run({"action": "right_click", "x": 1, "y": 1}) if m == "absMouseReport"]
    assert presses == [0, 2, 0]


async def test_drag_presses_moves_and_releases():
    inputs = await run({"action": "left_click_drag", "from": [0, 0], "to": [1279, 719]})
    assert inputs[0][1]["buttons"] == 0 and inputs[1][1]["buttons"] == 1
    assert inputs[-1] == ("absMouseReport", {"x": 32754, "y": 32744, "buttons": 0})
    assert len(inputs) == 2 + actions.DRAG_STEPS + 1


async def test_scroll_moves_then_wheels_one_click_at_a_time():
    inputs = await run({"action": "scroll", "x": 1, "y": 1, "direction": "down", "amount": 2})
    assert inputs[1:] == [("wheelReport", {"wheelY": -1}), ("wheelReport", {"wheelY": -1})]
    inputs = await run({"action": "scroll", "x": 1, "y": 1, "direction": "right"})
    assert inputs[1:] == [("wheelReport", {"wheelY": 0, "wheelX": 1})] * 3


async def test_type_presses_and_releases_each_character_minding_caps_lock():
    inputs = await run({"action": "type", "text": "Hi"}, device=FakeDevice(caps_lock=True))
    assert inputs == [
        ("keyboardReport", {"modifier": 0, "keys": [0x0B]}), ("keyboardReport", {"modifier": 0, "keys": []}),
        ("keyboardReport", {"modifier": 2, "keys": [0x0C]}), ("keyboardReport", {"modifier": 0, "keys": []}),
    ]


async def test_key_chord_and_hold():
    inputs = await run({"action": "key", "key": "cmd+space"})
    assert inputs == [("keyboardReport", {"modifier": 8, "keys": []}), ("keyboardReport", {"modifier": 8, "keys": [0x2C]}),
                      ("keyboardReport", {"modifier": 0, "keys": []})]
    inputs = await run({"action": "hold_key", "key": "alt", "seconds": 0})
    assert inputs == [("keyboardReport", {"modifier": 4, "keys": []}), ("keyboardReport", {"modifier": 0, "keys": []})]


async def test_screenshot_and_wait_send_nothing():
    assert await run({"action": "screenshot"}) == []
    assert await run({"action": "wait", "seconds": 0}) == []
