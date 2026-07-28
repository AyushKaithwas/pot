"""Speaker output: two channels mixed into one device, and an instant flush out.

Pot's voice arrives from the network in bursts, so playing each burst the moment it lands leaves the
device starved whenever the next one is late, and a gap mid-word is heard as a click or a rasp
rather than as silence. A short pre-buffer absorbs that at the cost of starting a fraction of a
second later. Flushing on interruption still empties everything immediately, because barge-in has
to feel instant.

Music is the second channel, and it is here rather than on the sound card directly for one reason:
what leaves this speaker is the only thing echo suppression can subtract from the microphone. Music
playing straight out of another process is unremovable, and a device that cannot hear over its own
music cannot be spoken to, which on a voice-first box is the whole thing broken. Mixing it here also
makes ducking a local gain change: instant, and it cannot fail on the network.
"""

from __future__ import annotations

import logging
import threading

import numpy as np
import sounddevice as sd

from pot.audio.devices import find_device, stream_rate
from pot.audio.resample import StreamResampler

log = logging.getLogger(__name__)

BYTES_PER_SAMPLE = 2
# How much of Pot's voice to gather before starting to play. Long enough to ride out normal network
# jitter, short enough that nobody notices the delay before Pot starts talking.
PREBUFFER_MILLISECONDS = 120
# Music arrives in real time and is played in real time, so a backlog is drift rather than jitter.
# Past this, drop the oldest: being a moment behind the stream is worse than a gap nobody hears.
MUSIC_BACKLOG_MILLISECONDS = 400
# What left the speaker recently, kept for echo suppression. Bounded so nothing grows without limit
# when suppression is switched off and nobody is draining it.
REFERENCE_MILLISECONDS = 1000
# How far music drops while Pot is speaking, listening, or ringing.
DUCKED_GAIN = 0.2


