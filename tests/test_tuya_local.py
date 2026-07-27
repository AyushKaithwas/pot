"""Work-mode handling for RGB lights.

Beta 1 is a 5-channel RGB batten, and its white and colour modes have separate data points. So
dimming has to follow whichever mode the light is in (keeping the hue when it's showing a
colour), a white temperature has to move it to white mode, and a read has to take the level from
the colour value while in colour mode.
"""

from __future__ import annotations

import pytest

from pot.skills.lights.provider import LightChange, LightError, LightUnsupported
from pot.skills.lights.tuya_local import _apply_sync, _read_sync

WHITE = "white"
COLOUR = "colour"


class FakeBulb:
    BULB_FEATURE_COLOUR = "colour"
    BULB_FEATURE_COLOURTEMP = "colourtemp"
    DPS_MODE_WHITE = WHITE
    DPS_MODE_COLOUR = COLOUR

    def __init__(self, *, mode: str = WHITE, capabilities: tuple[str, ...] = ("colour", "colourtemp")):
        self.mode = mode
        self.on = False
        self.brightness = 50
        self.colourtemp = 50
        self.hsv = (0.0, 1.0, 0.8)
        self.capabilities = capabilities
        self.calls: list[str] = []

    def bulb_has_capability(self, feature):
        return feature in self.capabilities

    def turn_on(self):
        self.calls.append("turn_on")
        self.on = True
        return {}

    def turn_off(self):
        self.calls.append("turn_off")
        self.on = False
        return {}

    def set_mode(self, mode):
        self.calls.append(f"set_mode:{mode}")
        self.mode = mode
        return {}

    def set_colour(self, r, g, b):
        self.calls.append(f"set_colour:{r},{g},{b}")
        self.mode = COLOUR
        return {}

    def set_hsv(self, h, s, v):
        self.calls.append(f"set_hsv:{h:.3f},{s:.3f},{v:.3f}")
        self.mode = COLOUR
        self.hsv = (h, s, v)
        return {}

    def set_brightness_percentage(self, value):
        self.calls.append(f"brightness:{value}")
        self.brightness = value
        return {}

    def set_colourtemp_percentage(self, value):
        self.calls.append(f"temp:{value}")
        self.colourtemp = value
        return {}

    def status(self):
        self.calls.append("status")
        return {"dps": {}}

    def state(self):
        return {"is_on": self.on, "mode": self.mode}

    def get_brightness_percentage(self, state=None):
        return self.brightness

    def get_colourtemp_percentage(self, state=None):
        return self.colourtemp

    def colour_hsv(self, state=None):
        return self.hsv


def test_dimming_a_coloured_light_keeps_the_colour():
    """ "Dim it" must not turn a red light white."""
    bulb = FakeBulb(mode=COLOUR)
    bulb.hsv = (0.0, 1.0, 1.0)  # red at full
    _apply_sync(bulb, LightChange(brightness_pct=30))
    assert "set_hsv:0.000,1.000,0.300" in bulb.calls
    assert f"set_mode:{WHITE}" not in bulb.calls
    assert not any(call.startswith("brightness:") for call in bulb.calls)
    assert bulb.mode == COLOUR


def test_dimming_a_white_light_uses_the_brightness_data_point():
    bulb = FakeBulb(mode=WHITE)
    _apply_sync(bulb, LightChange(brightness_pct=30))
    assert "brightness:30" in bulb.calls
    assert not any(call.startswith("set_hsv:") for call in bulb.calls)
    assert bulb.mode == WHITE


def test_asking_for_a_temperature_does_move_a_coloured_light_to_white():
    bulb = FakeBulb(mode=COLOUR)
    _apply_sync(bulb, LightChange(color_temp_pct=20))
    assert bulb.calls.index(f"set_mode:{WHITE}") < bulb.calls.index("temp:20")
    assert bulb.mode == WHITE


def test_setting_a_colour_does_not_flip_to_white_mode():
    bulb = FakeBulb(mode=WHITE)
    _apply_sync(bulb, LightChange(color="ff0000"))
    assert f"set_mode:{WHITE}" not in bulb.calls
    assert "set_hsv:0.000,1.000,1.000" in bulb.calls
    assert bulb.mode == COLOUR


