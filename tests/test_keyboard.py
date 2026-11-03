import pytest

from bridge import keyboard as kb


def test_text_is_usage_codes_with_shift_for_capitals_and_symbols():
    assert kb.text("aZ1!\n") == [(0, 0x04), (kb.LSHIFT, 0x1D), (0, 0x1E), (kb.LSHIFT, 0x1E), (0, 0x28)]


def test_caps_lock_flips_shift_for_letters_only():
    assert kb.text("aA!", caps_lock=True) == [(kb.LSHIFT, 0x04), (0, 0x04), (kb.LSHIFT, 0x1E)]


def test_characters_the_layout_cant_type_are_refused_all_at_once():
    with pytest.raises(kb.KeyError_, match="'é', '😀'"):
        kb.text("café 😀")


@pytest.mark.parametrize("spec, modifier, key", [
    ("Return", 0, 0x28),
    ("cmd+space", kb.LMETA, 0x2C),
    ("ctrl+alt+Delete", kb.LCTRL | kb.LALT, 0x4C),
    ("cmd+a", kb.LMETA, 0x04),
    ("cmd+A", kb.LMETA, 0x04),
    ("cmd++", kb.LMETA | kb.LSHIFT, 0x2E),
    ("shift+Tab", kb.LSHIFT, 0x2B),
    ("F12", 0, 0x45),
    ("Page_Down", 0, 0x4E),
    ("super", kb.LMETA, None),
])
def test_chords(spec, modifier, key):
    assert kb.chord(spec) == kb.Chord(modifier, key)


@pytest.mark.parametrize("spec", ["", "cmd+", "cmd + a", "foo", "a+cmd", "cmd+foo", "x" * 70])
def test_bad_chords_are_refused(spec):
    with pytest.raises(kb.KeyError_):
        kb.chord(spec)