class Speaker:
    def __init__(
        self,
        *,
        sample_rate: int,
        device_name: str = "",
        prebuffer_ms: int = PREBUFFER_MILLISECONDS,
    ) -> None:
        self.source_rate = sample_rate
        self._device = find_device(device_name, kind="output")
        self._native_rate = self._pick_rate()
        self._voice = bytearray()
        self._music = bytearray()
        # What actually reached the device, at the device's rate. Taken from the mix rather than
        # from writes because writes run ahead of playback, and a reference that isn't lined up
        # with what the microphone is hearing is worse than none.
        self._reference = bytearray()
        self._music_gain = 1.0
        self._lock = threading.Lock()
        self._stream: sd.RawOutputStream | None = None
        self._voice_resampler: StreamResampler | None = None
        self._music_resampler: StreamResampler | None = None
        self._reference_resampler: StreamResampler | None = None
        # A buffer that runs dry mid-sentence is heard as a click or a rasp, so count it: the
        # number is the difference between "the model sounds like that" and "we broke the audio".
        self.underruns = 0
        self.music_dropped = 0
        self._streaming = False
        # Set once a reply is complete, so the buffer emptying is understood as the end of it
        # rather than as the network failing to keep up.
        self._draining = False
        self._prebuffer_bytes = self._bytes_for(prebuffer_ms)
        self._music_cap = self._bytes_for(MUSIC_BACKLOG_MILLISECONDS)
        self._reference_cap = self._bytes_for(REFERENCE_MILLISECONDS)

    def _bytes_for(self, milliseconds: int) -> int:
        return int(self._native_rate * milliseconds / 1000) * BYTES_PER_SAMPLE

    @property
    def description(self) -> str:
        rate = f"{self._native_rate} Hz"
        if self._native_rate != self.source_rate:
            rate += f" upsampled from {self.source_rate} Hz"
        return rate

    @property
    def is_playing(self) -> bool:
        """Whether Pot itself is still talking. Music playing underneath doesn't count."""
        with self._lock:
            return bool(self._voice)

    @property
    def music_gain(self) -> float:
        return self._music_gain

    @property
    def music_queued(self) -> int:
        """Music still waiting for the device, in bytes."""
        with self._lock:
            return len(self._music)

    def duck_music(self, ducked: bool) -> None:
        """Push music down, or put it back. Local, so it takes effect on the next audio block."""
        self._music_gain = DUCKED_GAIN if ducked else 1.0

    def _pick_rate(self) -> int:
        rate = stream_rate(self._device, kind="output", wanted=self.source_rate)
        if rate != self.source_rate:
            log.info("playing at the device's own %s Hz, upsampled from %s", rate, self.source_rate)
        return rate

    async def __aenter__(self) -> Speaker:
        if self._native_rate != self.source_rate:
            self._voice_resampler = StreamResampler(self.source_rate, self._native_rate)
            self._music_resampler = StreamResampler(self.source_rate, self._native_rate)
            self._reference_resampler = StreamResampler(self._native_rate, self.source_rate)
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
        """Everything that has left the speaker since the last call, at the session rate.

        This is voice, cues, chimes and music together, which is exactly what the microphone is
        hearing and therefore exactly what suppression needs to subtract.
        """
        with self._lock:
            if not self._reference:
                return b""
            played = bytes(self._reference)
            self._reference.clear()
        if self._reference_resampler is None:
            return played
        return self._reference_resampler.feed(played)

    def write(self, pcm16: bytes) -> None:
        """Pot's own voice, or a cue. Queued, so it plays in order and never overlaps itself."""
        if self._voice_resampler is not None:
            pcm16 = self._voice_resampler.feed(pcm16)
            if not pcm16:
                return
        with self._lock:
            self._voice.extend(pcm16)
            self._draining = False

    def write_music(self, pcm16: bytes) -> None:
        """Music, mixed under whatever Pot is saying rather than queued behind it."""
        if self._music_resampler is not None:
            pcm16 = self._music_resampler.feed(pcm16)
            if not pcm16:
                return
        with self._lock:
            self._music.extend(pcm16)
            excess = len(self._music) - self._music_cap
            if excess > 0:
                del self._music[:excess]
                self.music_dropped += excess // BYTES_PER_SAMPLE

    def finish(self) -> None:
        """No more voice is coming for now: push out what the resampler is still holding.

        A streaming resampler keeps a few tens of milliseconds in hand. Mid-reply that's invisible,
        but at the end of one it's the last syllable, held back until Pot next says something.
        """
        tail = self._voice_resampler.close() if self._voice_resampler is not None else b""
        with self._lock:
            self._voice.extend(tail)
            self._draining = True

    def flush(self) -> None:
        """Drop Pot's queued speech, e.g. when the user interrupts. Music carries on."""
        if self._voice_resampler is not None:
            self._voice_resampler.close()
        with self._lock:
            self._voice.clear()
            self._streaming = False
            self._draining = True

    def stop_music(self) -> None:
        with self._lock:
            self._music.clear()

    def _on_request(self, outdata, frames: int, time_info, status) -> None:  # PortAudio thread
        if status:
            log.debug("output stream status: %s", status)
        wanted = frames * BYTES_PER_SAMPLE
        with self._lock:
            voice = self._take_voice(wanted)
            music = self._take(self._music, wanted)
            gain = self._music_gain
        outdata[:wanted] = _mix(voice, music, wanted, gain)
        with self._lock:
            # Pot's own voice only, not the mix. Music belongs here in principle, but the
            # suppressor subtracts per band, and against continuous music that removes the person
            # talking along with it: measured, it goes from hearing the wake word at equal loudness
            # to never hearing it at all. Cancelling music needs an adaptive filter, so until there
            # is one, the reference stays what the suppressor can actually handle.
            self._reference.extend(voice.ljust(wanted, b"\x00"))
            excess = len(self._reference) - self._reference_cap
            if excess > 0:
                del self._reference[:excess]

    def _take_voice(self, wanted: int) -> bytes:
        """Pot's voice for this block, respecting the pre-buffer. Caller holds the lock."""
        if not self._streaming:
            # Wait for enough audio to ride out the next late packet before starting.
            if len(self._voice) < self._prebuffer_bytes:
                return b""
            self._streaming = True
        taken = self._take(self._voice, wanted)
        if len(taken) < wanted:
            # Re-arm either way, so the next burst pre-buffers before it starts.
            self._streaming = False
            if not self._draining:
                # Ran dry mid-utterance rather than at the end of one: that's the click.
                self.underruns += 1
        return taken

    @staticmethod
    def _take(buffer: bytearray, wanted: int) -> bytes:
        available = min(wanted, len(buffer))
        taken = bytes(buffer[:available])
        del buffer[:available]
        return taken


def _mix(voice: bytes, music: bytes, wanted: int, music_gain: float) -> bytes:
    """Sum the two channels into one block, padded to length and clipped rather than wrapped.

    Either channel may be short: music is short at the start and end of a track, voice whenever
    Pot isn't talking, and both when the room is silent.
    """
    if not music or music_gain <= 0:
        return voice.ljust(wanted, b"\x00")
    together = np.zeros(wanted // BYTES_PER_SAMPLE, dtype=np.int32)
    if voice:
        speech = np.frombuffer(voice, dtype=np.int16)
        together[: len(speech)] += speech
    quiet = np.frombuffer(music, dtype=np.int16) * music_gain
    together[: len(quiet)] += quiet.astype(np.int32)
    return np.clip(together, -32768, 32767).astype(np.int16).tobytes()
