"""Answering "is the audio even working?" on a machine with no screen.

On a Pi over SSH, a silent assistant could be a dead microphone, a muted speaker, the wrong ALSA
device, a missing API key or a network problem. These check the audio parts alone, through the
same Microphone and Speaker the voice loop uses, so a pass here means the pipeline is sound.
"""

from __future__ import annotations

import asyncio
import logging

import numpy as np
import sounddevice as sd

from pot.audio.capture import Microphone
from pot.audio.playback import Speaker
from pot.config import AudioConfig

log = logging.getLogger(__name__)

# Below this the mic is probably not picking anything up; above it, likely clipping.
QUIET_PEAK = 0.02
LOUD_PEAK = 0.95


def device_rows() -> list[dict]:
    """Every audio device, with what it can do. Names here are what config matches on."""
    rows = []
    for index, device in enumerate(sd.query_devices()):
        rows.append(
            {
                "index": index,
                "name": device["name"],
                "inputs": device["max_input_channels"],
                "outputs": device["max_output_channels"],
                "rate": int(device["default_samplerate"]),
            }
        )
    return rows


def defaults() -> dict[str, str]:
    out = {}
    for kind in ("input", "output"):
        try:
            info = sd.query_devices(None, kind)
            out[kind] = f"{info['name']} @ {int(info['default_samplerate'])} Hz"
        except Exception as exc:  # no device at all is a normal answer on a fresh Pi
            out[kind] = f"unavailable ({exc})"
    return out


def peak_level(pcm: bytes) -> float:
    """Loudest sample as a fraction of full scale."""
    if not pcm:
        return 0.0
    samples = np.frombuffer(pcm, dtype=np.int16)
    return float(np.max(np.abs(samples))) / 32768.0


def describe_level(peak: float) -> str:
    if peak < QUIET_PEAK:
        return f"peak {peak:.1%} — nothing heard. Wrong input device, or the mic is muted?"
    if peak > LOUD_PEAK:
        return f"peak {peak:.1%} — clipping. Turn the input gain down."
    return f"peak {peak:.1%} — healthy"


async def record(audio: AudioConfig, seconds: float) -> bytes:
    """Capture through the same path the voice loop uses."""
    microphone = Microphone(sample_rate=audio.sample_rate, device_name=audio.input_device)
    collected = bytearray()
    async with microphone:
        deadline = asyncio.get_running_loop().time() + seconds
        async for chunk in microphone.chunks():
            collected.extend(chunk)
            if asyncio.get_running_loop().time() >= deadline:
                break
    return bytes(collected)


async def score_recording(models: list[str], pcm: bytes, sample_rate: int) -> dict[str, dict[str, float]]:
    """Run one recording past several wake models and report peak and median frame scores.

    Comparing models on *your* voice in *your* room is the only comparison that matters: the stock
    models are trained on synthetic speech, so which one hears you is largely luck of the draw.
    """
    import numpy as np

    from pot.audio.wake import OpenWakeWordEngine, WakeWord

    results: dict[str, dict[str, float]] = {}
    for model in models:
        detector = OpenWakeWordEngine([model])
        # Threshold of 0.99 so nothing fires; we only want the raw scores.
        wake = WakeWord(detector, threshold=0.99, sample_rate=sample_rate)
        scores: list[float] = []
        step = detector.frame_samples * 2
        buffer = bytearray()
        chunk_bytes = int(sample_rate * 0.02) * 2
        for index in range(0, len(pcm), chunk_bytes):
            piece = pcm[index : index + chunk_bytes]
            resampled = piece
            if wake._resampler is not None:
                samples = np.frombuffer(piece, dtype=np.int16)
                resampled = wake._resampler.resample_chunk(samples).tobytes()
            buffer.extend(resampled)
            while len(buffer) >= step:
                frame = np.frombuffer(bytes(buffer[:step]), dtype=np.int16)
                del buffer[:step]
                scores.append(max(wake.scores(frame).values(), default=0.0))
        array = np.array(scores) if scores else np.zeros(1)
        results[model] = {
            "peak": float(array.max()),
            "median": float(np.median(array)),
            "frames_over_half": float((array >= 0.5).sum()),
        }
    return results


async def play(audio: AudioConfig, pcm: bytes) -> None:
    speaker = Speaker(
        sample_rate=audio.sample_rate,
        device_name=audio.output_device,
        prebuffer_ms=audio.playback_buffer_ms,
    )
    async with speaker:
        speaker.write(pcm)
        while speaker.is_playing:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.2)  # let the tail actually leave the speaker
