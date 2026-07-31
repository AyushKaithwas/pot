"""Pot as a Spotify Connect speaker.

This is the Echo model: the device itself runs a Spotify client and audio streams to it
directly, while Pot only sends control commands. librespot is what provides that client here.

librespot's audio comes back through a pipe rather than going to the sound card itself, so it
passes through Pot's own mixer. That is what lets Pot hear you over its own music: the mixer knows
what left the speaker, so echo suppression can subtract it from what the microphone picks up. Music
straight out of another process is unremovable, and a voice-first box that goes deaf whenever it
plays something is broken. Piping is also the same on a Mac and on a Pi, so there is no audio
backend to choose or get wrong.

Pot supervises the process rather than bundling it. librespot is an unofficial client, so it
stays optional and off by default: everything still works by controlling the user's phone or
desktop, which is the path that's actually within Spotify's terms.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import sys

import numpy as np

from pot.audio.resample import StreamResampler
from pot.config import Settings

log = logging.getLogger(__name__)

RESTART_DELAY_SECONDS = 3.0
STOP_GRACE_SECONDS = 5.0
# What librespot writes down the pipe: signed 16-bit little-endian, CD rate, stereo.
PIPE_RATE = 44100
PIPE_CHANNELS = 2
BYTES_PER_SAMPLE = 2
FRAME_BYTES = BYTES_PER_SAMPLE * PIPE_CHANNELS
# One read is about 45ms of audio: small enough to stay responsive, large enough to be cheap.
READ_BYTES = 8192

INSTALL_HINTS = {
    "darwin": "brew install librespot",
    "linux": "sudo apt install librespot   # or see github.com/librespot-org/librespot",
}


def install_hint() -> str:
    return INSTALL_HINTS.get(sys.platform, "see github.com/librespot-org/librespot")


def _to_mono(pcm16: bytes) -> bytes:
    """Fold stereo down to the one channel Pot's mixer plays, averaging rather than dropping a side.

    Taking only the left channel would silence anything panned hard right, which some mixes do.
    """
    frames = np.frombuffer(pcm16, dtype=np.int16).reshape(-1, PIPE_CHANNELS)
    return frames.mean(axis=1).astype(np.int16).tobytes()


class ManagedSpeaker:
    """Runs librespot for as long as Pot is running, restarting it if it dies."""

    def __init__(self, settings: Settings, speaker: object | None = None) -> None:
        self._config = settings.music.speaker
        self._speaker = speaker
        self._process: asyncio.subprocess.Process | None = None
        self._supervisor: asyncio.Task | None = None

    @property
    def enabled(self) -> bool:
        return self._config.enabled

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    def binary_path(self) -> str | None:
        return shutil.which(self._config.binary)

    async def start(self) -> None:
        if not self.enabled or self.running:
            return
        if self.binary_path() is None:
            log.warning(
                "Pot's speaker is enabled but %s isn't installed (%s)",
                self._config.binary,
                install_hint(),
            )
            return
        self._supervisor = asyncio.create_task(self._supervise(), name="librespot")

    async def stop(self) -> None:
        supervisor, self._supervisor = self._supervisor, None
        if supervisor is not None:
            supervisor.cancel()
            await asyncio.gather(supervisor, return_exceptions=True)
        await self._terminate()
        self._silence()

    def command(self) -> list[str]:
        return [
            self._config.binary,
            "--name",
            self._config.name,
            # Through Pot's mixer, not straight to the sound card: see the note at the top.
            "--backend",
            "pipe",
            "--format",
            "S16",
            "--bitrate",
            str(self._config.bitrate),
            "--cache",
            str(self._config.cache_path()),
        ]

    async def _supervise(self) -> None:
        while True:
            try:
                self._process = await asyncio.create_subprocess_exec(
                    *self.command(),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError as exc:
                log.warning("could not start %s: %s", self._config.binary, exc)
                return
            log.info("Pot's Spotify speaker %r is up", self._config.name)
            # stderr must be drained or librespot eventually blocks writing to a full pipe and
            # the music just stops; its complaints are also the only diagnostics it has.
            chatter = asyncio.create_task(self._log_stderr(self._process.stderr), name="librespot-log")
            if self._process.stdout is not None:
                await self._play_from(self._process.stdout)
            await asyncio.gather(chatter, return_exceptions=True)
            code = await self._process.wait()
            self._silence()
            if code == 0:
                return
            log.warning("librespot exited with %s, restarting", code)
            await asyncio.sleep(RESTART_DELAY_SECONDS)

    async def _play_from(self, stdout: asyncio.StreamReader) -> None:
        """Feed librespot's audio into Pot's mixer until the stream ends."""
        if self._speaker is None:
            return
        resampler = StreamResampler(PIPE_RATE, self._speaker.source_rate)
        partial = b""
        while True:
            chunk = await stdout.read(READ_BYTES)
            if not chunk:
                return
            partial += chunk
            whole = len(partial) - len(partial) % FRAME_BYTES
            if not whole:
                continue
            mono = _to_mono(partial[:whole])
            partial = partial[whole:]
            resampled = resampler.feed(mono)
            if resampled:
                self._speaker.write_music(resampled)

    async def _log_stderr(self, stream: asyncio.StreamReader | None) -> None:
        if stream is None:
            return
        async for raw in stream:
            log.debug("librespot: %s", raw.decode(errors="replace").rstrip())

    def _silence(self) -> None:
        """Drop whatever is still queued, so a restart doesn't replay the last second."""
        if self._speaker is not None:
            self._speaker.stop_music()

    async def _terminate(self) -> None:
        process, self._process = self._process, None
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=STOP_GRACE_SECONDS)
        except TimeoutError:
            process.kill()
            await process.wait()
