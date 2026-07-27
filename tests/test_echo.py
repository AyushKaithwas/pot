"""Echo suppression, measured rather than asserted.

The number that matters is how much of Pot's own voice is removed from the mic signal, with a
synthetic echo so the answer is deterministic: playback, delayed and attenuated, plus the speech
the microphone is actually meant to hear.
"""

from __future__ import annotations

import numpy as np

from pot.audio.echo import BLOCK_MILLISECONDS, EchoSuppressor

RATE = 24000


def tone(seconds: float, frequency: float, amplitude: float = 8000.0) -> np.ndarray:
    t = np.arange(int(RATE * seconds)) / RATE
    return (np.sin(2 * np.pi * frequency * t) * amplitude).astype(np.int16)


def noisy_speech(seconds: float, amplitude: float = 2000.0) -> np.ndarray:
    """Not speech, but broadband and modulated like it, which is what matters here."""
    rng = np.random.default_rng(7)
    samples = rng.normal(0, amplitude, int(RATE * seconds))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * np.arange(len(samples)) / RATE)
    return (samples * envelope).astype(np.int16)


def energy(samples: np.ndarray) -> float:
    values = samples.astype(np.float64)
    return float(np.dot(values, values)) / max(1, len(values))


def run(suppressor: EchoSuppressor, playback: np.ndarray, mic: np.ndarray) -> np.ndarray:
    """Feed playback and mic in step, the way the audio callbacks do."""
    step = int(RATE * BLOCK_MILLISECONDS / 1000)
    out = bytearray()
    for start in range(0, min(len(playback), len(mic)) - step + 1, step):
        suppressor.playback(playback[start : start + step].tobytes())
        out.extend(suppressor.process(mic[start : start + step].tobytes()))
    return np.frombuffer(bytes(out), dtype=np.int16)


def echo_of(playback: np.ndarray, delay_samples: int, attenuation: float) -> np.ndarray:
    """What the mic hears back: the same audio, later and quieter."""
    delayed = np.concatenate([np.zeros(delay_samples, dtype=np.int16), playback])[: len(playback)]
    return (delayed.astype(np.float32) * attenuation).astype(np.int16)


def test_pots_own_voice_is_reduced():
    playback = tone(2.0, 440)
    mic = echo_of(playback, delay_samples=int(RATE * 0.06), attenuation=0.5)
    cleaned = run(EchoSuppressor(RATE), playback, mic)

    before = energy(mic[: len(cleaned)])
    after = energy(cleaned)
    reduction_db = 10 * np.log10(before / max(after, 1e-9))
    assert reduction_db > 6, f"only removed {reduction_db:.1f} dB of echo"


def test_a_quiet_room_is_left_alone():
    """With nothing playing there's nothing to subtract, so the audio must pass through."""
    suppressor = EchoSuppressor(RATE)
    speech = noisy_speech(0.5)
    passed = np.frombuffer(suppressor.process(speech.tobytes()), dtype=np.int16)
    assert np.array_equal(passed, speech)


def test_speech_survives_while_pot_is_talking():
    """The point is to hear the user over Pot, not to mute everything."""
    playback = tone(2.0, 440)
    speech = noisy_speech(2.0)
    echo = echo_of(playback, int(RATE * 0.06), 0.5).astype(np.int32)
    mic = (echo + speech.astype(np.int32)).astype(np.int16)

    cleaned = run(EchoSuppressor(RATE), playback, mic)
    kept = energy(cleaned) / max(energy(speech[: len(cleaned)]), 1e-9)
    assert kept > 0.15, f"only {kept:.0%} of the speech energy survived"


def bursts(seconds: float, amplitude: float = 9000.0) -> np.ndarray:
    """Loud 100 ms bursts with gaps, so the loudness envelope has an unambiguous shape."""
    rng = np.random.default_rng(3)
    samples = np.zeros(int(RATE * seconds), dtype=np.float32)
    burst = int(RATE * 0.1)
    for start in range(0, len(samples) - burst, int(RATE * 0.35)):
        samples[start : start + burst] = rng.normal(0, amplitude, burst)
    return samples.astype(np.int16)


def test_the_round_trip_delay_is_estimated_not_assumed():
    playback = bursts(4.0)
    delay_ms = 80
    mic = echo_of(playback, int(RATE * delay_ms / 1000), 0.6)

    suppressor = EchoSuppressor(RATE)
    run(suppressor, playback, mic)
    assert abs(suppressor.delay_ms - delay_ms) <= 2 * BLOCK_MILLISECONDS


def test_it_can_be_switched_off():
    playback = tone(1.0, 440)
    mic = echo_of(playback, int(RATE * 0.06), 0.5)
    cleaned = run(EchoSuppressor(RATE, enabled=False), playback, mic)
    assert np.array_equal(cleaned, mic[: len(cleaned)])


def test_output_length_matches_input_once_blocks_fill():
    playback = tone(1.0, 440)
    mic = echo_of(playback, int(RATE * 0.04), 0.5)
    cleaned = run(EchoSuppressor(RATE), playback, mic)
    step = int(RATE * BLOCK_MILLISECONDS / 1000)
    assert abs(len(cleaned) - len(mic)) <= step, "the detector needs a continuous stream"


def test_reset_forgets_the_playback_history():
    suppressor = EchoSuppressor(RATE)
    suppressor.playback(tone(0.2, 440).tobytes())
    suppressor.reset()
    speech = noisy_speech(0.2)
    assert np.array_equal(np.frombuffer(suppressor.process(speech.tobytes()), dtype=np.int16), speech)
