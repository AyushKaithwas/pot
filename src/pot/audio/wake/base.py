"""What a wake word engine has to provide, and the detector that wraps one.

Everything except scoring lives in the detector: resampling from the session's rate, cutting audio
into whatever frame size the engine wants, and the debounce that stops one utterance firing twice.
So an engine is a `scores`-shaped class, and swapping one in doesn't touch the pipeline. There's
one engine today (openWakeWord); the seam exists because a locally trained model or a different
runtime shouldn't require rewriting any of this.
"""

from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

import numpy as np
import soxr

log = logging.getLogger(__name__)

DETECTOR_RATE = 16000  # both engines want 16 kHz mono
BYTES_PER_SAMPLE = 2
# One "hey pot" shouldn't fire twice, and a conversation shouldn't re-trigger on itself.
COOLDOWN_SECONDS = 2.0


class WakeWordUnavailable(RuntimeError):
    """The engine isn't installed, isn't configured, or its models aren't on disk."""


@runtime_checkable
class WakeEngine(Protocol):
    """Scores one frame of 16 kHz audio against one or more trigger phrases."""

    name: str

    @property
    def names(self) -> list[str]:
        """The configured triggers, in the engine's own naming."""
        ...

    @property
    def frame_samples(self) -> int:
        """How many samples per call. Engines are strict about this."""
        ...

    def scores(self, frame: np.ndarray) -> dict[str, float]:
        """Per-trigger confidence for this frame, 0 to 1."""
        ...

    def reset(self) -> None: ...


class WakeWord:
    """Feeds mic audio to an engine and reports when a trigger fires."""

    def __init__(self, engine: WakeEngine, *, threshold: float = 0.5, sample_rate: int = 24000) -> None:
        self.engine = engine
        self.threshold = threshold
        self.last_score = 0.0
        self.last_model = ""
        self._sample_rate = sample_rate
        self._resampler = self._new_resampler()
        self._buffer = bytearray()
        self._muted_until = 0.0

    def _new_resampler(self) -> soxr.ResampleStream | None:
        if self._sample_rate == DETECTOR_RATE:
            return None
        return soxr.ResampleStream(self._sample_rate, DETECTOR_RATE, 1, dtype="int16")

    @property
    def model_names(self) -> list[str]:
        return self.engine.names

    @property
    def spoken(self) -> str:
        """How to tell someone what to say."""
        return " or ".join(name.replace("_", " ").replace(".ppn", "") for name in self.model_names)

    def feed(self, pcm: bytes) -> bool:
        """Push mic audio in. True means a trigger just fired."""
        if self._resampler is not None:
            samples = np.frombuffer(pcm, dtype=np.int16)
            pcm = self._resampler.resample_chunk(samples).tobytes()
        self._buffer.extend(pcm)

        detected = False
        frame_bytes = self.engine.frame_samples * BYTES_PER_SAMPLE
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
        # The resampler holds a tail of whatever was last heard, and "forget everything" that
        # leaks the end of the previous conversation into the next listen isn't forgetting.
        self._resampler = self._new_resampler()
        self.last_score = 0.0
        self._muted_until = 0.0
        self.engine.reset()

    def scores(self, frame: np.ndarray) -> dict[str, float]:
        return self.engine.scores(frame)

    def _score(self, frame: np.ndarray) -> float:
        """The best score across the configured triggers. Overridden in tests."""
        scores = self.engine.scores(frame)
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
