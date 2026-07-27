"""Earcons: the audio-only version of the terminal's status lines.

On the target device there is no screen, so "listening", the working spinner and error messages
have to be sounds. These are deliberately short, quiet and low-information: a rising blip when
Pot starts listening, a falling one when it stops, a soft double tick every few seconds while a
tool is still running, and a low buzz when something broke.

Cues are only played when Pot isn't speaking. The Speaker mixes nothing: writes are appended, so
a cue during speech would delay the reply rather than sound under it.
"""

from __future__ import annotations

import logging
import math
import struct
import time

from pot.audio.playback import Speaker

log = logging.getLogger(__name__)

FADE_SECONDS = 0.006  # clicks are worse than the cue itself; keep the edges soft
WORKING_TICK_SECONDS = 5.0


def tone(
    frequencies: list[float],
    *,
    sample_rate: int,
    seconds: float = 0.08,
    volume: float = 0.2,
    gap_seconds: float = 0.04,
) -> bytes:
    """Sine beeps in sequence, faded at both ends, as pcm16."""
    samples: list[int] = []
    fade = max(1, int(sample_rate * FADE_SECONDS))
    for index, frequency in enumerate(frequencies):
        count = int(sample_rate * seconds)
        for position in range(count):
            envelope = min(1.0, position / fade, (count - position) / fade)
            value = math.sin(2 * math.pi * frequency * position / sample_rate)
            samples.append(int(max(-1.0, min(1.0, value * envelope * volume)) * 32767))
        if index != len(frequencies) - 1:
            samples.extend([0] * int(sample_rate * gap_seconds))
    return struct.pack(f"<{len(samples)}h", *samples)


class Cues:
    """Speaks the interface state, for a device with no screen."""

    def __init__(self, speaker: Speaker, *, sample_rate: int, volume: float = 0.2, enabled: bool = True):
        self._speaker = speaker
        self.enabled = enabled
        rate, level = sample_rate, volume
        self._ready = tone([660, 880], sample_rate=rate, volume=level)
        self._closed = tone([880, 590], sample_rate=rate, volume=level)
        self._working = tone(
            [760, 760], sample_rate=rate, seconds=0.035, volume=level * 0.5, gap_seconds=0.07
        )
        self._error = tone([300, 220], sample_rate=rate, seconds=0.16, volume=level)
        self._last_working = 0.0

    def ready(self) -> None:
        """Pot is listening."""
        self._play(self._ready)

    def closed(self) -> None:
        """Pot stopped listening."""
        self._play(self._closed)

    def error(self) -> None:
        self._play(self._error)

    def working(self) -> None:
        """A quiet reminder that a tool is still running. Rate-limited, not per call."""
        now = time.monotonic()
        if now - self._last_working < WORKING_TICK_SECONDS:
            return
        if self._play(self._working):
            self._last_working = now

    def reset_working(self) -> None:
        self._last_working = 0.0

    def _play(self, pcm: bytes) -> bool:
        # Appending during speech would push the cue out after the reply, so skip it instead.
        if not self.enabled or self._speaker.is_playing:
            return False
        self._speaker.write(pcm)
        return True
