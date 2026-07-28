"""Rate conversion between the model's 24 kHz and whatever the hardware is running.

Streaming rather than chunk by chunk: converting each chunk on its own leaves a discontinuity at
every boundary, and a discontinuity that repeats at the chunk rate is a buzz. The cost of doing it
properly is that the converter keeps a few tens of milliseconds in hand, which has to be pushed out
at the end of an utterance or it takes the last syllable with it.
"""

from __future__ import annotations

import numpy as np
import soxr

EMPTY = np.zeros(0, dtype=np.int16)


class StreamResampler:
    """A continuous pcm16 rate converter with an explicit end of stream."""

    def __init__(self, source_rate: int, target_rate: int) -> None:
        self.source_rate = source_rate
        self.target_rate = target_rate
        self._stream = self._new_stream()

    def _new_stream(self) -> soxr.ResampleStream:
        return soxr.ResampleStream(self.source_rate, self.target_rate, 1, dtype="int16")

    def feed(self, pcm16: bytes) -> bytes:
        """Convert a chunk. Returns b"" while the converter is still filling up."""
        samples = np.frombuffer(pcm16, dtype=np.int16)
        return self._stream.resample_chunk(samples).tobytes()

    def close(self) -> bytes:
        """Everything still held, after which the converter is ready for a new stream."""
        tail = self._stream.resample_chunk(EMPTY, last=True).tobytes()
        self._stream = self._new_stream()
        return tail
