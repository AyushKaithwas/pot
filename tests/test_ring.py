"""The ring's end has to be reported, not just silent.

An alarm that rings out unanswered used to leave "something is ringing" set forever: mic open,
music ducked, the next alarm blocked. The callback is what lets the owner clean that up.
"""

from __future__ import annotations

import asyncio

from pot.audio.ring import Ringer


class FakeSpeaker:
    def __init__(self) -> None:
        self.chimes = 0
        self.chime_stopped = False
        self.voice_flushed = False

    def write_chime(self, pcm: bytes) -> None:
        self.chimes += 1

    def stop_chime(self) -> None:
        self.chime_stopped = True

    def flush(self) -> None:  # pragma: no cover - the assertion is that this is never called
        self.voice_flushed = True


async def test_giving_up_reports_back():
    gave_up = []
    ringer = Ringer(FakeSpeaker(), sample_rate=24000, max_seconds=0.05)
    ringer.start("test", on_gave_up=lambda: gave_up.append(True))
    await ringer.wait()
    assert gave_up, "ringing out unanswered must tell the owner"


async def test_a_dismissed_ring_does_not_also_report_giving_up():
    """stop() is the answered path; firing the callback there would dismiss twice."""
    gave_up = []
    ringer = Ringer(FakeSpeaker(), sample_rate=24000, max_seconds=30)
    ringer.start("test", on_gave_up=lambda: gave_up.append(True))
    await asyncio.sleep(0.05)
    ringer.stop()
    await ringer.wait()
    assert not gave_up


async def test_dismissing_the_ring_never_touches_pots_voice():
    """The dismissal usually arrives while Pot is mid-sentence; cutting it off answers "stop"
    by going silent, which reads as broken rather than obedient."""
    speaker = FakeSpeaker()
    ringer = Ringer(speaker, sample_rate=24000, max_seconds=30)
    ringer.start("test")
    await asyncio.sleep(0.05)
    ringer.stop()

    assert speaker.chime_stopped, "the chime itself has to stop"
    assert not speaker.voice_flushed, "and the voice channel is not the ring's to clear"
