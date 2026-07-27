"""A visible sign that Pot is still working.

Tools run without blocking speech, so Pot finishes a sentence, the mic reopens, and the terminal
says "listening" while a web search is still in flight. That reads as finished when it isn't.
This keeps a spinner up for as long as anything is running, and says so in words that make clear
you can still interrupt.
"""

from __future__ import annotations

import time

from rich.console import Console

from pot.console import console as shared_console

# Spoken-friendly names for the spinner: "researching" reads better than "research".
ACTIVITY_LABELS = {
    "research": "researching",
    "play_music": "starting music",
    "pause_music": "pausing music",
    "resume_music": "resuming music",
    "skip_track": "skipping",
    "set_music_volume": "changing volume",
    "now_playing": "checking what's playing",
    "list_speakers": "finding speakers",
    "set_lights": "adjusting the lights",
    "list_lights": "checking the lights",
    "apply_scene": "setting the scene",
    "set_timer": "setting a timer",
    "remind_me": "setting a reminder",
    "list_timers": "checking timers",
    "cancel_timer": "cancelling",
    "current_time": "checking the time",
    "capabilities": "checking what I can do",
}
# Below this, a tool finished so fast that reporting its duration is noise.
WORTH_TIMING_SECONDS = 1.0


def label_for(tool_name: str) -> str:
    return ACTIVITY_LABELS.get(tool_name, tool_name.replace("_", " "))


class ToolActivity:
    """Tracks in-flight tools and shows them as a spinner."""

    def __init__(self, console: Console | None = None) -> None:
        self._console = console or shared_console
        self._started: dict[str, float] = {}
        self._status = None

    @property
    def busy(self) -> bool:
        return bool(self._started)

    def start(self, tool_name: str) -> None:
        self._started[tool_name] = time.monotonic()
        self._refresh()

    def finish(self, tool_name: str) -> None:
        started = self._started.pop(tool_name, None)
        self._refresh()
        if started is None:
            return
        elapsed = time.monotonic() - started
        if elapsed >= WORTH_TIMING_SECONDS:
            self._console.print(f"[dim]{label_for(tool_name)} done in {elapsed:.1f}s[/dim]")

    def stop(self) -> None:
        self._started.clear()
        self._refresh()

    def _refresh(self) -> None:
        if not self._started:
            if self._status is not None:
                self._status.stop()
                self._status = None
            return
        text = f"{', '.join(label_for(name) for name in self._started)} · you can still talk"
        if self._status is None:
            self._status = self._console.status(f"[yellow]{text}[/yellow]", spinner="dots")
            self._status.start()
        else:
            self._status.update(f"[yellow]{text}[/yellow]")
