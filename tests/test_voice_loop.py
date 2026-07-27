"""Mic gating and speech state across a turn.

The mic has to shut while Pot speaks (a laptop's speakers otherwise interrupt it) and reopen
afterwards, or every single turn needs a keypress. The same moment drives ducking, so "Pot
stopped talking" must not fire until playback has actually drained.
"""

from __future__ import annotations

import asyncio

from pot.audio.capture import MicrophoneGate
from pot.config import Settings
from pot.runtime import Runtime
from pot.voice.activity import ToolActivity
from pot.voice.session import _finish_speaking


class FakeSpeaker:
    def __init__(self, playing_for: float = 0.0) -> None:
        self._until = asyncio.get_event_loop().time() + playing_for

    @property
    def is_playing(self) -> bool:
        return asyncio.get_event_loop().time() < self._until


async def test_mic_reopens_once_playback_has_drained():
    gate = MicrophoneGate()
    gate.close()
    speaker = FakeSpeaker(playing_for=0.2)

    task = asyncio.create_task(
        _finish_speaking(speaker, gate, Runtime(Settings()), reopen_mic=True, settle_seconds=0.01)
    )
    await asyncio.sleep(0.1)
    assert not gate.is_open, "must not reopen while Pot is still speaking"
    await task
    assert gate.is_open


async def test_mic_reopens_immediately_when_nothing_is_playing():
    gate = MicrophoneGate()
    await _finish_speaking(FakeSpeaker(), gate, Runtime(Settings()), reopen_mic=True, settle_seconds=0.01)
    assert gate.is_open


async def test_reopen_gives_up_rather_than_waiting_forever():
    gate = MicrophoneGate()
    await _finish_speaking(
        FakeSpeaker(playing_for=60.0),
        gate,
        Runtime(Settings()),
        reopen_mic=True,
        settle_seconds=0.01,
        timeout=0.1,
    )
    assert gate.is_open, "a stuck playback buffer must not strand the mic shut"


async def test_a_cancelled_reopen_leaves_the_mic_shut():
    gate = MicrophoneGate()
    task = asyncio.create_task(
        _finish_speaking(FakeSpeaker(playing_for=5.0), gate, Runtime(Settings()), reopen_mic=True)
    )
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not gate.is_open


async def test_hands_free_mode_leaves_the_gate_alone_but_still_ends_the_turn():
    gate = MicrophoneGate(open_at_start=True)
    runtime = Runtime(Settings())
    runtime.set_speaking(True)
    await _finish_speaking(FakeSpeaker(), gate, runtime, reopen_mic=False, settle_seconds=0.01)
    assert gate.is_open
    assert runtime.speaking is False


async def test_speech_state_only_clears_after_playback_drains():
    """Ducking hangs off this: unducking early would raise the music mid-sentence."""
    runtime = Runtime(Settings())
    seen: list[bool] = []
    runtime.on_speech(seen.append)

    runtime.set_speaking(True)
    task = asyncio.create_task(
        _finish_speaking(
            FakeSpeaker(playing_for=0.2), MicrophoneGate(), runtime, reopen_mic=False, settle_seconds=0.01
        )
    )
    await asyncio.sleep(0.1)
    assert seen == [True], "still speaking while audio is playing"
    await task
    assert seen == [True, False]


async def test_the_mic_reopens_quietly_while_a_tool_is_still_running():
    """Printing "listening" mid-search reads as finished; the spinner is already saying more."""
    from rich.console import Console

    printed = Console(force_terminal=False, no_color=True, record=True, width=100)
    activity = ToolActivity(printed)
    activity.start("research")
    gate = MicrophoneGate()
    await _finish_speaking(
        FakeSpeaker(), gate, Runtime(Settings()), activity, reopen_mic=True, settle_seconds=0.01
    )
    assert gate.is_open, "you must still be able to interrupt"


async def test_listening_is_announced_once_nothing_is_running():
    gate = MicrophoneGate()
    activity = ToolActivity()
    await _finish_speaking(
        FakeSpeaker(), gate, Runtime(Settings()), activity, reopen_mic=True, settle_seconds=0.01
    )
    assert gate.is_open and not activity.busy


def test_repeated_speech_states_are_not_republished():
    runtime = Runtime(Settings())
    seen: list[bool] = []
    runtime.on_speech(seen.append)
    runtime.set_speaking(True)
    runtime.set_speaking(True)
    runtime.set_speaking(False)
    assert seen == [True, False]


def test_a_failing_listener_does_not_break_the_broadcast():
    runtime = Runtime(Settings())
    seen: list[bool] = []

    def boom(_: bool) -> None:
        raise RuntimeError("nope")

    runtime.on_speech(boom)
    runtime.on_speech(seen.append)
    runtime.set_speaking(True)
    assert seen == [True]
