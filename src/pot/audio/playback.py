"""Speaker output with an instant flush, so barge-in actually feels like an interruption."""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd
import soxr

from pot.audio.devices import default_samplerate, find_device

log = logging.getLogger(__name__)

BYTES_PER_SAMPLE = 2


class Speaker:
    def __init__(self, *, sample_rate: int, device_name: str = "") -> None:
        self.source_rate = sample_rate
        # Audio that has been handed to us but not yet played, kept so echo suppression can be
        # told what left the speaker *when it left*. Writes run ahead of playback (the model sends
        # faster than real time), so tapping at write time would misalign the reference badly.
        self._reference = bytearray()
        self._played_frames = 0
        self._device = find_device(device_name, kind="output")
        self._native_rate = self._pick_rate()
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._stream: sd.RawOutputStream | None = None

    @property
    def description(self) -> str:
        rate = f"{self._native_rate} Hz"
        if self._native_rate != self.source_rate:
            rate += f" upsampled from {self.source_rate} Hz"
        return rate

    @property
    def is_playing(self) -> bool:
        with self._lock:
            return bool(self._buffer)

    def _pick_rate(self) -> int:
        try:
            sd.check_output_settings(device=self._device, samplerate=self.source_rate, channels=1)
            return self.source_rate
        except Exception:
            native = default_samplerate(self._device, kind="output")
            log.info("output device won't do %s Hz, playing at %s Hz", self.source_rate, native)
            return native

    async def __aenter__(self) -> Speaker:
        self._stream = sd.RawOutputStream(
            samplerate=self._native_rate,
            channels=1,
            dtype="int16",
            device=self._device,
            callback=self._on_request,
        )
        self._stream.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None

    def drain_reference(self) -> bytes:
        """Audio that has actually reached the speaker since the last call, at the session rate.

        Aligned to playback rather than to writes, which is what makes echo suppression able to
        line the reference up with what the microphone is hearing right now.
        """
        with self._lock:
            frames, self._played_frames = self._played_frames, 0
            if frames <= 0 or not self._reference:
                return b""
            wanted = int(frames * self.source_rate / self._native_rate) * BYTES_PER_SAMPLE
            wanted = min(wanted, len(self._reference))
            played = bytes(self._reference[:wanted])
            del self._reference[:wanted]
            return played

    def write(self, pcm16: bytes) -> None:
        with self._lock:
            self._reference.extend(pcm16)
        if self._native_rate != self.source_rate:
            samples = np.frombuffer(pcm16, dtype=np.int16)
            pcm16 = soxr.resample(samples, self.source_rate, self._native_rate).tobytes()
        with self._lock:
            self._buffer.extend(pcm16)

    def flush(self) -> None:
        """Drop everything still queued, e.g. when the user interrupts."""
        with self._lock:
            self._buffer.clear()
            # Audio that never played can't have echoed, so it isn't a reference for anything.
            self._reference.clear()
            self._played_frames = 0

    def _on_request(self, outdata, frames: int, time_info, status) -> None:  # PortAudio thread
        if status:
            log.debug("output stream status: %s", status)
        wanted = frames * BYTES_PER_SAMPLE
        with self._lock:
            available = min(wanted, len(self._buffer))
            chunk = bytes(self._buffer[:available])
            del self._buffer[:available]
        outdata[:available] = chunk
        if available < wanted:
            outdata[available:wanted] = b"\x00" * (wanted - available)
        # Count only real audio: the zero padding is silence and echoes nothing.
        self._played_frames += available // BYTES_PER_SAMPLE
