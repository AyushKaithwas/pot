"""Earcons, which are the entire interface on a device with no screen.

The rules that matter: never play over Pot's own speech (writes append, so a cue during a reply
would arrive after it), don't tick constantly, and keep the waveform free of clicks.
"""

from __future__ import annotations

import struct

from pot.audio.cues import WORKING_TICK_SECONDS, Cues, tone

RATE = 24000


class FakeSpeaker:
    def __init__(self, playing: bool = False) -> None:
        self.playing = playing
        self.written: list[bytes] = []

    @property
    def is_playing(self) -> bool:
        return self.playing

    def write(self, pcm: bytes) -> None:
        self.written.append(pcm)


def samples(pcm: bytes) -> list[int]:
    return list(struct.unpack(f"<{len(pcm) // 2}h", pcm))


def test_tone_is_pcm16_of_the_expected_length():
    pcm = tone([440], sample_rate=RATE, seconds=0.1)
    assert len(samples(pcm)) == int(RATE * 0.1)


def test_tone_starts_and_ends_near_silence_so_it_does_not_click():
    values = samples(tone([440], sample_rate=RATE, seconds=0.1, volume=1.0))
    assert abs(values[0]) < 500
    assert abs(values[-1]) < 500
    assert max(abs(v) for v in values) > 20_000, "and is still audible in between"


def test_volume_scales_the_waveform():
    quiet = max(abs(v) for v in samples(tone([440], sample_rate=RATE, volume=0.1)))
    loud = max(abs(v) for v in samples(tone([440], sample_rate=RATE, volume=0.8)))
    assert loud > quiet * 4


def test_two_beeps_include_the_gap_between_them():
    one = len(samples(tone([440], sample_rate=RATE, seconds=0.05, gap_seconds=0.02)))
    two = len(samples(tone([440, 660], sample_rate=RATE, seconds=0.05, gap_seconds=0.02)))
    assert two == one * 2 + int(RATE * 0.02)


def test_cues_are_played_when_pot_is_quiet():
    speaker = FakeSpeaker()
    Cues(speaker, sample_rate=RATE).ready()
    assert len(speaker.written) == 1


def test_cues_are_skipped_while_pot_is_speaking():
    """A cue appended mid-reply would land after it, which is worse than silence."""
    speaker = FakeSpeaker(playing=True)
    cues = Cues(speaker, sample_rate=RATE)
    cues.ready()
    cues.error()
    cues.working()
    assert speaker.written == []


def test_cues_can_be_switched_off():
    speaker = FakeSpeaker()
    Cues(speaker, sample_rate=RATE, enabled=False).ready()
    assert speaker.written == []


def test_the_working_tick_is_rate_limited():
    speaker = FakeSpeaker()
    cues = Cues(speaker, sample_rate=RATE)
    for _ in range(20):
        cues.working()
    assert len(speaker.written) == 1, "one tick, not twenty"


def test_a_skipped_tick_does_not_start_the_clock():
    """Pot was talking, so nothing sounded; the next quiet moment should tick."""
    speaker = FakeSpeaker(playing=True)
    cues = Cues(speaker, sample_rate=RATE)
    cues.working()
    speaker.playing = False
    cues.working()
    assert len(speaker.written) == 1


def test_a_new_tool_resets_the_tick_clock():
    speaker = FakeSpeaker()
    cues = Cues(speaker, sample_rate=RATE)
    cues.working()
    cues.reset_working()
    cues.working()
    assert len(speaker.written) == 2


def test_listening_and_stopping_sound_different():
    speaker = FakeSpeaker()
    cues = Cues(speaker, sample_rate=RATE)
    cues.ready()
    cues.closed()
    assert speaker.written[0] != speaker.written[1]


def test_the_tick_interval_is_long_enough_not_to_nag():
    assert WORKING_TICK_SECONDS >= 3.0


def test_a_cue_deafens_the_mic_while_it_sounds():
    """Otherwise Pot's own blip comes back as a user turn: transcribed, charged for, answered."""
    muted: list[float] = []
    cues = Cues(FakeSpeaker(), sample_rate=24000, mute=muted.append)
    cues.ready()

    assert muted, "the mic has to be told to ignore what it is about to hear"
    assert 0.2 < muted[0] < 1.5, f"muted for {muted[0]}s: long enough to cover the cue, not a reply"


def test_a_skipped_cue_does_not_deafen_anything():
    muted: list[float] = []
    speaker = FakeSpeaker()
    speaker.playing = True
    Cues(speaker, sample_rate=24000, mute=muted.append).ready()
    assert muted == [], "no cue was played, so there is nothing to ignore"
