from __future__ import annotations

import json
from pathlib import Path

from pot.config import load_settings, normalize

POT_TOML = """
[assistant]
name = "Pot"

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


def test_local_keys_are_merged_from_devices_json(tmp_path: Path):
    config = tmp_path / "pot.toml"
    config.write_text(POT_TOML)
    devices = tmp_path / "devices.json"
    devices.write_text(json.dumps([{"id": "gw-1", "key": "secret-key", "version": "3.5"}]))

    settings = load_settings(config, devices)
    device = settings.lights.devices["beta1"]
    assert device.local_key == "secret-key"
    assert device.version == 3.5
    assert settings.voice.voice == "cedar"
    assert settings.lights.scenes["night"]["beta1"].brightness_pct == 10


def test_missing_devices_json_leaves_keys_empty(tmp_path: Path):
    config = tmp_path / "pot.toml"
    config.write_text(POT_TOML)
    settings = load_settings(config, tmp_path / "nope.json")
    assert settings.lights.devices["beta1"].local_key is None


def test_defaults_apply_without_a_config_file(tmp_path: Path):
    settings = load_settings(tmp_path / "absent.toml", tmp_path / "absent.json")
    assert settings.assistant.name == "Pot"
    assert settings.voice.model == "gpt-realtime-2.1-mini"
    assert settings.lights.devices == {}


def test_match_terms_cover_name_room_and_aliases(tmp_path: Path):
    config = tmp_path / "pot.toml"
    config.write_text(POT_TOML)
    device = load_settings(config, tmp_path / "absent.json").lights.devices["beta1"]
    assert {"beta1", "beta 1", "tube", "bedroom", "bedroom light"} <= device.match_terms()


def test_normalize_strips_punctuation_and_case():
    assert normalize("  Beta-1's Light!  ") == "beta1s light"
