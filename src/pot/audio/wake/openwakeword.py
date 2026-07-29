"""openWakeWord: the default, because it's free to ship and needs no signup.

Its stock models are trained on synthetic speech, which is why they can be unreliable with a real
voice; measured across three voices, hey_mycroft fired 9/9 and hey_jarvis only 6/9. Training a
model on your own voice is the fix that keeps this open source.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

import numpy as np

from pot.audio.wake.base import WakeWordUnavailable

log = logging.getLogger(__name__)

ENGINE = "openwakeword"
FRAME_SAMPLES = 1280  # 80 ms at 16 kHz, the window openWakeWord's models expect
INSTALL_HINT = "install the wake word extra: uv sync --extra wakeword"


def _quieten_onnxruntime() -> None:
    """Stop onnxruntime warning about GPUs on a machine that has none.

    It hunts for DRM devices at session creation and logs two warnings when it finds none, which on
    a headless Pi means two lines of noise in the journal every time Pot starts.
    """
    try:
        import onnxruntime

        onnxruntime.set_default_logger_severity(3)  # errors only
    except Exception:  # pragma: no cover - onnxruntime is optional
        log.debug("could not quieten onnxruntime", exc_info=True)


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


def is_custom(model: str) -> bool:
    """A trained model is given as a path to an .onnx file rather than a stock name."""
    return model.endswith(".onnx")


def feature_files() -> list[Path]:
    """The shared melspectrogram and embedding models every wake word needs."""
    import openwakeword

    return [
        Path(feature["model_path"]).with_suffix(".onnx") for feature in openwakeword.FEATURE_MODELS.values()
    ]


def model_files(model: str) -> list[Path]:
    """The ONNX files a given wake word needs: its own, plus the shared feature extractors."""
    import openwakeword

    if is_custom(model):
        return [Path(model).expanduser(), *feature_files()]

    entry = openwakeword.MODELS.get(model)
    if entry is None:
        known = ", ".join(sorted(openwakeword.MODELS))
        raise WakeWordUnavailable(f"unknown wake word {model!r} (available: {known})")
    # The registry points at the tflite variants; the ONNX ones sit alongside.
    return [Path(entry["model_path"]).with_suffix(".onnx"), *feature_files()]


def is_downloaded(*models: str) -> bool:
    if not available():
        return False
    try:
        return all(path.exists() for model in models for path in model_files(model))
    except WakeWordUnavailable:
        return False


def download(*models: str) -> None:
    """Fetch the stock model files, about 1 MB each. Needs network, once.

    Custom models are yours: nothing to download, and nothing is uploaded to check them.
    """
    from openwakeword.utils import download_models

    for model in models:
        if is_custom(model):
            continue
        log.info("downloading wake word model %s", model)
        download_models([model])


class OpenWakeWordEngine:
    """Runs one or more stock or custom openWakeWord models over the same frames."""

    name = ENGINE

    def __init__(self, models: Sequence[str] | str = ("hey_mycroft",), *, download_if_missing: bool = True):
        if not available():
            raise WakeWordUnavailable(f"openWakeWord is not installed. {INSTALL_HINT}")
        _quieten_onnxruntime()
        from openwakeword.model import Model

        sources = [models] if isinstance(models, str) else list(models)
        if not sources:
            raise WakeWordUnavailable("no wake word configured")

        missing = [name for name in sources if not is_downloaded(name)]
        if missing:
            absent_files = [name for name in missing if is_custom(name)]
            if absent_files:
                raise WakeWordUnavailable(f"wake word file(s) not found: {', '.join(absent_files)}")
            if not download_if_missing:
                raise WakeWordUnavailable(f"wake word model(s) not downloaded: {', '.join(missing)}")
            download(*missing)

        # Predictions come back keyed by file stem, which for a stock model is just its name.
        self._names = [Path(name).stem if is_custom(name) else name for name in sources]
        # A copy, because Model rewrites the list it's given into resolved file paths in place.
        self._model = Model(
            wakeword_models=[str(Path(name).expanduser()) if is_custom(name) else name for name in sources],
            inference_framework="onnx",
        )

    @property
    def names(self) -> list[str]:
        return list(self._names)

    @property
    def frame_samples(self) -> int:
        return FRAME_SAMPLES

    def scores(self, frame: np.ndarray) -> dict[str, float]:
        raw = self._model.predict(frame)
        return {name: float(raw.get(name, 0.0)) for name in self._names}

    def reset(self) -> None:
        reset = getattr(self._model, "reset", None)
        if reset is not None:
            reset()
