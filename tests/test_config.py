"""Config layering.

Your settings sit on top of the shipped defaults, which is what lets an install keep its own
choices through an update and lets the repo ship no personal device ids at all. The merge has to
be deep: a user setting one thing under [lights] must not wipe the defaults' other tables.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from pot.config import AudioConfig, deep_merge, load_settings, normalize, read_config

DEFAULTS = """
[assistant]
name = "Pot"

[voice]
voice = "ash"
speed = 1.15

[costs.models."gpt-realtime-2.1-mini"]
audio_in = 10.0
audio_out = 20.0
"""

USER = """
[voice]
voice = "cedar"

[lights.devices.beta1]
gwid = "gw-1"
name = "Beta 1"
room = "bedroom"
aliases = ["tube"]

[lights.scenes.night]
beta1 = { power = true, brightness_pct = 10 }
"""


@pytest.fixture
def files(tmp_path: Path) -> tuple[Path, Path]:
    defaults, user = tmp_path / "defaults.toml", tmp_path / "user.toml"
    defaults.write_text(DEFAULTS)
    user.write_text(USER)
    return defaults, user


def test_user_settings_win_over_shipped_ones(files):
    defaults, user = files
    merged = read_config(defaults=defaults, user=user)
    assert merged["voice"]["voice"] == "cedar"


def test_untouched_shipped_settings_survive(files):
    defaults, user = files
    merged = read_config(defaults=defaults, user=user)
    assert merged["voice"]["speed"] == 1.15, "overriding one voice key must not drop the others"
    assert merged["assistant"]["name"] == "Pot"
    assert "gpt-realtime-2.1-mini" in merged["costs"]["models"]


def test_a_missing_user_file_leaves_the_defaults_alone(files, tmp_path: Path):
    defaults, _ = files
    merged = read_config(defaults=defaults, user=tmp_path / "absent.toml")
    assert merged["voice"]["voice"] == "ash"


def test_deep_merge_replaces_scalars_and_lists_but_joins_tables():
    base = {"a": {"keep": 1, "change": 1}, "list": [1, 2], "scalar": "x"}
    overlay = {"a": {"change": 2}, "list": [3], "scalar": "y"}
    assert deep_merge(base, overlay) == {
        "a": {"keep": 1, "change": 2},
        "list": [3],
        "scalar": "y",
    }


def test_lights_come_from_the_user_file_with_keys_merged_in(files, tmp_path: Path):
    defaults, user = files
    keys = tmp_path / "devices.json"
    keys.write_text(json.dumps([{"id": "gw-1", "key": "secret-key", "version": "3.5"}]))

    settings = load_settings(config_path=user, keys_path=keys)
    device = settings.lights.devices["beta1"]
    assert device.local_key == "secret-key"
    assert device.version == 3.5
    assert settings.lights.scenes["night"]["beta1"].brightness_pct == 10


def test_missing_device_keys_leave_the_light_declared_but_unusable(files, tmp_path: Path):
    _, user = files
    settings = load_settings(config_path=user, keys_path=tmp_path / "absent.json")
    assert settings.lights.devices["beta1"].local_key is None


def test_the_shipped_defaults_are_valid_and_carry_no_devices():
    """The repo is going public: it must ship settings, not somebody's house."""
    settings = load_settings(config_path=Path("/nonexistent"), keys_path=Path("/nonexistent"))
    assert settings.voice.model == "gpt-realtime-2.1-mini"
    assert settings.lights.devices == {}, "no personal device ids in the committed config"
    assert settings.music.spotify.client_id == "", "no personal credentials either"
    assert settings.costs.models, "but the rate table should ship"


def test_match_terms_cover_name_room_and_aliases(files, tmp_path: Path):
    _, user = files
    device = load_settings(config_path=user, keys_path=tmp_path / "absent.json").lights.devices["beta1"]
    assert {"beta1", "beta 1", "tube", "bedroom", "bedroom light"} <= device.match_terms()


def test_normalize_strips_punctuation_and_case():
    assert normalize("  Beta-1's Light!  ") == "beta1s light"


def test_the_playback_buffer_can_be_raised_for_a_jittery_network():
    """Measured on a Pi over Wi-Fi: 7ms best, 147ms worst. 120 can't absorb that; 300 can."""
    assert AudioConfig().playback_buffer_ms == 120
    assert AudioConfig(playback_buffer_ms=300).playback_buffer_ms == 300


def test_an_absurd_playback_buffer_is_refused():
    """A buffer measured in seconds would make Pot feel broken rather than smooth."""
    with pytest.raises(ValidationError):
        AudioConfig(playback_buffer_ms=5000)
    with pytest.raises(ValidationError):
        AudioConfig(playback_buffer_ms=0)
