"""The systemd unit, and refusing to start in a state where Pot could never hear you."""

from __future__ import annotations

import sys

import pytest

from pot import service
from pot.config import AudioConfig
from pot.voice.session import _check_input_is_reachable


def test_the_unit_runs_pot_and_restarts_it():
    unit = service.render_unit()
    assert "ExecStart=" in unit and unit.rstrip().endswith("WantedBy=default.target")
    assert "Restart=always" in unit
    assert "talk" in unit


def test_the_unit_waits_for_network_and_sound():
    """Starting before either exists is the classic way a boot-time assistant comes up deaf."""
    unit = service.render_unit()
    assert "network-online.target" in unit
    assert "sound.target" in unit


def test_logs_go_to_the_journal_since_there_is_no_terminal():
    unit = service.render_unit()
    assert "StandardOutput=journal" in unit
    assert "StandardError=journal" in unit


def test_it_is_a_user_unit_so_nothing_needs_root():
    assert service.unit_directory().parts[-3:] == (".config", "systemd", "user")
    assert "User=" not in service.render_unit(), "a user unit already runs as you"


def test_enabling_is_left_to_the_user():
    commands = service.enable_commands()
    assert any("enable --now" in command for command in commands)
    assert sum("sudo" in command for command in commands) == 1, "only linger should need sudo"


def test_the_journal_command_targets_pots_unit():
    assert service.journal(lines=10) == [
        "journalctl",
        "--user",
        "-u",
        "pot.service",
        "-n",
        "10",
    ]
    assert "-f" in service.journal(follow=True)


def test_support_is_reported_honestly():
    assert service.is_supported() is sys.platform.startswith("linux")


def test_push_to_talk_without_a_terminal_is_refused():
    """Under systemd this mode can never receive the keypress it waits for."""
    if sys.stdin.isatty():
        pytest.skip("this only bites when stdin is not a tty, which is how tests run in CI")
    with pytest.raises(RuntimeError, match="wake_word"):
        _check_input_is_reachable(AudioConfig(mode="push_to_talk"))


def test_hands_free_modes_are_fine_without_a_terminal():
    _check_input_is_reachable(AudioConfig(mode="always_on"))
    _check_input_is_reachable(AudioConfig(mode="wake_word"))
