"""The seam between the detector and whatever scores the audio.

The detector owns resampling, framing and debounce so an engine only has to score. These pin that
contract, including the frame size being the engine's to declare: feeding the wrong size doesn't
crash, it just silently never detects anything.
"""

from __future__ import annotations

import numpy as np

from pot.audio.wake import WakeEngine, WakeWord, triggers_for
from pot.config import WakeConfig


class FakeEngine:
    """Fires on the Nth frame, with a frame size the detector must respect."""

    name = "fake"

    def __init__(self, frame_samples: int = 512, fire_on_frame: int | None = None) -> None:
        self._frame_samples = frame_samples
        self.fire_on_frame = fire_on_frame
        self.frames_seen = 0
        self.resets = 0

    @property
    def names(self) -> list[str]:
        return ["trigger_one", "trigger_two"]

    @property
    def frame_samples(self) -> int:
        return self._frame_samples

    def scores(self, frame: np.ndarray) -> dict[str, float]:
        self.frames_seen += 1
        hit = self.frames_seen == self.fire_on_frame
        return {"trigger_one": 1.0 if hit else 0.0, "trigger_two": 0.0}

    def reset(self) -> None:
        self.resets += 1


def audio_at(rate: int, milliseconds: int) -> bytes:
    return np.zeros(int(rate * milliseconds / 1000), dtype=np.int16).tobytes()


def test_the_detector_respects_the_engines_frame_size():
    """openWakeWord wants 1280; another engine may want 512. The engine decides, not the pump."""
    engine = FakeEngine(frame_samples=512)
    wake = WakeWord(engine, sample_rate=16000)
    wake.feed(audio_at(16000, 320))  # 5120 samples = exactly 10 frames of 512
    assert engine.frames_seen == 10


def test_a_larger_frame_size_means_fewer_calls():
    engine = FakeEngine(frame_samples=1280)
    WakeWord(engine, sample_rate=16000).feed(audio_at(16000, 320))
    assert engine.frames_seen == 4


def test_firing_reports_which_trigger_it_was():
    engine = FakeEngine(frame_samples=512, fire_on_frame=3)
    wake = WakeWord(engine, threshold=0.5, sample_rate=16000)
    assert wake.feed(audio_at(16000, 320)) is True
    assert wake.last_model == "trigger_one"


def test_a_verdict_style_engine_still_honours_the_threshold():
    """An engine that answers 1.0 or 0.0 rather than a score must still get through."""
    engine = FakeEngine(frame_samples=512, fire_on_frame=2)
    wake = WakeWord(engine, threshold=0.99, sample_rate=16000)
    assert wake.feed(audio_at(16000, 320)) is True


def test_reset_reaches_the_engine():
    engine = FakeEngine()
    WakeWord(engine, sample_rate=16000).reset()
    assert engine.resets == 1


def test_spoken_form_is_readable_for_both_naming_styles():
    engine = FakeEngine()
    assert WakeWord(engine, sample_rate=16000).spoken == "trigger one or trigger two"


def test_the_fake_satisfies_the_engine_protocol():
    assert isinstance(FakeEngine(), WakeEngine)


def test_the_default_trigger_is_used_when_none_are_named():
    """An empty models list should mean "the usual word", not "no wake word"."""
    assert triggers_for(WakeConfig()) == ["hey_mycroft"]
    assert triggers_for(WakeConfig(models=["alexa"])) == ["alexa"]
    assert triggers_for(WakeConfig(models="~/hey_pot.onnx")) == ["~/hey_pot.onnx"]
