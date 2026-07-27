"""Local wake word detection, so a screenless device needs no keypress.

This also decides what Pot costs at rest. The microphone always runs, but audio only reaches
OpenAI after the wake word fires, so an idle room is free.

openWakeWord wants 16 kHz frames of exactly 1280 samples while the session runs at 24 kHz, so
this owns the resampling and the framing. It is deliberately forced onto ONNX: the library's
default is tflite, whose runtime is deprecated, capped at Python 3.11 and awkward on ARM.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import soxr

log = logging.getLogger(__name__)

DETECTOR_RATE = 16000
FRAME_SAMPLES = 1280  # 80 ms, the window openWakeWord's models expect
BYTES_PER_SAMPLE = 2
# One "hey jarvis" shouldn't fire twice, and a conversation shouldn't re-trigger on itself.
COOLDOWN_SECONDS = 2.0

INSTALL_HINT = "install the wake word extra: uv sync --extra wakeword"


class WakeWordUnavailable(RuntimeError):
    """openWakeWord isn't installed, or its models aren't on disk."""


def available() -> bool:
    try:
        import openwakeword  # noqa: F401
    except ImportError:
        return False
    return True


def stock_models() -> list[str]:
    """The pre-trained triggers, e.g. hey_jarvis. Custom models come later as file paths."""
    if not available():
        return []
    import openwakeword

    return sorted(openwakeword.MODELS)


def model_files(model: str) -> list[Path]:
    """The ONNX files a given wake word needs: its own, plus the shared feature extractors."""
    import openwakeword

    entry = openwakeword.MODELS.get(model)
    if entry is None:
        known = ", ".join(sorted(openwakeword.MODELS))
        raise WakeWordUnavailable(f"unknown wake word {model!r} (available: {known})")
    paths = [Path(entry["model_path"])]
    paths += [Path(feature["model_path"]) for feature in openwakeword.FEATURE_MODELS.values()]
    # The registry points at the tflite variants; the ONNX ones sit alongside.
    return [path.with_suffix(".onnx") for path in paths]


def is_downloaded(*models: str) -> bool:
    if not available():
        return False
    try:
        return all(path.exists() for model in models for path in model_files(model))
    except WakeWordUnavailable:
        return False


def download(*models: str) -> None:
    """Fetch the model files, about 5 MB each. Needs network, once."""
    from openwakeword.utils import download_models

    for model in models:
        log.info("downloading wake word model %s", model)
        download_models([model])


class WakeWord:
    """Listens for one or more trigger phrases, firing on whichever is heard.

    Several models cost almost nothing (about 1 ms per 80 ms frame each) and meaningfully improve
    the hit rate, since the stock models differ a lot in how reliably they catch a given voice.
    """

    def __init__(
        self,
        models: Sequence[str] | str = ("hey_mycroft",),
        *,
        threshold: float = 0.5,
        sample_rate: int = 24000,
        download_if_missing: bool = True,
    ) -> None:
        if not available():
            raise WakeWordUnavailable(f"openWakeWord is not installed. {INSTALL_HINT}")
        from openwakeword.model import Model

        self.model_names = [models] if isinstance(models, str) else list(models)
        if not self.model_names:
            raise WakeWordUnavailable("no wake word configured")
        self.threshold = threshold
        self.last_score = 0.0
        self.last_model = ""

        missing = [name for name in self.model_names if not is_downloaded(name)]
        if missing:
            if not download_if_missing:
                raise WakeWordUnavailable(f"wake word model(s) not downloaded: {', '.join(missing)}")
            download(*missing)

        # A copy, because Model rewrites the list it's given into resolved file paths in place.
        self._model = Model(wakeword_models=list(self.model_names), inference_framework="onnx")
        self._resampler = (
            soxr.ResampleStream(sample_rate, DETECTOR_RATE, 1, dtype="int16")
            if sample_rate != DETECTOR_RATE
            else None
        )
        self._buffer = bytearray()
        self._muted_until = 0.0

    def feed(self, pcm: bytes) -> bool:
        """Push mic audio in. True means the wake word just fired."""
        if self._resampler is not None:
            samples = np.frombuffer(pcm, dtype=np.int16)
            pcm = self._resampler.resample_chunk(samples).tobytes()
        self._buffer.extend(pcm)

        detected = False
        frame_bytes = FRAME_SAMPLES * BYTES_PER_SAMPLE
        while len(self._buffer) >= frame_bytes:
            frame = np.frombuffer(bytes(self._buffer[:frame_bytes]), dtype=np.int16)
            del self._buffer[:frame_bytes]
            self.last_score = self._score(frame)
            if self._should_fire():
                detected = True
        return detected

    def reset(self) -> None:
        """Forget everything heard so far, so the next listen starts clean.

        That includes the debounce: it exists to stop one utterance firing twice, not to make the
        wake word unavailable for two seconds after a conversation ends.
        """
        self._buffer.clear()
        self.last_score = 0.0
        self._muted_until = 0.0
        reset = getattr(self._model, "reset", None)
        if reset is not None:
            reset()

    @property
    def spoken(self) -> str:
        return " or ".join(name.replace("_", " ") for name in self.model_names)

    def scores(self, frame: np.ndarray) -> dict[str, float]:
        """Per-model scores for one frame, for tuning and diagnostics."""
        raw = self._model.predict(frame)
        return {name: float(raw.get(name, 0.0)) for name in self.model_names}

    def _score(self, frame: np.ndarray) -> float:
        """The best score across the configured triggers. Overridden in tests."""
        scores = self.scores(frame)
        self.last_model = max(scores, key=scores.get) if scores else ""
        return max(scores.values(), default=0.0)

    def _should_fire(self) -> bool:
        """Threshold plus debounce, kept out of scoring so both are always applied."""
        if self.last_score < self.threshold:
            return False
        now = time.monotonic()
        if now < self._muted_until:
            return False
        self._muted_until = now + COOLDOWN_SECONDS
        log.info("wake word %s fired at %.2f", self.last_model or self.model_names[0], self.last_score)
        return True
