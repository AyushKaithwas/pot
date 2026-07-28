"""Removing Pot's own voice from what the microphone hears.

A speaker and a microphone in the same box mean Pot hears itself. While it's mid-sentence the mic
gate already stops that audio reaching OpenAI, but the wake word detector has to keep listening or
you could never interrupt, and what it hears is mostly Pot.

We know exactly what went to the speaker, so the echo is predictable: this keeps a short history of
playback, works out how long the round trip takes by correlating envelopes, and subtracts the
delayed playback spectrum from the microphone spectrum. That's echo *suppression* rather than true
cancellation. It leaves artefacts a human would notice, which is fine, because it's only applied to
the detector's copy of the audio and never to what Pot sends to the model.

Music counts as playback too, as long as it goes through Pot's own mixer, which is why librespot
is piped through Pot rather than left to reach the sound card itself. Music playing on a phone, a
desktop app or another speaker has no reference and cannot be removed, so Pot has to compete with
it acoustically and generally loses.
"""

from __future__ import annotations

import logging

import numpy as np

log = logging.getLogger(__name__)

BLOCK_MILLISECONDS = 10
# How far back to look for the echo. Speaker output, air, and mic capture together are well under
# this on any normal device, and a longer search costs both memory and false alignments.
MAX_DELAY_MILLISECONDS = 320
# How much of the estimated echo to remove, and how much of the original to always keep. Removing
# everything sounds like a broken telephone and hurts detection more than the echo does.
SUBTRACT_STRENGTH = 2.0
SPECTRAL_FLOOR = 0.05
# A speaker, a room and a microphone don't attenuate every frequency equally, so the reference is
# scaled per band rather than once for the whole spectrum. This is most of the suppression depth.
BANDS = 24
# Envelope correlation needs a bit of history before it means anything.
MIN_BLOCKS_FOR_ALIGNMENT = 40
# Below this a reference block is silence, and subtracting silence can only damage the microphone.
# Roughly -55 dBFS: quiet enough that no real playback lands under it.
SILENT_RMS = 60.0


