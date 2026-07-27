"""Mic gating across a turn.

The mic has to shut while Pot speaks (a laptop's speakers otherwise interrupt it) and reopen
afterwards, or every single turn needs a keypress.
"""

from __future__ import annotations

import asyncio

from pot.audio.capture import MicrophoneGate
from pot.voice.session import _reopen_when_quiet


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

    task = asyncio.create_task(_reopen_when_quiet(speaker, gate, settle_seconds=0.01))
    await asyncio.sleep(0.1)
    assert not gate.is_open, "must not reopen while Pot is still speaking"
    await task
    assert gate.is_open


async def test_mic_reopens_immediately_when_nothing_is_playing():
    gate = MicrophoneGate()
    await _reopen_when_quiet(FakeSpeaker(), gate, settle_seconds=0.01)
    assert gate.is_open


async def test_reopen_gives_up_rather_than_waiting_forever():
    gate = MicrophoneGate()
    speaker = FakeSpeaker(playing_for=60.0)
    await _reopen_when_quiet(speaker, gate, settle_seconds=0.01, timeout=0.1)
    assert gate.is_open, "a stuck playback buffer must not strand the mic shut"


async def test_a_cancelled_reopen_leaves_the_mic_shut():
    gate = MicrophoneGate()
    speaker = FakeSpeaker(playing_for=5.0)
    task = asyncio.create_task(_reopen_when_quiet(speaker, gate))
    await asyncio.sleep(0.05)
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert not gate.is_open
