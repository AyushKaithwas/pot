"""Microphone capture as an async stream of pcm16 chunks at the model's sample rate.

Two details that matter in practice: not every input device will open at 24 kHz, so we fall back
to the device's native rate and resample; and capture never stops, because wake word detection
needs the audio even when none of it is being sent anywhere.

The gate is about *destination*, not capture. Closed means "don't send this to OpenAI", which is
what keeps an idle room free and stops Pot's own voice from interrupting it. Whoever drains
`chunks()` decides where each chunk goes.
"""

from __future__ import annotations

import asyncio
import logging
import time

import numpy as np
import sounddevice as sd

from pot.audio.devices import find_device, stream_rate
from pot.audio.resample import StreamResampler

log = logging.getLogger(__name__)

BLOCK_MILLISECONDS = 20
QUEUE_MAX_CHUNKS = 100


class MicrophoneGate:
    """Open while the user is talking to Pot, closed while Pot talks back."""

    def __init__(self, *, open_at_start: bool = False, clock=time.monotonic) -> None:
        self._open = open_at_start
        self._muted_until = 0.0
        self._clock = clock
        self.changed = asyncio.Event()

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def is_muted(self) -> bool:
        """Briefly deaf, because what the mic is hearing right now is Pot's own noise.

        An earcon is short and loud and goes out of the same speaker the mic is next to, so it
        comes back as a "user turn" of gibberish that gets transcribed, charged for, and answered.
        """
        return self._clock() < self._muted_until

    def mute_for(self, seconds: float) -> None:
        self._muted_until = max(self._muted_until, self._clock() + seconds)

    def open(self) -> None:
        self._set(True)

    def close(self) -> None:
        self._set(False)

    def toggle(self) -> bool:
        self._set(not self._open)
        return self._open

    def _set(self, value: bool) -> None:
        if value != self._open:
            self._open = value
            self.changed.set()


class MicBoost:
    """Automatic gain for the audio sent to the model, and only that.

    The wake word detector is a one-phrase classifier and fires happily on a quiet mic; open
    speech recognition starves on the same signal, and a mic whose hardware gain tops out at 0 dB
    has nothing more to give. This tracks the recent peak and lifts it toward a healthy level,
    capped so a silent room isn't amplified into hiss. The detector and echo paths see raw audio:
    their thresholds were tuned on it.
    """

    def __init__(self, cap: float = 4.0, *, target: float = 0.25) -> None:
        self.cap = cap
        self._target = target
        self._envelope = 0.0
        self._gain = 1.0

    def apply(self, pcm16: bytes) -> bytes:
        if self.cap <= 1.0 or not pcm16:
            return pcm16
        samples = np.frombuffer(pcm16, dtype=np.int16).astype(np.float32)
        peak = float(np.abs(samples).max()) / 32768.0
        # A slow-decaying envelope, so the gain rides the sentence rather than every syllable.
        self._envelope = max(peak, self._envelope * 0.995)
        wanted = min(self.cap, max(1.0, self._target / max(self._envelope, 0.01)))
        # Slewed, not stepped: a gain that jumps between chunks is audible as pumping.
        self._gain += (wanted - self._gain) * 0.2
        return np.clip(samples * self._gain, -32768, 32767).astype(np.int16).tobytes()


class Microphone:
    def __init__(
        self,
        *,
        sample_rate: int,
        device_name: str = "",
        gate: MicrophoneGate | None = None,
    ) -> None:
        self.target_rate = sample_rate
        self.gate = gate or MicrophoneGate(open_at_start=True)
        self._device = find_device(device_name, kind="input")
        self._native_rate = self._pick_rate()
        self._resampler: StreamResampler | None = None
        self._queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=QUEUE_MAX_CHUNKS)
        self._stream: sd.RawInputStream | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._dropped = 0

    @property
    def description(self) -> str:
        rate = f"{self._native_rate} Hz"
        if self._native_rate != self.target_rate:
            rate += f" resampled to {self.target_rate} Hz"
        return rate

    def _pick_rate(self) -> int:
        rate = stream_rate(self._device, kind="input", wanted=self.target_rate)
        if rate != self.target_rate:
            log.info("capturing at the device's own %s Hz and resampling to %s", rate, self.target_rate)
        return rate

    async def __aenter__(self) -> Microphone:
        self._loop = asyncio.get_running_loop()
        if self._native_rate != self.target_rate:
            self._resampler = StreamResampler(self._native_rate, self.target_rate)
        blocksize = int(self._native_rate * BLOCK_MILLISECONDS / 1000)
        self._stream = sd.RawInputStream(
            samplerate=self._native_rate,
            channels=1,
            dtype="int16",
            blocksize=blocksize,
            device=self._device,
            callback=self._on_audio,
        )
        self._stream.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        if self._dropped:
            log.debug("dropped %s mic chunks while the queue was full", self._dropped)

    def _on_audio(self, indata, frames: int, time_info, status) -> None:  # PortAudio thread
        if status:
            log.debug("input stream status: %s", status)
        if self._loop is None:
            return
        chunk = bytes(indata)
        if self._resampler is not None:
            # The streaming resampler buffers, so some blocks come back empty.
            chunk = self._resampler.feed(chunk)
            if not chunk:
                return
        self._loop.call_soon_threadsafe(self._enqueue, chunk)

    def _enqueue(self, chunk: bytes) -> None:
        try:
            self._queue.put_nowait(chunk)
        except asyncio.QueueFull:
            self._dropped += 1

    async def chunks(self):
        """Yield pcm16 chunks at the target sample rate until cancelled."""
        while True:
            yield await self._queue.get()