class EchoSuppressor:
    """Subtracts recent playback from mic audio, in the frequency domain."""

    def __init__(
        self,
        sample_rate: int,
        *,
        strength: float = SUBTRACT_STRENGTH,
        floor: float = SPECTRAL_FLOOR,
        enabled: bool = True,
    ) -> None:
        self.sample_rate = sample_rate
        self.strength = strength
        self.floor = floor
        self.enabled = enabled
        self.block = max(64, int(sample_rate * BLOCK_MILLISECONDS / 1000))
        self._window = np.hanning(self.block).astype(np.float32)
        self._band_edges = np.linspace(0, self.block // 2 + 1, BANDS + 1).astype(int)
        self._max_delay_blocks = max(2, int(MAX_DELAY_MILLISECONDS / BLOCK_MILLISECONDS))
        # History has to cover the whole delay search *plus* the correlation window, or alignment
        # silently never runs and the delay stays at whatever it was initialised to.
        self._max_blocks = self._max_delay_blocks + MIN_BLOCKS_FOR_ALIGNMENT
        # Playback spectra and their loudness, newest last.
        self._reference: list[np.ndarray] = []
        self._reference_energy: list[float] = []
        self._mic_energy: list[float] = []
        self._delay_blocks = 1
        self._pending = bytearray()
        # Mic blocks seen since the speaker last produced anything. Playback and mic audio arrive
        # from different places at different times, so this is what tells us the reference still
        # describes something the microphone could actually be hearing.
        self._blocks_since_playback = 0
        self._silent_energy = SILENT_RMS**2 * self.block
        self.suppressed_blocks = 0

    @property
    def delay_ms(self) -> float:
        """The round trip currently being assumed, useful for diagnostics."""
        return self._delay_blocks * BLOCK_MILLISECONDS

    def playback(self, pcm: bytes) -> None:
        """Tell the suppressor what just went to the speaker."""
        if not self.enabled:
            return
        for block in self._blocks(pcm):
            spectrum = np.abs(np.fft.rfft(block * self._window))
            self._reference.append(spectrum)
            self._reference_energy.append(float(np.dot(block, block)))
            self._blocks_since_playback = 0
            if len(self._reference) > self._max_blocks:
                del self._reference[0]
                del self._reference_energy[0]

    def process(self, pcm: bytes) -> bytes:
        """Clean a chunk of mic audio. Returns it unchanged when there's nothing to subtract."""
        if not self.enabled or not self._reference:
            return pcm
        if self._is_stale():
            self.reset()
            return pcm

        self._pending.extend(pcm)
        usable = len(self._pending) // (self.block * 2) * (self.block * 2)
        if usable == 0:
            return b""
        raw = bytes(self._pending[:usable])
        del self._pending[:usable]

        out = bytearray()
        for block in self._blocks(raw):
            self._track_mic(block)
            out.extend(self._clean(block))
        return bytes(out)

    def reset(self) -> None:
        self._reference.clear()
        self._reference_energy.clear()
        self._mic_energy.clear()
        self._pending.clear()

    def _is_stale(self) -> bool:
        """Has the speaker been quiet for longer than the echo takes to come back?

        If so, whatever is left in the reference has already been and gone, and subtracting it
        would mean removing Pot's last sentence from a silent room. The reference is still
        speech-shaped and the per-band scale is fitted to whatever the mic hears, so it would find
        plenty to remove: the person talking. That's the wake word going deaf after a reply.
        """
        # Against the round trip we actually measured, not the window we searched: the search
        # window is ten times the real delay, and waiting that long means damaging the first
        # third of a second of whatever the user says next.
        return self._blocks_since_playback > self._delay_blocks + 2

    def _blocks(self, pcm: bytes):
        samples = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        for start in range(0, len(samples) - self.block + 1, self.block):
            yield samples[start : start + self.block]

    def _track_mic(self, block: np.ndarray) -> None:
        self._blocks_since_playback += 1
        self._mic_energy.append(float(np.dot(block, block)))
        if len(self._mic_energy) > self._max_blocks:
            del self._mic_energy[0]
        if len(self._mic_energy) >= MIN_BLOCKS_FOR_ALIGNMENT:
            self._delay_blocks = self._estimate_delay()

    def _estimate_delay(self) -> int:
        """Find the lag where mic loudness best follows playback loudness.

        Correlating envelopes rather than waveforms keeps this cheap and immune to the phase mess
        introduced by the speaker, the room and the microphone.
        """
        mic = np.array(self._mic_energy[-MIN_BLOCKS_FOR_ALIGNMENT:], dtype=np.float64)
        reference = np.array(self._reference_energy, dtype=np.float64)
        if mic.std() < 1.0 or reference.std() < 1.0 or len(reference) < len(mic) + 1:
            return self._delay_blocks

        mic = (mic - mic.mean()) / (mic.std() + 1e-9)
        best_lag, best_score = self._delay_blocks, -np.inf
        for lag in range(1, min(self._max_delay_blocks, len(reference) - len(mic)) + 1):
            window = reference[-(len(mic) + lag) : -lag]
            if window.std() < 1.0:
                continue
            window = (window - window.mean()) / (window.std() + 1e-9)
            score = float(np.dot(mic, window))
            if score > best_score:
                best_lag, best_score = lag, score
        return best_lag

    def _clean(self, block: np.ndarray) -> bytes:
        index = len(self._reference) - self._delay_blocks
        if index < 0 or self._is_stale() or self._reference_energy[index] < self._silent_energy:
            # Nothing was coming out of the speaker then, so there is no echo to remove now. The
            # per-band scale is fitted to the microphone, so a silent reference would still find
            # something to subtract, and what it finds is whoever is talking.
            return block.astype(np.int16).tobytes()

        spectrum = np.fft.rfft(block * self._window)
        magnitude = np.abs(spectrum)
        echo = self._reference[index]
        if echo.shape != magnitude.shape:
            return block.astype(np.int16).tobytes()

        # Scale the reference before subtracting, per band: the speaker's volume, the room and the
        # mic's response are all unknown and all frequency-dependent, and only their combined
        # effect matters. One global scale leaves most of the echo behind.
        estimate = np.zeros_like(magnitude)
        for start, end in zip(self._band_edges[:-1], self._band_edges[1:], strict=True):
            if end <= start:
                continue
            band_echo = echo[start:end]
            denominator = float(np.dot(band_echo, band_echo))
            if denominator < 1e-9:
                continue
            scale = float(np.dot(magnitude[start:end], band_echo)) / denominator
            if scale > 0:
                estimate[start:end] = scale * band_echo

        cleaned = magnitude - self.strength * estimate
        floor = self.floor * magnitude
        cleaned = np.maximum(cleaned, floor)
        gain = cleaned / (magnitude + 1e-9)
        self.suppressed_blocks += 1

        # Reuse the original phase: for detection, magnitudes are what matter.
        restored = np.fft.irfft(spectrum * gain, n=self.block)
        # The analysis window is undone rather than overlap-added, which keeps this a per-block
        # operation. Detection tolerates that; a listener would not.
        restored = np.divide(restored, self._window, out=np.zeros_like(restored), where=self._window > 0.05)
        return np.clip(restored, -32768, 32767).astype(np.int16).tobytes()
