"""The voice loop opens the mic and speaker with `async with`, so both must be async.

This is here because a sync-only Speaker crashed `pot talk` at startup while every other
test still passed: nothing else in the suite enters those context managers.
"""

from __future__ import annotations

import asyncio
import inspect

import pytest

from pot.audio.capture import Microphone, MicrophoneGate
from pot.audio.playback import Speaker


@pytest.mark.parametrize("cls", [Speaker, Microphone])
def test_audio_objects_are_async_context_managers(cls):
    assert inspect.iscoroutinefunction(cls.__aenter__)
    assert inspect.iscoroutinefunction(cls.__aexit__)


async def test_speaker_plays_and_flushes():
    speaker = Speaker(sample_rate=24000)
    async with speaker:
        speaker.write(b"\x00\x00" * 2400)
        assert speaker.is_playing
        speaker.flush()
        assert not speaker.is_playing


def test_gate_starts_closed_for_push_to_talk_and_toggles():
    gate = MicrophoneGate()
    assert not gate.is_open
    assert gate.toggle() is True
    assert gate.is_open
    gate.close()
    assert not gate.is_open


def test_gate_can_start_open_for_always_on():
    assert MicrophoneGate(open_at_start=True).is_open


async def test_playback_waits_for_a_little_audio_before_starting():
    """Playing the first burst instantly is what leaves the device starved on the second one."""
    speaker = Speaker(sample_rate=24000, prebuffer_ms=100)
    async with speaker:
        speaker.write(b"\x00\x00" * 240)  # 10ms, well under the pre-buffer
        assert speaker.is_playing, "it's queued"
        await asyncio.sleep(0.15)
        assert speaker.underruns == 0, "a short write must not be reported as starvation"


async def test_a_full_buffer_plays_without_underruns():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=60)
    async with speaker:
        speaker.write(b"\x00\x00" * 24000)  # a full second
        await asyncio.sleep(0.4)
    assert speaker.underruns == 0


def test_flush_disarms_playback_so_the_next_burst_rebuffers():
    speaker = Speaker(sample_rate=24000, prebuffer_ms=60)
    speaker.write(b"\x00\x00" * 2400)
    speaker.flush()
    assert not speaker.is_playing
