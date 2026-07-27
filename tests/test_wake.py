"""Wake word framing and the gate rule that decides what Pot costs at rest.

openWakeWord needs 16 kHz frames of exactly 1280 samples; the session runs at 24 kHz. Getting
that conversion wrong doesn't crash, it just quietly stops detecting, so it's pinned here.
"""

from __future__ import annotations

import numpy as np
import pytest

from pot.audio.wake import DETECTOR_RATE, FRAME_SAMPLES, OpenWakeWordEngine, WakeWord, available

pytestmark = pytest.mark.skipif(not available(), reason="wake word extra not installed")

CHUNK_MS = 20
SESSION_RATE = 24000


def silence(milliseconds: int, rate: int = SESSION_RATE) -> bytes:
    return np.zeros(int(rate * milliseconds / 1000), dtype=np.int16).tobytes()


class RecordingWake(WakeWord):
    """A real detector with scoring swapped out, so framing and debounce can be inspected."""

    def __init__(self, models=("hey_mycroft",), **kwargs) -> None:
        super().__init__(OpenWakeWordEngine(models), **kwargs)
        self.frames: list[np.ndarray] = []
        self.scores: list[float] = []

    def _score(self, frame: np.ndarray) -> float:
        self.frames.append(frame)
        return self.scores.pop(0) if self.scores else 0.0


@pytest.fixture
def wake() -> RecordingWake:
    return RecordingWake(["hey_mycroft"], threshold=0.5, sample_rate=SESSION_RATE)


def test_the_real_model_loads_over_onnx(wake: RecordingWake):
    """The library defaults to tflite, whose runtime is deprecated and ARM-hostile."""
    assert wake.model_names == ["hey_mycroft"]


def test_several_triggers_can_run_at_once():
    """Any of them firing is enough, which is the cheap way to be heard more reliably."""
    several = RecordingWake(["hey_mycroft", "alexa"], sample_rate=SESSION_RATE)
    assert several.model_names == ["hey_mycroft", "alexa"]
    assert several.spoken == "hey mycroft or alexa"


def test_a_single_name_is_accepted_as_well():
    assert RecordingWake("alexa", sample_rate=SESSION_RATE).model_names == ["alexa"]


def test_frames_are_exactly_what_the_model_expects(wake: RecordingWake):
    wake.feed(silence(500))
    assert wake.frames, "half a second should produce frames"
    assert all(frame.shape == (FRAME_SAMPLES,) for frame in wake.frames)
    assert all(frame.dtype == np.int16 for frame in wake.frames)


def test_audio_is_resampled_to_the_detector_rate(wake: RecordingWake):
    """500 ms at 16 kHz is 8000 samples, about six 1280-sample frames."""
    wake.feed(silence(500))
    total = sum(frame.shape[0] for frame in wake.frames)
    expected = DETECTOR_RATE * 0.5
    assert abs(total - expected) < FRAME_SAMPLES * 2


def test_short_chunks_accumulate_instead_of_being_dropped(wake: RecordingWake):
    """Mic chunks are 20 ms, far shorter than a frame, so they must buffer."""
    for _ in range(10):
        wake.feed(silence(CHUNK_MS))
    assert wake.frames, "200 ms of 20 ms chunks must add up to at least one frame"


def test_a_high_score_fires_once_then_cools_down(wake: RecordingWake):
    wake.scores = [0.9] * 20
    assert wake.feed(silence(200)) is True
    # The same utterance keeps scoring high; a second trigger would double-open the mic.
    assert wake.feed(silence(200)) is False


def test_a_low_score_does_not_fire(wake: RecordingWake):
    wake.scores = [0.2] * 20
    assert wake.feed(silence(500)) is False


def test_reset_clears_buffered_audio(wake: RecordingWake):
    wake.feed(silence(10))  # not yet a full frame
    wake.reset()
    wake.feed(silence(10))
    assert not wake.frames, "buffered audio from before the reset must not complete a frame"


def test_reset_also_lifts_the_debounce(wake: RecordingWake):
    """After a conversation ends, the next wake word must work straight away."""
    wake.scores = [0.9] * 20
    assert wake.feed(silence(200)) is True
    wake.reset()
    wake.scores = [0.9] * 20
    assert wake.feed(silence(200)) is True


def test_no_resampler_when_already_at_the_detector_rate():
    wake = RecordingWake(["hey_mycroft"], sample_rate=DETECTOR_RATE)
    wake.feed(silence(200, rate=DETECTOR_RATE))
    total = sum(frame.shape[0] for frame in wake.frames)
    assert abs(total - DETECTOR_RATE * 0.2) < FRAME_SAMPLES