def test_a_dimmed_colour_rides_in_the_colour_value_not_the_white_brightness_dp():
    """tinytuya's set_brightness picks its mode from a stale cache and would undo the colour."""
    bulb = FakeBulb(mode=WHITE)
    _apply_sync(bulb, LightChange(color="ff0000", brightness_pct=20))
    assert "set_hsv:0.000,1.000,0.200" in bulb.calls
    assert not any(call.startswith("brightness:") for call in bulb.calls)
    assert f"set_mode:{WHITE}" not in bulb.calls
    assert bulb.mode == COLOUR


def test_colour_and_temperature_together_drop_the_temperature_with_a_note():
    change, note = LightChange(color="ff0000", color_temp_pct=80).resolved()
    assert change.color_temp_pct is None
    assert change.color == "ff0000"
    assert note is not None and "temperature was ignored" in note


def test_a_change_without_a_colour_keeps_the_temperature():
    change, note = LightChange(brightness_pct=50, color_temp_pct=80).resolved()
    assert (change.color_temp_pct, note) == (80, None)


def test_temperature_is_never_sent_while_a_colour_is_being_set():
    bulb = FakeBulb(mode=WHITE)
    change, _ = LightChange(color="00ff00", color_temp_pct=80).resolved()
    _apply_sync(bulb, change)
    assert not any(call.startswith("temp:") for call in bulb.calls)


def test_switching_off_skips_everything_else():
    bulb = FakeBulb(mode=COLOUR)
    state = _apply_sync(bulb, LightChange(power=False))
    assert bulb.calls == ["turn_off"]
    assert state.power is False


def test_brightness_implies_switching_on():
    bulb = FakeBulb()
    _apply_sync(bulb, LightChange(brightness_pct=70))
    assert "turn_on" in bulb.calls


def test_colour_mode_read_takes_the_level_from_the_colour_value():
    bulb = FakeBulb(mode=COLOUR)
    bulb.on = True
    bulb.brightness = 1  # the parked white dp, which would be misleading
    bulb.hsv = (0.0, 1.0, 0.8)
    state = _read_sync(bulb)
    assert state.brightness_pct == 80
    assert state.color_temp_pct is None
    assert state.mode == COLOUR
    assert state.color == "ff0000"
    assert "colour mode" in state.describe()
    assert "#ff0000" in state.describe()


def test_white_mode_read_uses_the_brightness_data_point():
    bulb = FakeBulb(mode=WHITE)
    bulb.on = True
    bulb.brightness, bulb.colourtemp = 40, 0
    state = _read_sync(bulb)
    assert (state.brightness_pct, state.color_temp_pct) == (40, 0)
    assert "mode" not in state.describe()


def test_a_change_reads_the_device_again_instead_of_trusting_the_cache():
    bulb = FakeBulb()
    _apply_sync(bulb, LightChange(power=True))
    assert "status" in bulb.calls


def test_colour_on_a_light_without_colour_support_is_rejected():
    bulb = FakeBulb(capabilities=("colourtemp",))
    with pytest.raises(LightUnsupported):
        _apply_sync(bulb, LightChange(color="ff0000"))


def test_temperature_on_a_fixed_white_light_is_rejected():
    bulb = FakeBulb(capabilities=("colour",))
    with pytest.raises(LightUnsupported):
        _apply_sync(bulb, LightChange(color_temp_pct=50))


def test_a_bad_hex_colour_is_rejected():
    with pytest.raises(LightError, match="6-digit hex"):
        _apply_sync(FakeBulb(), LightChange(color="reddish"))


def test_device_errors_surface_as_light_errors():
    bulb = FakeBulb()
    bulb.turn_on = lambda: {"Error": "Network Error: Device Unreachable"}
    with pytest.raises(LightError, match="Unreachable"):
        _apply_sync(bulb, LightChange(power=True))


def test_brightness_never_asks_for_zero():
    bulb = FakeBulb()
    _apply_sync(bulb, LightChange(brightness_pct=0))
    assert "brightness:1" in bulb.calls
