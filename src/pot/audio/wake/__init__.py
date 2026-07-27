"""Wake word detection.

`build` is the only thing callers need: hand it the audio config and it returns a detector, or
raises WakeWordUnavailable with a sentence explaining what's missing.

openWakeWord is the only engine, deliberately. Porcupine detects better, but its library phones
home to validate a licence and fingerprints the machine, and its free tier is non-commercial: both
wrong for an assistant whose whole point is that nothing leaves the room until you speak to it.
The way to close the accuracy gap without giving that up is a model trained on your own voice,
which drops in here as a path to an .onnx file. See docs/custom-wake-word.md.
"""

from __future__ import annotations

import logging

from pot.audio.wake.base import (
    COOLDOWN_SECONDS,
    DETECTOR_RATE,
    WakeEngine,
    WakeWord,
    WakeWordUnavailable,
)
from pot.audio.wake.openwakeword import (
    FRAME_SAMPLES,
    OpenWakeWordEngine,
    available,
    download,
    is_custom,
    is_downloaded,
    stock_models,
)

log = logging.getLogger(__name__)

DEFAULT_MODELS = ("hey_mycroft",)

__all__ = [
    "COOLDOWN_SECONDS",
    "DEFAULT_MODELS",
    "DETECTOR_RATE",
    "FRAME_SAMPLES",
    "OpenWakeWordEngine",
    "WakeEngine",
    "WakeWord",
    "WakeWordUnavailable",
    "available",
    "build",
    "download",
    "is_custom",
    "is_downloaded",
    "stock_models",
    "triggers_for",
]


def triggers_for(wake) -> list[str]:
    """The configured triggers, or the default if none were named."""
    return list(wake.models) if wake.models else list(DEFAULT_MODELS)


def build(audio) -> WakeWord:
    """The detector described by config, or WakeWordUnavailable saying what's missing."""
    engine = OpenWakeWordEngine(triggers_for(audio.wake))
    return WakeWord(engine, threshold=audio.wake.threshold, sample_rate=audio.sample_rate)
