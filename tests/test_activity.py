"""The "still working" indicator.

Tools run without blocking speech, so Pot finishes its sentence and the mic reopens while a web
search is still going. Without this the terminal looked idle and the work was invisible.
"""

from __future__ import annotations

from rich.console import Console

from pot.skills import discover
from pot.voice.activity import ACTIVITY_LABELS, ToolActivity, label_for


def activity() -> tuple[ToolActivity, Console]:
    console = Console(force_terminal=False, no_color=True, width=100)
    return ToolActivity(console), console


def test_busy_while_a_tool_is_running():
    tracker, _ = activity()
    assert not tracker.busy
    tracker.start("research")
    assert tracker.busy
    tracker.finish("research")
    assert not tracker.busy


def test_two_tools_at_once_both_have_to_finish():
    tracker, _ = activity()
    tracker.start("research")
    tracker.start("set_lights")
    tracker.finish("set_lights")
    assert tracker.busy, "the search is still running"
    tracker.finish("research")
    assert not tracker.busy


def test_finishing_an_unknown_tool_is_harmless():
    tracker, _ = activity()
    tracker.finish("never_started")
    assert not tracker.busy


def test_stop_clears_everything():
    tracker, _ = activity()
    tracker.start("research")
    tracker.stop()
    assert not tracker.busy


def test_labels_read_like_english():
    assert label_for("research") == "researching"
    assert label_for("set_music_volume") == "changing volume"
    # An unmapped tool still has to be readable rather than snake_case.
    assert label_for("some_new_tool") == "some new tool"


def test_every_tool_has_a_label():
    """A new skill's tools should show a phrase, not a function name, on the spinner."""
    tool_names = {tool.name for skill in discover().values() for tool in skill.tools}
    missing = sorted(tool_names - set(ACTIVITY_LABELS))
    assert not missing, f"add spinner labels for: {missing}"
