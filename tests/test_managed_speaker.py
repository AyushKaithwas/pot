"""librespot's audio arriving through a pipe rather than going to the sound card.

That routing is the point: music that passes through Pot's mixer is music Pot could eventually
subtract from what its microphone hears. The conversion in between is where it can go wrong, since
librespot speaks 44.1 kHz stereo and the mixer wants the session's mono rate.
"""

from __future__ import annotations

import asyncio

import numpy as np

from pot.config import Settings
from pot.skills.music.spotify.speaker import PIPE_RATE, ManagedSpeaker, _to_mono


class FakeSpeaker:
    source_rate = 24000

    def __init__(self) -> None:
        self.music = bytearray()
        self.stopped = 0

    def write_music(self, pcm16: bytes) -> None:
        self.music.extend(pcm16)

    def stop_music(self) -> None:
        self.stopped += 1


def stereo_tone(seconds: float) -> bytes:
    t = np.arange(int(PIPE_RATE * seconds)) / PIPE_RATE
    left = (np.sin(2 * np.pi * 220 * t) * 8000).astype(np.int16)
    right = (np.sin(2 * np.pi * 220 * t) * 4000).astype(np.int16)
    return np.stack([left, right], axis=1).reshape(-1).tobytes()


def test_stereo_is_averaged_not_half_discarded():
    """Taking only the left channel silences anything panned hard right, and some mixes do that."""
    hard_right = np.stack([np.zeros(4, dtype=np.int16), np.full(4, 8000, dtype=np.int16)], axis=1).reshape(-1)
    assert np.frombuffer(_to_mono(hard_right.tobytes()), dtype=np.int16).max() == 4000


async def test_the_pipe_is_converted_to_the_mixers_rate():
    speaker = FakeSpeaker()
    managed = ManagedSpeaker(Settings(), speaker)

    stream = asyncio.StreamReader()
    audio = stereo_tone(1.0)
    stream.feed_data(audio)
    stream.feed_eof()
    await managed._play_from(stream)

    seconds = len(speaker.music) / 2 / speaker.source_rate
    assert 0.95 < seconds < 1.05, f"a second of audio became {seconds:.2f}s"
    assert np.abs(np.frombuffer(bytes(speaker.music), dtype=np.int16)).max() > 3000


async def test_a_frame_split_across_reads_is_not_lost():
    """Reads land wherever they land, and half a stereo frame would shift every later sample."""
    speaker = FakeSpeaker()
    managed = ManagedSpeaker(Settings(), speaker)

    stream = asyncio.StreamReader()
    audio = stereo_tone(0.5)
    stream.feed_data(audio[:1001])  # deliberately not a whole number of frames
    stream.feed_data(audio[1001:])
    stream.feed_eof()
    await managed._play_from(stream)

    seconds = len(speaker.music) / 2 / speaker.source_rate
    assert 0.45 < seconds < 0.55


async def test_no_mixer_means_no_crash():
    """Pot's speaker can be enabled in a context that has no audio output, e.g. a CLI command."""
    managed = ManagedSpeaker(Settings(), None)
    stream = asyncio.StreamReader()
    stream.feed_eof()
    await managed._play_from(stream)


def test_librespot_is_told_to_pipe_rather_than_pick_a_backend():
    """One less thing to get wrong on a Pi: the pipe is identical on every platform."""
    command = ManagedSpeaker(Settings(), None).command()
    assert command[command.index("--backend") + 1] == "pipe"
    assert "S16" in command, "the mixer reads signed 16-bit, so don't let librespot choose"
