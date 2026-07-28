"""Rate conversion, which is only correct if it's continuous.

Both bugs this pins were audible: converting chunk by chunk buzzes at the chunk rate, and a
converter that is never closed keeps the last few tens of milliseconds of every reply.
"""

from __future__ import annotations

import numpy as np
import soxr

from pot.audio.resample import StreamResampler

SOURCE = 24000
TARGET = 48000


def speech_like(seconds: float) -> np.ndarray:
    t = np.arange(int(SOURCE * seconds)) / SOURCE
    wave = np.sin(2 * np.pi * 180 * t) + 0.5 * np.sin(2 * np.pi * 1400 * t)
    return (wave * 8000).astype(np.int16)


def feed_in_chunks(resampler: StreamResampler, samples: np.ndarray, chunk: int) -> np.ndarray:
    out = bytearray()
    for start in range(0, len(samples), chunk):
        out.extend(resampler.feed(samples[start : start + chunk].tobytes()))
    out.extend(resampler.close())
    return np.frombuffer(bytes(out), dtype=np.int16)


def test_nothing_is_dropped_or_invented():
    samples = speech_like(1.0)
    converted = feed_in_chunks(StreamResampler(SOURCE, TARGET), samples, chunk=480)
    assert abs(len(converted) - 2 * len(samples)) < TARGET * 0.01, "a second in is a second out"


def test_closing_gives_back_the_tail():
    """Without this the end of every reply sits in the converter until Pot next speaks."""
    resampler = StreamResampler(SOURCE, TARGET)
    fed = len(resampler.feed(speech_like(0.02).tobytes()))
    assert fed == 0, "the converter is still filling up, which is the whole problem"
    assert len(resampler.close()) > 0


def test_the_converter_is_reusable_after_closing():
    resampler = StreamResampler(SOURCE, TARGET)
    first = feed_in_chunks(resampler, speech_like(0.2), chunk=480)
    second = feed_in_chunks(resampler, speech_like(0.2), chunk=480)
    assert len(second) == len(first)


def test_chunking_does_not_change_the_result():
    """Converting each chunk on its own leaves a step at every boundary, and that's the buzz."""
    samples = speech_like(0.5)
    streamed = feed_in_chunks(StreamResampler(SOURCE, TARGET), samples, chunk=480)
    one_shot = soxr.resample(samples, SOURCE, TARGET).astype(np.int16)

    length = min(len(streamed), len(one_shot))
    difference = streamed[:length].astype(np.float64) - one_shot[:length].astype(np.float64)
    error_db = 10 * np.log10(np.mean(difference**2) / np.mean(one_shot[:length].astype(np.float64) ** 2))
    assert error_db < -60, f"streaming differs from one-shot by {error_db:.0f} dB"
